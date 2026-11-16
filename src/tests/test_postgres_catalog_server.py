"""Catalog shaping and the in-process MCP server, with a fake session."""

import asyncio
import logging
from unittest.mock import patch

import pytest
from psycopg2 import sql as pgsql

from agents.mcp_servers import database_server as server_module
from database_access import guard, operations
from database_access.engines import postgres as catalog
from database_access.guard import (
    MASK,
    DatabaseAccessError,
    DatabaseTarget,
    QueryResult,
)


def _target(alias="principal", schemas=()) -> DatabaseTarget:
    return DatabaseTarget(
        alias=alias,
        host="db.example.com",
        port=5432,
        database="app",
        username="estack_reader",
        password="s3cr3t-value",
        schemas=schemas,
    )


class FakeSession:
    def __init__(self, answers=None, fetched=None):
        self.answers = answers or {}
        self.fetched = fetched
        self.calls = []
        self.fetch_options = []

    def records(self, query, params=None, max_rows=1000):
        self.calls.append((query, params, max_rows))
        answer = self.answers.get(query, [])
        return [dict(row) for row in (answer(params) if callable(answer) else answer)]

    def one(self, query, params=None):
        rows = self.records(query, params, max_rows=1)
        return rows[0] if rows else None

    def fetch(self, query, params=None, max_rows=200, resolve_sources=False):
        self.calls.append((query, params, max_rows))
        self.fetch_options.append(resolve_sources)
        return self.fetched


RELATION = {"oid": 42, "schema": "public", "name": "pedido", "kind": "r"}


@pytest.fixture(autouse=True)
def _clear_caches():
    operations._catalog_cache.clear()
    guard._posture_cache.clear()
    yield
    operations._catalog_cache.clear()
    guard._posture_cache.clear()


class TestOverview:
    def test_merges_routine_counts_into_schemas(self):
        session = FakeSession(
            {
                catalog.OVERVIEW_SQL: [{"database": "app", "server_version": "16"}],
                catalog.SCHEMAS_SQL: [{"schema": "public", "tables": 3}, {"schema": "audit", "tables": 1}],
                catalog.ROUTINE_COUNT_SQL: [{"schema": "public", "routines": 5}],
                catalog.EXTENSIONS_SQL: [{"name": "uuid-ossp", "version": "1.1"}],
            }
        )

        overview = catalog.database_overview(session, None)

        assert overview["database"] == "app"
        assert overview["schemas"] == [
            {"schema": "public", "tables": 3, "routines": 5},
            {"schema": "audit", "tables": 1, "routines": 0},
        ]
        assert overview["extensions"][0]["name"] == "uuid-ossp"

    def test_passes_the_schema_allowlist(self):
        session = FakeSession()

        catalog.database_overview(session, ["vendas"])

        assert all(
            params == {"schemas": ["vendas"]}
            for query, params, _ in session.calls
            if query in (catalog.SCHEMAS_SQL, catalog.ROUTINE_COUNT_SQL)
        )


class TestListings:
    def test_tables_get_readable_kinds_and_a_cap(self):
        rows = [{"schema": "public", "name": f"t{i}", "kind": "m"} for i in range(catalog.MAX_LISTED_OBJECTS + 1)]
        session = FakeSession({catalog.LIST_TABLES_SQL: rows})

        listed = catalog.list_tables(session, None, None, None)

        assert listed["items"][0]["kind"] == "materialized view"
        assert listed["count"] == catalog.MAX_LISTED_OBJECTS
        assert listed["truncated"] is True

    def test_name_filter_is_escaped_for_like(self):
        session = FakeSession()

        catalog.list_tables(session, None, "public", "item_%")

        params = session.calls[0][1]
        assert params["pattern"] == "%item\\_\\%%"
        assert params["schema"] == "public"

    def test_routines_get_readable_kind_and_volatility(self):
        session = FakeSession(
            {catalog.LIST_ROUTINES_SQL: [{"name": "fn", "kind": "p", "volatility": "v"}]}
        )

        listed = catalog.list_routines(session, None, None, None)

        assert listed["items"] == [{"name": "fn", "kind": "procedure", "volatility": "volatile"}]


class TestResolveRelation:
    def test_splits_schema_qualified_names(self):
        session = FakeSession({catalog.RESOLVE_RELATION_SQL: [RELATION]})

        catalog.resolve_relation(session, None, None, "public.pedido")

        assert session.calls[0][1]["schema"] == "public"
        assert session.calls[0][1]["table"] == "pedido"

    def test_missing_table_points_to_list_tables(self):
        session = FakeSession()

        with pytest.raises(DatabaseAccessError, match="list_tables"):
            catalog.resolve_relation(session, None, None, "nada")

    def test_ambiguous_name_outside_public_asks_for_the_schema(self):
        session = FakeSession(
            {
                catalog.RESOLVE_RELATION_SQL: [
                    {**RELATION, "schema": "a"},
                    {**RELATION, "schema": "b"},
                ]
            }
        )

        with pytest.raises(DatabaseAccessError, match="mais de um schema"):
            catalog.resolve_relation(session, None, None, "pedido")

    def test_public_wins_an_ambiguous_name(self):
        session = FakeSession(
            {catalog.RESOLVE_RELATION_SQL: [RELATION, {**RELATION, "schema": "b"}]}
        )

        assert catalog.resolve_relation(session, None, None, "pedido")["schema"] == "public"


class TestDescribeTable:
    def _session(self, kind="r"):
        return FakeSession(
            {
                catalog.RESOLVE_RELATION_SQL: [{**RELATION, "kind": kind}],
                catalog.RELATION_INFO_SQL: [{"estimated_rows": 10, "comment": None}],
                catalog.COLUMNS_SQL: [
                    {"name": "id", "type": "integer", "readable": False}
                ],
                catalog.CONSTRAINTS_SQL: [{"name": "pedido_pkey", "kind": "p", "definition": "PRIMARY KEY (id)"}],
                catalog.TRIGGERS_SQL: [{"name": "trg", "state": "D"}],
                catalog.POLICIES_SQL: [{"name": "pol", "command": "r"}],
                catalog.VIEW_DEFINITION_SQL: [{"definition": "SELECT 1"}],
                catalog.PARTITIONS_SQL: [{"partition": "pedido_2026", "bound": "FOR VALUES ..."}],
            }
        )

    def test_labels_codes_and_drops_empty_sections(self):
        described = catalog.describe_table(self._session(), None, None, "pedido")

        assert described["table"] == "public.pedido"
        assert described["kind"] == "table"
        assert described["columns"][0]["readable"] is False
        assert described["constraints"][0]["kind"] == "primary key"
        assert described["triggers"][0]["state"] == "disabled"
        assert described["policies"][0]["command"] == "SELECT"
        assert "referenced_by" not in described
        assert "comment" not in described
        assert "view_definition" not in described

    def test_views_include_their_definition(self):
        described = catalog.describe_table(self._session("v"), None, None, "pedido")

        assert described["view_definition"] == "SELECT 1"

    def test_partitioned_tables_list_partitions(self):
        described = catalog.describe_table(self._session("p"), None, None, "pedido")

        assert described["partitions"][0]["partition"] == "pedido_2026"


class TestRelationships:
    def test_translates_actions_and_cardinality(self):
        session = FakeSession(
            {
                catalog.RELATIONSHIPS_SQL: [
                    {
                        "name": "fk_item_pedido",
                        "from_table": "public.item",
                        "to_table": "public.pedido",
                        "on_delete": "c",
                        "on_update": "a",
                        "from_is_unique": False,
                        "from_is_nullable": False,
                    },
                    {
                        "name": "fk_perfil_usuario",
                        "from_table": "public.perfil",
                        "to_table": "public.usuario",
                        "on_delete": "n",
                        "on_update": "r",
                        "from_is_unique": True,
                        "from_is_nullable": True,
                    },
                ]
            }
        )

        items = catalog.list_relationships(session, None, None)["items"]

        assert items[0]["on_delete"] == "cascade"
        assert items[0]["cardinality"] == "many-to-one"
        assert items[0]["required"] is True
        assert items[1]["cardinality"] == "one-to-one"
        assert items[1]["required"] is False
        assert "from_is_unique" not in items[0]

    def test_reminds_that_links_may_live_only_in_the_application(self):
        payload = catalog.list_relationships(FakeSession(), None, None)

        assert "aplicação" in payload["tables_without_relationships_hint"]


class TestRules:
    def test_truncates_trigger_source_and_hides_empty_sections(self):
        session = FakeSession(
            {
                catalog.TRIGGER_RULES_SQL: [
                    {"name": "trg", "state": "O", "function_source": "x" * (catalog.MAX_FUNCTION_SOURCE_CHARS + 10)}
                ],
                catalog.ENUMS_SQL: [{"name": "public.status", "values": ["aberto", "fechado"]}],
            }
        )

        rules = catalog.list_rules(session, None, None)

        assert rules["triggers"][0]["state"] == "enabled"
        assert "+10 caracteres omitidos" in rules["triggers"][0]["function_source"]
        assert rules["enums"][0]["values"] == ["aberto", "fechado"]
        assert "domains" not in rules
        assert "código da aplicação" in rules["note"]

    def test_table_filter_skips_database_wide_types(self):
        session = FakeSession()

        catalog.list_rules(session, None, "pedido")

        queried = {query for query, _, _ in session.calls}
        assert catalog.ENUMS_SQL not in queried
        assert catalog.DOMAINS_SQL not in queried


class TestDefinitions:
    def test_returns_routines_and_views(self):
        session = FakeSession(
            {
                catalog.ROUTINE_DEFINITION_SQL: [{"name": "public.fn", "kind": "f", "definition": "BEGIN END"}],
                catalog.VIEW_DEFINITIONS_SQL: [{"name": "public.vw", "kind": "m", "definition": "SELECT 1"}],
            }
        )

        found = catalog.get_definition(session, None, "public.fn", None)["definitions"]

        kinds = [item["kind"] for item in found]
        assert kinds == ["function", "materialized view"]
        assert session.calls[0][1]["schema"] == "public"

    def test_unknown_name_is_explained(self):
        session = FakeSession()

        with pytest.raises(DatabaseAccessError, match="list_routines"):
            catalog.get_definition(session, None, "nada", None)


class TestColumnProfile:
    def test_masks_common_values_of_sensitive_columns(self):
        session = FakeSession(
            {
                catalog.RESOLVE_RELATION_SQL: [RELATION],
                catalog.COLUMN_STATS_SQL: [
                    {
                        "column": "email",
                        "null_frac": 0.1,
                        "n_distinct": -1.0,
                        "avg_width": 20,
                        "correlation": 0.2,
                        "most_common_values": ["a@b.com"],
                        "most_common_freqs": [0.5],
                        "histogram_min": "a@a.com",
                        "histogram_max": "z@z.com",
                    },
                    {
                        "column": "status",
                        "null_frac": 0.0,
                        "n_distinct": 3,
                        "avg_width": 6,
                        "correlation": None,
                        "most_common_values": ["aberto", "fechado"],
                        "most_common_freqs": [0.7, 0.3],
                        "histogram_min": None,
                        "histogram_max": None,
                    },
                ],
            }
        )

        columns = catalog.column_profile(session, None, None, "pedido")["columns"]

        assert columns[0]["most_common"] == [{"value": MASK, "frequency": 0.5}]
        assert columns[0]["approx_min"] == MASK
        assert columns[0]["distinct"] == "100.0% das linhas"
        assert columns[1]["distinct"] == 3
        assert columns[1]["most_common"][0] == {"value": "aberto", "frequency": 0.7}

    def test_missing_statistics_are_explained(self):
        session = FakeSession({catalog.RESOLVE_RELATION_SQL: [RELATION]})

        profile = catalog.column_profile(session, None, None, "pedido")

        assert "ANALYZE" in profile["note"]


class TestSampleRows:
    def test_quotes_identifiers_caps_the_limit_and_masks(self):
        session = FakeSession(
            {
                catalog.RESOLVE_RELATION_SQL: [RELATION],
                catalog.COLUMNS_SQL: [
                    {"name": "id", "readable": True},
                    {"name": "cpf", "readable": True},
                    {"name": "email", "readable": False},
                ],
            },
            fetched=QueryResult(columns=["id", "cpf"], rows=[[1, "123.456.789-00"]]),
        )

        sampled = catalog.sample_rows(session, None, None, "pedido", limit=500)

        query, _, max_rows = session.calls[-1]
        assert isinstance(query, pgsql.Composed)
        selected_columns = query.seq[1]
        assert [part.strings[0] for part in selected_columns.seq if isinstance(part, pgsql.Identifier)] == [
            "id",
            "cpf",
        ]
        assert max_rows == guard.MAX_SAMPLE_ROWS
        assert sampled["rows"] == [[1, MASK]]
        assert sampled["table"] == "public.pedido"
        assert sampled["omitted_columns"] == ["email"]

    def test_returns_note_without_fetching_when_no_column_is_readable(self):
        session = FakeSession(
            {
                catalog.RESOLVE_RELATION_SQL: [RELATION],
                catalog.COLUMNS_SQL: [
                    {"name": "id", "readable": False},
                    {"name": "email", "readable": False},
                ],
            }
        )

        sampled = catalog.sample_rows(session, None, None, "pedido")

        assert sampled == {
            "table": "public.pedido",
            "note": "O usuário não tem SELECT em nenhuma coluna desta tabela.",
        }
        assert session.fetch_options == []


class TestPickTarget:
    def test_single_database_needs_no_alias(self):
        assert operations.pick_target((_target(),), None).alias == "principal"

    def test_alias_is_case_insensitive(self):
        targets = (_target("Vendas"), _target("rh"))

        assert operations.pick_target(targets, "vendas").alias == "Vendas"

    def test_several_databases_require_an_alias(self):
        targets = (_target("vendas"), _target("rh"))

        with pytest.raises(DatabaseAccessError, match="vendas, rh"):
            operations.pick_target(targets, None)

    def test_unknown_alias_lists_the_options(self):
        targets = (_target(),)

        with pytest.raises(DatabaseAccessError, match="Disponíveis: principal"):
            operations.pick_target(targets, "outro")

    def test_database_name_is_accepted_when_it_is_unique(self):
        targets = (_target("loja"), _target("rh"))
        targets = (targets[0], DatabaseTarget(**{**targets[1].__dict__, "database": "folha"}))

        assert operations.pick_target(targets, "FOLHA").alias == "rh"

    def test_ambiguous_database_name_still_asks_for_the_alias(self):
        targets = (_target("loja"), _target("loja_total"))

        with pytest.raises(DatabaseAccessError, match="Disponíveis: loja, loja_total"):
            operations.pick_target(targets, "app")


class TestSchemaScope:
    @pytest.mark.parametrize(
        ("name", "args"),
        [
            ("list_tables", {"schema": "rh"}),
            ("list_routines", {"schema": "RH"}),
            ("describe_table", {"table": "rh.funcionario"}),
            ("describe_table", {"table": "funcionario", "schema": "rh"}),
            ("get_definition", {"name": "rh.fn_salario"}),
            ("sample_rows", {"table": "rh.funcionario"}),
        ],
    )
    def test_schema_outside_the_allowlist_is_refused_with_the_reason(self, name, args):
        opener = _SessionContext()
        with patch.object(catalog, "readonly_session", opener):
            result = _run((_target(schemas=("vendas",)),), None, name, args)

        assert "fora dos liberados" in result["error"]
        assert "vendas" in result["error"]

    def test_allowed_schema_passes_in_any_case(self):
        catalog.check_schema_scope(_target(schemas=("vendas",)), "VENDAS")

    def test_without_allowlist_every_schema_passes(self):
        catalog.check_schema_scope(_target(), "rh", "rh.funcionario")

    def test_empty_schema_listing_explains_why(self):
        result = catalog.list_tables(FakeSession(), None, "nao_existe", None)

        assert result["count"] == 0
        assert "não existe ou o usuário não tem permissão" in result["note"]


class _SessionContext:
    def __init__(self, session=None, error=None):
        self.session = session or FakeSession()
        self.error = error
        self.opened = 0

    def __call__(self, target, check_posture=True):
        context = self

        class _Manager:
            def __enter__(self):
                context.opened += 1
                if context.error:
                    raise context.error
                return context.session

            def __exit__(self, *exc):
                return False

        return _Manager()


def _run(targets, database, name, args=None):
    return operations.DirectExecutor(targets).run(database, name, args or {})


class TestDirectExecutor:
    def test_errors_come_back_as_data(self):
        opener = _SessionContext(error=DatabaseAccessError("sem permissão"))
        with patch.object(catalog, "readonly_session", opener):
            result = _run((_target(),), None, "database_overview")

        assert result == {"error": "sem permissão"}

    def test_unexpected_errors_are_hidden_and_logged(self):
        with patch.object(catalog, "readonly_session", _SessionContext()), patch.object(
            catalog, "database_overview", side_effect=RuntimeError("password=s3cr3t-value")
        ):
            result = _run((_target(),), None, "database_overview")

        assert "s3cr3t-value" not in result["error"]
        assert "principal" in result["error"]

    def test_bad_alias_never_opens_a_connection(self):
        opener = _SessionContext()
        with patch.object(catalog, "readonly_session", opener):
            result = _run((_target(),), "outro", "database_overview")

        assert "error" in result
        assert opener.opened == 0

    def test_unknown_operation_is_refused_without_connecting(self):
        opener = _SessionContext()
        with patch.object(catalog, "readonly_session", opener):
            result = _run((_target(),), None, "drop_everything")

        assert "não existe" in result["error"]
        assert opener.opened == 0

    def test_unexpected_arguments_are_refused(self):
        opener = _SessionContext()
        with patch.object(catalog, "readonly_session", opener):
            result = _run((_target(),), None, "list_rules", {"table": "t", "cmd": "x"})

        assert "cmd" in result["error"]
        assert opener.opened == 0

    def test_missing_required_argument_is_refused(self):
        result = _run((_target(),), None, "describe_table", {})

        assert "table" in result["error"]

    def test_none_arguments_are_dropped(self):
        with patch.object(catalog, "readonly_session", _SessionContext()), patch.object(
            catalog, "list_rules", return_value={"ok": 1}
        ) as list_rules:
            _run((_target(),), None, "list_rules", {"table": None})

        assert list_rules.call_args.args[2] is None

    def test_catalog_results_are_cached_per_target(self):
        opener = _SessionContext()
        with patch.object(catalog, "readonly_session", opener), patch.object(
            catalog, "database_overview", return_value={"ok": 1}
        ):
            for _ in range(3):
                _run((_target(),), None, "database_overview")

        assert opener.opened == 1

    def test_data_operations_always_run(self):
        opener = _SessionContext()
        with patch.object(catalog, "readonly_session", opener), patch.object(
            catalog, "column_profile", return_value={"ok": 1}
        ):
            for _ in range(2):
                _run((_target(),), None, "column_profile", {"table": "pedido"})

        assert opener.opened == 2

    def test_errors_are_not_cached(self):
        opener = _SessionContext(error=DatabaseAccessError("rede"))
        with patch.object(catalog, "readonly_session", opener), patch.object(
            catalog, "database_overview", return_value={"ok": 1}
        ):
            _run((_target(),), None, "database_overview")
            opener.error = None
            result = _run((_target(),), None, "database_overview")

        assert result == {"ok": 1}

    def test_answers_name_the_database_when_there_are_several(self):
        with patch.object(catalog, "readonly_session", _SessionContext()), patch.object(
            catalog, "database_overview", return_value={"ok": 1}
        ):
            result = _run((_target("vendas"), _target("rh")), "rh", "database_overview")

        assert result == {"database": "rh", "ok": 1}

    def test_unsupported_engine_is_explained(self):
        target = DatabaseTarget(
            alias="x", host="h", port=1, database="d", username="u", password="p", engine="mssql"
        )

        result = _run((target,), None, "database_overview")

        assert "mssql" in result["error"]


class TestReadonlyQueryOperation:
    def _run_query(self, session, target, sql, limit):
        with patch.object(catalog, "readonly_session", _SessionContext(session)):
            return _run((target,), None, "run_readonly_query", {"sql": sql, "limit": limit})

    def test_wraps_the_query_and_masks_results(self):
        session = FakeSession(
            fetched=QueryResult(columns=["id", "email"], rows=[[1, "a@b.com"]])
        )

        result = self._run_query(session, _target(), "select id, email from cliente", 5)

        wrapped, _, max_rows = session.calls[-1]
        assert wrapped.endswith("LIMIT 6")
        assert max_rows == 5
        assert session.fetch_options[-1] is True
        assert result["rows"] == [[1, MASK]]

    def test_allowlist_checks_known_schemas(self):
        session = FakeSession(
            {catalog.KNOWN_SCHEMAS_SQL: [{"name": "vendas"}, {"name": "rh"}]}
        )

        result = self._run_query(
            session, _target(schemas=("vendas",)), "select * from rh.folha", 5
        )

        assert "rh" in result["error"]


class TestQueryRefusedBeforeConnecting:
    def test_refused_query_never_opens_a_session(self):
        opener = _SessionContext()
        with patch.object(catalog, "readonly_session", opener):
            result = _run((_target(),), None, "run_readonly_query", {"sql": "delete from t"})

        assert "Só são aceitas consultas de leitura" in result["error"]
        assert opener.opened == 0


class TestPostgresSuggestions:
    def test_infers_links_from_column_names(self):
        session = FakeSession(
            {
                catalog.RELATION_COLUMNS_SQL: [
                    {"schema": "public", "table": "pedido", "column": "id", "data_type": "integer", "text": None},
                    {"schema": "public", "table": "item", "column": "pedido_id", "data_type": "integer", "text": None},
                    {"schema": "public", "table": "nota", "column": "id", "data_type": "integer", "text": None},
                    {"schema": "public", "table": "cliente", "column": "codcli", "data_type": "integer", "text": "Codigo do cliente"},
                    {"schema": "public", "table": "venda", "column": "codcli", "data_type": "integer", "text": "Codigo do cliente"},
                ],
                catalog.SINGLE_KEYS_SQL: [
                    {"schema": "public", "table": "cliente", "column": "codcli"},
                    {"schema": "public", "table": "pedido", "column": "id"},
                    {"schema": "public", "table": "nota", "column": "id"},
                ],
                catalog.DECLARED_FK_COLUMNS_SQL: [],
            }
        )

        inferred = catalog.suggest_table_relationships(session, None, None)["inferred"]

        assert [(i["from_table"], i["to_table"], i["confidence"]) for i in inferred] == [
            ("public.venda", "public.cliente", "alta"),
            ("public.item", "public.pedido", "média"),
        ]
        assert "pedido_id" in inferred[1]["evidence"]


class TestServerInstructions:
    def test_single_database(self):
        text = server_module.server_instructions((_target(schemas=("vendas",)),))

        assert "'principal' (PostgreSQL, banco app, schemas vendas)" in text
        assert "pode ser omitido" in text

    def test_several_databases(self):
        text = server_module.server_instructions((_target("a"), _target("b")))

        assert "informe sempre" in text

    def test_never_mentions_credentials(self):
        text = server_module.server_instructions((_target(),))

        assert "s3cr3t-value" not in text
        assert "db.example.com" not in text


def _server(targets=None):
    targets = targets or (_target(),)
    return server_module.build_database_server(targets, operations.DirectExecutor(targets))


class RecordingExecutor:
    def __init__(self):
        self.calls = []

    def run(self, database, name, args):
        self.calls.append((database, name, args))
        return {"ok": True}


class TestInProcessServer:
    EXPECTED_TOOLS = frozenset({
        "database_overview",
        "list_tables",
        "list_routines",
        "describe_table",
        "list_relationships",
        "suggest_relationships",
        "list_rules",
        "get_definition",
        "column_profile",
        "sample_rows",
        "run_readonly_query",
    })

    def test_every_tool_is_an_allowlisted_operation(self):
        assert set(operations.OPERATIONS) == self.EXPECTED_TOOLS

    def test_building_the_server_leaves_root_logging_alone(self):
        root = logging.getLogger()
        before = (list(root.handlers), root.level)

        _server()

        after = (list(root.handlers), root.level)
        assert after == before

    def test_exposes_read_only_tools_through_mcp(self):
        from pydantic_ai.mcp import MCPToolset

        toolset = MCPToolset(_server())

        async def list_tools():
            async with toolset:
                return await toolset.client.list_tools()

        tools = asyncio.run(list_tools())

        names = {tool.name for tool in tools}
        assert names == self.EXPECTED_TOOLS
        assert all(tool.annotations.readOnlyHint for tool in tools)
        assert all(tool.description for tool in tools)

    def test_tool_call_reaches_the_catalog(self):
        from pydantic_ai.mcp import MCPToolset

        toolset = MCPToolset(_server())
        opener = _SessionContext()

        async def call():
            async with toolset:
                return await toolset.client.call_tool("list_rules", {"table": "pedido"})

        with patch.object(catalog, "readonly_session", opener), patch.object(
            catalog, "list_rules", return_value={"note": "ok"}
        ) as list_rules:
            result = asyncio.run(call())

        assert list_rules.call_args.args[2] == "pedido"
        assert '"note"' in result.content[0].text

    def test_tool_call_goes_through_the_executor(self):
        from pydantic_ai.mcp import MCPToolset

        executor = RecordingExecutor()
        toolset = MCPToolset(server_module.build_database_server((_target(),), executor))

        async def call():
            async with toolset:
                return await toolset.client.call_tool(
                    "describe_table", {"table": "pedido", "schema": "vendas"}
                )

        asyncio.run(call())

        assert executor.calls == [
            (None, "describe_table", {"table": "pedido", "schema": "vendas"})
        ]

"""SQL Server engine with a fake session; the SQL itself was validated on SQL Server 2022."""

from unittest.mock import MagicMock, patch

import pytest

from database_access import guard as generic
from database_access.engines import sqlserver
from database_access.guard import MASK, DatabaseAccessError, DatabaseTarget, QueryResult


def _target(**overrides) -> DatabaseTarget:
    values = {
        "alias": "loja",
        "host": "sql.local",
        "port": 1433,
        "database": "loja",
        "username": "estack_ro",
        "password": "s3cr3t-value",
        "ssl_mode": "verify-full",
        "schemas": ("vendas",),
        "engine": "sqlserver",
    }
    values.update(overrides)
    return DatabaseTarget(**values)


@pytest.fixture(autouse=True)
def _clear_posture_cache():
    generic._posture_cache.clear()
    yield
    generic._posture_cache.clear()


def prepare(sql, limit=10, **kwargs):
    return generic.prepare_readonly_query(sql, limit, dialect=sqlserver.DIALECT, **kwargs)


class TestDialect:
    def test_wraps_with_top_before_the_subquery(self):
        assert prepare("select id from vendas.cliente", 5) == (
            "SELECT TOP (6) * FROM (\nselect id from vendas.cliente\n) AS estack_q"
        )

    @pytest.mark.parametrize(
        "sql",
        [
            "select [a;b)] = 1",
            "select [x]]y;] = 1",
            "select N'x;y)' as t",
            "select n'it''s' as t",
            "select 'exec waitfor into' as texto",
            "select vendas.cliente.nome from vendas.cliente",
        ],
    )
    def test_brackets_and_national_strings_hide_their_content(self, sql):
        assert prepare(sql).startswith("SELECT TOP")

    def test_unterminated_bracket_is_refused(self):
        with pytest.raises(DatabaseAccessError, match=r"identificador \["):
            prepare("select [aberto from t")

    @pytest.mark.parametrize(
        ("sql", "message"),
        [
            ("select * into vendas.copia from vendas.cliente", "INTO"),
            ("select 1 exec xp_cmdshell 'dir'", "EXEC"),
            ("select 1 execute sp_who", "EXEC"),
            ("select * from openrowset('x', 'y', 'select 1')", "openrowset"),
            ("select * from openquery(srv, 'select 1')", "openquery"),
            ("select * from opendatasource('x', 'y').db.dbo.t", "opendatasource"),
            ("select * from srv.loja.vendas.cliente", "4 partes"),
            ("select * from [srv].[loja].[vendas].[cliente]", "4 partes"),
            ("select next value for vendas.seq_pedido", "NEXT VALUE FOR"),
            ("select name from sys.sql_logins", "credenciais"),
            ("select name from sys.[sql_logins]", "credenciais"),
            ("select 1 waitfor delay '00:00:05'", "WAITFOR"),
            ("select 1 dbcc checkdb", "(?i)dbcc"),
            ("select * from sys.fn_get_audit_file('c:\\x', default, default)", "fn_get_audit_file"),
            ("with x as (select 1 a) select * from x", "começam com SELECT"),
            ("update vendas.cliente set nome = 'x'", "começam com SELECT"),
        ],
    )
    def test_dangerous_constructs_are_refused(self, sql, message):
        with pytest.raises(DatabaseAccessError, match=message):
            prepare(sql)

    def test_bracketed_schema_is_checked_against_the_allowlist(self):
        with pytest.raises(DatabaseAccessError, match="'rh' está fora"):
            prepare("select * from [rh].[funcionario]", allowed_schemas=("vendas",), known_schemas={"rh"})

    def test_qualifiers_unwrap_brackets(self):
        assert "rh" in generic.schema_qualifiers("select * from [rh].funcionario", sqlserver.DIALECT)


class FakeSession:
    """Answers catalog SQL by the first registered fragment the query contains."""

    def __init__(self, answers=None, fetched=None, target=None):
        self.answers = list((answers or {}).items())
        self.fetched = fetched
        self.target = target or _target()
        self._scope = None
        self.calls = []

    def fetch(self, query, params=None, max_rows=200):
        self.calls.append((query, params, max_rows))
        if self.fetched is not None and query.lower().startswith("select top ("):
            return self.fetched
        for fragment, rows in self.answers:
            if fragment in query:
                if isinstance(rows, Exception):
                    raise rows
                columns = list(rows[0].keys()) if rows else []
                return QueryResult(columns=columns, rows=[[row[c] for c in columns] for row in rows])
        return QueryResult(columns=[], rows=[])

    def records(self, query, params=None, max_rows=1000):
        return self.fetch(query, params, max_rows).records()

    def one(self, query, params=None):
        found = self.records(query, params, max_rows=1)
        return found[0] if found else None


def _posture_session(
    server_roles=(), database_roles=(), server_permissions=("CONNECT SQL",),
    database_permissions=("CONNECT",), grants=None, outside=(),
):
    return FakeSession(
        {
            "serverproperty('ProductVersion')": [{"version": "16.0.4295.3", "role": "estack_ro"}],
            "as impersonates": [grants or {"impersonates": 0, "writes": 0, "executes": 0}],
            "is_srvrolemember": [{"name": name} for name in server_roles],
            "is_rolemember(r.name)": [{"name": name} for name in database_roles],
            "fn_my_permissions(null, 'SERVER')": [{"permission_name": name} for name in server_permissions],
            "fn_my_permissions(null, 'DATABASE')": [{"permission_name": name} for name in database_permissions],
            "has_perms_by_name": [{"schema_name": name} for name in outside],
        }
    )


class TestPosture:
    def test_reader_passes(self):
        posture = sqlserver.read_posture(_posture_session(), ("vendas",))

        assert posture.refusal("loja") is None
        assert posture.warnings() == []
        assert posture.server_version == "16.0.4295.3"

    def test_roles_come_first_in_the_refusal(self):
        session = _posture_session(database_roles=("db_owner",), database_permissions=("CONTROL", "ALTER", "CONNECT"))
        target = _target()

        with pytest.raises(DatabaseAccessError, match=r"tem db_owner, ALTER, CONTROL\."):
            sqlserver.enforce_posture(target, session)

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"server_roles": ("sysadmin",)}, "sysadmin"),
            ({"server_permissions": ("CONNECT SQL", "CONTROL SERVER")}, "CONTROL SERVER"),
            ({"database_permissions": ("CONNECT", "UNMASK")}, "UNMASK"),
            ({"database_roles": ("db_ddladmin",)}, "db_ddladmin"),
            ({"grants": {"impersonates": 1, "writes": 0, "executes": 0}}, "IMPERSONATE"),
        ],
    )
    def test_powerful_users_are_refused(self, kwargs, expected):
        posture = sqlserver.read_posture(_posture_session(**kwargs))

        assert expected in posture.refused

    def test_long_refusals_are_summarized(self):
        many = ("CONTROL", "ALTER", "ALTER ANY USER", "ALTER ANY ROLE", "ALTER ANY SCHEMA", "UNMASK")
        posture = sqlserver.read_posture(_posture_session(database_permissions=many))

        assert "e mais 2" in posture.refusal("loja")

    def test_writers_and_executors_only_warn(self):
        posture = sqlserver.read_posture(_posture_session(grants={"impersonates": 0, "writes": 1, "executes": 2}))

        assert posture.refusal("loja") is None
        assert len(posture.warnings()) == 2
        assert posture.can_write

    def test_reading_outside_the_allowlist_is_refused(self):
        posture = sqlserver.read_posture(_posture_session(outside=("rh",)), ("vendas",))

        assert "fora dos liberados (rh)" in posture.refusal("loja")

    def test_outside_scope_is_not_checked_without_an_allowlist(self):
        session = _posture_session(outside=("rh",))

        assert sqlserver.read_posture(session, ()).readable_outside_scope == ()

    def test_posture_is_read_once_per_target(self):
        session = _posture_session()

        for _ in range(3):
            sqlserver.enforce_posture(_target(), session)

        assert len([c for c in session.calls if "fn_my_permissions(null, 'SERVER')" in c[0]]) == 1


class TestConnection:
    def test_verify_full_checks_certificate_and_name(self):
        with patch.object(sqlserver, "_certificates_file", return_value="ca.pem"):
            params = sqlserver._connect_params(_target())

        assert params["cafile"] == "ca.pem"
        assert params["validate_host"] is True
        assert params["autocommit"] is False
        assert params["timeout"] == sqlserver.QUERY_TIMEOUT_SECONDS

    @pytest.mark.parametrize("ssl_mode", ["require", "verify-ca"])
    def test_other_tls_modes_validate_the_chain_but_not_the_name(self, ssl_mode):
        with patch.object(sqlserver, "_certificates_file", return_value="ca.pem"):
            params = sqlserver._connect_params(_target(ssl_mode=ssl_mode))

        assert params["cafile"] == "ca.pem"
        assert params["validate_host"] is False

    def test_disable_is_plain_tds(self):
        params = sqlserver._connect_params(_target(ssl_mode="disable"))

        assert "cafile" not in params

    def test_ca_file_option_wins_over_system_certificates(self):
        target = _target(options=(("ca_file", "/config/ca.pem"),))

        assert sqlserver._certificates_file(target) == "/config/ca.pem"

    def test_system_certificates_are_the_default(self):
        assert sqlserver._certificates_file(_target()).endswith(".pem")

    def test_public_policy_pins_an_address_when_the_name_is_not_verified(self):
        target = _target(ssl_mode="require", options=(("network_policy", "public"),))
        with patch.object(sqlserver, "resolve_allowed_addresses", return_value=["93.184.216.34"]):
            params = sqlserver._connect_params(target)

        assert params["server"] == "93.184.216.34"

    def test_public_policy_keeps_the_name_for_verify_full(self):
        target = _target(options=(("network_policy", "public"),))
        with patch.object(sqlserver, "resolve_allowed_addresses", return_value=["93.184.216.34"]):
            params = sqlserver._connect_params(target)

        assert params["server"] == "sql.local"

    def test_public_policy_refuses_plain_tds(self):
        target = _target(ssl_mode="disable", options=(("network_policy", "public"),))

        with pytest.raises(DatabaseAccessError, match="não é permitido"):
            sqlserver._connect_params(target)

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (type("LoginError", (Exception,), {"number": 18456})("login failed"), "usuário ou senha recusados"),
            (Exception("SSL routines certificate verify failed"), "falha de TLS"),
            (TimeoutError("timed out"), "tempo de conexão esgotado"),
            (OSError("connection refused"), "servidor inacessível"),
        ],
    )
    def test_connection_failures_are_explained_without_the_password(self, error, expected):
        driver = MagicMock()
        driver.connect.side_effect = error
        target = _target(ssl_mode="disable")
        with (
            patch.object(sqlserver, "_load_driver", return_value=driver),
            pytest.raises(DatabaseAccessError, match=expected) as raised,
        ):
            sqlserver.open_connection(target)

        assert "s3cr3t-value" not in str(raised.value)

    def test_new_sessions_get_the_safety_settings(self):
        driver = MagicMock()
        cursor = driver.connect.return_value.cursor.return_value
        cursor.description = None
        target = _target(ssl_mode="disable")
        with patch.object(sqlserver, "_load_driver", return_value=driver):
            sqlserver.open_connection(target)

        assert cursor.execute.call_args.args[0] == sqlserver.SESSION_SETTINGS


class TestSession:
    def _session(self, cursor):
        connection = MagicMock()
        connection.cursor.return_value = cursor
        return sqlserver.SqlServerSession(_target(), connection, MagicMock()), connection

    def test_every_statement_is_rolled_back(self):
        cursor = MagicMock(description=[("id",), ("nome",)])
        cursor.fetchmany.return_value = [(1, "Ana"), (2, "Bia")]
        session, connection = self._session(cursor)

        result = session.fetch("select id, nome from vendas.cliente", max_rows=1)

        assert result.columns == ["id", "nome"]
        assert result.rows == [[1, "Ana"]]
        assert result.truncated
        connection.rollback.assert_called_once()
        cursor.close.assert_called_once()

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (type("E", (Exception,), {"number": 1033})("order by"), "exige TOP"),
            (type("E", (Exception,), {"number": 229})("denied"), "não tem permissão"),
            (type("E", (Exception,), {"number": 208})("invalid object"), "não existe"),
            (TimeoutError(), "limite de 15s"),
            (type("E", (Exception,), {"number": 245})("conversion failed"), "245"),
        ],
    )
    def test_driver_errors_become_safe_messages(self, error, expected):
        cursor = MagicMock()
        cursor.execute.side_effect = error
        session, connection = self._session(cursor)

        with pytest.raises(DatabaseAccessError, match=expected):
            session.fetch("select 1")

        connection.rollback.assert_called_once()

    def test_statements_without_rows_return_an_empty_result(self):
        session, _ = self._session(MagicMock(description=None))

        assert session.fetch("SET LOCK_TIMEOUT 2000").columns == []

    def test_failed_rollback_and_close_are_logged_not_raised(self):
        session, connection = self._session(MagicMock(description=None))
        connection.rollback.side_effect = RuntimeError("lost")
        connection.close.side_effect = RuntimeError("lost")

        session.fetch("select 1")
        session.close()

        connection.close.assert_called_once()

    def test_one_returns_the_first_record(self):
        cursor = MagicMock(description=[("role",)])
        cursor.fetchmany.return_value = [("estack_ro",)]
        session, _ = self._session(cursor)

        assert session.one("select user_name() as role") == {"role": "estack_ro"}


class TestScope:
    def test_allowlist_wins(self):
        assert sqlserver.schema_scope(FakeSession()) == ["vendas"]

    def test_falls_back_to_visible_schemas(self):
        session = FakeSession(
            {"select distinct top (200) s.name as schema_name": [{"schema_name": "dbo"}]}, target=_target(schemas=())
        )

        assert sqlserver.schema_scope(session) == ["dbo"]

    def test_no_visible_schema_is_explained(self):
        session = FakeSession(target=_target(schemas=()))

        with pytest.raises(DatabaseAccessError, match="não enxerga tabelas"):
            sqlserver.schema_scope(session)

    def test_requested_schema_is_matched_without_case(self):
        assert sqlserver.schema_scope(FakeSession(), "[VENDAS]") == ["vendas"]

    def test_schema_outside_the_scope_is_refused(self):
        session = FakeSession()

        with pytest.raises(DatabaseAccessError, match="'rh' está fora"):
            sqlserver.schema_scope(session, "rh")


CATALOG = {
    "select s.name as schema_name, o.name as object_name, o.type as kind, o.object_id": [
        {"schema_name": "vendas", "object_name": "pedido", "kind": "U ", "object_id": 7}
    ],
    "group by s.name, o.type": [
        {"schema_name": "vendas", "kind": "U ", "n": 3},
        {"schema_name": "vendas", "kind": "P ", "n": 1},
    ],
    "order by rows desc": [{"schema_name": "vendas", "table_name": "pedido", "rows": 3}],
    "serverproperty('ProductVersion')": [{"version": "16.0.4295.3", "role": "estack_ro"}],
    "serverproperty('Edition')": [{"edition": "Developer Edition (64-bit)"}],
    "as table_name, o.type as kind": [
        {"schema_name": "vendas", "table_name": "pedido", "kind": "U ", "description": "Pedidos", "rows": 3},
        {"schema_name": "vendas", "table_name": "vw", "kind": "V ", "description": None, "rows": None},
    ],
    "as routine_name": [{"schema_name": "vendas", "routine_name": "sp_faturar", "kind": "P ", "description": None}],
    "minor_id = 0 and ep.name = 'MS_Description') as d": [{"d": "Pedidos de venda"}],
    "index_id in (0, 1)) as n": [{"n": 3}],
    "as computed_definition": [
        {"column_name": "numped", "data_type": "int", "is_computed": False, "description": None},
        {"column_name": "valor_com_imposto", "data_type": "decimal", "is_computed": True, "description": None},
    ],
    "from sys.key_constraints": [{"constraint_name": "pk_pedido", "kind": "PK", "columns": "numped"}],
    "from sys.check_constraints": [{"constraint_name": "ck_valor", "definition": "([valor]>(0))", "is_disabled": False}],
    "as from_columns": [
        {
            "constraint_name": "fk_pedido_cliente", "from_schema": "vendas", "from_table": "pedido",
            "to_schema": "vendas", "to_table": "cliente", "on_delete": "CASCADE", "is_disabled": False,
            "from_columns": "cliente_id", "to_columns": "id",
        }
    ],
    "i.is_primary_key": [{"index_name": "uq_aberto", "has_filter": True, "filter_definition": "([status]='ABERTO')"}],
    "from sys.triggers": [{"trigger_name": "trg_cancelado", "definition": "create trigger ...", "events": "UPDATE"}],
    "from sys.security_predicates": [{"policy_name": "pol_regiao", "predicate_definition": "([vendas].[fn]([regiao]))"}],
    "select definition from sys.sql_modules": [{"definition": "create view vendas.vw as select 1"}],
    "sql_expression_dependencies": DatabaseAccessError("sem permissão"),
    "as columns from sys.indexes i where i.is_unique = 1 and i.has_filter = 0": [
        {"schema_name": "vendas", "table_name": "pedido", "columns": "numped"}
    ],
    "c.is_nullable = 1": [{"schema_name": "vendas", "table_name": "pedido", "column_name": "status"}],
    "ty.name as data_type, c.max_length": [
        {"schema_name": "vendas", "table_name": "pedido", "column_name": "numped", "data_type": "int", "max_length": 4, "description": None},
        {"schema_name": "vendas", "table_name": "item", "column_name": "numped", "data_type": "int", "max_length": 4, "description": None},
    ],
    "x.is_included_column = 0) = 1": [{"schema_name": "vendas", "table_name": "pedido", "column_name": "numped"}],
    "from sys.foreign_key_columns fc where": [],
    "from sys.default_constraints dc where": [{"table_name": "pedido", "column_name": "status", "definition": "('ABERTO')"}],
    "i.has_filter = 1": [{"table_name": "pedido", "index_name": "uq_aberto", "filter_definition": "([status]='ABERTO')"}],
    "mc.masking_function from sys.masked_columns": [{"table_name": "cliente", "column_name": "email", "masking_function": "email()"}],
    "from sys.sequences": [{"schema_name": "vendas", "sequence_name": "seq_pedido"}],
    "left join sys.sql_modules m on m.object_id = o.object_id": [
        {"schema_name": "vendas", "object_name": "sp_faturar", "kind": "P ", "definition": "create procedure ..."},
        {"schema_name": "vendas", "object_name": "sp_faturar", "kind": "PC", "definition": None},
    ],
    "as masked from sys.columns c left join": [
        {"column_name": "doc", "description": "CPF do cliente", "masked": 0},
        {"column_name": "email", "description": None, "masked": 1},
    ],
    "dm_db_stats_histogram": [
        {"column_name": "id", "stats_id": 1, "rows": 4, "last_updated": None, "step_number": 1, "high_key": None, "equal_rows": 1, "distinct_range_rows": 0},
        {"column_name": "id", "stats_id": 1, "rows": 4, "last_updated": None, "step_number": 2, "high_key": "1", "equal_rows": 1, "distinct_range_rows": 0},
        {"column_name": "id", "stats_id": 1, "rows": 4, "last_updated": None, "step_number": 3, "high_key": "9", "equal_rows": 1, "distinct_range_rows": 1},
        {"column_name": "id", "stats_id": 2, "rows": 4, "last_updated": None, "step_number": 1, "high_key": "0", "equal_rows": 1, "distinct_range_rows": 0},
        {"column_name": "doc", "stats_id": 3, "rows": 4, "last_updated": None, "step_number": 1, "high_key": "111", "equal_rows": 1, "distinct_range_rows": 0},
    ],
    "select name from sys.schemas where name in": [{"name": "vendas"}],
    "from sys.databases": [],
    "c.name in (": [{"column_name": "doc", "description": "CPF do cliente", "masked": 0}],
    "select distinct top (200) s.name as schema_name": [{"schema_name": "vendas"}],
}


def _catalog(**overrides):
    return FakeSession({**CATALOG, **overrides})


def _single(kind):
    return {
        "select s.name as schema_name, o.name as object_name, o.type as kind, o.object_id": [
            {"schema_name": "vendas", "object_name": "vw", "kind": kind, "object_id": 8}
        ]
    }


class TestCatalogOperations:
    def test_overview_counts_objects_by_kind(self):
        overview = sqlserver.database_overview(_catalog(), _target())

        assert overview["schemas"] == [{"schema": "vendas", "objects": {"table": 3, "procedure": 1}}]
        assert overview["edition"] == "Developer Edition (64-bit)"

    def test_list_tables_names_kinds_and_escapes_the_filter(self):
        session = _catalog()

        listed = sqlserver.list_tables(session, _target(), name_filter="ped_[x]")

        assert [item["kind"] for item in listed["items"]] == ["table", "view"]
        assert session.calls[-1][1]["b1"] == "%ped[_][[]x]%"

    def test_empty_schema_listing_explains_why(self):
        listed = sqlserver.list_tables(FakeSession(), _target(), schema="vendas")

        assert "Nenhuma tabela visível" in listed["note"]

    def test_list_routines_names_kinds(self):
        listed = sqlserver.list_routines(_catalog(), _target(), name_filter="sp")

        assert listed["items"][0]["kind"] == "procedure"

    def test_long_listings_are_capped(self):
        rows = [{"schema_name": "vendas", "routine_name": f"p{i}", "kind": "P"} for i in range(sqlserver.MAX_LISTED_OBJECTS + 1)]

        listed = sqlserver.list_routines(FakeSession({"as routine_name": rows}), _target())

        assert listed["truncated"] is True

    def test_describe_table_gathers_rules_and_skips_what_needs_extra_permission(self):
        described = sqlserver.describe_table(_catalog(), _target(), "vendas.pedido")

        assert described["table"] == "vendas.pedido"
        assert described["description"] == "Pedidos de venda"
        assert described["keys"][0]["kind"] == "primary key"
        assert described["indexes"][0]["filter_definition"] == "([status]='ABERTO')"
        assert described["triggers"][0]["trigger_name"] == "trg_cancelado"
        assert described["row_level_security_policies"][0]["policy_name"] == "pol_regiao"
        assert "used_by" not in described

    def test_describe_view_includes_its_definition(self):
        described = sqlserver.describe_table(_catalog(**_single("V ")), _target(), "vw")

        assert described["view_definition"] == "create view vendas.vw as select 1"

    def test_encrypted_view_is_flagged(self):
        session = _catalog(**_single("V "), **{"select definition from sys.sql_modules": [{"definition": None}]})

        assert "WITH ENCRYPTION" in sqlserver.describe_table(session, _target(), "vw")["view_definition"]

    def test_unknown_table_is_explained(self):
        session = FakeSession()

        with pytest.raises(DatabaseAccessError, match="não encontrada no schema 'vendas'"):
            sqlserver.resolve_table(session, "vendas", "nada")

    def test_table_in_two_schemas_asks_for_the_schema(self):
        rows = [
            {"schema_name": "vendas", "object_name": "t", "kind": "U", "object_id": 1},
            {"schema_name": "dbo", "object_name": "t", "kind": "U", "object_id": 2},
        ]
        session = FakeSession({"o.type as kind, o.object_id": rows}, target=_target(schemas=("vendas", "dbo")))

        with pytest.raises(DatabaseAccessError, match="mais de um schema"):
            sqlserver.resolve_table(session, None, "t")

    def test_relationships_report_cardinality_and_actions(self):
        (item,) = sqlserver.list_relationships(_catalog(), _target())["items"]

        assert item["on_delete"] == "cascade"
        assert item["cardinality"] == "many-to-one"
        assert item["required"] is True

    def test_no_foreign_keys_suggests_inference(self):
        session = FakeSession()

        assert "suggest_relationships" in sqlserver.list_relationships(session, _target())["hint"]

    def test_inferred_links_use_single_column_keys(self):
        (link,) = sqlserver.suggest_table_relationships(_catalog(), _target())["inferred"]

        assert (link["from_table"], link["to_table"]) == ("vendas.item", "vendas.pedido")

    def test_rules_include_filtered_indexes_masks_and_sequences(self):
        rules = sqlserver.list_rules(_catalog(), _target())

        assert rules["unique_filtered_indexes"][0]["index_name"] == "uq_aberto"
        assert rules["masked_columns"][0]["column_name"] == "email"
        assert rules["sequences"][0]["sequence_name"] == "seq_pedido"
        assert "stored procedures" in rules["note"]

    def test_rules_for_one_table_skip_sequences(self):
        rules = sqlserver.list_rules(_catalog(), _target(), table="pedido")

        assert "sequences" not in rules
        assert rules["check_constraints"][0]["constraint_name"] == "ck_valor"

    def test_definitions_flag_clr_routines(self):
        definitions = sqlserver.get_definition(_catalog(), _target(), "vendas.sp_faturar")["definitions"]

        assert definitions[0]["definition"] == "create procedure ..."
        assert "CLR" in definitions[1]["note"]

    def test_missing_definition_is_explained(self):
        session = FakeSession()
        target = _target()

        with pytest.raises(DatabaseAccessError, match="Nenhuma procedure"):
            sqlserver.get_definition(session, target, "nada")

    def test_column_profile_reads_the_first_histogram_and_masks_sensitive_columns(self):
        profile = sqlserver.column_profile(_catalog(), _target(), "pedido")

        by_column = {column["column"]: column for column in profile["columns"]}
        assert by_column["id"]["distinct"] == 3
        assert by_column["id"]["null_fraction"] == 0.25
        assert (by_column["id"]["approx_min"], by_column["id"]["approx_max"]) == ("1", "9")
        assert by_column["doc"]["approx_min"] == MASK

    def test_column_profile_without_statistics_explains(self):
        session = _catalog(dm_db_stats_histogram=[])

        assert "Sem estatísticas" in sqlserver.column_profile(session, _target(), "pedido")["note"]

    def test_sample_rows_masks_by_description_and_native_mask(self):
        fetched = QueryResult(columns=["id", "doc", "email"], rows=[[1, "123.456.789-09", "aXXX@XXXX.com"]])
        session = _catalog(fetched=None)
        session.fetched = fetched

        sample = sqlserver.sample_rows(session, _target(), "pedido", limit=99)

        assert sample["rows"] == [[1, MASK, MASK]]
        assert "select top (20) * from [vendas].[pedido]" in session.calls[-1][0]


class _Opened:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *exc):
        return False


class TestEngine:
    def test_check_connection_reports_schemas_and_missing_ones(self):
        session = _posture_session()
        session.answers.append(("select distinct top (200) s.name as schema_name", [{"schema_name": "vendas"}]))
        target = _target(schemas=("vendas", "financeiro"))

        with patch.object(sqlserver, "readonly_session", return_value=_Opened(session)):
            check = sqlserver.ENGINE.check_connection(target)

        assert check["ok"] is True
        assert "financeiro" in check["warnings"][0]

    def test_check_connection_reports_connection_errors(self):
        target = _target()

        with patch.object(sqlserver, "readonly_session", side_effect=DatabaseAccessError("sem rede")):
            check = sqlserver.ENGINE.check_connection(target)

        assert check == {"ok": False, "error": "sem rede"}

    def test_queries_naming_another_database_are_refused(self):
        session = FakeSession({"from sys.databases": [{"name": "master"}]})

        with pytest.raises(DatabaseAccessError, match="'master', que é outro banco"):
            sqlserver.ENGINE.run_query(session, "select * from master.sys.databases", 10)

    def test_queries_in_the_registered_database_run(self):
        session = FakeSession(fetched=QueryResult(columns=["n"], rows=[[1]]))

        result = sqlserver.ENGINE.run_query(session, "select top (2) * from (select 1 n from vendas.t) as q", 1)

        assert result.rows == [[1]]

    def test_known_schemas_are_matched_without_case(self):
        session = FakeSession({"select name from sys.schemas where name in": [{"name": "vendas"}]})

        assert sqlserver.ENGINE.known_schemas(session, {"VENDAS", "rh"}) == {"VENDAS"}

    def test_known_schemas_without_candidates_is_empty(self):
        assert sqlserver.ENGINE.known_schemas(FakeSession(), set()) == set()

    def test_sensitive_columns_come_from_descriptions(self):
        session = FakeSession({"c.name in (": [{"column_name": "doc", "description": "CPF do cliente", "masked": 0}]})

        assert sqlserver.ENGINE.sensitive_columns(session, _target(), ["nome", "DOC"]) == frozenset({"DOC"})

    def test_sensitive_columns_without_names_or_scope_are_empty(self):
        no_scope = FakeSession(target=_target(schemas=()))

        assert sqlserver.ENGINE.sensitive_columns(FakeSession(), _target(), []) == frozenset()
        assert sqlserver.ENGINE.sensitive_columns(no_scope, _target(), ["doc"]) == frozenset()

    def test_readonly_session_checks_posture_and_closes(self):
        session = _posture_session()
        session.close = MagicMock()
        target = _target()
        with patch.object(sqlserver, "open_connection", return_value=session), sqlserver.readonly_session(target) as opened:
            assert opened is session

        session.close.assert_called_once()

    def test_engine_is_registered_and_wired(self):
        from database_access.engines.base import get_engine

        target = _target()
        with patch.object(sqlserver, "readonly_session") as opened:
            sqlserver.ENGINE.open_session(target, check_posture=False)

        assert get_engine("sqlserver") is sqlserver.ENGINE
        opened.assert_called_once_with(target, check_posture=False)
        assert set(sqlserver.ENGINE.catalog()) >= {"describe_table", "get_definition", "sample_rows"}

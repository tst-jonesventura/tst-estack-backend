"""Db2 for i engine with a fake Mapepire job; the SQL itself was validated on PUB400."""

import threading
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from database_access import guard as generic
from database_access.engines import db2i
from database_access.guard import MASK, DatabaseAccessError, DatabaseTarget, QueryResult


def _target(**overrides) -> DatabaseTarget:
    values = {
        "alias": "erp",
        "host": "as400.local",
        "port": 8076,
        "database": "S21A",
        "username": "ESTACKRO",
        "password": "s3cr3t-value",
        "ssl_mode": "require",
        "schemas": ("VENDAS",),
        "engine": "db2i",
    }
    values.update(overrides)
    return DatabaseTarget(**values)


@pytest.fixture(autouse=True)
def _clear_posture_cache():
    generic._posture_cache.clear()
    yield
    generic._posture_cache.clear()


def prepare(sql, limit=10, **kwargs):
    return generic.prepare_readonly_query(sql, limit, dialect=db2i.DIALECT, **kwargs)


class TestDialect:
    def test_wraps_with_fetch_first(self):
        assert prepare("select * from pedido", 5).endswith("FETCH FIRST 6 ROWS ONLY")

    @pytest.mark.parametrize(
        "sql",
        [
            "select qsys2.qcmdexc('DLTLIB X') from sysibm.sysdummy1",
            'select "QCMDEXC"(\'DLTLIB X\') from sysibm.sysdummy1',
            "values qcmdexc('X')",
            "select * from table(qsys2.ifs_read('/etc/passwd')) x",
            "select qsys2.http_get('http://x', '') from sysibm.sysdummy1",
            "select qsys2.generate_spreadsheet('/tmp/x', 'select 1') from sysibm.sysdummy1",
        ],
    )
    def test_functions_with_side_effects_are_refused(self, sql):
        with pytest.raises(DatabaseAccessError, match="não é permitida"):
            prepare(sql)

    def test_whole_systools_schema_is_refused(self):
        with pytest.raises(DatabaseAccessError, match="SYSTOOLS"):
            prepare("select * from SYSTOOLS.HTTPGETCLOB('http://x', '')")

    def test_unquoted_libraries_fold_to_upper_case(self):
        with pytest.raises(DatabaseAccessError, match="FINANCEIRO"):
            prepare(
                "select * from financeiro.folha",
                allowed_schemas=("VENDAS",),
                known_schemas={"VENDAS", "FINANCEIRO"},
            )

    def test_catalog_libraries_are_always_readable(self):
        prepare(
            "select * from qsys2.systables",
            allowed_schemas=("VENDAS",),
            known_schemas={"VENDAS", "QSYS2"},
        )

    def test_dollar_signs_are_not_quotes_in_db2(self):
        with pytest.raises(DatabaseAccessError, match="única consulta"):
            prepare("select '$$' from x; select $$ from y")


class FakeStatement:
    def __init__(self, job, sql, options):
        self.job, self.sql, self.options = job, sql, options

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, rows_to_fetch=None):
        self.job.calls.append((self.sql, self.options, rows_to_fetch))
        if self.job.delay:
            self.job.release.wait(self.job.delay)
        if self.job.error:
            raise self.job.error
        return self.job.result


class FakeJob:
    def __init__(self, result=None, error=None, delay=0):
        self.result = result or {"metadata": {"columns": []}, "data": []}
        self.error = error
        self.delay = delay
        self.release = threading.Event()
        self.calls = []
        self.closed = threading.Event()

    def query(self, sql, options):
        return FakeStatement(self, sql, options)

    def close(self):
        self.closed.set()
        self.release.set()


def _result(columns, rows):
    return {
        "metadata": {"columns": [{"name": name} for name in columns]},
        "data": [dict(zip(columns, row)) for row in rows],
        "is_done": True,
    }


class TestSession:
    def test_rows_follow_the_metadata_order(self):
        job = FakeJob(_result(["B", "A"], [[2, 1]]))

        result = db2i.Db2iSession(_target(), job).fetch("select b, a from t")

        assert (result.columns, result.rows) == (["B", "A"], [[2, 1]])

    def test_never_runs_cl_commands_and_binds_parameters(self):
        job = FakeJob()

        db2i.Db2iSession(_target(), job).fetch("select ? from t", ["x"], max_rows=3)

        _, options, rows = job.calls[0]
        assert options == {"isClCommand": False, "parameters": ["x"]}
        assert rows == 4

    def test_detects_truncation_from_the_extra_row(self):
        job = FakeJob(_result(["N"], [[1], [2], [3]]))

        result = db2i.Db2iSession(_target(), job).fetch("select n from t", max_rows=2)

        assert result.rows == [[1], [2]]
        assert result.truncated is True

    def test_dataclass_results_are_accepted(self):
        from dataclasses import dataclass, field

        @dataclass
        class Result:
            metadata: dict = field(default_factory=lambda: {"columns": [{"name": "N"}]})
            data: list = field(default_factory=lambda: [{"N": 1}])

        job = FakeJob(Result())

        assert db2i.Db2iSession(_target(), job).fetch("select n from t").rows == [[1]]

    def test_columns_fall_back_to_row_keys(self):
        job = FakeJob({"data": [{"X": 1}]})

        assert db2i.Db2iSession(_target(), job).fetch("select x from t").columns == ["X"]

    def test_server_errors_become_safe_messages(self):
        error = RuntimeError({"error": "[SQL0551] Not authorized to object PEDIDO", "sql_state": "42501"})
        job = FakeJob(error=error)

        session = db2i.Db2iSession(_target(), job)

        with pytest.raises(DatabaseAccessError, match="Sem autoridade.*SQL0551"):
            session.fetch("select * from pedido")

    def test_read_only_refusal_is_explained(self):
        job = FakeJob(error=RuntimeError({"error": "Connection authorization failure", "sql_state": "42505"}))

        session = db2i.Db2iSession(_target(), job)

        with pytest.raises(DatabaseAccessError, match="somente leitura"):
            session.fetch("create table x (a int)")

    def test_error_text_without_payload_is_parsed(self):
        message = db2i.describe_query_error(
            RuntimeError("Failed to run query: {'error': 'Object not found', 'sql_state': '42704'}")
        )

        assert message == "Erro do Db2 for i (42704): Object not found"

    def test_slow_statement_is_abandoned_and_reported(self):
        job = FakeJob(delay=5)

        session = db2i.Db2iSession(_target(), job)

        with patch.object(db2i, "QUERY_TIMEOUT_SECONDS", 0.05), pytest.raises(
            DatabaseAccessError, match="limite"
        ):
            session.fetch("select * from t")

        assert job.closed.wait(2)

    def test_abandoned_session_refuses_new_statements(self):
        session = db2i.Db2iSession(_target(), FakeJob())
        session.abandon()

        with pytest.raises(DatabaseAccessError, match="encerrada"):
            session.fetch("select 1 from sysibm.sysdummy1")


class TestConnection:
    def _open(self, target, job=None, error=None):
        daemon = MagicMock(name="DaemonServer")
        job = job or MagicMock(name="SQLJob instance")
        if error:
            job.connect.side_effect = error
        sql_job = MagicMock(return_value=job)
        with patch.object(db2i, "_load_mapepire", return_value=(daemon, sql_job)):
            db2i.open_job(target)
        return daemon, sql_job

    def test_job_is_read_only_with_sql_naming_and_the_library_list(self):
        _, sql_job = self._open(_target(schemas=("VENDAS", "ESTOQUE")))

        options = sql_job.call_args.kwargs["options"]
        assert options["access"] == "read only"
        assert options["naming"] == "sql"
        assert options["translate binary"] == "true"
        assert options["libraries"] == ["VENDAS", "ESTOQUE"]

    def test_require_encrypts_without_verifying_the_self_signed_certificate(self):
        daemon, _ = self._open(_target())

        assert daemon.call_args.kwargs["ignoreUnauthorized"] is True
        assert daemon.call_args.kwargs["port"] == 8076

    def test_verify_modes_need_a_ca_file(self):
        target = _target(ssl_mode="verify-full")

        with pytest.raises(DatabaseAccessError, match="ca_file"):
            self._open(target)

    def test_verify_modes_load_the_ca(self, tmp_path):
        ca = tmp_path / "ca.pem"
        ca.write_text("-----BEGIN CERTIFICATE-----", encoding="utf-8")

        daemon, _ = self._open(_target(ssl_mode="verify-ca", options=(("ca_file", str(ca)),)))

        assert daemon.call_args.kwargs["ignoreUnauthorized"] is False
        assert daemon.call_args.kwargs["ca"].startswith("-----BEGIN")

    def test_public_policy_pins_a_checked_address_when_tls_does_not_verify_the_name(self):
        target = _target(host="as400.example.com", options=(("network_policy", "public"),))
        with patch.object(db2i, "resolve_allowed_addresses", return_value=["93.184.216.34"]):
            daemon, _ = self._open(target)

        assert daemon.call_args.kwargs["host"] == "93.184.216.34"

    def test_public_policy_keeps_the_name_when_tls_verifies_it(self, tmp_path):
        ca = tmp_path / "ca.pem"
        ca.write_text("-----BEGIN CERTIFICATE-----", encoding="utf-8")
        target = _target(
            host="as400.example.com",
            ssl_mode="verify-full",
            options=(("ca_file", str(ca)), ("network_policy", "public")),
        )
        with patch.object(db2i, "resolve_allowed_addresses", return_value=["93.184.216.34"]) as resolve:
            daemon, _ = self._open(target)

        resolve.assert_called_once()
        assert daemon.call_args.kwargs["host"] == "as400.example.com"
        assert daemon.call_args.kwargs["ignoreUnauthorized"] is False

    def test_public_policy_refuses_private_hosts_before_connecting(self):
        target = _target(host="10.0.0.5", options=(("network_policy", "public"),))
        daemon = MagicMock()
        with (
            patch.object(db2i, "_load_mapepire", return_value=(daemon, MagicMock())),
            pytest.raises(DatabaseAccessError, match="rede privada"),
        ):
            db2i.open_job(target)

        daemon.assert_not_called()

    def test_public_policy_refuses_unencrypted_modes(self):
        target = _target(ssl_mode="disable", options=(("network_policy", "public"),))

        with pytest.raises(DatabaseAccessError, match="não é permitido"):
            self._open(target)

    def test_unreadable_ca_file_is_explained(self, tmp_path):
        target = _target(ssl_mode="verify-ca", options=(("ca_file", str(tmp_path / "nada.pem")),))

        with pytest.raises(DatabaseAccessError, match="certificado"):
            self._open(target)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("The application server rejected the connection. (Password is expired.:ESTACKRO)", "expirou"),
            ("(Password is not correct for user profile.)", "usuário ou senha"),
            ("User profile ESTACKRO is disabled", "desabilitado"),
            ("server rejected WebSocket connection: HTTP 403", "iprules"),
            ("[SSL: CERTIFICATE_VERIFY_FAILED]", "TLS"),
            ("timed out during opening handshake", "tempo de conexão"),
            ("[Errno 111] Connection refused", "inacessível"),
            ("algo inesperado", "não foi possível abrir"),
        ],
    )
    def test_connection_failures_are_categorised(self, raw, expected):
        target, error = _target(), Exception(raw)

        with pytest.raises(DatabaseAccessError) as raised:
            self._open(target, error=error)

        assert expected in str(raised.value)
        assert "s3cr3t-value" not in str(raised.value)


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
        if self.fetched is not None and query.lower().startswith("select *"):
            return self.fetched
        for fragment, rows in self.answers:
            if fragment in query:
                columns = list(rows[0].keys()) if rows else []
                return QueryResult(columns=columns, rows=[[row[c] for c in columns] for row in rows])
        return QueryResult(columns=[], rows=[])

    def records(self, query, params=None, max_rows=1000):
        return self.fetch(query, params, max_rows).records()

    def one(self, query, params=None):
        found = self.records(query, params, max_rows=1)
        return found[0] if found else None


PROFILE = {"SPECIAL_AUTHORITIES": None, "GROUP_PROFILE_NAME": "*NONE", "SUPPLEMENTAL_GROUP_LIST": None}
IDENTITY = {"VERSION": "V7R5", "ROLE": "ESTACKRO"}


class TestPosture:
    def _session(self, profile=None, groups=None):
        return FakeSession(
            {
                "sysibmadm.env_sys_info": [IDENTITY],
                "authorization_name = current_user": [profile or PROFILE],
                "authorization_name in": groups or [],
            }
        )

    def test_clean_profile_passes(self):
        posture = db2i.read_posture(self._session())

        assert posture.refused == []
        assert posture.warnings() == []
        assert posture.server_version == "V7R5"

    def test_allobj_is_refused(self):
        session = self._session({**PROFILE, "SPECIAL_AUTHORITIES": "*ALLOBJ *JOBCTL"})

        target = _target()

        with pytest.raises(DatabaseAccessError, match=r"\*ALLOBJ"):
            db2i.enforce_posture(target, session)

    def test_authorities_inherited_from_a_group_are_refused_too(self):
        session = self._session(
            {**PROFILE, "GROUP_PROFILE_NAME": "PGMRS", "SUPPLEMENTAL_GROUP_LIST": "OPER"},
            groups=[{"SPECIAL_AUTHORITIES": "*SECADM"}],
        )

        posture = db2i.read_posture(session)

        assert posture.refused == ["*SECADM"]
        assert any("grupo" in note for note in posture.warnings())
        _, params, _ = session.calls[-1]
        assert params == ["PGMRS", "OPER"]

    def test_operational_authorities_only_warn(self):
        posture = db2i.read_posture(self._session({**PROFILE, "SPECIAL_AUTHORITIES": "*JOBCTL *SPLCTL"}))

        assert posture.refused == []
        assert "*JOBCTL" in posture.warnings()[0]

    def test_posture_is_read_once_per_target(self):
        session = self._session()

        for _ in range(3):
            db2i.enforce_posture(_target(), session)

        identity_reads = [c for c in session.calls if "env_sys_info" in c[0]]
        assert len(identity_reads) == 1


class TestScope:
    def test_allowlist_wins_and_is_upper_cased(self):
        session = FakeSession(target=_target(schemas=("vendas",)))

        assert db2i.library_scope(session) == ["VENDAS"]

    def test_falls_back_to_the_profile_library_list(self):
        session = FakeSession({"library_list_info": [{"SCHEMA_NAME": "APPLIB"}]}, target=_target(schemas=()))

        assert db2i.library_scope(session) == ["APPLIB"]

    def test_no_libraries_at_all_is_explained(self):
        session = FakeSession(target=_target(schemas=()))

        with pytest.raises(DatabaseAccessError, match="Nenhuma biblioteca"):
            db2i.library_scope(session)

    def test_library_outside_the_scope_is_refused(self):
        session = FakeSession()

        with pytest.raises(DatabaseAccessError, match="QIWS"):
            db2i.library_scope(session, "qiws")


TABLE = {
    "TABLE_SCHEMA": "VENDAS",
    "TABLE_NAME": "PEDIDO",
    "SYSTEM_TABLE_NAME": "PEDIDO",
    "TABLE_TYPE": "P",
    "TABLE_TEXT": "Pedidos de venda",
    "LONG_COMMENT": None,
    "BASE_TABLE_SCHEMA": None,
    "BASE_TABLE_NAME": None,
}


class TestCatalog:
    def test_overview_groups_objects_per_library(self):
        session = FakeSession(
            {
                "sysibmadm.env_sys_info": [IDENTITY],
                "qsys2.sysschemas": [{"SCHEMA_NAME": "VENDAS", "SCHEMA_TEXT": "Vendas"}],
                "group by table_schema, table_type": [
                    {"TABLE_SCHEMA": "VENDAS", "TABLE_TYPE": "P", "N": 3},
                    {"TABLE_SCHEMA": "VENDAS", "TABLE_TYPE": "L", "N": 2},
                ],
                "group by routine_schema": [{"ROUTINE_SCHEMA": "VENDAS", "N": 1}],
                "order by data_size desc": [{"TABLE_SCHEMA": "VENDAS", "TABLE_NAME": "PEDIDO", "NUMBER_ROWS": 9, "DATA_SIZE": 1}],
            }
        )

        overview = db2i.database_overview(session, _target())

        library = overview["libraries"][0]
        assert library["objects"] == {"physical file": 3, "logical file": 2}
        assert library["routines"] == 1
        assert overview["largest_tables"][0]["table_name"] == "PEDIDO"

    def test_list_tables_searches_names_and_descriptions(self):
        session = FakeSession({"from qsys2.systables t": [{**TABLE, "BASE_TABLE_NAME": None, "NUMBER_ROWS": 3}]})

        listed = db2i.list_tables(session, _target(), name_filter="pedido_x")

        query, params, _ = session.calls[-1]
        assert "upper(t.table_text) like ?" in query
        assert params == ["VENDAS", "%PEDIDO\\_X%", "%PEDIDO\\_X%", "%PEDIDO\\_X%"]
        assert listed["items"][0]["table_type"] == "physical file"

    def test_list_routines_labels_kinds(self):
        session = FakeSession({"from qsys2.sysroutines": [{"ROUTINE_NAME": "F", "ROUTINE_TYPE": "FUNCTION"}]})

        assert db2i.list_routines(session, _target())["items"][0]["routine_type"] == "function"

    def test_resolve_accepts_system_names_and_library_prefix(self):
        session = FakeSession({"from qsys2.systables": [TABLE]})

        db2i.resolve_table(session, None, "vendas.pedido")

        _, params, _ = session.calls[-1]
        assert params == ["VENDAS", "PEDIDO", "PEDIDO"]

    def test_resolve_explains_missing_and_ambiguous_tables(self):
        empty = FakeSession()

        with pytest.raises(DatabaseAccessError, match="list_tables"):
            db2i.resolve_table(empty, None, "nada")

        session = FakeSession(
            {"from qsys2.systables": [TABLE, {**TABLE, "TABLE_SCHEMA": "ESTOQUE"}]},
            target=_target(schemas=("VENDAS", "ESTOQUE")),
        )
        with pytest.raises(DatabaseAccessError, match="mais de uma biblioteca"):
            db2i.resolve_table(session, None, "PEDIDO")

    def test_describe_points_external_triggers_at_their_program(self):
        session = FakeSession(
            {
                "from qsys2.systables where table_schema in": [TABLE],
                "from qsys2.systriggers": [
                    {
                        "TRIGGER_NAME": "TRGPED",
                        "ACTION_TIMING": "BEFORE",
                        "EVENT_MANIPULATION": "INSERT",
                        "TRIGGER_PROGRAM_LIBRARY": "APPLIB",
                        "TRIGGER_PROGRAM_NAME": "PEDTRG",
                        "ACTION_STATEMENT": None,
                    }
                ],
            }
        )

        described = db2i.describe_table(session, _target(), "PEDIDO")

        trigger = described["triggers"][0]
        assert trigger["external_program"] == "APPLIB/PEDTRG"
        assert "RPG" in trigger["note"]
        assert described["kind"] == "physical file"

    def test_describe_logical_file_names_its_physical_file(self):
        logical = {**TABLE, "TABLE_NAME": "PEDIDOL1", "TABLE_TYPE": "L", "BASE_TABLE_SCHEMA": "VENDAS", "BASE_TABLE_NAME": "PEDIDO"}
        session = FakeSession({"from qsys2.systables where table_schema in": [logical]})

        assert db2i.describe_table(session, _target(), "PEDIDOL1")["based_on"] == "VENDAS.PEDIDO"

    def test_view_definition_joins_its_pieces(self):
        view = {**TABLE, "TABLE_TYPE": "V"}
        session = FakeSession(
            {
                "from qsys2.systables where table_schema in": [view],
                "from qsys2.sysviews": [{"VIEW_DEFINITION": "SELECT A "}, {"VIEW_DEFINITION": "FROM B"}],
            }
        )

        assert db2i.describe_table(session, _target(), "PEDIDO")["view_definition"] == "SELECT A FROM B"

    def test_relationships_compute_cardinality_and_requirement(self):
        session = FakeSession(
            {
                "from qsys2.sysrefcst r": [
                    {
                        "CONSTRAINT_NAME": "FK1",
                        "FROM_SCHEMA": "VENDAS",
                        "FROM_TABLE": "PEDIDO",
                        "TO_SCHEMA": "VENDAS",
                        "TO_TABLE": "CLIENTE",
                        "DELETE_RULE": "CASCADE",
                        "UPDATE_RULE": "NO ACTION",
                        "FROM_COLUMNS": "CODCLI",
                        "TO_COLUMNS": "CODCLI",
                    }
                ],
                "constraint_type in ('PRIMARY KEY', 'UNIQUE')": [],
                "is_nullable = 'Y'": [{"TABLE_SCHEMA": "VENDAS", "TABLE_NAME": "PEDIDO", "COLUMN_NAME": "CODCLI"}],
            }
        )

        item = db2i.list_relationships(session, _target())["items"][0]

        assert (item["on_delete"], item["cardinality"], item["required"]) == ("cascade", "many-to-one", False)

    def test_suggestions_use_columns_keys_and_declared_links(self):
        columns = [
            {"TABLE_SCHEMA": "VENDAS", "TABLE_NAME": "PEDIDO", "COLUMN_NAME": "NUMPED", "DATA_TYPE": "INTEGER", "LENGTH": 4, "COLUMN_TEXT": "Numero do pedido"},
            {"TABLE_SCHEMA": "VENDAS", "TABLE_NAME": "ITEMPED", "COLUMN_NAME": "NUMPED", "DATA_TYPE": "INTEGER", "LENGTH": 4, "COLUMN_TEXT": "Numero do pedido"},
        ]
        session = FakeSession(
            {
                "c.column_text from qsys2.syscolumns c join": columns,
                "union": [{"TABLE_SCHEMA": "VENDAS", "TABLE_NAME": "PEDIDO", "COLUMN_NAME": "NUMPED"}],
                "constraint_type = 'FOREIGN KEY'": [],
            }
        )

        inferred = db2i.suggest_table_relationships(session, _target())["inferred"]

        assert inferred[0]["from_table"] == "VENDAS.ITEMPED"
        assert inferred[0]["confidence"] == "alta"

    def test_rules_carry_the_rpg_caveat(self):
        session = FakeSession(
            {"join qsys2.syschkcst": [{"TABLE_NAME": "PEDIDO", "CONSTRAINT_NAME": "CK", "CHECK_CLAUSE": "VLR > 0"}]}
        )

        rules = db2i.list_rules(session, _target())

        assert rules["check_constraints"][0]["check_clause"] == "VLR > 0"
        assert "RPG" in rules["note"]
        assert "sequences" not in rules

    def test_definitions_cover_routines_views_and_external_programs(self):
        session = FakeSession(
            {
                "from qsys2.sysroutines": [
                    {"ROUTINE_SCHEMA": "VENDAS", "ROUTINE_NAME": "CALC", "ROUTINE_TYPE": "PROCEDURE",
                     "ROUTINE_BODY": "EXTERNAL", "EXTERNAL_NAME": "APPLIB/CALC", "EXTERNAL_LANGUAGE": "RPGLE",
                     "ROUTINE_TEXT": None, "ROUTINE_DEFINITION": None}
                ],
                "from qsys2.sysviews": [{"TABLE_SCHEMA": "VENDAS", "TABLE_NAME": "CALC", "VIEW_DEFINITION": "SELECT 1"}],
            }
        )

        found = db2i.get_definition(session, _target(), "calc")["definitions"]

        assert found[0]["external_program"] == "APPLIB/CALC"
        assert found[1] == {"name": "VENDAS.CALC", "kind": "view", "definition": "SELECT 1"}

    def test_unknown_definition_is_explained(self):
        session, target = FakeSession(), _target()

        with pytest.raises(DatabaseAccessError, match="list_routines"):
            db2i.get_definition(session, target, "nada")

    def test_profile_masks_columns_described_as_sensitive(self):
        session = FakeSession(
            {
                "from qsys2.systables where table_schema in": [TABLE],
                "from qsys2.syscolumns where table_schema = ? and table_name = ?": [
                    {"COLUMN_NAME": "CPFCLI", "COLUMN_TEXT": "CPF do cliente", "COLUMN_HEADING": "CPFCLI"},
                ],
                "number_rows, data_size": [{"NUMBER_ROWS": 10, "DATA_SIZE": 1}],
                "from qsys2.syscolumnstat": [
                    {"COLUMN_NAME": "CPFCLI", "NUMBER_DISTINCT_VALUES": 10, "NUMBER_NULLS": 2,
                     "LOW2KEY": "000.000.000-01", "HIGH2KEY": "999.999.999-99",
                     "NUMBER_MOST_FREQUENT_VALUES": 0, "STATISTIC_LAST_UPDATED": None},
                ],
            }
        )

        column = db2i.column_profile(session, _target(), "PEDIDO")["columns"][0]

        assert (column["approx_min"], column["approx_max"], column["null_fraction"]) == (MASK, MASK, 0.2)

    def test_profile_without_statistics_is_explained(self):
        session = FakeSession({"from qsys2.systables where table_schema in": [TABLE]})

        assert "otimizador" in db2i.column_profile(session, _target(), "PEDIDO")["note"]

    def test_sample_masks_by_description_and_quotes_identifiers(self):
        session = FakeSession(
            {
                "from qsys2.systables where table_schema in": [TABLE],
                "from qsys2.syscolumns where table_schema = ? and table_name = ?": [
                    {"COLUMN_NAME": "TELRES", "COLUMN_TEXT": "Telefone residencial", "COLUMN_HEADING": None},
                ],
            },
            fetched=QueryResult(columns=["NOME", "TELRES"], rows=[["Ana", "(11) 3333-4444"]]),
        )

        sample = db2i.sample_rows(session, _target(), "PEDIDO", limit=500)

        query, _, max_rows = session.calls[-1]
        assert query == 'select * from "VENDAS"."PEDIDO" fetch first 20 rows only'
        assert max_rows == 20
        assert sample["rows"] == [["Ana", MASK]]


class TestEngine:
    def _session_cm(self, session):
        @contextmanager
        def _open(target, check_posture=True):
            yield session

        return _open

    def test_connection_check_reports_libraries_and_refuses_privileged_profiles(self):
        session = FakeSession(
            {
                "sysibmadm.env_sys_info": [IDENTITY],
                "authorization_name = current_user": [{**PROFILE, "SPECIAL_AUTHORITIES": "*ALLOBJ"}],
                "from qsys2.sysschemas": [{"SCHEMA_NAME": "VENDAS"}],
            },
            target=_target(schemas=("VENDAS", "RH")),
        )

        with patch.object(db2i, "readonly_session", self._session_cm(session)):
            check = db2i.ENGINE.check_connection(_target(schemas=("VENDAS", "RH")))

        assert check["ok"] is False
        assert "*ALLOBJ" in check["error"]
        assert check["schemas"] == ["VENDAS"]
        assert any("RH" in warning for warning in check["warnings"])

    def test_connection_check_without_allowlist_uses_the_library_list(self):
        session = FakeSession(
            {
                "sysibmadm.env_sys_info": [IDENTITY],
                "authorization_name = current_user": [PROFILE],
                "library_list_info": [{"SCHEMA_NAME": "APPLIB"}],
            },
            target=_target(schemas=()),
        )

        with patch.object(db2i, "readonly_session", self._session_cm(session)):
            check = db2i.ENGINE.check_connection(_target(schemas=()))

        assert check["ok"] is True
        assert check["schemas"] == ["APPLIB"]
        assert any("lista de bibliotecas" in warning for warning in check["warnings"])

    def test_connection_failure_is_data(self):
        @contextmanager
        def failing(target, check_posture=True):
            raise DatabaseAccessError("servidor inacessível")
            yield

        with patch.object(db2i, "readonly_session", failing):
            assert db2i.ENGINE.check_connection(_target()) == {"ok": False, "error": "servidor inacessível"}

    def test_known_schemas_only_looks_up_the_candidates(self):
        session = FakeSession({"from qsys2.sysschemas": [{"SCHEMA_NAME": "RH"}]})

        assert db2i.ENGINE.known_schemas(session, {"RH", "P"}) == {"RH"}
        assert db2i.ENGINE.known_schemas(session, set()) == set()

    def test_free_query_columns_are_masked_by_description(self):
        session = FakeSession(
            {"select distinct column_name": [{"COLUMN_NAME": "CPFCLI", "COLUMN_TEXT": "CPF do cliente", "COLUMN_HEADING": None}]}
        )

        assert db2i.ENGINE.sensitive_columns(session, _target(), ["CPFCLI", "NOME"]) == {"CPFCLI"}
        assert db2i.ENGINE.sensitive_columns(session, _target(), []) == frozenset()

    def test_every_operation_is_implemented(self):
        from database_access.operations import OPERATIONS

        assert set(db2i.ENGINE.catalog()) == set(OPERATIONS) - {"run_readonly_query"}

"""Oracle engine with a fake session; the SQL itself was validated on Oracle Free 23ai."""

import ssl
from unittest.mock import MagicMock, patch

import pytest

from database_access import guard as generic
from database_access.engines import oracle
from database_access.guard import MASK, DatabaseAccessError, DatabaseTarget, QueryResult


def _target(**overrides) -> DatabaseTarget:
    values = {
        "alias": "erp",
        "host": "oracle.local",
        "port": 1521,
        "database": "ORCLPDB1",
        "username": "ESTACK_RO",
        "password": "s3cr3t-value",
        "ssl_mode": "require",
        "schemas": ("ERP",),
        "engine": "oracle",
    }
    values.update(overrides)
    return DatabaseTarget(**values)


@pytest.fixture(autouse=True)
def _clear_posture_cache():
    generic._posture_cache.clear()
    yield
    generic._posture_cache.clear()


def prepare(sql, limit=10, **kwargs):
    return generic.prepare_readonly_query(sql, limit, dialect=oracle.DIALECT, **kwargs)


class TestDialect:
    def test_wraps_without_as_and_with_fetch_first(self):
        assert prepare("select id from cliente", 5) == (
            "SELECT * FROM (\nselect id from cliente\n) estack_q FETCH FIRST 6 ROWS ONLY"
        )

    @pytest.mark.parametrize(
        "sql",
        [
            "select q'[a;b ' c]' from dual",
            "select q'{x)y}' from dual",
            "select Q'!it's!' from dual",
            "select nq'<a;b>' from dual",
            "select 'a@b' from dual",
        ],
    )
    def test_q_quoted_and_plain_literals_hide_their_content(self, sql):
        assert prepare(sql).startswith("SELECT * FROM (")

    def test_unterminated_q_literal_is_refused(self):
        with pytest.raises(DatabaseAccessError, match="q'"):
            prepare("select q'[aberto from dual")

    @pytest.mark.parametrize(
        ("sql", "message"),
        [
            ("select dbms_lock.sleep(5) from dual", "dbms_lock"),
            ("select sys.utl_http.request('x') from dual", "utl_http"),
            ('select "DBMS_XMLGEN".getxml(\'select 1 from dual\') from dual', "DBMS_XMLGEN"),
            ("select httpuritype('http://x').getclob() from dual", "httpuritype"),
            ("select owa_util.get_cgi_env('x') from dual", "owa_util"),
            ("select * from cliente@outro_banco", "Database links"),
            ("with function f return number is begin return 1 end select f() from dual", "WITH FUNCTION"),
            ("select * from cliente for update", "FOR UPDATE"),
            ("select seq_pedido.nextval from dual", "NEXTVAL"),
            ("select password from sys.user$", "credenciais"),
            ("select xmlquery('1' returning content) from dual", "xmlquery"),
            ("update cliente set nome = 'x'", "UPDATE"),
            ("begin null end", "BEGIN"),
        ],
    )
    def test_dangerous_constructs_are_refused(self, sql, message):
        with pytest.raises(DatabaseAccessError, match=message):
            prepare(sql)

    def test_package_names_inside_literals_are_allowed(self):
        assert "dbms_lock" in prepare("select 'dbms_lock' as texto from dual")

    def test_currval_is_allowed(self):
        assert prepare("select seq_pedido.currval from dual")


class FakeSession:
    """Answers catalog SQL by the first registered fragment the query contains."""

    def __init__(self, answers=None, fetched=None, target=None):
        self.answers = list((answers or {}).items())
        self.fetched = fetched
        self.target = target or _target()
        self.server_version = "19.21.0.0.0"
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


def _posture_session(roles=(), privileges=("CREATE SESSION",), autonomous=(), outside=(), owns=0, writes=0):
    return FakeSession(
        {
            "AUTONOMOUS": [{"ROUTINE": name} for name in autonomous],
            "select user as role": [{"ROLE": "ESTACK_RO"}],
            "session_roles": [{"ROLE": role} for role in roles],
            "session_privs": [{"PRIVILEGE": privilege} for privilege in privileges],
            "user_tables": [{"N": owns}],
            "user_tab_privs": [{"N": writes}],
            "owner not in": [{"OWNER": owner} for owner in outside],
        }
    )


class TestPosture:
    def test_read_only_user_passes(self):
        posture = oracle.read_posture(_posture_session(), ("ERP",))

        assert posture.refusal("erp") is None
        assert posture.warnings() == []
        assert posture.server_version == "19.21.0.0.0"

    def test_dba_role_is_refused_and_listed_first(self):
        session = _posture_session(roles=("DBA",), privileges=("ALTER ANY TABLE", "SELECT ANY TABLE"))
        target = _target()

        with pytest.raises(DatabaseAccessError, match=r"tem DBA, ALTER ANY TABLE\."):
            oracle.enforce_posture(target, session)

    @pytest.mark.parametrize(
        "privilege",
        ["SELECT ANY DICTIONARY", "EXECUTE ANY PROCEDURE", "INSERT ANY TABLE", "ALTER SYSTEM", "EXEMPT ACCESS POLICY"],
    )
    def test_powerful_privileges_are_refused(self, privilege):
        posture = oracle.read_posture(_posture_session(privileges=("CREATE SESSION", privilege)))

        assert posture.refused == [privilege]

    def test_select_any_table_only_warns(self):
        posture = oracle.read_posture(_posture_session(privileges=("SELECT ANY TABLE",)))

        assert posture.refused == []
        assert "SELECT ANY TABLE" in posture.warnings()[0]

    def test_executable_autonomous_routines_are_refused(self):
        posture = oracle.read_posture(_posture_session(autonomous=("ERP.REGISTRA",)))

        assert "AUTONOMOUS_TRANSACTION" in posture.refusal("erp")
        assert "ERP.REGISTRA" in posture.refusal("erp")

    def test_autonomous_check_only_counts_routines_the_user_can_run(self):
        session = _posture_session()
        oracle.read_posture(session)

        query = next(q for q, _, _ in session.calls if "AUTONOMOUS" in q)
        assert "privilege = 'EXECUTE'" in query
        assert "s.owner = user" in query

    def test_tables_readable_outside_the_allowlist_are_refused(self):
        posture = oracle.read_posture(_posture_session(outside=("HR",)), ("ERP",))

        assert "HR" in posture.refusal("erp")

    def test_outside_scope_is_not_checked_without_an_allowlist(self):
        session = _posture_session(outside=("HR",))

        posture = oracle.read_posture(session, ())

        assert posture.readable_outside_scope == ()

    def test_owner_or_writer_only_warns(self):
        posture = oracle.read_posture(_posture_session(owns=3))

        assert posture.refusal("erp") is None
        assert "escrita" in posture.warnings()[0]

    def test_posture_is_read_once_per_target(self):
        session = _posture_session()

        for _ in range(3):
            oracle.enforce_posture(_target(), session)

        assert len([c for c in session.calls if "session_privs" in c[0]]) == 1


class TestConnection:
    @pytest.mark.parametrize("ssl_mode", ["require", "verify-ca", "verify-full"])
    def test_every_tls_mode_verifies_the_certificate_and_the_name(self, ssl_mode):
        params = oracle._connect_params(_target(ssl_mode=ssl_mode))

        assert params["protocol"] == "tcps"
        assert params["service_name"] == "ORCLPDB1"
        assert params["ssl_context"].verify_mode == ssl.CERT_REQUIRED
        assert params["ssl_context"].check_hostname is True
        assert params["ssl_server_dn_match"] is True

    def test_ca_file_is_loaded_into_the_context(self, tmp_path):
        target = _target(options=(("ca_file", str(tmp_path / "ca.pem")),))
        with patch.object(oracle.ssl, "create_default_context") as create:
            oracle._connect_params(target)

        create.assert_called_once_with(cafile=str(tmp_path / "ca.pem"))

    def test_disable_is_plain_tcp(self):
        params = oracle._connect_params(_target(ssl_mode="disable"))

        assert "protocol" not in params
        assert "ssl_context" not in params

    def test_public_policy_checks_the_address_and_keeps_the_name_for_tls(self):
        target = _target(host="oracle.example.com", options=(("network_policy", "public"),))
        with patch.object(oracle, "resolve_allowed_addresses", return_value=["93.184.216.34"]) as resolve:
            params = oracle._connect_params(target)

        resolve.assert_called_once_with(target)
        assert params["host"] == "oracle.example.com"

    def test_public_policy_refuses_plain_tcp(self):
        target = _target(ssl_mode="disable", options=(("network_policy", "public"),))

        with pytest.raises(DatabaseAccessError, match="não é permitido"):
            oracle._connect_params(target)

    def test_public_policy_refuses_private_hosts(self):
        target = _target(host="10.0.0.9", options=(("network_policy", "public"),))

        with pytest.raises(DatabaseAccessError, match="rede privada"):
            oracle._connect_params(target)

    def test_connection_failures_are_explained_without_the_password(self):
        driver = MagicMock()
        driver.Error = type("DriverError", (Exception,), {})
        detail = MagicMock(full_code="ORA-01017", message="ORA-01017: invalid credential")
        driver.connect.side_effect = driver.Error(detail)
        target = _target(ssl_mode="disable")
        with (
            patch.object(oracle, "_load_driver", return_value=driver),
            pytest.raises(DatabaseAccessError, match="usuário ou senha recusados") as raised,
        ):
            oracle.open_connection(target)

        assert "s3cr3t-value" not in str(raised.value)

    def test_single_owner_becomes_the_current_schema(self):
        driver = MagicMock()
        driver.Error = Exception
        connection = driver.connect.return_value
        with patch.object(oracle, "_load_driver", return_value=driver):
            oracle.open_connection(_target(ssl_mode="disable"))

        connection.cursor.return_value.execute.assert_called_once_with('alter session set current_schema = "ERP"')
        assert connection.call_timeout == oracle.CALL_TIMEOUT_MS


class TestSession:
    def _session(self, cursor):
        driver = MagicMock()
        driver.Error = type("DriverError", (Exception,), {})
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        return oracle.OracleSession(_target(), connection, driver), connection, driver

    def test_every_statement_runs_in_a_read_only_transaction_and_rolls_back(self):
        cursor = MagicMock(description=[("ID",), ("NOME",)])
        cursor.fetchmany.return_value = [(1, "Ana"), (2, "Bia")]
        session, connection, _ = self._session(cursor)

        result = session.fetch("select id, nome from cliente", max_rows=1)

        assert cursor.execute.call_args_list[0].args == ("set transaction read only",)
        assert result.columns == ["ID", "NOME"]
        assert result.rows == [[1, "Ana"]]
        assert result.truncated
        connection.rollback.assert_called_once()

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("DPY-4024", "limite"),
            ("ORA-00918", "alias diferente"),
            ("ORA-00942", "não existe ou o usuário não tem permissão"),
            ("ORA-01722", "ORA-01722"),
        ],
    )
    def test_driver_errors_become_safe_messages(self, code, expected):
        cursor = MagicMock()
        session, connection, driver = self._session(cursor)
        cursor.execute.side_effect = [None, driver.Error(MagicMock(full_code=code, message=f"{code}: detalhe"))]

        with pytest.raises(DatabaseAccessError, match=expected):
            session.fetch("select 1 from dual")

        connection.rollback.assert_called_once()


class TestScope:
    def test_allowlist_wins_and_is_upper_cased(self):
        assert oracle.owner_scope(FakeSession(target=_target(schemas=("erp",)))) == ["ERP"]

    def test_falls_back_to_the_owners_the_user_sees(self):
        session = FakeSession({"all_users": [{"USERNAME": "ERP"}]}, target=_target(schemas=()))

        assert oracle.owner_scope(session) == ["ERP"]

    def test_owner_outside_the_scope_is_refused(self):
        session = FakeSession()

        with pytest.raises(DatabaseAccessError, match="HR"):
            oracle.owner_scope(session, "hr")


class TestCatalog:
    def test_table_in_two_owners_asks_for_the_owner(self):
        session = FakeSession(
            {
                "from all_objects o": [
                    {"OWNER": "ERP", "OBJECT_NAME": "CLIENTE", "OBJECT_TYPE": "TABLE"},
                    {"OWNER": "VENDAS", "OBJECT_NAME": "CLIENTE", "OBJECT_TYPE": "TABLE"},
                ]
            },
            target=_target(schemas=("ERP", "VENDAS")),
        )

        with pytest.raises(DatabaseAccessError, match="mais de um owner"):
            oracle.resolve_table(session, None, "cliente")

    def test_materialized_view_wins_over_its_container_table(self):
        session = FakeSession(
            {
                "from all_objects o": [
                    {"OWNER": "ERP", "OBJECT_NAME": "MV_VENDAS", "OBJECT_TYPE": "MATERIALIZED VIEW"},
                    {"OWNER": "ERP", "OBJECT_NAME": "MV_VENDAS", "OBJECT_TYPE": "TABLE"},
                ]
            }
        )

        assert oracle.resolve_table(session, None, "mv_vendas")["KIND"] == "MATERIALIZED VIEW"

    def test_package_spec_and_body_are_rebuilt_from_source_lines(self):
        lines = [
            {"OWNER": "ERP", "NAME": "PKG", "TYPE": "PACKAGE", "LINE": 1, "TEXT": "package pkg as\n"},
            {"OWNER": "ERP", "NAME": "PKG", "TYPE": "PACKAGE", "LINE": 2, "TEXT": "end;"},
            {"OWNER": "ERP", "NAME": "PKG", "TYPE": "PACKAGE BODY", "LINE": 1, "TEXT": "package body pkg wrapped\na000000"},
        ]
        session = FakeSession({"from all_source": lines})

        definitions = oracle.get_definition(session, _target(), "pkg")["definitions"]

        assert [d["kind"] for d in definitions] == ["package spec", "package body"]
        assert definitions[0]["definition"] == "package pkg as\nend;"
        assert "wrapped" in definitions[1]["note"]

    def test_sample_rows_masks_columns_described_as_sensitive(self):
        fetched = QueryResult(columns=["ID", "DOCCLI"], rows=[[1, "123.456.789-09"]])
        session = FakeSession(
            {
                "from all_objects o": [{"OWNER": "ERP", "OBJECT_NAME": "CLIENTE", "OBJECT_TYPE": "TABLE"}],
                "from all_col_comments": [{"COLUMN_NAME": "DOCCLI", "COMMENTS": "CPF do cliente"}],
            },
            fetched=fetched,
        )

        sample = oracle.sample_rows(session, _target(), "cliente", limit=99)

        assert sample["rows"] == [[1, MASK]]
        assert 'fetch first 20 rows only' in session.calls[-1][0]

    def test_sensitive_result_columns_are_found_by_comment(self):
        session = FakeSession({"from all_col_comments": [{"COLUMN_NAME": "DOCCLI", "COMMENTS": "CPF do cliente"}]})

        found = oracle.ENGINE.sensitive_columns(session, _target(), ["NOME", "DOCCLI"])

        assert found == frozenset({"DOCCLI"})

    @pytest.mark.parametrize(
        ("data_type", "raw", "expected"),
        [
            ("NUMBER", bytes([0xC1, 0x02]), 1.0),
            ("NUMBER", bytes([0xC2, 0x02, 0x18]), 123.0),
            ("NUMBER", bytes([0x3E, 0x64, 0x66]), -1.0),
            ("NUMBER", b"\x80", 0.0),
            ("VARCHAR2", b"Ana", "Ana"),
            ("DATE", bytes([120, 126, 10, 19, 1, 1, 1]), "2026-10-19"),
            ("BLOB", b"\x00", None),
        ],
    )
    def test_statistics_bounds_are_decoded(self, data_type, raw, expected):
        assert oracle._decode_bound(data_type, raw) == expected


class TestEngine:
    def test_check_connection_reports_owners_and_refusal(self):
        session = _posture_session(roles=("DBA",))
        session.answers.append(("all_users u", [{"USERNAME": "ERP"}]))

        class _Manager:
            def __enter__(self):
                return session

            def __exit__(self, *exc):
                return False

        with patch.object(oracle, "readonly_session", return_value=_Manager()):
            check = oracle.ENGINE.check_connection(_target())

        assert check["ok"] is False
        assert "DBA" in check["error"]
        assert check["schemas"] == ["ERP"]

    def test_engine_is_registered(self):
        from database_access.engines.base import get_engine

        assert get_engine("oracle") is oracle.ENGINE


CATALOG = {
    "select o.owner, o.object_name, o.object_type": [
        {"OWNER": "ERP", "OBJECT_NAME": "PEDIDO", "OBJECT_TYPE": "TABLE"}
    ],
    "group by owner, object_type": [
        {"OWNER": "ERP", "OBJECT_TYPE": "TABLE", "N": 3},
        {"OWNER": "ERP", "OBJECT_TYPE": "PACKAGE", "N": 1},
    ],
    "order by num_rows desc": [{"OWNER": "ERP", "TABLE_NAME": "PEDIDO", "NUM_ROWS": 2}],
    "select user as role": [{"ROLE": "ESTACK_RO"}],
    "left join all_tab_comments c on c.owner = o.owner": [
        {"OWNER": "ERP", "TABLE_NAME": "PEDIDO", "OBJECT_TYPE": "TABLE", "COMMENTS": "Pedidos", "NUM_ROWS": 2},
        {"OWNER": "ERP", "TABLE_NAME": "VW", "OBJECT_TYPE": "VIEW", "COMMENTS": None, "NUM_ROWS": None},
    ],
    "all_procedures": [
        {
            "OWNER": "ERP",
            "OBJECT_NAME": "PKG_PEDIDO",
            "OBJECT_TYPE": "PACKAGE",
            "STATUS": "VALID",
            "SUBPROGRAMS": "TOTAL, FATURAR",
        }
    ],
    "t.iot_type": [
        {"COMMENTS": "Pedidos", "NUM_ROWS": 2, "LAST_ANALYZED": None, "PARTITIONED": "NO", "TEMPORARY": "N", "IOT_TYPE": None}
    ],
    "from all_tab_cols c": [
        {"COLUMN_NAME": "NUMPED", "DATA_TYPE": "NUMBER", "NULLABLE": "N", "DATA_DEFAULT": None, "COMMENTS": None},
        {"COLUMN_NAME": "STATUS", "DATA_TYPE": "VARCHAR2", "NULLABLE": "Y", "DATA_DEFAULT": "'ABERTO'  \n", "COMMENTS": "Situação"},
    ],
    "c.constraint_type in ('P', 'U', 'C')": [
        {"CONSTRAINT_NAME": "PK_PEDIDO", "CONSTRAINT_TYPE": "P", "STATUS": "ENABLED", "CHECK_CLAUSE": None, "COLUMNS": "NUMPED"}
    ],
    "as from_columns": [
        {
            "CONSTRAINT_NAME": "FK_PEDIDO_CLIENTE",
            "FROM_OWNER": "ERP",
            "FROM_TABLE": "PEDIDO",
            "TO_OWNER": "ERP",
            "TO_TABLE": "CLIENTE",
            "DELETE_RULE": "CASCADE",
            "STATUS": "ENABLED",
            "FROM_COLUMNS": "ID_CLIENTE",
            "TO_COLUMNS": "ID",
        }
    ],
    "from all_indexes i where i.table_owner = :owner": [
        {"INDEX_NAME": "IX_PEDIDO_CLIENTE", "INDEX_TYPE": "NORMAL", "UNIQUENESS": "NONUNIQUE", "STATUS": "VALID", "COLUMNS": "ID_CLIENTE"}
    ],
    "from all_triggers": [
        {
            "OWNER": "ERP",
            "TRIGGER_NAME": "TRG",
            "TABLE_NAME": "PEDIDO",
            "TRIGGER_TYPE": "BEFORE EACH ROW",
            "TRIGGERING_EVENT": "UPDATE",
            "STATUS": "ENABLED",
            "WHEN_CLAUSE": None,
            "TRIGGER_BODY": "begin null; end;",
        }
    ],
    "from all_policies": [],
    "from all_dependencies": [{"OWNER": "ERP", "NAME": "PKG_PEDIDO", "TYPE": "PACKAGE BODY"}],
    "select text_vc from all_views": [{"TEXT_VC": "select 1 from dual"}],
    "refresh_mode": [
        {"QUERY": "select 2 from dual", "REFRESH_MODE": "DEMAND", "REFRESH_METHOD": "FORCE", "LAST_REFRESH_DATE": None}
    ],
    "union": [{"OWNER": "ERP", "TABLE_NAME": "PEDIDO", "COLUMN_NAME": "NUMPED"}],
    "as columns from all_constraints c where c.owner in": [{"OWNER": "ERP", "TABLE_NAME": "PEDIDO", "COLUMNS": "NUMPED"}],
    "nullable = 'Y'": [{"OWNER": "ERP", "TABLE_NAME": "PEDIDO", "COLUMN_NAME": "STATUS"}],
    "join all_tables t on t.owner = c.owner": [
        {"OWNER": "ERP", "TABLE_NAME": "PEDIDO", "COLUMN_NAME": "NUMPED", "DATA_TYPE": "NUMBER", "DATA_LENGTH": 22, "COMMENTS": None},
        {"OWNER": "ERP", "TABLE_NAME": "ITEMPED", "COLUMN_NAME": "NUMPED", "DATA_TYPE": "NUMBER", "DATA_LENGTH": 22, "COMMENTS": None},
    ],
    "c.constraint_type = 'R'": [],
    "check_clause, c.status": [
        {"OWNER": "ERP", "TABLE_NAME": "PEDIDO", "CONSTRAINT_NAME": "CK", "CHECK_CLAUSE": "qtd > 0", "STATUS": "ENABLED"}
    ],
    "default_length > 0": [
        {"OWNER": "ERP", "TABLE_NAME": "PEDIDO", "COLUMN_NAME": "STATUS", "DATA_DEFAULT": "'ABERTO' ", "IDENTITY_COLUMN": "NO"}
    ],
    "from all_sequences": [{"SEQUENCE_OWNER": "ERP", "SEQUENCE_NAME": "SEQ_PEDIDO", "LAST_NUMBER": 1}],
    "from all_source": [],
    "select owner, view_name, text_vc": [{"OWNER": "ERP", "VIEW_NAME": "VW", "TEXT_VC": "select 1 from dual"}],
    "select owner, mview_name, query": [{"OWNER": "ERP", "MVIEW_NAME": "VW", "QUERY": "select 2 from dual"}],
    "from all_tab_col_statistics": [
        {
            "COLUMN_NAME": "NUMPED",
            "DATA_TYPE": "NUMBER",
            "NUM_DISTINCT": 2,
            "NUM_NULLS": 0,
            "LOW_VALUE": bytes([0xC1, 0x02]),
            "HIGH_VALUE": bytes([0xC1, 0x03]),
            "HISTOGRAM": "NONE",
            "LAST_ANALYZED": None,
        },
        {
            "COLUMN_NAME": "DOC",
            "DATA_TYPE": "VARCHAR2",
            "NUM_DISTINCT": 2,
            "NUM_NULLS": 1,
            "LOW_VALUE": b"111",
            "HIGH_VALUE": b"999",
            "HISTOGRAM": "NONE",
            "LAST_ANALYZED": None,
        },
    ],
    "from all_col_comments where owner = :owner": [{"COLUMN_NAME": "DOC", "COMMENTS": "CPF do cliente"}],
    "from all_users where username in": [{"USERNAME": "ERP"}],
    "from all_users u": [{"USERNAME": "ERP"}],
}


def _catalog_session(**overrides):
    return FakeSession({**CATALOG, **overrides})


def _single(kind):
    return {"select o.owner, o.object_name, o.object_type": [{"OWNER": "ERP", "OBJECT_NAME": "VW", "OBJECT_TYPE": kind}]}


class TestCatalogOperations:
    def test_overview_counts_objects_per_owner(self):
        overview = oracle.database_overview(_catalog_session(), _target())

        assert overview["owners"] == [{"owner": "ERP", "objects": {"table": 3, "package": 1}}]
        assert overview["largest_tables"][0]["table_name"] == "PEDIDO"
        assert overview["connected_as"] == "ESTACK_RO"

    def test_list_tables_names_the_kind_and_filters_by_name(self):
        session = _catalog_session()

        listed = oracle.list_tables(session, _target(), name_filter="ped_")

        assert [item["object_type"] for item in listed["items"]] == ["table", "view"]
        _, params, _ = session.calls[-1]
        assert params["b1"] == "%PED\\_%"

    def test_list_routines_shows_package_subprograms(self):
        session = FakeSession({"all_procedures": CATALOG["all_procedures"]})

        listed = oracle.list_routines(session, _target(), name_filter="pkg")

        assert listed["items"][0]["object_type"] == "package"
        assert listed["items"][0]["subprograms"] == "TOTAL, FATURAR"

    def test_long_listings_are_capped(self):
        rows = [
            {"OWNER": "ERP", "OBJECT_NAME": f"P{i}", "OBJECT_TYPE": "PACKAGE"}
            for i in range(oracle.MAX_LISTED_OBJECTS + 1)
        ]
        session = FakeSession({"all_procedures": rows})

        listed = oracle.list_routines(session, _target())

        assert listed["truncated"] is True
        assert listed["count"] == oracle.MAX_LISTED_OBJECTS

    def test_describe_table_gathers_keys_indexes_triggers_and_usage(self):
        described = oracle.describe_table(_catalog_session(), _target(), "erp.pedido")

        assert described["table"] == "ERP.PEDIDO"
        assert described["comment"] == "Pedidos"
        assert described["columns"][1]["data_default"] == "'ABERTO'"
        assert described["constraints"][0]["constraint_name"] == "PK_PEDIDO"
        assert described["foreign_keys"][0]["to_table"] == "CLIENTE"
        assert described["indexes"][0]["index_name"] == "IX_PEDIDO_CLIENTE"
        assert described["triggers"][0]["trigger_body"] == "begin null; end;"
        assert described["used_by"][0]["name"] == "PKG_PEDIDO"
        assert "row_level_security_policies" not in described

    def test_describe_view_includes_its_query(self):
        described = oracle.describe_table(_catalog_session(**_single("VIEW")), _target(), "vw")

        assert described["view_definition"] == "select 1 from dual"

    def test_describe_materialized_view_includes_query_and_refresh(self):
        described = oracle.describe_table(_catalog_session(**_single(oracle.MVIEW)), _target(), "vw")

        assert described["view_definition"] == "select 2 from dual"
        assert described["refresh"]["mode"] == "DEMAND"

    def test_unknown_table_is_explained(self):
        session = FakeSession()

        with pytest.raises(DatabaseAccessError, match="não encontrada no owner 'ERP'"):
            oracle.resolve_table(session, "erp", "nada")

    def test_list_relationships_reports_cardinality_and_requirement(self):
        relationships = oracle.list_relationships(_catalog_session(), _target())

        (item,) = relationships["items"]
        assert item["from_table"] == "ERP.PEDIDO"
        assert item["on_delete"] == "cascade"
        assert item["cardinality"] == "many-to-one"
        assert item["required"] is True

    def test_no_foreign_keys_suggests_inference(self):
        session = FakeSession()

        assert "suggest_relationships" in oracle.list_relationships(session, _target())["hint"]

    def test_suggest_relationships_infers_links_by_name(self):
        suggested = oracle.suggest_table_relationships(_catalog_session(), _target())

        (link,) = suggested["inferred"]
        assert (link["from_table"], link["to_table"]) == ("ERP.ITEMPED", "ERP.PEDIDO")

    def test_rules_for_the_whole_scope_include_sequences(self):
        rules = oracle.list_rules(_catalog_session(), _target())

        assert rules["check_constraints"][0]["check_clause"] == "qtd > 0"
        assert rules["defaults_and_identity"][0]["data_default"] == "'ABERTO'"
        assert rules["sequences"][0]["sequence_name"] == "SEQ_PEDIDO"
        assert "PL/SQL" in rules["note"]

    def test_rules_for_one_table_skip_sequences(self):
        rules = oracle.list_rules(_catalog_session(), _target(), table="pedido")

        assert "sequences" not in rules
        assert rules["triggers"][0]["trigger_name"] == "TRG"

    def test_view_and_materialized_view_definitions(self):
        definitions = oracle.get_definition(_catalog_session(), _target(), "erp.vw")["definitions"]

        assert [d["kind"] for d in definitions] == ["view", "materialized view"]

    def test_missing_definition_is_explained(self):
        session = FakeSession()
        target = _target()

        with pytest.raises(DatabaseAccessError, match="Nenhum package"):
            oracle.get_definition(session, target, "nada")

    def test_column_profile_decodes_bounds_and_masks_sensitive_ones(self):
        profile = oracle.column_profile(_catalog_session(), _target(), "pedido")

        numped, doc = profile["columns"]
        assert (numped["approx_min"], numped["approx_max"]) == (1.0, 2.0)
        assert doc["approx_min"] == MASK
        assert doc["null_fraction"] == 0.5

    def test_column_profile_without_statistics_explains(self):
        session = _catalog_session(**{"from all_tab_col_statistics": []})

        assert "DBMS_STATS" in oracle.column_profile(session, _target(), "pedido")["note"]


class TestSessionLifecycle:
    def _session(self):
        driver = MagicMock()
        driver.Error = type("DriverError", (Exception,), {})
        connection = MagicMock()
        return oracle.OracleSession(_target(), connection, driver), connection, driver

    def test_statements_without_rows_return_an_empty_result(self):
        session, connection, _ = self._session()
        connection.cursor.return_value.__enter__.return_value.description = None

        assert session.fetch("select 1 from dual").columns == []

    def test_failed_rollback_and_close_are_logged_not_raised(self):
        session, connection, driver = self._session()
        connection.cursor.return_value.__enter__.return_value.description = None
        connection.rollback.side_effect = driver.Error("lost")
        connection.close.side_effect = driver.Error("lost")

        session.fetch("select 1 from dual")
        session.close()

        connection.close.assert_called_once()

    def test_one_returns_the_first_record(self):
        session, connection, _ = self._session()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.description = [("ROLE",)]
        cursor.fetchmany.return_value = [("ESTACK_RO",)]

        assert session.one("select user as role from dual") == {"ROLE": "ESTACK_RO"}

    def test_one_without_rows_is_none(self):
        session, connection, _ = self._session()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.description = [("ROLE",)]
        cursor.fetchmany.return_value = []

        assert session.one("select user as role from dual") is None

    def test_missing_owner_for_current_schema_is_tolerated(self):
        driver = MagicMock()
        driver.Error = type("DriverError", (Exception,), {})
        driver.connect.return_value.cursor.return_value.execute.side_effect = driver.Error("ORA-01435")
        target = _target(ssl_mode="disable")
        with patch.object(oracle, "_load_driver", return_value=driver):
            session = oracle.open_connection(target)

        assert isinstance(session, oracle.OracleSession)

    def test_readonly_session_checks_posture_and_closes(self):
        session = _posture_session()
        session.close = MagicMock()
        target = _target()
        with patch.object(oracle, "open_connection", return_value=session), oracle.readonly_session(target) as opened:
            assert opened is session

        session.close.assert_called_once()

    def test_driver_loader_keeps_lobs_as_text(self):
        driver = oracle._load_driver()

        assert driver.defaults.fetch_lobs is False


class _Opened:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *exc):
        return False


class TestEngineMethods:
    def test_check_connection_warns_about_owners_without_tables(self):
        session = _posture_session()
        session.answers.append(("all_users u", [{"USERNAME": "ERP"}]))
        target = _target(schemas=("ERP", "FIN"))

        with patch.object(oracle, "readonly_session", return_value=_Opened(session)):
            check = oracle.ENGINE.check_connection(target)

        assert check["ok"] is True
        assert "FIN" in check["warnings"][0]

    def test_check_connection_reports_connection_errors(self):
        target = _target()

        with patch.object(oracle, "readonly_session", side_effect=DatabaseAccessError("sem rede")):
            check = oracle.ENGINE.check_connection(target)

        assert check == {"ok": False, "error": "sem rede"}

    def test_known_schemas_upper_cases_candidates(self):
        session = FakeSession({"from all_users where username in": [{"USERNAME": "ERP"}]})

        assert oracle.ENGINE.known_schemas(session, {"erp", "hr"}) == {"ERP"}

    def test_known_schemas_without_candidates_is_empty(self):
        assert oracle.ENGINE.known_schemas(FakeSession(), set()) == set()

    def test_sensitive_columns_without_names_is_empty(self):
        assert oracle.ENGINE.sensitive_columns(FakeSession(), _target(), []) == frozenset()

    def test_sensitive_columns_without_scope_is_empty(self):
        session = FakeSession(target=_target(schemas=()))

        assert oracle.ENGINE.sensitive_columns(session, _target(), ["DOC"]) == frozenset()

    def test_open_session_and_catalog_are_wired(self):
        target = _target()
        with patch.object(oracle, "readonly_session") as opened:
            oracle.ENGINE.open_session(target, check_posture=False)

        opened.assert_called_once_with(target, check_posture=False)
        assert set(oracle.ENGINE.catalog()) >= {"describe_table", "get_definition", "sample_rows"}

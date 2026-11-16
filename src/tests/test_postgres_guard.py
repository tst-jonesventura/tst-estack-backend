"""The guard between a model-written query and a client database."""

import datetime
import decimal
import socket
import uuid
from unittest.mock import MagicMock, patch

import psycopg2
import pytest

from database_access import guard as generic
from database_access import network
from database_access.engines import postgres as guard
from database_access.engines.postgres import (
    DIALECT,
    Posture,
    ReadOnlySession,
    describe_driver_error,
)
from database_access.guard import (
    MASK,
    MAX_CELL_CHARS,
    MAX_QUERY_ROWS,
    DatabaseAccessError,
    DatabaseTarget,
    QueryResult,
    is_sensitive_column,
    shape_cell,
    shape_rows,
)


def prepare_readonly_query(sql, limit, **kwargs):
    return generic.prepare_readonly_query(sql, limit, dialect=DIALECT, **kwargs)


def code_skeleton(sql):
    return generic.code_skeleton(sql, DIALECT)


def _target(**overrides) -> DatabaseTarget:
    values = {
        "alias": "principal",
        "host": "db.example.com",
        "port": 5432,
        "database": "app",
        "username": "estack_reader",
        "password": "s3cr3t-value",
        "ssl_mode": "require",
        "schemas": (),
        "options": ((network.NETWORK_POLICY_OPTION, network.PUBLIC_NETWORK_ONLY),),
    }
    values.update(overrides)
    return DatabaseTarget(**values)


@pytest.fixture(autouse=True)
def _clear_posture_cache():
    guard._posture_cache.clear()
    yield
    guard._posture_cache.clear()


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, type: [
            (socket.AF_INET, type, socket.IPPROTO_TCP, "", ("93.184.216.34", port))
        ],
    )


class TestPrepareReadonlyQuery:
    def test_wraps_the_query_as_a_bounded_subquery(self):
        wrapped = prepare_readonly_query("select id from pedido", 10)

        assert wrapped == "SELECT * FROM (\nselect id from pedido\n) AS estack_q LIMIT 11"

    def test_trailing_semicolons_are_tolerated(self):
        wrapped = prepare_readonly_query("select 1 ;; \n", 5)

        assert "select 1\n)" in wrapped

    @pytest.mark.parametrize(
        "sql",
        [
            "select 1; drop table pedido",
            "select 1) as q; delete from pedido; select * from (select 1",
            "select 1; -- fim\nselect 2",
        ],
    )
    def test_a_second_statement_is_refused(self, sql):
        with pytest.raises(DatabaseAccessError, match="única consulta"):
            prepare_readonly_query(sql, 10)

    @pytest.mark.parametrize(
        "sql",
        ["select U&'\\0070g_sleep'", 'select u&"pg_sleep"(1)', "select U&'x' uescape '!'"],
    )
    def test_unicode_escapes_are_refused(self, sql):
        with pytest.raises(DatabaseAccessError, match="escape Unicode"):
            prepare_readonly_query(sql, 10)

    def test_unicode_escape_text_inside_a_literal_is_allowed(self):
        assert "u&x" in prepare_readonly_query("select 'u&x', a&b from t", 10)

    @pytest.mark.parametrize(
        "function", ["table_to_xml", "schema_to_xml", "database_to_xml", "cursor_to_xml"]
    )
    def test_xml_export_functions_are_blocked(self, function):
        with pytest.raises(DatabaseAccessError, match=function):
            prepare_readonly_query(f"select {function}('t', true, false, '')", 10)

    def test_closing_the_wrapper_parenthesis_is_refused(self):
        with pytest.raises(DatabaseAccessError, match="parênteses"):
            prepare_readonly_query("select 1) as q union select (2", 10)

    def test_unbalanced_parentheses_are_refused(self):
        with pytest.raises(DatabaseAccessError, match="desbalanceados"):
            prepare_readonly_query("select count(*", 10)

    @pytest.mark.parametrize(
        "sql",
        [
            "select ';' as separador",
            "select ')' as fecha",
            "select $$ ; ) $$ as corpo",
            "select $tag$ ; $tag$",
            "select E'it\\'s ; )'",
            'select 1 as "a;b)"',
        ],
    )
    def test_semicolons_and_parentheses_inside_literals_are_fine(self, sql):
        wrapped = prepare_readonly_query(sql, 10)

        assert wrapped.startswith("SELECT * FROM (\n" + sql)

    @pytest.mark.parametrize("sql", ["select 1 /* ; ) */", "select 1 -- ; )"])
    def test_trailing_comments_are_dropped(self, sql):
        assert prepare_readonly_query(sql, 10).startswith("SELECT * FROM (\nselect 1\n)")

    @pytest.mark.parametrize(
        "sql",
        [
            "select * from pg_catalog.pg_authid",
            "select rolname from pg_shadow",
            'select * from "pg_user_mappings"',
            "select subconninfo from pg_subscription",
            "with x as (select * from PG_AUTHID) select * from x",
        ],
    )
    def test_catalogs_holding_credentials_are_refused(self, sql):
        with pytest.raises(DatabaseAccessError, match="credenciais"):
            prepare_readonly_query(sql, 10)

    def test_similar_names_are_not_mistaken_for_credential_catalogs(self):
        prepare_readonly_query("select pg_authid_x, my_pg_shadow from t", 10)

    @pytest.mark.parametrize(
        "sql",
        [
            "update vendas.pedido set valor = 0",
            "delete from pedido",
            "insert into pedido values (1)",
            "create table x (id int)",
            "call proc()",
            "grant select on x to y",
            "copy pedido to stdout",
        ],
    )
    def test_statements_that_are_not_queries_are_refused_with_a_clear_message(self, sql):
        with pytest.raises(DatabaseAccessError, match="Só são aceitas consultas de leitura"):
            prepare_readonly_query(sql, 10)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1",
            "  with x as (select 1) select * from x",
            "(select 1) union (select 2)",
            "values (1), (2)",
            "table vendas.pedido",
            "-- comentário\nselect 1",
        ],
    )
    def test_query_forms_are_accepted(self, sql):
        prepare_readonly_query(sql, 10)

    def test_quoted_function_name_is_still_blocked(self):
        with pytest.raises(DatabaseAccessError, match="pg_sleep"):
            prepare_readonly_query('select "pg_sleep"(30)', 10)

    def test_schema_qualified_function_name_is_still_blocked(self):
        with pytest.raises(DatabaseAccessError, match="pg_sleep"):
            prepare_readonly_query("select pg_catalog.pg_sleep(30)", 10)

    def test_literal_at_the_end_is_not_cut(self):
        wrapped = prepare_readonly_query("select $$)$$", 10)

        assert "select $$)$$\n)" in wrapped

    def test_trailing_comment_cannot_swallow_the_wrapper(self):
        wrapped = prepare_readonly_query("select 1 -- comentário", 10)

        assert wrapped.endswith("\n) AS estack_q LIMIT 11")

    @pytest.mark.parametrize(
        "sql",
        [
            "select pg_sleep(30)",
            "select PG_SLEEP (1)",
            "select set_config('default_transaction_read_only', 'off', false)",
            "select pg_terminate_backend(123)",
            "select nextval('pedido_id_seq')",
            "select * from dblink('host=x', 'select 1') as t(a int)",
            "select 'x'pg_sleep(1)",
        ],
    )
    def test_functions_with_side_effects_are_refused(self, sql):
        with pytest.raises(DatabaseAccessError, match="não é permitida"):
            prepare_readonly_query(sql, 10)

    def test_function_names_inside_strings_are_not_refused(self):
        prepare_readonly_query("select 'pg_sleep(1)' as texto", 10)

    def test_empty_query_is_refused(self):
        with pytest.raises(DatabaseAccessError, match="vazia"):
            prepare_readonly_query("   ", 10)

    def test_oversized_query_is_refused(self):
        sql = "select " + "1," * 20_000 + "1"

        with pytest.raises(DatabaseAccessError, match="longa demais"):
            prepare_readonly_query(sql, 10)

    @pytest.mark.parametrize(
        "sql", ["select 1 /* sem fim", "select 'sem fim", "select $$ sem fim", 'select "x']
    )
    def test_unterminated_literals_are_refused(self, sql):
        with pytest.raises(DatabaseAccessError):
            prepare_readonly_query(sql, 10)

    def test_limit_is_capped(self):
        wrapped = prepare_readonly_query("select 1", 10_000)

        assert wrapped.endswith(f"LIMIT {MAX_QUERY_ROWS + 1}")

    def test_limit_has_a_floor(self):
        assert prepare_readonly_query("select 1", 0).endswith("LIMIT 2")

    def test_schema_outside_the_allowlist_is_refused(self):
        with pytest.raises(DatabaseAccessError, match="financeiro"):
            prepare_readonly_query(
                "select * from financeiro.folha",
                10,
                allowed_schemas=("vendas",),
                known_schemas={"vendas", "financeiro"},
            )

    def test_quoted_schema_outside_the_allowlist_is_refused(self):
        with pytest.raises(DatabaseAccessError, match="Financeiro"):
            prepare_readonly_query(
                'select * from "Financeiro".folha',
                10,
                allowed_schemas=("vendas",),
                known_schemas={"vendas", "Financeiro"},
            )

    def test_allowed_and_system_schemas_pass(self):
        prepare_readonly_query(
            "select p.id from vendas.pedido p join pg_catalog.pg_class c on true",
            10,
            allowed_schemas=("vendas",),
            known_schemas={"vendas", "financeiro", "pg_catalog"},
        )

    def test_table_aliases_are_not_mistaken_for_schemas(self):
        prepare_readonly_query(
            "select p.id from pedido p",
            10,
            allowed_schemas=("vendas",),
            known_schemas={"vendas", "financeiro"},
        )

    def test_no_allowlist_means_no_schema_check(self):
        prepare_readonly_query(
            "select * from financeiro.folha", 10, known_schemas={"financeiro"}
        )


class TestCodeSkeleton:
    def test_keeps_the_length_of_the_input(self):
        sql = "select 'a;b', $$x$$, \"Id\" -- c\n from t /* d */"

        skeleton = code_skeleton(sql)
        assert len(skeleton) == len(sql)

    def test_blanks_literals_but_keeps_quoted_identifiers(self):
        skeleton = code_skeleton("select 'a;b' from \"Vendas\".t")

        assert ";" not in skeleton
        assert '"Vendas".t' in skeleton

    def test_dollar_inside_identifier_is_not_a_quote(self):
        skeleton = code_skeleton("select a$b$ from t; select 1")

        assert ";" in skeleton

    def test_nested_block_comments(self):
        skeleton = code_skeleton("select 1 /* a /* b */ ; */ ; select 2")

        assert skeleton.count(";") == 1


class TestSensitiveColumns:
    @pytest.mark.parametrize(
        "column",
        [
            "senha",
            "password_hash",
            "userEmail",
            "e_mail",
            "cpf",
            "cnpj_cliente",
            "telefone_celular",
            "api_key",
            "access_token",
            "num_rg",
            "chave_pix",
            "data_nascimento",
            "endereco_entrega",
            "card",
        ],
    )
    def test_detects_sensitive_names(self, column):
        assert is_sensitive_column(column)

    @pytest.mark.parametrize(
        "column", ["id", "org_id", "cardinality", "status", "valor_total", "created_at", None, ""]
    )
    def test_leaves_ordinary_names_alone(self, column):
        assert not is_sensitive_column(column)


class TestShapeCell:
    def test_sensitive_column_is_masked(self):
        assert shape_cell("cpf", "123.456.789-00") == MASK

    def test_null_stays_null_even_when_sensitive(self):
        assert shape_cell("email", None) is None

    def test_email_inside_free_text_is_redacted(self):
        value = shape_cell("observacao", "Falar com ana.silva@empresa.com.br amanhã")

        assert "ana.silva" not in value
        assert MASK in value

    def test_long_text_without_email_is_scanned_quickly(self):
        import time

        text = "a." * 50_000
        started = time.perf_counter()

        assert shape_cell("observacao", text).startswith("a.a.")
        assert time.perf_counter() - started < 1

    def test_email_is_redacted_whole(self):
        assert shape_cell("nota", "x ana.b+1@mail.co.uk y") == f"x {MASK} y"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Contato: (ana@x.com).", f"Contato: ({MASK})."),
            ('{"email": "ana@x.com", "n": 1}', f'{{"email": "{MASK}", "n": 1}}'),
            ("linha1\nana@x.com\tfim", f"linha1\n{MASK}\tfim"),
            ("@usuario no twitter", "@usuario no twitter"),
            ("a@b sem domínio", "a@b sem domínio"),
            ("a@@x.com", "a@@x.com"),
            ("a@x..com", "a@x..com"),
        ],
    )
    def test_email_detection_edges(self, text, expected):
        assert generic.redact_emails(text) == expected

    def test_formatted_documents_inside_free_text_are_redacted(self):
        value = shape_cell("nota", "CPF 123.456.789-00, CNPJ 12.345.678/0001-90")

        assert "123.456" not in value
        assert "12.345.678" not in value

    def test_json_values_are_serialised_and_redacted(self):
        value = shape_cell("payload", {"contato": "a@b.com", "itens": [1, 2]})

        assert isinstance(value, str)
        assert "a@b.com" not in value

    def test_long_text_is_trimmed(self):
        value = shape_cell("descricao", "x" * (MAX_CELL_CHARS + 50))

        assert value.startswith("x" * MAX_CELL_CHARS)
        assert "+50 caracteres" in value

    def test_binary_is_described_not_dumped(self):
        assert shape_cell("arquivo", b"\x00\x01\x02") == "<binário, 3 bytes>"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (decimal.Decimal("10.50"), "10.50"),
            (datetime.date(2026, 1, 2), "2026-01-02"),
            (uuid.UUID(int=0), "00000000-0000-0000-0000-000000000000"),
            (datetime.timedelta(hours=1), "1:00:00"),
            (7, 7),
            (True, True),
        ],
    )
    def test_values_become_json_safe(self, value, expected):
        assert shape_cell("valor", value) == expected


class TestShapeRows:
    def test_masks_by_column_name(self):
        payload = shape_rows(
            QueryResult(columns=["id", "email"], rows=[[1, "a@b.com"], [2, None]])
        )

        assert payload["rows"] == [[1, MASK], [2, None]]
        assert payload["row_count"] == 2
        assert "truncated" not in payload

    def test_flags_truncation_from_the_row_cap(self):
        payload = shape_rows(QueryResult(columns=["id"], rows=[[1]], truncated=True))

        assert payload["truncated"] is True
        assert "Refine" in payload["note"]

    def test_stops_at_the_response_budget(self):
        rows = [[i, "y" * 400] for i in range(1000)]

        payload = shape_rows(QueryResult(columns=["id", "texto"], rows=rows))

        assert payload["truncated"] is True
        assert payload["row_count"] < 1000


class TestDriverErrors:
    def _pg_error(self, code, primary, hint=None):
        exc = MagicMock(spec=psycopg2.Error)
        exc.pgcode = code
        exc.diag = MagicMock(message_primary=primary, message_hint=hint)
        return exc

    def test_timeout_explains_the_limit(self):
        message = describe_driver_error(self._pg_error("57014", "canceling statement"), "db")

        assert "15s" in message

    def test_read_only_violation(self):
        message = describe_driver_error(self._pg_error("25006", "read-only"), "db")

        assert "somente leitura" in message

    def test_permission_denied(self):
        message = describe_driver_error(
            self._pg_error("42501", "permission denied for table folha"), "db"
        )

        assert message == (
            "Sem permissão para ler este objeto: permission denied for table folha. "
            "Algumas colunas podem estar com acesso revogado: em vez de select *, "
            "liste explicitamente as colunas legíveis (veja 'readable' em "
            "describe_table)."
        )

    def test_other_errors_keep_code_message_and_hint(self):
        message = describe_driver_error(
            self._pg_error("42P01", 'relation "x" does not exist', "Confira o nome"), "db"
        )

        assert "42P01" in message
        assert "Confira o nome" in message

    def test_error_without_diagnostics_names_only_the_database(self):
        message = describe_driver_error(psycopg2.ProgrammingError("x"), "principal")

        assert message == "Erro ao consultar o banco 'principal'."

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ('FATAL:  password authentication failed for user "estack_reader"', "senha"),
            ("timeout expired", "tempo de conexão"),
            ('could not translate host name "db" to address', "host não encontrado"),
            ('FATAL:  database "app" does not exist', "inexistente"),
            ("SSL SYSCALL error", "SSL"),
            ("connection refused", "inacessível"),
        ],
    )
    def test_connection_failures_are_categorised(self, raw, expected):
        message = describe_driver_error(psycopg2.OperationalError(raw), "principal")

        assert expected in message
        assert "principal" in message


class TestConnection:
    def test_options_force_read_only_and_timeouts(self):
        options = guard._connection_options(_target())

        assert "default_transaction_read_only=on" in options
        assert "standard_conforming_strings=on" in options
        assert "statement_timeout=15000" in options
        assert "lock_timeout=" in options
        assert "search_path" not in options

    def test_allowlist_narrows_the_search_path(self):
        options = guard._connection_options(_target(schemas=("vendas", "Estoque")))

        assert '-c search_path="vendas","Estoque"' in options

    def test_connect_uses_ssl_and_read_only_session(self):
        connection = MagicMock()
        with patch.object(guard.psycopg2, "connect", return_value=connection) as connect:
            guard.open_connection(_target(ssl_mode="verify-full"))

        kwargs = connect.call_args.kwargs
        assert kwargs["sslmode"] == "verify-full"
        assert kwargs["host"] == "db.example.com"
        assert kwargs["hostaddr"] == "93.184.216.34"
        assert kwargs["port"] == "5432"
        assert kwargs["connect_timeout"] == guard.CONNECT_TIMEOUT_SECONDS
        connection.set_session.assert_called_once_with(readonly=True, autocommit=False)

    def test_multiple_public_addresses_are_pinned_in_resolver_order(self):
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 5432))
            for ip in ("93.184.216.34", "1.1.1.1")
        ]
        with patch.object(
            socket, "getaddrinfo", return_value=addresses
        ), patch.object(guard.psycopg2, "connect", return_value=MagicMock()) as connect:
            guard.open_connection(_target(host="db.example.com"))

        kwargs = connect.call_args.kwargs
        assert kwargs["host"] == "db.example.com,db.example.com"
        assert kwargs["hostaddr"] == "93.184.216.34,1.1.1.1"
        assert kwargs["port"] == "5432,5432"

    def test_duplicate_public_addresses_are_deduplicated(self):
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 5432))
            for ip in ("93.184.216.34", "1.1.1.1", "93.184.216.34")
        ]
        with patch.object(
            socket, "getaddrinfo", return_value=addresses
        ), patch.object(guard.psycopg2, "connect", return_value=MagicMock()) as connect:
            guard.open_connection(_target(host="db.example.com"))

        kwargs = connect.call_args.kwargs
        assert kwargs["host"] == "db.example.com,db.example.com"
        assert kwargs["hostaddr"] == "93.184.216.34,1.1.1.1"
        assert kwargs["port"] == "5432,5432"

    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",
            "10.0.0.1",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.169.254",
            "::1",
            "::ffff:127.0.0.1",
        ],
    )
    def test_private_addresses_are_refused_before_connecting(self, address, monkeypatch):
        target = _target()
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, port, type: [
                (
                    socket.AF_INET6 if ":" in address else socket.AF_INET,
                    type,
                    0,
                    "",
                    (address, port),
                )
            ],
        )
        with patch.object(guard.psycopg2, "connect") as connect, pytest.raises(
            DatabaseAccessError, match="host aponta para um endereço"
        ):
            guard.open_connection(target)

        connect.assert_not_called()

    def test_hostname_resolving_to_private_address_is_refused(self):
        target = _target(host="internal.example.com")
        with patch.object(
            socket,
            "getaddrinfo",
            return_value=[
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("10.1.2.3", 5432),
                )
            ],
        ), patch.object(guard.psycopg2, "connect") as connect, pytest.raises(
            DatabaseAccessError, match="host aponta para um endereço"
        ):
            guard.open_connection(target)

        connect.assert_not_called()

    def test_dns_failure_uses_host_not_found_message(self):
        target = _target()
        with patch.object(
            socket, "getaddrinfo", side_effect=socket.gaierror("not found")
        ), patch.object(guard.psycopg2, "connect") as connect, pytest.raises(
            DatabaseAccessError, match="host não encontrado"
        ):
            guard.open_connection(target)

        connect.assert_not_called()

    def test_without_network_policy_skips_resolution_and_hostaddr(self):
        with patch.object(socket, "getaddrinfo") as resolve, patch.object(
            guard.psycopg2, "connect", return_value=MagicMock()
        ) as connect:
            guard.open_connection(_target(ssl_mode="disable", options=()))

        resolve.assert_not_called()
        assert "hostaddr" not in connect.call_args.kwargs

    @pytest.mark.parametrize("ssl_mode", ["disable", "allow", "prefer"])
    def test_insecure_ssl_mode_is_refused_without_override(self, ssl_mode):
        target = _target(ssl_mode=ssl_mode)
        with patch.object(guard.psycopg2, "connect") as connect, pytest.raises(
            DatabaseAccessError,
            match=f"O modo SSL '{ssl_mode}' não é permitido",
        ):
            guard.open_connection(target)

        connect.assert_not_called()

    def test_connection_failure_never_carries_the_password(self):
        error = psycopg2.OperationalError("password authentication failed")
        target = _target()

        with patch.object(guard.psycopg2, "connect", side_effect=error), pytest.raises(
            DatabaseAccessError
        ) as raised:
            guard.open_connection(target)

        assert "s3cr3t-value" not in str(raised.value)

    def test_target_repr_hides_the_password(self):
        assert "s3cr3t-value" not in repr(_target())


def _fake_connection(rows, columns=("v",)):
    cursor = MagicMock()
    cursor.description = [MagicMock(name=c) for c in columns]
    for description, column in zip(cursor.description, columns):
        description.name = column
    cursor.fetchmany.return_value = rows
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    return connection, cursor


class TestReadOnlySession:
    def test_every_statement_is_rolled_back(self):
        connection, _ = _fake_connection([(1,)])

        ReadOnlySession(_target(), connection).fetch("select 1")

        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()

    def test_detects_truncation_from_the_extra_row(self):
        connection, cursor = _fake_connection([(1,), (2,), (3,)])

        result = ReadOnlySession(_target(), connection).fetch("select v", max_rows=2)

        cursor.fetchmany.assert_called_once_with(3)
        assert result.rows == [[1], [2]]
        assert result.truncated is True

    @pytest.mark.parametrize(
        ("source_column", "value", "expected"),
        [("senha", "segredo", MASK), ("nome", "Alice", "Alice")],
    )
    def test_resolves_source_column_for_alias_masking(
        self, source_column, value, expected
    ):
        connection, cursor = _fake_connection([(value,)], columns=("x",))
        cursor.description[0].table_oid = 42
        cursor.description[0].table_column = 3
        cursor.fetchall.return_value = [(source_column,)]

        result = ReadOnlySession(_target(), connection).fetch(
            "select source as x", resolve_sources=True
        )

        assert result.source_columns == [source_column]
        assert shape_rows(result)["rows"] == [[expected]]
        assert cursor.execute.call_count == 2
        assert "pg_attribute" in cursor.execute.call_args.args[0]
        assert cursor.execute.call_args.args[1] == {"oids": [42], "numbers": [3]}
        connection.rollback.assert_called_once()

    def test_columns_without_source_metadata_use_output_name_only(self):
        connection, cursor = _fake_connection([("safe value",)], columns=("x",))
        cursor.description[0].table_oid = 0
        cursor.description[0].table_column = 0

        result = ReadOnlySession(_target(), connection).fetch(
            "select expression as x", resolve_sources=True
        )

        assert result.source_columns == [None]
        assert shape_rows(result)["rows"] == [["safe value"]]
        assert cursor.execute.call_count == 1
        assert shape_rows(QueryResult(columns=["senha"], rows=[["segredo"]]))["rows"] == [
            [MASK]
        ]

    def test_driver_error_becomes_a_safe_message_and_rolls_back(self):
        connection, cursor = _fake_connection([])
        cursor.execute.side_effect = psycopg2.ProgrammingError("boom")

        session = ReadOnlySession(_target(), connection)

        with pytest.raises(DatabaseAccessError):
            session.fetch("select pg_sleep(99)")

        connection.rollback.assert_called_once()


def _posture(**overrides) -> Posture:
    values = {
        "server_version": "16.2",
        "role": "estack_reader",
        "is_superuser": False,
        "can_write": False,
        "reads_all_data": True,
        "server_file_access": False,
        "readable_outside_scope": (),
    }
    values.update(overrides)
    return Posture(**values)


def _open(target=None, check_posture=True):
    with guard.readonly_session(target or _target(), check_posture=check_posture):
        pass


class TestPosture:
    def test_superuser_is_refused(self):
        with patch.object(guard, "open_connection", return_value=MagicMock()), patch.object(
            guard, "read_posture", return_value=_posture(is_superuser=True, role="postgres")
        ), pytest.raises(DatabaseAccessError, match="superusuário"):
            _open()

    @pytest.mark.parametrize(
        ("overrides", "schemas", "expected"),
        [
            (
                {"is_superuser": True},
                (),
                "O usuário 'estack_reader' do banco 'principal' é superusuário.",
            ),
            (
                {"server_file_access": True},
                (),
                (
                    "O usuário 'estack_reader' do banco 'principal' pertence a "
                    "pg_read_server_files/pg_write_server_files/"
                    "pg_execute_server_program"
                ),
            ),
            (
                {
                    "readable_outside_scope": (
                        "rh",
                        "vendas",
                        "financeiro",
                        "interno",
                        "auditoria",
                        "outro",
                    )
                },
                ("public",),
                "fora dos schemas liberados (rh, vendas, financeiro, interno, auditoria)",
            ),
        ],
    )
    def test_each_posture_refusal_is_enforced(self, overrides, schemas, expected):
        target = _target(schemas=schemas)
        with (
            patch.object(guard, "open_connection", return_value=MagicMock()),
            patch.object(guard, "read_posture", return_value=_posture(**overrides)) as read,
            pytest.raises(DatabaseAccessError) as raised,
            guard.readonly_session(target),
        ):
            pass

        assert expected in str(raised.value)
        assert read.call_args.args[1] == schemas

    def test_connection_is_closed_even_when_refused(self):
        connection = MagicMock()
        with patch.object(guard, "open_connection", return_value=connection), patch.object(
            guard, "read_posture", return_value=_posture(is_superuser=True)
        ), pytest.raises(DatabaseAccessError):
            _open()

        connection.close.assert_called_once()

    def test_posture_is_read_once_per_target(self):
        with patch.object(guard, "open_connection", return_value=MagicMock()), patch.object(
            guard, "read_posture", return_value=_posture()
        ) as read:
            for _ in range(3):
                _open()

        assert read.call_count == 1

    def test_posture_check_can_be_skipped_for_the_connection_test(self):
        with patch.object(guard, "open_connection", return_value=MagicMock()), patch.object(
            guard, "read_posture"
        ) as read:
            _open(check_posture=False)

        read.assert_not_called()

    def test_warnings_recommend_a_read_only_role(self):
        warnings = _posture(can_write=True, reads_all_data=False).warnings()

        assert len(warnings) == 2
        assert any("pg_read_all_data" in note for note in warnings)

    def test_pg_read_all_data_warning_is_omitted_for_allowlisted_schemas(self):
        warnings = _posture(reads_all_data=False).warnings(("public",))

        assert all("pg_read_all_data" not in note for note in warnings)

    def test_read_posture_passes_schema_allowlist_and_parses_scope(self):
        session = MagicMock()
        session.one.side_effect = [
            {"v": 160000},
            {
                "server_version": "16.2",
                "role": "r",
                "is_superuser": False,
                "can_write": False,
                "reads_all_data": False,
                "server_file_access": True,
                "readable_outside_scope": ["rh", "vendas"],
            },
        ]

        posture = guard.read_posture(session, ("public",))

        assert session.one.call_args_list[1].args[1] == {"schemas": ["public"]}
        assert posture.server_file_access is True
        assert posture.readable_outside_scope == ("rh", "vendas")

    def test_uses_pre_14_query_on_old_servers(self):
        session = MagicMock()
        session.one.side_effect = [
            {"v": 130000},
            {
                "server_version": "13.1",
                "role": "r",
                "is_superuser": False,
                "can_write": False,
                "reads_all_data": False,
                "server_file_access": False,
                "readable_outside_scope": [],
            },
        ]

        posture = guard.read_posture(session)

        assert "pg_read_all_data" not in session.one.call_args_list[1].args[0]
        assert posture.server_version == "13.1"

    def test_missing_role_row_is_an_error(self):
        session = MagicMock()
        session.one.side_effect = [{"v": 160000}, None]

        with pytest.raises(DatabaseAccessError):
            guard.read_posture(session)

    def test_readable_sensitive_columns_filters_and_sorts_with_schema_scope(self):
        session = MagicMock()
        session.records.return_value = [
            {
                "schema_name": "z",
                "table_name": "t",
                "column_name": "cpf",
            },
            {
                "schema_name": "a",
                "table_name": "cliente",
                "column_name": "nome",
            },
            {
                "schema_name": "a",
                "table_name": "cliente",
                "column_name": "email",
            },
        ]

        columns = guard.readable_sensitive_columns(session, ("public",))

        assert columns == ["a.cliente.email", "z.t.cpf"]
        session.records.assert_called_once_with(
            guard.READABLE_SENSITIVE_COLUMNS_SQL,
            {"schemas": ["public"]},
            max_rows=5000,
        )
        assert "limit 5000" in guard.READABLE_SENSITIVE_COLUMNS_SQL.lower()

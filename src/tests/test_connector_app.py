"""The connector that runs on the client's network: configuration, jobs and loops."""

from unittest.mock import MagicMock

import httpx
import pytest
from estack_connector import runner as runner_module
from estack_connector.client import EstackClient, UnauthorizedError
from estack_connector.config import ConfigError, load_config
from estack_connector.runner import Connector

from database_access.guard import DatabaseAccessError

ENV = {
    "ESTACK_URL": "https://estack.example.com",
    "ESTACK_CONNECTOR_TOKEN": "estk_token",
    "ERP_PASSWORD": "s3cr3t-value",
}

VALID = """
databases:
  - alias: erp
    engine: db2i
    host: as400.local
    username: ESTACKRO
    password_env: ERP_PASSWORD
    schemas: [VENDAS]
    options:
      transport: odbc
  - alias: pg
    engine: postgres
    host: 10.0.0.1
    database: app
    username: reader
    password_env: ERP_PASSWORD
"""


ENTRY = (
    "  - {{alias: {alias}, engine: postgres, host: h, username: u, "
    "password_env: ERP_PASSWORD{extra}}}\n"
)


def _write(tmp_path, content):
    path = tmp_path / "databases.yaml"
    path.write_text(content, encoding="utf-8")
    return path


class TestConfig:
    def test_loads_targets_with_passwords_from_the_environment(self, tmp_path):
        config = load_config(_write(tmp_path, VALID), ENV)

        erp = config.target("ERP")
        assert erp.engine == "db2i"
        assert erp.port == 8076
        assert erp.password == "s3cr3t-value"
        assert erp.schemas == ("VENDAS",)
        assert erp.option("transport") == "odbc"
        assert config.target("pg").port == 5432
        assert config.estack_url == "https://estack.example.com/"

    def test_password_is_never_in_the_repr(self, tmp_path):
        config = load_config(_write(tmp_path, VALID), ENV)

        assert "s3cr3t-value" not in repr(config.targets)

    @pytest.mark.parametrize(
        ("env_drop", "message"),
        [
            ("ESTACK_URL", "ESTACK_URL"),
            ("ESTACK_CONNECTOR_TOKEN", "ESTACK_CONNECTOR_TOKEN"),
            ("ERP_PASSWORD", "ERP_PASSWORD"),
        ],
    )
    def test_missing_environment_is_explained(self, tmp_path, env_drop, message):
        env = {key: value for key, value in ENV.items() if key != env_drop}
        path = _write(tmp_path, VALID)

        with pytest.raises(ConfigError, match=message):
            load_config(path, env)

    @pytest.mark.parametrize(
        ("content", "message"),
        [
            ("databases: []", "Nenhum banco"),
            ("databases:\n  - alias: x\n    engine: mssql\n", "não suportado"),
            ("databases:\n  - alias: x\n    engine: postgres\n", "host"),
            (f"databases:\n{ENTRY.format(alias='x', extra='')}{ENTRY.format(alias='X', extra='')}", "repetidos"),
            (f"databases:\n{ENTRY.format(alias='x', extra=', schemas: [a;b]')}", "schema inválido"),
            (f"databases:\n{ENTRY.format(alias='x', extra=', options: [1]')}", "options"),
        ],
    )
    def test_invalid_files_are_explained(self, tmp_path, content, message):
        path = _write(tmp_path, content)

        with pytest.raises(ConfigError, match=message):
            load_config(path, ENV)

    def test_missing_file_is_explained(self, tmp_path):
        missing = tmp_path / "nada.yaml"

        with pytest.raises(ConfigError, match="não encontrado.*DB_HOST"):
            load_config(missing, ENV)

    def test_without_a_file_the_database_comes_from_db_variables(self, tmp_path):
        env = {
            **ENV,
            "DB_HOST": "pub400.com",
            "DB_USER": "ESTACKRO",
            "DB_PASSWORD": "db-secret",
            "DB_SCHEMAS": "VENDAS, ESTOQUE",
            "MAX_PARALLEL_JOBS": "1",
        }

        config = load_config(tmp_path / "nada.yaml", env)

        (target,) = config.targets
        assert (target.alias, target.engine, target.host, target.port) == ("erp", "db2i", "pub400.com", 8076)
        assert (target.username, target.password, target.ssl_mode) == ("ESTACKRO", "db-secret", "require")
        assert target.schemas == ("VENDAS", "ESTOQUE")
        assert config.max_parallel_jobs == 1

    def test_db_variables_accept_sql_server_on_its_default_port(self, tmp_path):
        env = {
            **ENV,
            "DB_HOST": "sql.local",
            "DB_USER": "estack_ro",
            "DB_PASSWORD": "db-secret",
            "DB_ENGINE": "sqlserver",
            "DB_NAME": "loja",
        }

        target = load_config(tmp_path / "nada.yaml", env).target("erp")

        assert (target.engine, target.port, target.database) == ("sqlserver", 1433, "loja")

    def test_db_variables_accept_oracle_on_its_default_port(self, tmp_path):
        env = {
            **ENV,
            "DB_HOST": "oracle.local",
            "DB_USER": "ESTACK_RO",
            "DB_PASSWORD": "db-secret",
            "DB_ENGINE": "oracle",
            "DB_NAME": "ORCLPDB1",
        }

        target = load_config(tmp_path / "nada.yaml", env).target("erp")

        assert (target.engine, target.port, target.database) == ("oracle", 1521, "ORCLPDB1")

    def test_db_variables_accept_another_engine(self, tmp_path):
        env = {
            **ENV,
            "DB_HOST": "10.0.0.1",
            "DB_USER": "reader",
            "DB_PASSWORD": "db-secret",
            "DB_ENGINE": "postgres",
            "DB_NAME": "app",
            "DB_ALIAS": "portal",
            "DB_SSL_MODE": "verify-full",
        }

        target = load_config(tmp_path / "nada.yaml", env).target("portal")

        assert (target.engine, target.port, target.database, target.ssl_mode) == (
            "postgres",
            5432,
            "app",
            "verify-full",
        )

    def test_db_variables_require_user_and_password(self, tmp_path):
        env = {**ENV, "DB_HOST": "pub400.com", "DB_USER": "ESTACKRO"}

        with pytest.raises(ConfigError, match="DB_PASSWORD"):
            load_config(tmp_path / "nada.yaml", env)

    def test_a_mounted_file_wins_over_db_variables(self, tmp_path):
        env = {**ENV, "DB_HOST": "ignored", "DB_USER": "u", "DB_PASSWORD": "p"}

        config = load_config(_write(tmp_path, VALID), env)

        assert [target.alias for target in config.targets] == ["erp", "pg"]


def _connector(tmp_path, run_job=None, clock=None):
    config = load_config(_write(tmp_path, VALID), ENV)
    client = MagicMock(spec=EstackClient)
    connector = Connector(
        config,
        client,
        run_job=run_job or MagicMock(return_value={"ok": True}),
        clock=clock or (lambda: 0.0),
    )
    return connector, client


def _job(alias="erp", operation="list_tables"):
    return {"id": "j1", "database_alias": alias, "operation": operation, "arguments": {"schema": "VENDAS"}}


class TestHandle:
    def test_runs_the_job_and_submits_the_result(self, tmp_path):
        run_job = MagicMock(return_value={"items": []})
        connector, client = _connector(tmp_path, run_job)

        connector.handle(_job())

        target, operation, args = run_job.call_args.args
        assert (target.alias, operation, args) == ("erp", "list_tables", {"schema": "VENDAS"})
        client.submit.assert_called_once_with("j1", {"items": []}, None)

    def test_unknown_alias_is_reported(self, tmp_path):
        connector, client = _connector(tmp_path)

        connector.handle(_job(alias="outro"))

        _, result, error = client.submit.call_args.args
        assert result is None
        assert "outro" in error

    def test_refusals_are_sent_as_errors(self, tmp_path):
        run_job = MagicMock(side_effect=DatabaseAccessError("A função 'QCMDEXC' não é permitida"))
        connector, client = _connector(tmp_path, run_job)

        connector.handle(_job())

        client.submit.assert_called_once_with("j1", None, "A função 'QCMDEXC' não é permitida")

    def test_unexpected_failures_are_hidden(self, tmp_path):
        run_job = MagicMock(side_effect=RuntimeError("password=s3cr3t-value"))
        connector, client = _connector(tmp_path, run_job)

        connector.handle(_job())

        error = client.submit.call_args.args[2]
        assert "s3cr3t-value" not in error
        assert "list_tables" in error

    def test_refused_token_on_submit_stops_the_connector(self, tmp_path):
        connector, client = _connector(tmp_path)
        client.submit.side_effect = UnauthorizedError("revogado")

        connector.handle(_job())

        assert connector.stopped.is_set()

    def test_network_failure_on_submit_is_survived(self, tmp_path):
        connector, client = _connector(tmp_path)
        client.submit.side_effect = httpx.ConnectError("down")

        connector.handle(_job())

        assert not connector.stopped.is_set()


class TestReport:
    def test_reports_every_database_with_its_status(self, tmp_path):
        run_job = MagicMock(
            return_value={"ok": True, "server_version": "V7R5M0", "schemas": ["VENDAS", "RH"]}
        )
        connector, _ = _connector(tmp_path, run_job)

        report = connector.database_report()

        erp, pg = report
        assert (erp["alias"], erp["engine"], erp["ok"]) == ("erp", "db2i", True)
        assert erp["schemas"] == ["VENDAS"]
        assert pg["schemas"] == ["VENDAS", "RH"]
        assert pg["database"] == "app"

    def test_status_is_cached_between_heartbeats(self, tmp_path):
        run_job = MagicMock(return_value={"ok": True})
        now = {"t": 0.0}
        connector, _ = _connector(tmp_path, run_job, clock=lambda: now["t"])

        connector.database_report()
        connector.database_report()
        now["t"] = runner_module.STATUS_REFRESH_SECONDS + 1
        connector.database_report()

        assert run_job.call_count == 4

    def test_failed_check_is_reported_not_raised(self, tmp_path):
        run_job = MagicMock(side_effect=DatabaseAccessError("host não encontrado"))
        connector, _ = _connector(tmp_path, run_job)

        report = connector.database_report()

        assert report[0]["ok"] is False
        assert report[0]["error"] == "host não encontrado"

    def test_unexpected_check_failure_is_hidden(self, tmp_path):
        run_job = MagicMock(side_effect=RuntimeError("password=s3cr3t-value"))
        connector, _ = _connector(tmp_path, run_job)

        report = connector.database_report()

        assert "s3cr3t-value" not in report[0]["error"]

    def test_heartbeat_sends_version_and_report(self, tmp_path):
        connector, client = _connector(tmp_path)

        connector.send_heartbeat()

        version, databases = client.heartbeat.call_args.args
        assert version == runner_module.VERSION
        assert len(databases) == 2


class TestLoops:
    def test_poll_dispatches_jobs(self, tmp_path):
        connector, client = _connector(tmp_path)
        client.poll_jobs.return_value = [_job()]

        assert connector.poll_once() == 1
        connector.stop()

    def test_refused_token_stops_polling(self, tmp_path):
        connector, client = _connector(tmp_path)
        client.poll_jobs.side_effect = UnauthorizedError("revogado")

        connector.poll_loop()

        assert connector.stopped.is_set()

    def test_network_errors_back_off_and_retry(self, tmp_path):
        connector, client = _connector(tmp_path)
        waits = []
        calls = {"n": 0}

        def poll():
            calls["n"] += 1
            if calls["n"] >= 4:
                connector.stopped.set()
                return []
            raise httpx.ConnectError("down")

        client.poll_jobs.side_effect = poll
        connector.stopped.wait = lambda seconds: waits.append(seconds)

        connector.poll_loop()

        assert waits == [1, 2, 4]

    def test_heartbeat_loop_stops_on_refused_token(self, tmp_path):
        connector, client = _connector(tmp_path)
        client.heartbeat.side_effect = UnauthorizedError("revogado")

        connector.heartbeat_loop()

        assert connector.stopped.is_set()

    def test_heartbeat_loop_survives_network_errors(self, tmp_path):
        connector, client = _connector(tmp_path)
        client.heartbeat.side_effect = httpx.ConnectError("down")
        connector.stopped.wait = lambda seconds: connector.stopped.set()

        connector.heartbeat_loop()

        assert client.heartbeat.call_count == 1


class TestMain:
    def test_invalid_configuration_exits_with_code_2(self, tmp_path, monkeypatch):
        from estack_connector import __main__ as entry

        monkeypatch.setenv("CONNECTOR_CONFIG", str(tmp_path / "nada.yaml"))

        assert entry.main() == 2

    def test_runs_the_connector_and_closes_the_client(self, tmp_path, monkeypatch):
        from unittest.mock import patch

        from estack_connector import __main__ as entry

        path = _write(tmp_path, VALID)
        monkeypatch.setenv("CONNECTOR_CONFIG", str(path))
        for key, value in ENV.items():
            monkeypatch.setenv(key, value)

        with patch.object(entry, "EstackClient") as client_class, patch.object(
            entry, "Connector"
        ) as connector_class, patch.object(entry.signal, "signal"):
            assert entry.main() == 0

        connector_class.return_value.run.assert_called_once()
        client_class.return_value.close.assert_called_once()

    def test_keyboard_interrupt_stops_cleanly(self, tmp_path, monkeypatch):
        from unittest.mock import patch

        from estack_connector import __main__ as entry

        monkeypatch.setenv("CONNECTOR_CONFIG", str(_write(tmp_path, VALID)))
        for key, value in ENV.items():
            monkeypatch.setenv(key, value)

        with patch.object(entry, "EstackClient") as client_class, patch.object(
            entry, "Connector"
        ) as connector_class, patch.object(entry.signal, "signal"):
            connector_class.return_value.run.side_effect = KeyboardInterrupt
            assert entry.main() == 0

        connector_class.return_value.stop.assert_called_once()
        client_class.return_value.close.assert_called_once()


def _client(handler):
    transport = httpx.MockTransport(handler)
    http = httpx.Client(base_url="https://estack.example.com/", transport=transport)
    return EstackClient("https://estack.example.com/", "estk_token", "1.0.0", http=http)


class TestClient:
    def test_sends_the_token_and_parses_jobs(self):
        seen = {}

        def handler(request):
            seen["auth"] = request.headers["Authorization"]
            seen["url"] = str(request.url)
            return httpx.Response(200, json=[_job()])

        jobs = _client(handler).poll_jobs(wait=5)

        assert jobs[0]["id"] == "j1"
        assert seen["auth"] == "Bearer estk_token"
        assert seen["url"].endswith("/connector/jobs?wait=5")

    def test_unauthorized_is_its_own_error(self):
        client = _client(lambda request: httpx.Response(401))

        with pytest.raises(UnauthorizedError):
            client.heartbeat("1.0.0", [])

    def test_server_errors_raise(self):
        client = _client(lambda request: httpx.Response(500))

        with pytest.raises(httpx.HTTPStatusError):
            client.poll_jobs()

    def test_result_for_a_closed_job_is_ignored(self):
        client = _client(lambda request: httpx.Response(404))

        client.submit("j1", {"ok": 1}, None)

    def test_result_is_posted(self):
        seen = {}

        def handler(request):
            seen["body"] = request.content
            return httpx.Response(204)

        client = _client(handler)
        client.submit("j1", None, "erro")
        client.close()

        assert b'"error":"erro"' in seen["body"].replace(b" ", b"")

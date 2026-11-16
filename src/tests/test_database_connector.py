"""The connector channel: tokens, the job queue, and how projects use connector databases."""

import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from database import models
from database.database import get_session
from database_access.guard import DatabaseAccessError, DatabaseTarget
from routers.crud import database_connector_router, project_database_router
from services import database_connector_service as connectors
from services import project_database_service as project_databases
from utils.bearer_auth import (
    check_project_access,
    check_project_ownership,
    retrieve_admin_data,
)

REPORTED = [
    {
        "alias": "erp",
        "engine": "db2i",
        "database": "S21A1B2C",
        "schemas": ["VENDAS"],
        "ok": True,
        "server_version": "V7R5M0",
    }
]


def _organization(session, name="Elumini"):
    org = models.Organization(id=str(uuid.uuid4()), name=name, is_active=True)
    session.add(org)
    session.commit()
    return org


def _project(session, org):
    project = models.Project(id=str(uuid.uuid4()), name="Proj", organization_id=org.id)
    session.add(project)
    session.commit()
    return project


def _connector(session, org, online=True, databases=REPORTED):
    connector, token = connectors.create_connector(session, org.id, "Datacenter SP")
    if online:
        connectors.record_heartbeat(session, connector, "1.0.0", databases)
    return connector, token


@pytest.fixture
def session_factory(db_session):
    factory = sessionmaker(bind=db_session.get_bind())

    @contextmanager
    def _open():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    return _open


class TestTokens:
    def test_only_the_hash_is_stored(self, db_session):
        connector, token = _connector(db_session, _organization(db_session))

        assert token.startswith(connectors.TOKEN_PREFIX)
        assert connector.token_hash != token
        assert token not in (connector.token_hint, connector.token_hash)
        assert token.startswith(connector.token_hint)

    def test_authenticates_the_right_connector(self, db_session):
        connector, token = _connector(db_session, _organization(db_session))

        assert connectors.authenticate(db_session, token).id == connector.id

    @pytest.mark.parametrize("token", [None, "", "estk_wrong", "Bearer x", "outro"])
    def test_rejects_unknown_tokens(self, db_session, token):
        _connector(db_session, _organization(db_session))

        assert connectors.authenticate(db_session, token) is None

    def test_revoked_connector_cannot_authenticate(self, db_session):
        connector, token = _connector(db_session, _organization(db_session))
        connector.is_active = False
        db_session.commit()

        assert connectors.authenticate(db_session, token) is None

    def test_rotation_invalidates_the_old_token(self, db_session):
        connector, old = _connector(db_session, _organization(db_session))

        new = connectors.rotate_token(db_session, connector)

        assert connectors.authenticate(db_session, old) is None
        assert connectors.authenticate(db_session, new).id == connector.id


class TestStatus:
    def test_online_within_the_window(self, db_session):
        connector, _ = _connector(db_session, _organization(db_session))

        assert connectors.is_online(connector)

    def test_offline_after_the_window(self, db_session):
        connector, _ = _connector(db_session, _organization(db_session))
        later = datetime.now(UTC) + timedelta(seconds=connectors.ONLINE_WINDOW_SECONDS + 1)

        assert not connectors.is_online(connector, later)

    def test_never_seen_is_offline(self, db_session):
        connector, _ = _connector(db_session, _organization(db_session), online=False)

        assert not connectors.is_online(connector)

    def test_naive_timestamps_from_the_driver_are_treated_as_utc(self, db_session):
        connector, _ = _connector(db_session, _organization(db_session))
        connector.last_seen_at = datetime.now(UTC).replace(tzinfo=None)

        assert connectors.is_online(connector)

    def test_reported_database_lookup_is_case_insensitive(self, db_session):
        connector, _ = _connector(db_session, _organization(db_session))

        assert connectors.reported_database(connector, "ERP")["engine"] == "db2i"
        assert connectors.reported_database(connector, "outro") is None

    def test_corrupt_report_reads_as_empty(self, db_session):
        connector, _ = _connector(db_session, _organization(db_session))
        connector.reported_databases = "{not json"

        assert connectors.reported_databases(connector) == []


def _job(session, connector, status="pending", created_at=None):
    job = models.DatabaseConnectorJob(
        connector_id=connector.id,
        database_alias="erp",
        operation="list_tables",
        arguments=json.dumps({"schema": "VENDAS"}),
        status=status,
    )
    if created_at is not None:
        job.created_at = created_at
    session.add(job)
    session.commit()
    return job


class TestQueue:
    def test_claims_pending_jobs_once(self, db_session):
        connector, _ = _connector(db_session, _organization(db_session))
        job = _job(db_session, connector)

        first = connectors.claim_jobs(db_session, connector)
        second = connectors.claim_jobs(db_session, connector)

        assert [claimed.id for claimed in first] == [job.id]
        assert second == []
        db_session.refresh(job)
        assert job.status == "running"
        assert job.picked_at is not None

    def test_never_claims_another_connectors_jobs(self, db_session):
        org = _organization(db_session)
        mine, _ = _connector(db_session, org)
        other, _ = _connector(db_session, org)
        _job(db_session, other)

        assert connectors.claim_jobs(db_session, mine) == []

    def test_claims_at_most_the_batch_size(self, db_session):
        connector, _ = _connector(db_session, _organization(db_session))
        for _ in range(connectors.MAX_JOBS_PER_POLL + 2):
            _job(db_session, connector)

        claimed = connectors.claim_jobs(db_session, connector)

        assert len(claimed) == connectors.MAX_JOBS_PER_POLL

    def test_result_is_stored_encrypted(self, db_session):
        connector, _ = _connector(db_session, _organization(db_session))
        job = _job(db_session, connector, status="running")

        accepted = connectors.complete_job(db_session, connector, job.id, {"rows": [[1]]}, None)

        assert accepted
        raw = db_session.execute(
            text("SELECT status, result FROM database_connector_job WHERE id = :id"),
            {"id": job.id},
        ).mappings().one()
        assert raw["status"] == "done"
        assert "rows" not in raw["result"]
        db_session.expire_all()
        assert json.loads(db_session.get(models.DatabaseConnectorJob, job.id).result) == {
            "rows": [[1]]
        }

    def test_error_is_stored(self, db_session):
        connector, _ = _connector(db_session, _organization(db_session))
        job = _job(db_session, connector, status="running")

        connectors.complete_job(db_session, connector, job.id, None, "sem permissão")

        db_session.refresh(job)
        assert (job.status, job.error) == ("error", "sem permissão")

    def test_oversized_result_becomes_an_error(self, db_session):
        connector, _ = _connector(db_session, _organization(db_session))
        job = _job(db_session, connector, status="running")
        huge = {"rows": ["x" * (connectors.MAX_RESULT_CHARS + 1)]}

        connectors.complete_job(db_session, connector, job.id, huge, None)

        db_session.refresh(job)
        assert job.status == "error"
        assert "grande demais" in job.error

    def test_cannot_complete_another_connectors_job(self, db_session):
        org = _organization(db_session)
        mine, _ = _connector(db_session, org)
        other, _ = _connector(db_session, org)
        job = _job(db_session, other, status="running")

        assert not connectors.complete_job(db_session, mine, job.id, {"ok": 1}, None)

    def test_cannot_complete_a_job_that_is_not_running(self, db_session):
        connector, _ = _connector(db_session, _organization(db_session))
        job = _job(db_session, connector, status="expired")

        assert not connectors.complete_job(db_session, connector, job.id, {"ok": 1}, None)

    def test_old_jobs_are_purged(self, db_session):
        connector, _ = _connector(db_session, _organization(db_session))
        _job(db_session, connector, created_at=datetime.now(UTC) - timedelta(hours=2))
        fresh_id = _job(db_session, connector).id

        removed = connectors.purge_finished_jobs(db_session)

        assert removed == 1
        remaining = {job.id for job in db_session.query(models.DatabaseConnectorJob)}
        assert remaining == {fresh_id}


class TestRunViaConnector:
    def _answer_on_first_wait(self, session_factory, connector_id, result=None, error=None):
        """Plays the connector inside the wait loop, in the same thread."""
        state = {"answered": False}

        def fake_sleep(_seconds):
            if state["answered"]:
                return
            with session_factory() as session:
                connector = session.get(models.DatabaseConnector, connector_id)
                for job in connectors.claim_jobs(session, connector):
                    connectors.complete_job(session, connector, job.id, result, error)
            state["answered"] = True

        return fake_sleep

    def test_round_trip_returns_the_result_and_cleans_up(self, db_session, session_factory):
        connector, _ = _connector(db_session, _organization(db_session))
        sleep = self._answer_on_first_wait(session_factory, connector.id, result={"items": [1]})

        with patch.object(connectors.time, "sleep", sleep):
            result = connectors.run_via_connector(
                connector.id, "erp", "list_tables", {}, session_factory=session_factory
            )

        assert result == {"items": [1]}
        assert db_session.query(models.DatabaseConnectorJob).count() == 0

    def test_connector_error_is_raised_as_a_safe_message(self, db_session, session_factory):
        connector, _ = _connector(db_session, _organization(db_session))
        sleep = self._answer_on_first_wait(session_factory, connector.id, error="SQL0551 sem autoridade")

        with patch.object(connectors.time, "sleep", sleep), pytest.raises(
            DatabaseAccessError, match="SQL0551"
        ):
            connectors.run_via_connector(
                connector.id, "erp", "list_tables", {}, session_factory=session_factory
            )

    def test_timeout_expires_the_job(self, db_session, session_factory):
        connector, _ = _connector(db_session, _organization(db_session))

        with patch.object(connectors.time, "sleep", return_value=None), pytest.raises(
            DatabaseAccessError, match="não respondeu"
        ):
            connectors.run_via_connector(
                connector.id, "erp", "list_tables", {}, wait_seconds=0.01,
                session_factory=session_factory,
            )

        job = db_session.query(models.DatabaseConnectorJob).one()
        assert job.status == "expired"

    def test_offline_connector_fails_fast_without_queueing(self, db_session, session_factory):
        connector, _ = _connector(db_session, _organization(db_session), online=False)

        with pytest.raises(DatabaseAccessError, match="offline"):
            connectors.run_via_connector(
                connector.id, "erp", "list_tables", {}, session_factory=session_factory
            )

        assert db_session.query(models.DatabaseConnectorJob).count() == 0

    def test_revoked_connector_is_refused(self, db_session, session_factory):
        connector, _ = _connector(db_session, _organization(db_session))
        connector.is_active = False
        db_session.commit()

        with pytest.raises(DatabaseAccessError, match="revogado"):
            connectors.run_via_connector(
                connector.id, "erp", "list_tables", {}, session_factory=session_factory
            )


def _connector_target(connector_id="c-1"):
    return DatabaseTarget(
        alias="erp",
        host="",
        port=0,
        database="S21A1B2C",
        username="",
        password="",
        engine="db2i",
        options=(("connector_alias", "ERP_PROD"), ("connector_id", connector_id)),
    )


class TestProjectDatabaseExecutor:
    def test_connector_targets_go_through_the_queue(self):
        executor = connectors.ProjectDatabaseExecutor((_connector_target(),))

        with patch.object(connectors, "run_via_connector", return_value={"ok": 1}) as run:
            result = executor.run(None, "describe_table", {"table": "PEDIDO"})

        assert result == {"ok": 1}
        run.assert_called_once_with("c-1", "ERP_PROD", "describe_table", {"table": "PEDIDO"})

    def test_invalid_operation_is_refused_before_queueing(self):
        executor = connectors.ProjectDatabaseExecutor((_connector_target(),))

        with patch.object(connectors, "run_via_connector") as run:
            result = executor.run(None, "drop_table", {})

        assert "não existe" in result["error"]
        run.assert_not_called()

    def test_connector_failures_come_back_as_data(self):
        executor = connectors.ProjectDatabaseExecutor((_connector_target(),))

        with patch.object(
            connectors, "run_via_connector", side_effect=DatabaseAccessError("conector offline")
        ):
            result = executor.run(None, "database_overview", {})

        assert result == {"error": "conector offline"}

    def test_direct_targets_still_run_in_process(self):
        direct = DatabaseTarget(
            alias="pg", host="h", port=5432, database="d", username="u", password="p"
        )
        executor = connectors.ProjectDatabaseExecutor((direct,))

        with patch.object(connectors, "run_via_connector") as run, patch(
            "database_access.operations.run_operation", return_value={"ok": 1}
        ):
            result = executor.run(None, "database_overview", {})

        assert result == {"ok": 1}
        run.assert_not_called()


@pytest.fixture
def api(db_session, session_factory):
    app = FastAPI()
    app.include_router(
        project_database_router.router, prefix="/projects/{project_id}/databases"
    )
    app.include_router(
        database_connector_router.project_router,
        prefix="/projects/{project_id}/database-connectors",
    )
    app.include_router(
        database_connector_router.admin_router,
        prefix="/organizations/{organization_id}/database-connectors",
    )
    app.include_router(database_connector_router.connector_router, prefix="/connector")
    app.include_router(database_connector_router.overview_router, prefix="/database-connectors")

    def _session():
        yield db_session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[check_project_access] = lambda: True
    app.dependency_overrides[check_project_ownership] = lambda: True
    app.dependency_overrides[retrieve_admin_data] = lambda: True
    with patch.object(database_connector_router, "create_session", session_factory):
        yield TestClient(app)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


class TestAdminApi:
    def test_overview_groups_connectors_by_company_without_tokens(self, api, db_session):
        first = _organization(db_session, "Alfa")
        second = _organization(db_session, "Beta")
        connector, token = _connector(db_session, first)

        overview = api.get("/database-connectors/")

        body = overview.json()
        assert [item["organization_name"] for item in body] == ["Alfa", "Beta"]
        assert [c["id"] for c in body[0]["connectors"]] == [connector.id]
        assert body[1]["connectors"] == []
        assert body[0]["connectors"][0]["online"] is True
        assert token not in overview.text
        assert second.id == body[1]["organization_id"]

    def test_token_is_returned_once(self, api, db_session):
        org = _organization(db_session)

        created = api.post(f"/organizations/{org.id}/database-connectors/", json={"name": "SP"})
        listed = api.get(f"/organizations/{org.id}/database-connectors/")

        token = created.json()["token"]
        assert created.status_code == 201
        assert token.startswith("estk_")
        assert token not in listed.text
        assert listed.json()[0]["online"] is False

    def test_rotate_returns_a_new_working_token(self, api, db_session):
        org = _organization(db_session)
        connector, old = _connector(db_session, org)

        rotated = api.post(
            f"/organizations/{org.id}/database-connectors/{connector.id}/rotate-token"
        )

        assert api.post("/connector/heartbeat", json={}, headers=_auth(old)).status_code == 401
        new = rotated.json()["token"]
        assert api.post("/connector/heartbeat", json={}, headers=_auth(new)).status_code == 204

    def test_revoke_blocks_the_connector(self, api, db_session):
        org = _organization(db_session)
        connector, token = _connector(db_session, org)

        api.delete(f"/organizations/{org.id}/database-connectors/{connector.id}")

        assert api.get("/connector/jobs?wait=0", headers=_auth(token)).status_code == 401

    def test_connector_of_another_company_is_not_found(self, api, db_session):
        connector, _ = _connector(db_session, _organization(db_session))
        other = _organization(db_session, "Outra")

        response = api.delete(f"/organizations/{other.id}/database-connectors/{connector.id}")

        assert response.status_code == 404


class TestProjectConnectorsApi:
    def test_lists_only_active_connectors_of_the_projects_company(self, api, db_session):
        org = _organization(db_session)
        project = _project(db_session, org)
        mine, _ = _connector(db_session, org)
        revoked, _ = _connector(db_session, org)
        revoked.is_active = False
        db_session.commit()
        _connector(db_session, _organization(db_session, "Outra"))

        listed = api.get(f"/projects/{project.id}/database-connectors/").json()

        assert [item["id"] for item in listed] == [mine.id]
        assert listed[0]["databases"][0]["alias"] == "erp"
        assert "token" not in listed[0]


class TestConnectorApi:
    def test_requires_a_token(self, api):
        assert api.get("/connector/jobs?wait=0").status_code == 401
        assert api.post("/connector/heartbeat", json={}).status_code == 401

    def test_heartbeat_updates_status_and_report(self, api, db_session):
        connector, token = _connector(db_session, _organization(db_session), online=False)

        response = api.post(
            "/connector/heartbeat",
            json={"version": "1.2.0", "databases": REPORTED},
            headers=_auth(token),
        )

        db_session.expire_all()
        stored = db_session.get(models.DatabaseConnector, connector.id)
        assert response.status_code == 204
        assert stored.version == "1.2.0"
        assert connectors.is_online(stored)
        assert connectors.reported_database(stored, "erp")["server_version"] == "V7R5M0"

    def test_poll_and_answer_a_job(self, api, db_session):
        connector, token = _connector(db_session, _organization(db_session))
        job = _job(db_session, connector)

        polled = api.get("/connector/jobs?wait=0", headers=_auth(token)).json()
        answered = api.post(
            f"/connector/jobs/{job.id}/result", json={"result": {"items": []}}, headers=_auth(token)
        )
        again = api.post(
            f"/connector/jobs/{job.id}/result", json={"result": {"items": []}}, headers=_auth(token)
        )

        assert polled == [
            {"id": job.id, "database_alias": "erp", "operation": "list_tables",
             "arguments": {"schema": "VENDAS"}}
        ]
        assert answered.status_code == 204
        assert again.status_code == 404

    def test_empty_poll_returns_after_the_wait(self, api, db_session):
        _, token = _connector(db_session, _organization(db_session))

        assert api.get("/connector/jobs?wait=0", headers=_auth(token)).json() == []

    def test_wait_is_capped_below_the_router_timeout(self, api, db_session):
        _, token = _connector(db_session, _organization(db_session))

        assert api.get("/connector/jobs?wait=60", headers=_auth(token)).status_code == 422


def _connector_payload(**overrides):
    body = {
        "alias": "erp",
        "access_mode": "connector",
        "connector_alias": "erp",
        "engine": "postgres",
        "schemas": [],
    }
    body.update(overrides)
    return body


class TestProjectDatabaseInConnectorMode:
    def test_engine_and_database_come_from_the_connector_report(self, api, db_session):
        org = _organization(db_session)
        project = _project(db_session, org)
        connector, _ = _connector(db_session, org)

        response = api.post(
            f"/projects/{project.id}/databases/",
            json=_connector_payload(connector_id=connector.id),
        )

        body = response.json()
        assert response.status_code == 201, body
        assert (body["engine"], body["database_name"], body["access_mode"]) == (
            "db2i",
            "S21A1B2C",
            "connector",
        )
        assert body["host"] is None

    def test_alias_not_reported_is_refused(self, api, db_session):
        org = _organization(db_session)
        project = _project(db_session, org)
        connector, _ = _connector(db_session, org)

        response = api.post(
            f"/projects/{project.id}/databases/",
            json=_connector_payload(connector_id=connector.id, connector_alias="nada"),
        )

        assert response.status_code == 422
        assert "nada" in response.text

    def test_connector_of_another_company_is_refused(self, api, db_session):
        project = _project(db_session, _organization(db_session))
        foreign, _ = _connector(db_session, _organization(db_session, "Outra"))

        response = api.post(
            f"/projects/{project.id}/databases/",
            json=_connector_payload(connector_id=foreign.id),
        )

        assert response.status_code == 404

    def test_db2_luw_cannot_be_registered_in_direct_mode(self, api, db_session):
        project = _project(db_session, _organization(db_session))

        response = api.post(
            f"/projects/{project.id}/databases/",
            json={
                "alias": "dw",
                "engine": "db2",
                "host": "db2.example.com",
                "database_name": "X",
                "username": "u",
                "password": "p",
            },
        )

        assert response.status_code == 422
        assert "conector" in response.text

    def test_sql_server_registers_in_direct_mode_on_port_1433(self, api, db_session):
        project = _project(db_session, _organization(db_session))

        response = api.post(
            f"/projects/{project.id}/databases/",
            json={
                "alias": "loja",
                "engine": "sqlserver",
                "host": "loja.database.windows.net",
                "database_name": "loja",
                "username": "estack_ro",
                "password": "p",
                "schemas": ["vendas"],
            },
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert (body["engine"], body["port"], body["database_name"]) == ("sqlserver", 1433, "loja")

    def test_sql_server_in_direct_mode_needs_the_database_name(self, api, db_session):
        project = _project(db_session, _organization(db_session))

        response = api.post(
            f"/projects/{project.id}/databases/",
            json={"alias": "loja", "engine": "sqlserver", "host": "h", "username": "u", "password": "p"},
        )

        assert response.status_code == 422
        assert "database_name" in response.text

    def test_oracle_registers_in_direct_mode_on_port_1521(self, api, db_session):
        project = _project(db_session, _organization(db_session))

        response = api.post(
            f"/projects/{project.id}/databases/",
            json={
                "alias": "erp",
                "engine": "oracle",
                "host": "oracle.example.com",
                "database_name": "ORCLPDB1",
                "username": "ESTACK_RO",
                "password": "p",
                "schemas": ["ERP"],
            },
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert (body["engine"], body["port"], body["database_name"]) == ("oracle", 1521, "ORCLPDB1")

    def test_oracle_in_direct_mode_needs_the_service_name(self, api, db_session):
        project = _project(db_session, _organization(db_session))

        response = api.post(
            f"/projects/{project.id}/databases/",
            json={"alias": "erp", "engine": "oracle", "host": "h", "username": "u", "password": "p"},
        )

        assert response.status_code == 422
        assert "database_name" in response.text

    def test_db2_for_i_registers_in_direct_mode_with_its_defaults(self, api, db_session):
        project = _project(db_session, _organization(db_session))

        response = api.post(
            f"/projects/{project.id}/databases/",
            json={
                "alias": "erp",
                "engine": "db2i",
                "host": "as400.example.com",
                "username": "ESTACKRO",
                "password": "p",
                "schemas": ["VENDAS"],
            },
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert (body["engine"], body["access_mode"], body["port"]) == ("db2i", "direct", 8076)
        assert body["database_name"] == "as400.example.com"

    @pytest.mark.parametrize("ssl_mode", ["verify-ca", "verify-full"])
    def test_db2_for_i_in_direct_mode_only_accepts_require(self, api, db_session, ssl_mode):
        project = _project(db_session, _organization(db_session))

        response = api.post(
            f"/projects/{project.id}/databases/",
            json={
                "alias": "erp",
                "engine": "db2i",
                "host": "as400.example.com",
                "username": "ESTACKRO",
                "password": "p",
                "ssl_mode": ssl_mode,
            },
        )

        assert response.status_code == 422
        assert "require" in response.json()["detail"]

    def test_direct_mode_still_requires_credentials(self, api, db_session):
        project = _project(db_session, _organization(db_session))

        response = api.post(
            f"/projects/{project.id}/databases/",
            json={"alias": "pg", "host": "h", "database_name": "d", "username": "u"},
        )

        assert response.status_code == 422
        assert "password" in response.text

    def test_target_carries_the_connector_route(self, db_session):
        org = _organization(db_session)
        project = _project(db_session, org)
        connector, _ = _connector(db_session, org)
        record = models.ProjectDatabase(
            project_id=project.id,
            alias="erp",
            engine="db2i",
            access_mode="connector",
            connector_id=connector.id,
            connector_alias="ERP_PROD",
            database_name="S21A1B2C",
        )
        db_session.add(record)
        db_session.commit()

        target = project_databases.target_from_record(record)

        assert target.option("connector_id") == connector.id
        assert target.option("connector_alias") == "ERP_PROD"
        assert target.option("network_policy") is None
        assert target.password == ""

    def test_connection_check_runs_on_the_connector(self, db_session):
        with patch.object(
            project_databases,
            "run_via_connector",
            return_value={"ok": True, "server_version": "V7R5M0", "schemas": ["VENDAS"]},
        ) as run:
            check = project_databases.check_connection(_connector_target())

        assert check.ok is True
        run.assert_called_once_with("c-1", "ERP_PROD", "check_connection", {})

    def test_connection_check_reports_an_offline_connector(self, db_session):
        with patch.object(
            project_databases,
            "run_via_connector",
            side_effect=DatabaseAccessError("conector offline"),
        ):
            check = project_databases.check_connection(_connector_target())

        assert (check.ok, check.error) == (False, "conector offline")

"""Project databases: encrypted storage, API and MCP resolution."""

import uuid
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from pydantic_ai.mcp import MCPToolset
from sqlalchemy import text

from agents.utils import mcp_providers as mp
from agents.utils.mcps import MCPS_DICT
from database import models
from database.database import get_session
from database_access.engines import postgres as postgres_engine
from routers.crud import project_database_router
from schemas.project_database_schemas import ProjectDatabaseConnectionCheck
from services import project_database_service as service
from utils.bearer_auth import check_project_access, check_project_ownership
from utils.crypto import DecryptionError

DATABASE = models.McpDomain.DATABASE.value
PASSWORD = "s3cr3t-value"


def _project(session) -> models.Project:
    org = models.Organization(id=str(uuid.uuid4()), name="Elumini", is_active=True)
    session.add(org)
    session.flush()
    project = models.Project(id=str(uuid.uuid4()), name="Proj", organization_id=org.id)
    session.add(project)
    session.commit()
    return project


def _database(session, project, alias="principal", is_active=True, schemas=None):
    record = models.ProjectDatabase(
        project_id=project.id,
        alias=alias,
        host="db.example.com",
        port=5432,
        database_name="app",
        username="estack_reader",
        password=PASSWORD,
        schema_allowlist=schemas,
        is_active=is_active,
    )
    session.add(record)
    session.commit()
    return record


class TestStorage:
    def test_password_is_encrypted_at_rest(self, db_session):
        record = _database(db_session, _project(db_session))

        raw = db_session.execute(
            text("SELECT password FROM project_database WHERE id = :id"), {"id": record.id}
        ).scalar_one()

        assert raw != PASSWORD
        db_session.expire_all()
        assert db_session.get(models.ProjectDatabase, record.id).password == PASSWORD

    def test_schema_allowlist_is_parsed(self, db_session):
        record = _database(db_session, _project(db_session), schemas=" vendas, ,rh ")

        assert record.schemas == ("vendas", "rh")

    def test_target_carries_everything_the_guard_needs(self, db_session):
        record = _database(db_session, _project(db_session), schemas="vendas")

        target = service.target_from_record(record)

        assert (target.alias, target.database, target.password, target.schemas) == (
            "principal",
            "app",
            PASSWORD,
            ("vendas",),
        )

    def test_direct_target_is_limited_to_public_networks(self, db_session, monkeypatch):
        monkeypatch.setattr(service.settings, "project_databases_allow_private_network", False)
        record = _database(db_session, _project(db_session))

        target = service.target_from_record(record)

        assert target.option("network_policy") == "public"

    def test_private_network_override_drops_the_network_policy(self, db_session, monkeypatch):
        monkeypatch.setattr(service.settings, "project_databases_allow_private_network", True)
        record = _database(db_session, _project(db_session))

        assert service.target_from_record(record).option("network_policy") is None

    def test_deleting_the_project_removes_its_databases(self, db_session):
        project = _project(db_session)
        _database(db_session, project)

        db_session.delete(project)
        db_session.commit()

        assert db_session.query(models.ProjectDatabase).count() == 0


class TestResolveDatabaseToolset:
    def test_project_without_databases_is_reported(self, db_session):
        project = _project(db_session)

        with pytest.raises(mp.McpConfigNotFoundError, match="Nenhum banco"):
            mp.resolve_mcp_toolset_for_domain(db_session, project.id, DATABASE, "ask_database")

    def test_inactive_databases_are_ignored(self, db_session):
        project = _project(db_session)
        _database(db_session, project, is_active=False)

        with pytest.raises(mp.McpConfigNotFoundError):
            mp.resolve_mcp_toolset_for_domain(db_session, project.id, DATABASE, "ask_database")

    def test_needs_no_company_integration_row(self, db_session):
        project = _project(db_session)
        _database(db_session, project)

        toolset = mp.resolve_mcp_toolset_for_domain(
            db_session, project.id, DATABASE, "ask_database"
        )

        assert isinstance(toolset, MCPToolset)
        assert toolset.include_instructions is True

    def test_toolset_is_bounded_by_the_mcp_timeouts(self, db_session):
        project = _project(db_session)
        _database(db_session, project)

        with patch.object(mp, "MCPToolset") as toolset:
            MCPS_DICT["db_mcp"]("ask_database", project.id, db_session)

        kwargs = toolset.call_args.kwargs
        assert kwargs["read_timeout"] == mp.MCP_READ_TIMEOUT_SECONDS
        assert kwargs["init_timeout"] == mp.MCP_INIT_TIMEOUT_SECONDS

    def test_server_gets_only_this_projects_active_databases(self, db_session):
        project = _project(db_session)
        other = _project(db_session)
        _database(db_session, project, alias="vendas")
        _database(db_session, project, alias="antigo", is_active=False)
        _database(db_session, other, alias="alheio")

        with patch(
            "agents.mcp_servers.database_server.build_database_server"
        ) as build, patch.object(mp, "MCPToolset"):
            mp.resolve_mcp_toolset_for_domain(db_session, project.id, DATABASE, "ask_database")

        assert [target.alias for target in build.call_args.args[0]] == ["vendas"]

    def test_undecryptable_credentials_become_a_connection_error(self, db_session):
        project = _project(db_session)
        _database(db_session, project)

        with patch.object(
            service, "active_project_databases", side_effect=DecryptionError("bad key")
        ), pytest.raises(mp.McpConnectionError, match="descriptografadas"):
            mp.resolve_mcp_toolset_for_domain(
                db_session, project.id, DATABASE, "ask_database"
            )

    def test_scm_routing_is_untouched(self, db_session):
        project = _project(db_session)
        _database(db_session, project)

        with pytest.raises(mp.McpProviderNotConfiguredError):
            mp.resolve_mcp_toolset_for_domain(
                db_session, project.id, models.McpDomain.SCM.value, "ask_git"
            )


@pytest.fixture
def api(db_session):
    app = FastAPI()
    app.include_router(
        project_database_router.router, prefix="/projects/{project_id}/databases"
    )

    def _session():
        yield db_session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[check_project_access] = lambda: True
    app.dependency_overrides[check_project_ownership] = lambda: True
    return TestClient(app)


def _payload(**overrides):
    body = {
        "alias": "principal",
        "host": "db.example.com",
        "port": 5432,
        "database_name": "app",
        "username": "estack_reader",
        "password": PASSWORD,
        "schemas": ["vendas"],
    }
    body.update(overrides)
    return body


HEALTHY = ProjectDatabaseConnectionCheck(ok=True, server_version="16", role="estack_reader")


class TestApi:
    def test_create_and_list_never_return_the_password(self, api, db_session):
        project = _project(db_session)

        created = api.post(f"/projects/{project.id}/databases/", json=_payload())
        listed = api.get(f"/projects/{project.id}/databases/")

        assert created.status_code == 201
        assert created.json()["schemas"] == ["vendas"]
        assert PASSWORD not in created.text
        assert PASSWORD not in listed.text
        assert [item["alias"] for item in listed.json()] == ["principal"]

    def test_duplicate_alias_conflicts(self, api, db_session):
        project = _project(db_session)
        api.post(f"/projects/{project.id}/databases/", json=_payload())

        response = api.post(f"/projects/{project.id}/databases/", json=_payload())

        assert response.status_code == 409

    def test_invalid_schema_name_is_rejected(self, api, db_session):
        project = _project(db_session)

        response = api.post(
            f"/projects/{project.id}/databases/", json=_payload(schemas=["vendas; drop"])
        )

        assert response.status_code == 422

    def test_create_rejects_insecure_ssl_mode(self, api, db_session):
        project = _project(db_session)

        response = api.post(
            f"/projects/{project.id}/databases/", json=_payload(ssl_mode="disable")
        )

        assert response.status_code == 422
        assert response.json()["detail"] == (
            "O modo SSL 'disable' não é permitido; use require, verify-ca ou verify-full."
        )
        assert db_session.query(models.ProjectDatabase).count() == 0

    def test_update_rejects_insecure_ssl_mode(self, api, db_session):
        project = _project(db_session)
        record = _database(db_session, project)

        response = api.put(
            f"/projects/{project.id}/databases/{record.id}",
            json={"ssl_mode": "disable", "password": "nova"},
        )

        db_session.expire_all()
        assert response.status_code == 422
        assert response.json()["detail"] == (
            "O modo SSL 'disable' não é permitido; use require, verify-ca ou verify-full."
        )
        assert db_session.get(models.ProjectDatabase, record.id).ssl_mode == "require"

    def test_insecure_ssl_mode_is_allowed_with_private_network_override(
        self, api, db_session, monkeypatch
    ):
        monkeypatch.setattr(service.settings, "project_databases_allow_private_network", True)
        project = _project(db_session)

        response = api.post(
            f"/projects/{project.id}/databases/", json=_payload(ssl_mode="disable")
        )

        assert response.status_code == 201
        assert response.json()["ssl_mode"] == "disable"

    def test_databases_of_another_project_are_not_reachable(self, api, db_session):
        project = _project(db_session)
        other = _project(db_session)
        record = _database(db_session, other)

        path = f"/projects/{project.id}/databases/{record.id}"
        assert api.get(path).status_code == 404
        assert api.put(path, json={"alias": "x"}).status_code == 404
        assert api.delete(path).status_code == 404
        assert db_session.get(models.ProjectDatabase, record.id) is not None

    def test_update_keeps_the_password_when_omitted(self, api, db_session):
        project = _project(db_session)
        record = _database(db_session, project)

        response = api.put(
            f"/projects/{project.id}/databases/{record.id}",
            json={"description": "Descrição atualizada", "schemas": []},
        )

        db_session.expire_all()
        stored = db_session.get(models.ProjectDatabase, record.id)
        assert response.status_code == 200
        assert stored.host == "db.example.com"
        assert stored.password == PASSWORD
        assert stored.description == "Descrição atualizada"
        assert stored.schema_allowlist is None

    def test_update_connection_change_requires_password_and_keeps_record(self, api, db_session):
        project = _project(db_session)
        record = _database(db_session, project)

        response = api.put(
            f"/projects/{project.id}/databases/{record.id}",
            json={"host": "novo.example.com"},
        )

        db_session.expire_all()
        stored = db_session.get(models.ProjectDatabase, record.id)
        assert response.status_code == 422
        assert response.json()["detail"] == (
            "Informe a senha ao alterar host, porta, banco, usuário ou modo SSL."
        )
        assert stored.host == "db.example.com"
        assert stored.password == PASSWORD

    def test_update_connection_change_with_password(self, api, db_session):
        project = _project(db_session)
        record = _database(db_session, project)

        response = api.put(
            f"/projects/{project.id}/databases/{record.id}",
            json={"host": "novo.example.com", "password": "nova"},
        )

        db_session.expire_all()
        stored = db_session.get(models.ProjectDatabase, record.id)
        assert response.status_code == 200
        assert stored.host == "novo.example.com"
        assert stored.password == "nova"

    def test_update_same_connection_value_keeps_password(self, api, db_session):
        project = _project(db_session)
        record = _database(db_session, project)

        response = api.put(
            f"/projects/{project.id}/databases/{record.id}",
            json={"host": "db.example.com"},
        )

        db_session.expire_all()
        stored = db_session.get(models.ProjectDatabase, record.id)
        assert response.status_code == 200
        assert stored.host == "db.example.com"
        assert stored.password == PASSWORD

    def test_update_non_connection_fields_keeps_password(self, api, db_session):
        project = _project(db_session)
        record = _database(db_session, project)

        response = api.put(
            f"/projects/{project.id}/databases/{record.id}",
            json={
                "alias": "renamed",
                "description": "Descrição",
                "schemas": [],
                "is_active": False,
            },
        )

        db_session.expire_all()
        stored = db_session.get(models.ProjectDatabase, record.id)
        assert response.status_code == 200
        assert stored.alias == "renamed"
        assert stored.description == "Descrição"
        assert stored.schemas == ()
        assert stored.is_active is False
        assert stored.password == PASSWORD

    def test_update_replaces_the_password_when_given(self, api, db_session):
        project = _project(db_session)
        record = _database(db_session, project)

        api.put(f"/projects/{project.id}/databases/{record.id}", json={"password": "nova"})

        db_session.expire_all()
        assert db_session.get(models.ProjectDatabase, record.id).password == "nova"

    def test_update_to_an_alias_in_use_conflicts(self, api, db_session):
        project = _project(db_session)
        _database(db_session, project, alias="a")
        record = _database(db_session, project, alias="b")

        response = api.put(f"/projects/{project.id}/databases/{record.id}", json={"alias": "a"})

        assert response.status_code == 409

    def test_delete(self, api, db_session):
        project = _project(db_session)
        record = _database(db_session, project)

        response = api.delete(f"/projects/{project.id}/databases/{record.id}")

        assert response.status_code == 204
        assert db_session.query(models.ProjectDatabase).count() == 0

    def test_non_owner_cannot_manage_project_databases(self, api, db_session):
        project = _project(db_session)
        record = _database(db_session, project)
        prefix = f"/projects/{project.id}/databases"
        database_path = f"{prefix}/{record.id}"

        def reject_non_owner():
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "Somente o proprietário pode gerenciar bancos."
            )

        api.app.dependency_overrides[check_project_ownership] = reject_non_owner

        with patch.object(project_database_router, "check_connection") as check:
            responses = (
                api.post(prefix + "/", json=_payload()),
                api.put(database_path, json={"description": "Membro"}),
                api.delete(database_path),
                api.post(prefix + "/test-connection", json=_payload()),
                api.post(database_path + "/test-connection"),
            )

        assert [response.status_code for response in responses] == [403] * 5
        check.assert_not_called()
        db_session.expire_all()
        assert db_session.get(models.ProjectDatabase, record.id) is not None

    def test_non_owner_can_list_and_get_project_databases(self, api, db_session):
        project = _project(db_session)
        record = _database(db_session, project)

        def reject_non_owner():
            raise HTTPException(status.HTTP_403_FORBIDDEN)

        api.app.dependency_overrides[check_project_ownership] = reject_non_owner

        assert api.get(f"/projects/{project.id}/databases/").status_code == 200
        assert api.get(f"/projects/{project.id}/databases/{record.id}").status_code == 200

    def test_create_with_test_refuses_an_unhealthy_connection(self, api, db_session):
        project = _project(db_session)
        failing = ProjectDatabaseConnectionCheck(ok=False, error="superusuário")

        with patch.object(service, "check_connection", return_value=failing):
            response = api.post(
                f"/projects/{project.id}/databases/?test_connection=true", json=_payload()
            )

        assert response.status_code == 422
        assert "superusuário" in response.text
        assert db_session.query(models.ProjectDatabase).count() == 0

    def test_failed_test_on_update_leaves_the_record_unchanged(self, api, db_session):
        project = _project(db_session)
        record = _database(db_session, project)
        failing = ProjectDatabaseConnectionCheck(ok=False, error="rede")

        with patch.object(service, "check_connection", return_value=failing):
            response = api.put(
                f"/projects/{project.id}/databases/{record.id}?test_connection=true",
                json={"host": "outro.example.com", "password": "nova"},
            )

        db_session.expire_all()
        assert response.status_code == 422
        assert db_session.get(models.ProjectDatabase, record.id).host == "db.example.com"

    def test_test_connection_before_saving(self, api, db_session):
        project = _project(db_session)

        with patch.object(
            project_database_router, "check_connection", return_value=HEALTHY
        ) as check:
            response = api.post(
                f"/projects/{project.id}/databases/test-connection", json=_payload()
            )

        assert response.json()["ok"] is True
        assert check.call_args.args[0].password == PASSWORD

    def test_test_connection_of_a_saved_database(self, api, db_session):
        project = _project(db_session)
        record = _database(db_session, project)

        with patch.object(
            project_database_router, "check_connection", return_value=HEALTHY
        ) as check:
            response = api.post(f"/projects/{project.id}/databases/{record.id}/test-connection")

        assert response.status_code == 200
        assert check.call_args.args[0].host == "db.example.com"


class TestCheckConnection:
    def _posture(self, **overrides):
        from database_access.engines.postgres import Posture

        values = {
            "server_version": "16.2",
            "role": "estack_reader",
            "is_superuser": False,
            "can_write": False,
            "reads_all_data": True,
        }
        values.update(overrides)
        return Posture(**values)

    def _run(self, posture, schemas=(), error=None, sensitive_columns=()):
        from database_access.guard import DatabaseTarget

        target = DatabaseTarget(
            alias="principal",
            host="h",
            port=5432,
            database="app",
            username="u",
            password=PASSWORD,
            schemas=schemas,
        )

        class _Session:
            def records(self, query, params=None, max_rows=1000):
                return [{"name": "public"}, {"name": "vendas"}]

        session = _Session()

        class _Manager:
            def __enter__(self):
                if error:
                    raise error
                return session

            def __exit__(self, *exc):
                return False

        with (
            patch.object(postgres_engine, "readonly_session", return_value=_Manager()),
            patch.object(postgres_engine, "read_posture", return_value=posture) as read,
            patch.object(
                postgres_engine,
                "readable_sensitive_columns",
                return_value=list(sensitive_columns),
            ) as readable,
        ):
            check = service.check_connection(target)

        if error is None:
            assert read.call_args.args[1] == schemas
            assert readable.call_args.args == (session, schemas)
        else:
            read.assert_not_called()
            readable.assert_not_called()
        return check

    def test_healthy_read_only_role(self):
        check = self._run(self._posture())

        assert check.ok is True
        assert check.read_only_role is True
        assert check.schemas == ["public", "vendas"]
        assert check.warnings == []

    def test_sensitive_columns_add_warning_without_failing_connection(self):
        check = self._run(
            self._posture(),
            sensitive_columns=[
                "public.cliente.email",
                "public.cliente.cpf",
                "public.cliente.telefone",
                "public.cliente.celular",
                "public.cliente.senha",
                "public.cliente.token",
            ],
        )

        assert check.ok is True
        assert check.warnings == [
            (
                "O usuário 'estack_reader' consegue ler 6 coluna(s) com dados "
                "sensíveis (ex.: public.cliente.email, public.cliente.cpf, "
                "public.cliente.telefone, public.cliente.celular, "
                "public.cliente.senha). O e-Stack mascara essas colunas nos "
                "resultados, mas não consegue mascarar valores derivados delas "
                "(ex.: upper(senha)). Para garantir a proteção, revogue o acesso no "
                "banco: REVOKE SELECT ON <tabela> FROM estack_reader; GRANT SELECT "
                "(<colunas não sensíveis>) ON <tabela> TO estack_reader;"
            )
        ]

    def test_superuser_fails_with_instructions(self):
        check = self._run(self._posture(is_superuser=True))

        assert check.ok is False
        assert "pg_read_all_data" in check.error

    def test_server_file_access_fails_with_instructions(self):
        check = self._run(self._posture(server_file_access=True))

        assert check.ok is False
        assert check.error == (
            "O usuário 'estack_reader' do banco 'principal' pertence a "
            "pg_read_server_files/pg_write_server_files/pg_execute_server_program, "
            "que dão acesso a arquivos do servidor. Cadastre um usuário só de "
            "leitura sem esses papéis."
        )

    def test_readable_outside_scope_fails_with_instructions(self):
        check = self._run(
            self._posture(readable_outside_scope=("rh", "financeiro")),
            schemas=("public",),
        )

        assert check.ok is False
        assert check.error == (
            "O usuário 'estack_reader' consegue ler tabelas fora dos schemas "
            "liberados (rh, financeiro). Restrinja o usuário: GRANT USAGE ON SCHEMA "
            "<schema> e GRANT SELECT ON ALL TABLES IN SCHEMA <schema> apenas nos "
            "schemas liberados, e não use pg_read_all_data."
        )

    def test_pg_read_all_data_warning_is_omitted_for_allowlist(self):
        check = self._run(self._posture(reads_all_data=False), schemas=("public",))

        assert all("pg_read_all_data" not in warning for warning in check.warnings)

    def test_writable_role_passes_with_a_warning(self):
        check = self._run(self._posture(can_write=True))

        assert check.ok is True
        assert check.read_only_role is False
        assert check.warnings

    def test_missing_allowlisted_schema_is_warned(self):
        check = self._run(self._posture(), schemas=("vendas", "rh"))

        assert any("rh" in warning for warning in check.warnings)

    def test_connection_failure_is_data(self):
        from database_access.guard import DatabaseAccessError

        check = self._run(self._posture(), error=DatabaseAccessError("host não encontrado"))

        assert check.ok is False
        assert check.error == "host não encontrado"

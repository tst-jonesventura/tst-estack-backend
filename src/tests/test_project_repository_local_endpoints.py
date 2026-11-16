from unittest.mock import MagicMock, patch

from database import models
from database.database import get_session
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers.crud.project_repository_router import router
from sqlalchemy.orm import Session
from utils.bearer_auth import check_project_access

app = FastAPI()
app.include_router(router, prefix="/projects/{project_id}/repositories")

mock_db_session = MagicMock(spec=Session)
fake_user = models.User(
    id="user-123", email="test@example.com", is_active=True, is_admin=False
)


def override_get_session():
    yield mock_db_session


def override_check_project_access():
    return fake_user


app.dependency_overrides[get_session] = override_get_session
app.dependency_overrides[check_project_access] = override_check_project_access
client = TestClient(app)


def test_list_local_folders_endpoint_enabled(tmp_path):
    sub = tmp_path / "frontend_app"
    sub.mkdir()
    (sub / "index.ts").write_text("console.log('hello');", encoding="utf-8")
    (sub / ".git").mkdir()

    with patch(
        "routers.crud.project_repository_router.is_local_workspace_enabled",
        return_value=True,
    ):
        with patch(
            "routers.crud.project_repository_router.get_target_project_root",
            return_value=tmp_path,
        ):
            response = client.get("/projects/p-123/repositories/local-folders")
            assert response.status_code == 200
            data = response.json()
            assert len(data) >= 2
            sub_entry = next(
                (e for e in data if e["relative_path"] == "frontend_app"), None
            )
            assert sub_entry is not None
            assert sub_entry["is_git"] is True
            assert sub_entry["detected_language"] == "TypeScript"


def test_list_local_folders_endpoint_disabled():
    with patch(
        "routers.crud.project_repository_router.is_local_workspace_enabled",
        return_value=False,
    ):
        response = client.get("/projects/p-123/repositories/local-folders")
        assert response.status_code == 403


def test_validate_local_folder_endpoint(tmp_path):
    sub = tmp_path / "python_service"
    sub.mkdir()
    (sub / "main.py").write_text("print('test')", encoding="utf-8")

    with patch(
        "routers.crud.project_repository_router.is_local_workspace_enabled",
        return_value=True,
    ):
        with patch("utils.environment.get_target_project_root", return_value=tmp_path):
            with patch(
                "routers.crud.project_repository_router.get_target_project_root",
                return_value=tmp_path,
            ):
                # Valid folder
                response = client.get(
                    "/projects/p-123/repositories/local-folders/validate?path=python_service"
                )
                assert response.status_code == 200
                data = response.json()
                assert data["valid"] is True
                assert data["detected_language"] == "Python"

                # Path traversal
                response = client.get(
                    "/projects/p-123/repositories/local-folders/validate?path=../../etc"
                )
                assert response.status_code == 200
                data = response.json()
                assert data["valid"] is False
                assert "error" in data


def test_create_local_repository_endpoint(tmp_path, db_session):
    org = models.Organization(name="OrgLocalRepo")
    db_session.add(org)
    db_session.commit()

    proj = models.Project(name="ProjLocalRepo", organization_id=org.id)
    db_session.add(proj)
    db_session.commit()

    sub = tmp_path / "my_app"
    sub.mkdir()
    (sub / "main.py").write_text("print(1)", encoding="utf-8")

    app.dependency_overrides[get_session] = lambda: db_session
    c = TestClient(app)

    payload = {
        "source_type": "local_filesystem",
        "local_path": "my_app",
        "name": "My Local App",
    }

    with patch(
        "routers.crud.project_repository_router.is_local_workspace_enabled",
        return_value=True,
    ):
        with patch("utils.environment.get_target_project_root", return_value=tmp_path):
            with patch(
                "routers.crud.project_repository_router.get_target_project_root",
                return_value=tmp_path,
            ):
                with patch(
                    "routers.crud.project_repository_router.create_tech_specs_from_repo_in_background"
                ) as mock_background_agent:
                    response = c.post(
                        f"/projects/{proj.id}/repositories/", json=payload
                    )
                    assert response.status_code == 200
                    data = response.json()
                    assert data["source_type"] == "local_filesystem"
                    assert data["name"] == "My Local App"
                    assert data["local_path"] == "my_app"
                    mock_background_agent.assert_called_once_with(
                        proj.id, data["url"], fake_user.id
                    )

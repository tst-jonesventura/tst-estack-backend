import os
from unittest.mock import patch

import pytest
from database import models
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers.system_router import router as system_router
from utils.environment import (
    is_local_workspace_enabled,
    is_running_in_docker,
    resolve_safe_target_path,
)

app = FastAPI()
app.include_router(system_router)


def test_is_running_in_docker():
    with patch.dict(os.environ, {"RUNNING_IN_DOCKER": "true"}):
        assert is_running_in_docker() is True

    with patch.dict(os.environ, {"RUNNING_IN_DOCKER": "false", "IS_DOCKER": "false"}):
        with patch("os.path.exists", return_value=False):
            assert is_running_in_docker() is False


def test_is_local_workspace_enabled_explicit():
    with patch("utils.environment.settings.local_workspace_enabled", True):
        assert is_local_workspace_enabled() is True

    with patch("utils.environment.settings.local_workspace_enabled", False):
        assert is_local_workspace_enabled() is False


def test_resolve_safe_target_path(tmp_path):
    with patch("utils.environment.get_target_project_root", return_value=tmp_path):
        # Direct root
        assert resolve_safe_target_path() == tmp_path
        assert resolve_safe_target_path("") == tmp_path

        # Safe subpath
        sub_dir = tmp_path / "my_project"
        sub_dir.mkdir()
        assert resolve_safe_target_path("my_project") == sub_dir

        # Path traversal attack
        with pytest.raises(ValueError):
            resolve_safe_target_path("../../etc/passwd")


def test_system_capabilities_endpoint():
    client = TestClient(app)
    response = client.get("/system/capabilities")
    assert response.status_code == 200
    data = response.json()
    assert "local_workspace_enabled" in data
    assert "is_docker" in data
    assert "target_project_root" in data


def test_project_repository_model_local_fields(db_session):
    repo = models.ProjectRepository(
        name="test-local-repo",
        source_type="local_filesystem",
        local_path="subdir/proj",
        default_branch="main",
    )
    db_session.add(repo)
    db_session.commit()
    db_session.refresh(repo)

    assert repo.id is not None
    assert repo.source_type == "local_filesystem"
    assert repo.local_path == "subdir/proj"
    assert repo.organization_github_installation_id is None

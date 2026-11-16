from pathlib import Path
from unittest.mock import MagicMock, patch

from agents import openhands_agent
from database import models


def test_resolve_project_workspace_local(db_session, tmp_path):
    org = models.Organization(name="OrgLocal")
    db_session.add(org)
    db_session.commit()

    proj = models.Project(name="ProjLocal", organization_id=org.id)
    db_session.add(proj)
    db_session.commit()

    repo = models.ProjectRepository(
        project_id=proj.id,
        name="target_app",
        source_type="local_filesystem",
        local_path="subfolder",
        default_branch="main",
    )
    db_session.add(repo)
    db_session.commit()

    with patch("utils.environment.get_target_project_root", return_value=tmp_path):
        is_local, workspace, mcp_config, repos = (
            openhands_agent.resolve_project_workspace_and_scm(db_session, proj.id)
        )

        assert is_local is True
        assert workspace == (tmp_path / "subfolder").resolve()
        assert mcp_config == {}
        assert len(repos) == 1
        assert repos[0].full_name == "target_app"


def test_build_local_user_message():
    msg = openhands_agent.build_local_user_message(
        "Refactor tests", Path("/workspace/target")
    )
    assert "local project repository" in msg
    assert "Refactor tests" in msg
    assert "estack/openhands" in msg


@patch(
    "agents.openhands_agent.run_openhands_conversation",
    return_value="Refactoring completed.",
)
def test_openhands_agent_run_local(mock_conversation, db_session, tmp_path):
    org = models.Organization(name="OrgTest")
    db_session.add(org)
    db_session.commit()

    proj = models.Project(name="ProjTest", organization_id=org.id)
    db_session.add(proj)
    db_session.commit()

    repo = models.ProjectRepository(
        project_id=proj.id,
        name="target_app",
        source_type="local_filesystem",
        default_branch="main",
    )
    db_session.add(repo)
    db_session.commit()

    mock_logger = MagicMock()
    deps = MagicMock()
    deps.session = db_session
    deps.project_id = proj.id
    deps.agent_logger = mock_logger

    with patch("utils.environment.get_target_project_root", return_value=tmp_path):
        with patch("utils.environment.is_local_workspace_enabled", return_value=True):
            result = openhands_agent.run("Refactor code", [], deps)
            assert result is not None
            assert result.output_message == "Refactoring completed."
            mock_conversation.assert_called_once()

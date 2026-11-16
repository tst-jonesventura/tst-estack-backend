import uuid
from unittest.mock import MagicMock, call, patch

import pytest
from agents import openhands_agent
from agents.utils.agent_deps import AgentDeps
from agents.utils.mcp_providers import McpConfigurationError
from database.models import Project, ProjectRepository, User
from fastapi.testclient import TestClient
from schemas.openhands_schemas import OpenHandsRepository
from services.agent_service import agent_service
from sqlalchemy.orm import Session


# region fixtures / helpers
@pytest.fixture
def sample_repositories(
    sample_project_repository: ProjectRepository,
) -> list[OpenHandsRepository]:
    return [OpenHandsRepository.model_validate(sample_project_repository)]


def _make_deps(db_session: Session, project_id: str) -> AgentDeps:
    return AgentDeps(
        project_id=project_id,
        user_id=str(uuid.uuid4()),
        session=db_session,
        agent_logger=MagicMock(),
    )


@pytest.fixture
def fake_github_access():
    with (
        patch.object(
            openhands_agent,
            "_resolve_github_access",
            return_value=("token-123", "https://mcp.example"),
        ) as access,
        patch.object(openhands_agent, "build_mcp_config", return_value={}),
        patch.object(openhands_agent, "sync_repositories") as sync,
    ):
        yield access, sync


# endregion


# region openhands_agent.run
def test_run_returns_agent_reply(
    db_session, sample_project, sample_repositories, tmp_path, fake_github_access
):
    deps = _make_deps(db_session, sample_project.id)
    reply = "Done. Opened a pull request for you."
    access, sync = fake_github_access

    with (
        patch.object(
            openhands_agent,
            "run_openhands_conversation",
            return_value=reply,
        ) as run_conversation,
        patch.object(openhands_agent, "_workspace_root_dir", return_value=tmp_path),
    ):
        output = openhands_agent.run(
            prompt="Add a health endpoint",
            repositories=sample_repositories,
            deps=deps,
        )

    access.assert_called_once_with(deps)
    expected_workspace = tmp_path / openhands_agent.PROJECTS_DIRNAME / sample_project.id
    sync.assert_called_once_with(expected_workspace, sample_repositories, "token-123")
    assert run_conversation.call_args.args[0] == str(expected_workspace)
    assert output is not None
    assert output.output_message == reply
    deps.agent_logger.finish_agent_run_with_usage.assert_called_once()


def test_run_returns_output_without_pr_urls(
    db_session, sample_project, sample_repositories, tmp_path, fake_github_access
):
    deps = _make_deps(db_session, sample_project.id)

    with (
        patch.object(
            openhands_agent, "run_openhands_conversation", return_value="no PR"
        ),
        patch.object(openhands_agent, "_workspace_root_dir", return_value=tmp_path),
    ):
        output = openhands_agent.run(
            prompt="Add a health endpoint",
            repositories=sample_repositories,
            deps=deps,
        )

    assert output is not None
    assert output.output_message == "no PR"
    deps.agent_logger.finish_agent_run_with_usage.assert_called_once()


def test_run_creates_missing_workspace_base_dir(
    db_session, sample_project, sample_repositories, tmp_path, fake_github_access
):
    deps = _make_deps(db_session, sample_project.id)
    pr_url = "https://github.com/example/test-repo/pull/1"
    workspace_base_dir = tmp_path / "missing" / "openhands"

    assert not workspace_base_dir.exists()

    with (
        patch.object(
            openhands_agent, "run_openhands_conversation", return_value=pr_url
        ),
        patch.object(
            openhands_agent,
            "_workspace_root_dir",
            return_value=workspace_base_dir,
        ),
    ):
        output = openhands_agent.run(
            prompt="Add a health endpoint",
            repositories=sample_repositories,
            deps=deps,
        )

    assert output is not None
    assert (
        workspace_base_dir
        / openhands_agent.PROJECTS_DIRNAME
        / sample_project.id
        / openhands_agent.REPOS_DIRNAME
    ).is_dir()


def test_run_returns_none_when_mcp_not_configured(
    db_session, sample_project, sample_repositories
):
    deps = _make_deps(db_session, sample_project.id)

    with patch.object(
        openhands_agent,
        "_resolve_github_access",
        side_effect=McpConfigurationError("missing config"),
    ):
        output = openhands_agent.run(
            prompt="Add a health endpoint",
            repositories=sample_repositories,
            deps=deps,
        )

    assert output is None


def test_run_returns_none_without_agent_logger(
    db_session, sample_project, sample_repositories
):
    deps = _make_deps(db_session, sample_project.id)
    deps.agent_logger = None

    assert (
        openhands_agent.run(prompt="x", repositories=sample_repositories, deps=deps)
        is None
    )


# endregion


# region GitHub access / MCP config
def test_resolve_github_access_uses_project_organization(db_session, sample_project):
    deps = _make_deps(db_session, sample_project.id)

    with patch.object(
        openhands_agent.GithubMcpProvider,
        "get_access_token",
        return_value=("token-123", "https://api.githubcopilot.com/mcp/"),
    ):
        token, mcp_url = openhands_agent._resolve_github_access(deps)

    server = openhands_agent.build_mcp_config(token, mcp_url)["github"]
    assert server.url == "https://api.githubcopilot.com/mcp/"
    assert server.auth.value.get_secret_value() == "token-123"


def test_resolve_github_access_raises_when_project_missing(db_session):
    deps = _make_deps(db_session, str(uuid.uuid4()))

    with pytest.raises(McpConfigurationError):
        openhands_agent._resolve_github_access(deps)


# endregion


# region repositories
def test_sync_repository_clones_when_missing(tmp_path, sample_repositories):
    repo = sample_repositories[0]
    workspace = tmp_path / "project"
    (workspace / openhands_agent.REPOS_DIRNAME).mkdir(parents=True)

    with patch.object(openhands_agent, "_git") as git:
        target = openhands_agent.sync_repository(workspace, repo, "tok")

    assert target == workspace / openhands_agent.REPOS_DIRNAME / "test-repo"
    git.assert_called_once_with(
        [
            "clone",
            "--branch",
            "main",
            "https://github.com/example/test-repo.git",
            str(target),
        ],
        cwd=target.parent,
        token="tok",
    )


def test_sync_repository_updates_existing_clone(tmp_path, sample_repositories):
    repo = sample_repositories[0]
    workspace = tmp_path / "project"
    target = workspace / openhands_agent.REPOS_DIRNAME / "test-repo"
    (target / ".git").mkdir(parents=True)

    with patch.object(openhands_agent, "_git") as git:
        openhands_agent.sync_repository(workspace, repo, "tok")

    assert git.call_args_list == [call(["pull"], cwd=target, token="tok")]


def test_git_passes_token_via_env_not_argv(tmp_path):
    with patch.object(openhands_agent.subprocess, "run") as run:
        openhands_agent._git(["fetch"], cwd=tmp_path, token="tok")

    args, kwargs = run.call_args
    assert args[0] == ["git", "fetch"]
    assert "tok" not in " ".join(args[0])
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert kwargs["env"]["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert kwargs["env"]["GIT_CONFIG_VALUE_0"].startswith("AUTHORIZATION: basic ")


def test_project_workspace_dir_is_segregated_by_project(tmp_path):
    with patch.object(openhands_agent, "_workspace_root_dir", return_value=tmp_path):
        first = openhands_agent.project_workspace_dir("project-a")
        second = openhands_agent.project_workspace_dir("project-b")

    assert first != second
    assert first == tmp_path / openhands_agent.PROJECTS_DIRNAME / "project-a"
    assert (first / openhands_agent.REPOS_DIRNAME).is_dir()


def test_workspace_root_dir_uses_configured_external_path(tmp_path):
    external_root = tmp_path / "external" / "openhands"

    with patch.object(
        openhands_agent.settings,
        "openhands_workspace_root",
        external_root,
    ):
        assert openhands_agent._workspace_root_dir() == external_root


# endregion


# region build_user_message / helpers
def test_build_user_message_lists_every_repository(sample_repositories):
    repositories = sample_repositories + [
        OpenHandsRepository(full_name="example/other-repo", default_branch="develop")
    ]
    message = openhands_agent.build_user_message("Add a health endpoint", repositories)

    assert "Add a health endpoint" in message
    assert "'example/test-repo' (default branch 'main')" in message
    assert "'example/other-repo' (default branch 'develop')" in message
    assert f"{openhands_agent.REPOS_DIRNAME}/test-repo" in message
    assert f"{openhands_agent.REPOS_DIRNAME}/other-repo" in message
    assert openhands_agent.BRANCH_PREFIX in message
    assert "Do not clone" in message
    assert "pull request" in message


def test_model_name_normalizes_provider_colon_format():
    with patch.object(openhands_agent.settings, "default_model", "openai:gpt-5-mini"):
        assert openhands_agent._model_name() == "openai/gpt-5-mini"


def test_model_name_keeps_slash_format_unchanged():
    with patch.object(openhands_agent.settings, "default_model", "openai/gpt-5-mini"):
        assert openhands_agent._model_name() == "openai/gpt-5-mini"


def test_model_name_keeps_plain_model_unchanged():
    with patch.object(openhands_agent.settings, "default_model", "gpt-5-mini"):
        assert openhands_agent._model_name() == "gpt-5-mini"


# endregion


# region agent_service.run_openhands_task
def test_run_openhands_task_uses_all_project_repositories(
    db_session, sample_project, sample_user, sample_project_repository
):
    second = ProjectRepository(
        id=str(uuid.uuid4()),
        project_id=sample_project.id,
        url="https://github.com/example/other-repo",
        name="other-repo",
        owner="example",
        full_name="example/other-repo",
        default_branch="develop",
        language="python",
        organization_github_installation_id=(
            sample_project_repository.organization_github_installation_id
        ),
    )
    db_session.add(second)
    db_session.commit()

    with (
        patch("services.agent_service.openhands_agent.run") as mock_run,
        patch("services.agent_service.create_agent_logger") as mock_logger,
    ):
        mock_run.return_value = MagicMock()
        agent_service.run_openhands_task(
            session=db_session,
            project_id=sample_project.id,
            user_data=sample_user,
            prompt="Do the task",
        )

    mock_logger.assert_called_once()
    mock_run.assert_called_once()
    kwargs = mock_run.call_args.kwargs
    assert kwargs["prompt"] == "Do the task"
    assert sorted(r.full_name for r in kwargs["repositories"]) == [
        "example/other-repo",
        "example/test-repo",
    ]


def test_run_openhands_task_returns_none_without_repositories(
    db_session, sample_project, sample_user
):
    with (
        patch("services.agent_service.openhands_agent.run") as mock_run,
        patch("services.agent_service.create_agent_logger"),
    ):
        result = agent_service.run_openhands_task(
            session=db_session,
            project_id=sample_project.id,
            user_data=sample_user,
            prompt="Do the task",
        )

    assert result is None
    mock_run.assert_not_called()


# endregion

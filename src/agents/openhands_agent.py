"""Generic autonomous agent that edits code with the OpenHands SDK.

The agent receives a free-form task prompt, works autonomously in the project's
repositories through the GitHub MCP server and its local tools, and finishes by
committing its work and opening pull requests.

Every project owns one persistent workspace (``<workspace_root>/projects/
<project_id>``) whose ``repos/`` folder holds a local clone of each project
repository. Clones are created once, on first use, and synced with ``origin`` before
each run, so the agent never re-clones between requests.
"""

import base64
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from agents.utils.agent_deps import AgentDeps
from agents.utils.mcp_providers import GithubMcpProvider, McpConfigurationError
from database import models
from schemas.openhands_schemas import OpenHandsRepository, OpenHandsTaskResult
from utils import log
from utils.config import settings

if TYPE_CHECKING:
    from openhands.sdk import Agent
    from openhands.sdk.mcp.config import MCPServer

logger = log.setup_custom_logger(__name__)

BRANCH_PREFIX = "estack/openhands"
PROJECTS_DIRNAME = "projects"
REPOS_DIRNAME = "repos"
GIT_TIMEOUT_SECONDS = 10 * 60


def run(
    prompt: str,
    repositories: list[OpenHandsRepository],
    deps: AgentDeps,
) -> OpenHandsTaskResult | None:
    """Execute ``prompt`` across ``repositories`` (in local folder or GitHub clones)."""
    if not deps.session or not deps.agent_logger:
        return None

    try:
        is_local, workspace, mcp_config, repos = resolve_project_workspace_and_scm(
            deps.session, deps.project_id
        )
    except McpConfigurationError:
        return None

    if is_local:
        deps.agent_logger.log(
            f"Executando o OpenHands diretamente na pasta local: {workspace}"
        )
        reply = run_openhands_conversation(
            str(workspace),
            build_local_user_message(prompt, workspace),
            mcp_config=mcp_config,
        )
    else:
        try:
            token, mcp_url = _resolve_github_access(deps)
        except McpConfigurationError:
            return None

        mcp_config = build_mcp_config(token, mcp_url)
        sync_repositories(workspace, repositories or repos, token)
        deps.agent_logger.log(
            f"Executando o OpenHands em workspace persistente: {workspace}"
        )
        reply = run_openhands_conversation(
            str(workspace),
            build_user_message(prompt, repositories or repos),
            mcp_config=mcp_config,
        )

    output = OpenHandsTaskResult(
        output_message=reply or "Execução finalizada.",
    )

    deps.agent_logger.log(output.output_message)
    deps.agent_logger.finish_agent_run_with_usage(
        "Execução finalizada.", _model_name(), None
    )
    return output


def resolve_project_workspace_and_scm(
    session: "models.Session", project_id: str
) -> tuple[bool, Path, dict[str, "MCPServer"], list[OpenHandsRepository]]:
    """Determine whether project runs on local mounted filesystem or remote GitHub."""
    from utils.environment import (
        get_target_project_root,
        is_local_workspace_enabled,
        resolve_safe_target_path,
    )

    db_repos = (
        session.query(models.ProjectRepository)
        .filter(models.ProjectRepository.project_id == project_id)
        .all()
    )
    local_repo = next(
        (
            r
            for r in db_repos
            if r.source_type == models.McpProviderType.LOCAL_FILESYSTEM.value
        ),
        None,
    )

    if local_repo is not None or (is_local_workspace_enabled() and not db_repos):
        if local_repo and local_repo.local_path:
            workspace = resolve_safe_target_path(local_repo.local_path)
        else:
            workspace = get_target_project_root()
        workspace.mkdir(parents=True, exist_ok=True)
        repos = [
            OpenHandsRepository(
                full_name=r.full_name or r.name,
                default_branch=r.default_branch or "main",
            )
            for r in db_repos
        ]
        return True, workspace, {}, repos

    # Remote GitHub mode
    workspace = project_workspace_dir(project_id)
    repos = [
        OpenHandsRepository(
            full_name=r.full_name or r.name,
            default_branch=r.default_branch or "main",
        )
        for r in db_repos
    ]
    return False, workspace, {}, repos


def _resolve_github_access_by_project(
    session: "models.Session", project_id: str
) -> tuple[str, str]:
    project = (
        session.query(models.Project)
        .filter(models.Project.id == project_id)
        .one_or_none()
    )
    if project is None or project.organization is None:
        raise McpConfigurationError(
            f"Project '{project_id}' has no associated company."
        )
    return GithubMcpProvider().get_access_token(session, project.organization)


def _resolve_github_access(deps: AgentDeps) -> tuple[str, str]:
    """``(installation token, mcp url)`` of the project's company GitHub App."""
    if not deps.session:
        raise McpConfigurationError("Deps has no database session.")
    return _resolve_github_access_by_project(deps.session, deps.project_id)


def build_mcp_config(token: str, mcp_url: str) -> dict[str, "MCPServer"]:
    from openhands.sdk.mcp.config import MCPBearerAuthCredential, MCPServer
    from pydantic import SecretStr

    return {
        "github": MCPServer(
            url=mcp_url,
            auth=MCPBearerAuthCredential(strategy="bearer", value=SecretStr(token)),
        )
    }


def build_agent(mcp_config: dict[str, "MCPServer"]) -> "Agent":
    """Build the OpenHands agent (LLM, default tools, skills, condenser, MCP)."""
    from openhands.sdk import LLM, Agent, AgentContext, load_skills_from_dir
    from openhands.tools.preset.default import (
        get_default_condenser,
        get_default_tools,
    )
    from pydantic import SecretStr

    api_key = settings.openai_api_key
    if not api_key:
        raise ValueError(
            "No LLM API key configured for the OpenHands agent "
            "(ESTACK_OPENHANDS_LLM_API_KEY or ESTACK_OPENAI_API_KEY)."
        )

    llm = LLM(
        usage_id="openhands_agent",
        model=_model_name(),
        api_key=SecretStr(api_key),
        stream=False,
    )
    skills_dir = Path(__file__).resolve().parent / "skills"
    _, _, agent_skills = load_skills_from_dir(str(skills_dir))
    agent_context = AgentContext(skills=list(agent_skills.values()))
    tools = get_default_tools(
        enable_browser=False,
    )
    agent = Agent(
        llm=llm,
        tools=tools,
        system_prompt_kwargs={"cli_mode": True},
        condenser=get_default_condenser(
            llm=llm.model_copy(update={"usage_id": "condenser"})
        ),
        mcp_config=mcp_config,
        agent_context=agent_context,
    )
    return agent


def run_openhands_conversation(
    workspace: str,
    user_message: str,
    mcp_config: dict[str, "MCPServer"],
) -> str:
    """Run a local OpenHands conversation and return the agent's final reply."""
    from openhands.sdk import Conversation
    from openhands.sdk.conversation.response_utils import get_agent_final_response

    conversation = Conversation(agent=build_agent(mcp_config), workspace=workspace)
    try:
        conversation.send_message(user_message)
        conversation.run()
    finally:
        conversation.close()
    return get_agent_final_response(conversation.state.events)


# region repositories
def project_workspace_dir(project_id: str) -> Path:
    """Persistent workspace shared by every OpenHands run of ``project_id``."""
    workspace = _workspace_root_dir() / PROJECTS_DIRNAME / project_id
    (workspace / REPOS_DIRNAME).mkdir(parents=True, exist_ok=True)
    return workspace


def repository_dir(workspace: Path, repository: OpenHandsRepository) -> Path:
    return workspace / REPOS_DIRNAME / repository.full_name.split("/")[-1]


def _git(args: list[str], cwd: Path, token: str) -> None:
    """Run ``git`` authenticated with ``token`` without persisting it anywhere."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )


def sync_repository(
    workspace: Path, repository: OpenHandsRepository, token: str
) -> Path:
    """Clone ``repository`` if it is not there yet, otherwise ``git pull``."""
    target = repository_dir(workspace, repository)
    if not (target / ".git").is_dir():
        _git(
            [
                "clone",
                "--branch",
                repository.default_branch,
                f"https://github.com/{repository.full_name}.git",
                str(target),
            ],
            cwd=target.parent,
            token=token,
        )
    else:
        _git(["pull"], cwd=target, token=token)
    return target


def sync_repositories(
    workspace: Path, repositories: list[OpenHandsRepository], token: str
) -> None:
    for repository in repositories:
        sync_repository(workspace, repository, token)


# endregion


def describe_repositories(repositories: list[OpenHandsRepository]) -> str:
    """Bullet list of the project repositories and where each one is cloned."""
    return "\n".join(
        f"- '{repo.full_name}' (default branch '{repo.default_branch}') -> "
        f"'{REPOS_DIRNAME}/{repo.full_name.split('/')[-1]}'"
        for repo in repositories
    )


def build_execution_instructions() -> str:
    """Implement / push / open PR contract shared by the one-shot and chat agents."""
    return (
        "For every repository that needs changes:\n"
        f"1. Check out a new branch named '{BRANCH_PREFIX}/<short-slug>' from its "
        "default branch.\n"
        "2. Implement the task and run any relevant local validation/tests before "
        "committing.\n"
        "3. Commit and push your changes to that branch.\n"
        "4. Open a pull request into the default branch with a title and body "
        "that summarize the work you did.\n"
        "Do not touch repositories the task does not require."
    )


def build_user_message(prompt: str, repositories: list[OpenHandsRepository]) -> str:
    return (
        "You are working on a project made of the following GitHub repositories. "
        "Each one is already cloned and up to date with its default branch in the "
        "local directory indicated below (relative to your current working "
        "directory). Do not clone them again; work only inside those clones. You "
        "have GitHub MCP tools available to push files and open pull requests:\n"
        f"{describe_repositories(repositories)}\n\n"
        f"Task:\n{prompt}\n\n"
        f"{build_execution_instructions()}"
    )


def build_local_user_message(prompt: str, workspace: Path | None = None) -> str:
    location_info = f" ({workspace})" if workspace else ""
    return (
        f"You are working directly in a local project repository mounted at your current working directory{location_info}. "
        "You have full access to view, edit, test, and run code directly using your terminal and file tools.\n\n"
        f"Task:\n{prompt}\n\n"
        "Instructions:\n"
        "1. Check if git is initialized; if git is present, create a new local branch named 'estack/openhands/<short-slug>'.\n"
        "2. Implement the requested changes and run relevant local validation/tests.\n"
        "3. Commit your changes locally if git is initialized.\n"
        "4. Summarize all changes made, test results, and status in your final response."
    )


def _model_name() -> str:
    return _normalize_openhands_model_name(settings.default_model)


def _normalize_openhands_model_name(model_name: str) -> str:
    if ":" in model_name and "/" not in model_name:
        provider, model = model_name.split(":", 1)
        if provider and model:
            return f"{provider}/{model}"
    return model_name


def _workspace_root_dir() -> Path:
    """External root for application-managed OpenHands workspaces."""
    return settings.openhands_workspace_root

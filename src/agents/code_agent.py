from typing import Any

from agents.utils.agent_deps import AgentDeps
from agents.utils.agent_factory import create_agent_from_database
from pydantic_ai import UsageLimits
import utils.log as log
from schemas.user_story_schemas import CodeGitAgentOutput
from services.user_story_branch_service import user_story_branch_service

logger = log.setup_custom_logger(__name__)


def run(
    repositories: list,
    dev_steps: list,
    deps: AgentDeps,
) -> CodeGitAgentOutput | None:
    
    code_agent = create_agent_from_database(
        name="code_agent",
        project_id=deps.project_id,
        session=deps.session,
        agent_logger=deps.agent_logger
    )

    if not deps.agent_logger:
        logger.warning("No agent logger found in deps")
        return None

    user_message = build_user_message(repositories, dev_steps)
    logger.info(f"prompt generated: \n{user_message}")

    deps.custom_usage_tool_limit["code_git"] = 1
    result =  code_agent.run_sync(
        user_message,
        output_type=CodeGitAgentOutput,
        deps=deps,
        usage_limits=UsageLimits(tool_calls_limit=5)
    )

    logger.info(result.output)
    output: CodeGitAgentOutput = result.output
    if output.branch_name and output.repository_url:
        user_story_branch_service.save_branch_from_agent_run(output, deps)

    deps.agent_logger.log(output.output_message)
    deps.agent_logger.finish_agent_run_with_usage("Execução finalizada.", result.response.model_name, result.usage)
    return output

def build_user_message(repositories: list, dev_steps: list) -> str:
    return (
        f"Repositories: {', '.join(repositories)}\n"
        f"Development Steps: {', '.join(dev_steps)}"
    )
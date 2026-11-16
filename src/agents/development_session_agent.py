import time
from datetime import datetime

import httpx
from fastapi import HTTPException
from pydantic import Field
from pydantic_ai import RunContext
from sqlalchemy.orm import joinedload

import utils.log as log
from agents.utils.agent_deps import AgentDeps
from agents.utils.agent_factory import (
    get_agent_info,
)
from agents.utils.agent_run_utils import AgentLogger
from agents.utils.context import CONTEXT_DICT
from database import models
from database.database import SessionDep
from database.models import GherkinRule, UserStory
from schemas.base import Base
from utils.config import settings

logger = log.setup_custom_logger(__name__)

POLL_INTERVAL_SECONDS = 15
MAX_POLLS = 60


class UserStoryDevStepForAgent(Base):
    order: int = Field(
        ..., description="The order of execution of the development step"
    )
    name: str = Field(..., description="The name of the development step")
    description: str = Field(
        ..., description="Detailed description of the development step"
    )


class GherkinScenarioForAgent(Base):
    title: str = Field(..., description="Scenario title")
    text: str = Field(..., description="Scenario text")


class GherkinRuleForAgent(Base):
    title: str = Field(..., description="Rule title")
    background: str = Field(..., description="Rule background")
    scenarios: list[GherkinScenarioForAgent] = []


class UserStoryForAgent(Base):
    """
    Minimal DTO required to build the Devin prompt.

    Only contains the information actually consumed by
    build_devin_prompt().
    """

    id: str
    project_id: str

    name: str = Field(..., description="User story title")
    description: str = Field(..., description="User story description")

    dev_steps: list[UserStoryDevStepForAgent] = []

    gherkin_rules: list[GherkinRuleForAgent] = []


def build_devin_prompt(
    user_story_for_agent: UserStoryForAgent, agent_info: models.Agent, deps: AgentDeps
) -> str:
    """Builds the prompt for the Devin agent based on user story and agent info."""
    ctx = RunContext(model=None, deps=deps, usage=None)  # type: ignore

    # Gather system prompt contexts
    system_prompt_context = [
        CONTEXT_DICT[context.context_name] for context in (agent_info.context or [])
    ]
    system_prompt_context_executed = [func(ctx) for func in system_prompt_context]
    system_prompt_context_executed = "".join(system_prompt_context_executed)

    def format_dev_steps(steps: list[UserStoryDevStepForAgent]):
        if not steps:
            return ""
        return (
            "**Etapas de desenvolvimento:**\n"
            + "".join(
                f"{step.order}. **{step.name}**: {step.description}\n" for step in steps
            )
            + "\n"
        )

    def format_gherkin_rules(rules: list[GherkinRuleForAgent]):
        if not rules:
            return ""
        formatted_rules = "**Regras de negócio (Gherkin):**\n"
        for rule in rules:
            formatted_rules += f"- **{rule.title}** \n {rule.background}\n"
            for scenario in rule.scenarios:
                formatted_rules += f"### {scenario.title}\n{scenario.text}\n\n"
        return formatted_rules + "\n"

    def format_full_prompt():
        return (
            f"{agent_info.user_prompt}\n{agent_info.system_prompt}\n\n"
            "CONTEXTOS RELEVANTES:\n\n"
            f"{system_prompt_context_executed}\n\n"
            "INSTRUÇÕES DE DESENVOLVIMENTO:\n\n"
            "Implemente a seguinte história de usuário.:\n\n"
            f"**Título:** {user_story_for_agent.name}\n\n"
            f"**Descrição:** {user_story_for_agent.description}\n\n"
            f"{format_gherkin_rules(user_story_for_agent.gherkin_rules)}"
            f"{format_dev_steps(user_story_for_agent.dev_steps)}"
            "Implemente esta história de usuário seguindo os passos de desenvolvimento fornecidos. Crie um PR (Pull Request) quando terminar."
        )

    prompt = format_full_prompt()
    prompt_length = len(prompt)
    if prompt_length > settings.devin_max_prompt_length:
        diff = prompt_length - settings.devin_max_prompt_length
        logger.warning(
            "Prompt length %d exceeds max %d by %d characters. removing context to fit the prompt.",
            prompt_length,
            settings.devin_max_prompt_length,
            diff,
        )
        if diff >= len(system_prompt_context_executed):
            raise HTTPException(
                status_code=500,
                detail="Prompt too long even after removing all context.",
            )
        system_prompt_context_executed = system_prompt_context_executed[:-diff]
        system_prompt_context_executed += (
            "...(Contexto removido devido ao limite de tamanho.)"
        )

        prompt = format_full_prompt()
    return prompt


def create_devin_session(prompt: str) -> dict:
    """Creates a new session with the Devin API using the provided prompt."""
    if not settings.devin_api_key:
        raise HTTPException(status_code=500, detail="Devin API key not configured")

    headers = {
        "Authorization": f"Bearer {settings.devin_api_key}",
        "Content-Type": "application/json",
    }
    payload = {"prompt": prompt}

    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{settings.devin_api_base_url}/sessions",
            headers=headers,
            json=payload,
        )
    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="Invalid Devin API key")
    if response.status_code != 200:
        logger.error(
            "Failed to create Devin session. Status: %s, Response: %s",
            response.status_code,
            response.text,
        )
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Failed to create Devin session: {response.text}",
        )
    return response.json()


def get_devin_session(session_id: str) -> dict:
    """Retrieves the current state of a Devin session by session ID."""
    if not settings.devin_api_key:
        raise HTTPException(status_code=500, detail="Devin API key not configured")

    headers = {
        "Authorization": f"Bearer {settings.devin_api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            f"{settings.devin_api_base_url}/sessions/{session_id}",
            headers=headers,
        )
    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="Invalid Devin API key")
    if response.status_code != 200:
        logger.error(
            "Failed to get Devin session %s. Status: %s, Response: %s",
            session_id,
            response.status_code,
            response.text,
        )
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Failed to get Devin session: {response.text}",
        )
    return response.json()


def _handle_poll_error(
    session_id: str,
    attempt: int,
    poll_error: HTTPException,
) -> bool:
    """Handles polling errors, returns True if should retry."""
    logger.warning(
        "Error polling Devin session %s (attempt %d/%d): %s",
        session_id,
        attempt + 1,
        MAX_POLLS,
        poll_error.detail,
    )
    if attempt < MAX_POLLS - 1:
        time.sleep(POLL_INTERVAL_SECONDS)
        return True
    return False


def _handle_timeout(session_id: str, agent_logger: AgentLogger) -> None:
    """Handles timeout when polling Devin session."""
    agent_logger.log(
        f"Sessão Devin ({session_id}) não finalizou dentro do tempo limite. Parando o monitoramento.",
    )
    logger.warning(
        "Devin session %s polling timeout after %d attempts", session_id, MAX_POLLS
    )


def export_prompt(session: SessionDep, user_id: str, user_story_id: str) -> str:
    user_story_for_agent = retrieve_user_story_for_agent(session, user_story_id)

    agent_info, deps = structure_agent(
        session,
        user_id,
        user_story_for_agent,
    )

    prompt = build_devin_prompt(user_story_for_agent, agent_info, deps)

    return prompt


def pre_processing(
    session: SessionDep, user_id: str, user_story_id: str, agent_logger: AgentLogger
) -> str:
    """Prepares and initiates a Devin session for a user story."""
    user_story_for_agent = retrieve_user_story_for_agent(session, user_story_id)
    agent_info, deps = structure_agent(
        session,
        user_id,
        user_story_for_agent,
        agent_logger=agent_logger,
    )
    try:
        deps.agent_logger.log("Iniciando criação de sessão no Devin...")
        logger.info("Built Devin prompt for user story: %s", user_story_for_agent.name)
        logger.info("Enviando requisição para API do Devin...")
        devin_response = create_devin_session(
            build_devin_prompt(user_story_for_agent, agent_info, deps)
        )
        session_id = str(devin_response.get("id") or devin_response.get("session_id"))
        session_url = devin_response.get("url")
        if not session_id:
            logger.error("Devin API response missing session ID: %s", devin_response)
            deps.agent_logger.log(
                "Erro: Resposta da API do Devin não contém ID da sessão."
            )
            raise HTTPException(
                status_code=500, detail="Devin API response missing session ID"
            )
        logger.info(
            "Devin session created successfully. Session ID: %s, URL: %s",
            session_id,
            session_url,
        )
        deps.agent_logger.log(
            f"Sessão Devin criada com sucesso! ID: {session_id}, URL: {session_url}"
        )
    except HTTPException as e:
        logger.error("HTTP error creating Devin session: %s", e.detail)
        deps.agent_logger.log(f"Erro ao criar sessão: {e.detail}")
        deps.agent_logger.finish_agent_run()
        raise e
    except Exception as e:
        logger.error("Unexpected error creating Devin session: %s", str(e))
        deps.agent_logger.log(
            f"Erro inesperado: {str(e)}",
        )
        deps.agent_logger.finish_agent_run()
        raise HTTPException(
            status_code=500, detail="Unexpected error creating Devin session"
        )
    return session_id


def structure_agent(
    session, user_id, user_story_for_agent, agent_logger: AgentLogger | None = None
):

    project_id = user_story_for_agent.project_id

    agent_info = get_agent_info(session, "devin-api", project_id=project_id)

    if not agent_info:
        logger.error("Agent 'devin-api' not found in database.")
        raise HTTPException(status_code=500, detail="Agent not found")

    deps_kwargs = {
        "project_id": project_id,
        "user_id": user_id,
        "input_id": user_story_for_agent.id,
        "input_type": "user_story",
        "session": session,
    }
    if agent_logger is not None:
        deps_kwargs["agent_logger"] = agent_logger
    deps = AgentDeps(**deps_kwargs)
    return agent_info, deps


def retrieve_user_story_for_agent(session, user_story_id: str) -> UserStoryForAgent:
    user_story = (
        session.query(UserStory)
        .filter(UserStory.id == user_story_id)
        .options(
            joinedload(UserStory.dev_steps),
            joinedload(UserStory.gherkin_rules).joinedload(GherkinRule.scenarios),
        )
        .first()
    )

    if user_story is None:
        raise HTTPException(status_code=404, detail="UserStory not found")

    return UserStoryForAgent.model_validate(user_story)


def get_sorted_devin_messages(messages):
    filtered = [
        item for item in (messages or []) if item.get("type") == "devin_message"
    ]
    return sorted(
        filtered,
        key=lambda x: datetime.fromisoformat(x["timestamp"]),
        reverse=True,
    )


def handle_status(session_info, filtered_messages, agent_logger):
    status = session_info.get("status_enum")
    if status in ["blocked", "stopped", "finished"]:
        if filtered_messages:
            agent_logger.log(
                f"Mensagem Devin: {filtered_messages[0].get('message')}",
            )
        return True
    return False


def poll_once(session_id, attempt, last_message, agent_logger):
    try:
        session_info = get_devin_session(session_id)
    except HTTPException as poll_error:
        if not _handle_poll_error(session_id, attempt, poll_error):
            raise
        return None, last_message, False

    devin_messages = session_info.get("messages") or []
    if not devin_messages:
        logger.info(
            "No messages found for Devin session %s on attempt %d/%d",
            session_id,
            attempt + 1,
            MAX_POLLS,
        )
        time.sleep(POLL_INTERVAL_SECONDS)
        return None, last_message, False

    filtered_messages = get_sorted_devin_messages(devin_messages)
    logger.info(
        "Devin session %s, status_enum: %s, messages: %s",
        session_id,
        session_info.get("status_enum"),
        filtered_messages,
    )

    if handle_status(session_info, filtered_messages, agent_logger):
        return True, last_message, True

    if filtered_messages and filtered_messages[0] != last_message:
        last_message = filtered_messages[0]
        agent_logger.log(f"Mensagem Devin: {last_message.get('message')}")

    time.sleep(POLL_INTERVAL_SECONDS)
    return None, last_message, False


def process_in_background(
        session: SessionDep,
        user_id: str,
        user_story_id: str,
        agent_logger: AgentLogger
) -> None:
    """Polls the Devin session in the background and logs updates."""

    session_id = pre_processing(session, user_id, user_story_id, agent_logger)

    try:
        last_message = None
        for attempt in range(MAX_POLLS):
            done, last_message, should_break = poll_once(
                session_id, attempt, last_message, agent_logger
            )
            if done or should_break:
                agent_logger.log("Execução finalizada.")
                agent_logger.finish_agent_run()
                return

        _handle_timeout(session_id, agent_logger)
    except HTTPException as e:
        logger.error("HTTP error polling Devin session: %s", e.detail)
        agent_logger.log(f"Erro ao consultar sessão Devin: {e.detail}")
        agent_logger.finish_agent_run()
    except Exception as e:
        logger.error("Unexpected error polling Devin session: %s", str(e))
        agent_logger.log(f"Erro inesperado ao consultar sessão Devin: {str(e)}")
        agent_logger.finish_agent_run()

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import joinedload

from database import models
from database.database import SessionDep
from schemas.agent_schemas import (
    AgentPublic,
    ProjectAgentEffectivePublic,
    ProjectAgentOverrideFromDefaultCreate,
    ProjectAgentOverrideUpsert,
)
from utils.bearer_auth import check_project_access

router = APIRouter()


def _build_agent_public(agent: models.Agent) -> AgentPublic:
    tools = [at.tool for at in agent.tools] if agent.tools else []
    contexts = [ac.context for ac in agent.context] if agent.context else []
    mcps = [am.mcp for am in agent.mcps] if agent.mcps else []
    return AgentPublic.model_validate(
        {**agent.__dict__, "tools": tools, "contexts": contexts, "mcps": mcps}
    )


def _get_default_agent_by_name(
    session: SessionDep, agent_name: str
) -> models.Agent | None:
    from agents.utils.agent_factory import get_default_agent_info_by_name

    return get_default_agent_info_by_name(session, agent_name)


@router.get("/agents", response_model=List[ProjectAgentEffectivePublic])
def list_project_agents(
    project_id: str,
    session: SessionDep,
    user_data=Depends(check_project_access),
) -> List[ProjectAgentEffectivePublic]:
    # Source of truth for what agent_names to show: newest defaults per name.
    defaults = (
        session.query(models.Agent)
        .filter(models.Agent.is_default == True)  # noqa: E712
        .options(
            joinedload(models.Agent.tools),
            joinedload(models.Agent.context),
            joinedload(models.Agent.mcps),
        )
        .order_by(models.Agent.name.asc(), models.Agent.created_at.desc())
        .all()
    )

    # Deduplicate by name keeping newest
    default_by_name: dict[str, models.Agent] = {}
    for agent in defaults:
        if agent.name not in default_by_name:
            default_by_name[agent.name] = agent

    if not default_by_name:
        return []

    overrides = (
        session.query(models.ProjectAgent)
        .filter(models.ProjectAgent.project_id == project_id)
        .all()
    )
    override_by_name = {o.agent_name: o for o in overrides}

    result: list[ProjectAgentEffectivePublic] = []
    for agent_name, default_agent in default_by_name.items():
        override = override_by_name.get(agent_name)
        if override:
            from agents.utils.agent_factory import get_project_agent_override

            effective_agent = get_project_agent_override(
                session, project_id, agent_name
            )
            if not effective_agent:
                raise HTTPException(
                    status_code=500,
                    detail=f"ProjectAgent override exists but agent not found (agent_name={agent_name}).",
                )
            result.append(
                ProjectAgentEffectivePublic(
                    agent_name=agent_name,
                    source="override",
                    agent=_build_agent_public(effective_agent),
                    default_agent_id=default_agent.id,
                    override_agent_id=effective_agent.id,
                )
            )
        else:
            result.append(
                ProjectAgentEffectivePublic(
                    agent_name=agent_name,
                    source="default",
                    agent=_build_agent_public(default_agent),
                    default_agent_id=default_agent.id,
                    override_agent_id=None,
                )
            )
    return result


@router.put("/agents/{agent_name}/override", response_model=ProjectAgentEffectivePublic)
def upsert_project_agent_override(
    project_id: str,
    agent_name: str,
    payload: ProjectAgentOverrideUpsert,
    session: SessionDep,
    user_data=Depends(check_project_access),
) -> ProjectAgentEffectivePublic:
    default_agent = _get_default_agent_by_name(session, agent_name)
    if not default_agent:
        raise HTTPException(status_code=404, detail="Default agent not found for name.")

    target_agent = (
        session.query(models.Agent).filter_by(id=payload.agent_id).one_or_none()
    )
    if not target_agent:
        raise HTTPException(status_code=404, detail="Agent not found.")

    override = (
        session.query(models.ProjectAgent)
        .filter_by(project_id=project_id, agent_name=agent_name)
        .one_or_none()
    )
    if override:
        override.agent_id = payload.agent_id
    else:
        override = models.ProjectAgent(
            project_id=project_id,
            agent_name=agent_name,
            agent_id=payload.agent_id,
        )
        session.add(override)
    session.commit()

    return ProjectAgentEffectivePublic(
        agent_name=agent_name,
        source="override",
        agent=_build_agent_public(target_agent),
        default_agent_id=default_agent.id,
        override_agent_id=target_agent.id,
    )


@router.post(
    "/agents/{agent_name}/override-from-default",
    response_model=ProjectAgentEffectivePublic,
)
def override_from_default(
    project_id: str,
    agent_name: str,
    payload: ProjectAgentOverrideFromDefaultCreate,
    session: SessionDep,
    user_data=Depends(check_project_access),
) -> ProjectAgentEffectivePublic:
    default_agent = _get_default_agent_by_name(session, agent_name)
    if not default_agent:
        raise HTTPException(status_code=404, detail="Default agent not found for name.")

    # Clone default into a new agent (non-default) and apply edits
    cloned = _clone_default_agent(payload, default_agent)
    session.add(cloned)
    session.flush()  # get cloned.id

    _set_tools(payload, session, default_agent, cloned)
    _set_contexts(payload, session, default_agent, cloned)
    _set_mcps(payload, session, default_agent, cloned)

    # Upsert override mapping
    override = (
        session.query(models.ProjectAgent)
        .filter_by(project_id=project_id, agent_name=agent_name)
        .one_or_none()
    )
    if override:
        override.agent_id = cloned.id
    else:
        override = models.ProjectAgent(
            project_id=project_id,
            agent_name=agent_name,
            agent_id=cloned.id,
        )
        session.add(override)

    session.commit()
    session.refresh(cloned)

    return ProjectAgentEffectivePublic(
        agent_name=agent_name,
        source="override",
        agent=_build_agent_public(cloned),
        default_agent_id=default_agent.id,
        override_agent_id=cloned.id,
    )


def _set_mcps(payload, session, default_agent, cloned):
    if payload.mcp_names is None:
        for am in default_agent.mcps or []:
            session.add(models.AgentMcp(agent_id=cloned.id, mcp_name=am.mcp_name))
    else:
        for mcp_name in payload.mcp_names:
            mcp = session.query(models.Mcp).filter_by(name=mcp_name).first()
            if mcp:
                session.add(models.AgentMcp(agent_id=cloned.id, mcp_name=mcp.name))


def _set_contexts(payload, session, default_agent, cloned):
    if payload.context_names is None:
        for ac in default_agent.context or []:
            session.add(
                models.AgentContext(agent_id=cloned.id, context_name=ac.context_name)
            )
    else:
        for context_name in payload.context_names:
            context = session.query(models.Context).filter_by(name=context_name).first()
            if context:
                session.add(
                    models.AgentContext(agent_id=cloned.id, context_name=context.name)
                )


def _set_tools(payload, session, default_agent, cloned):
    if payload.tool_names is None:
        for at in default_agent.tools or []:
            session.add(models.AgentTool(agent_id=cloned.id, tool_name=at.tool_name))
    else:
        for tool_name in payload.tool_names:
            tool = session.query(models.Tool).filter_by(name=tool_name).first()
            if tool:
                session.add(models.AgentTool(agent_id=cloned.id, tool_name=tool.name))


def _clone_default_agent(payload, default_agent):
    cloned = models.Agent(
        name=default_agent.name,
        model=payload.model or default_agent.model,
        user_prompt=payload.user_prompt or default_agent.user_prompt,
        system_prompt=payload.system_prompt or default_agent.system_prompt,
        temperature=payload.temperature
        if payload.temperature is not None
        else default_agent.temperature,
        max_retries=payload.max_retries
        if payload.max_retries is not None
        else default_agent.max_retries,
        is_default=False,
    )

    return cloned


@router.delete("/agents/{agent_name}/override", response_model=dict)
def delete_project_agent_override(
    project_id: str,
    agent_name: str,
    session: SessionDep,
    user_data=Depends(check_project_access),
):
    override = (
        session.query(models.ProjectAgent)
        .filter_by(project_id=project_id, agent_name=agent_name)
        .one_or_none()
    )
    if override:
        agent_id = override.agent_id

        # Set agent_id to NULL in agent_log entries (preserve logs)
        session.query(models.AgentLog).filter_by(agent_id=agent_id).update(
            {"agent_id": None}
        )

        # Delete related entries in agent_tool, agent_context, agent_mcp
        session.query(models.AgentTool).filter_by(agent_id=agent_id).delete()
        session.query(models.AgentContext).filter_by(agent_id=agent_id).delete()
        session.query(models.AgentMcp).filter_by(agent_id=agent_id).delete()

        # Delete the project_agent override entry
        session.delete(override)

        # Delete the cloned agent
        agent = session.query(models.Agent).filter_by(id=agent_id).one_or_none()
        if agent:
            session.delete(agent)

        session.commit()
    return {"ok": True}

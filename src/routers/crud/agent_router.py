from typing import Annotated, Dict, List
from fastapi import APIRouter, Depends

from agents.utils.supported_models import SupportedModels
from database.database import SessionDep, delete_entity
from database.models import (
    Agent,
    AgentContext,
    AgentMcp,
    AgentTool,
    Context,
    Mcp,
    Tool,
    User,
)
from schemas.agent_schemas import (
    AgentCreate,
    AgentPublic,
    AgentUpdate,
    ContextPublic,
    MCPPublic,
    ToolPublic,
)
from utils.bearer_auth import retrieve_user_data
from utils.validation import validate_entity_exists
import utils.log as log

router = APIRouter()
logger = log.setup_custom_logger(__name__)


# LIST SUPPORTED MODELS
@router.get("/supported-models", response_model=Dict[str, List[str]])
def list_supported_models(
    user_data: Annotated[User, Depends(retrieve_user_data)],
):
    return SupportedModels.PROVIDERS


# LIST ALL MCPS
@router.get("/agent-mcps", response_model=List[MCPPublic])
def list_mcps(
    session: SessionDep, user_data: Annotated[User, Depends(retrieve_user_data)]
):
    mcps = session.query(Mcp).all()
    return [MCPPublic.model_validate(mcp) for mcp in mcps]


# CREATE
@router.post("/", response_model=AgentPublic)
def create_agent(
    agent_in: AgentCreate,
    session: SessionDep,
    user_data: Annotated[User, Depends(retrieve_user_data)],
):
    agent = Agent(
        name=agent_in.name,
        model=agent_in.model,
        user_prompt=agent_in.user_prompt,
        system_prompt=agent_in.system_prompt,
        is_default=agent_in.is_default,
        temperature=agent_in.temperature,
        max_retries=agent_in.max_retries,
    )
    session.add(agent)
    session.flush()  # get agent.id
    # Add tools
    for tool_name in agent_in.tool_names:
        tool = session.query(Tool).filter_by(name=tool_name).first()
        if tool:
            session.add(AgentTool(agent_id=agent.id, tool_name=tool.name))
    # Add contexts
    for context_name in agent_in.context_names:
        context = session.query(Context).filter_by(name=context_name).first()
        if context:
            session.add(AgentContext(agent_id=agent.id, context_name=context.name))
    # Add Mcps
    for mcp_name in agent_in.mcp_names:
        mcp = session.query(Mcp).filter_by(name=mcp_name).first()
        if mcp:
            agent.mcps.append(AgentMcp(agent_id=agent.id, mcp_name=mcp.name))

    session.commit()
    # Prepare response
    session.refresh(agent)
    tools = [at.tool for at in agent.tools] if agent.tools else []
    contexts = [ac.context for ac in agent.context] if agent.context else []
    mcps = [am.mcp for am in agent.mcps] if agent.mcps else []
    return AgentPublic.model_validate(
        {**agent.__dict__, "tools": tools, "contexts": contexts, "mcps": mcps}
    )


# READ (list)
@router.get("/", response_model=List[AgentPublic])
def list_agents(
    session: SessionDep,
    user_data: Annotated[User, Depends(retrieve_user_data)],
):
    query = session.query(Agent).where(Agent.is_default)
    agents = query.all()
    result = []
    for agent in agents:
        tools = [at.tool for at in agent.tools] if agent.tools else []
        contexts = [ac.context for ac in agent.context] if agent.context else []
        mcps = [am.mcp for am in agent.mcps] if agent.mcps else []
        result.append(
            AgentPublic.model_validate(
                {**agent.__dict__, "tools": tools, "contexts": contexts, "mcps": mcps}
            )
        )
    return result


# READ (single)
@router.get("/{agent_id}", response_model=AgentPublic)
def get_agent(
    agent_id: str,
    session: SessionDep,
    user_data: Annotated[User, Depends(retrieve_user_data)],
):
    agent = validate_entity_exists(session, Agent, agent_id, "Agent")
    tools = [at.tool for at in agent.tools] if agent.tools else []
    contexts = [ac.context for ac in agent.context] if agent.context else []
    mcps = [am.mcp for am in agent.mcps] if agent.mcps else []
    return AgentPublic.model_validate(
        {**agent.__dict__, "tools": tools, "contexts": contexts, "mcps": mcps}
    )


def _update_agent_tools(session: SessionDep, agent_id: str, tool_names: List[str]):
    session.query(AgentTool).filter_by(agent_id=agent_id).delete()
    for tool_name in tool_names:
        tool = session.query(Tool).filter_by(name=tool_name).first()
        if tool:
            session.add(AgentTool(agent_id=agent_id, tool_name=tool.name))


def _update_agent_contexts(
    session: SessionDep, agent_id: str, context_names: List[str]
):
    session.query(AgentContext).filter_by(agent_id=agent_id).delete()
    for context_name in context_names:
        context = session.query(Context).filter_by(name=context_name).first()
        if context:
            session.add(AgentContext(agent_id=agent_id, context_name=context.name))


def _update_agent_mcps(session: SessionDep, agent_id: str, mcp_names: List[str]):
    session.query(AgentMcp).filter_by(agent_id=agent_id).delete()
    for mcp_name in mcp_names:
        mcp = session.query(Mcp).filter_by(name=mcp_name).first()
        if mcp:
            session.add(AgentMcp(agent_id=agent_id, mcp_name=mcp.name))


def _build_agent_response(agent: Agent) -> AgentPublic:
    tools = [at.tool for at in agent.tools] if agent.tools else []
    contexts = [ac.context for ac in agent.context] if agent.context else []
    mcps = [am.mcp for am in agent.mcps] if agent.mcps else []
    return AgentPublic.model_validate(
        {**agent.__dict__, "tools": tools, "contexts": contexts, "mcps": mcps}
    )


# UPDATE
@router.put("/{agent_id}", response_model=AgentPublic)
def update_agent(
    agent_id: str,
    agent_in: AgentUpdate,
    session: SessionDep,
    user_data: Annotated[User, Depends(retrieve_user_data)],
):
    agent = validate_entity_exists(session, Agent, agent_id, "Agent")
    # Update agent fields
    for field, value in agent_in.model_dump(exclude_unset=True).items():
        if field not in ["tool_names", "context_names", "mcp_names"]:
            setattr(agent, field, value)
    session.flush()
    # Update tools
    if agent_in.tool_names is not None:
        _update_agent_tools(session, agent.id, agent_in.tool_names)
    # Update contexts
    if agent_in.context_names is not None:
        _update_agent_contexts(session, agent.id, agent_in.context_names)
    # Update mcps
    if agent_in.mcp_names is not None:
        _update_agent_mcps(session, agent.id, agent_in.mcp_names)
    session.commit()
    session.refresh(agent)
    return _build_agent_response(agent)


# DELETE
@router.delete("/{agent_id}", response_model=dict)
def delete_agent(
    agent_id: str,
    session: SessionDep,
    user_data: Annotated[User, Depends(retrieve_user_data)],
):
    agent = validate_entity_exists(session, Agent, agent_id, "Agent")
    session.query(AgentTool).filter_by(agent_id=agent.id).delete()
    session.query(AgentContext).filter_by(agent_id=agent.id).delete()
    session.query(AgentMcp).filter_by(agent_id=agent.id).delete()
    return delete_entity(session, agent)
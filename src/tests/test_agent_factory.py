import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_ai import Tool
from sqlalchemy.orm import Session

from agents.utils import agent_factory
from agents.utils.agent_deps import AgentDeps
from agents.utils.agent_factory import (
    AgentSpec,
    LoggingToolset,
    _agent_spec_cache,
    _to_spec,
    build_mcp_toolsets,
    build_model_settings,
    create_agent_from_database,
    get_agent_info,
    get_agent_info_by_name,
    get_agent_spec,
    get_default_agent_info_by_name,
    get_project_agent_override,
    memoized_context,
)
from agents.utils.agent_run_utils import AgentLogger
from agents.utils.mcp_providers import McpConfigurationError
from database import models


def test_agent_spec_and_cache(db_session: Session, sample_project):
    agent = models.Agent(
        id=str(uuid.uuid4()),
        name="spec_agent",
        model="openai:gpt-4o",
        system_prompt="sys prompt",
        user_prompt="usr prompt",
        temperature=0.7,
        max_retries=3,
        is_default=True,
    )
    db_session.add(agent)
    db_session.commit()

    spec = _to_spec(agent)
    assert spec.id == agent.id
    assert spec.name == "spec_agent"
    assert spec.model == "openai:gpt-4o"
    assert spec.temperature == 0.7
    assert spec.max_retries == 3

    # Clear cache and test get_agent_spec
    _agent_spec_cache.clear()
    spec1 = get_agent_spec(db_session, "spec_agent", sample_project.id)
    assert spec1 is not None
    assert spec1.id == agent.id

    # Second call should fetch from cache
    spec2 = get_agent_spec(db_session, "spec_agent", sample_project.id)
    assert spec2 is spec1

    # Nonexistent agent
    assert get_agent_spec(db_session, "non_existent_spec_agent", None) is None


def test_get_agent_info_variants(db_session: Session, sample_project):
    # Case 1: Multiple defaults, should pick newest
    agent1 = models.Agent(
        id=str(uuid.uuid4()),
        name="variant_agent_1",
        model="gpt-4o-mini",
        is_default=True,
        max_retries=1,
    )
    db_session.add(agent1)
    db_session.commit()

    agent2 = models.Agent(
        id=str(uuid.uuid4()),
        name="variant_agent_2",
        model="gpt-4o",
        is_default=True,
        max_retries=1,
    )
    db_session.add(agent2)
    db_session.commit()

    resolved = get_default_agent_info_by_name(db_session, "variant_agent_2")
    assert resolved.id == agent2.id

    # Case 2: No is_default flag set (all False), fallback to newest
    agent_no_default = models.Agent(
        id=str(uuid.uuid4()),
        name="no_default_agent",
        model="gpt-4o",
        is_default=False,
        max_retries=1,
    )
    db_session.add(agent_no_default)
    db_session.commit()

    resolved_no_default = get_default_agent_info_by_name(db_session, "no_default_agent")
    assert resolved_no_default.id == agent_no_default.id

    # Case 3: Project override exists
    override_agent = models.Agent(
        id=str(uuid.uuid4()),
        name="overridden_agent",
        model="claude-3-5-sonnet",
        is_default=False,
        max_retries=1,
    )
    db_session.add(override_agent)
    db_session.commit()

    project_agent = models.ProjectAgent(
        id=str(uuid.uuid4()),
        project_id=sample_project.id,
        agent_name="variant_agent_override",
        agent_id=override_agent.id,
    )
    db_session.add(project_agent)
    db_session.commit()

    override_res = get_project_agent_override(db_session, sample_project.id, "variant_agent_override")
    assert override_res.id == override_agent.id

    # No override for another name
    assert get_project_agent_override(db_session, sample_project.id, "unknown_name") is None

    # get_agent_info with project_id
    info_with_proj = get_agent_info(db_session, "variant_agent_override", sample_project.id)
    assert info_with_proj.id == override_agent.id

    # get_agent_info_by_name (no project)
    info_by_name = get_agent_info_by_name(db_session, "variant_agent_2")
    assert info_by_name.id == agent2.id


def test_logging_toolset_call_tool():
    async def run():
        toolset = LoggingToolset([])
        
        mock_ctx = MagicMock()
        mock_deps = MagicMock(spec=AgentDeps)
        mock_deps.try_use_tool.return_value = False
        mock_deps.custom_usage_tool_limit = {"test_tool": 2}
        mock_deps.agent_logger = MagicMock()
        mock_ctx.deps = mock_deps

        mock_tool = MagicMock()

        # Tool limit reached branch
        res = await toolset.call_tool("test_tool", {}, mock_ctx, mock_tool)
        assert "máximo de vezes permitido" in res
        assert mock_deps.agent_logger.log.called

        # Tool limit allowed branch
        mock_deps.try_use_tool.return_value = True
        with patch("pydantic_ai.WrapperToolset.call_tool", new_callable=AsyncMock) as mock_super_call:
            mock_super_call.return_value = "super result"
            res2 = await toolset.call_tool("test_tool", {}, mock_ctx, mock_tool)
            assert res2 == "super result"

    asyncio.run(run())


def test_memoized_context():
    called_count = 0
    def sample_context_fn(ctx):
        nonlocal called_count
        called_count += 1
        return "context value"

    instruction = memoized_context("my_ctx", sample_context_fn)

    mock_ctx = MagicMock()
    mock_ctx.deps.context_cache = {}

    res1 = instruction(mock_ctx)
    assert res1 == "context value"
    assert called_count == 1

    # Second call should use cache
    res2 = instruction(mock_ctx)
    assert res2 == "context value"
    assert called_count == 1


def test_build_mcp_toolsets(db_session: Session, sample_project):
    # Mock MCPS_DICT
    with patch.dict("agents.utils.agent_factory.MCPS_DICT", {
        "mcp_good": lambda a, p, s: "good_toolset",
        "mcp_bad": MagicMock(side_effect=McpConfigurationError("Error config")),
    }):
        # optional = True
        mock_logger = MagicMock(spec=AgentLogger)
        toolsets = build_mcp_toolsets(
            "agent_1", sample_project.id, db_session, ("mcp_good", "mcp_bad"), mock_logger, optional=True
        )
        assert toolsets == ["good_toolset"]
        assert mock_logger.log.called

        # optional = False, should raise McpConfigurationError
        with pytest.raises(McpConfigurationError):
            build_mcp_toolsets(
                "agent_1", sample_project.id, db_session, ("mcp_bad",), None, optional=False
            )


def test_create_agent_from_database(db_session: Session, sample_project, sample_admin_user):
    agent_record = models.Agent(
        id=str(uuid.uuid4()),
        name="full_agent",
        model="test",
        system_prompt="system",
        user_prompt="user",
        temperature=0.5,
        max_retries=2,
        is_default=True,
    )
    db_session.add(agent_record)
    db_session.commit()

    run = models.AgentRun(
        id=str(uuid.uuid4()),
        user_id=sample_admin_user.id,
        project_id=sample_project.id,
        title="Agent Run",
        status="running",
        agent_id=None,
    )
    db_session.add(run)
    db_session.commit()

    logger = AgentLogger(agent_run_id=run.id, notify=False)

    with patch.dict("agents.utils.agent_factory.TOOLS_DICT", {}), \
         patch.dict("agents.utils.agent_factory.CONTEXT_DICT", {}):
        agent = create_agent_from_database(
            name="full_agent",
            session=db_session,
            project_id=sample_project.id,
            agent_logger=logger,
        )
        assert agent.name == "full_agent"
        assert agent._instructions[:3] == ["user", "system", agent_factory.REFERENCE_INSTRUCTIONS]

        db_session.refresh(run)
        assert run.agent_id == agent_record.id

    # Not found raises ValueError and logs
    mock_logger = MagicMock(spec=AgentLogger)
    with pytest.raises(ValueError, match="Agent with name 'non_existent_agent' not found"):
        create_agent_from_database(
            name="non_existent_agent",
            session=db_session,
            project_id=sample_project.id,
            agent_logger=mock_logger,
        )
    assert mock_logger.finish_agent_run_with_usage.called

import inspect

from agents.utils.mcp_providers import (
    MCP_INIT_TIMEOUT_SECONDS,
    MCP_READ_TIMEOUT_SECONDS,
    remove_disallowed_tools,
    resolve_mcp_toolset_for_domain,
)
from database import models
from pydantic_ai.mcp import MCPToolset
from sqlalchemy.orm import Session
from utils import log
from utils.config import settings

logger = log.setup_custom_logger(__name__)


class McpsRegistry:
    @staticmethod
    def ask_devin(agent_name: str, project_id: str, session: Session):
        """Devin MCP Server"""
        devin_mcp = MCPToolset(
            "https://mcp.devin.ai/mcp",
            headers={"Authorization": f"Bearer {settings.devin_api_key}"},
            init_timeout=MCP_INIT_TIMEOUT_SECONDS,
            read_timeout=MCP_READ_TIMEOUT_SECONDS,
        )
        devin_mcp = remove_disallowed_tools(devin_mcp, ["read_wiki_contents"])
        return devin_mcp

    @staticmethod
    def git_mcp(agent_name: str, project_id: str, session: Session):
        """Source-code MCP Server (vendor resolved from the company's config)"""
        return resolve_mcp_toolset_for_domain(
            session, project_id, models.McpDomain.SCM.value, agent_name
        )

    @staticmethod
    def db_mcp(agent_name: str, project_id: str, session: Session):
        """Read-only database MCP Server: maps the project's databases (structure, relationships, rules and masked data), engine resolved from the project's database config"""
        return resolve_mcp_toolset_for_domain(
            session, project_id, models.McpDomain.DATABASE.value, agent_name
        )


_MCPS_DICT = inspect.getmembers(McpsRegistry, predicate=inspect.isfunction)
MCPS_DICT = dict(_MCPS_DICT)


def _sync_mcps(session: Session):
    for name, func in MCPS_DICT.items():
        mcp_entry = session.query(models.Mcp).filter_by(name=name).scalar()
        if mcp_entry:
            mcp_entry.name = name
            mcp_entry.description = func.__doc__ or "No description available."
        else:
            mcp_entry = models.Mcp(
                name=name,
                description=func.__doc__ or "No description available.",
            )
        session.add(mcp_entry)
    session.commit()


def sync_mcps(session):
    if hasattr(session, "query"):
        _sync_mcps(session)
    else:
        with session as s:
            _sync_mcps(s)

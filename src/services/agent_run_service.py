from sqlalchemy import func
from sqlalchemy.orm import Session

from database.database import SessionDep
from database.models import Agent, AgentRun
from utils import log

logger = log.setup_custom_logger(__name__)


class AgentRunService:
    def __init__(self):
        self.model_label = func.coalesce(
            func.nullif(AgentRun.model, ""),
            "Não informado",
        )

    def get_usage_rows_filtered_by_projects_ids_grouped_by_user(self, session: Session, project_ids: list[str]):
        return (
            session.query(
                AgentRun.user_id,
                self.model_label.label("model"),
                AgentRun.agent_id.label("agent_id"),
                Agent.name.label("agent_name"),
                (
                    func.coalesce(func.sum(AgentRun.input_tokens), 0)
                    + func.coalesce(func.sum(AgentRun.output_tokens), 0)
                ).label("total_tokens"),
            )
            .outerjoin(Agent, AgentRun.agent_id == Agent.id)
            .filter(AgentRun.project_id.in_(project_ids))
            .group_by(AgentRun.user_id, self.model_label, AgentRun.agent_id, Agent.name)
            .all()
        )

    def get_usage_rows_filtered_by_projects_ids_grouped_by_project(self, session: Session, project_ids: list[str]):
        return (
            session.query(
                AgentRun.project_id,
                self.model_label.label("model"),
                AgentRun.agent_id.label("agent_id"),
                Agent.name.label("agent_name"),
                (
                    func.coalesce(func.sum(AgentRun.input_tokens), 0)
                    + func.coalesce(func.sum(AgentRun.output_tokens), 0)
                ).label("total_tokens"),
            )
            .outerjoin(Agent, AgentRun.agent_id == Agent.id)
            .filter(AgentRun.project_id.in_(project_ids))
            .group_by(AgentRun.project_id, self.model_label, AgentRun.agent_id, Agent.name)
            .all()
        )


agent_run_service = AgentRunService()

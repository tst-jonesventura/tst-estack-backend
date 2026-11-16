from datetime import timedelta

from database.database import create_session
from database.models import AgentLog, AgentRun, User
from pydantic_ai import RunUsage
from schemas.base import date_now
from services.notification_service import notification_service
from sqlalchemy import func
from sqlalchemy.orm import Session
from utils import log

logger = log.setup_custom_logger(__name__)

# A healthy run logs every tool call, seconds apart. Silence this long means the
# run died: an MCP call that never returned, a dyno recycle, an unhandled error.
STALE_RUN_TIMEOUT = timedelta(minutes=15)


def fail_stale_runs(session: Session, project_id: str | None = None) -> int:
    """Mark runs that stopped showing signs of life as failed, returning how many.

    Only the tutor reconciled its own dead turns, so every other agent left rows
    stuck in "running" forever, some for weeks, and the Processamentos tab kept
    reporting them as still working.
    """
    cutoff = date_now() - STALE_RUN_TIMEOUT
    last_activity = (
        session.query(
            AgentLog.agent_run_id.label("run_id"),
            func.max(AgentLog.timestamp).label("last_at"),
        )
        .group_by(AgentLog.agent_run_id)
        .subquery()
    )
    query = (
        session.query(AgentRun.id)
        .outerjoin(last_activity, last_activity.c.run_id == AgentRun.id)
        .filter(
            AgentRun.status == "running",
            func.coalesce(last_activity.c.last_at, AgentRun.created_at) < cutoff,
        )
    )
    if project_id is not None:
        query = query.filter(AgentRun.project_id == project_id)

    stale_ids = [row[0] for row in query.all()]
    if not stale_ids:
        return 0

    (
        session.query(AgentRun)
        .filter(AgentRun.id.in_(stale_ids))
        .update({AgentRun.status: "failed"}, synchronize_session=False)
    )
    session.commit()
    logger.warning("Reconciled %d stale agent run(s): %s", len(stale_ids), stale_ids)
    return len(stale_ids)


class AgentLogger:
    agent_run_id: str

    def __init__(self, agent_run_id: str, notify: bool = True):
        self.agent_run_id = agent_run_id
        self.notify = notify

    def log(self, message: str, is_finished: bool = False):
        with create_session() as session:
            agent_run = (
                session.query(AgentRun).filter(AgentRun.id == self.agent_run_id).first()
            )

            if agent_run:
                log_entry = AgentLog(
                    agent_run_id=self.agent_run_id,
                    message=message,
                )
                session.add(log_entry)
                session.commit()
                session.refresh(log_entry)

                if self.notify:
                    # Create and broadcast in-app notification
                    notification_service.notify_project_members(
                        session=session,
                        project_id=agent_run.project_id,
                        message={
                            "agent_run_id": self.agent_run_id,
                            "title": agent_run.title,
                            "message": message,
                            "timestamp": log_entry.timestamp,
                        },
                        notification_type="AGENT_RUN_UPDATED",
                    )

                if is_finished:
                    self.finish_agent_run()

    def finish_agent_run(self):
        with create_session() as session:
            agent_run = (
                session.query(AgentRun).filter(AgentRun.id == self.agent_run_id).first()
            )
            if agent_run:
                agent_run.status = "finished"
                session.commit()

                if self.notify:
                    # Create and broadcast in-app notification
                    notification_service.notify_project_members(
                        session=session,
                        project_id=agent_run.project_id,
                        message={
                            "agent_run_id": self.agent_run_id,
                            "title": agent_run.title,
                            "message": f"Agent run '{agent_run.title}' has finished.",
                        },
                        notification_type="AGENT_RUN_FINISHED",
                    )

    def finish_agent_run_with_usage(
        self, message: str, model: str | None, usage: RunUsage | None
    ):
        with create_session() as session:
            log_entry = AgentLog(
                agent_run_id=self.agent_run_id,
                message=message,
            )
            session.add(log_entry)
            session.commit()
            session.refresh(log_entry)

            agent_run = (
                session.query(AgentRun).filter(AgentRun.id == self.agent_run_id).first()
            )
            if agent_run:
                agent_run.model = model
                agent_run.status = "finished"

                if usage:
                    agent_run.requests = usage.requests
                    agent_run.tool_calls = usage.tool_calls
                    agent_run.input_tokens = usage.input_tokens
                    agent_run.output_tokens = usage.output_tokens
                    agent_run.cache_read_tokens = usage.cache_read_tokens
                    agent_run.cache_write_tokens = usage.cache_write_tokens
                    agent_run.input_audio_tokens = usage.input_audio_tokens
                    agent_run.cache_audio_read_tokens = usage.cache_audio_read_tokens

                if self.notify:
                    # Create and broadcast in-app notification
                    notification_service.notify_project_members(
                        session=session,
                        project_id=agent_run.project_id,
                        message={
                            "agent_run_id": self.agent_run_id,
                            "title": agent_run.title,
                            "message": f"Agent run '{agent_run.title}' has finished.",
                        },
                        notification_type="AGENT_RUN_FINISHED",
                    )


class NullAgentLogger(AgentLogger):
    """No-op stand-in so callers never need to null-check ``deps.agent_logger``."""

    def __init__(self):
        """Empty constructor for NullAgentLogger."""

    def __bool__(self) -> bool:
        # Preserves existing `if deps.agent_logger:` truthiness checks across the codebase.
        return False

    def log(self, message: str, is_finished: bool = False):
        """No-op log method for NullAgentLogger."""

    def finish_agent_run(self):
        """No-op finish_agent_run method for NullAgentLogger."""

    def finish_agent_run_with_usage(
        self, message: str, model: str | None, usage: RunUsage | None
    ):
        """No-op finish_agent_run_with_usage method for NullAgentLogger."""


def create_agent_logger(
    session: Session,
    user_id: str,
    project_id: str,
    agent_run_title: str,
    model: str | None = None,
    user_story_id: str | None = None,
    queue_key: str | None = None,
    agent_id: str | None = None,
    agent_name: str | None = None,
) -> AgentLogger:
    logger.info(f"agent_run_title '{agent_run_title}'")
    if not agent_id and agent_name:
        from agents.utils.agent_factory import get_agent_info
        resolved_agent = get_agent_info(
            session=session, agent_name=agent_name, project_id=project_id
        )
        if resolved_agent:
            agent_id = resolved_agent.id

    agent_run = AgentRun(
        model=model,
        user_id=user_id,
        status="running",
        title=agent_run_title,
        project_id=project_id,
        user_story_id=user_story_id,
        queue_key=queue_key,
        agent_id=agent_id,
    )
    session.add(agent_run)
    session.commit()
    session.refresh(agent_run)

    # Create and broadcast in-app notification for all project members
    notification_service.notify_project_members(
        session=session,
        project_id=project_id,
        message={
            "title": agent_run_title,
            "agent_run_id": agent_run.id,
            "message": f"Agent run '{agent_run_title}' has started.",
            "user": {
                "id": user_id,
                "email": session.query(User).get(user_id).email or "",
            },
            "timestamp": agent_run.created_at,
        },
        notification_type="AGENT_RUN_CREATED",
    )

    agent_logger = AgentLogger(agent_run.id)
    return agent_logger

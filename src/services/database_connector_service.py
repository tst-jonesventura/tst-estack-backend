"""Connectors that read databases on a company's network, and their job queue."""
import hashlib
import json
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from database.database import create_session
from database.models import ConnectorJobStatus, DatabaseConnector, DatabaseConnectorJob
from database_access.guard import DatabaseAccessError, DatabaseTarget
from database_access.operations import DirectExecutor, validate_operation
from utils import log

logger = log.setup_custom_logger(__name__)

TOKEN_PREFIX = "estk_"
ONLINE_WINDOW_SECONDS = 90
JOB_WAIT_SECONDS = 50.0
JOB_POLL_SECONDS = 0.25
LONG_POLL_MAX_SECONDS = 25
LONG_POLL_INTERVAL_SECONDS = 0.5
MAX_JOBS_PER_POLL = 4
MAX_RESULT_CHARS = 256_000
FINISHED_JOB_RETENTION = timedelta(hours=1)

CONNECTOR_OFFLINE = (
    "O conector '{name}' está offline (último contato: {seen}). Verifique se o "
    "serviço está rodando na rede do cliente."
)
CONNECTOR_TIMEOUT = (
    "O conector '{name}' não respondeu em {seconds:.0f}s. Ele pode estar lento ou "
    "sem acesso ao banco; siga com o que já reuniu."
)
CONNECTOR_REVOKED = "O conector deste banco foi revogado ou removido."


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


# region Tokens
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_hint(token: str) -> str:
    return token[: len(TOKEN_PREFIX) + 4]


def create_connector(
    session: Session, organization_id: str, name: str
) -> tuple[DatabaseConnector, str]:
    """A new connector and its token, which is returned here and never again."""
    token = new_token()
    connector = DatabaseConnector(
        organization_id=organization_id,
        name=name,
        token_hash=hash_token(token),
        token_hint=token_hint(token),
        is_active=True,
    )
    session.add(connector)
    session.commit()
    session.refresh(connector)
    return connector, token


def rotate_token(session: Session, connector: DatabaseConnector) -> str:
    token = new_token()
    connector.token_hash = hash_token(token)
    connector.token_hint = token_hint(token)
    session.commit()
    return token


def authenticate(session: Session, token: str | None) -> DatabaseConnector | None:
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    connector = (
        session.query(DatabaseConnector)
        .filter(DatabaseConnector.token_hash == hash_token(token))
        .one_or_none()
    )
    if connector is None or not connector.is_active:
        return None
    return connector


# endregion


# region Status
def is_online(connector: DatabaseConnector, now: datetime | None = None) -> bool:
    seen = _aware(connector.last_seen_at)
    if seen is None:
        return False
    return (now or utcnow()) - seen <= timedelta(seconds=ONLINE_WINDOW_SECONDS)


def reported_databases(connector: DatabaseConnector) -> list[dict[str, Any]]:
    if not connector.reported_databases:
        return []
    try:
        databases = json.loads(connector.reported_databases)
    except ValueError:
        return []
    return databases if isinstance(databases, list) else []


def reported_database(connector: DatabaseConnector, alias: str) -> dict[str, Any] | None:
    for database in reported_databases(connector):
        if str(database.get("alias", "")).lower() == alias.lower():
            return database
    return None


def record_heartbeat(
    session: Session,
    connector: DatabaseConnector,
    version: str | None,
    databases: list[dict[str, Any]],
) -> None:
    connector.version = version
    connector.last_seen_at = utcnow()
    connector.reported_databases = json.dumps(databases, ensure_ascii=False, default=str)
    session.commit()


# endregion


# region Jobs, connector side
def claim_jobs(
    session: Session, connector: DatabaseConnector, limit: int = MAX_JOBS_PER_POLL
) -> list[DatabaseConnectorJob]:
    """Pending jobs this connector now owns; the guarded UPDATE prevents double claims."""
    candidates = (
        session.query(DatabaseConnectorJob.id)
        .filter(
            DatabaseConnectorJob.connector_id == connector.id,
            DatabaseConnectorJob.status == ConnectorJobStatus.PENDING.value,
        )
        .order_by(DatabaseConnectorJob.created_at)
        .limit(limit)
        .all()
    )
    claimed = []
    for (job_id,) in candidates:
        updated = (
            session.query(DatabaseConnectorJob)
            .filter(
                DatabaseConnectorJob.id == job_id,
                DatabaseConnectorJob.status == ConnectorJobStatus.PENDING.value,
            )
            .update(
                {"status": ConnectorJobStatus.RUNNING.value, "picked_at": utcnow()},
                synchronize_session=False,
            )
        )
        if updated == 1:
            claimed.append(job_id)
    session.commit()
    if not claimed:
        return []
    return (
        session.query(DatabaseConnectorJob)
        .filter(DatabaseConnectorJob.id.in_(claimed))
        .order_by(DatabaseConnectorJob.created_at)
        .all()
    )


def complete_job(
    session: Session,
    connector: DatabaseConnector,
    job_id: str,
    result: dict[str, Any] | None,
    error: str | None,
) -> bool:
    """Store a job's outcome; False when the job is not this connector's running job."""
    job = (
        session.query(DatabaseConnectorJob)
        .filter(
            DatabaseConnectorJob.id == job_id,
            DatabaseConnectorJob.connector_id == connector.id,
            DatabaseConnectorJob.status == ConnectorJobStatus.RUNNING.value,
        )
        .one_or_none()
    )
    if job is None:
        return False
    if error is not None or result is None:
        job.status = ConnectorJobStatus.ERROR.value
        job.error = (error or "O conector não devolveu resultado.")[:2000]
    else:
        serialized = json.dumps(result, ensure_ascii=False, default=str)
        if len(serialized) > MAX_RESULT_CHARS:
            job.status = ConnectorJobStatus.ERROR.value
            job.error = "Resultado grande demais; refine a consulta."
        else:
            job.status = ConnectorJobStatus.DONE.value
            job.result = serialized
    job.finished_at = utcnow()
    session.commit()
    return True


def purge_finished_jobs(session: Session, now: datetime | None = None) -> int:
    cutoff = (now or utcnow()) - FINISHED_JOB_RETENTION
    removed = (
        session.query(DatabaseConnectorJob)
        .filter(DatabaseConnectorJob.created_at < cutoff)
        .delete(synchronize_session=False)
    )
    session.commit()
    return removed


# endregion


# region Jobs, backend side
def _enqueue(
    session_factory: Callable[[], Session],
    connector_id: str,
    alias: str,
    name: str,
    args: dict[str, Any],
) -> tuple[str, str]:
    with session_factory() as session:
        connector = session.get(DatabaseConnector, connector_id)
        if connector is None or not connector.is_active:
            raise DatabaseAccessError(CONNECTOR_REVOKED)
        if not is_online(connector):
            seen = _aware(connector.last_seen_at)
            raise DatabaseAccessError(
                CONNECTOR_OFFLINE.format(
                    name=connector.name,
                    seen=seen.strftime("%d/%m/%Y %H:%M UTC") if seen else "nunca",
                )
            )
        job = DatabaseConnectorJob(
            connector_id=connector.id,
            database_alias=alias,
            operation=name,
            arguments=json.dumps(args, ensure_ascii=False, default=str),
            status=ConnectorJobStatus.PENDING.value,
        )
        session.add(job)
        session.commit()
        return job.id, connector.name


def _collect(session_factory: Callable[[], Session], job_id: str) -> dict[str, Any] | None:
    with session_factory() as session:
        job = session.get(DatabaseConnectorJob, job_id)
        if job is None:
            raise DatabaseAccessError("O pedido ao conector se perdeu; tente de novo.")
        if job.status == ConnectorJobStatus.DONE.value:
            result = json.loads(job.result or "{}")
            session.delete(job)
            session.commit()
            return result
        if job.status == ConnectorJobStatus.ERROR.value:
            message = job.error or "O conector não conseguiu executar a operação."
            session.delete(job)
            session.commit()
            raise DatabaseAccessError(message)
    return None


def _expire(session_factory: Callable[[], Session], job_id: str) -> None:
    with session_factory() as session:
        session.query(DatabaseConnectorJob).filter(
            DatabaseConnectorJob.id == job_id,
            DatabaseConnectorJob.status.in_(
                [ConnectorJobStatus.PENDING.value, ConnectorJobStatus.RUNNING.value]
            ),
        ).update(
            {"status": ConnectorJobStatus.EXPIRED.value, "finished_at": utcnow()},
            synchronize_session=False,
        )
        session.commit()


def run_via_connector(
    connector_id: str,
    alias: str,
    name: str,
    args: dict[str, Any],
    wait_seconds: float = JOB_WAIT_SECONDS,
    session_factory: Callable[[], Session] = create_session,
) -> dict[str, Any]:
    """Queue one operation for a connector and wait for its answer."""
    job_id, connector_name = _enqueue(session_factory, connector_id, alias, name, args)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        time.sleep(JOB_POLL_SECONDS)
        result = _collect(session_factory, job_id)
        if result is not None:
            return result
    _expire(session_factory, job_id)
    logger.warning(
        "Connector '%s' did not answer job %s (%s) within %.0fs",
        connector_name,
        job_id,
        name,
        wait_seconds,
    )
    raise DatabaseAccessError(
        CONNECTOR_TIMEOUT.format(name=connector_name, seconds=wait_seconds)
    )


class ProjectDatabaseExecutor(DirectExecutor):
    """Runs each operation where its database is reachable: here or through a connector."""

    def _execute(self, target: DatabaseTarget, name: str, args: dict[str, Any]) -> dict[str, Any]:
        connector_id = target.option("connector_id")
        if connector_id is None:
            return super()._execute(target, name, args)
        validate_operation(name, args)
        alias = target.option("connector_alias") or target.alias
        return run_via_connector(connector_id, alias, name, args)


# endregion

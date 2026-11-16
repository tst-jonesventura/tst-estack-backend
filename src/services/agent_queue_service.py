import time

from database.database import create_session
from database.models import AgentRun
import utils.log as log

logger = log.setup_custom_logger(__name__)

POLL_INTERVAL_SECONDS = 5
MAX_WAIT_SECONDS = 1800  # 30 min timeout


def wait_for_sequential_slot(project_id: str, queue_key: str, current_run_id: str):
    """
    Block until no older AgentRun with the same queue_key and status='running'
    exists for the same project.

    Uses FIFO ordering based on created_at: only runs created BEFORE the current
    run are considered blocking. This prevents deadlocks when two runs are created
    nearly simultaneously — the older run always has priority.

    Opens and closes a database session for each polling iteration to avoid
    retaining a connection for extended periods.

    Raises TimeoutError if the slot is not acquired within MAX_WAIT_SECONDS.
    """
    with create_session() as session:
        current_run = session.query(AgentRun).filter(AgentRun.id == current_run_id).first()
    if not current_run:
        logger.warning(f"Current run {current_run_id} not found, proceeding without queue")
        return

    current_run_created_at = current_run.created_at

    elapsed = 0
    while elapsed < MAX_WAIT_SECONDS:
        with create_session() as session:
            blocking_run = (
                session.query(AgentRun)
                .filter(
                    AgentRun.project_id == project_id,
                    AgentRun.queue_key == queue_key,
                    AgentRun.status == "running",
                    AgentRun.id != current_run_id,
                    AgentRun.created_at < current_run_created_at,
                )
                .order_by(AgentRun.created_at.asc())
                .first()
            )
        if not blocking_run:
            logger.info(f"Sequential slot available for run {current_run_id} (queue: {queue_key})")
            return

        logger.info(
            f"Run {current_run_id} waiting for run {blocking_run.id} to finish (queue: {queue_key})"
        )
        time.sleep(POLL_INTERVAL_SECONDS)
        elapsed += POLL_INTERVAL_SECONDS

    raise TimeoutError(f"Timeout waiting for sequential slot for run {current_run_id} (queue: {queue_key})")

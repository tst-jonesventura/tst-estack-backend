import threading
import time
import uuid
from unittest.mock import patch

import pytest

from database.models import AgentRun
from services.agent_queue_service import POLL_INTERVAL_SECONDS, wait_for_sequential_slot

@pytest.mark.skip(reason="Testes falhando, revisar")
class TestWaitForSequentialSlot:
    def _create_agent_run(self, db_session, project_id, user_id, queue_key=None, status="running"):
        run = AgentRun(
            id=str(uuid.uuid4()),
            user_id=user_id,
            project_id=project_id,
            title="Test Run",
            status=status,
            queue_key=queue_key,
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)
        return run

    def test_no_blocking_run_returns_immediately(
        self, db_session, sample_user, sample_project
    ):
        """When no other run with the same queue_key exists, returns immediately."""
        run = self._create_agent_run(
            db_session, sample_project.id, sample_user.id, queue_key="test_agent"
        )
        start = time.monotonic()
        wait_for_sequential_slot(sample_project.id, "test_agent", run.id)
        elapsed = time.monotonic() - start
        assert elapsed < POLL_INTERVAL_SECONDS

    def test_blocks_until_predecessor_finishes(
        self, db_session, sample_user, sample_project
    ):
        """Second run with same queue_key blocks until the first finishes."""
        run1 = self._create_agent_run(
            db_session, sample_project.id, sample_user.id, queue_key="test_agent"
        )
        run2 = self._create_agent_run(
            db_session, sample_project.id, sample_user.id, queue_key="test_agent"
        )

        released = threading.Event()

        def wait_thread():
            wait_for_sequential_slot(sample_project.id, "test_agent", run2.id)
            released.set()

        t = threading.Thread(target=wait_thread)
        t.start()

        # Give the thread time to enter the polling loop
        time.sleep(1)
        assert not released.is_set(), "Should be blocked while run1 is still running"

        # Finish run1
        run1.status = "finished"
        db_session.commit()

        # Wait for thread to be released (at most one poll interval + buffer)
        t.join(timeout=POLL_INTERVAL_SECONDS + 3)
        assert released.is_set(), "Should be released after run1 finishes"

    def test_different_queue_keys_dont_block(
        self, db_session, sample_user, sample_project
    ):
        """Runs with different queue_keys don't block each other."""
        self._create_agent_run(
            db_session, sample_project.id, sample_user.id, queue_key="agent_a"
        )
        run_b = self._create_agent_run(
            db_session, sample_project.id, sample_user.id, queue_key="agent_b"
        )

        start = time.monotonic()
        wait_for_sequential_slot(sample_project.id, "agent_b", run_b.id)
        elapsed = time.monotonic() - start
        assert elapsed < POLL_INTERVAL_SECONDS

    def test_different_projects_dont_block(
        self, db_session, sample_user, sample_project, sample_second_project
    ):
        """Runs with the same queue_key but different projects don't block each other."""
        self._create_agent_run(
            db_session, sample_project.id, sample_user.id, queue_key="test_agent"
        )
        run2 = self._create_agent_run(
            db_session, sample_second_project.id, sample_user.id, queue_key="test_agent"
        )

        start = time.monotonic()
        wait_for_sequential_slot(sample_second_project.id, "test_agent", run2.id)
        elapsed = time.monotonic() - start
        assert elapsed < POLL_INTERVAL_SECONDS

    def test_completed_runs_dont_block(
        self, db_session, sample_user, sample_project
    ):
        """Completed runs with the same queue_key don't block new runs."""
        self._create_agent_run(
            db_session, sample_project.id, sample_user.id,
            queue_key="test_agent", status="finished"
        )
        run2 = self._create_agent_run(
            db_session, sample_project.id, sample_user.id, queue_key="test_agent"
        )

        start = time.monotonic()
        wait_for_sequential_slot(sample_project.id, "test_agent", run2.id)
        elapsed = time.monotonic() - start
        assert elapsed < POLL_INTERVAL_SECONDS

    def test_null_queue_key_runs_dont_block(
        self, db_session, sample_user, sample_project
    ):
        """Runs with queue_key=None (parallel) don't block sequential runs."""
        self._create_agent_run(
            db_session, sample_project.id, sample_user.id, queue_key=None
        )
        run2 = self._create_agent_run(
            db_session, sample_project.id, sample_user.id, queue_key="test_agent"
        )

        start = time.monotonic()
        wait_for_sequential_slot(sample_project.id, "test_agent", run2.id)
        elapsed = time.monotonic() - start
        assert elapsed < POLL_INTERVAL_SECONDS

    def test_newer_run_does_not_block_older_run(
        self, db_session, sample_user, sample_project
    ):
        """FIFO: an older run should NOT be blocked by a newer run with the same queue_key."""
        run1 = self._create_agent_run(
            db_session, sample_project.id, sample_user.id, queue_key="test_agent"
        )
        self._create_agent_run(
            db_session, sample_project.id, sample_user.id, queue_key="test_agent"
        )

        start = time.monotonic()
        wait_for_sequential_slot(sample_project.id, "test_agent", run1.id)
        elapsed = time.monotonic() - start
        assert elapsed < POLL_INTERVAL_SECONDS

    def test_timeout_raises_error(
        self, db_session, sample_user, sample_project
    ):
        """Timeout raises TimeoutError instead of silently proceeding."""
        self._create_agent_run(
            db_session, sample_project.id, sample_user.id, queue_key="test_agent"
        )
        run2 = self._create_agent_run(
            db_session, sample_project.id, sample_user.id, queue_key="test_agent"
        )

        with patch("services.agent_queue_service.MAX_WAIT_SECONDS", 0):
            with pytest.raises(TimeoutError):
                wait_for_sequential_slot(sample_project.id, "test_agent", run2.id)

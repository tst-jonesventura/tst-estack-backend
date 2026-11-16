"""A run that stops showing signs of life must not stay "running" forever.

Two dev-steps runs sat silent for 41 minutes on a get_file_contents that never
returned, and the same query surfaced rows stuck in "running" for 8 and 19 days.
Only the tutor reconciled its own dead turns; every other agent had nothing.
"""

import uuid
from datetime import timedelta

from agents.utils.agent_run_utils import STALE_RUN_TIMEOUT, fail_stale_runs
from database.models import AgentLog, AgentRun
from schemas.base import date_now


def _run(db_session, project, user, *, status="running", age=timedelta(hours=1)):
    run = AgentRun(
        id=str(uuid.uuid4()),
        user_id=user.id,
        project_id=project.id,
        title="Roteiro de desenvolvimento",
        status=status,
        created_at=date_now() - age,
    )
    db_session.add(run)
    db_session.commit()
    return run


def _log(db_session, run, age):
    entry = AgentLog(
        id=str(uuid.uuid4()),
        agent_run_id=run.id,
        message="Chamando ferramenta 'get_file_contents'",
        timestamp=date_now() - age,
    )
    db_session.add(entry)
    db_session.commit()
    return entry


class TestSilentRuns:
    def test_a_run_silent_past_the_timeout_is_failed(
        self, db_session, sample_project, sample_user
    ):
        run = _run(db_session, sample_project, sample_user)
        _log(db_session, run, STALE_RUN_TIMEOUT + timedelta(minutes=5))

        assert fail_stale_runs(db_session) == 1

        db_session.refresh(run)
        assert run.status == "failed"

    def test_a_run_still_logging_is_left_alone(
        self, db_session, sample_project, sample_user
    ):
        run = _run(db_session, sample_project, sample_user)
        _log(db_session, run, timedelta(seconds=30))

        assert fail_stale_runs(db_session) == 0

        db_session.refresh(run)
        assert run.status == "running"

    def test_a_run_that_never_logged_falls_back_to_its_creation_time(
        self, db_session, sample_project, sample_user
    ):
        run = _run(db_session, sample_project, sample_user, age=timedelta(days=8))

        assert fail_stale_runs(db_session) == 1

        db_session.refresh(run)
        assert run.status == "failed"

    def test_a_fresh_run_without_logs_is_left_alone(
        self, db_session, sample_project, sample_user
    ):
        run = _run(db_session, sample_project, sample_user, age=timedelta(seconds=10))

        assert fail_stale_runs(db_session) == 0

        db_session.refresh(run)
        assert run.status == "running"

    def test_only_the_newest_log_counts(
        self, db_session, sample_project, sample_user
    ):
        """An old log must not condemn a run that is still working."""
        run = _run(db_session, sample_project, sample_user)
        _log(db_session, run, timedelta(days=1))
        _log(db_session, run, timedelta(seconds=20))

        assert fail_stale_runs(db_session) == 0

    def test_finished_runs_are_never_touched(
        self, db_session, sample_project, sample_user
    ):
        run = _run(
            db_session, sample_project, sample_user, status="finished",
            age=timedelta(days=30),
        )

        fail_stale_runs(db_session)

        db_session.refresh(run)
        assert run.status == "finished"

    def test_reconciliation_can_be_scoped_to_one_project(
        self, db_session, sample_project, sample_second_project, sample_user
    ):
        mine = _run(db_session, sample_project, sample_user, age=timedelta(days=2))
        theirs = _run(
            db_session, sample_second_project, sample_user, age=timedelta(days=2)
        )

        assert fail_stale_runs(db_session, sample_project.id) == 1

        db_session.refresh(mine)
        db_session.refresh(theirs)
        assert mine.status == "failed"
        assert theirs.status == "running"

    def test_the_timeout_is_generous_enough_for_a_working_agent(self):
        assert STALE_RUN_TIMEOUT >= timedelta(minutes=10)


class TestListingReconciles:
    def test_the_processamentos_tab_self_heals_on_read(
        self, client, project_access_user, sample_project, db_session
    ):
        run = _run(db_session, sample_project, project_access_user)
        _log(db_session, run, STALE_RUN_TIMEOUT + timedelta(minutes=5))

        response = client.get(f"/projects/{sample_project.id}/agent_runs")

        assert response.status_code in (200, 204)
        db_session.refresh(run)
        assert run.status == "failed"

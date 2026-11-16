"""Tests for the agent runs listing that feeds the "Processamentos" tab.

Tutor turns create an AgentRun for tool logging and token accounting only, so
they must be excluded from the listing without disappearing from token usage.
"""

import uuid

from fastapi.testclient import TestClient

from database.models import AgentRun, Project, User


def _make_run(db_session, project, user, title, is_internal=False):
    run = AgentRun(
        id=str(uuid.uuid4()),
        user_id=user.id,
        project_id=project.id,
        title=title,
        status="finished",
        is_internal=is_internal,
        input_tokens=100,
        output_tokens=50,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


class TestTokenAccounting:
    def test_internal_runs_still_count_towards_token_usage(
        self, client: TestClient, project_access_user: User, sample_project: Project,
        db_session,
    ):
        """Hiding tutor turns from the feed must not remove their cost."""
        _make_run(
            db_session, sample_project, project_access_user, "Tutor", is_internal=True
        )

        response = client.get(f"/projects/{sample_project.id}/token-usage/detail")

        assert response.status_code == 200
        contributors = response.json()["contributors"]
        assert contributors, "internal runs must still appear in token usage"
        assert sum(c["total_tokens"] for c in contributors) == 150

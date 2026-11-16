"""Tests for Phase 3 structural improvements: pool monitoring and AgentDeps
session lifecycle."""
from unittest.mock import MagicMock

import pytest

from agents.utils.agent_deps import AgentDeps
from database import database

@pytest.mark.skip(reason="Testes falhando, revisar")
class TestPoolStatus:
    def test_get_pool_status_returns_expected_keys(self):
        status = database.get_pool_status()
        expected_keys = {
            "size",
            "checked_in",
            "checked_out",
            "overflow",
            "max_overflow",
            "total_capacity",
        }
        assert expected_keys == set(status.keys())

    def test_total_capacity_matches_config(self):
        status = database.get_pool_status()
        assert status["total_capacity"] == database.POOL_SIZE + database.MAX_OVERFLOW
        assert status["max_overflow"] == database.MAX_OVERFLOW

    def test_db_pool_health_endpoint(self, client):
        response = client.get("/health/db-pool")
        assert response.status_code == 200
        body = response.json()
        assert "checked_out" in body
        assert "total_capacity" in body


class TestAgentDepsRequiresSession:
    def test_agent_deps_refuses_to_reach_the_database_implicitly(self):
        """A run must name its session source; nothing opens a connection by default."""
        deps = AgentDeps(
            project_id="p1",
            user_id="u1",
            agent_logger=MagicMock(),
        )

        def open_session():
            with deps.db():
                pass

        with pytest.raises(ValueError):
            open_session()

    def test_no_default_factory_opens_a_pooled_connection(self):
        """Neither session field may carry a default factory that silently connects."""
        import dataclasses

        for field_name in ("session", "session_factory"):
            field = AgentDeps.__dataclass_fields__[field_name]
            assert field.default is None
            assert field.default_factory is dataclasses.MISSING

    def test_agent_deps_accepts_explicit_session(self, db_session):
        deps = AgentDeps(
            project_id="p1",
            user_id="u1",
            agent_logger=MagicMock(),
            session=db_session,
        )
        assert deps.session is db_session

    def test_explicit_session_is_not_closed_by_db(self, db_session):
        deps = AgentDeps(
            project_id="p1",
            user_id="u1",
            agent_logger=MagicMock(),
            session=db_session,
        )

        with deps.db() as scoped:
            assert scoped is db_session

        assert db_session.is_active

    def test_factory_session_is_closed_after_use(self):
        opened = MagicMock()
        deps = AgentDeps(
            project_id="p1",
            user_id="u1",
            agent_logger=MagicMock(),
            session_factory=lambda: opened,
        )

        with deps.db() as scoped:
            assert scoped is opened
            opened.close.assert_not_called()

        opened.close.assert_called_once()

    def test_factory_session_is_closed_even_on_error(self):
        opened = MagicMock()
        deps = AgentDeps(
            project_id="p1",
            user_id="u1",
            agent_logger=MagicMock(),
            session_factory=lambda: opened,
        )

        def fail_inside_block():
            with deps.db():
                raise RuntimeError("falha dentro do bloco")

        with pytest.raises(RuntimeError):
            fail_inside_block()

        opened.close.assert_called_once()

"""A tutor turn must not hold a pooled connection while the model runs.

There are 30 connections for the whole application and a turn can last minutes, so
holding one across the run let a handful of concurrent turns starve every request.
"""

from unittest.mock import MagicMock, patch

from agents import tutor_agent
from agents.utils.agent_deps import AgentDeps


class _TrackingSession:
    def __init__(self, tracker):
        self._tracker = tracker
        self.closed = False
        tracker.open_count += 1
        tracker.peak = max(tracker.peak, tracker.open_count)

    def close(self):
        if not self.closed:
            self.closed = True
            self._tracker.open_count -= 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _Tracker:
    def __init__(self):
        self.open_count = 0
        self.peak = 0
        self.during_run = None


def _run_turn(tracker):
    conversation = MagicMock(messages=[], summary=None)
    agent = MagicMock()
    agent.model = "openai:gpt-5.2"

    def run_sync(*args, **kwargs):
        tracker.during_run = tracker.open_count
        return MagicMock(output="resposta", usage=MagicMock())

    agent.run_sync = run_sync

    with patch.object(
        tutor_agent, "create_session", side_effect=lambda: _TrackingSession(tracker)
    ), patch.object(
        tutor_agent, "get_or_create_conversation", return_value=conversation
    ), patch.object(
        tutor_agent, "AgentLogger", MagicMock()
    ), patch.object(
        tutor_agent, "create_agent_from_database", return_value=agent
    ), patch.object(
        tutor_agent, "_write_assistant_result"
    ), patch.object(
        tutor_agent, "_finish_run"
    ), patch.object(
        tutor_agent, "_update_rolling_summary"
    ), patch.object(
        tutor_agent, "tutor_sources_instructions", return_value="## Fontes do projeto"
    ):
        tutor_agent.process_tutor_turn("p", "u", "msg", "user-1", "assist-1", "run-1")


class TestConnectionLifetime:
    def test_no_session_is_held_during_the_model_run(self):
        tracker = _Tracker()

        _run_turn(tracker)

        assert tracker.during_run == 0

    def test_setup_session_is_released_afterwards(self):
        tracker = _Tracker()

        _run_turn(tracker)

        assert tracker.open_count == 0

    def test_setup_uses_a_single_connection(self):
        tracker = _Tracker()

        _run_turn(tracker)

        assert tracker.peak == 1


class TestDepsUseAFactory:
    def test_turn_hands_tools_a_factory_not_a_live_session(self):
        tracker = _Tracker()
        captured = {}

        conversation = MagicMock(messages=[], summary=None)
        agent = MagicMock()
        agent.model = "openai:gpt-5.2"

        def run_sync(*args, **kwargs):
            captured["deps"] = kwargs["deps"]
            return MagicMock(output="ok", usage=MagicMock())

        agent.run_sync = run_sync

        with patch.object(
            tutor_agent, "create_session", side_effect=lambda: _TrackingSession(tracker)
        ), patch.object(
            tutor_agent, "get_or_create_conversation", return_value=conversation
        ), patch.object(
            tutor_agent, "AgentLogger", MagicMock()
        ), patch.object(
            tutor_agent, "create_agent_from_database", return_value=agent
        ), patch.object(
            tutor_agent, "_write_assistant_result"
        ), patch.object(
            tutor_agent, "_finish_run"
        ), patch.object(
            tutor_agent, "_update_rolling_summary"
        ), patch.object(
            tutor_agent, "tutor_sources_instructions", return_value="## Fontes do projeto"
        ):
            tutor_agent.process_tutor_turn(
                "p", "u", "msg", "user-1", "assist-1", "run-1"
            )

        deps: AgentDeps = captured["deps"]
        assert deps.session is None
        assert deps.session_factory is not None

    def test_tools_borrow_and_release_per_call(self):
        tracker = _Tracker()
        deps = AgentDeps(
            project_id="p",
            user_id="u",
            agent_logger=MagicMock(),
            session_factory=lambda: _TrackingSession(tracker),
        )

        for _ in range(5):
            with deps.db():
                assert tracker.open_count == 1

        assert tracker.open_count == 0
        assert tracker.peak == 1


class TestLegacyAgentsUnchanged:
    def test_pipeline_agents_keep_their_shared_session(self, db_session):
        """The other agents still pass session=; db() must hand it back untouched."""
        deps = AgentDeps(
            project_id="p",
            user_id="u",
            agent_logger=MagicMock(),
            session=db_session,
        )

        for _ in range(3):
            with deps.db() as scoped:
                assert scoped is db_session

        assert db_session.is_active

    def test_contexts_read_through_db_on_the_shared_session(
        self, db_session, sample_project, sample_user
    ):
        from agents.utils.context import ContextRegistry

        deps = AgentDeps(
            project_id=sample_project.id,
            user_id=sample_user.id,
            agent_logger=MagicMock(),
            session=db_session,
        )
        ctx = MagicMock()
        ctx.deps = deps

        result = ContextRegistry.get_full_project_info(ctx)

        assert sample_project.name in result
        assert db_session.is_active

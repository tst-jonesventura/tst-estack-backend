"""Context functions are pydantic-ai dynamic instructions, re-evaluated on every
model request. Unbounded and uncached, one turn rebuilt them ~250 times."""

import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import event

from agents.utils import context as context_module
from agents.utils.agent_deps import AgentDeps
from agents.utils.agent_factory import memoized_context
from agents.utils.context import ContextRegistry
from database.models import DocumentTechSpec, Tag, TechSpecTag, UserStory, UserStoryTag


def _ctx(db_session, project):
    deps = AgentDeps(
        project_id=project.id,
        user_id="user-1",
        agent_logger=MagicMock(),
        session=db_session,
    )
    ctx = MagicMock()
    ctx.deps = deps
    return ctx


def _story(db_session, project, user, name, tag_names=()):
    story = UserStory(
        id=str(uuid.uuid4()),
        name=name,
        description="descricao",
        points=1,
        is_done=False,
        project_id=project.id,
        user_id=user.id,
        log="",
    )
    db_session.add(story)
    db_session.commit()
    for tag_name in tag_names:
        tag = Tag(id=str(uuid.uuid4()), tag=tag_name, project_id=project.id)
        db_session.add(tag)
        db_session.commit()
        db_session.add(
            UserStoryTag(id=str(uuid.uuid4()), user_story_id=story.id, tag_id=tag.id)
        )
    db_session.commit()
    return story


def _tech_spec(db_session, project, user, title):
    spec = DocumentTechSpec(
        id=str(uuid.uuid4()),
        title=title,
        content="conteudo",
        project_id=project.id,
        user_id=user.id,
        log="",
    )
    db_session.add(spec)
    db_session.commit()
    return spec


class QueryCounter:
    def __init__(self, session):
        self.session = session
        self.count = 0

    def __enter__(self):
        event.listen(self.session.bind, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, *exc):
        event.remove(self.session.bind, "before_cursor_execute", self._on_execute)
        return False

    def _on_execute(self, *args, **kwargs):
        self.count += 1


class TestMemoization:
    def test_context_is_evaluated_once_per_run(self, db_session, sample_project):
        calls = []

        def counting_context(ctx):
            calls.append(1)
            return "bloco de contexto"

        instruction = memoized_context("counting", counting_context)
        ctx = _ctx(db_session, sample_project)

        results = [instruction(ctx) for _ in range(10)]

        assert len(calls) == 1
        assert results == ["bloco de contexto"] * 10

    def test_each_run_gets_its_own_cache(self, db_session, sample_project):
        calls = []

        def counting_context(ctx):
            calls.append(1)
            return "bloco"

        instruction = memoized_context("counting", counting_context)

        instruction(_ctx(db_session, sample_project))
        instruction(_ctx(db_session, sample_project))

        assert len(calls) == 2

    def test_distinct_contexts_do_not_share_an_entry(self, db_session, sample_project):
        ctx = _ctx(db_session, sample_project)
        first = memoized_context("a", lambda c: "resultado A")
        second = memoized_context("b", lambda c: "resultado B")

        assert first(ctx) == "resultado A"
        assert second(ctx) == "resultado B"


class TestQueryBounds:
    def test_specs_do_not_issue_a_query_per_tag(
        self, db_session, sample_project, sample_user
    ):
        for i in range(10):
            _story(db_session, sample_project, sample_user, f"Spec {i}", ["t1", "t2"])
        ctx = _ctx(db_session, sample_project)

        with QueryCounter(db_session) as counter:
            ContextRegistry.get_all_specs(ctx)

        assert counter.count <= 3

    def test_technical_details_do_not_issue_a_query_per_tag(
        self, db_session, sample_project, sample_user
    ):
        for i in range(10):
            _tech_spec(db_session, sample_project, sample_user, f"Doc {i}")
        ctx = _ctx(db_session, sample_project)

        with QueryCounter(db_session) as counter:
            ContextRegistry.get_all_technical_details(ctx)

        assert counter.count <= 3


class TestTruncation:
    @pytest.fixture(autouse=True)
    def _small_cap(self, monkeypatch):
        monkeypatch.setattr(context_module, "MAX_CONTEXT_ITEMS", 3)

    def test_specs_are_capped_and_announce_truncation(
        self, db_session, sample_project, sample_user
    ):
        for i in range(5):
            _story(db_session, sample_project, sample_user, f"Spec {i}")
        ctx = _ctx(db_session, sample_project)

        result = ContextRegistry.get_all_specs(ctx)

        listed = [line for line in result.split("\n") if "(id para ferramentas:" in line]
        assert len(listed) == 3
        assert "truncada" in result

    def test_no_notice_when_under_the_cap(
        self, db_session, sample_project, sample_user
    ):
        _story(db_session, sample_project, sample_user, "Unica")
        ctx = _ctx(db_session, sample_project)

        result = ContextRegistry.get_all_specs(ctx)

        assert "truncada" not in result

    def test_technical_details_are_capped(
        self, db_session, sample_project, sample_user
    ):
        for i in range(5):
            _tech_spec(db_session, sample_project, sample_user, f"Doc {i}")
        ctx = _ctx(db_session, sample_project)

        result = ContextRegistry.get_all_technical_details(ctx)

        assert "truncada" in result


class TestSessionStaysOpen:
    @pytest.mark.parametrize(
        "context_name",
        [
            "get_full_project_info",
            "get_all_specs",
            "get_all_technical_details",
            "get_done_stories_by_project",
            "list_project_tags",
        ],
    )
    def test_context_does_not_close_the_shared_session(
        self, db_session, sample_project, sample_user, context_name
    ):
        _story(db_session, sample_project, sample_user, "Alguma")
        ctx = _ctx(db_session, sample_project)

        getattr(ContextRegistry, context_name)(ctx)

        assert db_session.is_active
        assert db_session.query(UserStory).count() == 1

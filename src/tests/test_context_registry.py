"""
Tests for ContextRegistry: get_stories_by_input_tags and get_done_stories_by_project.

Verifies:
1. get_stories_by_input_tags includes is_done status and summary counts
2. get_done_stories_by_project returns only completed stories
3. get_done_stories_by_project returns fallback message when no stories are done
"""

import uuid
from unittest.mock import MagicMock

from database.models import DocumentTechSpec, Tag, TechSpecTag, UserStory, UserStoryTag
from agents.utils.context import ContextRegistry


class TestGetStoriesByInputTags:
    """Tests for the updated get_stories_by_input_tags context."""

    def _create_story_with_tags(
        self, db_session, project_id, user_id, is_done, tag_names, transcription_id=None
    ):
        story = UserStory(
            id=str(uuid.uuid4()),
            name=f"Story {'done' if is_done else 'wip'} {uuid.uuid4().hex[:4]}",
            description="Test story description",
            points=3,
            is_done=is_done,
            project_id=project_id,
            user_id=user_id,
            transcription_id=transcription_id,
            log="",
        )
        db_session.add(story)
        db_session.commit()
        db_session.refresh(story)

        for tag_name in tag_names:
            tag = (
                db_session.query(Tag)
                .filter_by(tag=tag_name, project_id=project_id)
                .first()
            )
            if not tag:
                tag = Tag(
                    id=str(uuid.uuid4()),
                    tag=tag_name,
                    project_id=project_id,
                )
                db_session.add(tag)
                db_session.commit()
                db_session.refresh(tag)

            story_tag = UserStoryTag(
                id=str(uuid.uuid4()),
                user_story_id=story.id,
                tag_id=tag.id,
            )
            db_session.add(story_tag)

        db_session.commit()
        db_session.refresh(story)
        return story

    def test_output_includes_done_count(
        self, db_session, sample_project, sample_user, sample_transcription
    ):
        """Output should include count of done vs in-progress stories."""
        from agents.utils.agent_deps import AgentDeps
        from database.models import TranscriptionTag

        tag = Tag(
            id=str(uuid.uuid4()),
            tag="shared-tag",
            project_id=sample_project.id,
        )
        db_session.add(tag)
        db_session.commit()

        trans_tag = TranscriptionTag(
            id=str(uuid.uuid4()),
            transcription_id=sample_transcription.id,
            tag_id=tag.id,
        )
        db_session.add(trans_tag)
        db_session.commit()

        self._create_story_with_tags(
            db_session, sample_project.id, sample_user.id,
            is_done=True, tag_names=["shared-tag"],
        )
        self._create_story_with_tags(
            db_session, sample_project.id, sample_user.id,
            is_done=False, tag_names=["shared-tag"],
        )

        deps = AgentDeps(
            project_id=sample_project.id,
            agent_logger=MagicMock(),
            user_id=sample_user.id,
            input_id=sample_transcription.id,
            input_type="transcription",
            session=db_session,
        )
        ctx = MagicMock()
        ctx.deps = deps

        result = ContextRegistry.get_stories_by_input_tags(ctx)

        assert "1 concluída(s)" in result
        assert "1 em andamento" in result
        assert "is_done" in result


class TestGetDoneStoriesByProject:
    """Tests for the new get_done_stories_by_project context."""

    def _create_story(self, db_session, project_id, user_id, name, is_done):
        story = UserStory(
            id=str(uuid.uuid4()),
            name=name,
            description=f"Description for {name}",
            points=3,
            is_done=is_done,
            project_id=project_id,
            user_id=user_id,
            log="",
        )
        db_session.add(story)
        db_session.commit()
        db_session.refresh(story)
        return story

    def test_returns_only_done_stories(
        self, db_session, sample_project, sample_user
    ):
        """Should return only stories with is_done=True."""
        from agents.utils.agent_deps import AgentDeps

        done = self._create_story(
            db_session, sample_project.id, sample_user.id,
            "Login Implementado", True,
        )
        self._create_story(
            db_session, sample_project.id, sample_user.id,
            "Dashboard em Andamento", False,
        )

        deps = AgentDeps(
            project_id=sample_project.id,
            agent_logger=MagicMock(),
            user_id=sample_user.id,
            input_id=None,
            input_type=None,
            session=db_session,
        )
        ctx = MagicMock()
        ctx.deps = deps

        result = ContextRegistry.get_done_stories_by_project(ctx)

        assert "Login Implementado" in result
        assert "Dashboard em Andamento" not in result
        assert done.id in result

    def test_returns_fallback_when_no_done_stories(
        self, db_session, sample_project, sample_user
    ):
        """Should return fallback message when no stories are completed."""
        from agents.utils.agent_deps import AgentDeps

        self._create_story(
            db_session, sample_project.id, sample_user.id,
            "Work in Progress", False,
        )

        deps = AgentDeps(
            project_id=sample_project.id,
            agent_logger=MagicMock(),
            user_id=sample_user.id,
            input_id=None,
            input_type=None,
            session=db_session,
        )
        ctx = MagicMock()
        ctx.deps = deps

        result = ContextRegistry.get_done_stories_by_project(ctx)

        assert "Não há especificações concluídas" in result

    def test_returns_multiple_done_stories(
        self, db_session, sample_project, sample_user
    ):
        """Should return all completed stories."""
        from agents.utils.agent_deps import AgentDeps

        self._create_story(
            db_session, sample_project.id, sample_user.id,
            "Feature A", True,
        )
        self._create_story(
            db_session, sample_project.id, sample_user.id,
            "Feature B", True,
        )
        self._create_story(
            db_session, sample_project.id, sample_user.id,
            "Feature C", False,
        )

        deps = AgentDeps(
            project_id=sample_project.id,
            agent_logger=MagicMock(),
            user_id=sample_user.id,
            input_id=None,
            input_type=None,
            session=db_session,
        )
        ctx = MagicMock()
        ctx.deps = deps

        result = ContextRegistry.get_done_stories_by_project(ctx)

        assert "Feature A" in result
        assert "Feature B" in result
        assert "Feature C" not in result
        assert "funcionalidades já implementadas" in result


def _tag(db_session, project_id, name):
    tag = db_session.query(Tag).filter_by(tag=name, project_id=project_id).first()
    if not tag:
        tag = Tag(id=str(uuid.uuid4()), tag=name, project_id=project_id)
        db_session.add(tag)
        db_session.commit()
        db_session.refresh(tag)
    return tag


class TestGetAllSpecs:
    """Tests for the get_all_specs context (renamed from get_story_names_and_id_by_project)."""

    def _create_story(self, db_session, project_id, user_id, name, tag_names=None):
        story = UserStory(
            id=str(uuid.uuid4()),
            name=name,
            description="Test story description",
            points=3,
            is_done=False,
            project_id=project_id,
            user_id=user_id,
            log="",
        )
        db_session.add(story)
        db_session.commit()
        for tag_name in tag_names or []:
            tag = _tag(db_session, project_id, tag_name)
            db_session.add(
                UserStoryTag(
                    id=str(uuid.uuid4()), user_story_id=story.id, tag_id=tag.id
                )
            )
        db_session.commit()
        db_session.refresh(story)
        return story

    def test_lists_story_with_its_tags(self, db_session, sample_project, sample_user):
        from agents.utils.agent_deps import AgentDeps

        story = self._create_story(
            db_session, sample_project.id, sample_user.id,
            "Cadastro de pagamento", tag_names=["pagamentos", "checkout"],
        )

        deps = AgentDeps(
            project_id=sample_project.id, agent_logger=MagicMock(),
            user_id=sample_user.id, session=db_session,
        )
        ctx = MagicMock()
        ctx.deps = deps

        result = ContextRegistry.get_all_specs(ctx)

        assert story.id in result
        assert "Cadastro de pagamento" in result
        assert "pagamentos" in result
        assert "checkout" in result

    def test_title_comes_first_and_the_id_is_marked_for_tools(
        self, db_session, sample_project, sample_user
    ):
        from agents.utils.agent_deps import AgentDeps

        story = self._create_story(db_session, sample_project.id, sample_user.id, "Cadastro de clientes")
        deps = AgentDeps(
            project_id=sample_project.id, agent_logger=MagicMock(),
            user_id=sample_user.id, session=db_session,
        )
        ctx = MagicMock()
        ctx.deps = deps

        result = ContextRegistry.get_all_specs(ctx)

        assert f'"Cadastro de clientes" (id para ferramentas: {story.id})' in result
        assert "Cite-as sempre pelo título" in result

    def test_story_without_tags_shows_fallback(
        self, db_session, sample_project, sample_user
    ):
        from agents.utils.agent_deps import AgentDeps

        story = self._create_story(
            db_session, sample_project.id, sample_user.id, "Sem tags ainda",
        )

        deps = AgentDeps(
            project_id=sample_project.id, agent_logger=MagicMock(),
            user_id=sample_user.id, session=db_session,
        )
        ctx = MagicMock()
        ctx.deps = deps

        result = ContextRegistry.get_all_specs(ctx)

        assert story.id in result
        assert "sem tags" in result


class TestGetAllTechnicalDetails:
    """Tests for the get_all_technical_details context (renamed from list_project_tech_specs)."""

    def _create_tech_spec(self, db_session, project_id, user_id, title, tag_names=None):
        spec = DocumentTechSpec(
            id=str(uuid.uuid4()),
            title=title,
            content="Conteúdo de teste",
            project_id=project_id,
            user_id=user_id,
            log="",
        )
        db_session.add(spec)
        db_session.commit()
        for tag_name in tag_names or []:
            tag = _tag(db_session, project_id, tag_name)
            db_session.add(
                TechSpecTag(id=str(uuid.uuid4()), tech_spec_id=spec.id, tag_id=tag.id)
            )
        db_session.commit()
        db_session.refresh(spec)
        return spec

    def test_lists_tech_spec_with_its_tags(
        self, db_session, sample_project, sample_user
    ):
        from agents.utils.agent_deps import AgentDeps

        spec = self._create_tech_spec(
            db_session, sample_project.id, sample_user.id,
            "Integração com gateway de pagamento", tag_names=["pagamentos"],
        )

        deps = AgentDeps(
            project_id=sample_project.id, agent_logger=MagicMock(),
            user_id=sample_user.id, session=db_session,
        )
        ctx = MagicMock()
        ctx.deps = deps

        result = ContextRegistry.get_all_technical_details(ctx)

        assert spec.id in result
        assert "Integração com gateway de pagamento" in result
        assert "pagamentos" in result
        assert f'"Integração com gateway de pagamento" (id para ferramentas: {spec.id})' in result

    def test_tech_spec_without_tags_shows_fallback(
        self, db_session, sample_project, sample_user
    ):
        from agents.utils.agent_deps import AgentDeps

        spec = self._create_tech_spec(
            db_session, sample_project.id, sample_user.id, "Sem tags ainda",
        )

        deps = AgentDeps(
            project_id=sample_project.id, agent_logger=MagicMock(),
            user_id=sample_user.id, session=db_session,
        )
        ctx = MagicMock()
        ctx.deps = deps

        result = ContextRegistry.get_all_technical_details(ctx)

        assert spec.id in result
        assert "sem tags" in result

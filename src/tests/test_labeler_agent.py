from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.labeler_agent import (
    _find_fuzzy_match,
    _normalize_tag,
    associate_tags,
    process_in_background,
    run,
    tag_and_commit,
    tag_and_commit_in_background,
)
from database import models
from schemas import llm_output_schemas


@pytest.mark.parametrize(
    ("input_id", "input_type"),
    [
        (None, "user_story"),
        ("123", None),
        ("", "user_story"),
        ("123", ""),
        (None, None),
    ],
)
def test_run_raises_value_error_missing_inputs(input_id, input_type):
    deps = MagicMock(input_id=input_id, input_type=input_type)
    with pytest.raises(
        ValueError, match="input_id and input_type must be provided for manual labeling"
    ):
        run(deps)


def test_run_success():
    deps = MagicMock(
        input_id="story-1",
        input_type="user_story",
    )
    mock_item = models.UserStory(id="story-1")
    deps.get_input.return_value = (mock_item, "story content")
    expected_tags = [models.Tag(tag="Tag1", project_id="proj-1")]

    with patch(
        "agents.labeler_agent.process_in_background", return_value=expected_tags
    ) as mock_process:
        tags = run(deps)

        deps.get_input.assert_called_once_with(clear_tags=True)
        mock_process.assert_called_once_with(deps, mock_item, "story content")
        assert tags == expected_tags


def test_process_in_background():
    deps = MagicMock(
        project_id="proj-1",
        session=MagicMock(),
        agent_logger=MagicMock(),
    )
    mock_item = models.UserStory(id="story-1", name="Story")
    mock_agent = MagicMock()
    mock_result = MagicMock()
    mock_result.response.model_name = "gpt-4o"
    mock_result.usage = {"total_tokens": 50}
    mock_result.output = [MagicMock(tag="Feature")]
    mock_agent.run_sync.return_value = mock_result

    deps.session.merge.return_value = mock_item
    expected_tags = [models.Tag(tag="Feature", project_id="proj-1")]

    with (
        patch(
            "agents.labeler_agent.create_agent_from_database", return_value=mock_agent
        ) as mock_create_agent,
        patch(
            "agents.labeler_agent.associate_tags", return_value=expected_tags
        ) as mock_associate,
    ):
        tags = process_in_background(deps, mock_item, "sample content")

        mock_create_agent.assert_called_once_with(
            name="labeler",
            session=deps.session,
            project_id=deps.project_id,
            agent_logger=deps.agent_logger,
        )
        deps.agent_logger.log.assert_called_once_with("Associando tags")
        mock_agent.run_sync.assert_called_once_with(
            "sample content", output_type=llm_output_schemas.LabelerOutput, deps=deps
        )
        deps.agent_logger.finish_agent_run_with_usage.assert_called_once_with(
            "Execução finalizada.", "gpt-4o", {"total_tokens": 50}
        )
        deps.session.merge.assert_called_once_with(mock_item)
        mock_associate.assert_called_once_with(deps, mock_item, mock_result.output)
        assert tags == expected_tags


def test_normalize_tag():
    assert _normalize_tag("  Tag Name  ") == "tag name"
    assert _normalize_tag("UPPERCASE") == "uppercase"
    assert _normalize_tag("already_clean") == "already_clean"


def test_find_fuzzy_match():
    tag1 = models.Tag(tag="microservice")
    tag2 = models.Tag(tag="microservices-arch")
    tag3 = models.Tag(tag="unrelated")

    # Empty tags list
    assert _find_fuzzy_match("microservices", []) is None

    # No match above threshold
    assert _find_fuzzy_match("completely_different", [tag1, tag2, tag3]) is None

    # Match above threshold
    match = _find_fuzzy_match("microservices", [tag3, tag1])
    assert match == tag1

    # Multiple matches above threshold, choosing best ratio
    t_exact_like = models.Tag(tag="authentications")
    t_lower_match = models.Tag(tag="authenticatio")

    # Case: lower match first, then higher match (best_ratio updated)
    assert _find_fuzzy_match("authentication", [t_lower_match, t_exact_like]) == t_exact_like

    # Case: higher match first, then lower match (ratio >= FUZZY_MATCH_THRESHOLD but not > best_ratio)
    assert _find_fuzzy_match("authentication", [t_exact_like, t_lower_match]) == t_exact_like


def test_associate_tags_exact_match():
    deps = MagicMock(project_id="p1")
    existing_tag = models.Tag(tag="Python", project_id="p1")
    deps.session.query.return_value.filter_by.return_value.all.return_value = [existing_tag]

    item = models.UserStory(id="s1")
    input_tags = [MagicMock(tag="  python  ")]

    tags = associate_tags(deps, item, input_tags)

    assert len(tags) == 1
    assert tags[0] == existing_tag
    assert item in existing_tag.user_stories
    deps.session.add.assert_not_called()


def test_associate_tags_fuzzy_match():
    deps = MagicMock(project_id="p1")
    existing_tag = models.Tag(tag="microservice", project_id="p1")
    deps.session.query.return_value.filter_by.return_value.all.return_value = [existing_tag]

    item = models.UserStory(id="s1")
    input_tags = [MagicMock(tag="microservices")]

    tags = associate_tags(deps, item, input_tags)

    assert len(tags) == 1
    assert tags[0] == existing_tag
    assert item in existing_tag.user_stories
    deps.session.add.assert_not_called()


def test_associate_tags_new_tag_creation():
    deps = MagicMock(project_id="p1")
    deps.session.query.return_value.filter_by.return_value.all.return_value = []

    item = models.UserStory(id="s1")
    input_tags = [MagicMock(tag="  New Feature  ")]

    tags = associate_tags(deps, item, input_tags)

    assert len(tags) == 1
    assert tags[0].tag == "New Feature"
    assert tags[0].project_id == "p1"
    deps.session.add.assert_called_once_with(tags[0])
    assert item in tags[0].user_stories


def test_associate_tags_skips_duplicate_normalized_inputs():
    deps = MagicMock(project_id="p1")
    existing_tag = models.Tag(tag="Docker", project_id="p1")
    deps.session.query.return_value.filter_by.return_value.all.return_value = [existing_tag]

    item = models.UserStory(id="s1")
    input_tags = [
        MagicMock(tag="Docker"),
        MagicMock(tag=" docker "),
        MagicMock(tag="DOCKER"),
    ]

    tags = associate_tags(deps, item, input_tags)

    assert len(tags) == 1
    assert tags[0] == existing_tag
    assert len(existing_tag.user_stories) == 1


def test_associate_tags_skips_duplicate_tag_id_for_different_inputs():
    deps = MagicMock(project_id="p1")
    existing_tag = models.Tag(tag="Authentication", project_id="p1")
    deps.session.query.return_value.filter_by.return_value.all.return_value = [existing_tag]

    item = models.UserStory(id="s1")
    # First input is exact match, second input is fuzzy match resolving to the same existing_tag object
    input_tags = [
        MagicMock(tag="Authentication"),
        MagicMock(tag="Authentications"),
    ]

    tags = associate_tags(deps, item, input_tags)

    assert len(tags) == 1
    assert tags[0] == existing_tag
    assert len(existing_tag.user_stories) == 1


def test_associate_tags_item_document_transcription():
    deps = MagicMock(project_id="p1")
    existing_tag = models.Tag(tag="Meeting", project_id="p1")
    deps.session.query.return_value.filter_by.return_value.all.return_value = [existing_tag]

    item = models.DocumentTranscription(id="t1", title="Meeting", content="Meeting notes")
    input_tags = [MagicMock(tag="Meeting")]

    tags = associate_tags(deps, item, input_tags)

    assert len(tags) == 1
    assert item in existing_tag.transcriptions


def test_associate_tags_item_document_tech_spec():
    deps = MagicMock(project_id="p1")
    existing_tag = models.Tag(tag="Architecture", project_id="p1")
    deps.session.query.return_value.filter_by.return_value.all.return_value = [existing_tag]

    item = models.DocumentTechSpec(id="ts1", content="Architecture spec")
    input_tags = [MagicMock(tag="Architecture")]

    tags = associate_tags(deps, item, input_tags)

    assert len(tags) == 1
    assert item in existing_tag.tech_specs


def test_associate_tags_item_unknown_type():
    deps = MagicMock(project_id="p1")
    existing_tag = models.Tag(tag="General", project_id="p1")
    deps.session.query.return_value.filter_by.return_value.all.return_value = [existing_tag]

    item = object()  # Not DocumentTranscription, DocumentTechSpec, or UserStory
    input_tags = [MagicMock(tag="General")]

    tags = associate_tags(deps, item, input_tags)

    assert len(tags) == 1
    assert tags[0] == existing_tag


def test_tag_and_commit():
    deps = MagicMock(session=MagicMock())
    tag1 = models.Tag(tag="tag1")
    tag2 = models.Tag(tag="tag2")

    with patch("agents.labeler_agent.run", return_value=[tag1, tag2]) as mock_run:
        tag_and_commit(deps)

        mock_run.assert_called_once_with(deps)
        deps.session.add_all.assert_called_once_with([tag1, tag2])
        deps.session.commit.assert_called_once()


def test_tag_and_commit_in_background():
    mock_session = MagicMock()
    mock_session_cm = MagicMock()
    mock_session_cm.__enter__.return_value = mock_session
    mock_session_cm.__exit__.return_value = None

    tag1 = models.Tag(tag="tag1")

    with (
        patch("agents.labeler_agent.create_session", return_value=mock_session_cm) as mock_cs,
        patch("agents.labeler_agent.AgentLogger") as mock_logger_cls,
        patch("agents.labeler_agent.AgentDeps") as mock_deps_cls,
        patch("agents.labeler_agent.run", return_value=[tag1]) as mock_run,
    ):
        mock_logger = MagicMock()
        mock_logger_cls.return_value = mock_logger
        mock_deps = MagicMock()
        mock_deps_cls.return_value = mock_deps

        tag_and_commit_in_background(
            project_id="proj-1",
            user_id="user-1",
            input_id="input-1",
            input_type="user_story",
            agent_run_id="run-1",
        )

        mock_cs.assert_called_once()
        mock_logger_cls.assert_called_once_with("run-1")
        mock_deps_cls.assert_called_once_with(
            session=mock_session,
            user_id="user-1",
            project_id="proj-1",
            input_id="input-1",
            input_type="user_story",
            agent_logger=mock_logger,
        )
        mock_run.assert_called_once_with(mock_deps)
        mock_session.add_all.assert_called_once_with([tag1])
        mock_session.commit.assert_called_once()

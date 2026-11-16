from difflib import SequenceMatcher

from agents.utils.agent_deps import AgentDeps
from agents.utils.agent_factory import create_agent_from_database
from agents.utils.agent_run_utils import AgentLogger
from database import models
from database.database import create_session
from schemas import llm_output_schemas

FUZZY_MATCH_THRESHOLD = 0.90


def run(deps: AgentDeps) -> list[models.Tag]:
    if not deps.input_id or not deps.input_type:
        raise ValueError("input_id and input_type must be provided for manual labeling")

    item, item_content = deps.get_input(clear_tags=True)
    tags = process_in_background(deps, item, item_content)
    return tags


def process_in_background(
    deps: AgentDeps,
    item: models.DocumentTranscription | models.DocumentTechSpec | models.UserStory,
    item_content: str,
) -> list[models.Tag]:
    labeler = create_agent_from_database(
        name="labeler",
        session=deps.session,
        project_id=deps.project_id,
        agent_logger=deps.agent_logger,
    )

    deps.agent_logger.log("Associando tags")
    result = labeler.run_sync(
        item_content, output_type=llm_output_schemas.LabelerOutput, deps=deps
    )
    deps.agent_logger.finish_agent_run_with_usage(
        "Execução finalizada.", result.response.model_name, result.usage
    )

    input_tags = result.output
    item = deps.session.merge(item)
    tags = associate_tags(deps, item, input_tags)

    return tags


def _normalize_tag(tag: str) -> str:
    """Normalize a tag for comparison: strip whitespace and lowercase."""
    return tag.strip().lower()


def _find_fuzzy_match(
    normalized_input: str, current_tags: list[models.Tag]
) -> models.Tag | None:
    """Find the best fuzzy match among existing tags above the threshold."""
    best_match: models.Tag | None = None
    best_ratio = 0.0
    for tag in current_tags:
        ratio = SequenceMatcher(None, normalized_input, _normalize_tag(tag.tag)).ratio()
        if ratio >= FUZZY_MATCH_THRESHOLD and ratio > best_ratio:
            best_ratio = ratio
            best_match = tag
    return best_match


def associate_tags(deps, item, input_tags) -> list[models.Tag]:
    current_tags = (
        deps.session.query(models.Tag).filter_by(project_id=deps.project_id).all()
    )
    tag_objs = []
    seen_normalized: set[str] = set()
    seen_tag_ids: set[int] = set()
    for input_tag in input_tags:
        normalized_input = _normalize_tag(input_tag.tag)
        if normalized_input in seen_normalized:
            continue
        seen_normalized.add(normalized_input)

        # 1) Exact match (case-insensitive)
        existing_tag = next(
            (
                tag
                for tag in current_tags
                if _normalize_tag(tag.tag) == normalized_input
            ),
            None,
        )

        # 2) Fuzzy match fallback
        if not existing_tag:
            existing_tag = _find_fuzzy_match(normalized_input, current_tags)

        if existing_tag:
            tag_obj = existing_tag
        else:
            tag_obj = models.Tag(
                tag=input_tag.tag.strip(),
                project_id=deps.project_id,
            )
            deps.session.add(tag_obj)
            current_tags.append(tag_obj)

        tag_id = id(tag_obj)
        if tag_id in seen_tag_ids:
            continue
        seen_tag_ids.add(tag_id)

        if isinstance(item, models.DocumentTranscription):
            tag_obj.transcriptions.append(item)
        elif isinstance(item, models.DocumentTechSpec):
            tag_obj.tech_specs.append(item)
        elif isinstance(item, models.UserStory):
            tag_obj.user_stories.append(item)
        tag_objs.append(tag_obj)
    return tag_objs


def tag_and_commit(deps: AgentDeps):
    tags = run(deps)
    deps.session.add_all(tags)
    deps.session.commit()


def tag_and_commit_in_background(
    project_id: str,
    user_id: str,
    input_id: str,
    input_type: str,
    agent_run_id: str,
):
    with create_session() as session:
        agent_logger = AgentLogger(agent_run_id)
        deps = AgentDeps(
            session=session,
            user_id=user_id,
            project_id=project_id,
            input_id=input_id,
            input_type=input_type,
            agent_logger=agent_logger,
        )
        tags = run(deps)
        session.add_all(tags)
        session.commit()

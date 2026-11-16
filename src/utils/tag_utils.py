from typing import List, Literal, Tuple, Union

from sqlalchemy import func
from sqlalchemy.orm import Session

import schemas.tech_spec_schemas
import schemas.transcription_schemas
import schemas.user_story_schemas
from database import models
from utils.config import settings

TaggableItem = Union[
    models.DocumentTechSpec, models.UserStory, models.DocumentTranscription
]
TaggableItemList = List[Tuple[TaggableItem, float]]

TagItemTypes = Literal["transcription", "tech_spec", "user_story"]


TAG_MODEL_MAP = {
    "transcription": models.DocumentTranscription,
    "tech_spec": models.DocumentTechSpec,
    "user_story": models.UserStory,
}
TAG_SECONDARY_MAP = {
    "transcription": models.TranscriptionTag,
    "tech_spec": models.TechSpecTag,
    "user_story": models.UserStoryTag,
}
TAG_SCHEMA_MAP = {
    "transcription": schemas.transcription_schemas.DocumentTranscriptionBase,
    "tech_spec": schemas.tech_spec_schemas.DocumentTechSpecBase,
    "user_story": schemas.user_story_schemas.UserStoryBase,
}
TAG_ID_COL_MAP = {
    "transcription": models.TranscriptionTag.transcription_id,
    "tech_spec": models.TechSpecTag.tech_spec_id,
    "user_story": models.UserStoryTag.user_story_id,
}

# Jaccard threshold data calibrated for ~3-5 tags per item.
# Format: {total_tags: min_jaccard_threshold}
JACCARD_THRESHOLD_DATA = {
    20: 0.50,
    60: 0.30,
    100: 0.25,
    140: 0.20,
    180: 0.15,
}


def get_project_tag_count(project_id: str, session: Session) -> int:
    """Get the total number of tags in a project."""
    return session.query(models.Tag).filter(models.Tag.project_id == project_id).count()


def get_jaccard_threshold(total_tags: int) -> float:
    """Get the Jaccard similarity threshold based on total tags in project.

    Uses the simplified 5-value threshold data with linear interpolation.
    """
    if total_tags <= 20:
        return JACCARD_THRESHOLD_DATA[20]
    elif total_tags >= 180:
        return JACCARD_THRESHOLD_DATA[180]

    # Find the bounding points for interpolation
    tag_counts = sorted(JACCARD_THRESHOLD_DATA.keys())
    lower_bound = None
    upper_bound = None

    for i, count in enumerate(tag_counts):
        if count <= total_tags:
            lower_bound = count
        if count >= total_tags:
            upper_bound = count
            break

    # If we have both bounds, interpolate
    if lower_bound and upper_bound and lower_bound != upper_bound:
        lower_threshold = JACCARD_THRESHOLD_DATA[lower_bound]
        upper_threshold = JACCARD_THRESHOLD_DATA[upper_bound]

        # Linear interpolation
        weight = (total_tags - lower_bound) / (upper_bound - lower_bound)
        return lower_threshold + (upper_threshold - lower_threshold) * weight

    # Fallback to nearest value
    closest_tags = min(JACCARD_THRESHOLD_DATA.keys(), key=lambda x: abs(x - total_tags))
    return JACCARD_THRESHOLD_DATA[closest_tags]


def get_items_with_tags(
    project_id, tag_ids: list[str], item_type, session: Session
) -> List[Tuple[TaggableItem, int]]:
    """Fetch Items that have all the specified tags."""
    mapped_model = TAG_SECONDARY_MAP[item_type]
    main_model = TAG_MODEL_MAP[item_type]
    id_col = TAG_ID_COL_MAP[item_type]
    # Query for item_id and count of matched tags, join with main item table
    results = (
        session.query(
            main_model, func.count(mapped_model.tag_id).label("matched_tag_count")
        )
        .join(main_model, id_col == main_model.id)
        .filter(mapped_model.tag_id.in_(tag_ids))
        .filter(main_model.project_id == project_id)
        .group_by(main_model)
        .order_by(func.count(mapped_model.tag_id).desc())
        .all()
    )
    # Returns list of tuples: (item, matched_tag_count)

    return results


def get_items_with_tags_jaccard(
    project_id, tag_ids: list[str], item_type, session: Session
) -> TaggableItemList:
    """Fetch Items ranked by Jaccard similarity - simple approach using ORM relationships."""
    query_tag_count = len(tag_ids)

    results = get_items_with_tags(project_id, tag_ids, item_type, session)

    jaccard_results = []
    for item, matched_count in results:
        total_tags = len(item.tags)

        # Calculate Jaccard similarity
        denominator = query_tag_count + total_tags - matched_count
        if denominator == 0:
            jaccard_similarity = 0.0
        else:
            jaccard_similarity = matched_count / denominator

        jaccard_results.append((item, jaccard_similarity))

    return sorted(jaccard_results, key=lambda x: x[1], reverse=True)


def filter_items(
    items: TaggableItemList,
    min_items: int,
    max_items: int,
    jaccard_threshold: float = 0.0,
) -> TaggableItemList:
    """Filter items respecting minimum and maximum counts, and applying Jaccard threshold."""
    thresholded_items = [item for item in items if item[1] >= jaccard_threshold]

    if len(thresholded_items) < min_items:
        filtered_items = items[:min_items]
    else:
        filtered_items = thresholded_items

    filtered_items = filtered_items[:max_items]

    return filtered_items


def get_related_items(
    input_item: TaggableItem,
    output_item_type: TagItemTypes,
    session: Session,
) -> TaggableItemList:
    input_tags = input_item.tags
    input_tag_ids = [tag.id for tag in input_tags]
    related_items = get_items_with_tags_jaccard(
        input_item.project_id, input_tag_ids, output_item_type, session
    )

    # Get total tags in project for threshold calculation
    total_tags = get_project_tag_count(input_item.project_id, session)
    jaccard_threshold = get_jaccard_threshold(total_tags)

    return filter_items(
        related_items,
        settings.min_related_items,
        settings.max_related_items,
        jaccard_threshold,
    )


def get_related_items_from_id(
    input_id: str,
    input_item_type: TagItemTypes,
    output_item_type: TagItemTypes,
    session: Session,
) -> TaggableItemList:
    mapped_model = TAG_MODEL_MAP[input_item_type]
    input_item = session.query(mapped_model).filter_by(id=input_id).scalar()
    if not input_item:
        raise ValueError(f"{input_item_type} with id {input_id} not found")

    related_items = get_related_items(input_item, output_item_type, session)
    return related_items

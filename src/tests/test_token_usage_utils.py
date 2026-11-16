import pytest
from database import models
from utils.token_usage import (
    ORPHAN_SPEC_GROUP_ID,
    SPEC_WITHOUT_STORY_LABEL,
    UNKNOWN_MODEL_LABEL,
    build_ranked_items,
    model_label_column,
    specification_group_columns,
    token_sum_column,
)
from schemas.token_usage_schemas import ModelTokenUsagePublic, RankedTokenUsageItemPublic


def test_build_ranked_items_7_elements():
    rows = [
        ("entity_1", "proj_1", "Label 1", "gpt-4o", "agent_1", "Agent Name 1", 100),
        ("entity_1", "proj_1", "Label 1", "gpt-4o", "agent_1", "Agent Name 1", 50),
        ("entity_1", "proj_1", "Label 1", "gpt-4o-mini", "agent_2", "Agent Name 2", 20),
    ]
    ranked = build_ranked_items(rows, ModelTokenUsagePublic, RankedTokenUsageItemPublic)
    assert len(ranked) == 1
    assert ranked[0].id == "entity_1"
    assert ranked[0].project_id == "proj_1"
    assert ranked[0].label == "Label 1"
    assert ranked[0].total_tokens == 170
    assert len(ranked[0].models) == 2
    assert ranked[0].models[0].model == "gpt-4o"
    assert ranked[0].models[0].agent_id == "agent_1"
    assert ranked[0].models[0].agent_name == "Agent Name 1"
    assert ranked[0].models[0].total_tokens == 150
    assert ranked[0].models[1].model == "gpt-4o-mini"
    assert ranked[0].models[1].total_tokens == 20


def test_build_ranked_items_6_elements_and_5_elements():
    rows_6 = [
        ("entity_2", "proj_2", "Label 2", "gpt-4o", "agent_3", 80),
    ]
    ranked_6 = build_ranked_items(rows_6, ModelTokenUsagePublic, RankedTokenUsageItemPublic)
    assert len(ranked_6) == 1
    assert ranked_6[0].models[0].agent_id == "agent_3"
    assert ranked_6[0].models[0].agent_name is None
    assert ranked_6[0].models[0].total_tokens == 80

    rows_5 = [
        ("entity_3", "proj_3", "Label 3", "gpt-4o", 60),
    ]
    ranked_5 = build_ranked_items(rows_5, ModelTokenUsagePublic, RankedTokenUsageItemPublic)
    assert len(ranked_5) == 1
    assert ranked_5[0].models[0].agent_id is None
    assert ranked_5[0].models[0].agent_name is None
    assert ranked_5[0].models[0].total_tokens == 60


def test_build_ranked_items_zero_or_negative_tokens():
    rows = [
        ("entity_4", "proj_4", "Label 4", "gpt-4o", 0),
        ("entity_4", "proj_4", "Label 4", "gpt-4o", -10),
    ]
    ranked = build_ranked_items(rows, ModelTokenUsagePublic, RankedTokenUsageItemPublic)
    assert len(ranked) == 0

    # Test bucket where total calculation drops bucket if total <= 0
    from unittest.mock import MagicMock
    dummy_model_cls = MagicMock()
    dummy_ranked_cls = MagicMock()
    # If model_usage has total_tokens <= 0
    dummy_item = MagicMock()
    dummy_item.total_tokens = 0
    dummy_model_cls.return_value = dummy_item
    # Force models_map to have entry
    rows_mock = [("e", "p", "l", "m", 10)]
    ranked_empty = build_ranked_items(rows_mock, dummy_model_cls, dummy_ranked_cls)
    assert len(ranked_empty) == 0


def test_columns_helpers():
    # Test that column expressions can be instantiated
    c1 = model_label_column()
    assert c1 is not None

    c2 = token_sum_column()
    assert c2 is not None

    spec_id, spec_label = specification_group_columns()
    assert spec_id is not None
    assert spec_label is not None

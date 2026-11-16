"""Stored agent config that reaches the pydantic-ai Agent.

temperature was stored and edited in the admin UI but never sent; sending it
unconditionally breaks reasoning models, which reject the parameter.
"""

from types import SimpleNamespace

from agents.utils import agent_factory
from agents.utils.agent_factory import build_model_settings


def _agent(temperature=None, max_retries=3):
    return SimpleNamespace(temperature=temperature, max_retries=max_retries)


class TestModelSettings:
    def test_every_agent_is_told_to_cite_stories_by_title(self):
        rules = agent_factory.REFERENCE_INSTRUCTIONS

        assert "sempre pelo título" in rules
        assert "Nunca escreva o id" in rules
        assert "campos estruturados que pedem explicitamente o id" in rules

    def test_no_settings_when_temperature_is_unset(self):
        assert build_model_settings(_agent(temperature=None)) is None

    def test_temperature_is_forwarded_when_configured(self):
        settings = build_model_settings(_agent(temperature=0.7))

        assert settings["temperature"] == 0.7

    def test_zero_is_a_real_value_not_an_absence(self):
        settings = build_model_settings(_agent(temperature=0.0))

        assert settings is not None
        assert settings["temperature"] == 0.0


class TestTutorSeed:
    def test_tutor_leaves_temperature_unset(self):
        from scripts.seed_tutor_agent import TUTOR_MODEL, TUTOR_TEMPERATURE

        assert TUTOR_TEMPERATURE is None
        assert TUTOR_MODEL.startswith("openai:gpt-5")

"""Guards the model catalog against pydantic-ai provider renames."""

import pathlib
import uuid

import pytest
from pydantic_ai.providers import infer_provider_class

from agents.utils import supported_models as sm
from database.models import Agent


class TestModelCatalog:
    @pytest.mark.parametrize("provider", list(sm.MODEL_CATALOG))
    def test_provider_prefix_is_known_to_pydantic_ai(self, provider):
        infer_provider_class(provider)

    def test_no_unknown_providers(self):
        assert sm.unknown_providers() == []

    def test_legacy_gemini_prefix_is_not_used(self):
        assert "google-gla" not in sm.MODEL_CATALOG

    @pytest.mark.parametrize("provider", list(sm.MODEL_CATALOG))
    def test_provider_has_an_api_key_setting(self, provider):
        assert provider in sm.API_KEY_SETTING_BY_PROVIDER

    def test_model_names_carry_no_provider_prefix(self):
        for provider, model_names in sm.MODEL_CATALOG.items():
            for model in model_names:
                assert ":" not in model, f"{provider} -> {model}"

    def test_catalog_has_no_empty_provider(self):
        for provider, model_names in sm.MODEL_CATALOG.items():
            assert model_names, f"{provider} has no models"


class TestBuildProviders:
    def test_hides_providers_without_an_api_key(self, monkeypatch):
        monkeypatch.setattr(sm.settings, "google_api_key", None)
        monkeypatch.setattr(sm.settings, "openai_api_key", "key")
        monkeypatch.setattr(sm.settings, "anthropic_api_key", None)

        providers = sm._build_providers()

        assert set(providers) == {"openai"}

    def test_exposes_every_provider_that_has_a_key(self, monkeypatch):
        for setting in sm.API_KEY_SETTING_BY_PROVIDER.values():
            monkeypatch.setattr(sm.settings, setting, "key")

        providers = sm._build_providers()

        assert set(providers) == set(sm.MODEL_CATALOG)

    def test_reports_a_prefix_pydantic_ai_does_not_know(self, monkeypatch):
        monkeypatch.setitem(sm.MODEL_CATALOG, "google-gla", ["gemini-2.5-pro"])

        assert sm.unknown_providers() == ["google-gla"]


class TestImportStaysLight:
    """Importing the catalog must not drag vendor SDKs into every worker.

    Loading them at import time put two Heroku workers at ~588 MB against a
    512 MB quota, which showed up as sustained R14s under QA load.
    """

    VENDOR_MODULES = ("google.genai", "anthropic", "openai")

    def test_importing_the_catalog_loads_no_vendor_sdk(self):
        import subprocess
        import sys

        probe = (
            "import sys;"
            "import agents.utils.supported_models as sm;"
            "sm.SupportedModels.PROVIDERS;"
            f"print([m for m in {self.VENDOR_MODULES!r} if m in sys.modules])"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(pathlib.Path(__file__).resolve().parents[1]),
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().endswith("[]"), result.stdout


class TestBackfillModelProviderPrefix:
    @staticmethod
    def _run(db_session):
        from contextlib import contextmanager

        from scripts import backfill_model_provider_prefix as script

        @contextmanager
        def _session_factory():
            yield db_session

        monkeypatched = script.create_session
        script.create_session = _session_factory
        try:
            return script.backfill()
        finally:
            script.create_session = monkeypatched

    def _agent(self, db_session, model):
        agent = Agent(
            id=str(uuid.uuid4()),
            name=f"agent-{uuid.uuid4().hex[:6]}",
            model=model,
            is_default=True,
            temperature=0.3,
            max_retries=3,
        )
        db_session.add(agent)
        db_session.commit()
        db_session.refresh(agent)
        return agent

    def test_renames_legacy_prefix_and_keeps_model_name(self, db_session):
        agent = self._agent(db_session, "google-gla:gemini-2.5-flash")

        assert self._run(db_session) == 1

        db_session.refresh(agent)
        assert agent.model == "google:gemini-2.5-flash"

    def test_applies_prefix_rename_and_model_replacement_together(self, db_session):
        agent = self._agent(db_session, "google-gla:gemini-2.5-pro")

        assert self._run(db_session) == 1

        db_session.refresh(agent)
        assert agent.model == "google:gemini-3.1-pro-preview"

    @pytest.mark.parametrize(
        "retired,expected",
        [
            ("anthropic:claude-opus-4-1", "anthropic:claude-opus-5"),
            ("anthropic:claude-opus-4-1-20250805", "anthropic:claude-opus-5"),
            ("anthropic:claude-sonnet-4-5", "anthropic:claude-sonnet-5"),
            ("anthropic:claude-sonnet-4-5-20250929", "anthropic:claude-sonnet-5"),
            ("google:gemini-2.5-pro", "google:gemini-3.1-pro-preview"),
        ],
    )
    def test_replaces_models_dropped_from_the_catalog(self, db_session, retired, expected):
        agent = self._agent(db_session, retired)

        assert self._run(db_session) == 1

        db_session.refresh(agent)
        assert agent.model == expected

    def test_every_replacement_is_in_the_catalog(self):
        from scripts.backfill_model_provider_prefix import (
            REPLACED_MODELS,
            catalog_model_specs,
        )

        known = catalog_model_specs()
        for retired, current in REPLACED_MODELS.items():
            assert current in known, f"{retired} -> {current} is not selectable"

    def test_leaves_other_providers_untouched(self, db_session):
        openai_agent = self._agent(db_session, "openai:gpt-5.2")
        anthropic_agent = self._agent(db_session, "anthropic:claude-opus-5")

        assert self._run(db_session) == 0

        db_session.refresh(openai_agent)
        db_session.refresh(anthropic_agent)
        assert openai_agent.model == "openai:gpt-5.2"
        assert anthropic_agent.model == "anthropic:claude-opus-5"

    def test_reports_a_model_outside_the_catalog_without_changing_it(
        self, db_session, capsys
    ):
        agent = self._agent(db_session, "openai:gpt-inventado-9")

        assert self._run(db_session) == 0

        db_session.refresh(agent)
        assert agent.model == "openai:gpt-inventado-9"
        assert "openai:gpt-inventado-9" in capsys.readouterr().out

    def test_is_idempotent(self, db_session):
        agent = self._agent(db_session, "google-gla:gemini-2.5-flash")

        assert self._run(db_session) == 1
        assert self._run(db_session) == 0

        db_session.refresh(agent)
        assert agent.model == "google:gemini-2.5-flash"

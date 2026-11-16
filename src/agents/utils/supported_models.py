"""
Module defining supported language model providers and their models.
"""

from utils.config import settings

MODEL_CATALOG: dict[str, list[str]] = {
    "google": [
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-3.1-pro-preview",
    ],
    "openai": [
        "gpt-4.1-nano",
        "gpt-4.1-mini",
        "gpt-4.1",
        "gpt-5-nano",
        "gpt-5-mini",
        "gpt-5",
        "gpt-5.1",
        "gpt-5.2",
        "gpt-5.4-nano",
        "gpt-5.4-mini",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    ],
    "anthropic": [
        "claude-fable-5",
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
    ],
}

API_KEY_SETTING_BY_PROVIDER: dict[str, str] = {
    "google": "google_api_key",
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
}


def unknown_providers() -> list[str]:
    """Catalog prefixes that pydantic-ai does not recognise.

    Resolving a prefix imports that vendor's SDK, so this runs in the test suite
    rather than at import time, where it cost every worker ~125 MB of resident
    memory to load SDKs the process might never use.
    """
    from pydantic_ai.providers import infer_provider_class

    unknown = []
    for provider in MODEL_CATALOG:
        try:
            infer_provider_class(provider)
        except ValueError:
            unknown.append(provider)
    return unknown


def _build_providers() -> dict[str, list[str]]:
    return {
        provider: models
        for provider, models in MODEL_CATALOG.items()
        if getattr(settings, API_KEY_SETTING_BY_PROVIDER[provider], None)
    }


class SupportedModels:
    """
    Supported language model providers and their respective models.
    """

    PROVIDERS = _build_providers()

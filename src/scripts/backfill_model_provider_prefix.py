"""Idempotent backfill of agent model strings that are no longer offered.

Covers provider prefixes renamed by pydantic-ai and models dropped from
MODEL_CATALOG, whether retired by the provider (claude-opus-4-1) or merely
delisted while still callable (claude-sonnet-4-5). Agents whose model is still
outside MODEL_CATALOG afterwards are reported, not changed.

Run:

    uv run python -m scripts.backfill_model_provider_prefix
"""

from agents.utils.supported_models import MODEL_CATALOG
from database import models
from database.database import create_session
from utils import log

logger = log.setup_custom_logger(__name__)

RENAMED_PREFIXES = {"google-gla": "google"}

REPLACED_MODELS = {
    "anthropic:claude-opus-4-1": "anthropic:claude-opus-5",
    "anthropic:claude-sonnet-4-5": "anthropic:claude-sonnet-5",
    "google:gemini-2.5-pro": "google:gemini-3.1-pro-preview",
}


def catalog_model_specs() -> set[str]:
    return {
        f"{provider}:{model}"
        for provider, model_names in MODEL_CATALOG.items()
        for model in model_names
    }


def resolve_replacement(model: str) -> str | None:
    """Return the model string to use instead, or None when already current."""
    if not model:
        return None

    provider, _, name = model.partition(":")
    replacement = f"{RENAMED_PREFIXES[provider]}:{name}" if provider in RENAMED_PREFIXES else model

    for retired, current in REPLACED_MODELS.items():
        if replacement == retired or replacement.startswith(f"{retired}-"):
            replacement = current
            break

    return replacement if replacement != model else None


def backfill() -> int:
    with create_session() as session:
        agents = session.query(models.Agent).all()

        updated = 0
        for agent in agents:
            replacement = resolve_replacement(agent.model)
            if replacement is None:
                continue
            logger.info(
                "Agent '%s' (id=%s): '%s' -> '%s'",
                agent.name,
                agent.id,
                agent.model,
                replacement,
            )
            agent.model = replacement
            updated += 1

        session.commit()

        known = catalog_model_specs()
        unknown = sorted({a.model for a in agents if a.model not in known})
        for model in unknown:
            logger.warning("Agent model outside MODEL_CATALOG, review manually: %s", model)

        print(f"Backfill complete: {updated} agent(s) updated.")
        if unknown:
            print("Models outside the catalog (unchanged): " + ", ".join(unknown))
        return updated


if __name__ == "__main__":
    backfill()

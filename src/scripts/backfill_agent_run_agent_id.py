"""Idempotent backfill of agent_id on historical agent_run records.

Matches agent_run rows without agent_id by inferring the agent name from the run title
and associating the appropriate agent (checking project overrides first, then defaults).

Run:
    uv run python -m scripts.backfill_agent_run_agent_id
    or
    python -m src.scripts.backfill_agent_run_agent_id
"""

import argparse
from typing import Optional

from sqlalchemy.orm import Session

from agents.utils.agent_factory import get_agent_info
from database import models
from database.database import create_session
from utils import log

logger = log.setup_custom_logger(__name__)

_TITLE_AGENT_RULES: tuple[tuple[tuple[tuple[str, ...], ...], list[str]], ...] = (
    ((("tutor",),), ["tutor_agent", "tutor"]),
    ((("openhands",),), ["openhands_agent", "openhands"]),
    ((("gherkin", "criação de cenários gherkin", "criacao de cenarios gherkin"),), ["gherkin_agent", "gherkin"]),
    (
        (("roteiro de desenvolvimento", "development steps"),),
        ["user_story_to_dev_steps", "dev_steps_agent"],
    ),
    (
        (
            (
                "código baseado em testes",
                "codigo baseado em testes",
                "geração do código",
                "geracao do codigo",
                "geração de código",
                "geracao de codigo",
                "code_agent",
            ),
        ),
        ["code_agent"],
    ),
    (
        (("testes unitários", "testes unitarios", "unit_test", "geração de testes unitários", "geracao de testes unitarios"),),
        ["unit_test_agent"],
    ),
    (
        (
            ("transcrição", "transcricao"),
            ("tech spec", "especificação técnica", "especificacao tecnica"),
        ),
        ["transcription_to_tech_spec_agent", "transcription_to_tech_spec"],
    ),
    (
        (
            ("transcrição", "transcricao"),
            ("user story", "história", "historia"),
        ),
        ["transcription_to_user_story_agent", "transcription_to_user_story", "transcription_to_story"],
    ),
    (
        (
            (
                "tags",
                "tag",
                "labeler",
                "classificação de conteúdo",
                "classificacao de conteudo",
                "adicionando transcrição",
                "adicionando transcricao",
            ),
        ),
        ["labeler_agent", "labeler"],
    ),
    (
        (("repositório", "repositorio", "repo_to_tech_spec", "análise de repositório", "analise de repositorio"),),
        ["repo_to_tech_spec", "repo_to_tech_spec_agent"],
    ),
    (
        (
            (
                "finalização de especificação",
                "finalizacao de especificacao",
                "story_completion",
                "detalhamento técnico",
                "detalhamento tecnico",
            ),
        ),
        ["story_completion", "story_completion_agent"],
    ),
    (
        (("devin", "sessão de desenvolvimento", "sessao de desenvolvimento"),),
        ["devin-api", "devin_agent", "devin"],
    ),
)


def _matches_rule(text: str, keyword_groups: tuple[tuple[str, ...], ...]) -> bool:
    return all(any(kw in text for kw in group) for group in keyword_groups)


def infer_agent_candidates(
    title: str | None
) -> list[str]:
    """Return a list of candidate agent names in order of preference based on run title."""
    if not title:
        return []

    t = title.strip().lower()
    for keyword_groups, candidates in _TITLE_AGENT_RULES:
        if _matches_rule(t, keyword_groups):
            return list(candidates)

    return []


def resolve_agent_for_run(
        session: Session,
        project_id: str | None,
        title: str | None,
) -> models.Agent | None:
    candidates = infer_agent_candidates(title)
    for agent_name in candidates:
        agent = get_agent_info(
            session=session, agent_name=agent_name, project_id=project_id
        )
        if agent:
            return agent

    # Fallback search if exact name match wasn't found in defaults/overrides
    for agent_name in candidates:
        agent = (
            session.query(models.Agent)
            .filter(models.Agent.name.ilike(f"%{agent_name}%"))
            .order_by(models.Agent.is_default.desc(), models.Agent.created_at.desc())
            .first()
        )
        if agent:
            return agent

    return None


def _run_backfill(session: Session, dry_run: bool = False) -> tuple[int, int]:
    runs = (
        session.query(models.AgentRun)
        .filter(models.AgentRun.agent_id.is_(None))
        .filter(models.AgentRun.model.isnot(None))
        .all()
    )

    total_pending = len(runs)
    updated = 0
    unmatched = 0

    logger.info("Found %d agent_run records without agent_id", total_pending)

    for run in runs:
        agent = resolve_agent_for_run(
            session=session,
            project_id=run.project_id,
            title=run.title,
        )
        if agent:
            logger.info(
                "Matching run '%s' (id=%s) -> Agent '%s' (id=%s)",
                run.title,
                run.id,
                agent.name,
                agent.id,
            )
            if not dry_run:
                run.agent_id = agent.id
            updated += 1
        else:
            logger.warning(
                "Could not match run '%s' (id=%s, project_id=%s) to an agent",
                run.title,
                run.id,
                run.project_id,
            )
            unmatched += 1

    if not dry_run:
        session.commit()
        logger.info("Committed %d agent_run updates.", updated)
    else:
        logger.info("DRY RUN: %d agent_run records would be updated.", updated)

    print(
        f"Backfill finished: {updated} updated, {unmatched} unmatched out of {total_pending} pending."
    )
    return updated, unmatched


def backfill_agent_runs(
        dry_run: bool = False, session: Optional[Session] = None
) -> tuple[int, int]:
    if session is not None:
        return _run_backfill(session, dry_run=dry_run)

    with create_session() as session:
        return _run_backfill(session, dry_run=dry_run)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill agent_id on historical agent_run rows."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform a trial run without making changes.",
    )
    args = parser.parse_args()
    backfill_agent_runs(dry_run=args.dry_run)

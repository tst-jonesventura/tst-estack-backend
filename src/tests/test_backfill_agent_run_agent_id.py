import uuid

from database import models
from scripts.backfill_agent_run_agent_id import (
    backfill_agent_runs,
    infer_agent_candidates,
)


def test_infer_agent_candidates():
    assert "tutor_agent" in infer_agent_candidates("Tutor")
    assert "openhands_agent" in infer_agent_candidates("OpenHands")
    assert "gherkin_agent" in infer_agent_candidates("Criação de cenários Gherkin para user story Test")
    assert "user_story_to_dev_steps" in infer_agent_candidates("Roteiro de desenvolvimento para User Story - Test")
    assert "unit_test_agent" in infer_agent_candidates("Geração de testes unitários para User Story - Test")
    assert "code_agent" in infer_agent_candidates(
        "Geração do código baseado em testes unitários para User Story - Test")
    assert "transcription_to_tech_spec_agent" in infer_agent_candidates("Transcrição para Tech Spec 123")
    assert "transcription_to_user_story_agent" in infer_agent_candidates("Transcrição para User Story 123")
    assert "labeler_agent" in infer_agent_candidates("Criação de tags user_story")
    assert "labeler_agent" in infer_agent_candidates("Criação de tags para user story Card")
    assert "labeler_agent" in infer_agent_candidates("Criação de tags para especificação técnica")
    assert "labeler" in infer_agent_candidates("Criação de tags para user story Card")
    assert "repo_to_tech_spec" in infer_agent_candidates(
        "Criação de especificações técnicas para repositório https://github.com/org/repo")
    assert "story_completion" in infer_agent_candidates(
        "Finalização de especificação e atualização de detalhamento técnico")
    assert "devin-api" in infer_agent_candidates("Criação de sessão de desenvolvimento para User Story 123")


def test_backfill_agent_runs_populates_agent_id(db_session, sample_admin_user, sample_second_project):
    agent_tutor = models.Agent(
        id=str(uuid.uuid4()),
        name="tutor_agent",
        model="openai:gpt-4o",
        is_default=True,
        max_retries=1,
    )
    agent_gherkin = models.Agent(
        id=str(uuid.uuid4()),
        name="gherkin_agent",
        model="openai:gpt-4o",
        is_default=True,
        max_retries=1,
    )
    agent_labeler = models.Agent(
        id=str(uuid.uuid4()),
        name="labeler",
        model="openai:gpt-4o",
        is_default=True,
        max_retries=1,
    )
    db_session.add_all([agent_tutor, agent_gherkin, agent_labeler])
    db_session.commit()

    run1 = models.AgentRun(
        id=str(uuid.uuid4()),
        user_id=sample_admin_user.id,
        project_id=sample_second_project.id,
        title="Tutor",
        status="finished",
        model="gpt-4o",
        agent_id=None,
    )
    run2 = models.AgentRun(
        id=str(uuid.uuid4()),
        user_id=sample_admin_user.id,
        project_id=sample_second_project.id,
        title="Criação de cenários Gherkin para user story US-1",
        status="finished",
        model="gpt-4o",
        agent_id=None,
    )
    run3 = models.AgentRun(
        id=str(uuid.uuid4()),
        user_id=sample_admin_user.id,
        project_id=sample_second_project.id,
        title="Criação de tags para user story Card",
        status="finished",
        model="gpt-4o",
        agent_id=None,
    )
    run4 = models.AgentRun(
        id=str(uuid.uuid4()),
        user_id=sample_admin_user.id,
        project_id=sample_second_project.id,
        title="Criação de tags para especificação técnica",
        status="finished",
        model="gpt-4o",
        agent_id=None,
    )
    db_session.add_all([run1, run2, run3, run4])
    db_session.commit()

    updated, unmatched = backfill_agent_runs(dry_run=False, session=db_session)

    db_session.refresh(run1)
    db_session.refresh(run2)
    db_session.refresh(run3)
    db_session.refresh(run4)

    assert run1.agent_id == agent_tutor.id
    assert run2.agent_id == agent_gherkin.id
    assert run3.agent_id == agent_labeler.id
    assert run4.agent_id == agent_labeler.id
    assert updated >= 4
    assert unmatched == 0


def test_backfill_agent_runs_uses_project_override_and_falls_back_to_default(
        db_session, sample_admin_user, sample_project, sample_second_project
):
    default_agent = models.Agent(
        id=str(uuid.uuid4()),
        name="tutor_agent",
        model="openai:gpt-4o",
        is_default=True,
        max_retries=1,
    )
    custom_agent = models.Agent(
        id=str(uuid.uuid4()),
        name="custom_tutor_agent",
        model="openai:gpt-4o-mini",
        is_default=False,
        max_retries=1,
    )
    db_session.add_all([default_agent, custom_agent])
    db_session.commit()

    # Project override for sample_project
    override = models.ProjectAgent(
        id=str(uuid.uuid4()),
        project_id=sample_project.id,
        agent_name="tutor_agent",
        agent_id=custom_agent.id,
    )
    db_session.add(override)
    db_session.commit()

    # Run in customized project
    run_custom = models.AgentRun(
        id=str(uuid.uuid4()),
        user_id=sample_admin_user.id,
        project_id=sample_project.id,
        title="Tutor",
        status="finished",
        model="gpt-4o-mini",
        agent_id=None,
    )
    # Run in project without customization (should use default)
    run_default = models.AgentRun(
        id=str(uuid.uuid4()),
        user_id=sample_admin_user.id,
        project_id=sample_second_project.id,
        title="Tutor",
        status="finished",
        model="gpt-4o",
        agent_id=None,
    )
    db_session.add_all([run_custom, run_default])
    db_session.commit()

    updated, unmatched = backfill_agent_runs(dry_run=False, session=db_session)

    db_session.refresh(run_custom)
    db_session.refresh(run_default)

    assert run_custom.agent_id == custom_agent.id
    assert run_default.agent_id == default_agent.id
    assert updated >= 2
    assert unmatched == 0

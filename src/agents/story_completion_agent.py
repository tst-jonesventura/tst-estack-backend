from datetime import datetime, timezone

import utils.log as log
from agents import labeler_agent
from agents.utils.agent_deps import AgentDeps
from agents.utils.agent_factory import create_agent_from_database
from agents.utils.agent_run_utils import create_agent_logger
from database import models
from database.database import create_session
from database.models import DocumentTechSpec
from schemas import llm_output_schemas
from services.agent_queue_service import wait_for_sequential_slot

logger = log.setup_custom_logger(__name__)

QUEUE_KEY = "story_completion"


def run_story_completion(
    project_id: str,
    user_story_id: str,
    user_id: str,
):
    with create_session() as session:
        agent_logger = create_agent_logger(
            session=session,
            user_id=user_id,
            project_id=project_id,
            agent_run_title="Finalização de especificação e atualização de detalhamento técnico",
            user_story_id=user_story_id,
            queue_key=QUEUE_KEY,
            agent_name="story_completion",
        )

        wait_for_sequential_slot(project_id, QUEUE_KEY, agent_logger.agent_run_id)

        story_completion = create_agent_from_database(
            project_id=project_id,
            name="story_completion",
            session=session,
            agent_logger=agent_logger
        )

        deps = AgentDeps(
            project_id=project_id,
            agent_logger=agent_logger,
            user_id=user_id,
            input_id=user_story_id,
            input_type="user_story",
            session=session,
        )

        _, story_content = deps.get_input()

        deps.agent_logger.log("Analisando especificação finalizada e detalhamentos técnicos relacionados...")

        result = story_completion.run_sync(
            story_content,
            output_type=llm_output_schemas.StoryCompletionTechSpecOutput,
            deps=deps,
        )

        deps.agent_logger.log(f"Detalhamento técnico criado por agente de finalização de especificação: {result.output}")
        deps.agent_logger.log(result.output)
        deps.agent_logger.finish_agent_run_with_usage(
            "Execução finalizada.", result.response.model_name, result.usage
        )

        new_tech_specs = result.output.created
        updated_tech_specs = result.output.updated
        tech_spec_db = []

        for tech_spec in new_tech_specs:
            tech_spec_db.append(
                DocumentTechSpec(
                    title=tech_spec.title,
                    content=tech_spec.content,
                    project_id=project_id,
                    user_id=user_id,
                    log="Detalhamento técnico criado por agente de finalização de especificação",
                )
            )

        updated_ids = []
        for updated_tech_spec in updated_tech_specs:
            existing = (
                session.query(models.DocumentTechSpec)
                .filter_by(id=updated_tech_spec.id)
                .one_or_none()
            )
            if not existing:
                logger.warning("Tech spec %s not found, skipping update", updated_tech_spec.id)
                continue
            existing.title = updated_tech_spec.title
            existing.content = updated_tech_spec.content
            existing.user_id = user_id
            existing.created_at = datetime.now(timezone.utc)
            existing.log = "Detalhamento técnico atualizado por agente de finalização de especificação"
            session.add(existing)
            updated_ids.append(existing.id)

        session.add_all(tech_spec_db)
        session.commit()

        ids = [ts.id for ts in tech_spec_db]
        ids.extend(updated_ids)

        for _id in ids:
            labeler_agent_logger = create_agent_logger(
                session=session,
                user_id=user_id,
                project_id=project_id,
                agent_run_title=f"Criação de tags para detalhamento técnico {_id}",
                agent_name="labeler",
            )
            labeler_deps = AgentDeps(
                input_id=_id,
                input_type="tech_spec",
                project_id=project_id,
                agent_logger=labeler_agent_logger,
                user_id=user_id,
                session=session,
            )
            tags = labeler_agent.run(labeler_deps)
            session.add_all(tags)
            session.commit()

    logger.info(
        "Story completion agent finished for story %s: %d created, %d updated",
        user_story_id,
        len(new_tech_specs),
        len(updated_tech_specs),
    )

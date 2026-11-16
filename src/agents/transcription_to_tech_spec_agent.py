from datetime import datetime, timezone

import utils.log as log
from agents import labeler_agent
from agents.utils.agent_deps import AgentDeps
from agents.utils.agent_factory import create_agent_from_database
from agents.utils.agent_run_utils import AgentLogger
from database import models
from database.database import create_session
from database.models import DocumentTechSpec
from schemas import llm_output_schemas
from services.agent_queue_service import wait_for_sequential_slot

logger = log.setup_custom_logger(__name__)


def process_in_background(
    project_id: str,
    user_message: str,
    user_id: str,
    transcription_id: str,
    agent_logger: AgentLogger,
):
    with create_session() as session:
        wait_for_sequential_slot(project_id, "transcription_to_tech_spec", agent_logger.agent_run_id)
        transcription_to_tech_spec_agent = create_agent_from_database(
            project_id=project_id,
            name="transcription_to_tech_spec",
            session=session,
            agent_logger=agent_logger
        )
        deps = AgentDeps(
            project_id=project_id,
            agent_logger=agent_logger,
            user_id=user_id,
            input_id=transcription_id,
            input_type="transcription",
            session=session,
        )

        agent_logger.log("Transformando transcrição em especificação técnica...")
        result = transcription_to_tech_spec_agent.run_sync(
            user_message,
            output_type=llm_output_schemas.TranscriptionToTechSpecOutput,
            deps=deps,
        )
        agent_logger.finish_agent_run_with_usage("Execução finalizada.", result.response.model_name, result.usage)

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
                    transcription_id=transcription_id,
                    log="Especificação técnica criada por transcrição",
                )
            )
        for updated_tech_spec in updated_tech_specs:
            tech_spec = (
                session.query(models.DocumentTechSpec)
                .filter_by(id=updated_tech_spec.id)
                .one_or_none()
            )
            if not tech_spec:
                continue
            tech_spec.title = updated_tech_spec.title
            tech_spec.content = updated_tech_spec.content
            tech_spec.user_id = user_id
            tech_spec.created_at = datetime.now(timezone.utc)
            tech_spec.transcription_id = transcription_id
            tech_spec.log = "Especificação técnica atualizada por transcrição"
            session.add(tech_spec)
        session.add_all(tech_spec_db)
        session.commit()
        ids = [ts.id for ts in tech_spec_db]
        ids.extend([ts.id for ts in updated_tech_specs])

        for _id in ids:
            deps.input_id = _id
            deps.input_type = "tech_spec"
            tags = labeler_agent.run(deps)
            session.add_all(tags)
            session.commit()

    logger.info("Generated tech specs: %s", new_tech_specs)
    logger.info("Updated tech specs: %s", updated_tech_specs)

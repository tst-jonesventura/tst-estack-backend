import utils.log as log
from agents import labeler_agent
from agents.utils.agent_deps import AgentDeps
from agents.utils.agent_factory import create_agent_from_database
from agents.utils.agent_run_utils import create_agent_logger
from database.database import create_session
from database.models import DocumentTechSpec
from schemas import llm_output_schemas
from services.agent_queue_service import wait_for_sequential_slot

logger = log.setup_custom_logger(__name__)


def create_tech_specs_from_repo_in_background(
    project_id: str,
    repo_url: str,
    user_id: str,
):
    with create_session() as session:
        try:
            agent_logger = create_agent_logger(
                session=session,
                user_id=user_id,
                project_id=project_id,
                agent_run_title=f"Criação de especificações técnicas para repositório {repo_url}",
                queue_key="repo_to_tech_spec",
                agent_name="repo_to_tech_spec",
            )

            wait_for_sequential_slot(project_id, "repo_to_tech_spec", agent_logger.agent_run_id)

            repo_to_tech_spec_agent = create_agent_from_database(
                project_id=project_id,
                name="repo_to_tech_spec",
                session=session,
                agent_logger=agent_logger
            )

            deps = AgentDeps(
                project_id=project_id,
                agent_logger=agent_logger,
                user_id=user_id,
                session=session,
            )

            deps.agent_logger.log(
                f"Analisando repositório para gerar especificações técnicas: {repo_url}"
            )
            result = repo_to_tech_spec_agent.run_sync(
                repo_url,
                output_type=llm_output_schemas.TranscriptionToTechSpecOutput,
                deps=deps,
            )
            deps.agent_logger.finish_agent_run_with_usage("Execução finalizada.", result.response.model_name, result.usage)

            new_tech_specs = result.output.created
            tech_spec_db = []
            for tech_spec in new_tech_specs:
                tech_spec_db.append(
                    DocumentTechSpec(
                        title=tech_spec.title,
                        content=tech_spec.content,
                        project_id=project_id,
                        user_id=user_id,
                        log="Especificação técnica criada por análise de repositório",
                    )
                )
            session.add_all(tech_spec_db)
            session.commit()
            ids = [ts.id for ts in tech_spec_db]

            for _id in ids:
                labeler_agent_logger = create_agent_logger(
                    session=session,
                    user_id=user_id,
                    project_id=project_id,
                    agent_run_title=f"Criação de tags para especificação técnica {_id}",
                    agent_name="labeler",
                )

                labeler_agent_deps = AgentDeps(
                    input_id = _id,
                    input_type = "tech_spec",
                    project_id=project_id,
                    agent_logger=labeler_agent_logger,
                    user_id=user_id,
                    session=session,
                )

                tags = labeler_agent.run(labeler_agent_deps)
                session.add_all(tags)
                session.commit()
        except Exception as e:
            logger.error(f"Error generating tech specs from repository: {e}")
            raise

    logger.info("Generated tech specs from repository: %s", [ts.title for ts in new_tech_specs])

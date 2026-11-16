from typing import List

from agents.utils.agent_deps import AgentDeps
from agents.utils.agent_factory import create_agent_from_database
from database import models
from schemas.llm_output_schemas import GherkinOutput


def run(deps: AgentDeps) -> List[models.GherkinRule]:
    if not deps.input_id or not deps.input_type:
        raise ValueError("input_id and input_type must be provided for manual labeling")

    user_story, user_story_content = deps.get_input()
    assert isinstance(user_story, models.UserStory)
    llm_output = process_in_background(deps, user_story_content)
    orm_output = map_to_orm(user_story, llm_output)
    return orm_output


def map_to_orm(
    user_story: models.UserStory, gherkin_output: GherkinOutput
) -> List[models.GherkinRule]:
    gherkin_rules_orm = []
    for rule in gherkin_output:
        scenarios_orm = [
            models.GherkinScenario(
                index=scenario.index,
                title=scenario.title,
                text=scenario.text,
                is_done=scenario.is_done,
            )
            for scenario in rule.scenarios
        ]
        gherkin_rule_orm = models.GherkinRule(
            index=rule.index,
            title=rule.title,
            background=rule.background,
            user_story_id=user_story.id,
            scenarios=scenarios_orm,
        )
        gherkin_rules_orm.append(gherkin_rule_orm)
    return gherkin_rules_orm


def process_in_background(
    deps: AgentDeps,
    item_content: str,
) -> GherkinOutput:
    gherkin = create_agent_from_database(
        name="gherkin",
        session=deps.session,
        project_id=deps.project_id,
        agent_logger=deps.agent_logger
    )
    deps.agent_logger.log("Criando cenários Gherkin")

    result = gherkin.run_sync(
        item_content, output_type=GherkinOutput, deps=deps
    )

    deps.agent_logger.finish_agent_run_with_usage("Execução finalizada.", result.response.model_name, result.usage)
    return result.output

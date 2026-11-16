from typing import List

from pydantic import Field

from schemas.base import Base
from schemas.tag_schemas import TagBase
from schemas.tech_spec_schemas import DocumentTechSpecBase, DocumentTechSpecBaseWithId
from schemas.user_story_schemas import (
    GherkinRuleBase,
    GherkinScenarioBase,
    UserStoryBaseLLM,
    UserStoryBaseLLMWithId,
    UserStoryDevStepBase,
)

LabelerOutput = List[TagBase]


class TranscriptionToTechSpecOutput(Base):
    created: List[DocumentTechSpecBase] = Field(
        default_factory=list,
        description="List of newly created technical specification documents. The UUID will be generated upon creation.",
    )
    updated: List[DocumentTechSpecBaseWithId] = Field(
        default_factory=list,
        description="List of updated technical specification documents with their UUIDs.",
    )


class TranscriptionToStoryOutput(Base):
    created: List[UserStoryBaseLLM] = Field(
        default_factory=list,
        description="List of user stories derived from the transcription. The UUID will be generated upon creation.",
    )
    updated: List[UserStoryBaseLLMWithId] = Field(
        default_factory=list,
        description="List of updated user stories with their UUIDs.",
    )


class StoryCompletionTechSpecOutput(Base):
    updated: List[DocumentTechSpecBaseWithId] = Field(
        default_factory=list,
        description="Lista de detalhamentos técnicos existentes que foram atualizados com base nos incrementos da especificação.",
    )
    created: List[DocumentTechSpecBase] = Field(
        default_factory=list,
        description="Lista de novos detalhamentos técnicos criados a partir dos incrementos da especificação.",
    )


class GherkinRuleOutput(GherkinRuleBase):
    scenarios: List[GherkinScenarioBase] = Field(
        default_factory=list,
        description="List of Gherkin scenarios associated with the rule.",
    )


GherkinOutput = List[GherkinRuleOutput]
UserStoryToDevStepsOutput = List[UserStoryDevStepBase]

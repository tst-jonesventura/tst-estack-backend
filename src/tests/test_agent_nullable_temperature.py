"""An agent may have no temperature, and the API must still serialize it.

Reasoning models such as the gpt-5 family reject the parameter, so the column was
made nullable and the seeds now leave it unset. AgentBase still required a float,
so listing a project's agents raised a ValidationError and the Agentes tab showed
"Erro ao carregar agentes do projeto".
"""

import uuid

import pytest

from database.models import Agent
from schemas.agent_schemas import AgentPublic, AgentShortPublic


def _agent(db_session, temperature):
    agent = Agent(
        id=str(uuid.uuid4()),
        name=f"agent-{uuid.uuid4().hex[:6]}",
        model="openai:gpt-5.2",
        is_default=True,
        temperature=temperature,
        max_retries=3,
    )
    db_session.add(agent)
    db_session.commit()
    db_session.refresh(agent)
    return agent


class TestSerialization:
    @pytest.mark.parametrize("schema", [AgentPublic, AgentShortPublic])
    def test_an_agent_without_temperature_serializes(self, db_session, schema):
        agent = _agent(db_session, None)

        assert schema.model_validate(agent).temperature is None

    @pytest.mark.parametrize("schema", [AgentPublic, AgentShortPublic])
    def test_a_configured_temperature_survives(self, db_session, schema):
        agent = _agent(db_session, 0.3)

        assert schema.model_validate(agent).temperature == 0.3


class TestProjectAgentsEndpoint:
    def test_listing_agents_works_when_temperature_is_unset(
        self, client, project_access_user, sample_project, db_session
    ):
        _agent(db_session, None)

        response = client.get(f"/projects/{sample_project.id}/agents")

        assert response.status_code == 200

    def test_the_unset_temperature_is_reported_as_null(
        self, client, project_access_user, sample_project, db_session
    ):
        agent = _agent(db_session, None)

        response = client.get(f"/projects/{sample_project.id}/agents")

        listed = {
            row["agent"]["name"]: row["agent"]["temperature"]
            for row in response.json()
        }
        assert listed[agent.name] is None

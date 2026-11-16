"""Conversational OpenHands agent: persistence, turn processing and router."""

import json
import uuid
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest
from agents import openhands_agent, openhands_chat_agent, openhands_goals
from database.models import AgentRun, OpenHandsConversation, OpenHandsMessage, User
from fastapi.testclient import TestClient
from pydantic_ai import RunUsage
from schemas.openhands_schemas import OpenHandsRepository

from src.routers.agents import openhands_chat_router


class _NoCloseSession:
    def __init__(self, session):
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, *_exc):
        return False


@pytest.fixture
def conversation(
    db_session, sample_project, sample_user, sample_project_repository, tmp_path
):
    with patch.object(openhands_agent, "_workspace_root_dir", return_value=tmp_path):
        return openhands_chat_agent.create_conversation(
            db_session, sample_project.id, sample_user.id, "mcp_creator"
        )


@pytest.fixture
def agent_session(db_session):
    with patch.object(
        openhands_chat_agent,
        "create_session",
        return_value=_NoCloseSession(db_session),
    ):
        yield db_session


@pytest.fixture
def no_background_turn():
    with patch.object(openhands_chat_router, "process_turn") as mock_turn:
        yield mock_turn


@pytest.fixture
def router_session(db_session):
    """The stream endpoint opens its own session; point it at the test one."""
    with patch.object(
        openhands_chat_router,
        "create_session",
        return_value=_NoCloseSession(db_session),
    ):
        yield db_session


def _fake_turn(reply: str, resumed: bool = True):
    """Patch the SDK boundary: no OpenHands, no MCP, canned reply."""
    mocks = {
        "open_sdk_conversation": MagicMock(return_value=(MagicMock(), resumed)),
        "run_sdk_turn": MagicMock(return_value=(reply, RunUsage(requests=2))),
    }
    return (
        patch.multiple(openhands_chat_agent, **mocks),
        _fake_github(),
        mocks,
    )


def _fake_github():
    return patch.multiple(
        openhands_agent,
        _resolve_github_access=MagicMock(return_value=("tok", "https://mcp")),
        build_mcp_config=MagicMock(return_value={}),
        sync_repositories=MagicMock(),
    )


# region create_conversation / start_turn
def test_create_conversation_creates_workspace_inside_project(
    conversation, sample_project, tmp_path
):
    assert uuid.UUID(conversation.openhands_conversation_id)
    assert conversation.status == openhands_chat_agent.CONVERSATION_STATUS_ACTIVE
    project_workspace = tmp_path / openhands_agent.PROJECTS_DIRNAME / sample_project.id
    assert conversation.workspace_dir == str(project_workspace)
    assert openhands_agent.Path(conversation.workspace_dir).is_dir()
    assert (project_workspace / openhands_agent.REPOS_DIRNAME).is_dir()


def test_start_turn_persists_user_message_run_and_placeholder(db_session, conversation):
    placeholder = openhands_chat_agent.start_turn(db_session, conversation, "oi", "oi")

    messages = (
        db_session.query(OpenHandsMessage).order_by(OpenHandsMessage.created_at).all()
    )
    assert [(m.role, m.status) for m in messages] == [
        ("user", "completed"),
        ("assistant", "processing"),
    ]
    run = db_session.get(AgentRun, placeholder.agent_run_id)
    assert run.status == "running"
    assert run.is_internal is True
    assert run.title == openhands_chat_agent.AGENT_RUN_TITLE


def test_start_turn_without_user_message_only_adds_placeholder(
    db_session, conversation
):
    openhands_chat_agent.start_turn(db_session, conversation, None, "execute")
    assert db_session.query(OpenHandsMessage).filter_by(role="user").count() == 0
    assert db_session.query(OpenHandsMessage).filter_by(role="assistant").count() == 1


# endregion


# region prompts
def test_context_message_includes_transcript_when_session_was_lost(
    sample_project_repository,
):
    repositories = [
        OpenHandsRepository.model_validate(sample_project_repository),
        OpenHandsRepository(full_name="example/other-repo", default_branch="develop"),
    ]
    prior = [
        OpenHandsMessage(role="user", content="quero um endpoint /health"),
        OpenHandsMessage(role="assistant", content="posso usar FastAPI"),
    ]
    text = openhands_chat_agent.build_context_message(repositories, prior)

    assert "example/test-repo" in text
    assert "example/other-repo" in text
    assert "do NOT modify files" in text
    assert "Developer: quero um endpoint /health" in text
    assert "You: posso usar FastAPI" in text
    assert (
        "previous session was lost"
        not in openhands_chat_agent.build_context_message(repositories, [])
    )


def test_context_message_includes_goal_prompt_and_guardrail(sample_project_repository):
    repositories = [OpenHandsRepository.model_validate(sample_project_repository)]
    goal = openhands_goals.MCP_CREATOR

    text = openhands_chat_agent.build_context_message(repositories, [], goal)

    assert goal.context_prompt in text
    assert text.index(goal.context_prompt) < text.index("example/test-repo")

    without_goal = openhands_chat_agent.build_context_message(repositories, [])
    assert goal.context_prompt not in without_goal


def test_extract_pr_metadata_requires_structured_json():
    reply = json.dumps(
        {
            "status": "pr_opened",
            "message": "PR aberto.",
            "branch_name": "estack/openhands/add-mcp-server",
            "pull_request_url": "https://github.com/example/test-repo/pull/12",
            "pull_request_number": 12,
        }
    )

    metadata = openhands_chat_agent.extract_pr_metadata(reply)

    assert metadata.status == openhands_chat_agent.CONVERSATION_STATUS_PR_OPENED
    assert metadata.message == "PR aberto."
    assert metadata.branch_name == "estack/openhands/add-mcp-server"
    assert metadata.pull_request_url == "https://github.com/example/test-repo/pull/12"
    assert metadata.pull_request_number == 12


@pytest.mark.parametrize(
    "reply",
    [
        "Opened https://github.com/example/test-repo/pull/12 on estack/openhands/x",
        json.dumps({"message": "faltou metadata"}),
        json.dumps(
            {
                "status": "pr_opened",
                "message": "PR aberto.",
                "branch_name": "feature/add-mcp-server",
                "pull_request_url": "https://github.com/example/test-repo/pull/12",
                "pull_request_number": 12,
            }
        ),
    ],
)
def test_extract_pr_metadata_rejects_free_text_or_invalid_metadata(reply):
    with pytest.raises(ValueError):
        openhands_chat_agent.extract_pr_metadata(reply)


def test_extract_pr_metadata_accepts_structured_failed_pr():
    reply = json.dumps(
        {
            "status": "pr_failed",
            "message": "No pull request was opened because there are no agreed changes.",
            "branch_name": None,
            "pull_request_url": None,
            "pull_request_number": None,
        }
    )

    metadata = openhands_chat_agent.extract_pr_metadata(reply)

    assert metadata.status == openhands_chat_agent.CONVERSATION_STATUS_PR_FAILED
    assert (
        metadata.message
        == "No pull request was opened because there are no agreed changes."
    )


def test_finish_message_forbids_new_implementation_without_agreed_changes():
    message = openhands_chat_agent.build_finish_message()

    assert "Do not treat this button press as a new instruction" in message
    assert "only contains investigation/planning/questions" in message
    assert '"status": "pr_failed"' in message


# endregion


# endregion


# region SDK session (real SDK, no LLM calls)
def test_open_sdk_conversation_creates_then_resumes(tmp_path, monkeypatch):
    orig_read_text = Path.read_text

    def _safe_read_text(self, encoding=None, errors=None):
        return orig_read_text(self, encoding=encoding or "utf-8", errors=errors)

    monkeypatch.setattr(Path, "read_text", _safe_read_text)

    sdk_id = str(uuid.uuid4())
    chat_dir = tmp_path / "chat"
    working_dir = tmp_path / "project"
    working_dir.mkdir()
    with patch.object(openhands_agent.settings, "openai_api_key", "sk-test"):
        first, resumed = openhands_chat_agent.open_sdk_conversation(
            str(chat_dir), sdk_id, {}, working_dir=str(working_dir)
        )
        first.close()
        second, resumed_again = openhands_chat_agent.open_sdk_conversation(
            str(chat_dir), sdk_id, {}, working_dir=str(working_dir)
        )
        second.close()

    assert (resumed, resumed_again) == (False, True)
    assert str(second.id) == sdk_id
    assert (chat_dir / openhands_chat_agent.PERSISTENCE_DIRNAME).is_dir()
    assert str(second.workspace.working_dir) == str(working_dir)


# endregion


# region process_turn
def test_chat_turn_writes_reply_and_usage(
    db_session, agent_session, conversation, sample_project, tmp_path
):
    placeholder = openhands_chat_agent.start_turn(db_session, conversation, "oi", "oi")

    sdk_patch, mcp_patch, mocks = _fake_turn("olá! já olhei o repositório.")
    with (
        sdk_patch,
        mcp_patch,
        patch.object(openhands_agent, "_workspace_root_dir", return_value=tmp_path),
    ):
        openhands_chat_agent.process_turn(
            conversation.id, placeholder.id, user_message="oi"
        )
        sync = openhands_agent.sync_repositories

    db_session.refresh(placeholder)
    assert placeholder.status == "completed"
    assert placeholder.content == "olá! já olhei o repositório."
    run = db_session.get(AgentRun, placeholder.agent_run_id)
    assert run.status == "finished"
    assert run.requests == 2
    # A resumed session gets the raw user message, no context preamble.
    sent = mocks["run_sdk_turn"].call_args.args[1]
    assert sent == "oi"
    project_workspace = tmp_path / openhands_agent.PROJECTS_DIRNAME / sample_project.id
    mocks["open_sdk_conversation"].assert_called_once_with(
        conversation.workspace_dir,
        conversation.openhands_conversation_id,
        {},
        working_dir=str(project_workspace),
        callbacks=[ANY],
    )
    sync.assert_called_once()
    assert sync.call_args.args[0] == project_workspace
    assert [r.full_name for r in sync.call_args.args[1]] == ["example/test-repo"]
    assert sync.call_args.args[2] == "tok"


def test_extract_completed_thought_reads_only_finalized_action_thought():
    from openhands.sdk.event.llm_convertible import (
        ActionEvent,
        MessageEvent,
        ObservationEvent,
    )
    from openhands.sdk.llm import TextContent

    action = MagicMock(spec=ActionEvent)
    action.thought = [
        TextContent(text="Agora vou localizar o clone."),
        TextContent(text="Depois vou revisar os arquivos."),
    ]
    message = MagicMock(spec=MessageEvent)
    message.reasoning_content = "vou apresentar o resultado"

    assert openhands_chat_agent.extract_completed_thought(action) == (
        "Agora vou localizar o clone. Depois vou revisar os arquivos."
    )
    assert openhands_chat_agent.extract_completed_thought(message) == ""
    assert (
        openhands_chat_agent.extract_completed_thought(MagicMock(spec=ObservationEvent))
        == ""
    )
    assert openhands_chat_agent.extract_completed_thought(MagicMock()) == ""


def test_append_completed_thought_persists_atomic_json_list(
    db_session, agent_session, conversation
):
    placeholder = openhands_chat_agent.start_turn(db_session, conversation, "oi", "oi")
    openhands_chat_agent.register_stream_buffer(placeholder.id)

    openhands_chat_agent.append_completed_thought(placeholder.id, "vou olhar o repo")
    openhands_chat_agent.append_completed_thought(placeholder.id, "encontrei")

    db_session.refresh(placeholder)
    assert json.loads(placeholder.stream_content) == ["vou olhar o repo", "encontrei"]
    assert openhands_chat_agent.get_stream_buffer(placeholder.id) == [
        "vou olhar o repo",
        "encontrei",
    ]

    openhands_chat_agent.clear_stream_buffer(placeholder.id)
    db_session.refresh(placeholder)
    assert placeholder.stream_content is None


def test_chat_turn_publishes_only_complete_event_reasoning(
    db_session, agent_session, conversation
):
    from openhands.sdk.event.llm_convertible import ActionEvent

    placeholder = openhands_chat_agent.start_turn(db_session, conversation, "oi", "oi")
    captured: dict = {}
    seen: list[list[str]] = []

    def _open(*_args, **kwargs):
        captured.update(kwargs)
        return MagicMock(), True

    def _run(*_args, **_kwargs):
        on_event = captured["callbacks"][0]
        seen.append(openhands_chat_agent.get_stream_buffer(placeholder.id))
        action = MagicMock(spec=ActionEvent)
        action.thought = [MagicMock(text="pensamento completo")]
        action.tool_name = "bash"
        on_event(action)
        on_event(MagicMock())
        seen.append(openhands_chat_agent.get_stream_buffer(placeholder.id))
        return "resposta final", RunUsage(requests=1)

    with (
        _fake_github(),
        patch.object(openhands_chat_agent, "open_sdk_conversation", side_effect=_open),
        patch.object(openhands_chat_agent, "run_sdk_turn", side_effect=_run),
    ):
        openhands_chat_agent.process_turn(
            conversation.id, placeholder.id, user_message="oi"
        )

    assert "token_callbacks" not in captured
    assert seen == [[], ["pensamento completo"]]
    db_session.refresh(placeholder)
    assert placeholder.content == "resposta final"
    assert placeholder.stream_content is None


def test_chat_turn_seeds_fresh_session_with_transcript(
    db_session, agent_session, conversation
):
    first = openhands_chat_agent.start_turn(db_session, conversation, "primeira", "x")
    first.content = "resposta anterior"
    first.status = "completed"
    db_session.commit()
    placeholder = openhands_chat_agent.start_turn(
        db_session, conversation, "segunda", "x"
    )

    sdk_patch, mcp_patch, mocks = _fake_turn("ok", resumed=False)
    with sdk_patch, mcp_patch:
        openhands_chat_agent.process_turn(
            conversation.id,
            placeholder.id,
            user_message="segunda",
        )

    sent = mocks["run_sdk_turn"].call_args.args[1]
    assert "Developer: primeira" in sent
    assert "You: resposta anterior" in sent
    assert sent.rstrip().endswith("segunda")
    assert sent.count("segunda") == 1


def test_chat_turn_failure_marks_message_and_run_failed(
    db_session, agent_session, conversation
):
    placeholder = openhands_chat_agent.start_turn(db_session, conversation, "oi", "oi")

    with (
        _fake_github(),
        patch.object(
            openhands_chat_agent,
            "open_sdk_conversation",
            side_effect=RuntimeError("boom"),
        ),
    ):
        openhands_chat_agent.process_turn(
            conversation.id, placeholder.id, user_message="oi"
        )

    db_session.refresh(placeholder)
    assert placeholder.status == "failed"
    assert placeholder.content == openhands_chat_agent.FAILURE_MESSAGE
    assert db_session.get(AgentRun, placeholder.agent_run_id).status == "failed"


def test_finish_turn_stores_structured_pr_metadata(
    db_session, agent_session, conversation
):
    conversation.status = openhands_chat_agent.CONVERSATION_STATUS_OPENING_PR
    placeholder = openhands_chat_agent.start_turn(
        db_session,
        conversation,
        None,
        openhands_chat_agent.build_finish_message(),
    )
    reply = json.dumps(
        {
            "status": "pr_opened",
            "message": "Pull request aberto.",
            "branch_name": "estack/openhands/add-mcp-server",
            "pull_request_url": "https://github.com/example/test-repo/pull/12",
            "pull_request_number": 12,
        }
    )

    sdk_patch, mcp_patch, _mocks = _fake_turn(reply)
    with sdk_patch, mcp_patch:
        openhands_chat_agent.process_turn(
            conversation.id,
            placeholder.id,
            user_message=openhands_chat_agent.build_finish_message(),
            finish_conversation=True,
        )

    db_session.refresh(conversation)
    db_session.refresh(placeholder)
    assert conversation.status == openhands_chat_agent.CONVERSATION_STATUS_PR_OPENED
    assert conversation.branch_name == "estack/openhands/add-mcp-server"
    assert (
        conversation.pull_request_url == "https://github.com/example/test-repo/pull/12"
    )
    assert conversation.pull_request_number == 12
    assert placeholder.status == "completed"
    assert placeholder.content == "Pull request aberto."


def test_finish_turn_marks_structured_no_agreed_changes_as_failed(
    db_session, agent_session, conversation
):
    conversation.status = openhands_chat_agent.CONVERSATION_STATUS_OPENING_PR
    placeholder = openhands_chat_agent.start_turn(
        db_session,
        conversation,
        None,
        openhands_chat_agent.build_finish_message(),
    )
    reply = json.dumps(
        {
            "status": "pr_failed",
            "message": "No pull request was opened because there are no agreed changes.",
            "branch_name": None,
            "pull_request_url": None,
            "pull_request_number": None,
        }
    )

    sdk_patch, mcp_patch, _mocks = _fake_turn(reply)
    with sdk_patch, mcp_patch:
        openhands_chat_agent.process_turn(
            conversation.id,
            placeholder.id,
            user_message=openhands_chat_agent.build_finish_message(),
            finish_conversation=True,
        )

    db_session.refresh(conversation)
    db_session.refresh(placeholder)
    assert conversation.status == openhands_chat_agent.CONVERSATION_STATUS_PR_FAILED
    assert conversation.branch_name is None
    assert conversation.pull_request_url is None
    assert conversation.pull_request_number is None
    assert placeholder.status == "failed"
    assert (
        placeholder.content
        == "No pull request was opened because there are no agreed changes."
    )


def test_finish_turn_rejects_free_text_pr_metadata(
    db_session, agent_session, conversation
):
    conversation.status = openhands_chat_agent.CONVERSATION_STATUS_OPENING_PR
    placeholder = openhands_chat_agent.start_turn(
        db_session,
        conversation,
        None,
        openhands_chat_agent.build_finish_message(),
    )

    sdk_patch, mcp_patch, _mocks = _fake_turn(
        "Opened https://github.com/example/test-repo/pull/12 on estack/openhands/x"
    )
    with sdk_patch, mcp_patch:
        openhands_chat_agent.process_turn(
            conversation.id,
            placeholder.id,
            user_message=openhands_chat_agent.build_finish_message(),
            finish_conversation=True,
        )

    db_session.refresh(conversation)
    db_session.refresh(placeholder)
    assert conversation.status == openhands_chat_agent.CONVERSATION_STATUS_PR_FAILED
    assert conversation.branch_name is None
    assert conversation.pull_request_url is None
    assert conversation.pull_request_number is None
    assert placeholder.status == "failed"
    assert placeholder.content == openhands_chat_agent.PR_METADATA_FAILURE_MESSAGE


# endregion


# region router
def test_create_conversation_with_goal_endpoint(
    client: TestClient,
    project_access_user,
    sample_project,
    sample_project_repository,
    no_background_turn,
    tmp_path,
):
    with patch.object(openhands_agent, "_workspace_root_dir", return_value=tmp_path):
        response = client.post(
            f"/openhands/conversations/projects/{sample_project.id}/goals/mcp_creator",
            json={"message": "quero expor este backend via MCP"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["goal"] == "mcp_creator"
    assert body["name"] == "quero expor este backend via MCP"
    assert body["status"] == openhands_chat_agent.CONVERSATION_STATUS_ACTIVE
    assert [(m["role"], m["status"]) for m in body["messages"]] == [
        ("user", "completed"),
        ("assistant", "processing"),
    ]
    kwargs = no_background_turn.call_args.kwargs
    assert kwargs["conversation_id"] == body["id"]
    assert kwargs["user_message"] == "quero expor este backend via MCP"


def test_create_conversation_with_goal_endpoint_404_when_project_has_no_repositories(
    client: TestClient, project_access_user, sample_project
):
    response = client.post(
        f"/openhands/conversations/projects/{sample_project.id}/goals/mcp_creator",
        json={"message": "oi"},
    )
    assert response.status_code == 404


def test_create_conversation_with_unknown_goal_returns_404(
    client: TestClient, project_access_user, sample_project, sample_project_repository
):
    response = client.post(
        f"/openhands/conversations/projects/{sample_project.id}/goals/not_a_goal",
        json={"message": "oi"},
    )
    assert response.status_code == 404


def test_send_message_endpoint_rejects_turn_in_flight(
    client: TestClient, authenticated_user, db_session, conversation
):
    openhands_chat_agent.start_turn(db_session, conversation, "oi", "oi")

    response = client.post(
        f"/openhands/conversations/{conversation.id}/messages",
        json={"message": "e aí?"},
    )

    assert response.status_code == 409
    assert db_session.query(OpenHandsMessage).filter_by(role="user").count() == 1


def test_create_conversation_normalizes_and_truncates_name(
    client: TestClient,
    project_access_user,
    sample_project,
    sample_project_repository,
    no_background_turn,
    tmp_path,
):
    message = f"  primeira linha\n  {'x' * 120}  "
    with patch.object(openhands_agent, "_workspace_root_dir", return_value=tmp_path):
        response = client.post(
            f"/openhands/conversations/projects/{sample_project.id}/goals/mcp_creator",
            json={"message": message},
        )

    assert response.status_code == 200
    assert response.json()["name"] == f"primeira linha {'x' * 85}"
    assert len(response.json()["name"]) == 100


def test_update_conversation_name(
    client: TestClient, authenticated_user, db_session, conversation
):
    response = client.put(
        f"/openhands/conversations/{conversation.id}",
        json={"name": "  Novo nome  "},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Novo nome"
    db_session.refresh(conversation)
    assert conversation.name == "Novo nome"


@pytest.mark.parametrize("name", ["   ", "x" * 101])
def test_update_conversation_rejects_invalid_name(
    client: TestClient, authenticated_user, db_session, conversation, name
):
    response = client.put(
        f"/openhands/conversations/{conversation.id}", json={"name": name}
    )

    assert response.status_code == 422
    db_session.refresh(conversation)
    assert conversation.name is None


def test_update_conversation_name_is_available_when_busy_or_closed(
    client: TestClient, authenticated_user, db_session, conversation
):
    openhands_chat_agent.start_turn(db_session, conversation, "oi", "oi")
    conversation.status = openhands_chat_agent.CONVERSATION_STATUS_PR_OPENED
    db_session.commit()

    response = client.put(
        f"/openhands/conversations/{conversation.id}", json={"name": "Finalizada"}
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Finalizada"


def test_send_message_endpoint_schedules_turn(
    client: TestClient, authenticated_user, db_session, conversation, no_background_turn
):
    response = client.post(
        f"/openhands/conversations/{conversation.id}/messages",
        json={"message": "e aí?"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "processing"
    kwargs = no_background_turn.call_args.kwargs
    assert kwargs["assistant_message_id"] == response.json()["id"]
    assert kwargs["user_message"] == "e aí?"
    assert kwargs["finish_conversation"] is False


def test_send_message_endpoint_rejects_finalized_conversation(
    client: TestClient, authenticated_user, db_session, conversation
):
    conversation.status = openhands_chat_agent.CONVERSATION_STATUS_PR_OPENED
    db_session.commit()

    response = client.post(
        f"/openhands/conversations/{conversation.id}/messages",
        json={"message": "mais uma"},
    )

    assert response.status_code == 409
    assert db_session.query(OpenHandsMessage).filter_by(role="user").count() == 0


def test_finish_endpoint_schedules_pr_turn(
    client: TestClient, authenticated_user, db_session, conversation, no_background_turn
):
    response = client.post(f"/openhands/conversations/{conversation.id}/finish")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == openhands_chat_agent.CONVERSATION_STATUS_OPENING_PR
    assert [(m["role"], m["status"]) for m in body["messages"]] == [
        ("user", "completed"),
        ("assistant", "processing"),
    ]
    assert body["messages"][0]["content"] == openhands_chat_router.FINISH_USER_MESSAGE
    kwargs = no_background_turn.call_args.kwargs
    assert kwargs["user_message"] == openhands_chat_agent.build_finish_message()
    assert kwargs["finish_conversation"] is True


def test_finish_endpoint_rejects_duplicate_finish(
    client: TestClient, authenticated_user, db_session, conversation, no_background_turn
):
    conversation.status = openhands_chat_agent.CONVERSATION_STATUS_OPENING_PR
    db_session.commit()

    response = client.post(f"/openhands/conversations/{conversation.id}/finish")

    assert response.status_code == 409
    no_background_turn.assert_not_called()


def test_stream_endpoint_emits_done_with_final_content(
    client: TestClient, authenticated_user, db_session, conversation, router_session
):
    placeholder = openhands_chat_agent.start_turn(db_session, conversation, "oi", "oi")
    placeholder.content = "resposta final"
    placeholder.status = "completed"
    db_session.commit()

    response = client.get(
        f"/openhands/conversations/{conversation.id}/stream"
        f"?assistant_message_id={placeholder.id}"
    )

    assert response.status_code == 200
    assert json.loads(response.text.split("data: ")[1]) == {
        "type": "done",
        "content": "resposta final",
    }


def test_stream_endpoint_emits_unseen_thoughts_before_done(
    client: TestClient, authenticated_user, db_session, conversation, router_session
):
    placeholder = openhands_chat_agent.start_turn(db_session, conversation, "oi", "oi")
    placeholder.stream_content = json.dumps(["primeiro", "segundo"])
    placeholder.content = "resposta final"
    placeholder.status = "completed"
    db_session.commit()

    response = client.get(
        f"/openhands/conversations/{conversation.id}/stream"
        f"?assistant_message_id={placeholder.id}&cursor=1"
    )

    events = [
        json.loads(chunk) for chunk in response.text.split("data: ") if chunk.strip()
    ]
    assert events[0] == {"type": "thought", "text": "segundo", "cursor": 2}
    assert events[1]["type"] == "done"


def test_stream_endpoint_times_out_with_an_explicit_event(
    client: TestClient, authenticated_user, db_session, conversation, router_session
):
    placeholder = openhands_chat_agent.start_turn(db_session, conversation, "oi", "oi")

    with patch.object(openhands_chat_router, "STREAM_TIMEOUT", 0):
        response = client.get(
            f"/openhands/conversations/{conversation.id}/stream"
            f"?assistant_message_id={placeholder.id}"
        )

    assert json.loads(response.text.split("data: ")[1]) == {"type": "timeout"}


def test_get_and_list_endpoints_are_scoped_to_the_user(
    client: TestClient, project_access_user, db_session, sample_project, conversation
):
    other_user = User(id=str(uuid.uuid4()), email="other@example.com", is_active=True)
    db_session.add(other_user)
    db_session.commit()
    other = OpenHandsConversation(
        project_id=sample_project.id,
        user_id=other_user.id,
        workspace_dir="/tmp/other",
        goal="mcp_creator",
    )
    db_session.add(other)
    db_session.commit()

    assert client.get(f"/openhands/conversations/{other.id}").status_code == 404
    assert (
        client.put(
            f"/openhands/conversations/{other.id}", json={"name": "Sem acesso"}
        ).status_code
        == 404
    )
    assert client.get(f"/openhands/conversations/{conversation.id}").status_code == 200

    listed = client.get(f"/openhands/conversations/projects/{sample_project.id}")
    assert listed.status_code == 200
    assert [c["id"] for c in listed.json()] == [conversation.id]
    assert listed.json()[0]["name"] is None
    assert listed.json()[0]["status"] == openhands_chat_agent.CONVERSATION_STATUS_ACTIVE
    assert listed.json()[0]["branch_name"] is None
    assert listed.json()[0]["pull_request_url"] is None
    assert listed.json()[0]["pull_request_number"] is None
    assert "messages" not in listed.json()[0]


# endregion

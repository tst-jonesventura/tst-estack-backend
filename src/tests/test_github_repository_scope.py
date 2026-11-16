"""A project may only ever see its own repositories through the GitHub MCP.

The installation token is granted per company, so it reaches every repository the
GitHub App was installed on. Without scoping, a question about project A could be
answered with code from project B.
"""

import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
import requests
from agents.utils import mcp_providers as mp
from database import models
from database.models import OrganizationGitHubAppInstallation
from pydantic_ai.exceptions import ModelRetry
from services import github_app_service
from tests.test_mcp_providers import SCM, _make_company, _make_project

BACKEND = "dev-elumini/dev-estack-backend"
OUTSIDER = "acme/unrelated"
READ_ARGS = {"owner": "dev-elumini", "repo": "dev-estack-backend", "path": "main.py"}


def _add_repository(session, project, org, full_name: str):
    installation = (
        session.query(OrganizationGitHubAppInstallation)
        .filter_by(organization_id=org.id)
        .one()
    )
    owner, name = full_name.split("/")
    repository = models.ProjectRepository(
        id=str(uuid.uuid4()),
        url=f"https://github.com/{full_name}",
        name=name,
        owner=owner,
        language="Python",
        full_name=full_name,
        default_branch="main",
        project_id=project.id,
        organization_github_installation_id=installation.id,
    )
    session.add(repository)
    session.commit()
    return repository


class _FakeWrapped:
    def __init__(self, result=None, instructions=None, error=None):
        self.result = {} if result is None else result
        self.instructions = instructions
        self.error = error
        self.calls = []

    async def call_tool(self, name, tool_args, ctx, tool):
        self.calls.append((name, dict(tool_args)))
        if self.error is not None:
            raise self.error
        return self.result

    async def get_instructions(self, ctx):
        return self.instructions


def _toolset(result=None, repositories=(), instructions=None, error=None):
    return mp.GithubFilterToolset(
        _FakeWrapped(result, instructions, error), repositories=repositories
    )


def _hit(full_name, path="a.py"):
    return {"path": path, "repository": {"full_name": full_name}}


@contextmanager
def _building(accessible=None, list_error=None):
    """Build a real toolset with GitHub's network calls stubbed out."""
    lister = (
        MagicMock(side_effect=list_error)
        if list_error
        else MagicMock(return_value=accessible or set())
    )
    with (
        patch.object(
            github_app_service, "get_installation_access_token", return_value="tok"
        ),
        patch.object(
            github_app_service, "list_accessible_repository_full_names", lister
        ),
        patch.object(mp, "MCPToolset"),
        patch.object(mp, "filter_allowed_tools", return_value=MagicMock()),
        patch.object(mp, "GithubFilterToolset") as mock_wrap,
    ):
        yield mock_wrap


class TestProjectRepositoryLookup:
    def test_returns_the_projects_repositories(self, db_session):
        org = _make_company(db_session)
        project = _make_project(db_session, org)
        _add_repository(db_session, project, org, BACKEND)

        assert mp.project_repository_full_names(db_session, project.id) == (BACKEND,)

    def test_excludes_repositories_of_other_projects(self, db_session):
        org = _make_company(db_session)
        mine = _make_project(db_session, org)
        theirs = _make_project(db_session, org)
        _add_repository(db_session, mine, org, BACKEND)
        _add_repository(db_session, theirs, org, "dev-elumini/other-project")

        assert mp.project_repository_full_names(db_session, mine.id) == (BACKEND,)

    def test_project_without_repository_is_empty(self, db_session):
        org = _make_company(db_session)
        project = _make_project(db_session, org)

        assert mp.project_repository_full_names(db_session, project.id) == ()

    def test_toolset_is_built_with_the_projects_repositories(self, db_session):
        org = _make_company(db_session)
        project = _make_project(db_session, org)
        _add_repository(db_session, project, org, BACKEND)

        with _building(accessible={BACKEND.lower()}) as mock_wrap:
            mp.resolve_mcp_toolset_for_domain(db_session, project.id, SCM, "ask_git")

        assert mock_wrap.call_args.kwargs["repositories"] == (BACKEND,)


class TestSearchScoping:
    @pytest.mark.anyio
    async def test_query_is_restricted_to_the_projects_repositories(self):
        toolset = _toolset({"items": []}, (BACKEND,))

        await toolset.call_tool("search_code", {"query": "JwtStrategy"}, None, None)

        assert toolset.wrapped.calls[0][1]["query"] == f"JwtStrategy repo:{BACKEND}"

    @pytest.mark.anyio
    async def test_an_explicit_scope_from_the_model_is_left_alone(self):
        toolset = _toolset({"items": []}, (BACKEND,))

        await toolset.call_tool(
            "search_code", {"query": f"x repo:{OUTSIDER}"}, None, None
        )

        assert toolset.wrapped.calls[0][1]["query"] == f"x repo:{OUTSIDER}"

    @pytest.mark.anyio
    async def test_hits_from_another_repository_are_dropped(self):
        toolset = _toolset({"items": [_hit(BACKEND), _hit(OUTSIDER)]}, (BACKEND,))

        result = await toolset.call_tool("search_code", {"query": "x"}, None, None)

        assert [i["repository"]["full_name"] for i in result["items"]] == [BACKEND]

    @pytest.mark.anyio
    async def test_provenance_survives_so_the_model_can_check_it(self):
        toolset = _toolset({"items": [_hit(BACKEND)]}, (BACKEND,))

        result = await toolset.call_tool("search_code", {"query": "x"}, None, None)

        assert result["items"][0]["repository"]["full_name"] == BACKEND

    @pytest.mark.anyio
    async def test_a_hit_without_provenance_is_not_trusted(self):
        toolset = _toolset({"items": [{"path": "a.py"}]}, (BACKEND,))

        result = await toolset.call_tool("search_code", {"query": "x"}, None, None)

        assert result["items"] == []

    @pytest.mark.anyio
    async def test_unscoped_project_keeps_the_previous_behaviour(self):
        toolset = _toolset({"items": [_hit(OUTSIDER)]}, ())

        result = await toolset.call_tool("search_code", {"query": "x"}, None, None)

        assert len(result["items"]) == 1
        assert toolset.wrapped.calls[0][1]["query"] == "x"


class TestReadScoping:
    @pytest.mark.anyio
    async def test_reading_another_repository_is_refused(self):
        toolset = _toolset({"content": "segredo"}, (BACKEND,))

        result = await toolset.call_tool(
            "get_file_contents", {"owner": "acme", "repo": "unrelated"}, None, None
        )

        assert "não pertence a este projeto" in result["error"]
        assert toolset.wrapped.calls == []

    @pytest.mark.anyio
    async def test_reading_the_projects_repository_is_allowed(self):
        toolset = _toolset({"content": "ok"}, (BACKEND,))

        result = await toolset.call_tool(
            "get_file_contents",
            {"owner": "dev-elumini", "repo": "dev-estack-backend"},
            None,
            None,
        )

        assert result == {"content": "ok"}

    @pytest.mark.anyio
    async def test_scope_check_ignores_case(self):
        toolset = _toolset({"content": "ok"}, (BACKEND,))

        result = await toolset.call_tool(
            "get_file_contents",
            {"owner": "Dev-Elumini", "repo": "Dev-EStack-Backend"},
            None,
            None,
        )

        assert result == {"content": "ok"}

    @pytest.mark.anyio
    async def test_refusal_names_the_repositories_the_model_may_use(self):
        toolset = _toolset({}, (BACKEND,))

        result = await toolset.call_tool(
            "create_branch", {"owner": "acme", "repo": "unrelated"}, None, None
        )

        assert BACKEND in result["error"]

    @pytest.mark.anyio
    async def test_writes_are_scoped_too(self):
        toolset = _toolset({}, (BACKEND,))

        result = await toolset.call_tool(
            "create_or_update_file",
            {"owner": "acme", "repo": "unrelated", "path": "x"},
            None,
            None,
        )

        assert "error" in result
        assert toolset.wrapped.calls == []


class TestScopeInstruction:
    @pytest.mark.anyio
    async def test_model_is_told_which_repositories_are_in_scope(self):
        toolset = _toolset({}, (BACKEND,))

        assert BACKEND in await toolset.get_instructions(None)

    @pytest.mark.anyio
    async def test_scope_is_appended_to_the_servers_own_instructions(self):
        toolset = _toolset({}, (BACKEND,), instructions="Use as ferramentas.")

        instructions = await toolset.get_instructions(None)

        assert instructions.startswith("Use as ferramentas.")
        assert BACKEND in instructions

    @pytest.mark.anyio
    async def test_unscoped_project_adds_no_instruction(self):
        toolset = _toolset({}, ())

        assert await toolset.get_instructions(None) is None


class TestReachability:
    """A repository the installation cannot see must fail loudly, not silently.

    Registering 'dev-elumini/dev-estack-backend' while the App only reaches
    'EluminiIT/estack-backend' produced empty searches, which the tutor reported
    as "no results found" -- indistinguishable from the code not mentioning it.
    """

    def test_unreachable_repository_is_reported_not_searched(self, db_session):
        org = _make_company(db_session)
        project = _make_project(db_session, org)
        _add_repository(db_session, project, org, "dev-elumini/dev-estack-backend")

        with (
            _building(accessible={"eluminiit/estack-backend"}),
            pytest.raises(mp.McpConfigNotFoundError) as exc,
        ):
            mp.resolve_mcp_toolset_for_domain(db_session, project.id, SCM, "ask_git")

        assert "dev-elumini/dev-estack-backend" in str(exc.value)

    def test_the_error_names_what_the_installation_can_reach(self, db_session):
        org = _make_company(db_session)
        project = _make_project(db_session, org)
        _add_repository(db_session, project, org, "dev-elumini/dev-estack-backend")

        with (
            _building(accessible={"eluminiit/estack-backend"}),
            pytest.raises(mp.McpConfigNotFoundError) as exc,
        ):
            mp.resolve_mcp_toolset_for_domain(db_session, project.id, SCM, "ask_git")

        assert "eluminiit/estack-backend" in str(exc.value)

    def test_unreachable_repositories_are_dropped_from_a_mixed_project(
        self, db_session
    ):
        org = _make_company(db_session)
        project = _make_project(db_session, org)
        _add_repository(db_session, project, org, BACKEND)
        _add_repository(db_session, project, org, "dev-elumini/ghost")

        with _building(accessible={BACKEND.lower()}) as mock_wrap:
            mp.resolve_mcp_toolset_for_domain(db_session, project.id, SCM, "ask_git")

        assert mock_wrap.call_args.kwargs["repositories"] == (BACKEND,)

    def test_reachability_check_ignores_case(self, db_session):
        org = _make_company(db_session)
        project = _make_project(db_session, org)
        _add_repository(db_session, project, org, "Dev-Elumini/Dev-EStack-Backend")

        with _building(accessible={"dev-elumini/dev-estack-backend"}) as mock_wrap:
            mp.resolve_mcp_toolset_for_domain(db_session, project.id, SCM, "ask_git")

        assert mock_wrap.call_args.kwargs["repositories"] == (
            "Dev-Elumini/Dev-EStack-Backend",
        )

    def test_github_being_down_does_not_block_the_agent(self, db_session):
        org = _make_company(db_session)
        project = _make_project(db_session, org)
        _add_repository(db_session, project, org, BACKEND)

        with _building(list_error=requests.ConnectionError("boom")) as mock_wrap:
            mp.resolve_mcp_toolset_for_domain(db_session, project.id, SCM, "ask_git")

        assert mock_wrap.call_args.kwargs["repositories"] == (BACKEND,)

    def test_project_without_repositories_skips_the_check(self, db_session):
        org = _make_company(db_session)
        project = _make_project(db_session, org)

        with _building() as mock_wrap:
            mp.resolve_mcp_toolset_for_domain(db_session, project.id, SCM, "ask_git")

        assert mock_wrap.call_args.kwargs["repositories"] == ()

    def test_the_repository_list_is_fetched_once_per_installation(self, db_session):
        org = _make_company(db_session)
        first = _make_project(db_session, org)
        second = _make_project(db_session, org)
        _add_repository(db_session, first, org, BACKEND)
        _add_repository(db_session, second, org, BACKEND)

        with (
            patch.object(
                github_app_service, "get_installation_access_token", return_value="tok"
            ),
            patch.object(
                github_app_service,
                "list_accessible_repository_full_names",
                return_value={BACKEND.lower()},
            ) as lister,
            patch.object(mp, "MCPToolset"),
            patch.object(mp, "filter_allowed_tools", return_value=MagicMock()),
            patch.object(mp, "GithubFilterToolset"),
        ):
            mp.resolve_mcp_toolset_for_domain(db_session, first.id, SCM, "ask_git")
            mp.resolve_mcp_toolset_for_domain(db_session, second.id, SCM, "ask_git")

        lister.assert_called_once()


class TestEmptySearchIsNotProofOfAbsence:
    """search_code comes back empty on these repos even when the file exists.

    File reads work, so an empty search must steer the model to browse rather
    than let it conclude the code does not contain the thing it looked for.
    """

    @pytest.mark.anyio
    async def test_empty_search_carries_a_next_step(self):
        toolset = _toolset({"items": []}, (BACKEND,))

        result = await toolset.call_tool("search_code", {"query": "x"}, None, None)

        assert "get_file_contents" in result["note"]
        assert "ENTIDADE" in result["note"]

    @pytest.mark.anyio
    async def test_the_note_names_the_repositories_to_browse(self):
        toolset = _toolset({"items": []}, (BACKEND,))

        result = await toolset.call_tool("search_code", {"query": "x"}, None, None)

        assert BACKEND in result["note"]

    @pytest.mark.anyio
    async def test_a_search_emptied_by_scoping_also_gets_the_note(self):
        toolset = _toolset({"items": [_hit(OUTSIDER)]}, (BACKEND,))

        result = await toolset.call_tool("search_code", {"query": "x"}, None, None)

        assert result["items"] == []
        assert "note" in result

    @pytest.mark.anyio
    async def test_results_found_carry_no_note(self):
        toolset = _toolset({"items": [_hit(BACKEND)]}, (BACKEND,))

        result = await toolset.call_tool("search_code", {"query": "x"}, None, None)

        assert "note" not in result

    @pytest.mark.anyio
    async def test_an_error_payload_is_not_annotated(self):
        toolset = _toolset({"error": "boom"}, (BACKEND,))

        result = await toolset.call_tool("search_code", {"query": "x"}, None, None)

        assert "note" not in result

    @pytest.mark.anyio
    async def test_reads_are_still_the_authoritative_path(self):
        toolset = _toolset({"content": "def main():"}, (BACKEND,))

        result = await toolset.call_tool(
            "get_file_contents",
            {"owner": "dev-elumini", "repo": "dev-estack-backend", "path": "main.py"},
            None,
            None,
        )

        assert result == {"content": "def main():"}


class TestScopeInstructionWarnsAboutSearch:
    @pytest.mark.anyio
    async def test_model_is_warned_that_search_may_be_empty(self):
        toolset = _toolset({}, (BACKEND,))

        instructions = await toolset.get_instructions(None)

        assert "get_file_contents" in instructions
        assert "search_code" in instructions


class TestStrategyInstruction:
    """The instruction has to prevent both failure modes at once.

    Repeating a failed search burned 55 tool calls per turn; giving up on the
    first empty one left the tutor with no grounding in the code.
    """

    @pytest.mark.anyio
    async def test_model_is_told_not_to_repeat_a_failed_search(self):
        instructions = await _toolset({}, (BACKEND,)).get_instructions(None)

        assert "nunca repita uma busca" in instructions.lower()

    @pytest.mark.anyio
    async def test_model_is_told_to_switch_to_browsing(self):
        instructions = await _toolset({}, (BACKEND,)).get_instructions(None)

        assert "ENTIDADE" in instructions
        assert "get_file_contents" in instructions

    @pytest.mark.anyio
    async def test_model_is_told_to_answer_with_what_it_read(self):
        instructions = await _toolset({}, (BACKEND,)).get_instructions(None)

        assert "ficou por verificar" in instructions


class TestToolFailuresReachTheModel:
    """A vendor retry must come back as a readable error, not blow up the run."""

    @pytest.mark.anyio
    async def test_a_failed_read_is_reported_as_an_error(self):
        toolset = _toolset(repositories=(BACKEND,), error=ModelRetry("arquivo sumiu"))

        result = await toolset.call_tool(
            "get_file_contents",
            {"owner": "dev-elumini", "repo": "dev-estack-backend", "path": "x.py"},
            None,
            None,
        )

        assert "arquivo sumiu" in result["error"]

    @pytest.mark.anyio
    async def test_a_failed_write_is_reported_as_an_error(self):
        toolset = _toolset(repositories=(BACKEND,), error=ModelRetry("sem permissao"))

        result = await toolset.call_tool(
            "create_or_update_file",
            {"owner": "dev-elumini", "repo": "dev-estack-backend", "path": "x"},
            None,
            None,
        )

        assert "sem permissao" in result["error"]

    @pytest.mark.anyio
    async def test_a_failed_search_tells_the_model_to_stop_retrying(self):
        toolset = _toolset(repositories=(BACKEND,), error=ModelRetry("query invalida"))

        result = await toolset.call_tool("search_code", {"query": "x"}, None, None)

        assert "query invalida" in result["error"]
        assert "Don't retry" in result["error"]

    @pytest.mark.anyio
    async def test_an_unlisted_tool_passes_straight_through(self):
        toolset = _toolset({"ref": "refs/heads/nova"}, (BACKEND,))

        result = await toolset.call_tool(
            "create_branch",
            {"owner": "dev-elumini", "repo": "dev-estack-backend", "branch": "nova"},
            None,
            None,
        )

        assert result == {"ref": "refs/heads/nova"}
        assert toolset.wrapped.calls[0][0] == "create_branch"


class TestScopeAppendedToSequenceInstructions:
    @pytest.mark.anyio
    async def test_scope_is_appended_when_the_server_returns_a_list(self):
        toolset = _toolset({}, (BACKEND,), instructions=["primeira", "segunda"])

        instructions = await toolset.get_instructions(None)

        assert instructions[:2] == ["primeira", "segunda"]
        assert BACKEND in instructions[-1]


class TestListAccessibleRepositories:
    """Reads every page, because an installation can span more than one page."""

    @staticmethod
    def _page(*full_names, status=200):
        response = MagicMock()
        response.status_code = status
        response.json.return_value = {
            "repositories": [{"full_name": name} for name in full_names]
        }
        return response

    def _call(self, pages):
        with patch.object(
            github_app_service.requests, "get", side_effect=pages
        ) as mock_get:
            names = github_app_service.list_accessible_repository_full_names("tok")
        return names, mock_get

    def test_returns_the_repository_names(self):
        names, _ = self._call([self._page(BACKEND, OUTSIDER)])

        assert names == {BACKEND.lower(), OUTSIDER.lower()}

    def test_names_are_lowercased_for_comparison(self):
        names, _ = self._call([self._page("Dev-Elumini/Dev-EStack-Backend")])

        assert names == {"dev-elumini/dev-estack-backend"}

    def test_a_short_page_ends_the_pagination(self):
        _, mock_get = self._call([self._page(BACKEND)])

        mock_get.assert_called_once()

    def test_a_full_page_is_followed_by_another_request(self):
        full = [
            f"org/repo-{i}"
            for i in range(github_app_service.GITHUB_REPOSITORY_PAGE_SIZE)
        ]
        names, mock_get = self._call([self._page(*full), self._page("org/last")])

        assert mock_get.call_count == 2
        assert "org/last" in names

    def test_pagination_stops_at_the_page_cap(self):
        full = [
            f"org/repo-{i}"
            for i in range(github_app_service.GITHUB_REPOSITORY_PAGE_SIZE)
        ]
        pages = [
            self._page(*full)
            for _ in range(github_app_service.GITHUB_MAX_REPOSITORY_PAGES)
        ]

        _, mock_get = self._call(pages)

        assert mock_get.call_count == github_app_service.GITHUB_MAX_REPOSITORY_PAGES

    def test_entries_without_a_full_name_are_skipped(self):
        response = MagicMock()
        response.json.return_value = {
            "repositories": [{"id": 1}, {"full_name": BACKEND}]
        }

        names, _ = self._call([response])

        assert names == {BACKEND.lower()}

    def test_a_payload_without_repositories_yields_nothing(self):
        response = MagicMock()
        response.json.return_value = {}

        names, _ = self._call([response])

        assert names == set()

    def test_an_http_error_propagates(self):
        response = MagicMock()
        response.raise_for_status.side_effect = requests.HTTPError("401")

        with (
            pytest.raises(requests.HTTPError),
            patch.object(github_app_service.requests, "get", return_value=response),
        ):
            github_app_service.list_accessible_repository_full_names("tok")

    def test_every_request_carries_the_token_and_a_timeout(self):
        _, mock_get = self._call([self._page(BACKEND)])

        kwargs = mock_get.call_args.kwargs
        assert kwargs["headers"]["Authorization"] == "Bearer tok"
        assert kwargs["timeout"] == github_app_service.GITHUB_REQUEST_TIMEOUT


class TestNameCollisionGuard:
    """A string match is not proof of implementation.

    Asked about the Tutor feature, the sub-agent matched AgentRun(title="Tutor")
    and described the agent-run machinery instead.
    """

    @pytest.mark.anyio
    async def test_model_is_told_a_name_match_is_weak_evidence(self):
        instructions = await _toolset({}, (BACKEND,)).get_instructions(None)

        assert "NÃO prova que ele a implementa" in instructions

    @pytest.mark.anyio
    async def test_model_is_told_to_confirm_by_opening_the_file(self):
        instructions = await _toolset({}, (BACKEND,)).get_instructions(None)

        assert "abra o arquivo e confirme" in instructions

    @pytest.mark.anyio
    async def test_model_is_told_to_report_ambiguity(self):
        instructions = await _toolset({}, (BACKEND,)).get_instructions(None)

        assert "relate a ambiguidade" in instructions


class _Deps:
    def __init__(self):
        self.file_cache = {}


class _Ctx:
    def __init__(self, deps=None):
        self.deps = deps


class TestReadsAreNotRepeated:
    """87% of a turn's tool calls were get_file_contents, ~27 per ask_git call.

    Each ask_git spawns a fresh sub-agent, so the same entry files were fetched
    again on every question of the same turn.
    """

    ARGS = READ_ARGS

    @pytest.mark.anyio
    async def test_the_same_file_is_fetched_once_per_turn(self):
        toolset = _toolset({"content": "codigo"}, (BACKEND,))
        ctx = _Ctx(_Deps())

        first = await toolset.call_tool("get_file_contents", dict(self.ARGS), ctx, None)
        second = await toolset.call_tool(
            "get_file_contents", dict(self.ARGS), ctx, None
        )

        assert first == second == {"content": "codigo"}
        assert len(toolset.wrapped.calls) == 1

    @pytest.mark.anyio
    async def test_the_cache_is_shared_across_sub_agents_of_a_turn(self):
        deps = _Deps()
        first_agent = _toolset({"content": "codigo"}, (BACKEND,))
        second_agent = _toolset({"content": "outro"}, (BACKEND,))

        await first_agent.call_tool(
            "get_file_contents", dict(self.ARGS), _Ctx(deps), None
        )
        result = await second_agent.call_tool(
            "get_file_contents", dict(self.ARGS), _Ctx(deps), None
        )

        assert result == {"content": "codigo"}
        assert second_agent.wrapped.calls == []

    @pytest.mark.anyio
    async def test_a_different_file_is_still_fetched(self):
        toolset = _toolset({"content": "codigo"}, (BACKEND,))
        ctx = _Ctx(_Deps())

        await toolset.call_tool("get_file_contents", dict(self.ARGS), ctx, None)
        await toolset.call_tool(
            "get_file_contents", {**self.ARGS, "path": "outro.py"}, ctx, None
        )

        assert len(toolset.wrapped.calls) == 2

    @pytest.mark.anyio
    async def test_a_different_ref_is_a_different_file(self):
        toolset = _toolset({"content": "codigo"}, (BACKEND,))
        ctx = _Ctx(_Deps())

        await toolset.call_tool(
            "get_file_contents", {**self.ARGS, "ref": "main"}, ctx, None
        )
        await toolset.call_tool(
            "get_file_contents", {**self.ARGS, "ref": "dev"}, ctx, None
        )

        assert len(toolset.wrapped.calls) == 2

    @pytest.mark.anyio
    async def test_turns_do_not_share_a_cache(self):
        toolset = _toolset({"content": "codigo"}, (BACKEND,))

        await toolset.call_tool(
            "get_file_contents", dict(self.ARGS), _Ctx(_Deps()), None
        )
        await toolset.call_tool(
            "get_file_contents", dict(self.ARGS), _Ctx(_Deps()), None
        )

        assert len(toolset.wrapped.calls) == 2

    @pytest.mark.anyio
    async def test_the_cache_is_bounded(self):
        toolset = _toolset({"content": "codigo"}, (BACKEND,))
        deps = _Deps()
        ctx = _Ctx(deps)

        for i in range(mp.MAX_CACHED_FILE_READS + 20):
            await toolset.call_tool(
                "get_file_contents", {**self.ARGS, "path": f"f{i}.py"}, ctx, None
            )

        assert len(deps.file_cache) == mp.MAX_CACHED_FILE_READS

    @pytest.mark.anyio
    async def test_a_run_without_deps_still_reads(self):
        toolset = _toolset({"content": "codigo"}, (BACKEND,))

        result = await toolset.call_tool(
            "get_file_contents", dict(self.ARGS), None, None
        )

        assert result == {"content": "codigo"}

    @pytest.mark.anyio
    async def test_a_failed_read_is_still_reported_as_an_error(self):
        toolset = _toolset(repositories=(BACKEND,), error=ModelRetry("sumiu"))

        result = await toolset.call_tool(
            "get_file_contents", dict(self.ARGS), _Ctx(_Deps()), None
        )

        assert "sumiu" in result["error"]


class TestReadBudgetInstruction:
    @pytest.mark.anyio
    async def test_model_is_given_a_read_budget(self):
        instructions = await _toolset({}, (BACKEND,)).get_instructions(None)

        assert str(mp.SUGGESTED_FILE_READ_BUDGET) in instructions

    @pytest.mark.anyio
    async def test_model_is_told_not_to_reread(self):
        instructions = await _toolset({}, (BACKEND,)).get_instructions(None)

        assert "Nunca releia um arquivo" in instructions

    @pytest.mark.anyio
    async def test_model_is_told_to_answer_partially_at_the_budget(self):
        instructions = await _toolset({}, (BACKEND,)).get_instructions(None)

        assert "resposta parcial" in instructions

    @pytest.mark.anyio
    async def test_a_read_without_a_path_is_never_cached(self):
        toolset = _toolset({"content": "listagem"}, (BACKEND,))
        ctx = _Ctx(_Deps())
        args = {"owner": "dev-elumini", "repo": "dev-estack-backend"}

        await toolset.call_tool("get_file_contents", dict(args), ctx, None)
        await toolset.call_tool("get_file_contents", dict(args), ctx, None)

        assert len(toolset.wrapped.calls) == 2
        assert ctx.deps.file_cache == {}


class TestHitShapesFromTheRealServer:
    """The MCP server does not always describe a hit's repository as an object.

    Production crashed with AttributeError: 'str' object has no attribute 'get'
    while generating a development plan, because every test here used a dict.
    """

    @pytest.mark.anyio
    async def test_a_repository_given_as_a_string_is_kept(self):
        toolset = _toolset({"items": [{"path": "a.py", "repository": BACKEND}]}, (BACKEND,))

        result = await toolset.call_tool("search_code", {"query": "x"}, None, None)

        assert len(result["items"]) == 1

    @pytest.mark.anyio
    async def test_a_string_from_another_repository_is_dropped(self):
        toolset = _toolset({"items": [{"path": "a.py", "repository": OUTSIDER}]}, (BACKEND,))

        result = await toolset.call_tool("search_code", {"query": "x"}, None, None)

        assert result["items"] == []

    @pytest.mark.anyio
    async def test_a_bare_repository_name_is_matched(self):
        """Some responses carry only the name, without the owner."""
        toolset = _toolset(
            {"items": [{"path": "a.py", "repository": "dev-estack-backend"}]}, (BACKEND,)
        )

        result = await toolset.call_tool("search_code", {"query": "x"}, None, None)

        assert len(result["items"]) == 1

    @pytest.mark.anyio
    async def test_a_bare_name_of_another_repository_is_dropped(self):
        toolset = _toolset({"items": [{"path": "a.py", "repository": "unrelated"}]}, (BACKEND,))

        result = await toolset.call_tool("search_code", {"query": "x"}, None, None)

        assert result["items"] == []

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "item",
        ["texto solto", 42, None, {"path": "a.py"}, {"repository": None},
         {"repository": []}, {"repository": {"id": 1}}],
    )
    async def test_a_malformed_hit_never_raises(self, item):
        toolset = _toolset({"items": [item]}, (BACKEND,))

        result = await toolset.call_tool("search_code", {"query": "x"}, None, None)

        assert result["items"] == []

    @pytest.mark.anyio
    async def test_items_that_are_not_a_list_are_left_alone(self):
        toolset = _toolset({"items": "nenhum resultado"}, (BACKEND,))

        result = await toolset.call_tool("search_code", {"query": "x"}, None, None)

        assert result["items"] == "nenhum resultado"

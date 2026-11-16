import uuid
from unittest.mock import MagicMock, patch

import pytest
import requests
from sqlalchemy import text

from agents.utils import mcp_providers as mp
from database import models
from database.models import (
    Organization,
    OrganizationGithubMcpConfig,
    OrganizationGitHubAppInstallation,
    OrganizationMcpIntegration,
    Project,
)
from services import github_app_service
from utils.crypto import DecryptionError, decrypt_value, encrypt_value

SCM = models.McpDomain.SCM.value


# region fixtures / helpers
def _make_company(
    session,
    *,
    mcp_provider: str | None = "github",
    with_config: bool = True,
    with_installation: bool = True,
) -> Organization:
    org = Organization(
        id=str(uuid.uuid4()),
        name="Elumini-sa",
        description="Empresa de tecnologia",
        mcp_provider=mcp_provider,
        is_active=True,
    )
    session.add(org)
    session.flush()

    if with_config:
        session.add(
            OrganizationGithubMcpConfig(
                id=str(uuid.uuid4()),
                organization_id=org.id,
                github_app_id="123456",
                github_app_private_key_base64="cHJpdmF0ZS1rZXk=",
                mcp_url="https://api.githubcopilot.com/mcp/",
            )
        )
    if with_installation:
        session.add(
            OrganizationGitHubAppInstallation(
                id=str(uuid.uuid4()),
                organization_id=org.id,
                github_app_installation_id=987654,
                github_account_login="elumini",
            )
        )
    session.commit()
    return org


def _make_project(session, org: Organization) -> Project:
    project = Project(
        id=str(uuid.uuid4()),
        name="Proj",
        organization_id=org.id,
        is_active=True,
    )
    session.add(project)
    session.commit()
    return project


# endregion


class TestEncryption:
    def test_roundtrip(self):
        token = encrypt_value("super-secret")
        assert token != "super-secret"
        assert decrypt_value(token) == "super-secret"

    def test_encrypted_at_rest_and_decrypted_on_load(self, db_session):
        org = _make_company(db_session)
        raw = db_session.execute(
            text(
                "SELECT github_app_id, github_app_private_key_base64 "
                "FROM organization_github_mcp_config WHERE organization_id = :oid"
            ),
            {"oid": org.id},
        ).one()
        # Stored ciphertext must not equal the plaintext values.
        assert raw[0] != "123456"
        assert raw[1] != "cHJpdmF0ZS1rZXk="

        config = (
            db_session.query(OrganizationGithubMcpConfig)
            .filter_by(organization_id=org.id)
            .one()
        )
        assert config.github_app_id == "123456"
        assert config.github_app_private_key_base64 == "cHJpdmF0ZS1rZXk="

    def test_decrypt_with_wrong_key_raises(self):
        from cryptography.fernet import Fernet

        import utils.crypto as crypto

        token = encrypt_value("value")
        other = Fernet.generate_key().decode()
        with patch.object(crypto.settings, "config_encryption_key", other):
            with pytest.raises(DecryptionError):
                decrypt_value(token)


class TestToolPolicies:
    @pytest.mark.parametrize(
        "agent_name,expected",
        [
            (
                "ask_git",
                ["get_file_contents", "search_code"],
            ),
            (
                "code_git",
                [
                    "get_file_contents",
                    "search_code",
                    "create_branch",
                    "create_or_update_file",
                    "push_files",
                    "create_pull_request",
                ],
            ),
            ("unit_test_agent", ["create_pull_request"]),
            ("some_other_agent", ["search_code"]),
        ],
    )
    def test_allowed_tools(self, agent_name, expected):
        assert mp.GithubMcpProvider().get_allowed_tools(agent_name) == expected

    def test_read_only_agent_cannot_write(self):
        allowed = mp.GithubMcpProvider().get_allowed_tools("ask_git")
        for write_tool in (
            "create_branch",
            "create_or_update_file",
            "push_files",
            "create_pull_request",
        ):
            assert write_tool not in allowed

    def test_policy_names_no_tool_the_server_never_exposed(self):
        """get_repository_tree was in the policy but never once callable.

        filter_allowed_tools drops names the server does not offer, so the agent
        silently had two tools while its prompt pointed at a third.
        """
        for agent_name in ("ask_git", "code_git", "unit_test_agent"):
            allowed = mp.GithubMcpProvider().get_allowed_tools(agent_name)
            assert "get_repository_tree" not in allowed

    def test_reading_agents_can_still_search_and_read(self):
        allowed = mp.GithubMcpProvider().get_allowed_tools("ask_git")
        assert allowed == ["get_file_contents", "search_code"]


class TestRegistry:
    def test_github_provider_resolved(self):
        provider = mp.McpProviderRegistry.get("github")
        assert isinstance(provider, mp.GithubMcpProvider)

    def test_github_provider_serves_scm_domain(self):
        provider = mp.McpProviderRegistry.get("github", models.McpDomain.SCM.value)
        assert isinstance(provider, mp.GithubMcpProvider)
        assert provider.domain == models.McpDomain.SCM.value

    def test_missing_provider_raises(self):
        with pytest.raises(mp.McpProviderNotConfiguredError):
            mp.McpProviderRegistry.get(None)

    def test_unsupported_provider_raises(self):
        with pytest.raises(mp.McpProviderNotConfiguredError):
            mp.McpProviderRegistry.get("gitlab")

    def test_provider_valid_in_another_domain_is_rejected(self):
        with pytest.raises(mp.McpProviderNotConfiguredError):
            mp.McpProviderRegistry.get("github", models.McpDomain.WIKI.value)

    def test_scm_and_database_have_implemented_providers_today(self):
        assert mp.McpProviderRegistry.configured_domains() == {
            models.McpDomain.SCM.value,
            models.McpDomain.DATABASE.value,
        }


class TestResolveToolset:
    def test_project_not_found(self, db_session):
        with pytest.raises(mp.McpConfigNotFoundError):
            mp.resolve_mcp_toolset_for_domain(
                db_session, "does-not-exist", SCM, "code_git"
            )

    def test_company_without_provider(self, db_session):
        org = _make_company(db_session, mcp_provider=None, with_config=False,
                            with_installation=False)
        project = _make_project(db_session, org)
        with pytest.raises(mp.McpProviderNotConfiguredError):
            mp.resolve_mcp_toolset_for_domain(db_session, project.id, SCM, "code_git")

    def test_domain_without_integration(self, db_session):
        org = _make_company(db_session)
        project = _make_project(db_session, org)
        with pytest.raises(mp.McpProviderNotConfiguredError):
            mp.resolve_mcp_toolset_for_domain(
                db_session, project.id, models.McpDomain.WIKI.value, "tutor"
            )

    def test_config_missing(self, db_session):
        org = _make_company(db_session, with_config=False)
        project = _make_project(db_session, org)
        with pytest.raises(mp.McpConfigNotFoundError):
            mp.resolve_mcp_toolset_for_domain(db_session, project.id, SCM, "code_git")

    def test_installation_missing(self, db_session):
        org = _make_company(db_session, with_installation=False)
        project = _make_project(db_session, org)
        with pytest.raises(mp.McpConfigNotFoundError):
            mp.resolve_mcp_toolset_for_domain(db_session, project.id, SCM, "code_git")

    def test_token_error_wrapped_as_connection_error(self, db_session):
        org = _make_company(db_session)
        project = _make_project(db_session, org)
        with patch.object(
            github_app_service,
            "get_installation_access_token",
            side_effect=requests.RequestException("boom"),
        ):
            with pytest.raises(mp.McpConnectionError):
                mp.resolve_mcp_toolset_for_domain(
                    db_session, project.id, SCM, "code_git"
                )

    def test_happy_path_builds_toolset_with_policy(self, db_session):
        org = _make_company(db_session)
        project = _make_project(db_session, org)

        built = MagicMock(name="mcp_server")
        filtered = MagicMock(name="filtered_toolset")
        wrapped = MagicMock(name="github_filter_toolset")

        with patch.object(
            github_app_service, "get_installation_access_token", return_value="tok-123"
        ) as mock_token, patch.object(
            mp, "MCPToolset", return_value=built
        ) as mock_server, patch.object(
            mp, "filter_allowed_tools", return_value=filtered
        ) as mock_filter, patch.object(
            mp, "GithubFilterToolset", return_value=wrapped
        ) as mock_wrap:
            result = mp.resolve_mcp_toolset_for_domain(
                db_session, project.id, SCM, "code_git"
            )

        # per-company credentials are passed through to token exchange
        mock_token.assert_called_once_with(
            app_id=123456,
            private_key_base64="cHJpdmF0ZS1rZXk=",
            installation_id=987654,
        )
        # connection built against the configured MCP url with the bearer token
        mock_server.assert_called_once_with(
            "https://api.githubcopilot.com/mcp/",
            headers={"Authorization": "Bearer tok-123"},
            init_timeout=mp.MCP_INIT_TIMEOUT_SECONDS,
            read_timeout=mp.MCP_READ_TIMEOUT_SECONDS,
        )
        # code_git policy applied
        mock_filter.assert_called_once_with(
            built, mp.GithubMcpProvider().get_allowed_tools("code_git")
        )
        mock_wrap.assert_called_once_with(filtered, repositories=())
        assert result is wrapped


class TestDialectPortability:

    @staticmethod
    def _compiled_for_mssql(db_session, org):
        from sqlalchemy.dialects import mssql

        query = (
            db_session.query(OrganizationMcpIntegration)
            .filter(
                OrganizationMcpIntegration.organization_id == org.id,
                OrganizationMcpIntegration.domain == SCM,
                OrganizationMcpIntegration.is_active == True,  # noqa: E712
            )
        )
        return str(query.statement.compile(dialect=mssql.dialect()))

    def test_boolean_filter_is_valid_tsql(self, db_session):
        org = _make_company(db_session)

        sql = self._compiled_for_mssql(db_session, org)

        assert "IS 1" not in sql
        assert "is_active = 1" in sql

    def test_provider_lookup_runs_on_a_boolean_column(self, db_session):
        org = _make_company(db_session, mcp_provider=None)
        db_session.add(
            OrganizationMcpIntegration(
                id=str(uuid.uuid4()),
                organization_id=org.id,
                domain=SCM,
                provider="github",
                is_active=True,
            )
        )
        db_session.commit()

        assert mp.get_organization_provider_name(db_session, org, SCM) == "github"


class TestDomainRouting:
    def _add_integration(self, session, org, domain, provider, is_active=True):
        integration = OrganizationMcpIntegration(
            id=str(uuid.uuid4()),
            organization_id=org.id,
            domain=domain,
            provider=provider,
            is_active=is_active,
        )
        session.add(integration)
        session.commit()
        return integration

    def test_integration_row_resolves_provider(self, db_session):
        org = _make_company(db_session, mcp_provider=None)
        self._add_integration(db_session, org, SCM, "github")
        assert mp.get_organization_provider_name(db_session, org, SCM) == "github"

    def test_integration_takes_precedence_over_legacy_column(self, db_session):
        org = _make_company(db_session, mcp_provider="stale-legacy-value")
        self._add_integration(db_session, org, SCM, "github")
        assert mp.get_organization_provider_name(db_session, org, SCM) == "github"

    def test_legacy_column_used_when_no_integration_row(self, db_session):
        org = _make_company(db_session, mcp_provider="github")
        assert mp.get_organization_provider_name(db_session, org, SCM) == "github"

    def test_legacy_column_does_not_leak_into_other_domains(self, db_session):
        org = _make_company(db_session, mcp_provider="github")
        assert (
            mp.get_organization_provider_name(
                db_session, org, models.McpDomain.WIKI.value
            )
            is None
        )

    def test_inactive_integration_is_ignored(self, db_session):
        org = _make_company(db_session, mcp_provider=None)
        self._add_integration(db_session, org, SCM, "github", is_active=False)
        assert mp.get_organization_provider_name(db_session, org, SCM) is None

    def test_domains_are_independent(self, db_session):
        org = _make_company(db_session, mcp_provider=None)
        self._add_integration(db_session, org, SCM, "github")
        assert mp.get_organization_provider_name(db_session, org, SCM) == "github"
        assert (
            mp.get_organization_provider_name(
                db_session, org, models.McpDomain.BACKLOG.value
            )
            is None
        )


class TestBackfillMcpIntegrations:
    @staticmethod
    def _run(db_session):
        from contextlib import contextmanager

        from scripts import backfill_mcp_integrations as script

        @contextmanager
        def _session_factory():
            yield db_session

        with patch.object(script, "create_session", _session_factory):
            return script.backfill()

    def _integrations(self, db_session, org):
        return (
            db_session.query(OrganizationMcpIntegration)
            .filter_by(organization_id=org.id)
            .all()
        )

    def test_creates_scm_integration_from_legacy_column(self, db_session):
        org = _make_company(db_session, mcp_provider="github")

        created = self._run(db_session)

        assert created == 1
        integrations = self._integrations(db_session, org)
        assert len(integrations) == 1
        assert integrations[0].domain == SCM
        assert integrations[0].provider == "github"
        assert integrations[0].is_active is True

    def test_is_idempotent(self, db_session):
        org = _make_company(db_session, mcp_provider="github")

        assert self._run(db_session) == 1
        assert self._run(db_session) == 0
        assert len(self._integrations(db_session, org)) == 1

    def test_company_without_legacy_provider_is_untouched(self, db_session):
        org = _make_company(
            db_session, mcp_provider=None, with_config=False, with_installation=False
        )

        assert self._run(db_session) == 0
        assert self._integrations(db_session, org) == []


class TestGithubTokenAndRepositories:
    def test_get_installation_access_token(self):
        response = MagicMock()
        response.json.return_value = {"token": "ghs_abc"}
        response.raise_for_status.return_value = None
        with patch.object(github_app_service, "requests") as mock_requests, patch.object(
            github_app_service, "build_app_jwt", return_value="jwt-xyz"
        ):
            mock_requests.post.return_value = response
            token = github_app_service.get_installation_access_token(
                app_id=1, private_key_base64="a2V5", installation_id=42
            )
        assert token == "ghs_abc"
        called_url = mock_requests.post.call_args[0][0]
        assert "installations/42/access_tokens" in called_url

    def test_list_repositories_with_token(self):
        response = MagicMock()
        response.json.return_value = {
            "total_count": 2,
            "repositories": [{"name": "repo-a"}, {"name": "repo-b"}],
        }
        response.raise_for_status.return_value = None
        with patch.object(github_app_service, "requests") as mock_requests:
            mock_requests.get.return_value = response
            result = github_app_service.list_repositories_with_token("tok")
        assert result["total_count"] == 2
        assert [r["name"] for r in result["repositories"]] == ["repo-a", "repo-b"]

    def test_company_connection_and_repositories_flow(self, db_session):
        """End-to-end (mocked network): resolve token for the company, list
        repositories, assert the connection was accepted and repos returned."""
        org = _make_company(db_session)
        provider = mp.GithubMcpProvider()

        repos_response = MagicMock()
        repos_response.json.return_value = {
            "total_count": 1,
            "repositories": [{"name": "estack-backend"}],
        }
        repos_response.raise_for_status.return_value = None

        with patch.object(
            github_app_service, "get_installation_access_token", return_value="tok-xyz"
        ):
            token, mcp_url = provider.get_access_token(db_session, org)

        assert token == "tok-xyz"
        assert mcp_url == "https://api.githubcopilot.com/mcp/"

        with patch.object(github_app_service, "requests") as mock_requests:
            mock_requests.get.return_value = repos_response
            repos = github_app_service.list_repositories_with_token(token)

        assert repos["total_count"] == 1
        assert repos["repositories"][0]["name"] == "estack-backend"


class TestGithubRequestTimeouts:
    """A hung GitHub connection holds an anyio threadpool slot indefinitely."""

    def test_token_exchange_sends_a_timeout(self):
        response = MagicMock()
        response.json.return_value = {"token": "ghs_abc"}
        response.raise_for_status.return_value = None
        with patch.object(github_app_service, "requests") as mock_requests, patch.object(
            github_app_service, "build_app_jwt", return_value="jwt"
        ):
            mock_requests.post.return_value = response
            github_app_service.get_installation_access_token(
                app_id=1, private_key_base64="a2V5", installation_id=42
            )

        assert (
            mock_requests.post.call_args.kwargs["timeout"]
            == github_app_service.GITHUB_REQUEST_TIMEOUT
        )

    def test_installation_lookup_sends_a_timeout(self):
        response = MagicMock()
        response.json.return_value = {"account": {"login": "elumini"}}
        with patch.object(github_app_service, "requests") as mock_requests, patch.object(
            github_app_service, "build_app_jwt", return_value="jwt"
        ):
            mock_requests.get.return_value = response
            github_app_service.get_github_installation_by_id(
                42, app_id=1, private_key_base64="a2V5"
            )

        assert (
            mock_requests.get.call_args.kwargs["timeout"]
            == github_app_service.GITHUB_REQUEST_TIMEOUT
        )

    def test_repository_listing_sends_a_timeout(self):
        response = MagicMock()
        response.json.return_value = {"total_count": 0, "repositories": []}
        response.raise_for_status.return_value = None
        with patch.object(github_app_service, "requests") as mock_requests:
            mock_requests.get.return_value = response
            github_app_service.list_repositories_with_token("tok")

        assert (
            mock_requests.get.call_args.kwargs["timeout"]
            == github_app_service.GITHUB_REQUEST_TIMEOUT
        )

    def test_timeout_covers_connect_and_read(self):
        connect, read = github_app_service.GITHUB_REQUEST_TIMEOUT
        assert connect > 0
        assert read > 0


class TestGithubAppCredentialsFromDatabase:
    """The GitHub App management endpoints must source credentials from the
    encrypted table, never from environment settings."""

    def test_load_credentials_decrypts_from_database(self, db_session):
        org = _make_company(db_session)
        app_id, private_key_base64 = github_app_service.load_github_app_credentials(
            db_session, org.id
        )
        assert app_id == 123456
        assert private_key_base64 == "cHJpdmF0ZS1rZXk="

    def test_load_credentials_missing_config_raises(self, db_session):
        org = _make_company(db_session, with_config=False)
        with pytest.raises(github_app_service.GithubAppCredentialsNotFoundError):
            github_app_service.load_github_app_credentials(db_session, org.id)

    def test_get_installation_by_id_uses_explicit_credentials(self):
        response = MagicMock()
        response.json.return_value = {"account": {"login": "elumini"}}
        with patch.object(github_app_service, "requests") as mock_requests, patch.object(
            github_app_service, "build_app_jwt", return_value="jwt-db"
        ) as mock_jwt:
            mock_requests.get.return_value = response
            result = github_app_service.get_github_installation_by_id(
                42, app_id=123456, private_key_base64="a2V5"
            )
        mock_jwt.assert_called_once_with(123456, "a2V5")
        assert result["account"]["login"] == "elumini"

    def test_get_project_repositories_uses_explicit_credentials(self):
        repos_response = MagicMock()
        repos_response.json.return_value = {
            "total_count": 1,
            "repositories": [{"name": "estack-backend"}],
        }
        repos_response.raise_for_status.return_value = None
        with patch.object(
            github_app_service, "get_installation_access_token", return_value="tok-db"
        ) as mock_token, patch.object(
            github_app_service, "requests"
        ) as mock_requests:
            mock_requests.get.return_value = repos_response
            result = github_app_service.get_project_github_repositories(
                42, app_id=123456, private_key_base64="a2V5"
            )
        mock_token.assert_called_once_with(123456, "a2V5", 42)
        assert result["repositories"][0]["name"] == "estack-backend"


class TestMcpTimeouts:
    """A tool call must have a deadline.

    Two dev-steps runs sat silent for 41 minutes on a get_file_contents that never
    returned, each holding a thread from the pool the whole application shares.
    """

    def test_the_git_toolset_is_built_with_timeouts(self, db_session):
        org = _make_company(db_session)
        project = _make_project(db_session, org)

        with patch.object(
            github_app_service, "get_installation_access_token", return_value="tok"
        ), patch.object(mp, "MCPToolset") as mock_server, patch.object(
            mp, "filter_allowed_tools", return_value=MagicMock()
        ), patch.object(mp, "GithubFilterToolset"):
            mp.resolve_mcp_toolset_for_domain(db_session, project.id, SCM, "ask_git")

        kwargs = mock_server.call_args.kwargs
        assert kwargs["init_timeout"] == mp.MCP_INIT_TIMEOUT_SECONDS
        assert kwargs["read_timeout"] == mp.MCP_READ_TIMEOUT_SECONDS

    def test_the_read_deadline_is_bounded(self):
        assert 0 < mp.MCP_READ_TIMEOUT_SECONDS <= 120

    def test_the_connect_deadline_is_shorter_than_the_read_one(self):
        assert mp.MCP_INIT_TIMEOUT_SECONDS < mp.MCP_READ_TIMEOUT_SECONDS

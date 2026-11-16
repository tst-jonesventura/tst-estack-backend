"""MCP provider abstraction (Strategy pattern), routed by knowledge domain.

An agent asks for a domain of knowledge (source code, backlog, wiki, ...), never
for a vendor. :func:`resolve_mcp_toolset_for_domain` looks up the project's
company, finds which provider is configured for that domain, and delegates to
the matching :class:`McpProvider` strategy.

Adding a new MCP provider means adding a new strategy + config table and
registering it — existing providers are untouched (Open/Closed principle).
"""

import fnmatch
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from pydantic_ai import FunctionToolset, RunContext, Tool, ToolsetTool, WrapperToolset
from pydantic_ai.exceptions import ModelRetry

from database import models

try:
    from pydantic_ai.mcp import MCPToolset
except ImportError:
    MCPToolset = None
from sqlalchemy.orm import Session
from utils import log
from utils.crypto import DecryptionError, EncryptionKeyMissingError
from utils.ttl_cache import TTLCache

logger = log.setup_custom_logger(__name__)

# GitHub installation tokens are valid for an hour; expire ours early so a token is
# never handed out close to its own expiry.
INSTALLATION_TOKEN_TTL_SECONDS = 50 * 60
_installation_token_cache: TTLCache[str] = TTLCache(INSTALLATION_TOKEN_TTL_SECONDS)

ACCESSIBLE_REPOSITORIES_TTL_SECONDS = 10 * 60
_accessible_repositories_cache: TTLCache[set[str]] = TTLCache(
    ACCESSIBLE_REPOSITORIES_TTL_SECONDS
)

MAX_REPORTED_REPOSITORIES = 20

NO_ACCESSIBLE_REPOSITORY_MESSAGE = (
    "A instalação do GitHub App não alcança nenhum dos repositórios configurados "
    "para este projeto ({configured}). Repositórios que a instalação alcança: "
    "{accessible}. Verifique se o App está instalado na organização correta e se "
    "esses repositórios foram liberados para ele."
)

# Without these a tool call waits on the MCP server forever: two dev-steps runs sat
# silent for 41 minutes on a get_file_contents that never returned, each holding a
# thread from the pool the whole application shares.
MCP_INIT_TIMEOUT_SECONDS = 20.0
MCP_READ_TIMEOUT_SECONDS = 60.0

SCOPE_QUALIFIERS = ("repo:", "org:", "user:")
QUERY_ARG_NAMES = ("query", "q")
RETRY_AS_ERROR_TOOLS = ("create_or_update_file",)
MAX_SCOPED_REPOSITORIES = 5
MAX_CACHED_FILE_READS = 150
SUGGESTED_FILE_READ_BUDGET = 10

OUT_OF_SCOPE_MESSAGE = (
    "O repositório '{requested}' não pertence a este projeto e não foi acessado. "
    "Repositórios deste projeto: {allowed}."
)

REPOSITORY_SCOPE_INSTRUCTION = (
    "Os repositórios deste projeto são: {repositories}. Toda busca e leitura de "
    "código deve ficar restrita a eles. Resultados de outros repositórios são "
    "descartados antes de chegarem até você, então nunca afirme nada sobre o "
    "código com base em outro repositório.\n\n"
    "Você tem duas ferramentas: 'search_code' para localizar e 'get_file_contents' "
    "para ler. A leitura é a fonte confiável: o que você leu, pode afirmar. A busca "
    "é frágil nestes repositórios e pode voltar vazia mesmo quando o arquivo "
    "existe, então uma busca vazia nunca prova ausência.\n\n"
    "Busque pela ENTIDADE, não pelo nome da funcionalidade. O nome de uma "
    "funcionalidade aparece espalhado por código que apenas a menciona (títulos, "
    "rótulos, constantes), enquanto a entidade que a implementa tem nome próprio: "
    "para a funcionalidade 'Tutor', procure 'TutorMessage' ou 'TutorConversation', "
    "não 'Tutor'. Para perguntas sobre estados, campos ou dados de uma entidade, vá "
    "direto aos arquivos de modelo, schema ou tipos: é onde a resposta está, e "
    "costuma bastar um arquivo.\n\n"
    "Um trecho conter o nome de uma funcionalidade NÃO prova que ele a implementa. "
    "Uma string igual ao nome procurado é pista fraca: abra o arquivo e confirme "
    "antes de afirmar qualquer coisa. Se a evidência ficar ambígua entre duas "
    "funcionalidades, relate a ambiguidade em vez de escolher uma delas.\n\n"
    "Estratégia: nunca repita uma busca que já falhou com os mesmos termos, mude os "
    "termos ou passe a ler arquivos. Antes de abrir um arquivo, decida o que espera "
    "encontrar nele; abrir arquivos por tentativa gasta o orçamento sem aproximar "
    "da resposta. Nunca releia um arquivo que já leu.\n\n"
    "Orçamento: você tem cerca de {budget} leituras. Com uma boa hipótese, três ou "
    "quatro bastam. Ao atingir o limite, responda com o que tem e diga "
    "explicitamente o que ficou por verificar: uma resposta parcial e honesta vale "
    "mais que uma varredura."
)

SEARCH_EMPTY_NOTE = (
    "A busca não retornou resultados, o que NÃO significa que o código não exista: "
    "a busca por texto é pouco confiável nestes repositórios. Tente de novo com o "
    "nome da ENTIDADE (uma classe, um modelo, um tipo) em vez do nome da "
    "funcionalidade, ou leia diretamente com 'get_file_contents' os arquivos de "
    "modelo, schema ou tipos de {repositories}. Não afirme ausência a partir desta "
    "busca."
)


# region Errors
class McpConfigurationError(Exception):
    """Base error for MCP configuration/connection problems."""


class McpProviderNotConfiguredError(McpConfigurationError):
    """The company has no (supported) MCP provider configured."""


class McpConfigNotFoundError(McpConfigurationError):
    """The provider is selected but its configuration/credentials are missing."""


class McpConnectionError(McpConfigurationError):
    """Failed to authenticate or build the MCP connection."""


# endregion


# region Toolset filtering helpers
def filter_allowed_tools(toolset, allowed_tools: list[str]):
    return toolset.filtered(lambda ctx, tool_def: tool_def.name in allowed_tools)


def remove_disallowed_tools(toolset, disallowed_tools: list[str]):
    return toolset.filtered(lambda ctx, tool_def: tool_def.name not in disallowed_tools)


@dataclass
class GithubFilterToolset(WrapperToolset):
    """Confines a company-wide GitHub installation to one project's repositories.

    The installation token reaches every repository the GitHub App was granted,
    which is wider than the project asking the question. This toolset narrows
    searches to ``repositories``, rejects reads aimed elsewhere, and keeps the
    ``repository`` field on every hit so provenance survives to the model.
    """

    repositories: tuple[str, ...] = ()

    @property
    def _allowed(self) -> set[str]:
        return {name.lower() for name in self.repositories}

    def _requested_repository(self, tool_args: dict[str, Any]) -> str | None:
        owner, repo = tool_args.get("owner"), tool_args.get("repo")
        return f"{owner}/{repo}" if owner and repo else None

    def _scoped_query(self, query: str) -> str:
        if any(qualifier in query for qualifier in SCOPE_QUALIFIERS):
            return query
        scope = " ".join(
            f"repo:{name}"
            for name in sorted(self.repositories)[:MAX_SCOPED_REPOSITORIES]
        )
        return f"{query} {scope}".strip()

    @property
    def _allowed_short_names(self) -> set[str]:
        return {name.rsplit("/", 1)[-1].lower() for name in self.repositories}

    @staticmethod
    def _repository_label(item: Any) -> str | None:
        """Which repository a hit came from, whatever shape the server used.

        The GitHub MCP returns this as an object for some responses and as a plain
        string for others; assuming the object shape crashed a production run.
        """
        if not isinstance(item, dict):
            return None
        repository = item.get("repository")
        if isinstance(repository, str):
            return repository
        if isinstance(repository, dict):
            for key in ("full_name", "fullName", "name"):
                value = repository.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    def _in_scope(self, item: Any) -> bool:
        label = self._repository_label(item)
        if not label:
            return False
        label = label.lower()
        if "/" in label:
            return label in self._allowed
        return label in self._allowed_short_names

    def _scope_violation(
            self, name: str, tool_args: dict[str, Any]
    ) -> dict[str, Any] | None:
        """The refusal to return when a call names a repository of another project."""
        if not self.repositories:
            return None
        requested = self._requested_repository(tool_args)
        if not requested or requested.lower() in self._allowed:
            return None
        logger.warning(
            "Blocked '%s' call to out-of-scope repository '%s'.", name, requested
        )
        return {
            "error": OUT_OF_SCOPE_MESSAGE.format(
                requested=requested, allowed=", ".join(self.repositories)
            )
        }

    def _apply_query_scope(self, tool_args: dict[str, Any]) -> None:
        if not self.repositories:
            return
        for key in QUERY_ARG_NAMES:
            if tool_args.get(key):
                tool_args[key] = self._scoped_query(tool_args[key])
                return

    def _drop_out_of_scope_hits(self, result: Any) -> Any:
        if not self.repositories or not isinstance(result, dict):
            return result
        items = result.get("items")
        if not isinstance(items, list):
            return result
        kept = [item for item in items if self._in_scope(item)]
        if len(kept) != len(items):
            logger.warning(
                "Dropped %d search_code hit(s) outside project scope.",
                len(items) - len(kept),
            )
        result["items"] = kept
        return result

    async def _call_reporting_retry(
            self, name: str, tool_args: dict[str, Any], ctx: RunContext, tool: ToolsetTool
    ) -> Any:
        try:
            return await super().call_tool(name, tool_args, ctx, tool)
        except ModelRetry as e:
            return {"error": f"ModelRetry: {e.message}"}

    @staticmethod
    def _file_cache(ctx: RunContext) -> dict | None:
        return getattr(getattr(ctx, "deps", None), "file_cache", None)

    @staticmethod
    def _read_key(tool_args: dict[str, Any]) -> str | None:
        owner, repo = tool_args.get("owner"), tool_args.get("repo")
        path = tool_args.get("path")
        if not (owner and repo and path):
            return None
        ref = tool_args.get("ref") or tool_args.get("sha") or ""
        return f"{owner}/{repo}@{ref}:{path}".lower()

    async def _read_once(
            self, name: str, tool_args: dict[str, Any], ctx: RunContext, tool: ToolsetTool
    ) -> Any:
        """Serve a repeated read of the same file from the turn's cache.

        Each ask_git call spawns a fresh sub-agent with no memory of the previous
        one, so the same entry files get fetched again on every question of a turn.
        """
        cache = self._file_cache(ctx)
        key = self._read_key(tool_args) if cache is not None else None
        if key is not None and key in cache:
            return cache[key]

        result = await self._call_reporting_retry(name, tool_args, ctx, tool)

        if key is not None and len(cache) < MAX_CACHED_FILE_READS:
            cache[key] = result
        return result

    def _annotate_if_empty(self, result: Any) -> Any:
        """Stop an empty search from reading as proof the code does not exist."""
        if not isinstance(result, dict) or "items" not in result or result["items"]:
            return result
        result["note"] = SEARCH_EMPTY_NOTE.format(
            repositories=", ".join(self.repositories) or "os repositórios do projeto"
        )
        return result

    async def _search_in_scope(
            self, name: str, tool_args: dict[str, Any], ctx: RunContext, tool: ToolsetTool
    ) -> Any:
        self._apply_query_scope(tool_args)
        try:
            result = await super().call_tool(name, tool_args, ctx, tool)
        except ModelRetry as e:
            e.message += " Any new calls to search_code will be blocked. Don't retry."
            return {"error": f"ModelRetry: {e.message}"}
        return self._annotate_if_empty(self._drop_out_of_scope_hits(result))

    async def call_tool(
            self, name: str, tool_args: dict[str, Any], ctx: RunContext, tool: ToolsetTool
    ) -> Any:
        violation = self._scope_violation(name, tool_args)
        if violation is not None:
            return violation
        if name == "search_code":
            return await self._search_in_scope(name, tool_args, ctx, tool)
        if name == "get_file_contents":
            return await self._read_once(name, tool_args, ctx, tool)
        if name in RETRY_AS_ERROR_TOOLS:
            return await self._call_reporting_retry(name, tool_args, ctx, tool)
        return await super().call_tool(name, tool_args, ctx, tool)

    async def get_instructions(self, ctx: RunContext):
        inherited = await super().get_instructions(ctx)
        if not self.repositories:
            return inherited
        scope = REPOSITORY_SCOPE_INSTRUCTION.format(
            repositories=", ".join(self.repositories),
            budget=SUGGESTED_FILE_READ_BUDGET,
        )
        if inherited is None:
            return scope
        if isinstance(inherited, str):
            return f"{inherited}\n\n{scope}"
        return [*inherited, scope]


# endregion


class McpProvider(ABC):
    """Strategy interface: build an MCP toolset for a company/agent."""

    provider_name: str
    domain: str
    agent_tool_policies: dict[str, list[str]] = {}
    default_tools: list[str] = []

    def get_allowed_tools(self, agent_name: str) -> list[str]:
        """Return the vendor tools this agent is allowed to call."""
        return self.agent_tool_policies.get(agent_name, self.default_tools)

    @abstractmethod
    def build_toolset(
            self,
            session: Session,
            organization: models.Organization,
            project_id: str,
            agent_name: str,
    ):
        """Build the configured MCP toolset, applying per-agent tool policy."""


class GithubMcpProvider(McpProvider):
    provider_name = models.McpProviderType.GITHUB.value
    domain = models.McpDomain.SCM.value

    _READ_TOOLS = ["get_file_contents", "search_code"]

    agent_tool_policies: dict[str, list[str]] = {
        "ask_git": _READ_TOOLS,
        "code_git": _READ_TOOLS
                    + [
                        "create_branch",
                        "create_or_update_file",
                        "push_files",
                        "create_pull_request",
                    ],
        "unit_test_agent": ["create_pull_request"],
    }
    default_tools: list[str] = ["search_code"]

    def _load_config(
            self, session: Session, organization: models.Organization
    ) -> models.OrganizationGithubMcpConfig:
        try:
            config = (
                session.query(models.OrganizationGithubMcpConfig)
                .filter(
                    models.OrganizationGithubMcpConfig.organization_id
                    == organization.id
                )
                .one_or_none()
            )
        except (EncryptionKeyMissingError, DecryptionError) as exc:
            logger.error(
                "Cannot decrypt GitHub MCP config for organization '%s' (id=%s): %s",
                organization.name,
                organization.id,
                exc,
            )
            raise McpConnectionError(
                f"GitHub MCP credentials for company '{organization.name}' "
                "could not be decrypted."
            ) from exc
        if config is None:
            logger.error(
                "GitHub MCP config not found for organization '%s' (id=%s)",
                organization.name,
                organization.id,
            )
            raise McpConfigNotFoundError(
                f"GitHub MCP configuration missing for company '{organization.name}'."
            )
        return config

    def _load_installation(
            self, session: Session, organization: models.Organization
    ) -> models.OrganizationGitHubAppInstallation:
        installation = (
            session.query(models.OrganizationGitHubAppInstallation)
            .filter(
                models.OrganizationGitHubAppInstallation.organization_id
                == organization.id
            )
            .first()
        )
        if installation is None:
            logger.error(
                "GitHub App installation not found for organization '%s' (id=%s)",
                organization.name,
                organization.id,
            )
            raise McpConfigNotFoundError(
                f"GitHub App installation missing for company '{organization.name}'."
            )
        return installation

    def get_access_token(
            self, session: Session, organization: models.Organization
    ) -> tuple[str, str]:
        """Return ``(access_token, mcp_url)`` for the organization's GitHub App."""
        # Imported here to keep this module importable without side effects and
        # to avoid a hard dependency cycle at import time.
        from services.github_app_service import get_installation_access_token

        config = self._load_config(session, organization)
        installation = self._load_installation(session, organization)

        cached = _installation_token_cache.get(installation.github_app_installation_id)
        if cached:
            return cached, config.mcp_url

        try:
            token = get_installation_access_token(
                app_id=int(config.github_app_id),
                private_key_base64=config.github_app_private_key_base64,
                installation_id=installation.github_app_installation_id,
            )
        except requests.RequestException as exc:
            logger.error(
                "Failed to obtain GitHub installation token for organization "
                "'%s' (id=%s): %s",
                organization.name,
                organization.id,
                exc,
            )
            raise McpConnectionError(
                f"Could not authenticate GitHub MCP for company '{organization.name}'."
            ) from exc

        _installation_token_cache.put(installation.github_app_installation_id, token)
        return token, config.mcp_url

    def _accessible_repositories(self, token: str, installation_id: int) -> set[str]:
        cached = _accessible_repositories_cache.get(installation_id)
        if cached is not None:
            return cached
        from services.github_app_service import (
            list_accessible_repository_full_names,
        )

        names = list_accessible_repository_full_names(token)
        _accessible_repositories_cache.put(installation_id, names)
        return names

    def _reachable_repositories(
            self, session: Session, project_id: str, token: str, installation_id: int
    ) -> tuple[str, ...]:
        """The project's repositories, minus any the installation cannot see."""
        configured = project_repository_full_names(session, project_id)
        if not configured:
            return configured

        try:
            accessible = self._accessible_repositories(token, installation_id)
        except requests.RequestException as exc:
            logger.warning(
                "Could not list repositories for installation %s (%s); "
                "skipping reachability check.",
                installation_id,
                exc,
            )
            return configured

        reachable = tuple(name for name in configured if name.lower() in accessible)
        unreachable = [name for name in configured if name not in reachable]
        if unreachable:
            logger.error(
                "Project '%s' lists repositories the GitHub App installation %s "
                "cannot reach: %s. Reachable for this installation: %s.",
                project_id,
                installation_id,
                unreachable,
                sorted(accessible)[:MAX_REPORTED_REPOSITORIES] or "none",
            )
        if not reachable:
            raise McpConfigNotFoundError(
                NO_ACCESSIBLE_REPOSITORY_MESSAGE.format(
                    configured=", ".join(configured),
                    accessible=", ".join(sorted(accessible)[:MAX_REPORTED_REPOSITORIES])
                               or "nenhum",
                )
            )
        return reachable

    def build_toolset(
            self,
            session: Session,
            organization: models.Organization,
            project_id: str,
            agent_name: str,
    ):
        token, mcp_url = self.get_access_token(session, organization)
        installation = self._load_installation(session, organization)
        repositories = self._reachable_repositories(
            session, project_id, token, installation.github_app_installation_id
        )

        git_mcp = MCPToolset(
            mcp_url,
            headers={"Authorization": f"Bearer {token}"},
            init_timeout=MCP_INIT_TIMEOUT_SECONDS,
            read_timeout=MCP_READ_TIMEOUT_SECONDS,
        )
        git_mcp = filter_allowed_tools(git_mcp, self.get_allowed_tools(agent_name))
        if not repositories:
            logger.warning(
                "Project '%s' has no repository registered; GitHub MCP for agent "
                "'%s' will not be scoped to a repository.",
                project_id,
                agent_name,
            )
        logger.info(
            "Built GitHub MCP toolset for agent '%s' (organization '%s', repos %s)",
            agent_name,
            organization.name,
            list(repositories) or "unscoped",
        )
        return GithubFilterToolset(git_mcp, repositories=repositories)


IGNORED_WORKSPACE_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "dist",
    "build",
    ".idea",
    ".vscode",
    "coverage",
}
MAX_FILE_READ_SIZE = 2 * 1024 * 1024
MAX_SEARCH_FILE_SIZE = 1024 * 1024
MAX_SEARCH_MATCHES = 30


def _safe_resolve_subpath(root_path: Path, subpath: str) -> Path:
    cleaned = subpath.lstrip("/\\") if subpath else ""
    resolved = (root_path / cleaned).resolve()
    try:
        resolved.relative_to(root_path)
    except ValueError:
        raise ValueError(f"Access denied: path '{subpath}' escapes the repository root.")
    return resolved


def _read_file_text(target_file: Path, path: str) -> str:
    if not target_file.exists():
        return f"Error: File '{path}' does not exist in local repository."
    if not target_file.is_file():
        return f"Error: Path '{path}' is a directory, not a file."
    if target_file.stat().st_size > MAX_FILE_READ_SIZE:
        return f"Error: File '{path}' exceeds 2MB limit for direct reading."

    try:
        with open(target_file, "r", encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        with open(target_file, "r", encoding="latin-1") as f:
            return f.read()


def _build_get_file_contents_tool(root_path: Path) -> Tool:
    def get_file_contents(path: str) -> str:
        """Read content of a file in the local repository.

        Args:
            path: Relative path to the file within the repository.
        """
        try:
            target_file = _safe_resolve_subpath(root_path, path)
            return _read_file_text(target_file, path)
        except Exception as e:
            return f"Error reading file '{path}': {e}"

    return Tool(get_file_contents)


def _list_directory_entries(target_dir: Path, path: str) -> str:
    if not target_dir.exists():
        return f"Error: Directory '{path}' does not exist."
    if not target_dir.is_dir():
        return f"Error: Path '{path}' is a file, not a directory."

    entries = []
    for entry in sorted(target_dir.iterdir()):
        if entry.name in IGNORED_WORKSPACE_DIRS:
            continue
        entry_type = "directory" if entry.is_dir() else "file"
        entries.append(f"[{entry_type}] {entry.name}")
    return "\n".join(entries) if entries else "(empty directory)"


def _build_list_directory_tool(root_path: Path) -> Tool:
    def list_directory(path: str = "") -> str:
        """List files and directories within a local repository subfolder.

        Args:
            path: Relative subpath to list (defaults to root).
        """
        try:
            target_dir = _safe_resolve_subpath(root_path, path)
            return _list_directory_entries(target_dir, path)
        except Exception as e:
            return f"Error listing directory '{path}': {e}"

    return Tool(list_directory)


def _search_in_file(
    root_path: Path,
    fpath: Path,
    query_lower: str,
    matches: list[str],
) -> None:
    try:
        if fpath.stat().st_size > MAX_SEARCH_FILE_SIZE:
            return
        rel_path = fpath.relative_to(root_path).as_posix()
        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
            for line_idx, line in enumerate(f, start=1):
                if query_lower in line.lower():
                    matches.append(f"{rel_path}:{line_idx}: {line.strip()[:150]}")
                    if len(matches) >= MAX_SEARCH_MATCHES:
                        return
    except Exception:
        return


def _search_files_in_directory(
    root_path: Path,
    query_lower: str,
    path_pattern: str | None,
    matches: list[str],
) -> None:
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_WORKSPACE_DIRS]
        for fname in filenames:
            if path_pattern and not fnmatch.fnmatch(fname, path_pattern):
                continue
            _search_in_file(root_path, Path(dirpath) / fname, query_lower, matches)
            if len(matches) >= MAX_SEARCH_MATCHES:
                return


def _execute_code_search(root_path: Path, query: str, path_pattern: str | None) -> str:
    if not query or len(query.strip()) < 2:
        return "Error: Search query must be at least 2 characters."

    matches: list[str] = []
    _search_files_in_directory(root_path, query.lower(), path_pattern, matches)

    if not matches:
        return f"No matches found for query '{query}' in local workspace."
    return f"Found {len(matches)} match(es):\n" + "\n".join(matches)


def _build_search_code_tool(root_path: Path) -> Tool:
    def search_code(query: str, path_pattern: str | None = None) -> str:
        """Search for a text string or pattern in the local repository code files.

        Args:
            query: String to search for.
            path_pattern: Optional filename glob filter (e.g. '*.py' or '*.ts').
        """
        try:
            return _execute_code_search(root_path, query, path_pattern)
        except Exception as e:
            return f"Error searching code for '{query}': {e}"

    return Tool(search_code)


def _write_file_content(root_path: Path, path: str, content: str) -> str:
    target_file = _safe_resolve_subpath(root_path, path)
    target_file.parent.mkdir(parents=True, exist_ok=True)
    with open(target_file, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Successfully wrote {len(content)} characters to '{path}'."


def _build_create_or_update_file_tool(root_path: Path) -> Tool:
    def create_or_update_file(path: str, content: str) -> str:
        """Create or update a file with the given content in the local repository.

        Args:
            path: Relative path to write.
            content: Text content to write into the file.
        """
        try:
            return _write_file_content(root_path, path, content)
        except Exception as e:
            return f"Error writing file '{path}': {e}"

    return Tool(create_or_update_file)


_LOCAL_TOOL_BUILDERS = {
    "get_file_contents": _build_get_file_contents_tool,
    "list_directory": _build_list_directory_tool,
    "search_code": _build_search_code_tool,
    "create_or_update_file": _build_create_or_update_file_tool,
}


def create_local_filesystem_toolset(root_path: Path, allowed_tools: list[str]):
    """Create a sandboxed FunctionToolset operating on the target workspace directory."""
    tools = [
        builder(root_path)
        for name, builder in _LOCAL_TOOL_BUILDERS.items()
        if name in allowed_tools
    ]
    return FunctionToolset(tools=tools)


class LocalFilesystemMcpProvider(McpProvider):
    provider_name = models.McpProviderType.LOCAL_FILESYSTEM.value
    domain = models.McpDomain.SCM.value

    _READ_TOOLS = ["get_file_contents", "search_code", "list_directory"]

    agent_tool_policies: dict[str, list[str]] = {
        "ask_git": _READ_TOOLS,
        "tutor": _READ_TOOLS,
        "code_git": _READ_TOOLS + ["create_or_update_file"],
        "unit_test_agent": _READ_TOOLS + ["create_or_update_file"],
    }
    default_tools: list[str] = _READ_TOOLS

    def _get_project_local_root(self, session: Session, project_id: str) -> Path:
        """Find the local path for the project repository or fallback to target_project_root."""
        from utils.environment import get_target_project_root, resolve_safe_target_path

        local_repo = (
            session.query(models.ProjectRepository)
            .filter(
                models.ProjectRepository.project_id == project_id,
                models.ProjectRepository.source_type
                == models.McpProviderType.LOCAL_FILESYSTEM.value,
            )
            .first()
        )
        if local_repo and local_repo.local_path:
            return resolve_safe_target_path(local_repo.local_path)
        return get_target_project_root()

    def build_toolset(
            self,
            session: Session,
            organization: models.Organization,
            project_id: str,
            agent_name: str,
    ):
        from utils.environment import is_local_workspace_enabled

        if not is_local_workspace_enabled():
            raise McpProviderNotConfiguredError(
                "Local workspace functionality is disabled or unavailable in this environment "
                "(requires Docker container execution with mounted target project)."
            )

        root_path = self._get_project_local_root(session, project_id)
        if not root_path.exists():
            raise McpConfigNotFoundError(
                f"Local project workspace path does not exist: {root_path}"
            )

        allowed_tools = self.get_allowed_tools(agent_name)
        logger.info(
            "Built Local Filesystem toolset for agent '%s' (project '%s', path '%s')",
            agent_name,
            project_id,
            root_path,
        )
        return create_local_filesystem_toolset(root_path, allowed_tools)


NO_PROJECT_DATABASE_MESSAGE = (
    "Nenhum banco de dados ativo está cadastrado para este projeto."
)


class DatabaseMcpProvider(McpProvider):
    """Read-only MCP server over the project's databases, of any engine, run in-process."""

    provider_name = "builtin"
    domain = models.McpDomain.DATABASE.value

    def _targets(self, session: Session, project_id: str) -> tuple:
        from services.project_database_service import (
            active_project_databases,
            target_from_record,
        )

        try:
            records = active_project_databases(session, project_id)
        except (EncryptionKeyMissingError, DecryptionError) as exc:
            logger.error(
                "Cannot decrypt database credentials for project '%s': %s",
                project_id,
                exc,
            )
            raise McpConnectionError(
                "As credenciais do banco de dados deste projeto não puderam ser "
                "descriptografadas."
            ) from exc
        targets = tuple(target_from_record(record) for record in records)
        if not targets:
            raise McpConfigNotFoundError(NO_PROJECT_DATABASE_MESSAGE)
        return targets

    def build_toolset(
        self,
        session: Session,
        organization: models.Organization,
        project_id: str,
        agent_name: str,
    ):
        from agents.mcp_servers.database_server import build_database_server
        from services.database_connector_service import ProjectDatabaseExecutor

        targets = self._targets(session, project_id)
        server = build_database_server(targets, ProjectDatabaseExecutor(targets))
        logger.info(
            "Built database MCP toolset for agent '%s' (project %s, databases %s)",
            agent_name,
            project_id,
            [target.alias for target in targets],
        )
        return MCPToolset(
            server,
            include_instructions=True,
            init_timeout=MCP_INIT_TIMEOUT_SECONDS,
            read_timeout=MCP_READ_TIMEOUT_SECONDS,
        )


class McpProviderRegistry:
    """Maps a ``(domain, provider name)`` pair to its strategy instance."""

    _providers: dict[tuple[str, str], McpProvider] = {
        (GithubMcpProvider.domain, GithubMcpProvider.provider_name): (
            GithubMcpProvider()
        ),
        (DatabaseMcpProvider.domain, DatabaseMcpProvider.provider_name): (
            DatabaseMcpProvider()
        ),
        (LocalFilesystemMcpProvider.domain, LocalFilesystemMcpProvider.provider_name): (
            LocalFilesystemMcpProvider()
        ),
    }

    @classmethod
    def get(
            cls,
            provider_name: str | None,
            domain: str = models.McpDomain.SCM.value,
    ) -> McpProvider:
        if not provider_name:
            raise McpProviderNotConfiguredError(
                f"No MCP provider configured for the '{domain}' domain."
            )
        provider = cls._providers.get((domain, provider_name))
        if provider is None:
            raise McpProviderNotConfiguredError(
                f"Unsupported MCP provider '{provider_name}' for the '{domain}' domain."
            )
        return provider

    @classmethod
    def configured_domains(cls) -> set[str]:
        """Domains that have at least one implemented provider."""
        return {provider.domain for provider in cls._providers.values()}


def get_organization_provider_name(
        session: Session, organization: models.Organization, domain: str
) -> str | None:
    """Provider configured by the company for ``domain``, or ``None``."""
    integration = (
        session.query(models.OrganizationMcpIntegration)
        .filter(
            models.OrganizationMcpIntegration.organization_id == organization.id,
            models.OrganizationMcpIntegration.domain == domain,
            models.OrganizationMcpIntegration.is_active == True,  # noqa: E712
        )
        .one_or_none()
    )
    if integration is not None:
        return integration.provider

    # Legacy column, only ever used for source code. Read until the backfill runs.
    if domain == models.McpDomain.SCM.value:
        return organization.mcp_provider
    return None


def project_database_provider(session: Session, project_id: str) -> str:
    """Provider for the project's databases; they are per project, so no company integration row."""
    has_database = (
        session.query(models.ProjectDatabase.id)
        .filter(
            models.ProjectDatabase.project_id == project_id,
            models.ProjectDatabase.is_active == True,
        )
        .first()
    )
    if has_database is None:
        raise McpConfigNotFoundError(NO_PROJECT_DATABASE_MESSAGE)
    return DatabaseMcpProvider.provider_name


def project_repository_full_names(session: Session, project_id: str) -> tuple[str, ...]:
    """``owner/name`` of every repository registered for the project."""
    rows = (
        session.query(models.ProjectRepository.full_name)
        .filter(models.ProjectRepository.project_id == project_id)
        .all()
    )
    return tuple(full_name for (full_name,) in rows if full_name)


def _get_project_organization(session: Session, project_id: str) -> models.Organization:
    project = (
        session.query(models.Project)
        .filter(models.Project.id == project_id)
        .one_or_none()
    )
    if project is None:
        logger.error("Project '%s' not found while resolving MCP.", project_id)
        raise McpConfigNotFoundError(f"Project '{project_id}' not found.")

    organization = project.organization
    if organization is None:
        logger.error("Project '%s' is not associated with any company.", project_id)
        raise McpProviderNotConfiguredError(
            f"Project '{project_id}' is not associated with a company."
        )
    return organization


def resolve_mcp_toolset_for_domain(
        session: Session, project_id: str, domain: str, agent_name: str
):
    """Intermediary service: resolve the MCP toolset serving ``domain``.

    Looks up the project's company, determines which provider it configured for
    the requested knowledge domain, and delegates connection building to the
    matching strategy.
    """
    organization = _get_project_organization(session, project_id)
    if domain == models.McpDomain.DATABASE.value:
        provider_name = project_database_provider(session, project_id)
    else:
        provider_name = get_organization_provider_name(session, organization, domain)

    if not provider_name and domain == models.McpDomain.SCM.value:
        local_repo = (
            session.query(models.ProjectRepository)
            .filter(
                models.ProjectRepository.project_id == project_id,
                models.ProjectRepository.source_type
                == models.McpProviderType.LOCAL_FILESYSTEM.value,
            )
            .first()
        )
        if local_repo:
            provider_name = models.McpProviderType.LOCAL_FILESYSTEM.value

    provider = McpProviderRegistry.get(provider_name, domain)
    return provider.build_toolset(session, organization, project_id, agent_name)

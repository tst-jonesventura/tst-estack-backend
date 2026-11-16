"""MCP server that lets an agent map and understand a project's databases."""
import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol

from database_access.guard import (
    DEFAULT_QUERY_ROWS,
    DEFAULT_SAMPLE_ROWS,
    DatabaseTarget,
)

SERVER_NAME = "estack-database"

ENGINE_LABELS = {
    "postgres": "PostgreSQL",
    "db2i": "Db2 for i (IBM i/AS400)",
    "oracle": "Oracle",
    "sqlserver": "SQL Server",
    "db2": "Db2",
}

SERVER_INSTRUCTIONS = """Ferramentas somente leitura sobre os bancos de dados deste projeto: {databases}.
{database_hint}
Comece pelo mapa (database_overview), depois relacionamentos (list_relationships; em sistemas legados e AS400, sem FKs declaradas, use suggest_relationships e trate o resultado como hipótese) e detalhes de tabela (describe_table). Regras do banco estão em list_rules e o código de funções e views em get_definition. Para entender os dados, prefira column_profile (estatísticas, sem varrer a tabela) a sample_rows, e use run_readonly_query só quando as outras não bastarem, sempre com filtros ou agregações. No Postgres, describe_table informa `readable` por coluna e sample_rows omite as colunas sem acesso, listando-as em `omitted_columns`. Colunas sensíveis voltam como '[mascarado]': nunca tente contornar o mascaramento nem inferir o valor original."""


class Executor(Protocol):
    def run(self, database: str | None, name: str, args: dict[str, Any]) -> dict[str, Any]: ...


@contextmanager
def _root_logging_preserved() -> Iterator[None]:
    """Undo the root handler FastMCP's constructor adds, which duplicates every log line."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        yield
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)


def describe_targets(targets: tuple[DatabaseTarget, ...]) -> str:
    described = []
    for target in targets:
        engine = ENGINE_LABELS.get(target.engine, target.engine)
        scope = f", schemas {', '.join(target.schemas)}" if target.schemas else ""
        described.append(f"'{target.alias}' ({engine}, banco {target.database}{scope})")
    return "; ".join(described)


def server_instructions(targets: tuple[DatabaseTarget, ...]) -> str:
    if len(targets) > 1:
        hint = "Há mais de um banco: informe sempre o parâmetro 'database' com o alias."
    else:
        hint = "Há um único banco; o parâmetro 'database' pode ser omitido."
    return SERVER_INSTRUCTIONS.format(databases=describe_targets(targets), database_hint=hint)


def _load_fastmcp():
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    return FastMCP, ToolAnnotations


def build_database_server(targets: tuple[DatabaseTarget, ...], executor: Executor):
    """An MCP server exposing read-only introspection tools for ``targets``."""
    fastmcp_class, tool_annotations = _load_fastmcp()
    with _root_logging_preserved():
        server = fastmcp_class(
            SERVER_NAME,
            instructions=server_instructions(targets),
            log_level="WARNING",
        )
    read_only = tool_annotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )

    async def run(database: str | None, name: str, args: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(executor.run, database, name, args)

    @server.tool(annotations=read_only)
    async def database_overview(database: str | None = None) -> dict[str, Any]:
        """Map of the database: version, size, schemas with table/view/routine counts, largest tables and extensions. Call this first."""
        return await run(database, "database_overview", {})

    @server.tool(annotations=read_only)
    async def list_tables(
        schema: str | None = None, name_filter: str | None = None, database: str | None = None
    ) -> dict[str, Any]:
        """Tables, views and materialized views with estimated rows, size, row-level security and comments. Filter by schema or by part of the name."""
        return await run(database, "list_tables", {"schema": schema, "name_filter": name_filter})

    @server.tool(annotations=read_only)
    async def list_routines(
        schema: str | None = None, name_filter: str | None = None, database: str | None = None
    ) -> dict[str, Any]:
        """Functions and procedures written for this database (extension routines excluded), with arguments, return type, language and comments."""
        return await run(database, "list_routines", {"schema": schema, "name_filter": name_filter})

    @server.tool(annotations=read_only)
    async def describe_table(
        table: str, schema: str | None = None, database: str | None = None
    ) -> dict[str, Any]:
        """Everything about one table or view: columns (type, nullability, default, generated, comment and whether they are readable), primary key, foreign keys in both directions, unique/check/exclusion constraints, indexes, triggers, row-level security policies, partitions and view definition. Accepts 'schema.table'."""
        return await run(database, "describe_table", {"table": table, "schema": schema})

    @server.tool(annotations=read_only)
    async def list_relationships(
        schema: str | None = None, database: str | None = None
    ) -> dict[str, Any]:
        """Foreign-key graph: source and target tables and columns, cardinality, whether the link is required, and ON DELETE/ON UPDATE actions. Use it to draw the entity-relationship model."""
        return await run(database, "list_relationships", {"schema": schema})

    @server.tool(annotations=read_only)
    async def suggest_relationships(
        schema: str | None = None, database: str | None = None
    ) -> dict[str, Any]:
        """Links the database never declared, inferred from columns that share a name or description with another table's key. Common in legacy and AS400 systems, where foreign keys live only in the application. Every result is a hypothesis, labelled with its confidence and evidence."""
        return await run(database, "suggest_relationships", {"schema": schema})

    @server.tool(annotations=read_only)
    async def list_rules(table: str | None = None, database: str | None = None) -> dict[str, Any]:
        """Business rules enforced by the database: check/unique/exclusion constraints, triggers with their function source, rewrite rules, row-level security policies, enums, domains, column defaults and generated columns. Pass a table name to narrow it."""
        return await run(database, "list_rules", {"table": table})

    @server.tool(annotations=read_only)
    async def get_definition(
        name: str, schema: str | None = None, database: str | None = None
    ) -> dict[str, Any]:
        """Source code of a function, procedure, view or materialized view (all overloads). Accepts 'schema.name'."""
        return await run(database, "get_definition", {"name": name, "schema": schema})

    @server.tool(annotations=read_only)
    async def column_profile(
        table: str, schema: str | None = None, database: str | None = None
    ) -> dict[str, Any]:
        """How the data in a table looks, from planner statistics and without scanning it: null fraction, distinct values, most common values with frequencies, approximate min and max per column. Sensitive columns are masked."""
        return await run(database, "column_profile", {"table": table, "schema": schema})

    @server.tool(annotations=read_only)
    async def sample_rows(
        table: str,
        schema: str | None = None,
        limit: int = DEFAULT_SAMPLE_ROWS,
        database: str | None = None,
    ) -> dict[str, Any]:
        """A few real rows of a table (at most 20) with sensitive columns masked. Columns without SELECT are omitted and listed in omitted_columns. Prefer column_profile to understand distributions."""
        return await run(database, "sample_rows", {"table": table, "schema": schema, "limit": limit})

    @server.tool(annotations=read_only)
    async def run_readonly_query(
        sql: str, limit: int = DEFAULT_QUERY_ROWS, database: str | None = None
    ) -> dict[str, Any]:
        """Run one read-only SELECT (or WITH ... SELECT) and get at most 200 rows, sensitive values masked. A single statement only: no semicolons, no writes, 15-second timeout. Use filters and aggregations instead of pulling whole tables."""
        return await run(database, "run_readonly_query", {"sql": sql, "limit": limit})

    return server

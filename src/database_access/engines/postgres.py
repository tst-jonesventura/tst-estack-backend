"""Postgres engine: read-only connection, role posture and ``pg_catalog`` introspection."""
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import psycopg2
from psycopg2 import sql as pgsql

from database_access.engines.base import CatalogOperation, Engine
from database_access.guard import (
    DEFAULT_SAMPLE_ROWS,
    MAX_QUERY_ROWS,
    MAX_SAMPLE_ROWS,
    DatabaseAccessError,
    DatabaseTarget,
    Dialect,
    QueryResult,
    _posture_cache,
    is_sensitive_column,
    shape_cell,
    shape_records,
    shape_rows,
)
from database_access.network import (
    check_ssl_mode,
    public_network_only,
    resolve_allowed_addresses,
)
from database_access.relationships import ColumnInfo, suggest_relationships
from utils import log

logger = log.setup_custom_logger(__name__)

STATEMENT_TIMEOUT_MS = 15_000
LOCK_TIMEOUT_MS = 2_000
IDLE_IN_TRANSACTION_TIMEOUT_MS = 30_000
CONNECT_TIMEOUT_SECONDS = 10
APPLICATION_NAME = "estack-mcp"

SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")

BLOCKED_FUNCTIONS = (
    "pg_sleep",
    "pg_sleep_for",
    "pg_sleep_until",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_reload_conf",
    "pg_rotate_logfile",
    "pg_promote",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "lo_import",
    "lo_export",
    "lo_unlink",
    "set_config",
    "setval",
    "nextval",
    "pg_notify",
    "dblink",
    "dblink_exec",
    "pg_advisory_lock",
    "pg_advisory_xact_lock",
    "pg_try_advisory_lock",
    "pg_logical_emit_message",
    "query_to_xml",
    "query_to_json",
    "query_to_xml_and_xmlschema",
    "table_to_xml",
    "table_to_xml_and_xmlschema",
    "schema_to_xml",
    "schema_to_xml_and_xmlschema",
    "database_to_xml",
    "database_to_xml_and_xmlschema",
    "cursor_to_xml",
)

CREDENTIAL_CATALOGS = (
    "pg_authid",
    "pg_shadow",
    "pg_user_mapping",
    "pg_user_mappings",
    "pg_subscription",
    "pg_largeobject",
)

DIALECT = Dialect(
    name="postgres",
    blocked_functions=BLOCKED_FUNCTIONS,
    system_schemas=SYSTEM_SCHEMAS,
    blocked_relations=CREDENTIAL_CATALOGS,
    query_keywords=("select", "with", "values", "table"),
    dollar_quotes=True,
    escape_strings=True,
    unicode_escapes=True,
)


@dataclass(frozen=True)
class Posture:
    """What the connected role can do, checked before anything else runs."""

    server_version: str
    role: str
    is_superuser: bool
    can_write: bool
    reads_all_data: bool
    server_file_access: bool = False
    readable_outside_scope: tuple[str, ...] = ()

    def warnings(self, schemas: tuple[str, ...] = ()) -> list[str]:
        notes = []
        if self.can_write:
            notes.append(
                "O usuário tem permissão de escrita em alguma tabela. As travas "
                "impedem escritas, mas o recomendado é um usuário só de leitura."
            )
        if not self.reads_all_data and not schemas:
            notes.append(
                "O usuário não tem 'pg_read_all_data'; tabelas sem GRANT de SELECT "
                "ficarão invisíveis para amostras e consultas."
            )
        return notes

    def refusal(self, alias: str) -> str | None:
        if self.is_superuser:
            return (
                f"O usuário '{self.role}' do banco '{alias}' é superusuário. "
                "Por segurança o acesso foi recusado: cadastre um usuário só de "
                "leitura (GRANT pg_read_all_data)."
            )
        if self.server_file_access:
            return (
                f"O usuário '{self.role}' do banco '{alias}' pertence a "
                "pg_read_server_files/pg_write_server_files/"
                "pg_execute_server_program, que dão acesso a arquivos do servidor. "
                "Cadastre um usuário só de leitura sem esses papéis."
            )
        if self.readable_outside_scope:
            schemas = ", ".join(self.readable_outside_scope[:5])
            return (
                f"O usuário '{self.role}' consegue ler tabelas fora dos schemas "
                f"liberados ({schemas}). Restrinja o usuário: GRANT USAGE ON SCHEMA "
                "<schema> e GRANT SELECT ON ALL TABLES IN SCHEMA <schema> apenas "
                "nos schemas liberados, e não use pg_read_all_data."
            )
        return None


USER_SCHEMAS_SQL = """
select nspname as name
from pg_namespace
where nspname <> 'information_schema' and nspname !~ '^pg_'
order by 1
"""


# region Errors
CONNECTION_FAILURE_REASONS = (
    (("authentication",), "usuário ou senha recusados"),
    (("timeout", "timed out"), "tempo de conexão esgotado; verifique rede e liberação de IP"),
    (("could not translate host name", "name or service not known"), "host não encontrado"),
    (("does not exist",), "banco ou usuário inexistente"),
    (("ssl",), "falha na negociação SSL; revise o sslmode"),
)

QUERY_ERROR_MESSAGES = {
    "57014": (
        f"A consulta passou do limite de {STATEMENT_TIMEOUT_MS // 1000}s e foi "
        "cancelada. Use filtros, índices ou agregações mais estreitas."
    ),
    "25006": "O banco está em modo somente leitura: escritas não são permitidas.",
}


def _connection_failure_reason(exc: Exception) -> str:
    text = str(exc).lower()
    for needles, reason in CONNECTION_FAILURE_REASONS:
        if any(needle in text for needle in needles):
            return reason
    return "servidor inacessível"


def _query_error_message(exc: Exception, alias: str) -> str:
    code = getattr(exc, "pgcode", None)
    if code in QUERY_ERROR_MESSAGES:
        return QUERY_ERROR_MESSAGES[code]
    diag = getattr(exc, "diag", None)
    primary = getattr(diag, "message_primary", None)
    if code == "42501":
        return (
            f"Sem permissão para ler este objeto: {primary or 'acesso negado'}. "
            "Algumas colunas podem estar com acesso revogado: em vez de select *, "
            "liste explicitamente as colunas legíveis (veja 'readable' em "
            "describe_table)."
        )
    if not primary:
        return f"Erro ao consultar o banco '{alias}'."
    hint = getattr(diag, "message_hint", None)
    suffix = f" Dica: {hint}" if hint else ""
    return f"Erro do Postgres ({code or 'desconhecido'}): {primary}.{suffix}"


def describe_driver_error(exc: Exception, alias: str) -> str:
    """A message about a driver failure that never carries credentials."""
    if isinstance(exc, psycopg2.OperationalError) and getattr(exc, "pgcode", None) is None:
        reason = _connection_failure_reason(exc)
        return f"Não foi possível conectar ao banco '{alias}': {reason}."
    return _query_error_message(exc, alias)


# endregion


# region Connection
def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _connection_options(target: DatabaseTarget) -> str:
    options = [
        "-c default_transaction_read_only=on",
        "-c standard_conforming_strings=on",
        f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
        f"-c lock_timeout={LOCK_TIMEOUT_MS}",
        f"-c idle_in_transaction_session_timeout={IDLE_IN_TRANSACTION_TIMEOUT_MS}",
    ]
    if target.schemas:
        path = ",".join(_quote_identifier(schema) for schema in target.schemas)
        options.append(f"-c search_path={path}")
    return " ".join(options)


def _connection_args(target: DatabaseTarget) -> dict[str, Any]:
    args: dict[str, Any] = {
        "host": target.host,
        "port": target.port,
        "dbname": target.database,
        "user": target.username,
        "password": target.password,
        "sslmode": target.ssl_mode,
        "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        "application_name": APPLICATION_NAME,
        "options": _connection_options(target),
    }
    if not public_network_only(target):
        return args
    check_ssl_mode(target)
    addresses = resolve_allowed_addresses(target)
    args.update(
        {
            "host": ",".join([target.host] * len(addresses)),
            "hostaddr": ",".join(addresses),
            "port": ",".join([str(target.port)] * len(addresses)),
        }
    )
    return args


def open_connection(target: DatabaseTarget):
    args = _connection_args(target)
    try:
        connection = psycopg2.connect(**args)
    except psycopg2.Error as exc:
        logger.warning(
            "Postgres connection failed for alias '%s' (%s:%s/%s): %s",
            target.alias,
            target.host,
            target.port,
            target.database,
            type(exc).__name__,
        )
        raise DatabaseAccessError(describe_driver_error(exc, target.alias)) from exc
    connection.set_session(readonly=True, autocommit=False)
    return connection


SOURCE_COLUMNS_SQL = """
select a.attname
from unnest(%(oids)s::oid[], %(numbers)s::int2[]) with ordinality as u(oid, number, ordinality)
left join pg_attribute a on a.attrelid = u.oid and a.attnum = u.number
order by u.ordinality
"""


def _resolve_source_columns(cursor, description) -> list[str | None]:
    oids = [getattr(column, "table_oid", 0) or 0 for column in description]
    numbers = [getattr(column, "table_column", 0) or 0 for column in description]
    if not any(oid and number for oid, number in zip(oids, numbers)):
        return [None] * len(description)
    cursor.execute(SOURCE_COLUMNS_SQL, {"oids": oids, "numbers": numbers})
    return [row[0] for row in cursor.fetchall()]


class ReadOnlySession:
    """One connection, every statement inside a read-only transaction."""

    def __init__(self, target: DatabaseTarget, connection):
        self.target = target
        self._connection = connection

    def fetch(
        self,
        query: str | pgsql.Composable,
        params: Any = None,
        max_rows: int = MAX_QUERY_ROWS,
        resolve_sources: bool = False,
    ) -> QueryResult:
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(query, params)
                if cursor.description is None:
                    return QueryResult(columns=[], rows=[])
                columns = [column.name for column in cursor.description]
                rows = cursor.fetchmany(max_rows + 1)
                sources = (
                    _resolve_source_columns(cursor, cursor.description) if resolve_sources else []
                )
        except psycopg2.Error as exc:
            self._connection.rollback()
            raise DatabaseAccessError(describe_driver_error(exc, self.target.alias)) from exc
        self._connection.rollback()
        truncated = len(rows) > max_rows
        return QueryResult(
            columns=columns,
            rows=[list(row) for row in rows[:max_rows]],
            truncated=truncated,
            source_columns=sources,
        )

    def records(self, query: str, params: Any = None, max_rows: int = 1000) -> list[dict[str, Any]]:
        return self.fetch(query, params, max_rows).records()

    def one(self, query: str, params: Any = None) -> dict[str, Any] | None:
        found = self.records(query, params, max_rows=1)
        return found[0] if found else None


POSTURE_SQL = """
select current_setting('server_version') as server_version,
       current_user as role,
       r.rolsuper as is_superuser,
       pg_has_role(current_user, 'pg_read_all_data', 'MEMBER') as reads_all_data,
       exists (
           select 1
           from pg_roles g
           where g.rolname in (
               'pg_read_server_files',
               'pg_write_server_files',
               'pg_execute_server_program'
           )
             and pg_has_role(current_user, g.oid, 'MEMBER')
       ) as server_file_access,
       exists (
           select 1
           from information_schema.table_privileges p
           where p.grantee = current_user
             and p.privilege_type in ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE')
       ) or exists (
           select 1
           from pg_class c
           join pg_namespace n on n.oid = c.relnamespace
           where c.relowner = r.oid
             and c.relkind in ('r', 'p')
             and n.nspname not in ('pg_catalog', 'information_schema')
       ) as can_write,
       case
           when %(schemas)s::text[] is not null then coalesce((
               select array_agg(distinct n.nspname::text order by n.nspname::text)
               from pg_class c
               join pg_namespace n on n.oid = c.relnamespace
               where n.nspname <> 'information_schema'
                 and n.nspname !~ '^pg_'
                 and not n.nspname = any(%(schemas)s::text[])
                 and c.relkind in ('r', 'p', 'v', 'm', 'f')
                 and (
                     has_table_privilege(c.oid, 'SELECT')
                     or has_any_column_privilege(c.oid, 'SELECT')
                 )
           ), '{}'::text[])
           else '{}'::text[]
       end as readable_outside_scope
from pg_roles r
where r.rolname = current_user
"""

POSTURE_SQL_PRE14 = POSTURE_SQL.replace(
    "pg_has_role(current_user, 'pg_read_all_data', 'MEMBER')", "false"
)


def read_posture(session: ReadOnlySession, schemas: tuple[str, ...] = ()) -> Posture:
    version_num = session.one("select current_setting('server_version_num')::int as v")
    query = POSTURE_SQL if version_num and version_num["v"] >= 140000 else POSTURE_SQL_PRE14
    row = session.one(query, {"schemas": list(schemas) or None})
    if row is None:
        raise DatabaseAccessError("Não foi possível identificar o usuário conectado.")
    return Posture(
        server_version=row["server_version"],
        role=row["role"],
        is_superuser=bool(row["is_superuser"]),
        can_write=bool(row["can_write"]),
        reads_all_data=bool(row["reads_all_data"]),
        server_file_access=bool(row["server_file_access"]),
        readable_outside_scope=tuple(row["readable_outside_scope"] or ()),
    )


READABLE_SENSITIVE_COLUMNS_SQL = """
select n.nspname as schema_name, c.relname as table_name, a.attname as column_name
from pg_attribute a
join pg_class c on c.oid = a.attrelid
join pg_namespace n on n.oid = c.relnamespace
where a.attnum > 0
  and not a.attisdropped
  and c.relkind in ('r', 'p', 'v', 'm', 'f')
  and n.nspname <> 'information_schema'
  and n.nspname !~ '^pg_'
  and (%(schemas)s::text[] is null or n.nspname = any(%(schemas)s::text[]))
  and has_column_privilege(c.oid, a.attnum, 'SELECT')
order by n.nspname, c.relname, a.attnum
limit 5000
"""


def readable_sensitive_columns(session: ReadOnlySession, schemas: tuple[str, ...]) -> list[str]:
    rows = session.records(
        READABLE_SENSITIVE_COLUMNS_SQL, {"schemas": list(schemas) or None}, max_rows=5000
    )
    return sorted(
        f"{row['schema_name']}.{row['table_name']}.{row['column_name']}"
        for row in rows
        if is_sensitive_column(row["column_name"])
    )


def sensitive_columns_warning(role: str, columns: list[str]) -> str:
    return (
        f"O usuário '{role}' consegue ler {len(columns)} coluna(s) com dados "
        f"sensíveis (ex.: {', '.join(columns[:5])}). O e-Stack mascara essas "
        "colunas nos resultados, mas não consegue mascarar valores derivados "
        "delas (ex.: upper(senha)). Para garantir a proteção, revogue o acesso "
        f"no banco: REVOKE SELECT ON <tabela> FROM {role}; "
        f"GRANT SELECT (<colunas não sensíveis>) ON <tabela> TO {role};"
    )


def enforce_posture(target: DatabaseTarget, session: ReadOnlySession) -> Posture:
    posture = _posture_cache.get(target.cache_key)
    if posture is None:
        posture = read_posture(session, target.schemas)
        _posture_cache.put(target.cache_key, posture)
    refusal = posture.refusal(target.alias)
    if refusal:
        raise DatabaseAccessError(refusal)
    return posture


@contextmanager
def readonly_session(target: DatabaseTarget, check_posture: bool = True) -> Iterator[ReadOnlySession]:
    connection = open_connection(target)
    try:
        session = ReadOnlySession(target, connection)
        if check_posture:
            enforce_posture(target, session)
        yield session
    finally:
        connection.close()


# endregion


MAX_LISTED_OBJECTS = 500
MAX_RELATIONSHIPS = 1000
MAX_RULES_PER_KIND = 300
MAX_FUNCTION_SOURCE_CHARS = 6000
MAX_COMMON_VALUES = 10
LARGEST_TABLES = 15

SCHEMA_FILTER = (
    "n.nspname <> 'information_schema' and n.nspname !~ '^pg_' "
    "and (%(schemas)s::text[] is null or n.nspname = any(%(schemas)s::text[]))"
)

FK_ACTIONS = {
    "a": "no action",
    "r": "restrict",
    "c": "cascade",
    "n": "set null",
    "d": "set default",
}
RELATION_KINDS = {
    "r": "table",
    "p": "partitioned table",
    "v": "view",
    "m": "materialized view",
    "f": "foreign table",
}
CONSTRAINT_KINDS = {
    "p": "primary key",
    "f": "foreign key",
    "u": "unique",
    "c": "check",
    "x": "exclusion",
    "t": "constraint trigger",
    "n": "not null",
}
ROUTINE_KINDS = {"f": "function", "p": "procedure", "a": "aggregate", "w": "window"}
POLICY_COMMANDS = {"r": "SELECT", "a": "INSERT", "w": "UPDATE", "d": "DELETE", "*": "ALL"}
TRIGGER_STATES = {"O": "enabled", "D": "disabled", "R": "replica", "A": "always"}


def _params(schemas: list[str] | None, **extra: Any) -> dict[str, Any]:
    return {"schemas": schemas, **extra}


def _truncate(text: str | None, limit: int) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return f"{text[:limit]}\n-- … (+{len(text) - limit} caracteres omitidos)"


# region Overview
OVERVIEW_SQL = """
select current_database() as database,
       current_setting('server_version') as server_version,
       current_user as connected_as,
       pg_size_pretty(pg_database_size(current_database())) as size
"""

SCHEMAS_SQL = f"""
select n.nspname as schema,
       count(c.oid) filter (where c.relkind in ('r', 'p')) as tables,
       count(c.oid) filter (where c.relkind = 'v') as views,
       count(c.oid) filter (where c.relkind = 'm') as materialized_views,
       sum(greatest(c.reltuples, 0)) filter (where c.relkind in ('r', 'p'))::bigint as estimated_rows,
       obj_description(n.oid, 'pg_namespace') as comment
from pg_namespace n
left join pg_class c
       on c.relnamespace = n.oid
      and c.relkind in ('r', 'p', 'v', 'm')
      and not c.relispartition
where {SCHEMA_FILTER}
group by n.oid, n.nspname
order by n.nspname
"""

ROUTINE_COUNT_SQL = f"""
select n.nspname as schema, count(*) as routines
from pg_proc p
join pg_namespace n on n.oid = p.pronamespace
where {SCHEMA_FILTER}
  and not exists (
      select 1 from pg_depend d
      where d.classid = 'pg_proc'::regclass and d.objid = p.oid and d.deptype = 'e'
  )
group by n.nspname
"""

LARGEST_TABLES_SQL = f"""
select n.nspname || '.' || c.relname as table,
       case when c.reltuples < 0 then null else c.reltuples::bigint end as estimated_rows,
       pg_size_pretty(pg_total_relation_size(c.oid)) as total_size
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where c.relkind in ('r', 'p') and not c.relispartition and {SCHEMA_FILTER}
order by pg_total_relation_size(c.oid) desc
limit {LARGEST_TABLES}
"""

EXTENSIONS_SQL = "select extname as name, extversion as version from pg_extension order by 1"


def database_overview(session: ReadOnlySession, schemas: list[str] | None) -> dict[str, Any]:
    params = _params(schemas)
    routines = {
        row["schema"]: row["routines"] for row in session.records(ROUTINE_COUNT_SQL, params)
    }
    schema_rows = session.records(SCHEMAS_SQL, params)
    for row in schema_rows:
        row["routines"] = routines.get(row["schema"], 0)
    return {
        **(session.one(OVERVIEW_SQL) or {}),
        "schemas": shape_records(schema_rows),
        "largest_tables": shape_records(session.records(LARGEST_TABLES_SQL, params)),
        "extensions": shape_records(session.records(EXTENSIONS_SQL)),
    }


# endregion


# region Tables and routines
LIST_TABLES_SQL = f"""
select n.nspname as schema,
       c.relname as name,
       c.relkind as kind,
       case when c.reltuples < 0 then null else c.reltuples::bigint end as estimated_rows,
       pg_size_pretty(pg_total_relation_size(c.oid)) as total_size,
       c.relrowsecurity as row_level_security,
       obj_description(c.oid, 'pg_class') as comment
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where c.relkind in ('r', 'p', 'v', 'm', 'f')
  and not c.relispartition
  and {SCHEMA_FILTER}
  and (%(schema)s::text is null or n.nspname = %(schema)s::text)
  and (%(pattern)s::text is null or c.relname ilike %(pattern)s::text)
order by n.nspname, c.relname
limit {MAX_LISTED_OBJECTS + 1}
"""


def _like_pattern(name_filter: str | None) -> str | None:
    if not name_filter:
        return None
    escaped = name_filter.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _cap(rows: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    payload: dict[str, Any] = {"items": shape_records(rows[:limit]), "count": min(len(rows), limit)}
    if len(rows) > limit:
        payload["truncated"] = True
        payload["note"] = "Lista cortada. Filtre por schema ou por parte do nome."
    return payload


def list_tables(
    session: ReadOnlySession,
    schemas: list[str] | None,
    schema: str | None,
    name_filter: str | None,
) -> dict[str, Any]:
    rows = session.records(
        LIST_TABLES_SQL,
        _params(schemas, schema=schema, pattern=_like_pattern(name_filter)),
        max_rows=MAX_LISTED_OBJECTS + 1,
    )
    for row in rows:
        row["kind"] = RELATION_KINDS.get(row["kind"], row["kind"])
    payload = _cap(rows, MAX_LISTED_OBJECTS)
    if schema and not rows:
        payload["note"] = (
            f"Nenhuma tabela visível no schema '{schema}': ele não existe ou o usuário "
            "não tem permissão de leitura nele."
        )
    return payload


LIST_ROUTINES_SQL = f"""
select n.nspname as schema,
       p.proname as name,
       p.prokind as kind,
       pg_get_function_identity_arguments(p.oid) as arguments,
       case when p.prokind in ('f', 'w') then pg_get_function_result(p.oid) end as returns,
       l.lanname as language,
       p.provolatile as volatility,
       obj_description(p.oid, 'pg_proc') as comment
from pg_proc p
join pg_namespace n on n.oid = p.pronamespace
join pg_language l on l.oid = p.prolang
where {SCHEMA_FILTER}
  and (%(schema)s::text is null or n.nspname = %(schema)s::text)
  and (%(pattern)s::text is null or p.proname ilike %(pattern)s::text)
  and not exists (
      select 1 from pg_depend d
      where d.classid = 'pg_proc'::regclass and d.objid = p.oid and d.deptype = 'e'
  )
order by n.nspname, p.proname
limit {MAX_LISTED_OBJECTS + 1}
"""

VOLATILITY = {"i": "immutable", "s": "stable", "v": "volatile"}


def list_routines(
    session: ReadOnlySession,
    schemas: list[str] | None,
    schema: str | None,
    name_filter: str | None,
) -> dict[str, Any]:
    rows = session.records(
        LIST_ROUTINES_SQL,
        _params(schemas, schema=schema, pattern=_like_pattern(name_filter)),
        max_rows=MAX_LISTED_OBJECTS + 1,
    )
    for row in rows:
        row["kind"] = ROUTINE_KINDS.get(row["kind"], row["kind"])
        row["volatility"] = VOLATILITY.get(row["volatility"], row["volatility"])
    return _cap(rows, MAX_LISTED_OBJECTS)


# endregion


# region Describe table
RESOLVE_RELATION_SQL = f"""
select c.oid, n.nspname as schema, c.relname as name, c.relkind as kind
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where c.relkind in ('r', 'p', 'v', 'm', 'f')
  and {SCHEMA_FILTER}
  and c.relname = %(table)s
  and (%(schema)s::text is null or n.nspname = %(schema)s::text)
order by (n.nspname = 'public') desc, n.nspname
"""

RELATION_INFO_SQL = """
select case when c.reltuples < 0 then null else c.reltuples::bigint end as estimated_rows,
       pg_size_pretty(pg_total_relation_size(c.oid)) as total_size,
       c.relrowsecurity as row_level_security,
       c.relforcerowsecurity as force_row_level_security,
       c.relispartition as is_partition,
       case when c.relkind = 'p' then pg_get_partkeydef(c.oid) end as partition_key,
       case when c.relispartition then pg_get_expr(c.relpartbound, c.oid) end as partition_bound,
       obj_description(c.oid, 'pg_class') as comment
from pg_class c
where c.oid = %(oid)s
"""

COLUMNS_SQL = """
select a.attname as name,
       format_type(a.atttypid, a.atttypmod) as type,
       a.attnotnull as not_null,
       case when a.attgenerated = '' then pg_get_expr(d.adbin, d.adrelid) end as default,
       case when a.attgenerated <> '' then pg_get_expr(d.adbin, d.adrelid) end as generated_as,
       nullif(a.attidentity, '') as identity,
       col_description(a.attrelid, a.attnum) as comment,
       has_column_privilege(a.attrelid, a.attnum, 'SELECT') as readable
from pg_attribute a
left join pg_attrdef d on d.adrelid = a.attrelid and d.adnum = a.attnum
where a.attrelid = %(oid)s and a.attnum > 0 and not a.attisdropped
order by a.attnum
"""

CONSTRAINTS_SQL = """
select conname as name, contype as kind, pg_get_constraintdef(oid, true) as definition
from pg_constraint
where conrelid = %(oid)s
order by contype, conname
"""

REFERENCED_BY_SQL = """
select conname as name,
       conrelid::regclass::text as from_table,
       pg_get_constraintdef(oid, true) as definition
from pg_constraint
where confrelid = %(oid)s and contype = 'f'
order by 2, 1
"""

INDEXES_SQL = """
select i.relname as name,
       pg_get_indexdef(x.indexrelid) as definition,
       x.indisunique as is_unique,
       x.indisprimary as is_primary
from pg_index x
join pg_class i on i.oid = x.indexrelid
where x.indrelid = %(oid)s
order by x.indisprimary desc, i.relname
"""

TRIGGERS_SQL = """
select t.tgname as name,
       pg_get_triggerdef(t.oid, true) as definition,
       t.tgenabled as state,
       t.tgfoid::regprocedure::text as function
from pg_trigger t
where t.tgrelid = %(oid)s and not t.tgisinternal
order by t.tgname
"""

POLICIES_SQL = """
select p.polname as name,
       p.polcmd as command,
       p.polpermissive as permissive,
       array(
           select case when r = 0 then 'public' else pg_get_userbyid(r) end
           from unnest(p.polroles) as r
       ) as roles,
       pg_get_expr(p.polqual, p.polrelid) as using_expression,
       pg_get_expr(p.polwithcheck, p.polrelid) as check_expression
from pg_policy p
where p.polrelid = %(oid)s
order by p.polname
"""

PARTITIONS_SQL = """
select c.oid::regclass::text as partition, pg_get_expr(c.relpartbound, c.oid) as bound
from pg_inherits i
join pg_class c on c.oid = i.inhrelid
where i.inhparent = %(oid)s
order by 1
limit 200
"""

VIEW_DEFINITION_SQL = "select pg_get_viewdef(%(oid)s::oid, true) as definition"


def resolve_relation(
    session: ReadOnlySession, schemas: list[str] | None, schema: str | None, table: str
) -> dict[str, Any]:
    if schema is None and "." in table:
        schema, table = table.split(".", 1)
    matches = session.records(
        RESOLVE_RELATION_SQL, _params(schemas, schema=schema, table=table), max_rows=20
    )
    if not matches:
        raise DatabaseAccessError(
            f"Tabela ou view '{table}' não encontrada"
            + (f" no schema '{schema}'" if schema else "")
            + ". Use list_tables para ver os nomes exatos."
        )
    if schema is None and len(matches) > 1 and matches[0]["schema"] != "public":
        found = ", ".join(f"{m['schema']}.{m['name']}" for m in matches)
        raise DatabaseAccessError(
            f"'{table}' existe em mais de um schema ({found}). Informe o schema."
        )
    return matches[0]


def _labelled(rows: list[dict[str, Any]], key: str, labels: dict[str, str]) -> list[dict[str, Any]]:
    for row in rows:
        row[key] = labels.get(row[key], row[key])
    return shape_records(rows)


def describe_table(
    session: ReadOnlySession, schemas: list[str] | None, schema: str | None, table: str
) -> dict[str, Any]:
    relation = resolve_relation(session, schemas, schema, table)
    params = {"oid": relation["oid"]}
    kind = relation["kind"]
    described: dict[str, Any] = {
        "table": f"{relation['schema']}.{relation['name']}",
        "kind": RELATION_KINDS.get(kind, kind),
        **shape_records([session.one(RELATION_INFO_SQL, params) or {}])[0],
        "columns": shape_records(session.records(COLUMNS_SQL, params)),
        "constraints": _labelled(
            session.records(CONSTRAINTS_SQL, params), "kind", CONSTRAINT_KINDS
        ),
        "referenced_by": shape_records(session.records(REFERENCED_BY_SQL, params)),
        "indexes": shape_records(session.records(INDEXES_SQL, params)),
        "triggers": _labelled(session.records(TRIGGERS_SQL, params), "state", TRIGGER_STATES),
        "policies": _labelled(session.records(POLICIES_SQL, params), "command", POLICY_COMMANDS),
    }
    if kind == "p":
        described["partitions"] = shape_records(session.records(PARTITIONS_SQL, params))
    if kind in ("v", "m"):
        row = session.one(VIEW_DEFINITION_SQL, params)
        described["view_definition"] = row["definition"] if row else None
    return {key: value for key, value in described.items() if value not in (None, [], "")}


# endregion


# region Relationships
RELATIONSHIPS_SQL = f"""
select c.conname as name,
       sn.nspname || '.' || s.relname as from_table,
       array(
           select a.attname
           from unnest(c.conkey) with ordinality as k(attnum, ord)
           join pg_attribute a on a.attrelid = c.conrelid and a.attnum = k.attnum
           order by k.ord
       ) as from_columns,
       tn.nspname || '.' || t.relname as to_table,
       array(
           select a.attname
           from unnest(c.confkey) with ordinality as k(attnum, ord)
           join pg_attribute a on a.attrelid = c.confrelid and a.attnum = k.attnum
           order by k.ord
       ) as to_columns,
       c.confdeltype as on_delete,
       c.confupdtype as on_update,
       exists (
           select 1 from pg_index x
           where x.indrelid = c.conrelid
             and x.indisunique
             and x.indpred is null
             and x.indnatts = cardinality(c.conkey)
             and (x.indkey::int2[]) @> c.conkey
       ) as from_is_unique,
       exists (
           select 1 from pg_attribute a
           where a.attrelid = c.conrelid and a.attnum = any(c.conkey) and not a.attnotnull
       ) as from_is_nullable
from pg_constraint c
join pg_class s on s.oid = c.conrelid
join pg_namespace sn on sn.oid = s.relnamespace
join pg_class t on t.oid = c.confrelid
join pg_namespace tn on tn.oid = t.relnamespace
join pg_namespace n on n.oid = c.connamespace
where c.contype = 'f'
  and {SCHEMA_FILTER}
  and (%(schema)s::text is null or sn.nspname = %(schema)s::text or tn.nspname = %(schema)s::text)
order by from_table, c.conname
limit {MAX_RELATIONSHIPS + 1}
"""


def _cardinality(row: dict[str, Any]) -> None:
    row["cardinality"] = "one-to-one" if row.pop("from_is_unique") else "many-to-one"
    row["required"] = not row.pop("from_is_nullable")


def list_relationships(
    session: ReadOnlySession, schemas: list[str] | None, schema: str | None
) -> dict[str, Any]:
    rows = session.records(
        RELATIONSHIPS_SQL, _params(schemas, schema=schema), max_rows=MAX_RELATIONSHIPS + 1
    )
    for row in rows:
        row["on_delete"] = FK_ACTIONS.get(row["on_delete"], row["on_delete"])
        row["on_update"] = FK_ACTIONS.get(row["on_update"], row["on_update"])
        _cardinality(row)
    payload = _cap(rows, MAX_RELATIONSHIPS)
    payload["tables_without_relationships_hint"] = (
        "Tabelas que não aparecem aqui não têm chave estrangeira declarada; o "
        "vínculo pode existir só na aplicação (colunas *_id sem FK)."
    )
    return payload


# endregion


# region Rules
RULE_TABLE_FILTER = "(%(table)s::text is null or {column} = %(table)s::text)"

CHECKS_SQL = f"""
select c.conrelid::regclass::text as table,
       c.conname as name,
       c.contype as kind,
       pg_get_constraintdef(c.oid, true) as definition,
       obj_description(c.oid, 'pg_constraint') as comment
from pg_constraint c
join pg_class r on r.oid = c.conrelid
join pg_namespace n on n.oid = r.relnamespace
where c.contype in ('c', 'u', 'x', 't')
  and {SCHEMA_FILTER}
  and {RULE_TABLE_FILTER.format(column='r.relname')}
order by 1, 2
limit {MAX_RULES_PER_KIND}
"""

TRIGGER_RULES_SQL = f"""
select t.tgrelid::regclass::text as table,
       t.tgname as name,
       pg_get_triggerdef(t.oid, true) as definition,
       t.tgenabled as state,
       p.oid::regprocedure::text as function,
       pg_get_functiondef(p.oid) as function_source
from pg_trigger t
join pg_proc p on p.oid = t.tgfoid
join pg_class r on r.oid = t.tgrelid
join pg_namespace n on n.oid = r.relnamespace
where not t.tgisinternal
  and {SCHEMA_FILTER}
  and {RULE_TABLE_FILTER.format(column='r.relname')}
order by 1, 2
limit {MAX_RULES_PER_KIND}
"""

REWRITE_RULES_SQL = f"""
select n.nspname || '.' || r.tablename as table, r.rulename as name, r.definition
from pg_rules r
join pg_namespace n on n.nspname = r.schemaname
where {SCHEMA_FILTER}
  and {RULE_TABLE_FILTER.format(column='r.tablename')}
order by 1, 2
limit {MAX_RULES_PER_KIND}
"""

POLICY_RULES_SQL = f"""
select n.nspname || '.' || p.tablename as table,
       p.policyname as name,
       p.permissive,
       p.roles,
       p.cmd as command,
       p.qual as using_expression,
       p.with_check as check_expression
from pg_policies p
join pg_namespace n on n.nspname = p.schemaname
where {SCHEMA_FILTER}
  and {RULE_TABLE_FILTER.format(column='p.tablename')}
order by 1, 2
limit {MAX_RULES_PER_KIND}
"""

ENUMS_SQL = f"""
select n.nspname || '.' || t.typname as name,
       array_agg(e.enumlabel order by e.enumsortorder) as values
from pg_type t
join pg_enum e on e.enumtypid = t.oid
join pg_namespace n on n.oid = t.typnamespace
where {SCHEMA_FILTER}
group by n.nspname, t.typname
order by 1
limit {MAX_RULES_PER_KIND}
"""

DOMAINS_SQL = f"""
select n.nspname || '.' || t.typname as name,
       format_type(t.typbasetype, t.typtypmod) as base_type,
       t.typnotnull as not_null,
       t.typdefault as default,
       array(
           select pg_get_constraintdef(c.oid, true)
           from pg_constraint c
           where c.contypid = t.oid
       ) as checks
from pg_type t
join pg_namespace n on n.oid = t.typnamespace
where t.typtype = 'd' and {SCHEMA_FILTER}
order by 1
limit {MAX_RULES_PER_KIND}
"""

COLUMN_RULES_SQL = f"""
select r.oid::regclass::text as table,
       a.attname as column,
       case when a.attgenerated <> '' then 'generated' else 'default' end as kind,
       pg_get_expr(d.adbin, d.adrelid) as expression
from pg_attrdef d
join pg_attribute a on a.attrelid = d.adrelid and a.attnum = d.adnum
join pg_class r on r.oid = d.adrelid
join pg_namespace n on n.oid = r.relnamespace
where r.relkind in ('r', 'p')
  and not a.attisdropped
  and pg_get_expr(d.adbin, d.adrelid) !~ '^nextval\\('
  and {SCHEMA_FILTER}
  and {RULE_TABLE_FILTER.format(column='r.relname')}
order by 1, 2
limit {MAX_RULES_PER_KIND}
"""


def list_rules(
    session: ReadOnlySession, schemas: list[str] | None, table: str | None
) -> dict[str, Any]:
    params = _params(schemas, table=table)
    triggers = session.records(TRIGGER_RULES_SQL, params, max_rows=MAX_RULES_PER_KIND)
    for trigger in triggers:
        trigger["state"] = TRIGGER_STATES.get(trigger["state"], trigger["state"])
        trigger["function_source"] = _truncate(
            trigger["function_source"], MAX_FUNCTION_SOURCE_CHARS
        )
    rules: dict[str, Any] = {
        "constraints": _labelled(
            session.records(CHECKS_SQL, params, max_rows=MAX_RULES_PER_KIND),
            "kind",
            CONSTRAINT_KINDS,
        ),
        "triggers": shape_records(triggers),
        "rewrite_rules": shape_records(
            session.records(REWRITE_RULES_SQL, params, max_rows=MAX_RULES_PER_KIND)
        ),
        "row_level_security_policies": shape_records(
            session.records(POLICY_RULES_SQL, params, max_rows=MAX_RULES_PER_KIND)
        ),
        "column_defaults_and_generated": shape_records(
            session.records(COLUMN_RULES_SQL, params, max_rows=MAX_RULES_PER_KIND)
        ),
    }
    if table is None:
        rules["enums"] = shape_records(
            session.records(ENUMS_SQL, params, max_rows=MAX_RULES_PER_KIND)
        )
        rules["domains"] = shape_records(
            session.records(DOMAINS_SQL, params, max_rows=MAX_RULES_PER_KIND)
        )
    rules = {key: value for key, value in rules.items() if value}
    rules["note"] = (
        "Regras que vivem só no código da aplicação não aparecem aqui. Cada lista "
        f"traz no máximo {MAX_RULES_PER_KIND} itens; filtre por tabela para detalhar."
    )
    return rules


# endregion


# region Definitions
ROUTINE_DEFINITION_SQL = f"""
select n.nspname || '.' || p.proname as name,
       p.prokind as kind,
       pg_get_function_identity_arguments(p.oid) as arguments,
       case when p.prokind <> 'a' then pg_get_functiondef(p.oid) end as definition,
       obj_description(p.oid, 'pg_proc') as comment
from pg_proc p
join pg_namespace n on n.oid = p.pronamespace
where p.proname = %(name)s
  and {SCHEMA_FILTER}
  and (%(schema)s::text is null or n.nspname = %(schema)s::text)
order by 1, 3
limit 10
"""

VIEW_DEFINITIONS_SQL = f"""
select n.nspname || '.' || c.relname as name,
       c.relkind as kind,
       pg_get_viewdef(c.oid, true) as definition,
       obj_description(c.oid, 'pg_class') as comment
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where c.relkind in ('v', 'm')
  and c.relname = %(name)s
  and {SCHEMA_FILTER}
  and (%(schema)s::text is null or n.nspname = %(schema)s::text)
order by 1
limit 10
"""


def get_definition(
    session: ReadOnlySession, schemas: list[str] | None, name: str, schema: str | None
) -> dict[str, Any]:
    if schema is None and "." in name:
        schema, name = name.split(".", 1)
    params = _params(schemas, name=name, schema=schema)
    routines = session.records(ROUTINE_DEFINITION_SQL, params, max_rows=10)
    for routine in routines:
        routine["kind"] = ROUTINE_KINDS.get(routine["kind"], routine["kind"])
        routine["definition"] = _truncate(routine["definition"], MAX_FUNCTION_SOURCE_CHARS * 2)
    views = session.records(VIEW_DEFINITIONS_SQL, params, max_rows=10)
    for view in views:
        view["kind"] = RELATION_KINDS.get(view["kind"], view["kind"])
    found = shape_records(routines + views)
    if not found:
        raise DatabaseAccessError(
            f"Nenhuma função, procedure ou view chamada '{name}'. Use list_routines "
            "ou list_tables para ver os nomes exatos."
        )
    return {"definitions": found}


# endregion


# region Data understanding
COLUMN_STATS_SQL = """
select s.attname as column,
       s.null_frac,
       s.n_distinct,
       s.avg_width,
       s.correlation,
       (s.most_common_vals::text::text[])[1:%(limit)s] as most_common_values,
       (s.most_common_freqs)[1:%(limit)s] as most_common_freqs,
       (s.histogram_bounds::text::text[])[1] as histogram_min,
       (s.histogram_bounds::text::text[])[array_length(s.histogram_bounds::text::text[], 1)] as histogram_max
from pg_stats s
where s.schemaname = %(schema)s and s.tablename = %(table)s
order by s.attname
"""


def _distinct_label(n_distinct: float | None) -> Any:
    if n_distinct is None:
        return None
    if n_distinct < 0:
        return f"{round(-n_distinct * 100, 1)}% das linhas"
    return int(n_distinct)


def column_profile(
    session: ReadOnlySession, schemas: list[str] | None, schema: str | None, table: str
) -> dict[str, Any]:
    relation = resolve_relation(session, schemas, schema, table)
    rows = session.records(
        COLUMN_STATS_SQL,
        {"schema": relation["schema"], "table": relation["name"], "limit": MAX_COMMON_VALUES},
    )
    profile = []
    for row in rows:
        column = row["column"]
        values = row["most_common_values"] or []
        profile.append(
            {
                "column": column,
                "null_fraction": row["null_frac"],
                "distinct": _distinct_label(row["n_distinct"]),
                "avg_width_bytes": row["avg_width"],
                "physical_order_correlation": row["correlation"],
                "most_common": [
                    {"value": shape_cell(column, value), "frequency": freq}
                    for value, freq in zip(values, row["most_common_freqs"] or [])
                ],
                "approx_min": shape_cell(column, row["histogram_min"]),
                "approx_max": shape_cell(column, row["histogram_max"]),
            }
        )
    payload: dict[str, Any] = {
        "table": f"{relation['schema']}.{relation['name']}",
        "columns": shape_records(profile),
    }
    if not profile:
        payload["note"] = (
            "Sem estatísticas para esta tabela: ela nunca passou por ANALYZE ou o "
            "usuário não tem SELECT nas colunas. Use sample_rows ou uma consulta."
        )
    return payload


def sample_rows(
    session: ReadOnlySession,
    schemas: list[str] | None,
    schema: str | None,
    table: str,
    limit: int = DEFAULT_SAMPLE_ROWS,
) -> dict[str, Any]:
    relation = resolve_relation(session, schemas, schema, table)
    bounded = max(1, min(int(limit), MAX_SAMPLE_ROWS))
    columns = session.records(COLUMNS_SQL, {"oid": relation["oid"]}, max_rows=5000)
    readable_columns = [column["name"] for column in columns if column["readable"]]
    omitted_columns = [column["name"] for column in columns if not column["readable"]]
    payload: dict[str, Any] = {"table": f"{relation['schema']}.{relation['name']}"}
    if not readable_columns:
        payload["note"] = "O usuário não tem SELECT em nenhuma coluna desta tabela."
        return payload
    query = pgsql.SQL("select {} from {}.{} limit {}").format(
        pgsql.SQL(", ").join(pgsql.Identifier(column) for column in readable_columns),
        pgsql.Identifier(relation["schema"]),
        pgsql.Identifier(relation["name"]),
        pgsql.Literal(bounded),
    )
    result = session.fetch(query, max_rows=bounded)
    result.truncated = False
    payload.update(shape_rows(result))
    if omitted_columns:
        payload["omitted_columns"] = omitted_columns
    return payload


RELATION_COLUMNS_SQL = f"""
select n.nspname as schema, c.relname as table, a.attname as column,
       format_type(a.atttypid, a.atttypmod) as data_type,
       col_description(a.attrelid, a.attnum) as text
from pg_attribute a
join pg_class c on c.oid = a.attrelid
join pg_namespace n on n.oid = c.relnamespace
where c.relkind in ('r', 'p') and not c.relispartition and a.attnum > 0 and not a.attisdropped
  and {SCHEMA_FILTER}
  and (%(schema)s::text is null or n.nspname = %(schema)s::text)
"""

SINGLE_KEYS_SQL = f"""
select n.nspname as schema, c.relname as table, a.attname as column
from pg_constraint k
join pg_class c on c.oid = k.conrelid
join pg_namespace n on n.oid = c.relnamespace
join pg_attribute a on a.attrelid = k.conrelid and a.attnum = k.conkey[1]
where k.contype in ('p', 'u') and cardinality(k.conkey) = 1 and {SCHEMA_FILTER}
"""

DECLARED_FK_COLUMNS_SQL = f"""
select n.nspname as schema, c.relname as table, a.attname as column
from pg_constraint k
join pg_class c on c.oid = k.conrelid
join pg_namespace n on n.oid = c.relnamespace
join pg_attribute a on a.attrelid = k.conrelid and a.attnum = any(k.conkey)
where k.contype = 'f' and {SCHEMA_FILTER}
"""

RELATIONSHIP_COLUMNS = 10_000


def suggest_table_relationships(
    session: ReadOnlySession, schemas: list[str] | None, schema: str | None
) -> dict[str, Any]:
    column_rows = session.records(
        RELATION_COLUMNS_SQL, _params(schemas, schema=schema), max_rows=RELATIONSHIP_COLUMNS
    )
    columns = {
        (row["schema"], row["table"], row["column"]): ColumnInfo(
            schema=row["schema"],
            table=row["table"],
            column=row["column"],
            data_type=row["data_type"],
            text=row["text"],
        )
        for row in column_rows
    }
    keys = [
        columns[key]
        for key in (
            (row["schema"], row["table"], row["column"])
            for row in session.records(SINGLE_KEYS_SQL, _params(schemas), max_rows=5000)
        )
        if key in columns
    ]
    declared = {
        (row["schema"], row["table"], row["column"])
        for row in session.records(DECLARED_FK_COLUMNS_SQL, _params(schemas), max_rows=5000)
    }
    return suggest_relationships(list(columns.values()), keys, declared)


KNOWN_SCHEMAS_SQL = "select nspname as name from pg_namespace"


def known_schemas(session: ReadOnlySession) -> set[str]:
    return {row["name"] for row in session.records(KNOWN_SCHEMAS_SQL, max_rows=5000)}


# endregion


def check_schema_scope(target: DatabaseTarget, schema: str | None, name: str | None = None) -> None:
    """Refuse a schema outside the allowlist instead of silently returning nothing."""
    if schema is None and name and "." in name:
        schema = name.split(".", 1)[0]
    if not schema or not target.schemas:
        return
    allowed = {allowed.lower() for allowed in target.schemas}
    if schema.strip('"').lower() not in allowed:
        raise DatabaseAccessError(
            f"O schema '{schema}' está fora dos liberados para este banco "
            f"({', '.join(target.schemas)}). Use database_overview para ver os schemas disponíveis."
        )


class PostgresEngine(Engine):
    name = "postgres"
    dialect = DIALECT

    def open_session(self, target: DatabaseTarget, check_posture: bool = True):
        return readonly_session(target, check_posture=check_posture)

    def run_query(self, session: ReadOnlySession, query: str, max_rows: int) -> QueryResult:
        return session.fetch(query, max_rows=max_rows, resolve_sources=True)

    def check_connection(self, target: DatabaseTarget) -> dict[str, Any]:
        try:
            with readonly_session(target, check_posture=False) as session:
                posture = read_posture(session, target.schemas)
                schemas = [row["name"] for row in session.records(USER_SCHEMAS_SQL)]
                sensitive = readable_sensitive_columns(session, target.schemas)
        except DatabaseAccessError as exc:
            return {"ok": False, "error": str(exc)}
        warnings = posture.warnings(target.schemas)
        if sensitive:
            warnings.append(sensitive_columns_warning(posture.role, sensitive))
        missing = [name for name in target.schemas if name not in schemas]
        if missing:
            warnings.append(f"Schemas liberados que não existem no banco: {', '.join(missing)}.")
        refusal = posture.refusal(target.alias)
        return {
            "ok": refusal is None,
            "server_version": posture.server_version,
            "role": posture.role,
            "read_only_role": not posture.can_write and not posture.is_superuser,
            "schemas": schemas,
            "warnings": warnings,
            "error": refusal,
        }

    def known_schemas(self, session, candidates: set[str]) -> set[str]:
        return known_schemas(session)

    def catalog(self) -> dict[str, CatalogOperation]:
        def scoped(operation, name_arg=None):
            def run(session, target, **args):
                check_schema_scope(target, args.get("schema"), args.get(name_arg) if name_arg else None)
                return operation(session, target, **args)

            return run

        return {
            "database_overview": lambda session, target: database_overview(
                session, target.schema_list
            ),
            "list_tables": scoped(
                lambda session, target, schema=None, name_filter=None: list_tables(
                    session, target.schema_list, schema, name_filter
                )
            ),
            "list_routines": scoped(
                lambda session, target, schema=None, name_filter=None: list_routines(
                    session, target.schema_list, schema, name_filter
                )
            ),
            "describe_table": scoped(
                lambda session, target, table, schema=None: describe_table(
                    session, target.schema_list, schema, table
                ),
                "table",
            ),
            "list_relationships": scoped(
                lambda session, target, schema=None: list_relationships(
                    session, target.schema_list, schema
                )
            ),
            "suggest_relationships": scoped(
                lambda session, target, schema=None: suggest_table_relationships(
                    session, target.schema_list, schema
                )
            ),
            "list_rules": scoped(
                lambda session, target, table=None: list_rules(
                    session, target.schema_list, table
                ),
                "table",
            ),
            "get_definition": scoped(
                lambda session, target, name, schema=None: get_definition(
                    session, target.schema_list, name, schema
                ),
                "name",
            ),
            "column_profile": scoped(
                lambda session, target, table, schema=None: column_profile(
                    session, target.schema_list, schema, table
                ),
                "table",
            ),
            "sample_rows": scoped(
                lambda session, target, table, schema=None, limit=DEFAULT_SAMPLE_ROWS: sample_rows(
                    session, target.schema_list, schema, table, limit
                ),
                "table",
            ),
        }


ENGINE = PostgresEngine()

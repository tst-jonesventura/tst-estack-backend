"""SQL Server engine over python-tds: rolled-back transactions, posture and ``sys.*`` catalog.

SQL Server has no read-only session, so every statement runs in a transaction that is
always rolled back, the guard refuses anything that writes or leaves the database, and the
posture refuses users with write, DDL or administrative rights.
"""
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

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
    schema_qualifiers,
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

SQLSERVER_PORT = 1433
QUERY_TIMEOUT_SECONDS = 15
CONNECT_TIMEOUT_SECONDS = 10
CATALOG_ROWS = 1000
RELATIONSHIP_COLUMNS = 10_000
MAX_LISTED_OBJECTS = 500
MAX_RULES_PER_KIND = 300
MAX_SOURCE_CHARS = 12_000
LARGEST_TABLES = 15
APPLICATION_NAME = "estack-mcp"
SESSION_SETTINGS = "SET LOCK_TIMEOUT 2000; SET TEXTSIZE 65536; SET DEADLOCK_PRIORITY LOW;"
TLS_MODES = ("require", "verify-ca", "verify-full")
SYSTEM_SCHEMAS = ("sys", "INFORMATION_SCHEMA", "guest")

REFUSED_SERVER_ROLES = ("sysadmin", "securityadmin", "serveradmin")
REFUSED_DATABASE_ROLES = ("db_owner", "db_securityadmin", "db_accessadmin", "db_ddladmin")
REFUSED_SERVER_PERMISSIONS = frozenset(
    {
        "CONTROL SERVER",
        "ALTER ANY LOGIN",
        "ALTER ANY LINKED SERVER",
        "ALTER ANY CREDENTIAL",
        "ALTER SETTINGS",
        "ALTER SERVER STATE",
        "ADMINISTER BULK OPERATIONS",
        "IMPERSONATE ANY LOGIN",
        "UNMASK",
    }
)
REFUSED_DATABASE_PERMISSIONS = frozenset(
    {
        "CONTROL",
        "ALTER",
        "ALTER ANY USER",
        "ALTER ANY ROLE",
        "ALTER ANY SCHEMA",
        "ALTER ANY SECURITY POLICY",
        "ALTER ANY MASK",
        "TAKE OWNERSHIP",
        "UNMASK",
        "CREATE TABLE",
        "CREATE PROCEDURE",
        "CREATE FUNCTION",
        "EXECUTE ANY EXTERNAL SCRIPT",
    }
)

IDENTIFIER_BOUNDARY = r"(?<![\w@#$])"
IDENTIFIER_END = r"(?![\w@#$])"
NAME_PART = r'(?:\[[^\]]*\]|"[^"]*"|[A-Za-z_@#][\w@#$]*)'
FOUR_PART_NAME = rf"{NAME_PART}\s*\.\s*{NAME_PART}\s*\.\s*{NAME_PART}\s*\.\s*{NAME_PART}"
DIALECT = Dialect(
    name="sqlserver",
    blocked_functions=(
        "OPENROWSET",
        "OPENQUERY",
        "OPENDATASOURCE",
        "OPENXML",
        "FN_GET_AUDIT_FILE",
        "FN_XE_FILE_TARGET_READ_FILE",
        "FN_DBLOG",
        "FN_DUMP_DBLOG",
    ),
    system_schemas=("sys", "INFORMATION_SCHEMA"),
    blocked_prefixes=("xp_", "sp_"),
    blocked_relations=("sql_logins", "syslogins", "sysxlogins", "linked_logins", "credentials", "server_principal_credentials"),
    query_keywords=("select",),
    bracket_identifiers=True,
    national_strings=True,
    row_limit="TOP ({rows})",
    limit_position="prefix",
    refused_patterns=(
        (rf"{IDENTIFIER_BOUNDARY}INTO{IDENTIFIER_END}", "SELECT ... INTO cria uma tabela e não é permitido."),
        (rf"{IDENTIFIER_BOUNDARY}EXEC(?:UTE)?{IDENTIFIER_END}", "Executar procedures (EXEC) não é permitido em consultas de leitura."),
        (rf"{IDENTIFIER_BOUNDARY}WAITFOR{IDENTIFIER_END}", "WAITFOR não é permitido em consultas de leitura."),
        (r"NEXT\s+VALUE\s+FOR", "Ler NEXT VALUE FOR avança a sequence e não é permitido."),
        (FOUR_PART_NAME, "Nomes com 4 partes ({match}) apontam para outro servidor (linked server) e não são permitidos."),
        (rf"{IDENTIFIER_BOUNDARY}(?:DBCC|BULK|SHUTDOWN|KILL|RECONFIGURE){IDENTIFIER_END}", "O comando {match} não é permitido em consultas de leitura."),
    ),
)

OBJECT_KINDS = {
    "U": "table",
    "V": "view",
    "P": "procedure",
    "PC": "procedure (CLR)",
    "FN": "scalar function",
    "IF": "inline table function",
    "TF": "table function",
    "FS": "scalar function (CLR)",
    "FT": "table function (CLR)",
    "TR": "trigger",
    "SO": "sequence",
}
ROUTINE_TYPES = ("P", "PC", "FN", "IF", "TF", "FS", "FT")

QUERY_ERRORS = {
    1033: (
        "No SQL Server, ORDER BY dentro da consulta exige TOP. Use SELECT TOP (n) ... ORDER BY ... "
        "ou tire o ORDER BY."
    ),
    1222: "A consulta esperou demais por um bloqueio e foi cancelada. Tente de novo em instantes.",
    8152: "Um valor não coube no tipo esperado. Revise conversões e tamanhos.",
}
PERMISSION_ERRORS = (229, 230, 262, 297, 300, 916)
MISSING_OBJECT_ERRORS = (208, 207, 4121)
CONNECTION_FAILURES = {
    18456: "usuário ou senha recusados",
    18486: "a conta está bloqueada no SQL Server",
    18487: "a senha expirou; troque-a no SQL Server",
    4060: "o banco informado não existe ou o usuário não tem acesso a ele",
}
TIMEOUT_MESSAGE = (
    f"A consulta passou do limite de {QUERY_TIMEOUT_SECONDS}s e foi cancelada. "
    "Use filtros, TOP ou agregações mais estreitas."
)


def _load_driver():
    import pytds

    return pytds


def _certificates_file(target: DatabaseTarget) -> str:
    configured = target.option("ca_file")
    if configured:
        return configured
    import certifi

    return certifi.where()


# region Errors
def _error_number(exc: Exception) -> int | None:
    number = getattr(exc, "number", None)
    return number if isinstance(number, int) else None


def _first_line(exc: Exception) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def _is_timeout(exc: Exception) -> bool:
    return isinstance(exc, TimeoutError) or type(exc).__name__ == "TimeoutError"


def describe_query_error(exc: Exception) -> str:
    if _is_timeout(exc):
        return TIMEOUT_MESSAGE
    number = _error_number(exc)
    if number in QUERY_ERRORS:
        return QUERY_ERRORS[number]
    if number in PERMISSION_ERRORS:
        return f"O usuário não tem permissão para isso ({_first_line(exc)})."
    if number in MISSING_OBJECT_ERRORS:
        return f"Objeto ou coluna não existe, ou o usuário não o enxerga ({_first_line(exc)})."
    return f"Erro do SQL Server ({number or 'desconhecido'}): {_first_line(exc)}"[:600]


def describe_connection_error(exc: Exception, alias: str) -> str:
    number = _error_number(exc)
    text = str(exc).lower()
    if number in CONNECTION_FAILURES:
        reason = CONNECTION_FAILURES[number]
    elif "certificate" in text or "ssl" in text or "tls" in text:
        reason = (
            "falha de TLS: o certificado do servidor não foi validado. Para certificado de CA "
            "interna ou autoassinado, use o conector com options.ca_file"
        )
    elif _is_timeout(exc) or "timed out" in text:
        reason = "tempo de conexão esgotado; confira rede, firewall e a porta"
    else:
        reason = "servidor inacessível; confira host, porta e firewall"
    return f"Não foi possível conectar ao banco '{alias}': {reason}."


# endregion


# region Session
class Binds:
    """Named parameters (pyformat) for one statement."""

    def __init__(self):
        self.values: dict[str, Any] = {}

    def add(self, value: Any) -> str:
        name = f"b{len(self.values)}"
        self.values[name] = value
        return f"%({name})s"

    def many(self, values: list[Any]) -> str:
        return ", ".join(self.add(value) for value in values)


class SqlServerSession:
    """One connection; every statement inside a transaction that is always rolled back."""

    def __init__(self, target: DatabaseTarget, connection: Any, driver: Any):
        self.target = target
        self._connection = connection
        self._driver = driver
        self._scope: list[str] | None = None

    def fetch(self, query: str, params: Any = None, max_rows: int = MAX_QUERY_ROWS) -> QueryResult:
        try:
            cursor = self._connection.cursor()
            try:
                cursor.execute(query, params or None)
                if cursor.description is None:
                    return QueryResult(columns=[], rows=[])
                columns = [column[0] for column in cursor.description]
                rows = cursor.fetchmany(max_rows + 1)
            finally:
                cursor.close()
        except Exception as exc:
            if isinstance(exc, DatabaseAccessError):
                raise
            raise DatabaseAccessError(describe_query_error(exc)) from exc
        finally:
            self._rollback()
        return QueryResult(
            columns=columns,
            rows=[list(row) for row in rows[:max_rows]],
            truncated=len(rows) > max_rows,
        )

    def _rollback(self) -> None:
        try:
            self._connection.rollback()
        except Exception:  # noqa: BLE001
            logger.warning("Rollback failed for SQL Server alias '%s'", self.target.alias)

    def records(self, query: str, params: Any = None, max_rows: int = CATALOG_ROWS) -> list[dict[str, Any]]:
        return self.fetch(query, params, max_rows).records()

    def one(self, query: str, params: Any = None) -> dict[str, Any] | None:
        found = self.records(query, params, max_rows=1)
        return found[0] if found else None

    def close(self) -> None:
        try:
            self._connection.close()
        except Exception:  # noqa: BLE001
            logger.debug("Closing SQL Server connection for alias '%s' failed", self.target.alias)


def _connect_params(target: DatabaseTarget) -> dict[str, Any]:
    """Connection arguments; every TLS mode validates the certificate chain."""
    encrypted = target.ssl_mode in TLS_MODES
    verify_name = target.ssl_mode == "verify-full"
    params: dict[str, Any] = {
        "server": target.host,
        "port": target.port or SQLSERVER_PORT,
        "database": target.database,
        "user": target.username,
        "password": target.password,
        "timeout": QUERY_TIMEOUT_SECONDS,
        "login_timeout": CONNECT_TIMEOUT_SECONDS,
        "autocommit": False,
        "appname": APPLICATION_NAME,
    }
    if public_network_only(target):
        check_ssl_mode(target)
        addresses = resolve_allowed_addresses(target)
        if not verify_name:
            params["server"] = addresses[0]
    if encrypted:
        params["cafile"] = _certificates_file(target)
        params["validate_host"] = verify_name
    return params


def open_connection(target: DatabaseTarget) -> SqlServerSession:
    driver = _load_driver()
    try:
        connection = driver.connect(**_connect_params(target))
    except DatabaseAccessError:
        raise
    except Exception as exc:
        logger.warning(
            "SQL Server connection failed for alias '%s' (%s:%s/%s): %s",
            target.alias,
            target.host,
            target.port,
            target.database,
            _error_number(exc) or type(exc).__name__,
        )
        raise DatabaseAccessError(describe_connection_error(exc, target.alias)) from exc
    session = SqlServerSession(target, connection, driver)
    session.fetch(SESSION_SETTINGS)
    return session


# endregion


# region Posture
@dataclass(frozen=True)
class Posture:
    server_version: str
    role: str
    server_roles: frozenset[str]
    database_roles: frozenset[str]
    server_permissions: frozenset[str]
    database_permissions: frozenset[str]
    impersonates: bool = False
    write_grants: bool = False
    execute_grants: bool = False
    readable_outside_scope: tuple[str, ...] = ()

    @property
    def refused(self) -> list[str]:
        roles = sorted(self.server_roles | self.database_roles)
        permissions = sorted(
            (self.server_permissions & REFUSED_SERVER_PERMISSIONS)
            | (self.database_permissions & REFUSED_DATABASE_PERMISSIONS)
        )
        impersonation = ["IMPERSONATE"] if self.impersonates else []
        return roles + permissions + impersonation

    @property
    def can_write(self) -> bool:
        return self.write_grants

    def warnings(self) -> list[str]:
        notes = []
        if self.write_grants:
            notes.append(
                "O usuário tem permissão de escrita (db_datawriter ou INSERT/UPDATE/DELETE). As "
                "travas impedem escritas, mas o recomendado é um usuário só com SELECT."
            )
        if self.execute_grants:
            notes.append(
                "O usuário pode executar procedures ou funções da aplicação. Consultas livres não "
                "chamam procedures, mas o recomendado é não conceder EXECUTE ao usuário de leitura."
            )
        return notes

    def refusal(self, alias: str) -> str | None:
        if self.refused:
            extra = len(self.refused) - 4
            listed = ", ".join(self.refused[:4]) + (f" e mais {extra}" if extra > 0 else "")
            return (
                f"O usuário '{self.role}' do banco '{alias}' tem {listed}. Por segurança o acesso "
                "foi recusado: use um usuário só com db_datareader (ou SELECT nos schemas "
                "liberados) e VIEW DEFINITION."
            )
        if self.readable_outside_scope:
            return (
                f"O usuário '{self.role}' consegue ler tabelas de schemas fora dos liberados "
                f"({', '.join(self.readable_outside_scope[:5])}). Restrinja o SELECT aos schemas "
                "liberados."
            )
        return None


IDENTITY_SQL = "select cast(serverproperty('ProductVersion') as nvarchar(64)) as version, user_name() as role"
SERVER_ROLES_SQL = (
    "select r.name from (values ('sysadmin'), ('securityadmin'), ('serveradmin')) as r(name) "
    "where is_srvrolemember(r.name) = 1"
)
DATABASE_ROLES_SQL = (
    "select r.name from (values ('db_owner'), ('db_securityadmin'), ('db_accessadmin'), "
    "('db_ddladmin')) as r(name) where is_rolemember(r.name) = 1"
)
SERVER_PERMISSIONS_SQL = "select permission_name from fn_my_permissions(null, 'SERVER')"
DATABASE_PERMISSIONS_SQL = "select permission_name from fn_my_permissions(null, 'DATABASE')"
GRANTS_SQL = (
    "select "
    "(select count(*) from sys.database_permissions where grantee_principal_id = database_principal_id() "
    "and permission_name = 'IMPERSONATE' and state in ('G', 'W')) as impersonates, "
    "(select count(*) from sys.database_permissions where grantee_principal_id = database_principal_id() "
    "and permission_name in ('INSERT', 'UPDATE', 'DELETE') and state in ('G', 'W')) "
    "+ is_rolemember('db_datawriter') as writes, "
    "(select count(*) from sys.database_permissions where grantee_principal_id = database_principal_id() "
    "and permission_name = 'EXECUTE' and state in ('G', 'W')) as executes"
)
OUTSIDE_SCOPE_SQL = (
    "select distinct top (20) s.name as schema_name from sys.objects o "
    "join sys.schemas s on s.schema_id = o.schema_id "
    "where o.type in ('U', 'V') and o.is_ms_shipped = 0 and s.name not in ({schemas}) "
    "and has_perms_by_name(quotename(s.name) + '.' + quotename(o.name), 'OBJECT', 'SELECT') = 1"
)


def _names(rows: list[dict[str, Any]], key: str) -> frozenset[str]:
    return frozenset(row[key] for row in rows if row.get(key))


def read_posture(session: SqlServerSession, schemas: tuple[str, ...] = ()) -> Posture:
    identity = session.one(IDENTITY_SQL) or {}
    grants = session.one(GRANTS_SQL) or {}
    outside: tuple[str, ...] = ()
    if schemas:
        binds = Binds()
        outside = tuple(
            row["schema_name"]
            for row in session.records(OUTSIDE_SCOPE_SQL.format(schemas=binds.many(list(schemas))), binds.values)
        )
    return Posture(
        server_version=identity.get("version") or "",
        role=identity.get("role") or "",
        server_roles=_names(session.records(SERVER_ROLES_SQL), "name"),
        database_roles=_names(session.records(DATABASE_ROLES_SQL), "name"),
        server_permissions=_names(session.records(SERVER_PERMISSIONS_SQL), "permission_name"),
        database_permissions=_names(session.records(DATABASE_PERMISSIONS_SQL), "permission_name"),
        impersonates=bool(grants.get("impersonates")),
        write_grants=bool(grants.get("writes")),
        execute_grants=bool(grants.get("executes")),
        readable_outside_scope=outside,
    )


def enforce_posture(target: DatabaseTarget, session: SqlServerSession) -> Posture:
    posture = _posture_cache.get(target.cache_key)
    if posture is None:
        posture = read_posture(session, target.schemas)
        _posture_cache.put(target.cache_key, posture)
    refusal = posture.refusal(target.alias)
    if refusal:
        raise DatabaseAccessError(refusal)
    return posture


@contextmanager
def readonly_session(target: DatabaseTarget, check_posture: bool = True) -> Iterator[SqlServerSession]:
    session = open_connection(target)
    try:
        if check_posture:
            enforce_posture(target, session)
        yield session
    finally:
        session.close()


# endregion


# region Scope
VISIBLE_SCHEMAS_SQL = (
    "select distinct top (200) s.name as schema_name from sys.schemas s "
    "join sys.objects o on o.schema_id = s.schema_id "
    "where o.type in ('U', 'V') and o.is_ms_shipped = 0 order by s.name"
)
NO_SCHEMAS = (
    "O usuário do banco '{alias}' não enxerga tabelas em nenhum schema. Dê SELECT nos schemas "
    "e libere-os no cadastro."
)


def schema_scope(session: SqlServerSession, schema: str | None = None) -> list[str]:
    """Schemas an operation may read: the allowlist, else every schema with visible tables."""
    if session._scope is None:
        configured = list(session.target.schemas)
        session._scope = configured or [row["schema_name"] for row in session.records(VISIBLE_SCHEMAS_SQL)]
    if not session._scope:
        raise DatabaseAccessError(NO_SCHEMAS.format(alias=session.target.alias))
    if schema is None:
        return session._scope
    requested = schema.strip("[]")
    for allowed in session._scope:
        if allowed.lower() == requested.lower():
            return [allowed]
    raise DatabaseAccessError(
        f"O schema '{requested}' está fora dos liberados para este banco ({', '.join(session._scope)})."
    )


def _like(name_filter: str | None) -> str | None:
    if not name_filter:
        return None
    escaped = name_filter.replace("[", "[[]").replace("%", "[%]").replace("_", "[_]")
    return f"%{escaped}%"


def _cap(rows: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    payload: dict[str, Any] = {"items": shape_records(rows[:limit]), "count": min(len(rows), limit)}
    if len(rows) > limit:
        payload["truncated"] = True
        payload["note"] = "Lista cortada. Filtre por schema ou por parte do nome."
    return payload


def _truncate(text: str | None, limit: int = MAX_SOURCE_CHARS) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return f"{text[:limit]}\n-- … (+{len(text) - limit} caracteres omitidos)"


def _split(columns: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in (columns or "").split(",") if part.strip())


def _kind(code: str | None) -> str | None:
    code = (code or "").strip()
    return OBJECT_KINDS.get(code, code.lower() or None)


# endregion


# region Overview and listings
DESCRIPTION = (
    "(select cast(ep.value as nvarchar(4000)) from sys.extended_properties ep where ep.class = 1 "
    "and ep.major_id = {object_id} and ep.minor_id = {minor_id} and ep.name = 'MS_Description')"
)
ROWS = "(select sum(p.rows) from sys.partitions p where p.object_id = {object_id} and p.index_id in (0, 1))"
OBJECT_COUNTS_SQL = (
    "select s.name as schema_name, o.type as kind, count(*) as n from sys.objects o "
    "join sys.schemas s on s.schema_id = o.schema_id where s.name in ({schemas}) and o.is_ms_shipped = 0 "
    "and o.type in ('U', 'V', 'P', 'PC', 'FN', 'IF', 'TF', 'FS', 'FT', 'TR', 'SO') group by s.name, o.type"
)
LARGEST_TABLES_SQL = (
    "select top ({limit}) s.name as schema_name, o.name as table_name, "
    f"{ROWS.format(object_id='o.object_id')} as rows "
    "from sys.objects o join sys.schemas s on s.schema_id = o.schema_id "
    "where o.type = 'U' and o.is_ms_shipped = 0 and s.name in ({schemas}) order by rows desc"
)


def database_overview(session: SqlServerSession, target: DatabaseTarget) -> dict[str, Any]:
    schemas = schema_scope(session)
    binds = Binds()
    in_schemas = binds.many(schemas)
    counts: dict[str, dict[str, int]] = {name: {} for name in schemas}
    for row in session.records(OBJECT_COUNTS_SQL.format(schemas=in_schemas), binds.values):
        kind = _kind(row["kind"]) or "other"
        counts.setdefault(row["schema_name"], {})[kind] = row["n"]
    largest = session.records(LARGEST_TABLES_SQL.format(schemas=in_schemas, limit=LARGEST_TABLES), binds.values)
    identity = session.one(IDENTITY_SQL) or {}
    edition = session.one("select cast(serverproperty('Edition') as nvarchar(128)) as edition") or {}
    return {
        "server_version": identity.get("version"),
        "edition": edition.get("edition"),
        "database": target.database,
        "connected_as": identity.get("role"),
        "schemas": [{"schema": name, "objects": counts.get(name, {})} for name in schemas],
        "largest_tables": shape_records(largest),
        "note": (
            "No SQL Server muitas regras de negócio ficam em stored procedures e triggers: veja "
            "list_routines e get_definition. A descrição das colunas (MS_Description) é a documentação."
        ),
    }


LIST_TABLES_SQL = (
    "select top ({limit}) s.name as schema_name, o.name as table_name, o.type as kind, "
    f"{DESCRIPTION.format(object_id='o.object_id', minor_id=0)} as description, "
    f"{ROWS.format(object_id='o.object_id')} as rows "
    "from sys.objects o join sys.schemas s on s.schema_id = o.schema_id "
    "where o.type in ('U', 'V') and o.is_ms_shipped = 0 and s.name in ({schemas}) {name_clause} "
    "order by s.name, o.name"
)


def list_tables(
    session: SqlServerSession, target: DatabaseTarget, schema: str | None = None, name_filter: str | None = None
) -> dict[str, Any]:
    schemas = schema_scope(session, schema)
    binds = Binds()
    in_schemas = binds.many(schemas)
    pattern = _like(name_filter)
    name_clause = ""
    if pattern:
        bound = binds.add(pattern)
        name_clause = (
            f"and (o.name like {bound} or "
            f"{DESCRIPTION.format(object_id='o.object_id', minor_id=0)} like {bound})"
        )
    rows = session.records(
        LIST_TABLES_SQL.format(schemas=in_schemas, name_clause=name_clause, limit=MAX_LISTED_OBJECTS + 1),
        binds.values,
        max_rows=MAX_LISTED_OBJECTS + 1,
    )
    for row in rows:
        row["kind"] = _kind(row["kind"])
    payload = _cap(rows, MAX_LISTED_OBJECTS)
    if schema and not rows:
        payload["note"] = f"Nenhuma tabela visível no schema '{schema}'."
    return payload


LIST_ROUTINES_SQL = (
    "select top ({limit}) s.name as schema_name, o.name as routine_name, o.type as kind, o.modify_date, "
    f"{DESCRIPTION.format(object_id='o.object_id', minor_id=0)} as description "
    "from sys.objects o join sys.schemas s on s.schema_id = o.schema_id "
    "where o.type in ('P', 'PC', 'FN', 'IF', 'TF', 'FS', 'FT') and o.is_ms_shipped = 0 "
    "and s.name in ({schemas}) {name_clause} order by s.name, o.name"
)


def list_routines(
    session: SqlServerSession, target: DatabaseTarget, schema: str | None = None, name_filter: str | None = None
) -> dict[str, Any]:
    schemas = schema_scope(session, schema)
    binds = Binds()
    in_schemas = binds.many(schemas)
    pattern = _like(name_filter)
    name_clause = f"and o.name like {binds.add(pattern)}" if pattern else ""
    rows = session.records(
        LIST_ROUTINES_SQL.format(schemas=in_schemas, name_clause=name_clause, limit=MAX_LISTED_OBJECTS + 1),
        binds.values,
        max_rows=MAX_LISTED_OBJECTS + 1,
    )
    for row in rows:
        row["kind"] = _kind(row["kind"])
    return _cap(rows, MAX_LISTED_OBJECTS)


# endregion


# region Describe
RESOLVE_SQL = (
    "select s.name as schema_name, o.name as object_name, o.type as kind, o.object_id "
    "from sys.objects o join sys.schemas s on s.schema_id = o.schema_id "
    "where o.type in ('U', 'V') and s.name in ({schemas}) and o.name = {name} order by s.name"
)
COLUMNS_SQL = (
    "select c.name as column_name, t.name as data_type, c.max_length, c.precision, c.scale, "
    "c.is_nullable, c.is_identity, c.is_computed, cc.definition as computed_definition, "
    "dc.definition as default_definition, "
    f"{DESCRIPTION.format(object_id='c.object_id', minor_id='c.column_id')} as description, "
    "mc.masking_function from sys.columns c join sys.types t on t.user_type_id = c.user_type_id "
    "left join sys.computed_columns cc on cc.object_id = c.object_id and cc.column_id = c.column_id "
    "left join sys.default_constraints dc on dc.parent_object_id = c.object_id and dc.parent_column_id = c.column_id "
    "left join sys.masked_columns mc on mc.object_id = c.object_id and mc.column_id = c.column_id "
    "where c.object_id = %(object_id)s order by c.column_id"
)
INDEX_COLUMNS = (
    "(select string_agg(col_name(ic.object_id, ic.column_id) + case when ic.is_descending_key = 1 "
    "then ' DESC' else '' end, ', ') within group (order by ic.key_ordinal) from sys.index_columns ic "
    "where ic.object_id = {table} and ic.index_id = {index} and ic.is_included_column = 0)"
)
KEYS_SQL = (
    "select k.name as constraint_name, k.type as kind, "
    f"{INDEX_COLUMNS.format(table='k.parent_object_id', index='k.unique_index_id')} as columns "
    "from sys.key_constraints k where k.parent_object_id = %(object_id)s order by k.type, k.name"
)
CHECKS_SQL = (
    "select ck.name as constraint_name, ck.definition, ck.is_disabled, "
    "object_schema_name(ck.parent_object_id) as schema_name, object_name(ck.parent_object_id) as table_name "
    "from sys.check_constraints ck where {where} order by table_name, ck.name"
)
FK_COLUMNS = (
    "(select string_agg(col_name(fc.{side}_object_id, fc.{side}_column_id), ', ') "
    "within group (order by fc.constraint_column_id) from sys.foreign_key_columns fc "
    "where fc.constraint_object_id = fk.object_id)"
)
FOREIGN_KEYS_SQL = (
    "select top ({limit}) fk.name as constraint_name, "
    "object_schema_name(fk.parent_object_id) as from_schema, object_name(fk.parent_object_id) as from_table, "
    "object_schema_name(fk.referenced_object_id) as to_schema, object_name(fk.referenced_object_id) as to_table, "
    "fk.delete_referential_action_desc as on_delete, fk.is_disabled, "
    f"{FK_COLUMNS.format(side='parent')} as from_columns, {FK_COLUMNS.format(side='referenced')} as to_columns "
    "from sys.foreign_keys fk where {where} order by from_table, fk.name"
)
INDEXES_SQL = (
    "select i.name as index_name, i.type_desc, i.is_unique, i.is_primary_key, i.has_filter, "
    f"i.filter_definition, {INDEX_COLUMNS.format(table='i.object_id', index='i.index_id')} as columns "
    "from sys.indexes i where i.object_id = %(object_id)s and i.index_id > 0 order by i.index_id"
)
TRIGGERS_SQL = (
    "select top ({limit}) object_schema_name(tr.parent_id) as schema_name, object_name(tr.parent_id) as table_name, "
    "tr.name as trigger_name, tr.is_disabled, tr.is_instead_of_trigger, "
    "(select string_agg(te.type_desc, ', ') from sys.trigger_events te where te.object_id = tr.object_id) as events, "
    "m.definition from sys.triggers tr left join sys.sql_modules m on m.object_id = tr.object_id "
    "where tr.parent_class = 1 and {where} order by table_name, tr.name"
)
POLICIES_SQL = (
    "select top ({limit}) object_schema_name(sp.target_object_id) as schema_name, "
    "object_name(sp.target_object_id) as table_name, p.name as policy_name, p.is_enabled, "
    "sp.predicate_type_desc, sp.operation_desc, sp.predicate_definition "
    "from sys.security_predicates sp join sys.security_policies p on p.object_id = sp.object_id "
    "where {where} order by table_name, p.name"
)
CHECK_TABLE = "ck.parent_object_id"
TRIGGER_TABLE = "tr.parent_id"
POLICY_TABLE = "sp.target_object_id"
MODULE_SQL = "select definition from sys.sql_modules where object_id = %(object_id)s"
USED_BY_SQL = (
    "select distinct top (100) object_schema_name(d.referencing_id) as schema_name, "
    "object_name(d.referencing_id) as object_name, o.type as kind "
    "from sys.sql_expression_dependencies d join sys.objects o on o.object_id = d.referencing_id "
    "where d.referenced_id = %(object_id)s order by kind, object_name"
)


def resolve_table(session: SqlServerSession, schema: str | None, table: str) -> dict[str, Any]:
    if schema is None and "." in table:
        schema, table = table.split(".", 1)
    schemas = schema_scope(session, schema)
    name = table.strip("[]")
    binds = Binds()
    in_schemas = binds.many(schemas)
    matches = session.records(RESOLVE_SQL.format(schemas=in_schemas, name=binds.add(name)), binds.values, max_rows=20)
    if not matches:
        raise DatabaseAccessError(
            f"Tabela ou view '{name}' não encontrada"
            + (f" no schema '{schema}'" if schema else " nos schemas liberados")
            + ". Use list_tables para ver os nomes exatos."
        )
    if len(matches) > 1:
        found = ", ".join(f"{row['schema_name']}.{row['object_name']}" for row in matches)
        raise DatabaseAccessError(f"'{name}' existe em mais de um schema ({found}). Informe o schema.")
    return matches[0]


def _trigger_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    shaped = []
    for row in rows:
        row["definition"] = _truncate(row.get("definition"))
        shaped.append(row)
    return shape_records(shaped)


def _optional_records(session: SqlServerSession, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Catalog detail that needs extra permissions: omitted rather than failing the whole call."""
    try:
        return shape_records(session.records(query, params))
    except DatabaseAccessError:
        return []


def describe_table(
    session: SqlServerSession, target: DatabaseTarget, table: str, schema: str | None = None
) -> dict[str, Any]:
    relation = resolve_table(session, schema, table)
    key = {"object_id": relation["object_id"]}
    kind = relation["kind"].strip()
    where_table = "{column} = %(object_id)s"
    described: dict[str, Any] = {
        "table": f"{relation['schema_name']}.{relation['object_name']}",
        "kind": _kind(kind),
        "description": (session.one(f"select {DESCRIPTION.format(object_id='%(object_id)s', minor_id=0)} as d", key) or {}).get("d"),
        "rows": (session.one(f"select {ROWS.format(object_id='%(object_id)s')} as n", key) or {}).get("n"),
        "columns": shape_records(session.records(COLUMNS_SQL, key)),
        "keys": shape_records(session.records(KEYS_SQL, key)),
        "check_constraints": shape_records(
            session.records(CHECKS_SQL.format(where=where_table.format(column=CHECK_TABLE)), key)
        ),
        "foreign_keys": shape_records(
            session.records(FOREIGN_KEYS_SQL.format(where=where_table.format(column="fk.parent_object_id"), limit=200), key)
        ),
        "referenced_by": shape_records(
            session.records(FOREIGN_KEYS_SQL.format(where=where_table.format(column="fk.referenced_object_id"), limit=200), key)
        ),
        "indexes": shape_records(session.records(INDEXES_SQL, key)),
        "triggers": _trigger_rows(
            session.records(TRIGGERS_SQL.format(where=where_table.format(column=TRIGGER_TABLE), limit=100), key)
        ),
        "row_level_security_policies": shape_records(
            session.records(POLICIES_SQL.format(where=where_table.format(column=POLICY_TABLE), limit=100), key)
        ),
        "used_by": _optional_records(session, USED_BY_SQL, key),
    }
    for row in described["keys"]:
        row["kind"] = "primary key" if (row.get("kind") or "").strip() == "PK" else "unique"
    if kind == "V":
        module = session.one(MODULE_SQL, key) or {}
        described["view_definition"] = _truncate(module.get("definition")) or "(definição cifrada: WITH ENCRYPTION)"
    return {name: value for name, value in described.items() if value not in (None, [], "")}


# endregion


# region Relationships
UNIQUE_KEYS_SQL = (
    "select object_schema_name(i.object_id) as schema_name, object_name(i.object_id) as table_name, "
    f"{INDEX_COLUMNS.format(table='i.object_id', index='i.index_id')} as columns "
    "from sys.indexes i where i.is_unique = 1 and i.has_filter = 0 "
    "and object_schema_name(i.object_id) in ({schemas})"
)
NULLABLE_SQL = (
    "select object_schema_name(c.object_id) as schema_name, object_name(c.object_id) as table_name, "
    "c.name as column_name from sys.columns c join sys.tables t on t.object_id = c.object_id "
    "where c.is_nullable = 1 and schema_name(t.schema_id) in ({schemas})"
)
RELATION_COLUMNS_SQL = (
    "select schema_name(t.schema_id) as schema_name, t.name as table_name, c.name as column_name, "
    "ty.name as data_type, c.max_length, "
    f"{DESCRIPTION.format(object_id='c.object_id', minor_id='c.column_id')} as description "
    "from sys.columns c join sys.tables t on t.object_id = c.object_id "
    "join sys.types ty on ty.user_type_id = c.user_type_id where schema_name(t.schema_id) in ({schemas})"
)
SINGLE_KEYS_SQL = (
    "select object_schema_name(i.object_id) as schema_name, object_name(i.object_id) as table_name, "
    "col_name(ic.object_id, ic.column_id) as column_name from sys.indexes i "
    "join sys.index_columns ic on ic.object_id = i.object_id and ic.index_id = i.index_id and ic.is_included_column = 0 "
    "where i.is_unique = 1 and i.has_filter = 0 and object_schema_name(i.object_id) in ({schemas}) "
    "and (select count(*) from sys.index_columns x where x.object_id = i.object_id and x.index_id = i.index_id "
    "and x.is_included_column = 0) = 1"
)
DECLARED_FK_COLUMNS_SQL = (
    "select object_schema_name(fc.parent_object_id) as schema_name, object_name(fc.parent_object_id) as table_name, "
    "col_name(fc.parent_object_id, fc.parent_column_id) as column_name from sys.foreign_key_columns fc "
    "where object_schema_name(fc.parent_object_id) in ({schemas})"
)
FK_ACTIONS = {"CASCADE": "cascade", "SET_NULL": "set null", "SET_DEFAULT": "set default", "NO_ACTION": "no action"}


def list_relationships(
    session: SqlServerSession, target: DatabaseTarget, schema: str | None = None
) -> dict[str, Any]:
    schemas = schema_scope(session, schema)
    binds = Binds()
    in_schemas = binds.many(schemas)
    rows = session.records(
        FOREIGN_KEYS_SQL.format(where=f"object_schema_name(fk.parent_object_id) in ({in_schemas})", limit=1001),
        binds.values,
        max_rows=1001,
    )
    unique = {
        (row["schema_name"], row["table_name"], _split(row["columns"]))
        for row in session.records(UNIQUE_KEYS_SQL.format(schemas=in_schemas), binds.values, max_rows=5000)
    }
    nullable = {
        (row["schema_name"], row["table_name"], row["column_name"])
        for row in session.records(NULLABLE_SQL.format(schemas=in_schemas), binds.values, max_rows=RELATIONSHIP_COLUMNS)
    }
    items = []
    for row in rows[:1000]:
        columns = _split(row["from_columns"])
        from_key = (row["from_schema"], row["from_table"])
        items.append(
            {
                "name": row["constraint_name"],
                "from_table": f"{from_key[0]}.{from_key[1]}",
                "from_columns": list(columns),
                "to_table": f"{row['to_schema']}.{row['to_table']}",
                "to_columns": list(_split(row["to_columns"])),
                "on_delete": FK_ACTIONS.get(row["on_delete"], row["on_delete"]),
                "enabled": not row["is_disabled"],
                "cardinality": "one-to-one" if (*from_key, columns) in unique else "many-to-one",
                "required": not any((*from_key, column) in nullable for column in columns),
            }
        )
    payload: dict[str, Any] = {"items": shape_records(items), "count": len(items)}
    if len(rows) > 1000:
        payload["truncated"] = True
    if not items:
        payload["hint"] = "Nenhuma chave estrangeira declarada; use suggest_relationships para vínculos inferidos."
    return payload


def suggest_table_relationships(
    session: SqlServerSession, target: DatabaseTarget, schema: str | None = None
) -> dict[str, Any]:
    schemas = schema_scope(session, schema)
    binds = Binds()
    in_schemas = binds.many(schemas)
    column_rows = session.records(
        RELATION_COLUMNS_SQL.format(schemas=in_schemas), binds.values, max_rows=RELATIONSHIP_COLUMNS
    )
    columns = {
        (row["schema_name"], row["table_name"], row["column_name"]): ColumnInfo(
            schema=row["schema_name"],
            table=row["table_name"],
            column=row["column_name"],
            data_type=row["data_type"],
            length=row["max_length"],
            text=row["description"],
        )
        for row in column_rows
    }
    keys = [
        columns[key]
        for row in session.records(SINGLE_KEYS_SQL.format(schemas=in_schemas), binds.values, max_rows=5000)
        if (key := (row["schema_name"], row["table_name"], row["column_name"])) in columns
    ]
    declared = {
        (row["schema_name"], row["table_name"], row["column_name"])
        for row in session.records(DECLARED_FK_COLUMNS_SQL.format(schemas=in_schemas), binds.values, max_rows=5000)
    }
    payload = suggest_relationships(list(columns.values()), keys, declared)
    if len(column_rows) >= RELATIONSHIP_COLUMNS:
        payload["note"] += " Schemas grandes: só parte das colunas foi analisada; filtre por schema."
    return payload


# endregion


# region Rules and definitions
DEFAULTS_SQL = (
    "select top ({limit}) object_schema_name(dc.parent_object_id) as schema_name, "
    "object_name(dc.parent_object_id) as table_name, col_name(dc.parent_object_id, dc.parent_column_id) as column_name, "
    "dc.definition from sys.default_constraints dc where {where} order by table_name, column_name"
)
FILTERED_UNIQUE_SQL = (
    "select top ({limit}) object_schema_name(i.object_id) as schema_name, object_name(i.object_id) as table_name, "
    f"i.name as index_name, i.filter_definition, {INDEX_COLUMNS.format(table='i.object_id', index='i.index_id')} as columns "
    "from sys.indexes i where i.is_unique = 1 and i.has_filter = 1 and {where} order by table_name, i.name"
)
MASKED_SQL = (
    "select top ({limit}) object_schema_name(mc.object_id) as schema_name, object_name(mc.object_id) as table_name, "
    "mc.name as column_name, mc.masking_function from sys.masked_columns mc where {where}"
)
SEQUENCES_SQL = (
    "select top ({limit}) schema_name(s.schema_id) as schema_name, s.name as sequence_name, "
    "cast(s.start_value as nvarchar(40)) as start_value, cast(s.increment as nvarchar(40)) as increment, "
    "cast(s.current_value as nvarchar(40)) as current_value from sys.sequences s "
    "where schema_name(s.schema_id) in ({schemas})"
)


RULE_SCOPE_COLUMNS = (
    ("checks", CHECK_TABLE),
    ("triggers", TRIGGER_TABLE),
    ("policies", POLICY_TABLE),
    ("defaults", "dc.parent_object_id"),
    ("indexes", "i.object_id"),
    ("masked", "mc.object_id"),
)


def list_rules(session: SqlServerSession, target: DatabaseTarget, table: str | None = None) -> dict[str, Any]:
    binds = Binds()
    schemas_in = ""
    if table:
        relation = resolve_table(session, None, table)
        object_id = binds.add(relation["object_id"])
        scope = {name: f"{column} = {object_id}" for name, column in RULE_SCOPE_COLUMNS}
    else:
        schemas_in = binds.many(schema_scope(session))
        scope = {name: f"object_schema_name({column}) in ({schemas_in})" for name, column in RULE_SCOPE_COLUMNS}
    params = binds.values
    limit = MAX_RULES_PER_KIND
    rules: dict[str, Any] = {
        "check_constraints": shape_records(session.records(CHECKS_SQL.format(where=scope["checks"]), params)),
        "unique_filtered_indexes": shape_records(
            session.records(FILTERED_UNIQUE_SQL.format(where=scope["indexes"], limit=limit), params)
        ),
        "triggers": _trigger_rows(session.records(TRIGGERS_SQL.format(where=scope["triggers"], limit=limit), params)),
        "row_level_security_policies": shape_records(
            session.records(POLICIES_SQL.format(where=scope["policies"], limit=limit), params)
        ),
        "defaults": shape_records(session.records(DEFAULTS_SQL.format(where=scope["defaults"], limit=limit), params)),
        "masked_columns": shape_records(session.records(MASKED_SQL.format(where=scope["masked"], limit=limit), params)),
    }
    if not table:
        rules["sequences"] = shape_records(
            session.records(SEQUENCES_SQL.format(schemas=schemas_in, limit=limit), params)
        )
    rules = {key: value for key, value in rules.items() if value}
    rules["note"] = (
        "No SQL Server muitas regras de negócio ficam em stored procedures e triggers. Use "
        "list_routines e get_definition para lê-las. Índices únicos filtrados também são regras "
        "(ex.: no máximo um registro ativo por cliente)."
    )
    return rules


DEFINITION_SQL = (
    "select s.name as schema_name, o.name as object_name, o.type as kind, m.definition "
    "from sys.objects o join sys.schemas s on s.schema_id = o.schema_id "
    "left join sys.sql_modules m on m.object_id = o.object_id "
    "where o.type in ('P', 'PC', 'FN', 'IF', 'TF', 'FS', 'FT', 'TR', 'V') and s.name in ({schemas}) "
    "and o.name = {name} order by s.name, o.type"
)


def get_definition(
    session: SqlServerSession, target: DatabaseTarget, name: str, schema: str | None = None
) -> dict[str, Any]:
    if schema is None and "." in name:
        schema, name = name.split(".", 1)
    schemas = schema_scope(session, schema)
    name = name.strip("[]")
    binds = Binds()
    query = DEFINITION_SQL.format(schemas=binds.many(schemas), name=binds.add(name))
    found = []
    for row in session.records(query, binds.values):
        kind = row["kind"].strip()
        item: dict[str, Any] = {
            "name": f"{row['schema_name']}.{row['object_name']}",
            "kind": _kind(kind),
            "definition": _truncate(row["definition"]),
        }
        if row["definition"] is None:
            item["note"] = (
                "Rotina CLR (código .NET fora do banco)." if kind in ("PC", "FS", "FT")
                else "Definição cifrada (WITH ENCRYPTION): o código não pode ser lido."
            )
        found.append(item)
    if not found:
        raise DatabaseAccessError(
            f"Nenhuma procedure, function, trigger ou view chamada '{name}'. "
            "Use list_routines ou list_tables para ver os nomes exatos."
        )
    return {"definitions": shape_records(found)}


# endregion


# region Data understanding
COLUMN_TEXTS_SQL = (
    f"select c.name as column_name, {DESCRIPTION.format(object_id='c.object_id', minor_id='c.column_id')} as description, "
    "case when mc.column_id is null then 0 else 1 end as masked "
    "from sys.columns c left join sys.masked_columns mc on mc.object_id = c.object_id and mc.column_id = c.column_id "
    "where c.object_id = %(object_id)s"
)
HISTOGRAM_SQL = (
    "select c.name as column_name, s.stats_id, sp.rows, sp.last_updated, h.step_number, "
    "cast(h.range_high_key as nvarchar(200)) as high_key, h.equal_rows, h.distinct_range_rows "
    "from sys.stats s "
    "join sys.stats_columns sc on sc.object_id = s.object_id and sc.stats_id = s.stats_id and sc.stats_column_id = 1 "
    "join sys.columns c on c.object_id = sc.object_id and c.column_id = sc.column_id "
    "cross apply sys.dm_db_stats_properties(s.object_id, s.stats_id) sp "
    "cross apply sys.dm_db_stats_histogram(s.object_id, s.stats_id) h "
    "where s.object_id = %(object_id)s order by c.column_id, s.stats_id, h.step_number"
)
SENSITIVE_TEXTS_SQL = (
    f"select c.name as column_name, {DESCRIPTION.format(object_id='c.object_id', minor_id='c.column_id')} as description, "
    "case when mc.column_id is null then 0 else 1 end as masked from sys.columns c "
    "join sys.objects o on o.object_id = c.object_id "
    "left join sys.masked_columns mc on mc.object_id = c.object_id and mc.column_id = c.column_id "
    "where o.type in ('U', 'V') and schema_name(o.schema_id) in ({schemas}) and c.name in ({names})"
)


def _described_as_sensitive(rows: list[dict[str, Any]]) -> frozenset[str]:
    return frozenset(
        row["column_name"] for row in rows if row.get("masked") or is_sensitive_column(row.get("description"))
    )


def _profile(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    chosen: dict[str, int] = {}
    for row in rows:
        column = row["column_name"]
        if chosen.setdefault(column, row["stats_id"]) != row["stats_id"]:
            continue
        profile = profiles.setdefault(
            column,
            {"rows": row["rows"], "nulls": 0, "distinct": 0, "min": None, "max": None, "updated": row["last_updated"]},
        )
        if row["high_key"] is None:
            profile["nulls"] = row["equal_rows"] or 0
            continue
        profile["distinct"] += 1 + int(row["distinct_range_rows"] or 0)
        profile["min"] = profile["min"] if profile["min"] is not None else row["high_key"]
        profile["max"] = row["high_key"]
    return profiles


def column_profile(
    session: SqlServerSession, target: DatabaseTarget, table: str, schema: str | None = None
) -> dict[str, Any]:
    relation = resolve_table(session, schema, table)
    key = {"object_id": relation["object_id"]}
    sensitive = _described_as_sensitive(session.records(COLUMN_TEXTS_SQL, key))
    profiles = _profile(session.records(HISTOGRAM_SQL, key, max_rows=20_000))
    columns = []
    for column, profile in profiles.items():
        hidden = column in sensitive or is_sensitive_column(column)
        rows = profile["rows"] or 0
        columns.append(
            {
                "column": column,
                "distinct": profile["distinct"],
                "null_fraction": round(profile["nulls"] / rows, 4) if rows else None,
                "approx_min": "[mascarado]" if hidden else profile["min"],
                "approx_max": "[mascarado]" if hidden else profile["max"],
                "statistics_updated": profile["updated"],
            }
        )
    payload: dict[str, Any] = {
        "table": f"{relation['schema_name']}.{relation['object_name']}",
        "columns": shape_records(columns),
    }
    if not columns:
        payload["note"] = (
            "Sem estatísticas de coluna para esta tabela (o SQL Server as cria conforme as consultas "
            "usam as colunas). Use sample_rows ou uma consulta agregada."
        )
    return payload


def _quoted(schema: str, name: str) -> str:
    return ".".join("[" + part.replace("]", "]]") + "]" for part in (schema, name))


def sample_rows(
    session: SqlServerSession,
    target: DatabaseTarget,
    table: str,
    schema: str | None = None,
    limit: int = DEFAULT_SAMPLE_ROWS,
) -> dict[str, Any]:
    relation = resolve_table(session, schema, table)
    key = {"object_id": relation["object_id"]}
    bounded = max(1, min(int(limit), MAX_SAMPLE_ROWS))
    sensitive = _described_as_sensitive(session.records(COLUMN_TEXTS_SQL, key))
    result = session.fetch(
        f"select top ({bounded}) * from {_quoted(relation['schema_name'], relation['object_name'])}",
        max_rows=bounded,
    )
    result.truncated = False
    return {"table": f"{relation['schema_name']}.{relation['object_name']}", **shape_rows(result, sensitive)}


# endregion


# region Engine
KNOWN_SCHEMAS_SQL = "select name from sys.schemas where name in ({names})"
OTHER_SOURCES_SQL = (
    "select name from sys.databases where name in ({names}) and name <> db_name() "
    "union select name from sys.servers where is_linked = 1 and name in ({names})"
)


class SqlServerEngine(Engine):
    name = "sqlserver"
    dialect = DIALECT

    def open_session(self, target: DatabaseTarget, check_posture: bool = True):
        return readonly_session(target, check_posture=check_posture)

    def check_connection(self, target: DatabaseTarget) -> dict[str, Any]:
        try:
            with readonly_session(target, check_posture=False) as session:
                posture = read_posture(session, target.schemas)
                schemas = [row["schema_name"] for row in session.records(VISIBLE_SCHEMAS_SQL)]
        except DatabaseAccessError as exc:
            return {"ok": False, "error": str(exc)}
        warnings = posture.warnings()
        visible = {name.lower() for name in schemas}
        missing = [name for name in target.schemas if name.lower() not in visible]
        if missing:
            warnings.append(f"Schemas liberados sem tabelas visíveis para o usuário: {', '.join(missing)}.")
        refusal = posture.refusal(target.alias)
        return {
            "ok": refusal is None,
            "server_version": posture.server_version,
            "role": posture.role,
            "read_only_role": not (posture.can_write or posture.refused),
            "schemas": schemas,
            "warnings": warnings,
            "error": refusal,
        }

    def run_query(self, session: SqlServerSession, query: str, max_rows: int) -> QueryResult:
        qualifiers = sorted(schema_qualifiers(query, DIALECT))[:200]
        if qualifiers:
            binds = Binds()
            names = binds.many(qualifiers)
            outside = session.records(OTHER_SOURCES_SQL.format(names=names), binds.values)
            if outside:
                raise DatabaseAccessError(
                    f"A consulta cita '{outside[0]['name']}', que é outro banco ou um linked server. "
                    "Consulte só o banco cadastrado."
                )
        return session.fetch(query, max_rows=max_rows)

    def known_schemas(self, session: SqlServerSession, candidates: set[str]) -> set[str]:
        names = sorted(candidates)[:200]
        if not names:
            return set()
        binds = Binds()
        rows = session.records(KNOWN_SCHEMAS_SQL.format(names=binds.many(names)), binds.values)
        found = {row["name"].lower() for row in rows}
        return {name for name in candidates if name.lower() in found}

    def sensitive_columns(
        self, session: SqlServerSession, target: DatabaseTarget, columns: list[str]
    ) -> frozenset[str]:
        names = sorted(set(columns))[:200]
        if not names:
            return frozenset()
        try:
            schemas = schema_scope(session)
        except DatabaseAccessError:
            return frozenset()
        binds = Binds()
        query = SENSITIVE_TEXTS_SQL.format(schemas=binds.many(schemas), names=binds.many(names))
        sensitive = {name.lower() for name in _described_as_sensitive(session.records(query, binds.values, max_rows=5000))}
        return frozenset(column for column in columns if column.lower() in sensitive)

    def catalog(self) -> dict[str, CatalogOperation]:
        return {
            "database_overview": database_overview,
            "list_tables": list_tables,
            "list_routines": list_routines,
            "describe_table": describe_table,
            "list_relationships": list_relationships,
            "suggest_relationships": suggest_table_relationships,
            "list_rules": list_rules,
            "get_definition": get_definition,
            "column_profile": column_profile,
            "sample_rows": sample_rows,
        }


ENGINE = SqlServerEngine()


# endregion

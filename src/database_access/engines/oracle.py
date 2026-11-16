"""Oracle engine over python-oracledb (thin mode): read-only transactions, posture and ``ALL_*`` catalog.

A read-only transaction does not stop functions declared with ``PRAGMA AUTONOMOUS_TRANSACTION``
from writing, so a user that can run any of them is refused.
"""
import ssl
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

ORACLE_PORT = 1521
CALL_TIMEOUT_MS = 15_000
CONNECT_TIMEOUT_SECONDS = 10
CATALOG_ROWS = 1000
RELATIONSHIP_COLUMNS = 10_000
MAX_LISTED_OBJECTS = 500
MAX_RULES_PER_KIND = 300
MAX_SOURCE_CHARS = 12_000
MAX_SOURCE_LINES = 4000
LARGEST_TABLES = 15
MAX_SCOPE_OWNERS = 200

REFUSED_ROLES = frozenset(
    {
        "DBA",
        "IMP_FULL_DATABASE",
        "EXP_FULL_DATABASE",
        "DATAPUMP_IMP_FULL_DATABASE",
        "DATAPUMP_EXP_FULL_DATABASE",
        "SYSDBA",
        "SYSOPER",
    }
)
TOLERATED_ANY_PRIVILEGES = frozenset({"SELECT ANY TABLE", "READ ANY TABLE"})
REFUSED_PRIVILEGES = frozenset(
    {"ALTER SYSTEM", "ALTER DATABASE", "BECOME USER", "EXEMPT ACCESS POLICY", "EXEMPT REDACTION POLICY"}
)

PACKAGE_REFUSAL = (
    "Pacotes e tipos de sistema ({match}) não são permitidos em consultas de leitura: "
    "eles acessam rede, arquivos ou executam SQL dinâmico."
)
DIALECT = Dialect(
    name="oracle",
    blocked_functions=("XMLQUERY", "XMLTABLE", "XMLEXISTS"),
    system_schemas=("SYS",),
    blocked_relations=(
        "USER$",
        "LINK$",
        "ENC$",
        "DEFAULT_PWD$",
        "USER_HISTORY$",
        "SCHEDULER$_CREDENTIAL",
        "CDB_USERS",
        "DBA_USERS",
        "DBA_USERS_WITH_DEFPWD",
    ),
    query_keywords=("select", "with"),
    q_quotes=True,
    folds_to_upper=True,
    row_limit="FETCH FIRST {rows} ROWS ONLY",
    subquery_alias="estack_q",
    refused_patterns=(
        (
            r'(?<![\w$])"?((?:DBMS_|UTL_|OWA_)[\w$]*|HTP|HTF|HTTPURITYPE|DBURITYPE|XDBURITYPE|URIFACTORY)"?(?![\w$])',
            PACKAGE_REFUSAL,
        ),
        ("@", "Database links (tabela@outro_banco) não são permitidos: a consulta fica neste banco."),
        (
            r"(?<![\w$])WITH\s+(?:FUNCTION|PROCEDURE)(?![\w$])",
            "PL/SQL dentro da consulta (WITH FUNCTION/PROCEDURE) não é permitido.",
        ),
        (r"(?<![\w$])FOR\s+UPDATE(?![\w$])", "SELECT ... FOR UPDATE trava linhas e não é permitido."),
        (r'\.\s*"?NEXTVAL"?(?![\w$])', "Ler NEXTVAL avança a sequence e não é permitido; use CURRVAL ou o catálogo."),
    ),
)

MVIEW = "MATERIALIZED VIEW"
OBJECT_KINDS = {"TABLE": "table", "VIEW": "view", MVIEW: "materialized view"}
TLS_MODES = ("require", "verify-ca", "verify-full")
CONSTRAINT_KINDS = {"P": "primary key", "U": "unique", "C": "check", "R": "foreign key"}

QUERY_ERRORS = {
    "DPY-4024": (
        f"A consulta passou do limite de {CALL_TIMEOUT_MS // 1000}s e foi cancelada. "
        "Use filtros ou agregações mais estreitas."
    ),
    "ORA-01456": "A transação é somente leitura: o banco recusou a escrita.",
    "ORA-00918": (
        "A consulta devolve colunas com o mesmo nome. Dê um alias diferente a cada uma "
        "(ex.: a.id as pedido_id, b.id as cliente_id)."
    ),
}
PRIVILEGE_ERRORS = ("ORA-00942", "ORA-01031", "ORA-41900")
CONNECTION_FAILURES = {
    "ORA-01017": "usuário ou senha recusados",
    "ORA-28000": "a conta está bloqueada no Oracle",
    "ORA-28001": "a senha expirou; troque-a no Oracle",
    "ORA-12514": "service name desconhecido pelo listener; confira o campo Banco (service name)",
    "ORA-12541": "nenhum listener nessa porta; confira host e porta",
    "DPY-6005": (
        "servidor inacessível ou conexão recusada. Confira host, porta e firewall; se o servidor "
        "exige Native Network Encryption, habilite TLS (TCPS) no Oracle"
    ),
}


def _load_driver():
    import oracledb

    oracledb.defaults.fetch_lobs = False
    return oracledb


# region Errors
def _error_code(exc: Exception) -> tuple[str | None, str]:
    detail = exc.args[0] if exc.args else None
    code = getattr(detail, "full_code", None)
    message = getattr(detail, "message", None) or str(exc)
    return code, message.splitlines()[0] if message else ""


def describe_query_error(exc: Exception) -> str:
    code, message = _error_code(exc)
    if code in QUERY_ERRORS:
        return QUERY_ERRORS[code]
    if code in PRIVILEGE_ERRORS:
        return f"O objeto não existe ou o usuário não tem permissão para lê-lo ({message})."
    return f"Erro do Oracle ({code or 'desconhecido'}): {message}"[:600]


def describe_connection_error(exc: Exception, alias: str) -> str:
    code, _ = _error_code(exc)
    reason = CONNECTION_FAILURES.get(code or "", "não foi possível abrir a sessão")
    return f"Não foi possível conectar ao banco '{alias}': {reason}."


# endregion


# region Session
class Binds:
    """Named bind variables for one statement."""

    def __init__(self):
        self.values: dict[str, Any] = {}

    def add(self, value: Any) -> str:
        name = f"b{len(self.values)}"
        self.values[name] = value
        return f":{name}"

    def many(self, values: list[Any]) -> str:
        return ", ".join(self.add(value) for value in values)


class OracleSession:
    """One connection; every statement inside its own read-only transaction."""

    def __init__(self, target: DatabaseTarget, connection: Any, driver: Any):
        self.target = target
        self._connection = connection
        self._driver = driver
        self._scope: list[str] | None = None

    @property
    def server_version(self) -> str:
        return str(self._connection.version)

    def fetch(self, query: str, params: Any = None, max_rows: int = MAX_QUERY_ROWS) -> QueryResult:
        try:
            with self._connection.cursor() as cursor:
                cursor.execute("set transaction read only")
                cursor.execute(query, params or {})
                if cursor.description is None:
                    return QueryResult(columns=[], rows=[])
                columns = [column[0] for column in cursor.description]
                rows = cursor.fetchmany(max_rows + 1)
        except self._driver.Error as exc:
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
        except self._driver.Error:
            logger.warning("Rollback failed for Oracle alias '%s'", self.target.alias)

    def records(self, query: str, params: Any = None, max_rows: int = CATALOG_ROWS) -> list[dict[str, Any]]:
        return self.fetch(query, params, max_rows).records()

    def one(self, query: str, params: Any = None) -> dict[str, Any] | None:
        found = self.records(query, params, max_rows=1)
        return found[0] if found else None

    def close(self) -> None:
        try:
            self._connection.close()
        except self._driver.Error:
            logger.debug("Closing Oracle connection for alias '%s' failed", self.target.alias)


def _connect_params(target: DatabaseTarget) -> dict[str, Any]:
    """Connection arguments; every TLS mode verifies the certificate and the server name."""
    params: dict[str, Any] = {
        "user": target.username,
        "password": target.password,
        "host": target.host,
        "port": target.port or ORACLE_PORT,
        "service_name": target.database,
        "tcp_connect_timeout": CONNECT_TIMEOUT_SECONDS,
    }
    if public_network_only(target):
        check_ssl_mode(target)
        resolve_allowed_addresses(target)
    if target.ssl_mode in TLS_MODES:
        params["protocol"] = "tcps"
        params["ssl_context"] = ssl.create_default_context(cafile=target.option("ca_file"))
        params["ssl_server_dn_match"] = True
    return params


def open_connection(target: DatabaseTarget):
    driver = _load_driver()
    try:
        connection = driver.connect(**_connect_params(target))
    except driver.Error as exc:
        logger.warning(
            "Oracle connection failed for alias '%s' (%s:%s/%s): %s",
            target.alias,
            target.host,
            target.port,
            target.database,
            _error_code(exc)[0] or type(exc).__name__,
        )
        raise DatabaseAccessError(describe_connection_error(exc, target.alias)) from exc
    connection.call_timeout = CALL_TIMEOUT_MS
    if len(target.schemas) == 1:
        owner = target.schemas[0].upper().replace('"', '""')
        try:
            connection.cursor().execute(f'alter session set current_schema = "{owner}"')
        except driver.Error:
            logger.info("Owner '%s' not found for alias '%s'", owner, target.alias)
    return OracleSession(target, connection, driver)


# endregion


# region Posture
@dataclass(frozen=True)
class Posture:
    server_version: str
    role: str
    roles: frozenset[str]
    privileges: frozenset[str]
    owns_tables: bool
    write_grants: bool
    autonomous_routines: tuple[str, ...] = ()
    readable_outside_scope: tuple[str, ...] = ()

    @property
    def reads_any_table(self) -> bool:
        return bool(self.privileges & TOLERATED_ANY_PRIVILEGES)

    @property
    def refused(self) -> list[str]:
        powerful = {
            privilege
            for privilege in self.privileges
            if (" ANY " in f" {privilege} " and privilege not in TOLERATED_ANY_PRIVILEGES)
            or privilege in REFUSED_PRIVILEGES
        }
        return sorted(self.roles & REFUSED_ROLES) + sorted(powerful)

    def warnings(self) -> list[str]:
        notes = []
        if self.owns_tables or self.write_grants:
            notes.append(
                "O usuário é dono de tabelas ou tem permissão de escrita em alguma. As travas "
                "impedem escritas, mas o recomendado é um usuário só com SELECT."
            )
        if self.reads_any_table:
            notes.append(
                "O usuário tem SELECT ANY TABLE: lê qualquer tabela do banco. Prefira GRANT SELECT "
                "nas tabelas dos owners liberados."
            )
        return notes

    def refusal(self, alias: str) -> str | None:
        if self.refused:
            extra = len(self.refused) - 4
            listed = ", ".join(self.refused[:4]) + (f" e mais {extra}" if extra > 0 else "")
            return (
                f"O usuário '{self.role}' do banco '{alias}' tem {listed}. "
                "Por segurança o acesso foi recusado: use um usuário só com CREATE SESSION e "
                "SELECT nas tabelas liberadas."
            )
        if self.autonomous_routines:
            return (
                f"O usuário '{self.role}' pode executar rotinas com PRAGMA AUTONOMOUS_TRANSACTION "
                f"({', '.join(self.autonomous_routines[:5])}), que gravam mesmo numa transação "
                "somente leitura. Revogue o EXECUTE dessas rotinas do usuário de leitura."
            )
        if self.readable_outside_scope:
            return (
                f"O usuário '{self.role}' consegue ler tabelas de owners fora dos liberados "
                f"({', '.join(self.readable_outside_scope[:5])}). Restrinja os GRANTs aos owners "
                "liberados (sinônimos públicos dariam acesso a eles)."
            )
        return None


ROLES_SQL = "select role from session_roles"
PRIVILEGES_SQL = "select privilege from session_privs"
OWNS_TABLES_SQL = "select count(*) as n from user_tables"
WRITE_GRANTS_SQL = (
    "select count(*) as n from user_tab_privs where grantee = user "
    "and privilege in ('INSERT', 'UPDATE', 'DELETE', 'ALTER', 'INDEX', 'REFERENCES')"
)
USER_OWNERS = "select username from all_users where oracle_maintained = 'N'"
AUTONOMOUS_SQL = (
    "select distinct s.owner || '.' || s.name as routine from all_source s "
    f"where s.owner in ({USER_OWNERS}) "
    "and s.type in ('PROCEDURE', 'FUNCTION', 'PACKAGE BODY', 'TYPE BODY') "
    "and upper(s.text) like '%AUTONOMOUS\\_TRANSACTION%' escape '\\' "
    "and (s.owner = user or exists (select 1 from all_tab_privs p where p.table_schema = s.owner "
    "and p.table_name = s.name and p.privilege = 'EXECUTE' "
    "and (p.grantee in (user, 'PUBLIC') or p.grantee in (select role from session_roles)))) "
    "fetch first 20 rows only"
)
OUTSIDE_SCOPE_SQL = (
    "select distinct owner from all_objects "
    f"where object_type in ('TABLE', 'VIEW', 'MATERIALIZED VIEW') and owner in ({USER_OWNERS}) "
    "and owner not in ({owners}) fetch first 20 rows only"
)


def read_posture(session: OracleSession, schemas: tuple[str, ...] = ()) -> Posture:
    identity = session.one("select user as role from dual") or {}
    outside: tuple[str, ...] = ()
    if schemas:
        binds = Binds()
        owners = binds.many([name.upper() for name in schemas])
        outside = tuple(
            row["OWNER"]
            for row in session.records(OUTSIDE_SCOPE_SQL.format(owners=owners), binds.values)
            if row["OWNER"] != identity.get("ROLE")
        )
    return Posture(
        server_version=session.server_version,
        role=identity.get("ROLE") or "",
        roles=frozenset(row["ROLE"] for row in session.records(ROLES_SQL)),
        privileges=frozenset(row["PRIVILEGE"] for row in session.records(PRIVILEGES_SQL)),
        owns_tables=bool((session.one(OWNS_TABLES_SQL) or {}).get("N")),
        write_grants=bool((session.one(WRITE_GRANTS_SQL) or {}).get("N")),
        autonomous_routines=tuple(row["ROUTINE"] for row in session.records(AUTONOMOUS_SQL)),
        readable_outside_scope=outside,
    )


def enforce_posture(target: DatabaseTarget, session: OracleSession) -> Posture:
    posture = _posture_cache.get(target.cache_key)
    if posture is None:
        posture = read_posture(session, target.schemas)
        _posture_cache.put(target.cache_key, posture)
    refusal = posture.refusal(target.alias)
    if refusal:
        raise DatabaseAccessError(refusal)
    return posture


@contextmanager
def readonly_session(target: DatabaseTarget, check_posture: bool = True) -> Iterator[OracleSession]:
    session = open_connection(target)
    try:
        if check_posture:
            enforce_posture(target, session)
        yield session
    finally:
        session.close()


# endregion


# region Scope
VISIBLE_OWNERS_SQL = (
    f"select u.username from all_users u where u.username in ({USER_OWNERS}) "
    "and exists (select 1 from all_objects o where o.owner = u.username "
    "and o.object_type in ('TABLE', 'VIEW', 'MATERIALIZED VIEW')) "
    f"order by u.username fetch first {MAX_SCOPE_OWNERS} rows only"
)
NO_OWNERS = (
    "O usuário do banco '{alias}' não enxerga tabelas de nenhum owner. Dê GRANT SELECT nas "
    "tabelas e libere os owners (schemas) no cadastro."
)


def owner_scope(session: OracleSession, schema: str | None = None) -> list[str]:
    """Owners an operation may read: the allowlist, else every non-Oracle owner the user sees."""
    if session._scope is None:
        configured = [name.upper() for name in session.target.schemas]
        session._scope = configured or [row["USERNAME"] for row in session.records(VISIBLE_OWNERS_SQL)]
    if not session._scope:
        raise DatabaseAccessError(NO_OWNERS.format(alias=session.target.alias))
    if schema is None:
        return session._scope
    requested = schema.strip('"') if schema.startswith('"') else schema.upper()
    if requested not in session._scope:
        raise DatabaseAccessError(
            f"O owner '{requested}' está fora dos liberados para este banco ({', '.join(session._scope)})."
        )
    return [requested]


def _like(name_filter: str | None) -> str | None:
    if not name_filter:
        return None
    escaped = name_filter.upper().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _cap(rows: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    payload: dict[str, Any] = {"items": shape_records(rows[:limit]), "count": min(len(rows), limit)}
    if len(rows) > limit:
        payload["truncated"] = True
        payload["note"] = "Lista cortada. Filtre por owner ou por parte do nome."
    return payload


def _truncate(text: str | None, limit: int = MAX_SOURCE_CHARS) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return f"{text[:limit]}\n-- … (+{len(text) - limit} caracteres omitidos)"


def _lower_keys(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key.lower(): value for key, value in row.items()} for row in rows]


def _split(columns: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in (columns or "").split(",") if part.strip())


# endregion


# region Overview and listings
OBJECT_COUNTS_SQL = (
    "select owner, object_type, count(*) as n from all_objects where owner in ({owners}) "
    "and object_type in ('TABLE', 'VIEW', 'MATERIALIZED VIEW', 'PACKAGE', 'PROCEDURE', 'FUNCTION', "
    "'TRIGGER', 'SEQUENCE', 'TYPE') and generated = 'N' and object_name not like 'BIN$%' "
    "group by owner, object_type"
)
LARGEST_TABLES_SQL = (
    "select owner, table_name, num_rows, blocks, last_analyzed from all_tables "
    "where owner in ({owners}) and num_rows is not null "
    "order by num_rows desc fetch first {limit} rows only"
)


def database_overview(session: OracleSession, target: DatabaseTarget) -> dict[str, Any]:
    owners = owner_scope(session)
    binds = Binds()
    in_owners = binds.many(owners)
    counts: dict[str, dict[str, int]] = {name: {} for name in owners}
    for row in session.records(OBJECT_COUNTS_SQL.format(owners=in_owners), binds.values):
        counts.setdefault(row["OWNER"], {})[row["OBJECT_TYPE"].lower()] = row["N"]
    largest = session.records(
        LARGEST_TABLES_SQL.format(owners=in_owners, limit=LARGEST_TABLES), binds.values
    )
    identity = session.one("select user as role from dual") or {}
    return {
        "server_version": session.server_version,
        "connected_as": identity.get("ROLE"),
        "owners": [{"owner": name, "objects": counts.get(name, {})} for name in owners],
        "largest_tables": shape_records(_lower_keys(largest)),
        "note": (
            "No Oracle os owners (usuários donos dos objetos) são os schemas. Regras de negócio "
            "costumam estar em packages PL/SQL: veja list_routines e get_definition."
        ),
    }


LIST_TABLES_SQL = (
    "select o.owner, o.object_name as table_name, o.object_type, c.comments, t.num_rows "
    "from all_objects o "
    "left join all_tab_comments c on c.owner = o.owner and c.table_name = o.object_name "
    "left join all_tables t on t.owner = o.owner and t.table_name = o.object_name "
    "where o.owner in ({owners}) and o.object_type in ('TABLE', 'VIEW', 'MATERIALIZED VIEW') "
    "and o.generated = 'N' and o.object_name not like 'BIN$%' "
    "and not (o.object_type = 'TABLE' and exists (select 1 from all_mviews m "
    "where m.owner = o.owner and m.mview_name = o.object_name)) {name_clause} "
    "order by o.owner, o.object_name fetch first {limit} rows only"
)


def list_tables(
    session: OracleSession, target: DatabaseTarget, schema: str | None = None, name_filter: str | None = None
) -> dict[str, Any]:
    owners = owner_scope(session, schema)
    binds = Binds()
    in_owners = binds.many(owners)
    pattern = _like(name_filter)
    name_clause = ""
    if pattern:
        bound = binds.add(pattern)
        name_clause = f"and (o.object_name like {bound} escape '\\' or upper(c.comments) like {bound} escape '\\')"
    rows = session.records(
        LIST_TABLES_SQL.format(owners=in_owners, name_clause=name_clause, limit=MAX_LISTED_OBJECTS + 1),
        binds.values,
        max_rows=MAX_LISTED_OBJECTS + 1,
    )
    for row in rows:
        row["OBJECT_TYPE"] = OBJECT_KINDS.get(row["OBJECT_TYPE"], row["OBJECT_TYPE"])
    return _cap(_lower_keys(rows), MAX_LISTED_OBJECTS)


LIST_ROUTINES_SQL = (
    "select o.owner, o.object_name, o.object_type, o.status, o.last_ddl_time, "
    "(select listagg(p.procedure_name, ', ' on overflow truncate) within group (order by p.subprogram_id) "
    "from all_procedures p where p.owner = o.owner and p.object_name = o.object_name "
    "and p.procedure_name is not null) as subprograms "
    "from all_objects o where o.owner in ({owners}) "
    "and o.object_type in ('PACKAGE', 'PROCEDURE', 'FUNCTION', 'TYPE') {name_clause} "
    "order by o.owner, o.object_name fetch first {limit} rows only"
)


def list_routines(
    session: OracleSession, target: DatabaseTarget, schema: str | None = None, name_filter: str | None = None
) -> dict[str, Any]:
    owners = owner_scope(session, schema)
    binds = Binds()
    in_owners = binds.many(owners)
    pattern = _like(name_filter)
    name_clause = f"and o.object_name like {binds.add(pattern)} escape '\\'" if pattern else ""
    rows = session.records(
        LIST_ROUTINES_SQL.format(owners=in_owners, name_clause=name_clause, limit=MAX_LISTED_OBJECTS + 1),
        binds.values,
        max_rows=MAX_LISTED_OBJECTS + 1,
    )
    for row in rows:
        row["OBJECT_TYPE"] = row["OBJECT_TYPE"].lower()
    return _cap(_lower_keys(rows), MAX_LISTED_OBJECTS)


# endregion


# region Describe
RESOLVE_SQL = (
    "select o.owner, o.object_name, o.object_type from all_objects o "
    "where o.owner in ({owners}) and o.object_name = {name} "
    "and o.object_type in ('TABLE', 'VIEW', 'MATERIALIZED VIEW') order by o.owner, o.object_type"
)
TABLE_INFO_SQL = (
    "select c.comments, t.num_rows, t.last_analyzed, t.partitioned, t.temporary, t.iot_type "
    "from all_tab_comments c left join all_tables t on t.owner = c.owner and t.table_name = c.table_name "
    "where c.owner = :owner and c.table_name = :name"
)
COLUMNS_SQL = (
    "select c.column_name, c.data_type, c.data_length, c.data_precision, c.data_scale, c.nullable, "
    "c.data_default, c.identity_column, c.virtual_column, m.comments "
    "from all_tab_cols c left join all_col_comments m "
    "on m.owner = c.owner and m.table_name = c.table_name and m.column_name = c.column_name "
    "where c.owner = :owner and c.table_name = :name and c.hidden_column = 'NO' order by c.column_id"
)
CONS_COLUMNS = (
    "(select listagg(x.column_name, ', ' on overflow truncate) within group (order by x.position) "
    "from all_cons_columns x where x.owner = {alias}.owner and x.constraint_name = {alias}.constraint_name)"
)
NOT_A_NOT_NULL = (
    "not (c.constraint_type = 'C' and c.search_condition_vc like '\"%\" IS NOT NULL')"
)
CONSTRAINTS_SQL = (
    "select c.constraint_name, c.constraint_type, c.status, c.search_condition_vc as check_clause, "
    f"{CONS_COLUMNS.format(alias='c')} as columns "
    "from all_constraints c where c.owner = :owner and c.table_name = :name "
    f"and c.constraint_type in ('P', 'U', 'C') and {NOT_A_NOT_NULL} "
    "order by c.constraint_type, c.constraint_name"
)
FOREIGN_KEYS_SQL = (
    "select c.constraint_name, c.owner as from_owner, c.table_name as from_table, "
    "r.owner as to_owner, r.table_name as to_table, c.delete_rule, c.status, "
    f"{CONS_COLUMNS.format(alias='c')} as from_columns, {CONS_COLUMNS.format(alias='r')} as to_columns "
    "from all_constraints c join all_constraints r "
    "on r.owner = c.r_owner and r.constraint_name = c.r_constraint_name "
    "where c.constraint_type = 'R' and {where} "
    "order by c.table_name, c.constraint_name fetch first {limit} rows only"
)
INDEXES_SQL = (
    "select i.index_name, i.index_type, i.uniqueness, i.status, "
    "(select listagg(x.column_name || case when x.descend = 'DESC' then ' DESC' end, ', ' on overflow truncate) "
    "within group (order by x.column_position) from all_ind_columns x "
    "where x.index_owner = i.owner and x.index_name = i.index_name) as columns "
    "from all_indexes i where i.table_owner = :owner and i.table_name = :name "
    "and i.index_type <> 'LOB' order by i.index_name"
)
TRIGGERS_SQL = (
    "select owner, trigger_name, table_name, trigger_type, triggering_event, status, "
    "when_clause, trigger_body from all_triggers where {where} "
    "order by table_name, trigger_name fetch first {limit} rows only"
)
POLICIES_SQL = (
    "select object_owner, object_name, policy_name, pf_owner, package, function, "
    "sel, ins, upd, del, enable from all_policies where {where} "
    "order by object_name, policy_name fetch first {limit} rows only"
)
VIEW_TEXT_SQL = "select text_vc from all_views where owner = :owner and view_name = :name"
MVIEW_TEXT_SQL = (
    "select query, refresh_mode, refresh_method, last_refresh_date from all_mviews "
    "where owner = :owner and mview_name = :name"
)
DEPENDENT_VIEWS_SQL = (
    "select owner, name, type from all_dependencies where referenced_owner = :owner "
    "and referenced_name = :name and type in ('VIEW', 'MATERIALIZED VIEW', 'PACKAGE BODY', 'PROCEDURE', "
    "'FUNCTION', 'TRIGGER') order by type, owner, name fetch first 100 rows only"
)


def resolve_table(session: OracleSession, schema: str | None, table: str) -> dict[str, Any]:
    if schema is None and "." in table:
        schema, table = table.split(".", 1)
    owners = owner_scope(session, schema)
    name = table.strip('"') if table.startswith('"') else table.upper()
    binds = Binds()
    in_owners = binds.many(owners)
    matches = session.records(
        RESOLVE_SQL.format(owners=in_owners, name=binds.add(name)), binds.values, max_rows=20
    )
    if not matches:
        raise DatabaseAccessError(
            f"Tabela ou view '{name}' não encontrada"
            + (f" no owner '{schema.upper()}'" if schema else " nos owners liberados")
            + ". Use list_tables para ver os nomes exatos."
        )
    distinct = {(row["OWNER"], row["OBJECT_NAME"]) for row in matches}
    if len(distinct) > 1:
        found = ", ".join(f"{owner}.{name}" for owner, name in sorted(distinct))
        raise DatabaseAccessError(f"'{name}' existe em mais de um owner ({found}). Informe o owner.")
    kinds = {row["OBJECT_TYPE"] for row in matches}
    kind = MVIEW if MVIEW in kinds else matches[0]["OBJECT_TYPE"]
    return {"OWNER": matches[0]["OWNER"], "NAME": matches[0]["OBJECT_NAME"], "KIND": kind}


def _trigger_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    shaped = []
    for row in _lower_keys(rows):
        row["trigger_body"] = _truncate(row.get("trigger_body"))
        shaped.append(row)
    return shape_records(shaped)


def _records(session: OracleSession, query: str, key: dict[str, str]) -> list[dict[str, Any]]:
    return shape_records(_lower_keys(session.records(query, key)))


def describe_table(
    session: OracleSession, target: DatabaseTarget, table: str, schema: str | None = None
) -> dict[str, Any]:
    relation = resolve_table(session, schema, table)
    key = {"owner": relation["OWNER"], "name": relation["NAME"]}
    info = session.one(TABLE_INFO_SQL, key) or {}
    kind = relation["KIND"]
    described: dict[str, Any] = {
        "table": f"{key['owner']}.{key['name']}",
        "kind": OBJECT_KINDS.get(kind, kind.lower()),
        "comment": info.get("COMMENTS"),
        "rows": info.get("NUM_ROWS"),
        "statistics_updated": info.get("LAST_ANALYZED"),
        "partitioned": info.get("PARTITIONED") == "YES" or None,
        "temporary": info.get("TEMPORARY") == "Y" or None,
        "columns": _records(session, COLUMNS_SQL, key),
        "constraints": _records(session, CONSTRAINTS_SQL, key),
        "foreign_keys": _records(
            session, FOREIGN_KEYS_SQL.format(where="c.owner = :owner and c.table_name = :name", limit=200), key
        ),
        "referenced_by": _records(
            session, FOREIGN_KEYS_SQL.format(where="r.owner = :owner and r.table_name = :name", limit=200), key
        ),
        "indexes": _records(session, INDEXES_SQL, key),
        "triggers": _trigger_rows(
            session.records(TRIGGERS_SQL.format(where="table_owner = :owner and table_name = :name", limit=100), key)
        ),
        "row_level_security_policies": _records(
            session, POLICIES_SQL.format(where="object_owner = :owner and object_name = :name", limit=100), key
        ),
        "used_by": _records(session, DEPENDENT_VIEWS_SQL, key),
    }
    for column in described["columns"]:
        column["data_default"] = (column.get("data_default") or "").strip() or None
    if kind == "VIEW":
        view = session.one(VIEW_TEXT_SQL, key) or {}
        described["view_definition"] = _truncate(view.get("TEXT_VC"))
    if kind == MVIEW:
        mview = session.one(MVIEW_TEXT_SQL, key) or {}
        described["view_definition"] = _truncate(mview.get("QUERY"))
        described["refresh"] = {
            "mode": mview.get("REFRESH_MODE"),
            "method": mview.get("REFRESH_METHOD"),
            "last": str(mview.get("LAST_REFRESH_DATE") or "") or None,
        }
    return {name: value for name, value in described.items() if value not in (None, [], "")}


# endregion


# region Relationships
UNIQUE_KEYS_SQL = (
    f"select c.owner, c.table_name, {CONS_COLUMNS.format(alias='c')} as columns "
    "from all_constraints c where c.owner in ({owners}) and c.constraint_type in ('P', 'U')"
)
NULLABLE_SQL = (
    "select owner, table_name, column_name from all_tab_columns "
    "where owner in ({owners}) and nullable = 'Y'"
)
RELATION_COLUMNS_SQL = (
    "select c.owner, c.table_name, c.column_name, c.data_type, c.data_length, m.comments "
    "from all_tab_columns c join all_tables t on t.owner = c.owner and t.table_name = c.table_name "
    "left join all_col_comments m on m.owner = c.owner and m.table_name = c.table_name "
    "and m.column_name = c.column_name where c.owner in ({owners}) and t.table_name not like 'BIN$%'"
)
SINGLE_KEYS_SQL = (
    "select c.owner, c.table_name, x.column_name from all_constraints c "
    "join all_cons_columns x on x.owner = c.owner and x.constraint_name = c.constraint_name "
    "where c.owner in ({owners}) and c.constraint_type in ('P', 'U') "
    "and (select count(*) from all_cons_columns y where y.owner = c.owner "
    "and y.constraint_name = c.constraint_name) = 1 "
    "union "
    "select i.table_owner, i.table_name, x.column_name from all_indexes i "
    "join all_ind_columns x on x.index_owner = i.owner and x.index_name = i.index_name "
    "where i.table_owner in ({owners}) and i.uniqueness = 'UNIQUE' "
    "and (select count(*) from all_ind_columns y where y.index_owner = i.owner "
    "and y.index_name = i.index_name) = 1"
)
DECLARED_FK_COLUMNS_SQL = (
    "select c.owner, c.table_name, x.column_name from all_constraints c "
    "join all_cons_columns x on x.owner = c.owner and x.constraint_name = c.constraint_name "
    "where c.owner in ({owners}) and c.constraint_type = 'R'"
)
FK_ACTIONS = {"CASCADE": "cascade", "SET NULL": "set null", "NO ACTION": "no action"}


def list_relationships(
    session: OracleSession, target: DatabaseTarget, schema: str | None = None
) -> dict[str, Any]:
    owners = owner_scope(session, schema)
    binds = Binds()
    in_owners = binds.many(owners)
    rows = session.records(
        FOREIGN_KEYS_SQL.format(where=f"c.owner in ({in_owners})", limit=1001), binds.values, max_rows=1001
    )
    unique = {
        (row["OWNER"], row["TABLE_NAME"], _split(row["COLUMNS"]))
        for row in session.records(UNIQUE_KEYS_SQL.format(owners=in_owners), binds.values, max_rows=5000)
    }
    nullable = {
        (row["OWNER"], row["TABLE_NAME"], row["COLUMN_NAME"])
        for row in session.records(NULLABLE_SQL.format(owners=in_owners), binds.values, max_rows=RELATIONSHIP_COLUMNS)
    }
    items = []
    for row in rows[:1000]:
        columns = _split(row["FROM_COLUMNS"])
        from_key = (row["FROM_OWNER"], row["FROM_TABLE"])
        items.append(
            {
                "name": row["CONSTRAINT_NAME"],
                "from_table": f"{from_key[0]}.{from_key[1]}",
                "from_columns": list(columns),
                "to_table": f"{row['TO_OWNER']}.{row['TO_TABLE']}",
                "to_columns": list(_split(row["TO_COLUMNS"])),
                "on_delete": FK_ACTIONS.get(row["DELETE_RULE"], row["DELETE_RULE"]),
                "enabled": row["STATUS"] == "ENABLED",
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
    session: OracleSession, target: DatabaseTarget, schema: str | None = None
) -> dict[str, Any]:
    owners = owner_scope(session, schema)
    binds = Binds()
    in_owners = binds.many(owners)
    column_rows = session.records(
        RELATION_COLUMNS_SQL.format(owners=in_owners), binds.values, max_rows=RELATIONSHIP_COLUMNS
    )
    columns = {
        (row["OWNER"], row["TABLE_NAME"], row["COLUMN_NAME"]): ColumnInfo(
            schema=row["OWNER"],
            table=row["TABLE_NAME"],
            column=row["COLUMN_NAME"],
            data_type=row["DATA_TYPE"],
            length=row["DATA_LENGTH"],
            text=row["COMMENTS"],
        )
        for row in column_rows
    }
    key_rows = session.records(SINGLE_KEYS_SQL.format(owners=in_owners), binds.values, max_rows=5000)
    keys = [
        columns[(row["OWNER"], row["TABLE_NAME"], row["COLUMN_NAME"])]
        for row in key_rows
        if (row["OWNER"], row["TABLE_NAME"], row["COLUMN_NAME"]) in columns
    ]
    declared = {
        (row["OWNER"], row["TABLE_NAME"], row["COLUMN_NAME"])
        for row in session.records(DECLARED_FK_COLUMNS_SQL.format(owners=in_owners), binds.values, max_rows=5000)
    }
    payload = suggest_relationships(list(columns.values()), keys, declared)
    if len(column_rows) >= RELATIONSHIP_COLUMNS:
        payload["note"] += " Owners grandes: só parte das colunas foi analisada; filtre por owner."
    return payload


# endregion


# region Rules and definitions
CHECKS_SQL = (
    "select c.owner, c.table_name, c.constraint_name, c.search_condition_vc as check_clause, c.status "
    f"from all_constraints c where c.constraint_type = 'C' and {NOT_A_NOT_NULL} and {{where}} "
    "order by c.table_name, c.constraint_name fetch first {limit} rows only"
)
DEFAULTS_SQL = (
    "select owner, table_name, column_name, data_default, identity_column from all_tab_columns "
    "where {where} and (identity_column = 'YES' or default_length > 0) "
    "order by table_name, column_id fetch first {limit} rows only"
)
SEQUENCES_SQL = (
    "select sequence_owner, sequence_name, min_value, max_value, increment_by, cycle_flag, last_number "
    "from all_sequences where sequence_owner in ({owners}) fetch first {limit} rows only"
)


def list_rules(session: OracleSession, target: DatabaseTarget, table: str | None = None) -> dict[str, Any]:
    binds = Binds()
    owners = ""
    if table:
        relation = resolve_table(session, None, table)
        owner, name = binds.add(relation["OWNER"]), binds.add(relation["NAME"])
        checks_where = f"c.owner = {owner} and c.table_name = {name}"
        triggers_where = f"table_owner = {owner} and table_name = {name}"
        policies_where = f"object_owner = {owner} and object_name = {name}"
        plain_where = f"owner = {owner} and table_name = {name}"
    else:
        owners = binds.many(owner_scope(session))
        checks_where = f"c.owner in ({owners})"
        triggers_where = f"table_owner in ({owners})"
        policies_where = f"object_owner in ({owners})"
        plain_where = f"owner in ({owners})"
    params = binds.values
    defaults = _lower_keys(session.records(DEFAULTS_SQL.format(where=plain_where, limit=MAX_RULES_PER_KIND), params))
    for row in defaults:
        row["data_default"] = (row.get("data_default") or "").strip() or None
    rules: dict[str, Any] = {
        "check_constraints": shape_records(
            _lower_keys(session.records(CHECKS_SQL.format(where=checks_where, limit=MAX_RULES_PER_KIND), params))
        ),
        "triggers": _trigger_rows(
            session.records(TRIGGERS_SQL.format(where=triggers_where, limit=MAX_RULES_PER_KIND), params)
        ),
        "row_level_security_policies": shape_records(
            _lower_keys(session.records(POLICIES_SQL.format(where=policies_where, limit=MAX_RULES_PER_KIND), params))
        ),
        "defaults_and_identity": shape_records(defaults),
    }
    if not table:
        rules["sequences"] = shape_records(
            _lower_keys(
                session.records(SEQUENCES_SQL.format(owners=owners, limit=MAX_RULES_PER_KIND), params)
            )
        )
    rules = {key: value for key, value in rules.items() if value}
    rules["note"] = (
        "No Oracle muitas regras de negócio estão em packages, procedures e triggers PL/SQL. "
        "Use list_routines e get_definition para lê-las."
    )
    return rules


SOURCE_SQL = (
    "select owner, name, type, line, text from all_source "
    "where owner in ({owners}) and name = {name} "
    "order by owner, name, decode(type, 'PACKAGE', 1, 'TYPE', 1, 2), type, line"
)
VIEW_SOURCE_SQL = "select owner, view_name, text_vc from all_views where owner in ({owners}) and view_name = {name}"
MVIEW_SOURCE_SQL = "select owner, mview_name, query from all_mviews where owner in ({owners}) and mview_name = {name}"
SOURCE_KINDS = {"PACKAGE": "package spec", "PACKAGE BODY": "package body", "TYPE": "type spec", "TYPE BODY": "type body"}


def _source_definitions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[str]] = {}
    for row in rows:
        grouped.setdefault((row["OWNER"], row["NAME"], row["TYPE"]), []).append(row["TEXT"] or "")
    found = []
    for (owner, name, kind), lines in grouped.items():
        source = "".join(lines)
        item: dict[str, Any] = {
            "name": f"{owner}.{name}",
            "kind": SOURCE_KINDS.get(kind, kind.lower()),
            "definition": _truncate(source),
        }
        if " wrapped" in source[:200].lower():
            item["note"] = "Código PL/SQL ofuscado (wrapped): o texto original não está no banco."
        found.append(item)
    return found


def get_definition(
    session: OracleSession, target: DatabaseTarget, name: str, schema: str | None = None
) -> dict[str, Any]:
    if schema is None and "." in name:
        schema, name = name.split(".", 1)
    owners = owner_scope(session, schema)
    name = name.strip('"') if name.startswith('"') else name.upper()
    binds = Binds()
    in_owners = binds.many(owners)
    bound = binds.add(name)
    found = _source_definitions(
        session.records(SOURCE_SQL.format(owners=in_owners, name=bound), binds.values, max_rows=MAX_SOURCE_LINES)
    )
    found += [
        {"name": f"{row['OWNER']}.{row['VIEW_NAME']}", "kind": "view", "definition": _truncate(row["TEXT_VC"])}
        for row in session.records(VIEW_SOURCE_SQL.format(owners=in_owners, name=bound), binds.values)
    ]
    found += [
        {"name": f"{row['OWNER']}.{row['MVIEW_NAME']}", "kind": "materialized view", "definition": _truncate(row["QUERY"])}
        for row in session.records(MVIEW_SOURCE_SQL.format(owners=in_owners, name=bound), binds.values)
    ]
    if not found:
        raise DatabaseAccessError(
            f"Nenhum package, procedure, function, trigger, type ou view chamado '{name}'. "
            "Use list_routines ou list_tables para ver os nomes exatos."
        )
    return {"definitions": shape_records(found)}


# endregion


# region Data understanding
COLUMN_COMMENTS_SQL = (
    "select column_name, comments from all_col_comments where owner = :owner and table_name = :name"
)
COLUMN_STATS_SQL = (
    "select s.column_name, c.data_type, s.num_distinct, s.num_nulls, s.low_value, s.high_value, "
    "s.histogram, s.last_analyzed from all_tab_col_statistics s join all_tab_columns c "
    "on c.owner = s.owner and c.table_name = s.table_name and c.column_name = s.column_name "
    "where s.owner = :owner and s.table_name = :name order by c.column_id"
)
SENSITIVE_COMMENTS_SQL = (
    "select distinct column_name, comments from all_col_comments "
    "where owner in ({owners}) and column_name in ({names}) and comments is not null"
)
TEXT_TYPES = ("CHAR", "VARCHAR2", "NCHAR", "NVARCHAR2")


def _described_as_sensitive(rows: list[dict[str, Any]]) -> frozenset[str]:
    return frozenset(row["COLUMN_NAME"] for row in rows if is_sensitive_column(row.get("COMMENTS")))


def _decode_number(raw: bytes) -> float | None:
    if not raw or raw == b"\x80":
        return 0.0 if raw == b"\x80" else None
    positive = raw[0] & 0x80
    if positive:
        exponent = (raw[0] & 0x7F) - 65
        digits = [byte - 1 for byte in raw[1:]]
    else:
        exponent = ((~raw[0]) & 0x7F) - 65
        digits = [101 - byte for byte in raw[1:] if byte != 102]
    value = sum(digit * 100.0 ** (exponent - index) for index, digit in enumerate(digits))
    return value if positive else -value


def _decode_bound(data_type: str, raw: Any) -> Any:
    if not isinstance(raw, (bytes, bytearray)):
        return None
    if data_type in TEXT_TYPES:
        return bytes(raw).decode("utf-8", errors="replace")
    if data_type == "NUMBER":
        return _decode_number(bytes(raw))
    if data_type == "DATE" and len(raw) == 7:
        century, year, month, day = raw[0] - 100, raw[1] - 100, raw[2], raw[3]
        return f"{century * 100 + year:04d}-{month:02d}-{day:02d}"
    return None


def column_profile(
    session: OracleSession, target: DatabaseTarget, table: str, schema: str | None = None
) -> dict[str, Any]:
    relation = resolve_table(session, schema, table)
    key = {"owner": relation["OWNER"], "name": relation["NAME"]}
    sensitive = _described_as_sensitive(session.records(COLUMN_COMMENTS_SQL, key))
    info = session.one(TABLE_INFO_SQL, key) or {}
    rows = info.get("NUM_ROWS") or 0
    profile = []
    for row in session.records(COLUMN_STATS_SQL, key):
        column = row["COLUMN_NAME"]
        hidden = column in sensitive or is_sensitive_column(column)
        nulls = row["NUM_NULLS"]
        profile.append(
            {
                "column": column,
                "distinct": row["NUM_DISTINCT"],
                "null_fraction": round(nulls / rows, 4) if rows and nulls is not None else None,
                "approx_min": "[mascarado]" if hidden else _decode_bound(row["DATA_TYPE"], row["LOW_VALUE"]),
                "approx_max": "[mascarado]" if hidden else _decode_bound(row["DATA_TYPE"], row["HIGH_VALUE"]),
                "histogram": row["HISTOGRAM"],
                "statistics_updated": row["LAST_ANALYZED"],
            }
        )
    payload: dict[str, Any] = {
        "table": f"{key['owner']}.{key['name']}",
        "rows": rows or None,
        "columns": shape_records(profile),
    }
    if not profile:
        payload["note"] = (
            "Sem estatísticas para esta tabela (DBMS_STATS nunca rodou nela). "
            "Use sample_rows ou uma consulta agregada."
        )
    return payload


def _quoted(owner: str, name: str) -> str:
    return ".".join('"' + part.replace('"', '""') + '"' for part in (owner, name))


def sample_rows(
    session: OracleSession,
    target: DatabaseTarget,
    table: str,
    schema: str | None = None,
    limit: int = DEFAULT_SAMPLE_ROWS,
) -> dict[str, Any]:
    relation = resolve_table(session, schema, table)
    key = {"owner": relation["OWNER"], "name": relation["NAME"]}
    bounded = max(1, min(int(limit), MAX_SAMPLE_ROWS))
    sensitive = _described_as_sensitive(session.records(COLUMN_COMMENTS_SQL, key))
    result = session.fetch(
        f"select * from {_quoted(key['owner'], key['name'])} fetch first {bounded} rows only", max_rows=bounded
    )
    result.truncated = False
    return {"table": f"{key['owner']}.{key['name']}", **shape_rows(result, sensitive)}


# endregion


# region Engine
KNOWN_SCHEMAS_SQL = "select username from all_users where username in ({names})"


class OracleEngine(Engine):
    name = "oracle"
    dialect = DIALECT

    def open_session(self, target: DatabaseTarget, check_posture: bool = True):
        return readonly_session(target, check_posture=check_posture)

    def check_connection(self, target: DatabaseTarget) -> dict[str, Any]:
        try:
            with readonly_session(target, check_posture=False) as session:
                posture = read_posture(session, target.schemas)
                owners = [row["USERNAME"] for row in session.records(VISIBLE_OWNERS_SQL)]
        except DatabaseAccessError as exc:
            return {"ok": False, "error": str(exc)}
        warnings = posture.warnings()
        configured = [name.upper() for name in target.schemas]
        missing = [name for name in configured if name not in owners]
        if missing:
            warnings.append(f"Owners liberados sem tabelas visíveis para o usuário: {', '.join(missing)}.")
        refusal = posture.refusal(target.alias)
        return {
            "ok": refusal is None,
            "server_version": posture.server_version,
            "role": posture.role,
            "read_only_role": not (posture.owns_tables or posture.write_grants or posture.refused),
            "schemas": owners,
            "warnings": warnings,
            "error": refusal,
        }

    def known_schemas(self, session: OracleSession, candidates: set[str]) -> set[str]:
        names = sorted({name.upper() for name in candidates})[:200]
        if not names:
            return set()
        binds = Binds()
        rows = session.records(KNOWN_SCHEMAS_SQL.format(names=binds.many(names)), binds.values)
        return {row["USERNAME"] for row in rows}

    def sensitive_columns(
        self, session: OracleSession, target: DatabaseTarget, columns: list[str]
    ) -> frozenset[str]:
        names = sorted({column.upper() for column in columns})[:200]
        if not names:
            return frozenset()
        try:
            owners = owner_scope(session)
        except DatabaseAccessError:
            return frozenset()
        binds = Binds()
        query = SENSITIVE_COMMENTS_SQL.format(owners=binds.many(owners), names=binds.many(names))
        sensitive = _described_as_sensitive(session.records(query, binds.values, max_rows=5000))
        return frozenset(column for column in columns if column.upper() in sensitive)

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


ENGINE = OracleEngine()


# endregion

"""Db2 for i (AS400) engine over Mapepire.

A read-only job still runs side-effect functions such as ``QSYS2.QCMDEXC``,
so the dialect blocklist is the real barrier for free queries.
"""
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
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

MAPEPIRE_PORT = 8076
QUERY_TIMEOUT_SECONDS = 15
CATALOG_ROWS = 1000
RELATIONSHIP_COLUMNS = 10_000
MAX_LISTED_OBJECTS = 500
MAX_RULES_PER_KIND = 300
MAX_SOURCE_CHARS = 6000
LARGEST_TABLES = 15

CONNECTION_OPTIONS = {
    "naming": "sql",
    "access": "read only",
    "translate binary": "true",
    "date format": "iso",
    "time format": "iso",
}

REFUSED_AUTHORITIES = ("*ALLOBJ", "*SECADM")
WARNED_AUTHORITIES = ("*JOBCTL", "*SAVSYS", "*SERVICE", "*SPLCTL", "*IOSYSCFG", "*AUDIT")

DIALECT = Dialect(
    name="db2i",
    blocked_functions=(
        "QCMDEXC",
        "GENERATE_SPREADSHEET",
        "LPRINTF",
        "OVERRIDE_QAQQINI",
        "CHANGE_PLAN_CACHE_SIZE",
        "DUMP_PLAN_CACHE",
        "SEND_MESSAGE",
        "SET_SERVER_SBS_ROUTING",
    ),
    system_schemas=("QSYS2", "SYSIBM", "SYSIBMADM", "QSYS", "SYSPROC", "SYSFUN"),
    blocked_schemas=("SYSTOOLS",),
    blocked_prefixes=("IFS_", "HTTP_"),
    folds_to_upper=True,
    row_limit="FETCH FIRST {rows} ROWS ONLY",
)

TABLE_TYPES = {
    "T": "table",
    "P": "physical file",
    "L": "logical file",
    "V": "view",
    "M": "materialized query table",
    "A": "alias",
}
ROUTINE_KINDS = {"FUNCTION": "function", "PROCEDURE": "procedure"}

QUERY_ERRORS = {
    "42505": "A conexão é somente leitura: o banco recusou a instrução.",
    "57014": (
        f"A consulta passou do limite de {QUERY_TIMEOUT_SECONDS}s e foi cancelada. "
        "Use filtros ou agregações mais estreitas."
    ),
}
CONNECTION_FAILURES = (
    (("password is expired", "password expired"), "a senha do perfil expirou; troque-a no IBM i"),
    (("disabled",), "o perfil está desabilitado no IBM i"),
    (("password", "not valid", "user id", "signon"), "usuário ou senha recusados"),
    (("403",), "o Mapepire recusou a conexão (regras de acesso no iprules.conf)"),
    (("certificate", "ssl", "tls"), "falha de TLS; use ssl_mode=require para certificado autoassinado ou informe ca_file"),
    (("timed out", "timeout"), "tempo de conexão esgotado; confira rede, firewall e a porta do Mapepire"),
    (("refused", "unreachable", "getaddrinfo", "name or service", "nodename"), "servidor inacessível; confira host, porta e se o Mapepire está ativo"),
)
STATE_IN_ERROR = re.compile(r"'sql_state':\s*'(\w+)'")
MESSAGE_IN_ERROR = re.compile(r"'error':\s*'([^']*)'")


def _load_mapepire():
    from mapepire_python import DaemonServer, SQLJob

    return DaemonServer, SQLJob


# region Errors
def _error_parts(exc: Exception) -> tuple[str | None, str]:
    payload = exc.args[0] if exc.args else None
    if isinstance(payload, dict):
        return payload.get("sql_state"), str(payload.get("error") or "")
    text = str(exc)
    state = STATE_IN_ERROR.search(text)
    message = MESSAGE_IN_ERROR.search(text)
    return (state.group(1) if state else None), (message.group(1) if message else text)


def describe_query_error(exc: Exception) -> str:
    state, message = _error_parts(exc)
    if state in QUERY_ERRORS:
        return QUERY_ERRORS[state]
    if state in ("42501", "42502"):
        return f"Sem autoridade para ler este objeto: {message}"
    return f"Erro do Db2 for i ({state or 'desconhecido'}): {message}"[:600]


def describe_connection_error(exc: Exception, alias: str) -> str:
    text = str(exc).lower()
    reason = "não foi possível abrir a sessão"
    for needles, candidate in CONNECTION_FAILURES:
        if any(needle in text for needle in needles):
            reason = candidate
            break
    return f"Não foi possível conectar ao banco '{alias}': {reason}."


# endregion


# region Session
class Db2iSession:
    """One Mapepire job; each statement bounded in rows and in time."""

    def __init__(self, target: DatabaseTarget, job: Any):
        self.target = target
        self._job = job
        self._scope: list[str] | None = None

    def abandon(self) -> None:
        job, self._job = self._job, None
        if job is not None:
            threading.Thread(target=job.close, daemon=True).start()

    def fetch(self, query: str, params: Any = None, max_rows: int = MAX_QUERY_ROWS) -> QueryResult:
        if self._job is None:
            raise DatabaseAccessError("A sessão com o banco foi encerrada.")
        outcome: dict[str, Any] = {}
        job = self._job

        def work() -> None:
            try:
                options = {"isClCommand": False, "parameters": list(params) if params else None}
                with job.query(query, options) as statement:
                    result = statement.run(rows_to_fetch=max_rows + 1)
                outcome["result"] = asdict(result) if is_dataclass(result) else result
            except Exception as exc:  # noqa: BLE001
                outcome["error"] = exc

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        worker.join(QUERY_TIMEOUT_SECONDS)
        if worker.is_alive():
            self.abandon()
            raise DatabaseAccessError(QUERY_ERRORS["57014"])
        if "error" in outcome:
            raise DatabaseAccessError(describe_query_error(outcome["error"]))
        return _to_result(outcome["result"], max_rows)

    def records(self, query: str, params: Any = None, max_rows: int = CATALOG_ROWS) -> list[dict[str, Any]]:
        return self.fetch(query, params, max_rows).records()

    def one(self, query: str, params: Any = None) -> dict[str, Any] | None:
        found = self.records(query, params, max_rows=1)
        return found[0] if found else None


def _to_result(raw: dict[str, Any], max_rows: int) -> QueryResult:
    metadata = raw.get("metadata") or {}
    columns = [column["name"] for column in metadata.get("columns") or []]
    data = raw.get("data") or []
    if not columns and data:
        columns = list(data[0].keys())
    rows = [[row.get(column) for column in columns] for row in data]
    return QueryResult(columns=columns, rows=rows[:max_rows], truncated=len(rows) > max_rows)


def _ca_data(target: DatabaseTarget) -> str | None:
    path = target.option("ca_file")
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise DatabaseAccessError(
            f"Não foi possível ler o certificado '{path}' do banco '{target.alias}'."
        ) from exc


def _server_host(target: DatabaseTarget, verify: bool) -> str:
    """Host to dial; under the public-network policy, a checked address pinned when TLS won't verify the name."""
    if not public_network_only(target):
        return target.host
    check_ssl_mode(target)
    addresses = resolve_allowed_addresses(target)
    return target.host if verify else addresses[0]


def open_job(target: DatabaseTarget):
    daemon_server, sql_job = _load_mapepire()
    verify = target.ssl_mode in ("verify-ca", "verify-full")
    ca = _ca_data(target)
    if verify and not ca:
        raise DatabaseAccessError(
            f"Banco '{target.alias}': ssl_mode={target.ssl_mode} exige o certificado da CA em ca_file."
        )
    server = daemon_server(
        host=_server_host(target, verify),
        port=target.port or MAPEPIRE_PORT,
        user=target.username,
        password=target.password,
        ignoreUnauthorized=not verify,
        ca=ca,
    )
    options: dict[str, Any] = dict(CONNECTION_OPTIONS)
    if target.schemas:
        options["libraries"] = list(target.schemas)
    job = sql_job(options=options)
    try:
        job.connect(server)
    except Exception as exc:
        logger.warning(
            "Mapepire connection failed for alias '%s' (%s:%s): %s",
            target.alias,
            target.host,
            target.port,
            type(exc).__name__,
        )
        raise DatabaseAccessError(describe_connection_error(exc, target.alias)) from exc
    return job


# endregion


# region Posture
@dataclass(frozen=True)
class Posture:
    server_version: str
    role: str
    authorities: frozenset[str]
    group_authorities: frozenset[str]

    @property
    def all_authorities(self) -> frozenset[str]:
        return self.authorities | self.group_authorities

    @property
    def refused(self) -> list[str]:
        return sorted(a for a in self.all_authorities if a in REFUSED_AUTHORITIES)

    def warnings(self) -> list[str]:
        notes = []
        warned = sorted(a for a in self.all_authorities if a in WARNED_AUTHORITIES)
        if warned:
            notes.append(
                f"O perfil tem autoridades especiais ({', '.join(warned)}). O recomendado "
                "é um perfil só com *USE nas bibliotecas de dados."
            )
        if self.group_authorities - self.authorities:
            notes.append("Parte das autoridades especiais vem do perfil de grupo.")
        return notes


VERSION_SQL = (
    "select 'V' || os_version || 'R' || os_release as version, current_user as role "
    "from sysibmadm.env_sys_info"
)
PROFILE_SQL = (
    "select special_authorities, group_profile_name, supplemental_group_list "
    "from qsys2.user_info where authorization_name = current_user"
)
GROUP_AUTHORITIES_SQL = (
    "select special_authorities from qsys2.user_info where authorization_name in ({names})"
)


def _authorities(text: str | None) -> frozenset[str]:
    return frozenset(item for item in (text or "").split() if item.startswith("*"))


def _in(values: list[Any]) -> str:
    return ", ".join("?" for _ in values)


def read_posture(session: Db2iSession) -> Posture:
    identity = session.one(VERSION_SQL) or {}
    profile = session.one(PROFILE_SQL) or {}
    groups = [
        name
        for name in [profile.get("GROUP_PROFILE_NAME"), *(profile.get("SUPPLEMENTAL_GROUP_LIST") or "").split()]
        if name and name != "*NONE"
    ]
    group_authorities: frozenset[str] = frozenset()
    if groups:
        rows = session.records(GROUP_AUTHORITIES_SQL.format(names=_in(groups)), groups)
        group_authorities = frozenset().union(
            *(_authorities(row.get("SPECIAL_AUTHORITIES")) for row in rows)
        )
    return Posture(
        server_version=identity.get("VERSION") or "",
        role=identity.get("ROLE") or "",
        authorities=_authorities(profile.get("SPECIAL_AUTHORITIES")),
        group_authorities=group_authorities,
    )


def enforce_posture(target: DatabaseTarget, session: Db2iSession) -> Posture:
    posture = _posture_cache.get(target.cache_key)
    if posture is None:
        posture = read_posture(session)
        _posture_cache.put(target.cache_key, posture)
    if posture.refused:
        raise DatabaseAccessError(
            f"O perfil '{posture.role}' do banco '{target.alias}' tem "
            f"{', '.join(posture.refused)}. Por segurança o acesso foi recusado: use um "
            "perfil sem autoridades especiais, só com *USE nas bibliotecas de dados."
        )
    return posture


@contextmanager
def readonly_session(target: DatabaseTarget, check_posture: bool = True) -> Iterator[Db2iSession]:
    job = open_job(target)
    session = Db2iSession(target, job)
    try:
        if check_posture:
            enforce_posture(target, session)
        yield session
    finally:
        session.abandon()


# endregion


# region Scope
LIBRARY_LIST_SQL = (
    "select schema_name from qsys2.library_list_info "
    "where type in ('CURRENT', 'USER') order by ordinal_position"
)
NO_LIBRARIES = (
    "Nenhuma biblioteca liberada para o banco '{alias}' e a lista de bibliotecas do "
    "perfil está vazia. Configure as bibliotecas (schemas) no conector."
)


def library_scope(session: Db2iSession, schema: str | None = None) -> list[str]:
    """Libraries an operation may read: the allowlist, else the profile's library list."""
    if session._scope is None:
        configured = [name.upper() for name in session.target.schemas]
        session._scope = configured or [
            row["SCHEMA_NAME"] for row in session.records(LIBRARY_LIST_SQL)
        ]
    if not session._scope:
        raise DatabaseAccessError(NO_LIBRARIES.format(alias=session.target.alias))
    if schema is None:
        return session._scope
    requested = schema.upper()
    if requested not in session._scope:
        raise DatabaseAccessError(
            f"A biblioteca '{requested}' está fora das liberadas para este banco "
            f"({', '.join(session._scope)})."
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
        payload["note"] = "Lista cortada. Filtre por biblioteca ou por parte do nome."
    return payload


def _truncate(text: str | None, limit: int = MAX_SOURCE_CHARS) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return f"{text[:limit]}\n-- … (+{len(text) - limit} caracteres omitidos)"


def _lower_keys(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key.lower(): value for key, value in row.items()} for row in rows]


# endregion


# region Overview and listings
SCHEMAS_SQL = "select schema_name, schema_text from qsys2.sysschemas where schema_name in ({libs})"
TABLE_COUNTS_SQL = (
    "select table_schema, table_type, count(*) as n from qsys2.systables "
    "where table_schema in ({libs}) and file_type <> 'S' group by table_schema, table_type"
)
ROUTINE_COUNTS_SQL = (
    "select routine_schema, count(*) as n from qsys2.sysroutines "
    "where routine_schema in ({libs}) group by routine_schema"
)
LARGEST_TABLES_SQL = (
    "select table_schema, table_name, number_rows, data_size from qsys2.systablestat "
    "where table_schema in ({libs}) order by data_size desc fetch first {limit} rows only"
)


def database_overview(session: Db2iSession, target: DatabaseTarget) -> dict[str, Any]:
    libraries = library_scope(session)
    libs = _in(libraries)
    identity = session.one(VERSION_SQL) or {}
    texts = {
        row["SCHEMA_NAME"]: row["SCHEMA_TEXT"]
        for row in session.records(SCHEMAS_SQL.format(libs=libs), libraries)
    }
    counts: dict[str, dict[str, int]] = {name: {} for name in libraries}
    for row in session.records(TABLE_COUNTS_SQL.format(libs=libs), libraries):
        kind = TABLE_TYPES.get(row["TABLE_TYPE"], row["TABLE_TYPE"])
        counts.setdefault(row["TABLE_SCHEMA"], {})[kind] = row["N"]
    routines = {
        row["ROUTINE_SCHEMA"]: row["N"]
        for row in session.records(ROUTINE_COUNTS_SQL.format(libs=libs), libraries)
    }
    largest = session.records(
        LARGEST_TABLES_SQL.format(libs=libs, limit=LARGEST_TABLES), libraries
    )
    return {
        "server_version": identity.get("VERSION"),
        "connected_as": identity.get("ROLE"),
        "libraries": [
            {
                "library": name,
                "text": texts.get(name),
                "objects": counts.get(name, {}),
                "routines": routines.get(name, 0),
            }
            for name in libraries
        ],
        "largest_tables": shape_records(_lower_keys(largest)),
        "note": (
            "No IBM i as bibliotecas fazem o papel de schemas. As descrições "
            "(text) costumam ser a melhor documentação do sistema."
        ),
    }


LIST_TABLES_SQL = (
    "select t.table_schema, t.table_name, t.system_table_name, t.table_type, t.table_text, "
    "t.base_table_name, s.number_rows "
    "from qsys2.systables t left join qsys2.systablestat s "
    "on s.table_schema = t.table_schema and s.table_name = t.table_name "
    "where t.table_schema in ({libs}) and t.file_type <> 'S' {name_clause} "
    "order by t.table_schema, t.table_name fetch first {limit} rows only"
)
NAME_CLAUSE = (
    "and (t.table_name like ? escape '\\' or t.system_table_name like ? escape '\\' "
    "or upper(t.table_text) like ? escape '\\')"
)


def list_tables(
    session: Db2iSession, target: DatabaseTarget, schema: str | None = None, name_filter: str | None = None
) -> dict[str, Any]:
    libraries = library_scope(session, schema)
    pattern = _like(name_filter)
    params: list[Any] = [*libraries, *([pattern] * 3 if pattern else [])]
    rows = session.records(
        LIST_TABLES_SQL.format(
            libs=_in(libraries),
            name_clause=NAME_CLAUSE if pattern else "",
            limit=MAX_LISTED_OBJECTS + 1,
        ),
        params,
        max_rows=MAX_LISTED_OBJECTS + 1,
    )
    for row in rows:
        row["TABLE_TYPE"] = TABLE_TYPES.get(row["TABLE_TYPE"], row["TABLE_TYPE"])
    return _cap(_lower_keys(rows), MAX_LISTED_OBJECTS)


LIST_ROUTINES_SQL = (
    "select routine_schema, routine_name, routine_type, routine_body, external_name, "
    "external_language, sql_data_access, routine_text from qsys2.sysroutines "
    "where routine_schema in ({libs}) {name_clause} "
    "order by routine_schema, routine_name fetch first {limit} rows only"
)


def list_routines(
    session: Db2iSession, target: DatabaseTarget, schema: str | None = None, name_filter: str | None = None
) -> dict[str, Any]:
    libraries = library_scope(session, schema)
    pattern = _like(name_filter)
    rows = session.records(
        LIST_ROUTINES_SQL.format(
            libs=_in(libraries),
            name_clause="and routine_name like ? escape '\\'" if pattern else "",
            limit=MAX_LISTED_OBJECTS + 1,
        ),
        [*libraries, *([pattern] if pattern else [])],
        max_rows=MAX_LISTED_OBJECTS + 1,
    )
    for row in rows:
        row["ROUTINE_TYPE"] = ROUTINE_KINDS.get(row["ROUTINE_TYPE"], row["ROUTINE_TYPE"])
    return _cap(_lower_keys(rows), MAX_LISTED_OBJECTS)


# endregion


# region Describe
RESOLVE_SQL = (
    "select table_schema, table_name, system_table_name, table_type, table_text, long_comment, "
    "base_table_schema, base_table_name from qsys2.systables "
    "where table_schema in ({libs}) and (table_name = ? or system_table_name = ?) and file_type <> 'S' "
    "order by table_schema"
)
STATS_SQL = (
    "select number_rows, data_size from qsys2.systablestat where table_schema = ? and table_name = ?"
)
COLUMNS_SQL = (
    "select column_name, system_column_name, data_type, length, numeric_scale, is_nullable, "
    "has_default, column_default, is_identity, column_text, column_heading, ccsid "
    "from qsys2.syscolumns where table_schema = ? and table_name = ? order by ordinal_position"
)
KEY_COLUMNS = (
    "(select listagg(k.column_name, ', ') within group (order by k.ordinal_position) "
    "from qsys2.syskeycst k where k.constraint_schema = {alias}.constraint_schema "
    "and k.constraint_name = {alias}.constraint_name)"
)
CONSTRAINTS_SQL = (
    "select c.constraint_name, c.constraint_type, c.enabled, k.check_clause, "
    f"{KEY_COLUMNS.format(alias='c')} as columns "
    "from qsys2.syscst c left join qsys2.syschkcst k "
    "on k.constraint_schema = c.constraint_schema and k.constraint_name = c.constraint_name "
    "where c.table_schema = ? and c.table_name = ? and c.constraint_type <> 'FOREIGN KEY' "
    "order by c.constraint_type, c.constraint_name"
)
FOREIGN_KEYS_SQL = (
    "select r.constraint_name, c.table_schema as from_schema, c.table_name as from_table, "
    "u.table_schema as to_schema, u.table_name as to_table, r.delete_rule, r.update_rule, "
    f"{KEY_COLUMNS.format(alias='r')} as from_columns, "
    "(select listagg(k.column_name, ', ') within group (order by k.ordinal_position) "
    "from qsys2.syskeycst k where k.constraint_schema = r.unique_constraint_schema "
    "and k.constraint_name = r.unique_constraint_name) as to_columns "
    "from qsys2.sysrefcst r "
    "join qsys2.syscst c on c.constraint_schema = r.constraint_schema and c.constraint_name = r.constraint_name "
    "join qsys2.syscst u on u.constraint_schema = r.unique_constraint_schema "
    "and u.constraint_name = r.unique_constraint_name "
    "where {where} order by c.table_name, r.constraint_name fetch first {limit} rows only"
)
INDEXES_SQL = (
    "select i.index_schema, i.index_name, i.system_index_name, i.is_unique, i.index_text, "
    "(select listagg(k.column_name || case when k.ordering = 'D' then ' DESC' else '' end, ', ') "
    "within group (order by k.ordinal_position) from qsys2.syskeys k "
    "where k.index_schema = i.index_schema and k.index_name = i.index_name) as columns "
    "from qsys2.sysindexes i where i.table_schema = ? and i.table_name = ? order by i.index_name"
)
LOGICAL_FILES_SQL = (
    "select table_schema, table_name, system_table_name, table_text from qsys2.systables "
    "where base_table_schema = ? and base_table_name = ? and table_type = 'L' order by table_name"
)
DEPENDENT_VIEWS_SQL = (
    "select distinct view_schema, view_name from qsys2.sysviewdep "
    "where object_schema = ? and object_name = ? order by view_schema, view_name"
)
TRIGGERS_SQL = (
    "select trigger_name, action_timing, event_manipulation, action_orientation, enabled, "
    "trigger_text, trigger_program_library, trigger_program_name, action_statement "
    "from qsys2.systriggers where {where} order by event_object_table, trigger_name "
    "fetch first {limit} rows only"
)
CONTROLS_SQL = (
    "select table_schema, table_name, rcac_name, control_type, column_name, enable, ruletext "
    "from qsys2.syscontrols where {where} order by table_name, rcac_name fetch first {limit} rows only"
)
VIEW_TEXT_SQL = (
    "select view_definition from qsys2.sysviews where table_schema = ? and table_name = ? order by seqno"
)


def resolve_table(
    session: Db2iSession, schema: str | None, table: str
) -> dict[str, Any]:
    if schema is None and "." in table:
        schema, table = table.split(".", 1)
    libraries = library_scope(session, schema)
    name = table.strip('"') if table.startswith('"') else table.upper()
    matches = session.records(
        RESOLVE_SQL.format(libs=_in(libraries)), [*libraries, name, name], max_rows=20
    )
    if not matches:
        raise DatabaseAccessError(
            f"Tabela ou arquivo '{name}' não encontrado"
            + (f" na biblioteca '{schema.upper()}'" if schema else " nas bibliotecas liberadas")
            + ". Use list_tables para ver os nomes exatos."
        )
    if len(matches) > 1:
        found = ", ".join(f"{m['TABLE_SCHEMA']}.{m['TABLE_NAME']}" for m in matches)
        raise DatabaseAccessError(
            f"'{name}' existe em mais de uma biblioteca ({found}). Informe a biblioteca."
        )
    return matches[0]


def _trigger_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    shaped = []
    for row in _lower_keys(rows):
        program = row.pop("trigger_program_name", None)
        library = row.pop("trigger_program_library", None)
        statement = row.pop("action_statement", None)
        if statement:
            row["body"] = _truncate(statement)
        elif program:
            row["external_program"] = f"{library}/{program}"
            row["note"] = "Regra em programa externo (RPG/CL/COBOL): o código não está no banco."
        shaped.append(row)
    return shape_records(shaped)


def describe_table(
    session: Db2iSession, target: DatabaseTarget, table: str, schema: str | None = None
) -> dict[str, Any]:
    relation = resolve_table(session, schema, table)
    key = [relation["TABLE_SCHEMA"], relation["TABLE_NAME"]]
    stats = session.one(STATS_SQL, key) or {}
    kind = relation["TABLE_TYPE"]
    described: dict[str, Any] = {
        "table": f"{key[0]}.{key[1]}",
        "system_name": relation["SYSTEM_TABLE_NAME"],
        "kind": TABLE_TYPES.get(kind, kind),
        "text": relation["TABLE_TEXT"],
        "comment": relation["LONG_COMMENT"],
        "rows": stats.get("NUMBER_ROWS"),
        "data_size": stats.get("DATA_SIZE"),
        "based_on": (
            f"{relation['BASE_TABLE_SCHEMA']}.{relation['BASE_TABLE_NAME']}"
            if kind == "L" and relation.get("BASE_TABLE_NAME")
            else None
        ),
        "columns": shape_records(_lower_keys(session.records(COLUMNS_SQL, key))),
        "constraints": shape_records(_lower_keys(session.records(CONSTRAINTS_SQL, key))),
        "foreign_keys": shape_records(
            _lower_keys(
                session.records(
                    FOREIGN_KEYS_SQL.format(where="c.table_schema = ? and c.table_name = ?", limit=200), key
                )
            )
        ),
        "referenced_by": shape_records(
            _lower_keys(
                session.records(
                    FOREIGN_KEYS_SQL.format(where="u.table_schema = ? and u.table_name = ?", limit=200), key
                )
            )
        ),
        "indexes": shape_records(_lower_keys(session.records(INDEXES_SQL, key))),
        "logical_files": shape_records(_lower_keys(session.records(LOGICAL_FILES_SQL, key))),
        "dependent_views": shape_records(_lower_keys(session.records(DEPENDENT_VIEWS_SQL, key))),
        "triggers": _trigger_rows(
            session.records(
                TRIGGERS_SQL.format(where="event_object_schema = ? and event_object_table = ?", limit=100), key
            )
        ),
        "row_and_column_access_controls": shape_records(
            _lower_keys(
                session.records(CONTROLS_SQL.format(where="table_schema = ? and table_name = ?", limit=100), key)
            )
        ),
    }
    if kind == "V":
        parts = session.records(VIEW_TEXT_SQL, key)
        described["view_definition"] = _truncate("".join(p["VIEW_DEFINITION"] or "" for p in parts))
    return {name: value for name, value in described.items() if value not in (None, [], "")}


# endregion


# region Relationships
UNIQUE_KEYS_SQL = (
    "select c.table_schema, c.table_name, "
    f"{KEY_COLUMNS.format(alias='c')} as columns "
    "from qsys2.syscst c where c.table_schema in ({libs}) "
    "and c.constraint_type in ('PRIMARY KEY', 'UNIQUE')"
)
NULLABLE_SQL = (
    "select table_schema, table_name, column_name from qsys2.syscolumns "
    "where table_schema in ({libs}) and is_nullable = 'Y'"
)
FK_ACTIONS = {"CASCADE": "cascade", "RESTRICT": "restrict", "NO ACTION": "no action", "SET NULL": "set null", "SET DEFAULT": "set default"}


def _split(columns: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in (columns or "").split(",") if part.strip())


def list_relationships(
    session: Db2iSession, target: DatabaseTarget, schema: str | None = None
) -> dict[str, Any]:
    libraries = library_scope(session, schema)
    libs = _in(libraries)
    rows = session.records(
        FOREIGN_KEYS_SQL.format(where=f"c.table_schema in ({libs})", limit=1001), libraries, max_rows=1001
    )
    unique = {
        (row["TABLE_SCHEMA"], row["TABLE_NAME"], _split(row["COLUMNS"]))
        for row in session.records(UNIQUE_KEYS_SQL.format(libs=libs), libraries, max_rows=5000)
    }
    nullable = {
        (row["TABLE_SCHEMA"], row["TABLE_NAME"], row["COLUMN_NAME"])
        for row in session.records(NULLABLE_SQL.format(libs=libs), libraries, max_rows=RELATIONSHIP_COLUMNS)
    }
    items = []
    for row in rows[:1000]:
        columns = _split(row["FROM_COLUMNS"])
        from_key = (row["FROM_SCHEMA"], row["FROM_TABLE"])
        items.append(
            {
                "name": row["CONSTRAINT_NAME"],
                "from_table": f"{from_key[0]}.{from_key[1]}",
                "from_columns": list(columns),
                "to_table": f"{row['TO_SCHEMA']}.{row['TO_TABLE']}",
                "to_columns": list(_split(row["TO_COLUMNS"])),
                "on_delete": FK_ACTIONS.get(row["DELETE_RULE"], row["DELETE_RULE"]),
                "on_update": FK_ACTIONS.get(row["UPDATE_RULE"], row["UPDATE_RULE"]),
                "cardinality": "one-to-one" if (*from_key, columns) in unique else "many-to-one",
                "required": not any((*from_key, column) in nullable for column in columns),
            }
        )
    payload: dict[str, Any] = {"items": shape_records(items), "count": len(items)}
    if len(rows) > 1000:
        payload["truncated"] = True
    payload["tables_without_relationships_hint"] = (
        "Sistemas AS400 raramente declaram chaves estrangeiras; use "
        "suggest_relationships para vínculos inferidos por nome e descrição."
    )
    return payload


RELATION_COLUMNS_SQL = (
    "select c.table_schema, c.table_name, c.column_name, c.data_type, c.length, c.column_text "
    "from qsys2.syscolumns c join qsys2.systables t "
    "on t.table_schema = c.table_schema and t.table_name = c.table_name "
    "where c.table_schema in ({libs}) and t.table_type in ('T', 'P') and t.file_type <> 'S'"
)
SINGLE_KEYS_SQL = (
    "select c.table_schema, c.table_name, k.column_name from qsys2.syscst c "
    "join qsys2.syskeycst k on k.constraint_schema = c.constraint_schema and k.constraint_name = c.constraint_name "
    "where c.table_schema in ({libs}) and c.constraint_type in ('PRIMARY KEY', 'UNIQUE') "
    "and (select count(*) from qsys2.syskeycst x where x.constraint_schema = c.constraint_schema "
    "and x.constraint_name = c.constraint_name) = 1 "
    "union "
    "select i.table_schema, i.table_name, s.column_name from qsys2.sysindexes i "
    "join qsys2.syskeys s on s.index_schema = i.index_schema and s.index_name = i.index_name "
    "where i.table_schema in ({libs}) and i.is_unique in ('U', 'V') and i.column_count = 1"
)
DECLARED_FK_COLUMNS_SQL = (
    "select c.table_schema, c.table_name, k.column_name from qsys2.syscst c "
    "join qsys2.syskeycst k on k.constraint_schema = c.constraint_schema and k.constraint_name = c.constraint_name "
    "where c.table_schema in ({libs}) and c.constraint_type = 'FOREIGN KEY'"
)


def suggest_table_relationships(
    session: Db2iSession, target: DatabaseTarget, schema: str | None = None
) -> dict[str, Any]:
    libraries = library_scope(session, schema)
    libs = _in(libraries)
    column_rows = session.records(
        RELATION_COLUMNS_SQL.format(libs=libs), libraries, max_rows=RELATIONSHIP_COLUMNS
    )
    columns = {
        (row["TABLE_SCHEMA"], row["TABLE_NAME"], row["COLUMN_NAME"]): ColumnInfo(
            schema=row["TABLE_SCHEMA"],
            table=row["TABLE_NAME"],
            column=row["COLUMN_NAME"],
            data_type=row["DATA_TYPE"],
            length=row["LENGTH"],
            text=row["COLUMN_TEXT"],
        )
        for row in column_rows
    }
    key_rows = session.records(SINGLE_KEYS_SQL.format(libs=libs), [*libraries, *libraries], max_rows=5000)
    keys = [
        columns[(row["TABLE_SCHEMA"], row["TABLE_NAME"], row["COLUMN_NAME"])]
        for row in key_rows
        if (row["TABLE_SCHEMA"], row["TABLE_NAME"], row["COLUMN_NAME"]) in columns
    ]
    declared = {
        (row["TABLE_SCHEMA"], row["TABLE_NAME"], row["COLUMN_NAME"])
        for row in session.records(DECLARED_FK_COLUMNS_SQL.format(libs=libs), libraries, max_rows=5000)
    }
    payload = suggest_relationships(list(columns.values()), keys, declared)
    if len(column_rows) >= RELATIONSHIP_COLUMNS:
        payload["note"] += " Bibliotecas grandes: só parte das colunas foi analisada; filtre por biblioteca."
    return payload


# endregion


# region Rules and definitions
CHECKS_SQL = (
    "select c.table_schema, c.table_name, c.constraint_name, k.check_clause, c.enabled "
    "from qsys2.syscst c join qsys2.syschkcst k "
    "on k.constraint_schema = c.constraint_schema and k.constraint_name = c.constraint_name "
    "where {where} order by c.table_name, c.constraint_name fetch first {limit} rows only"
)
SEQUENCES_SQL = (
    "select sequence_schema, sequence_name, data_type, start, increment, sequence_text "
    "from qsys2.syssequences where sequence_schema in ({libs}) fetch first {limit} rows only"
)
DEFAULTS_SQL = (
    "select table_schema, table_name, column_name, column_default, is_identity, column_text "
    "from qsys2.syscolumns where {where} and (is_identity = 'YES' or "
    "(has_default = 'Y' and column_default is not null)) "
    "order by table_name, ordinal_position fetch first {limit} rows only"
)


def list_rules(session: Db2iSession, target: DatabaseTarget, table: str | None = None) -> dict[str, Any]:
    if table:
        relation = resolve_table(session, None, table)
        params: list[Any] = [relation["TABLE_SCHEMA"], relation["TABLE_NAME"]]
        checks_where = "c.table_schema = ? and c.table_name = ?"
        triggers_where = "event_object_schema = ? and event_object_table = ?"
        plain_where = "table_schema = ? and table_name = ?"
    else:
        params = library_scope(session)
        libs = _in(params)
        checks_where = f"c.table_schema in ({libs})"
        triggers_where = f"event_object_schema in ({libs})"
        plain_where = f"table_schema in ({libs})"
    rules: dict[str, Any] = {
        "check_constraints": shape_records(
            _lower_keys(session.records(CHECKS_SQL.format(where=checks_where, limit=MAX_RULES_PER_KIND), params))
        ),
        "triggers": _trigger_rows(
            session.records(TRIGGERS_SQL.format(where=triggers_where, limit=MAX_RULES_PER_KIND), params)
        ),
        "row_and_column_access_controls": shape_records(
            _lower_keys(session.records(CONTROLS_SQL.format(where=plain_where, limit=MAX_RULES_PER_KIND), params))
        ),
        "defaults_and_identity": shape_records(
            _lower_keys(session.records(DEFAULTS_SQL.format(where=plain_where, limit=MAX_RULES_PER_KIND), params))
        ),
    }
    if not table:
        rules["sequences"] = shape_records(
            _lower_keys(
                session.records(SEQUENCES_SQL.format(libs=_in(params), limit=MAX_RULES_PER_KIND), params)
            )
        )
    rules = {key: value for key, value in rules.items() if value}
    rules["note"] = (
        "No AS400 a maior parte das regras de negócio vive nos programas RPG/CL/COBOL, "
        "não no banco. O que está aqui é só o que o banco garante."
    )
    return rules


ROUTINE_SOURCE_SQL = (
    "select routine_schema, routine_name, routine_type, routine_body, external_name, "
    "external_language, routine_text, routine_definition from qsys2.sysroutines "
    "where routine_schema in ({libs}) and routine_name = ? fetch first 10 rows only"
)
VIEW_SOURCE_SQL = (
    "select table_schema, table_name, view_definition from qsys2.sysviews "
    "where table_schema in ({libs}) and table_name = ? order by table_schema, table_name, seqno"
)
MQT_SOURCE_SQL = (
    "select table_schema, table_name, mqt_definition from qsys2.systables "
    "where table_schema in ({libs}) and table_name = ? and table_type = 'M'"
)
TRIGGER_SOURCE_SQL = (
    "select trigger_schema, trigger_name, event_object_table, trigger_program_library, "
    "trigger_program_name, action_statement from qsys2.systriggers "
    "where trigger_schema in ({libs}) and trigger_name = ?"
)


def get_definition(
    session: Db2iSession, target: DatabaseTarget, name: str, schema: str | None = None
) -> dict[str, Any]:
    if schema is None and "." in name:
        schema, name = name.split(".", 1)
    libraries = library_scope(session, schema)
    libs = _in(libraries)
    name = name.upper()
    params = [*libraries, name]
    found: list[dict[str, Any]] = []
    for row in session.records(ROUTINE_SOURCE_SQL.format(libs=libs), params):
        body = row["ROUTINE_DEFINITION"]
        found.append(
            {
                "name": f"{row['ROUTINE_SCHEMA']}.{row['ROUTINE_NAME']}",
                "kind": ROUTINE_KINDS.get(row["ROUTINE_TYPE"], row["ROUTINE_TYPE"]),
                "text": row["ROUTINE_TEXT"],
                "definition": _truncate(body),
                "external_program": None if body else row["EXTERNAL_NAME"],
                "language": row["EXTERNAL_LANGUAGE"],
            }
        )
    views: dict[str, list[str]] = {}
    for row in session.records(VIEW_SOURCE_SQL.format(libs=libs), params):
        views.setdefault(f"{row['TABLE_SCHEMA']}.{row['TABLE_NAME']}", []).append(row["VIEW_DEFINITION"] or "")
    found += [
        {"name": view, "kind": "view", "definition": _truncate("".join(parts))} for view, parts in views.items()
    ]
    found += [
        {"name": f"{row['TABLE_SCHEMA']}.{row['TABLE_NAME']}", "kind": "materialized query table",
         "definition": _truncate(row["MQT_DEFINITION"])}
        for row in session.records(MQT_SOURCE_SQL.format(libs=libs), params)
    ]
    found += [
        {
            "name": f"{row['TRIGGER_SCHEMA']}.{row['TRIGGER_NAME']}",
            "kind": "trigger",
            "table": row["EVENT_OBJECT_TABLE"],
            "definition": _truncate(row["ACTION_STATEMENT"]),
            "external_program": (
                f"{row['TRIGGER_PROGRAM_LIBRARY']}/{row['TRIGGER_PROGRAM_NAME']}"
                if not row["ACTION_STATEMENT"]
                else None
            ),
        }
        for row in session.records(TRIGGER_SOURCE_SQL.format(libs=libs), params)
    ]
    if not found:
        raise DatabaseAccessError(
            f"Nenhuma função, procedure, view ou trigger chamada '{name}'. Use list_routines "
            "ou list_tables para ver os nomes exatos."
        )
    return {
        "definitions": [
            {key: value for key, value in item.items() if value is not None}
            for item in shape_records(found)
        ]
    }


# endregion


# region Data understanding
COLUMN_TEXTS_SQL = (
    "select column_name, column_text, column_heading from qsys2.syscolumns "
    "where table_schema = ? and table_name = ?"
)
COLUMN_STATS_SQL = (
    "select column_name, number_distinct_values, number_nulls, low2key, high2key, "
    "number_most_frequent_values, statistic_last_updated from qsys2.syscolumnstat "
    "where table_schema = ? and table_name = ? and number_column_names = 1 order by column_name"
)
SENSITIVE_TEXTS_SQL = (
    "select distinct column_name, column_text, column_heading from qsys2.syscolumns "
    "where table_schema in ({libs}) and column_name in ({names})"
)


def _described_as_sensitive(rows: list[dict[str, Any]]) -> frozenset[str]:
    return frozenset(
        row["COLUMN_NAME"]
        for row in rows
        if is_sensitive_column(row.get("COLUMN_TEXT")) or is_sensitive_column(row.get("COLUMN_HEADING"))
    )


def column_profile(
    session: Db2iSession, target: DatabaseTarget, table: str, schema: str | None = None
) -> dict[str, Any]:
    relation = resolve_table(session, schema, table)
    key = [relation["TABLE_SCHEMA"], relation["TABLE_NAME"]]
    sensitive = _described_as_sensitive(session.records(COLUMN_TEXTS_SQL, key))
    stats = session.one(STATS_SQL, key) or {}
    rows = stats.get("NUMBER_ROWS") or 0
    profile = []
    for row in session.records(COLUMN_STATS_SQL, key):
        column = row["COLUMN_NAME"]
        hidden = column in sensitive
        nulls = row["NUMBER_NULLS"]
        profile.append(
            {
                "column": column,
                "distinct": row["NUMBER_DISTINCT_VALUES"],
                "null_fraction": round(nulls / rows, 4) if rows and nulls is not None else None,
                "approx_min": "[mascarado]" if hidden else shape_cell(column, row["LOW2KEY"]),
                "approx_max": "[mascarado]" if hidden else shape_cell(column, row["HIGH2KEY"]),
                "most_frequent_values_tracked": row["NUMBER_MOST_FREQUENT_VALUES"],
                "statistics_updated": row["STATISTIC_LAST_UPDATED"],
            }
        )
    payload: dict[str, Any] = {
        "table": f"{key[0]}.{key[1]}",
        "rows": rows or None,
        "columns": shape_records(profile),
    }
    if not profile:
        payload["note"] = (
            "Sem estatísticas de coluna para esta tabela: o otimizador do IBM i só as cria "
            "conforme as consultas usam as colunas. Use sample_rows ou uma consulta agregada."
        )
    return payload


def sample_rows(
    session: Db2iSession,
    target: DatabaseTarget,
    table: str,
    schema: str | None = None,
    limit: int = DEFAULT_SAMPLE_ROWS,
) -> dict[str, Any]:
    relation = resolve_table(session, schema, table)
    key = [relation["TABLE_SCHEMA"], relation["TABLE_NAME"]]
    bounded = max(1, min(int(limit), MAX_SAMPLE_ROWS))
    sensitive = _described_as_sensitive(session.records(COLUMN_TEXTS_SQL, key))
    quoted = ".".join('"' + part.replace('"', '""') + '"' for part in key)
    result = session.fetch(f"select * from {quoted} fetch first {bounded} rows only", max_rows=bounded)
    result.truncated = False
    return {"table": f"{key[0]}.{key[1]}", **shape_rows(result, sensitive)}


# endregion


# region Engine
USER_LIBRARIES_SQL = "select schema_name from qsys2.sysschemas where schema_name in ({libs})"
KNOWN_SCHEMAS_SQL = "select schema_name from qsys2.sysschemas where schema_name in ({names})"


class Db2iEngine(Engine):
    name = "db2i"
    dialect = DIALECT

    def open_session(self, target: DatabaseTarget, check_posture: bool = True):
        return readonly_session(target, check_posture=check_posture)

    def check_connection(self, target: DatabaseTarget) -> dict[str, Any]:
        try:
            with readonly_session(target, check_posture=False) as session:
                posture = read_posture(session)
                libraries = self._report_libraries(session, target)
        except DatabaseAccessError as exc:
            return {"ok": False, "error": str(exc)}
        warnings = posture.warnings()
        configured = [name.upper() for name in target.schemas]
        missing = [name for name in configured if name not in libraries]
        if missing:
            warnings.append(f"Bibliotecas liberadas que não existem: {', '.join(missing)}.")
        if not configured:
            warnings.append(
                "Nenhuma biblioteca liberada: o agente usará a lista de bibliotecas do perfil."
            )
        return {
            "ok": not posture.refused,
            "server_version": posture.server_version,
            "role": posture.role,
            "read_only_role": not posture.all_authorities,
            "schemas": libraries,
            "warnings": warnings,
            "error": (
                f"O perfil tem {', '.join(posture.refused)} e será recusado pelos agentes. "
                "Use um perfil sem autoridades especiais, só com *USE nas bibliotecas de dados."
                if posture.refused
                else None
            ),
        }

    @staticmethod
    def _report_libraries(session: Db2iSession, target: DatabaseTarget) -> list[str]:
        configured = [name.upper() for name in target.schemas]
        if configured:
            rows = session.records(USER_LIBRARIES_SQL.format(libs=_in(configured)), configured)
            return [row["SCHEMA_NAME"] for row in rows]
        return [row["SCHEMA_NAME"] for row in session.records(LIBRARY_LIST_SQL)]

    def known_schemas(self, session: Db2iSession, candidates: set[str]) -> set[str]:
        names = sorted(candidates)[:200]
        if not names:
            return set()
        rows = session.records(KNOWN_SCHEMAS_SQL.format(names=_in(names)), names)
        return {row["SCHEMA_NAME"] for row in rows}

    def sensitive_columns(
        self, session: Db2iSession, target: DatabaseTarget, columns: list[str]
    ) -> frozenset[str]:
        names = sorted({column.upper() for column in columns})[:200]
        if not names:
            return frozenset()
        try:
            libraries = library_scope(session)
        except DatabaseAccessError:
            return frozenset()
        rows = session.records(
            SENSITIVE_TEXTS_SQL.format(libs=_in(libraries), names=_in(names)),
            [*libraries, *names],
            max_rows=5000,
        )
        return _described_as_sensitive(rows)

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


ENGINE = Db2iEngine()


# endregion

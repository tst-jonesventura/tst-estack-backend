"""The fixed set of operations an agent may run against a database, by name."""
from dataclasses import dataclass
from typing import Any

from database_access.engines.base import Engine, Session, get_engine
from database_access.guard import (
    MAX_QUERY_ROWS,
    DatabaseAccessError,
    DatabaseTarget,
    prepare_readonly_query,
    schema_qualifiers,
    shape_rows,
)
from utils import log
from utils.ttl_cache import TTLCache

logger = log.setup_custom_logger(__name__)

CATALOG_TTL_SECONDS = 10 * 60
_catalog_cache: TTLCache[dict[str, Any]] = TTLCache(CATALOG_TTL_SECONDS)

UNEXPECTED_ERROR = (
    "Falha inesperada ao consultar o banco '{alias}'. Siga com o que já reuniu e "
    "informe que esta parte não pôde ser verificada."
)


@dataclass(frozen=True)
class OperationSpec:
    name: str
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    cacheable: bool = False

    def validate(self, args: dict[str, Any]) -> None:
        unknown = set(args) - set(self.required) - set(self.optional)
        missing = [name for name in self.required if args.get(name) in (None, "")]
        if unknown:
            raise DatabaseAccessError(
                f"Argumentos não aceitos por '{self.name}': {', '.join(sorted(unknown))}."
            )
        if missing:
            raise DatabaseAccessError(
                f"Argumentos obrigatórios ausentes em '{self.name}': {', '.join(missing)}."
            )


OPERATIONS: dict[str, OperationSpec] = {
    spec.name: spec
    for spec in (
        OperationSpec("database_overview", cacheable=True),
        OperationSpec("list_tables", optional=("schema", "name_filter"), cacheable=True),
        OperationSpec("list_routines", optional=("schema", "name_filter"), cacheable=True),
        OperationSpec("describe_table", required=("table",), optional=("schema",), cacheable=True),
        OperationSpec("list_relationships", optional=("schema",), cacheable=True),
        OperationSpec("suggest_relationships", optional=("schema",), cacheable=True),
        OperationSpec("list_rules", optional=("table",), cacheable=True),
        OperationSpec("get_definition", required=("name",), optional=("schema",), cacheable=True),
        OperationSpec("column_profile", required=("table",), optional=("schema",)),
        OperationSpec("sample_rows", required=("table",), optional=("schema", "limit")),
        OperationSpec("run_readonly_query", required=("sql",), optional=("limit",)),
    )
}


def _readonly_query(
    engine: Engine, session: Session, target: DatabaseTarget, sql: str, limit: int = 50
) -> dict[str, Any]:
    candidates = schema_qualifiers(sql, engine.dialect)
    known = engine.known_schemas(session, candidates) if target.schemas and candidates else set()
    wrapped = prepare_readonly_query(
        sql,
        limit,
        dialect=engine.dialect,
        allowed_schemas=target.schemas,
        known_schemas=known,
    )
    bounded = max(1, min(int(limit), MAX_QUERY_ROWS))
    result = engine.run_query(session, wrapped, bounded)
    sensitive = engine.sensitive_columns(session, target, result.columns)
    return shape_rows(result, sensitive)


CHECK_CONNECTION = "check_connection"


def validate_operation(name: str, args: dict[str, Any]) -> OperationSpec:
    spec = OPERATIONS.get(name)
    if spec is None:
        raise DatabaseAccessError(f"Operação '{name}' não existe.")
    spec.validate(args)
    return spec


def run_operation(target: DatabaseTarget, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run one allowlisted operation, raising ``DatabaseAccessError`` on refusal."""
    validate_operation(name, args)
    engine = get_engine(target.engine)
    if name == "run_readonly_query":
        prepare_readonly_query(args["sql"], args.get("limit", 50), dialect=engine.dialect)
    with engine.open_session(target) as session:
        if name == "run_readonly_query":
            return _readonly_query(engine, session, target, **args)
        return engine.catalog()[name](session, target, **args)


def run_connector_job(target: DatabaseTarget, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """What a connector runs: an agent operation, or the setup screen's connection check."""
    if name == CHECK_CONNECTION:
        return get_engine(target.engine).check_connection(target)
    return run_operation(target, name, args)


def pick_target(targets: tuple[DatabaseTarget, ...], database: str | None) -> DatabaseTarget:
    if database:
        for target in targets:
            if target.alias.lower() == database.lower():
                return target
        by_name = [target for target in targets if target.database.lower() == database.lower()]
        if len(by_name) == 1:
            return by_name[0]
        raise DatabaseAccessError(
            f"Banco '{database}' não existe neste projeto. Disponíveis: "
            f"{', '.join(target.alias for target in targets)}."
        )
    if len(targets) == 1:
        return targets[0]
    raise DatabaseAccessError(
        "Este projeto tem mais de um banco; informe 'database' com um destes "
        f"aliases: {', '.join(target.alias for target in targets)}."
    )


class DirectExecutor:
    """Runs operations in this process, returning errors as data."""

    def __init__(self, targets: tuple[DatabaseTarget, ...]):
        self.targets = targets

    def _execute(self, target: DatabaseTarget, name: str, args: dict[str, Any]) -> dict[str, Any]:
        return run_operation(target, name, args)

    def run(self, database: str | None, name: str, args: dict[str, Any]) -> dict[str, Any]:
        args = {key: value for key, value in args.items() if value is not None}
        try:
            target = pick_target(self.targets, database)
        except DatabaseAccessError as exc:
            return {"error": str(exc)}

        spec = OPERATIONS.get(name)
        cache_key = (
            (target.cache_key, name, tuple(sorted(args.items())))
            if spec is not None and spec.cacheable
            else None
        )
        if cache_key is not None:
            cached = _catalog_cache.get(cache_key)
            if cached is not None:
                return cached

        try:
            result = self._execute(target, name, args)
        except DatabaseAccessError as exc:
            return {"error": str(exc)}
        except Exception:
            logger.exception("Unexpected failure querying database alias '%s'", target.alias)
            return {"error": UNEXPECTED_ERROR.format(alias=target.alias)}

        if len(self.targets) > 1:
            result = {"database": target.alias, **result}
        if cache_key is not None:
            _catalog_cache.put(cache_key, result)
        return result

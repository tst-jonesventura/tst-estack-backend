"""Connector configuration; passwords come from environment variables, never the file."""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from database_access.guard import SCHEMA_NAME_PATTERN, DatabaseTarget

SUPPORTED_ENGINES = ("postgres", "db2i", "oracle", "sqlserver", "db2")
DEFAULT_PORTS = {"postgres": 5432, "db2i": 8076, "oracle": 1521, "sqlserver": 1433, "db2": 50000}


class ConfigError(ValueError):
    """The configuration cannot be used; the message says what to fix."""


@dataclass(frozen=True)
class ConnectorConfig:
    estack_url: str
    token: str
    targets: tuple[DatabaseTarget, ...]
    max_parallel_jobs: int = 4
    heartbeat_seconds: int = 30

    def target(self, alias: str) -> DatabaseTarget | None:
        for target in self.targets:
            if target.alias.lower() == alias.lower():
                return target
        return None


def _required(entry: dict[str, Any], key: str, alias: str) -> Any:
    value = entry.get(key)
    if value in (None, ""):
        raise ConfigError(f"Banco '{alias}': o campo '{key}' é obrigatório.")
    return value


def _schemas(entry: dict[str, Any], alias: str) -> tuple[str, ...]:
    schemas = tuple(str(name).strip() for name in entry.get("schemas") or ())
    for name in schemas:
        if not SCHEMA_NAME_PATTERN.match(name):
            raise ConfigError(f"Banco '{alias}': nome de schema inválido '{name}'.")
    return schemas


def _password(entry: dict[str, Any], alias: str, environ: dict[str, str]) -> str:
    variable = _required(entry, "password_env", alias)
    password = environ.get(variable)
    if not password:
        raise ConfigError(
            f"Banco '{alias}': a variável de ambiente '{variable}' com a senha não está definida."
        )
    return password


def _options(entry: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    options = entry.get("options") or {}
    if not isinstance(options, dict):
        raise ConfigError("O campo 'options' de um banco deve ser um mapa chave: valor.")
    return tuple(sorted((str(key), str(value)) for key, value in options.items()))


def _target(entry: dict[str, Any], environ: dict[str, str]) -> DatabaseTarget:
    alias = str(_required(entry, "alias", "?"))
    engine = str(_required(entry, "engine", alias))
    if engine not in SUPPORTED_ENGINES:
        raise ConfigError(
            f"Banco '{alias}': motor '{engine}' não suportado ({', '.join(SUPPORTED_ENGINES)})."
        )
    return DatabaseTarget(
        alias=alias,
        host=str(_required(entry, "host", alias)),
        port=int(entry.get("port") or DEFAULT_PORTS[engine]),
        database=str(entry.get("database") or ""),
        username=str(_required(entry, "username", alias)),
        password=_password(entry, alias, environ),
        ssl_mode=str(entry.get("ssl_mode") or "require"),
        schemas=_schemas(entry, alias),
        engine=engine,
        options=_options(entry),
    )


def _from_environment(environ: dict[str, str]) -> dict[str, Any]:
    """A single database described by DB_* variables, for hosts without a config file."""
    missing = [name for name in ("DB_HOST", "DB_USER", "DB_PASSWORD") if not environ.get(name)]
    if missing:
        raise ConfigError(f"Defina as variáveis {', '.join(missing)} para descrever o banco.")
    entry: dict[str, Any] = {
        "alias": environ.get("DB_ALIAS") or "erp",
        "engine": environ.get("DB_ENGINE") or "db2i",
        "host": environ.get("DB_HOST"),
        "port": environ.get("DB_PORT"),
        "database": environ.get("DB_NAME"),
        "username": environ.get("DB_USER"),
        "password_env": "DB_PASSWORD",
        "ssl_mode": environ.get("DB_SSL_MODE"),
        "schemas": [name for name in (environ.get("DB_SCHEMAS") or "").split(",") if name.strip()],
    }
    return {"databases": [entry]}


def load_config(path: str | Path, environ: dict[str, str] | None = None) -> ConnectorConfig:
    environ = dict(os.environ if environ is None else environ)
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        if not environ.get("DB_HOST"):
            raise ConfigError(
                f"Arquivo de configuração não encontrado: {path}. Monte o databases.yaml "
                "ou descreva o banco pelas variáveis DB_HOST, DB_USER e DB_PASSWORD."
            ) from exc
        raw = _from_environment(environ)

    estack_url = environ.get("ESTACK_URL") or raw.get("estack_url")
    token = environ.get("ESTACK_CONNECTOR_TOKEN")
    if not estack_url:
        raise ConfigError("Defina ESTACK_URL (endereço do backend do e-Stack).")
    if not token:
        raise ConfigError("Defina ESTACK_CONNECTOR_TOKEN com o token gerado no e-Stack.")

    entries = raw.get("databases") or []
    if not entries:
        raise ConfigError("Nenhum banco configurado em 'databases'.")
    targets = tuple(_target(entry, environ) for entry in entries)
    aliases = [target.alias.lower() for target in targets]
    if len(set(aliases)) != len(aliases):
        raise ConfigError("Há aliases de banco repetidos na configuração.")

    return ConnectorConfig(
        estack_url=estack_url.rstrip("/") + "/",
        token=token,
        targets=targets,
        max_parallel_jobs=int(environ.get("MAX_PARALLEL_JOBS") or raw.get("max_parallel_jobs") or 4),
        heartbeat_seconds=int(raw.get("heartbeat_seconds") or 30),
    )

"""The contract every database engine implements, and the lazy engine registry."""
import importlib
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Any, Protocol

from database_access.guard import (
    DatabaseAccessError,
    DatabaseTarget,
    Dialect,
    QueryResult,
)

ENGINE_MODULES = {
    "postgres": "database_access.engines.postgres",
    "db2i": "database_access.engines.db2i",
    "oracle": "database_access.engines.oracle",
    "sqlserver": "database_access.engines.sqlserver",
}

CatalogOperation = Callable[..., dict[str, Any]]


class Session(Protocol):
    def fetch(self, query: Any, params: Any = None, max_rows: int = ...) -> QueryResult: ...

    def records(self, query: Any, params: Any = None, max_rows: int = ...) -> list[dict[str, Any]]: ...

    def one(self, query: Any, params: Any = None) -> dict[str, Any] | None: ...


class Engine(ABC):
    """One database engine: how to connect safely and how to read its catalog."""

    name: str
    dialect: Dialect

    @abstractmethod
    def open_session(
        self, target: DatabaseTarget, check_posture: bool = True
    ) -> AbstractContextManager[Session]:
        """A read-only session, refused when the connected role is privileged."""

    @abstractmethod
    def check_connection(self, target: DatabaseTarget) -> dict[str, Any]:
        """Connectivity and role posture, reported as data for the setup screen."""

    @abstractmethod
    def known_schemas(self, session: Session, candidates: set[str]) -> set[str]:
        """Which of ``candidates`` are real schemas, to keep free queries inside the allowlist."""

    def run_query(self, session: Session, query: str, max_rows: int) -> QueryResult:
        """Run an already wrapped free query."""
        return session.fetch(query, max_rows=max_rows)

    def sensitive_columns(
        self, session: Session, target: DatabaseTarget, columns: list[str]
    ) -> frozenset[str]:
        """Result columns to mask beyond what their names reveal."""
        return frozenset()

    @abstractmethod
    def catalog(self) -> dict[str, CatalogOperation]:
        """Catalog operations by name, each called as ``fn(session, target, **args)``."""


def get_engine(name: str) -> Engine:
    module_name = ENGINE_MODULES.get(name)
    if module_name is None:
        raise DatabaseAccessError(f"Motor de banco '{name}' não é suportado.")
    return importlib.import_module(module_name).ENGINE


def supported_engines() -> Iterator[str]:
    return iter(ENGINE_MODULES)

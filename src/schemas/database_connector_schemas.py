from datetime import datetime
from typing import Any

from pydantic import Field

from schemas.base import Base


class ReportedDatabase(Base):
    alias: str = Field(..., min_length=1, max_length=100)
    engine: str = Field(..., min_length=1, max_length=20)
    database: str | None = Field(default=None, max_length=255)
    schemas: list[str] = Field(default_factory=list, max_length=500)
    ok: bool | None = None
    server_version: str | None = Field(default=None, max_length=200)
    role: str | None = Field(default=None, max_length=200)
    warnings: list[str] = Field(default_factory=list, max_length=20)
    error: str | None = Field(default=None, max_length=2000)


class DatabaseConnectorCreate(Base):
    name: str = Field(..., min_length=1, max_length=100)


class DatabaseConnectorPublic(Base):
    id: str
    organization_id: str
    name: str
    token_hint: str
    version: str | None = None
    last_seen_at: datetime | None = None
    online: bool = False
    databases: list[ReportedDatabase] = Field(default_factory=list)
    is_active: bool
    created_at: datetime | None = None


class DatabaseConnectorWithToken(DatabaseConnectorPublic):
    token: str = Field(..., description="Exibido uma única vez; guarde-o no conector.")


class OrganizationConnectors(Base):
    organization_id: str
    organization_name: str
    connectors: list[DatabaseConnectorPublic] = Field(default_factory=list)


class ConnectorHeartbeat(Base):
    version: str | None = Field(default=None, max_length=40)
    databases: list[ReportedDatabase] = Field(default_factory=list, max_length=50)


class ConnectorJob(Base):
    id: str
    database_alias: str
    operation: str
    arguments: dict[str, Any]


class ConnectorJobResult(Base):
    result: dict[str, Any] | None = None
    error: str | None = Field(default=None, max_length=2000)

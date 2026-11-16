from datetime import datetime
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator

from database_access.guard import SCHEMA_NAME_PATTERN
from schemas.base import Base

SslMode = Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]
Engine = Literal["postgres", "db2i", "oracle", "sqlserver", "db2"]
AccessMode = Literal["direct", "connector"]

DIRECT_ENGINES = ("postgres", "db2i", "oracle", "sqlserver")
DEFAULT_PORTS = {"postgres": 5432, "db2i": 8076, "oracle": 1521, "sqlserver": 1433, "db2": 50000}
NEEDS_DATABASE_NAME = ("postgres", "oracle", "sqlserver")

SCHEMAS_DESC = "Schemas que os agentes podem ler. Vazio libera todos os schemas de usuário."
CONNECTOR_ONLY = (
    "Este motor só é acessado por um conector na rede do cliente; escolha o modo 'connector'."
)


def _validate_schemas(schemas: list[str] | None) -> list[str] | None:
    if schemas is None:
        return None
    cleaned = []
    for name in schemas:
        name = name.strip()
        if not SCHEMA_NAME_PATTERN.match(name):
            raise ValueError(f"Nome de schema inválido: '{name}'.")
        if name not in cleaned:
            cleaned.append(name)
    return cleaned


class ProjectDatabaseBase(Base):
    alias: str = Field(..., min_length=1, max_length=100, description="Nome curto do banco no projeto")
    engine: Engine = "postgres"
    access_mode: AccessMode = "direct"
    connector_id: str | None = Field(default=None, description="Conector, no modo 'connector'")
    connector_alias: str | None = Field(
        default=None, max_length=100, description="Alias do banco na configuração do conector"
    )
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535, description="Vazio usa a porta padrão do motor")
    database_name: str | None = Field(default=None, min_length=1, max_length=255)
    username: str | None = Field(
        default=None, min_length=1, max_length=255, description="Usuário somente leitura"
    )
    ssl_mode: SslMode = "require"
    schemas: list[str] = Field(default_factory=list, description=SCHEMAS_DESC)
    description: str | None = Field(default=None, max_length=1000)
    is_active: bool = True

    _check_schemas = field_validator("schemas")(_validate_schemas)


class ProjectDatabaseCreate(ProjectDatabaseBase):
    password: SecretStr | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _check_access_mode(self):
        if self.access_mode == "connector":
            if not self.connector_id or not self.connector_alias:
                raise ValueError("No modo 'connector', informe o conector e o alias do banco.")
            return self
        if self.engine not in DIRECT_ENGINES:
            raise ValueError(CONNECTOR_ONLY)
        required = ["host", "username", "password"]
        if self.engine in NEEDS_DATABASE_NAME:
            required.insert(1, "database_name")
        missing = [name for name in required if not getattr(self, name)]
        if missing:
            raise ValueError(f"Campos obrigatórios no modo direto: {', '.join(missing)}.")
        self.port = self.port or DEFAULT_PORTS[self.engine]
        self.database_name = self.database_name or self.host
        return self


class ProjectDatabaseUpdate(Base):
    alias: str | None = Field(default=None, min_length=1, max_length=100)
    connector_alias: str | None = Field(default=None, min_length=1, max_length=100)
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    database_name: str | None = Field(default=None, min_length=1, max_length=255)
    username: str | None = Field(default=None, min_length=1, max_length=255)
    password: SecretStr | None = Field(default=None, description="Omitir mantém a senha atual")
    ssl_mode: SslMode | None = None
    schemas: list[str] | None = Field(default=None, description=SCHEMAS_DESC)
    description: str | None = Field(default=None, max_length=1000)
    is_active: bool | None = None

    _check_schemas = field_validator("schemas")(_validate_schemas)


class ProjectDatabasePublic(ProjectDatabaseBase):
    id: str
    project_id: str
    database_name: str
    created_at: datetime | None = None


class ProjectDatabaseConnectionCheck(Base):
    ok: bool
    server_version: str | None = None
    role: str | None = None
    read_only_role: bool | None = None
    schemas: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None

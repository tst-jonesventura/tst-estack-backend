from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from database.models import (
    DatabaseAccessMode,
    DatabaseConnector,
    DatabaseEngine,
    Project,
    ProjectDatabase,
)
from database_access.engines.base import get_engine
from database_access.guard import DatabaseAccessError, DatabaseTarget
from database_access.network import (
    ALLOWED_SSL_MODES,
    NETWORK_POLICY_OPTION,
    PUBLIC_NETWORK_ONLY,
    SSL_MODE_REFUSED,
)
from database_access.operations import CHECK_CONNECTION
from schemas.project_database_schemas import (
    ProjectDatabaseConnectionCheck,
    ProjectDatabaseCreate,
    ProjectDatabaseUpdate,
)
from services.database_connector_service import reported_database, run_via_connector
from utils import log
from utils.config import settings

logger = log.setup_custom_logger(__name__)


CONNECTION_FIELDS = ("host", "port", "database_name", "username", "ssl_mode")
DB2I_DIRECT_SSL_MODE = (
    "No Db2 for i em modo direto use o modo SSL 'require': a conexão é cifrada com o "
    "certificado do Mapepire. Verificação por CA própria só está disponível via conector."
)
PASSWORD_REQUIRED_FOR_CONNECTION_CHANGE = (
    "Informe a senha ao alterar host, porta, banco, usuário ou modo SSL."
)


def _validate_ssl_mode(ssl_mode: str, engine: str) -> None:
    if engine == DatabaseEngine.DB2I.value and ssl_mode != "require":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, DB2I_DIRECT_SSL_MODE)
    if (
        not settings.project_databases_allow_private_network
        and ssl_mode not in ALLOWED_SSL_MODES
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            SSL_MODE_REFUSED.format(mode=ssl_mode),
        )


def _require_password_for_connection_change(
    record: ProjectDatabase, changes: dict[str, object]
) -> None:
    connection_changed = any(
        field in changes
        and changes[field] is not None
        and changes[field] != getattr(record, field)
        for field in CONNECTION_FIELDS
    )
    if changes.get("password") is None and connection_changed:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            PASSWORD_REQUIRED_FOR_CONNECTION_CHANGE,
        )


def _target_options(connector_id: str | None, alias: str | None) -> tuple[tuple[str, str], ...]:
    if connector_id:
        options = {"connector_alias": alias or "", "connector_id": connector_id}
    elif not settings.project_databases_allow_private_network:
        options = {NETWORK_POLICY_OPTION: PUBLIC_NETWORK_ONLY}
    else:
        options = {}
    return tuple(sorted(options.items()))


def target_from_record(record: ProjectDatabase) -> DatabaseTarget:
    return DatabaseTarget(
        alias=record.alias,
        host=record.host or "",
        port=record.port or 0,
        database=record.database_name,
        username=record.username or "",
        password=record.password or "",
        ssl_mode=record.ssl_mode,
        schemas=record.schemas,
        engine=record.engine,
        options=_target_options(
            record.connector_id if record.access_mode == DatabaseAccessMode.CONNECTOR.value else None,
            record.connector_alias,
        ),
    )


def target_from_payload(payload: ProjectDatabaseCreate) -> DatabaseTarget:
    connector = payload.access_mode == DatabaseAccessMode.CONNECTOR.value
    return DatabaseTarget(
        alias=payload.alias,
        host=payload.host or "",
        port=payload.port or 0,
        database=payload.database_name or payload.connector_alias or "",
        username=payload.username or "",
        password=payload.password.get_secret_value() if payload.password else "",
        ssl_mode=payload.ssl_mode,
        schemas=tuple(payload.schemas),
        engine=payload.engine,
        options=_target_options(
            payload.connector_id if connector else None, payload.connector_alias
        ),
    )


def check_connection(target: DatabaseTarget) -> ProjectDatabaseConnectionCheck:
    """Connect, identify the role and list schemas, reporting problems as data."""
    connector_id = target.option("connector_id")
    try:
        if connector_id:
            result = run_via_connector(
                connector_id, target.option("connector_alias") or target.alias, CHECK_CONNECTION, {}
            )
        else:
            result = get_engine(target.engine).check_connection(target)
    except DatabaseAccessError as exc:
        return ProjectDatabaseConnectionCheck(ok=False, error=str(exc))
    return ProjectDatabaseConnectionCheck(**result)


def _apply_changes(record: ProjectDatabase, changes: dict[str, object]) -> None:
    if "password" in changes:
        password = changes.pop("password")
        if password is not None:
            record.password = password.get_secret_value()
    if "schemas" in changes:
        record.schema_allowlist = ",".join(changes.pop("schemas") or []) or None
    for field, value in changes.items():
        if value is not None or field == "description":
            setattr(record, field, value)


def active_project_databases(session: Session, project_id: str) -> list[ProjectDatabase]:
    return (
        session.query(ProjectDatabase)
        .filter(
            ProjectDatabase.project_id == project_id,
            ProjectDatabase.is_active == True,
        )
        .order_by(ProjectDatabase.alias)
        .all()
    )


class ProjectDatabaseService:
    def __init__(self, session: Session):
        self.session = session

    def list_for_project(self, project_id: str) -> list[ProjectDatabase]:
        return (
            self.session.query(ProjectDatabase)
            .filter(ProjectDatabase.project_id == project_id)
            .order_by(ProjectDatabase.alias)
            .all()
        )

    def get(self, project_id: str, database_id: str) -> ProjectDatabase:
        record = (
            self.session.query(ProjectDatabase)
            .filter(
                ProjectDatabase.id == database_id,
                ProjectDatabase.project_id == project_id,
            )
            .one_or_none()
        )
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Banco de dados não encontrado.")
        return record

    def _ensure_alias_free(self, project_id: str, alias: str, keep_id: str | None = None) -> None:
        query = self.session.query(ProjectDatabase.id).filter(
            ProjectDatabase.project_id == project_id, ProjectDatabase.alias == alias
        )
        if keep_id is not None:
            query = query.filter(ProjectDatabase.id != keep_id)
        if query.first() is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"Já existe um banco com o alias '{alias}' neste projeto."
            )

    @staticmethod
    def _require_healthy(target: DatabaseTarget) -> None:
        check = check_connection(target)
        if not check.ok:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, check.error)

    def _connector_for(self, project_id: str, connector_id: str) -> DatabaseConnector:
        project = self.session.get(Project, project_id)
        connector = self.session.get(DatabaseConnector, connector_id)
        if (
            project is None
            or connector is None
            or not connector.is_active
            or connector.organization_id != project.organization_id
        ):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "Conector não encontrado para a empresa deste projeto."
            )
        return connector

    def _apply_connector_report(self, project_id: str, payload: ProjectDatabaseCreate) -> None:
        """Take engine and database name from what the connector reported, not from the form."""
        connector = self._connector_for(project_id, payload.connector_id)
        reported = reported_database(connector, payload.connector_alias)
        if reported is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"O conector '{connector.name}' não reportou um banco com o alias "
                f"'{payload.connector_alias}'. Confira a configuração dele.",
            )
        payload.engine = reported.get("engine", payload.engine)
        payload.database_name = reported.get("database") or payload.connector_alias

    def create(
        self, project_id: str, payload: ProjectDatabaseCreate, test_connection: bool
    ) -> ProjectDatabase:
        self._ensure_alias_free(project_id, payload.alias)
        connector_mode = payload.access_mode == DatabaseAccessMode.CONNECTOR.value
        if connector_mode:
            self._apply_connector_report(project_id, payload)
        else:
            _validate_ssl_mode(payload.ssl_mode, payload.engine)
        if test_connection:
            self._require_healthy(target_from_payload(payload))
        record = ProjectDatabase(
            project_id=project_id,
            alias=payload.alias,
            engine=payload.engine,
            access_mode=payload.access_mode,
            connector_id=payload.connector_id if connector_mode else None,
            connector_alias=payload.connector_alias if connector_mode else None,
            host=None if connector_mode else payload.host,
            port=None if connector_mode else payload.port,
            database_name=payload.database_name,
            username=None if connector_mode else payload.username,
            password=None if connector_mode else payload.password.get_secret_value(),
            ssl_mode=payload.ssl_mode,
            schema_allowlist=",".join(payload.schemas) or None,
            description=payload.description,
            is_active=payload.is_active,
        )
        self.session.add(record)
        self.session.commit()
        self.session.refresh(record)
        logger.info("Registered database '%s' for project %s", record.alias, project_id)
        return record

    def update(
        self,
        project_id: str,
        database_id: str,
        payload: ProjectDatabaseUpdate,
        test_connection: bool,
    ) -> ProjectDatabase:
        record = self.get(project_id, database_id)
        changes = payload.model_dump(exclude_unset=True)
        if record.access_mode != DatabaseAccessMode.CONNECTOR.value:
            _require_password_for_connection_change(record, changes)
            _validate_ssl_mode(changes.get("ssl_mode") or record.ssl_mode, record.engine)
        if changes.get("alias"):
            self._ensure_alias_free(project_id, changes["alias"], keep_id=record.id)
        _apply_changes(record, changes)
        if test_connection:
            try:
                self._require_healthy(target_from_record(record))
            except HTTPException:
                self.session.rollback()
                raise
        self.session.commit()
        self.session.refresh(record)
        return record

    def delete(self, project_id: str, database_id: str) -> None:
        record = self.get(project_id, database_id)
        self.session.delete(record)
        self.session.commit()

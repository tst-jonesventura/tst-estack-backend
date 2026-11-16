import uuid
from typing import Any, ClassVar

import httpx
from fastapi import HTTPException, status
from sqlalchemy.orm import Session, selectinload

from database import models
from integrations.base import IntegrationProvider
from integrations.jira_provider import JiraIntegrationProvider
from schemas.integration_schemas import (
    JiraGetColumnsPayload,
    JiraIntegrationColumnsMappingCreate,
    JiraIntegrationConfigCreate,
    JiraIntegrationConfigUpdate,
    JiraTestConnectionPayload,
)
from utils import log

logger = log.setup_custom_logger(__name__)


class IntegrationService:
    SUPPORTED_PROVIDERS: ClassVar[set[str]] = {"jira"}
    FUTURE_PROVIDERS: ClassVar[set[str]] = {"azure", "trello"}

    def __init__(self, session: Session):
        self.session = session

    def _validate_provider(self, provider: str) -> str:
        prov = provider.lower().strip()
        if prov in self.FUTURE_PROVIDERS:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail=f"Provider '{provider}' is not yet implemented",
            )
        if prov not in self.SUPPORTED_PROVIDERS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Provider '{provider}' is not supported",
            )
        return prov

    def get_provider_instance(self, provider: str) -> IntegrationProvider:
        prov = self._validate_provider(provider)
        if prov == "jira":
            return JiraIntegrationProvider(self.session)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"Provider '{provider}' is not yet implemented",
        )

    def list_configs(self, provider: str, project_id: str | None = None) -> list[Any]:
        prov = self._validate_provider(provider)
        if prov == "jira":
            query = self.session.query(models.JiraIntegrationConfig).options(
                selectinload(models.JiraIntegrationConfig.column_mappings)
            )
            if project_id:
                query = query.filter(models.JiraIntegrationConfig.project_id == project_id)
            return query.all()
        return []

    def get_config_by_id(self, provider: str, config_id: str) -> Any:
        prov = self._validate_provider(provider)
        if prov == "jira":
            config = (
                self.session.query(models.JiraIntegrationConfig)
                .options(selectinload(models.JiraIntegrationConfig.column_mappings))
                .filter(models.JiraIntegrationConfig.id == config_id)
                .first()
            )
            if not config:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Jira integration config '{config_id}' not found",
                )
            return config
        return None

    def get_config_by_project(self, provider: str, project_id: str) -> Any:
        prov = self._validate_provider(provider)
        if prov == "jira":
            config = (
                self.session.query(models.JiraIntegrationConfig)
                .options(selectinload(models.JiraIntegrationConfig.column_mappings))
                .filter(models.JiraIntegrationConfig.project_id == project_id)
                .first()
            )
            if not config:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Jira integration config for project '{project_id}' not found",
                )
            return config
        return None

    async def create_config(self, provider: str, payload: Any, test_conn: bool = False) -> Any:
        prov = self._validate_provider(provider)
        if prov == "jira":
            data: JiraIntegrationConfigCreate = payload
            project = self.session.query(models.Project).filter(models.Project.id == data.project_id).first()
            if not project:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Project '{data.project_id}' not found",
                )

            existing = (
                self.session.query(models.JiraIntegrationConfig)
                .filter(models.JiraIntegrationConfig.project_id == data.project_id)
                .first()
            )
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Jira integration config already exists for project '{data.project_id}'",
                )

            config = models.JiraIntegrationConfig(
                id=str(uuid.uuid4()),
                project_id=data.project_id,
                jira_base_url=data.jira_base_url.rstrip("/"),
                jira_email=data.jira_email,
                jira_api_token=data.jira_api_token,
                jira_project_id=data.jira_project_id,
                jira_project_key=data.jira_project_key,
                default_issue_type=data.default_issue_type,
            )

            if test_conn:
                provider_inst = self.get_provider_instance(provider)
                try:
                    await provider_inst.test_connection(config)
                except Exception as exc:
                    logger.error(f"Failed to connect to Jira: {exc}")
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Failed to connect to Jira: {exc!s}",
                    ) from exc

            self.session.add(config)
            self.session.flush()

            if data.column_mappings:
                self._save_column_mappings(config.id, data.column_mappings)

            self.session.commit()
            self.session.refresh(config)
            return config
        return None

    async def update_config(self, provider: str, config_id: str, payload: Any, test_conn: bool = False) -> Any:
        prov = self._validate_provider(provider)
        if prov == "jira":
            config = self._fetch_config_for_update(config_id)

            data: JiraIntegrationConfigUpdate = payload
            update_data = data.model_dump(exclude_unset=True)
            column_mappings = update_data.pop("column_mappings", None)

            self._apply_config_updates(config, update_data)

            if test_conn:
                await self._test_connection_for_update(provider, config)

            if column_mappings is not None:
                self._update_column_mappings(config.id, data.column_mappings or [])

            self.session.commit()
            self.session.refresh(config)
            return config
        return None

    def _fetch_config_for_update(self, config_id: str) -> models.JiraIntegrationConfig:
        config = (
            self.session.query(models.JiraIntegrationConfig)
            .options(selectinload(models.JiraIntegrationConfig.column_mappings))
            .filter(models.JiraIntegrationConfig.id == config_id)
            .first()
        )
        if not config:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Jira integration config '{config_id}' not found",
            )
        return config

    def _apply_config_updates(self, config: models.JiraIntegrationConfig, update_data: dict[str, Any]) -> None:
        for key, value in update_data.items():
            if key == "jira_base_url" and isinstance(value, str):
                value = value.rstrip("/")
            setattr(config, key, value)

    async def _test_connection_for_update(self, provider: str, config: models.JiraIntegrationConfig) -> None:
        provider_inst = self.get_provider_instance(provider)
        try:
            await provider_inst.test_connection(config)
        except Exception as exc:
            logger.error(f"Failed to connect to Jira during update: {exc}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to connect to Jira: {exc!s}",
            ) from exc

    def _update_column_mappings(self, config_id: str,
                                column_mappings: list[JiraIntegrationColumnsMappingCreate]) -> None:
        self.session.query(models.JiraIntegrationColumnsMapping).filter(
            models.JiraIntegrationColumnsMapping.jira_integration_config_id == config_id
        ).delete()
        self.session.flush()
        self._save_column_mappings(config_id, column_mappings)

    def delete_config(self, provider: str, config_id: str) -> dict[str, Any]:
        prov = self._validate_provider(provider)
        if prov == "jira":
            config = (
                self.session.query(models.JiraIntegrationConfig)
                .filter(models.JiraIntegrationConfig.id == config_id)
                .first()
            )
            if not config:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Jira integration config '{config_id}' not found",
                )
            self.session.delete(config)
            self.session.commit()
            return {"detail": f"Integration config '{config_id}' deleted successfully"}
        return {}

    def _save_column_mappings(
        self, config_id: str, mappings: list[JiraIntegrationColumnsMappingCreate]
    ) -> None:
        for m in mappings:
            mapping = models.JiraIntegrationColumnsMapping(
                id=str(uuid.uuid4()),
                jira_integration_config_id=config_id,
                project_kanban_column_id=m.project_kanban_column_id,
                jira_status_id=m.jira_status_id,
                jira_status_name=m.jira_status_name,
            )
            self.session.add(mapping)

    async def test_connection_and_get_projects(
        self, provider: str, payload: JiraTestConnectionPayload
    ) -> list[dict[str, Any]]:
        base_url = (payload.jira_base_url or payload.basepath or "").rstrip("/")
        email = payload.jira_email or payload.email or ""
        token = payload.jira_api_token or payload.token or ""

        if not base_url or not email or not token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="basepath (or jira_base_url), email (or jira_email) and token (or jira_api_token) are required",
            )

        provider_inst = self.get_provider_instance(provider)
        try:
            return await provider_inst.get_projects(base_url, email, token)
        except HTTPException:
            raise
        except httpx.HTTPStatusError as exc:
            logger.error(f"Jira API error testing connection: {exc}")
            raise HTTPException(
                status_code=exc.response.status_code,
                detail=f"Jira error: {exc.response.text}",
            ) from exc
        except Exception as exc:
            logger.error(f"Error connecting to Jira: {exc}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to connect to Jira: {exc!s}",
            ) from exc

    async def get_board_columns(
            self, provider: str, payload: JiraGetColumnsPayload
    ) -> dict[str, Any]:
        base_url, email, token, project_key_or_id, board_id = self._extract_credentials_and_params(
            provider, payload
        )

        self._validate_board_columns_params(base_url, email, token, board_id, project_key_or_id)

        provider_inst = self.get_provider_instance(provider)
        return await self._fetch_board_columns_with_error_handling(
            provider_inst, base_url, email, token, board_id, project_key_or_id
        )

    def _extract_credentials_and_params(
            self, provider: str, payload: JiraGetColumnsPayload
    ) -> tuple[str, str, str, str | None, str | None]:
        base_url = (payload.jira_base_url or payload.basepath or "").rstrip("/")
        email = payload.jira_email or payload.email or ""
        token = payload.jira_api_token or payload.token or ""
        project_key_or_id = (
                payload.jira_project_key
                or payload.project_key
                or payload.jira_project_id
                or payload.project_id
        )
        board_id = payload.board_id

        if not (base_url and email and token) and payload.project_id:
            base_url, email, token, project_key_or_id = self._load_credentials_from_db(
                provider, payload.project_id, project_key_or_id
            )

        return base_url, email, token, project_key_or_id, board_id

    def _load_credentials_from_db(
            self, provider: str, project_id: str, project_key_or_id: str | None
    ) -> tuple[str, str, str, str | None]:
        try:
            db_config = self.get_config_by_project(provider, project_id)
            base_url = db_config.jira_base_url
            email = db_config.jira_email
            token = db_config.jira_api_token
            if not project_key_or_id:
                project_key_or_id = (
                        db_config.jira_project_key or db_config.jira_project_id
                )
            return base_url, email, token, project_key_or_id
        except HTTPException as exc:
            logger.debug(f"Config for project '{project_id}' not found in DB: {exc}")
            return "", "", "", project_key_or_id

    def _validate_board_columns_params(
            self, base_url: str, email: str, token: str, board_id: str | None, project_key_or_id: str | None
    ) -> None:
        if not base_url or not email or not token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="basepath (or jira_base_url), email (or jira_email), and token (or jira_api_token) are required",
            )

        if not board_id and not project_key_or_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Either board_id, project_key, or project_id is required",
            )

    async def _fetch_board_columns_with_error_handling(
            self,
            provider_inst: IntegrationProvider,
            base_url: str,
            email: str,
            token: str,
            board_id: str | None,
            project_key_or_id: str | None,
    ) -> dict[str, Any]:
        try:
            return await provider_inst.get_board_columns(
                base_url=base_url,
                email=email,
                token=token,
                board_id=board_id,
                project_key_or_id=project_key_or_id,
            )
        except HTTPException:
            raise
        except httpx.HTTPStatusError as exc:
            logger.error(f"Jira API error fetching board configuration: {exc}")
            raise HTTPException(
                status_code=exc.response.status_code,
                detail=f"Jira error: {exc.response.text}",
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except Exception as exc:
            logger.error(f"Error fetching board columns: {exc}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to fetch board columns: {exc!s}",
            ) from exc

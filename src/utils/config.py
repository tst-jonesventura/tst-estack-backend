import os
from pathlib import Path

import logfire
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanProcessor
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from utils import log

logger = log.setup_custom_logger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )
    openai_api_key: str | None = Field(None, alias="ESTACK_OPENAI_API_KEY")
    google_api_key: str | None = Field(None, alias="ESTACK_GOOGLE_API_KEY")
    anthropic_api_key: str | None = Field(None, alias="ESTACK_ANTHROPIC_API_KEY")
    devin_api_key: str | None = Field(None, alias="ESTACK_DEVIN_API_KEY")
    default_kanban_columns: str = Field(
        "Backlog,Especificando,Testes Unitários,Roteiro de Desenvolvimento,Concluído",
        alias="DEFAULT_KANBAN_COLUMNS",
    )
    db_connection_string: str = Field(alias="ESTACK_DB_CONNECTION_STRING")
    estack_front_host: str = Field(alias="ESTACK_FRONT_HOST")
    default_model: str = Field(alias="DEFAULT_MODEL")
    logfire_token: str = Field(alias="LOGFIRE_TOKEN")
    new_relic_license_key: str | None = Field(None, alias="NEW_RELIC_LICENSE_KEY")
    new_relic_otlp_endpoint: str = Field(
        "https://otlp.nr-data.net:4317", alias="NEW_RELIC_OTLP_ENDPOINT"
    )
    new_relic_app_name: str = Field("estack-backend", alias="NEW_RELIC_APP_NAME")
    access_token_expire_minutes: int = Field(
        60 * 24 * 2, alias="ACCESS_TOKEN_EXPIRE_MINUTES"
    )
    access_code_expire_minutes: int = Field(15, alias="ACCESS_CODE_EXPIRE_MINUTES")
    secret_hash_key: str = Field(alias="SECRET_HASH_KEY")
    email_user: str = Field(alias="EMAIL_USER")
    email_password: str = Field(alias="EMAIL_PASSWORD")
    email_host: str = Field(alias="EMAIL_HOST")
    cors_origins: str = Field(alias="CORS_ORIGINS")
    max_related_items: int = Field(10, alias="MAX_RELATED_ITEMS")
    min_related_items: int = Field(2, alias="MIN_RELATED_ITEMS")
    devin_api_base_url: str | None = Field(
        "https://api.devin.ai/v1", alias="DEVIN_API_BASE_URL"
    )
    devin_max_prompt_length: int = Field(29500, alias="DEVIN_MAX_PROMPT_LENGTH")
    github_app_id: int | None = Field(None, alias="GITHUB_APP_ID")
    github_app_private_key_base64: str | None = Field(
        None, alias="GITHUB_APP_PRIVATE_KEY_BASE64"
    )
    default_organization_name: str | None = Field("Elumini-IT", alias="DEFAULT_ORG")
    config_encryption_key: str | None = Field(
        None, alias="ESTACK_CONFIG_ENCRYPTION_KEY"
    )
    project_databases_allow_private_network: bool = Field(
        False,
        alias="ESTACK_PROJECT_DATABASES_ALLOW_PRIVATE_NETWORK",
        description="Permitir redes privadas apenas durante o desenvolvimento local.",
    )
    openhands_suppress_banner: bool = Field(True, alias="OPENHANDS_SUPPRESS_BANNER")
    openhands_log_auto_config: bool = Field(False, alias="LOG_AUTO_CONFIG")
    openhands_workspace_root: Path = Field(
        default_factory=lambda: Path.home() / ".estack" / "openhands",
        alias="OPENHANDS_WORKSPACE_ROOT",
    )
    otel_exporter_otlp_traces_endpoint: str = Field(
        "https://logfire-us.pydantic.dev/v1/traces",
        alias="OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    )
    otel_exporter_otlp_traces_protocol: str = Field(
        "http/protobuf",
        alias="OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
    )
    estack_environment: str = Field("production", alias="ESTACK_ENVIRONMENT")
    estack_auth_bypass_local: bool = Field(False, alias="ESTACK_AUTH_BYPASS_LOCAL")
    seed_admin_email: str | None = Field(None, alias="SEED_ADMIN_EMAIL")

    def is_local_dev_auth_enabled(self) -> bool:
        """
        Fail-safe check to determine if local dev auth bypass is active.
        Strictly requires ESTACK_ENVIRONMENT to be 'local' AND ESTACK_AUTH_BYPASS_LOCAL to be True.
        """
        return (
            self.estack_environment.strip().lower() == "local"
            and bool(self.estack_auth_bypass_local)
        )
    local_workspace_enabled: bool | None = Field(None, alias="LOCAL_WORKSPACE_ENABLED")
    target_project_root: Path = Field(
        Path("/workspace/target_project"), alias="TARGET_PROJECT_ROOT"
    )


def build_new_relic_span_processors(config: Settings) -> list[SpanProcessor]:
    """Span processors that export Logfire/OTel traces to New Relic via OTLP gRPC."""
    if not config.new_relic_license_key:
        return []
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )

        exporter = OTLPSpanExporter(
            endpoint=config.new_relic_otlp_endpoint,
            headers={"api-key": config.new_relic_license_key},
        )
        return [BatchSpanProcessor(exporter)]
    except Exception as e:
        logger.warning("Could not initialize New Relic OTLP exporter: %s", e)
        return []


settings = Settings()  # type: ignore

os.environ["OPENHANDS_SUPPRESS_BANNER"] = str(
    settings.openhands_suppress_banner
).lower()
os.environ["LOG_AUTO_CONFIG"] = str(settings.openhands_log_auto_config).lower()

# Centralized OTEL setup for Logfire export.
os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = (
    settings.otel_exporter_otlp_traces_endpoint
)
os.environ["OTEL_EXPORTER_OTLP_TRACES_PROTOCOL"] = (
    settings.otel_exporter_otlp_traces_protocol
)
os.environ["OTEL_EXPORTER_OTLP_TRACES_HEADERS"] = (
    f"authorization=Bearer {settings.logfire_token}"
)

logfire.configure(
    token=settings.logfire_token,
    service_name=settings.new_relic_app_name,
    scrubbing=False,
    additional_span_processors=build_new_relic_span_processors(settings),
)
if settings.new_relic_license_key:
    logger.info(
        "New Relic OTLP exporter enabled (%s).", settings.new_relic_otlp_endpoint
    )
logfire.instrument_pydantic_ai()

logger.info("Settings loaded successfully.")
if settings.is_local_dev_auth_enabled():
    logger.warning(
        "LOCAL DEV AUTH BYPASS IS ENABLED (ESTACK_ENVIRONMENT=%s, ESTACK_AUTH_BYPASS_LOCAL=%s). "
        "Direct local login endpoint is active. NEVER enable this in production or staging!",
        settings.estack_environment,
        settings.estack_auth_bypass_local,
    )

# Manually set api keys
if settings.openai_api_key:
    os.environ["OPENAI_API_KEY"] = settings.openai_api_key
if settings.google_api_key:
    os.environ["GOOGLE_API_KEY"] = settings.google_api_key
if settings.anthropic_api_key:
    os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key

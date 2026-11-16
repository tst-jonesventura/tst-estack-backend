from datetime import datetime

from pydantic import Field

from schemas.base import Base

JIRA_API_TOKEN_DESC = "API Token do Jira"
JIRA_EMAIL_DESC = "E-mail da conta no Jira"
JIRA_BASE_URL_DESC = "URL base da instância Jira"


class JiraIntegrationColumnsMappingBase(Base):
    project_kanban_column_id: str = Field(
        ..., description="ID da coluna Kanban no E-Stack"
    )
    jira_status_id: str | None = Field(default=None, description="ID do status no Jira")
    jira_status_name: str = Field(..., description="Nome do status no Jira")


class JiraIntegrationColumnsMappingCreate(JiraIntegrationColumnsMappingBase):
    pass


class JiraIntegrationColumnsMappingPublic(JiraIntegrationColumnsMappingBase):
    id: str
    jira_integration_config_id: str


class JiraIntegrationConfigBase(Base):
    jira_base_url: str = Field(..., description=JIRA_BASE_URL_DESC)
    jira_email: str = Field(..., description="E-mail da conta de integração no Jira")
    jira_api_token: str = Field(..., description=JIRA_API_TOKEN_DESC)
    jira_project_id: str = Field(..., description="ID do projeto no Jira")
    jira_project_key: str = Field(..., description="Key do projeto no Jira")
    default_issue_type: str = Field(
        default="Story", description="Tipo padrão de issue (ex: Story, Task)"
    )


class JiraIntegrationConfigCreate(JiraIntegrationConfigBase):
    project_id: str = Field(..., description="ID do projeto no E-Stack")
    column_mappings: list[JiraIntegrationColumnsMappingCreate] | None = Field(
        default=None,
        description="Lista opcional de mapeamentos de colunas Kanban para status do Jira",
    )


class JiraIntegrationConfigUpdate(Base):
    jira_base_url: str | None = None
    jira_email: str | None = None
    jira_api_token: str | None = None
    jira_project_id: str | None = None
    jira_project_key: str | None = None
    default_issue_type: str | None = None
    column_mappings: list[JiraIntegrationColumnsMappingCreate] | None = None


class JiraIntegrationConfigPublic(JiraIntegrationConfigBase):
    id: str
    project_id: str
    created_at: datetime | None = None
    column_mappings: list[JiraIntegrationColumnsMappingPublic] = Field(
        default_factory=list, description="Lista de mapeamentos de colunas"
    )


class JiraTestConnectionPayload(Base):
    basepath: str | None = Field(
        default=None,
        description="URL base da instância Jira (ex: https://empresa.atlassian.net)",
    )
    jira_base_url: str | None = Field(default=None, description=JIRA_BASE_URL_DESC)
    email: str | None = Field(default=None, description=JIRA_EMAIL_DESC)
    jira_email: str | None = Field(default=None, description=JIRA_EMAIL_DESC)
    token: str | None = Field(default=None, description=JIRA_API_TOKEN_DESC)
    jira_api_token: str | None = Field(default=None, description=JIRA_API_TOKEN_DESC)


class JiraGetColumnsPayload(Base):
    basepath: str | None = Field(default=None, description=JIRA_BASE_URL_DESC)
    jira_base_url: str | None = Field(default=None, description=JIRA_BASE_URL_DESC)
    email: str | None = Field(default=None, description=JIRA_EMAIL_DESC)
    jira_email: str | None = Field(default=None, description=JIRA_EMAIL_DESC)
    token: str | None = Field(default=None, description=JIRA_API_TOKEN_DESC)
    jira_api_token: str | None = Field(default=None, description=JIRA_API_TOKEN_DESC)
    project_key: str | None = Field(
        default=None, description="Key do projeto no Jira (ex: ESTACK)"
    )
    jira_project_key: str | None = Field(
        default=None, description="Key do projeto no Jira"
    )
    project_id: str | None = Field(
        default=None, description="ID do projeto no Jira ou E-Stack"
    )
    jira_project_id: str | None = Field(
        default=None, description="ID do projeto no Jira"
    )
    board_id: str | int | None = Field(default=None, description="ID do Board no Jira")

from datetime import datetime
from schemas.base import Base


class ProjectGitHubAppInstallationBase(Base):
    github_app_installation_id: int
    github_account_login: str | None = None
    github_org_name: str | None = None


class ProjectGitHubAppInstallationPublic(ProjectGitHubAppInstallationBase):
    id: str
    organization_id: str
    created_at: datetime | None = None


class ProjectGitHubAppInstallationCreate(Base):
    github_app_installation_id: int
    github_account_login: str | None = None
    github_org_name: str | None = None


class ProjectGitHubAppInstallationUpdate(Base):
    github_app_installation_id: int | None = None
    github_account_login: str | None = None
    github_org_name: str | None = None


class GitHubOwner(Base):
    login: str


class GitHubRepository(Base):
    id: int
    name: str
    full_name: str
    private: bool
    html_url: str
    default_branch: str
    owner: GitHubOwner
    language: str | None


class GitHubRepositoriesResponse(Base):
    total_count: int
    github_app_installation_id: str
    repositories: list[GitHubRepository]

from schemas.base import Base
from schemas.user_story_schemas import UserStoryFullPublic


class ProjectRepositoryBase(Base):
    source_type: str = "github"
    local_path: str | None = None
    url: str | None = None
    name: str
    owner: str | None = None
    full_name: str | None = None
    default_branch: str | None = "main"
    organization_github_installation_id: str | None = None
    language: str | None = None


class LocalFolderInfo(Base):
    name: str
    relative_path: str
    is_git: bool
    detected_language: str | None = None
    file_count: int = 0


class LocalFolderValidationResult(Base):
    valid: bool
    path: str
    is_git: bool
    detected_language: str | None = None
    file_count: int = 0
    error: str | None = None


class ProjectBase(Base):
    name: str
    is_active: bool = True
    description: str | None = None
    organization_id: str | None = None


class ProjectCreate(ProjectBase):
    pass


class ProjectRepositoryCreate(ProjectRepositoryBase):
    pass


class ProjectPublic(ProjectBase):
    id: str


class ProjectWithCountsPublic(ProjectPublic):
    user_stories_count: int
    tech_specs_count: int


class ProjectRepositoryPublic(ProjectRepositoryBase):
    id: str
    project_id: str | None = None


class ProjectWithFullStoriesPublic(ProjectPublic):
    user_stories: list[UserStoryFullPublic] = []


class ProjectUpdate(Base):
    name: str | None = None
    description: str | None = None
    is_active: bool | None = None


class ProjectModelTokenUsagePublic(Base):
    model: str
    agent_id: str | None = None
    agent_name: str | None = None
    total_tokens: int


class ProjectTokenUsagePublic(Base):
    id: str
    name: str
    models: list[ProjectModelTokenUsagePublic] = []

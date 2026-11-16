import os
from collections import Counter
from pathlib import Path
from typing import Annotated

from agents.repo_to_tech_spec_agent import create_tech_specs_from_repo_in_background
from database.database import SessionDep
from database.models import McpProviderType, ProjectRepository, User
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from schemas.project_schemas import (
    LocalFolderInfo,
    LocalFolderValidationResult,
    ProjectRepositoryCreate,
    ProjectRepositoryPublic,
)
from utils import log
from utils.bearer_auth import check_project_access
from utils.environment import (
    get_target_project_root,
    is_local_workspace_enabled,
    resolve_safe_target_path,
)
from utils.validation import validate_entity_exists

logger = log.setup_custom_logger(__name__)
router = APIRouter()

LOCAL_WORKSPACE_NOT_ENABLED_MSG = (
    "Local workspace functionality is not enabled in this environment."
)

EXTENSION_LANGUAGE_MAP = {
    ".py": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".java": "Java",
    ".go": "Go",
    ".rs": "Rust",
    ".cs": "C#",
    ".cpp": "C++",
    ".c": "C",
    ".rb": "Ruby",
    ".php": "PHP",
    ".html": "HTML",
    ".css": "CSS",
    ".vue": "Vue",
    ".kt": "Kotlin",
}


def _inspect_directory(target_dir: Path) -> tuple[bool, str | None, int]:
    is_git = (target_dir / ".git").exists()
    ext_counts: Counter[str] = Counter()
    total_files = 0
    ignored_dirs = {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "dist",
        "build",
        ".idea",
        ".vscode",
        "coverage",
    }

    try:
        for root, dirs, files in os.walk(target_dir):
            dirs[:] = [d for d in dirs if d not in ignored_dirs]
            for f in files:
                total_files += 1
                ext = Path(f).suffix.lower()
                if ext in EXTENSION_LANGUAGE_MAP:
                    ext_counts[EXTENSION_LANGUAGE_MAP[ext]] += 1
                if total_files > 5000:
                    break
            if total_files > 5000:
                break
    except Exception as e:
        logger.warning("Error walking directory %s: %s", target_dir, e)

    detected_language = ext_counts.most_common(1)[0][0] if ext_counts else None
    return is_git, detected_language, total_files


# LOCAL WORKSPACE INSPECTION
@router.get(
    "/local-folders",
    response_model=list[LocalFolderInfo],
    responses={
        200: {"description": "List of local folders"},
        403: {"description": LOCAL_WORKSPACE_NOT_ENABLED_MSG},
    },
)
def list_local_folders(
    project_id: str,
    user_data: Annotated[User, Depends(check_project_access)],
):
    """List available folders within target_project_root for Docker environments."""
    if not is_local_workspace_enabled():
        raise HTTPException(
            status_code=403,
            detail=LOCAL_WORKSPACE_NOT_ENABLED_MSG,
        )

    root_path = get_target_project_root()
    if not root_path.exists():
        return []

    results = []
    # 1. Include root itself
    is_git, lang, count = _inspect_directory(root_path)
    results.append(
        LocalFolderInfo(
            name=f"{root_path.name} (Root)",
            relative_path="",
            is_git=is_git,
            detected_language=lang,
            file_count=count,
        )
    )

    # 2. Subdirectories
    ignored_names = {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "dist",
        "build",
        ".idea",
        ".vscode",
    }
    try:
        for entry in sorted(root_path.iterdir()):
            if entry.is_dir() and entry.name not in ignored_names:
                sub_git, sub_lang, sub_count = _inspect_directory(entry)
                results.append(
                    LocalFolderInfo(
                        name=entry.name,
                        relative_path=entry.name,
                        is_git=sub_git,
                        detected_language=sub_lang,
                        file_count=sub_count,
                    )
                )
    except Exception as e:
        logger.warning("Error scanning target_project_root subfolders: %s", e)

    return results


@router.get(
    "/local-folders/validate",
    response_model=LocalFolderValidationResult,
    responses={
        200: {
            "description": "Validation result for a specific path in target_project_root"
        },
        403: {"description": LOCAL_WORKSPACE_NOT_ENABLED_MSG},
    },
)
def validate_local_folder(
    project_id: str,
    user_data: Annotated[User, Depends(check_project_access)],
    path: Annotated[
        str, Query(description="Relative path within target_project_root")
    ] = "",
):
    """Validate a specific path in target_project_root and inspect language and git status."""
    if not is_local_workspace_enabled():
        raise HTTPException(
            status_code=403,
            detail=LOCAL_WORKSPACE_NOT_ENABLED_MSG,
        )

    try:
        target_path = resolve_safe_target_path(path)
    except ValueError as e:
        return LocalFolderValidationResult(
            valid=False,
            path=path,
            is_git=False,
            error=str(e),
        )

    if not target_path.exists() or not target_path.is_dir():
        return LocalFolderValidationResult(
            valid=False,
            path=path,
            is_git=False,
            error=f"Directory '{path}' does not exist.",
        )

    is_git, lang, count = _inspect_directory(target_path)
    return LocalFolderValidationResult(
        valid=True,
        path=path,
        is_git=is_git,
        detected_language=lang,
        file_count=count,
    )


# HELPERS FOR REPOSITORY CREATION
def _validate_and_resolve_local_path(local_path: str | None) -> Path:
    if not is_local_workspace_enabled():
        raise HTTPException(
            status_code=403,
            detail=LOCAL_WORKSPACE_NOT_ENABLED_MSG,
        )
    try:
        target_path = resolve_safe_target_path(local_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not target_path.exists():
        raise HTTPException(
            status_code=400,
            detail=f"Local path '{local_path}' does not exist in target project root.",
        )
    return target_path


def _create_local_repository_entity(
    project_id: str, repo: ProjectRepositoryCreate
) -> ProjectRepository:
    target_path = _validate_and_resolve_local_path(repo.local_path)
    _, detected_lang, _ = _inspect_directory(target_path)
    repo_name = repo.name or target_path.name
    return ProjectRepository(
        source_type=McpProviderType.LOCAL_FILESYSTEM.value,
        local_path=repo.local_path or "",
        url=repo.url or str(target_path),
        name=repo_name,
        owner=repo.owner or "local",
        project_id=project_id,
        language=repo.language or detected_lang or "unknown",
        full_name=repo.full_name or repo_name,
        default_branch=repo.default_branch or "main",
        organization_github_installation_id=None,
    )


def _create_github_repository_entity(
    project_id: str, repo: ProjectRepositoryCreate
) -> ProjectRepository:
    return ProjectRepository(
        source_type=repo.source_type or "github",
        local_path=None,
        url=repo.url,
        name=repo.name,
        owner=repo.owner,
        project_id=project_id,
        language=repo.language,
        full_name=repo.full_name,
        default_branch=repo.default_branch or "main",
        organization_github_installation_id=repo.organization_github_installation_id,
    )


def _persist_and_schedule_repo(
    db_repo: ProjectRepository,
    session: SessionDep,
    background_tasks: BackgroundTasks,
    user_id: str,
) -> ProjectRepository:
    session.add(db_repo)
    session.commit()
    session.refresh(db_repo)

    if db_repo.url:
        background_tasks.add_task(
            create_tech_specs_from_repo_in_background,
            db_repo.project_id,
            db_repo.url,
            user_id,
        )

    return db_repo


# CREATE
@router.post(
    "/",
    response_model=ProjectRepositoryPublic,
    responses={
        200: {"description": "Created project repository"},
        400: {"description": "Invalid request"},
        403: {"description": LOCAL_WORKSPACE_NOT_ENABLED_MSG},
    },
)
def create_project_repository(
    project_id: str,
    repo: ProjectRepositoryCreate,
    session: SessionDep,
    background_tasks: BackgroundTasks,
    user_data: Annotated[User, Depends(check_project_access)],
):
    if repo.source_type == McpProviderType.LOCAL_FILESYSTEM.value:
        db_repo = _create_local_repository_entity(project_id, repo)
    else:
        db_repo = _create_github_repository_entity(project_id, repo)

    return _persist_and_schedule_repo(db_repo, session, background_tasks, user_data.id)


# READ (single)
@router.get("/{repo_id}", response_model=ProjectRepositoryPublic)
def get_project_repository(
    project_id: str,
    repo_id: str,
    session: SessionDep,
    has_access: Annotated[bool, Depends(check_project_access)],
):
    repo = validate_entity_exists(
        session, ProjectRepository, repo_id, "ProjectRepository"
    )
    return repo


# READ (list)
@router.get("/", response_model=list[ProjectRepositoryPublic])
def list_project_repositories(
    project_id: str,
    session: SessionDep,
    has_access: Annotated[bool, Depends(check_project_access)],
):
    return session.query(ProjectRepository).filter_by(project_id=project_id).all()


# DELETE
@router.delete("/{repo_id}", response_model=dict)
def delete_project_repository(
    project_id: str,
    repo_id: str,
    session: SessionDep,
    has_access: Annotated[bool, Depends(check_project_access)],
):
    repo = validate_entity_exists(
        session, ProjectRepository, repo_id, "ProjectRepository"
    )
    session.delete(repo)
    session.commit()
    return {"detail": "Repository deleted"}

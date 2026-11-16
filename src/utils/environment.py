import os
from pathlib import Path

from utils import log
from utils.config import settings

logger = log.setup_custom_logger(__name__)


def is_running_in_docker() -> bool:
    """Check if the backend is currently running inside a Docker/containerized environment."""
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    env_docker = os.getenv("RUNNING_IN_DOCKER", "").strip().lower()
    if env_docker in ("true", "1", "yes"):
        return True
    env_is_docker = os.getenv("IS_DOCKER", "").strip().lower()
    if env_is_docker in ("true", "1", "yes"):
        return True
    return False


def get_target_project_root() -> Path:
    """Get the base path where external projects are mounted."""
    return Path(settings.target_project_root).resolve()


def is_local_workspace_enabled() -> bool:
    """
    Determine if local workspace functionality is enabled.

    Returns True if explicitly enabled via configuration or if auto-detected
    inside a Docker container with an accessible target project volume.
    """
    if settings.local_workspace_enabled is not None:
        return bool(settings.local_workspace_enabled)

    # Auto-detection
    if not is_running_in_docker():
        return False

    target_root = get_target_project_root()
    return target_root.exists() and target_root.is_dir()


def resolve_safe_target_path(subpath: str | None = None) -> Path:
    """
    Resolve and validate a target workspace path, preventing path traversal attacks.

    Raises:
        ValueError: If the resolved path escapes the base target_project_root directory.
    """
    base_root = get_target_project_root()
    if not subpath or subpath.strip() in ("", ".", "/"):
        return base_root

    # Clean leading slashes/backslashes to treat as relative to base_root
    cleaned_subpath = subpath.lstrip("/\\")
    resolved_path = (base_root / cleaned_subpath).resolve()

    try:
        resolved_path.relative_to(base_root)
    except ValueError:
        logger.error(
            "Security alert: Attempted path traversal outside workspace root. Base: %s, Requested: %s",
            base_root,
            subpath,
        )
        raise ValueError(
            f"Path '{subpath}' is outside the allowed target project directory."
        )

    return resolved_path

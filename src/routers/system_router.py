from fastapi import APIRouter
from pydantic import BaseModel
from utils.environment import (
    get_target_project_root,
    is_local_workspace_enabled,
    is_running_in_docker,
)

router = APIRouter(prefix="/system", tags=["system"])


class SystemCapabilitiesResponse(BaseModel):
    local_workspace_enabled: bool
    is_docker: bool
    target_project_root: str


@router.get("/capabilities")
def get_system_capabilities() -> SystemCapabilitiesResponse:
    """Return runtime capabilities and feature flags for the frontend."""
    return SystemCapabilitiesResponse(
        local_workspace_enabled=is_local_workspace_enabled(),
        is_docker=is_running_in_docker(),
        target_project_root=str(get_target_project_root()),
    )

from typing import List, Annotated

from fastapi import APIRouter, Depends

from database.database import SessionDep
from database.models import Tool
from schemas.agent_schemas import ToolPublic
from utils.bearer_auth import retrieve_user_data

router = APIRouter()


@router.get("/agent-tools", response_model=List[ToolPublic])
def list_tools(
        session: SessionDep,
        user_data: Annotated[bool, Depends(retrieve_user_data)],
):
    tools = session.query(Tool).all()
    return [ToolPublic.model_validate(tool) for tool in tools]

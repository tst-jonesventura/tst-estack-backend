from typing import List, Annotated

from fastapi import APIRouter, Depends

from database.database import SessionDep
from database.models import Context
from schemas.agent_schemas import ContextPublic
from utils.bearer_auth import retrieve_user_data

router = APIRouter()


@router.get("/agent-contexts", response_model=List[ContextPublic])
def list_contexts(
        session: SessionDep,
        user_data: Annotated[bool, Depends(retrieve_user_data)],
):
    contexts = session.query(Context).all()
    return [ContextPublic.model_validate(context) for context in contexts]

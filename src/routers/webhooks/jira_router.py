import json
from fastapi import APIRouter, Request, Depends
from services.sync_service import SyncService
from database.database import SessionDep
import utils.log as log

logger = log.setup_custom_logger(__name__)
router = APIRouter()

@router.post("/")
async def jira_webhook(request: Request, session: SessionDep):
    headers = dict(request.headers)
    logger.info(f"JIRA WEBHOOK - Headers: {headers}")
    
    try:
        body_bytes = await request.body()
        
        if not body_bytes:
             logger.warning("JIRA WEBHOOK - Body is empty.")
             return {"detail": "Empty webhook body"}
        
        payload = json.loads(body_bytes)
        logger.info("JIRA WEBHOOK - Parsed payload successfully")
        
        sync_service = SyncService(session)
        sync_service.handle_webhook(payload)
        return {"detail": "Webhook received"}
        
    except json.JSONDecodeError as e:
        logger.error(f"JIRA WEBHOOK DEBUG - JSON Decode Error: {e}")
        return {"detail": "Invalid JSON"}
    except Exception as e:
        logger.error(f"JIRA WEBHOOK DEBUG - Error: {e}", exc_info=True)
        return {"detail": "Error processing webhook"}

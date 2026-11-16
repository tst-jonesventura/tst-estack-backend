import asyncio
from database.database import create_session
from services.notification_service import notification_service
from utils import log

logger = log.setup_custom_logger(__name__)

async def run_daily_purge():
    """
    Background task that runs the notification purge once per day.
    """
    logger.info("Starting daily notification purge scheduler.")
    while True:
        try:
            # Run the purge
            logger.info("Executing daily notification purge...")
            # Use to_thread to avoid blocking the event loop with sync DB operations
            await asyncio.to_thread(self_contained_purge)
            
            # Wait for 24 hours
            logger.info("Purge executed. Next run in 24 hours.")
            await asyncio.sleep(24 * 3600)
            
        except Exception as e:
            logger.error(f"Error in daily notification purge task: {e}")
            # Wait a bit before retrying in case of persistent error
            await asyncio.sleep(3600)

def self_contained_purge():
    """
    Self-contained purge logic to be run in a separate thread.
    """
    try:
        with create_session() as session:
            notification_service.purge_old_notifications(session)
    except Exception as e:
        logger.error(f"Error during self_contained_purge: {e}")
        raise

def start_scheduler():
    """
    Starts the background scheduler tasks.
    """
    task = asyncio.create_task(run_daily_purge())
    return task

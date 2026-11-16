from typing import Annotated

from fastapi import Depends
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from utils import log
from utils.config import settings

logger = log.setup_custom_logger(__name__)

POOL_SIZE = 10
MAX_OVERFLOW = 20
# Emit a WARNING once the number of connections in use crosses this threshold,
# so pool exhaustion can be detected before it turns into a login incident.
POOL_CHECKEDOUT_WARNING_THRESHOLD = 25


def get_engine():
    connection_string = settings.db_connection_string
 
    if connection_string.startswith("sqlite"):
        return create_engine(
            connection_string,
            connect_args={"check_same_thread": False},
        )
 
    return create_engine(
        connection_string,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_timeout=30,
        pool_recycle=1800,
        pool_pre_ping=True,
    )

engine = get_engine()
create_session = sessionmaker(bind=engine)


def get_pool_status() -> dict:
    """Return current connection pool usage metrics."""
    pool = engine.pool
    return {
        "size": pool.size(),
        "checked_in": pool.checkedin(),
        "checked_out": pool.checkedout(),
        "overflow": pool.overflow(),
        "max_overflow": MAX_OVERFLOW,
        "total_capacity": POOL_SIZE + MAX_OVERFLOW,
    }


def _register_pool_listeners(target_engine) -> None:
    """Register listeners that log pool lifecycle events for observability."""

    @event.listens_for(target_engine, "connect")
    def _on_connect(_dbapi_connection, _connection_record):

        pass  # log removido

    @event.listens_for(target_engine, "checkout")
    def _on_checkout(_dbapi_connection, _connection_record, _connection_proxy):
        status = get_pool_status()
        if status["checked_out"] >= POOL_CHECKEDOUT_WARNING_THRESHOLD:
            logger.warning(
                "DB pool nearing exhaustion: %d/%d connections checked out | %s",
                status["checked_out"],
                status["total_capacity"],
                status,
            )

    @event.listens_for(target_engine, "checkin")
    def _on_checkin(_dbapi_connection, _connection_record):
        pass  # log removido


_register_pool_listeners(engine)


def get_session():
    session = create_session()
    try:
        yield session
    finally:
        session.close()


def create_entity(session: Session, entity):
    """Generic create operation with commit and refresh."""
    session.add(entity)
    session.commit()
    session.refresh(entity)
    return entity


def update_entity(session: Session, entity, update_data: dict):
    """Generic update operation with commit and refresh."""
    for key, value in update_data.items():
        if value is None:
            continue
        setattr(entity, key, value)
    session.add(entity)
    session.commit()
    session.refresh(entity)
    return entity


def delete_entity(session: Session, entity):
    """Generic delete operation with commit."""
    session.delete(entity)
    session.commit()
    return {"ok": True}


SessionDep = Annotated[Session, Depends(get_session)]

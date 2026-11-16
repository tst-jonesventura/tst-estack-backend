from fastapi import HTTPException
from sqlalchemy.orm import joinedload

from database.database import SessionDep
from database.models import DocumentTranscription


def format_transcription(
    session: SessionDep, project_id: str, document_transcription_id: str
) -> str:
    transcription = (
        session.query(DocumentTranscription)
        .options(joinedload(DocumentTranscription.tags))
        .where(DocumentTranscription.id == document_transcription_id)
        .where(DocumentTranscription.project_id == project_id)
        .scalar()
    )
    if not transcription:
        raise HTTPException(status_code=404, detail="DocumentTranscription not found")
    transcription_formatted = transcription.content
    transcription_formatted += "\nTags:" + "\n".join(
        tag.tag for tag in transcription.tags
    )
    return transcription_formatted

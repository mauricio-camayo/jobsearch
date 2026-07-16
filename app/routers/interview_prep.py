from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import get_current_user_api
from app.db.database import get_db
from app.models.interview_prep import InterviewPrep
from app.models.tracker_record import TrackerRecord
from app.models.user import User

router = APIRouter(prefix="/api/tracker/{record_id}/prep", tags=["interview-prep"])


def _note_to_dict(n: InterviewPrep) -> dict:
    return {
        "id": n.id,
        "tracker_record_id": n.tracker_record_id,
        "title": n.title,
        "body": n.body,
        "pinned": n.pinned,
        "created_at": n.created_at,
        "updated_at": n.updated_at,
    }


def _get_owned_record_or_404(db: Session, user_id: int, record_id: int) -> TrackerRecord:
    record = (
        db.query(TrackerRecord)
        .filter(TrackerRecord.id == record_id, TrackerRecord.user_id == user_id)
        .first()
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Tracker record not found")
    return record


def _get_owned_note_or_404(db: Session, user_id: int, record_id: int, note_id: int) -> InterviewPrep:
    _get_owned_record_or_404(db, user_id, record_id)
    note = (
        db.query(InterviewPrep)
        .filter(InterviewPrep.id == note_id, InterviewPrep.tracker_record_id == record_id)
        .first()
    )
    if note is None:
        raise HTTPException(status_code=404, detail="Prep note not found")
    return note


def _ordered_notes(db: Session, record_id: int) -> list[InterviewPrep]:
    return (
        db.query(InterviewPrep)
        .filter(InterviewPrep.tracker_record_id == record_id)
        .order_by(InterviewPrep.pinned.desc(), InterviewPrep.updated_at.desc())
        .all()
    )


_TITLE_MAX = 200
_BODY_MAX = 20_000


class PrepCreate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=_TITLE_MAX)
    body: str = Field(min_length=1, max_length=_BODY_MAX)
    pinned: bool = False


class PrepUpdate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=_TITLE_MAX)
    body: Optional[str] = Field(default=None, min_length=1, max_length=_BODY_MAX)
    pinned: Optional[bool] = None


@router.get("")
def list_notes(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    _get_owned_record_or_404(db, current_user.id, record_id)
    return [_note_to_dict(n) for n in _ordered_notes(db, record_id)]


@router.post("", status_code=201)
def create_note(
    record_id: int,
    body: PrepCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    _get_owned_record_or_404(db, current_user.id, record_id)
    note = InterviewPrep(
        tracker_record_id=record_id,
        title=(body.title or "").strip() or None,
        body=body.body,
        pinned=body.pinned,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return _note_to_dict(note)


@router.patch("/{note_id}")
def update_note(
    record_id: int,
    note_id: int,
    body: PrepUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    note = _get_owned_note_or_404(db, current_user.id, record_id, note_id)
    if body.title is not None:
        note.title = body.title.strip() or None
    if body.body is not None:
        note.body = body.body
    if body.pinned is not None:
        note.pinned = body.pinned
    note.updated_at = datetime.utcnow()
    db.commit()
    return _note_to_dict(note)


@router.delete("/{note_id}", status_code=204)
def delete_note(
    record_id: int,
    note_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    note = _get_owned_note_or_404(db, current_user.id, record_id, note_id)
    db.delete(note)
    db.commit()
    return Response(status_code=204)

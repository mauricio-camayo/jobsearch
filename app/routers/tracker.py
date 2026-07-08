from datetime import date, datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session
from app.auth import get_current_user_api
from app.db.database import get_db
from app.models.app_config import AppConfig
from app.models.tracker_record import TrackerRecord, VALID_TRANSITIONS, TERMINAL_STATES, ALL_STATES
from app.models.user import User

router = APIRouter(prefix="/api/tracker", tags=["tracker"])


def _record_to_dict(r: TrackerRecord) -> dict:
    return {
        "id": r.id,
        "company": r.company,
        "role_title": r.role_title,
        "apply_url": r.apply_url,
        "status": r.status,
        "fit_pct": r.fit_pct,
        "date_shown": r.date_shown.isoformat() if r.date_shown else None,
        "date_applied": r.date_applied.isoformat() if r.date_applied else None,
        "notes": r.notes,
        "listing_id": r.listing_id,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
    }


class TrackerCreate(BaseModel):
    company: str
    role_title: str
    apply_url: Optional[str] = None
    fit_pct: Optional[int] = Field(default=None, ge=0, le=100)
    date_shown: Optional[date] = None
    notes: Optional[str] = None

    @field_validator("apply_url")
    @classmethod
    def _http_scheme_only(cls, v):
        if v and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("apply_url must be an http:// or https:// URL")
        return v


class StatusUpdate(BaseModel):
    status: str


class NotesUpdate(BaseModel):
    notes: str


def _get_owned_or_404(db: Session, user_id: int, record_id: int) -> TrackerRecord:
    record = (
        db.query(TrackerRecord)
        .filter(TrackerRecord.id == record_id, TrackerRecord.user_id == user_id)
        .first()
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Record not found")
    return record


@router.get("")
def list_records(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    q = db.query(TrackerRecord).filter(TrackerRecord.user_id == current_user.id)
    if status:
        q = q.filter(TrackerRecord.status == status)
    return [_record_to_dict(r) for r in q.order_by(TrackerRecord.id).all()]


@router.get("/{record_id}")
def get_record(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    return _record_to_dict(_get_owned_or_404(db, current_user.id, record_id))


@router.post("", status_code=201)
def create_record(
    body: TrackerCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    record = TrackerRecord(
        user_id=current_user.id,
        company=body.company,
        role_title=body.role_title,
        apply_url=body.apply_url,
        status="shown",
        fit_pct=body.fit_pct,
        date_shown=body.date_shown or date.today(),
        notes=body.notes,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return _record_to_dict(record)


@router.patch("/{record_id}/status")
def update_status(
    record_id: int,
    body: StatusUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    record = _get_owned_or_404(db, current_user.id, record_id)

    target = body.status
    if target not in ALL_STATES:
        raise HTTPException(status_code=422, detail=f"Unknown status '{target}'")

    current = record.status
    if current in TERMINAL_STATES:
        raise HTTPException(
            status_code=422,
            detail=f"Status '{current}' is terminal — no further transitions allowed",
        )

    allowed = VALID_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"Transition '{current}' → '{target}' is not allowed. "
                   f"Valid next states: {sorted(allowed)}",
        )

    record.status = target
    if target == "applied" and record.date_applied is None:
        record.date_applied = date.today()
    db.commit()
    return _record_to_dict(record)


@router.post("/expire-stale")
def expire_stale(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    """Expire shown records that have had no action after shown_expiry_days days.

    Uses the value of AppConfig.shown_expiry_days (default 30). Returns the
    count and IDs of records that were transitioned to 'expired'.
    """
    cfg = db.query(AppConfig).filter_by(user_id=current_user.id).first()
    expiry_days = cfg.shown_expiry_days if cfg else 30
    cutoff = date.today() - timedelta(days=expiry_days)
    records = (
        db.query(TrackerRecord)
        .filter(
            TrackerRecord.user_id == current_user.id,
            TrackerRecord.status == "shown",
            TrackerRecord.date_shown <= cutoff,
        )
        .all()
    )
    now = datetime.utcnow()
    ids = []
    for r in records:
        r.status = "expired"
        r.updated_at = now
        ids.append(r.id)
    if ids:
        db.commit()
    return {"expired": len(ids), "ids": ids, "expiry_days": expiry_days}


@router.patch("/bulk-skip")
def bulk_skip(
    max_fit_pct: int = Query(..., ge=0, le=100, description="Skip all 'shown' records with fit_pct <= this value"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    """Transition all shown records with fit_pct <= max_fit_pct to skipped."""
    records = (
        db.query(TrackerRecord)
        .filter(
            TrackerRecord.user_id == current_user.id,
            TrackerRecord.status == "shown",
            TrackerRecord.fit_pct <= max_fit_pct,
        )
        .all()
    )
    now = datetime.utcnow()
    for r in records:
        r.status = "skipped"
        r.updated_at = now
    db.commit()
    return {"skipped": len(records)}


@router.patch("/{record_id}/notes")
def update_notes(
    record_id: int,
    body: NotesUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    record = _get_owned_or_404(db, current_user.id, record_id)
    record.notes = body.notes
    db.commit()
    return _record_to_dict(record)

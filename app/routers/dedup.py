from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import get_current_user_api
from app.db.database import get_db
from app.models.user import User
from app.services.dedup import check_duplicate, DedupResult

router = APIRouter(prefix="/api/dedup", tags=["dedup"])


class DedupCheckRequest(BaseModel):
    company: str = Field(..., min_length=1)
    role_title: str = Field(..., min_length=1)
    apply_url: str | None = None


class DedupCheckResponse(BaseModel):
    action: str
    reason: str
    check: str | None
    matched_record_id: int | None
    matched_status: str | None
    resurface_note: str | None


@router.post("/check", response_model=DedupCheckResponse)
def dedup_check(
    body: DedupCheckRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    result: DedupResult = check_duplicate(
        db,
        company=body.company,
        role_title=body.role_title,
        user_id=current_user.id,
        apply_url=body.apply_url,
    )
    return DedupCheckResponse(
        action=result.action,
        reason=result.reason,
        check=result.check,
        matched_record_id=result.matched_record_id,
        matched_status=result.matched_status,
        resurface_note=result.resurface_note,
    )

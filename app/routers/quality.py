from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth import get_current_user_api
from app.services.quality_flags import check_quality_flags

router = APIRouter(prefix="/api/quality", tags=["quality"], dependencies=[Depends(get_current_user_api)])


class QualityCheckRequest(BaseModel):
    title: str
    description: Optional[str] = ""


class QualityCheckResponse(BaseModel):
    flags: list[str]
    skip: bool
    skip_reason: Optional[str]
    urgency_note: Optional[str]


@router.post("/check", response_model=QualityCheckResponse)
def quality_check(body: QualityCheckRequest):
    result = check_quality_flags(body.title, body.description or "")
    return QualityCheckResponse(
        flags=result.flags,
        skip=result.skip,
        skip_reason=result.skip_reason,
        urgency_note=result.urgency_note,
    )

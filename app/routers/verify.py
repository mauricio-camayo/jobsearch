from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, HttpUrl
from app.auth import get_current_user_api
from app.services.verifier import verify_url, VerificationResult

router = APIRouter(prefix="/api/verify", tags=["verify"], dependencies=[Depends(get_current_user_api)])


class VerifyRequest(BaseModel):
    url: HttpUrl


def _result_to_dict(r: VerificationResult) -> dict:
    return {
        "status": r.status,
        "reason": r.reason,
        "checked_url": r.checked_url,
        "http_status": r.http_status,
        "title": r.title,
    }


@router.post("")
def verify_listing(body: VerifyRequest):
    """
    Verify whether a job listing URL is still active.

    Returns:
      - status: "active" | "expired" | "unverified"
      - reason: human-readable explanation
      - checked_url: final URL after any redirects
      - http_status: HTTP status code received (null if network error)
      - title: page <title> if found (null otherwise)
    """
    result = verify_url(str(body.url))
    return _result_to_dict(result)

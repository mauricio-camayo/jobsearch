import json
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.auth import get_current_user_api
from app.db.database import get_db
from app.models.listing import JobListing
from app.models.user import User

router = APIRouter(prefix="/api/listings", tags=["listings"])

_VALID_STATUSES = {"new", "shown", "skipped", "pipeline_run"}


# ── Serialiser ────────────────────────────────────────────────────────────────

def _to_dict(l: JobListing) -> dict:
    return {
        "id": l.id,
        "company": l.company,
        "role_title": l.role_title,
        "apply_url": l.apply_url,
        "source": l.source,
        "remote_type": l.remote_type,
        "geo_restriction": l.geo_restriction,
        "relocation_offered": l.relocation_offered,
        "visa_sponsorship": l.visa_sponsorship,
        "description": l.description,
        "required_skills": json.loads(l.required_skills or "[]"),
        "role_domains": json.loads(l.role_domains or "[]"),
        "aggregator_remote_type": l.aggregator_remote_type,
        "quality_flags": json.loads(l.quality_flags or "[]"),
        "fit_score": l.fit_score,
        "verified_active": l.verified_active,
        "verified_at": l.verified_at,
        "status": l.status,
        "discovered_at": l.discovered_at,
        "updated_at": l.updated_at,
    }


# ── Schemas ───────────────────────────────────────────────────────────────────

class ListingCreate(BaseModel):
    company: str = Field(..., min_length=1)
    role_title: str = Field(..., min_length=1)
    apply_url: Optional[str] = None
    source: Optional[str] = None
    remote_type: str = "unknown"
    geo_restriction: str = "unknown"
    relocation_offered: bool = False
    visa_sponsorship: bool = False
    description: Optional[str] = None
    required_skills: list[str] = Field(default_factory=list)
    role_domains: list[str] = Field(default_factory=list)
    aggregator_remote_type: Optional[str] = None
    quality_flags: list[str] = Field(default_factory=list)
    fit_score: Optional[int] = Field(default=None, ge=0, le=110)
    verified_active: Optional[bool] = None
    status: str = "new"

    @field_validator("apply_url")
    @classmethod
    def _http_scheme_only(cls, v):
        if v and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("apply_url must be an http:// or https:// URL")
        return v


class ListingUpdate(BaseModel):
    company: Optional[str] = None
    role_title: Optional[str] = None
    apply_url: Optional[str] = None
    source: Optional[str] = None
    remote_type: Optional[str] = None
    geo_restriction: Optional[str] = None
    relocation_offered: Optional[bool] = None
    visa_sponsorship: Optional[bool] = None
    description: Optional[str] = None
    required_skills: Optional[list[str]] = None
    role_domains: Optional[list[str]] = None
    aggregator_remote_type: Optional[str] = None
    quality_flags: Optional[list[str]] = None
    fit_score: Optional[int] = Field(default=None, ge=0, le=110)
    verified_active: Optional[bool] = None
    status: Optional[str] = None

    @field_validator("apply_url")
    @classmethod
    def _http_scheme_only(cls, v):
        if v and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("apply_url must be an http:// or https:// URL")
        return v


def _get_owned_or_404(db: Session, user_id: int, listing_id: int) -> JobListing:
    listing = (
        db.query(JobListing)
        .filter(JobListing.id == listing_id, JobListing.user_id == user_id)
        .first()
    )
    if listing is None or listing.deleted_at is not None:
        raise HTTPException(404, "Listing not found")
    return listing


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("", status_code=201)
def create_listing(
    body: ListingCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    if body.status not in _VALID_STATUSES:
        raise HTTPException(422, f"status must be one of {sorted(_VALID_STATUSES)}")
    listing = JobListing(
        user_id=current_user.id,
        company=body.company,
        role_title=body.role_title,
        apply_url=body.apply_url,
        source=body.source,
        remote_type=body.remote_type,
        geo_restriction=body.geo_restriction,
        relocation_offered=body.relocation_offered,
        visa_sponsorship=body.visa_sponsorship,
        description=body.description,
        required_skills=json.dumps(body.required_skills),
        role_domains=json.dumps(body.role_domains),
        aggregator_remote_type=body.aggregator_remote_type,
        quality_flags=json.dumps(body.quality_flags),
        fit_score=body.fit_score,
        verified_active=body.verified_active,
        status=body.status,
    )
    db.add(listing)
    db.commit()
    db.refresh(listing)
    return _to_dict(listing)


@router.get("")
def list_listings(
    status: Optional[str] = None,
    include_deleted: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    q = db.query(JobListing).filter(JobListing.user_id == current_user.id)
    if not include_deleted:
        q = q.filter(JobListing.deleted_at == None)  # noqa: E711
    if status:
        q = q.filter(JobListing.status == status)
    return [_to_dict(l) for l in q.order_by(JobListing.discovered_at.desc()).all()]


@router.get("/{listing_id}")
def get_listing(
    listing_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    return _to_dict(_get_owned_or_404(db, current_user.id, listing_id))


@router.patch("/{listing_id}")
def update_listing(
    listing_id: int,
    body: ListingUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    listing = _get_owned_or_404(db, current_user.id, listing_id)
    updates = body.model_dump(exclude_none=True)
    if "status" in updates and updates["status"] not in _VALID_STATUSES:
        raise HTTPException(422, f"status must be one of {sorted(_VALID_STATUSES)}")
    for key, value in updates.items():
        if key in ("required_skills", "role_domains", "quality_flags"):
            setattr(listing, key, json.dumps(value))
        else:
            setattr(listing, key, value)
    listing.updated_at = datetime.utcnow()
    db.commit()
    return _to_dict(listing)


@router.delete("/{listing_id}", status_code=204)
def delete_listing(
    listing_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    listing = _get_owned_or_404(db, current_user.id, listing_id)
    listing.deleted_at = datetime.utcnow()
    db.commit()

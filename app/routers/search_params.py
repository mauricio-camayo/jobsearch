"""
Search parameters CRUD — GET/PATCH /api/search-params
Also exposes POST /api/filter/check to test filter logic against any listing.
"""
import json
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.auth import get_current_user_api
from app.db.database import get_db
from app.models.search_params import SearchParams
from app.models.user import User
from app.services.filters import apply_all_filters, GEO_VALUES, REMOTE_FILTER_VALUES

router = APIRouter(tags=["search"])

_GEO_FILTER_VALUES = {"worldwide", "latam", "emea", "north_america", "any"}


def _to_dict(sp: SearchParams) -> dict:
    return {
        "id": sp.id,
        "remote_type": sp.remote_type,
        "geo": sp.geo,
        "role_types": json.loads(sp.role_types),
        "keywords": json.loads(sp.keywords),
        "salary_min": sp.salary_min,
        "relocation_required": sp.relocation_required,
        "visa_required": sp.visa_required,
        "updated_at": sp.updated_at,
    }


def _get_or_404(db: Session, user_id: int) -> SearchParams:
    sp = db.query(SearchParams).filter_by(user_id=user_id).first()
    if sp is None:
        raise HTTPException(status_code=404, detail="SearchParams not initialised")
    return sp


# ── SearchParams CRUD ─────────────────────────────────────────────────────────

class SearchParamsUpdate(BaseModel):
    remote_type: Optional[str] = None
    geo: Optional[str] = None
    role_types: Optional[list[str]] = None
    keywords: Optional[list[str]] = None
    salary_min: Optional[int] = Field(default=None, ge=0)
    relocation_required: Optional[bool] = None
    visa_required: Optional[bool] = None

    @field_validator("remote_type")
    @classmethod
    def valid_remote(cls, v):
        if v not in REMOTE_FILTER_VALUES:
            raise ValueError(f"remote_type must be one of {sorted(REMOTE_FILTER_VALUES)}")
        return v

    @field_validator("geo")
    @classmethod
    def valid_geo(cls, v):
        if v not in _GEO_FILTER_VALUES:
            raise ValueError(f"geo must be one of {sorted(_GEO_FILTER_VALUES)}")
        return v


@router.get("/api/search-params")
def get_search_params(db: Session = Depends(get_db), current_user: User = Depends(get_current_user_api)):
    return _to_dict(_get_or_404(db, current_user.id))


@router.patch("/api/search-params")
def update_search_params(
    body: SearchParamsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    sp = _get_or_404(db, current_user.id)
    updates = body.model_dump(exclude_none=True)
    for key, value in updates.items():
        if key in ("role_types", "keywords"):
            setattr(sp, key, json.dumps(value))
        else:
            setattr(sp, key, value)
    sp.updated_at = datetime.utcnow()
    db.commit()
    return _to_dict(sp)


# ── Filter check endpoint ─────────────────────────────────────────────────────

class FilterCheckRequest(BaseModel):
    remote_type: str = "unknown"
    geo: str = "unknown"
    relocation_offered: bool = False
    visa_sponsorship: bool = False
    aggregator_remote_type: Optional[str] = None
    # Override search params for this check (optional)
    search_remote_type: Optional[str] = None
    search_geo: Optional[str] = None


class FilterCheckResponse(BaseModel):
    passed: bool
    flags: list[str]
    skip_reason: Optional[str]
    override_reason: Optional[str]
    applied_remote_filter: str
    applied_geo_filter: str


@router.post("/api/filter/check", response_model=FilterCheckResponse)
def filter_check(
    body: FilterCheckRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    sp = _get_or_404(db, current_user.id)
    remote_filter = body.search_remote_type or sp.remote_type
    geo_filter = body.search_geo or sp.geo

    result = apply_all_filters(
        job_remote_type=body.remote_type,
        job_geo=body.geo,
        search_remote_type=remote_filter,
        search_geo=geo_filter,
        relocation_offered=body.relocation_offered,
        visa_sponsorship=body.visa_sponsorship,
        relocation_required=sp.relocation_required,
        visa_required=sp.visa_required,
        aggregator_remote_type=body.aggregator_remote_type,
    )
    return FilterCheckResponse(
        passed=result.passed,
        flags=result.flags,
        skip_reason=result.skip_reason,
        override_reason=result.override_reason,
        applied_remote_filter=remote_filter,
        applied_geo_filter=geo_filter,
    )

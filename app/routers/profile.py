from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app.auth import get_current_user_api
from app.db.database import get_db
from app.models.user import User
from app.models.user_profile import UserProfile

router = APIRouter(prefix="/api/profile", tags=["profile"])


def _to_dict(profile: UserProfile) -> dict:
    return {
        "id": profile.id,
        "full_name": profile.full_name,
        "email": profile.email,
        "skills": profile.skills,
        "experience_years": profile.experience_years,
        "seniority": profile.seniority,
        "domains": profile.domains,
        "updated_at": profile.updated_at,
    }


class ProfileUpdate(BaseModel):
    full_name: Optional[str] = None
    skills: Optional[list[str]] = None
    experience_years: Optional[int] = Field(default=None, ge=0)
    seniority: Optional[str] = None
    domains: Optional[list[str]] = None


@router.get("")
def get_profile(db: Session = Depends(get_db), current_user: User = Depends(get_current_user_api)):
    profile = db.query(UserProfile).filter_by(user_id=current_user.id).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return _to_dict(profile)


@router.patch("")
def update_profile(
    body: ProfileUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    profile = db.query(UserProfile).filter_by(user_id=current_user.id).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    updates = body.model_dump(exclude_none=True)
    for key, value in updates.items():
        setattr(profile, key, value)
    profile.updated_at = datetime.utcnow()
    db.commit()
    return _to_dict(profile)

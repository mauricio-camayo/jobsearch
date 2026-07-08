from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app.auth import get_current_user_api
from app.db.database import get_db
from app.models.app_config import AppConfig
from app.models.user import User

router = APIRouter(prefix="/api/config", tags=["config"])


def _to_dict(c: AppConfig) -> dict:
    return {
        "id": c.id,
        "fit_autosave_threshold": c.fit_autosave_threshold,
        "shown_expiry_days": c.shown_expiry_days,
        "updated_at": c.updated_at,
    }


class ConfigUpdate(BaseModel):
    fit_autosave_threshold: Optional[int] = Field(default=None, ge=0, le=100)
    shown_expiry_days: Optional[int] = Field(default=None, ge=1)


@router.get("")
def get_config(db: Session = Depends(get_db), current_user: User = Depends(get_current_user_api)):
    config = db.query(AppConfig).filter_by(user_id=current_user.id).first()
    if config is None:
        raise HTTPException(status_code=404, detail="Config not initialised")
    return _to_dict(config)


@router.patch("")
def update_config(
    body: ConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    config = db.query(AppConfig).filter_by(user_id=current_user.id).first()
    if config is None:
        raise HTTPException(status_code=404, detail="Config not initialised")
    updates = body.model_dump(exclude_none=True)
    for key, value in updates.items():
        setattr(config, key, value)
    config.updated_at = datetime.utcnow()
    db.commit()
    return _to_dict(config)

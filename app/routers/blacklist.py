from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.auth import get_current_user_api
from app.db.database import get_db
from app.models.company_blacklist import CompanyBlacklist
from app.models.user import User

router = APIRouter(prefix="/api/blacklist", tags=["blacklist"])


def _to_dict(entry: CompanyBlacklist) -> dict:
    return {
        "id": entry.id,
        "company_name": entry.company_name,
        "notes": entry.notes,
        "created_at": entry.created_at,
    }


class BlacklistCreate(BaseModel):
    company_name: str
    notes: Optional[str] = None


@router.get("")
def list_blacklist(db: Session = Depends(get_db), current_user: User = Depends(get_current_user_api)):
    entries = (
        db.query(CompanyBlacklist)
        .filter_by(user_id=current_user.id)
        .order_by(CompanyBlacklist.company_name)
        .all()
    )
    return [_to_dict(e) for e in entries]


@router.post("", status_code=201)
def create_blacklist_entry(
    body: BlacklistCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    existing = (
        db.query(CompanyBlacklist)
        .filter(
            CompanyBlacklist.user_id == current_user.id,
            CompanyBlacklist.company_name == body.company_name,
        )
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail=f"'{body.company_name}' is already blacklisted")
    entry = CompanyBlacklist(user_id=current_user.id, company_name=body.company_name, notes=body.notes)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return _to_dict(entry)


@router.delete("/{entry_id}", status_code=204)
def delete_blacklist_entry(
    entry_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    entry = (
        db.query(CompanyBlacklist)
        .filter(CompanyBlacklist.id == entry_id, CompanyBlacklist.user_id == current_user.id)
        .first()
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="Blacklist entry not found")
    db.delete(entry)
    db.commit()

from typing import Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.auth import get_current_user_api
from app.db.database import get_db
from app.models.company_career_page import CompanyCareerPage, ATS_TYPES, detect_ats_type

router = APIRouter(
    prefix="/api/company-pages", tags=["company-pages"],
    dependencies=[Depends(get_current_user_api)],
)


def _to_dict(p: CompanyCareerPage) -> dict:
    return {
        "id": p.id,
        "company": p.company,
        "careers_url": p.careers_url,
        "ats_type": p.ats_type,
        "last_verified_at": p.last_verified_at,
        "active": p.active,
    }


class PageCreate(BaseModel):
    company: str
    careers_url: str
    ats_type: Optional[str] = None  # auto-detected from URL if omitted


class PageUpdate(BaseModel):
    company: Optional[str] = None
    careers_url: Optional[str] = None
    ats_type: Optional[str] = None
    active: Optional[bool] = None


@router.get("")
def list_pages(
    active_only: bool = False,
    ats_type: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(CompanyCareerPage)
    if active_only:
        q = q.filter(CompanyCareerPage.active == True)  # noqa: E712
    if ats_type:
        q = q.filter(CompanyCareerPage.ats_type == ats_type)
    return [_to_dict(p) for p in q.order_by(CompanyCareerPage.company).all()]


@router.get("/{page_id}")
def get_page(page_id: int, db: Session = Depends(get_db)):
    page = db.get(CompanyCareerPage, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="Company page not found")
    return _to_dict(page)


@router.post("", status_code=201)
def upsert_page(body: PageCreate, db: Session = Depends(get_db)):
    """Create or reactivate a company career page by URL.

    If the URL already exists (even if inactive), the row is updated and
    reactivated rather than creating a duplicate.
    """
    ats = body.ats_type or detect_ats_type(body.careers_url)
    if ats not in ATS_TYPES:
        raise HTTPException(status_code=422, detail=f"ats_type must be one of {sorted(ATS_TYPES)}")

    existing = (
        db.query(CompanyCareerPage)
        .filter(CompanyCareerPage.careers_url == body.careers_url)
        .first()
    )
    if existing:
        existing.company = body.company
        existing.ats_type = ats
        existing.active = True
        db.commit()
        return _to_dict(existing)

    page = CompanyCareerPage(company=body.company, careers_url=body.careers_url, ats_type=ats)
    db.add(page)
    db.commit()
    db.refresh(page)
    return _to_dict(page)


@router.patch("/{page_id}")
def update_page(page_id: int, body: PageUpdate, db: Session = Depends(get_db)):
    page = db.get(CompanyCareerPage, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="Company page not found")
    updates = body.model_dump(exclude_none=True)
    if "ats_type" in updates and updates["ats_type"] not in ATS_TYPES:
        raise HTTPException(status_code=422, detail=f"ats_type must be one of {sorted(ATS_TYPES)}")
    for key, value in updates.items():
        setattr(page, key, value)
    db.commit()
    return _to_dict(page)


@router.delete("/{page_id}", status_code=204)
def disable_page(page_id: int, db: Session = Depends(get_db)):
    """Soft-delete: sets active=false. Row is preserved."""
    page = db.get(CompanyCareerPage, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="Company page not found")
    page.active = False
    db.commit()


@router.post("/{page_id}/mark-verified", status_code=200)
def mark_verified(page_id: int, db: Session = Depends(get_db)):
    """Called by the pipeline after a successful crawl of this page."""
    page = db.get(CompanyCareerPage, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="Company page not found")
    page.last_verified_at = datetime.utcnow()
    db.commit()
    return _to_dict(page)

from datetime import datetime
from sqlalchemy import Integer, String, Boolean, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from app.db.database import Base

ATS_TYPES = {"ashby", "workable", "lever", "greenhouse", "custom", "unknown"}


def detect_ats_type(url: str) -> str:
    if "ashbyhq.com" in url:
        return "ashby"
    if "workable.com" in url:
        return "workable"
    if "lever.co" in url:
        return "lever"
    if "greenhouse.io" in url:
        return "greenhouse"
    return "custom"


class CompanyCareerPage(Base):
    __tablename__ = "company_career_pages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company: Mapped[str] = mapped_column(String, nullable=False)
    careers_url: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    ats_type: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

from datetime import date, datetime
from sqlalchemy import Integer, String, Date, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.db.database import Base

VALID_TRANSITIONS: dict[str, set[str]] = {
    "shown":       {"applied", "skipped", "expired"},
    "applied":     {"interviewing", "rejected", "expired"},
    "interviewing": {"offer", "rejected", "expired"},
}
TERMINAL_STATES = {"offer", "rejected", "skipped", "expired"}
ALL_STATES = {"shown", "applied", "interviewing"} | TERMINAL_STATES


class TrackerRecord(Base):
    __tablename__ = "tracker_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    listing_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    company: Mapped[str] = mapped_column(String, nullable=False)
    role_title: Mapped[str] = mapped_column(String, nullable=False)
    apply_url: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    fit_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    date_shown: Mapped[date] = mapped_column(Date, nullable=False)
    date_applied: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    fit_breakdown: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON: {dim: {score, max_score, explanation}}
    quality_flags: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array of flag strings
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

from datetime import datetime
from sqlalchemy import Integer, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column
from app.db.database import Base


class SearchSession(Base):
    """One record per real (non-dry-run) search pipeline run — SPEC.md §3.6."""

    __tablename__ = "search_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    query_params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    listings_found: Mapped[int] = mapped_column(Integer, default=0)
    listings_saved: Mapped[int] = mapped_column(Integer, default=0)
    listings_skipped: Mapped[int] = mapped_column(Integer, default=0)
    skip_reasons: Mapped[dict | None] = mapped_column(JSON, nullable=True)

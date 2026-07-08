from datetime import datetime
from sqlalchemy import Integer, String, JSON, Boolean, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from app.db.database import Base


class SearchEngine(Base):
    __tablename__ = "search_engines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    search_url_template: Mapped[str] = mapped_column(String, nullable=False)
    fetch_strategy: Mapped[str] = mapped_column(String, nullable=False)  # rss | html | api | sitemap
    quirks: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # Per-engine auxiliary config/secrets (e.g. LinkedIn's li_at session cookie).
    # Never sourced from process env — some values are per-user browser session
    # state, not shared app-level config. Distinct from `quirks`, which holds
    # crawl-behavior rules rather than credentials/parameters.
    search_params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

"""
User-configurable search parameters — singleton (id=1).
Stores the defaults applied to every search run unless overridden per-run.
"""
import json
from datetime import datetime
from sqlalchemy import Integer, String, Boolean, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from app.db.database import Base

_DEFAULT_ROLE_TYPES = json.dumps([
    "engineering manager",
    "director of engineering",
    "head of engineering",
])
_DEFAULT_KEYWORDS = json.dumps([])


class SearchParams(Base):
    __tablename__ = "search_params"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Remote type filter: remote | hybrid | onsite | any
    # "any" disables the filter; default allows remote and hybrid
    remote_type: Mapped[str] = mapped_column(String, nullable=False, default="remote_hybrid")

    # Geo preference: worldwide | latam | emea | north_america | any
    geo: Mapped[str] = mapped_column(String, nullable=False, default="worldwide")

    # JSON arrays stored as text
    role_types: Mapped[str] = mapped_column(Text, nullable=False, default=_DEFAULT_ROLE_TYPES)
    keywords: Mapped[str] = mapped_column(Text, nullable=False, default=_DEFAULT_KEYWORDS)

    # Salary floor in USD/year — null means no filter
    salary_min: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)

    # When True, only include roles that offer relocation packages
    relocation_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # When True, only include roles that offer visa/work-permit sponsorship
    visa_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

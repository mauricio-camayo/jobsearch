import json
from datetime import datetime
from sqlalchemy import Integer, String, Boolean, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from app.db.database import Base


class JobListing(Base):
    __tablename__ = "job_listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    company: Mapped[str] = mapped_column(String, nullable=False)
    role_title: Mapped[str] = mapped_column(String, nullable=False)
    apply_url: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str | None] = mapped_column(String, nullable=True)

    # Listing attributes (mirrors ListingInput)
    remote_type: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    geo_restriction: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    relocation_offered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    visa_sponsorship: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    required_skills: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # JSON array
    role_domains: Mapped[str] = mapped_column(Text, nullable=False, default="[]")     # JSON array
    aggregator_remote_type: Mapped[str | None] = mapped_column(String, nullable=True)

    # Pipeline outputs
    quality_flags: Mapped[str] = mapped_column(Text, nullable=False, default="[]")   # JSON array
    fit_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verified_active: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Lifecycle
    status: Mapped[str] = mapped_column(String, nullable=False, default="new")
    # status values: new | shown | skipped | pipeline_run
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def to_listing_input_dict(self) -> dict:
        """Return fields in ListingInput-compatible shape for the pipeline."""
        return {
            "company": self.company,
            "role_title": self.role_title,
            "apply_url": self.apply_url,
            "remote_type": self.remote_type,
            "geo": self.geo_restriction,
            "description": self.description or "",
            "required_skills": json.loads(self.required_skills or "[]"),
            "role_domains": json.loads(self.role_domains or "[]"),
            "relocation_offered": self.relocation_offered,
            "visa_sponsorship": self.visa_sponsorship,
            "aggregator_remote_type": self.aggregator_remote_type,
        }

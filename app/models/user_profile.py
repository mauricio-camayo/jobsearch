from datetime import datetime
from sqlalchemy import Integer, String, JSON, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from app.db.database import Base


class UserProfile(Base):
    __tablename__ = "user_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False)
    skills: Mapped[list] = mapped_column(JSON, nullable=False)
    experience_years: Mapped[int] = mapped_column(Integer, nullable=False)
    seniority: Mapped[str] = mapped_column(String, nullable=False)
    domains: Mapped[list] = mapped_column(JSON, nullable=False)
    resume_path: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

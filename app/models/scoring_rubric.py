from sqlalchemy import Integer, String, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from app.db.database import Base

DIMENSIONS = {"domain_match", "tech_stack", "seniority", "remote_geo", "relocation_visa_bonus"}


class ScoringRubric(Base):
    __tablename__ = "scoring_rubric"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dimension: Mapped[str] = mapped_column(String, nullable=False)
    weight: Mapped[int] = mapped_column(Integer, nullable=False)
    is_bonus: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

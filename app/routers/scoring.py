from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.auth import get_current_user_api
from app.db.database import get_db
from app.models.scoring_rubric import ScoringRubric
from app.models.app_config import AppConfig
from app.models.user import User
from app.models.user_profile import UserProfile
from app.services.scorer import score_job

router = APIRouter(tags=["scoring"])


# ── Rubric CRUD ───────────────────────────────────────────────────────────────

def _rubric_to_dict(r: ScoringRubric) -> dict:
    return {"id": r.id, "dimension": r.dimension, "weight": r.weight, "is_bonus": r.is_bonus}


def _non_bonus_sum(db: Session, user_id: int, exclude_dimension: str | None = None) -> int:
    rows = (
        db.query(ScoringRubric)
        .filter(ScoringRubric.user_id == user_id, ScoringRubric.is_bonus == False)  # noqa: E712
        .all()
    )
    return sum(r.weight for r in rows if r.dimension != exclude_dimension)


@router.get("/api/scoring-rubric")
def list_rubric(db: Session = Depends(get_db), current_user: User = Depends(get_current_user_api)):
    return [
        _rubric_to_dict(r)
        for r in db.query(ScoringRubric).filter_by(user_id=current_user.id).order_by(ScoringRubric.id).all()
    ]


class RubricBulkUpdate(BaseModel):
    # Map of dimension → new weight (non-bonus only)
    weights: dict[str, int]


@router.put("/api/scoring-rubric")
def replace_rubric(
    body: RubricBulkUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    """Replace all non-bonus weights in one transaction. Sum must equal 100."""
    total = sum(body.weights.values())
    if total != 100:
        raise HTTPException(
            status_code=422,
            detail=f"Non-bonus weights must sum to 100; got {total}",
        )
    for dimension, weight in body.weights.items():
        row = (
            db.query(ScoringRubric)
            .filter(ScoringRubric.user_id == current_user.id, ScoringRubric.dimension == dimension)
            .first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"Dimension '{dimension}' not found")
        if row.is_bonus:
            raise HTTPException(
                status_code=422,
                detail=f"Dimension '{dimension}' is a bonus — use PATCH to update it separately",
            )
        row.weight = weight
    db.commit()
    return [
        _rubric_to_dict(r)
        for r in db.query(ScoringRubric).filter_by(user_id=current_user.id).order_by(ScoringRubric.id).all()
    ]


class DimensionUpdate(BaseModel):
    weight: int


@router.patch("/api/scoring-rubric/{dimension}")
def update_dimension(
    dimension: str,
    body: DimensionUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    """Update a single dimension weight. Validates non-bonus sum stays at 100."""
    row = (
        db.query(ScoringRubric)
        .filter(ScoringRubric.user_id == current_user.id, ScoringRubric.dimension == dimension)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Dimension '{dimension}' not found")
    if not row.is_bonus:
        new_sum = _non_bonus_sum(db, current_user.id, exclude_dimension=dimension) + body.weight
        if new_sum != 100:
            raise HTTPException(
                status_code=422,
                detail=f"Updating '{dimension}' to {body.weight} gives non-bonus sum of {new_sum}, must be 100",
            )
    row.weight = body.weight
    db.commit()
    return _rubric_to_dict(row)


# ── Score endpoint ────────────────────────────────────────────────────────────

class ScoreRequest(BaseModel):
    role_title: str
    description: Optional[str] = ""
    required_skills: Optional[list[str]] = []
    role_domains: Optional[list[str]] = []
    remote_type: Optional[str] = "unknown"
    geo_restriction: Optional[str] = "unknown"
    relocation_offered: Optional[bool] = False
    visa_sponsorship: Optional[bool] = False


@router.post("/api/score")
def score_listing(
    body: ScoreRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    profile = db.query(UserProfile).filter_by(user_id=current_user.id).first()
    if profile is None:
        raise HTTPException(status_code=500, detail="UserProfile not seeded")

    rubric = db.query(ScoringRubric).filter_by(user_id=current_user.id).all()
    config = db.query(AppConfig).filter_by(user_id=current_user.id).first()
    threshold = config.fit_autosave_threshold if config else 70

    result = score_job(
        profile=profile,
        rubric=rubric,
        threshold=threshold,
        role_title=body.role_title,
        description=body.description or "",
        required_skills=body.required_skills or [],
        role_domains=body.role_domains or [],
        remote_type=body.remote_type or "unknown",
        geo_restriction=body.geo_restriction or "unknown",
        relocation_offered=body.relocation_offered or False,
        visa_sponsorship=body.visa_sponsorship or False,
    )

    return {
        "total_score": result.total_score,
        "exceeds_threshold": result.exceeds_threshold,
        "threshold": result.threshold,
        "breakdown": {
            dim: {
                "score": ds.score,
                "max": ds.max_score,
                "explanation": ds.explanation,
            }
            for dim, ds in result.breakdown.items()
        },
    }

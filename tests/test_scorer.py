"""
Unit tests for app/services/scorer.py — score_job() directly.
No database required; profile and rubric are plain namespace objects
(score_job only reads attributes — no ORM machinery needed).

T1  — empty description/domains/skills → domain_match=0, tech_stack=0, total<40
T2  — full domain overlap → domain_match equals rubric weight
T3  — partial domain match → domain_match proportional
T4  — provided_skills set-intersection scores correctly
T5  — seniority full match on "engineering manager"
T6  — seniority partial match on "manager" (not full title)
T7  — tech_stack text-scan returns 0 when description=""
T8  — remote=remote, geo=worldwide → remote_geo full score
T9  — remote=onsite → remote_geo zero
T10 — relocation_offered=True triggers bonus score
"""
import sys
import os
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.scorer import score_job


def _make_profile(**overrides) -> SimpleNamespace:
    """Return a namespace object with the attributes score_job() reads."""
    defaults = dict(
        skills=["Golang", "Python", "AWS", "Kubernetes", "PostgreSQL"],
        seniority="senior",
        domains=["payments", "fintech", "security", "platform", "distributed systems"],
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_rubric(weights: dict | None = None) -> list[SimpleNamespace]:
    """Return a list of namespace objects with the attributes score_job() reads."""
    defaults = {
        "domain_match": (35, False),
        "tech_stack": (30, False),
        "seniority": (25, False),
        "remote_geo": (10, False),
        "relocation_visa_bonus": (10, True),
    }
    if weights:
        for dim, w in weights.items():
            bonus = defaults[dim][1]
            defaults[dim] = (w, bonus)
    return [
        SimpleNamespace(dimension=dim, weight=weight, is_bonus=is_bonus)
        for dim, (weight, is_bonus) in defaults.items()
    ]


# T1 — empty payload: domain_match=0, tech_stack=0, total<40
def test_t1_empty_fields_zero_heavy_dims():
    profile = _make_profile()
    rubric = _make_rubric()
    result = score_job(
        profile=profile,
        rubric=rubric,
        threshold=70,
        role_title="Engineering Manager",
        description="",
        required_skills=[],
        role_domains=[],
    )
    assert result.breakdown["domain_match"].score == 0
    assert result.breakdown["tech_stack"].score == 0
    assert result.total_score < 40


# T2 — all profile domains present → full domain_match weight
def test_t2_full_domain_match():
    profile = _make_profile(domains=["payments", "fintech"])
    rubric = _make_rubric()
    result = score_job(
        profile=profile,
        rubric=rubric,
        threshold=70,
        role_title="EM",
        description="We build payments and fintech products",
        required_skills=[],
        role_domains=["payments", "fintech"],
    )
    assert result.breakdown["domain_match"].score == 35


# T3 — partial domain overlap → proportional score
def test_t3_partial_domain_match():
    profile = _make_profile(domains=["payments", "fintech", "security"])
    rubric = _make_rubric()
    result = score_job(
        profile=profile,
        rubric=rubric,
        threshold=70,
        role_title="EM",
        description="payments platform",
        required_skills=[],
        role_domains=["payments"],
    )
    dm = result.breakdown["domain_match"]
    # 1 of 3 domains → ratio=1/3 → round(35/3) = 12
    assert dm.score == round(35 / 3)


# T4 — provided_skills set-intersection counts correctly
def test_t4_provided_skills_intersection():
    profile = _make_profile(skills=["Golang", "Python", "AWS"])
    rubric = _make_rubric()
    result = score_job(
        profile=profile,
        rubric=rubric,
        threshold=70,
        role_title="EM",
        description="",
        required_skills=["Golang", "Python", "Java", "Rust"],
        role_domains=[],
    )
    ts = result.breakdown["tech_stack"]
    # 2 matches out of 4 required → ratio=0.5 → round(0.5*30)=15
    assert ts.score == 15


# T5 — seniority: full match on "engineering manager" in title
def test_t5_seniority_full_match():
    profile = _make_profile(seniority="senior")
    rubric = _make_rubric()
    result = score_job(
        profile=profile,
        rubric=rubric,
        threshold=70,
        role_title="Senior Engineering Manager, Payments",
        description="",
        required_skills=[],
        role_domains=[],
    )
    assert result.breakdown["seniority"].score == 25


# T6 — seniority: partial match (title has "manager" but not the full signal)
def test_t6_seniority_partial_match():
    profile = _make_profile(seniority="senior")
    rubric = _make_rubric()
    result = score_job(
        profile=profile,
        rubric=rubric,
        threshold=70,
        role_title="Product Manager",
        description="",
        required_skills=[],
        role_domains=[],
    )
    seniority = result.breakdown["seniority"]
    # partial match → round(25 * 0.6) = 15
    assert seniority.score == round(25 * 0.6)


# T7 — tech_stack text-scan returns 0 when description="" and no skills provided
def test_t7_tech_stack_empty_description_zero():
    profile = _make_profile(skills=["Golang", "Python", "AWS"])
    rubric = _make_rubric()
    result = score_job(
        profile=profile,
        rubric=rubric,
        threshold=70,
        role_title="EM",
        description="",
        required_skills=[],
        role_domains=[],
    )
    assert result.breakdown["tech_stack"].score == 0


# T8 — remote=remote, geo=worldwide → full remote_geo weight
def test_t8_remote_geo_full():
    profile = _make_profile()
    rubric = _make_rubric()
    result = score_job(
        profile=profile,
        rubric=rubric,
        threshold=70,
        role_title="EM",
        description="",
        required_skills=[],
        role_domains=[],
        remote_type="remote",
        geo_restriction="worldwide",
    )
    assert result.breakdown["remote_geo"].score == 10


# T9 — remote=onsite → remote_geo zero (ratio=0.0)
def test_t9_remote_geo_onsite_zero():
    profile = _make_profile()
    rubric = _make_rubric()
    result = score_job(
        profile=profile,
        rubric=rubric,
        threshold=70,
        role_title="EM",
        description="",
        required_skills=[],
        role_domains=[],
        remote_type="onsite",
        geo_restriction="worldwide",
    )
    assert result.breakdown["remote_geo"].score == 0


# T10 — relocation_offered=True triggers bonus score
def test_t10_relocation_bonus():
    profile = _make_profile()
    rubric = _make_rubric()
    result = score_job(
        profile=profile,
        rubric=rubric,
        threshold=70,
        role_title="EM",
        description="",
        required_skills=[],
        role_domains=[],
        relocation_offered=True,
    )
    assert result.breakdown["relocation_visa_bonus"].score == 10

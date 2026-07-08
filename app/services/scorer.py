"""
Fit scoring service.

Scores a job listing against the UserProfile using weights from the
ScoringRubric table. Each dimension produces a partial score; bonuses
are additive and may push the total above 100.
"""
from dataclasses import dataclass, field
from app.models.user_profile import UserProfile
from app.models.scoring_rubric import ScoringRubric

# Common skill aliases so profile entries match their alternate names in job descriptions
_SKILL_ALIASES: dict[str, list[str]] = {
    "golang": ["golang", "go language", " go,", " go ", "go/"],
    "javascript": ["javascript", "js", "node.js", "nodejs"],
    "aws": ["aws", "amazon web services", "amazon cloud"],
    "bash": ["bash", "shell scripting", "unix shell"],
}

# Seniority signals per profile level (ordered most → least specific)
_SENIORITY_SIGNALS: dict[str, list[str]] = {
    "senior": [
        "engineering manager", "director of engineering", "head of engineering",
        "vp of engineering", "vp, engineering", "vice president of engineering",
        "director", "head of", "vp ", "senior manager", "group manager",
        "tech lead", "technical lead", "platform lead", "tpm", "program manager",
    ],
    "director": [
        "director", "head of", "vp", "vice president", "group engineering manager",
    ],
    "vp": ["vp ", "vice president", "chief", "cto"],
    "staff": ["staff engineer", "principal", "distinguished"],
}

# User prefers remote/worldwide — these ratios reflect that preference
_REMOTE_RATIO = {"remote": 1.0, "hybrid": 0.6, "onsite": 0.0, "unknown": 0.8}
_GEO_RATIO = {
    "worldwide": 1.0, "latam": 1.0, "emea": 0.9,
    "north_america": 0.7, "usa": 0.5, "brazil": 0.8,
    "unknown": 0.8,
}


@dataclass
class DimensionScore:
    score: int
    max_score: int
    explanation: str


@dataclass
class ScoreResult:
    total_score: int
    exceeds_threshold: bool
    threshold: int
    breakdown: dict[str, DimensionScore] = field(default_factory=dict)


def _score_domain_match(
    text: str, profile_domains: list[str], weight: int
) -> DimensionScore:
    text_lower = text.lower()
    matches = [d for d in profile_domains if d.lower() in text_lower]
    ratio = len(matches) / max(len(profile_domains), 1)
    score = round(ratio * weight)
    return DimensionScore(
        score=score,
        max_score=weight,
        explanation=f"{len(matches)}/{len(profile_domains)} profile domains found: {matches or 'none'}",
    )


def _skill_matches_text(skill: str, text_lower: str) -> bool:
    """Check if a skill appears in text, including common aliases."""
    skill_lower = skill.lower()
    if skill_lower in text_lower:
        return True
    for alias in _SKILL_ALIASES.get(skill_lower, []):
        if alias in text_lower:
            return True
    return False


def _score_tech_stack(
    text: str,
    provided_skills: list[str],
    profile_skills: list[str],
    weight: int,
) -> DimensionScore:
    if provided_skills:
        target_lower = {s.lower() for s in provided_skills}
        matches = [s for s in profile_skills if s.lower() in target_lower]
        ratio = len(matches) / max(len(provided_skills), 1)
        explanation = f"{len(matches)} profile skills match {len(provided_skills)} listed requirements"
    else:
        text_lower = text.lower()
        matches = [s for s in profile_skills if _skill_matches_text(s, text_lower)]
        # Not all skills appear in descriptions; scale denominator down
        ratio = min(len(matches) / max(len(profile_skills) * 0.35, 1), 1.0)
        explanation = f"{len(matches)} profile skills found in description text"
    score = round(min(ratio, 1.0) * weight)
    return DimensionScore(score=score, max_score=weight, explanation=explanation)


def _score_seniority(
    role_title: str,
    profile_seniority: str,
    weight: int,
    seniority_hint: str | None = None,
) -> DimensionScore:
    title_lower = role_title.lower()
    signals = _SENIORITY_SIGNALS.get(profile_seniority, [])

    # Structured signal (e.g. LinkedIn's "Seniority level" criteria field) takes
    # priority over the title-regex heuristic — it's a direct ATS-provided value
    # rather than an inference, but only checked ahead of, not instead of, the
    # title match: falls back to title/partial matching when absent or unmatched.
    hint_lower = seniority_hint.lower() if seniority_hint else ""
    if hint_lower:
        for signal in signals:
            if signal in hint_lower:
                return DimensionScore(
                    score=weight,
                    max_score=weight,
                    explanation=f"strong match on structured seniority signal '{seniority_hint}' (matched '{signal}')",
                )

    for signal in signals:
        if signal in title_lower:
            return DimensionScore(
                score=weight,
                max_score=weight,
                explanation=f"strong match on '{signal}'",
            )
    # Partial match for adjacent seniority signals
    for partial in ["manager", "lead", "tpm", "senior", "head"]:
        if partial in title_lower or partial in hint_lower:
            score = round(weight * 0.6)
            return DimensionScore(
                score=score,
                max_score=weight,
                explanation=f"partial match on '{partial}' ({score}/{weight})",
            )
    return DimensionScore(score=0, max_score=weight, explanation="no seniority signals matched")


def _score_remote_geo(
    remote_type: str, geo_restriction: str, weight: int
) -> DimensionScore:
    r = _REMOTE_RATIO.get(remote_type, 0.8)
    g = _GEO_RATIO.get(geo_restriction, 0.8)
    score = round(r * g * weight)
    return DimensionScore(
        score=score,
        max_score=weight,
        explanation=f"remote={remote_type}({r:.0%}), geo={geo_restriction}({g:.0%})",
    )


def _score_relocation_visa_bonus(
    relocation_offered: bool, visa_sponsorship: bool, weight: int
) -> DimensionScore:
    triggered = relocation_offered or visa_sponsorship
    reasons = []
    if relocation_offered:
        reasons.append("relocation offered")
    if visa_sponsorship:
        reasons.append("visa sponsorship")
    return DimensionScore(
        score=weight if triggered else 0,
        max_score=weight,
        explanation=", ".join(reasons) if reasons else "neither relocation nor visa offered",
    )


def score_job(
    profile: UserProfile,
    rubric: list[ScoringRubric],
    threshold: int,
    role_title: str,
    description: str = "",
    required_skills: list[str] | None = None,
    role_domains: list[str] | None = None,
    remote_type: str = "unknown",
    geo_restriction: str = "unknown",
    relocation_offered: bool = False,
    visa_sponsorship: bool = False,
    seniority_hint: str | None = None,
) -> ScoreResult:
    required_skills = required_skills or []
    role_domains = role_domains or []
    text = f"{role_title} {description} {' '.join(role_domains)}".strip()

    weights = {row.dimension: (row.weight, row.is_bonus) for row in rubric}
    breakdown: dict[str, DimensionScore] = {}

    def _w(dim: str) -> int:
        return weights.get(dim, (0, False))[0]

    breakdown["domain_match"] = _score_domain_match(text, profile.domains, _w("domain_match"))
    breakdown["tech_stack"] = _score_tech_stack(
        description, required_skills, profile.skills, _w("tech_stack")
    )
    breakdown["seniority"] = _score_seniority(
        role_title, profile.seniority, _w("seniority"), seniority_hint
    )
    breakdown["remote_geo"] = _score_remote_geo(remote_type, geo_restriction, _w("remote_geo"))
    breakdown["relocation_visa_bonus"] = _score_relocation_visa_bonus(
        relocation_offered, visa_sponsorship, _w("relocation_visa_bonus")
    )

    total = sum(d.score for d in breakdown.values())

    return ScoreResult(
        total_score=total,
        exceeds_threshold=total >= threshold,
        threshold=threshold,
        breakdown=breakdown,
    )

"""
Remote / geo / relocation / visa filter service — §6 of the functional spec.

Each filter function takes the job's declared attributes and the user's current
SearchParams, and returns a FilterResult: pass=True means "include this role",
pass=False means "skip it (unless overridden)", plus a list of flags that are
surfaced in the tracker note regardless of pass/fail.

P3-12 override: a geo-restricted role that explicitly offers relocation or visa
sponsorship must NOT be silently excluded — it is included with a ⭐ flag.
"""
from dataclasses import dataclass, field
from typing import Literal

# ── Constants ─────────────────────────────────────────────────────────────────

REMOTE_TYPES = {"remote", "hybrid", "onsite", "unknown"}
GEO_VALUES = {"worldwide", "latam", "emea", "north_america", "usa", "brazil", "unknown"}

# remote_type filter values accepted by SearchParams.remote_type
REMOTE_FILTER_VALUES = {"remote", "hybrid", "remote_hybrid", "onsite", "any"}

# Remote-type combinations that "pass" for each filter setting
_REMOTE_PASS: dict[str, set[str]] = {
    "remote":        {"remote"},
    "hybrid":        {"hybrid"},
    "remote_hybrid": {"remote", "hybrid"},
    "onsite":        {"onsite"},
    "any":           REMOTE_TYPES,
}

# Geo regions considered "geo-restricted" (not worldwide-accessible by default)
_GEO_RESTRICTED = {"usa", "brazil", "emea", "latam", "north_america"}

# Geo regions compatible with each user-preference setting
_GEO_PASS: dict[str, set[str]] = {
    # "worldwide" = no restriction — pass all, but geo-restricted roles still get flagged
    "worldwide":     GEO_VALUES,
    "latam":         {"worldwide", "latam", "unknown"},
    "emea":          {"worldwide", "emea", "unknown"},
    "north_america": {"worldwide", "north_america", "latam", "unknown"},
    "any":           GEO_VALUES,
}

# Human-readable flag labels
_GEO_FLAG_LABELS: dict[str, str] = {
    "usa":           "usa-remote-only",
    "brazil":        "brazil-only",
    "emea":          "emea-only",
    "latam":         "latam-only",
    "north_america": "north-america-only",
}


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class FilterResult:
    passed: bool
    flags: list[str] = field(default_factory=list)
    skip_reason: str | None = None
    override_reason: str | None = None  # set when P3-12 relocation/visa overrides a geo skip


# ── Remote filter (P3-9) ─────────────────────────────────────────────────────

def apply_remote_filter(
    job_remote_type: str,          # remote | hybrid | onsite | unknown
    search_remote_type: str,       # from SearchParams.remote_type
    aggregator_remote_type: str | None = None,  # declared by aggregator (may differ from ATS)
) -> FilterResult:
    """
    Filter by remote type. Flags 'remote-mismatch' when an aggregator marks a
    role 'remote' but the ATS reveals it is 'hybrid' or 'onsite'.
    """
    job_rt = job_remote_type.lower() if job_remote_type else "unknown"
    filter_rt = search_remote_type.lower() if search_remote_type else "remote_hybrid"

    allowed = _REMOTE_PASS.get(filter_rt, _REMOTE_PASS["remote_hybrid"])
    flags: list[str] = []

    # Detect aggregator mismatch before applying filter
    if aggregator_remote_type and aggregator_remote_type.lower() != job_rt:
        agg_rt = aggregator_remote_type.lower()
        if agg_rt == "remote" and job_rt in {"hybrid", "onsite"}:
            flags.append("remote-mismatch")

    if job_rt in allowed or job_rt == "unknown":
        return FilterResult(passed=True, flags=flags)

    return FilterResult(
        passed=False,
        flags=flags,
        skip_reason=f"remote_type '{job_rt}' excluded by filter '{filter_rt}'",
    )


# ── Geo filter (P3-10 + P3-12 override) ──────────────────────────────────────

def apply_geo_filter(
    job_geo: str,                  # worldwide | latam | emea | north_america | usa | brazil | unknown
    search_geo: str,               # from SearchParams.geo
    relocation_offered: bool = False,
    visa_sponsorship: bool = False,
    relocation_required: bool = False,  # from SearchParams
    visa_required: bool = False,        # from SearchParams
) -> FilterResult:
    """
    Filter by geo restriction.

    P3-12 override: if the job is geo-restricted but offers relocation or visa
    sponsorship, include it with a ⭐ note rather than skipping silently.
    """
    job_g = job_geo.lower() if job_geo else "unknown"
    filter_g = search_geo.lower() if search_geo else "worldwide"

    flags: list[str] = []

    # Flag geo-restricted roles regardless of pass/fail
    if job_g in _GEO_RESTRICTED:
        flags.append(_GEO_FLAG_LABELS.get(job_g, f"{job_g}-only"))

    if relocation_offered:
        flags.append("relocation-offered")
    if visa_sponsorship:
        flags.append("visa-sponsorship")

    # Apply relocation/visa requirement filters first
    if relocation_required and not relocation_offered:
        return FilterResult(
            passed=False,
            flags=flags,
            skip_reason="relocation_required=True but role does not offer relocation",
        )
    if visa_required and not visa_sponsorship:
        return FilterResult(
            passed=False,
            flags=flags,
            skip_reason="visa_required=True but role does not offer visa sponsorship",
        )

    allowed = _GEO_PASS.get(filter_g, _GEO_PASS["worldwide"])
    if job_g in allowed or job_g == "unknown":
        return FilterResult(passed=True, flags=flags)

    # Job fails geo filter — check for P3-12 relocation/visa override
    if relocation_offered or visa_sponsorship:
        override = []
        if relocation_offered:
            override.append("relocation package offered")
        if visa_sponsorship:
            override.append("visa sponsorship offered")
        flags.insert(0, "⭐ high — " + " + ".join(override))
        return FilterResult(
            passed=True,
            flags=flags,
            override_reason=(
                f"geo '{job_g}' would be excluded by filter '{filter_g}' "
                f"but included due to: {', '.join(override)}"
            ),
        )

    return FilterResult(
        passed=False,
        flags=flags,
        skip_reason=f"geo '{job_g}' excluded by filter '{filter_g}'",
    )


# ── Combined filter (convenience wrapper) ────────────────────────────────────

def apply_all_filters(
    job_remote_type: str,
    job_geo: str,
    search_remote_type: str,
    search_geo: str,
    relocation_offered: bool = False,
    visa_sponsorship: bool = False,
    relocation_required: bool = False,
    visa_required: bool = False,
    aggregator_remote_type: str | None = None,
) -> FilterResult:
    """
    Run remote filter then geo filter. First failure short-circuits.
    Flags are accumulated from both filters.
    """
    remote_result = apply_remote_filter(job_remote_type, search_remote_type, aggregator_remote_type)
    if not remote_result.passed:
        return remote_result

    geo_result = apply_geo_filter(
        job_geo, search_geo,
        relocation_offered, visa_sponsorship,
        relocation_required, visa_required,
    )
    # Merge flags from both checks
    combined_flags = remote_result.flags + [f for f in geo_result.flags if f not in remote_result.flags]
    return FilterResult(
        passed=geo_result.passed,
        flags=combined_flags,
        skip_reason=geo_result.skip_reason,
        override_reason=geo_result.override_reason,
    )

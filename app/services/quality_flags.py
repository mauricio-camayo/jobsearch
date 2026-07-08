"""
Listing quality flag detection — §5 pipeline step 5, P3-13.

Detects two signal types from listing title + description text:
  - internal-only: role is not open to external applicants → skip
  - cover-role: temporary maternity/parental/contract cover → flag with urgency note

The remote-mismatch flag is handled separately in app/services/filters.py.
"""
import re
from dataclasses import dataclass, field

# ── Signal patterns ───────────────────────────────────────────────────────────

_INTERNAL_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\binternal\s+(transfer|posting|candidates?|applicants?|only|vacancy|vacancies)\b",
        r"\bnot\s+open\s+to\s+external\b",
        r"\bcurrent\s+(employees?|staff)\s+only\b",
        r"\bfor\s+internal\s+(use|applications?|applicants?)\b",
        r"\bopen\s+to\s+internal\b",
        r"\binternal\s+job\s+posting\b",
        r"\bemployees?\s+only\b",
    ]
]

_COVER_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\bmaternity\s+(leave\s+)?cover\b",
        r"\bpaternity\s+(leave\s+)?cover\b",
        r"\bparental\s+(leave\s+)?cover\b",
        r"\bcover(ing)?\s+for\s+(maternity|paternity|parental)\b",
        r"\btemporary\s+cover\b",
        r"\bcover\s+role\b",
        r"\bfixed[- ]term\s+(contract\s+)?\(?(\d+[- ]months?)\)?\b",
        r"\b(\d+[- ]months?)\s+fixed[- ]term\b",
    ]
]

# Duration hints extracted from cover patterns (for the urgency note)
_DURATION_RE = re.compile(r"\b(\d+)[- ]months?\b", re.IGNORECASE)


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class QualityFlagResult:
    flags: list[str] = field(default_factory=list)
    skip: bool = False
    skip_reason: str | None = None
    urgency_note: str | None = None


# ── Detection functions ───────────────────────────────────────────────────────

def _check_text(text: str, patterns: list[re.Pattern]) -> re.Match | None:
    for pat in patterns:
        m = pat.search(text)
        if m:
            return m
    return None


def check_quality_flags(title: str, description: str = "") -> QualityFlagResult:
    """
    Analyse listing title and description for quality signals.
    Returns QualityFlagResult with flags, skip decision, and any urgency note.
    """
    combined = f"{title} {description}"
    result = QualityFlagResult()

    # Internal-only check
    internal_match = _check_text(combined, _INTERNAL_PATTERNS)
    if internal_match:
        result.flags.append("internal-only")
        result.skip = True
        result.skip_reason = f"internal-only posting detected: '{internal_match.group(0)}'"

    # Cover role check (independent of internal check)
    cover_match = _check_text(combined, _COVER_PATTERNS)
    if cover_match:
        result.flags.append("cover-role")
        duration_m = _DURATION_RE.search(combined)
        if duration_m:
            months = duration_m.group(1)
            result.urgency_note = f"Cover role — fixed-term {months}-month position; apply before it fills."
        else:
            result.urgency_note = "Cover role — temporary position; deadline likely sooner than it appears."

    return result

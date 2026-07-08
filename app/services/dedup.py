"""
Duplicate suppression service — §8 of the functional spec.

Runs two independent checks before a listing is saved to the tracker:
  Check A — exact URL match
  Check B — normalized company + role title match (only if A didn't match)

Decision table:
  applied / interviewing / offer / skipped / rejected → skip
  shown     → skip (already in front of user)
  expired   → resurface as new 'shown' with a note
  no match  → save
"""
import re
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from app.models.tracker_record import TrackerRecord

# ── Normalisation helpers ─────────────────────────────────────────────────────

_GEO_SUFFIX = re.compile(
    r"\s*[—–\-]\s*(?:EMEA|LATAM|Remote|Worldwide|North America|USA|Brazil|UK|Europe|APAC|Colombia|Argentina|Mexico)",
    re.IGNORECASE,
)
_PAREN_REMOTE = re.compile(
    r"\s*\([^)]*(?:Remote|Worldwide|Hybrid|EMEA|LATAM|USA)[^)]*\)",
    re.IGNORECASE,
)
_DATE_TOKEN = re.compile(
    r"\s*[\(\[][^\)\]]*\d{4}[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
_COMPANY_SUFFIX = re.compile(
    r"\s*(?:,\s*)?(?:Inc\.?|LLC\.?|Ltd\.?|Corp\.?|S\.A\.?|SAS|GmbH|B\.V\.?)$",
    re.IGNORECASE,
)


def _normalize_title(title: str) -> str:
    t = title
    t = _GEO_SUFFIX.sub("", t)
    t = _PAREN_REMOTE.sub("", t)
    t = _DATE_TOKEN.sub("", t)
    return t.strip().lower()


def _normalize_company(company: str) -> str:
    c = _COMPANY_SUFFIX.sub("", company)
    return c.strip().lower()


# ── Result type ───────────────────────────────────────────────────────────────

Action = Literal["save", "skip", "resurface"]

RESURFACE_NOTE = "Previously expired — re-appeared active"

_SKIP_STATUSES = {"applied", "interviewing", "offer", "skipped", "rejected", "shown"}


@dataclass
class DedupResult:
    action: Action
    reason: str
    check: Literal["url", "title", None]
    matched_record_id: int | None = None
    matched_status: str | None = None
    resurface_note: str | None = None


# ── Core check ────────────────────────────────────────────────────────────────

def _decide(record: TrackerRecord, check: Literal["url", "title"]) -> DedupResult:
    status = record.status
    if status in _SKIP_STATUSES:
        return DedupResult(
            action="skip",
            reason=f"Check {check.upper()}: existing record #{record.id} has status '{status}'",
            check=check,
            matched_record_id=record.id,
            matched_status=status,
        )
    if status == "expired":
        return DedupResult(
            action="resurface",
            reason=f"Check {check.upper()}: existing record #{record.id} was expired — re-appeared active",
            check=check,
            matched_record_id=record.id,
            matched_status=status,
            resurface_note=RESURFACE_NOTE,
        )
    # Unknown status — treat conservatively as skip
    return DedupResult(
        action="skip",
        reason=f"Check {check.upper()}: matched record #{record.id} with status '{status}'",
        check=check,
        matched_record_id=record.id,
        matched_status=status,
    )


def check_duplicate(
    db: Session,
    company: str,
    role_title: str,
    user_id: int,
    apply_url: str | None = None,
) -> DedupResult:
    """
    Run Check A (URL) then Check B (title), scoped to the given user. Return the
    first match decision, or action='save' if no match found.
    """
    # Check A — exact URL match
    if apply_url:
        record = (
            db.query(TrackerRecord)
            .filter(TrackerRecord.apply_url == apply_url, TrackerRecord.user_id == user_id)
            .first()
        )
        if record:
            return _decide(record, "url")

    # Check B — normalised company + title match (skip if URL already matched)
    norm_company = _normalize_company(company)
    norm_title = _normalize_title(role_title)

    candidates = db.query(TrackerRecord).filter(TrackerRecord.user_id == user_id).all()
    for record in candidates:
        if (
            _normalize_company(record.company) == norm_company
            and _normalize_title(record.role_title) == norm_title
        ):
            return _decide(record, "title")

    return DedupResult(
        action="save",
        reason="No match in Check A (URL) or Check B (title)",
        check=None,
    )

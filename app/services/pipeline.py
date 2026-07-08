"""
Search pipeline orchestrator — §5 of the functional spec, P3-14.

Two-phase execution:
  Phase 1 (parallel) — verify all listing URLs concurrently via asyncio + semaphore.
                        HTTP I/O is the bottleneck; this is where the parallelism matters.
  Phase 2 (sequential) — for each verified-active listing, run:
                          quality flags → filter → dedup → score → save/skip
  Phase 3 (post-pipeline) — auto-bulk-skip any shown record with
                             fit_pct < floor(threshold * 2/3) (#37).
  Phase 4 (post-pipeline) — expire shown records with date_shown older than
                             shown_expiry_days (default 30) (#21).

DB writes stay sequential to avoid SQLite concurrent-write conflicts.
"""
import asyncio
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from sqlalchemy.orm import Session

from app.models.tracker_record import TrackerRecord
from app.models.user_profile import UserProfile
from app.models.scoring_rubric import ScoringRubric
from app.models.app_config import AppConfig
from app.models.search_params import SearchParams
from app.models.company_blacklist import CompanyBlacklist
from app.models.search_session import SearchSession
from app.services.verifier import verify_url
from app.services.quality_flags import check_quality_flags
from app.services.filters import apply_all_filters
from app.services.dedup import check_duplicate, _normalize_company
from app.services.scorer import score_job

_VERIFY_CONCURRENCY = 5  # max parallel HTTP connections


# ── Input / output types ──────────────────────────────────────────────────────

@dataclass
class ListingInput:
    company: str
    role_title: str
    apply_url: str | None = None
    remote_type: str = "unknown"
    geo: str = "unknown"
    description: str = ""
    required_skills: list[str] = field(default_factory=list)
    role_domains: list[str] = field(default_factory=list)
    relocation_offered: bool = False
    visa_sponsorship: bool = False
    aggregator_remote_type: str | None = None  # for remote-mismatch detection
    engine_quirks: dict | None = None  # passed through to verifier
    seniority_hint: str | None = None  # structured seniority signal (e.g. LinkedIn's criteria)

    @classmethod
    def from_dict(cls, d: dict) -> "ListingInput":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


Decision = Literal["saved", "skipped", "resurfaced"]
SkipCategory = Literal["verify_inactive", "verify_skip", "quality_flags", "filter", "dedup", "below_threshold"]


@dataclass
class ListingResult:
    company: str
    role_title: str
    apply_url: str | None
    decision: Decision | str
    reason: str
    flags: list[str]
    fit_pct: int | None = None
    tracker_id: int | None = None
    skip_category: SkipCategory | str | None = None
    urgency_note: str | None = None
    fit_breakdown: dict | None = None  # per-dimension breakdown for manual pipeline display (#42)


@dataclass
class PipelineSession:
    submitted: int
    saved: int
    skipped: int
    resurfaced: int
    skip_reasons: dict[str, int]
    results: list[ListingResult]
    auto_removed: int = 0   # listings auto-bulk-skipped at floor(threshold*2/3) — #37
    auto_expired: int = 0   # shown listings expired after shown_expiry_days — #21


# ── Phase 1: parallel verification ───────────────────────────────────────────

async def _verify_one(
    sem: asyncio.Semaphore,
    listing: ListingInput,
) -> tuple[ListingInput, str, str]:
    """Returns (listing, status, reason)."""
    async with sem:
        if not listing.apply_url:
            return (listing, "unverified", "no URL provided")
        result = await asyncio.to_thread(
            verify_url, listing.apply_url, 10.0, listing.engine_quirks
        )
        return (listing, result.status, result.reason)


async def _verify_all(
    listings: list[ListingInput],
) -> list[tuple[ListingInput, str, str]]:
    sem = asyncio.Semaphore(_VERIFY_CONCURRENCY)
    tasks = [_verify_one(sem, lst) for lst in listings]
    return await asyncio.gather(*tasks)


# ── Phase 2: sequential DB pipeline ──────────────────────────────────────────

def _load_db_context(db: Session, user_id: int) -> tuple:
    """Load all per-user singleton DB rows needed for the pipeline."""
    profile = db.query(UserProfile).filter_by(user_id=user_id).first()
    rubric = db.query(ScoringRubric).filter_by(user_id=user_id).all()
    app_cfg = db.query(AppConfig).filter_by(user_id=user_id).first()
    sp = db.query(SearchParams).filter_by(user_id=user_id).first()
    threshold = app_cfg.fit_autosave_threshold if app_cfg else 70
    shown_expiry_days = app_cfg.shown_expiry_days if app_cfg else 30
    blacklist = {
        _normalize_company(e.company_name)
        for e in db.query(CompanyBlacklist).filter_by(user_id=user_id).all()
    }
    return profile, rubric, threshold, sp, shown_expiry_days, blacklist


def _build_notes(flags: list[str], urgency_note: str | None, extra: str | None) -> str:
    parts = []
    if flags:
        parts.append("Flags: " + ", ".join(flags))
    if urgency_note:
        parts.append(urgency_note)
    if extra:
        parts.append(extra)
    return " | ".join(parts) if parts else ""


def _process_one(
    listing: ListingInput,
    verify_status: str,
    verify_reason: str,
    db: Session,
    user_id: int,
    profile,
    rubric,
    threshold: int,
    sp: SearchParams,
    dry_run: bool,
    blacklist: set[str] | None = None,
) -> ListingResult:
    base = ListingResult(
        company=listing.company,
        role_title=listing.role_title,
        apply_url=listing.apply_url,
        decision="skipped",
        reason="",
        flags=[],
    )

    # ── Blacklist ─────────────────────────────────────────────────────────────
    if blacklist and _normalize_company(listing.company) in blacklist:
        base.reason = f"Company '{listing.company}' is blacklisted"
        base.skip_category = "blacklisted"
        return base

    # ── Verify ────────────────────────────────────────────────────────────────
    if verify_status == "expired":
        base.reason = f"Verification: {verify_reason}"
        base.skip_category = "verify_inactive"
        return base
    if verify_status == "skip":
        base.reason = f"Verification: {verify_reason}"
        base.skip_category = "verify_skip"
        return base
    # "active" or "unverified" both proceed

    # ── Quality flags ─────────────────────────────────────────────────────────
    qr = check_quality_flags(listing.role_title, listing.description)
    base.flags.extend(qr.flags)
    base.urgency_note = qr.urgency_note
    # Persist the flag list regardless — used when saving the record (#36)
    listing_quality_flags = qr.flags
    if qr.skip:
        base.reason = qr.skip_reason or "quality flag"
        base.skip_category = "quality_flags"
        return base

    # ── Filter ────────────────────────────────────────────────────────────────
    search_rt = sp.remote_type if sp else "remote_hybrid"
    search_geo = sp.geo if sp else "worldwide"
    reloc_req = sp.relocation_required if sp else False
    visa_req = sp.visa_required if sp else False

    fr = apply_all_filters(
        job_remote_type=listing.remote_type,
        job_geo=listing.geo,
        search_remote_type=search_rt,
        search_geo=search_geo,
        relocation_offered=listing.relocation_offered,
        visa_sponsorship=listing.visa_sponsorship,
        relocation_required=reloc_req,
        visa_required=visa_req,
        aggregator_remote_type=listing.aggregator_remote_type,
    )
    for f in fr.flags:
        if f not in base.flags:
            base.flags.append(f)
    if not fr.passed:
        base.reason = fr.skip_reason or "filter"
        base.skip_category = "filter"
        return base

    # ── Dedup ─────────────────────────────────────────────────────────────────
    dr = check_duplicate(db, listing.company, listing.role_title, user_id, listing.apply_url)
    if dr.action == "skip":
        base.reason = dr.reason
        base.skip_category = "dedup"
        return base

    # ── Score ─────────────────────────────────────────────────────────────────
    fit_pct = None
    fit_breakdown_json: str | None = None
    if profile and rubric:
        score = score_job(
            profile=profile,
            rubric=rubric,
            threshold=threshold,
            role_title=listing.role_title,
            description=listing.description,
            required_skills=listing.required_skills,
            role_domains=listing.role_domains,
            remote_type=listing.remote_type,
            geo_restriction=listing.geo,
            relocation_offered=listing.relocation_offered,
            visa_sponsorship=listing.visa_sponsorship,
            seniority_hint=listing.seniority_hint,
        )
        fit_pct = score.total_score
        fit_breakdown_json = json.dumps({
            dim: {"score": ds.score, "max_score": ds.max_score, "explanation": ds.explanation}
            for dim, ds in score.breakdown.items()
        })
        base.fit_breakdown = {
            dim: {"score": ds.score, "max_score": ds.max_score, "explanation": ds.explanation}
            for dim, ds in score.breakdown.items()
        }
    base.fit_pct = fit_pct

    # ── Threshold check ───────────────────────────────────────────────────────
    if fit_pct is not None and fit_pct < threshold:
        base.reason = f"Fit score {fit_pct}% is below autosave threshold ({threshold}%)"
        base.skip_category = "below_threshold"
        return base

    # ── Save ──────────────────────────────────────────────────────────────────
    notes = _build_notes(
        base.flags,
        base.urgency_note,
        fr.override_reason,
    )
    if dr.action == "resurface":
        notes_with_resurface = (
            (dr.resurface_note or "") + (" | " + notes if notes else "")
        ).strip(" | ")
        if not dry_run:
            existing = db.get(TrackerRecord, dr.matched_record_id)
            if existing:
                existing.status = "shown"
                existing.fit_pct = fit_pct
                existing.date_shown = date.today()
                existing.notes = notes_with_resurface
                existing.fit_breakdown = fit_breakdown_json
                existing.quality_flags = json.dumps(listing_quality_flags)
                existing.updated_at = datetime.utcnow()
                db.commit()
                base.tracker_id = existing.id
        base.decision = "resurfaced"
        base.reason = dr.reason
        return base

    # action == "save"
    if not dry_run:
        record = TrackerRecord(
            user_id=user_id,
            company=listing.company,
            role_title=listing.role_title,
            apply_url=listing.apply_url,
            status="shown",
            fit_pct=fit_pct,
            date_shown=date.today(),
            notes=notes or None,
            fit_breakdown=fit_breakdown_json,
            quality_flags=json.dumps(listing_quality_flags),
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        base.tracker_id = record.id
    base.decision = "saved"
    base.reason = "Passed all pipeline checks"
    return base


# ── Public API ────────────────────────────────────────────────────────────────

async def run_pipeline(
    listings: list[ListingInput],
    db: Session,
    user_id: int,
    dry_run: bool = False,
) -> PipelineSession:
    """
    Run the full pipeline on a batch of listings, scoped to the given user.
    Phase 1 (parallel): verify all URLs concurrently.
    Phase 2 (sequential): quality → filter → dedup → score → save.
    """
    started_at = datetime.utcnow()
    profile, rubric, threshold, sp, shown_expiry_days, blacklist = _load_db_context(db, user_id)

    verified = await _verify_all(listings)

    results: list[ListingResult] = []
    for listing, v_status, v_reason in verified:
        result = _process_one(
            listing, v_status, v_reason,
            db, user_id, profile, rubric, threshold, sp,
            dry_run=dry_run,
            blacklist=blacklist,
        )
        results.append(result)

    skip_reasons: dict[str, int] = {}
    saved = skipped = resurfaced = 0
    for r in results:
        if r.decision == "saved":
            saved += 1
        elif r.decision == "resurfaced":
            resurfaced += 1
        else:
            skipped += 1
            cat = r.skip_category or "unknown"
            skip_reasons[cat] = skip_reasons.get(cat, 0) + 1

    # ── Phase 3: auto-remove far-below-threshold shown listings (#37) ─────────
    # Any shown record with fit_pct < floor(threshold * 2/3) is bulk-skipped
    # automatically so the user isn't shown listings that are clearly out of range.
    auto_removed = 0
    if not dry_run:
        auto_floor = math.floor(threshold * 2 / 3)
        stale = (
            db.query(TrackerRecord)
            .filter(
                TrackerRecord.user_id == user_id,
                TrackerRecord.status == "shown",
                TrackerRecord.fit_pct < auto_floor,
            )
            .all()
        )
        if stale:
            now = datetime.utcnow()
            for rec in stale:
                rec.status = "skipped"
                rec.updated_at = now
            db.commit()
            auto_removed = len(stale)

    # ── Phase 4: expire shown records past shown_expiry_days (#21) ────────────
    # Shown rows with date_shown older than the configurable window are expired
    # automatically so they don't clog the dashboard indefinitely.
    auto_expired = 0
    if not dry_run:
        cutoff = date.today() - timedelta(days=shown_expiry_days)
        aged = (
            db.query(TrackerRecord)
            .filter(
                TrackerRecord.user_id == user_id,
                TrackerRecord.status == "shown",
                TrackerRecord.date_shown <= cutoff,
            )
            .all()
        )
        if aged:
            now = datetime.utcnow()
            for rec in aged:
                rec.status = "expired"
                rec.updated_at = now
            db.commit()
            auto_expired = len(aged)

    # ── Session record (#54) ───────────────────────────────────────────────────
    # Audit trail of real search runs — skipped for dry_run since nothing was
    # actually written to the tracker, so there's no real outcome to audit.
    if not dry_run:
        db.add(SearchSession(
            user_id=user_id,
            started_at=started_at,
            finished_at=datetime.utcnow(),
            query_params={
                "remote_type": sp.remote_type if sp else None,
                "geo": sp.geo if sp else None,
                "keywords": json.loads(sp.keywords) if sp and sp.keywords else [],
                "salary_min": sp.salary_min if sp else None,
                "relocation_required": sp.relocation_required if sp else None,
                "visa_required": sp.visa_required if sp else None,
            },
            listings_found=len(listings),
            listings_saved=saved + resurfaced,
            listings_skipped=skipped,
            skip_reasons=skip_reasons,
        ))
        db.commit()

    return PipelineSession(
        submitted=len(listings),
        saved=saved,
        skipped=skipped,
        resurfaced=resurfaced,
        skip_reasons=skip_reasons,
        results=results,
        auto_removed=auto_removed,
        auto_expired=auto_expired,
    )

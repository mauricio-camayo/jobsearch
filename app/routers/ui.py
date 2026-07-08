"""
Server-rendered HTML UI — P4-15.
All DB mutations go through the same service layer the API uses.
"""
import asyncio
import json
import math
import re
from datetime import date, datetime, timedelta
from typing import Annotated, Optional

import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import get_current_user_ui, require_admin
from app.csrf import get_csrf_token, require_csrf_token
from app.db.database import get_db
from app.models.listing import JobListing
from app.models.tracker_record import TrackerRecord, VALID_TRANSITIONS, TERMINAL_STATES
from app.models.user import User
from app.models.user_profile import UserProfile
from app.models.search_engine import SearchEngine
from app.models.scoring_rubric import ScoringRubric
from app.models.app_config import AppConfig
from app.models.search_params import SearchParams
from app.models.company_blacklist import CompanyBlacklist
from app.models.company_career_page import CompanyCareerPage
from app.services.pipeline import run_pipeline, ListingInput
from app.services.crawler import crawl_all_engines
from app.services import linkedin
from app.services.url_safety import UnsafeUrlError, assert_safe_external_url
from app.routers.engines import VALID_STRATEGIES as VALID_ENGINE_STRATEGIES
from app.version import APP_VERSION

router = APIRouter(dependencies=[Depends(require_csrf_token)])
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["csrf_token"] = get_csrf_token
templates.env.globals["app_version"] = APP_VERSION

STATUS_ORDER = ["shown", "applied", "interviewing", "offer", "rejected", "skipped", "expired"]

_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
# Job-board suffixes stripped from page <title> before role/company parsing
_TITLE_SUFFIXES = [
    " | Greenhouse", " - Greenhouse", " | Lever", " - Lever",
    " | Ashby", " - Ashby", " | Workable", " - Workable",
    " | LinkedIn", " - LinkedIn", " | Indeed", " - Indeed",
    " | Jobs", " - Jobs", " | Careers", " - Careers",
    " | Hiring", " - Hiring",
]


def _parse_role_company(raw: str) -> tuple[str, str]:
    """Best-effort parse of 'Role at Company' or 'Role - Company' title strings."""
    text = raw.strip()
    for suffix in _TITLE_SUFFIXES:
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
    # "Role at Company"
    m = re.match(r"^(.+?)\s+at\s+(.+)$", text, re.I)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    # "Role — Company" / "Role - Company" / "Role | Company" (last separator wins)
    for sep in (" — ", " – ", " | ", " - "):
        idx = text.rfind(sep)
        if idx > 5:
            role, company = text[:idx].strip(), text[idx + len(sep) :].strip()
            if role and company:
                return role, company
    return text, "Unknown"


async def _resolve_url(url: str, db: Session, user_id: int) -> ListingInput | None:
    """Resolve a job URL to a ListingInput.

    Order: job_listings table → tracker_record table → live HTTP fetch.
    """
    url = url.strip()
    if not url:
        return None

    # 1. Already in listings registry
    stored = (
        db.query(JobListing)
        .filter(JobListing.apply_url == url, JobListing.user_id == user_id, JobListing.deleted_at.is_(None))
        .first()
    )
    if stored:
        return ListingInput.from_dict(stored.to_listing_input_dict())

    # 2. Already in tracker
    tracked = (
        db.query(TrackerRecord)
        .filter(TrackerRecord.apply_url == url, TrackerRecord.user_id == user_id)
        .first()
    )
    if tracked:
        return ListingInput(
            company=tracked.company,
            role_title=tracked.role_title,
            apply_url=url,
        )

    # 3. Fetch live and parse title
    if "linkedin.com" in url:
        # Direct unauthenticated fetch always hits LinkedIn's login wall —
        # route through the guest API instead when a cookie is configured
        # (app/services/linkedin.py). No cookie configured falls through to
        # the raw fetch below, same degraded behavior as before this feature.
        cookie = linkedin.get_configured_cookie(db)
        if cookie:
            import asyncio

            try:
                detail = await asyncio.to_thread(linkedin.get_job_detail, url, cookie)
            except Exception:
                detail = None
            if not detail or (not detail["title"] and not detail["description"]):
                return None
            desc_lower = detail["description"].lower()
            _PROFILE_DOMAINS = ["payments", "fintech", "security", "platform", "distributed systems"]
            role_domains = [d for d in _PROFILE_DOMAINS if d in desc_lower]
            return ListingInput(
                company=detail["company"] or "Unknown",
                role_title=detail["title"],
                apply_url=url,
                description=detail["description"],
                role_domains=role_domains,
            )

    try:
        try:
            await asyncio.to_thread(assert_safe_external_url, url)
        except UnsafeUrlError:
            return None

        async with httpx.AsyncClient(
            timeout=10, follow_redirects=True, headers=_FETCH_HEADERS
        ) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            return None
        final_url = str(resp.url)
        soup = BeautifulSoup(resp.text, "html.parser")
        og = soup.find("meta", property="og:title")
        raw = (og.get("content", "") if og else "") or ""
        if not raw:
            t = soup.find("title")
            raw = t.get_text(strip=True) if t else ""
        if not raw:
            return None
        role_title, company = _parse_role_company(raw)
        # If company couldn't be parsed, infer from URL slug or domain
        if company == "Unknown":
            company = _company_from_url(final_url) or _company_from_url(url) or "Unknown"

        # Extract body text for scoring — prefer semantic content containers
        main = (
            soup.find("main")
            or soup.find("article")
            or soup.find(id=re.compile(r"content|job|description", re.I))
            or soup.find("body")
        )
        description = main.get_text(separator=" ", strip=True)[:8000] if main else ""

        # Infer role_domains from page content
        _PROFILE_DOMAINS = ["payments", "fintech", "security", "platform", "distributed systems"]
        desc_lower = description.lower()
        role_domains = [d for d in _PROFILE_DOMAINS if d in desc_lower]

        return ListingInput(
            company=company,
            role_title=role_title,
            apply_url=url,
            description=description,
            role_domains=role_domains,
        )
    except Exception:
        return None


def _company_from_url(url: str) -> str | None:
    """Extract a human-readable company name from a job URL."""
    from urllib.parse import urlparse
    # ATS slug patterns (e.g. jobs.lever.co/dlocal/... → "Dlocal")
    for pattern in [
        r"lever\.co/([^/?#]+)",
        r"ashbyhq\.com/([^/?#]+)",
        r"greenhouse\.io/([^/?#]+)/jobs",
        r"workable\.com/([^/?#]+)/",
    ]:
        m = re.search(pattern, url)
        if m:
            slug = m.group(1)
            return slug.replace("-", " ").replace(".", " ").title()
    # Fall back to subdomain or domain name (stripe.com → "Stripe")
    parsed = urlparse(url)
    host = parsed.netloc.lower().lstrip("www.")
    # Use first segment of host: "stripe.com" → "Stripe"
    name = host.split(".")[0]
    if name and name not in ("jobs", "boards", "apply", "careers"):
        return name.replace("-", " ").title()
    return None


def _derive_careers_url(url: str) -> str:
    """Best-effort board root URL to register in the CompanyCareerPage registry
    (e.g. a specific job posting on Ashby → the company's whole Ashby board)."""
    from urllib.parse import urlparse
    for pattern in [
        r"(jobs\.lever\.co/[^/?#]+)",
        r"(jobs\.ashbyhq\.com/[^/?#]+)",
        r"(boards\.greenhouse\.io/[^/?#]+)",
        r"(apply\.workable\.com/[^/?#]+)",
        r"(jobs\.workable\.com/[^/?#]+)",
    ]:
        m = re.search(pattern, url)
        if m:
            return f"https://{m.group(1)}"
    # Fallback: scheme + netloc — reasonable default for a custom careers page
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _parse_urls(text: str) -> list[str]:
    """Split a block of text into individual URLs (comma or newline separated)."""
    parts = re.split(r"[\n,]+", text)
    return [p.strip() for p in parts if p.strip().startswith("http")]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _redirect(url: str, flash: str = "", flash_type: str = "ok"):
    sep = "&" if "?" in url else "?"
    if flash:
        from urllib.parse import quote
        url = f"{url}{sep}flash={quote(flash)}&flash_type={flash_type}"
    return RedirectResponse(url, status_code=303)


def _flash_from_request(request: Request) -> tuple[str, str]:
    return request.query_params.get("flash", ""), request.query_params.get("flash_type", "ok")


# ── Root redirect ─────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def root():
    return RedirectResponse("/ui/dashboard", status_code=302)


# ── Dashboard ─────────────────────────────────────────────────────────────────

def _parse_fit_breakdown(raw: str | None) -> dict | None:
    """Parse stored fit_breakdown JSON into a dict of {dim: {score, max_score, explanation}}."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _parse_quality_flags(raw: str | None) -> list[str]:
    """Parse stored quality_flags JSON into a list of flag strings."""
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


# Rubric average weight for each non-bonus dimension (used to classify pros/cons)
_DIM_WEIGHTS = {
    "domain_match": 35,
    "tech_stack": 30,
    "seniority": 25,
    "remote_geo": 10,
    "relocation_visa_bonus": 10,
}


def _classify_breakdown(breakdown: dict, fit_pct: int | None, threshold: int) -> dict:
    """Return enriched breakdown suitable for the template.

    Adds:
      - pros: list of (dim_label, explanation) for dimensions at or above their average weight
      - cons: list of (dim_label, explanation) for dimensions below their average weight
      - low_score_reason: human-readable summary if fit_pct < threshold
    """
    _DIM_LABELS = {
        "domain_match": "Domain match",
        "tech_stack": "Tech stack",
        "seniority": "Seniority",
        "remote_geo": "Remote / Geo",
        "relocation_visa_bonus": "Relocation / Visa bonus",
    }
    pros, cons = [], []
    for dim, data in breakdown.items():
        label = _DIM_LABELS.get(dim, dim.replace("_", " ").title())
        score = data.get("score", 0)
        max_score = data.get("max_score", _DIM_WEIGHTS.get(dim, 1))
        expl = data.get("explanation", "")
        avg_weight = _DIM_WEIGHTS.get(dim, max_score)
        if max_score and score >= avg_weight * 0.6:
            pros.append({"label": label, "score": score, "max_score": max_score, "explanation": expl})
        else:
            cons.append({"label": label, "score": score, "max_score": max_score, "explanation": expl})

    low_score_reason = None
    if fit_pct is not None and fit_pct < threshold and cons:
        dragging = ", ".join(c["label"] for c in cons[:3])
        low_score_reason = f"Score dragged down by: {dragging}"

    return {"pros": pros, "cons": cons, "low_score_reason": low_score_reason}


@router.get("/ui/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_ui),
):
    records = (
        db.query(TrackerRecord)
        .filter(TrackerRecord.user_id == current_user.id)
        .order_by(TrackerRecord.fit_pct.desc().nullslast(), TrackerRecord.date_shown.desc())
        .all()
    )

    app_cfg = db.query(AppConfig).filter_by(user_id=current_user.id).first()
    threshold = app_cfg.fit_autosave_threshold if app_cfg else 70
    shown_expiry_days = app_cfg.shown_expiry_days if app_cfg else 30
    auto_floor = math.floor(threshold * 2 / 3)

    # Enrich shown+applied+ records with parsed fit_breakdown and quality_flags
    _ENRICH_STATUSES = {"shown", "applied", "interviewing", "offer", "rejected"}
    enriched: dict[int, dict] = {}
    for r in records:
        if r.status in _ENRICH_STATUSES:
            breakdown = _parse_fit_breakdown(r.fit_breakdown)
            flags = _parse_quality_flags(r.quality_flags)
            classified = _classify_breakdown(breakdown, r.fit_pct, threshold) if breakdown else None
            enriched[r.id] = {"breakdown": classified, "quality_flags": flags}

    groups: dict[str, list] = {st: [] for st in STATUS_ORDER}
    counts: dict[str, int] = {st: 0 for st in STATUS_ORDER}
    for r in records:
        st = r.status if r.status in groups else "expired"
        groups[st].append(r)
        counts[st] = counts.get(st, 0) + 1

    valid_transitions = {
        st: sorted(targets)
        for st, targets in VALID_TRANSITIONS.items()
    }

    flash, flash_type = _flash_from_request(request)
    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request,
        "current_user": current_user,
        "title": "Dashboard",
        "active": "dashboard",
        "groups": groups,
        "counts": counts,
        "status_order": STATUS_ORDER,
        "total": len(records),
        "valid_transitions": valid_transitions,
        "fit_autosave_threshold": threshold,
        "shown_expiry_days": shown_expiry_days,
        "auto_floor": auto_floor,
        "enriched": enriched,
        "flash": flash,
        "flash_type": flash_type,
    })


@router.post("/ui/tracker/{record_id}/status")
def update_status(
    record_id: int,
    new_status: Annotated[str, Form()],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_ui),
):
    record = (
        db.query(TrackerRecord)
        .filter(TrackerRecord.id == record_id, TrackerRecord.user_id == current_user.id)
        .first()
    )
    if record is None:
        return _redirect("/ui/dashboard", f"Record #{record_id} not found", "err")
    if record.status in TERMINAL_STATES:
        return _redirect("/ui/dashboard", f"#{record_id} is already in terminal state '{record.status}'", "err")
    allowed = VALID_TRANSITIONS.get(record.status, set())
    if new_status not in allowed:
        return _redirect("/ui/dashboard",
                         f"Cannot transition '{record.status}' → '{new_status}'", "err")
    record.status = new_status
    if new_status == "applied" and record.date_applied is None:
        record.date_applied = date.today()
    record.updated_at = datetime.utcnow()
    db.commit()
    return _redirect(f"/ui/dashboard#section-{new_status}",
                     f"#{record_id} {record.company} → {new_status}")


@router.post("/ui/tracker/{record_id}/notes")
def update_notes(
    record_id: int,
    notes: Annotated[str, Form()],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_ui),
):
    record = (
        db.query(TrackerRecord)
        .filter(TrackerRecord.id == record_id, TrackerRecord.user_id == current_user.id)
        .first()
    )
    if record is None:
        return _redirect("/ui/dashboard", f"Record #{record_id} not found", "err")
    record.notes = notes.strip() or None
    record.updated_at = datetime.utcnow()
    db.commit()
    return _redirect("/ui/dashboard", f"Notes updated for #{record_id}")


@router.post("/ui/tracker/check-applied-live")
async def check_applied_live(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_ui),
):
    """Verify all applied records; transition any expired URLs to 'expired' status."""
    import asyncio
    from app.services.verifier import verify_url

    records = (
        db.query(TrackerRecord)
        .filter(TrackerRecord.user_id == current_user.id, TrackerRecord.status == "applied")
        .all()
    )

    li_at = linkedin.get_configured_cookie(db)
    quirks = {"li_at_cookie": li_at} if li_at else None

    async def _check_one(r: TrackerRecord):
        if not r.apply_url:
            return "skipped"
        result = await asyncio.to_thread(verify_url, r.apply_url, 10.0, quirks)
        return result.status

    results = await asyncio.gather(*[_check_one(r) for r in records])

    now = datetime.utcnow()
    expired_count = 0
    for r, status in zip(records, results):
        if status == "expired":
            r.status = "expired"
            r.updated_at = now
            expired_count += 1
    if expired_count:
        db.commit()

    checked = len([s for s in results if s != "skipped"])
    return _redirect(
        "/ui/dashboard#section-applied",
        f"{checked} checked, {expired_count} expired",
        "ok",
    )


@router.post("/ui/tracker/bulk-skip")
def bulk_skip_ui(
    max_fit_pct: Annotated[int, Form()],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_ui),
):
    records = (
        db.query(TrackerRecord)
        .filter(
            TrackerRecord.user_id == current_user.id,
            TrackerRecord.status == "shown",
            TrackerRecord.fit_pct <= max_fit_pct,
        )
        .all()
    )
    now = datetime.utcnow()
    for r in records:
        r.status = "skipped"
        r.updated_at = now
    db.commit()
    return _redirect(
        "/ui/dashboard#section-shown",
        f"Skipped {len(records)} listing(s) with fit ≤ {max_fit_pct}%",
        "ok",
    )


@router.post("/ui/tracker/expire-stale")
def expire_stale_ui(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_ui),
):
    cfg = db.query(AppConfig).filter_by(user_id=current_user.id).first()
    expiry_days = cfg.shown_expiry_days if cfg else 30
    cutoff = date.today() - timedelta(days=expiry_days)
    records = (
        db.query(TrackerRecord)
        .filter(
            TrackerRecord.user_id == current_user.id,
            TrackerRecord.status == "shown",
            TrackerRecord.date_shown <= cutoff,
        )
        .all()
    )
    now = datetime.utcnow()
    for r in records:
        r.status = "expired"
        r.updated_at = now
    if records:
        db.commit()
    return _redirect(
        "/ui/dashboard#section-shown",
        f"Expired {len(records)} stale listing(s) (shown > {expiry_days} days ago)",
        "ok",
    )


# ── Search ────────────────────────────────────────────────────────────────────

@router.get("/ui/search", response_class=HTMLResponse)
def search_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_ui),
):
    sp = db.query(SearchParams).filter_by(user_id=current_user.id).first()
    kw_list = json.loads(sp.keywords) if sp and sp.keywords else []
    flash, flash_type = _flash_from_request(request)
    return templates.TemplateResponse(request, "search.html", {
        "request": request,
        "current_user": current_user,
        "title": "Search",
        "active": "search",
        "sp": sp,
        "kw_list": kw_list,
        "career_page_suggestions": {},
        "flash": flash,
        "flash_type": flash_type,
        "session": None,
        "prev_urls": "",
        "prev_dry_run": False,
    })


@router.post("/ui/search/params")
def update_search_params(
    remote_type: Annotated[str, Form()],
    geo: Annotated[str, Form()],
    keywords: Annotated[str, Form()] = "",
    salary_min: Annotated[Optional[str], Form()] = None,
    relocation_required: Annotated[Optional[str], Form()] = None,
    visa_required: Annotated[Optional[str], Form()] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_ui),
):
    sp = db.query(SearchParams).filter_by(user_id=current_user.id).first()
    if sp is None:
        return _redirect("/ui/search", "SearchParams not initialised", "err")
    sp.remote_type = remote_type
    sp.geo = geo
    kws = [k.strip() for k in keywords.splitlines() if k.strip()]
    sp.keywords = json.dumps(kws)
    sp.salary_min = int(salary_min) if salary_min and salary_min.strip() else None
    sp.relocation_required = relocation_required == "on"
    sp.visa_required = visa_required == "on"
    sp.updated_at = datetime.utcnow()
    db.commit()
    return _redirect("/ui/search", "Filter defaults saved")


@router.post("/ui/search/run", response_class=HTMLResponse)
async def run_search_ui(
    request: Request,
    urls_text: Annotated[str, Form()],
    dry_run: Annotated[Optional[str], Form()] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_ui),
):
    sp = db.query(SearchParams).filter_by(user_id=current_user.id).first()
    error = None
    session = None

    raw_urls = _parse_urls(urls_text)
    if not raw_urls:
        error = "Paste at least one job URL before running the pipeline."
    else:
        # Resolve all URLs concurrently
        import asyncio
        resolved = await asyncio.gather(*[_resolve_url(u, db, current_user.id) for u in raw_urls])
        listings = [l for l in resolved if l is not None]
        failed = len(raw_urls) - len(listings)
        if not listings:
            error = "None of the URLs could be resolved. Check that they are valid job posting links."
        else:
            session = await run_pipeline(listings, db, current_user.id, dry_run=(dry_run == "on"))
            session.dry_run = (dry_run == "on")
            if failed:
                error = f"{failed} URL(s) could not be fetched and were skipped."

    career_page_suggestions: dict[str, dict] = {}
    if session:
        active_companies = {
            page.company.strip().lower()
            for page in db.query(CompanyCareerPage).filter(CompanyCareerPage.active == True).all()  # noqa: E712
        }
        for r in session.results:
            if r.apply_url and r.company.strip().lower() not in active_companies:
                career_page_suggestions[r.apply_url] = {
                    "company": r.company,
                    "careers_url": _derive_careers_url(r.apply_url),
                }

    kw_list = json.loads(sp.keywords) if sp and sp.keywords else []
    flash = error or ""
    flash_type = "err" if error else "ok"
    return templates.TemplateResponse(request, "search.html", {
        "request": request,
        "current_user": current_user,
        "title": "Search",
        "active": "search",
        "sp": sp,
        "kw_list": kw_list,
        "session": session,
        "career_page_suggestions": career_page_suggestions,
        "prev_urls": urls_text if error else "",
        "prev_dry_run": dry_run == "on",
        "flash": flash,
        "flash_type": flash_type,
    })


@router.post("/ui/search/crawl", response_class=HTMLResponse)
async def crawl_ui(
    request: Request,
    dry_run: Annotated[Optional[str], Form()] = None,
    next: Annotated[Optional[str], Form()] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_ui),
):
    sp = db.query(SearchParams).filter_by(user_id=current_user.id).first()
    cfg = db.query(AppConfig).filter_by(user_id=current_user.id).first()
    summary = await crawl_all_engines(db, current_user.id, dry_run=(dry_run == "on"))
    session = summary.pipeline
    session.dry_run = (dry_run == "on")

    flash = (
        f"Crawled {summary.engines_crawled} engines — "
        f"{summary.raw_listings_found} listings found, "
        f"{session.saved} saved, {session.skipped} skipped."
    )
    if getattr(session, "auto_removed", 0):
        flash += f" ({session.auto_removed} auto-removed below auto-floor)"
    if getattr(session, "auto_expired", 0):
        expiry_days = cfg.shown_expiry_days if cfg else 30
        flash += f" ({session.auto_expired} expired after {expiry_days}d)"
    if summary.engine_errors:
        flash += f" ({summary.engines_errored} engine errors)"

    # Redirect to caller page when a next= destination was supplied
    if next and next.startswith("/"):
        return _redirect(next, flash, "ok")

    kw_list = json.loads(sp.keywords) if sp and sp.keywords else []
    return templates.TemplateResponse(request, "search.html", {
        "request": request,
        "current_user": current_user,
        "title": "Search",
        "active": "search",
        "sp": sp,
        "kw_list": kw_list,
        "session": session,
        "career_page_suggestions": {},
        "prev_json": "",
        "prev_dry_run": dry_run == "on",
        "flash": flash,
        "flash_type": "ok",
        "crawl_errors": summary.engine_errors,
    })


# ── Profile ───────────────────────────────────────────────────────────────────

@router.get("/ui/profile", response_class=HTMLResponse)
def profile_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_ui),
):
    profile = db.query(UserProfile).filter_by(user_id=current_user.id).first()
    rubric = db.query(ScoringRubric).filter_by(user_id=current_user.id).all()
    cfg = db.query(AppConfig).filter_by(user_id=current_user.id).first()
    flash, flash_type = _flash_from_request(request)
    return templates.TemplateResponse(request, "profile.html", {
        "request": request,
        "current_user": current_user,
        "title": "Profile",
        "active": "profile",
        "profile": profile,
        "rubric": rubric,
        "threshold": cfg.fit_autosave_threshold if cfg else 70,
        "flash": flash,
        "flash_type": flash_type,
    })


def _parse_list(text: str) -> list[str]:
    """Split a comma- or newline-separated textarea value into a clean list."""
    parts = re.split(r"[\n,]+", text)
    return [p.strip() for p in parts if p.strip()]


@router.post("/ui/profile/update")
def update_profile_ui(
    full_name: Annotated[str, Form()],
    experience_years: Annotated[int, Form()],
    seniority: Annotated[str, Form()],
    skills: Annotated[str, Form()] = "",
    domains: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_ui),
):
    profile = db.query(UserProfile).filter_by(user_id=current_user.id).first()
    if profile is None:
        return _redirect("/ui/profile", "Profile not found", "err")
    profile.full_name = full_name.strip()
    profile.experience_years = experience_years
    profile.seniority = seniority.strip()
    profile.skills = _parse_list(skills)
    profile.domains = _parse_list(domains)
    profile.updated_at = datetime.utcnow()
    db.commit()
    return _redirect("/ui/profile", "Profile updated")


@router.post("/ui/profile/rubric")
def update_rubric_ui(
    weight_domain_match: Annotated[int, Form()],
    weight_tech_stack: Annotated[int, Form()],
    weight_seniority: Annotated[int, Form()],
    weight_remote_geo: Annotated[int, Form()],
    weight_relocation_visa_bonus: Annotated[int, Form()],
    fit_autosave_threshold: Annotated[int, Form()],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_ui),
):
    non_bonus = {
        "domain_match": weight_domain_match,
        "tech_stack": weight_tech_stack,
        "seniority": weight_seniority,
        "remote_geo": weight_remote_geo,
    }
    if sum(non_bonus.values()) != 100:
        return _redirect(
            "/ui/profile",
            f"Non-bonus weights must sum to 100; got {sum(non_bonus.values())}",
            "err",
        )
    if not 0 <= fit_autosave_threshold <= 100:
        return _redirect("/ui/profile", "Autosave threshold must be between 0 and 100", "err")
    rows = {r.dimension: r for r in db.query(ScoringRubric).filter_by(user_id=current_user.id).all()}
    for dim, weight in non_bonus.items():
        if dim in rows:
            rows[dim].weight = weight
    if "relocation_visa_bonus" in rows:
        rows["relocation_visa_bonus"].weight = weight_relocation_visa_bonus
    cfg = db.query(AppConfig).filter_by(user_id=current_user.id).first()
    if cfg is not None:
        cfg.fit_autosave_threshold = fit_autosave_threshold
        cfg.updated_at = datetime.utcnow()
    db.commit()
    return _redirect("/ui/profile", "Scoring rubric updated")


# ── Engines ───────────────────────────────────────────────────────────────────

@router.get("/ui/engines", response_class=HTMLResponse)
def engines_page(
    request: Request,
    db: Session = Depends(get_db),
    admin: User | None = Depends(require_admin),
):
    engines = db.query(SearchEngine).order_by(SearchEngine.id).all()
    flash, flash_type = _flash_from_request(request)
    return templates.TemplateResponse(request, "engines.html", {
        "request": request,
        "current_user": admin,
        "title": "Engines",
        "active": "engines",
        "engines": engines,
        "flash": flash,
        "flash_type": flash_type,
    })


@router.post("/ui/engines/add")
def add_engine(
    name: Annotated[str, Form()],
    fetch_strategy: Annotated[str, Form()],
    search_url_template: Annotated[str, Form()],
    quirks: Annotated[Optional[str], Form()] = None,
    search_params: Annotated[Optional[str], Form()] = None,
    db: Session = Depends(get_db),
    admin: User | None = Depends(require_admin),
):
    if fetch_strategy not in VALID_ENGINE_STRATEGIES:
        return _redirect(
            "/ui/engines",
            f"fetch_strategy must be one of {sorted(VALID_ENGINE_STRATEGIES)}",
            "err",
        )
    try:
        quirks_dict = json.loads(quirks) if quirks and quirks.strip() else {}
        search_params_dict = json.loads(search_params) if search_params and search_params.strip() else {}
    except json.JSONDecodeError as exc:
        return _redirect("/ui/engines", f"Invalid JSON in quirks/search params: {exc}", "err")

    if db.query(SearchEngine).filter(SearchEngine.name == name.strip()).first():
        return _redirect("/ui/engines", f"Engine '{name}' already exists", "err")

    engine = SearchEngine(
        name=name.strip(),
        search_url_template=search_url_template.strip(),
        fetch_strategy=fetch_strategy,
        quirks=quirks_dict,
        search_params=search_params_dict,
        active=True,
    )
    db.add(engine)
    db.commit()
    return _redirect("/ui/engines", f"Engine '{name}' added")


@router.post("/ui/engines/{engine_id}/toggle")
def toggle_engine(
    engine_id: int,
    db: Session = Depends(get_db),
    admin: User | None = Depends(require_admin),
):
    engine = db.get(SearchEngine, engine_id)
    if engine is None:
        return _redirect("/ui/engines", f"Engine #{engine_id} not found", "err")
    engine.active = not engine.active
    db.commit()
    state = "activated" if engine.active else "deactivated"
    return _redirect("/ui/engines", f"{engine.name} {state}")


@router.post("/ui/engines/{engine_id}/delete")
def delete_engine(
    engine_id: int,
    db: Session = Depends(get_db),
    admin: User | None = Depends(require_admin),
):
    engine = db.get(SearchEngine, engine_id)
    if engine is None:
        return _redirect("/ui/engines", f"Engine #{engine_id} not found", "err")
    name = engine.name
    db.delete(engine)
    db.commit()
    return _redirect("/ui/engines", f"Engine '{name}' deleted")


def _engine_status(engines: list[SearchEngine]) -> list[dict]:
    """Non-admin-safe engine status: name + configured bool, no search_params
    contents. 'Configured' means any quirks-declared required search_params
    key (e.g. LinkedIn's requires_search_params: "li_at") is actually set."""
    result = []
    for e in engines:
        needs = (e.quirks or {}).get("requires_search_params")
        configured = True if not needs else bool((e.search_params or {}).get(needs))
        result.append({"name": e.name, "active": e.active, "configured": configured, "needs": needs})
    return result


@router.get("/ui/blacklist", response_class=HTMLResponse)
def blacklist_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_ui),
):
    blacklist = (
        db.query(CompanyBlacklist)
        .filter_by(user_id=current_user.id)
        .order_by(CompanyBlacklist.company_name)
        .all()
    )
    engines = db.query(SearchEngine).order_by(SearchEngine.id).all()
    flash, flash_type = _flash_from_request(request)
    return templates.TemplateResponse(request, "blacklist.html", {
        "request": request,
        "current_user": current_user,
        "title": "Blacklist",
        "active": "blacklist",
        "blacklist": blacklist,
        "engine_status": _engine_status(engines),
        "flash": flash,
        "flash_type": flash_type,
    })


@router.post("/ui/blacklist/add")
def blacklist_add(
    company_name: Annotated[str, Form()],
    notes: Annotated[Optional[str], Form()] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_ui),
):
    name = company_name.strip()
    if not name:
        return _redirect("/ui/blacklist", "Company name is required", "err")
    existing = (
        db.query(CompanyBlacklist)
        .filter(CompanyBlacklist.user_id == current_user.id, CompanyBlacklist.company_name == name)
        .first()
    )
    if existing:
        return _redirect("/ui/blacklist", f"'{name}' is already blacklisted", "err")
    entry = CompanyBlacklist(user_id=current_user.id, company_name=name, notes=(notes or "").strip() or None)
    db.add(entry)
    db.commit()
    return _redirect("/ui/blacklist", f"'{name}' added to blacklist")


@router.post("/ui/blacklist/{entry_id}/delete")
def blacklist_delete(
    entry_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_ui),
):
    entry = (
        db.query(CompanyBlacklist)
        .filter(CompanyBlacklist.id == entry_id, CompanyBlacklist.user_id == current_user.id)
        .first()
    )
    if entry is None:
        return _redirect("/ui/blacklist", f"Blacklist entry #{entry_id} not found", "err")
    name = entry.company_name
    db.delete(entry)
    db.commit()
    return _redirect("/ui/blacklist", f"'{name}' removed from blacklist")

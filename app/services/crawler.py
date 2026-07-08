"""
Search engine crawler — P3-28.

Fetches listings from each active SearchEngine entry, parses them into
ListingInput objects, and feeds the batch into the existing pipeline.

Strategy dispatch:
  api     — Remotive JSON API (and similar structured endpoints)
  ashby   — per-company Ashby GraphQL (engine.name == "Ashby")
  lever   — per-company Lever public posting API (api.lever.co/v0/postings/{slug})
  workable— per-company Workable jobs API (apply.workable.com/api/v3/accounts/{slug}/jobs)
  greenhouse — per-company Greenhouse boards API (boards-api.greenhouse.io/v1/boards/{slug}/jobs)
  html    — best-effort BeautifulSoup link extraction
  rss     — feedparser

Per-company ATS engines (Ashby, Greenhouse, Lever, Workable) iterate over
CompanyCareerPage rows whose ats_type matches the engine name.

All network calls run in a thread pool via asyncio.to_thread so they don't
block the event loop.
"""
import asyncio
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime

import httpx
import feedparser
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app.models.search_engine import SearchEngine
from app.models.company_career_page import CompanyCareerPage
from app.models.search_params import SearchParams
from app.services import linkedin
from app.services.pipeline import ListingInput, PipelineSession, run_pipeline

# ── Constants ─────────────────────────────────────────────────────────────────

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_ASHBY_GRAPHQL = "https://jobs.ashbyhq.com/api/non-user-graphql"
# Ashby removed the flat `jobPostings(...)` root query (confirmed live: "Cannot query
# field \"jobPostings\" on type \"Query\"") — replaced by a two-step fetch: the board-level
# query below for the list (title/id only, used for relevance filtering), then a per-job
# detail query (same one _verify_ashby() in verifier.py already uses) for isListed/
# location/description, since jobBoardWithTeams's own jobPostings don't expose those.
_ASHBY_LIST_QUERY = """
query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {
  jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) {
    jobPostings { id title teamId employmentType }
  }
}
"""
_ASHBY_DETAIL_QUERY = """
query ApiJobPostingShow($org: String!, $id: String!) {
  jobPosting(organizationHostedJobsPageName: $org, jobPostingId: $id) {
    id title isListed locationName descriptionHtml
  }
}
"""

_GEO_KEYWORDS: list[tuple[str, list[str]]] = [
    ("worldwide",     ["worldwide", "anywhere", "global"]),
    ("usa",           ["usa", "united states", "u.s.", "us only", "us-based"]),
    ("latam",         ["latin america", "latam", "south america"]),
    ("emea",          ["emea", "europe", "eu", "uk", "germany", "spain", "france"]),
    ("north_america", ["north america", "canada", "usa/canada"]),
    ("brazil",        ["brazil", "brasil"]),
]


# ── Geo / remote helpers ──────────────────────────────────────────────────────

def _parse_geo(text: str) -> str:
    t = text.lower()
    for geo, signals in _GEO_KEYWORDS:
        if any(s in t for s in signals):
            return geo
    return "unknown"


def _parse_remote_type(text: str) -> str:
    t = text.lower()
    if "hybrid" in t:
        return "hybrid"
    if "remote" in t:
        return "remote"
    if "onsite" in t or "on-site" in t or "in office" in t or "in-office" in t:
        return "onsite"
    return "unknown"


def _strip_html(html: str) -> str:
    text = BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)
    # Some ATS APIs (Greenhouse) return double-escaped HTML; strip again if tags remain.
    if "<" in text:
        text = BeautifulSoup(text, "html.parser").get_text(separator=" ", strip=True)
    return text[:2000]


def _extract_slug(url: str, domain: str) -> str | None:
    """Extract the path segment immediately after domain."""
    m = re.search(rf"{re.escape(domain)}/([^/?#]+)", url or "")
    return m.group(1) if m else None


# ── Role relevance filter ─────────────────────────────────────────────────────

# Broad fallback: title has a management token AND an engineering-adjacent token.
# This catches "VP Engineering", "Director, Platform", "Head of Infrastructure", etc.
# that would not match any full role_type phrase.
_MGMT_TOKENS = frozenset(["manager", "director", "head", "vp"])
_ENG_TOKENS = frozenset(["engineering", "engineer", "software",
                          "infrastructure", "backend", "frontend"])
# Broad-token match exclusions: PM/sales/support titles that contain both a
# management word and an engineering-adjacent word but are NOT EM roles.
_MGMT_EXCLUSION_PHRASES = frozenset([
    "product manager", "account manager", "program manager",
    "sales manager", "project manager", "support manager",
    "technical account manager", "technical program manager",
])


def _is_relevant(title: str, role_types: list[str], keywords: list[str]) -> bool:
    """Return True if the listing title matches role_type signals or broad EM token pattern.

    Keywords are intentionally NOT used as standalone pass signals — they are too
    broad (e.g. 'payments' matches 'Account Executive, Payments Platform') and
    cause false positives for sales/IC roles.  Keywords contribute to scoring via
    the scorer's domain/tech_stack dimensions instead.
    """
    if not role_types:
        return True
    t = title.lower()
    # Exact phrase match against role types
    if any(s.lower() in t for s in role_types):
        return True
    # Exclude known non-EM management titles before broad token check
    if any(phrase in t for phrase in _MGMT_EXCLUSION_PHRASES):
        return False
    # Broad token match: management word + engineering-adjacent word
    words = set(re.findall(r'\b\w+\b', t))
    return bool(words & _MGMT_TOKENS and words & _ENG_TOKENS)


# ── Engine-specific fetchers (all sync, called via to_thread) ─────────────────

def _fetch_remotive(
    engine: SearchEngine,
    role_types: list[str],
    keywords: list[str],
) -> list[ListingInput]:
    queries = (keywords[:1] + role_types[:2]) if keywords else role_types[:2]
    seen: set[str] = set()
    results: list[ListingInput] = []

    with httpx.Client(timeout=20, follow_redirects=True) as client:
        for q in queries:
            url = engine.search_url_template.replace(
                "{query}", urllib.parse.quote(q)
            ).replace("{filters}", "")
            try:
                resp = client.get(url)
                if resp.status_code != 200:
                    continue
                jobs = resp.json().get("jobs", [])
                for j in jobs:
                    apply_url = j.get("url", "")
                    if not apply_url or apply_url in seen:
                        continue
                    seen.add(apply_url)
                    title = j.get("title", "")
                    if not _is_relevant(title, role_types, keywords):
                        continue
                    location = j.get("candidate_required_location") or ""
                    results.append(ListingInput(
                        company=j.get("company_name", "Unknown"),
                        role_title=title,
                        apply_url=apply_url,
                        remote_type=_parse_remote_type(location),
                        geo=_parse_geo(location),
                        description=_strip_html(j.get("description", "")),
                    ))
            except Exception:
                continue
    return results


def _fetch_ashby_company(
    page: CompanyCareerPage,
    role_types: list[str],
    keywords: list[str],
) -> list[ListingInput]:
    slug = _extract_slug(page.careers_url, "jobs.ashbyhq.com")
    if not slug:
        return []
    with httpx.Client(timeout=15) as client:
        try:
            resp = client.post(_ASHBY_GRAPHQL, json={
                "operationName": "ApiJobBoardWithTeams",
                "variables": {"organizationHostedJobsPageName": slug},
                "query": _ASHBY_LIST_QUERY,
            })
            board = resp.json().get("data", {}).get("jobBoardWithTeams") or {}
            postings = board.get("jobPostings") or []
        except Exception:
            return []

        relevant = [p for p in postings if _is_relevant(p.get("title", ""), role_types, keywords)]

        results = []
        for p in relevant:
            job_id = p.get("id")
            title = p.get("title", "")
            if not job_id:
                continue
            try:
                detail_resp = client.post(_ASHBY_GRAPHQL, json={
                    "operationName": "ApiJobPostingShow",
                    "variables": {"org": slug, "id": job_id},
                    "query": _ASHBY_DETAIL_QUERY,
                })
                detail = detail_resp.json().get("data", {}).get("jobPosting") or {}
            except Exception:
                continue
            if not detail.get("isListed", True):
                continue
            location = detail.get("locationName") or ""
            desc = _strip_html(detail.get("descriptionHtml") or "")
            results.append(ListingInput(
                company=page.company,
                role_title=title,
                apply_url=f"https://jobs.ashbyhq.com/{slug}/{job_id}",
                remote_type=_parse_remote_type(location),
                geo=_parse_geo(location),
                description=desc,
            ))
    return results


def _fetch_lever_company(
    page: CompanyCareerPage,
    role_types: list[str],
    keywords: list[str],
) -> list[ListingInput]:
    slug = _extract_slug(page.careers_url, "jobs.lever.co")
    if not slug:
        return []
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    with httpx.Client(timeout=15) as client:
        try:
            resp = client.get(url)
            if resp.status_code != 200:
                return []
            jobs = resp.json()
        except Exception:
            return []

    results = []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        title = j.get("text", "")
        if not _is_relevant(title, role_types, keywords):
            continue
        categories = j.get("categories", {})
        location = categories.get("location") or categories.get("allLocations", [""])[0] if isinstance(categories.get("allLocations"), list) else ""
        results.append(ListingInput(
            company=page.company,
            role_title=title,
            apply_url=j.get("hostedUrl"),
            remote_type=_parse_remote_type(location),
            geo=_parse_geo(location),
            description=_strip_html(j.get("descriptionPlain") or j.get("description") or ""),
        ))
    return results


def _fetch_workable_company(
    page: CompanyCareerPage,
    role_types: list[str],
    keywords: list[str],
) -> list[ListingInput]:
    slug = _extract_slug(page.careers_url, "apply.workable.com")
    if not slug:
        return []
    url = f"https://apply.workable.com/api/v3/accounts/{slug}/jobs"
    with httpx.Client(timeout=15) as client:
        try:
            resp = client.post(url, json={
                # "remote" must be an array now, not a bool (confirmed live:
                # {"remote":"\"remote\" must be an array"} on the old True value).
                "query": "", "location": [], "department": [],
                "worktype": [], "remote": [],
            })
            if resp.status_code != 200:
                return []
            jobs = resp.json().get("results", [])
        except Exception:
            return []

    results = []
    for j in jobs:
        title = j.get("title", "")
        if not _is_relevant(title, role_types, keywords):
            continue
        shortcode = j.get("shortcode", "")
        apply_url = f"{page.careers_url}/j/{shortcode}" if shortcode else None
        # Workable's API now returns "location" as a structured object
        # ({country, countryCode, city, region}), not a plain string —
        # confirmed live (RecargaPay). Flatten it for the geo/remote-type
        # keyword matchers below, which expect free text.
        loc = j.get("location") or {}
        location = ", ".join(p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p)
        remote = j.get("remote", False) or j.get("workplace") == "remote"
        results.append(ListingInput(
            company=page.company,
            role_title=title,
            apply_url=apply_url,
            remote_type="remote" if remote else _parse_remote_type(location),
            geo=_parse_geo(location),
        ))
    return results


def _fetch_greenhouse_company(
    page: CompanyCareerPage,
    role_types: list[str],
    keywords: list[str],
) -> list[ListingInput]:
    slug = _extract_slug(page.careers_url, "boards.greenhouse.io")
    if not slug:
        return []
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    with httpx.Client(timeout=15) as client:
        try:
            resp = client.get(url)
            if resp.status_code != 200:
                return []
            jobs = resp.json().get("jobs", [])
        except Exception:
            return []

    seen_titles: set[str] = set()
    matched = []
    for j in jobs:
        title = j.get("title", "")
        if not _is_relevant(title, role_types, keywords):
            continue
        # Greenhouse posts the same role for multiple locations; keep one per title.
        title_key = title.lower().strip()
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        matched.append(j)

    results = []
    with httpx.Client(timeout=15) as client:
        for j in matched:
            title = j.get("title", "")
            location = (j.get("location") or {}).get("name") or ""
            # Fetch job detail for description (improves fit scoring)
            desc = ""
            job_id = j.get("id")
            if job_id:
                try:
                    detail = client.get(
                        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"
                    )
                    if detail.status_code == 200:
                        desc = _strip_html(detail.json().get("content") or "")
                except Exception:
                    pass
            results.append(ListingInput(
                company=page.company,
                role_title=title,
                apply_url=j.get("absolute_url"),
                remote_type=_parse_remote_type(location),
                geo=_parse_geo(location),
                description=desc,
            ))
    return results


def _fetch_linkedin_engine(
    engine: SearchEngine,
    role_types: list[str],
    keywords: list[str],
) -> list[ListingInput]:
    """Search + enrich LinkedIn postings via the guest API (app/services/linkedin.py).

    Requires the LinkedIn engine's search_params to hold a li_at session cookie
    (set per-user on the Engines page — never a global env var, since the
    cookie is per-user browser session state). Returns no results if absent,
    same as any other misconfigured engine.
    """
    cookie = (engine.search_params or {}).get("li_at")
    if not cookie:
        return []

    queries = (keywords[:1] + role_types[:2]) if keywords else role_types[:2]
    seen: set[str] = set()
    results: list[ListingInput] = []

    with httpx.Client(timeout=15, follow_redirects=True) as client:
        for q in queries:
            if len(results) >= 30:
                break
            stubs = linkedin.search_jobs(q, cookie)
            for stub in stubs:
                if len(results) >= 30:
                    break
                url = stub.get("url")
                if not url or url in seen:
                    continue
                title = stub.get("title", "")
                if not _is_relevant(title, role_types, keywords):
                    continue
                seen.add(url)

                try:
                    detail = linkedin.get_job_detail(url, cookie, client)
                except Exception:
                    detail = None

                location = stub.get("location", "")
                description = ""
                hint = None
                if detail:
                    description = _strip_html(detail.get("description") or "")
                    hint = linkedin.seniority_hint(detail.get("criteria") or {})
                    location = detail.get("location") or location

                results.append(ListingInput(
                    company=stub.get("company") or engine.name,
                    role_title=title,
                    apply_url=url,
                    remote_type=_parse_remote_type(location),
                    geo=_parse_geo(location),
                    description=description,
                    seniority_hint=hint,
                ))
    return results


def _fetch_html_engine(
    engine: SearchEngine,
    role_types: list[str],
    keywords: list[str],
) -> list[ListingInput]:
    q = (keywords[0] if keywords else "") or (role_types[0] if role_types else "engineering manager")
    url = engine.search_url_template.replace(
        "{query}", urllib.parse.quote(q)
    ).replace("{filters}", "")

    with httpx.Client(timeout=20, follow_redirects=True, headers=_BROWSER_HEADERS) as client:
        try:
            resp = client.get(url)
            if resp.status_code != 200:
                return []
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception:
            return []

    seen: set[str] = set()
    results: list[ListingInput] = []
    for link in soup.find_all("a", href=True):
        # Resolve relative hrefs against the fetched page's URL — some boards
        # (e.g. golangprojects.com) link to postings with relative paths like
        # "/golang-go-job-....html" rather than absolute URLs; urljoin() is a
        # no-op for hrefs that are already absolute.
        href = urllib.parse.urljoin(str(resp.url), link["href"])
        if not href.startswith("http"):
            continue
        if href in seen:
            continue
        # -job- also matches slug-embedded patterns like golangprojects.com's
        # /golang-go-job-<slug>.html, which has no /job/ path segment at all.
        if not re.search(r"/jobs?/|/careers?/|/positions?/|/openings?/|/role/|-job-", href, re.I):
            continue
        seen.add(href)
        title = link.get_text(strip=True)
        if not title or len(title) < 5 or len(title) > 120:
            continue
        if not _is_relevant(title, role_types, keywords):
            continue
        results.append(ListingInput(
            company=engine.name,
            role_title=title,
            apply_url=href,
        ))
        if len(results) >= 30:
            break
    return results


def _fetch_rss_engine(
    engine: SearchEngine,
    role_types: list[str],
    keywords: list[str],
) -> list[ListingInput]:
    q = (keywords[0] if keywords else "") or (role_types[0] if role_types else "engineering manager")
    url = engine.search_url_template.replace(
        "{query}", urllib.parse.quote(q)
    ).replace("{filters}", "")

    try:
        feed = feedparser.parse(url)
    except Exception:
        return []

    results = []
    for entry in feed.entries[:50]:
        title = entry.get("title", "")
        if not _is_relevant(title, role_types, keywords):
            continue
        apply_url = entry.get("link") or entry.get("id")
        summary = _strip_html(entry.get("summary") or entry.get("description") or "")
        results.append(ListingInput(
            company=entry.get("author") or engine.name,
            role_title=title,
            apply_url=apply_url,
            description=summary,
        ))
    return results


# ── Per-engine dispatch ───────────────────────────────────────────────────────

_ATS_FETCHERS = {
    "ashby":      _fetch_ashby_company,
    "lever":      _fetch_lever_company,
    "workable":   _fetch_workable_company,
    "greenhouse": _fetch_greenhouse_company,
}

# Maps board_url_pattern domain fragment → ATS key used in _ATS_FETCHERS and pages_by_ats
_BOARD_PATTERN_TO_ATS = {
    "jobs.ashbyhq.com":          "ashby",
    "boards.greenhouse.io":       "greenhouse",
    "jobs.lever.co":              "lever",
    "apply.workable.com":         "workable",
}


def _quirk_ats_key(engine: SearchEngine) -> str | None:
    """Return the ATS key (ashby/lever/workable/greenhouse) driven by engine.quirks,
    or None if this engine is not a per-company ATS board engine."""
    q = engine.quirks or {}
    pattern = q.get("board_url_pattern", "")
    for fragment, key in _BOARD_PATTERN_TO_ATS.items():
        if fragment in pattern:
            return key
    return None


def _crawl_engine_sync(
    engine: SearchEngine,
    role_types: list[str],
    keywords: list[str],
    pages_by_ats: dict[str, list[CompanyCareerPage]],
) -> tuple[list[ListingInput], str | None]:
    """Run one engine synchronously. Returns (listings, error_message|None).

    Strategy is driven entirely by engine.quirks and engine.fetch_strategy —
    no string comparisons against engine.name.
    """
    try:
        q = engine.quirks or {}

        # Per-company ATS board — identified by board_url_pattern quirk key
        ats_key = _quirk_ats_key(engine)
        if ats_key:
            fetcher = _ATS_FETCHERS[ats_key]
            pages = pages_by_ats.get(ats_key, [])
            if not pages:
                return [], (
                    f"No {engine.name} company pages registered "
                    "— add entries via /api/company-pages"
                )
            results = []
            for page in pages:
                results.extend(fetcher(page, role_types, keywords))
        elif engine.fetch_strategy == "api":
            results = _fetch_remotive(engine, role_types, keywords)
        elif engine.fetch_strategy == "rss":
            results = _fetch_rss_engine(engine, role_types, keywords)
        elif engine.fetch_strategy == "html":
            results = _fetch_html_engine(engine, role_types, keywords)
        elif engine.fetch_strategy == "linkedin":
            results = _fetch_linkedin_engine(engine, role_types, keywords)
        else:
            return [], f"Unsupported fetch_strategy '{engine.fetch_strategy}'"

        # Stamp engine quirks onto every listing so the pipeline can pass
        # them to the verifier (e.g. follow_redirect, may_403_direct). The
        # LinkedIn li_at cookie rides along the same channel, merged in from
        # search_params (a separate, editable-secrets field — not quirks)
        # since the verifier's engine_quirks dict is the only per-listing
        # context passed through to verify_url.
        quirks_out = dict(q)
        li_at = (engine.search_params or {}).get("li_at")
        if li_at:
            quirks_out["li_at_cookie"] = li_at
        if quirks_out:
            for lst in results:
                lst.engine_quirks = quirks_out

        return results, None
    except Exception as exc:
        return [], str(exc)


# ── Public entry point ────────────────────────────────────────────────────────

@dataclass
class CrawlSummary:
    engines_crawled: int
    engines_errored: int
    raw_listings_found: int
    engine_errors: list[dict]
    pipeline: PipelineSession


async def crawl_all_engines(
    db: Session,
    user_id: int,
    dry_run: bool = False,
    on_engine_done=None,
) -> CrawlSummary:
    """Crawl all active engines (a shared, global registry) and run the pipeline
    on results, scoped to the given user's tracker/blacklist/search params.

    Args:
        db: SQLAlchemy session.
        user_id: the user whose search params, blacklist, and tracker this crawl writes to.
        dry_run: If True, pipeline runs without saving to the tracker.
        on_engine_done: Optional async callable(engine_name, found, saved, skipped).
            Called after each engine's listings have been processed through the pipeline.
    """
    engines = db.query(SearchEngine).filter(SearchEngine.active == True).all()  # noqa: E712
    pages = db.query(CompanyCareerPage).filter(CompanyCareerPage.active == True).all()  # noqa: E712
    sp = db.query(SearchParams).filter_by(user_id=user_id).first()

    role_types = json.loads(sp.role_types) if sp else ["engineering manager"]
    keywords = json.loads(sp.keywords) if sp else []

    pages_by_ats: dict[str, list] = {}
    for page in pages:
        pages_by_ats.setdefault((page.ats_type or "").lower(), []).append(page)

    now = datetime.utcnow()
    errors: list[dict] = []
    all_listings: list[ListingInput] = []

    # Merged pipeline session accumulators (used when on_engine_done is None)
    merged_saved = 0
    merged_skipped = 0
    merged_resurfaced = 0
    merged_auto_removed = 0
    merged_skip_reasons: dict[str, int] = {}
    merged_results: list = []

    async def _process_engine(engine: SearchEngine) -> None:
        nonlocal merged_saved, merged_skipped, merged_resurfaced, merged_auto_removed

        listings, err = await asyncio.to_thread(
            _crawl_engine_sync, engine, role_types, keywords, pages_by_ats
        )
        engine.last_crawled_at = now

        if err:
            errors.append({"engine": engine.name, "error": err})
            if on_engine_done is not None:
                await on_engine_done(engine.name, 0, 0, 0)
            return

        all_listings.extend(listings)
        found = len(listings)

        if listings:
            ps = await run_pipeline(listings, db, user_id, dry_run=dry_run)
        else:
            ps = PipelineSession(
                submitted=0, saved=0, skipped=0, resurfaced=0, skip_reasons={}, results=[]
            )

        merged_saved += ps.saved
        merged_skipped += ps.skipped
        merged_resurfaced += ps.resurfaced
        merged_auto_removed += ps.auto_removed
        for k, v in ps.skip_reasons.items():
            merged_skip_reasons[k] = merged_skip_reasons.get(k, 0) + v
        merged_results.extend(ps.results)

        if on_engine_done is not None:
            await on_engine_done(engine.name, found, ps.saved, ps.skipped)

    # Run all engines concurrently
    await asyncio.gather(*[_process_engine(e) for e in engines])
    db.commit()

    pipeline_session = PipelineSession(
        submitted=len(all_listings),
        saved=merged_saved,
        skipped=merged_skipped,
        resurfaced=merged_resurfaced,
        auto_removed=merged_auto_removed,
        skip_reasons=merged_skip_reasons,
        results=merged_results,
    )

    return CrawlSummary(
        engines_crawled=len(engines) - len(errors),
        engines_errored=len(errors),
        raw_listings_found=len(all_listings),
        engine_errors=errors,
        pipeline=pipeline_session,
    )

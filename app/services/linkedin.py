"""
LinkedIn guest-API job fetching and parsing.

Ported from the recruiter skill's linkedin_fetch.py
(/home/macaco/.claude/commands/linkedin_fetch.py). LinkedIn's "guest" endpoints
(jobs-guest/jobs/api/...) return full job data given only an li_at session
cookie — no authenticated session/login flow required. The cookie is per-user
browser session state; callers must source it from the LinkedIn SearchEngine's
search_params field (app/models/search_engine.py), never from process env,
since it differs for every user of this app.
"""
import re
import urllib.parse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_JOB_ID_RE = re.compile(r"/jobs/view/(?:[^/?]*-)?(\d+)")
_GUEST_DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
_GUEST_SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"


def extract_job_id(url: str) -> str | None:
    m = _JOB_ID_RE.search(url or "")
    return m.group(1) if m else None


def guest_headers(cookie: str) -> dict:
    headers = dict(BROWSER_HEADERS)
    headers["Cookie"] = f"li_at={cookie}"
    return headers


def parse_job_detail(html: str, original_url: str) -> dict:
    """Parse a jobs-guest job-posting response into structured fields.

    Returns a dict with title/company/location/criteria/description. `criteria`
    holds LinkedIn's job-criteria list (Seniority level, Employment type, Job
    function, ...) keyed by their on-page label.
    """
    soup = BeautifulSoup(html, "html.parser")

    def text(selector: str) -> str:
        el = soup.select_one(selector)
        return el.get_text(" ", strip=True) if el else ""

    title = text("h2.top-card-layout__title") or text("h2")
    company = text("a.topcard__org-name-link") or text(".topcard__flavor")
    location = text("span.topcard__flavor--bullet") or text(".topcard__flavor:nth-of-type(2)")

    criteria: dict[str, str] = {}
    for li in soup.select("ul.description__job-criteria-list li"):
        label = li.select_one(".description__job-criteria-subheader")
        value = li.select_one(".description__job-criteria-text")
        if label and value:
            criteria[label.get_text(strip=True)] = value.get_text(strip=True)

    desc_el = (
        soup.select_one("div.show-more-less-html__markup")
        or soup.select_one("div.description__text")
    )
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    return {
        "url": original_url,
        "title": title,
        "company": company,
        "location": location,
        "criteria": criteria,
        "description": description,
    }


def seniority_hint(criteria: dict) -> str | None:
    """Extract LinkedIn's structured seniority signal from a parsed criteria dict."""
    return criteria.get("Seniority level")


def get_configured_cookie(db: Session) -> str | None:
    """Look up the li_at cookie from the LinkedIn SearchEngine's search_params.

    Shared by every call site that needs to verify or fetch a LinkedIn URL
    outside the crawler's own engine loop — the crawler stamps the cookie onto
    its own listings directly from the engine row it's already iterating, but
    other callers (e.g. the "Check if still live" button, manual URL
    submission) only have a db session and a URL, so they look it up here.
    """
    from app.models.search_engine import SearchEngine

    engine = (
        db.query(SearchEngine)
        .filter(SearchEngine.fetch_strategy == "linkedin")
        .first()
    )
    if engine is None:
        return None
    return (engine.search_params or {}).get("li_at")


def fetch_job_detail(job_id: str, cookie: str, client: httpx.Client | None = None) -> str:
    """Fetch the raw jobs-guest job-posting HTML for *job_id*.

    Does not raise on non-200 status — a removed/invalid job ID typically
    still returns 200 with an empty posting shell, so callers should check
    the parsed title/description rather than the HTTP status. Only network
    errors (timeout, connection refused, ...) propagate as httpx.RequestError.
    """
    url = _GUEST_DETAIL_URL.format(job_id=job_id)
    if client is not None:
        resp = client.get(url, headers=guest_headers(cookie))
        return resp.text
    with httpx.Client(timeout=15, follow_redirects=True) as c:
        resp = c.get(url, headers=guest_headers(cookie))
        return resp.text


def get_job_detail(
    url: str, cookie: str, client: httpx.Client | None = None
) -> dict | None:
    """Fetch and parse a single LinkedIn job posting given its /jobs/view/<id> URL.

    Returns None if a job ID can't be extracted from *url*.
    """
    job_id = extract_job_id(url)
    if not job_id:
        return None
    html = fetch_job_detail(job_id, cookie, client)
    return parse_job_detail(html, url)


def search_jobs(
    keywords: str, cookie: str, location: str = "", start: int = 0
) -> list[dict]:
    """Query LinkedIn's guest search API for job stubs matching *keywords*.

    Returns a list of dicts with `job_id`, `title`, `company`, `location`, `url`.
    This endpoint is undocumented, so parsing failures degrade to an empty
    list rather than raising.
    """
    params = {"keywords": keywords, "start": start}
    if location:
        params["location"] = location
    url = _GUEST_SEARCH_URL + "?" + urllib.parse.urlencode(params)

    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            resp = client.get(url, headers=guest_headers(cookie))
            if resp.status_code != 200:
                return []
            html = resp.text
    except httpx.RequestError:
        return []

    soup = BeautifulSoup(html, "html.parser")
    results: list[dict] = []
    for card in soup.select("li"):
        link = card.select_one("a.base-card__full-link")
        if not link:
            continue
        href = (link.get("href") or "").split("?")[0]
        job_id = extract_job_id(href)
        if not job_id:
            continue
        title_el = card.select_one("h3.base-search-card__title")
        company_el = card.select_one("h4.base-search-card__subtitle")
        location_el = card.select_one("span.job-search-card__location")
        results.append({
            "job_id": job_id,
            "title": title_el.get_text(strip=True) if title_el else "",
            "company": company_el.get_text(strip=True) if company_el else "",
            "location": location_el.get_text(strip=True) if location_el else "",
            "url": href,
        })
    return results

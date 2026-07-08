"""
Listing verification service.

Given an ATS URL, fetches it directly and determines whether the listing
is still active. Returns one of three states:
  - "active"     : listing confirmed live
  - "expired"    : 404/410, title signals closure, or ATS API confirms gone
  - "unverified" : network error, timeout, 403, or ambiguous response

ATS-specific handling:
  Ashby    — GraphQL API (jobs.ashbyhq.com/api/non-user-graphql); null = expired
  LinkedIn — guest API (jobs-guest/jobs/api/jobPosting/<id>) with li_at cookie;
             empty posting = expired; no cookie available = unverified (skip)
  Workable — redirect to ?not_found=true = expired
  Lever    — HTML title "Not found – 404 error" caught by title signals
  Others   — HTTP status + title heuristic
"""
import re
from dataclasses import dataclass
from typing import Literal

import httpx

from app.services import linkedin as linkedin_service
from app.services.url_safety import UnsafeUrlError, assert_safe_external_url

# Quirk keys used at runtime:
#   login_wall  (bool)  — treat URL as unverifiable login wall; skip verification
#   skip        (bool)  — alias for login_wall
#   follow_redirect (bool) — check final URL for ?not_found=true redirect (Workable-style)
#   li_at_cookie (str)  — LinkedIn session cookie, sourced from the LinkedIn
#                         engine's search_params field; enables _verify_linkedin

_EXPIRED_TITLE_SIGNALS = [
    "job not found",
    "position not found",
    "listing not found",
    "page not found",
    "404",
    "not found",
    "no longer accepting",
    "this position has been filled",
    "job has been closed",
    "job is closed",
    "application closed",
    "posting not found",
    "opportunity not found",
]

_ASHBY_GRAPHQL = "https://jobs.ashbyhq.com/api/non-user-graphql"
_ASHBY_URL_RE = re.compile(
    r"https://jobs\.ashbyhq\.com/([^/]+)/([0-9a-f\-]{36})", re.IGNORECASE
)
_ASHBY_QUERY = """
query ApiJobPostingShow($org: String!, $id: String!) {
  jobPosting(organizationHostedJobsPageName: $org, jobPostingId: $id) {
    id title isListed
  }
}
""".strip()

VerificationStatus = Literal["active", "expired", "unverified"]


@dataclass
class VerificationResult:
    status: VerificationStatus
    reason: str
    checked_url: str
    http_status: int | None = None
    title: str | None = None


def _extract_title(html: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(1)).strip()


def _title_signals_expired(title: str) -> bool:
    lower = title.lower()
    return any(signal in lower for signal in _EXPIRED_TITLE_SIGNALS)


def _verify_ashby(url: str, client: httpx.Client) -> VerificationResult | None:
    """Return a result if URL is an Ashby job posting, else None."""
    m = _ASHBY_URL_RE.match(url)
    if not m:
        return None
    org, job_id = m.group(1), m.group(2)
    try:
        resp = client.post(
            _ASHBY_GRAPHQL,
            json={
                "operationName": "ApiJobPostingShow",
                "variables": {"org": org, "id": job_id},
                "query": _ASHBY_QUERY,
            },
            timeout=10.0,
        )
        data = resp.json().get("data", {})
        posting = data.get("jobPosting")
        if posting is None:
            return VerificationResult(
                status="expired",
                reason="Ashby API: jobPosting is null (listing removed)",
                checked_url=url,
                http_status=resp.status_code,
            )
        if not posting.get("isListed", True):
            return VerificationResult(
                status="expired",
                reason=f"Ashby API: isListed=false for '{posting.get('title')}'",
                checked_url=url,
                http_status=resp.status_code,
                title=posting.get("title"),
            )
        return VerificationResult(
            status="active",
            reason=f"Ashby API: isListed=true — '{posting.get('title')}'",
            checked_url=url,
            http_status=resp.status_code,
            title=posting.get("title"),
        )
    except Exception as exc:
        return VerificationResult(
            status="unverified",
            reason=f"Ashby API error: {exc}",
            checked_url=url,
        )


def _verify_linkedin(url: str, client: httpx.Client, quirks: dict) -> VerificationResult | None:
    """Return a result if URL is a LinkedIn job posting, else None.

    Uses the jobs-guest guest API with the li_at session cookie sourced from
    quirks['li_at_cookie'] (populated at crawl time from the LinkedIn engine's
    search_params field — see app/models/search_engine.py). Without a cookie,
    LinkedIn URLs can't be verified at all — same unverifiable-login-wall
    outcome as before this feature existed.

    An empty parsed posting (no title, no description) is reported as
    "unverified" rather than "expired": the guest API can return the same
    empty shell whether the listing was actually removed or the li_at cookie
    has simply expired (cookies need periodic manual refresh — see quirks),
    and we'd rather under-report expiry than mass-expire live tracker records
    because of a stale cookie.
    """
    if "linkedin.com" not in url:
        return None

    cookie = quirks.get("li_at_cookie")
    if not cookie:
        return VerificationResult(
            status="unverified",
            reason="LinkedIn login wall — no li_at cookie configured for this engine",
            checked_url=url,
        )

    job_id = linkedin_service.extract_job_id(url)
    if not job_id:
        return VerificationResult(
            status="unverified",
            reason="Could not extract LinkedIn job ID from URL",
            checked_url=url,
        )

    try:
        html = linkedin_service.fetch_job_detail(job_id, cookie, client)
    except httpx.RequestError as exc:
        return VerificationResult(
            status="unverified",
            reason=f"LinkedIn guest API network error: {exc}",
            checked_url=url,
        )

    detail = linkedin_service.parse_job_detail(html, url)
    if not detail["title"] and not detail["description"]:
        return VerificationResult(
            status="unverified",
            reason="LinkedIn guest API returned no posting data (listing removed, or li_at cookie expired — refresh it on the Engines page)",
            checked_url=url,
        )
    return VerificationResult(
        status="active",
        reason=f"LinkedIn guest API: posting found — '{detail['title']}'",
        checked_url=url,
        title=detail["title"],
    )


def verify_url(
    url: str,
    timeout: float = 10.0,
    quirks: dict | None = None,
) -> VerificationResult:
    """Fetch *url* and return its verification status.

    Args:
        url: The job posting URL to verify.
        timeout: Request timeout in seconds.
        quirks: Optional engine quirks dict.  Recognised keys:
            login_wall / skip (bool) — mark as unverifiable without fetching.
            follow_redirect (bool)   — check final URL for ?not_found=true
                                       redirect (Workable-style); always
                                       checked when the URL domain already
                                       contains "workable", but the quirk
                                       makes it explicit for any engine.
            li_at_cookie (str)       — LinkedIn session cookie; enables guest-API
                                       verification of linkedin.com URLs instead
                                       of the unverifiable-login-wall fallback.
    """
    q = quirks or {}

    # Quirk: login_wall / skip — don't bother fetching
    if q.get("login_wall") or q.get("skip"):
        return VerificationResult(
            status="unverified",
            reason="Engine quirk: login_wall/skip — verification skipped",
            checked_url=url,
        )

    # Decide whether to check for Workable-style ?not_found=true redirect:
    # always if the quirk is set, or when the URL itself is on workable.com.
    check_redirect = q.get("follow_redirect", False) or "workable.com" in url

    # SSRF-1: refuse to fetch anything that doesn't resolve to a public address.
    try:
        assert_safe_external_url(url)
    except UnsafeUrlError as exc:
        return VerificationResult(status="unverified", reason=f"Blocked unsafe URL: {exc}", checked_url=url)

    with httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0 (compatible; JobSearch-Verifier/1.0)"},
    ) as client:
        # Ashby — use GraphQL API instead of HTML scrape
        ashby_result = _verify_ashby(url, client)
        if ashby_result is not None:
            return ashby_result

        # LinkedIn — guest API with li_at cookie instead of a direct HTML fetch
        # (a direct GET without the cookie always hits the login wall)
        linkedin_result = _verify_linkedin(url, client, q)
        if linkedin_result is not None:
            return linkedin_result

        try:
            response = client.get(url)
        except httpx.TimeoutException:
            return VerificationResult(
                status="unverified", reason="Request timed out", checked_url=url
            )
        except httpx.RequestError as exc:
            return VerificationResult(
                status="unverified", reason=f"Network error: {exc}", checked_url=url
            )

        final_url = str(response.url)

        if response.status_code in (404, 410):
            return VerificationResult(
                status="expired",
                reason=f"HTTP {response.status_code}",
                checked_url=final_url,
                http_status=response.status_code,
            )

        if response.status_code == 403:
            return VerificationResult(
                status="unverified",
                reason="HTTP 403 — possible IP block; cannot verify",
                checked_url=final_url,
                http_status=403,
            )

        if response.status_code != 200:
            return VerificationResult(
                status="unverified",
                reason=f"Unexpected HTTP {response.status_code}",
                checked_url=final_url,
                http_status=response.status_code,
            )

        # Workable redirects expired listings to ?not_found=true
        if check_redirect and "not_found=true" in final_url:
            return VerificationResult(
                status="expired",
                reason="Workable redirected to ?not_found=true",
                checked_url=final_url,
                http_status=200,
            )

        title = _extract_title(response.text)

        if title is None:
            return VerificationResult(
                status="unverified",
                reason="HTTP 200 but no <title> tag found",
                checked_url=final_url,
                http_status=200,
            )

        if _title_signals_expired(title):
            return VerificationResult(
                status="expired",
                reason=f"Title signals closed listing: '{title}'",
                checked_url=final_url,
                http_status=200,
                title=title,
            )

        return VerificationResult(
            status="active",
            reason="HTTP 200 with valid title",
            checked_url=final_url,
            http_status=200,
            title=title,
        )

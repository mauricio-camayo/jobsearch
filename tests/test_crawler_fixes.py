"""
Unit tests for the 2026-07-07 live-engine-health-check fixes (PRIORITIES.md #62-64).

All three engines' underlying third-party APIs/HTML changed shape since these
fetchers were written, silently returning 0 listings for every company on that
engine. httpx is mocked here (no live network) so these tests are deterministic;
the fixes themselves were verified against the real live APIs during development.

CR1 — Ashby: _fetch_ashby_company() uses the new two-step
      jobBoardWithTeams (list) + jobPosting (detail) query pair, since the old
      flat jobPostings(...) root query no longer exists.
CR2 — Ashby: a posting with isListed=false in the detail response is excluded.
CR3 — Workable: _fetch_workable_company() sends "remote": [] (array, not bool)
      and handles the new structured `location` object without crashing.
CR4 — HTML engine: relative-path hrefs (no leading "http") are resolved to
      absolute URLs via urljoin() instead of being silently dropped.
"""
import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.crawler import _fetch_ashby_company, _fetch_workable_company, _fetch_html_engine


class _FakePage:
    def __init__(self, company, careers_url):
        self.company = company
        self.careers_url = careers_url


class _FakeResponse:
    def __init__(self, json_data=None, status_code=200, text="", url=""):
        self._json = json_data
        self.status_code = status_code
        self.text = text
        self.url = url

    def json(self):
        return self._json


def _fake_client(post_side_effect=None, get_return=None):
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    if post_side_effect is not None:
        client.post.side_effect = post_side_effect
    if get_return is not None:
        client.get.return_value = get_return
    return client


# ── CR1/CR2 — Ashby two-step fetch ──────────────────────────────────────────────

def test_cr1_ashby_two_step_fetch_returns_relevant_listing():
    list_resp = _FakeResponse({
        "data": {
            "jobBoardWithTeams": {
                "jobPostings": [
                    {"id": "job-1", "title": "Engineering Manager, Platform"},
                    {"id": "job-2", "title": "Account Executive"},  # not relevant
                ]
            }
        }
    })
    detail_resp = _FakeResponse({
        "data": {
            "jobPosting": {
                "id": "job-1", "title": "Engineering Manager, Platform",
                "isListed": True, "locationName": "Remote - Worldwide",
                "descriptionHtml": "<p>Lead the platform team.</p>",
            }
        }
    })

    client = _fake_client(post_side_effect=[list_resp, detail_resp])
    with patch("app.services.crawler.httpx.Client", return_value=client):
        results = _fetch_ashby_company(
            _FakePage("Acme", "https://jobs.ashbyhq.com/acme"),
            ["engineering manager"], [],
        )

    assert len(results) == 1
    assert results[0].role_title == "Engineering Manager, Platform"
    assert results[0].apply_url == "https://jobs.ashbyhq.com/acme/job-1"
    assert "platform team" in results[0].description
    # Only the relevant posting should have triggered a detail fetch (1 list + 1 detail call).
    assert client.post.call_count == 2


def test_cr2_ashby_excludes_unlisted_posting():
    list_resp = _FakeResponse({
        "data": {"jobBoardWithTeams": {"jobPostings": [
            {"id": "job-1", "title": "Engineering Manager"},
        ]}}
    })
    detail_resp = _FakeResponse({
        "data": {"jobPosting": {"id": "job-1", "title": "Engineering Manager", "isListed": False}}
    })
    client = _fake_client(post_side_effect=[list_resp, detail_resp])
    with patch("app.services.crawler.httpx.Client", return_value=client):
        results = _fetch_ashby_company(
            _FakePage("Acme", "https://jobs.ashbyhq.com/acme"),
            ["engineering manager"], [],
        )
    assert results == []


# ── CR3 — Workable: array "remote" param + structured location object ──────────

def test_cr3_workable_handles_structured_location_without_crashing():
    resp = _FakeResponse({
        "results": [
            {
                "title": "Engineering Manager", "shortcode": "ABC123",
                "remote": True, "workplace": "remote",
                "location": {"country": "Brazil", "city": None, "region": None},
            },
        ]
    }, status_code=200)
    client = _fake_client()
    client.post.return_value = resp
    with patch("app.services.crawler.httpx.Client", return_value=client):
        results = _fetch_workable_company(
            _FakePage("Acme", "https://apply.workable.com/acme"),
            ["engineering manager"], [],
        )

    assert len(results) == 1
    assert results[0].role_title == "Engineering Manager"
    assert results[0].remote_type == "remote"
    assert results[0].geo == "brazil"
    assert results[0].apply_url == "https://apply.workable.com/acme/j/ABC123"

    # Confirm the request body sends "remote" as an array, not a bool.
    sent_json = client.post.call_args.kwargs.get("json")
    assert sent_json["remote"] == []


# ── CR4 — generic HTML engine: relative href resolution ─────────────────────────

def test_cr4_html_engine_resolves_relative_hrefs():
    html = """
    <html><body>
      <a href="/golang-go-job-abc-Senior-Backend-Engineer.html">Senior Backend Engineer</a>
      <a href="/about.html">About</a>
    </body></html>
    """
    resp = _FakeResponse(text=html, status_code=200, url="https://www.golangprojects.com/golang-remote-jobs.html")
    client = _fake_client()
    client.get.return_value = resp

    class _FakeEngine:
        name = "golangprojects"
        search_url_template = "https://www.golangprojects.com/golang-remote-jobs.html"

    with patch("app.services.crawler.httpx.Client", return_value=client):
        results = _fetch_html_engine(_FakeEngine(), [], [])

    assert len(results) == 1
    assert results[0].apply_url == "https://www.golangprojects.com/golang-go-job-abc-Senior-Backend-Engineer.html"

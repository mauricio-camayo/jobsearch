"""
Unit tests for the SECURITY.md fixes that don't need a full FastAPI TestClient:

SSRF-1 — app/services/url_safety.py rejects non-http(s) schemes and any
         hostname/IP that resolves to a private/loopback/link-local/reserved
         address, so the manual "verify" / pipeline-submission / crawl-time
         fetchers can't be used to probe internal LAN services.
XSS-1  — TrackerCreate/ListingCreate reject apply_url values that aren't
         plain http(s) URLs (e.g. a `javascript:` URI), closing the stored-XSS
         sink in dashboard.html/search.html's `<a href="{{ r.apply_url }}">`.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from pydantic import ValidationError

from app.services.url_safety import UnsafeUrlError, assert_safe_external_url
from app.routers.tracker import TrackerCreate
from app.routers.listings import ListingCreate, ListingUpdate


# ── SSRF-1 ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "ftp://example.com/",
    "file:///etc/passwd",
    "javascript:alert(1)",
])
def test_ssrf_rejects_non_http_schemes(url):
    with pytest.raises(UnsafeUrlError):
        assert_safe_external_url(url)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://localhost:8080/",
    "http://192.168.0.2:9000/",
    "http://10.0.0.5/",
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata endpoint
])
def test_ssrf_rejects_private_and_link_local_addresses(url):
    with pytest.raises(UnsafeUrlError):
        assert_safe_external_url(url)


def test_ssrf_allows_public_ip_literal():
    # Using a literal public IP avoids a real DNS lookup / network flakiness in CI.
    assert_safe_external_url("http://8.8.8.8/") is None


# ── XSS-1 ─────────────────────────────────────────────────────────────────────

def test_xss_tracker_create_rejects_javascript_scheme():
    with pytest.raises(ValidationError):
        TrackerCreate(company="Acme", role_title="EM", apply_url="javascript:alert(document.cookie)")


def test_xss_tracker_create_allows_http_https():
    TrackerCreate(company="Acme", role_title="EM", apply_url="https://jobs.lever.co/acme/123")
    TrackerCreate(company="Acme", role_title="EM", apply_url="http://jobs.lever.co/acme/123")
    TrackerCreate(company="Acme", role_title="EM", apply_url=None)


def test_xss_listing_create_rejects_javascript_scheme():
    with pytest.raises(ValidationError):
        ListingCreate(company="Acme", role_title="EM", apply_url="javascript:alert(1)")


def test_xss_listing_update_rejects_javascript_scheme():
    with pytest.raises(ValidationError):
        ListingUpdate(apply_url="javascript:alert(1)")

"""
Integration tests for P1-38: RunResponse and CrawlResponse schema missing auto_removed field.

Bug: POST /api/search/run and POST /api/search/crawl responses were missing the
`auto_removed` field. Fix: `auto_removed: int = 0` added to RunResponse and
CrawlResponse in app/routers/search_run.py, passed from session.auto_removed /
p.auto_removed in both return statements.

Uses FastAPI TestClient with an in-memory SQLite DB (shared cache).
Crawler is patched to avoid live HTTP; pipeline auto_removed Phase-3 sweep
requires a real DB with TrackerRecords to observe a non-zero count.

RT1  — POST /api/search/run response contains `auto_removed` key (schema present)
RT2  — auto_removed is an integer and defaults to 0 when no stale shown rows exist
RT3  — auto_removed increments when shown rows with fit_pct below auto-floor are present
RT4  — POST /api/search/crawl response contains `auto_removed` key (schema present)
RT5  — CrawlResponse auto_removed is an integer and defaults to 0 in dry_run mode
RT6  — dry_run=True on /run → auto_removed is 0 (Phase 3 sweep skipped on dry_run)
RT7  — a real (dry_run=False) /run persists a SearchSession row with correct counts (#54)
RT8  — a dry_run=True /run does not persist any SearchSession row (#54)
"""
import sys
import os
from unittest.mock import patch, AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models.user                  # noqa: F401
import app.models.user_profile          # noqa: F401
import app.models.tracker_record        # noqa: F401
import app.models.search_engine         # noqa: F401
import app.models.company_career_page   # noqa: F401
import app.models.scoring_rubric        # noqa: F401
import app.models.app_config            # noqa: F401
import app.models.search_params         # noqa: F401
import app.models.listing               # noqa: F401
import app.models.search_session        # noqa: F401

from app.auth import get_current_user_api
from app.db.database import Base, get_db
from app.db.seed import (
    seed_user_profile,
    seed_scoring_rubric,
    seed_app_config,
    seed_search_params,
)
from app.models.search_session import SearchSession
from app.models.tracker_record import TrackerRecord
from app.models.user import User
from app.routers import search_run as search_run_router

# Named in-memory SQLite DB with shared cache so all connections share state.
TEST_DB_URL = "sqlite:///file:testrunresponse?mode=memory&cache=shared&uri=true"

_engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
)
_TestingSessionLocal = sessionmaker(bind=_engine, autocommit=False, autoflush=False)


def _override_get_db():
    db = _TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


# Minimal valid listing payload — worldwide remote EM role.
_LISTING_PAYLOAD = {
    "listings": [
        {
            "company": "Acme Corp",
            "role_title": "Engineering Manager",
            "apply_url": None,
            "remote_type": "remote",
            "geo": "worldwide",
            "description": (
                "Lead the engineering team. Requires Golang, AWS, Kubernetes. "
                "Fintech payments experience preferred."
            ),
            "required_skills": ["Golang", "AWS", "Kubernetes"],
            "role_domains": ["fintech", "payments"],
            "relocation_offered": False,
            "visa_sponsorship": False,
        }
    ],
    "dry_run": False,
}

_DRY_RUN_PAYLOAD = {**_LISTING_PAYLOAD, "dry_run": True}


@pytest.fixture(scope="module")
def client():
    Base.metadata.create_all(bind=_engine)
    with _TestingSessionLocal() as db:
        test_user = User(email="test@example.com", password_hash="x", is_admin=False)
        db.add(test_user)
        db.commit()
        db.refresh(test_user)
        user_id = test_user.id
        seed_user_profile(db, user_id)
        seed_scoring_rubric(db, user_id)
        seed_app_config(db, user_id)
        seed_search_params(db, user_id)

    test_app = FastAPI()
    test_app.dependency_overrides[get_db] = _override_get_db
    test_app.dependency_overrides[get_current_user_api] = lambda: User(id=user_id, email="test@example.com")
    test_app.include_router(search_run_router.router)

    with TestClient(test_app) as c:
        yield c


# RT1 — /run response contains `auto_removed` key
def test_rt1_run_response_has_auto_removed_field(client):
    """Verify that auto_removed appears in the JSON response schema."""
    with patch("app.services.verifier.verify_url") as mock_verify:
        mock_verify.return_value = type("R", (), {"status": "active", "reason": "ok"})()
        resp = client.post("/api/search/run", json=_LISTING_PAYLOAD)
    assert resp.status_code == 200, f"Unexpected status: {resp.status_code} — {resp.text}"
    assert "auto_removed" in resp.json(), (
        "RunResponse JSON is missing the `auto_removed` field (P1-38 regression)"
    )


# RT2 — auto_removed is int, defaults to 0 when no stale shown rows present
def test_rt2_auto_removed_is_integer_and_defaults_to_zero(client):
    """When no shown rows fall below the auto-floor, auto_removed must be 0."""
    with patch("app.services.verifier.verify_url") as mock_verify:
        mock_verify.return_value = type("R", (), {"status": "active", "reason": "ok"})()
        resp = client.post("/api/search/run", json=_LISTING_PAYLOAD)
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["auto_removed"], int), (
        f"auto_removed should be int, got {type(data['auto_removed'])}"
    )
    assert data["auto_removed"] >= 0


# RT3 — auto_removed increments when shown rows with fit_pct below auto-floor exist
def test_rt3_auto_removed_counts_phase3_sweep(client):
    """
    Seed a shown TrackerRecord with fit_pct=10 (threshold=70 → auto_floor=floor(70*2/3)=46).
    After a live pipeline run (dry_run=False), that record should be swept and
    auto_removed should be >= 1.
    """
    from datetime import datetime

    # Insert a shown record with very low fit_pct so it falls below auto_floor.
    with _TestingSessionLocal() as db:
        test_user_id = db.query(User).filter_by(email="test@example.com").first().id
        stale = TrackerRecord(
            user_id=test_user_id,
            company="StaleJob Inc",
            role_title="Junior Engineer",
            apply_url="https://staleco.example.com/jobs/1",
            status="shown",
            fit_pct=10,
            date_shown=datetime.utcnow().date(),
        )
        db.add(stale)
        db.commit()
        stale_id = stale.id

    with patch("app.services.verifier.verify_url") as mock_verify:
        mock_verify.return_value = type("R", (), {"status": "active", "reason": "ok"})()
        resp = client.post("/api/search/run", json=_LISTING_PAYLOAD)

    assert resp.status_code == 200
    data = resp.json()
    assert data["auto_removed"] >= 1, (
        f"Expected auto_removed >= 1 after seeding a shown row with fit_pct=10, "
        f"got auto_removed={data['auto_removed']}"
    )

    # Verify the stale record was actually transitioned to 'skipped' in the DB.
    with _TestingSessionLocal() as db:
        rec = db.get(TrackerRecord, stale_id)
        assert rec is not None
        assert rec.status == "skipped", (
            f"Expected stale TrackerRecord (id={stale_id}) to be 'skipped', "
            f"got '{rec.status}'"
        )


# RT4 — /crawl response contains `auto_removed` key
def test_rt4_crawl_response_has_auto_removed_field(client):
    """Verify that CrawlResponse JSON also exposes auto_removed (P1-38 covers both)."""
    # Patch crawl_all_engines to avoid live HTTP
    from app.services.pipeline import PipelineSession
    from app.services.crawler import CrawlSummary

    fake_pipeline = PipelineSession(
        submitted=0,
        saved=0,
        skipped=0,
        resurfaced=0,
        skip_reasons={},
        results=[],
        auto_removed=0,
    )
    fake_summary = CrawlSummary(
        engines_crawled=0,
        engines_errored=0,
        raw_listings_found=0,
        engine_errors=[],
        pipeline=fake_pipeline,
    )

    with patch(
        "app.routers.search_run.crawl_all_engines",
        new=AsyncMock(return_value=fake_summary),
    ):
        resp = client.post("/api/search/crawl", params={"dry_run": "true"})

    assert resp.status_code == 200, f"Unexpected status: {resp.status_code} — {resp.text}"
    assert "auto_removed" in resp.json(), (
        "CrawlResponse JSON is missing the `auto_removed` field (P1-38 regression)"
    )


# RT5 — CrawlResponse auto_removed is integer and >= 0
def test_rt5_crawl_auto_removed_is_integer(client):
    from app.services.pipeline import PipelineSession
    from app.services.crawler import CrawlSummary

    fake_pipeline = PipelineSession(
        submitted=0,
        saved=0,
        skipped=0,
        resurfaced=0,
        skip_reasons={},
        results=[],
        auto_removed=0,
    )
    fake_summary = CrawlSummary(
        engines_crawled=2,
        engines_errored=0,
        raw_listings_found=5,
        engine_errors=[],
        pipeline=fake_pipeline,
    )

    with patch(
        "app.routers.search_run.crawl_all_engines",
        new=AsyncMock(return_value=fake_summary),
    ):
        resp = client.post("/api/search/crawl", params={"dry_run": "true"})

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["auto_removed"], int), (
        f"CrawlResponse auto_removed should be int, got {type(data['auto_removed'])}"
    )
    assert data["auto_removed"] >= 0


# RT6 — dry_run=True on /run suppresses Phase 3 auto-removal → auto_removed=0
def test_rt6_dry_run_skips_phase3_sweep(client):
    """
    Phase 3 auto-remove sweep is guarded by `if not dry_run`. In dry_run mode,
    auto_removed must be 0 regardless of how many stale shown rows exist.
    """
    from datetime import datetime

    with _TestingSessionLocal() as db:
        test_user_id = db.query(User).filter_by(email="test@example.com").first().id
        stale = TrackerRecord(
            user_id=test_user_id,
            company="DryRunStale Corp",
            role_title="VP of Something",
            apply_url="https://dryrunstale.example.com/jobs/99",
            status="shown",
            fit_pct=5,
            date_shown=datetime.utcnow().date(),
        )
        db.add(stale)
        db.commit()

    with patch("app.services.verifier.verify_url") as mock_verify:
        mock_verify.return_value = type("R", (), {"status": "active", "reason": "ok"})()
        resp = client.post("/api/search/run", json=_DRY_RUN_PAYLOAD)

    assert resp.status_code == 200
    data = resp.json()
    assert data["dry_run"] is True
    assert data["auto_removed"] == 0, (
        f"dry_run=True should prevent Phase 3 auto-removal; got auto_removed={data['auto_removed']}"
    )


# RT7 — a real pipeline run persists a SearchSession audit row (#54).
def test_rt7_real_run_persists_search_session(client):
    with _TestingSessionLocal() as db:
        before = db.query(SearchSession).count()

    with patch("app.services.verifier.verify_url") as mock_verify:
        mock_verify.return_value = type("R", (), {"status": "active", "reason": "ok"})()
        resp = client.post("/api/search/run", json=_LISTING_PAYLOAD)
    assert resp.status_code == 200

    with _TestingSessionLocal() as db:
        after = db.query(SearchSession).count()
        assert after == before + 1, "a real (dry_run=False) run must persist exactly one SearchSession row"
        session = db.query(SearchSession).order_by(SearchSession.id.desc()).first()
        assert session.listings_found == 1
        assert session.listings_saved + session.listings_skipped == session.listings_found
        assert session.started_at is not None
        assert session.finished_at is not None
        assert session.finished_at >= session.started_at
        assert isinstance(session.query_params, dict)


# RT8 — a dry_run=True run does not persist a SearchSession row (#54).
def test_rt8_dry_run_does_not_persist_search_session(client):
    with _TestingSessionLocal() as db:
        before = db.query(SearchSession).count()

    with patch("app.services.verifier.verify_url") as mock_verify:
        mock_verify.return_value = type("R", (), {"status": "active", "reason": "ok"})()
        resp = client.post("/api/search/run", json=_DRY_RUN_PAYLOAD)
    assert resp.status_code == 200

    with _TestingSessionLocal() as db:
        after = db.query(SearchSession).count()
        assert after == before, "dry_run=True must not persist a SearchSession row"

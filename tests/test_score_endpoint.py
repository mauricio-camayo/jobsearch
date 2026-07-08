"""
Integration tests for POST /api/score and contract test CT1.

Uses FastAPI TestClient with an in-memory SQLite DB (no file I/O).
The app's get_db dependency is overridden to use the in-memory session.

IT1 — minimal valid request returns 200 with expected keys
IT2 — empty description/domains/skills → domain_match=0, tech_stack=0
IT3 — enriched request (description + role_domains + required_skills) scores > empty
IT4 — missing role_title returns 422
IT5 — exceeds_threshold is True when total_score >= threshold

CT1 — enriched payload scores ≥40 points higher than empty payload (VGS regression guard)
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Import all models first so they register with Base before create_all is called
import app.models.user              # noqa: F401
import app.models.user_profile      # noqa: F401
import app.models.tracker_record    # noqa: F401
import app.models.search_engine     # noqa: F401
import app.models.company_career_page  # noqa: F401
import app.models.scoring_rubric    # noqa: F401
import app.models.app_config        # noqa: F401
import app.models.search_params     # noqa: F401
import app.models.listing           # noqa: F401

from app.auth import get_current_user_api
from app.db.database import Base, get_db
from app.db.seed import seed_user_profile, seed_scoring_rubric, seed_app_config
from app.models.user import User
from fastapi import FastAPI
from app.routers import scoring as scoring_router

# Use a named in-memory DB with shared cache so all connections see the same data.
# Plain "sqlite://" gives each connection its own empty DB.
TEST_DB_URL = "sqlite:///file:testscoring?mode=memory&cache=shared&uri=true"

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

    test_app = FastAPI()
    test_app.dependency_overrides[get_db] = _override_get_db
    test_app.dependency_overrides[get_current_user_api] = lambda: User(id=user_id, email="test@example.com")
    test_app.include_router(scoring_router.router)

    with TestClient(test_app) as c:
        yield c


_EMPTY_PAYLOAD = {
    "role_title": "Engineering Manager",
    "description": "",
    "required_skills": [],
    "role_domains": [],
    "remote_type": "unknown",
    "geo_restriction": "unknown",
}

# VGS-style enriched payload — mimics what the recruiter skill should send
_ENRICHED_PAYLOAD = {
    "role_title": "Engineering Manager, Payments",
    "description": (
        "We are looking for an Engineering Manager to lead our payments platform team. "
        "You will work with Golang, Python, AWS, Kubernetes, and PostgreSQL. "
        "You will drive security best practices, including OWASP and compliance. "
        "Experience with fintech, distributed systems, and payment processing is required."
    ),
    "required_skills": ["Golang", "Python", "AWS", "Kubernetes", "PostgreSQL"],
    "role_domains": ["payments", "fintech", "security"],
    "remote_type": "remote",
    "geo_restriction": "worldwide",
}


# IT1 — minimal valid request returns 200 with expected keys
def test_it1_minimal_request_returns_200(client):
    resp = client.post("/api/score", json={"role_title": "Engineering Manager"})
    assert resp.status_code == 200
    data = resp.json()
    assert "total_score" in data
    assert "exceeds_threshold" in data
    assert "threshold" in data
    assert "breakdown" in data


# IT2 — empty description/domains/skills → domain_match=0, tech_stack=0
def test_it2_empty_payload_heavy_dims_zero(client):
    resp = client.post("/api/score", json=_EMPTY_PAYLOAD)
    assert resp.status_code == 200
    breakdown = resp.json()["breakdown"]
    assert breakdown["domain_match"]["score"] == 0
    assert breakdown["tech_stack"]["score"] == 0


# IT3 — enriched request scores higher than empty request
def test_it3_enriched_scores_higher_than_empty(client):
    empty_resp = client.post("/api/score", json=_EMPTY_PAYLOAD)
    enriched_resp = client.post("/api/score", json=_ENRICHED_PAYLOAD)
    assert empty_resp.status_code == 200
    assert enriched_resp.status_code == 200
    assert enriched_resp.json()["total_score"] > empty_resp.json()["total_score"]


# IT4 — missing role_title returns 422
def test_it4_missing_role_title_returns_422(client):
    resp = client.post("/api/score", json={"description": "some desc"})
    assert resp.status_code == 422


# IT5 — exceeds_threshold is True when total_score >= 70
def test_it5_exceeds_threshold_flag(client):
    resp = client.post("/api/score", json=_ENRICHED_PAYLOAD)
    assert resp.status_code == 200
    data = resp.json()
    expected = data["total_score"] >= data["threshold"]
    assert data["exceeds_threshold"] == expected


# CT1 — enriched payload must score ≥40 points higher than empty payload (regression guard)
def test_ct1_enriched_vs_empty_gap_at_least_40(client):
    empty_resp = client.post("/api/score", json=_EMPTY_PAYLOAD)
    enriched_resp = client.post("/api/score", json=_ENRICHED_PAYLOAD)
    assert empty_resp.status_code == 200
    assert enriched_resp.status_code == 200
    empty_score = empty_resp.json()["total_score"]
    enriched_score = enriched_resp.json()["total_score"]
    gap = enriched_score - empty_score
    assert gap >= 40, (
        f"Enriched payload scored only {gap} points higher than empty payload "
        f"(empty={empty_score}, enriched={enriched_score}). "
        "Expected ≥40 point gap — the recruiter skill must populate description, "
        "role_domains, and required_skills before calling /api/score."
    )

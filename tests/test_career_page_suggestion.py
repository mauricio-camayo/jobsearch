"""
Integration tests for Priority #53 — prompt to add a company career page
when submitting a URL to the manual pipeline.

Uses FastAPI TestClient with an in-memory SQLite DB (shared cache), following
the same self-contained per-file pattern as the other test files in this repo.

CP1 — a resolved listing whose company has no active CompanyCareerPage entry
      renders the "Add {company} to tracked career pages?" prompt, with a
      careers_url derived from the ATS board root (not the specific job URL).
CP2 — once an active CompanyCareerPage exists for that company, the prompt
      does not render on a subsequent submission.
"""
import json
import re
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.middleware.sessions import SessionMiddleware

import app.models.user                 # noqa: F401
import app.models.user_profile          # noqa: F401
import app.models.tracker_record        # noqa: F401
import app.models.search_engine         # noqa: F401
import app.models.company_career_page   # noqa: F401
import app.models.scoring_rubric        # noqa: F401
import app.models.app_config            # noqa: F401
import app.models.search_params         # noqa: F401
import app.models.listing               # noqa: F401
import app.models.company_blacklist     # noqa: F401

from app.auth import get_current_user_ui
from app.db.database import Base, get_db
from app.db.seed import seed_user_profile, seed_scoring_rubric, seed_app_config, seed_search_params
from app.models.company_career_page import CompanyCareerPage
from app.models.listing import JobListing
from app.models.user import User
from app.routers import ui as ui_router

TEST_DB_URL = "sqlite:///file:testcareerpage?mode=memory&cache=shared&uri=true"

_engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_TestingSessionLocal = sessionmaker(bind=_engine, autocommit=False, autoflush=False)

_APPLY_URL = "https://jobs.lever.co/acmecorp/xyz123"
_EXPECTED_CAREERS_URL = "https://jobs.lever.co/acmecorp"


def _override_get_db():
    db = _TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
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

        db.add(JobListing(
            user_id=user_id,
            company="Acme Corp",
            role_title="Engineering Manager",
            apply_url=_APPLY_URL,
            remote_type="remote",
            geo_restriction="worldwide",
            description="Lead the engineering org.",
            required_skills=json.dumps(["Golang"]),
            role_domains=json.dumps(["fintech"]),
        ))
        db.commit()

    test_app = FastAPI()
    test_app.add_middleware(SessionMiddleware, secret_key="test-secret")
    test_app.dependency_overrides[get_db] = _override_get_db
    test_app.dependency_overrides[get_current_user_ui] = lambda: User(id=user_id, email="test@example.com")
    test_app.include_router(ui_router.router)
    with TestClient(test_app) as c:
        yield c

    with _engine.connect() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        conn.commit()


def _csrf_token(client):
    resp = client.get("/ui/search")
    match = re.search(r'name="csrf_token" value="([^"]+)"', resp.text)
    assert match, "no csrf_token field found on /ui/search"
    return match.group(1)


# CP1 — prompt renders with the board-root careers_url when no page exists yet.
def test_cp1_prompt_shown_when_no_career_page_exists(client):
    resp = client.post(
        "/ui/search/run",
        data={"urls_text": _APPLY_URL, "dry_run": "on", "csrf_token": _csrf_token(client)},
    )
    assert resp.status_code == 200
    assert "Add <strong>Acme Corp</strong> to tracked career pages?" in resp.text
    assert f'data-careers-url="{_EXPECTED_CAREERS_URL}"' in resp.text


# CP2 — prompt is suppressed once an active CompanyCareerPage row exists.
def test_cp2_prompt_hidden_once_career_page_exists(client):
    with _TestingSessionLocal() as db:
        db.add(CompanyCareerPage(
            company="Acme Corp", careers_url=_EXPECTED_CAREERS_URL, ats_type="lever", active=True,
        ))
        db.commit()

    resp = client.post(
        "/ui/search/run",
        data={"urls_text": _APPLY_URL, "dry_run": "on", "csrf_token": _csrf_token(client)},
    )
    assert resp.status_code == 200
    assert "Add <strong>Acme Corp</strong> to tracked career pages?" not in resp.text
    assert 'class="career-page-prompt"' not in resp.text

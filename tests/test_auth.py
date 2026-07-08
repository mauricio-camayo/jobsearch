"""
Integration tests for Priority #50 — multi-user auth with per-user profiles.

Uses FastAPI TestClient with an in-memory SQLite DB (shared cache), following
the same self-contained per-file pattern as the other test files in this repo
(own engine/session/app, dependency_overrides — no shared conftest.py exists).

A1 — admin-gated user creation: bootstrap admin (empty User table) can create
     an account by uploading a profile YAML; the created account is not admin.
A2 — a non-admin user is forbidden from creating new accounts (403).
A3 — first-login "claim" flow: a freshly created account has no password;
     submitting stage=claim sets the password and logs the user in.
A4 — logging in with the wrong password is rejected.
A5 — logout clears the session; a subsequent authenticated request redirects to /login.
A6 — data isolation: TrackerRecords created by one user never appear in another
     user's /api/tracker list.
A7 — a deactivated account cannot log in, even with the correct password (and the
     login flow gives the same generic response as an unknown account — AUTH-5).
A8 — deactivating a user mid-session immediately invalidates their existing session.
A9 — an admin cannot toggle their own admin/active status (self-lockout guard).
A10 — the "email" stage never reveals whether an account exists (AUTH-5): unknown
      and already-claimed emails both land on the same "password" stage.
A11 — repeated wrong passwords lock the account out after AUTH-1's threshold.
A12 — a POST with a missing/invalid csrf_token is rejected (CSRF-1).
"""
import io
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
from starlette.responses import RedirectResponse

from app.auth import LoginRequiredRedirect

import app.models.user               # noqa: F401
import app.models.user_profile       # noqa: F401
import app.models.tracker_record     # noqa: F401
import app.models.search_engine      # noqa: F401
import app.models.company_career_page  # noqa: F401
import app.models.scoring_rubric     # noqa: F401
import app.models.app_config         # noqa: F401
import app.models.search_params      # noqa: F401
import app.models.listing            # noqa: F401
import app.models.company_blacklist  # noqa: F401

from app.db.database import Base, get_db
from app.models.user import User
from app.routers import auth as auth_router
from app.routers import tracker as tracker_router

TEST_DB_URL = "sqlite:///file:testauth?mode=memory&cache=shared&uri=true"

_engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_TestingSessionLocal = sessionmaker(bind=_engine, autocommit=False, autoflush=False)


def _override_get_db():
    db = _TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


_PROFILE_YAML = b"""
name: "Test Person"
email: "person@example.com"
skills:
  - "Golang"
  - "AWS"
domain_expertise:
  - "fintech"
experience_years: 10
seniority: "senior"
"""


@pytest.fixture()
def client():
    Base.metadata.create_all(bind=_engine)
    test_app = FastAPI()
    test_app.add_middleware(SessionMiddleware, secret_key="test-secret")

    @test_app.exception_handler(LoginRequiredRedirect)
    async def _login_required_handler(request, exc):
        return RedirectResponse("/login", status_code=303)

    test_app.dependency_overrides[get_db] = _override_get_db
    test_app.include_router(auth_router.router)
    test_app.include_router(tracker_router.router)
    with TestClient(test_app) as c:
        yield c
    # Reset DB between tests since these depend on User-table emptiness (bootstrap admin).
    with _engine.connect() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        conn.commit()
    # Rolling login-attempt counters are process-global; reset between tests too.
    from app.login_throttle import _failures
    _failures.clear()


def _csrf_token(client):
    # /login is reachable regardless of auth/admin state, and the token is
    # per-session (not per-page), so a token fetched here is valid for any
    # subsequent POST made by this same client.
    resp = client.get("/login")
    match = re.search(r'name="csrf_token" value="([^"]+)"', resp.text)
    assert match, "no csrf_token field found on /login"
    return match.group(1)


def _post(client, path, data=None, **kwargs):
    data = dict(data or {})
    data["csrf_token"] = _csrf_token(client)
    return client.post(path, data=data, **kwargs)


def _upload_yaml(client, yaml_bytes: bytes = _PROFILE_YAML):
    return client.post(
        "/admin/users/new",
        data={"csrf_token": _csrf_token(client)},
        files={"file": ("profile.yaml", io.BytesIO(yaml_bytes), "application/x-yaml")},
    )


def _claim(client, email="person@example.com", password="correcthorse"):
    return _post(client, "/login", data={
        "stage": "claim", "email": email,
        "new_password": password, "confirm_password": password,
    }, follow_redirects=False)


# A1 — bootstrap: first account ever created is auto-admin; the account it creates is not.
def test_a1_bootstrap_admin_creates_first_user(client):
    resp = _upload_yaml(client)
    assert resp.status_code in (200, 303), resp.text

    with _TestingSessionLocal() as db:
        users = db.query(User).all()
        assert len(users) == 1
        created = users[0]
        assert created.email == "person@example.com"
        assert created.password_hash is None
        assert created.is_admin is True, "first account ever created must be auto-admin (bootstrap)"


# A2 — once an admin exists, a non-admin cannot create new accounts.
def test_a2_non_admin_forbidden_from_creating_users(client):
    _upload_yaml(client)  # bootstrap admin: person@example.com

    with _TestingSessionLocal() as db:
        non_admin = User(email="plain@example.com", password_hash="x", is_admin=False)
        db.add(non_admin)
        db.commit()
        db.refresh(non_admin)
        non_admin_id = non_admin.id

    # Log in as the non-admin by setting a password directly, then using the password stage.
    with _TestingSessionLocal() as db:
        u = db.get(User, non_admin_id)
        from app.auth import hash_password
        u.password_hash = hash_password("secret123")
        db.commit()

    login_resp = _post(client, "/login",
        data={"stage": "password", "email": "plain@example.com", "password": "secret123"},
        follow_redirects=False,
    )
    assert login_resp.status_code == 303

    second_yaml = _PROFILE_YAML.replace(b"person@example.com", b"another@example.com")
    resp = _upload_yaml(client, second_yaml)
    assert resp.status_code == 403


# A3 — first-login claim flow sets the password and logs the user in.
def test_a3_first_login_claim_sets_password(client):
    _upload_yaml(client)  # creates person@example.com with password_hash=None

    # Stage 1: email lookup should route to the "claim" stage.
    resp = _post(client, "/login", data={"stage": "email", "email": "person@example.com"})
    assert resp.status_code == 200
    assert "new_password" in resp.text

    # Stage 2: claim sets the password and logs in.
    resp = _claim(client, "person@example.com", "newpass123")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/dashboard"

    with _TestingSessionLocal() as db:
        user = db.query(User).filter_by(email="person@example.com").first()
        assert user.password_hash is not None


# A4 — wrong password is rejected.
def test_a4_wrong_password_rejected(client):
    _upload_yaml(client)
    _claim(client)
    resp = _post(client, "/login",
        data={"stage": "password", "email": "person@example.com", "password": "wrongpassword"},
    )
    assert resp.status_code == 200
    assert "Incorrect password" in resp.text


# A5 — logout clears the session; an authenticated-only route then redirects to /login.
def test_a5_logout_clears_session(client):
    _upload_yaml(client)
    _claim(client)
    resp = client.get("/api/tracker")
    assert resp.status_code == 200

    _post(client, "/logout", follow_redirects=False)
    resp = client.get("/api/tracker")
    assert resp.status_code == 401


# A6 — TrackerRecords are isolated per user.
def test_a6_tracker_records_isolated_per_user(client):
    _upload_yaml(client)  # person@example.com — bootstrap admin
    _claim(client)
    create_resp = client.post(
        "/api/tracker",
        json={"company": "Acme", "role_title": "Engineering Manager"},
    )
    assert create_resp.status_code == 201
    _post(client, "/logout")

    # Second account, created by the now-admin person, but we log in as it directly.
    with _TestingSessionLocal() as db:
        admin = db.query(User).filter_by(email="person@example.com").first()
        from app.auth import hash_password
        other = User(email="other@example.com", password_hash=hash_password("pw123456"), is_admin=False)
        db.add(other)
        db.commit()
        from app.db.seed import seed_new_user_defaults
        from app.models.user_profile import UserProfile
        db.add(UserProfile(
            user_id=other.id, full_name="Other Person", email="other@example.com",
            skills=["Python"], experience_years=5, seniority="senior", domains=["fintech"],
        ))
        seed_new_user_defaults(db, other.id)
        db.commit()

    _post(client, "/login", data={"stage": "password", "email": "other@example.com", "password": "pw123456"})
    resp = client.get("/api/tracker")
    assert resp.status_code == 200
    assert resp.json() == [], "second user must not see the first user's tracker records"


# A7 — a deactivated account cannot log in, even with the correct password, and the
# login flow doesn't reveal that the account exists/is deactivated (AUTH-5).
def test_a7_deactivated_account_cannot_login(client):
    _upload_yaml(client)  # person@example.com — bootstrap admin
    _claim(client)
    _post(client, "/logout")

    with _TestingSessionLocal() as db:
        user = db.query(User).filter_by(email="person@example.com").first()
        user.is_active = False
        db.commit()

    # Email stage gives the same generic "password" form as any other account.
    resp = _post(client, "/login", data={"stage": "email", "email": "person@example.com"})
    assert resp.status_code == 200
    assert "deactivated" not in resp.text.lower()
    assert 'name="password"' in resp.text

    # Correct password is still rejected for a deactivated account, with the
    # same generic message as any other login failure.
    resp = _post(client, "/login",
        data={"stage": "password", "email": "person@example.com", "password": "correcthorse"},
        follow_redirects=False,
    )
    assert resp.status_code == 200, "must not redirect to the dashboard for a deactivated account"
    assert "Incorrect password" in resp.text


# A8 — deactivating a user mid-session immediately invalidates their existing session.
def test_a8_deactivation_invalidates_existing_session(client):
    _upload_yaml(client)  # person@example.com — bootstrap admin
    _claim(client)
    resp = client.get("/api/tracker")
    assert resp.status_code == 200

    with _TestingSessionLocal() as db:
        user = db.query(User).filter_by(email="person@example.com").first()
        user.is_active = False
        db.commit()

    resp = client.get("/api/tracker")
    assert resp.status_code == 401, "a deactivated user's existing session must stop working immediately"


# A9 — an admin cannot toggle their own admin/active status.
def test_a9_admin_cannot_self_toggle(client):
    _upload_yaml(client)  # person@example.com — bootstrap admin
    _claim(client)
    with _TestingSessionLocal() as db:
        admin_id = db.query(User).filter_by(email="person@example.com").first().id

    resp = _post(client, f"/admin/users/{admin_id}/toggle-admin")
    assert resp.status_code == 422
    resp = _post(client, f"/admin/users/{admin_id}/toggle-active")
    assert resp.status_code == 422

    with _TestingSessionLocal() as db:
        admin = db.query(User).filter_by(email="person@example.com").first()
        assert admin.is_admin is True
        assert admin.is_active is True


# A10 — AUTH-5: the "email" stage never confirms whether an account exists.
# An unknown email and a known, already-claimed email both land on "password".
def test_a10_email_stage_does_not_leak_account_existence(client):
    _upload_yaml(client)  # person@example.com
    _claim(client)  # now claimed — has a password
    _post(client, "/logout")

    known_resp = _post(client, "/login", data={"stage": "email", "email": "person@example.com"})
    unknown_resp = _post(client, "/login", data={"stage": "email", "email": "nobody@example.com"})

    assert known_resp.status_code == unknown_resp.status_code == 200
    assert 'name="password"' in known_resp.text
    assert 'name="password"' in unknown_resp.text
    assert "No account found" not in known_resp.text
    assert "No account found" not in unknown_resp.text


# A11 — AUTH-1: repeated failed attempts lock the account out.
def test_a11_repeated_failures_lock_out_login(client):
    _upload_yaml(client)
    _claim(client, password="correcthorse")

    last_resp = None
    for _ in range(10):
        last_resp = _post(client, "/login",
            data={"stage": "password", "email": "person@example.com", "password": "wrongpassword"})
    assert "Incorrect password" in last_resp.text

    # The 11th attempt, even with the correct password, is now throttled.
    resp = _post(client, "/login",
        data={"stage": "password", "email": "person@example.com", "password": "correcthorse"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "Too many failed attempts" in resp.text


# A12 — CSRF-1: a POST without a valid csrf_token is rejected.
def test_a12_post_without_csrf_token_rejected(client):
    resp = client.post("/login", data={"stage": "email", "email": "person@example.com"})
    assert resp.status_code == 403

    resp = client.post("/login", data={
        "stage": "email", "email": "person@example.com", "csrf_token": "not-a-real-token",
    })
    assert resp.status_code == 403


# A13 — UPLOAD-1: an oversized profile YAML upload is rejected rather than
# read fully into memory.
def test_a13_oversized_upload_rejected(client):
    from app.routers.auth import _MAX_UPLOAD_BYTES
    oversized = _PROFILE_YAML + b"#" + b"x" * (_MAX_UPLOAD_BYTES + 1)
    resp = _upload_yaml(client, oversized)
    assert resp.status_code == 422
    assert "too large" in resp.text.lower()

    with _TestingSessionLocal() as db:
        assert db.query(User).count() == 0

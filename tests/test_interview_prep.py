"""
Tests for the interview-prep notes feature (PRIORITIES #74).

IP1 — create note returns 201 with expected fields
IP2 — list orders pinned-first, then most-recently-updated
IP3 — patch updates title/body/pinned
IP4 — delete removes the note
IP5 — 404 for a tracker record owned by another user
IP6 — 404 for a note id that doesn't belong to the given record

MD1-MD5 — markdown_lite renders headers/bold/bullets/links and escapes HTML
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.middleware.sessions import SessionMiddleware

import app.models.user              # noqa: F401
import app.models.tracker_record    # noqa: F401
import app.models.interview_prep    # noqa: F401

from app.auth import get_current_user_api, get_current_user_ui
from app.csrf import require_csrf_token
from app.db.database import Base, get_db
from app.models.tracker_record import TrackerRecord
from app.models.user import User
from app.routers import interview_prep as interview_prep_router
from app.routers import ui as ui_router
from app.services import markdown_lite

TEST_DB_URL = "sqlite:///file:testinterviewprep?mode=memory&cache=shared&uri=true"

_engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
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
        me = User(email="me@example.com", password_hash="x", is_admin=False)
        other = User(email="other@example.com", password_hash="x", is_admin=False)
        db.add_all([me, other])
        db.commit()
        db.refresh(me)
        db.refresh(other)

        my_record = TrackerRecord(
            user_id=me.id, company="Pariveda", role_title="Engineering Manager",
            status="interviewing", date_shown=date(2026, 7, 15),
        )
        other_record = TrackerRecord(
            user_id=other.id, company="StoneX", role_title="Software Dev Manager",
            status="interviewing", date_shown=date(2026, 7, 15),
        )
        db.add_all([my_record, other_record])
        db.commit()
        db.refresh(my_record)
        db.refresh(other_record)
        my_id, other_id, my_record_id, other_record_id = me.id, other.id, my_record.id, other_record.id

    test_app = FastAPI()
    test_app.dependency_overrides[get_db] = _override_get_db
    test_app.dependency_overrides[get_current_user_api] = lambda: User(id=my_id, email="me@example.com")
    test_app.include_router(interview_prep_router.router)

    with TestClient(test_app) as c:
        c.my_record_id = my_record_id
        c.other_record_id = other_record_id
        c.my_user_id = my_id
        yield c


@pytest.fixture(scope="module")
def ui_client(client):
    """Drives app.routers.ui's /ui/tracker/{id}/prep routes directly, bypassing
    login/CSRF (overridden) since those are already covered by test_auth.py —
    this fixture exists to regression-test prep_add's own validation logic."""
    test_app = FastAPI()
    test_app.add_middleware(SessionMiddleware, secret_key="test-secret")
    test_app.dependency_overrides[get_db] = _override_get_db
    test_app.dependency_overrides[get_current_user_ui] = lambda: User(id=client.my_user_id, email="me@example.com")
    test_app.dependency_overrides[require_csrf_token] = lambda: None
    test_app.include_router(ui_router.router)

    with TestClient(test_app, follow_redirects=False) as c:
        c.my_record_id = client.my_record_id
        yield c


def test_ip1_create_note_returns_201(client):
    resp = client.post(f"/api/tracker/{client.my_record_id}/prep", json={
        "title": "Company & role overview",
        "body": "## Pariveda\n- B Corp\n- Bogota office launch",
        "pinned": True,
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == "Company & role overview"
    assert body["pinned"] is True
    assert body["tracker_record_id"] == client.my_record_id


def test_ip2_list_orders_pinned_first_then_recent(client):
    client.post(f"/api/tracker/{client.my_record_id}/prep", json={"title": "Older unpinned", "body": "x"})
    resp = client.post(f"/api/tracker/{client.my_record_id}/prep", json={"title": "Newer unpinned", "body": "y"})
    newer_id = resp.json()["id"]

    listing = client.get(f"/api/tracker/{client.my_record_id}/prep").json()
    titles = [n["title"] for n in listing]
    assert titles[0] == "Company & role overview"  # pinned note stays first
    assert titles.index("Newer unpinned") < titles.index("Older unpinned")
    assert listing[1]["id"] == newer_id


def test_ip3_patch_updates_fields(client):
    created = client.post(f"/api/tracker/{client.my_record_id}/prep", json={"title": "Draft", "body": "v1"}).json()
    resp = client.patch(f"/api/tracker/{client.my_record_id}/prep/{created['id']}", json={"body": "v2", "pinned": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["body"] == "v2"
    assert body["pinned"] is True
    assert body["title"] == "Draft"  # untouched field preserved


def test_ip4_delete_removes_note(client):
    created = client.post(f"/api/tracker/{client.my_record_id}/prep", json={"title": "Temp", "body": "x"}).json()
    resp = client.delete(f"/api/tracker/{client.my_record_id}/prep/{created['id']}")
    assert resp.status_code == 204
    listing = client.get(f"/api/tracker/{client.my_record_id}/prep").json()
    assert all(n["id"] != created["id"] for n in listing)


def test_ip5_other_users_record_returns_404(client):
    resp = client.get(f"/api/tracker/{client.other_record_id}/prep")
    assert resp.status_code == 404
    resp = client.post(f"/api/tracker/{client.other_record_id}/prep", json={"body": "x"})
    assert resp.status_code == 404


def test_ip6_note_id_from_other_record_returns_404(client):
    mine = client.post(f"/api/tracker/{client.my_record_id}/prep", json={"body": "mine"}).json()
    # my_record note id, queried against a record I don't own → still 404 (record check first)
    resp = client.patch(f"/api/tracker/{client.other_record_id}/prep/{mine['id']}", json={"body": "hacked"})
    assert resp.status_code == 404


def test_ip7_api_rejects_body_over_max_length(client):
    resp = client.post(f"/api/tracker/{client.my_record_id}/prep", json={"body": "x" * 20_001})
    assert resp.status_code == 422


def test_ip8_api_rejects_empty_body(client):
    resp = client.post(f"/api/tracker/{client.my_record_id}/prep", json={"body": ""})
    assert resp.status_code == 422


# ---- app/routers/ui.py prep_add regression tests ----

def test_ip9_ui_whitespace_only_body_not_saved_and_flashes_error(ui_client):
    resp = ui_client.post(
        f"/ui/tracker/{ui_client.my_record_id}/prep",
        data={"title": "Whitespace only", "body": "   \n  \n  "},
    )
    assert resp.status_code == 303
    assert "err" in resp.headers["location"]
    assert "empty" in resp.headers["location"].lower()

    fragment = ui_client.get(f"/ui/tracker/{ui_client.my_record_id}/prep/fragment").text
    assert "Whitespace only" not in fragment


def test_ip10_ui_rejects_oversized_body(ui_client):
    resp = ui_client.post(
        f"/ui/tracker/{ui_client.my_record_id}/prep",
        data={"title": "Too long", "body": "x" * 20_001},
    )
    assert resp.status_code == 303
    assert "err" in resp.headers["location"]

    fragment = ui_client.get(f"/ui/tracker/{ui_client.my_record_id}/prep/fragment").text
    assert "Too long" not in fragment


# ---- markdown_lite unit tests ----

def test_md1_headers_and_bold():
    html = markdown_lite.render("## Title\n**bold text**")
    assert "<h4>Title</h4>" in html
    assert "<strong>bold text</strong>" in html


def test_md2_bullets_grouped_into_one_list():
    html = markdown_lite.render("- one\n- two")
    assert html.count("<ul>") == 1
    assert "<li>one</li>" in html and "<li>two</li>" in html


def test_md3_link_rendered_with_safe_scheme():
    html = markdown_lite.render("[about](https://example.com/about)")
    assert '<a href="https://example.com/about" target="_blank" rel="noopener noreferrer">about</a>' in html


def test_md4_javascript_scheme_link_not_rendered_as_link():
    html = markdown_lite.render("[xss](javascript:alert(1))")
    assert "<a " not in html
    assert "javascript:alert(1)" in html  # left as literal escaped text


def test_md5_html_in_body_is_escaped_not_executed():
    html = markdown_lite.render("<script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html

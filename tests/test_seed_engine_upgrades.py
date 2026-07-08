"""
Unit tests for seed_search_engines()'s one-off upgrade paths (2026-07-07,
PRIORITIES.md #64-66) — these patch already-existing installs (like the live
htpc DB), not just fresh ones, since seed_search_engines() only inserts
missing-by-name rows by default.

SE1 — a pre-existing golangprojects row on the old broken {query} URL template
      gets patched to the fixed URL on re-seed.
SE2 — a pre-existing golangprojects row already on a different (custom) URL is
      left alone — the patch only targets the known-broken exact template.
SE3 — pre-existing Himalayas/builtin rows get deactivated (active=False) on
      re-seed, since both are domain-wide blocked with no code-level fix.
SE4 — a fresh install seeds Himalayas/builtin as inactive from the start.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models.search_engine  # noqa: F401

from app.db.database import Base
from app.db.seed import seed_search_engines
from app.models.search_engine import SearchEngine

TEST_DB_URL = "sqlite:///file:testseedengines?mode=memory&cache=shared&uri=true"
_engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_TestingSessionLocal = sessionmaker(bind=_engine, autocommit=False, autoflush=False)


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=_engine)
    session = _TestingSessionLocal()
    yield session
    session.close()
    with _engine.connect() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        conn.commit()


def test_se1_golangprojects_broken_url_patched_on_reseed(db):
    db.add(SearchEngine(
        name="golangprojects",
        search_url_template="https://www.golangprojects.com/golang-go-job-{query}.html",
        fetch_strategy="html",
        quirks={}, search_params={}, active=True,
    ))
    db.commit()

    seed_search_engines(db)

    row = db.query(SearchEngine).filter_by(name="golangprojects").first()
    assert row.search_url_template == "https://www.golangprojects.com/golang-remote-jobs.html"


def test_se2_golangprojects_custom_url_left_alone(db):
    db.add(SearchEngine(
        name="golangprojects",
        search_url_template="https://www.golangprojects.com/some-custom-url.html",
        fetch_strategy="html",
        quirks={}, search_params={}, active=True,
    ))
    db.commit()

    seed_search_engines(db)

    row = db.query(SearchEngine).filter_by(name="golangprojects").first()
    assert row.search_url_template == "https://www.golangprojects.com/some-custom-url.html"


def test_se3_himalayas_and_builtin_deactivated_on_reseed(db):
    db.add(SearchEngine(
        name="Himalayas", search_url_template="https://himalayas.app/jobs?q={query}",
        fetch_strategy="html", quirks={}, search_params={}, active=True,
    ))
    db.add(SearchEngine(
        name="builtin", search_url_template="https://builtin.com/jobs?search={query}",
        fetch_strategy="html", quirks={}, search_params={}, active=True,
    ))
    db.commit()

    seed_search_engines(db)

    himalayas = db.query(SearchEngine).filter_by(name="Himalayas").first()
    builtin = db.query(SearchEngine).filter_by(name="builtin").first()
    assert himalayas.active is False
    assert builtin.active is False


def test_se4_fresh_install_seeds_himalayas_and_builtin_inactive(db):
    seed_search_engines(db)

    himalayas = db.query(SearchEngine).filter_by(name="Himalayas").first()
    builtin = db.query(SearchEngine).filter_by(name="builtin").first()
    assert himalayas.active is False
    assert builtin.active is False

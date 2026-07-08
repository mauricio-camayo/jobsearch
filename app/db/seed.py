from datetime import datetime
from sqlalchemy.orm import Session
from app.models.user import User
from app.models.user_profile import UserProfile
from app.models.tracker_record import TrackerRecord
from app.models.listing import JobListing
from app.models.company_blacklist import CompanyBlacklist
from app.models.search_engine import SearchEngine
from app.models.company_career_page import CompanyCareerPage
from app.models.scoring_rubric import ScoringRubric
from app.models.app_config import AppConfig
from app.models.search_params import SearchParams

# Demo/test profile — used only by seed_user_profile() for test fixtures, not at app startup.
_PROFILE = {
    "full_name": "Demo User",
    "email": "demo@example.com",
    "skills": [
        "Golang", "Java", "Python", "Perl", "Ruby", "JavaScript", "Bash",
        "AWS", "Docker", "Kubernetes",
        "PostgreSQL", "Redis", "Kafka",
        "Datadog", "Prometheus", "AWS CloudWatch",
        "Microservices", "Distributed Systems", "Event-driven Architecture",
        "CI/CD", "Jenkins", "GitHub Actions",
        "Scrum", "Agile",
        "OWASP", "CEH", "Security",
    ],
    "experience_years": 18,
    "seniority": "senior",
    "domains": ["payments", "fintech", "security", "platform", "distributed systems"],
    "updated_at": datetime.utcnow(),
}

_SEARCH_ENGINES = [
    dict(
        name="Remotive",
        search_url_template="https://remotive.com/api/remote-jobs?category=management&search={query}",
        fetch_strategy="api",
        quirks={
            "response_format": "json",
            "listing_key": "jobs",
            "notes": "JSON API; no auth required; category=management covers EM/director roles",
        },
    ),
    dict(
        name="Ashby",
        search_url_template="https://jobs.ashbyhq.com/{company}",
        fetch_strategy="api",
        quirks={
            "board_url_pattern": "jobs.ashbyhq.com/{company}",
            "crawlable_without_auth": True,
            "status_in_json": True,
            "notes": "Per-company boards only; no global search. Use CompanyCareerPage registry to enumerate targets.",
        },
    ),
    dict(
        name="Himalayas",
        search_url_template="https://himalayas.app/jobs?q={query}&location=worldwide",
        fetch_strategy="html",
        active=False,
        quirks={
            "stale_rate": "~50%",
            "verify_strategy": "always_fetch_ats",
            "notes": (
                "Deactivated 2026-07-07 — the entire domain is now behind a "
                "Cloudflare bot-challenge (confirmed live: 403 with a "
                "'Just a moment...' JS-challenge page on every path tried, "
                "including alternate API endpoints). Not fixable with a "
                "header/parameter change; needs a headless-browser fetch path "
                "(not built) or a different data source. Re-activate once one "
                "of those exists — see PRIORITIES.md item 65."
            ),
        },
    ),
    dict(
        name="Workable",
        search_url_template="https://jobs.workable.com/jobs?query={query}",
        fetch_strategy="html",
        quirks={
            "redirects_through": "apply.workable.com",
            "follow_redirect": True,
            "notes": "Apply URLs redirect through apply.workable.com — follow redirect to get canonical ATS URL.",
        },
    ),
    dict(
        name="Lever",
        search_url_template="https://jobs.lever.co/{company}",
        fetch_strategy="html",
        quirks={
            "may_403_direct": True,
            "fallback": "company_page",
            "notes": "Direct job URLs may return 403 from some IPs. Fall back to company board page.",
        },
    ),
    dict(
        name="Greenhouse",
        search_url_template="https://boards.greenhouse.io/{company}",
        fetch_strategy="html",
        quirks={
            "board_url_pattern": "boards.greenhouse.io/{company}",
            "crawlable_without_auth": True,
            "notes": "Per-company boards; no global search. Use CompanyCareerPage registry to enumerate targets.",
        },
    ),
    dict(
        name="golangprojects",
        search_url_template="https://www.golangprojects.com/golang-remote-jobs.html",
        fetch_strategy="html",
        quirks={
            "notes": (
                "Golang-focused job board. Fixed 2026-07-07 — the old "
                "{query}-substituted URL never had a valid keyword-search "
                "scheme (always 404'd); real postings live on this fixed "
                "remote-jobs listing page as relative-path links "
                "(/golang-go-job-<slug>.html), which also required a "
                "generic crawler fix (relative→absolute URL resolution) to "
                "surface at all. Mostly IC-titled listings (Senior/Staff "
                "Backend Engineer, Golang Developer) — genuinely low EM "
                "signal most of the time, not a bug."
            ),
        },
    ),
    dict(
        name="builtin",
        search_url_template="https://builtin.com/jobs?search={query}&remote=true",
        fetch_strategy="html",
        active=False,
        quirks={
            "notes": (
                "Deactivated 2026-07-07 — the site is a JS-rendered SPA; "
                "the static HTML fetch returns no real job links (confirmed "
                "live: only a false-positive nav link matches, and the one "
                "embedded JSON script tag on the page holds aggregate "
                "skill-tag counts, not actual postings — real listings load "
                "via a client-side XHR call not discoverable from static "
                "page source). Same category of blocker as Himalayas: needs "
                "a headless-browser fetch path or reverse-engineering the "
                "internal API (not attempted — more fragility/ToS risk than "
                "the other engines). Re-activate once one of those exists — "
                "see PRIORITIES.md item 66."
            ),
        },
    ),
    dict(
        name="LinkedIn",
        search_url_template="https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={query}",
        fetch_strategy="linkedin",
        quirks={
            "requires_search_params": "li_at",
            "notes": (
                "Uses LinkedIn's guest API (jobs-guest/jobs/api/...), not a direct "
                "HTML fetch — a direct fetch always hits the login wall. Requires a "
                "li_at session cookie, set per-engine on the Engines page's 'Search "
                "params' field (never a global env var — this cookie is a per-user "
                "browser session value). To obtain/refresh it: log into linkedin.com "
                "in a browser, open DevTools -> Application (or Storage) -> Cookies "
                "-> https://www.linkedin.com, copy the value of the 'li_at' cookie, "
                "and paste it into this engine's search params as "
                '{"li_at": "<value>"}. Sessions typically last ~1 year but are '
                "invalidated by logout or password change — refresh here when "
                "LinkedIn fetches start failing or verification returns unverified "
                "with a 'no posting data' reason."
            ),
        },
    ),
]


_COMPANY_PAGES = [
    # Ashby boards — discovered from tracker history
    dict(company="SentiLink",            careers_url="https://jobs.ashbyhq.com/sentilink",          ats_type="ashby"),
    dict(company="Polygon Labs",         careers_url="https://jobs.ashbyhq.com/polygon-labs",       ats_type="ashby"),
    dict(company="Kraken",               careers_url="https://jobs.ashbyhq.com/kraken.com",         ats_type="ashby"),
    dict(company="Alternative Payments", careers_url="https://jobs.ashbyhq.com/alternativepayments",ats_type="ashby"),
    dict(company="Finmid",               careers_url="https://jobs.ashbyhq.com/finmid.com",         ats_type="ashby"),
    dict(company="Teya",                 careers_url="https://jobs.ashbyhq.com/teya",               ats_type="ashby"),
    # Workable boards
    dict(company="RecargaPay",           careers_url="https://apply.workable.com/recargapay",       ats_type="workable"),
    dict(company="NALA",                 careers_url="https://apply.workable.com/nalamoney",        ats_type="workable"),
    # Lever boards
    dict(company="dLocal",               careers_url="https://jobs.lever.co/dlocal",                ats_type="lever"),
    dict(company="Versapay",             careers_url="https://jobs.lever.co/versapay",              ats_type="lever"),
    # Custom careers pages
    dict(company="Tether Operations",    careers_url="https://careers.tether.io",                  ats_type="custom"),
]


_SCORING_RUBRIC = [
    dict(dimension="domain_match",          weight=35, is_bonus=False),
    dict(dimension="tech_stack",            weight=30, is_bonus=False),
    dict(dimension="seniority",             weight=25, is_bonus=False),
    dict(dimension="remote_geo",            weight=10, is_bonus=False),
    dict(dimension="relocation_visa_bonus", weight=10, is_bonus=True),
]


def seed_user_profile(db: Session, user_id: int) -> None:
    """Demo/test fixture helper — not used at app startup (real profiles come from
    the admin YAML-upload flow in app/routers/auth.py)."""
    if db.query(UserProfile).filter_by(user_id=user_id).first() is not None:
        return
    db.add(UserProfile(user_id=user_id, **_PROFILE))
    db.commit()


_BROKEN_GOLANGPROJECTS_URL = "https://www.golangprojects.com/golang-go-job-{query}.html"


def seed_search_engines(db: Session) -> None:
    """Global/shared registry. Inserts any engine from _SEARCH_ENGINES missing
    by name — covers both a fresh install and adding a new engine (e.g.
    LinkedIn) to an install that was already seeded."""
    existing = {e.name: e for e in db.query(SearchEngine).all()}
    for row in _SEARCH_ENGINES:
        name = row["name"]
        if name not in existing:
            db.add(SearchEngine(**row))
            continue
        e = existing[name]
        # One-off upgrade: some installs already have a "LinkedIn" placeholder
        # row predating this feature (fetch_strategy="html", login_wall/skip
        # quirk, no guest-API support). Replace its stale config in place —
        # but keep any search_params (li_at cookie) already saved on it.
        if name == "LinkedIn" and e.fetch_strategy != "linkedin":
            e.search_url_template = row["search_url_template"]
            e.fetch_strategy = row["fetch_strategy"]
            e.quirks = row["quirks"]
        # One-off upgrade (2026-07-07, item 64): golangprojects' old seeded
        # URL never had a valid keyword-search scheme (always 404'd) — patch
        # existing rows still on the broken template to the fixed one.
        if name == "golangprojects" and e.search_url_template == _BROKEN_GOLANGPROJECTS_URL:
            e.search_url_template = row["search_url_template"]
            e.quirks = row["quirks"]
        # One-off upgrade (2026-07-07, items 65/66): Himalayas/builtin.com are
        # both domain-wide-blocked (Cloudflare challenge / JS-rendered SPA) —
        # deactivate existing rows rather than leaving them silently returning
        # 0 results on every crawl. Only forces active=False, never re-enables
        # a row a user may have already toggled off for another reason.
        if name in ("Himalayas", "builtin") and e.active:
            e.active = False
            e.quirks = row["quirks"]
    db.commit()


def seed_scoring_rubric(db: Session, user_id: int) -> None:
    if db.query(ScoringRubric).filter_by(user_id=user_id).first() is not None:
        return
    for row in _SCORING_RUBRIC:
        db.add(ScoringRubric(user_id=user_id, **row))
    db.commit()


def seed_app_config(db: Session, user_id: int) -> None:
    if db.query(AppConfig).filter_by(user_id=user_id).first() is not None:
        return
    db.add(AppConfig(user_id=user_id, fit_autosave_threshold=70))
    db.commit()


def seed_company_pages(db: Session) -> None:
    """Global/shared registry — seeded once at startup, not per-user."""
    if db.query(CompanyCareerPage).count() > 0:
        return
    for row in _COMPANY_PAGES:
        db.add(CompanyCareerPage(**row))
    db.commit()


def seed_search_params(db: Session, user_id: int) -> None:
    if db.query(SearchParams).filter_by(user_id=user_id).first() is not None:
        return
    db.add(SearchParams(user_id=user_id))
    db.commit()


def seed_new_user_defaults(db: Session, user_id: int) -> None:
    """Called right after a User + UserProfile are created (admin YAML upload,
    or legacy-migration bootstrap) to give the account its own config/search-params/rubric."""
    seed_app_config(db, user_id)
    seed_search_params(db, user_id)
    seed_scoring_rubric(db, user_id)


def seed_legacy_user_and_backfill(db: Session) -> None:
    """One-time migration for the pre-multi-tenant install: the original singleton
    UserProfile (created with user_id=NULL before this migration existed) becomes
    owned by a real User account, and every other pre-existing row with user_id=NULL
    is backfilled to that same account. No-op on a fresh install with no legacy data."""
    legacy_profile = db.query(UserProfile).filter(UserProfile.user_id.is_(None)).first()
    if legacy_profile is None:
        return

    legacy_user = db.query(User).filter(User.email == legacy_profile.email).first()
    if legacy_user is None:
        legacy_user = User(email=legacy_profile.email, password_hash=None, is_admin=True)
        db.add(legacy_user)
        db.flush()

    legacy_profile.user_id = legacy_user.id
    for model in (TrackerRecord, JobListing, CompanyBlacklist, AppConfig, SearchParams, ScoringRubric):
        db.query(model).filter(model.user_id.is_(None)).update({"user_id": legacy_user.id})
    db.commit()

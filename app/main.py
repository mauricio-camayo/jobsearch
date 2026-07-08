import os

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse

from app.auth import LoginRequiredRedirect
from app.version import APP_VERSION
from app.db.database import Base, engine, SessionLocal, apply_migrations
import app.models.user_profile      # noqa: F401
import app.models.tracker_record    # noqa: F401
import app.models.search_engine     # noqa: F401
import app.models.company_career_page  # noqa: F401
import app.models.scoring_rubric    # noqa: F401
import app.models.app_config        # noqa: F401
import app.models.search_params     # noqa: F401
import app.models.listing           # noqa: F401
import app.models.company_blacklist  # noqa: F401
import app.models.user              # noqa: F401
import app.models.search_session    # noqa: F401
from app.db.seed import (
    seed_search_engines,
    seed_company_pages,
    seed_legacy_user_and_backfill,
)
from app.routers import (
    auth, profile, tracker, engines, company_pages,
    verify, scoring, config, dedup, search_params, quality, search_run, ui, listings, blacklist,
)

Base.metadata.create_all(bind=engine)
apply_migrations()

with SessionLocal() as db:
    seed_search_engines(db)
    seed_company_pages(db)
    seed_legacy_user_and_backfill(db)

# AUTH-2: fail fast rather than silently signing sessions with a hardcoded
# default that's now sitting in source control (this default value must
# never be reused — see SECURITY.md AUTH-2).
_session_secret_key = os.environ["SESSION_SECRET_KEY"]

# AUTH-4: only send the session cookie over HTTPS when the app is deployed
# behind TLS (Cloudflare tunnel / reverse proxy). Off by default so plain
# HTTP LAN access (http://htpc.local:8080) keeps working — see SECURITY.md
# AUTH-4 for the accepted-risk tradeoff this represents.
_cookie_https_only = os.getenv("SESSION_COOKIE_HTTPS_ONLY", "false").lower() == "true"

# DOCS-1: FastAPI's auto docs hand over the full API surface with zero auth.
# Disabled unless explicitly re-enabled for local development.
_enable_docs = os.getenv("ENABLE_API_DOCS", "false").lower() == "true"

app = FastAPI(
    title="JobSearch",
    version=APP_VERSION,
    docs_url="/docs" if _enable_docs else None,
    redoc_url="/redoc" if _enable_docs else None,
    openapi_url="/openapi.json" if _enable_docs else None,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret_key,
    https_only=_cookie_https_only,
)


# HEADERS-1: every template is same-origin only (no CDN scripts/fonts/images), but several
# rely on inline <script>/<style> blocks and inline event handlers (onclick=, onchange=),
# so script-src/style-src need 'unsafe-inline' — this still constrains all resource
# loading to same-origin and sets frame-ancestors (the real anti-clickjacking value here;
# X-Frame-Options below is the legacy fallback for older browsers), but 'unsafe-inline'
# means it does NOT stop inline-script injection the way a nonce/hash-based CSP would.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


@app.middleware("http")
async def _security_headers(request, call_next):
    # HEADERS-1
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = _CSP
    if _cookie_https_only:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.exception_handler(LoginRequiredRedirect)
async def _login_required_handler(request, exc):
    return RedirectResponse("/login", status_code=303)


app.include_router(auth.router)
app.include_router(profile.router)
app.include_router(tracker.router)
app.include_router(engines.router)
app.include_router(company_pages.router)
app.include_router(verify.router)
app.include_router(scoring.router)
app.include_router(config.router)
app.include_router(dedup.router)
app.include_router(search_params.router)
app.include_router(quality.router)
app.include_router(search_run.router)
app.include_router(listings.router)
app.include_router(blacklist.router)
app.include_router(ui.router)


@app.get("/health")
def health():
    return {"status": "ok", "version": APP_VERSION}

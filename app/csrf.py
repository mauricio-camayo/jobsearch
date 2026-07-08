"""Anti-CSRF token support for the server-rendered (form-based) routers.

Not applied to /api/* routers: those are JSON-only, so a classic HTML-form
CSRF submission can't reach them (browsers can't set Content-Type: application/json
on a simple cross-site form post), and they're already behind the same
SameSite=Lax session cookie.
"""
import secrets

from fastapi import HTTPException, Request

_SESSION_KEY = "csrf_token"
_STATE_CHANGING_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def get_csrf_token(request: Request) -> str:
    """Get-or-create the per-session CSRF token. Used as a Jinja2 template global."""
    token = request.session.get(_SESSION_KEY)
    if not token:
        token = secrets.token_hex(32)
        request.session[_SESSION_KEY] = token
    return token


async def require_csrf_token(request: Request) -> None:
    """Router-level dependency: validates the csrf_token form field on any
    state-changing request. No-op for GET/HEAD/OPTIONS so it's safe to attach
    to an entire router (mixed GET+POST routes) at once."""
    if request.method not in _STATE_CHANGING_METHODS:
        return
    expected = request.session.get(_SESSION_KEY)
    form = await request.form()
    submitted = form.get("csrf_token")
    if not expected or not submitted or not secrets.compare_digest(str(submitted), str(expected)):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")

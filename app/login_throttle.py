"""In-memory login attempt throttling (AUTH-1).

A per-email rolling window is enough at this app's scale (small,
admin-managed user base, single uvicorn process/worker — see SECURITY.md
AUTH-1). Not shared across processes; if this app is ever run with more
than one worker, move this to a SQLite-backed table instead.
"""
import time

_MAX_ATTEMPTS = 10
_WINDOW_SECONDS = 15 * 60

_failures: dict[str, list[float]] = {}


def _recent_attempts(email: str) -> list[float]:
    now = time.time()
    attempts = [t for t in _failures.get(email, []) if now - t < _WINDOW_SECONDS]
    _failures[email] = attempts
    return attempts


def seconds_until_retry(email: str) -> int:
    """Seconds remaining before this email may attempt another login, or 0 if not locked out."""
    attempts = _recent_attempts(email)
    if len(attempts) < _MAX_ATTEMPTS:
        return 0
    oldest = attempts[0]
    return max(int(_WINDOW_SECONDS - (time.time() - oldest)), 0)


def register_failure(email: str) -> None:
    _failures.setdefault(email, []).append(time.time())


def reset_failures(email: str) -> None:
    _failures.pop(email, None)

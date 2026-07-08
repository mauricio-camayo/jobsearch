import bcrypt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.models.user import User


class LoginRequiredRedirect(Exception):
    """Raised by UI-facing auth dependencies; main.py converts this to a redirect to /login."""


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def _user_from_session(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        return None
    return user


def get_current_user_api(request: Request, db: Session = Depends(get_db)) -> User:
    user = _user_from_session(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def get_current_user_ui(request: Request, db: Session = Depends(get_db)) -> User:
    user = _user_from_session(request, db)
    if user is None:
        raise LoginRequiredRedirect()
    return user


def require_admin(request: Request, db: Session = Depends(get_db)) -> User | None:
    """Returns the logged-in admin, or None when no User rows exist yet (bootstrap mode:
    the first account ever created through the gated route is auto-promoted to admin)."""
    if db.query(User).count() == 0:
        return None
    user = _user_from_session(request, db)
    if user is None:
        raise LoginRequiredRedirect()
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user

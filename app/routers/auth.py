from typing import Annotated
from urllib.parse import quote

import yaml
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import hash_password, require_admin, verify_password
from app.csrf import get_csrf_token, require_csrf_token
from app.db.database import get_db
from app.db.seed import seed_new_user_defaults
from app.login_throttle import register_failure, reset_failures, seconds_until_retry
from app.models.search_session import SearchSession
from app.models.tracker_record import TrackerRecord
from app.models.user import User
from app.models.user_profile import UserProfile
from app.version import APP_VERSION

router = APIRouter(dependencies=[Depends(require_csrf_token)])
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["csrf_token"] = get_csrf_token
templates.env.globals["app_version"] = APP_VERSION

_REQUIRED_YAML_FIELDS = ["email", "name", "skills", "experience_years", "seniority", "domain_expertise"]
_MAX_UPLOAD_BYTES = 1_000_000


def _redirect(url: str, flash: str = "", flash_type: str = "ok"):
    sep = "&" if "?" in url else "?"
    if flash:
        url = f"{url}{sep}flash={quote(flash)}&flash_type={flash_type}"
    return RedirectResponse(url, status_code=303)


def _flash_from_request(request: Request) -> tuple[str, str]:
    return request.query_params.get("flash", ""), request.query_params.get("flash_type", "ok")


def _render_login(request: Request, stage: str, email: str = "", error: str = ""):
    return templates.TemplateResponse(request, "login.html", {
        "request": request,
        "title": "Log in",
        "stage": stage,
        "email": email,
        "error": error,
    })


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return _render_login(request, stage="email")


@router.post("/login", response_class=HTMLResponse)
def login_post(
    request: Request,
    stage: Annotated[str, Form()],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()] = "",
    new_password: Annotated[str, Form()] = "",
    confirm_password: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
):
    email = email.strip().lower()

    if stage == "email":
        # AUTH-5: deliberately generic — an unknown email, a deactivated account, and an
        # already-claimed account are all routed to the same "password" stage, so this
        # step never confirms whether an account exists. Only a genuinely unclaimed,
        # active account (the admin-onboarding case) advances to "claim".
        user = db.query(User).filter_by(email=email).first()
        if user is not None and user.is_active and user.password_hash is None:
            return _render_login(request, stage="claim", email=email)
        return _render_login(request, stage="password", email=email)

    if stage == "claim":
        user = db.query(User).filter_by(email=email).first()
        if user is None or user.password_hash is not None or not user.is_active:
            return _render_login(request, stage="email", error="Something went wrong — try again.")
        if not new_password or new_password != confirm_password:
            return _render_login(request, stage="claim", email=email, error="Passwords do not match.")
        if len(new_password) < 8:
            return _render_login(request, stage="claim", email=email,
                                  error="Password must be at least 8 characters.")
        user.password_hash = hash_password(new_password)
        db.commit()
        request.session["user_id"] = user.id
        return RedirectResponse("/ui/dashboard", status_code=303)

    if stage == "password":
        # AUTH-1: per-email rolling lockout after repeated failures.
        retry_after = seconds_until_retry(email)
        if retry_after > 0:
            return _render_login(
                request, stage="password", email=email,
                error=f"Too many failed attempts. Try again in {retry_after}s.",
            )
        user = db.query(User).filter_by(email=email).first()
        valid = (
            user is not None
            and user.is_active
            and user.password_hash is not None
            and verify_password(password, user.password_hash)
        )
        if not valid:
            register_failure(email)
            return _render_login(request, stage="password", email=email, error="Incorrect password.")
        reset_failures(email)
        request.session["user_id"] = user.id
        return RedirectResponse("/ui/dashboard", status_code=303)

    return _render_login(request, stage="email", error="Invalid request.")


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/admin/users/new", response_class=HTMLResponse)
def new_user_form(request: Request, admin: User | None = Depends(require_admin)):
    flash, flash_type = _flash_from_request(request)
    return templates.TemplateResponse(request, "admin_new_user.html", {
        "request": request,
        "title": "Add User",
        "active": "admin",
        "current_user": admin,
        "flash": flash,
        "flash_type": flash_type,
        "error": "",
    })


@router.post("/admin/users/new", response_class=HTMLResponse)
async def create_user(
    request: Request,
    file: UploadFile = File(...),
    admin: User | None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    def _error(message: str):
        return templates.TemplateResponse(request, "admin_new_user.html", {
            "request": request,
            "title": "Add User",
            "active": "admin",
            "current_user": admin,
            "flash": "",
            "flash_type": "err",
            "error": message,
        }, status_code=422)

    raw = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(raw) > _MAX_UPLOAD_BYTES:
        return _error(f"File too large — max {_MAX_UPLOAD_BYTES // 1000}KB.")
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        return _error("Could not parse that file as YAML.")

    missing = [f for f in _REQUIRED_YAML_FIELDS if not data.get(f)]
    if missing:
        return _error(f"YAML is missing required field(s): {', '.join(missing)}")

    email = str(data["email"]).strip().lower()
    if db.query(User).filter_by(email=email).first():
        return _error(f"An account for {email} already exists.")

    is_bootstrap_admin = admin is None  # User table was empty — first account becomes admin
    new_user = User(email=email, password_hash=None, is_admin=is_bootstrap_admin)
    db.add(new_user)
    db.flush()

    db.add(UserProfile(
        user_id=new_user.id,
        full_name=data["name"],
        email=email,
        skills=data["skills"],
        experience_years=data["experience_years"],
        seniority=data["seniority"],
        domains=data["domain_expertise"],
        resume_path=data.get("resume_file"),
    ))
    seed_new_user_defaults(db, new_user.id)
    db.commit()

    return _redirect(
        "/admin/users/new",
        f"Account created for {email}. They can log in at /login and set their password.",
    )


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users_list(request: Request, db: Session = Depends(get_db), admin: User | None = Depends(require_admin)):
    users = db.query(User).order_by(User.id).all()

    tracker_counts = dict(
        db.query(TrackerRecord.user_id, func.count(TrackerRecord.id))
        .group_by(TrackerRecord.user_id)
        .all()
    )
    last_search_at = dict(
        db.query(SearchSession.user_id, func.max(SearchSession.started_at))
        .group_by(SearchSession.user_id)
        .all()
    )
    usage = {
        u.id: {
            "tracker_count": tracker_counts.get(u.id, 0),
            "last_search_at": last_search_at.get(u.id),
        }
        for u in users
    }

    flash, flash_type = _flash_from_request(request)
    return templates.TemplateResponse(request, "admin_users.html", {
        "request": request,
        "title": "Admin — Users",
        "active": "admin",
        "current_user": admin,
        "users": users,
        "usage": usage,
        "flash": flash,
        "flash_type": flash_type,
    })


def _get_target_user(db: Session, admin: User | None, user_id: int) -> User:
    if admin is not None and user_id == admin.id:
        raise HTTPException(status_code=422, detail="You cannot change your own admin/active status.")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.post("/admin/users/{user_id}/toggle-admin")
def toggle_admin(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User | None = Depends(require_admin),
):
    user = _get_target_user(db, admin, user_id)
    user.is_admin = not user.is_admin
    db.commit()
    state = "granted" if user.is_admin else "revoked"
    return _redirect("/admin/users", f"Admin access {state} for {user.email}")


@router.post("/admin/users/{user_id}/toggle-active")
def toggle_active(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User | None = Depends(require_admin),
):
    user = _get_target_user(db, admin, user_id)
    user.is_active = not user.is_active
    db.commit()
    state = "reactivated" if user.is_active else "deactivated"
    return _redirect("/admin/users", f"{user.email} {state}")

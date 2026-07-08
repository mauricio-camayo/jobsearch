from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.auth import require_admin
from app.db.database import get_db
from app.models.search_engine import SearchEngine

# Admin-only: SearchEngine is a global/shared table, and search_params can hold
# live credentials (e.g. LinkedIn's li_at session cookie) — visible/editable by
# every user otherwise (item 57).
router = APIRouter(prefix="/api/engines", tags=["engines"], dependencies=[Depends(require_admin)])

VALID_STRATEGIES = {"rss", "html", "api", "sitemap", "linkedin"}


def _to_dict(e: SearchEngine) -> dict:
    return {
        "id": e.id,
        "name": e.name,
        "search_url_template": e.search_url_template,
        "fetch_strategy": e.fetch_strategy,
        "quirks": e.quirks,
        "search_params": e.search_params,
        "active": e.active,
        "last_crawled_at": e.last_crawled_at,
    }


class EngineCreate(BaseModel):
    name: str
    search_url_template: str
    fetch_strategy: str
    quirks: Optional[dict] = {}
    search_params: Optional[dict] = {}
    active: Optional[bool] = True


class EngineUpdate(BaseModel):
    name: Optional[str] = None
    search_url_template: Optional[str] = None
    fetch_strategy: Optional[str] = None
    quirks: Optional[dict] = None
    search_params: Optional[dict] = None
    active: Optional[bool] = None


@router.get("")
def list_engines(active_only: bool = False, db: Session = Depends(get_db)):
    q = db.query(SearchEngine)
    if active_only:
        q = q.filter(SearchEngine.active == True)  # noqa: E712
    return [_to_dict(e) for e in q.order_by(SearchEngine.id).all()]


@router.get("/{engine_id}")
def get_engine(engine_id: int, db: Session = Depends(get_db)):
    engine = db.get(SearchEngine, engine_id)
    if engine is None:
        raise HTTPException(status_code=404, detail="Engine not found")
    return _to_dict(engine)


@router.post("", status_code=201)
def create_engine(body: EngineCreate, db: Session = Depends(get_db)):
    if body.fetch_strategy not in VALID_STRATEGIES:
        raise HTTPException(
            status_code=422,
            detail=f"fetch_strategy must be one of {sorted(VALID_STRATEGIES)}",
        )
    if db.query(SearchEngine).filter(SearchEngine.name == body.name).first():
        raise HTTPException(status_code=409, detail=f"Engine '{body.name}' already exists")
    engine = SearchEngine(**body.model_dump())
    db.add(engine)
    db.commit()
    db.refresh(engine)
    return _to_dict(engine)


@router.patch("/{engine_id}")
def update_engine(engine_id: int, body: EngineUpdate, db: Session = Depends(get_db)):
    engine = db.get(SearchEngine, engine_id)
    if engine is None:
        raise HTTPException(status_code=404, detail="Engine not found")
    updates = body.model_dump(exclude_none=True)
    if "fetch_strategy" in updates and updates["fetch_strategy"] not in VALID_STRATEGIES:
        raise HTTPException(
            status_code=422,
            detail=f"fetch_strategy must be one of {sorted(VALID_STRATEGIES)}",
        )
    for key, value in updates.items():
        setattr(engine, key, value)
    db.commit()
    return _to_dict(engine)


@router.delete("/{engine_id}", status_code=204)
def disable_engine(engine_id: int, db: Session = Depends(get_db)):
    """Soft-delete: sets active=false. Row is preserved for history."""
    engine = db.get(SearchEngine, engine_id)
    if engine is None:
        raise HTTPException(status_code=404, detail="Engine not found")
    engine.active = False
    db.commit()


@router.get("/{engine_id}/quirks")
def get_engine_quirks(engine_id: int, db: Session = Depends(get_db)):
    """Return the quirks dict for an engine."""
    engine = db.get(SearchEngine, engine_id)
    if engine is None:
        raise HTTPException(status_code=404, detail="Engine not found")
    return engine.quirks or {}


@router.patch("/{engine_id}/quirks")
def patch_engine_quirks(engine_id: int, body: dict, db: Session = Depends(get_db)):
    """Deep-merge *body* into the engine's existing quirks dict.

    Merge semantics: callers can update one key without overwriting others.
    To remove a key, set its value to None explicitly — None-valued keys are
    dropped from the stored dict.
    """
    engine = db.get(SearchEngine, engine_id)
    if engine is None:
        raise HTTPException(status_code=404, detail="Engine not found")
    current = dict(engine.quirks or {})
    for key, value in body.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
    engine.quirks = current
    db.commit()
    return engine.quirks


@router.get("/{engine_id}/search-params")
def get_engine_search_params(engine_id: int, db: Session = Depends(get_db)):
    """Return the search_params dict for an engine (e.g. LinkedIn's li_at cookie)."""
    engine = db.get(SearchEngine, engine_id)
    if engine is None:
        raise HTTPException(status_code=404, detail="Engine not found")
    return engine.search_params or {}


@router.patch("/{engine_id}/search-params")
def patch_engine_search_params(engine_id: int, body: dict, db: Session = Depends(get_db)):
    """Deep-merge *body* into the engine's existing search_params dict.

    Same merge semantics as /quirks: None-valued keys are dropped.
    """
    engine = db.get(SearchEngine, engine_id)
    if engine is None:
        raise HTTPException(status_code=404, detail="Engine not found")
    current = dict(engine.search_params or {})
    for key, value in body.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
    engine.search_params = current
    db.commit()
    return engine.search_params

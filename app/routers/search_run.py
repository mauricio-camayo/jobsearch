import asyncio
import json
from typing import AsyncGenerator, Optional
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import get_current_user_api
from app.db.database import get_db
from app.models.user import User
from app.services.pipeline import run_pipeline, ListingInput
from app.services.crawler import crawl_all_engines

router = APIRouter(prefix="/api/search", tags=["search"])


class ListingInputSchema(BaseModel):
    company: str = Field(..., min_length=1)
    role_title: str = Field(..., min_length=1)
    apply_url: Optional[str] = None
    remote_type: str = "unknown"
    geo: str = "unknown"
    description: str = ""
    required_skills: list[str] = Field(default_factory=list)
    role_domains: list[str] = Field(default_factory=list)
    relocation_offered: bool = False
    visa_sponsorship: bool = False
    aggregator_remote_type: Optional[str] = None


class RunRequest(BaseModel):
    listings: list[ListingInputSchema] = Field(..., min_length=1)
    dry_run: bool = False


class ListingResultSchema(BaseModel):
    company: str
    role_title: str
    apply_url: Optional[str]
    decision: str
    reason: str
    flags: list[str]
    fit_pct: Optional[int]
    tracker_id: Optional[int]
    skip_category: Optional[str]
    urgency_note: Optional[str]


class RunResponse(BaseModel):
    submitted: int
    saved: int
    skipped: int
    resurfaced: int
    auto_removed: int = 0
    skip_reasons: dict[str, int]
    dry_run: bool
    results: list[ListingResultSchema]


@router.post("/run", response_model=RunResponse)
async def run_search(
    body: RunRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    listings = [
        ListingInput(
            company=l.company,
            role_title=l.role_title,
            apply_url=l.apply_url,
            remote_type=l.remote_type,
            geo=l.geo,
            description=l.description,
            required_skills=l.required_skills,
            role_domains=l.role_domains,
            relocation_offered=l.relocation_offered,
            visa_sponsorship=l.visa_sponsorship,
            aggregator_remote_type=l.aggregator_remote_type,
        )
        for l in body.listings
    ]
    session = await run_pipeline(listings, db, current_user.id, dry_run=body.dry_run)
    return RunResponse(
        submitted=session.submitted,
        saved=session.saved,
        skipped=session.skipped,
        resurfaced=session.resurfaced,
        auto_removed=session.auto_removed,
        skip_reasons=session.skip_reasons,
        dry_run=body.dry_run,
        results=[
            ListingResultSchema(
                company=r.company,
                role_title=r.role_title,
                apply_url=r.apply_url,
                decision=r.decision,
                reason=r.reason,
                flags=r.flags,
                fit_pct=r.fit_pct,
                tracker_id=r.tracker_id,
                skip_category=r.skip_category,
                urgency_note=r.urgency_note,
            )
            for r in session.results
        ],
    )


class EngineError(BaseModel):
    engine: str
    error: str


class CrawlResponse(BaseModel):
    engines_crawled: int
    engines_errored: int
    raw_listings_found: int
    engine_errors: list[EngineError]
    submitted: int
    saved: int
    skipped: int
    resurfaced: int
    auto_removed: int = 0
    skip_reasons: dict[str, int]
    dry_run: bool
    results: list[ListingResultSchema]


@router.post("/crawl", response_model=CrawlResponse)
async def crawl_search(
    dry_run: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    summary = await crawl_all_engines(db, current_user.id, dry_run=dry_run)
    p = summary.pipeline
    return CrawlResponse(
        engines_crawled=summary.engines_crawled,
        engines_errored=summary.engines_errored,
        raw_listings_found=summary.raw_listings_found,
        engine_errors=[EngineError(**e) for e in summary.engine_errors],
        submitted=p.submitted,
        saved=p.saved,
        skipped=p.skipped,
        resurfaced=p.resurfaced,
        auto_removed=p.auto_removed,
        skip_reasons=p.skip_reasons,
        dry_run=dry_run,
        results=[
            ListingResultSchema(
                company=r.company,
                role_title=r.role_title,
                apply_url=r.apply_url,
                decision=r.decision,
                reason=r.reason,
                flags=r.flags,
                fit_pct=r.fit_pct,
                tracker_id=r.tracker_id,
                skip_category=r.skip_category,
                urgency_note=r.urgency_note,
            )
            for r in p.results
        ],
    )


@router.get("/crawl/stream")
async def crawl_stream(
    dry_run: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    """SSE endpoint — streams one event per engine, then a final done event."""

    async def _generate() -> AsyncGenerator[str, None]:
        queue: asyncio.Queue = asyncio.Queue()

        async def on_engine_done(engine_name: str, found: int, saved: int, skipped: int) -> None:
            payload = json.dumps({"engine": engine_name, "found": found, "saved": saved, "skipped": skipped})
            await queue.put(f"data: {payload}\n\n")

        async def _run_crawl() -> None:
            summary = await crawl_all_engines(
                db, current_user.id, dry_run=dry_run, on_engine_done=on_engine_done
            )
            p = summary.pipeline
            done_payload = json.dumps({
                "done": True,
                "total_saved": p.saved,
                "total_skipped": p.skipped,
                "engines_crawled": summary.engines_crawled,
                "engines_errored": summary.engines_errored,
            })
            await queue.put(f"data: {done_payload}\n\n")
            await queue.put(None)  # sentinel

        crawl_task = asyncio.create_task(_run_crawl())

        while True:
            item = await queue.get()
            if item is None:
                break
            yield item

        await crawl_task  # propagate any exception

    return StreamingResponse(_generate(), media_type="text/event-stream")

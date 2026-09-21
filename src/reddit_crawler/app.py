"""FastAPI application: JSON API used by the panel + static file serving."""
from __future__ import annotations

import csv
import io
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import __version__
from .backends.arctic_shift import ArcticShiftBackend
from .backends.base import BackendError, FetchContext
from .backends.reddit_api import RedditApiBackend
from .config import (RedditCredentials, clear_credentials, db_path, default_user_agent, load_credentials,
                     save_credentials)
from .crawler import DEFAULT_MAX_PAGES, describe_timeframe, run_crawl
from .jobs import JobManager
from .keywords import KeywordError, KeywordSet, validate_keyword_set
from .storage import Storage
from .timeframe import PRESET_LABELS, TimeframeError, build_timeframe

STATIC_DIR = Path(__file__).parent / "static"
SUBREDDIT_RE = re.compile(r"^[A-Za-z0-9_]{2,21}$")

DEFAULT_KEYWORD_SET = {
    "id": "website-leads",
    "name": "People who want a website built",
    "include_any": [
        "need a website", "need a new website", "want a website", "looking for a web developer",
        "looking for a web designer", "looking for someone to build", "build me a website", "build my website",
        "make me a website", "someone to build a website", "someone to make a website", "get a website built",
        "get a website made", "who can build a website", "how much does a website cost",
        "how much should a website cost", "cost of a website", "quote for a website", "web design quote",
        "hire a web developer", "hire a web designer", "hire someone to build", "recommend a web developer",
        "recommend a web designer", "website for my business", "website for my small business",
        "redesign my website", "revamp my website", "need an ecommerce website", "need an online store",
        "should i hire a developer",
        "/\\b(need|want|looking for|searching for|seeking)\\b[^.!?\\n]{0,40}\\b(website|web ?site|landing page|online store|e-?commerce (?:site|store)|web app)\\b/ need … website",
        "/\\bhow much\\b[^.!?\\n]{0,40}\\b(website|web ?site|web design|landing page)\\b/ how much … website",
    ],
    "require_any": ["website", "web site", "landing page", "web page", "online store", "ecommerce", "e-commerce",
                    "web app", "wordpress", "shopify", "squarespace", "wix", "webflow", "web developer", "web designer"],
    "exclude": ["title: [hiring]", "title: [for hire]", "for hire", "hire me", "we are hiring", "my portfolio",
                "check out my", "dm me", "i built", "i made", "i created", "just launched", "i'm a web developer",
                "i am a web developer", "i'm a freelance", "our agency", "our services", "free consultation",
                "promo code", "discount code", "affiliate", "tutorial", "udemy", "bootcamp", "salary", "resume",
                "internship", "interview"],
    "exclude_authors": ["AutoModerator"],
    "whole_word": True,
    "plural_tolerant": True,
    "case_sensitive": False,
}


# ----------------------------------------------------------------- request models
class KeywordSetIn(BaseModel):
    id: str = ""
    name: str = "Untitled"
    include_any: list[str] | str = Field(default_factory=list)
    require_any: list[str] | str = Field(default_factory=list)
    exclude: list[str] | str = Field(default_factory=list)
    exclude_authors: list[str] | str = Field(default_factory=lambda: ["AutoModerator"])
    whole_word: bool = True
    plural_tolerant: bool = True
    case_sensitive: bool = False


class TimeframeIn(BaseModel):
    preset: str = "day"
    start: str | None = None
    end: str | None = None
    tz: str = "UTC"


class TargetsIn(BaseModel):
    posts: bool = True
    comments: bool = True


class RunIn(BaseModel):
    subreddits: list[str] | str
    keyword_set: KeywordSetIn
    timeframe: TimeframeIn
    targets: TargetsIn = Field(default_factory=TargetsIn)
    backend: str = "arctic"
    max_pages: int = Field(default=DEFAULT_MAX_PAGES, ge=1, le=2000)


class KeywordTestIn(BaseModel):
    keyword_set: KeywordSetIn
    title: str = ""
    text: str = ""


class CredentialsIn(BaseModel):
    client_id: str = ""
    client_secret: str | None = None     # None = keep the stored secret
    user_agent: str = ""
    username: str = ""


class TimeframePreviewIn(BaseModel):
    timeframe: TimeframeIn


# ------------------------------------------------------------------- helpers
def parse_subreddits(value: list[str] | str) -> list[str]:
    raw = value if isinstance(value, list) else re.split(r"[\s,;]+", value)
    out: list[str] = []
    for token in raw:
        name = token.strip().lstrip("/")
        if name.lower().startswith("r/"):
            name = name[2:]
        name = name.strip("/")
        if not name:
            continue
        if not SUBREDDIT_RE.match(name):
            raise HTTPException(400, f"{token!r} is not a valid subreddit name")
        if name.lower() not in [o.lower() for o in out]:
            out.append(name)
    if not out:
        raise HTTPException(400, "Enter at least one subreddit")
    if len(out) > 50:
        raise HTTPException(400, "At most 50 subreddits per run")
    return out


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:60] or f"set-{int(time.time())}"


def _no_job_context() -> FetchContext:
    """A throw-away context for one-off backend calls made from API handlers."""
    timeframe = build_timeframe({"preset": "day"})
    return FetchContext(timeframe=timeframe, cancel_event=threading.Event(), log=lambda _m: None,
                        on_request=lambda: None, on_progress=lambda _m: None, prefilter_terms=[])


def create_app(storage: Storage | None = None, *, seed_defaults: bool = True) -> FastAPI:
    storage = storage or Storage(db_path())
    storage.mark_stale_runs()
    if seed_defaults and not storage.list_keyword_sets():
        storage.save_keyword_set(DEFAULT_KEYWORD_SET["id"], DEFAULT_KEYWORD_SET)
    jobs = JobManager(storage, run_crawl)
    app = FastAPI(title="Reddit Crawler", version=__version__, docs_url="/api/docs", redoc_url=None)
    app.state.storage = storage
    app.state.jobs = jobs

    # ------------------------------------------------------------ static
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/static/{name}", include_in_schema=False)
    def static_file(name: str) -> FileResponse:
        path = (STATIC_DIR / name).resolve()
        if path.parent != STATIC_DIR.resolve() or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path)

    # ------------------------------------------------------------ status
    @app.get("/api/status")
    def status() -> dict:
        credentials = load_credentials()
        current = jobs.current()
        return {
            "version": __version__,
            "credentials": credentials.public_dict(),
            "backends": [
                {"id": "arctic", "label": "Arctic Shift archive (no login, date ranges, comments)", "available": True},
                {"id": "reddit", "label": "Official Reddit API (needs credentials)", "available": credentials.configured},
            ],
            "presets": [{"id": key, "label": label} for key, label in PRESET_LABELS.items()],
            "current_job": current.snapshot() if current else None,
            "last_params": storage.get_setting("last_params"),
            "default_max_pages": DEFAULT_MAX_PAGES,
        }

    # ------------------------------------------------------ keyword sets
    @app.get("/api/keyword-sets")
    def list_keyword_sets() -> list[dict]:
        return storage.list_keyword_sets()

    @app.post("/api/keyword-sets")
    def save_keyword_set(body: KeywordSetIn) -> dict:
        try:
            keyword_set = validate_keyword_set(body.model_dump())
        except KeywordError as exc:
            raise HTTPException(400, str(exc)) from exc
        set_id = body.id.strip() or slugify(keyword_set.name)
        keyword_set.id = set_id
        return storage.save_keyword_set(set_id, keyword_set.to_dict())

    @app.delete("/api/keyword-sets/{set_id}")
    def delete_keyword_set(set_id: str) -> dict:
        if not storage.delete_keyword_set(set_id):
            raise HTTPException(404, "Keyword set not found")
        return {"ok": True}

    @app.post("/api/keyword-sets/test")
    def test_keyword_set(body: KeywordTestIn) -> dict:
        try:
            matcher = validate_keyword_set(body.keyword_set.model_dump()).compile()
        except KeywordError as exc:
            raise HTTPException(400, str(exc)) from exc
        result = matcher.match(title=body.title, text=body.text)
        return {
            "matched": result.matched,
            "matched_terms": result.matched_terms,
            "excluded_by": [h.term for h in result.exclude_hits],
            "missing_required": bool(matcher.require and result.include_hits and not result.require_hits),
            "relevance": result.relevance,
            "title_spans": result.spans("title"),
            "text_spans": result.spans("text"),
            "prefilter_terms": matcher.server_terms(),
        }

    # --------------------------------------------------------- timeframe
    @app.post("/api/timeframe/preview")
    def timeframe_preview(body: TimeframePreviewIn) -> dict:
        try:
            return describe_timeframe(body.timeframe.model_dump())
        except TimeframeError as exc:
            raise HTTPException(400, str(exc)) from exc

    # -------------------------------------------------------------- runs
    @app.post("/api/runs", status_code=201)
    def start_run(body: RunIn) -> dict:
        subreddits = parse_subreddits(body.subreddits)
        try:
            keyword_set = validate_keyword_set(body.keyword_set.model_dump())
            timeframe = build_timeframe(body.timeframe.model_dump())
        except (KeywordError, TimeframeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        if not (body.targets.posts or body.targets.comments):
            raise HTTPException(400, "Select posts, comments or both")
        if body.backend not in ("arctic", "reddit"):
            raise HTTPException(400, "Unknown backend")
        if body.backend == "reddit" and not load_credentials().configured:
            raise HTTPException(400, "Reddit API credentials are not configured (see Settings)")
        params = {
            "subreddits": subreddits,
            "keyword_set": keyword_set.to_dict(),
            "timeframe": body.timeframe.model_dump(),
            "timeframe_resolved": timeframe.to_dict(),
            "targets": body.targets.model_dump(),
            "backend": body.backend,
            "max_pages": body.max_pages,
        }
        try:
            job = jobs.start(params)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        storage.set_setting("last_params", params)
        return {"run_id": job.id, "job": job.snapshot()}

    @app.get("/api/runs")
    def list_runs(limit: int = Query(50, ge=1, le=500)) -> list[dict]:
        return storage.list_runs(limit)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: int) -> dict:
        run = storage.get_run(run_id)
        if run is None:
            raise HTTPException(404, "Run not found")
        job = jobs.get(run_id)
        run["job"] = job.snapshot() if job else None
        return run

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: int) -> dict:
        if not jobs.cancel(run_id):
            raise HTTPException(409, "That run is not in progress")
        return {"ok": True}

    @app.delete("/api/runs/{run_id}")
    def delete_run(run_id: int) -> dict:
        job = jobs.get(run_id)
        if job and job.status not in ("done", "failed", "cancelled"):
            raise HTTPException(409, "Cancel the run before deleting it")
        if storage.get_run(run_id) is None:
            raise HTTPException(404, "Run not found")
        storage.delete_run(run_id)
        return {"ok": True}

    @app.get("/api/runs/{run_id}/results")
    def run_results(run_id: int) -> dict:
        run = storage.get_run(run_id)
        if run is None:
            raise HTTPException(404, "Run not found")
        job = jobs.get(run_id)
        return {"run": run, "job": job.snapshot() if job else None, "results": storage.get_results(run_id)}

    @app.get("/api/runs/{run_id}/export")
    def export_run(run_id: int, format: str = Query("csv", pattern="^(csv|json)$")) -> Response:
        run = storage.get_run(run_id)
        if run is None:
            raise HTTPException(404, "Run not found")
        rows = storage.get_full_results(run_id)
        stamp = time.strftime("%Y%m%d-%H%M", time.localtime(run["created_at"]))
        if format == "json":
            payload = {"run": run, "results": rows}
            return Response(json.dumps(payload, ensure_ascii=False, indent=2), media_type="application/json",
                            headers={"Content-Disposition": f'attachment; filename="reddit-run-{run_id}-{stamp}.json"'})
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["kind", "subreddit", "author", "created_utc", "created_iso", "title", "text", "url",
                         "score", "num_comments", "flair", "matched_keywords", "relevance", "is_new"])
        for row in rows:
            writer.writerow([
                row["kind"], row["subreddit"], row["author"], row["created_utc"],
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(row["created_utc"])),
                row["title"], row["text"], row["url"], row["score"], row["num_comments"], row["flair"] or "",
                "; ".join(row["matched"]), row["relevance"], int(row["is_new"]),
            ])
        data = "﻿" + buffer.getvalue()  # BOM so Excel opens UTF-8 correctly
        return Response(data, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="reddit-run-{run_id}-{stamp}.csv"'})

    # -------------------------------------------------------- subreddits
    @app.get("/api/subreddits/search")
    def search_subreddits(q: str = Query(..., min_length=1, max_length=50), backend: str = "arctic") -> dict:
        ctx = _no_job_context()
        try:
            if backend == "reddit":
                credentials = load_credentials()
                if not credentials.configured:
                    raise HTTPException(400, "Reddit API credentials are not configured")
                results = RedditApiBackend(credentials.client_id, credentials.client_secret, credentials.user_agent,
                                           credentials.username or None).search_subreddits(q, ctx)
            else:
                results = ArcticShiftBackend().search_subreddits(q, ctx)
        except BackendError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"query": q, "results": results}

    # ---------------------------------------------------------- settings
    @app.get("/api/settings/credentials")
    def get_credentials() -> dict:
        return load_credentials().public_dict()

    @app.put("/api/settings/credentials")
    def put_credentials(body: CredentialsIn) -> dict:
        if not body.client_id.strip():
            raise HTTPException(400, "Client ID is required")
        saved: RedditCredentials = save_credentials(body.client_id, body.client_secret, body.user_agent, body.username)
        if not saved.client_secret:
            raise HTTPException(400, "Client secret is required")
        return saved.public_dict()

    @app.delete("/api/settings/credentials")
    def delete_credentials() -> dict:
        clear_credentials()
        return load_credentials().public_dict()

    @app.post("/api/settings/credentials/test")
    def test_credentials() -> dict:
        credentials = load_credentials()
        if not credentials.configured:
            raise HTTPException(400, "Save a client id and secret first")
        backend = RedditApiBackend(credentials.client_id, credentials.client_secret,
                                   credentials.user_agent or default_user_agent(credentials.username or None),
                                   credentials.username or None)
        try:
            return backend.test_connection()
        except BackendError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/api/data/purge")
    def purge_data() -> dict:
        if jobs.current():
            raise HTTPException(409, "Cancel the running crawl first")
        storage.purge_all()
        return {"ok": True}

    @app.exception_handler(HTTPException)
    async def http_error(_request: Any, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app

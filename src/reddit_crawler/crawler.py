"""One crawl run: sweep each subreddit, match client-side, persist matches."""
from __future__ import annotations

from .backends.arctic_shift import ArcticShiftBackend
from .backends.base import Backend, BackendError, CancelledError, FetchContext
from .backends.reddit_api import RedditApiBackend
from .config import load_credentials
from .jobs import Job
from .keywords import KeywordSet, MatchResult
from .models import Coverage, Item, RunStats
from .storage import Storage
from .timeframe import build_timeframe, format_local

DEFAULT_MAX_PAGES = 100


def make_backend(name: str) -> Backend:
    """``arctic`` (default) or ``reddit`` (needs credentials in ``.env``)."""
    if name == "reddit":
        credentials = load_credentials()
        if not credentials.configured:
            raise BackendError("Reddit API credentials are not configured — open Settings, or use Arctic Shift.")
        return RedditApiBackend(credentials.client_id, credentials.client_secret,
                                credentials.user_agent, credentials.username or None)
    return ArcticShiftBackend()


def run_crawl(job: Job, storage: Storage, backend: Backend | None = None) -> RunStats:
    params = job.params
    timeframe = build_timeframe(params["timeframe"])
    keyword_set = KeywordSet.from_dict(params["keyword_set"])
    matcher = keyword_set.compile()
    backend = backend or make_backend(params.get("backend") or "arctic")
    subreddits: list[str] = params["subreddits"]
    kinds = [k for k, enabled in (("post", params["targets"].get("posts", True)),
                                  ("comment", params["targets"].get("comments", False))) if enabled]
    ctx = FetchContext(
        timeframe=timeframe,
        cancel_event=job.cancel_event,
        log=job.log,
        on_request=job.count_request,
        on_progress=job.set_progress,
        prefilter_terms=matcher.server_terms(),
        max_pages=int(params.get("max_pages") or DEFAULT_MAX_PAGES),
    )
    stats = RunStats()
    job.log(f"Backend: {backend.name} · timeframe: {timeframe.label} · "
            f"{len(subreddits)} subreddit(s) · {', '.join(k + 's' for k in kinds)}")
    if ctx.prefilter_terms:
        job.log(f"Server-side prefilter available ({len(ctx.prefilter_terms)} plain phrases)")

    sources = [(subreddit, kind) for subreddit in subreddits for kind in kinds]
    for index, (subreddit, kind) in enumerate(sources):
        job.set_phase(f"r/{subreddit} · {kind}s", index, len(sources))
        coverage = Coverage(subreddit, kind)
        matches: list[tuple[Item, MatchResult]] = []
        fetched = 0
        try:
            iterator = backend.fetch_posts(subreddit, ctx, coverage) if kind == "post" \
                else backend.fetch_comments(subreddit, ctx, coverage)
            for item in iterator:
                fetched += 1
                result = matcher.match(title=item.title if kind == "post" else "",
                                       text=item.text, author=item.author)
                if result.matched:
                    matches.append((item, result))
                if fetched % 50 == 0:
                    job.add_counts(fetched=50)
        except CancelledError:
            _persist(job, storage, backend, ctx, kind, matches, stats, coverage)
            raise
        except BackendError as exc:
            coverage.complete = False
            coverage.note = f"failed: {exc}"
            job.log(f"r/{subreddit} {kind}s: {exc}")
        job.add_counts(fetched=fetched % 50)
        if kind == "post":
            stats.fetched_posts += fetched
        else:
            stats.fetched_comments += fetched
        _persist(job, storage, backend, ctx, kind, matches, stats, coverage)
        job.log(f"r/{subreddit} {kind}s: {fetched} fetched, {len(matches)} matched"
                + ("" if coverage.complete else " — PARTIAL"))

    job.warnings = [f"r/{c.subreddit} {c.kind}s: {c.note}" for c in stats.coverage if not c.complete and c.note]
    stats.requests = job.requests
    storage.update_run(job.id, stats=stats.to_dict(), warnings=job.warnings)
    job.set_phase("Finished", len(sources), len(sources))
    return stats


def _persist(job: Job, storage: Storage, backend: Backend, ctx: FetchContext, kind: str,
             matches: list[tuple[Item, MatchResult]], stats: RunStats, coverage: Coverage) -> None:
    """Save one (subreddit, kind) batch so partial results show up while the run continues."""
    stats.coverage.append(coverage)
    if not matches:
        stats.requests = job.requests
        storage.update_run(job.id, stats=stats.to_dict())
        return
    if kind == "comment":
        try:
            backend.resolve_post_titles([item for item, _ in matches], ctx)
        except CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - titles are cosmetic
            job.log(f"Could not resolve parent post titles: {exc}")
    rows = [(item, result.matched_terms, result.relevance, result.spans("title"), result.spans("text"))
            for item, result in matches]
    new_count = storage.save_matches(job.id, rows)
    if kind == "post":
        stats.matched_posts += len(matches)
    else:
        stats.matched_comments += len(matches)
    stats.new_items += new_count
    stats.requests = job.requests
    job.add_counts(matched=len(matches), new_items=new_count)
    job.results_changed()
    storage.update_run(job.id, stats=stats.to_dict())


def describe_timeframe(timeframe_params: dict) -> dict:
    """Resolve a timeframe for display (used by the API's validation endpoint)."""
    timeframe = build_timeframe(timeframe_params)
    return {
        **timeframe.to_dict(),
        "start_local": format_local(timeframe.start_utc, timeframe.tz_name),
        "end_local": format_local(timeframe.end_utc, timeframe.tz_name) if timeframe.end_utc else "now",
    }

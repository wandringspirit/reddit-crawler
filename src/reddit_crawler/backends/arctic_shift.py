"""Arctic Shift backend (https://arctic-shift.photon-reddit.com).

A free, unauthenticated, near-real-time archive of Reddit that supports true
date ranges for both posts and comments.  Strategy:

* posts    – "window sweep": ``after``/``before`` + ``limit=auto`` + ``sort=desc``,
             paginated with ``before = oldest_created_utc + 1`` (dedupe by id).
* comments – window sweep while it is cheap; for wide windows on busy
             subreddits switch to the server-side full-text prefilter
             (``body="phrase" OR "phrase"``) which is stemmed and case-insensitive,
             i.e. a recall layer only.  Client-side matching always confirms.

Rate limits are dynamic; on HTTP 429/422 we honour ``X-RateLimit-Reset``.
"""
from __future__ import annotations

import time
from typing import Iterator, Sequence

import requests

from ..models import Coverage, Item, reddit_permalink
from .base import BackendError, CancelledError, FetchContext

BASE_URL = "https://arctic-shift.photon-reddit.com"
POST_FIELDS = "id,author,created_utc,subreddit,title,selftext,score,num_comments,link_flair_text,over_18,url"
COMMENT_FIELDS = "id,author,created_utc,subreddit,body,score,link_id,parent_id"
MIN_INTERVAL = 3.0           # idle seconds after each response before the next request (~15 pages/min is safe)
MAX_INTERVAL = 10.0          # adaptive pacing never waits longer than this between requests
MAX_ATTEMPTS = 6
MAX_BACKOFF = 90             # seconds
WIDEN_STEPS = (3600, 6 * 3600, 24 * 3600)   # earlier `after` bounds tried when a window page times out
COMMENT_WINDOW_MAX_PAGES = 10   # beyond this (~40 s of sweeping), comments switch to the prefilter strategy
PREFILTER_QUERY_MAX_CHARS = 240
REQUEST_TIMEOUT = 120


class ArcticShiftBackend:
    name = "arctic"

    def __init__(self, user_agent: str = "reddit-crawler/0.1 (local lead-finding panel)",
                 session: requests.Session | None = None, base_url: str = BASE_URL,
                 min_interval: float = MIN_INTERVAL):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.min_interval = min_interval
        self.interval = min_interval     # current pacing, adapted from the server's responses
        self._last_response = 0.0
        self._success_streak = 0

    # ------------------------------------------------------------------ HTTP
    def _get(self, path: str, params: dict, ctx: FetchContext, *, alternatives: Sequence[dict] = ()) -> dict:
        return self._get_with_shape(path, params, ctx, alternatives=alternatives)[0]

    def _get_with_shape(self, path: str, params: dict, ctx: FetchContext, *,
                        alternatives: Sequence[dict] = ()) -> tuple[dict, int]:
        """GET with pacing, retries and *alternatives*: other parameter shapes to try when the
        archive answers HTTP 422 "Timeout" for the original one.

        A 422 is either load shedding (a later retry works) or a query that the archive's
        database cannot plan within its statement timeout (the same request fails forever,
        but a slightly different window works).  We therefore try the alternative shapes
        quickly before waiting for the rate-limit window to reset.  Returns the payload and
        the index of the shape that succeeded (0 = the original parameters).
        """
        url = f"{self.base_url}{path}"
        shapes = [params, *alternatives]
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            ctx.check_cancelled()
            current = shapes[(attempt - 1) % len(shapes)]
            wait = self.interval - (time.monotonic() - self._last_response)
            if wait > 0:
                self._sleep(wait, ctx)
            ctx.on_request()
            try:
                response = self.session.get(url, params=current, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                last_error = exc
                self._last_response = time.monotonic()
                ctx.log(f"Network error ({exc.__class__.__name__}); retry {attempt}/{MAX_ATTEMPTS}")
                self._sleep(min(5 * attempt, MAX_BACKOFF), ctx)
                continue
            self._last_response = time.monotonic()
            if response.status_code == 200:
                self._note_success()
                try:
                    return response.json(), (attempt - 1) % len(shapes)
                except ValueError as exc:
                    raise BackendError(f"Arctic Shift returned invalid JSON for {path}") from exc
            if response.status_code in (429, 422, 503):
                reset = _reset_seconds(response)
                more_shapes = attempt < len(shapes)
                if response.status_code == 422 and more_shapes:
                    delay = 3.0                       # try the next query shape right away
                elif response.status_code == 422 and attempt == 1:
                    delay = 5.0                       # single shape: one quick retry first
                else:
                    delay = min(max(reset, 5.0), MAX_BACKOFF)
                    self._note_throttled()
                ctx.log(f"Arctic Shift answered HTTP {response.status_code} "
                        f"({'trying another query shape' if response.status_code == 422 and more_shapes else f'waiting {delay:.0f}s'}, "
                        f"pacing {self.interval:.0f}s)")
                last_error = BackendError(f"HTTP {response.status_code}: {response.text[:120]}")
                self._sleep(delay, ctx)
                continue
            if response.status_code == 400:
                raise BackendError(f"Arctic Shift rejected the request: {response.text[:200]}")
            raise BackendError(f"Arctic Shift HTTP {response.status_code}: {response.text[:200]}")
        raise BackendError(f"Arctic Shift kept failing for {path}: {last_error}")

    def _note_success(self) -> None:
        self._success_streak += 1
        if self._success_streak >= 20 and self.interval > self.min_interval:
            self.interval = max(self.min_interval, self.interval - 0.5)
            self._success_streak = 0

    def _note_throttled(self) -> None:
        self._success_streak = 0
        self.interval = min(MAX_INTERVAL, self.interval + 1.0)

    @staticmethod
    def _sleep(seconds: float, ctx: FetchContext) -> None:
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if ctx.cancel_event.wait(min(remaining, 0.5)):
                raise CancelledError("Cancelled")

    # ------------------------------------------------------------- sweeping
    def _sweep_pages(self, path: str, base_params: dict, ctx: FetchContext, coverage: Coverage,
                     *, max_pages: int | None = None) -> Iterator[list[dict]]:
        """Yield pages of *fresh* raw rows newest→oldest for the timeframe, paginating with ``before``."""
        timeframe = ctx.timeframe
        before = timeframe.end_utc if timeframe.end_utc is not None else int(time.time()) + 60
        seen: set[str] = set()
        pages = 0
        limit = max_pages if max_pages is not None else ctx.max_pages
        widen_index = 0  # sticky: once a wider `after` bound was needed, keep using it for this sweep
        while True:
            ctx.check_cancelled()
            # Some narrow windows make the archive's query planner time out; the same page with an
            # earlier `after` bound usually works, and rows outside the timeframe are dropped below.
            widths = (0, *WIDEN_STEPS)[widen_index:]
            shapes = [dict(base_params, after=timeframe.start_utc - widen, before=before, sort="desc")
                      for widen in widths]
            if isinstance(base_params.get("limit"), int):  # keyword prefilter: the archive suggests a smaller limit
                shapes.append(dict(shapes[0], limit=max(25, base_params["limit"] // 4)))
            payload, used = self._get_with_shape(path, shapes[0], ctx, alternatives=shapes[1:])
            if used < len(widths):
                widen_index += used
            raw_rows = payload.get("data") or []
            rows = [row for row in raw_rows if timeframe.contains(int(row.get("created_utc") or 0))]
            # a widened request that returned rows older than the window proves the window is exhausted
            passed_start = any(int(row.get("created_utc") or 0) < timeframe.start_utc for row in raw_rows)
            pages += 1
            coverage.pages += 1
            fresh = [row for row in rows if str(row.get("id")) not in seen]
            for row in fresh:
                seen.add(str(row.get("id")))
            if fresh:
                oldest = min(int(row["created_utc"]) for row in fresh)
                coverage.oldest_utc = oldest if coverage.oldest_utc is None else min(coverage.oldest_utc, oldest)
                yield fresh
            if not rows or not fresh or passed_start:
                return  # exhausted the window
            oldest = min(int(row["created_utc"]) for row in rows)
            # `before` is exclusive: +1 keeps same-second siblings, dedupe drops the repeated row.
            next_before = oldest + 1
            if next_before >= before:
                next_before = oldest  # every row shares one second: step past it
            before = next_before
            if oldest <= timeframe.start_utc:
                return
            if pages >= limit:
                coverage.complete = False
                coverage.note = (f"stopped after {pages} pages (limit); only reached "
                                 f"{_fmt(coverage.oldest_utc)} — narrow the date range or raise the page limit")
                return

    def _sweep(self, path: str, base_params: dict, ctx: FetchContext, coverage: Coverage,
               *, max_pages: int | None = None) -> Iterator[dict]:
        for page in self._sweep_pages(path, base_params, ctx, coverage, max_pages=max_pages):
            yield from page

    def fetch_posts(self, subreddit: str, ctx: FetchContext, coverage: Coverage) -> Iterator[Item]:
        coverage.strategy = "window"
        params = {"subreddit": subreddit, "limit": "auto", "fields": POST_FIELDS}
        for row in self._sweep("/api/posts/search", params, ctx, coverage):
            coverage.fetched += 1
            if coverage.fetched % 200 == 0:
                ctx.on_progress(f"r/{subreddit}: {coverage.fetched} posts fetched (back to {_fmt(coverage.oldest_utc)})")
            yield _post_item(row)

    def fetch_comments(self, subreddit: str, ctx: FetchContext, coverage: Coverage) -> Iterator[Item]:
        coverage.strategy = "window"
        params = {"subreddit": subreddit, "limit": "auto", "fields": COMMENT_FIELDS}
        timeframe = ctx.timeframe
        pages = self._sweep_pages("/api/comments/search", params, ctx, coverage)
        # The first page doubles as a probe: how much of the window does one page cover?
        first_page = next(pages, [])
        for row in first_page:
            coverage.fetched += 1
            yield _comment_item(row)
        if not first_page:
            return
        created = [int(row["created_utc"]) for row in first_page]
        page_span = max(1, max(created) - min(created))
        remaining = max(0, min(created) - timeframe.start_utc)
        estimated_pages = remaining / page_span
        seen = {str(row["id"]) for row in first_page}
        if estimated_pages > COMMENT_WINDOW_MAX_PAGES and ctx.prefilter_terms:
            ctx.log(f"r/{subreddit}: ~{int(estimated_pages)} more comment pages needed; using keyword prefilter instead")
            coverage.strategy = "prefilter"
            try:
                for item in self._prefiltered_comments(subreddit, ctx, coverage, seen=seen):
                    yield item
                pages.close()
                return
            except BackendError as exc:
                # keep going with the (slower but complete) window sweep; `pages` still holds its cursor
                ctx.log(f"r/{subreddit}: keyword prefilter failed ({exc}); continuing with the full sweep")
                coverage.strategy = "window"
        elif estimated_pages > ctx.max_pages:
            ctx.log(f"r/{subreddit}: ~{int(estimated_pages)} comment pages needed (limit {ctx.max_pages}); "
                    "add a 'must also contain' word to enable the keyword prefilter, or narrow the range")
        for page in pages:
            for row in page:
                if str(row["id"]) in seen:
                    continue
                seen.add(str(row["id"]))
                coverage.fetched += 1
                yield _comment_item(row)
            ctx.on_progress(f"r/{subreddit}: {coverage.fetched} comments fetched (back to {_fmt(coverage.oldest_utc)})")

    def _prefiltered_comments(self, subreddit: str, ctx: FetchContext, coverage: Coverage,
                              *, seen: set[str]) -> Iterator[Item]:
        """Server-side full-text search (stemmed, case-insensitive) as a recall layer.

        Raises BackendError if a batch cannot be fetched so the caller can fall back.
        """
        batches = build_fts_queries(ctx.prefilter_terms)
        for index, query in enumerate(batches, 1):
            ctx.on_progress(f"r/{subreddit}: keyword prefilter batch {index}/{len(batches)}")
            batch_cov = Coverage(subreddit, "comment")
            params = {"subreddit": subreddit, "limit": 100, "fields": COMMENT_FIELDS, "body": query}
            for row in self._sweep("/api/comments/search", params, ctx, batch_cov):
                if str(row["id"]) in seen:
                    continue
                seen.add(str(row["id"]))
                coverage.fetched += 1
                yield _comment_item(row)
            coverage.pages += batch_cov.pages
            if batch_cov.oldest_utc is not None:
                coverage.oldest_utc = (batch_cov.oldest_utc if coverage.oldest_utc is None
                                       else min(coverage.oldest_utc, batch_cov.oldest_utc))
            if not batch_cov.complete:
                coverage.complete = False
                coverage.note = batch_cov.note

    # ------------------------------------------------------------- helpers
    def resolve_post_titles(self, items: list[Item], ctx: FetchContext) -> None:
        wanted = sorted({item.link_id[3:] for item in items if item.kind == "comment" and item.link_id and not item.title})
        titles: dict[str, dict] = {}
        for start in range(0, len(wanted), 200):
            chunk = wanted[start:start + 200]
            try:
                payload = self._get("/api/posts/ids", {"ids": ",".join(chunk), "fields": "id,title,num_comments,score"}, ctx)
            except BackendError as exc:
                ctx.log(f"Could not resolve parent post titles: {exc}")
                break
            for row in payload.get("data") or []:
                titles[str(row.get("id"))] = row
        for item in items:
            if item.kind == "comment" and item.link_id:
                row = titles.get(item.link_id[3:])
                if row:
                    item.title = row.get("title") or ""
                    if item.num_comments is None:
                        item.num_comments = row.get("num_comments")

    def search_subreddits(self, prefix: str, ctx: FetchContext, limit: int = 15) -> list[dict]:
        prefix = prefix.strip().lstrip("r/").strip("/")
        if not prefix:
            return []
        payload = self._get("/api/subreddits/search", {"subreddit_prefix": prefix, "limit": limit,
                                                        "sort": "desc", "sort_type": "subscribers"}, ctx)
        out = []
        for row in payload.get("data") or []:
            out.append({
                "name": row.get("display_name") or row.get("subreddit") or "",
                "subscribers": row.get("subscribers"),
                "description": (row.get("public_description") or row.get("title") or "")[:160],
                "over_18": bool(row.get("over18") or row.get("over_18")),
            })
        return [r for r in out if r["name"]]


def build_fts_queries(terms: list[str], max_chars: int = PREFILTER_QUERY_MAX_CHARS) -> list[str]:
    """Pack phrases into ``"a b" OR c`` websearch queries no longer than *max_chars*."""
    pieces = []
    for term in terms:
        term = " ".join(term.split())
        if not term:
            continue
        pieces.append(f'"{term}"' if " " in term else term)
    batches: list[str] = []
    current: list[str] = []
    for piece in pieces:
        candidate = " OR ".join(current + [piece])
        if current and len(candidate) > max_chars:
            batches.append(" OR ".join(current))
            current = [piece]
        else:
            current.append(piece)
    if current:
        batches.append(" OR ".join(current))
    return batches


def _reset_seconds(response: requests.Response) -> float:
    for header in ("X-RateLimit-Reset", "Retry-After"):
        value = response.headers.get(header)
        if value:
            try:
                return float(value)
            except ValueError:
                pass
    return 10.0


def _fmt(epoch: int | None) -> str:
    if epoch is None:
        return "?"
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(epoch))


def _post_item(row: dict) -> Item:
    post_id = str(row.get("id"))
    subreddit = str(row.get("subreddit") or "")
    url = row.get("url") or ""
    permalink = reddit_permalink(subreddit, post_id)
    is_self = bool(url) and f"/comments/{post_id}" in url
    return Item(
        fullname=f"t3_{post_id}",
        kind="post",
        subreddit=subreddit,
        author=str(row.get("author") or "[deleted]"),
        created_utc=int(row.get("created_utc") or 0),
        title=str(row.get("title") or ""),
        text=str(row.get("selftext") or ""),
        url=permalink,
        score=row.get("score"),
        num_comments=row.get("num_comments"),
        flair=row.get("link_flair_text") or None,
        is_self=is_self if url else None,
        over_18=bool(row.get("over_18")),
        source="arctic",
    )


def _comment_item(row: dict) -> Item:
    comment_id = str(row.get("id"))
    subreddit = str(row.get("subreddit") or "")
    link_id = str(row.get("link_id") or "")
    post_id = link_id[3:] if link_id.startswith("t3_") else link_id
    return Item(
        fullname=f"t1_{comment_id}",
        kind="comment",
        subreddit=subreddit,
        author=str(row.get("author") or "[deleted]"),
        created_utc=int(row.get("created_utc") or 0),
        title="",
        text=str(row.get("body") or ""),
        url=reddit_permalink(subreddit, post_id, comment_id) if post_id else "",
        score=row.get("score"),
        link_id=link_id or None,
        source="arctic",
    )

"""Official Reddit Data API backend via PRAW (optional, needs OAuth credentials).

Limits that shape this backend (verified Sept 2026):

* listings (``/new``, ``/r/x/comments``) return at most ~1000 items;
* ``/search`` only knows relative windows (``t=hour|day|week|month|year|all``),
  arbitrary date ranges are filtered client-side;
* there is no comment keyword search at all.

Posts are swept from ``/new`` and, when the listing cap is hit before the start
of the timeframe, complemented with keyword ``/search`` (recall layer).
Comments come from the newest-comments listing only.  PRAW is not thread-safe:
the ``praw.Reddit`` instance is created lazily inside the worker thread.
"""
from __future__ import annotations

import time
from typing import Iterator

from ..models import Coverage, Item, reddit_permalink
from .base import BackendError, FetchContext

LISTING_CAP = 1000
SEARCH_QUERY_MAX_CHARS = 480


class RedditApiBackend:
    name = "reddit"

    def __init__(self, client_id: str, client_secret: str, user_agent: str, username: str | None = None):
        if not client_id or not client_secret:
            raise BackendError("Reddit API credentials are not configured.")
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_agent = user_agent or "windows:reddit-crawler:0.1.0"
        self.username = username or None
        self._reddit = None

    # ------------------------------------------------------------------ PRAW
    @property
    def reddit(self):
        if self._reddit is None:
            import praw  # imported lazily so the Arctic Shift path never needs it

            self._reddit = praw.Reddit(
                client_id=self.client_id,
                client_secret=self.client_secret,
                user_agent=self.user_agent,
                check_for_updates=False,
                check_for_async=False,
            )
        return self._reddit

    def test_connection(self) -> dict:
        """Cheap read-only call; raises BackendError with a readable message."""
        try:
            subreddit = self.reddit.subreddit("announcements")
            subscribers = subreddit.subscribers
        except Exception as exc:  # prawcore raises many different classes
            raise BackendError(_explain(exc)) from exc
        limits = dict(self.reddit.auth.limits or {})
        return {"ok": True, "sample": f"r/announcements has {subscribers:,} subscribers",
                "rate_limit": {k: v for k, v in limits.items() if v is not None}}

    # -------------------------------------------------------------- fetching
    def fetch_posts(self, subreddit: str, ctx: FetchContext, coverage: Coverage) -> Iterator[Item]:
        coverage.strategy = "listing"
        timeframe = ctx.timeframe
        seen: set[str] = set()
        reached_start = False
        try:
            for index, submission in enumerate(self.reddit.subreddit(subreddit).new(limit=None), 1):
                ctx.check_cancelled()
                if index % 100 == 1:
                    ctx.on_request()
                    coverage.pages += 1
                created = int(submission.created_utc)
                coverage.oldest_utc = created if coverage.oldest_utc is None else min(coverage.oldest_utc, created)
                if created < timeframe.start_utc:
                    reached_start = True
                    break
                if not timeframe.contains(created):
                    continue
                seen.add(submission.fullname)
                coverage.fetched += 1
                yield _submission_item(submission)
        except Exception as exc:
            raise BackendError(_explain(exc)) from exc

        if reached_start:
            return
        # Listing cap hit before the start of the window: fall back to keyword search.
        queries = build_search_queries(ctx.prefilter_terms)
        if not queries:
            coverage.complete = False
            coverage.note = (f"Reddit's newest-posts listing only reaches {_fmt(coverage.oldest_utc)}; "
                             "older posts in this range are unreachable without a plain-text phrase list")
            return
        coverage.strategy = "listing+search"
        time_filter = timeframe.reddit_time_filter()
        oldest_search: int | None = None
        try:
            for query in queries:
                ctx.on_progress(f"r/{subreddit}: keyword search ({time_filter})")
                for index, submission in enumerate(
                        self.reddit.subreddit(subreddit).search(query, sort="new", time_filter=time_filter, limit=None), 1):
                    ctx.check_cancelled()
                    if index % 100 == 1:
                        ctx.on_request()
                        coverage.pages += 1
                    created = int(submission.created_utc)
                    oldest_search = created if oldest_search is None else min(oldest_search, created)
                    if created < timeframe.start_utc:
                        break
                    if submission.fullname in seen or not timeframe.contains(created):
                        continue
                    seen.add(submission.fullname)
                    coverage.fetched += 1
                    yield _submission_item(submission)
        except Exception as exc:
            raise BackendError(_explain(exc)) from exc
        if oldest_search is None or oldest_search > timeframe.start_utc:
            coverage.complete = False
            coverage.note = (f"newest-posts listing reached {_fmt(coverage.oldest_utc)}; keyword search reached "
                             f"{_fmt(oldest_search)} — earlier posts in this range may be missing (Reddit API caps)")

    def fetch_comments(self, subreddit: str, ctx: FetchContext, coverage: Coverage) -> Iterator[Item]:
        coverage.strategy = "listing"
        timeframe = ctx.timeframe
        reached_start = False
        try:
            for index, comment in enumerate(self.reddit.subreddit(subreddit).comments(limit=None), 1):
                ctx.check_cancelled()
                if index % 100 == 1:
                    ctx.on_request()
                    coverage.pages += 1
                created = int(comment.created_utc)
                coverage.oldest_utc = created if coverage.oldest_utc is None else min(coverage.oldest_utc, created)
                if created < timeframe.start_utc:
                    reached_start = True
                    break
                if not timeframe.contains(created):
                    continue
                coverage.fetched += 1
                yield _comment_item(comment)
        except Exception as exc:
            raise BackendError(_explain(exc)) from exc
        if not reached_start:
            coverage.complete = False
            coverage.note = (f"Reddit only exposes the newest ~{LISTING_CAP} comments (back to {_fmt(coverage.oldest_utc)}); "
                             "there is no comment search in the official API — use Arctic Shift for wider ranges")

    def resolve_post_titles(self, items: list[Item], ctx: FetchContext) -> None:
        wanted = sorted({item.link_id for item in items if item.kind == "comment" and item.link_id and not item.title})
        if not wanted:
            return
        titles: dict[str, str] = {}
        try:
            for start in range(0, len(wanted), 100):
                ctx.on_request()
                for submission in self.reddit.info(fullnames=wanted[start:start + 100]):
                    titles[submission.fullname] = submission.title
        except Exception as exc:
            ctx.log(f"Could not resolve parent post titles: {_explain(exc)}")
        for item in items:
            if item.kind == "comment" and item.link_id in titles:
                item.title = titles[item.link_id]

    def search_subreddits(self, query: str, ctx: FetchContext, limit: int = 15) -> list[dict]:
        ctx.on_request()
        try:
            results = self.reddit.subreddits.search(query, limit=limit)
            return [{
                "name": s.display_name,
                "subscribers": getattr(s, "subscribers", None),
                "description": (getattr(s, "public_description", "") or "")[:160],
                "over_18": bool(getattr(s, "over18", False)),
            } for s in results]
        except Exception as exc:
            raise BackendError(_explain(exc)) from exc


def build_search_queries(terms: list[str], max_chars: int = SEARCH_QUERY_MAX_CHARS) -> list[str]:
    """Quoted phrases joined by OR — the only operators Reddit search honours reliably."""
    pieces = [f'"{" ".join(t.split())}"' for t in terms if t.strip()]
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


def _explain(exc: Exception) -> str:
    name = exc.__class__.__name__
    text = str(exc)
    if "401" in text:
        return "Reddit rejected the credentials (HTTP 401). Check the client id / secret."
    if "403" in text:
        return "Reddit refused access (HTTP 403). The app may not be approved for the Data API."
    if "429" in text or name == "TooManyRequests":
        return "Reddit rate limit hit (HTTP 429). Wait a minute and try again."
    return f"{name}: {text}" if text else name


def _fmt(epoch: int | None) -> str:
    if epoch is None:
        return "?"
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(epoch))


def _submission_item(submission) -> Item:
    subreddit = str(submission.subreddit)
    return Item(
        fullname=submission.fullname,
        kind="post",
        subreddit=subreddit,
        author=str(submission.author) if submission.author else "[deleted]",
        created_utc=int(submission.created_utc),
        title=submission.title or "",
        text=submission.selftext or "",
        url=reddit_permalink(subreddit, submission.id),
        score=submission.score,
        num_comments=submission.num_comments,
        flair=getattr(submission, "link_flair_text", None) or None,
        is_self=bool(getattr(submission, "is_self", False)),
        over_18=bool(getattr(submission, "over_18", False)),
        source="reddit",
    )


def _comment_item(comment) -> Item:
    subreddit = str(comment.subreddit)
    link_id = comment.link_id
    return Item(
        fullname=comment.fullname,
        kind="comment",
        subreddit=subreddit,
        author=str(comment.author) if comment.author else "[deleted]",
        created_utc=int(comment.created_utc),
        title=getattr(comment, "link_title", "") or "",
        text=comment.body or "",
        url=reddit_permalink(subreddit, link_id[3:], comment.id),
        score=comment.score,
        link_id=link_id,
        source="reddit",
    )

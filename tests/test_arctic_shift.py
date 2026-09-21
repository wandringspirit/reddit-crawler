"""Arctic Shift backend against a fake HTTP session (no network)."""
import json
import threading

import pytest

from reddit_crawler.backends.arctic_shift import (COMMENT_WINDOW_MAX_PAGES, ArcticShiftBackend, build_fts_queries)
from reddit_crawler.backends.base import BackendError, CancelledError, FetchContext
from reddit_crawler.models import Coverage
from reddit_crawler.timeframe import build_timeframe

NOW = 1_789_000_000  # ~10 days before the real clock so fake rows are never 'in the future'


class FakeResponse:
    def __init__(self, status, payload, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    """Serves posts/comments from an in-memory list, honouring after/before/limit like the real API."""

    def __init__(self, posts=(), comments=(), page_size=3):
        self.headers = {}
        self.posts = sorted(posts, key=lambda r: -r["created_utc"])
        self.comments = sorted(comments, key=lambda r: -r["created_utc"])
        self.page_size = page_size
        self.calls = []
        self.fail_next = []  # list of (status, headers) to return before succeeding

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if self.fail_next:
            status, headers = self.fail_next.pop(0)
            return FakeResponse(status, {"data": None, "error": "slow down"}, headers)
        if url.endswith("/api/posts/ids"):
            ids = params["ids"].split(",")
            return FakeResponse(200, {"data": [dict(id=i, title=f"Title {i}", num_comments=7) for i in ids]})
        rows = self.posts if "/posts/" in url else self.comments
        if "body" in params:
            rows = [r for r in rows if any(w.strip('"').lower() in r["body"].lower() for w in params["body"].split(" OR "))]
        after, before = int(params["after"]), int(params["before"])
        rows = [r for r in rows if after <= r["created_utc"] < before]
        return FakeResponse(200, {"data": rows[: self.page_size]})


def post(i, created, title="hello", selftext="body"):
    return {"id": f"p{i}", "author": "alice", "created_utc": created, "subreddit": "test", "title": title,
            "selftext": selftext, "score": 1, "num_comments": 0, "link_flair_text": None, "over_18": False,
            "url": f"https://www.reddit.com/r/test/comments/p{i}/x/"}


def comment(i, created, body="text"):
    return {"id": f"c{i}", "author": "bob", "created_utc": created, "subreddit": "test", "body": body, "score": 1,
            "link_id": "t3_p1", "parent_id": "t3_p1"}


def make_ctx(preset="day", prefilter=(), max_pages=100, cancel=None):
    logs = []
    tf = build_timeframe({"preset": preset, "tz": "UTC"}, now=NOW)
    ctx = FetchContext(timeframe=tf, cancel_event=cancel or threading.Event(), log=logs.append,
                       on_request=lambda: None, on_progress=lambda _m: None, prefilter_terms=list(prefilter),
                       max_pages=max_pages)
    return ctx, logs


@pytest.fixture
def backend_factory(monkeypatch):
    def make(session):
        backend = ArcticShiftBackend(session=session, min_interval=0)
        monkeypatch.setattr(backend, "_sleep", lambda seconds, ctx: None)
        return backend
    return make


def test_posts_are_paginated_with_before_and_deduplicated(backend_factory):
    posts = [post(i, NOW - i * 600) for i in range(10)] + [post(99, NOW - 86400 * 3)]  # last one outside the window
    session = FakeSession(posts=posts, page_size=4)
    backend = backend_factory(session)
    ctx, _ = make_ctx("day")
    coverage = Coverage("test", "post")
    items = list(backend.fetch_posts("test", ctx, coverage))
    assert [i.fullname for i in items] == [f"t3_p{i}" for i in range(10)]
    assert coverage.complete and coverage.fetched == 10
    assert coverage.oldest_utc == NOW - 9 * 600
    # every page after the first uses before = oldest + 1 (exclusive cursor keeps same-second siblings)
    befores = [c[1]["before"] for c in session.calls[1:]]
    assert befores[0] == NOW - 3 * 600 + 1
    assert all(c[1]["fields"] for c in session.calls)
    assert items[0].url == "https://www.reddit.com/r/test/comments/p0/"
    assert items[0].is_self is True


def test_page_limit_marks_coverage_incomplete(backend_factory):
    posts = [post(i, NOW - i * 60) for i in range(30)]
    backend = backend_factory(FakeSession(posts=posts, page_size=5))
    ctx, _ = make_ctx("day", max_pages=2)
    coverage = Coverage("test", "post")
    items = list(backend.fetch_posts("test", ctx, coverage))
    assert len(items) == 9  # 5 + 4: the boundary row of page 1 is re-served on page 2 and deduplicated
    assert not coverage.complete and "limit" in coverage.note


def test_rate_limit_responses_are_retried(backend_factory):
    session = FakeSession(posts=[post(1, NOW - 10)])
    session.fail_next = [(429, {"X-RateLimit-Reset": "3"}), (422, {})]
    backend = backend_factory(session)
    ctx, logs = make_ctx("hour")
    items = list(backend.fetch_posts("test", ctx, Coverage("test", "post")))
    assert len(items) == 1
    assert any("HTTP 429" in line for line in logs) and any("HTTP 422" in line for line in logs)


def test_persistent_failure_raises_backend_error(backend_factory):
    session = FakeSession(posts=[post(1, NOW - 10)])
    session.fail_next = [(429, {})] * 10
    backend = backend_factory(session)
    ctx, _ = make_ctx("hour")
    with pytest.raises(BackendError):
        list(backend.fetch_posts("test", ctx, Coverage("test", "post")))


def test_cancellation_stops_the_sweep(backend_factory):
    cancel = threading.Event()
    backend = backend_factory(FakeSession(posts=[post(i, NOW - i) for i in range(20)], page_size=5))
    ctx, _ = make_ctx("hour", cancel=cancel)
    gen = backend.fetch_posts("test", ctx, Coverage("test", "post"))
    next(gen)
    cancel.set()
    with pytest.raises(CancelledError):
        for _ in gen:
            pass


def test_comments_use_window_sweep_for_small_windows(backend_factory):
    comments = [comment(i, NOW - i * 300) for i in range(12)]
    backend = backend_factory(FakeSession(comments=comments, page_size=5))
    ctx, _ = make_ctx("hour", prefilter=["website"])
    coverage = Coverage("test", "comment")
    items = list(backend.fetch_comments("test", ctx, coverage))
    assert len(items) == 12
    assert coverage.strategy == "window" and coverage.complete
    assert items[0].url == "https://www.reddit.com/r/test/comments/p1/_/c0/"


def test_comments_switch_to_prefilter_for_wide_windows(backend_factory):
    # First page covers 5 minutes of a 30-day window => far more pages than the threshold.
    comments = [comment(i, NOW - i * 60, body=("need a website" if i % 4 == 0 else "unrelated")) for i in range(200)]
    session = FakeSession(comments=comments, page_size=5)
    backend = backend_factory(session)
    ctx, logs = make_ctx("month", prefilter=["website", "web developer"])
    coverage = Coverage("test", "comment")
    items = list(backend.fetch_comments("test", ctx, coverage))
    assert coverage.strategy == "prefilter"
    assert any("keyword prefilter" in line for line in logs)
    bodies = {i.text for i in items}
    assert "need a website" in bodies
    assert all(c[1].get("body") for c in session.calls[1:]), "all requests after the probe page carry the body= query"
    assert len({i.fullname for i in items}) == len(items)


def test_comments_without_prefilter_terms_stay_on_window_sweep(backend_factory):
    comments = [comment(i, NOW - i * 60) for i in range(50)]
    backend = backend_factory(FakeSession(comments=comments, page_size=5))
    ctx, logs = make_ctx("month", prefilter=[], max_pages=3)
    coverage = Coverage("test", "comment")
    items = list(backend.fetch_comments("test", ctx, coverage))
    assert coverage.strategy == "window"
    assert not coverage.complete
    assert len(items) == 13  # 5 + 4 + 4 (boundary rows deduplicated)


def test_resolve_post_titles_batches_ids(backend_factory):
    session = FakeSession()
    backend = backend_factory(session)
    ctx, _ = make_ctx("hour")
    from reddit_crawler.backends.arctic_shift import _comment_item
    items = [_comment_item(comment(i, NOW)) for i in range(3)]
    backend.resolve_post_titles(items, ctx)
    assert all(i.title == "Title p1" for i in items)
    assert all(i.num_comments == 7 for i in items)
    assert len([c for c in session.calls if c[0].endswith("/api/posts/ids")]) == 1


def test_build_fts_queries_quotes_phrases_and_batches():
    assert build_fts_queries(["website", "web developer"]) == ['website OR "web developer"']
    batches = build_fts_queries([f"phrase number {i}" for i in range(40)], max_chars=60)
    assert len(batches) > 1
    assert all(len(b) <= 60 for b in batches)
    assert build_fts_queries([]) == []
    assert COMMENT_WINDOW_MAX_PAGES > 0


def test_prefilter_failure_falls_back_to_window_sweep(backend_factory):
    comments = [comment(i, NOW - i * 60, body=("need a website" if i % 10 == 0 else "x")) for i in range(60)]
    session = FakeSession(comments=comments, page_size=5)
    backend = backend_factory(session)
    original_get = session.get

    def failing_get(url, params=None, timeout=None):
        if params and "body" in params:
            return FakeResponse(422, {"data": None, "error": "Timeout"}, {"X-RateLimit-Reset": "1"})
        return original_get(url, params=params, timeout=timeout)

    session.get = failing_get
    ctx, logs = make_ctx("month", prefilter=["website"])
    coverage = Coverage("test", "comment")
    items = list(backend.fetch_comments("test", ctx, coverage))
    assert any("prefilter failed" in line for line in logs)
    assert coverage.strategy == "window"
    assert len({i.fullname for i in items}) == 60  # nothing lost, nothing duplicated


def test_pacing_backs_off_on_throttling_and_relaxes_after_successes(backend_factory):
    session = FakeSession(posts=[post(i, NOW - i) for i in range(3)], page_size=3)
    session.fail_next = [(429, {"X-RateLimit-Reset": "30"})]  # a real rate limit slows the pacing down
    backend = backend_factory(session)
    backend.min_interval = backend.interval = 2.0
    ctx, _ = make_ctx("hour")
    list(backend.fetch_posts("test", ctx, Coverage("test", "post")))
    assert backend.interval == 3.0
    for _ in range(20):
        backend._note_success()
    assert backend.interval == 2.5


def test_timed_out_window_page_is_retried_with_a_wider_after_bound(backend_factory):
    posts = [post(i, NOW - i * 600) for i in range(12)] + [post(99, NOW - 86400 - 30)]  # last one just outside
    session = FakeSession(posts=posts, page_size=5)
    backend = backend_factory(session)
    original_get = session.get
    start = NOW - 86400

    def planner_timeout_get(url, params=None, timeout=None):
        if params and int(params["after"]) == start and int(params["before"]) < NOW - 3000:
            return FakeResponse(422, {"data": None, "error": "Timeout. Maybe slow down a bit"}, {"X-RateLimit-Reset": "50"})
        return original_get(url, params=params, timeout=timeout)

    session.get = planner_timeout_get
    ctx, logs = make_ctx("day")
    coverage = Coverage("test", "post")
    items = list(backend.fetch_posts("test", ctx, coverage))
    assert [i.fullname for i in items] == [f"t3_p{i}" for i in range(12)]  # complete, the outside row filtered
    assert coverage.complete
    assert any("another query shape" in line for line in logs)
    widened = [c[1]["after"] for c in session.calls if int(c[1]["after"]) < start]
    assert widened and widened[0] == start - 3600
    assert backend.interval == backend.min_interval  # shape retries do not slow the pacing down


def test_widening_is_sticky_for_the_rest_of_the_sweep(backend_factory):
    posts = [post(i, NOW - i * 600) for i in range(30)]
    session = FakeSession(posts=posts, page_size=5)
    backend = backend_factory(session)
    original_get = session.get
    start = NOW - 86400

    def planner_timeout_get(url, params=None, timeout=None):
        if params and int(params["after"]) == start and int(params["before"]) < NOW - 3000:
            return FakeResponse(422, {"data": None, "error": "Timeout"}, {})
        return original_get(url, params=params, timeout=timeout)

    session.get = planner_timeout_get
    ctx, _ = make_ctx("day")
    items = list(backend.fetch_posts("test", ctx, Coverage("test", "post")))
    assert len(items) == 30
    statuses = [int(c[1]["after"]) == start for c in session.calls]
    # exactly one 422 round-trip: after the first failure every later page uses the wider bound
    assert statuses.count(True) == 2  # the first (successful) page + the one failing attempt

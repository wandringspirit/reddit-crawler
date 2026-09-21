import threading

import pytest

from reddit_crawler.backends.base import BackendError, FetchContext
from reddit_crawler.backends.reddit_api import RedditApiBackend, build_search_queries, _explain
from reddit_crawler.models import Coverage, Item
from reddit_crawler.storage import Storage
from reddit_crawler.timeframe import build_timeframe

NOW = 1_789_000_000


def item(fullname, kind="post", created=NOW, **kw):
    base = dict(fullname=fullname, kind=kind, subreddit="test", author="a", created_utc=created, title="T",
                text="need a website", url="https://www.reddit.com/r/test/comments/x/", score=1, num_comments=2)
    base.update(kw)
    return Item(**base)


# ------------------------------------------------------------------ storage
def test_storage_round_trip_and_new_flag(tmp_path):
    storage = Storage(tmp_path / "t.db")
    run1 = storage.create_run({"subreddits": ["test"]})
    new = storage.save_matches(run1, [(item("t3_a"), ["need a website"], 1.0, [], [[0, 14]])])
    assert new == 1
    run2 = storage.create_run({"subreddits": ["test"]})
    new = storage.save_matches(run2, [(item("t3_a", score=5), ["need a website"], 1.0, [], [[0, 14]]),
                                      (item("t1_b", kind="comment"), ["website"], 0.5, [], [[7, 14]])])
    assert new == 1
    rows = storage.get_results(run2)
    by_name = {r["fullname"]: r for r in rows}
    assert by_name["t3_a"]["is_new"] is False and by_name["t3_a"]["score"] == 5  # score refreshed
    assert by_name["t1_b"]["is_new"] is True
    assert by_name["t3_a"]["text_spans"] == [[0, 14]]
    assert storage.get_run(run2)["result_count"] == 2
    assert [r["id"] for r in storage.list_runs()] == [run2, run1]

    storage.update_run(run2, status="done", stats={"requests": 3}, warnings=["w"], finished=True)
    run = storage.get_run(run2)
    assert run["status"] == "done" and run["stats"] == {"requests": 3} and run["warnings"] == ["w"] and run["finished_at"]

    storage.delete_run(run1)
    assert storage.get_run(run1) is None
    assert storage.get_results(run2)  # shared item survives while another run references it
    storage.delete_run(run2)
    assert storage.list_runs() == []


def test_storage_truncates_long_text_and_clips_spans(tmp_path):
    storage = Storage(tmp_path / "t.db")
    run = storage.create_run({})
    long_text = "x" * 5000 + " need a website"
    storage.save_matches(run, [(item("t3_long", text=long_text), ["need a website"], 1, [], [[5001, 5015]])])
    row = storage.get_results(run)[0]
    assert row["text"].endswith("…") and len(row["text"]) == 3001
    assert row["text_spans"] == []
    assert storage.get_full_results(run)[0]["text"] == long_text


def test_keyword_set_crud_and_settings(tmp_path):
    storage = Storage(tmp_path / "t.db")
    saved = storage.save_keyword_set("leads", {"name": "Leads", "include_any": ["a"]})
    assert saved["id"] == "leads" and saved["updated_at"]
    assert [s["name"] for s in storage.list_keyword_sets()] == ["Leads"]
    assert storage.get_keyword_set("nope") is None
    assert storage.delete_keyword_set("leads") is True
    assert storage.delete_keyword_set("leads") is False
    storage.set_setting("last_params", {"x": 1})
    assert storage.get_setting("last_params") == {"x": 1}
    assert storage.get_setting("missing", 42) == 42


def test_mark_stale_runs(tmp_path):
    storage = Storage(tmp_path / "t.db")
    run = storage.create_run({})
    storage.update_run(run, status="running")
    storage.mark_stale_runs()
    assert storage.get_run(run)["status"] == "failed"


# ---------------------------------------------------------------- reddit api
class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __str__(self):
        return self.__dict__.get("name", "obj")


class FakeSubreddit:
    def __init__(self, posts, comments, search_results):
        self._posts, self._comments, self._search = posts, comments, search_results
        self.search_calls = []

    def new(self, limit=None):
        return iter(self._posts)

    def comments(self, limit=None):
        return iter(self._comments)

    def search(self, query, *, sort, time_filter, limit):
        self.search_calls.append((query, sort, time_filter))
        return iter(self._search)


class FakeReddit:
    def __init__(self, subreddit):
        self._subreddit = subreddit

    def subreddit(self, name):
        return self._subreddit

    def info(self, fullnames):
        return [Obj(fullname=f, title=f"Title {f}") for f in fullnames]


def submission(i, created):
    return Obj(fullname=f"t3_s{i}", id=f"s{i}", subreddit=Obj(name="test"), author=Obj(name="alice"),
               created_utc=created, title=f"post {i}", selftext="need a website", score=3, num_comments=1,
               link_flair_text=None, is_self=True, over_18=False)


def praw_comment(i, created):
    return Obj(fullname=f"t1_c{i}", id=f"c{i}", subreddit=Obj(name="test"), author=Obj(name="bob"),
               created_utc=created, body="website please", score=1, link_id="t3_s1", link_title="")


def make_ctx(preset="day", prefilter=()):
    logs = []
    ctx = FetchContext(timeframe=build_timeframe({"preset": preset, "tz": "UTC"}, now=NOW),
                       cancel_event=threading.Event(), log=logs.append, on_request=lambda: None,
                       on_progress=lambda _m: None, prefilter_terms=list(prefilter))
    return ctx, logs


def make_backend(fake_reddit):
    backend = RedditApiBackend("id", "secret", "ua")
    backend._reddit = fake_reddit
    return backend


def test_reddit_posts_listing_reaches_start_of_window():
    posts = [submission(i, NOW - i * 3600) for i in range(30)]  # 30 hours of posts, window = 24h
    sub = FakeSubreddit(posts, [], [])
    backend = make_backend(FakeReddit(sub))
    ctx, _ = make_ctx("day", prefilter=["website"])
    coverage = Coverage("test", "post")
    items = list(backend.fetch_posts("test", ctx, coverage))
    assert len(items) == 25 and coverage.complete and coverage.strategy == "listing"
    assert sub.search_calls == []  # no need for the search fallback
    assert items[0].url == "https://www.reddit.com/r/test/comments/s0/"


def test_reddit_posts_fall_back_to_search_when_listing_is_capped(monkeypatch):
    import reddit_crawler.timeframe as timeframe_module
    monkeypatch.setattr(timeframe_module._time, "time", lambda: NOW)  # t= is chosen relative to the real clock
    listing = [submission(i, NOW - i * 60) for i in range(50)]           # only reaches 50 minutes back
    older = [submission(100 + i, NOW - 3600 * (i + 2)) for i in range(5)]  # found via search
    sub = FakeSubreddit(listing, [], listing[:3] + older + [submission(999, NOW - 10 * 86400)])
    backend = make_backend(FakeReddit(sub))
    ctx, _ = make_ctx("day", prefilter=["need a website", "web developer"])
    coverage = Coverage("test", "post")
    items = list(backend.fetch_posts("test", ctx, coverage))
    assert coverage.strategy == "listing+search"
    assert len(items) == 55  # 50 listing + 5 older, the 3 overlapping search hits are deduplicated
    assert sub.search_calls[0][0] == '"need a website" OR "web developer"'
    assert sub.search_calls[0][1:] == ("new", "week")  # 24h window + 5% headroom => the next Reddit window
    assert coverage.complete  # search reached before the window start


def test_reddit_posts_incomplete_without_prefilter_terms():
    listing = [submission(i, NOW - i * 60) for i in range(5)]
    sub = FakeSubreddit(listing, [], [])
    backend = make_backend(FakeReddit(sub))
    ctx, _ = make_ctx("day", prefilter=[])
    coverage = Coverage("test", "post")
    list(backend.fetch_posts("test", ctx, coverage))
    assert not coverage.complete and "unreachable" in coverage.note


def test_reddit_comments_listing_cap_is_reported():
    comments = [praw_comment(i, NOW - i * 60) for i in range(10)]
    backend = make_backend(FakeReddit(FakeSubreddit([], comments, [])))
    ctx, _ = make_ctx("day")
    coverage = Coverage("test", "comment")
    items = list(backend.fetch_comments("test", ctx, coverage))
    assert len(items) == 10 and not coverage.complete and "no comment search" in coverage.note
    backend.resolve_post_titles(items, ctx)
    assert items[0].title == "Title t3_s1"


def test_reddit_backend_requires_credentials_and_explains_errors():
    with pytest.raises(BackendError):
        RedditApiBackend("", "", "ua")
    assert "401" in _explain(Exception("received 401 HTTP response"))
    assert "rate limit" in _explain(Exception("429 Too Many Requests")).lower()
    assert _explain(ValueError("boom")) == "ValueError: boom"


def test_build_search_queries_batches_under_limit():
    queries = build_search_queries([f"phrase {i}" for i in range(100)], max_chars=100)
    assert len(queries) > 5 and all(len(q) <= 100 for q in queries)
    assert build_search_queries([]) == []

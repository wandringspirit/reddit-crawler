"""End-to-end API tests with a fake backend (no network)."""
import csv
import io
import json
import time

import pytest
from fastapi.testclient import TestClient

import reddit_crawler.crawler as crawler_module
from reddit_crawler.app import create_app
from reddit_crawler.backends.base import CancelledError
from reddit_crawler.models import Item
from reddit_crawler.storage import Storage

NOW = int(time.time()) - 600


class FakeBackend:
    name = "fake"
    slow = False

    def __init__(self):
        self.calls = []

    def fetch_posts(self, subreddit, ctx, coverage):
        self.calls.append(("post", subreddit))
        rows = [
            Item("t3_a1", "post", subreddit, "alice", NOW - 10, "Need a website for my bakery", "Budget is $500, any web developer?",
                 "https://www.reddit.com/r/x/comments/a1/", score=4, num_comments=2),
            Item("t3_a2", "post", subreddit, "bob", NOW - 20, "[For Hire] I build websites", "need a website? hire me",
                 "https://www.reddit.com/r/x/comments/a2/"),
            Item("t3_a3", "post", subreddit, "carol", NOW - 30, "Unrelated post", "nothing to see", "https://www.reddit.com/r/x/comments/a3/"),
        ]
        for row in rows:
            if self.slow:
                for _ in range(20):
                    ctx.check_cancelled()
                    time.sleep(0.05)
            coverage.fetched += 1
            yield row

    def fetch_comments(self, subreddit, ctx, coverage):
        self.calls.append(("comment", subreddit))
        coverage.complete = False
        coverage.note = "pretend partial coverage"
        coverage.fetched += 1
        yield Item("t1_c1", "comment", subreddit, "dave", NOW - 5, "", "I need a website too, who can build one?",
                   "https://www.reddit.com/r/x/comments/a1/_/c1/", link_id="t3_a1")

    def resolve_post_titles(self, items, ctx):
        for it in items:
            it.title = "Parent post title"


@pytest.fixture
def client(tmp_path, monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(crawler_module, "make_backend", lambda name: backend)
    monkeypatch.setenv("REDDIT_CRAWLER_ENV", str(tmp_path / ".env"))
    app = create_app(Storage(tmp_path / "t.db"))
    with TestClient(app) as test_client:
        test_client.backend = backend
        yield test_client


def keyword_set():
    return {"name": "Website leads", "include_any": "need a website\nweb developer", "require_any": "website",
            "exclude": "title: [for hire]"}


def run_body(**overrides):
    body = {"subreddits": "r/smallbusiness, webdev", "keyword_set": keyword_set(),
            "timeframe": {"preset": "day", "tz": "Asia/Kolkata"}, "targets": {"posts": True, "comments": True},
            "backend": "arctic"}
    body.update(overrides)
    return body


def wait_for(client, run_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in ("done", "failed", "cancelled"):
            return run
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_status_and_seeded_keyword_set(client):
    status = client.get("/api/status").json()
    assert status["credentials"]["configured"] is False
    assert [b["available"] for b in status["backends"]] == [True, False]
    assert status["current_job"] is None
    sets = client.get("/api/keyword-sets").json()
    assert sets and sets[0]["id"] == "website-leads"
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/../pyproject.toml").status_code in (404, 400)


def test_keyword_set_validation_and_crud(client):
    bad = client.post("/api/keyword-sets", json={"name": "x", "include_any": "/(oops/"})
    assert bad.status_code == 400 and "regex" in bad.json()["error"].lower()
    empty = client.post("/api/keyword-sets", json={"name": "x", "include_any": ""})
    assert empty.status_code == 400
    saved = client.post("/api/keyword-sets", json=keyword_set()).json()
    assert saved["id"] == "website-leads-2" or saved["id"] == "website-leads"
    assert saved["include_any"] == ["need a website", "web developer"]
    assert client.delete(f"/api/keyword-sets/{saved['id']}").json() == {"ok": True}
    assert client.delete("/api/keyword-sets/nope").status_code == 404


def test_keyword_test_endpoint(client):
    response = client.post("/api/keyword-sets/test", json={"keyword_set": keyword_set(), "text": "I need a website"})
    assert response.json()["matched"] is True
    response = client.post("/api/keyword-sets/test", json={"keyword_set": keyword_set(), "title": "[For Hire] dev", "text": "need a website"})
    assert response.json()["matched"] is False and response.json()["excluded_by"] == ["title: [for hire]"]


def test_timeframe_preview_and_errors(client):
    ok = client.post("/api/timeframe/preview", json={"timeframe": {"preset": "custom", "start": "2026-09-01", "end": "2026-09-05", "tz": "Asia/Kolkata"}})
    assert ok.status_code == 200 and ok.json()["start_local"] == "2026-09-01 00:00" and ok.json()["end_local"] == "2026-09-06 00:00"
    bad = client.post("/api/timeframe/preview", json={"timeframe": {"preset": "custom", "start": "2026-09-05", "end": "2026-09-01", "tz": "UTC"}})
    assert bad.status_code == 400


def test_run_end_to_end(client):
    created = client.post("/api/runs", json=run_body())
    assert created.status_code == 201, created.text
    run_id = created.json()["run_id"]
    run = wait_for(client, run_id)
    assert run["status"] == "done", run
    assert run["params"]["subreddits"] == ["smallbusiness", "webdev"]
    assert run["stats"]["matched_posts"] == 2 and run["stats"]["matched_comments"] == 2
    assert run["stats"]["new_items"] == 2  # same fake items for both subreddits => only 2 distinct
    assert any("pretend partial coverage" in w for w in run["warnings"])
    assert client.backend.calls == [("post", "smallbusiness"), ("comment", "smallbusiness"), ("post", "webdev"), ("comment", "webdev")]

    payload = client.get(f"/api/runs/{run_id}/results").json()
    results = payload["results"]
    assert {r["fullname"] for r in results} == {"t3_a1", "t1_c1"}
    post = next(r for r in results if r["kind"] == "post")
    assert post["title_spans"] == [[0, 14]] and post["matched"] == ["need a website", "web developer", "website"]
    assert post["is_new"] is True
    comment = next(r for r in results if r["kind"] == "comment")
    assert comment["title"] == "Parent post title" and comment["text_spans"]
    assert payload["job"]["status"] == "done" and payload["job"]["steps"] == 4

    # second run: nothing is "new" any more
    run2 = client.post("/api/runs", json=run_body()).json()["run_id"]
    wait_for(client, run2)
    assert all(r["is_new"] is False for r in client.get(f"/api/runs/{run2}/results").json()["results"])

    history = client.get("/api/runs").json()
    assert [r["id"] for r in history] == [run2, run_id] and history[0]["result_count"] == 2

    csv_text = client.get(f"/api/runs/{run_id}/export?format=csv").text.lstrip("﻿")
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert len(rows) == 2 and rows[0]["matched_keywords"]
    exported = json.loads(client.get(f"/api/runs/{run_id}/export?format=json").text)
    assert exported["run"]["id"] == run_id and len(exported["results"]) == 2

    assert client.delete(f"/api/runs/{run_id}").json() == {"ok": True}
    assert client.get(f"/api/runs/{run_id}").status_code == 404
    assert client.get("/api/status").json()["last_params"]["subreddits"] == ["smallbusiness", "webdev"]


def test_run_validation_errors(client):
    assert client.post("/api/runs", json=run_body(subreddits="")).status_code == 400
    assert client.post("/api/runs", json=run_body(subreddits="bad name!")).status_code == 400
    assert client.post("/api/runs", json=run_body(targets={"posts": False, "comments": False})).status_code == 400
    assert client.post("/api/runs", json=run_body(backend="reddit")).status_code == 400  # not configured
    assert client.post("/api/runs", json=run_body(keyword_set={"name": "x", "include_any": ""})).status_code == 400


def test_cancel_run(client):
    client.backend.slow = True
    run_id = client.post("/api/runs", json=run_body()).json()["run_id"]
    assert client.get("/api/status").json()["current_job"]["id"] == run_id
    assert client.post("/api/runs", json=run_body()).status_code == 409  # one at a time
    assert client.post(f"/api/runs/{run_id}/cancel").json() == {"ok": True}
    run = wait_for(client, run_id)
    assert run["status"] == "cancelled"
    assert client.post(f"/api/runs/{run_id}/cancel").status_code == 409


def test_credentials_settings(client, tmp_path):
    assert client.get("/api/settings/credentials").json()["configured"] is False
    saved = client.put("/api/settings/credentials", json={"client_id": "abc", "client_secret": "s3cret", "username": "me"}).json()
    assert saved["configured"] is True and saved["has_secret"] is True and "by /u/me" in saved["user_agent"]
    env_text = (tmp_path / ".env").read_text()
    assert "REDDIT_CLIENT_ID" in env_text and "s3cret" in env_text
    # updating without a secret keeps the stored one
    again = client.put("/api/settings/credentials", json={"client_id": "abc2", "client_secret": None, "username": "me"}).json()
    assert again["client_id"] == "abc2" and again["has_secret"] is True
    assert client.get("/api/status").json()["backends"][1]["available"] is True
    assert client.delete("/api/settings/credentials").json()["configured"] is False
    assert client.put("/api/settings/credentials", json={"client_id": "", "client_secret": "x"}).status_code == 400


def test_purge(client):
    run_id = client.post("/api/runs", json=run_body()).json()["run_id"]
    wait_for(client, run_id)
    assert client.post("/api/data/purge").json() == {"ok": True}
    assert client.get("/api/runs").json() == []
    assert client.get("/api/keyword-sets").json()  # keyword sets are kept

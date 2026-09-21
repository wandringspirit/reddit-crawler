"""SQLite persistence: runs, results, saved keyword sets and settings.

One short-lived connection per call keeps this safe to use from the API
thread and the crawl worker thread at the same time (WAL mode).
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import Item

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   INTEGER NOT NULL,
    finished_at  INTEGER,
    status       TEXT NOT NULL DEFAULT 'queued',
    params_json  TEXT NOT NULL,
    stats_json   TEXT,
    warnings_json TEXT,
    error        TEXT
);
CREATE TABLE IF NOT EXISTS items (
    fullname     TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    subreddit    TEXT NOT NULL,
    author       TEXT,
    created_utc  INTEGER NOT NULL,
    title        TEXT,
    text         TEXT,
    url          TEXT,
    score        INTEGER,
    num_comments INTEGER,
    flair        TEXT,
    is_self      INTEGER,
    over_18      INTEGER,
    link_id      TEXT,
    source       TEXT,
    first_seen_run_id INTEGER
);
CREATE TABLE IF NOT EXISTS run_items (
    run_id       INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    fullname     TEXT NOT NULL REFERENCES items(fullname) ON DELETE CASCADE,
    matched_json TEXT NOT NULL,
    relevance    REAL NOT NULL DEFAULT 0,
    title_spans_json TEXT,
    text_spans_json  TEXT,
    PRIMARY KEY (run_id, fullname)
);
CREATE INDEX IF NOT EXISTS idx_run_items_run ON run_items(run_id);
CREATE TABLE IF NOT EXISTS keyword_sets (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    json         TEXT NOT NULL,
    updated_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key          TEXT PRIMARY KEY,
    value_json   TEXT NOT NULL
);
"""


class Storage:
    def __init__(self, path: Path | str):
        self.path = str(path)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------------------------------------------------------------- runs
    def create_run(self, params: dict) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO runs (created_at, status, params_json) VALUES (?, 'queued', ?)",
                (int(time.time()), json.dumps(params)),
            )
            return int(cursor.lastrowid)

    def update_run(self, run_id: int, *, status: str | None = None, stats: dict | None = None,
                   warnings: list[str] | None = None, error: str | None = None,
                   finished: bool = False) -> None:
        sets, values = [], []
        if status is not None:
            sets.append("status = ?"); values.append(status)
        if stats is not None:
            sets.append("stats_json = ?"); values.append(json.dumps(stats))
        if warnings is not None:
            sets.append("warnings_json = ?"); values.append(json.dumps(warnings))
        if error is not None:
            sets.append("error = ?"); values.append(error)
        if finished:
            sets.append("finished_at = ?"); values.append(int(time.time()))
        if not sets:
            return
        values.append(run_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id = ?", values)

    def get_run(self, run_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            count = conn.execute("SELECT COUNT(*) FROM run_items WHERE run_id = ?", (run_id,)).fetchone()[0]
        return _run_row(row, count)

    def list_runs(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT r.*, (SELECT COUNT(*) FROM run_items ri WHERE ri.run_id = r.id) AS n
                   FROM runs r ORDER BY r.id DESC LIMIT ?""", (limit,)).fetchall()
        return [_run_row(row, row["n"]) for row in rows]

    def delete_run(self, run_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM run_items WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
            conn.execute("DELETE FROM items WHERE fullname NOT IN (SELECT fullname FROM run_items)")

    def purge_all(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM run_items")
            conn.execute("DELETE FROM runs")
            conn.execute("DELETE FROM items")

    def mark_stale_runs(self) -> None:
        """Runs left 'running' by a previous process are marked failed on startup."""
        with self._connect() as conn:
            conn.execute("UPDATE runs SET status='failed', error='Interrupted (app closed)', finished_at=? "
                         "WHERE status IN ('queued','running')", (int(time.time()),))

    # --------------------------------------------------------------- items
    def save_matches(self, run_id: int, matches: list[tuple[Item, list[str], float, list, list]]) -> int:
        """Upsert items and record them as results of *run_id*. Returns how many were never seen before."""
        new_count = 0
        with self._connect() as conn:
            for item, matched, relevance, title_spans, text_spans in matches:
                exists = conn.execute("SELECT 1 FROM items WHERE fullname = ?", (item.fullname,)).fetchone()
                if exists:
                    conn.execute(
                        """UPDATE items SET score = COALESCE(?, score), num_comments = COALESCE(?, num_comments),
                           title = CASE WHEN ? != '' THEN ? ELSE title END WHERE fullname = ?""",
                        (item.score, item.num_comments, item.title, item.title, item.fullname))
                else:
                    new_count += 1
                    conn.execute(
                        """INSERT INTO items (fullname, kind, subreddit, author, created_utc, title, text, url, score,
                           num_comments, flair, is_self, over_18, link_id, source, first_seen_run_id)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (item.fullname, item.kind, item.subreddit, item.author, item.created_utc, item.title,
                         item.text, item.url, item.score, item.num_comments, item.flair,
                         None if item.is_self is None else int(item.is_self), int(item.over_18), item.link_id,
                         item.source, run_id))
                conn.execute(
                    """INSERT OR REPLACE INTO run_items (run_id, fullname, matched_json, relevance, title_spans_json, text_spans_json)
                       VALUES (?,?,?,?,?,?)""",
                    (run_id, item.fullname, json.dumps(matched), relevance, json.dumps(title_spans), json.dumps(text_spans)))
        return new_count

    def get_results(self, run_id: int, *, text_limit: int = 3000) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT i.*, ri.matched_json, ri.relevance, ri.title_spans_json, ri.text_spans_json
                   FROM run_items ri JOIN items i ON i.fullname = ri.fullname
                   WHERE ri.run_id = ? ORDER BY i.created_utc DESC""", (run_id,)).fetchall()
        results = []
        for row in rows:
            text = row["text"] or ""
            truncated = len(text) > text_limit
            text_spans = [s for s in json.loads(row["text_spans_json"] or "[]") if s[0] < text_limit]
            results.append({
                "fullname": row["fullname"],
                "kind": row["kind"],
                "subreddit": row["subreddit"],
                "author": row["author"],
                "created_utc": row["created_utc"],
                "title": row["title"] or "",
                "text": text[:text_limit] + ("…" if truncated else ""),
                "url": row["url"],
                "score": row["score"],
                "num_comments": row["num_comments"],
                "flair": row["flair"],
                "source": row["source"],
                "matched": json.loads(row["matched_json"] or "[]"),
                "relevance": row["relevance"],
                "title_spans": json.loads(row["title_spans_json"] or "[]"),
                "text_spans": [[s[0], min(s[1], text_limit)] for s in text_spans],
                "is_new": row["first_seen_run_id"] == run_id,
            })
        return results

    def get_full_results(self, run_id: int) -> list[dict]:
        """Untruncated rows for export."""
        return self.get_results(run_id, text_limit=10**9)

    # -------------------------------------------------------- keyword sets
    def list_keyword_sets(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id, name, json, updated_at FROM keyword_sets ORDER BY name COLLATE NOCASE").fetchall()
        return [dict(json.loads(row["json"]), id=row["id"], name=row["name"], updated_at=row["updated_at"]) for row in rows]

    def get_keyword_set(self, set_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT id, name, json, updated_at FROM keyword_sets WHERE id = ?", (set_id,)).fetchone()
        if row is None:
            return None
        return dict(json.loads(row["json"]), id=row["id"], name=row["name"], updated_at=row["updated_at"])

    def save_keyword_set(self, set_id: str, data: dict) -> dict:
        payload = dict(data)
        payload.pop("updated_at", None)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO keyword_sets (id, name, json, updated_at) VALUES (?,?,?,?)",
                (set_id, payload.get("name") or set_id, json.dumps(payload), int(time.time())))
        return self.get_keyword_set(set_id) or payload

    def delete_keyword_set(self, set_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM keyword_sets WHERE id = ?", (set_id,))
            return cursor.rowcount > 0

    # ------------------------------------------------------------ settings
    def get_setting(self, key: str, default=None):
        with self._connect() as conn:
            row = conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else default

    def set_setting(self, key: str, value) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value_json) VALUES (?, ?)", (key, json.dumps(value)))


def _run_row(row: sqlite3.Row, result_count: int) -> dict:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
        "status": row["status"],
        "params": json.loads(row["params_json"]),
        "stats": json.loads(row["stats_json"]) if row["stats_json"] else None,
        "warnings": json.loads(row["warnings_json"]) if row["warnings_json"] else [],
        "error": row["error"],
        "result_count": result_count,
    }

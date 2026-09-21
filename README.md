# Reddit Crawler

A local control panel that finds Reddit **posts and comments** matching your keywords inside a
chosen **timeframe** – e.g. "people in r/smallbusiness who asked for a website to be built in the
last 24 hours".

* Pick subreddits, a keyword set and a timeframe (past hour / 24 h / 7 d / 30 d / custom date range)
* Every post and comment in the window is downloaded and matched **on your machine**, so matches are
  exact (whole-word, phrase, wildcard, regex) and highlighted
* Results table with filters, sorting, "new since last run" badges, one-click open on Reddit,
  CSV / JSON export and a searchable run history
* Works **without any Reddit account or API key** (see [Data sources](#data-sources))

---

## Quick start (Windows)

Requirements: Python 3.10+ (`python --version`).

```bat
run.bat
```

`run.bat` creates a virtual environment in `.venv` on first use, installs the dependencies, starts the
local server and opens <http://127.0.0.1:8765/> in your browser. Close the console window (or press
`Ctrl+C`) to stop it.

Manual equivalent:

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -e .
.venv\Scripts\python -m reddit_crawler          # add --no-browser or --port 9000 if needed
```

Everything the app stores lives next to it:

| Path               | Contents                                                            |
|--------------------|---------------------------------------------------------------------|
| `data/crawler.db`  | SQLite: runs, results, saved keyword sets, settings                 |
| `.env`             | optional Reddit API credentials (only if you use the official API)  |

---

## Using the panel

### 1. Subreddits
Type names separated by commas or new lines (`smallbusiness, Entrepreneur, r/webdev`). *Find* looks
up subreddit names by prefix and shows subscriber counts so you can add them with one click.

### 2. Keywords
A keyword set has three lists (one entry per line):

| List                  | Meaning                                                                    |
|-----------------------|----------------------------------------------------------------------------|
| **Search phrases**    | the item matches if **any** of these appear                               |
| **Must also contain** | optional extra gate – at least one of these must appear too (e.g. `website`) |
| **Exclude**           | the item is skipped if **any** of these appear (e.g. `[for hire]`)         |

Syntax for every line:

| Line                                | Matches                                                                  |
|-------------------------------------|--------------------------------------------------------------------------|
| `need a website`                    | case-insensitive whole-word phrase; `need a web-site`, `NEED A  WEBSITE` too |
| `recommend*`                        | trailing `*` = any ending (recommend, recommends, recommendation)         |
| `/how much .{0,30} website/`        | a regular expression (Python `re`, case-insensitive)                     |
| `/how much .{0,30} website/ price question` | the same, shown as "price question" in the results               |
| `title: [hiring]`                   | only look in the post title (`text:` = only the body)                    |
| `# note`                            | comment line, ignored                                                    |

Options: *Whole words* (off = substring matching), *Plural tolerant* (the last word may take an
`s`/`es`), *Case sensitive*. *Ignore authors* skips bots such as `AutoModerator`.

Use **Test** to paste a sample post and see whether it matches and why. **Save set** stores the set for
reuse; the app ships with a ready-made set *"People who want a website built"*.

### 3. Timeframe
Presets are relative to now. **Custom date range** takes a start and an end in your local timezone;
a date without a time means the whole day.

### 4. What to search
Posts, comments or both, and the data source (see below). *Advanced → Max pages* caps the number of
API pages per subreddit and type (a page is roughly 100–1000 posts or 100 comments) so a huge custom
range cannot run forever; the panel tells you when a cap was hit.

### Results
* Filter by text, type, subreddit or *New only*; sort by date, relevance, score or comment count.
* **NEW** marks posts/comments never seen in any earlier run – re-run the same search daily and
  look only at what is new.
* *Show full text* expands long posts; every match is highlighted.
* **Export CSV / JSON** downloads the full run. **History** lists past runs; *Load* shows their
  results, *Reuse setup* copies their subreddits/keywords/timeframe back into the form.
* A yellow **Partial coverage** box appears whenever some part of the window could not be fetched
  completely (page cap, API limit, backend error) and says how far back the crawl got.

---

## Data sources

### Arctic Shift (default – no login needed)
[Arctic Shift](https://github.com/ArthurHeitmann/arctic_shift) is a free, community-run archive that
mirrors Reddit within seconds and supports real date ranges for posts **and** comments. The crawler
sweeps every post/comment in your window page by page (`after`/`before` cursors) and matches locally.
For comments on very busy subreddits over wide ranges it first asks the archive for comments that
contain your *must also contain* words (or your search phrases) and confirms each hit locally.

Things to know:
* It is a free service without uptime guarantees and its capacity varies with load. The crawler
  paces itself to roughly 15 requests per minute; when the archive asks us to slow down
  (HTTP 429/422 "Timeout. Maybe slow down a bit") it waits, retries and slows down further. If a
  source keeps failing the run continues with the next one and reports partial coverage.
* Wide windows on busy subreddits take a while: a page is 100–300 comments or 100–1000 posts, so
  a 24-hour sweep of r/smallbusiness is ~15 requests (~1 minute) and a week is several minutes.
  The panel shows progress; you can cancel at any time and keep what was found so far.
* Scores and comment counts of items younger than ~36 hours reflect the moment they were posted.
* Posts later removed by moderators may still appear (they were archived at posting time).

### Official Reddit API (optional)
If you already own Reddit API credentials (client id + secret of a *script* app), enter them under
**Settings** and choose *Official Reddit API* as the data source. Limits of the official API:
* newest ~1000 posts and ~1000 comments per subreddit – the panel falls back to keyword search
  (`sort=new`, `t=…`) for older posts and reports partial coverage when the window is not reached;
* **no comment keyword search** exists, so comments only cover the newest ~1000;
* 100 requests/minute per client id (handled by PRAW).

Since 11 Nov 2025 Reddit only issues new API credentials on request under its
[Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy),
and unauthenticated `reddit.com/….json` access is blocked (HTTP 403) – which is why Arctic Shift is the
default. Existing credentials keep working.

---

## How a search runs

```
for each subreddit × (posts, comments):
    backend sweeps the timeframe newest → oldest (pages of 100–1000 items)
    every item is matched against the keyword set on your machine
    matches are saved to SQLite immediately → they appear in the panel while the run continues
coverage notes per source are collected → "Partial coverage" box
```

Matching order per item: excluded author? → any **exclude** hit? → any **search phrase** hit? →
(if configured) any **must also contain** hit? Relevance = number of distinct search phrases hit
(title hits count 1.5) + 0.5 when a required word hit.

---

## Project layout

```
src/reddit_crawler/
  server.py            console entry point: starts uvicorn, opens the browser
  app.py               FastAPI routes (JSON API + static files)
  crawler.py           one run: sweep sources, match, persist
  jobs.py              background job thread + progress snapshot
  keywords.py          keyword set model and matcher (spans for highlighting)
  timeframe.py         presets / custom ranges → UTC epoch bounds
  storage.py           SQLite (runs, items, run_items, keyword_sets, settings)
  config.py            data dir, .env credentials
  backends/
    arctic_shift.py    Arctic Shift archive backend (default)
    reddit_api.py      official Reddit API backend via PRAW (optional)
  static/              index.html, style.css, app.js – the panel (no build step)
tests/                 pytest suite (no network needed)
run.bat                one-click launcher for Windows
```

### HTTP API (used by the panel, handy for scripting)

| Method & path                          | Purpose                                             |
|----------------------------------------|-----------------------------------------------------|
| `GET  /api/status`                     | version, backends, credentials state, running job   |
| `GET/POST /api/keyword-sets`, `DELETE /api/keyword-sets/{id}` | saved keyword sets           |
| `POST /api/keyword-sets/test`          | match a sample text against a set                   |
| `POST /api/timeframe/preview`          | resolve a timeframe to UTC bounds                   |
| `POST /api/runs`                       | start a run → `{run_id}`                            |
| `GET  /api/runs`, `GET /api/runs/{id}` | history / one run incl. live progress               |
| `POST /api/runs/{id}/cancel`, `DELETE /api/runs/{id}` | cancel / delete                      |
| `GET  /api/runs/{id}/results`          | matched items with highlight spans                  |
| `GET  /api/runs/{id}/export?format=csv\|json` | download                                     |
| `GET  /api/subreddits/search?q=`       | subreddit lookup                                    |
| `GET/PUT/DELETE /api/settings/credentials`, `POST …/test` | Reddit API credentials           |
| `POST /api/data/purge`                 | delete all runs and results                         |

Interactive docs: <http://127.0.0.1:8765/api/docs>.

Example – start a run from the command line:

```bash
curl -X POST http://127.0.0.1:8765/api/runs -H "Content-Type: application/json" -d "{
  \"subreddits\": \"smallbusiness, webdev\",
  \"keyword_set\": {\"name\": \"ad hoc\", \"include_any\": \"need a website\\nhire a web developer\", \"require_any\": \"website\", \"exclude\": \"for hire\"},
  \"timeframe\": {\"preset\": \"custom\", \"start\": \"2026-09-01\", \"end\": \"2026-09-07\", \"tz\": \"Asia/Kolkata\"},
  \"targets\": {\"posts\": true, \"comments\": true}
}"
```

---

## Development

```bash
.venv\Scripts\python -m pip install -e .[dev]
.venv\Scripts\python -m pytest -q
```

The test-suite (56 tests) runs entirely offline: a fake HTTP session stands in for Arctic Shift, fake
PRAW objects for the Reddit API, and a fake backend for the end-to-end API tests.

### Arctic Shift quirks the backend works around
* **Load shedding** – HTTP 422 `Timeout. Maybe slow down a bit` / HTTP 429. The client paces itself
  (3 s idle after every response, slower after throttling) and waits for `X-RateLimit-Reset`.
* **Query-planner timeouts** – some narrow `after`/`before` windows (typically the last few hours of a
  sweep on a busy subreddit) time out *every* time, while the same page with an `after` bound one hour
  earlier returns instantly. The sweep retries such pages with widened bounds (1 h, 6 h, 24 h), filters
  the extra rows client-side and keeps the widened bound for the rest of the sweep.
* **Variable page size** – `limit=auto` returns 100–1000 posts or 100–300 comments depending on server
  load; pagination uses `before = oldest_created_utc + 1` with id de-duplication so nothing is lost.

---

## Responsible use

The tool is read-only and intended for personal research and lead discovery. Reddit's terms ask that
content deleted by its authors is not kept indefinitely – delete old runs from *History* or use
*Settings → Delete all runs & results* from time to time, and always link back to Reddit (the panel
does). Do not use it to spam or mass-message people.

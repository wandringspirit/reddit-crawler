"""Background crawl jobs.

One job runs at a time in a daemon thread.  The UI polls
:meth:`Job.snapshot` (lock protected) for progress; results are written to
SQLite incrementally so partial results are visible while a run is going.
"""
from __future__ import annotations

import threading
import time
import traceback
from collections import deque
from typing import Callable

from .backends.base import CancelledError
from .storage import Storage

TERMINAL = ("done", "failed", "cancelled")


class Job:
    def __init__(self, run_id: int, params: dict):
        self.id = run_id
        self.params = params
        self.status = "queued"
        self.phase = "Queued"
        self.progress_line = ""
        self.requests = 0
        self.fetched = 0
        self.matched = 0
        self.new_items = 0
        self.warnings: list[str] = []
        self.error: str | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.results_version = 0
        self.step = 0
        self.steps = 0
        self.cancel_event = threading.Event()
        self._log: deque[str] = deque(maxlen=200)
        self._lock = threading.Lock()

    # ---- hooks used by the crawler / backends (thread-safe)
    def log(self, message: str) -> None:
        with self._lock:
            self._log.append(f"{time.strftime('%H:%M:%S')}  {message}")

    def count_request(self) -> None:
        with self._lock:
            self.requests += 1

    def set_progress(self, line: str) -> None:
        with self._lock:
            self.progress_line = line

    def set_phase(self, phase: str, step: int | None = None, steps: int | None = None) -> None:
        with self._lock:
            self.phase = phase
            self.progress_line = ""
            if step is not None:
                self.step = step
            if steps is not None:
                self.steps = steps
        self.log(phase)

    def add_counts(self, *, fetched: int = 0, matched: int = 0, new_items: int = 0) -> None:
        with self._lock:
            self.fetched += fetched
            self.matched += matched
            self.new_items += new_items

    def results_changed(self) -> None:
        with self._lock:
            self.results_version += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "status": self.status,
                "phase": self.phase,
                "progress_line": self.progress_line,
                "requests": self.requests,
                "fetched": self.fetched,
                "matched": self.matched,
                "new_items": self.new_items,
                "warnings": list(self.warnings),
                "error": self.error,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed": round((self.finished_at or time.time()) - self.started_at, 1) if self.started_at else 0,
                "results_version": self.results_version,
                "step": self.step,
                "steps": self.steps,
                "log": list(self._log)[-60:],
                "cancel_requested": self.cancel_event.is_set(),
            }


class JobManager:
    def __init__(self, storage: Storage, runner: Callable[[Job, Storage], None]):
        self.storage = storage
        self.runner = runner
        self._jobs: dict[int, Job] = {}
        self._lock = threading.Lock()

    def current(self) -> Job | None:
        with self._lock:
            for job in self._jobs.values():
                if job.status not in TERMINAL:
                    return job
        return None

    def get(self, run_id: int) -> Job | None:
        with self._lock:
            return self._jobs.get(run_id)

    def start(self, params: dict) -> Job:
        with self._lock:
            for job in self._jobs.values():
                if job.status not in TERMINAL:
                    raise RuntimeError(f"Run #{job.id} is still in progress; wait for it or cancel it first.")
            run_id = self.storage.create_run(params)
            job = Job(run_id, params)
            self._jobs[run_id] = job
            # keep the in-memory map small
            for old_id in sorted(self._jobs)[:-20]:
                if self._jobs[old_id].status in TERMINAL:
                    del self._jobs[old_id]
        thread = threading.Thread(target=self._run, args=(job,), name=f"crawl-{run_id}", daemon=True)
        thread.start()
        return job

    def cancel(self, run_id: int) -> bool:
        job = self.get(run_id)
        if job is None or job.status in TERMINAL:
            return False
        job.cancel_event.set()
        job.log("Cancel requested…")
        return True

    def _run(self, job: Job) -> None:
        job.started_at = time.time()
        job.status = "running"
        self.storage.update_run(job.id, status="running")
        try:
            self.runner(job, self.storage)
            job.status = "cancelled" if job.cancel_event.is_set() else "done"
        except CancelledError:
            job.status = "cancelled"
        except Exception as exc:  # noqa: BLE001 - anything else is a failed run, shown in the UI
            job.status = "failed"
            job.error = f"{exc.__class__.__name__}: {exc}"
            job.log(job.error)
            job.log(traceback.format_exc().strip().splitlines()[-1])
        finally:
            job.finished_at = time.time()
            job.log(f"Finished: {job.status} ({job.matched} matches, {job.requests} requests, {round(job.finished_at - job.started_at)}s)")
            self.storage.update_run(job.id, status=job.status, warnings=job.warnings, error=job.error, finished=True)

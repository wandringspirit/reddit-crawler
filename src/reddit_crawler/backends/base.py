"""Common backend interface.

A backend turns (subreddit, timeframe, kind) into a stream of :class:`Item`
objects.  Backends never do keyword matching themselves; they may use keyword
phrases only as a *recall* prefilter (see ``prefilter_terms``), the crawler
always confirms matches client-side.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Iterator, Protocol

from ..models import Coverage, Item
from ..timeframe import Timeframe


class CancelledError(RuntimeError):
    pass


class BackendError(RuntimeError):
    pass


@dataclass
class FetchContext:
    """Per-run hooks a backend uses to report progress and check for cancellation."""

    timeframe: Timeframe
    cancel_event: threading.Event
    log: Callable[[str], None]
    on_request: Callable[[], None]           # called once per HTTP request
    on_progress: Callable[[str], None]       # short status line for the UI
    prefilter_terms: list[str]               # plain phrases usable server-side (may be empty)
    max_pages: int = 100

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise CancelledError("Cancelled")


class Backend(Protocol):
    name: str

    def fetch_posts(self, subreddit: str, ctx: FetchContext, coverage: Coverage) -> Iterator[Item]: ...

    def fetch_comments(self, subreddit: str, ctx: FetchContext, coverage: Coverage) -> Iterator[Item]: ...

    def resolve_post_titles(self, items: list[Item], ctx: FetchContext) -> None:
        """Fill ``title`` on comment items from their parent posts (best effort)."""
        ...

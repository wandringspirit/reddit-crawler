"""Plain data records shared by backends, the crawler and the API."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Item:
    """One Reddit post or comment, normalised across backends."""

    fullname: str               # "t3_abc" for posts, "t1_abc" for comments
    kind: str                   # "post" | "comment"
    subreddit: str
    author: str
    created_utc: int
    title: str                  # post title; for comments the parent post title (may be "")
    text: str                   # selftext / comment body
    url: str                    # absolute reddit.com permalink
    score: int | None = None
    num_comments: int | None = None
    flair: str | None = None
    is_self: bool | None = None
    over_18: bool = False
    link_id: str | None = None  # comments: fullname of the parent post
    source: str = ""            # backend that produced it

    @property
    def id(self) -> str:
        return self.fullname.split("_", 1)[-1]

    def to_dict(self) -> dict:
        return {
            "fullname": self.fullname,
            "kind": self.kind,
            "subreddit": self.subreddit,
            "author": self.author,
            "created_utc": self.created_utc,
            "title": self.title,
            "text": self.text,
            "url": self.url,
            "score": self.score,
            "num_comments": self.num_comments,
            "flair": self.flair,
            "is_self": self.is_self,
            "over_18": self.over_18,
            "link_id": self.link_id,
            "source": self.source,
        }


@dataclass
class Coverage:
    """How completely one (subreddit, kind) source was swept."""

    subreddit: str
    kind: str
    fetched: int = 0
    pages: int = 0
    oldest_utc: int | None = None
    complete: bool = True
    note: str = ""              # human readable explanation when not complete
    strategy: str = "window"    # "window" | "prefilter" | "listing" | "search"

    def to_dict(self) -> dict:
        return {
            "subreddit": self.subreddit,
            "kind": self.kind,
            "fetched": self.fetched,
            "pages": self.pages,
            "oldest_utc": self.oldest_utc,
            "complete": self.complete,
            "note": self.note,
            "strategy": self.strategy,
        }


@dataclass
class RunStats:
    requests: int = 0
    fetched_posts: int = 0
    fetched_comments: int = 0
    matched_posts: int = 0
    matched_comments: int = 0
    new_items: int = 0
    coverage: list[Coverage] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "requests": self.requests,
            "fetched_posts": self.fetched_posts,
            "fetched_comments": self.fetched_comments,
            "matched_posts": self.matched_posts,
            "matched_comments": self.matched_comments,
            "new_items": self.new_items,
            "coverage": [c.to_dict() for c in self.coverage],
        }


def reddit_permalink(subreddit: str, post_id: str, comment_id: str | None = None) -> str:
    base = f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/"
    if comment_id:
        return f"{base}_/{comment_id}/"
    return base

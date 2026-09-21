"""Paths and credential handling.

Everything lives next to the project by default::

    <project>/data/crawler.db   results, run history, saved keyword sets
    <project>/.env              optional Reddit API credentials

Override with ``REDDIT_CRAWLER_DATA`` / ``REDDIT_CRAWLER_ENV`` environment variables.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, set_key, unset_key

from . import __version__

ENV_KEYS = ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT", "REDDIT_USERNAME")
DEFAULT_PORT = 8765


def data_dir() -> Path:
    path = Path(os.environ.get("REDDIT_CRAWLER_DATA") or Path.cwd() / "data")
    path.mkdir(parents=True, exist_ok=True)
    return path


def env_path() -> Path:
    return Path(os.environ.get("REDDIT_CRAWLER_ENV") or Path.cwd() / ".env")


def db_path() -> Path:
    return data_dir() / "crawler.db"


def default_user_agent(username: str | None = None) -> str:
    suffix = f" (by /u/{username})" if username else ""
    return f"windows:reddit-crawler:{__version__}{suffix}"


@dataclass(frozen=True)
class RedditCredentials:
    client_id: str = ""
    client_secret: str = ""
    user_agent: str = ""
    username: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def public_dict(self) -> dict:
        """Safe to send to the UI: the secret is only reported as present/absent."""
        return {
            "configured": self.configured,
            "client_id": self.client_id,
            "has_secret": bool(self.client_secret),
            "user_agent": self.user_agent or default_user_agent(self.username or None),
            "username": self.username,
        }


def load_credentials() -> RedditCredentials:
    values: dict[str, str | None] = {}
    path = env_path()
    if path.exists():
        values.update(dotenv_values(path))
    for key in ENV_KEYS:
        if os.environ.get(key):
            values[key] = os.environ[key]
    return RedditCredentials(
        client_id=(values.get("REDDIT_CLIENT_ID") or "").strip(),
        client_secret=(values.get("REDDIT_CLIENT_SECRET") or "").strip(),
        user_agent=(values.get("REDDIT_USER_AGENT") or "").strip(),
        username=(values.get("REDDIT_USERNAME") or "").strip().lstrip("u/"),
    )


def save_credentials(client_id: str, client_secret: str | None, user_agent: str, username: str) -> RedditCredentials:
    """Write credentials to ``.env``; ``client_secret=None`` keeps the existing secret."""
    path = env_path()
    path.touch(exist_ok=True)
    current = load_credentials()
    secret = current.client_secret if client_secret is None else client_secret.strip()
    values = {
        "REDDIT_CLIENT_ID": client_id.strip(),
        "REDDIT_CLIENT_SECRET": secret,
        "REDDIT_USER_AGENT": user_agent.strip() or default_user_agent(username.strip() or None),
        "REDDIT_USERNAME": username.strip().lstrip("u/"),
    }
    existing = dotenv_values(path)
    for key, value in values.items():
        if value:
            set_key(str(path), key, value, quote_mode="always")
        elif key in existing:
            unset_key(str(path), key)
    return load_credentials()


def clear_credentials() -> None:
    path = env_path()
    if not path.exists():
        return
    existing = dotenv_values(path)
    for key in ENV_KEYS:
        if key in existing:
            unset_key(str(path), key)

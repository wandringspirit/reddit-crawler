"""Timeframe selection: presets ("past 24 hours") and custom local date ranges.

All crawling works on UTC epoch seconds ``[start_utc, end_utc)``; ``end_utc`` is
``None`` for presets so that anything posted right up to "now" is included.
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PRESETS: dict[str, int] = {
    "hour": 3600,
    "6h": 6 * 3600,
    "day": 86400,
    "3d": 3 * 86400,
    "week": 7 * 86400,
    "month": 30 * 86400,
    "year": 365 * 86400,
}

PRESET_LABELS: dict[str, str] = {
    "hour": "Past hour",
    "6h": "Past 6 hours",
    "day": "Past 24 hours",
    "3d": "Past 3 days",
    "week": "Past 7 days",
    "month": "Past 30 days",
    "year": "Past year",
    "custom": "Custom date range",
}

# Reddit's own relative windows, used by the official-API backend's search endpoint.
_REDDIT_T = (("hour", 3600), ("day", 86400), ("week", 7 * 86400), ("month", 31 * 86400), ("year", 366 * 86400))


class TimeframeError(ValueError):
    pass


@dataclass(frozen=True)
class Timeframe:
    preset: str                 # one of PRESETS or "custom"
    start_utc: int
    end_utc: int | None         # exclusive; None = open ended ("now")
    tz_name: str
    label: str

    @property
    def span_seconds(self) -> int:
        end = self.end_utc if self.end_utc is not None else int(_time.time())
        return max(0, end - self.start_utc)

    def reddit_time_filter(self) -> str:
        """Smallest Reddit ``t=`` window that covers this timeframe (with 5% headroom)."""
        span = int(_time.time()) - self.start_utc
        for name, seconds in _REDDIT_T:
            if seconds * 0.95 >= span:
                return name
        return "all"

    def contains(self, created_utc: int | float) -> bool:
        created = int(created_utc)
        if created < self.start_utc:
            return False
        return self.end_utc is None or created < self.end_utc

    def to_dict(self) -> dict:
        return {
            "preset": self.preset,
            "start_utc": self.start_utc,
            "end_utc": self.end_utc,
            "tz": self.tz_name,
            "label": self.label,
        }


def _zone(tz_name: str | None) -> ZoneInfo:
    name = (tz_name or "").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise TimeframeError(f"Unknown timezone {name!r}") from exc


def _parse_local(value: str, tz: ZoneInfo, *, field: str) -> datetime:
    """Parse ``YYYY-MM-DD`` or ``YYYY-MM-DDTHH:MM[:SS]`` as a local time in *tz*."""
    value = (value or "").strip()
    if not value:
        raise TimeframeError(f"Missing {field} date")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise TimeframeError(f"Invalid {field} date {value!r} (use YYYY-MM-DD or YYYY-MM-DDTHH:MM)") from exc
    if parsed.tzinfo is not None:
        return parsed.astimezone(tz)
    return parsed.replace(tzinfo=tz)


def build_timeframe(data: dict, *, now: int | None = None) -> Timeframe:
    """Build a :class:`Timeframe` from the JSON the panel sends.

    ``{"preset": "day"}`` or
    ``{"preset": "custom", "start": "2026-09-01", "end": "2026-09-05", "tz": "Asia/Kolkata"}``.

    A date-only ``end`` is inclusive (the whole day is searched); a date-time
    ``end`` is exclusive, exactly as typed.
    """
    now = int(now if now is not None else _time.time())
    preset = str(data.get("preset") or "day")
    tz_name = str(data.get("tz") or "UTC")
    tz = _zone(tz_name)

    if preset in PRESETS:
        return Timeframe(preset, now - PRESETS[preset], None, tz_name, PRESET_LABELS[preset])
    if preset != "custom":
        raise TimeframeError(f"Unknown timeframe preset {preset!r}")

    start_raw = str(data.get("start") or "")
    end_raw = str(data.get("end") or "")
    start = _parse_local(start_raw, tz, field="start")
    end = _parse_local(end_raw, tz, field="end")
    if "T" not in end_raw.strip() and " " not in end_raw.strip():
        end = end + timedelta(days=1)  # inclusive calendar day
    if end <= start:
        raise TimeframeError("The end of the range must be after the start.")
    start_utc = int(start.timestamp())
    end_utc = int(end.timestamp())
    if start_utc > now:
        raise TimeframeError("The range starts in the future.")
    end_utc = min(end_utc, now + 120)
    label = f"{start.strftime('%b %d, %Y %H:%M')} → {(end - timedelta(seconds=1)).strftime('%b %d, %Y %H:%M')} ({tz_name})"
    return Timeframe("custom", start_utc, end_utc, tz_name, label)


def format_local(epoch: int | float | None, tz_name: str) -> str:
    if epoch is None:
        return ""
    try:
        tz = _zone(tz_name)
    except TimeframeError:
        tz = ZoneInfo("UTC")
    return datetime.fromtimestamp(int(epoch), tz).strftime("%Y-%m-%d %H:%M")

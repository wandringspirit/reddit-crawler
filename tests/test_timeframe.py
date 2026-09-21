from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from reddit_crawler.timeframe import TimeframeError, build_timeframe, format_local

NOW = 1_789_920_000  # 2026-09-20T16:00:00Z


def test_presets_are_relative_to_now_and_open_ended():
    tf = build_timeframe({"preset": "day", "tz": "Asia/Kolkata"}, now=NOW)
    assert tf.start_utc == NOW - 86400
    assert tf.end_utc is None
    assert tf.contains(NOW - 10) and not tf.contains(NOW - 86401)
    assert tf.label == "Past 24 hours"
    assert build_timeframe({"preset": "hour"}, now=NOW).start_utc == NOW - 3600
    assert build_timeframe({"preset": "month"}, now=NOW).start_utc == NOW - 30 * 86400


def test_custom_date_only_range_is_inclusive_of_end_day_in_local_tz():
    tf = build_timeframe({"preset": "custom", "start": "2026-09-01", "end": "2026-09-05", "tz": "Asia/Kolkata"}, now=NOW)
    kolkata = ZoneInfo("Asia/Kolkata")
    assert tf.start_utc == int(datetime(2026, 9, 1, tzinfo=kolkata).timestamp())
    assert tf.end_utc == int(datetime(2026, 9, 6, tzinfo=kolkata).timestamp())  # exclusive next midnight
    assert tf.contains(tf.end_utc - 1) and not tf.contains(tf.end_utc)
    assert "Sep 01, 2026 00:00" in tf.label and "Sep 05, 2026 23:59" in tf.label


def test_custom_datetime_range_is_exclusive_as_typed():
    tf = build_timeframe({"preset": "custom", "start": "2026-09-01T10:00", "end": "2026-09-01T12:30", "tz": "UTC"}, now=NOW)
    assert tf.end_utc - tf.start_utc == 2.5 * 3600
    assert tf.tz_name == "UTC"


def test_custom_range_errors():
    with pytest.raises(TimeframeError):
        build_timeframe({"preset": "custom", "start": "2026-09-05", "end": "2026-09-01", "tz": "UTC"}, now=NOW)
    with pytest.raises(TimeframeError):
        build_timeframe({"preset": "custom", "start": "2030-01-01", "end": "2030-01-02", "tz": "UTC"}, now=NOW)
    with pytest.raises(TimeframeError):
        build_timeframe({"preset": "custom", "start": "not a date", "end": "2026-09-02", "tz": "UTC"}, now=NOW)
    with pytest.raises(TimeframeError):
        build_timeframe({"preset": "custom", "start": "2026-09-01", "end": "2026-09-02", "tz": "Mars/Olympus"}, now=NOW)
    with pytest.raises(TimeframeError):
        build_timeframe({"preset": "fortnight"}, now=NOW)


def test_future_end_is_clamped_to_now():
    tf = build_timeframe({"preset": "custom", "start": "2026-09-19", "end": "2030-01-01", "tz": "UTC"}, now=NOW)
    assert tf.end_utc == NOW + 120


def test_reddit_time_filter_picks_smallest_covering_window(monkeypatch):
    import reddit_crawler.timeframe as module
    monkeypatch.setattr(module._time, "time", lambda: NOW)
    assert build_timeframe({"preset": "hour"}, now=NOW).reddit_time_filter() == "day"  # 5% headroom pushes past 'hour'
    assert build_timeframe({"preset": "custom", "start": "2026-09-19T05:00", "end": "2026-09-20", "tz": "UTC"}, now=NOW).reddit_time_filter() == "week"
    assert build_timeframe({"preset": "year"}, now=NOW).reddit_time_filter() == "all"


def test_format_local():
    assert format_local(NOW, "UTC") == "2026-09-20 16:00"
    assert format_local(NOW, "Asia/Kolkata") == "2026-09-20 21:30"
    assert format_local(None, "UTC") == ""

from datetime import UTC, datetime, timedelta

import pytest

from app.metrics.common import (
    filter_by_range,
    range_to_timedelta,
    record_cost,
    total_tokens,
)
from app.sources.claude_jsonl import Record


def make_record(ts: datetime) -> Record:
    return Record(
        project="test-proj",
        model="claude-3-5-sonnet-20241022",
        timestamp=ts,
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=0,
        cache_write_tokens=0,
        session_id="sess-1",
        source_file="file.jsonl",
        source="claude",
        agent=None,
    )


def test_filter_by_range_7d_keeps_only_last_7_days():
    now = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
    rec_8d_ago = make_record(now - timedelta(days=8))
    rec_6d_ago = make_record(now - timedelta(days=6))
    rec_today = make_record(now)

    records = [rec_8d_ago, rec_6d_ago, rec_today]
    result = filter_by_range(records, "7d", now=now)

    assert len(result) == 2
    assert result == [rec_6d_ago, rec_today]


def test_filter_by_range_30d_keeps_only_last_30_days():
    now = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
    rec_31d_ago = make_record(now - timedelta(days=31))
    rec_29d_ago = make_record(now - timedelta(days=29))
    rec_today = make_record(now)

    records = [rec_31d_ago, rec_29d_ago, rec_today]
    result = filter_by_range(records, "30d", now=now)

    assert len(result) == 2
    assert result == [rec_29d_ago, rec_today]


def test_filter_by_range_all_returns_everything():
    now = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
    rec_100d_ago = make_record(now - timedelta(days=100))
    rec_today = make_record(now)

    records = [rec_100d_ago, rec_today]
    result = filter_by_range(records, "all", now=now)

    assert result == records


def test_filter_by_range_unknown_raises_value_error():
    records = []
    with pytest.raises(ValueError):
        filter_by_range(records, "xyz")


def test_range_to_timedelta_maps_all_supported_keys():
    assert range_to_timedelta("15m") == timedelta(minutes=15)
    assert range_to_timedelta("1h") == timedelta(hours=1)
    assert range_to_timedelta("24h") == timedelta(hours=24)
    assert range_to_timedelta("7d") == timedelta(days=7)
    assert range_to_timedelta("30d") == timedelta(days=30)
    assert range_to_timedelta("all") is None
    with pytest.raises(ValueError):
        range_to_timedelta("xyz")


def test_filter_by_range_honors_sub_day_windows():
    now = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    recent = make_record(now - timedelta(minutes=5))
    stale = make_record(now - timedelta(minutes=45))

    result = filter_by_range([recent, stale], "15m", now=now)

    assert result == [recent]


def _rec(**kw: object) -> Record:
    base = dict(
        project="p",
        model="claude-sonnet-4",
        timestamp=datetime(2026, 7, 21, tzinfo=UTC),
        input_tokens=100,
        output_tokens=10,
        cache_read_tokens=5,
        cache_write_tokens=1,
        session_id="s",
        source_file="f",
        source="claude",
    )
    base.update(kw)
    return Record(**base)  # type: ignore[arg-type]


def test_total_tokens_sums_all_four_counters() -> None:
    assert total_tokens(_rec()) == 116


def test_record_cost_is_positive_for_a_priced_model() -> None:
    assert 0.00044 < record_cost(_rec()) < 0.00046


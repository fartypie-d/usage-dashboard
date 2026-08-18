"""Tests for app.metrics.model_mix — project_mix, daily_cost, mismatches."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from app.sources.claude_jsonl import Record


def _rec(
    *,
    project: str = "proj-a",
    model: str = "claude-opus-4",
    timestamp: datetime | None = None,
    input_tokens: int = 1000,
    output_tokens: int = 200,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    session_id: str = "sess-1",
    source_file: str = "f.jsonl",
) -> Record:
    if timestamp is None:
        timestamp = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)
    return Record(
        project=project,
        model=model,
        timestamp=timestamp,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        session_id=session_id,
        source_file=source_file,
        source="claude",
    )


# ── project_mix ──────────────────────────────────────────────────────────────


def test_project_mix_groups_by_project_and_sums_cost():
    from app.metrics.model_mix import project_mix

    records = [
        _rec(project="alpha", model="claude-opus-4", input_tokens=1_000_000, output_tokens=100_000),
        _rec(project="alpha", model="claude-haiku-4", input_tokens=500_000, output_tokens=50_000),
        _rec(project="beta", model="claude-opus-4", input_tokens=2_000_000, output_tokens=200_000),
    ]
    result = project_mix(records)

    assert len(result) == 2
    # Sorted by cost_usd desc — beta has more tokens at opus rate
    assert result[0]["project"] == "beta"
    assert result[1]["project"] == "alpha"

    # alpha total tokens = (1M+100K) + (500K+50K) = 1_650_000
    alpha = result[1]
    assert alpha["total_tokens"] == 1_650_000
    assert alpha["cost_usd"] > 0

    beta = result[0]
    assert beta["total_tokens"] == 2_200_000


def test_project_mix_by_model_sorted_by_tokens_desc():
    from app.metrics.model_mix import project_mix

    records = [
        _rec(project="p", model="claude-opus-4", input_tokens=100_000, output_tokens=50_000),
        _rec(project="p", model="claude-haiku-4", input_tokens=500_000, output_tokens=200_000),
        _rec(project="p", model="claude-opus-4", input_tokens=200_000, output_tokens=100_000),
    ]
    result = project_mix(records)

    assert len(result) == 1
    by_model = result[0]["by_model"]
    assert len(by_model) == 2
    # haiku has 700K tokens, opus has 450K → haiku first
    assert by_model[0]["model"] == "claude-haiku-4"
    assert by_model[0]["tokens"] == 700_000
    assert by_model[1]["model"] == "claude-opus-4"
    assert by_model[1]["tokens"] == 450_000


def test_project_mix_empty_records():
    from app.metrics.model_mix import project_mix

    assert project_mix([]) == []


def test_project_mix_uses_cost_for_not_hardcoded_rates():
    """Verify cost_for is called per record (not hardcoded pricing)."""
    from app.metrics.model_mix import project_mix

    records = [
        _rec(project="p", model="claude-opus-4", input_tokens=1_000_000, output_tokens=0),
    ]

    with patch("app.metrics.common.cost_for") as mock_cost:
        mock_cost.return_value = (0.99, [])
        result = project_mix(records)

    assert mock_cost.call_count == 1
    assert result[0]["cost_usd"] == pytest.approx(0.99)


# ── daily_cost ───────────────────────────────────────────────────────────────


def test_daily_cost_groups_by_utc_date():
    """Records on different UTC dates must be split into separate entries."""
    from app.metrics.model_mix import daily_cost

    records = [
        _rec(timestamp=datetime(2026, 6, 25, 23, 59, 0, tzinfo=UTC), input_tokens=1_000_000),
        _rec(timestamp=datetime(2026, 6, 26, 0, 1, 0, tzinfo=UTC), input_tokens=500_000),
        _rec(timestamp=datetime(2026, 6, 25, 10, 0, 0, tzinfo=UTC), input_tokens=200_000),
    ]
    result = daily_cost(records)

    assert len(result) == 2
    # Ascending date order
    assert result[0]["date"] == "2026-06-25"
    assert result[1]["date"] == "2026-06-26"
    # Each date has positive cost
    assert result[0]["cost_usd"] > 0
    assert result[1]["cost_usd"] > 0


def test_daily_cost_returns_only_days_with_data():
    from app.metrics.model_mix import daily_cost

    records = [
        _rec(timestamp=datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)),
        _rec(timestamp=datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)),
    ]
    result = daily_cost(records)

    # Only 2 days with data — no gap-filling for 26, 27
    assert len(result) == 2
    dates = [r["date"] for r in result]
    assert "2026-06-26" not in dates
    assert "2026-06-27" not in dates


def test_daily_cost_empty():
    from app.metrics.model_mix import daily_cost

    assert daily_cost([]) == []


def test_daily_cost_fill_gaps_inserts_zero_for_missing_days():
    """fill_gaps=True fills missing days between min and max with cost_usd 0.0."""
    from app.metrics.model_mix import daily_cost

    records = [
        _rec(timestamp=datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)),
        _rec(timestamp=datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)),
    ]
    result = daily_cost(records, fill_gaps=True)

    assert len(result) == 3
    assert result[0]["date"] == "2026-07-01"
    assert result[1]["date"] == "2026-07-02"
    assert result[1]["cost_usd"] == 0.0
    assert result[2]["date"] == "2026-07-03"
    assert result[0]["cost_usd"] > 0
    assert result[2]["cost_usd"] > 0


def test_daily_cost_fill_gaps_false_preserves_current_behavior():
    """Default (fill_gaps unset) does not fill gaps — only days with data."""
    from app.metrics.model_mix import daily_cost

    records = [
        _rec(timestamp=datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)),
        _rec(timestamp=datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)),
    ]
    result = daily_cost(records)

    assert len(result) == 2
    dates = [r["date"] for r in result]
    assert dates == ["2026-07-01", "2026-07-03"]
    assert "2026-07-02" not in dates


def test_daily_cost_fill_gaps_single_day():
    """Single day of data with fill_gaps=True returns only that day."""
    from app.metrics.model_mix import daily_cost

    records = [
        _rec(timestamp=datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)),
    ]
    result = daily_cost(records, fill_gaps=True)

    assert len(result) == 1
    assert result[0]["date"] == "2026-07-01"
    assert result[0]["cost_usd"] > 0


def test_daily_cost_fill_gaps_empty_returns_empty():
    """Empty input returns [] regardless of fill_gaps."""
    from app.metrics.model_mix import daily_cost

    assert daily_cost([], fill_gaps=True) == []
    assert daily_cost([], fill_gaps=False) == []


# ── mismatches ───────────────────────────────────────────────────────────────


def test_mismatches_flags_expensive_model_on_low_complexity_session():
    """An expensive-model session with low output tokens and multiple turns is a mismatch."""
    from app.metrics.model_mix import mismatches

    # 3 turns, avg output 50 tokens → clearly simple work on opus
    records = [
        _rec(
            project="proj-alpha",
            model="claude-opus-4",
            session_id="sess-mismatch",
            output_tokens=50,
            input_tokens=10_000,
            timestamp=datetime(2026, 6, 25, 10, 0, 0, tzinfo=UTC),
        ),
        _rec(
            project="proj-alpha",
            model="claude-opus-4",
            session_id="sess-mismatch",
            output_tokens=50,
            input_tokens=10_000,
            timestamp=datetime(2026, 6, 25, 10, 1, 0, tzinfo=UTC),
        ),
        _rec(
            project="proj-alpha",
            model="claude-opus-4",
            session_id="sess-mismatch",
            output_tokens=50,
            input_tokens=10_000,
            timestamp=datetime(2026, 6, 25, 10, 2, 0, tzinfo=UTC),
        ),
    ]
    result = mismatches(records)

    assert len(result) >= 1
    m = result[0]
    assert m["session_id"] == "sess-mismatch"
    assert m["model"] == "claude-opus-4"
    assert m["severity"] in ("high", "med", "low")
    assert m["avg_output_tokens"] == 50
    assert m["turns"] == 3
    assert m["suggested_model"] == "claude-haiku-4"
    assert m["estimated_savings_usd"] > 0
    assert m["cost_usd"] > 0
    assert "reason" in m and len(m["reason"]) > 0


def test_mismatches_respects_top_n_limit():
    from app.metrics.model_mix import mismatches

    records = []
    for i in range(5):
        for t in range(3):
            records.append(
                _rec(
                    model="claude-opus-4",
                    session_id=f"sess-{i}",
                    output_tokens=30,
                    input_tokens=5_000,
                    timestamp=datetime(2026, 6, 25, 10, t, 0, tzinfo=UTC),
                )
            )
    result = mismatches(records, top_n=2)
    assert len(result) == 2


def test_mismatches_empty_when_no_expensive_models():
    """Haiku-only sessions should produce no mismatches."""
    from app.metrics.model_mix import mismatches

    records = [
        _rec(
            model="claude-haiku-4",
            session_id="sess-cheap",
            output_tokens=30,
            input_tokens=5_000,
            timestamp=datetime(2026, 6, 25, 10, t, 0, tzinfo=UTC),
        )
        for t in range(5)
    ]
    result = mismatches(records)
    assert result == []


def test_mismatches_severity_thresholds():
    """Savings >= 5 → high, 1 <= savings < 5 → med, < 1 → low."""
    from app.metrics.model_mix import mismatches

    # Create a high-savings session: lots of input tokens on opus
    high_records = [
        _rec(
            model="claude-opus-4",
            session_id="sess-high",
            output_tokens=20,
            input_tokens=500_000,
            timestamp=datetime(2026, 6, 25, 10, t, 0, tzinfo=UTC),
        )
        for t in range(3)
    ]
    result = mismatches(high_records)
    assert len(result) == 1
    assert result[0]["severity"] == "high"


def test_mismatches_empty_for_empty_input():
    from app.metrics.model_mix import mismatches

    assert mismatches([]) == []

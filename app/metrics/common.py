from datetime import UTC, datetime, timedelta

from app.pricing import cost_for
from app.sources.claude_jsonl import Record

__all__ = ["filter_by_range", "range_to_timedelta", "record_cost", "total_tokens"]

# 대시보드가 지원하는 기간 프리셋. 15m/1h는 "지금 무슨 일이 벌어지는가"용,
# 나머지는 누적 통계용.
RANGE_WINDOWS: dict[str, timedelta] = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


def range_to_timedelta(range_key: str) -> timedelta | None:
    """Return the lookback window for *range_key*, or None for "all"."""
    if range_key == "all":
        return None
    window = RANGE_WINDOWS.get(range_key)
    if window is None:
        raise ValueError(f"Unknown range_key: {range_key!r}")
    return window


def filter_by_range(
    records: list[Record], range_key: str, *, now: datetime | None = None
) -> list[Record]:
    """Return records whose timestamp falls within the *range_key* window."""
    window = range_to_timedelta(range_key)
    if window is None:
        return list(records)

    current_time = now if now is not None else datetime.now(UTC)
    cutoff = current_time - window
    return [r for r in records if r.timestamp >= cutoff]


def total_tokens(rec: Record) -> int:
    """Return the sum of all four token counters on *rec*."""
    return (
        rec.input_tokens
        + rec.output_tokens
        + rec.cache_read_tokens
        + rec.cache_write_tokens
    )


def record_cost(rec: Record) -> float:
    """Return the USD cost of a single record under the active pricing table.

    ``cost_for``의 경고는 버린다. ``compute_kpi``가 이미 같은 레코드셋에서 미등록
    모델 경고를 모아 응답에 넣으므로(``app/main.py``가 ``kpi_warnings``를
    ``all_warnings``에 합친다), 여기서 또 모으면 중복만 는다.
    """
    cost, _ = cost_for(
        rec.model,
        rec.input_tokens,
        rec.output_tokens,
        rec.cache_read_tokens,
        rec.cache_write_tokens,
    )
    return cost


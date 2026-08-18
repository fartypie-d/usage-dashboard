"""Tests for session detail metadata in app/metrics/work_sessions.py."""

from __future__ import annotations

from datetime import UTC, datetime

from app.metrics.work_sessions import session_summary
from app.sources.claude_jsonl import Record


def _record(
    *,
    session_id: str,
    project: str = "proj-a",
    source: str = "claude",
    hour: int = 9,
    agent: str | None = None,
    parent_session_id: str | None = None,
    model: str = "claude-opus-4-8",
    cwd: str | None = None,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read_tokens: int = 20,
    cache_write_tokens: int = 10,
) -> Record:
    return Record(
        project=project,
        model=model,
        timestamp=datetime(2026, 7, 22, hour, 0, 0, tzinfo=UTC),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        session_id=session_id,
        source_file=f"{project}/{session_id}.jsonl",
        source=source,
        agent=agent,
        cwd=cwd,
        parent_session_id=parent_session_id,
    )


def test_session_summary_aggregates_time_cost_and_models() -> None:
    recs = [
        _record(session_id="s1", hour=9),
        _record(session_id="s1", hour=11, model="claude-haiku-4-5"),
    ]
    s = session_summary(recs, title="제목")
    assert s["id"] == "s1"
    assert s["source"] == "claude"
    assert s["title"] == "제목"
    assert s["phase"] is None
    assert s["phase_slug"] is None
    assert s["started_at"] == int(recs[0].timestamp.timestamp() * 1000)
    assert s["ended_at"] == int(recs[1].timestamp.timestamp() * 1000)
    assert s["models"] == ["claude-haiku-4-5", "claude-opus-4-8"]
    assert s["is_subagent"] is False
    assert isinstance(s["cost_usd"], float)


def test_session_summary_card_has_exactly_eleven_keys() -> None:
    s = session_summary([_record(session_id="k1")], title=None)
    assert set(s.keys()) == {
        "id", "source", "title", "phase", "phase_slug",
        "started_at", "ended_at", "cost_usd", "models", "agent", "is_subagent",
    }


def test_session_summary_flags_subagents_from_records_or_parent_id() -> None:
    by_record = session_summary(
        [_record(session_id="s2", parent_session_id="parent")], title=None
    )
    assert by_record["is_subagent"] is True
    by_parent = session_summary(
        [_record(session_id="s3", source="opencode")], title=None, parent_id="p"
    )
    assert by_parent["is_subagent"] is True


def test_session_summary_surfaces_the_delegated_agent() -> None:
    recs = [
        _record(session_id="a1", hour=9),
        _record(session_id="a1", hour=10, agent="code-reviewer"),
    ]
    assert session_summary(recs, title=None)["agent"] == "code-reviewer"


def test_session_summary_agent_is_none_without_delegated_records() -> None:
    assert session_summary([_record(session_id="a2")], title=None)["agent"] is None


def test_session_summary_rounds_cost_usd_to_four_decimals() -> None:
    """cost_usd는 round(..., 4) — 소수 5자리 이상 raw 합이 4자리로 반올림된다."""
    from app.metrics.common import record_cost

    # claude-opus-4: in=15/out=75/cr=1.5/cw=18.75 per 1M
    # 100+50+20+10 → raw 0.0054675 → rounded 0.0055
    rec = _record(session_id="cost1")
    raw = record_cost(rec)
    assert raw != round(raw, 4)  # 반올림이 실제로 갈라지는 케이스
    s = session_summary([rec], title=None)
    assert s["cost_usd"] == round(raw, 4)
    assert s["cost_usd"] == 0.0055

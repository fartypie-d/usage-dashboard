"""orchestrate_events 파서 테스트 — 정상/깨진 줄/부재/워크트리 병합."""
import json
from pathlib import Path

from app.sources.orchestrate_events import project_events, read_events


def _write_events(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows),
        encoding="utf-8",
    )


def test_read_events_parses_rows_and_ts_ms(tmp_path):
    p = tmp_path / ".orchestrate/events.jsonl"
    _write_events(p, [
        {"ts": "2026-08-08T16:02:30+09:00", "phase": 14, "event": "phase_claimed"},
    ])
    events, broken, error = read_events(p)
    assert broken == 0
    assert error is None
    assert events[0]["event"] == "phase_claimed"
    assert events[0]["phase"] == 14
    assert isinstance(events[0]["ts_ms"], int)


def test_read_events_counts_broken_lines_and_keeps_good_ones(tmp_path):
    p = tmp_path / "events.jsonl"
    _write_events(p, [
        "{bad json",
        {"ts": "nonsense-ts", "phase": 1, "event": "phase_closed"},
        json.dumps(["not", "a", "dict"]),
        {"phase": 2},  # event 키 없음 → 깨진 줄
    ])
    events, broken, error = read_events(p)
    assert broken == 3
    assert error is None
    assert len(events) == 1
    assert events[0]["ts_ms"] is None  # ts 파싱 실패는 줄을 버리지 않는다


def test_read_events_missing_file_is_normal(tmp_path):
    events, broken, error = read_events(tmp_path / "absent.jsonl")
    assert (events, broken, error) == ([], 0, None)


def test_project_events_merges_worktree_dedupes_and_sorts(tmp_path):
    root = tmp_path
    shared = {"ts": "2026-08-10T10:00:00+09:00", "phase": 18, "event": "phase_claimed"}
    _write_events(root / ".orchestrate/events.jsonl", [
        {"ts": "2026-08-10T12:00:00+09:00", "phase": 17, "event": "phase_closed"},
        shared,
    ])
    _write_events(
        root / ".claude/worktrees/phase18-x/.orchestrate/events.jsonl",
        [shared, {"ts": "2026-08-10T10:05:00+09:00", "phase": 18, "event": "gate_answered",
                  "gate": "gate1", "answer": "approve"}],
    )
    events, warnings = project_events(
        root, [{"phase": 18, "worktree": ".claude/worktrees/phase18-x"}]
    )
    assert [e["event"] for e in events] == ["phase_claimed", "gate_answered", "phase_closed"]
    assert warnings == []


def test_project_events_reports_broken_lines_as_warning(tmp_path):
    _write_events(tmp_path / ".orchestrate/events.jsonl", ["{bad"])
    events, warnings = project_events(tmp_path, [])
    assert events == []
    assert len(warnings) == 1
    assert "1개" in warnings[0]


def test_read_events_reports_decode_failure_and_project_warning(tmp_path):
    path = tmp_path / ".orchestrate/events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe")

    events, broken, error = read_events(path)
    assert (events, broken) == ([], 0)
    assert isinstance(error, str)

    events, warnings = project_events(tmp_path, [])
    assert events == []
    assert len(warnings) == 1
    assert "읽기 실패" in warnings[0]


def test_read_events_keeps_event_without_ts(tmp_path):
    path = tmp_path / "events.jsonl"
    _write_events(path, [{"event": "phase_closed"}])

    events, broken, error = read_events(path)

    assert (broken, error) == (0, None)
    assert events == [{"event": "phase_closed", "ts_ms": None}]


def test_project_events_warns_for_active_entry_without_worktree(tmp_path):
    _write_events(
        tmp_path / ".orchestrate/events.jsonl",
        [{"ts": "2026-08-10T10:00:00+09:00", "event": "phase_closed"}],
    )

    events, warnings = project_events(tmp_path, [{"phase": 18}])

    assert [event["event"] for event in events] == ["phase_closed"]
    assert warnings == ["active phase 18: worktree 경로 없음 — 이벤트 병합 제외"]

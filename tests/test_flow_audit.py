"""flow_audit 단계 판정 테스트 — 실측/추정/skipped/active/전무 케이스."""
from app.metrics.flow_audit import (
    ACTIVE,
    INFERRED,
    MEASURED,
    MISSING,
    SKIPPED,
    STAGES,
    attach_sessions,
    audit,
    phase_tasks,
)


def _row(phase, **over):
    base = {
        "phase": phase, "slug": f"s{phase}", "date": None, "kind": None,
        "domain": None, "status": None, "cost": None, "cost_raw": None,
        "compactions": None, "interventions": None, "summary": None,
        "commits": [], "doc_path": f"docs/PHASE{phase}_s{phase}.md",
        "review_path": None, "tasks": [], "active": False,
    }
    base.update(over)
    return base


def _ev(phase, event, ts_ms=0, **extra):
    return {"phase": phase, "event": event, "ts_ms": ts_ms, **extra}


def _states(row):
    return {c["id"]: c["state"] for c in row["stages"]}


def test_measured_full_flow():
    events = [
        _ev(14, "phase_claimed", 1),
        _ev(14, "gate_answered", 2, gate="gate1", answer="approve"),
        _ev(14, "delegation_started", 3, task="1", agent="dash-backend"),
        _ev(14, "delegation_done", 4, task="1", agent="dash-backend", exit=0),
        _ev(14, "review_verdict", 5, task="1", verdict="pass", red=0, orange=0),
        _ev(14, "gate_answered", 6, gate="gate2", answer="approve"),
        _ev(14, "phase_closed", 7),
    ]
    rows, warnings = audit([_row(14)], events, [], [])
    assert warnings == []
    st = _states(rows[0])
    for stage in ("gate1", "claim", "delegate", "review", "gate2", "close"):
        assert st[stage] == MEASURED, stage
    assert rows[0]["flow_status"] == "closed"


def test_no_events_all_inferred_from_docs_and_git():
    commits = [
        {"hash": "aaa1111", "ts_ms": 1, "subject": "docs: Phase 9 설계 — 어쩌고"},
        {"hash": "bbb2222", "ts_ms": 2, "subject": "docs: Phase 9 구현 플랜"},
        {"hash": "ccc3333", "ts_ms": 9, "subject": "merge: Phase 9 위임 타임라인"},
    ]
    row = _row(9, review_path="docs/reviews/PHASE9_x.md",
               tasks=[{"n": 1, "label": "1", "title": "t", "verdict": "pass",
                       "verdict_raw": "✅", "commit": "abc", "commits": ["abc"]}])
    rows, warnings = audit([row], [], commits, [])
    assert warnings == []
    st = _states(rows[0])
    assert all(st[s] == INFERRED for s in STAGES), st
    assert rows[0]["flow_status"] == "closed"


def test_skipped_vs_missing():
    # 종료는 실측인데 gate1 증거가 전혀 없다 → gate1은 missing이 아니라 skipped.
    events = [_ev(7, "phase_claimed", 1), _ev(7, "phase_closed", 2)]
    rows, warnings = audit([_row(7, doc_path=None)], events, [], [])
    assert warnings == []
    st = _states(rows[0])
    assert st["gate1"] == SKIPPED
    assert st["design"] == SKIPPED
    assert st["close"] == MEASURED


def test_active_phase_marks_next_stage():
    events = [
        _ev(18, "phase_claimed", 1),
        _ev(18, "gate_answered", 2, gate="gate1", answer="approve"),
        _ev(18, "delegation_started", 3, task="1", agent="a"),
    ]
    rows, warnings = audit([_row(18, active=True)], events, [], [])
    assert warnings == []
    st = _states(rows[0])
    assert st["claim"] == MEASURED
    assert st["delegate"] == MEASURED
    assert st["review"] == ACTIVE
    assert st["gate2"] == MISSING
    assert rows[0]["flow_status"] == "active"


def test_orphan_when_no_close_evidence():
    rows, warnings = audit([_row(12, doc_path="docs/PHASE12_x.md")], [], [], [])
    assert warnings == []
    assert rows[0]["flow_status"] == "orphan"
    assert _states(rows[0])["close"] == MISSING


def test_delegation_hint_infers_delegate_stage():
    hints = [{"phase": 5, "slug": "phase5-x", "source": "opencode",
              "session_id": "ses1", "agent": "worker", "start_ms": 10, "end_ms": 20}]
    rows, warnings = audit([_row(5)], [], [], hints)
    assert warnings == []
    assert _states(rows[0])["delegate"] == INFERRED


def test_stage_cells_carry_evidence_text():
    events = [_ev(14, "phase_claimed", 1)]
    rows, warnings = audit([_row(14)], events, [], [])
    assert warnings == []
    claim = next(c for c in rows[0]["stages"] if c["id"] == "claim")
    assert claim["evidence"]
    assert claim["ts"] == 1


def test_gate_reject_without_approval_is_not_measured():
    rows, warnings = audit(
        [_row(3)], [_ev(3, "gate_answered", 1, gate="gate1", answer="reject")], [], []
    )
    gate1 = next(cell for cell in rows[0]["stages"] if cell["id"] == "gate1")
    assert gate1["state"] != MEASURED
    assert "reject" in gate1["evidence"]
    assert warnings == []


def test_gate_reject_then_later_stage_is_skipped_with_reject_evidence():
    events = [
        _ev(3, "gate_answered", 1, gate="gate1", answer="reject"),
        _ev(3, "phase_claimed", 2),
    ]
    rows, warnings = audit([_row(3)], events, [], [])
    gate1 = next(cell for cell in rows[0]["stages"] if cell["id"] == "gate1")
    assert gate1["state"] == SKIPPED
    assert "reject" in gate1["evidence"]
    assert warnings == []


def test_gate_approval_wins_after_reject():
    events = [
        _ev(3, "gate_answered", 1, gate="gate1", answer="reject"),
        _ev(3, "gate_answered", 2, gate="gate1", answer="approve"),
    ]
    rows, warnings = audit([_row(3)], events, [], [])
    gate1 = next(cell for cell in rows[0]["stages"] if cell["id"] == "gate1")
    assert gate1["state"] == MEASURED
    assert gate1["evidence"] == "gate1 approve"
    assert warnings == []


def test_string_phase_is_coerced_without_warning():
    rows, warnings = audit([_row(20)], [_ev("20", "phase_claimed", 1)], [], [])
    assert _states(rows[0])["claim"] == MEASURED
    assert warnings == []


def test_non_integer_phase_is_dropped_with_one_warning():
    events = [_ev([], "phase_claimed", 1), _ev([], "phase_closed", 2)]
    rows, warnings = audit([_row(20)], events, [], [])
    assert _states(rows[0])["claim"] != MEASURED
    assert warnings == ["phase 값이 정수가 아닌 이벤트 2건 무시"]


def test_delegation_hint_precedes_verdict_fallback_and_keeps_timestamp():
    row = _row(5, tasks=[{"verdict": "pass"}])
    hints = [{"phase": 5, "start_ms": 10}]
    rows, warnings = audit([row], [], [], hints)
    delegate = next(cell for cell in rows[0]["stages"] if cell["id"] == "delegate")
    assert delegate["evidence"] == "위임 세션 1건 (cwd 귀속)"
    assert delegate["ts"] == 10
    assert warnings == []


def test_gate1_uses_commits_fallback_without_merge_commit():
    rows, warnings = audit([_row(6, commits=["abc1234"])], [], [], [])
    assert _states(rows[0])["gate1"] == INFERRED
    assert warnings == []


def test_phase_commit_matching_does_not_cross_single_and_double_digits():
    commits = [
        {"hash": "one1111", "ts_ms": 1, "subject": "docs: Phase 1 설계"},
        {"hash": "eighteen", "ts_ms": 18, "subject": "docs: Phase 18 설계"},
    ]
    rows, warnings = audit([_row(1), _row(18)], [], commits, [])
    design_evidence = [
        next(cell for cell in row["stages"] if cell["id"] == "design")["evidence"]
        for row in rows
    ]
    assert "one1111" in design_evidence[0]
    assert "eighteen" not in design_evidence[0]
    assert "eighteen" in design_evidence[1]
    assert "one1111" not in design_evidence[1]
    assert warnings == []


def test_phase_tasks_groups_fix_retries_into_attempts():
    events = [
        _ev(184, "delegation_started", 10, task="5b", agent="api-router"),
        _ev(184, "delegation_done", 20, task="5b", agent="api-router",
            model="m1", exit=0),
        _ev(184, "review_verdict", 21, task="5b", verdict="reject", red=1, orange=3,
            reviewers=["python-reviewer"]),
        _ev(184, "delegation_started", 30, task="5b-fix", agent="api-router"),
        _ev(184, "delegation_done", 40, task="5b-fix", agent="api-router",
            model="m2", exit=0),
        _ev(184, "review_verdict", 41, task="5b", verdict="pass", red=0, orange=0),
        _ev(184, "task_committed", 50, task="5b", commit="bf618d5"),
    ]
    tasks, warnings = phase_tasks(events)
    assert warnings == []
    tasks = tasks[184]
    assert len(tasks) == 1
    task = tasks[0]
    assert task["task"] == "5b"
    assert task["commit"] == "bf618d5"
    assert [attempt["task"] for attempt in task["attempts"]] == ["5b", "5b-fix"]
    assert task["attempts"][0]["verdict"] == "reject"
    assert task["attempts"][1]["verdict"] == "pass"
    assert task["attempts"][1]["model"] == "m2"


def test_phase_tasks_done_without_started_still_recorded_with_warning():
    events = [_ev(3, "delegation_done", 5, task="1", agent="w", model="m", exit=1)]
    tasks, warnings = phase_tasks(events)
    task = tasks[3][0]
    assert task["attempts"][0]["exit"] == 1
    assert task["attempts"][0]["started_ms"] is None
    assert warnings == ["짝 없는 위임 이벤트 1건 — attempt를 합성/재부착"]


def test_attach_sessions_matches_by_time_window():
    events = [
        _ev(18, "delegation_started", 100_000, task="1", agent="w"),
        _ev(18, "delegation_done", 400_000, task="1", agent="w", exit=0),
    ]
    tasks, warnings = phase_tasks(events)
    assert warnings == []
    hints = [
        {"phase": 18, "slug": "x", "source": "opencode", "session_id": "near",
         "agent": "w", "start_ms": 130_000, "end_ms": 390_000},
        {"phase": 18, "slug": "x", "source": "claude", "session_id": "wrong-source",
         "agent": "w", "start_ms": 130_000, "end_ms": 390_000},
    ]
    attach_sessions(tasks, hints)
    assert tasks[18][0]["session"] == {"source": "opencode", "id": "near"}


def test_audit_rows_include_phase_tasks():
    events = [
        _ev(14, "delegation_started", 3, task="1", agent="dash-backend"),
        _ev(14, "delegation_done", 4, task="1", agent="dash-backend", exit=0),
    ]
    rows, warnings = audit([_row(14)], events, [], [])
    assert rows[0]["tasks"][0]["task"] == "1"
    assert rows[0]["tasks"][0]["agent"] == "dash-backend"
    assert warnings == []


def test_bool_phase_is_dropped_with_one_warning():
    rows, warnings = audit([_row(1)], [_ev(True, "phase_claimed", 1)], [], [])
    assert _states(rows[0])["claim"] == MISSING
    assert warnings == ["phase 값이 정수가 아닌 이벤트 1건 무시"]


def test_verdict_before_started_fills_one_attempt():
    events = [
        _ev(18, "review_verdict", 10, task="5b", verdict="reject"),
        _ev(18, "delegation_started", 20, task="5b", agent="worker"),
    ]
    tasks, warnings = phase_tasks(events)
    attempts = tasks[18][0]["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["verdict"] == "reject"
    assert attempts[0]["started_ms"] == 20
    assert warnings == ["짝 없는 위임 이벤트 1건 — attempt를 합성/재부착"]


def test_fix_verdict_before_started_fills_its_own_attempt():
    events = [
        _ev(18, "review_verdict", 10, task="5b-fix", verdict="reject"),
        _ev(18, "delegation_started", 20, task="5b-fix", agent="worker"),
        _ev(18, "delegation_done", 30, task="5b-fix", agent="worker", exit=0),
    ]
    tasks, _ = phase_tasks(events)
    attempts = tasks[18][0]["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["task"] == "5b-fix"
    assert attempts[0]["verdict"] == "reject"
    assert attempts[0]["started_ms"] == 20
    assert attempts[0]["done_ms"] == 30


def test_task_integer_is_coerced_and_other_non_string_task_is_warned_and_dropped():
    events = [
        _ev(18, "delegation_started", 10, task=5, agent="worker"),
        _ev(18, "delegation_started", 11, task=[], agent="worker"),
    ]
    tasks, warnings = phase_tasks(events)
    assert tasks[18][0]["task"] == "5"
    assert warnings == ["task 필드가 문자열이 아닌 이벤트 1건 무시"]


def test_string_phase_populates_stage_and_tasks_consistently():
    events = [_ev("18", "delegation_started", 10, task="1", agent="worker")]
    rows, warnings = audit([_row(18)], events, [], [])
    assert _states(rows[0])["delegate"] == MEASURED
    assert rows[0]["tasks"][0]["task"] == "1"
    assert warnings == []


def test_attach_sessions_ignores_hints_without_integer_start_ms():
    tasks, _ = phase_tasks([_ev(18, "delegation_started", 100_000, task="1")])
    hints = [
        {"phase": 18, "source": "opencode", "session_id": "missing"},
        {"phase": 18, "source": "opencode", "session_id": "none", "start_ms": None},
        {"phase": 18, "source": "opencode", "session_id": "valid", "start_ms": 110_000},
    ]
    attach_sessions(tasks, hints)
    assert tasks[18][0]["attempts"][0]["session"] == {"source": "opencode", "id": "valid"}


def test_attach_sessions_consumes_nearest_hint_once_when_task_windows_overlap():
    tasks, _ = phase_tasks([
        _ev(18, "delegation_started", 100_000, task="1"),
        _ev(18, "delegation_started", 110_000, task="2"),
    ])
    hints = [{"phase": 18, "source": "opencode", "session_id": "only", "start_ms": 108_000}]
    attach_sessions(tasks, hints)
    sessions = [task["attempts"][0]["session"] for task in tasks[18]]
    assert sessions == [None, {"source": "opencode", "id": "only"}]


def test_attach_sessions_assigns_distinct_sessions_to_each_retry_attempt():
    tasks, _ = phase_tasks([
        _ev(18, "delegation_started", 100_000, task="5b"),
        _ev(18, "delegation_done", 110_000, task="5b", exit=0),
        _ev(18, "delegation_started", 300_000, task="5b-fix"),
        _ev(18, "delegation_done", 310_000, task="5b-fix", exit=0),
    ])
    hints = [
        {"phase": 18, "source": "opencode", "session_id": "first", "start_ms": 105_000},
        {"phase": 18, "source": "opencode", "session_id": "retry", "start_ms": 305_000},
    ]
    attach_sessions(tasks, hints)
    task = tasks[18][0]
    assert [attempt["session"]["id"] for attempt in task["attempts"]] == ["first", "retry"]
    assert task["session"] == {"source": "opencode", "id": "first"}


def test_reviewers_are_copied_from_input_event():
    reviewers = ["reviewer"]
    tasks, _ = phase_tasks([_ev(18, "review_verdict", 10, task="1", reviewers=reviewers)])
    tasks[18][0]["attempts"][0]["reviewers"].append("later")
    assert reviewers == ["reviewer"]


def test_done_timestamp_zero_is_not_treated_as_open_ended():
    tasks, _ = phase_tasks([
        _ev(18, "delegation_started", -200_000, task="1"),
        _ev(18, "delegation_done", 0, task="1"),
    ])
    hints = [{"phase": 18, "source": "opencode", "session_id": "too-late", "start_ms": 200_000}]
    attach_sessions(tasks, hints)
    assert tasks[18][0]["attempts"][0]["session"] is None


def test_phase_tasks_groups_multiple_fix_retries():
    events = [
        _ev(18, "delegation_started", 1, task="5b"),
        _ev(18, "delegation_started", 2, task="5b-fix"),
        _ev(18, "delegation_started", 3, task="5b-fix2"),
    ]
    tasks, warnings = phase_tasks(events)
    assert [attempt["task"] for attempt in tasks[18][0]["attempts"]] == ["5b", "5b-fix", "5b-fix2"]
    assert warnings == []

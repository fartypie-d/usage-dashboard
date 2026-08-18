"""오케스트레이션 흐름 감사 — 페이즈별 8단계 도달 판정 (순수 함수).

파일·git·레지스트리 접근 없음 — 호출자(main.py)가 증거를 모아 넘긴다.
증거 우선순위는 events.jsonl 실측(measured) > 문서·git 추정(inferred)이고,
미도달(missing)과 건너뜀(skipped)을 구분한다 — 뒤 단계가 도달했는데 앞 단계
증거가 없으면 skipped다 ("GATE 1 없이 코드가 바뀐 페이즈"를 잡는 감사 신호).
"""

from __future__ import annotations

import re

STAGES = ("design", "brief", "gate1", "claim", "delegate", "review", "gate2", "close")

MEASURED = "measured"
INFERRED = "inferred"
ACTIVE = "active"
MISSING = "missing"
SKIPPED = "skipped"

_CLOSE_STATUSES = {"done", "merged", "closed", "complete", "완료"}
_STATE_RANK = {MEASURED: 2, INFERRED: 1}

_EVENT_STAGE = {
    "phase_claimed": ("claim", "phase_claimed 이벤트"),
    "delegation_started": ("delegate", "위임 이벤트"),
    "delegation_done": ("delegate", "위임 이벤트"),
    "review_verdict": ("review", "review_verdict 이벤트"),
    "phase_closed": ("close", "phase_closed 이벤트"),
}

__all__ = [
    "ACTIVE", "INFERRED", "MEASURED", "MISSING", "SKIPPED", "STAGES",
    "attach_sessions", "audit", "phase_tasks",
]


def _cells() -> list[dict]:
    return [{"id": s, "state": None, "evidence": None, "ts": None} for s in STAGES]


def _idx(stage: str) -> int:
    return STAGES.index(stage)


def _coerce_phase(value: object) -> int | None:
    """이벤트 phase 값을 정수로 정규화한다."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _set(cell: dict, state: str, evidence: str, ts: int | None) -> None:
    """measured는 inferred로 덮이지 않는다. 같은 등급은 첫 증거가 이긴다."""
    if _STATE_RANK.get(cell["state"], 0) >= _STATE_RANK[state]:
        return
    cell.update(state=state, evidence=evidence, ts=ts)


def _apply_events(cells: list[dict], events: list[dict]) -> dict[str, str]:
    rejected_gates: dict[str, str] = {}
    for event in events:
        name = event.get("event")
        if name == "gate_answered" and event.get("gate") in ("gate1", "gate2"):
            stage, label = event["gate"], f"{event['gate']} {event.get('answer') or '?'}"
            if event.get("answer") == "reject":
                rejected_gates[stage] = f"{stage} reject 후 승인 없음"
            if event.get("answer") != "approve":
                continue
        elif name in _EVENT_STAGE:
            stage, label = _EVENT_STAGE[name]
        else:
            continue
        _set(cells[_idx(stage)], MEASURED, label, event.get("ts_ms"))
    return rejected_gates


def _apply_fallbacks(
    cells: list[dict], row: dict, commits: list[dict], hints: list[dict]
) -> None:
    number = row["phase"]
    pattern = re.compile(rf"[Pp]hase\s*{number}\b")
    mine = [commit for commit in commits if pattern.search(commit["subject"])]

    def find(needles: tuple[str, ...] = (), prefix: str | None = None) -> dict | None:
        for commit in mine:
            if prefix is not None and not commit["subject"].startswith(prefix):
                continue
            if all(needle in commit["subject"] for needle in needles):
                return commit
        return None

    def commit_ev(commit: dict) -> str:
        return f"커밋 {commit['hash']}: {commit['subject']}"

    if row.get("active"):
        _set(cells[_idx("claim")], MEASURED, "레지스트리 active 등록", None)

    design = find(("설계",))
    if design:
        _set(cells[_idx("design")], INFERRED, commit_ev(design), design["ts_ms"])
    plan = find(("플랜",)) or find(("지시서",))
    if plan:
        _set(cells[_idx("brief")], INFERRED, commit_ev(plan), plan["ts_ms"])
    if row.get("doc_path"):
        _set(cells[_idx("brief")], INFERRED, "지시서 문서 있음", None)

    merge = find(prefix="merge:")
    if (row.get("commits") or []) or merge:
        _set(cells[_idx("gate1")], INFERRED, "구현 커밋 존재 — 통과 추정", None)
    if merge:
        _set(cells[_idx("claim")], INFERRED, commit_ev(merge), merge["ts_ms"])
        _set(cells[_idx("close")], INFERRED, commit_ev(merge), merge["ts_ms"])
    status = str(row.get("status") or "").lower()
    if status in _CLOSE_STATUSES:
        _set(cells[_idx("close")], INFERRED, f"문서 status={status}", None)

    phase_hints = [hint for hint in hints if _coerce_phase(hint.get("phase")) == number]
    if phase_hints:
        starts = [
            hint["start_ms"] for hint in phase_hints
            if isinstance(hint.get("start_ms"), int)
            and not isinstance(hint["start_ms"], bool)
        ]
        _set(
            cells[_idx("delegate")], INFERRED,
            f"위임 세션 {len(phase_hints)}건 (cwd 귀속)",
            min(starts) if starts else None,
        )
    verdicts = [task for task in (row.get("tasks") or []) if task.get("verdict")]
    if verdicts:
        _set(cells[_idx("delegate")], INFERRED, f"검수 표 판정 {len(verdicts)}건", None)
    if row.get("review_path"):
        _set(cells[_idx("review")], INFERRED, "리뷰 문서 있음", None)
    if row.get("review_path") and cells[_idx("close")]["state"]:
        _set(cells[_idx("gate2")], INFERRED, "리뷰 문서 + 종료 증거 — 통과 추정", None)


def _resolve(cells: list[dict], is_active: bool, rejected_gates: dict[str, str]) -> None:
    reached = [index for index, cell in enumerate(cells) if cell["state"] in (MEASURED, INFERRED)]
    last = reached[-1] if reached else -1
    for index, cell in enumerate(cells):
        if cell["state"]:
            continue
        if index < last:
            cell.update(state=SKIPPED, evidence="뒤 단계는 도달했는데 이 단계 증거가 없습니다")
        else:
            cell["state"] = MISSING
    if is_active and 0 <= last + 1 < len(cells) and cells[last + 1]["state"] == MISSING:
        cells[last + 1].update(state=ACTIVE, evidence="진행 중 — 다음 단계")
    for gate, evidence in rejected_gates.items():
        cell = cells[_idx(gate)]
        if cell["state"] in (MISSING, SKIPPED):
            cell["evidence"] = evidence


_FIX_RE = re.compile(r"-fix\d*$")


def _new_attempt(group: dict, task_id: str, agent: object = None) -> dict:
    attempt = {
        "task": task_id, "agent": agent or group["agent"], "model": None,
        "exit": None, "verdict": None, "red": None, "orange": None,
        "reviewers": [], "started_ms": None, "done_ms": None, "session": None,
    }
    group["attempts"].append(attempt)
    return attempt


def _last_attempt(group: dict) -> dict:
    return group["attempts"][-1] if group["attempts"] else _new_attempt(group, group["task"])


def phase_tasks(events: list[dict]) -> tuple[dict[int, list[dict]], list[str]]:
    """이벤트는 ts_ms 오름차순 전제 (project_events가 보장).

    task 이벤트를 base id로 묶는다 — `5b-fix`는 `5b`의 재시도(attempt)다.
    """
    out: dict[int, list[dict]] = {}
    index: dict[tuple[int, str], dict] = {}
    invalid_phase_count = 0
    invalid_task_count = 0
    unmatched_count = 0
    for event in events:
        number = _coerce_phase(event.get("phase"))
        if number is None:
            invalid_phase_count += 1
            continue
        name = event.get("event")
        if name not in {
            "delegation_started", "delegation_done", "review_verdict", "task_committed",
        }:
            continue
        task_id = event.get("task")
        if isinstance(task_id, bool):
            invalid_task_count += 1
            continue
        if isinstance(task_id, int):
            task_id = str(task_id)
        elif not isinstance(task_id, str):
            invalid_task_count += 1
            continue
        base = _FIX_RE.sub("", task_id)
        group = index.get((number, base))
        if group is None:
            group = {
                "task": base, "agent": None, "attempts": [],
                "commit": None, "session": None,
            }
            index[(number, base)] = group
            out.setdefault(number, []).append(group)

        if name == "delegation_started":
            attempt = next(
                (attempt for attempt in reversed(group["attempts"])
                 if attempt["task"] == task_id and attempt["started_ms"] is None),
                None,
            )
            if attempt is None:
                attempt = _new_attempt(group, task_id, event.get("agent"))
                attempt["started_ms"] = event.get("ts_ms")
            else:
                attempt["started_ms"] = event.get("ts_ms")
                attempt["agent"] = attempt["agent"] or event.get("agent")
            group["agent"] = group["agent"] or event.get("agent")
        elif name == "delegation_done":
            attempt = next(
                (attempt for attempt in reversed(group["attempts"])
                 if attempt["task"] == task_id and attempt["done_ms"] is None),
                None,
            )
            if attempt is None:
                attempt = _new_attempt(group, task_id, event.get("agent"))
                unmatched_count += 1
            attempt.update(model=event.get("model"), exit=event.get("exit"),
                           done_ms=event.get("ts_ms"))
            attempt["agent"] = attempt["agent"] or event.get("agent")
            group["agent"] = group["agent"] or event.get("agent")
        elif name == "review_verdict":
            if not group["attempts"]:
                attempt = _new_attempt(group, task_id, event.get("agent"))
                unmatched_count += 1
            else:
                attempt = next(
                    (attempt for attempt in reversed(group["attempts"])
                     if attempt["verdict"] is None),
                    None,
                )
            if attempt is None:
                attempt = _last_attempt(group)
                unmatched_count += 1
            attempt.update(verdict=event.get("verdict"), red=event.get("red"),
                           orange=event.get("orange"),
                           reviewers=list(event.get("reviewers") or []))
        elif name == "task_committed":
            group["commit"] = event.get("commit")
    warnings = []
    if invalid_phase_count:
        warnings.append(f"phase 값이 정수가 아닌 이벤트 {invalid_phase_count}건 무시")
    if invalid_task_count:
        warnings.append(f"task 필드가 문자열이 아닌 이벤트 {invalid_task_count}건 무시")
    if unmatched_count:
        warnings.append(f"짝 없는 위임 이벤트 {unmatched_count}건 — attempt를 합성/재부착")
    return out, warnings


_SESSION_SLACK_MS = 120_000
_OPEN_ENDED_MS = 4 * 3600_000


def attach_sessions(tasks_by_phase: dict[int, list[dict]], hints: list[dict]) -> None:
    """tasks_by_phase를 제자리에서 수정해 attempt 시간창의 opencode 세션을 연결한다."""
    consumed: set[tuple[object, object]] = set()
    for number, tasks in tasks_by_phase.items():
        candidates = [
            hint for hint in hints
            if _coerce_phase(hint.get("phase")) == number
            and hint.get("source") == "opencode"
            and isinstance(hint.get("start_ms"), int)
            and not isinstance(hint["start_ms"], bool)
        ]
        matches: list[tuple[int, dict, dict, dict]] = []
        for task in tasks:
            for attempt in task["attempts"]:
                if attempt["started_ms"] is None:
                    continue
                lo = attempt["started_ms"] - _SESSION_SLACK_MS
                hi = (attempt["done_ms"] if attempt["done_ms"] is not None
                      else attempt["started_ms"] + _OPEN_ENDED_MS)
                hi += _SESSION_SLACK_MS
                matches.extend(
                    (abs(hint["start_ms"] - attempt["started_ms"]), task, attempt, hint)
                    for hint in candidates if lo <= hint["start_ms"] <= hi
                )
        assigned_attempts: set[int] = set()
        for _, task, attempt, hit in sorted(matches, key=lambda match: match[0]):
            key = (hit.get("source"), hit.get("session_id"))
            if key in consumed or id(attempt) in assigned_attempts:
                continue
            session = {"source": hit["source"], "id": hit["session_id"]}
            attempt["session"] = session
            consumed.add(key)
            assigned_attempts.add(id(attempt))
            if task["session"] is None:
                task["session"] = session


def audit(
    phases: list[dict],
    events: list[dict],
    git_commits: list[dict],
    delegation_hints: list[dict],
) -> tuple[list[dict], list[str]]:
    """이벤트는 ts_ms 오름차순 전제 (project_events가 보장).

    phase 행 + 증거 → 파이프라인 행. 입력 순서를 유지하고 입력을 바꾸지 않는다.
    """
    events_by_phase: dict[int, list[dict]] = {}
    for event in events:
        number = _coerce_phase(event.get("phase"))
        if number is None:
            continue
        events_by_phase.setdefault(number, []).append(event)

    tasks_by_phase, warnings = phase_tasks(events)
    attach_sessions(tasks_by_phase, delegation_hints)

    rows: list[dict] = []
    for row in phases:
        cells = _cells()
        rejected_gates = _apply_events(cells, events_by_phase.get(row["phase"], []))
        _apply_fallbacks(cells, row, git_commits, delegation_hints)
        is_active = bool(row.get("active"))
        _resolve(cells, is_active, rejected_gates)
        close_state = cells[_idx("close")]["state"]
        flow_status = (
            "active" if is_active
            else "closed" if close_state in (MEASURED, INFERRED)
            else "orphan"
        )
        rows.append({
            "phase": row["phase"],
            "slug": row.get("slug"),
            "date": row.get("date"),
            "active": is_active,
            "flow_status": flow_status,
            "stages": cells,
            "tasks": tasks_by_phase.get(row["phase"], []),
        })
    return rows, warnings

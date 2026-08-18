"""`.orchestrate/events.jsonl` 파서 — 오케스트레이션 흐름의 실측 이벤트를 읽는다.

파일이 없는 것은 정상이다(이벤트를 아직 안 쌓는 프로젝트). 반면
깨진 줄은 조용히 버리지 않고 개수를 세어 호출자가 경고로 노출하게 한다.
진행 중 페이즈의 이벤트는 워크트리 안에만 있으므로(phase_claimed note 실측)
``project_events``가 루트 로그와 active 워크트리 로그를 병합한다.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.sources.claude_jsonl import _parse_iso_timestamp

EVENTS_RELPATH = ".orchestrate/events.jsonl"

__all__ = ["EVENTS_RELPATH", "project_events", "read_events"]


def _ts_ms(raw: object) -> int | None:
    if not isinstance(raw, str):
        return None
    try:
        return int(_parse_iso_timestamp(raw).timestamp() * 1000)
    except ValueError:
        return None


def read_events(path: Path) -> tuple[list[dict], int, str | None]:
    """한 events.jsonl → (이벤트 목록, 깨진 줄 수, 읽기 실패 사유)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], 0, None
    except (OSError, UnicodeError) as exc:
        return [], 0, str(exc)

    events: list[dict] = []
    broken = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            broken += 1
            continue
        if not isinstance(row, dict) or not isinstance(row.get("event"), str):
            broken += 1
            continue
        row = dict(row)
        row["ts_ms"] = _ts_ms(row.get("ts"))
        events.append(row)
    return events, broken, None


def project_events(root: Path, active: list[dict]) -> tuple[list[dict], list[str]]:
    """루트 + active 워크트리의 events.jsonl 병합 — 중복 제거, 시간순."""
    paths = [root / EVENTS_RELPATH]
    warnings: list[str] = []
    for entry in active:
        worktree = entry.get("worktree")
        if isinstance(worktree, str) and worktree:
            paths.append(root / worktree / EVENTS_RELPATH)
        else:
            warnings.append(
                f"active phase {entry.get('phase')}: worktree 경로 없음 — 이벤트 병합 제외"
            )

    merged: list[dict] = []
    seen: set[str] = set()
    for path in paths:
        events, broken, error = read_events(path)
        if error is not None:
            warnings.append(f"events.jsonl 읽기 실패: {path}: {error}")
        if broken:
            warnings.append(f"events.jsonl 깨진 줄 {broken}개 건너뜀: {path}")
        for event in events:
            key = json.dumps(event, sort_keys=True, ensure_ascii=False)
            if key in seen:
                continue
            seen.add(key)
            merged.append(event)

    merged.sort(key=lambda e: (e["ts_ms"] is None, e["ts_ms"] or 0))
    return merged, warnings

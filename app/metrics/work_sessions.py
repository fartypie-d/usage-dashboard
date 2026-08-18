"""세션 상세 카드 메타를 계산하는 순수 함수.

순수 함수 — 파일·DB 접근 없음. 제목은 호출자(main.py)가 소스별 인덱스로 넘긴다.
워크트리 cwd는 ``project_phase_from_cwd``로 원 프로젝트에 귀속시키고 phase를 뽑는다
(기존 사용량 지표의 ``Record.project`` 소비처는 건드리지 않는다 — 이 계층 전용).
"""

from __future__ import annotations

from app.metrics.common import record_cost
from app.sources.claude_jsonl import Record
from app.sources.transcript_common import project_phase_from_cwd


def session_summary(
    records: list[Record], *, title: str | None, parent_id: str | None = None
) -> dict:
    """한 세션의 Record들 → 목록 카드 메타 11키. ``records``는 비어 있으면 안 된다."""
    first = records[0]
    _, phase, phase_slug = project_phase_from_cwd(first.cwd, first.project)
    started = min(r.timestamp for r in records)
    ended = max(r.timestamp for r in records)
    agents = [r.agent for r in records if r.agent]
    is_subagent = (
        any(r.parent_session_id for r in records) or parent_id is not None
    )
    return {
        "id": first.session_id,
        "source": first.source,
        "title": title,
        "phase": phase,
        "phase_slug": phase_slug,
        "started_at": int(started.timestamp() * 1000),
        "ended_at": int(ended.timestamp() * 1000),
        "cost_usd": round(sum(record_cost(r) for r in records), 4),
        "models": sorted({r.model for r in records}),
        "agent": agents[0] if agents else None,
        "is_subagent": is_subagent,
    }

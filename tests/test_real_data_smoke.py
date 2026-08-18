"""Smoke checks against the operator's real data directories.

These skip cleanly on machines without the real sources. Their purpose is to
catch the failure mode that shipped this bug: fixtures encoding a schema that
does not exist in production data.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.sources.claude_jsonl import parse_directory
from app.sources.opencode_db import read_records

CLAUDE_ROOT = Path(os.path.expanduser("~/.claude/projects"))
OPENCODE_DB = Path(os.path.expanduser("~/.local/share/opencode/opencode.db"))

real_data = pytest.mark.skipif(
    not CLAUDE_ROOT.is_dir() or not OPENCODE_DB.exists(),
    reason="real Claude/opencode data not present on this machine",
)


@real_data
def test_real_claude_data_yields_records() -> None:
    records, _ = parse_directory(CLAUDE_ROOT)
    assert len(records) > 0, "Claude JSONL이 한 건도 파싱되지 않았다"


@real_data
def test_real_claude_data_has_both_direct_and_delegated() -> None:
    records, _ = parse_directory(CLAUDE_ROOT)
    assert any(r.agent is None for r in records), "직접 세션이 없다"
    assert any(r.agent is not None for r in records), "위임 세션이 없다"


@real_data
def test_real_subagent_names_mostly_resolved() -> None:
    from app.sources.claude_subagents import FALLBACK_AGENT

    records, _ = parse_directory(CLAUDE_ROOT)
    delegated = [r for r in records if r.agent is not None]
    assert delegated, "위임 레코드가 없다"
    resolved = [r for r in delegated if r.agent != FALLBACK_AGENT]
    ratio = len(resolved) / len(delegated)
    assert ratio > 0.5, f"서브에이전트 이름 복원율이 낮다: {ratio:.0%}"


@real_data
def test_real_sources_both_present() -> None:
    claude_records, _ = parse_directory(CLAUDE_ROOT)
    opencode_records, _ = read_records(OPENCODE_DB)
    sources = {r.source for r in claude_records + opencode_records}
    assert sources == {"claude", "opencode"}


@real_data
@pytest.mark.pricing_gap
def test_real_data_has_no_unknown_model_warnings() -> None:
    """미등록 모델은 비용 0으로 계산되므로 경고가 0이어야 한다.

    이 테스트는 코드 결함이 아니라 **데이터 간극**을 알린다 — 통과하려면
    사용자가 config/pricing.json에 실단가를 넣어야 한다. 기본 실행에서
    제외하고 ``pytest -m pricing_gap``으로 필요할 때 돌린다.
    """
    from app.pricing import cost_for

    records, _ = parse_directory(CLAUDE_ROOT)
    unknown = set()
    for rec in records:
        _, warnings = cost_for(rec.model, rec.input_tokens, rec.output_tokens)
        if warnings:
            unknown.add(rec.model)
    assert not unknown, f"단가 미등록 모델: {sorted(unknown)}"

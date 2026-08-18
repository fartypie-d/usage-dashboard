"""인지부채 신호 — quiz 기록 발견·등급·불변성."""

from __future__ import annotations

from pathlib import Path

from app.metrics.cognitive_debt import attach_debt, debt_for, quiz_details, quiz_records

QUIZ_OK = """---
phase: 3
date: 2026-08-17
model: claude-sonnet-5
gaps_found: 2
gaps_unresolved: 0
cost: $0.12
---
# Phase 3 쪽지시험

### Q1
왜 대안 X가 아닌가?
"""

QUIZ_GAPPY = QUIZ_OK.replace("gaps_unresolved: 0", "gaps_unresolved: 1").replace(
    "date: 2026-08-17", "date: 2026-08-16"
)

QUIZ_BROKEN = QUIZ_OK.replace("gaps_unresolved: 0", "gaps_unresolved: 몰라")


def _write(root: Path, name: str, text: str) -> None:
    (root / "quizzes").mkdir(exist_ok=True)
    (root / "quizzes" / name).write_text(text, encoding="utf-8")


def test_quiz_records_groups_by_phase_latest_first(tmp_path: Path) -> None:
    _write(tmp_path, "PHASE3_20260816.md", QUIZ_GAPPY)
    _write(tmp_path, "PHASE3_20260817-101010.md", QUIZ_OK)

    by_phase, warnings = quiz_records(tmp_path)

    assert warnings == []
    records = by_phase[3]
    assert [r["gaps_unresolved"] for r in records] == [0, 1]  # 최신(파일명 내림차순) 우선
    assert records[0]["date"] == "2026-08-17"
    assert "### Q1" in records[0]["body"]


def test_quiz_records_excludes_invalid_meta_with_warning(tmp_path: Path) -> None:
    _write(tmp_path, "PHASE3_20260817.md", QUIZ_BROKEN)

    by_phase, warnings = quiz_records(tmp_path)

    assert by_phase == {}
    assert warnings[0].code == "quiz_meta_invalid"
    assert warnings[0].phase == 3


def test_quiz_records_missing_dir_is_silent(tmp_path: Path) -> None:
    assert quiz_records(tmp_path) == ({}, [])


def _phase_row(*, verdicts: list[str | None], review: bool) -> dict:
    return {
        "phase": 3,
        "review_path": "docs/reviews/PHASE3_x.md" if review else None,
        "tasks": [{"n": i + 1, "verdict": v} for i, v in enumerate(verdicts)],
    }


def test_quiz_records_same_day_timestamped_beats_bare_date(tmp_path: Path) -> None:
    """같은 날짜에서 -HHMMSS 파일이 bare-date 파일보다 최신으로 온다 (ASCII '.'>'-' 함정)."""
    _write(tmp_path, "PHASE3_20260817.md", QUIZ_GAPPY)
    _write(tmp_path, "PHASE3_20260817-235959.md", QUIZ_OK)

    by_phase, warnings = quiz_records(tmp_path)

    assert warnings == []
    assert [r["gaps_unresolved"] for r in by_phase[3]] == [0, 1]  # 타임스탬프 파일이 최신


def test_debt_repaid_when_latest_quiz_has_no_gaps() -> None:
    record = {"date": "2026-08-17", "gaps_found": 2, "gaps_unresolved": 0}

    debt = debt_for(_phase_row(verdicts=[None], review=False), [record])

    assert debt["level"] == "repaid"
    assert debt["quiz_count"] == 1
    assert debt["latest_quiz"]["gaps_unresolved"] == 0
    assert "판정 미상 task 1개" in debt["signals"]  # 신호는 지어내지도 숨기지도 않는다


def test_debt_partial_when_quiz_has_unresolved_gaps() -> None:
    record = {"date": "2026-08-17", "gaps_found": 2, "gaps_unresolved": 1}

    debt = debt_for(_phase_row(verdicts=["pass"], review=True), [record])

    assert debt["level"] == "partial"
    assert any("미해소 갭 1개" in s for s in debt["signals"])


def test_debt_partial_when_no_quiz_but_verdicts_and_review_complete() -> None:
    debt = debt_for(_phase_row(verdicts=["pass", "fail"], review=True), [])

    assert debt["level"] == "partial"
    assert "쪽지시험 기록 없음" in debt["signals"]


def test_debt_unrepaid_when_no_quiz_and_missing_evidence() -> None:
    debt = debt_for(_phase_row(verdicts=["pass", None], review=False), [])

    assert debt["level"] == "unrepaid"
    assert "판정 미상 task 1개" in debt["signals"]
    assert "리뷰 문서 없음" in debt["signals"]


def test_attach_debt_does_not_mutate_input(tmp_path: Path) -> None:
    phases = [{"phase": 7, "review_path": None, "tasks": None}]  # merge_active 합성 행

    out, warnings = attach_debt(phases, tmp_path)

    assert "debt" not in phases[0]
    assert out[0]["debt"]["level"] == "unrepaid"
    assert warnings == []


def test_quiz_details_returns_summary_only(tmp_path: Path) -> None:
    _write(tmp_path, "PHASE3_20260817.md", QUIZ_OK)

    details, warnings = quiz_details(tmp_path, 3)

    assert warnings == []
    assert details[0] == {
        "name": "PHASE3_20260817.md",
        "date": "2026-08-17",
        "gaps_found": 2,
        "gaps_unresolved": 0,
    }


QUIZ_WITH_TRANSCRIPT = """---
phase: 3
date: 2026-08-17
gaps_found: 1
gaps_unresolved: 0
---
# Phase 3 쪽지시험 (2026-08-17)

## 평가

gaps_found: 1

- 갭: 해소됨

## 대화 전사

### Q1

왜 대안 X가 아닌가?

### A1

비밀스러운 답변 내용.
"""


def test_quiz_details_never_carries_transcript_content(tmp_path: Path) -> None:
    """전사·평가 본문은 파일에 보존되지만 상세 응답에는 어떤 형태로도 싣지 않는다."""
    import json

    _write(tmp_path, "PHASE3_20260817.md", QUIZ_WITH_TRANSCRIPT)

    details, warnings = quiz_details(tmp_path, 3)

    assert warnings == []
    payload = json.dumps(details, ensure_ascii=False)
    assert "비밀스러운 답변" not in payload
    assert "대화 전사" not in payload
    assert details[0]["gaps_unresolved"] == 0
    # 파일 자체에는 전사가 그대로 남아 있다 — 기록 보존 계약
    saved = (tmp_path / "quizzes" / "PHASE3_20260817.md").read_text(encoding="utf-8")
    assert "비밀스러운 답변" in saved

"""쪽지시험 순수 함수 — 전사 검증·평가 파싱·기록 마크다운."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import quiz


def test_validate_transcript_maps_to_api_messages() -> None:
    raw = [
        {"role": "assistant", "text": "왜 대안 X가 아닌가?"},
        {"role": "user", "text": "X는 캐시 미스가 나서."},
    ]

    messages = quiz.validate_transcript(raw)

    assert messages == [
        {"role": "assistant", "content": "왜 대안 X가 아닌가?"},
        {"role": "user", "content": "X는 캐시 미스가 나서."},
    ]


@pytest.mark.parametrize("raw", [
    "문자열",                                        # 배열 아님
    [{"role": "system", "text": "x"}],               # 허용 밖 role
    [{"role": "user", "text": "  "}],                # 공백뿐
    [{"role": "user"}],                              # text 없음
    [{"role": "user", "text": "x" * 8001}],          # 텍스트 한도 초과
    [{"role": "user", "text": "x"}] * 41,            # 턴 수 초과
])
def test_validate_transcript_rejects_bad_shapes(raw: object) -> None:
    with pytest.raises(ValueError):
        quiz.validate_transcript(raw)


def test_parse_summary_extracts_gap_counts() -> None:
    text = "gaps_found: 3\ngaps_unresolved: 1\nsummary: 설계 근거 이해 부족\n\n- 갭: ..."

    parsed = quiz.parse_summary(text)

    assert parsed == {"gaps_found": 3, "gaps_unresolved": 1, "summary": "설계 근거 이해 부족"}


def test_parse_summary_missing_fields_returns_none() -> None:
    parsed = quiz.parse_summary("자유 서술만 있는 응답")

    assert parsed == {"gaps_found": None, "gaps_unresolved": None, "summary": None}


def test_record_markdown_roundtrips_through_quiz_records(tmp_path: Path) -> None:
    """만든 기록이 지표 파서(cognitive_debt)로 그대로 읽혀야 한다 — 규약 왕복 검증."""
    from app.metrics.cognitive_debt import quiz_records

    markdown = quiz.record_markdown(
        phase=3, model="claude-sonnet-5", date_str="2026-08-17",
        evaluation_text="gaps_found: 1\ngaps_unresolved: 0\nsummary: ok\n\n- 갭: 해소됨",
        gaps={"gaps_found": 1, "gaps_unresolved": 0, "summary": "ok"},
        usage={"input_tokens": 100, "output_tokens": 50},
        cost=0.0123,
    )

    path = quiz.record_path(tmp_path, 3, "20260817-101010")
    assert quiz.save_record(path, markdown) is None

    by_phase, warnings = quiz_records(tmp_path)
    assert warnings == []
    record = by_phase[3][0]
    assert record["gaps_unresolved"] == 0
    assert record["gaps_found"] == 1
    assert record["cost"] == 0.0123
    assert "## 대화 전사" not in markdown
    assert "### Q1" not in record["body"]
    assert "갭: 해소됨" in record["body"]


def test_record_markdown_writes_dash_for_unknown_gaps() -> None:
    markdown = quiz.record_markdown(
        phase=3, model="m", date_str="2026-08-17",
        evaluation_text="자유 서술",
        gaps={"gaps_found": None, "gaps_unresolved": None, "summary": None},
        usage={"input_tokens": 1, "output_tokens": 1},
        cost=0.0,
    )

    assert "gaps_unresolved: -" in markdown  # 0으로 지어내지 않는다


def test_save_record_failure_returns_reason(tmp_path: Path) -> None:
    blocker = tmp_path / "quizzes"
    blocker.write_text("파일이라 디렉터리를 만들 수 없다", encoding="utf-8")

    error = quiz.save_record(quiz.record_path(tmp_path, 3, "20260817-101010"), "x")

    assert error is not None


def test_unique_record_path_bumps_on_same_second_collision(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 8, 17, 10, 10, 10, tzinfo=UTC)
    first = quiz.unique_record_path(tmp_path, 3, now)
    first.parent.mkdir(parents=True)
    first.write_text("기존 기록", encoding="utf-8")

    second = quiz.unique_record_path(tmp_path, 3, now)

    assert second != first
    assert second.name == "PHASE3_20260817-101011.md"  # 1초 밀림 — 규약 유지


def test_save_record_never_overwrites_existing_file(tmp_path: Path) -> None:
    path = quiz.record_path(tmp_path, 3, "20260817-101010")
    path.parent.mkdir(parents=True)
    path.write_text("기존 기록", encoding="utf-8")

    error = quiz.save_record(path, "새 기록")

    assert error is not None
    assert path.read_text(encoding="utf-8") == "기존 기록"


def test_system_prompt_clips_long_context() -> None:
    prompt = quiz.system_prompt("긴" * 40000)

    assert len(prompt) < 40000
    assert "생략" in prompt


def test_system_prompt_progression_rules() -> None:
    prompt = quiz.SYSTEM_PROMPT

    assert "주제당 최대 1회" in prompt
    assert "꼬리 질문 1회 후" in prompt
    assert "새 주제로 넘어간다" in prompt
    assert "주제 3~5개" in prompt
    assert "시험 종료" in prompt
    assert "꼬리 질문 2회" not in prompt
    assert "라고 하면 시험을 마무리한다" not in prompt

"""Tests for app/sources/claude_transcript.py (JSONL → 제목·턴 타임라인)."""

from __future__ import annotations

import json
from pathlib import Path

from app.sources.claude_transcript import (
    NO_TEXT_INSTRUCTION,
    TitleCache,
    extract_titles,
    parse_session_lines,
    title_index,
)

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures/claude_projects/proj_transcript/work-sess-0001.jsonl"
)


def _fixture_lines() -> list[str]:
    return FIXTURE.read_text(encoding="utf-8").splitlines()


def test_extract_titles_returns_the_ai_title_per_session() -> None:
    assert extract_titles(_fixture_lines()) == {"work-sess-0001": "차트 축 버그 수정"}


def test_extract_titles_keeps_the_last_title_when_repeated() -> None:
    lines = [
        json.dumps({"type": "ai-title", "aiTitle": "옛 제목", "sessionId": "s1"}),
        json.dumps({"type": "ai-title", "aiTitle": "새 제목", "sessionId": "s1"}),
    ]
    assert extract_titles(lines) == {"s1": "새 제목"}


def test_parse_session_lines_builds_two_turns_from_the_fixture() -> None:
    turns, changes, warnings = parse_session_lines(_fixture_lines(), "work-sess-0001.jsonl")
    assert warnings == []
    assert len(turns) == 2
    assert turns[0]["instruction"].startswith("차트가 안 그려져요")
    assert turns[0]["reasoning"] == [
        "원인은 축 범위 설정일 가능성이 높다. y축 최소값이 잘못 잡혀 있다."
    ]
    # Edit 액션에는 change_pos가 붙으므로 부분 일치만 확인
    assert turns[0]["actions"][0]["tool"] == "Edit"
    assert turns[0]["actions"][0]["target"] == "static/chart-page.js"
    assert turns[0]["response"] == "y축 범위를 자동 계산으로 수정했습니다."
    assert turns[1]["instruction"] == "이제 색상도 바꿔줘"
    assert turns[1]["actions"] == [{"tool": "Bash", "target": "npm test"}]


def test_tool_result_only_user_lines_are_not_instructions() -> None:
    # fixture의 4번째 라인(tool_result)이 지시로 오인되면 턴이 3개가 된다.
    turns, _, _ = parse_session_lines(_fixture_lines(), "f")
    assert len(turns) == 2


def test_malformed_json_line_is_reported_not_fatal() -> None:
    turns, _, warnings = parse_session_lines(["{broken"], "bad.jsonl")
    assert turns == []
    assert len(warnings) == 1 and "bad.jsonl:1" in warnings[0]


def test_usage_only_assistant_lines_produce_no_turn_content() -> None:
    # 기존 사용량 fixture처럼 content가 없는 assistant 라인은 조용히 건너뛴다.
    line = json.dumps({
        "type": "assistant", "timestamp": "2026-07-22T09:00:00.000Z",
        "sessionId": "s", "cwd": "/anon/p",
        "message": {"model": "m", "usage": {"input_tokens": 1}},
    })
    turns, _, warnings = parse_session_lines([line], "f")
    assert turns == []
    assert warnings == []


def test_title_index_scans_the_corpus_and_uses_the_cache(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    (root / "p1").mkdir(parents=True)
    f = root / "p1" / "a.jsonl"
    f.write_text(
        json.dumps({"type": "ai-title", "aiTitle": "제목A", "sessionId": "sA"}) + "\n",
        encoding="utf-8",
    )
    cache = TitleCache()
    titles, warnings = title_index(root, cache=cache)
    assert titles == {"sA": "제목A"}
    assert warnings == []
    mtime = f.stat().st_mtime
    assert cache.get(f, mtime) == {"sA": "제목A"}


def test_title_index_warns_for_a_missing_root(tmp_path: Path) -> None:
    titles, warnings = title_index(tmp_path / "nope")
    assert titles == {}
    assert len(warnings) == 1


def _user_line(content: object, ts: str = "2026-07-22T09:00:00.000Z") -> str:
    return json.dumps({
        "type": "user", "timestamp": ts, "cwd": "/anon/p", "sessionId": "s",
        "message": {"role": "user", "content": content},
    })


def _assistant_text_line(text: str, ts: str = "2026-07-22T09:00:10.000Z") -> str:
    return json.dumps({
        "type": "assistant", "timestamp": ts, "cwd": "/anon/p", "sessionId": "s",
        "message": {"role": "assistant", "model": "m",
                    "usage": {"input_tokens": 1},
                    "content": [{"type": "text", "text": text}]},
    })


def test_mixed_tool_result_and_text_user_line_is_an_instruction() -> None:
    line = _user_line([
        {"type": "tool_result", "content": "ok"},
        {"type": "text", "text": "이것도 고쳐줘"},
    ])
    turns, _, warnings = parse_session_lines([line], "f")
    assert len(turns) == 1
    assert turns[0]["instruction"] == "이것도 고쳐줘"
    assert warnings == []


def test_image_only_user_line_keeps_the_turn_boundary() -> None:
    """이미지 전용 지시가 사라지면 다음 응답이 직전 턴에 오귀속된다 (리뷰 🔴 재현)."""
    lines = [
        _user_line("첫 지시"),
        _assistant_text_line("첫 응답"),
        _user_line([{"type": "image", "source": {}}], ts="2026-07-22T09:01:00.000Z"),
        _assistant_text_line("둘째 응답", ts="2026-07-22T09:01:10.000Z"),
    ]
    turns, _, _ = parse_session_lines(lines, "f")
    assert len(turns) == 2
    assert turns[0]["response"] == "첫 응답"          # 오귀속 없음
    assert turns[1]["instruction"] == NO_TEXT_INSTRUCTION
    assert turns[1]["response"] == "둘째 응답"


def test_pure_tool_result_user_line_still_starts_no_turn() -> None:
    line = _user_line([{"type": "tool_result", "content": "ok"}])
    turns, _, warnings = parse_session_lines([line], "f")
    assert turns == []
    assert warnings == []


def test_unknown_assistant_block_types_are_reported_once() -> None:
    line = json.dumps({
        "type": "assistant", "timestamp": "2026-07-22T09:00:00.000Z",
        "cwd": "/anon/p", "sessionId": "s",
        "message": {"role": "assistant", "model": "m", "usage": {"input_tokens": 1},
                    "content": [{"type": "mystery", "x": 1},
                                {"type": "fallback", "note": "routing"}]},
    })
    turns, _, warnings = parse_session_lines([line], "f")
    assert any("mystery" in w for w in warnings)      # 미지 타입은 경고
    assert not any("fallback" in w for w in warnings)  # 알려진 무해 타입은 무경고


def test_turns_max_truncation_warning_survives_parse_session_lines() -> None:
    from app.sources.transcript_common import TURNS_MAX

    lines = [_user_line(f"지시 {i}") for i in range(TURNS_MAX + 3)]
    turns, _, warnings = parse_session_lines(lines, "f")
    assert len(turns) == TURNS_MAX
    assert any(str(TURNS_MAX) in w for w in warnings)


def test_non_object_json_line_is_reported() -> None:
    turns, _, warnings = parse_session_lines(["[1, 2, 3]"], "f")
    assert turns == []
    assert len(warnings) == 1 and "f:1" in warnings[0]


def test_parse_session_lines_reconstructs_the_edit_diff() -> None:
    turns, changes, warnings = parse_session_lines(
        _fixture_lines(), "work-sess-0001.jsonl"
    )

    edits = [c for c in changes if c.tool == "Edit"]
    assert edits, "Edit 도구 호출에서 변경이 수집되어야 한다"
    assert edits[0].path == "static/chart-page.js"
    assert edits[0].additions == 2
    assert edits[0].deletions == 2
    assert edits[0].turn == 0


def test_parse_session_lines_skips_non_file_tools() -> None:
    _, changes, _ = parse_session_lines(_fixture_lines(), "work-sess-0001.jsonl")

    assert all(c.tool in {"Edit", "Write"} for c in changes)


# ── Fix C: 실패한 tool_use 블록은 failed=True로 표시되어야 한다 ──

def _make_edit_lines(tool_use_id: str, is_error: bool = False) -> list[str]:
    """Edit tool_use + tool_result 쌍을 JSONL 라인으로 만든다."""
    user_instr = json.dumps({
        "type": "user", "timestamp": "2026-08-01T10:00:00.000Z",
        "cwd": "/anon/proj", "sessionId": "s1",
        "message": {"role": "user", "content": "고쳐줘"},
    })
    assistant = json.dumps({
        "type": "assistant", "timestamp": "2026-08-01T10:00:01.000Z",
        "cwd": "/anon/proj", "sessionId": "s1",
        "message": {
            "role": "assistant", "model": "m", "usage": {"input_tokens": 1},
            "content": [{
                "type": "tool_use",
                "id": tool_use_id,
                "name": "Edit",
                "input": {
                    "file_path": "/anon/proj/a.py",
                    "old_string": "old",
                    "new_string": "new",
                },
            }],
        },
    })
    tool_result = json.dumps({
        "type": "user", "timestamp": "2026-08-01T10:00:02.000Z",
        "cwd": "/anon/proj", "sessionId": "s1",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "is_error": is_error,
                "content": "String to replace not found" if is_error else "ok",
            }],
        },
    })
    return [user_instr, assistant, tool_result]


def test_failed_edit_is_marked_failed_true() -> None:
    """is_error=True인 tool_result가 있는 Edit 도구 호출은 failed=True로 표시되어야 한다."""
    lines = _make_edit_lines("tu-001", is_error=True)
    _, changes, warnings = parse_session_lines(lines, "test.jsonl")

    assert len(changes) == 1
    assert changes[0].failed is True
    assert any("a.py" in w for w in warnings), "실패 경고가 경로를 포함해야 한다"


def test_successful_edit_is_not_marked_failed() -> None:
    """is_error=False인 tool_result가 있는 Edit 도구 호출은 failed=False여야 한다."""
    lines = _make_edit_lines("tu-002", is_error=False)
    _, changes, warnings = parse_session_lines(lines, "test.jsonl")

    assert len(changes) == 1
    assert changes[0].failed is False


def test_failed_edit_still_has_hunks() -> None:
    """실패한 Edit도 hunk를 포함해야 한다 — 배지 표시 후 diff 본문이 남아야 한다."""
    lines = _make_edit_lines("tu-003", is_error=True)
    _, changes, _ = parse_session_lines(lines, "test.jsonl")

    assert changes[0].hunks, "실패한 변경에도 hunk가 있어야 한다"

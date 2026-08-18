"""Tests for the shared turn-building logic (app/sources/transcript_common.py)."""

from __future__ import annotations

from app.sources.diffs import CHANGE_POS_KEY, Hunk, make_change
from app.sources.transcript_common import (
    ACTION_TARGET_MAX_CHARS,
    ACTIONS_MAX_PER_TURN,
    INSTRUCTION_MAX_CHARS,
    REASONING_EXCERPT_MAX_CHARS,
    REASONING_MAX_PER_TURN,
    RESPONSE_MAX_CHARS,
    TURNS_MAX,
    TurnBuilder,
    first_str_value,
    project_phase_from_cwd,
    text_from_content,
    truncate,
)


def test_truncate_returns_text_unchanged_when_under_the_limit() -> None:
    assert truncate("abc", 10) == ("abc", False)


def test_truncate_clips_and_flags_when_over_the_limit() -> None:
    text, was_truncated = truncate("a" * 20, 10)
    assert text == "a" * 10
    assert was_truncated is True


def test_builder_groups_assistant_content_under_the_preceding_instruction() -> None:
    b = TurnBuilder()
    b.start_turn(1000, "첫 지시")
    b.add_reasoning("추론1")
    b.add_action("Edit", "app/main.py")
    b.add_response("응답1")
    b.start_turn(2000, "둘째 지시")
    b.add_response("응답2")
    turns, warnings = b.finish()
    assert warnings == []
    assert len(turns) == 2
    assert turns[0]["instruction"] == "첫 지시"
    assert turns[0]["reasoning"] == ["추론1"]
    assert turns[0]["actions"] == [{"tool": "Edit", "target": "app/main.py"}]
    assert turns[0]["response"] == "응답1"
    assert turns[1]["instruction"] == "둘째 지시"
    assert turns[1]["reasoning"] == []


def test_content_before_the_first_instruction_becomes_a_leading_null_turn() -> None:
    b = TurnBuilder()
    b.ensure_turn(500)
    b.add_response("재개 세션 응답")
    b.start_turn(1000, "지시")
    turns, _ = b.finish()
    assert len(turns) == 2
    assert turns[0]["instruction"] is None
    assert turns[0]["response"] == "재개 세션 응답"


def test_instruction_is_truncated_at_the_cap_with_a_flag() -> None:
    b = TurnBuilder()
    b.start_turn(0, "가" * (INSTRUCTION_MAX_CHARS + 1))
    turns, _ = b.finish()
    assert len(turns[0]["instruction"]) == INSTRUCTION_MAX_CHARS
    assert turns[0]["instruction_truncated"] is True


def test_reasoning_excerpts_are_capped_in_length_and_count() -> None:
    b = TurnBuilder()
    b.start_turn(0, "지시")
    for _ in range(REASONING_MAX_PER_TURN + 2):
        b.add_reasoning("나" * (REASONING_EXCERPT_MAX_CHARS + 100))
    turns, _ = b.finish()
    assert len(turns[0]["reasoning"]) == REASONING_MAX_PER_TURN
    assert all(len(r) == REASONING_EXCERPT_MAX_CHARS for r in turns[0]["reasoning"])
    assert turns[0]["reasoning_truncated"] is True


def test_response_blocks_are_joined_then_truncated() -> None:
    b = TurnBuilder()
    b.start_turn(0, "지시")
    b.add_response("다" * RESPONSE_MAX_CHARS)
    b.add_response("라" * 10)
    turns, _ = b.finish()
    assert len(turns[0]["response"]) == RESPONSE_MAX_CHARS
    assert turns[0]["response_truncated"] is True


def test_assistant_content_with_no_turn_started_creates_a_leading_turn() -> None:
    b = TurnBuilder()
    b.add_reasoning("고아 추론")  # ensure_turn 없이 호출해도 조용히 버리지 않는다
    turns, _ = b.finish()
    assert len(turns) == 1
    assert turns[0]["instruction"] is None
    assert turns[0]["reasoning"] == ["고아 추론"]


def test_turns_beyond_the_max_are_dropped_with_a_warning() -> None:
    b = TurnBuilder()
    for i in range(TURNS_MAX + 3):
        b.start_turn(i, f"지시 {i}")
    turns, warnings = b.finish()
    assert len(turns) == TURNS_MAX
    assert len(warnings) == 1 and str(TURNS_MAX) in warnings[0]


def test_empty_reasoning_and_response_are_ignored() -> None:
    b = TurnBuilder()
    b.start_turn(0, "지시")
    b.add_reasoning("   ")
    b.add_response("")
    turns, _ = b.finish()
    assert turns[0]["reasoning"] == []
    assert turns[0]["response"] == ""
    assert turns[0]["response_truncated"] is False


def test_worktree_cwd_is_normalized_to_the_parent_project_with_phase() -> None:
    project, phase, slug = project_phase_from_cwd(
        "/anon/usage-dashboard/.claude/worktrees/phase11-work-browser"
    )
    assert project == "usage-dashboard"
    assert phase == 11
    assert slug == "phase11-work-browser"


def test_plain_cwd_keeps_its_basename_and_has_no_phase() -> None:
    assert project_phase_from_cwd("/anon/proj-a") == ("proj-a", None, None)


def test_missing_cwd_falls_back_to_the_given_project() -> None:
    assert project_phase_from_cwd(None, "fallback") == ("fallback", None, None)


def test_worktree_slug_without_a_phase_number_yields_null_phase() -> None:
    project, phase, slug = project_phase_from_cwd(
        "/anon/proj-b/.claude/worktrees/refactor-driver"
    )
    assert project == "proj-b"
    assert phase is None
    assert slug == "refactor-driver"


def test_actions_beyond_the_cap_are_dropped_with_the_truncated_flag() -> None:
    b = TurnBuilder()
    b.start_turn(0, "지시")
    for i in range(ACTIONS_MAX_PER_TURN + 5):
        b.add_action("Edit", f"file-{i}.py")
    turns, _ = b.finish()
    assert len(turns[0]["actions"]) == ACTIONS_MAX_PER_TURN
    assert turns[0]["actions_truncated"] is True


def test_actions_under_the_cap_are_not_flagged_and_targets_are_clipped() -> None:
    b = TurnBuilder()
    b.start_turn(0, "지시")
    b.add_action("Bash", "x" * (ACTION_TARGET_MAX_CHARS + 50))
    turns, _ = b.finish()
    assert turns[0]["actions_truncated"] is False
    assert len(turns[0]["actions"][0]["target"]) == ACTION_TARGET_MAX_CHARS


def test_whitespace_only_response_does_not_create_a_phantom_leading_turn() -> None:
    b = TurnBuilder()
    b.add_response("   ")  # 턴 시작 전 공백만 — 선행 턴이 생기면 안 된다
    turns, _ = b.finish()
    assert turns == []


def test_text_from_content_flattens_only_text_blocks() -> None:
    assert text_from_content("그대로") == "그대로"
    blocks = [
        {"type": "tool_result", "content": "ok"},
        {"type": "text", "text": "a"},
        {"type": "image", "source": {}},
        {"type": "text", "text": "b"},
    ]
    assert text_from_content(blocks) == "a\nb"
    assert text_from_content(blocks, separator=" ") == "a b"
    assert text_from_content(None) == ""


def test_first_str_value_returns_the_first_non_empty_string_by_priority() -> None:
    assert first_str_value({"b": "y", "a": "x"}, ("a", "b")) == "x"
    assert first_str_value({"a": "", "b": "y"}, ("a", "b")) == "y"
    assert first_str_value({"a": 3}, ("a",)) == ""
    assert first_str_value(None, ("a",)) == ""


def _sample_change(path="a.py"):
    return make_change(
        raw_path=path,
        tool="Edit",
        turn=0,
        hunks=[Hunk(header="@@", lines=["+x"])],
        additions=1,
        deletions=0,
    )


def test_add_action_collects_the_change_and_stamps_the_position():
    builder = TurnBuilder()
    builder.add_action("Edit", "a.py", _sample_change())

    assert len(builder.changes) == 1
    assert builder.changes[0].path == "a.py"
    turns, _ = builder.finish()
    assert turns[0]["actions"][0][CHANGE_POS_KEY] == 0


def test_add_action_keeps_the_change_even_when_badges_are_truncated():
    builder = TurnBuilder()
    for _ in range(ACTIONS_MAX_PER_TURN + 3):
        builder.add_action("Edit", "a.py", _sample_change())

    assert len(builder.changes) == ACTIONS_MAX_PER_TURN + 3


def test_add_action_without_a_change_has_no_change_pos():
    builder = TurnBuilder()
    builder.add_action("Bash", "ls")

    assert builder.changes == []
    turns, _ = builder.finish()
    assert CHANGE_POS_KEY not in turns[0]["actions"][0]


def test_turn_index_advances_with_each_started_turn():
    builder = TurnBuilder()
    assert builder.turn_index == 0
    builder.start_turn(None, "first")
    assert builder.turn_index == 0
    builder.start_turn(None, "second")
    assert builder.turn_index == 1

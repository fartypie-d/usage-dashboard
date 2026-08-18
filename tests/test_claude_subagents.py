"""Tests for subagent name recovery from Agent tool_use prompts."""

from __future__ import annotations

import json
from pathlib import Path

from app.sources.claude_subagents import (
    FALLBACK_AGENT,
    agent_from_meta,
    agent_key,
    build_agent_index,
    is_subagent_file,
    resolve_agent,
    resolve_agent_from_lines,
)

PROMPT = "Review app/pricing.py for silent failures and report findings."


def _main_line(tool_name: str, subagent_type: str, prompt: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "name": tool_name,
                        "input": {"prompt": prompt, "subagent_type": subagent_type},
                    }
                ],
            },
        }
    ) + "\n"


def _subagent_line(prompt: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "isSidechain": True,
            "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
        }
    ) + "\n"


def test_is_subagent_file_detects_subagents_directory():
    assert is_subagent_file(Path("/p/sess/subagents/agent-a.jsonl")) is True
    assert is_subagent_file(Path("/p/sess.jsonl")) is False


def test_agent_key_truncates_and_strips():
    assert agent_key("  hello  ") == "hello"
    assert len(agent_key("x" * 500)) == 120


def test_build_agent_index_reads_agent_tool_use(tmp_path: Path):
    main = tmp_path / "sess.jsonl"
    main.write_text(_main_line("Agent", "python-reviewer", PROMPT), encoding="utf-8")

    index = build_agent_index([main])

    assert index[agent_key(PROMPT)] == ("python-reviewer", str(main))


def test_build_agent_index_also_accepts_task_tool_name(tmp_path: Path):
    main = tmp_path / "sess.jsonl"
    main.write_text(_main_line("Task", "code-explorer", PROMPT), encoding="utf-8")

    index = build_agent_index([main])

    assert index[agent_key(PROMPT)] == ("code-explorer", str(main))


def test_resolve_agent_matches_subagent_file_to_type(tmp_path: Path):
    sub_dir = tmp_path / "sess" / "subagents"
    sub_dir.mkdir(parents=True)
    sub = sub_dir / "agent-abc.jsonl"
    sub.write_text(_subagent_line(PROMPT), encoding="utf-8")
    main = tmp_path / "main.jsonl"
    index = {agent_key(PROMPT): ("fastapi-reviewer", str(main))}

    assert resolve_agent(sub, index) == ("fastapi-reviewer", str(main))


def test_resolve_agent_falls_back_when_prompt_not_indexed(tmp_path: Path):
    sub_dir = tmp_path / "sess" / "subagents"
    sub_dir.mkdir(parents=True)
    sub = sub_dir / "agent-abc.jsonl"
    sub.write_text(_subagent_line("an unindexed prompt"), encoding="utf-8")

    assert resolve_agent(sub, {}) == (FALLBACK_AGENT, None)


def test_resolve_agent_falls_back_on_empty_file(tmp_path: Path):
    sub_dir = tmp_path / "sess" / "subagents"
    sub_dir.mkdir(parents=True)
    sub = sub_dir / "agent-empty.jsonl"
    sub.write_text("", encoding="utf-8")

    assert resolve_agent(sub, {}) == (FALLBACK_AGENT, None)


# --- 방어 가드 커버리지: build_agent_index / _first_user_text의 이상 입력 ---

def test_build_agent_index_skips_line_with_subagent_type_substring_but_invalid_json(
    tmp_path: Path,
) -> None:
    """'subagent_type' 문자열은 포함하되 JSON이 깨진 라인은 조용히 스킵한다."""
    main = tmp_path / "sess.jsonl"
    main.write_text('{"subagent_type": broken JSON\n', encoding="utf-8")

    assert build_agent_index([main]) == {}


def test_build_agent_index_ignores_shapes_that_cannot_carry_a_dispatch(
    tmp_path: Path,
) -> None:
    """message가 dict가 아니거나 content가 list가 아니거나 블록이 dict가 아니면 스킵."""
    main = tmp_path / "sess.jsonl"
    weird_lines = [
        # message가 문자열
        json.dumps({"message": "subagent_type-in-string"}) + "\n",
        # content가 문자열
        json.dumps({"message": {"content": "\"subagent_type\": inline"}}) + "\n",
        # content가 리스트이나 블록이 문자열
        json.dumps({"message": {"content": ["\"subagent_type\": free"]}}) + "\n",
        # 블록은 dict이나 tool_use가 아님
        json.dumps(
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "\"subagent_type\": inline"}
                    ]
                }
            }
        ) + "\n",
        # 올바른 tool_use이나 name이 Agent/Task 아님
        json.dumps(
            {
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {
                                "prompt": "p",
                                "subagent_type": "python-reviewer",
                            },
                        }
                    ]
                }
            }
        ) + "\n",
    ]
    main.write_text("".join(weird_lines), encoding="utf-8")

    assert build_agent_index([main]) == {}


def test_build_agent_index_swallows_oserror_on_missing_file(tmp_path: Path) -> None:
    """존재하지 않는 파일 경로는 OSError를 조용히 넘기고 나머지 파일은 정상 처리한다."""
    missing = tmp_path / "does-not-exist.jsonl"

    valid = tmp_path / "sess.jsonl"
    valid.write_text(
        json.dumps(
            {
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Agent",
                            "input": {"prompt": "hello", "subagent_type": "sr"},
                        }
                    ]
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    index = build_agent_index([missing, valid])
    assert index == {agent_key("hello"): ("sr", str(valid))}


def test_resolve_agent_falls_back_when_first_message_is_not_dict(
    tmp_path: Path,
) -> None:
    """첫 유효 라인의 message가 dict가 아니면 폴백한다 (빈 줄은 건너뛴다)."""
    sub_dir = tmp_path / "sess" / "subagents"
    sub_dir.mkdir(parents=True)
    sub = sub_dir / "agent-x.jsonl"
    sub.write_text(
        "\n" + json.dumps({"message": "not-a-dict"}) + "\n",
        encoding="utf-8",
    )

    assert resolve_agent(sub, {}) == (FALLBACK_AGENT, None)


def test_resolve_agent_falls_back_on_missing_path(tmp_path: Path) -> None:
    """존재하지 않는 파일은 OSError를 삼키고 폴백한다."""
    missing = tmp_path / "gone.jsonl"

    assert resolve_agent(missing, {}) == (FALLBACK_AGENT, None)


def test_index_records_the_dispatching_file(tmp_path: Path) -> None:
    main = tmp_path / "main.jsonl"
    main.write_text(
        json.dumps({
            "message": {"content": [{
                "type": "tool_use", "name": "Agent",
                "input": {"subagent_type": "python-reviewer", "prompt": "PROMPT ONE"},
            }]}
        }) + "\n",
        encoding="utf-8",
    )

    index = build_agent_index([main])

    assert index[agent_key("PROMPT ONE")] == ("python-reviewer", str(main))


def test_resolver_returns_agent_and_dispatcher() -> None:
    index = {agent_key("PROMPT ONE"): ("python-reviewer", "/anon/p/main.jsonl")}
    lines = [json.dumps({"message": {"content": [{"type": "text", "text": "PROMPT ONE"}]}})]

    assert resolve_agent_from_lines(lines, index) == ("python-reviewer", "/anon/p/main.jsonl")


def test_resolver_falls_back_with_no_dispatcher() -> None:
    lines = [json.dumps({"message": {"content": [{"type": "text", "text": "UNKNOWN"}]}})]

    assert resolve_agent_from_lines(lines, {}) == (FALLBACK_AGENT, None)


def test_same_prompt_from_two_files_drops_the_dispatcher(tmp_path: Path) -> None:
    """앞 120자가 같은 디스패치가 두 파일에 있으면 누가 불렀는지 결정할 수 없다."""
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    for path in (a, b):
        path.write_text(
            json.dumps({
                "message": {"content": [{
                    "type": "tool_use", "name": "Agent",
                    "input": {"subagent_type": "python-reviewer", "prompt": "SHARED PROMPT"},
                }]}
            }) + "\n",
            encoding="utf-8",
        )

    index = build_agent_index([a, b])

    assert index[agent_key("SHARED PROMPT")] == ("python-reviewer", None)


def test_dropped_dispatcher_stays_dropped_for_a_third_file(tmp_path: Path) -> None:
    """세 번째 파일이 같은 키를 또 던져도 디스패처는 되살아나지 않는다."""
    paths = []
    for name in ("a.jsonl", "b.jsonl", "c.jsonl"):
        path = tmp_path / name
        path.write_text(
            json.dumps({
                "message": {"content": [{
                    "type": "tool_use", "name": "Agent",
                    "input": {"subagent_type": "python-reviewer", "prompt": "SHARED PROMPT"},
                }]}
            }) + "\n",
            encoding="utf-8",
        )
        paths.append(path)

    index = build_agent_index(paths)

    assert index[agent_key("SHARED PROMPT")] == ("python-reviewer", None)


# --- meta.json 폴백: 프롬프트 매칭이 실패해도 하네스 기록으로 이름을 복원한다 ---

def _write_meta(sub: Path, payload: object) -> Path:
    """``agent-x.jsonl`` 옆의 ``agent-x.meta.json``을 만든다."""
    meta = sub.with_name(sub.stem + ".meta.json")
    meta.write_text(json.dumps(payload), encoding="utf-8")
    return meta


def test_agent_from_meta_reads_sibling_agent_type(tmp_path: Path) -> None:
    sub_dir = tmp_path / "sess" / "subagents"
    sub_dir.mkdir(parents=True)
    sub = sub_dir / "agent-abc.jsonl"
    sub.write_text(_subagent_line("whatever"), encoding="utf-8")
    _write_meta(sub, {"agentType": "general-purpose", "description": "d"})

    assert agent_from_meta(sub) == "general-purpose"


def test_agent_from_meta_returns_none_when_meta_missing(tmp_path: Path) -> None:
    sub_dir = tmp_path / "sess" / "subagents"
    sub_dir.mkdir(parents=True)
    sub = sub_dir / "agent-abc.jsonl"
    sub.write_text(_subagent_line("whatever"), encoding="utf-8")

    assert agent_from_meta(sub) is None


def test_agent_from_meta_returns_none_on_broken_json(tmp_path: Path) -> None:
    sub_dir = tmp_path / "sess" / "subagents"
    sub_dir.mkdir(parents=True)
    sub = sub_dir / "agent-abc.jsonl"
    sub.write_text(_subagent_line("whatever"), encoding="utf-8")
    meta = sub.with_name(sub.stem + ".meta.json")
    meta.write_text('{"agentType": broken', encoding="utf-8")

    assert agent_from_meta(sub) is None


def test_agent_from_meta_rejects_non_string_or_empty_agent_type(tmp_path: Path) -> None:
    sub_dir = tmp_path / "sess" / "subagents"
    sub_dir.mkdir(parents=True)
    sub = sub_dir / "agent-abc.jsonl"
    sub.write_text(_subagent_line("whatever"), encoding="utf-8")

    for bad in ({"agentType": None}, {"agentType": 7}, {"agentType": ""}, {}, [1, 2], "str"):
        _write_meta(sub, bad)
        assert agent_from_meta(sub) is None, f"payload {bad!r}는 None이어야 한다"


def test_ambiguous_key_still_resolves_the_agent_name(tmp_path: Path) -> None:
    """디스패처만 버린다 — 에이전트 이름은 계속 복원된다."""
    index = {agent_key("SHARED PROMPT"): ("python-reviewer", None)}
    lines = [json.dumps({"message": {"content": [{"type": "text", "text": "SHARED PROMPT"}]}})]

    assert resolve_agent_from_lines(lines, index) == ("python-reviewer", None)

"""Recover Claude subagent names by matching prompts to ``subagent_type``.

Subagent sessions live in ``<session>/subagents/agent-*.jsonl`` and carry no
agent-type field of their own (``slug`` is a random session codename). The
parent session's main chain does: an ``Agent`` (formerly ``Task``) tool_use
block holds both ``input.subagent_type`` and ``input.prompt``, and the
subagent file's first user message repeats that prompt verbatim.

Matching on the first 120 characters of the prompt recovered 177/177 (100%)
of subagent files in the author's corpus.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from app.sources.transcript_common import text_from_content

SUBAGENT_DIR_NAME = "subagents"
PROMPT_KEY_LEN = 120
AGENT_TOOL_NAMES = frozenset({"Agent", "Task"})
FALLBACK_AGENT = "claude-subagent"

__all__ = [
    "AGENT_TOOL_NAMES",
    "FALLBACK_AGENT",
    "agent_from_meta",
    "agent_key",
    "build_agent_index",
    "is_subagent_file",
    "resolve_agent",
    "resolve_agent_from_lines",
]


def is_subagent_file(path: Path) -> bool:
    """Return True if *path* lives under a ``subagents/`` directory."""
    return SUBAGENT_DIR_NAME in path.parts


def agent_key(text: str) -> str:
    """Return the normalized match key for a prompt."""
    return text.strip()[:PROMPT_KEY_LEN]


def _content_text(content: object) -> str:
    """Flatten a message ``content`` value into plain text."""
    return text_from_content(content, separator=" ")


def build_agent_index(main_files: list[Path]) -> dict[str, tuple[str, str | None]]:
    """Map prompt keys to ``(subagent_type, dispatcher_path)``.

    같은 키를 두 파일이 던졌으면 디스패처는 ``None``이 된다.

    디스패처 경로까지 담는 이유: 서브에이전트 파일 경로에는 ``/subagents/``
    세그먼트가 항상 정확히 하나뿐이라, 손자 에이전트도 경로상으로는 루트의
    직계 자식처럼 보인다. "누가 이 프롬프트를 던졌는가"만이 2-hop을 구분한다.

    Lines are pre-filtered with a substring check so that only the handful of
    lines actually containing an agent dispatch are JSON-decoded.
    """
    index: dict[str, tuple[str, str | None]] = {}
    for path in main_files:
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if '"subagent_type"' not in line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    message = data.get("message")
                    if not isinstance(message, dict):
                        continue
                    content = message.get("content")
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_use":
                            continue
                        if block.get("name") not in AGENT_TOOL_NAMES:
                            continue
                        params = block.get("input") or {}
                        prompt = params.get("prompt")
                        subagent_type = params.get("subagent_type")
                        if prompt and subagent_type:
                            key = agent_key(prompt)
                            prev = index.get(key)
                            dispatcher: str | None = str(path)
                            if prev is not None and prev[1] != str(path):
                                # 앞 120자가 같은 디스패치가 다른 파일에도 있다.
                                # 어느 쪽이 이 서브에이전트를 불렀는지 결정할 수 없으므로
                                # 디스패처를 버리고 1-hop 부착으로 내려보낸다.
                                # (실측: 키 568개 중 26개가 여기 걸린다.)
                                dispatcher = None
                            index[key] = (subagent_type, dispatcher)
        except OSError:
            continue
    return index


def agent_from_meta(path: Path) -> str | None:
    """``agent-x.jsonl`` 옆 ``agent-x.meta.json``의 ``agentType``.

    하네스가 서브에이전트 파일마다 함께 쓰는 기록이라 프롬프트 매칭보다
    권위 있지만, 디스패처 경로는 담고 있지 않다. 매칭 실패 시 이름만이라도
    복원하는 폴백으로 쓴다. 없거나 깨졌으면 None.
    """
    meta_path = path.with_name(path.stem + ".meta.json")
    try:
        with open(meta_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    agent_type = data.get("agentType")
    if isinstance(agent_type, str) and agent_type:
        return agent_type
    return None


def _first_user_text_from_lines(lines: Iterable[str]) -> str | None:
    """Return the text of the first non-blank record among *lines*."""
    for line in lines:
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None
        message = data.get("message")
        if not isinstance(message, dict):
            return None
        return _content_text(message.get("content"))
    return None


def _first_user_text(path: Path) -> str | None:
    """Return the text of the first non-blank record in *path*."""
    try:
        with open(path, encoding="utf-8") as fh:
            return _first_user_text_from_lines(fh)
    except OSError:
        return None


def resolve_agent_from_lines(
    lines: Iterable[str], index: dict[str, tuple[str, str | None]]
) -> tuple[str, str | None]:
    """Return ``(agent, dispatcher_path)`` for already-read subagent file *lines*.

    Callers that have the file contents in hand use this to avoid opening the
    same file twice — and to avoid racing a file that is being deleted between
    the two reads.
    """
    text = _first_user_text_from_lines(lines)
    if not text:
        return FALLBACK_AGENT, None
    hit = index.get(agent_key(text))
    if hit is None:
        return FALLBACK_AGENT, None
    return hit


def resolve_agent(path: Path, index: dict[str, tuple[str, str | None]]) -> tuple[str, str | None]:
    """Return ``(agent, dispatcher_path)`` for a subagent file, or the fallback."""
    text = _first_user_text(path)
    if not text:
        return FALLBACK_AGENT, None
    hit = index.get(agent_key(text))
    if hit is None:
        return FALLBACK_AGENT, None
    return hit

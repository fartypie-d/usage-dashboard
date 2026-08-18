"""세션 트랜스크립트 공용 턴 빌더 (claude JSONL·opencode DB 양쪽이 쓴다).

발췌 상한은 스펙(2026-08-05-session-browser-design.md)이 확정한 계약이다.
상한을 바꾸면 크기 회귀 가드 테스트(test_work_session_detail.py)도 함께 보라.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.sources.diffs import CHANGE_POS_KEY, WORKTREE_MARKER, FileChange

INSTRUCTION_MAX_CHARS = 2000
REASONING_EXCERPT_MAX_CHARS = 700
REASONING_MAX_PER_TURN = 5
RESPONSE_MAX_CHARS = 1000
ACTIONS_MAX_PER_TURN = 50
ACTION_TARGET_MAX_CHARS = 200
TURNS_MAX = 500

NO_TEXT_INSTRUCTION = "(텍스트 없는 지시 — 이미지/첨부)"

_PHASE_RE = re.compile(r"phase[_-]?(\d+)", re.IGNORECASE)


def first_str_value(mapping: object, keys: tuple[str, ...]) -> str:
    """dict에서 우선순위 키 순서로 첫 비어있지 않은 문자열 값 — 도구 target 추출 공용."""
    if not isinstance(mapping, dict):
        return ""
    for key in keys:
        val = mapping.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def project_phase_from_cwd(
    cwd: str | None, fallback_project: str = "unknown"
) -> tuple[str, int | None, str | None]:
    """워크트리 cwd를 원 프로젝트로 정규화하고 페이즈를 추출한다.

    ``<프로젝트>/.claude/worktrees/<slug>`` → (프로젝트 basename, phase 번호, slug).
    워크트리가 아니면 (basename 또는 fallback, None, None).
    실측 근거: 워크트리 세션은 ``Path(cwd).name``이 slug를 프로젝트로 오인한다.
    """
    if not cwd:
        return fallback_project, None, None
    if WORKTREE_MARKER in cwd:
        root, _, slug_part = cwd.partition(WORKTREE_MARKER)
        slug = slug_part.split("/", 1)[0]
        project = Path(root).name or fallback_project
        match = _PHASE_RE.search(slug)
        phase = int(match.group(1)) if match else None
        return project, phase, slug or None
    return Path(cwd).name or fallback_project, None, None


def truncate(text: str, limit: int) -> tuple[str, bool]:
    """Return ``(clipped_text, was_truncated)``."""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


class TurnBuilder:
    """사용자 지시 1개 단위로 assistant 콘텐츠를 묶는다.

    첫 지시 이전에 assistant 콘텐츠가 오면(재개 세션 등) ``instruction=None``인
    선행 턴 1개로 묶는다 — 프런트는 "(이전 세션에서 이어짐)"으로 표시한다.
    add_* 는 현재 턴이 없으면 선행 턴을 만들어 받는다 (조용한 유실 금지).
    """

    def __init__(self) -> None:
        self._turns: list[dict] = []
        self._current: dict | None = None
        self._response_parts: list[str] = []
        self._changes: list[FileChange] = []

    def start_turn(self, ts: int | None, instruction: str | None) -> None:
        self._finalize()
        if instruction is None:
            text, was_truncated = None, False
        else:
            text, was_truncated = truncate(instruction, INSTRUCTION_MAX_CHARS)
        self._current = {
            "ts": ts,
            "instruction": text,
            "instruction_truncated": was_truncated,
            "reasoning": [],
            "reasoning_truncated": False,
            "actions": [],
            "actions_truncated": False,
            "response": "",
            "response_truncated": False,
        }

    def ensure_turn(self, ts: int | None) -> None:
        if self._current is None:
            self.start_turn(ts, None)

    def add_reasoning(self, text: str) -> None:
        if not text.strip():
            return
        self.ensure_turn(None)
        assert self._current is not None
        if len(self._current["reasoning"]) >= REASONING_MAX_PER_TURN:
            self._current["reasoning_truncated"] = True
            return
        excerpt, was_truncated = truncate(text, REASONING_EXCERPT_MAX_CHARS)
        self._current["reasoning"].append(excerpt)
        if was_truncated:
            self._current["reasoning_truncated"] = True

    def add_action(
        self,
        tool: str,
        target: str,
        change: FileChange | None = None,
        changes: list[FileChange] | None = None,
    ) -> None:
        """액션 1개를 현재 턴에 추가한다.

        ``changes`` 가 주어지면 복수의 FileChange를 등록한다 (멀티-파일 apply_patch 용).
        ``change`` 는 단일-파일 레거시 인터페이스 — 내부적으로 ``changes=[change]``로 처리한다.
        배지가 상한에 걸려 버려져도 diff는 남긴다 (조용한 유실 금지).
        """
        self.ensure_turn(None)
        assert self._current is not None

        # 단일·복수 인터페이스를 통일한다.
        effective: list[FileChange] = []
        if changes is not None:
            effective = list(changes)
        elif change is not None:
            effective = [change]

        # 배지 상한 **이전**에 모든 변경을 _changes에 추가 — 조용한 유실 금지.
        first_pos: int | None = None
        for fc in effective:
            self._changes.append(fc)
            if first_pos is None:
                first_pos = len(self._changes) - 1

        if len(self._current["actions"]) >= ACTIONS_MAX_PER_TURN:
            self._current["actions_truncated"] = True
            return

        clipped, _ = truncate(target, ACTION_TARGET_MAX_CHARS)
        action = {"tool": tool, "target": clipped}
        # 점프 대상은 멀티-파일 패치에서도 첫 번째 파일을 가리킨다.
        if first_pos is not None:
            action[CHANGE_POS_KEY] = first_pos
        self._current["actions"].append(action)

    @property
    def turn_index(self) -> int:
        """현재 작성 중인 턴의 0-based 인덱스 — FileChange.turn에 쓴다."""
        return len(self._turns)

    @property
    def changes(self) -> list[FileChange]:
        return list(self._changes)

    def add_response(self, text: str) -> None:
        if not text.strip():
            return
        self.ensure_turn(None)
        self._response_parts.append(text)

    def _finalize(self) -> None:
        if self._current is None:
            return
        joined = "\n".join(self._response_parts)
        response, was_truncated = truncate(joined, RESPONSE_MAX_CHARS)
        self._current["response"] = response
        self._current["response_truncated"] = was_truncated
        self._turns.append(self._current)
        self._current = None
        self._response_parts = []

    def finish(self) -> tuple[list[dict], list[str]]:
        self._finalize()
        if len(self._turns) > TURNS_MAX:
            warning = (
                f"턴 {len(self._turns)}개 중 앞 {TURNS_MAX}개만 반환합니다"
            )
            return self._turns[:TURNS_MAX], [warning]
        return self._turns, []


def text_from_content(content: object, *, separator: str = "\n") -> str:
    """message.content(str | 블록 리스트)를 평문으로 평탄화 — ``type=="text"`` 블록만.

    claude_transcript(지시 추출)와 claude_subagents(프롬프트 키)가 공유한다.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return separator.join(p for p in parts if p)
    return ""

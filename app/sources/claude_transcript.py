"""Claude Code 세션 JSONL → 제목(ai-title)·턴 타임라인.

사용량 파서(claude_jsonl.py)는 usage 필드만 보지만, 여기서는 message.content
원문(지시·thinking·tool_use)을 다룬다. 상세는 파일 1개 단위 온디맨드,
목록 제목은 mtime 캐시(TitleCache)로 더운 요청에서 파일을 다시 읽지 않는다.
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace as dc_replace
from pathlib import Path

from app.sources.claude_jsonl import _parse_iso_timestamp
from app.sources.diff_adapters import from_claude_block
from app.sources.diffs import FileChange, repo_relative
from app.sources.transcript_common import (
    NO_TEXT_INSTRUCTION,
    TurnBuilder,
    first_str_value,
    text_from_content,
)

_ACTION_TARGET_KEYS = ("file_path", "command", "name", "description", "skill")

# 실데이터에 존재하는 알려진 무해 블록 타입 — 모델 폴백 라우팅 공지 등 (경고 대상 아님)
KNOWN_SKIPPED_BLOCK_TYPES = frozenset({"fallback"})

FAILED_EDIT_WARNING = (
    "{path}: Edit 도구가 실패했습니다 — diff는 표시하지만 실제 적용되지 않은 변경입니다"
)


def _ts_ms(data: dict) -> int | None:
    raw = data.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return int(_parse_iso_timestamp(raw).timestamp() * 1000)
    except ValueError:
        return None


def _action_target(tool_input: object) -> str:
    return first_str_value(tool_input, _ACTION_TARGET_KEYS)


def extract_titles(lines: list[str]) -> dict[str, str]:
    """ai-title 라인에서 ``{sessionId: title}`` — 같은 세션은 마지막 값이 이긴다."""
    titles: dict[str, str] = {}
    for line in lines:
        if '"ai-title"' not in line:  # json.loads 전 빠른 배제 (코퍼스 전체 스캔용)
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or data.get("type") != "ai-title":
            continue
        session_id, title = data.get("sessionId"), data.get("aiTitle")
        if isinstance(session_id, str) and isinstance(title, str) and title:
            titles[session_id] = title
    return titles


def _has_attachment_blocks(content: object) -> bool:
    """tool_result·text 외 블록(이미지·첨부 등)이 있는가 — 텍스트 없는 실제 사용자 턴."""
    return isinstance(content, list) and any(
        isinstance(block, dict)
        and block.get("type") not in ("tool_result", "text")
        for block in content
    )



def _prescan_failed_tool_ids(lines: list[str]) -> set[str]:
    """raw 라인에서 is_error를 가진 tool_result의 tool_use_id를 수집한다.

    '"is_error"' 부분 문자열이 없는 라인은 json.loads 없이 건너뛴다 (전체 코퍼스 스캔 비용 절감).
    """
    failed: set[str] = set()
    for line in lines:
        if '"is_error"' not in line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or data.get("type") != "user":
            continue
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            if block.get("is_error"):
                tool_use_id = block.get("tool_use_id")
                if isinstance(tool_use_id, str) and tool_use_id:
                    failed.add(tool_use_id)
    return failed


def parse_session_lines(
    lines: list[str], source_file: str
) -> tuple[list[dict], list[FileChange], list[str]]:
    """세션 JSONL 라인들 → (턴 목록, 변경 목록, 경고). 비대화 라인은 조용히 건너뛴다."""
    builder = TurnBuilder()
    warnings: list[str] = []
    unknown_block_types: set[str] = set()

    # 실패한 tool_use_id를 raw 라인 빠른 검사로 먼저 수집한다 (단일 패스, 최소 파싱).
    failed_tool_ids = _prescan_failed_tool_ids(lines)

    for line_num, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            warnings.append(f"{source_file}:{line_num}: {exc}")
            continue
        if not isinstance(data, dict):
            warnings.append(
                f"{source_file}:{line_num}: top-level JSON is not an object "
                f"(type={type(data).__name__})"
            )
            continue
        rec_type = data.get("type")
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if rec_type == "user":
            text = text_from_content(content).strip()
            if text:
                builder.start_turn(_ts_ms(data), text)
            elif _has_attachment_blocks(content):
                # 이미지·첨부 전용 지시 — 경계를 유실하면 다음 assistant 응답이
                # 직전 턴에 오귀속된다 (리뷰 🔴 실데이터 재현). 플레이스홀더로 유지.
                builder.start_turn(_ts_ms(data), NO_TEXT_INSTRUCTION)
            # 순수 tool_result 라인은 직전 턴의 도구 흐름 — 턴을 만들지 않는다
        elif rec_type == "assistant":
            if not isinstance(content, list):
                continue  # 사용량 전용 라인(콘텐츠 없음)은 정상 — 경고 없이 스킵
            builder.ensure_turn(_ts_ms(data))
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "thinking":
                    builder.add_reasoning(block.get("thinking") or "")
                elif block_type == "text":
                    builder.add_response(block.get("text") or "")
                elif block_type == "tool_use":
                    change_list, change_warnings = from_claude_block(
                        block, builder.turn_index
                    )
                    warnings.extend(change_warnings)
                    # tool_use_id가 실패 집합에 있으면 변경을 실패로 표시한다.
                    tool_use_id = block.get("id")
                    if (
                        change_list
                        and isinstance(tool_use_id, str)
                        and tool_use_id in failed_tool_ids
                    ):
                        marked: list = []
                        for fc in change_list:
                            marked.append(dc_replace(fc, failed=True))
                            warnings.append(
                                FAILED_EDIT_WARNING.format(
                                    path=repo_relative(fc.raw_path) or "(경로 불명)"
                                )
                            )
                        change_list = marked
                    builder.add_action(
                        block.get("name") or "(unknown)",
                        _action_target(block.get("input")),
                        changes=change_list or None,
                    )
                elif block_type not in KNOWN_SKIPPED_BLOCK_TYPES:
                    unknown_block_types.add(str(block_type))
    if unknown_block_types:
        warnings.append(
            f"{source_file}: 알 수 없는 assistant 블록 타입을 건너뜀: "
            f"{sorted(unknown_block_types)}"
        )
    turns, finish_warnings = builder.finish()
    return turns, builder.changes, warnings + finish_warnings


class TitleCache:
    """mtime-keyed title cache — ``ParserCache``와 같은 규약 (스레드 안전)."""

    def __init__(self) -> None:
        self._cache: dict[Path, tuple[float, dict[str, str]]] = {}
        self._lock = threading.Lock()

    def get(self, path: Path, mtime: float) -> dict[str, str] | None:
        with self._lock:
            entry = self._cache.get(path)
        if entry is not None:
            cached_mtime, titles = entry
            if cached_mtime == mtime:
                return titles
        return None

    def set(self, path: Path, mtime: float, titles: dict[str, str]) -> None:
        with self._lock:
            self._cache[path] = (mtime, titles)


def title_index(
    root: Path, cache: TitleCache | None = None
) -> tuple[dict[str, str], list[str]]:
    """코퍼스 전체의 ``{session_id: 제목}`` + 경고.

    콜드 요청은 코퍼스를 한 번 읽지만(기존 parse_directory와 같은 비용 계급),
    더운 요청은 mtime이 바뀐 파일만 다시 읽는다. FileNotFoundError는 정상
    스캔 경합이라 무경고, 그 외 OSError는 경고로 드러낸다.
    """
    titles: dict[str, str] = {}
    warnings: list[str] = []
    if not root.exists() or not root.is_dir():
        return titles, [f"Directory does not exist or is not a directory: {root}"]
    for jsonl_file in sorted(root.rglob("*.jsonl")):
        rel = str(jsonl_file.relative_to(root))
        try:
            mtime = jsonl_file.stat().st_mtime
        except FileNotFoundError:
            continue  # 스캔 중 정리된 파일 — 정상 경합
        except OSError as exc:
            warnings.append(f"{rel}: {exc}")
            continue
        if cache is not None:
            cached = cache.get(jsonl_file, mtime)
            if cached is not None:
                titles.update(cached)
                continue
        try:
            with open(jsonl_file, encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            continue
        except OSError as exc:
            warnings.append(f"{rel}: {exc}")
            continue
        file_titles = extract_titles(lines)
        if cache is not None:
            cache.set(jsonl_file, mtime, file_titles)
        titles.update(file_titles)
    return titles, warnings

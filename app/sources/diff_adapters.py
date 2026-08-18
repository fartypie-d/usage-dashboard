"""트랜스크립트 도구 호출 → FileChange 변환. diffs.py에만 의존한다."""

from __future__ import annotations

import re
from typing import Any

from app.sources.diffs import (
    FileChange,
    Hunk,
    make_change,
    parse_unified_diff,
    repo_relative,
    split_unified_diff,
    synthesize_edit,
    synthesize_write,
)

OPENCODE_FILE_TOOLS = frozenset({"edit", "write", "apply_patch"})
CLAUDE_FILE_TOOLS = frozenset({"Edit", "Write"})

NO_DIFF_WARNING = "{path}: 변경 내용이 트랜스크립트에 없어 diff를 표시할 수 없습니다"
COUNT_MISMATCH_WARNING = (
    "{path}: 기록된 +{recorded_add}/-{recorded_del}과 "
    "실제 diff의 +{counted_add}/-{counted_del}이 다릅니다"
)
FAILED_TOOL_WARNING = (
    "{path}: opencode 도구 호출이 실패했습니다 (status=error) — "
    "diff는 표시하지만 실제 적용되지 않은 변경입니다"
)

_OPENCODE_PATH_KEYS = ("filePath", "file_path", "path")

_INDEX_LINE_RE = re.compile(r"^Index:\s+(.+)$", re.MULTILINE)
_PLUS_PLUS_RE = re.compile(r"^\+\+\+\s+b/(.+)$", re.MULTILINE)
_PLUS_PLUS_ABS_RE = re.compile(r"^\+\+\+\s+(/\S.*)$", re.MULTILINE)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_path(mapping: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _display_path(raw_path: str) -> str:
    """경고 문구에 넣을 경로. 경로를 못 찾았을 때 문장이 ': '로 시작하지 않게 한다."""
    return repo_relative(raw_path) or "(경로 불명)"


def _path_from_diff_text(diff_text: str) -> str:
    """diff 텍스트의 첫 'Index: ' 또는 '+++ b/' 줄에서 경로를 추출한다.

    apply_patch 도구는 입력에 경로가 없고 diff 텍스트 안에만 있다.
    """
    m = _INDEX_LINE_RE.search(diff_text)
    if m:
        return m.group(1).strip()
    m = _PLUS_PLUS_RE.search(diff_text)
    if m:
        return m.group(1).strip()
    m = _PLUS_PLUS_ABS_RE.search(diff_text)
    if m:
        return m.group(1).strip()
    return ""


def from_opencode_part(
    part: dict[str, Any], turn: int
) -> tuple[list[FileChange], list[str]]:
    """opencode의 tool part에서 파일 변경을 뽑는다.

    단일 파일 도구는 1-요소 목록, 멀티-파일 apply_patch는 N-요소 목록을 반환한다.
    인식된 파일 도구이지만 diff가 없으면 1-요소 목록(빈 hunks + 경고)을 반환한다 — 조용한 유실 금지.
    인식되지 않은 도구는 빈 목록을 반환한다.
    """
    tool = part.get("tool")
    if tool not in OPENCODE_FILE_TOOLS:
        return [], []

    state = _as_dict(part.get("state"))
    is_failed = state.get("status") == "error"
    tool_input = _as_dict(state.get("input"))
    metadata = _as_dict(state.get("metadata"))
    filediff = _as_dict(metadata.get("filediff"))

    raw_path = ""
    if isinstance(filediff.get("file"), str):
        raw_path = filediff["file"]
    if not raw_path:
        raw_path = _first_path(tool_input, _OPENCODE_PATH_KEYS)

    warnings: list[str] = []

    patch = filediff.get("patch")
    diff_text = metadata.get("diff")
    content = tool_input.get("content")

    # apply_patch (또는 멀티-파일 diff) 처리: split_unified_diff로 파일별 분리
    if isinstance(patch, str) and patch.strip():
        # filediff.patch는 단일 파일용 — 기존 로직 유지
        hunks, counted_add, counted_del = parse_unified_diff(patch)
        additions, deletions = counted_add, counted_del
        recorded_add = filediff.get("additions")
        recorded_del = filediff.get("deletions")
        if isinstance(recorded_add, int) and isinstance(recorded_del, int):
            additions, deletions = recorded_add, recorded_del
            if (recorded_add, recorded_del) != (counted_add, counted_del):
                warnings.append(
                    COUNT_MISMATCH_WARNING.format(
                        path=_display_path(raw_path),
                        recorded_add=recorded_add,
                        recorded_del=recorded_del,
                        counted_add=counted_add,
                        counted_del=counted_del,
                    )
                )
        if is_failed:
            warnings.append(
                FAILED_TOOL_WARNING.format(path=_display_path(raw_path))
            )
        return (
            [
                make_change(
                    raw_path=raw_path,
                    tool=str(tool),
                    turn=turn,
                    hunks=hunks,
                    additions=additions,
                    deletions=deletions,
                    failed=is_failed,
                )
            ],
            warnings,
        )

    elif isinstance(diff_text, str) and diff_text.strip():
        # diff_text は멀티-파일일 수 있다 (apply_patch가 주된 사례).
        segments = split_unified_diff(diff_text)
        changes: list[FileChange] = []
        for seg_path, hunks, additions, deletions in segments:
            effective_path = seg_path or raw_path or _path_from_diff_text(diff_text)
            if is_failed:
                warnings.append(
                    FAILED_TOOL_WARNING.format(path=_display_path(effective_path))
                )
            changes.append(
                make_change(
                    raw_path=effective_path,
                    tool=str(tool),
                    turn=turn,
                    hunks=hunks,
                    additions=additions,
                    deletions=deletions,
                    failed=is_failed,
                )
            )
        if changes:
            return changes, warnings
        # split_unified_diff가 빈 결과를 냈을 때 폴백 (보호)
        effective_path = raw_path or _path_from_diff_text(diff_text)
        if is_failed:
            warnings.append(
                FAILED_TOOL_WARNING.format(path=_display_path(effective_path))
            )
        warnings.append(NO_DIFF_WARNING.format(path=_display_path(effective_path)))
        return (
            [
                make_change(
                    raw_path=effective_path,
                    tool=str(tool),
                    turn=turn,
                    hunks=[],
                    additions=0,
                    deletions=0,
                    failed=is_failed,
                )
            ],
            warnings,
        )

    elif tool == "write" and isinstance(content, str) and content:
        hunks, additions, deletions = synthesize_write(content)
        if is_failed:
            warnings.append(
                FAILED_TOOL_WARNING.format(path=_display_path(raw_path))
            )
        return (
            [
                make_change(
                    raw_path=raw_path,
                    tool=str(tool),
                    turn=turn,
                    hunks=hunks,
                    additions=additions,
                    deletions=deletions,
                    failed=is_failed,
                )
            ],
            warnings,
        )
    else:
        if is_failed:
            warnings.append(
                FAILED_TOOL_WARNING.format(path=_display_path(raw_path))
            )
        warnings.append(NO_DIFF_WARNING.format(path=_display_path(raw_path)))
        return (
            [
                make_change(
                    raw_path=raw_path,
                    tool=str(tool),
                    turn=turn,
                    hunks=[],
                    additions=0,
                    deletions=0,
                    failed=is_failed,
                )
            ],
            warnings,
        )


def from_claude_block(
    block: dict[str, Any], turn: int
) -> tuple[list[FileChange], list[str]]:
    """Claude Code의 tool_use 블록에서 파일 변경을 합성한다.

    Claude의 Edit/Write는 항상 단일 파일이므로 0- 또는 1-요소 목록을 반환한다.
    """
    name = block.get("name")
    if name not in CLAUDE_FILE_TOOLS:
        return [], []

    tool_input = _as_dict(block.get("input"))
    raw_path = _first_path(tool_input, ("file_path", "path"))

    warnings: list[str] = []
    hunks: list[Hunk] = []
    additions = deletions = 0
    replace_all = bool(tool_input.get("replace_all"))

    old_string = tool_input.get("old_string")
    new_string = tool_input.get("new_string")
    content = tool_input.get("content")

    if name == "Edit" and isinstance(old_string, str) and isinstance(new_string, str):
        hunks, additions, deletions = synthesize_edit(old_string, new_string)
    elif name == "Write" and isinstance(content, str) and content:
        hunks, additions, deletions = synthesize_write(content)
    else:
        warnings.append(NO_DIFF_WARNING.format(path=_display_path(raw_path)))

    return (
        [
            make_change(
                raw_path=raw_path,
                tool=str(name),
                turn=turn,
                hunks=hunks,
                additions=additions,
                deletions=deletions,
                replace_all=replace_all,
            )
        ],
        warnings,
    )

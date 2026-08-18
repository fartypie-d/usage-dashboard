"""트랜스크립트에서 재구성한 파일 변경(diff) 표현 — stdlib만 의존하는 리프 모듈."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, replace
from typing import Any

DIFF_HUNK_LINES_MAX = 400
DIFF_FILES_MAX = 40
DIFF_TOTAL_BYTES_MAX = 400_000

WORKTREE_MARKER = "/.claude/worktrees/"
EXCERPT_HEADER = "@@ 발췌 @@"
WHOLE_FILE_HEADER = "@@ 전체 @@"

# 홈 디렉터리 접두사만 접는다. /home/<user>, /Users/<user> (macOS), /root (root 계정).
# /opt·/var 등 홈이 아닌 절대경로는 저장소 루트를 알 수 없으므로 그대로 둔다.
_HOME_RE = re.compile(r"^(?:/(?:home|Users)/[^/]+|/root)/(.+)$")


@dataclass(frozen=True)
class Hunk:
    header: str
    lines: tuple[str, ...] = ()
    truncated: bool = False

    def __post_init__(self) -> None:
        # frozen=True는 재할당만 막는다. 내부 시퀀스를 굳혀야 in-place 변이가 불가능해진다.
        # 호출부가 list를 넘겨도 되도록 경계에서 변환한다.
        object.__setattr__(self, "lines", tuple(self.lines))


@dataclass(frozen=True)
class FileChange:
    path: str
    raw_path: str
    tool: str
    turn: int
    additions: int
    deletions: int
    hunks: tuple[Hunk, ...] = ()
    truncated: bool = False
    replace_all: bool = False
    failed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "hunks", tuple(self.hunks))


def repo_relative(path: str) -> str:
    """절대 경로를 저장소 상대 경로로 줄인다.

    워크트리 접두사(``/.claude/worktrees/<name>/``)는 ``<repo>/<tail>``로 접는다.
    홈 디렉터리 접두사도 접는다. 홈이 아닌 절대경로(``/opt`` 등)는 저장소 루트를
    추정할 수 없으므로 그대로 반환한다.
    """
    if not isinstance(path, str) or not path:
        return ""
    if WORKTREE_MARKER in path:
        root, _, rest = path.partition(WORKTREE_MARKER)
        repo = root.rsplit("/", 1)[-1]
        tail = rest.split("/", 1)[1] if "/" in rest else ""
        if repo and tail:
            return f"{repo}/{tail}"
        return path
    match = _HOME_RE.match(path)
    if match:
        return match.group(1)
    return path


def is_file_boundary(line: str) -> bool:
    """unified diff에서 다음 파일 구역이 시작되는 마커인지 판정한다.

    ``"---"``는 삭제된 블록 안에 있을 수 있는 유효 콘텐츠(SQL 주석, YAML 구분자 등)라
    마커로 쓰지 않는다. ``parse_unified_diff``와 ``split_unified_diff``가 같은 규칙을
    쓰도록 한 곳에 둔다.
    """
    return line.startswith("Index: ") or line.startswith("diff --git ")


def parse_unified_diff(text: str) -> tuple[list[Hunk], int, int]:
    """unified diff 본문을 hunk 목록과 +/- 줄 수로 분해한다."""
    hunks: list[Hunk] = []
    current_header = ""
    current_lines: list[str] = []
    additions = 0
    deletions = 0

    def flush() -> None:
        nonlocal current_header, current_lines
        if current_header:
            hunks.append(Hunk(header=current_header, lines=current_lines))
        current_header = ""
        current_lines = []

    for line in text.splitlines():
        if line.startswith("@@"):
            flush()
            current_header = line
            continue
        # 다음 파일의 서두 마커 — 현재 hunk를 flush하고 헤더 대기 상태로 돌아간다.
        if is_file_boundary(line):
            flush()
            current_header = ""
            continue
        if not current_header:
            continue  # Index:/---/+++ 등 서두 헤더는 버린다
        if line.startswith("\\"):
            continue  # "\\ No newline at end of file"
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
        elif line and not line.startswith(" "):
            continue  # diff 문법 밖의 잡음은 무시
        current_lines.append(line)
    flush()
    return hunks, additions, deletions


def synthesize_edit(old: str, new: str) -> tuple[list[Hunk], int, int]:
    """Claude Edit의 old_string/new_string으로 diff를 합성한다."""
    generated = difflib.unified_diff(
        old.splitlines(), new.splitlines(), lineterm="", n=3
    )
    hunks, additions, deletions = parse_unified_diff("\n".join(generated))
    return (
        [replace(hunk, header=EXCERPT_HEADER) for hunk in hunks],
        additions,
        deletions,
    )


def synthesize_write(content: str) -> tuple[list[Hunk], int, int]:
    """Write/신규 파일 내용을 전부 추가된 줄로 표현한다."""
    lines = ["+" + line for line in content.splitlines()]
    if not lines:
        return [], 0, 0
    return [Hunk(header=WHOLE_FILE_HEADER, lines=lines)], len(lines), 0


def cap_hunk_lines(hunks: list[Hunk]) -> tuple[list[Hunk], bool]:
    """한 변경당 표시 줄 수를 DIFF_HUNK_LINES_MAX로 제한한다."""
    budget = DIFF_HUNK_LINES_MAX
    out: list[Hunk] = []
    truncated = False
    for hunk in hunks:
        if budget <= 0:
            truncated = True
            break
        if len(hunk.lines) <= budget:
            out.append(hunk)
            budget -= len(hunk.lines)
            continue
        out.append(replace(hunk, lines=hunk.lines[:budget], truncated=True))
        budget = 0
        truncated = True
    return out, truncated


def make_change(
    *,
    raw_path: str,
    tool: str,
    turn: int,
    hunks: list[Hunk],
    additions: int,
    deletions: int,
    replace_all: bool = False,
    failed: bool = False,
) -> FileChange:
    """경로를 정규화하고 hunk 수를 제한한 FileChange를 만든다."""
    capped, truncated = cap_hunk_lines(hunks)
    return FileChange(
        path=repo_relative(raw_path),
        raw_path=raw_path,
        tool=tool,
        turn=turn,
        additions=additions,
        deletions=deletions,
        hunks=capped,
        truncated=truncated,
        replace_all=replace_all,
        failed=failed,
    )


_PLUS_PLUS_B_RE = re.compile(r"^\+\+\+\s+b/(.+)$")
_PLUS_PLUS_ABS_RE = re.compile(r"^\+\+\+\s+(/\S.*)$")
_MINUS_MINUS_A_RE = re.compile(r"^---\s+a/(.+)$")


def split_unified_diff(
    text: str,
) -> list[tuple[str, list[Hunk], int, int]]:
    """unified diff 텍스트를 파일별 ``(path, hunks, additions, deletions)``로 분해한다.

    ``Index: `` 및 ``diff --git `` 경계를 기준으로 각 파일 구역을 분리한다.
    경로는 ``Index: `` → ``+++ b/`` → ``+++ /abs/`` → ``--- a/`` 순으로 폴백한다.
    """
    results: list[tuple[str, list[Hunk], int, int]] = []

    # 각 파일 구역의 원시 라인들을 수집한다.
    segments: list[tuple[str, list[str]]] = []
    current_path = ""
    current_seg: list[str] = []
    in_file = False

    for line in text.splitlines():
        if is_file_boundary(line):
            if in_file:
                segments.append((current_path, current_seg))
            current_path = line[len("Index: "):].strip() if line.startswith("Index: ") else ""
            current_seg = []
            in_file = True
            continue
        if in_file:
            # +++ 줄에서 경로를 보완 — Index: 가 없을 때도 여기서 잡힌다.
            if not current_path:
                m = _PLUS_PLUS_B_RE.match(line)
                if m:
                    current_path = m.group(1).strip()
                else:
                    m = _PLUS_PLUS_ABS_RE.match(line)
                    if m:
                        current_path = m.group(1).strip()
            if not current_path:
                m = _MINUS_MINUS_A_RE.match(line)
                if m:
                    current_path = m.group(1).strip()
            current_seg.append(line)

    if in_file:
        segments.append((current_path, current_seg))

    # 파일 구역이 없으면(Index:/diff --git 없는 단일 파일 diff) 전체를 하나로 간주한다.
    if not segments:
        hunks, additions, deletions = parse_unified_diff(text)
        return [("", hunks, additions, deletions)]

    for path, seg_lines in segments:
        hunks, additions, deletions = parse_unified_diff("\n".join(seg_lines))
        results.append((path, hunks, additions, deletions))

    return results


CHANGE_POS_KEY = "change_pos"

FILES_CAP_WARNING = (
    "변경 파일이 {total}개라 상위 {cap}개만 표시합니다 (상한 {cap}개)"
)
BYTES_CAP_WARNING = (
    "diff 본문이 상한({cap} bytes)을 넘어 일부 파일의 내용을 생략했습니다"
)


def _hunk_dict(hunk: Hunk) -> dict[str, Any]:
    return {
        "header": hunk.header,
        "lines": list(hunk.lines),
        "truncated": hunk.truncated,
    }


def _change_dict(change: FileChange) -> dict[str, Any]:
    return {
        "tool": change.tool,
        "turn": change.turn,
        "additions": change.additions,
        "deletions": change.deletions,
        "replace_all": change.replace_all,
        "truncated": change.truncated,
        "failed": change.failed,
        "hunks": [_hunk_dict(hunk) for hunk in change.hunks],
    }


def _change_bytes(change: FileChange) -> int:
    return sum(
        len(hunk.header.encode("utf-8"))
        + sum(len(line.encode("utf-8")) + 1 for line in hunk.lines)
        for hunk in change.hunks
    )


def build_files(
    changes: list[FileChange],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[int | None], list[str]]:
    """FileChange 목록을 파일 단위 패널 페이로드로 조립한다."""
    grouped: dict[str, list[tuple[int, FileChange]]] = {}
    for pos, change in enumerate(changes):
        grouped.setdefault(change.path, []).append((pos, change))

    ranked = sorted(
        grouped.items(),
        key=lambda item: (
            -sum(c.additions + c.deletions for _, c in item[1]),
            item[0],
        ),
    )

    files: list[dict[str, Any]] = []
    index_map: list[int | None] = [None] * len(changes)
    warnings: list[str] = []
    total_additions = 0
    total_deletions = 0
    byte_budget = DIFF_TOTAL_BYTES_MAX
    bytes_exceeded = False

    for path, entries in ranked:
        # 실패한 변경은 diff_stat 합계에서 제외한다 — 실제로 디스크에 반영되지 않았으므로.
        # 파일 행에는 (실패 배지와 함께) 여전히 나타난다.
        total_additions += sum(c.additions for _, c in entries if not c.failed)
        total_deletions += sum(c.deletions for _, c in entries if not c.failed)

        if len(files) >= DIFF_FILES_MAX:
            continue  # 파일 행 자체가 잘림 — index_map은 None으로 남는다

        needed = sum(_change_bytes(c) for _, c in entries)
        drop_bodies = bytes_exceeded or needed > byte_budget
        if drop_bodies:
            bytes_exceeded = True
        else:
            byte_budget -= needed

        change_dicts = []
        for pos, change in entries:
            index_map[pos] = len(files)
            payload = _change_dict(change)
            if drop_bodies:
                payload["hunks"] = []
                payload["truncated"] = True
            change_dicts.append(payload)

        files.append(
            {
                "path": path,
                "additions": sum(c.additions for _, c in entries if not c.failed),
                "deletions": sum(c.deletions for _, c in entries if not c.failed),
                "change_count": len(entries),
                "truncated": drop_bodies or any(c.truncated for _, c in entries),
                "changes": change_dicts,
            }
        )

    if len(grouped) > DIFF_FILES_MAX:
        warnings.append(
            FILES_CAP_WARNING.format(total=len(grouped), cap=DIFF_FILES_MAX)
        )
    if bytes_exceeded:
        warnings.append(BYTES_CAP_WARNING.format(cap=DIFF_TOTAL_BYTES_MAX))

    diff_stat = {
        "files": len(grouped),
        "additions": total_additions,
        "deletions": total_deletions,
        # warnings: 파일 수·바이트 상한 초과. any(f["truncated"]): hunk 줄 수 상한 초과.
        "truncated": bool(warnings) or any(f["truncated"] for f in files),
    }
    return files, diff_stat, index_map, warnings


def attach_file_index(
    turns: list[dict[str, Any]], index_map: list[int | None]
) -> list[dict[str, Any]]:
    """액션의 임시 change_pos를 files 인덱스로 바꾼 새 턴 목록을 만든다."""
    out: list[dict[str, Any]] = []
    for turn in turns:
        actions = []
        for action in turn.get("actions", []):
            new_action = dict(action)
            pos = new_action.pop(CHANGE_POS_KEY, None)
            if isinstance(pos, int) and 0 <= pos < len(index_map):
                mapped = index_map[pos]
                if mapped is not None:
                    new_action["file_index"] = mapped
            actions.append(new_action)
        new_turn = dict(turn)
        new_turn["actions"] = actions
        out.append(new_turn)
    return out

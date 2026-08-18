from dataclasses import replace

import pytest

from app.sources.diffs import (
    DIFF_FILES_MAX,
    DIFF_HUNK_LINES_MAX,
    DIFF_TOTAL_BYTES_MAX,
    EXCERPT_HEADER,
    WHOLE_FILE_HEADER,
    FileChange,
    Hunk,
    attach_file_index,
    build_files,
    cap_hunk_lines,
    make_change,
    parse_unified_diff,
    repo_relative,
    synthesize_edit,
    synthesize_write,
)

OPENCODE_DIFF = """Index: /home/dev/usage-dashboard/app/main.py
===================================================================
--- /home/dev/usage-dashboard/app/main.py
+++ /home/dev/usage-dashboard/app/main.py
@@ -1,4 +1,5 @@
 import os
-import sys
+import json
+import re

 app = None
"""


def test_repo_relative_strips_the_home_prefix():
    assert repo_relative("/home/dev/usage-dashboard/app/main.py") == (
        "usage-dashboard/app/main.py"
    )


def test_repo_relative_folds_the_worktree_prefix():
    raw = "/home/dev/usage-dashboard/.claude/worktrees/phase15/app/main.py"

    assert repo_relative(raw) == "usage-dashboard/app/main.py"


def test_parse_unified_diff_counts_additions_and_deletions():
    hunks, additions, deletions = parse_unified_diff(OPENCODE_DIFF)

    assert len(hunks) == 1
    assert hunks[0].header == "@@ -1,4 +1,5 @@"
    assert additions == 2
    assert deletions == 1


def test_parse_unified_diff_drops_the_preamble_headers():
    hunks, _, _ = parse_unified_diff(OPENCODE_DIFF)

    assert not any(line.startswith("---") for line in hunks[0].lines)
    assert not any(line.startswith("+++") for line in hunks[0].lines)


def test_synthesize_edit_builds_an_excerpt_hunk():
    hunks, additions, deletions = synthesize_edit("a\nb\nc", "a\nB\nc")

    assert [h.header for h in hunks] == [EXCERPT_HEADER]
    assert additions == 1
    assert deletions == 1


def test_synthesize_write_marks_every_line_as_added():
    hunks, additions, deletions = synthesize_write("one\ntwo")

    assert hunks[0].header == WHOLE_FILE_HEADER
    assert hunks[0].lines == ("+one", "+two")
    assert (additions, deletions) == (2, 0)


def test_synthesize_write_of_empty_content_has_no_hunks():
    assert synthesize_write("") == ([], 0, 0)


def test_cap_hunk_lines_truncates_at_the_line_budget():
    big = Hunk(header="@@", lines=["+x"] * (DIFF_HUNK_LINES_MAX + 10))

    capped, truncated = cap_hunk_lines([big])

    assert truncated is True
    assert len(capped[0].lines) == DIFF_HUNK_LINES_MAX
    assert capped[0].truncated is True


def test_cap_hunk_lines_leaves_small_hunks_alone():
    small = Hunk(header="@@", lines=["+x", "-y"])

    capped, truncated = cap_hunk_lines([small])

    assert truncated is False
    assert capped == [small]


def test_make_change_normalizes_the_path_and_keeps_the_raw_one():
    hunks, additions, deletions = synthesize_write("one")

    change = make_change(
        raw_path="/home/dev/usage-dashboard/a.py",
        tool="Write",
        turn=3,
        hunks=hunks,
        additions=additions,
        deletions=deletions,
    )

    assert change.path == "usage-dashboard/a.py"
    assert change.raw_path == "/home/dev/usage-dashboard/a.py"
    assert change.turn == 3
    assert change.replace_all is False


def _change(path, turn=0, adds=1, dels=0, lines=None):
    return make_change(
        raw_path=path,
        tool="edit",
        turn=turn,
        hunks=[Hunk(header="@@", lines=lines or ["+x"])],
        additions=adds,
        deletions=dels,
    )


def test_build_files_groups_changes_by_path():
    files, diff_stat, index_map, warnings = build_files(
        [_change("a.py", turn=0), _change("a.py", turn=2), _change("b.py", turn=1)]
    )

    by_path = {f["path"]: f for f in files}
    assert set(by_path) == {"a.py", "b.py"}
    assert by_path["a.py"]["change_count"] == 2
    assert [c["turn"] for c in by_path["a.py"]["changes"]] == [0, 2]
    assert diff_stat["files"] == 2
    assert warnings == []
    assert index_map == [0, 0, 1]


def test_build_files_ranks_by_total_change_volume():
    files, _, _, _ = build_files(
        [_change("small.py", adds=1), _change("big.py", adds=50, dels=10)]
    )

    assert [f["path"] for f in files] == ["big.py", "small.py"]


def test_build_files_sums_the_diff_stat():
    _, diff_stat, _, _ = build_files(
        [_change("a.py", adds=3, dels=1), _change("b.py", adds=2, dels=4)]
    )

    assert diff_stat["additions"] == 5
    assert diff_stat["deletions"] == 5
    assert diff_stat["truncated"] is False


def test_build_files_caps_the_file_count_but_keeps_the_total():
    changes = [_change(f"f{i}.py") for i in range(DIFF_FILES_MAX + 5)]

    files, diff_stat, index_map, warnings = build_files(changes)

    assert len(files) == DIFF_FILES_MAX
    assert diff_stat["files"] == DIFF_FILES_MAX + 5
    assert diff_stat["truncated"] is True
    assert index_map.count(None) == 5
    assert any(str(DIFF_FILES_MAX) in w for w in warnings)


def test_build_files_empties_hunk_bodies_past_the_byte_budget():
    fat = ["+" + "y" * 200] * 300
    changes = [_change(f"f{i}.py", lines=list(fat)) for i in range(20)]

    files, diff_stat, _, warnings = build_files(changes)

    assert len(files) == 20  # 파일 행은 사라지지 않는다
    assert files[-1]["changes"][0]["hunks"] == []
    assert files[-1]["truncated"] is True
    assert diff_stat["truncated"] is True
    assert any(str(DIFF_TOTAL_BYTES_MAX) in w for w in warnings)


def test_build_files_of_nothing_is_empty():
    files, diff_stat, index_map, warnings = build_files([])

    assert files == []
    assert diff_stat == {
        "files": 0,
        "additions": 0,
        "deletions": 0,
        "truncated": False,
    }
    assert index_map == []
    assert warnings == []


def test_attach_file_index_replaces_change_pos_with_file_index():
    turns = [{"actions": [{"tool": "edit", "target": "a.py", "change_pos": 0}]}]

    out = attach_file_index(turns, [3])

    assert out[0]["actions"][0] == {"tool": "edit", "target": "a.py", "file_index": 3}
    assert "change_pos" in turns[0]["actions"][0]  # 원본은 그대로


def test_attach_file_index_drops_positions_that_were_cut():
    turns = [{"actions": [{"tool": "edit", "target": "a.py", "change_pos": 0}]}]

    out = attach_file_index(turns, [None])

    assert out[0]["actions"][0] == {"tool": "edit", "target": "a.py"}


def test_attach_file_index_leaves_non_file_actions_untouched():
    turns = [{"actions": [{"tool": "Bash", "target": "ls"}]}]

    out = attach_file_index(turns, [])

    assert out[0]["actions"][0] == {"tool": "Bash", "target": "ls"}


# ── Fix B: 멀티-파일 unified diff가 첫 번째 파일의 hunk를 오염하지 않아야 한다 ──

TWO_FILE_PATCH = """\
Index: first.py
===================================================================
--- first.py
+++ first.py
@@ -1,3 +1,3 @@
 a
-b
+B
 c
Index: second.py
===================================================================
--- second.py
+++ second.py
@@ -10,3 +10,4 @@
 x
+y
 z
"""


def test_parse_unified_diff_multi_file_hunk_grouping():
    """두 번째 파일 서두가 첫 번째 파일의 hunk에 오염되지 않아야 한다."""
    hunks, additions, deletions = parse_unified_diff(TWO_FILE_PATCH)

    # 두 hunk가 분리되어야 하며, 두 번째 파일의 --- / +++ 줄이 첫 hunk 본문에 없어야 한다.
    assert len(hunks) == 2
    assert hunks[0].header == "@@ -1,3 +1,3 @@"
    assert hunks[1].header == "@@ -10,3 +10,4 @@"
    # 두 번째 파일의 preamble이 첫 hunk 줄에 없어야 한다
    assert not any(line.startswith("---") or line.startswith("+++") for line in hunks[0].lines)


def test_parse_unified_diff_multi_file_correct_counts():
    """멀티-파일 diff 의 additions/deletions 합계가 올바르게 계산되어야 한다."""
    _, additions, deletions = parse_unified_diff(TWO_FILE_PATCH)

    assert additions == 2  # +B (first) + +y (second)
    assert deletions == 1  # -b (first)


def test_parse_unified_diff_content_minus_lines_not_flushed():
    """삭제된 블록 안의 SQL/YAML 구분자 '---'는 hunk를 끊지 않아야 한다."""
    diff = (
        "@@ -1,4 +1,3 @@\n"
        " select 1\n"
        "----- SQL comment line\n"  # 삭제된 SQL 주석 — hunk 내 콘텐츠
        "-select 2\n"
        " end\n"
    )
    hunks, additions, deletions = parse_unified_diff(diff)

    assert len(hunks) == 1
    assert deletions == 2  # '-- SQL comment'와 'select 2'


# ── Fix D: diff_stat.truncated이 hunk 줄 수 상한도 반영해야 한다 ──

def test_build_files_truncated_flag_reflects_hunk_line_cap():
    """hunk 줄 수 상한에 걸려 개별 파일이 truncated일 때 diff_stat.truncated도 참이어야 한다."""
    fat_lines = ["+x"] * (DIFF_HUNK_LINES_MAX + 10)
    change = make_change(
        raw_path="big.py",
        tool="Write",
        turn=0,
        hunks=[Hunk(header="@@ 전체 @@", lines=fat_lines)],
        additions=len(fat_lines),
        deletions=0,
    )
    # 파일 수·바이트 상한에는 걸리지 않지만 hunk 줄 수 상한에는 걸린다.
    files, diff_stat, _, warnings = build_files([change])

    assert files[0]["truncated"] is True
    assert diff_stat["truncated"] is True  # Fix D: 기존에는 False였다
    assert warnings == []  # 파일 수·바이트 경고는 없어야 한다


# ── Fix C: 실패한 변경이 diff_stat 합계에서 제외되어야 한다 ──

def _failed_change(path: str, adds: int = 2, dels: int = 2) -> FileChange:
    hunks, a, d = synthesize_edit("old1\nold2", "new1\nnew2")
    c = make_change(
        raw_path=path,
        tool="Edit",
        turn=0,
        hunks=hunks,
        additions=adds,
        deletions=dels,
    )
    return replace(c, failed=True)


def test_build_files_failed_change_excluded_from_diff_stat():
    """실패한 변경의 additions/deletions가 diff_stat 합계에 포함되지 않아야 한다."""
    success = _change("a.py", adds=3, dels=1)
    failed = _failed_change("a.py", adds=99, dels=99)

    _, diff_stat, _, _ = build_files([success, failed])

    assert diff_stat["additions"] == 3
    assert diff_stat["deletions"] == 1


def test_build_files_failed_change_still_appears_in_file_row():
    """실패한 변경은 파일 행의 changes 목록에 나타나야 한다 (배지 표시 대상)."""
    failed = _failed_change("b.py")

    files, _, _, _ = build_files([failed])

    assert len(files) == 1
    assert files[0]["changes"][0]["failed"] is True


def test_build_files_file_row_sums_exclude_failed_changes():
    """파일 행의 additions/deletions 합계도 실패한 변경을 제외해야 한다 (Fix 4).

    diff_stat은 이미 제외하고 있었지만 파일 행 합계는 기존에 실패 변경을 포함했다.
    """
    success = _change("c.py", adds=5, dels=2)
    failed = _failed_change("c.py", adds=99, dels=99)

    files, _, _, _ = build_files([success, failed])

    assert len(files) == 1
    assert files[0]["additions"] == 5, (
        f"파일 행 additions가 {files[0]['additions']}인데 5여야 한다 — 실패 변경이 포함됐음"
    )
    assert files[0]["deletions"] == 2, (
        f"파일 행 deletions가 {files[0]['deletions']}인데 2여야 한다 — 실패 변경이 포함됐음"
    )
    # 개별 change 블록은 여전히 자신의 값을 보여야 한다 (실패 배지 옆에 표시됨).
    change_dicts = files[0]["changes"]
    assert len(change_dicts) == 2
    failed_dict = next(c for c in change_dicts if c["failed"])
    assert failed_dict["additions"] == 99


def test_hunk_lines_are_immutable_even_when_built_from_a_list():
    """frozen=True는 재할당만 막는다 — 내부 시퀀스까지 굳혀야 실제로 불변이다."""
    source = ["+a", "+b"]
    hunk = Hunk(header="@@", lines=source)

    assert hunk.lines == ("+a", "+b")
    assert isinstance(hunk.lines, tuple)

    source.append("+c")  # 원본을 건드려도 Hunk는 영향받지 않는다
    assert hunk.lines == ("+a", "+b")

    with pytest.raises(AttributeError):
        hunk.lines.append("+d")


def test_file_change_hunks_are_immutable_even_when_built_from_a_list():
    source = [Hunk(header="@@", lines=["+a"])]
    change = make_change(
        raw_path="/home/dev/proj/x.py",
        tool="Write",
        turn=0,
        hunks=source,
        additions=1,
        deletions=0,
    )

    assert isinstance(change.hunks, tuple)

    source.append(Hunk(header="@@2", lines=["+z"]))
    assert len(change.hunks) == 1

    with pytest.raises(AttributeError):
        change.hunks.append(Hunk(header="@@3"))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/home/dev/proj/src/x.py", "proj/src/x.py"),
        ("/Users/dev/proj/src/x.py", "proj/src/x.py"),
        ("/root/proj/src/x.py", "proj/src/x.py"),
        ("/home/dev/x.py", "x.py"),
        # 홈이 아닌 절대경로는 접을 근거가 없다 — 그대로 둔다.
        ("/opt/tools/x.py", "/opt/tools/x.py"),
        ("/var/lib/app/x.py", "/var/lib/app/x.py"),
        # /rootfs는 /root와 다르다 — /root 패턴이 /rootfs를 삼키면 안 된다.
        ("/rootfs/tools/x.py", "/rootfs/tools/x.py"),
        # 홈 디렉터리 자체(하위 경로 없음)는 접지 않는다.
        ("/home/dev", "/home/dev"),
        ("/root", "/root"),
        # 이미 상대경로면 그대로.
        ("app/main.py", "app/main.py"),
        ("", ""),
    ],
)
def test_repo_relative_normalizes_only_home_prefixes(raw, expected):
    assert repo_relative(raw) == expected


def test_repo_relative_rejects_non_string_input():
    assert repo_relative(None) == ""
    assert repo_relative(123) == ""

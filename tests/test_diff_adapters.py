from app.sources.diff_adapters import from_claude_block, from_opencode_part

PATCH = "@@ -1,2 +1,3 @@\n a\n-b\n+B\n+c\n"


def _oc_part(tool, state):
    return {"type": "tool", "tool": tool, "callID": "c1", "state": state}


def test_opencode_edit_prefers_the_filediff_patch():
    part = _oc_part(
        "edit",
        {
            "status": "completed",
            "input": {"filePath": "/home/dev/usage-dashboard/app/main.py"},
            "metadata": {
                "filediff": {
                    "file": "/home/dev/usage-dashboard/app/main.py",
                    "patch": PATCH,
                    "additions": 2,
                    "deletions": 1,
                }
            },
        },
    )

    changes, warnings = from_opencode_part(part, turn=0)
    assert len(changes) == 1
    change = changes[0]
    assert warnings == []
    assert change.path == "usage-dashboard/app/main.py"
    assert change.tool == "edit"
    assert (change.additions, change.deletions) == (2, 1)
    assert change.hunks[0].header == "@@ -1,2 +1,3 @@"


def test_opencode_edit_falls_back_to_metadata_diff():
    part = _oc_part(
        "edit",
        {
            "status": "completed",
            "input": {"filePath": "/home/dev/usage-dashboard/a.py"},
            "metadata": {"diff": "--- a\n+++ b\n" + PATCH},
        },
    )

    changes, warnings = from_opencode_part(part, turn=1)
    assert len(changes) == 1
    change = changes[0]
    assert warnings == []
    assert (change.additions, change.deletions) == (2, 1)
    assert change.turn == 1


def test_opencode_write_synthesizes_from_the_input_content():
    part = _oc_part(
        "write",
        {
            "status": "completed",
            "input": {
                "filePath": "/home/dev/usage-dashboard/new.py",
                "content": "line1\nline2",
            },
        },
    )

    changes, warnings = from_opencode_part(part, turn=2)
    assert len(changes) == 1
    change = changes[0]
    assert warnings == []
    assert (change.additions, change.deletions) == (2, 0)
    assert change.hunks[0].lines == ("+line1", "+line2")


def test_opencode_file_tool_without_material_keeps_the_entry_and_warns():
    part = _oc_part(
        "edit",
        {"status": "completed", "input": {"file_path": "infra/compose.yml"}},
    )

    changes, warnings = from_opencode_part(part, turn=0)

    assert len(changes) == 1
    change = changes[0]
    assert change.path == "infra/compose.yml"
    assert change.hunks == ()
    assert len(warnings) == 1
    assert "infra/compose.yml" in warnings[0]


def test_opencode_non_file_tool_is_skipped():
    part = _oc_part("bash", {"status": "completed", "input": {"command": "ls"}})

    assert from_opencode_part(part, turn=0) == ([], [])


def test_opencode_filediff_count_mismatch_warns_but_keeps_the_diff():
    part = _oc_part(
        "edit",
        {
            "status": "completed",
            "input": {"filePath": "/home/dev/usage-dashboard/a.py"},
            "metadata": {
                "filediff": {
                    "file": "/home/dev/usage-dashboard/a.py",
                    "patch": PATCH,
                    "additions": 99,
                    "deletions": 99,
                }
            },
        },
    )

    changes, warnings = from_opencode_part(part, turn=0)
    assert len(changes) == 1
    assert changes[0].hunks
    assert len(warnings) == 1
    assert "usage-dashboard/a.py" in warnings[0]


def test_claude_edit_synthesizes_and_keeps_replace_all():
    block = {
        "type": "tool_use",
        "name": "Edit",
        "input": {
            "file_path": "/home/dev/usage-dashboard/static/sessions.js",
            "old_string": "a\nb\nc",
            "new_string": "a\nB\nc",
            "replace_all": True,
        },
    }

    changes, warnings = from_claude_block(block, turn=4)
    assert len(changes) == 1
    change = changes[0]
    assert warnings == []
    assert change.path == "usage-dashboard/static/sessions.js"
    assert change.tool == "Edit"
    assert change.replace_all is True
    assert (change.additions, change.deletions) == (1, 1)


def test_claude_write_marks_the_whole_file_as_added():
    block = {
        "type": "tool_use",
        "name": "Write",
        "input": {"file_path": "docs/a.md", "content": "x\ny"},
    }

    changes, warnings = from_claude_block(block, turn=0)
    assert len(changes) == 1
    assert warnings == []
    assert (changes[0].additions, changes[0].deletions) == (2, 0)


def test_claude_edit_without_strings_keeps_the_entry_and_warns():
    block = {
        "type": "tool_use",
        "name": "Edit",
        "input": {"file_path": "static/chart-page.js"},
    }

    changes, warnings = from_claude_block(block, turn=0)

    assert len(changes) == 1
    assert changes[0].hunks == ()
    assert len(warnings) == 1


def test_claude_non_file_tool_is_skipped():
    block = {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}

    assert from_claude_block(block, turn=0) == ([], [])


# ── Fix A: apply_patch 도구의 경로 추출 ──

APPLY_PATCH_DIFF = """\
Index: /home/dev/usage-dashboard/app/main.py
===================================================================
--- /home/dev/usage-dashboard/app/main.py
+++ /home/dev/usage-dashboard/app/main.py
@@ -1,2 +1,3 @@
 import os
+import sys
 app = None
"""


def test_opencode_apply_patch_extracts_path_from_index_line():
    """apply_patch 도구는 input에 경로가 없고 metadata.diff 안의 'Index: ' 줄에서 추출해야 한다."""
    part = _oc_part(
        "apply_patch",
        {
            "status": "completed",
            "input": {},  # 경로 없음
            "metadata": {"diff": APPLY_PATCH_DIFF},
        },
    )

    changes, warnings = from_opencode_part(part, turn=0)

    assert len(changes) == 1
    change = changes[0]
    assert change.path == "usage-dashboard/app/main.py"
    assert change.additions == 1
    assert change.deletions == 0


def test_opencode_apply_patch_falls_back_to_plus_plus_line():
    """'Index: ' 줄이 없을 때 '+++ b/' 줄에서 경로를 추출해야 한다."""
    diff = (
        "--- a/src/util.py\n"
        "+++ b/src/util.py\n"
        "@@ -1 +1,2 @@\n"
        " x\n"
        "+y\n"
    )
    part = _oc_part(
        "apply_patch",
        {
            "status": "completed",
            "input": {},
            "metadata": {"diff": diff},
        },
    )

    changes, warnings = from_opencode_part(part, turn=0)

    assert len(changes) == 1
    change = changes[0]
    assert change.path == "src/util.py"
    assert change.additions == 1


def test_opencode_apply_patch_multi_file_correct_hunk_grouping():
    """apply_patch의 멀티-파일 diff가 각 파일별 FileChange로 분리되어야 한다."""
    two_file = """\
Index: /home/dev/proj/a.py
===================================================================
--- /home/dev/proj/a.py
+++ /home/dev/proj/a.py
@@ -1 +1,2 @@
 x
+y
Index: /home/dev/proj/b.py
===================================================================
--- /home/dev/proj/b.py
+++ /home/dev/proj/b.py
@@ -5 +5 @@
-old
+new
"""
    part = _oc_part(
        "apply_patch",
        {
            "status": "completed",
            "input": {},
            "metadata": {"diff": two_file},
        },
    )

    changes, warnings = from_opencode_part(part, turn=0)

    # 두 파일이 각각 별개의 FileChange로 분리되어야 한다.
    assert len(changes) == 2, f"멀티-파일 diff는 FileChange 2개여야 한다, 실제: {len(changes)}"

    # 첫 번째 파일
    assert "proj/a.py" in changes[0].path
    assert changes[0].additions == 1
    assert changes[0].deletions == 0

    # 두 번째 파일
    assert "proj/b.py" in changes[1].path
    assert changes[1].additions == 1
    assert changes[1].deletions == 1

    # 서두 헤더(--- / +++)가 hunk 줄에 포함되지 않아야 한다.
    for change in changes:
        all_lines = [line for hunk in change.hunks for line in hunk.lines]
        assert not any(
            line.startswith("---") or line.startswith("+++") for line in all_lines
        ), f"{change.path}: hunk 줄에 헤더가 포함됨: {all_lines}"


def test_opencode_apply_patch_four_files_all_tracked():
    """4-파일 apply_patch가 4개 FileChange를 모두 생성한다 (유실 금지 검증)."""
    four_file = """\
Index: /home/dev/proj/_db_url.py
===================================================================
--- /home/dev/proj/_db_url.py
+++ /home/dev/proj/_db_url.py
@@ -1 +1,2 @@
 x
+y
Index: /home/dev/proj/import_vpn_configs.py
===================================================================
--- /home/dev/proj/import_vpn_configs.py
+++ /home/dev/proj/import_vpn_configs.py
@@ -1 +1 @@
-old
+new
Index: /home/dev/proj/activate_iphone_vpns.py
===================================================================
--- /home/dev/proj/activate_iphone_vpns.py
+++ /home/dev/proj/activate_iphone_vpns.py
@@ -1 +1,3 @@
 a
+b
+c
Index: /home/dev/proj/swap_shop_vpns.py
===================================================================
--- /home/dev/proj/swap_shop_vpns.py
+++ /home/dev/proj/swap_shop_vpns.py
@@ -3 +3 @@
-x
+z
"""
    part = _oc_part(
        "apply_patch",
        {
            "status": "completed",
            "input": {},
            "metadata": {"diff": four_file},
        },
    )

    changes, warnings = from_opencode_part(part, turn=0)

    assert len(changes) == 4, f"4-파일 diff는 FileChange 4개여야 한다, 실제: {len(changes)}"
    paths = [c.path for c in changes]
    assert any("_db_url.py" in p for p in paths)
    assert any("import_vpn_configs.py" in p for p in paths)
    assert any("activate_iphone_vpns.py" in p for p in paths)
    assert any("swap_shop_vpns.py" in p for p in paths)


# ── Fix B: opencode failed tool calls (status="error") ──

def test_opencode_failed_status_marks_change_as_failed():
    """status=error인 tool part는 FileChange.failed=True로 표시되어야 한다."""
    part = _oc_part(
        "write",
        {
            "status": "error",
            "input": {
                "filePath": "/tmp/docker-compose.prod.yml",
                "content": "version: '3'\n" + "x: y\n" * 64,
            },
        },
    )

    changes, warnings = from_opencode_part(part, turn=0)

    assert len(changes) == 1
    change = changes[0]
    assert change.failed is True
    assert any("실패" in w or "error" in w for w in warnings)


def test_opencode_failed_edit_with_diff_still_shows_hunks_but_failed():
    """실패한 edit라도 diff가 있으면 hunks가 채워지고 failed=True여야 한다."""
    part = _oc_part(
        "edit",
        {
            "status": "error",
            "input": {"filePath": "/home/dev/proj/foo.py"},
            "metadata": {"diff": "--- a/proj/foo.py\n+++ b/proj/foo.py\n@@ -1 +1 @@\n-x\n+y\n"},
        },
    )

    changes, warnings = from_opencode_part(part, turn=0)

    assert len(changes) == 1
    change = changes[0]
    assert change.failed is True
    assert change.hunks  # diff 내용은 그대로 보여야 한다
    assert any("실패" in w or "error" in w for w in warnings)


def test_opencode_completed_status_not_failed():
    """status=completed이면 failed=False여야 한다."""
    part = _oc_part(
        "edit",
        {
            "status": "completed",
            "input": {"filePath": "/home/dev/proj/ok.py"},
            "metadata": {"diff": "--- a/proj/ok.py\n+++ b/proj/ok.py\n@@ -1 +1 @@\n-a\n+b\n"},
        },
    )

    changes, warnings = from_opencode_part(part, turn=0)

    assert len(changes) == 1
    assert changes[0].failed is False

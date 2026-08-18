"""Tests for GET /api/work/session/{source}/{session_id} (턴 타임라인 상세)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

SIZE_BUDGET_BYTES = 20_000  # fixture 실측 대비 넉넉한 상한 — 필드를 무심코 늘리면 걸린다


def test_claude_detail_returns_the_contract_keys(client: TestClient) -> None:
    res = client.get("/api/work/session/claude/work-sess-0001")
    assert res.status_code == 200
    data = res.json()
    assert set(data.keys()) == {
        "session",
        "turns",
        "files",
        "diff_stat",
        "warnings",
    }
    assert data["session"]["project"] == "proj_transcript"
    assert data["session"]["title"] == "차트 축 버그 수정"


def test_claude_detail_builds_the_turn_timeline(client: TestClient) -> None:
    data = client.get("/api/work/session/claude/work-sess-0001").json()
    turns = data["turns"]
    assert len(turns) == 2
    assert turns[0]["instruction"].startswith("차트가 안 그려져요")
    assert turns[0]["reasoning"] != []
    action = turns[0]["actions"][0]
    assert action["tool"] == "Edit"
    assert action["target"] == "static/chart-page.js"
    assert action["file_index"] == 0
    assert "change_pos" not in action
    assert turns[1]["instruction"] == "이제 색상도 바꿔줘"


def test_turn_objects_carry_the_full_key_set(client: TestClient) -> None:
    data = client.get("/api/work/session/claude/work-sess-0001").json()
    assert set(data["turns"][0].keys()) == {
        "ts", "instruction", "instruction_truncated",
        "reasoning", "reasoning_truncated",
        "actions", "actions_truncated", "response", "response_truncated",
    }


def test_opencode_detail_builds_the_turn_timeline(client: TestClient) -> None:
    res = client.get("/api/work/session/opencode/oc-work-0001")
    assert res.status_code == 200
    data = res.json()
    assert data["session"]["title"] == "배포 설정 정리"
    assert data["session"]["project"] == "proj_oc"
    assert len(data["turns"]) == 2
    assert data["turns"][0]["instruction"] == "compose 파일 정리해줘"


def test_unknown_source_is_a_404(client: TestClient) -> None:
    assert client.get("/api/work/session/gemini/x").status_code == 404


def test_unknown_claude_session_is_a_404(client: TestClient) -> None:
    assert client.get("/api/work/session/claude/no-such-id").status_code == 404


def test_unknown_opencode_session_is_a_404(client: TestClient) -> None:
    assert client.get("/api/work/session/opencode/no-such-id").status_code == 404


def test_detail_response_stays_under_the_size_budget(client: TestClient) -> None:
    """발췌 상한이 실제로 응답 크기를 통제하는지의 회귀 가드.

    상한 상수(transcript_common)나 턴 직렬화 필드를 늘리면 이 예산이 잡아낸다.
    """
    res = client.get("/api/work/session/claude/work-sess-0001")
    assert len(res.content) < SIZE_BUDGET_BYTES


def test_claude_corpus_warnings_surface_in_the_detail_response(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_load_and_filter의 corpus 경고가 상세 응답 warnings에 도달한다 (합류 유실 가드)."""
    import app.main as main_mod

    real = main_mod._load_and_filter

    def fake(range_key: str, source: str):
        records, warnings, freshness = real(range_key, source)
        return records, [*warnings, "synthetic-corpus-warning"], freshness

    monkeypatch.setattr(main_mod, "_load_and_filter", fake)
    data = client.get("/api/work/session/claude/work-sess-0001").json()
    assert "synthetic-corpus-warning" in data["warnings"]


def test_unknown_claude_session_404_carries_corpus_warnings(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """레코드 0개가 인프라 장애 때문일 때 404 detail이 그 사실을 드러낸다."""
    import app.main as main_mod

    monkeypatch.setattr(
        main_mod,
        "_load_and_filter",
        lambda range_key, source: ([], ["synthetic: Permission denied"], {}),
    )
    res = client.get("/api/work/session/claude/no-such-id")
    assert res.status_code == 404
    assert "Permission denied" in res.json()["detail"]


def test_opencode_orphan_session_fallback_is_signaled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """세션 행 없는 비정상 DB 폴백이 경고 없이 정상처럼 보이지 않는다."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "session_meta", lambda db, sid: (None, []))
    data = client.get("/api/work/session/opencode/oc-work-0001").json()
    assert data["session"]["project"] == "unknown"
    assert any("message-only fallback" in w for w in data["warnings"])


def test_claude_parse_warnings_surface_in_the_detail_response(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """parse_session_lines 경고가 응답 warnings에 도달한다 (합류 유실 가드)."""
    import app.main as main_mod

    monkeypatch.setattr(
        main_mod,
        "parse_session_lines",
        lambda lines, rel: ([], [], ["synthetic-parse-warning"]),
    )
    data = client.get("/api/work/session/claude/work-sess-0001").json()
    assert "synthetic-parse-warning" in data["warnings"]


def test_opencode_meta_warnings_surface_in_the_detail_response(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """session_meta 경고가 응답 warnings에 도달한다 (합류 유실 가드)."""
    import app.main as main_mod

    real = main_mod.session_meta

    def fake(db, sid):
        meta_row, warnings = real(db, sid)
        return meta_row, [*warnings, "synthetic-meta-warning"]

    monkeypatch.setattr(main_mod, "session_meta", fake)
    data = client.get("/api/work/session/opencode/oc-work-0001").json()
    assert "synthetic-meta-warning" in data["warnings"]


def test_opencode_empty_turns_is_a_200_with_an_empty_timeline(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """턴 0개([])는 404(None)와 다르다 — 빈 타임라인 200 (Task 4 이월 확인사항)."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "session_turns", lambda db, sid: ([], [], []))
    res = client.get("/api/work/session/opencode/oc-work-0001")
    assert res.status_code == 200
    assert res.json()["turns"] == []


def test_claude_unreadable_file_is_a_404_not_a_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FileNotFoundError 외 OSError(권한 등)도 500이 아니라 404로 처리된다."""
    import builtins

    real_open = builtins.open

    def deny(path, *args, **kwargs):
        # Only deny the session transcript open; leave other opens alone.
        path_str = str(path)
        if path_str.endswith(".jsonl") or "work-sess" in path_str:
            raise PermissionError("synthetic-permission-denied")
        return real_open(path, *args, **kwargs)

    # 파서 캐시를 웜업해 corpus 스캔이 open을 타지 않게 한다 — 이 테스트는
    # 핸들러의 직접 open() 경로(except OSError)만 검증한다 (순서 의존 제거).
    assert client.get("/api/work/session/claude/work-sess-0001").status_code == 200

    monkeypatch.setattr(builtins, "open", deny)
    res = client.get("/api/work/session/claude/work-sess-0001")
    assert res.status_code == 404
    assert "unreadable" in res.json()["detail"]


def test_claude_detail_exposes_the_changed_files_panel(client: TestClient) -> None:
    data = client.get("/api/work/session/claude/work-sess-0001").json()

    assert data["files"], "Edit 호출이 있는 세션은 파일 패널이 비면 안 된다"
    first = data["files"][0]
    assert first["path"] == "static/chart-page.js"
    assert first["change_count"] == 1
    assert first["changes"][0]["turn"] == 0
    assert first["changes"][0]["hunks"], "hunk 본문이 있어야 한다"


def test_claude_detail_diff_stat_matches_the_files(client: TestClient) -> None:
    data = client.get("/api/work/session/claude/work-sess-0001").json()

    assert data["diff_stat"]["files"] == len(data["files"])
    assert data["diff_stat"]["additions"] == sum(
        f["additions"] for f in data["files"]
    )
    assert data["diff_stat"]["truncated"] is False


def test_opencode_detail_always_carries_the_new_keys(client: TestClient) -> None:
    data = client.get("/api/work/session/opencode/oc-work-0001").json()

    assert isinstance(data["files"], list)
    assert set(data["diff_stat"]) == {
        "files",
        "additions",
        "deletions",
        "truncated",
    }


def test_file_index_never_points_outside_the_files_list(client: TestClient) -> None:
    data = client.get("/api/work/session/claude/work-sess-0001").json()

    for turn in data["turns"]:
        for action in turn["actions"]:
            if "file_index" in action:
                assert 0 <= action["file_index"] < len(data["files"])


# ── Fix E: 잘린 턴 범위 밖의 변경 점프 버튼이 누락되어야 한다 (JS 렌더러) ──
# JS 렌더러 테스트는 domcheck-diff.mjs에서 수행하므로 여기서는 데이터 계층을 검증한다.
# 특히 turnCount 한도(TURNS_MAX)가 실제로 turn_index를 제한하는지 확인한다.

def test_changes_have_turn_index_within_the_returned_turn_count(
    client: TestClient,
) -> None:
    """각 change.turn 이 반환된 turns 길이 미만이어야 한다 — 정상 세션의 범위 검증."""
    data = client.get("/api/work/session/claude/work-sess-0001").json()
    turn_count = len(data["turns"])

    for f in data["files"]:
        for change in f["changes"]:
            assert change["turn"] < turn_count, (
                f"change.turn={change['turn']} >= turn_count={turn_count}: "
                "점프 버튼 대상 앵커가 없다"
            )


# ── Fix D: diff_stat.truncated이 hunk 줄 수 상한도 포함해야 한다 ──

def test_diff_stat_truncated_is_true_when_all_changes_hit_hunk_line_cap(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """모든 파일이 hunk 줄 수 상한에 걸렸을 때 diff_stat.truncated가 참이어야 한다."""
    import app.main as main_mod
    from app.sources.diffs import DIFF_HUNK_LINES_MAX, Hunk, make_change

    fat_lines = ["+x"] * (DIFF_HUNK_LINES_MAX + 5)
    fat_change = make_change(
        raw_path="big.py",
        tool="Write",
        turn=0,
        hunks=[Hunk(header="@@ 전체 @@", lines=fat_lines)],
        additions=len(fat_lines),
        deletions=0,
    )

    def fake_parse(lines, rel):
        return [], [fat_change], []

    monkeypatch.setattr(main_mod, "parse_session_lines", fake_parse)
    data = client.get("/api/work/session/claude/work-sess-0001").json()

    assert data["diff_stat"]["truncated"] is True


DIFF_BUILD_FAILED_FRAGMENT = "diff 조립에 실패"


def test_diff_assembly_failure_keeps_200_and_reports_the_cause(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """diff 조립이 터져도 턴 타임라인은 살아 있어야 한다 — 500으로 전부 잃지 않는다."""
    import app.main as main

    def _boom(changes):
        raise ValueError("synthetic build_files failure")

    monkeypatch.setattr(main, "build_files", _boom)

    response = client.get("/api/work/session/claude/work-sess-0001")

    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"session", "turns", "files", "diff_stat", "warnings"}
    assert len(data["turns"]) == 2, "턴은 그대로 남아야 한다"
    assert data["files"] == []
    assert data["diff_stat"] == {
        "files": 0,
        "additions": 0,
        "deletions": 0,
        "truncated": False,
    }
    assert any(DIFF_BUILD_FAILED_FRAGMENT in w for w in data["warnings"])
    assert any("ValueError" in w for w in data["warnings"])


def test_file_index_attachment_failure_keeps_200_and_reports_the_cause(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """attach_file_index가 터지는 경우도 같은 경로로 강등돼야 한다."""
    import app.main as main

    def _boom(turns, index_map):
        raise KeyError("synthetic attach_file_index failure")

    monkeypatch.setattr(main, "attach_file_index", _boom)

    response = client.get("/api/work/session/claude/work-sess-0001")

    assert response.status_code == 200
    data = response.json()
    assert len(data["turns"]) == 2
    assert data["files"] == []
    assert any(DIFF_BUILD_FAILED_FRAGMENT in w for w in data["warnings"])
    assert any("KeyError" in w for w in data["warnings"])
    assert all("change_pos" not in a for t in data["turns"] for a in t["actions"])

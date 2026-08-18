"""Tests for app/sources/opencode_transcript.py (DB → 세션 인덱스·메타·턴)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.sources.opencode_transcript import session_index, session_meta, session_turns
from app.sources.transcript_common import NO_TEXT_INSTRUCTION

FIXTURE_DB = Path(__file__).resolve().parent / "fixtures/opencode.db"


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE session (id text PRIMARY KEY, parent_id text, title text, "
        "directory text, cost real, model text, agent text, time_created integer, "
        "time_updated integer)"
    )
    conn.execute(
        "CREATE TABLE message (id text PRIMARY KEY, session_id text, "
        "time_created integer, time_updated integer, data text)"
    )
    conn.execute(
        "CREATE TABLE part (id text PRIMARY KEY, message_id text, session_id text, "
        "time_created integer, time_updated integer, data text)"
    )
    conn.commit()
    conn.close()
    return db


def test_session_index_maps_id_to_title_and_parent() -> None:
    index, _ = session_index(FIXTURE_DB)
    assert index["oc-work-0001"] == {"title": "배포 설정 정리", "parent_id": None}


def test_session_index_warns_for_a_missing_db(tmp_path: Path) -> None:
    index, warnings = session_index(tmp_path / "nope.db")
    assert index == {}
    assert len(warnings) == 1


def test_session_index_warns_when_the_session_table_is_absent(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE message (id text, session_id text, time_created integer, data text)"
    )
    conn.commit()
    conn.close()
    index, warnings = session_index(db)
    assert index == {}
    assert any("session" in w for w in warnings)


def test_session_meta_returns_the_header_fields() -> None:
    meta, _ = session_meta(FIXTURE_DB, "oc-work-0001")
    assert meta == {
        "id": "oc-work-0001",
        "source": "opencode",
        "title": "배포 설정 정리",
        "project": "proj_oc",
        "phase": None,
        "phase_slug": None,
        "started_at": 1753174800000,
        "ended_at": 1753175400000,
        "cost_usd": 0.42,
        "models": ["gemini-3-pro"],
        "agent": "build",
        "is_subagent": False,
    }


def test_session_meta_returns_none_for_an_unknown_id() -> None:
    meta, _ = session_meta(FIXTURE_DB, "no-such-session")
    assert meta is None


def test_session_turns_builds_two_turns_from_the_fixture() -> None:
    turns, _, _ = session_turns(FIXTURE_DB, "oc-work-0001")
    assert turns is not None
    assert len(turns) == 2
    assert turns[0]["instruction"] == "compose 파일 정리해줘"
    assert turns[0]["reasoning"] == ["오버레이 구조로 나누는 것이 좋겠다."]
    # edit 액션에는 change_pos가 붙으므로 부분 일치만 확인
    assert turns[0]["actions"][0]["tool"] == "edit"
    assert turns[0]["actions"][0]["target"] == "infra/compose.yml"
    assert turns[0]["response"] == "오버레이 파일로 분리했습니다."
    assert turns[1]["instruction"] == "env 파일도 정리해줘"
    assert turns[1]["response"] == ".env.example을 갱신했습니다."


def test_session_turns_returns_none_for_an_unknown_session() -> None:
    turns, _, _ = session_turns(FIXTURE_DB, "no-such-session")
    assert turns is None


def test_malformed_part_data_is_reported_not_silently_skipped(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO message VALUES ('m1','s1',1000,1000,'{\"role\":\"assistant\"}')")
    conn.execute("INSERT INTO part VALUES ('p1','m1','s1',1000,1000,'{broken json')")
    conn.commit()
    conn.close()
    turns, _, warnings = session_turns(db, "s1")
    assert turns is not None
    assert any("p1" in w for w in warnings)  # 손상 part가 경고로 드러난다


def test_session_meta_tolerates_null_timestamps(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO session VALUES ('s1', NULL, '제목', '/anon/p', 0.1, NULL, NULL, NULL, NULL)"
    )
    conn.commit()
    conn.close()
    meta, _ = session_meta(db, "s1")
    assert meta is not None
    assert meta["started_at"] is None
    assert meta["ended_at"] is None


def test_patch_files_with_non_string_items_does_not_crash(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO message VALUES ('m1','s1',1000,1000,'{\"role\":\"assistant\"}')")
    conn.execute(
        "INSERT INTO part VALUES ('p1','m1','s1',1000,1000,"
        "'{\"type\":\"patch\",\"files\":[1,\"a.py\",null]}')"
    )
    conn.commit()
    conn.close()
    turns, _, _ = session_turns(db, "s1")
    assert turns is not None
    assert turns[0]["actions"] == [{"tool": "patch", "target": "a.py"}]


def test_non_object_message_data_is_reported(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO message VALUES ('m1','s1',1000,1000,'5')")
    conn.commit()
    conn.close()
    turns, _, warnings = session_turns(db, "s1")
    assert turns is not None
    assert any("m1" in w and "not a JSON object" in w for w in warnings)


def test_non_object_part_data_is_reported(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO message VALUES ('m1','s1',1000,1000,'{\"role\":\"assistant\"}')")
    conn.execute("INSERT INTO part VALUES ('p1','m1','s1',1000,1000,'5')")
    conn.commit()
    conn.close()
    turns, _, warnings = session_turns(db, "s1")
    assert turns is not None
    assert any("p1" in w and "not a JSON object" in w for w in warnings)


def test_unknown_role_is_reported_once(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO message VALUES ('m1','s1',1000,1000,'{\"role\":\"system\"}')")
    conn.execute("INSERT INTO message VALUES ('m2','s1',2000,2000,'{\"no_role\":1}')")
    conn.commit()
    conn.close()
    turns, _, warnings = session_turns(db, "s1")
    assert turns is not None
    role_warnings = [w for w in warnings if "role" in w]
    assert len(role_warnings) == 1
    assert "system" in role_warnings[0] and "None" in role_warnings[0]


def test_unknown_part_types_are_reported_and_step_markers_are_not(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO message VALUES ('m1','s1',1000,1000,'{\"role\":\"assistant\"}')")
    for i, ptype in enumerate(["step-start", "step-finish", "mystery"]):
        conn.execute(
            "INSERT INTO part VALUES (?,'m1','s1',1000,1000,?)",
            (f"p{i}", f'{{"type":"{ptype}"}}'),
        )
    conn.commit()
    conn.close()
    turns, _, warnings = session_turns(db, "s1")
    assert turns is not None
    assert any("mystery" in w for w in warnings)          # 미지 타입은 경고
    assert not any("step-start" in w for w in warnings)   # 알려진 스캐폴딩은 무경고


def test_text_less_user_message_keeps_the_turn_boundary(tmp_path: Path) -> None:
    """텍스트 없는 user 메시지가 사라지면 다음 응답이 직전 턴에 오귀속된다 (Task 3 🔴 쌍둥이)."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO message VALUES ('u1','s1',1000,1000,'{\"role\":\"user\"}')"
    )
    conn.execute(
        "INSERT INTO part VALUES ('pu1','u1','s1',1000,1000,"
        "'{\"type\":\"text\",\"text\":\"첫 지시\"}')"
    )
    conn.execute(
        "INSERT INTO message VALUES ('a1','s1',2000,2000,'{\"role\":\"assistant\"}')"
    )
    conn.execute(
        "INSERT INTO part VALUES ('pa1','a1','s1',2000,2000,"
        "'{\"type\":\"text\",\"text\":\"첫 응답\"}')"
    )
    conn.execute(
        "INSERT INTO message VALUES ('u2','s1',3000,3000,'{\"role\":\"user\"}')"
    )
    conn.execute(
        "INSERT INTO part VALUES ('pu2','u2','s1',3000,3000,"
        "'{\"type\":\"file\",\"name\":\"x.png\"}')"
    )
    conn.execute(
        "INSERT INTO message VALUES ('a2','s1',4000,4000,'{\"role\":\"assistant\"}')"
    )
    conn.execute(
        "INSERT INTO part VALUES ('pa2','a2','s1',4000,4000,"
        "'{\"type\":\"text\",\"text\":\"둘째 응답\"}')"
    )
    conn.commit()
    conn.close()
    turns, _, _ = session_turns(db, "s1")
    assert len(turns) == 2
    assert turns[0]["response"] == "첫 응답"          # 오귀속 없음
    assert turns[1]["instruction"] == NO_TEXT_INSTRUCTION
    assert turns[1]["response"] == "둘째 응답"


def test_turns_max_truncation_warning_survives_session_turns(tmp_path: Path) -> None:
    from app.sources.transcript_common import TURNS_MAX

    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    for i in range(TURNS_MAX + 3):
        conn.execute(
            "INSERT INTO message VALUES (?,'s1',?,?,'{\"role\":\"user\"}')",
            (f"m{i:04d}", 1000 + i, 1000 + i),
        )
        conn.execute(
            "INSERT INTO part VALUES (?,?,'s1',?,?,?)",
            (f"p{i:04d}", f"m{i:04d}", 1000 + i, 1000 + i,
             f'{{"type":"text","text":"지시 {i}"}}'),
        )
    conn.commit()
    conn.close()
    turns, _, warnings = session_turns(db, "s1")
    assert len(turns) == TURNS_MAX
    assert any(str(TURNS_MAX) in w for w in warnings)


def test_session_turns_reports_a_file_tool_without_diff_material(fixtures_dir):
    turns, changes, warnings = session_turns(
        fixtures_dir / "opencode.db", "oc-work-0001"
    )

    assert turns is not None
    assert [c.path for c in changes] == ["infra/compose.yml"]
    assert changes[0].hunks == ()
    assert any("infra/compose.yml" in w for w in warnings)


def test_filediff_patch_path_is_wired_through_session_turns(tmp_path: Path) -> None:
    """session_turns → from_opencode_part 배선 회귀 테스트.

    filediff.patch 경로가 실제로 통과되는지 검증한다.
    part 딕셔너리를 opencode_transcript가 올바른 중첩 구조로 전달하지 않으면
    hunks가 비어 있고 "diff를 표시할 수 없습니다" 경고가 나타나지만,
    기존 단위 테스트는 모두 통과한다 — 이 테스트가 그 무음 퇴행을 잡는다.
    """
    patch = (
        "--- a/app/config.py\n"
        "+++ b/app/config.py\n"
        "@@ -1,3 +1,4 @@\n"
        " import os\n"
        "-DEBUG = False\n"
        "+DEBUG = True\n"
        "+LOG_LEVEL = 'INFO'\n"
        " \n"
    )
    part_data = {
        "type": "tool",
        "tool": "edit",
        "state": {
            "input": {
                "filePath": "app/config.py",
            },
            "metadata": {
                "filediff": {
                    "file": "app/config.py",
                    "patch": patch,
                    "additions": 2,
                    "deletions": 1,
                },
            },
        },
    }

    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    import json as _json

    conn.execute(
        "INSERT INTO session VALUES ('s1', NULL, '설정 수정', '/proj', 0.0, NULL, NULL, 1000, 2000)"
    )
    conn.execute(
        "INSERT INTO message VALUES ('u1','s1',1000,1000,'{\"role\":\"user\"}')"
    )
    conn.execute(
        "INSERT INTO part VALUES ('pu1','u1','s1',1000,1000,"
        "'{\"type\":\"text\",\"text\":\"config 수정해줘\"}')"
    )
    conn.execute(
        "INSERT INTO message VALUES ('a1','s1',2000,2000,'{\"role\":\"assistant\"}')"
    )
    conn.execute(
        "INSERT INTO part VALUES ('pa1','a1','s1',2000,2000,?)",
        (_json.dumps(part_data),),
    )
    conn.commit()
    conn.close()

    turns, changes, warnings = session_turns(db, "s1")

    assert turns is not None
    assert len(changes) == 1

    fc = changes[0]
    assert fc.path == "app/config.py"
    assert fc.additions == 2
    assert fc.deletions == 1
    assert fc.hunks, (
        "filediff.patch 경로가 session_turns를 통과하지 못했습니다 — "
        "opencode_transcript.py의 part 딕셔너리 전달 배선을 확인하세요"
    )
    assert not any("diff를 표시할 수 없습니다" in w for w in warnings), (
        "fallback 경고가 발생했습니다 — filediff.patch 경로가 취해지지 않았습니다"
    )

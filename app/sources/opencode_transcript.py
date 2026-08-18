"""opencode SQLite → 작업 브라우저용 세션 인덱스·메타·턴 타임라인.

DB 연결은 opencode_db._connect(read-only + immutable 폴백)를 그대로 재사용한다.
session/part 테이블이 없는 오래된 DB는 경고와 함께 빈 결과를 낸다 (죽지 않는다).
part.data 손상은 경고로 드러낸다 — Phase 11 Task 1의 무검출 손상 실측 대응.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.sources.diff_adapters import from_opencode_part
from app.sources.diffs import FileChange
from app.sources.opencode_db import _connect
from app.sources.transcript_common import (
    NO_TEXT_INSTRUCTION,
    TurnBuilder,
    first_str_value,
    project_phase_from_cwd,
    text_from_content,
)

_PART_TARGET_KEYS = ("file_path", "filePath", "command", "name", "description")

# 실 DB에 대량 존재하는 알려진 스캐폴딩 part 타입 — 콘텐츠가 아니라 경고 대상 아님
KNOWN_SKIPPED_PART_TYPES = frozenset({"step-start", "step-finish"})


def session_index(db_path: Path) -> tuple[dict[str, dict], list[str]]:
    """목록용 ``{session_id: {"title", "parent_id"}}``."""
    if not db_path.exists():
        return {}, [f"Database does not exist: {db_path}"]
    conn, warnings = _connect(db_path)
    if conn is None:
        return {}, warnings
    try:
        rows = conn.execute("SELECT id, title, parent_id FROM session").fetchall()
    except sqlite3.OperationalError as exc:
        warnings.append(f"opencode session 테이블을 읽지 못했습니다: {exc}")
        return {}, warnings
    finally:
        conn.close()
    return (
        {row[0]: {"title": row[1], "parent_id": row[2]} for row in rows},
        warnings,
    )


def session_meta(db_path: Path, session_id: str) -> tuple[dict | None, list[str]]:
    """상세 헤더 메타. 세션 행이 없으면 ``(None, warnings)``."""
    if not db_path.exists():
        return None, [f"Database does not exist: {db_path}"]
    conn, warnings = _connect(db_path)
    if conn is None:
        return None, warnings
    try:
        row = conn.execute(
            "SELECT id, parent_id, title, directory, cost, model, agent, "
            "time_created, time_updated FROM session WHERE id = ?",
            (session_id,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        warnings.append(f"opencode session 테이블을 읽지 못했습니다: {exc}")
        return None, warnings
    finally:
        conn.close()
    if row is None:
        return None, warnings
    (_, parent_id, title, directory, cost, model, agent, created, updated) = row
    project, phase, phase_slug = project_phase_from_cwd(directory)
    return {
        "id": session_id,
        "source": "opencode",
        "title": title,
        "project": project,
        "phase": phase,
        "phase_slug": phase_slug,
        "started_at": int(created) if created is not None else None,
        "ended_at": int(updated) if updated is not None else None,
        "cost_usd": float(cost or 0),
        "models": [model] if model else [],
        "agent": agent,
        "is_subagent": parent_id is not None,
    }, warnings


def _part_target(part: dict) -> str:
    state = part.get("state")
    tool_input = state.get("input") if isinstance(state, dict) else None
    return first_str_value(tool_input, _PART_TARGET_KEYS)


def session_turns(
    db_path: Path, session_id: str
) -> tuple[list[dict] | None, list[FileChange], list[str]]:
    """세션의 턴 타임라인. 메시지가 하나도 없으면 ``(None, [], warnings)`` — 404 신호."""
    if not db_path.exists():
        return None, [], [f"Database does not exist: {db_path}"]
    conn, warnings = _connect(db_path)
    if conn is None:
        return None, [], warnings
    try:
        messages = conn.execute(
            "SELECT id, time_created, data FROM message "
            "WHERE session_id = ? ORDER BY time_created, id",
            (session_id,),
        ).fetchall()
        try:
            part_rows = conn.execute(
                "SELECT id, message_id, data FROM part "
                "WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            part_rows = []
            warnings.append(f"opencode part 테이블을 읽지 못했습니다: {exc}")
    except sqlite3.OperationalError as exc:
        warnings.append(f"opencode message 테이블을 읽지 못했습니다: {exc}")
        return None, [], warnings
    finally:
        conn.close()

    if not messages:
        return None, [], warnings

    parts_by_message: dict[str, list[dict]] = {}
    for part_id, message_id, data_json in part_rows:
        try:
            data = json.loads(data_json)
        except (json.JSONDecodeError, TypeError) as exc:
            # 무검출 유실 금지 — 손상 part는 경고로 드러낸다 (Task 1 실측 대응)
            warnings.append(f"{db_path}:part:{part_id}: malformed data JSON: {exc}")
            continue
        if not isinstance(data, dict):
            warnings.append(
                f"{db_path}:part:{part_id}: data is not a JSON object "
                f"(type={type(data).__name__})"
            )
            continue
        parts_by_message.setdefault(message_id, []).append(data)

    builder = TurnBuilder()
    unknown_roles: set[str] = set()
    unknown_part_types: set[str] = set()
    for message_id, time_created, data_json in messages:
        try:
            msg = json.loads(data_json)
        except (json.JSONDecodeError, TypeError) as exc:
            warnings.append(f"{db_path}:{message_id}: malformed data JSON: {exc}")
            continue
        if not isinstance(msg, dict):
            warnings.append(
                f"{db_path}:{message_id}: data is not a JSON object "
                f"(type={type(msg).__name__})"
            )
            continue
        role = msg.get("role")
        parts = parts_by_message.get(message_id, [])
        ts = int(time_created) if time_created else None
        if role == "user":
            # part 리스트는 content 블록과 동형({"type","text"}) — 정본 헬퍼 재사용
            text = text_from_content(parts).strip()
            if text:
                builder.start_turn(ts, text)
            elif parts:
                # 텍스트 없는 첨부성 user 메시지 — 경계를 유실하면 다음 응답이
                # 직전 턴에 오귀속된다 (Task 3 🔴의 쌍둥이). 플레이스홀더로 유지.
                builder.start_turn(ts, NO_TEXT_INSTRUCTION)
        elif role == "assistant":
            builder.ensure_turn(ts)
            for part in parts:
                part_type = part.get("type")
                if part_type == "reasoning":
                    builder.add_reasoning(part.get("text") or "")
                elif part_type == "text":
                    builder.add_response(part.get("text") or "")
                elif part_type == "tool":
                    change_list, change_warnings = from_opencode_part(
                        part, builder.turn_index
                    )
                    warnings.extend(change_warnings)
                    builder.add_action(
                        part.get("tool") or "(unknown)",
                        _part_target(part),
                        changes=change_list or None,
                    )
                elif part_type == "patch":
                    files = part.get("files")
                    target = (
                        ", ".join(f for f in files if isinstance(f, str))
                        if isinstance(files, list)
                        else ""
                    )
                    builder.add_action("patch", target)
                elif part_type not in KNOWN_SKIPPED_PART_TYPES:
                    unknown_part_types.add(str(part_type))
        else:
            unknown_roles.add(str(role))
    if unknown_roles:
        warnings.append(
            f"{db_path}: 알 수 없는 message role을 건너뜀: {sorted(unknown_roles)}"
        )
    if unknown_part_types:
        warnings.append(
            f"{db_path}: 알 수 없는 part 타입을 건너뜀: {sorted(unknown_part_types)}"
        )
    turns, finish_warnings = builder.finish()
    return turns, builder.changes, warnings + finish_warnings

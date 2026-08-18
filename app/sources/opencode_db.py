"""Read opencode SQLite ``message`` table into normalized :class:`Record` rows.

The opencode database is opened **read-only** (``file:<path>?mode=ro`` URI)
with ``busy_timeout = 5000`` ms. This module never writes to the database.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.sources.claude_jsonl import Record


def _safe_int(val: object, default: int = 0) -> int:
    """Convert to int; return *default* on failure."""
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _ms_to_utc(ms: int) -> datetime:
    """Convert a unix-millisecond timestamp to an aware UTC datetime."""
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def _project_from_cwd(cwd: str) -> str:
    """Return the last path segment of *cwd*."""
    return Path(cwd).name


def _extract_model(data: dict) -> str | None:
    """Return model ID from top-level ``modelID`` or nested ``model.modelID``."""
    model: str | None = data.get("modelID")
    if not model:
        model_obj = data.get("model")
        if isinstance(model_obj, dict):
            model = model_obj.get("modelID")
    return model


def _extract_tokens(
    tokens_obj: object,
    source_file: str,
    row_id: str,
) -> tuple[dict[str, int] | None, str | None]:
    """Extract token counts from a *tokens* JSON object.

    Returns ``(token_dict, None)`` on success or ``(None, warning)`` on failure.
    """
    if not isinstance(tokens_obj, dict):
        return None, (
            f"{source_file}:{row_id}: 'tokens' is not a dict "
            f"(type={type(tokens_obj).__name__})"
        )
    cache = tokens_obj.get("cache") or {}
    return {
        "input": _safe_int(tokens_obj.get("input", 0)),
        "output": _safe_int(tokens_obj.get("output", 0)),
        "cache_read": _safe_int(cache.get("read", 0)),
        "cache_write": _safe_int(cache.get("write", 0)),
    }, None


def _extract_timestamp(
    data: dict,
    time_created: int,
    source_file: str,
    row_id: str,
) -> tuple[datetime, list[str]]:
    """Return ``(utc_datetime, warnings)`` from *data.time* or *time_created*."""
    warnings: list[str] = []
    time_obj = data.get("time")
    if time_obj is not None and not isinstance(time_obj, dict):
        warnings.append(
            f"{source_file}:{row_id}: unexpected 'time' type "
            f"{type(time_obj).__name__}, using time_created column"
        )
    if isinstance(time_obj, dict) and time_obj.get("created"):
        ts_ms = _safe_int(time_obj["created"])
    else:
        ts_ms = _safe_int(time_created)
    return _ms_to_utc(ts_ms), warnings


def _parse_row(  # noqa: PLR0911
    row_id: str, session_id: str, time_created: int,
    data_json: str, source_file: str,
) -> tuple[Record | None, list[str]]:
    """Parse a ``message`` row → ``(record, [])`` or ``(None, [warn, …])``."""
    try:
        data = json.loads(data_json)
    except (json.JSONDecodeError, TypeError) as exc:
        return None, [f"{source_file}:{row_id}: malformed data JSON: {exc}"]
    if not isinstance(data, dict):
        return None, [f"{source_file}:{row_id}: data is not a JSON object"]

    # Expected non-assistant records (e.g. user prompts) are silently skipped.
    # opencode schema: role ∈ {"user", "assistant"}; only assistant rows carry
    # tokens/path. If role is absent entirely, fall through to required-field
    # checks so schema drift is still surfaced as a warning.
    role = data.get("role")
    if role is not None and role != "assistant":
        return None, []

    model = _extract_model(data)
    agent: str | None = data.get("agent")
    tokens_obj = data.get("tokens")
    cwd = (data.get("path") or {}).get("cwd")

    missing = [f for f, v in (
        ("model", model), ("agent", agent),
        ("tokens", tokens_obj if isinstance(tokens_obj, dict) else None),
        ("path.cwd", cwd),
    ) if not v]
    if missing:
        return None, [
            f"{source_file}:{row_id}: Missing required field(s): "
            f"{', '.join(missing)}"
        ]

    # tokens_obj guaranteed dict by required-fields check above
    token_counts, _ = _extract_tokens(tokens_obj, source_file, row_id)

    timestamp, ts_warns = _extract_timestamp(data, time_created, source_file, row_id)
    return Record(
        project=_project_from_cwd(cwd), model=model, timestamp=timestamp,
        input_tokens=token_counts["input"], output_tokens=token_counts["output"],
        cache_read_tokens=token_counts["cache_read"],
        cache_write_tokens=token_counts["cache_write"],
        session_id=session_id, source_file=source_file,
        source="opencode", agent=agent, cwd=cwd,
    ), ts_warns


def _connect(db_path: Path) -> tuple[sqlite3.Connection | None, list[str]]:
    """Open the opencode DB read-only, preferring live-WAL visibility.

    ``immutable=1`` makes SQLite ignore the ``-wal`` file entirely, so recent
    writes stay invisible until a checkpoint. We therefore try plain
    ``mode=ro`` first and only fall back to ``immutable=1`` when the
    filesystem refuses it (e.g. a single-file ``:ro`` bind mount without the
    sidecar ``-wal`` / ``-shm`` files).
    """
    warnings: list[str] = []
    live: sqlite3.Connection | None = None
    try:
        live = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        live.execute("PRAGMA busy_timeout = 5000")
        live.execute("SELECT 1 FROM message LIMIT 1").fetchone()
        return live, warnings
    except sqlite3.OperationalError:
        # 첫 시도가 실패하면 열린 핸들을 확실히 닫고 immutable로 폴백한다.
        if live is not None:
            live.close()

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
        conn.execute("PRAGMA busy_timeout = 5000")
    except sqlite3.OperationalError as exc:
        return None, [f"Failed to open database {db_path}: {exc}"]

    warnings.append(
        f"opencode DB를 immutable 모드로 열었습니다 ({db_path}) — "
        "체크포인트 전 최근 쓰기가 보이지 않을 수 있습니다. "
        "docker-compose에서 opencode 디렉터리를 :ro 마운트하면 해소됩니다."
    )
    return conn, warnings


def read_records(
    db_path: Path,
) -> tuple[list[Record], list[str]]:
    """Read normalized :class:`Record` rows from an opencode SQLite DB.

    Parameters
    ----------
    db_path:
        Path to the opencode SQLite database.

    Returns
    -------
    tuple of (records, warnings)
        ``records`` is a list of :class:`Record` instances. ``warnings`` is a
        list of human-readable strings for skipped rows or errors.
    """
    records: list[Record] = []
    warnings: list[str] = []

    if not db_path.exists():
        warnings.append(f"Database does not exist: {db_path}")
        return records, warnings

    source_file = str(db_path)

    conn, connect_warnings = _connect(db_path)
    warnings.extend(connect_warnings)
    if conn is None:
        return records, warnings

    try:
        rows = conn.execute(
            "SELECT id, session_id, time_created, data FROM message"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        warnings.append(f"Failed to query message table: {exc}")
        return records, warnings
    finally:
        conn.close()

    for row_id, session_id, time_created, data_json in rows:
        rec, warns = _parse_row(
            row_id, session_id, time_created, data_json, source_file
        )
        warnings.extend(warns)
        if rec is not None:
            records.append(rec)

    return records, warnings

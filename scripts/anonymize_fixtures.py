"""Anonymize Claude Code JSONL and opencode SQLite fixtures for testing.

Reads from the user's real data directories (read-only) and writes anonymized
copies to the project's tests/fixtures/ directory.

Usage:
    python scripts/anonymize_fixtures.py \
        --claude-source ~/.claude/projects \
        --opencode-source ~/.local/share/opencode/opencode.db \
        --out tests/fixtures/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256_prefix(value: str, length: int = 8) -> str:
    """Return the first *length* hex chars of the SHA-256 of *value*."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _anon_path(cwd: str) -> str:
    """Replace an absolute path with /anon/<sha256 prefix>."""
    return f"/anon/{_sha256_prefix(cwd)}"


def _anon_id() -> str:
    """Generate a fresh UUID4 string."""
    return str(uuid.uuid4())


def _project_hash(project_dir_name: str) -> str:
    """Hash a project directory name to an anonymous label."""
    return f"proj_{_sha256_prefix(project_dir_name)}"


# ---------------------------------------------------------------------------
# JSONL anonymization
# ---------------------------------------------------------------------------

# 최상위에서 보존할 필드
_PRESERVE_TOP = {"type", "timestamp", "isSidechain"}
# message 안에서 보존할 필드
_PRESERVE_MESSAGE = {"role", "model"}
# usage 안에서 보존할 필드
_PRESERVE_USAGE = {
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
}


def _process_jsonl_line(
    record: dict[str, Any],
    project_hash: str,
) -> dict[str, Any] | None:
    """실제 Claude JSONL 구조(message 중첩)를 보존한 채 익명화한다.

    model/usage를 가진 assistant 메시지만 남긴다.
    """
    msg = record.get("message")
    if not isinstance(msg, dict):
        return None

    model = msg.get("model")
    raw_usage = msg.get("usage")
    if not model or not isinstance(raw_usage, dict):
        return None

    anon: dict[str, Any] = {
        k: record.get(k) for k in _PRESERVE_TOP if k in record
    }
    anon.setdefault("type", "assistant")
    anon.setdefault("timestamp", "")

    anon_msg: dict[str, Any] = {
        k: msg.get(k) for k in _PRESERVE_MESSAGE if k in msg
    }
    anon_msg.setdefault("role", "assistant")
    anon_msg["model"] = model
    anon_msg["usage"] = {k: raw_usage.get(k, 0) for k in _PRESERVE_USAGE}

    anon["message"] = anon_msg
    anon["cwd"] = _anon_path(record.get("cwd", "/unknown"))
    anon["sessionId"] = _anon_id()
    return anon


def _collect_jsonl_files(
    source_dir: Path,
    max_projects: int,
) -> tuple[list[Path], list[str]]:
    """Return (project_dirs, warnings) for up to *max_projects* projects."""
    warnings: list[str] = []
    if not source_dir.is_dir():
        warnings.append(f"Claude source directory not found: {source_dir}")
        return [], warnings

    project_dirs = sorted(
        d for d in source_dir.iterdir() if d.is_dir()
    )[:max_projects]

    if not project_dirs:
        warnings.append(f"No project directories found under {source_dir}")
    return project_dirs, warnings


def _write_anonymized_project(
    proj_dir: Path,
    claude_out: Path,
    max_lines_per_session: int,
) -> list[str]:
    """Anonymize one project directory, preserving its directory layout."""
    warnings: list[str] = []
    proj_hash = _project_hash(proj_dir.name)
    out_proj_dir = claude_out / proj_hash

    for jsonl_file in sorted(proj_dir.rglob("*.jsonl")):
        # 원본의 프로젝트 상대 경로를 유지하되 파일명만 익명화한다.
        rel_parent = jsonl_file.parent.relative_to(proj_dir)
        out_dir = out_proj_dir / rel_parent
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{_sha256_prefix(jsonl_file.stem)}.jsonl"

        out_lines: list[dict[str, Any]] = []
        with open(jsonl_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    warnings.append(f"JSONL parse error in {jsonl_file.name}: {exc}")
                    continue
                anon = _process_jsonl_line(record, proj_hash)
                if anon is None:
                    continue
                out_lines.append(anon)
                if len(out_lines) >= max_lines_per_session:
                    break

        if out_lines:
            with open(out_path, "w", encoding="utf-8") as out_fh:
                for anon in out_lines:
                    out_fh.write(json.dumps(anon, ensure_ascii=False) + "\n")

    return warnings


def anonymize_claude_jsonl(
    source_dir: Path,
    out_dir: Path,
    max_projects: int = 3,
    max_lines_per_session: int = 5,
) -> list[str]:
    """Anonymize Claude Code JSONL sessions.

    Walks *source_dir* (``~/.claude/projects``), picks up to *max_projects*
    project directories, and for each project extracts up to
    *max_lines_per_session* metric-bearing lines from each session JSONL.

    Source files are opened read-only. Output is written to
    ``out_dir / claude_projects /``.

    Returns a list of warnings (empty on full success).
    """
    warnings: list[str] = []
    claude_out = out_dir / "claude_projects"
    claude_out.mkdir(parents=True, exist_ok=True)

    project_dirs, collect_warnings = _collect_jsonl_files(source_dir, max_projects)
    warnings.extend(collect_warnings)

    for proj_dir in project_dirs:
        proj_warnings = _write_anonymized_project(
            proj_dir, claude_out, max_lines_per_session,
        )
        warnings.extend(proj_warnings)

    return warnings


# ---------------------------------------------------------------------------
# SQLite anonymization
# ---------------------------------------------------------------------------

# Regex for scrubbing /home/<user>/ substrings from JSON text
_HOME_PATH_RE = re.compile(r"/home/[^/\"\\]+/")


def _anonymize_opencode_data(
    data_json: str,
    id_mapping: dict[str, str],
) -> str:
    """Anonymize a single ``message.data`` JSON string.

    Preserves: agent, modelID, providerID, tokens.*, cost, time.
    Anonymizes: path.cwd, path.root, session_id (top-level), text content for
    user/assistant roles.  Also scrubs any remaining ``/home/<user>/``
    substrings from the final JSON text as a safety net.

    The *id_mapping* dict is used to remap ``parentID`` fields inside the
    data JSON so they reference the anonymized message IDs.
    """
    try:
        data = json.loads(data_json)
    except (json.JSONDecodeError, TypeError):
        return data_json

    if not isinstance(data, dict):
        return data_json

    # Anonymize path.cwd and path.root
    path = data.get("path")
    if isinstance(path, dict):
        if "cwd" in path:
            path["cwd"] = _anon_path(str(path["cwd"]))
        if "root" in path:
            path["root"] = _anon_path(str(path["root"]))

    # Redact text content for user/assistant
    role = data.get("role")
    if role in ("user", "assistant") and "content" in data:
        data["content"] = "<redacted>"

    # Redact summary.diffs patches (may contain file paths with user dirs)
    summary = data.get("summary")
    if isinstance(summary, dict) and "diffs" in summary:
        diffs = summary["diffs"]
        if isinstance(diffs, list):
            for diff in diffs:
                if isinstance(diff, dict) and "patch" in diff:
                    diff["patch"] = "<redacted>"

    # Remap parentID if present
    parent_id = data.get("parentID")
    if isinstance(parent_id, str) and parent_id:
        data["parentID"] = _anon_msg_id(parent_id, id_mapping)

    result = json.dumps(data, ensure_ascii=False)

    # Safety net: replace any remaining /home/<user>/ patterns
    return _HOME_PATH_RE.sub("/anon/", result)


def _anon_msg_id(original: str, id_mapping: dict[str, str]) -> str:
    """Return a stable anonymized UUID for an original message ID."""
    if original not in id_mapping:
        id_mapping[original] = str(uuid.uuid4())
    return id_mapping[original]


def _read_source_schema_and_rows(
    src_uri: str,
    max_rows: int,
) -> tuple[list[tuple], list[tuple], list[tuple], list[str]]:
    """Open the source DB once (read-only) and fetch schema, indexes, rows.

    Returns (schema_rows, idx_sqls, rows, warnings).
    """
    warnings: list[str] = []
    with sqlite3.connect(src_uri, uri=True) as src_conn:
        src_conn.execute("PRAGMA busy_timeout = 5000")

        schema_rows = src_conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='message'"
        ).fetchall()
        if not schema_rows:
            warnings.append("'message' table not found in source DB")
            return [], [], [], warnings

        idx_sqls = src_conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND sql IS NOT NULL AND tbl_name='message'"
        ).fetchall()

        rows = src_conn.execute(
            "SELECT id, session_id, time_created, time_updated, data "
            "FROM message LIMIT ?",
            (max_rows,),
        ).fetchall()

    if not rows:
        warnings.append("message table is empty in source DB")
    return schema_rows, idx_sqls, rows, warnings


def _copy_schema(dest_conn: sqlite3.Connection, schema_rows: list[tuple]) -> None:
    """Recreate the message table in the destination DB."""
    dest_conn.execute(schema_rows[0][0])


def _copy_indexes(dest_conn: sqlite3.Connection, idx_sqls: list[tuple]) -> None:
    """Recreate indexes on the message table in the destination DB."""
    for (idx_sql,) in idx_sqls:
        dest_conn.execute(idx_sql)


def _insert_anonymized_rows(
    dest_conn: sqlite3.Connection,
    rows: list[tuple],
    id_mapping: dict[str, str],
) -> None:
    """Insert anonymized rows into the destination message table."""
    for row_id, _session_id, time_created, time_updated, data in rows:
        anon_id = _anon_msg_id(row_id, id_mapping)
        anon_session = _anon_id()
        anon_data = _anonymize_opencode_data(data, id_mapping)
        dest_conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (anon_id, anon_session, time_created, time_updated, anon_data),
        )


def anonymize_opencode_db(
    source_path: Path,
    out_dir: Path,
    max_rows: int = 20,
) -> list[str]:
    """Anonymize the opencode SQLite database.

    Opens the source read-only via URI, copies up to *max_rows* from the
    ``message`` table into a new database at ``out_dir / opencode.db``.
    Message IDs are remapped to UUID4 values; ``data.parentID`` references
    are remapped consistently via the same mapping.

    Returns a list of warnings.
    """
    warnings: list[str] = []
    dest_path = out_dir / "opencode.db"

    if not source_path.is_file():
        warnings.append(f"opencode source DB not found: {source_path}")
        return warnings

    src_uri = f"file:{source_path}?mode=ro"
    schema_rows, idx_sqls, rows, read_warnings = _read_source_schema_and_rows(
        src_uri, max_rows,
    )
    warnings.extend(read_warnings)
    if not schema_rows or not rows:
        return warnings

    # Remove old destination if exists
    if dest_path.exists():
        dest_path.unlink()

    # Stable mapping: original message id → anonymized UUID
    id_mapping: dict[str, str] = {}

    # Create destination DB (no WAL)
    with sqlite3.connect(str(dest_path)) as dest_conn:
        dest_conn.execute("PRAGMA journal_mode = DELETE")
        _copy_schema(dest_conn, schema_rows)
        _copy_indexes(dest_conn, idx_sqls)
        _insert_anonymized_rows(dest_conn, rows, id_mapping)
        dest_conn.commit()

    return warnings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Anonymize Claude Code and opencode fixtures for testing.",
    )
    parser.add_argument(
        "--claude-source",
        type=Path,
        required=True,
        help="Path to ~/.claude/projects directory",
    )
    parser.add_argument(
        "--opencode-source",
        type=Path,
        required=True,
        help="Path to ~/.local/share/opencode/opencode.db",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output directory (typically tests/fixtures/)",
    )
    parser.add_argument(
        "--max-projects",
        type=int,
        default=3,
        help="Maximum number of Claude project directories to anonymize (default: 3)",
    )
    parser.add_argument(
        "--max-lines-per-session",
        type=int,
        default=5,
        help="Maximum metric-bearing lines kept per session JSONL (default: 5)",
    )
    args = parser.parse_args()

    all_warnings: list[str] = []

    # Verify output directory exists
    args.out.mkdir(parents=True, exist_ok=True)

    print(f"Anonymizing Claude JSONL from {args.claude_source} ...")
    w1 = anonymize_claude_jsonl(
        args.claude_source,
        args.out,
        max_projects=args.max_projects,
        max_lines_per_session=args.max_lines_per_session,
    )
    all_warnings.extend(w1)
    for w in w1:
        print(f"  WARNING: {w}", file=sys.stderr)

    print(f"Anonymizing opencode DB from {args.opencode_source} ...")
    w2 = anonymize_opencode_db(args.opencode_source, args.out)
    all_warnings.extend(w2)
    for w in w2:
        print(f"  WARNING: {w}", file=sys.stderr)

    if all_warnings:
        print(f"\n{len(all_warnings)} warning(s) — see above.", file=sys.stderr)
    else:
        print("Done. No warnings.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

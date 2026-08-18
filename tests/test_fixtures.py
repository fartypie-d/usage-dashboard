"""Tests that verify fixture files exist and are properly anonymized."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def test_claude_jsonl_fixture_has_required_fields(fixtures_dir: Path) -> None:
    """At least one .jsonl file under claude_projects/ with required fields per line."""
    claude_dir = fixtures_dir / "claude_projects"
    jsonl_files = list(claude_dir.rglob("*.jsonl"))
    assert len(jsonl_files) >= 1, f"No .jsonl files found in {claude_dir}"

    required_top_fields = {"timestamp"}
    required_message_fields = {"model", "usage"}
    required_usage_fields = {
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    }

    for jsonl_file in jsonl_files:
        line_count = 0
        with open(jsonl_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                line_count += 1

                # 파서(`_parse_line`)와 동일한 기준: type이 있고 assistant가 아니면
                # 토큰 필드를 갖지 않는 정상 라인이므로 형태 검사 대상이 아니다.
                rec_type = record.get("type")
                if rec_type is not None and rec_type != "assistant":
                    continue

                # Check top-level fields
                for field in required_top_fields:
                    assert field in record, (
                        f"Missing top-level field '{field}' in {jsonl_file.name} line {line_count}"
                    )

                # Check message sub-fields (model/usage live under 'message')
                message = record["message"]
                assert isinstance(message, dict), (
                    f"'message' should be a dict in {jsonl_file.name} line {line_count}"
                )
                for mf in required_message_fields:
                    assert mf in message, (
                        f"Missing message field '{mf}' in {jsonl_file.name} line {line_count}"
                    )

                # Check usage sub-fields
                usage = message["usage"]
                assert isinstance(usage, dict), (
                    f"'usage' should be a dict in {jsonl_file.name} line {line_count}"
                )
                for uf in required_usage_fields:
                    assert uf in usage, (
                        f"Missing usage field '{uf}' in {jsonl_file.name} line {line_count}"
                    )

        assert line_count > 0, f"Empty JSONL file: {jsonl_file.name}"


def test_opencode_db_fixture_has_message_table(fixtures_dir: Path) -> None:
    """tests/fixtures/opencode.db must exist, have a 'message' table, and ≥1 row."""
    db_path = fixtures_dir / "opencode.db"
    assert db_path.exists(), f"Fixture DB not found: {db_path}"

    conn = sqlite3.connect(str(db_path))
    try:
        # Check message table exists
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "message" in tables, f"'message' table not found. Tables: {tables}"

        # Check at least 1 row
        count = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
        assert count >= 1, f"message table is empty (count={count})"
    finally:
        conn.close()


def test_fixtures_are_anonymized(fixtures_dir: Path) -> None:
    """No fixture file should contain absolute paths to any developer's home dir.

    Checks for /home/<anyuser>/, /Users/<anyuser>/, and msg_ prefixed
    original message IDs — patterns that would leak real environment data.
    """
    home = Path.home()
    forbidden_prefixes = [
        str(home) + "/",          # e.g. /home/dev/ or /Users/alice/
        "/home/",                 # any /home/<user>/ pattern
        "/Users/",                # any /Users/<user>/ pattern (macOS)
    ]
    # Also check for original opencode message IDs (msg_ prefix, 20+ chars)
    import re
    msg_id_re = re.compile(r"msg_[a-zA-Z0-9]{20,}")

    matches: list[str] = []

    # Collect all fixture files
    all_files = [p for p in fixtures_dir.rglob("*") if p.is_file()]
    assert len(all_files) >= 1, (
        f"No fixture files found under {fixtures_dir} — "
        "run scripts/anonymize_fixtures.py first"
    )

    for file_path in all_files:
        # Check as text
        try:
            text = file_path.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError):
            text = None

        if text is not None:
            for prefix in forbidden_prefixes:
                if prefix in text:
                    matches.append(f"{file_path} (text contains '{prefix}')")
            if msg_id_re.search(text):
                matches.append(f"{file_path} (text contains msg_ ID)")
        else:
            # Check as binary
            raw = file_path.read_bytes()
            for prefix in forbidden_prefixes:
                if prefix.encode("utf-8") in raw:
                    matches.append(f"{file_path} (binary contains '{prefix}')")

    assert not matches, (
        f"Found forbidden patterns in fixture files: {matches}"
    )

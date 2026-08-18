"""Tests for app.sources.opencode_db module."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

from app.sources.claude_jsonl import Record

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
OPENCODE_DB = FIXTURES_DIR / "opencode.db"


def _make_db(path: Path, rows: list[tuple]) -> None:
    """Create a minimal opencode-style SQLite DB with given message rows."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE message (
            id text PRIMARY KEY,
            session_id text NOT NULL,
            time_created integer NOT NULL,
            time_updated integer NOT NULL,
            data text NOT NULL
        )
        """
    )
    conn.executemany(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_read_records_returns_normalized_records() -> None:
    """Fixture DB should yield normalized Record instances."""
    from app.sources.opencode_db import read_records

    records, warnings = read_records(OPENCODE_DB)

    assert isinstance(records, list)
    assert isinstance(warnings, list)
    assert len(records) > 0
    assert all(isinstance(r, Record) for r in records)

    # At least one assistant record with expected shape
    assert any(r.agent == "scheduler" for r in records)
    assert any(r.model == "gemini-3-pro" for r in records)
    assert any(r.input_tokens > 0 for r in records)
    assert any(r.output_tokens >= 0 for r in records)
    assert any(r.session_id for r in records)
    assert any(r.source_file == str(OPENCODE_DB) for r in records)
    # project = last segment of cwd
    assert any(r.project == "d08950cd" for r in records)
    # timestamp must be aware UTC datetime
    assert any(r.timestamp.tzinfo is not None for r in records)


def test_read_records_uses_readonly_uri() -> None:
    """Connection must be opened with read-only URI (mode=ro) and uri=True."""
    from app.sources.opencode_db import read_records

    with patch("app.sources.opencode_db.sqlite3.connect") as mock_connect:
        # Make the mock return a context-managed connection-like object
        mock_conn = mock_connect.return_value
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = lambda s, *a: False

        read_records(OPENCODE_DB)

        mock_connect.assert_called_once()
        args, kwargs = mock_connect.call_args
        # Either the path is passed as first positional arg, or via keyword
        path_arg = args[0] if args else kwargs.get("database") or kwargs.get("uri")
        assert "mode=ro" in str(path_arg), (
            f"read-only URI expected, got {path_arg!r}"
        )
        # uri=True must be set (either positional or keyword)
        assert kwargs.get("uri") is True, (
            f"uri=True required, kwargs={kwargs!r}"
        )


def test_read_records_sets_busy_timeout() -> None:
    """busy_timeout PRAGMA must be set to 5000 ms after connection."""
    from app.sources.opencode_db import read_records

    # Wrap the real connection in a proxy that records ``execute`` calls,
    # since sqlite3.Connection.execute is read-only and cannot be monkeypatched.
    executed_pragmas: list[str] = []
    real_connect = sqlite3.connect

    class _TracingConn:
        def __init__(self, inner: sqlite3.Connection) -> None:
            self._inner = inner

        def execute(self, sql, params=()):
            if "busy_timeout" in str(sql).lower():
                executed_pragmas.append(sql)
            return self._inner.execute(sql, params)

        def close(self) -> None:
            self._inner.close()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self._inner.close()

    def spy_connect(*args, **kwargs):
        return _TracingConn(real_connect(*args, **kwargs))

    with patch("app.sources.opencode_db.sqlite3.connect", side_effect=spy_connect):
        read_records(OPENCODE_DB)

    assert any("5000" in p for p in executed_pragmas), (
        f"PRAGMA busy_timeout = 5000 not observed; saw {executed_pragmas!r}"
    )


def test_read_records_yields_warning_on_malformed_data_json(
    tmp_path: Path,
) -> None:
    """Rows with unparseable JSON data should be skipped with a warning."""
    from app.sources.opencode_db import read_records

    bad_db = tmp_path / "bad.db"
    _make_db(
        bad_db,
        [
            (
                "id-1",
                "sess-1",
                1_700_000_000_000,
                1_700_000_001_000,
                "NOT-JSON{{{",
            ),
        ],
    )

    records, warnings = read_records(bad_db)

    assert records == []
    assert len(warnings) >= 1
    assert any("bad.db" in w or "id-1" in w for w in warnings)


def test_read_records_warns_when_db_missing(tmp_path: Path) -> None:
    """Nonexistent DB path should return empty records and a warning."""
    from app.sources.opencode_db import read_records

    missing = tmp_path / "does_not_exist.db"
    records, warnings = read_records(missing)

    assert records == []
    assert len(warnings) >= 1
    assert any("does_not_exist" in w or "not" in w.lower() for w in warnings)


def test_read_records_never_writes_to_db() -> None:
    """Function must not modify the fixture DB (mtime must be unchanged)."""
    from app.sources.opencode_db import read_records

    before = os.stat(OPENCODE_DB).st_mtime_ns
    records, _ = read_records(OPENCODE_DB)
    after = os.stat(OPENCODE_DB).st_mtime_ns

    assert before == after, "fixture DB mtime changed — write detected"
    assert len(records) > 0  # sanity: we did read something


def _good_data(**overrides: object) -> str:
    """Return a JSON string for a valid assistant message, with overrides."""
    import json

    base = {
        "role": "assistant",
        "agent": "build",
        "modelID": "gemini-3-pro",
        "path": {"cwd": "/anon/testproj"},
        "tokens": {
            "input": 100,
            "output": 50,
            "cache": {"read": 10, "write": 5},
        },
        "time": {"created": 1_700_000_000_000},
    }
    base.update(overrides)
    return json.dumps(base)


def test_read_records_warns_on_missing_cwd(tmp_path: Path) -> None:
    """Rows without path.cwd should be skipped with a warning."""
    from app.sources.opencode_db import read_records

    db = tmp_path / "nocwd.db"
    data_no_cwd = _good_data(path=None)  # no path at all
    _make_db(
        db,
        [("r1", "s1", 1_700_000_000_000, 1_700_000_001_000, data_no_cwd)],
    )

    records, warnings = read_records(db)

    assert records == []
    assert len(warnings) >= 1
    assert any("path.cwd" in w for w in warnings)


def test_read_records_handles_non_numeric_token_gracefully(
    tmp_path: Path,
) -> None:
    """Non-numeric token values should be coerced to 0 (no crash)."""
    from app.sources.opencode_db import read_records

    db = tmp_path / "badtoken.db"
    data_bad_token = _good_data(
        tokens={"input": "N/A", "output": 50, "cache": {"read": 0, "write": 0}},
    )
    _make_db(
        db,
        [("r2", "s2", 1_700_000_000_000, 1_700_000_001_000, data_bad_token)],
    )

    records, warnings = read_records(db)

    # _safe_int should coerce "N/A" → 0, no crash, record still created
    assert len(records) == 1
    assert records[0].input_tokens == 0
    assert records[0].output_tokens == 50


def test_read_records_warns_on_non_dict_time(tmp_path: Path) -> None:
    """When time is a non-dict (e.g. string), warn but use time_created fallback."""
    from app.sources.opencode_db import read_records

    db = tmp_path / "badtime.db"
    data_bad_time = _good_data(time="2024-01-01T00:00:00Z")  # string, not dict
    _make_db(
        db,
        [("r3", "s3", 1_700_000_000_000, 1_700_000_001_000, data_bad_time)],
    )

    records, warnings = read_records(db)

    # Record should still be created using time_created column fallback
    assert len(records) == 1
    # Warning about unexpected time type should be present
    assert len(warnings) >= 1
    assert any("time" in w.lower() for w in warnings)
    # Timestamp should come from time_created column (1_700_000_000_000 ms)
    from datetime import UTC, datetime

    expected_ts = datetime.fromtimestamp(1_700_000_000_000 / 1000.0, tz=UTC)
    assert records[0].timestamp == expected_ts


# ── Additional coverage tests ────────────────────────────────────────────


def test_safe_int_handles_none_and_invalid() -> None:
    """_safe_int should return default for None and non-numeric values."""
    from app.sources.opencode_db import _safe_int

    assert _safe_int(None) == 0
    assert _safe_int(None, 42) == 42
    assert _safe_int("N/A") == 0
    assert _safe_int(3.7) == 3
    assert _safe_int("100") == 100


def test_extract_tokens_rejects_non_dict() -> None:
    """_extract_tokens should return warning for non-dict input."""
    from app.sources.opencode_db import _extract_tokens

    result, warn = _extract_tokens("not-a-dict", "f.db", "r1")
    assert result is None
    assert warn is not None
    assert "not a dict" in warn

    result, warn = _extract_tokens(42, "f.db", "r2")
    assert result is None
    assert "int" in warn


def test_read_records_warns_on_non_dict_json_data(tmp_path: Path) -> None:
    """Rows where JSON data is an array (not object) should be skipped."""
    from app.sources.opencode_db import read_records

    db = tmp_path / "array.db"
    _make_db(db, [("r1", "s1", 1_700_000_000_000, 1_700_000_001_000, "[]")])

    records, warnings = read_records(db)
    assert records == []
    assert any("not a JSON object" in w for w in warnings)


def test_read_records_null_token_values(tmp_path: Path) -> None:
    """Null token values should be coerced to 0 via _safe_int."""
    from app.sources.opencode_db import read_records

    db = tmp_path / "nulltok.db"
    data = _good_data(tokens={"input": None, "output": None, "cache": None})
    _make_db(db, [("r1", "s1", 1_700_000_000_000, 1_700_000_001_000, data)])

    records, warnings = read_records(db)
    assert len(records) == 1
    assert records[0].input_tokens == 0
    assert records[0].output_tokens == 0


def test_read_records_warns_on_connect_failure(tmp_path: Path) -> None:
    """sqlite3.OperationalError on connect should produce a warning."""
    from app.sources.opencode_db import read_records

    db = tmp_path / "x.db"
    db.touch()  # file must exist so we pass the exists() check

    with patch(
        "app.sources.opencode_db.sqlite3.connect",
        side_effect=sqlite3.OperationalError("cannot open"),
    ):
        records, warnings = read_records(db)

    assert records == []
    assert any("cannot open" in w for w in warnings)


def test_read_records_warns_on_query_failure(tmp_path: Path) -> None:
    """sqlite3.OperationalError on query should produce a warning."""
    from app.sources.opencode_db import read_records

    db = tmp_path / "qfail.db"
    _make_db(db, [])  # empty but valid

    real_connect = sqlite3.connect

    class _FailingConn:
        def __init__(self, inner: sqlite3.Connection) -> None:
            self._inner = inner

        def execute(self, sql, params=()):
            if "select" in str(sql).lower():
                raise sqlite3.OperationalError("query failed")
            return self._inner.execute(sql, params)

        def close(self) -> None:
            self._inner.close()

    def spy_connect(*args, **kwargs):
        return _FailingConn(real_connect(*args, **kwargs))

    with patch("app.sources.opencode_db.sqlite3.connect", side_effect=spy_connect):
        records, warnings = read_records(db)

    assert records == []
    assert any("query failed" in w for w in warnings)


def test_read_records_skips_non_assistant_role_without_warning(
    tmp_path: Path,
) -> None:
    """opencode의 user role 등 비-assistant 레코드는 warning 없이 조용히 스킵."""
    import json as _json

    from app.sources.opencode_db import read_records

    db = tmp_path / "role.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE message (
            id text PRIMARY KEY,
            session_id text NOT NULL,
            time_created integer NOT NULL,
            time_updated integer NOT NULL,
            data text NOT NULL
        )
        """
    )
    # user role — assistant가 아니므로 조용히 스킵
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        (
            "msg1",
            "sess1",
            1_700_000_000_000,
            1_700_000_001_000,
            _json.dumps({"role": "user", "content": "hi"}),
        ),
    )
    # assistant — 정상 파싱 대상
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        (
            "msg2",
            "sess1",
            1_700_000_000_000,
            1_700_000_001_000,
            _json.dumps(
                {
                    "role": "assistant",
                    "agent": "worker",
                    "modelID": "qwen3.7-plus",
                    "tokens": {
                        "input": 100,
                        "output": 50,
                        "cache": {"read": 0, "write": 0},
                    },
                    "path": {"cwd": "/home/x/proj"},
                }
            ),
        ),
    )
    conn.commit()
    conn.close()

    records, warnings = read_records(db)

    assert len(records) == 1  # assistant만
    assert records[0].session_id == "sess1"
    # user 메시지에서 발생한 warning이 없어야 함
    assert not any("msg1" in w for w in warnings)


def test_read_records_prefers_live_wal_mode(tmp_path: Path, monkeypatch) -> None:
    """정상 DB는 immutable 폴백 없이 열리고 경고를 남기지 않는다."""
    import sqlite3

    from app.sources.opencode_db import read_records

    db = tmp_path / "oc.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE message (id TEXT, session_id TEXT, "
        "time_created INTEGER, data TEXT)"
    )
    conn.commit()
    conn.close()

    records, warnings = read_records(db)

    assert records == []
    assert not any("immutable" in w for w in warnings)


def test_read_records_falls_back_to_immutable_when_live_probe_fails() -> None:
    """mode=ro 프로브가 OperationalError면 immutable=1로 재시도하고 경고를 남긴다."""
    from app.sources.opencode_db import read_records

    call_uris: list[str] = []

    real_connect = sqlite3.connect

    def fake_connect(*args, **kwargs) -> sqlite3.Connection:
        # 첫 호출(live mode=ro)만 실패, 두 번째(immutable=1)는 실데이터로 위임한다.
        connect_uri = args[0] if args else kwargs.get("database", "")
        call_uris.append(connect_uri)
        if len(call_uris) == 1:
            raise sqlite3.OperationalError("simulated live-mode probe failure")
        return real_connect(connect_uri, uri=True)

    with patch("app.sources.opencode_db.sqlite3.connect", side_effect=fake_connect):
        _, warnings = read_records(OPENCODE_DB)

    assert len(call_uris) == 2, f"expected two connect attempts, got {call_uris!r}"
    assert "mode=ro" in call_uris[0] and "immutable=1" not in call_uris[0]
    assert "immutable=1" in call_uris[1]
    assert any("immutable" in w for w in warnings), (
        f"expected immutable-fallback warning, got {warnings!r}"
    )


def test_opencode_record_carries_full_cwd(fixtures_dir: Path) -> None:
    from app.sources.opencode_db import read_records

    records, _warnings = read_records(fixtures_dir / "opencode.db")
    inside = [r for r in records if r.session_id == "oc-inside-01"]
    assert inside, "oc-inside-01 rows missing from fixture"
    assert all(r.cwd == "/anon/flowproj" for r in inside)
    assert all(r.project == "flowproj" for r in inside)


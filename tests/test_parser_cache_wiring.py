"""The app must reuse one parser cache across requests.

``ParserCache`` existed but was never wired into ``app.main``, so every API
request re-parsed the whole JSONL corpus from scratch. With a real corpus that
pushed ``/api/sessions`` past the frontend's 15s abort timeout and surfaced as
"API 응답이 없습니다 — 백엔드 연결을 확인하세요".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.sources.claude_jsonl import ParserCache

ENDPOINTS = ("/api/summary", "/api/sessions", "/api/delegation")


@pytest.fixture()
def _claude_root(monkeypatch: pytest.MonkeyPatch, fixtures_dir: Path) -> None:
    monkeypatch.setenv("USAGE_CLAUDE_ROOT", str(fixtures_dir / "claude_projects"))


@pytest.mark.usefixtures("_claude_root")
def test_requests_share_one_parser_cache(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every request passes the same cache instance into parse_directory."""
    # Arrange
    from app import main

    seen: list[object] = []
    real = main.parse_directory

    def spy(root: Path, cache: ParserCache | None = None):
        seen.append(cache)
        return real(root, cache=cache)

    monkeypatch.setattr(main, "parse_directory", spy)

    # Act
    for endpoint in ENDPOINTS:
        assert client.get(f"{endpoint}?range=30d&source=all").status_code == 200

    # Assert
    assert len(seen) == len(ENDPOINTS)
    assert all(isinstance(c, ParserCache) for c in seen), "cache was never passed"
    assert len({id(c) for c in seen}) == 1, "each request built its own cache"


@pytest.mark.usefixtures("_claude_root")
def test_second_request_does_not_reread_unchanged_files(client: TestClient) -> None:
    """A warm cache serves the second request without reopening any JSONL file."""
    # Arrange
    assert client.get("/api/summary?range=30d&source=all").status_code == 200

    import app.sources.claude_jsonl as mod

    real_open = open
    reopened: list[str] = []

    def spy_open(file, *args, **kwargs):
        if str(file).endswith(".jsonl"):
            reopened.append(str(file))
        return real_open(file, *args, **kwargs)

    # Act
    mod.open = spy_open  # type: ignore[assignment]
    try:
        assert client.get("/api/summary?range=30d&source=all").status_code == 200
    finally:
        del mod.open

    # Assert
    assert reopened == [], f"re-read {len(reopened)} file(s) despite a warm cache"


@pytest.mark.usefixtures("_claude_root")
def test_endpoints_do_not_block_the_event_loop(client: TestClient) -> None:
    """Data endpoints must not be coroutines doing blocking file I/O.

    FastAPI runs ``def`` handlers in a threadpool; ``async def`` handlers run on
    the event loop, so three parallel dashboard requests serialized head-to-tail
    (measured 3.0s / 5.8s / 8.5s) instead of overlapping.
    """
    # Arrange
    import inspect

    from app import main

    handlers = {
        "summary": main.get_summary,
        "sessions": main.get_sessions,
        "delegation": main.get_delegation,
    }

    # Act / Assert
    blocking = [
        name for name, fn in handlers.items() if inspect.iscoroutinefunction(fn)
    ]
    assert blocking == [], f"async handlers doing blocking I/O: {blocking}"

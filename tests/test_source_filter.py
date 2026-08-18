"""Tests for the ?source= query parameter across all API endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

ENDPOINTS = ("/api/summary", "/api/delegation", "/api/sessions")


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_endpoint_defaults_to_all_sources(endpoint: str, client: TestClient) -> None:
    res = client.get(endpoint, params={"range": "all"})
    assert res.status_code == 200


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("source", ("all", "claude", "opencode"))
def test_endpoint_accepts_valid_source(
    endpoint: str, source: str, client: TestClient
) -> None:
    res = client.get(endpoint, params={"range": "all", "source": source})
    assert res.status_code == 200


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("source", ("all", "claude", "opencode"))
def test_endpoint_echoes_source_in_response(
    endpoint: str, source: str, client: TestClient
) -> None:
    """The response must echo the requested source (basis for the UI toggle)."""
    res = client.get(endpoint, params={"range": "all", "source": source})
    assert res.json()["source"] == source


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_endpoint_rejects_invalid_source(endpoint: str, client: TestClient) -> None:
    res = client.get(endpoint, params={"range": "all", "source": "bogus"})
    assert res.status_code == 400


def test_claude_source_excludes_opencode_projects(client: TestClient) -> None:
    all_res = client.get("/api/summary", params={"range": "all"}).json()
    claude_res = client.get(
        "/api/summary", params={"range": "all", "source": "claude"}
    ).json()

    assert claude_res["kpi"]["total_tokens"] <= all_res["kpi"]["total_tokens"]


def test_source_split_sums_to_all(client: TestClient) -> None:
    def tokens(source: str | None) -> int:
        params = {"range": "all"}
        if source:
            params["source"] = source
        return client.get("/api/summary", params=params).json()["kpi"]["total_tokens"]

    assert tokens("claude") + tokens("opencode") == tokens(None)


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("range_key", ("15m", "1h", "24h", "7d", "30d", "all"))
def test_endpoint_accepts_all_range_presets(
    endpoint: str, range_key: str, client: TestClient
) -> None:
    res = client.get(endpoint, params={"range": range_key})
    assert res.status_code == 200
    assert res.json()["range"] == range_key


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_endpoint_reports_source_freshness(endpoint: str, client: TestClient) -> None:
    body = client.get(endpoint, params={"range": "all"}).json()

    assert "source_freshness" in body
    freshness = body["source_freshness"]
    assert set(freshness) == {"claude", "opencode"}
    for value in freshness.values():
        assert value is None or isinstance(value, int)


def test_source_freshness_ignores_range_filter(client: TestClient) -> None:
    """빈 라이브 창에서도 마지막 데이터 시각은 보여야 한다."""
    live = client.get("/api/summary", params={"range": "15m"}).json()
    everything = client.get("/api/summary", params={"range": "all"}).json()

    assert live["source_freshness"] == everything["source_freshness"]

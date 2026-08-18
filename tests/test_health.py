"""Tests for the /health endpoint."""

from __future__ import annotations

from starlette.testclient import TestClient


def test_health_endpoint_returns_ok(client: TestClient) -> None:
    """GET /health should return 200 with {"status": "ok"}."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

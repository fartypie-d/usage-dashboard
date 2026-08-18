"""Tests for GET /api/summary endpoint."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient


def test_summary_endpoint_returns_200_and_expected_top_level_keys(client: TestClient) -> None:
    response = client.get("/api/summary?range=7d")
    assert response.status_code == 200
    data = response.json()
    expected_keys = {
        "range",
        "source",
        "generated_at",
        "kpi",
        "project_mix",
        "model_rank",
        "daily_cost",
        "mismatches",
        "cache",
        "warnings",
        "source_freshness",
    }
    assert set(data.keys()) == expected_keys


def test_summary_endpoint_echoes_range_query_param(client: TestClient) -> None:
    response = client.get("/api/summary?range=30d")
    assert response.status_code == 200
    data = response.json()
    assert data["range"] == "30d"

    response_all = client.get("/api/summary?range=all")
    assert response_all.status_code == 200
    assert response_all.json()["range"] == "all"


def test_summary_endpoint_rejects_invalid_range(client: TestClient) -> None:
    response = client.get("/api/summary?range=xyz")
    assert response.status_code == 400
    data = response.json()
    assert "detail" in data or "message" in data or "error" in data


def test_summary_endpoint_kpi_has_all_required_fields(client: TestClient) -> None:
    response = client.get("/api/summary?range=7d")
    assert response.status_code == 200
    kpi = response.json()["kpi"]
    assert "total_cost_usd" in kpi and isinstance(kpi["total_cost_usd"], (int, float))
    assert "total_tokens" in kpi and isinstance(kpi["total_tokens"], int)
    assert "cache_hit_rate" in kpi and isinstance(kpi["cache_hit_rate"], (int, float))
    assert "delegated_session_ratio" in kpi and isinstance(
        kpi["delegated_session_ratio"], (int, float)
    )
    assert "anomaly_count" in kpi and isinstance(kpi["anomaly_count"], int)


def test_summary_endpoint_cache_has_all_required_fields(client: TestClient) -> None:
    response = client.get("/api/summary?range=7d")
    assert response.status_code == 200
    cache = response.json()["cache"]
    assert "savings_now_usd" in cache and isinstance(
        cache["savings_now_usd"], (int, float)
    )
    assert "savings_potential_usd" in cache and isinstance(
        cache["savings_potential_usd"], (int, float)
    )
    assert "by_project_model" in cache and isinstance(cache["by_project_model"], list)
    assert "worst_sessions" in cache and isinstance(cache["worst_sessions"], list)


def test_summary_endpoint_model_rank_has_expected_structure(
    client: TestClient,
) -> None:
    response = client.get("/api/summary?range=all")
    assert response.status_code == 200
    model_rank_data = response.json()["model_rank"]
    assert isinstance(model_rank_data, list)
    assert len(model_rank_data) > 0, (
        "fixture data must produce at least one model_rank row"
    )
    row = model_rank_data[0]
    assert "model" in row and isinstance(row["model"], str)
    assert "cost_usd" in row and isinstance(row["cost_usd"], (int, float))
    assert "cost_share" in row and isinstance(row["cost_share"], (int, float))
    assert "tokens" in row and isinstance(row["tokens"], int)
    assert "by_agent" in row and isinstance(row["by_agent"], list)
    assert len(row["by_agent"]) > 0
    agent_row = row["by_agent"][0]
    assert "agent" in agent_row and isinstance(agent_row["agent"], str)
    assert "cost_usd" in agent_row and isinstance(
        agent_row["cost_usd"], (int, float)
    )
    assert "tokens" in agent_row and isinstance(agent_row["tokens"], int)


def test_summary_endpoint_uses_fixture_paths_by_default(client: TestClient) -> None:
    response = client.get("/api/summary?range=all")
    assert response.status_code == 200
    data = response.json()
    # Check that data from fixtures is parsed and present
    assert len(data["project_mix"]) > 0
    assert data["kpi"]["total_tokens"] > 0


def test_summary_endpoint_generated_at_is_recent_epoch_ms(client: TestClient) -> None:
    now_ms = int(time.time() * 1000)
    response = client.get("/api/summary?range=7d")
    assert response.status_code == 200
    generated_at = response.json()["generated_at"]
    assert isinstance(generated_at, int)
    # Generated within the last 10 seconds
    assert abs(now_ms - generated_at) < 10000


def test_summary_endpoint_model_rank_cost_sum_equals_kpi_total(
    client: TestClient,
) -> None:
    """D5 invariant: sum of model_rank costs must equal kpi total_cost_usd.

    model_rank 각 행의 cost_usd 합이 kpi.total_cost_usd와 일치해야 한다.
    두 값은 서로 다른 순서로 부동소수점 덧셈을 수행하므로 pytest.approx를
    사용한다 (측정된 최대 오차 ~5e-13).
    range=all을 쓰는 이유: 시간 경계 필터가 live datetime.now(UTC)를 쓰므로
    고정 날짜 픽스처에 대해 bounded range를 쓰면 결과가 공집합이 되어 검증이
    무의미해진다.
    """
    response = client.get("/api/summary?range=all")
    assert response.status_code == 200
    data = response.json()
    model_rank = data["model_rank"]
    assert len(model_rank) > 0, "fixture data must produce at least one model_rank row"
    model_rank_total = sum(row["cost_usd"] for row in model_rank)
    kpi_total = data["kpi"]["total_cost_usd"]
    assert model_rank_total == pytest.approx(kpi_total, rel=1e-9)


def test_summary_endpoint_health_still_works(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

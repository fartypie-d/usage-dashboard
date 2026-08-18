"""Tests for GET /api/delegation and app/metrics/delegation pure functions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.sources.claude_jsonl import Record


def _record(
    *,
    agent: str | None,
    model: str,
    session_id: str = "s1",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read_tokens: int = 20,
    cache_write_tokens: int = 10,
) -> Record:
    """Build a minimal Record for delegation tests."""
    return Record(
        project="test-project",
        model=model,
        timestamp=datetime.now(UTC),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        session_id=session_id,
        source_file="inline",
        source="claude",
        agent=agent,
    )


def test_delegation_endpoint_returns_200_and_top_level_keys(client: TestClient) -> None:
    response = client.get("/api/delegation?range=7d")
    assert response.status_code == 200
    data = response.json()
    expected_keys = {
        "range",
        "source",
        "flow",
        "flows",
        "agents",
        "overhead",
        "warnings",
        "source_freshness",
        "flows_total",
        "flows_limit",
    }
    assert set(data.keys()) == expected_keys


def test_delegation_endpoint_echoes_range_query_param(client: TestClient) -> None:
    response = client.get("/api/delegation?range=7d")
    assert response.status_code == 200
    assert response.json()["range"] == "7d"

    response_30 = client.get("/api/delegation?range=30d")
    assert response_30.status_code == 200
    assert response_30.json()["range"] == "30d"

    response_all = client.get("/api/delegation?range=all")
    assert response_all.status_code == 200
    assert response_all.json()["range"] == "all"


def test_delegation_endpoint_rejects_invalid_range(client: TestClient) -> None:
    response = client.get("/api/delegation?range=xyz")
    assert response.status_code in {400, 422}


def test_flow_excludes_claude_direct_records() -> None:
    from app.metrics.delegation import flow

    records = [
        _record(agent="web-ui", model="qwen3.7-plus", session_id="s1"),
        _record(agent="build", model="kimi-k2.7-code", session_id="s2"),
        _record(agent=None, model="claude-opus-4", session_id="s3"),
    ]
    result = flow(records)

    assert len(result) == 2
    agents_in_flow = {item["agent"] for item in result}
    assert "web-ui" in agents_in_flow
    assert "build" in agents_in_flow
    assert None not in agents_in_flow
    # tokens desc sort
    assert result[0]["tokens"] >= result[1]["tokens"]


def test_agents_has_model_breakdown() -> None:
    from app.metrics.delegation import agents

    records = [
        _record(agent="web-ui", model="qwen3.7-plus", session_id="s1"),
        _record(agent="web-ui", model="qwen3.7-plus", session_id="s1"),
        _record(agent="web-ui", model="kimi-k2.7-code", session_id="s2"),
        _record(agent=None, model="claude-opus-4", session_id="s3"),
    ]
    result = agents(records)

    assert len(result) == 1
    web_ui = result[0]
    assert web_ui["agent"] == "web-ui"
    assert web_ui["calls"] == 3
    assert web_ui["tokens"] == 3 * 180
    assert "cost_usd" in web_ui
    assert "avg_turns" in web_ui
    assert isinstance(web_ui["models"], list)
    assert len(web_ui["models"]) == 2

    # Models sorted by tokens desc
    assert web_ui["models"][0]["tokens"] >= web_ui["models"][1]["tokens"]
    model_names = {m["model"] for m in web_ui["models"]}
    assert model_names == {"qwen3.7-plus", "kimi-k2.7-code"}

    # Model breakdown fields
    for m in web_ui["models"]:
        assert "model" in m
        assert "calls" in m
        assert "tokens" in m
        assert "cost_usd" in m


def test_delegation_endpoint_no_longer_exposes_the_old_overhead_fields(
    client: TestClient,
) -> None:
    response = client.get("/api/delegation?range=all")
    assert response.status_code == 200
    overhead = response.json()["overhead"]
    removed = {
        "delegation_token_overhead",
        "direct_cost_per_task_usd",
        "delegated_cost_per_task_usd",
    }
    assert removed.isdisjoint(overhead.keys())


def test_delegation_endpoint_overhead_has_the_redefined_fields(
    client: TestClient,
) -> None:
    response = client.get("/api/delegation?range=all")
    assert response.status_code == 200
    overhead = response.json()["overhead"]
    expected = {
        "delegation_share",
        "delegated_cost_usd",
        "total_flow_cost_usd",
        "setup_cost_usd",
        "work_cost_usd",
        "setup_share",
        "flow_count",
        "two_hop_count",
    }
    assert expected <= set(overhead.keys())
    assert 0.0 <= overhead["delegation_share"] <= 1.0
    assert 0.0 <= overhead["setup_share"] <= 1.0
    assert isinstance(overhead["flow_count"], int)
    assert isinstance(overhead["two_hop_count"], int)


def test_delegation_endpoint_surfaces_flow_warnings_in_the_response(
    client: TestClient,
) -> None:
    """Phase 8 carry-over: flow_warnings must reach the response warnings array."""
    response = client.get("/api/delegation?range=all")
    assert response.status_code == 200
    warnings = response.json()["warnings"]
    assert isinstance(warnings, list)
    matching = [w for w in warnings if "부모를 찾지 못한" in w]
    assert len(matching) >= 1


def test_delegation_endpoint_uses_fixture_paths_by_default(client: TestClient) -> None:
    response = client.get("/api/delegation?range=all")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data["flow"], list)
    assert isinstance(data["agents"], list)
    assert isinstance(data["overhead"], dict)
    # Fixture has both direct (claude) and delegated (opencode) sessions
    assert len(data["flow"]) > 0 or len(data["agents"]) > 0


def test_summary_endpoint_still_works(client: TestClient) -> None:
    response = client.get("/api/summary?range=7d")
    assert response.status_code == 200
    assert "kpi" in response.json()


def test_delegation_endpoint_exposes_flows_with_the_fixture_session(
    client: TestClient,
) -> None:
    response = client.get("/api/delegation?range=all")
    assert response.status_code == 200
    data = response.json()

    by_session = {f["session_id"]: f for f in data["flows"]}
    flow = by_session["root-sess-0001"]

    assert flow["child_count"] == 4
    assert flow["max_parallel"] == 4
    assert flow["two_hop_count"] == 1
    assert flow["cwd"] == "/anon/flowproj"
    assert len(flow["children"]) == 4


def test_delegation_endpoint_surfaces_unknown_model_warnings(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Unknown models in filtered records must appear in response warnings."""
    unknown_model = "totally-unregistered-model-xyz"
    project_dir = tmp_path / "proj_unknown_model"
    project_dir.mkdir()
    record = {
        "type": "assistant",
        "isSidechain": False,
        "timestamp": "2026-07-22T07:50:07.177Z",
        "message": {
            "role": "assistant",
            "model": unknown_model,
            "usage": {
                "output_tokens": 270,
                "cache_creation_input_tokens": 10395,
                "input_tokens": 2,
                "cache_read_input_tokens": 12615,
            },
        },
        "cwd": "/anon/f99a554b",
        "sessionId": "104db3be-59a7-4dcb-8e81-0d11a39c2cdf",
    }
    (project_dir / "sess-unknown.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )
    monkeypatch.setenv("USAGE_CLAUDE_ROOT", str(tmp_path))

    response = client.get("/api/delegation?range=all")
    assert response.status_code == 200
    assert f"unknown model: {unknown_model}" in response.json()["warnings"]


def _synthetic_flows(n: int) -> list[dict[str, object]]:
    """Build n cost-desc-sorted flow dicts for endpoint capping tests.

    Warning: real flows() returns ~16 keys; this helper only builds the 6 keys
    needed to exercise the limit/cap path, so schema drift will not fail here.
    """
    flows: list[dict[str, object]] = []
    for i in range(n):
        cost = float(n - i)  # descending: n, n-1, ..., 1
        flows.append(
            {
                "session_id": f"synth-{i:04d}",
                "cost_usd": cost,
                "self": {"cost_usd": cost * 0.5},
                "setup_cost_usd": cost * 0.1,
                "two_hop_count": 0,
                "children": [],
            }
        )
    return flows


def _patch_many_flows(
    monkeypatch: pytest.MonkeyPatch, n: int = 30
) -> list[dict[str, object]]:
    """Replace delegation_flows so the endpoint sees n synthetic flows."""
    fake = _synthetic_flows(n)

    def _fake_flows(_records: list) -> tuple[list[dict[str, object]], list[str]]:
        return fake, []

    monkeypatch.setattr("app.main.delegation_flows", _fake_flows)
    return fake


def test_delegation_flows_are_capped_at_the_default_limit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_many_flows(monkeypatch, n=30)
    response = client.get("/api/delegation?range=all")
    assert response.status_code == 200
    assert len(response.json()["flows"]) <= 20


def test_delegation_reports_the_total_flow_count_before_capping(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_many_flows(monkeypatch, n=30)
    response = client.get("/api/delegation?range=all")
    assert response.status_code == 200
    data = response.json()
    assert data["flows_total"] >= len(data["flows"])
    assert data["flows_limit"] == 20


def test_delegation_limit_parameter_returns_more_flows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _patch_many_flows(monkeypatch, n=30)
    response = client.get("/api/delegation?range=all&limit=1")
    assert response.status_code == 200
    data = response.json()
    assert len(data["flows"]) == 1
    assert data["flows_total"] == len(fake)


def test_delegation_rejects_a_limit_outside_the_allowed_range(
    client: TestClient,
) -> None:
    r0 = client.get("/api/delegation?range=all&limit=0")
    assert r0.status_code == 422
    r1001 = client.get("/api/delegation?range=all&limit=1001")
    assert r1001.status_code == 422


def test_capping_flows_does_not_change_the_overhead_metrics(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capping must only shrink the serialized flows array.

    overhead totals (total_flow_cost_usd, delegated_cost_usd, setup_cost_usd,
    flow_count, …) are the period-wide cost allocation.  If the limit is
    applied before overhead(), those numbers silently become \"top-N only\"
    while the UI still looks plausible — this guard catches that regression.
    """
    _patch_many_flows(monkeypatch, n=30)
    r1 = client.get("/api/delegation?range=all&limit=1")
    r1000 = client.get("/api/delegation?range=all&limit=1000")
    assert r1.status_code == 200
    assert r1000.status_code == 200
    assert r1.json()["overhead"] == r1000.json()["overhead"]


def test_overhead_flow_count_still_counts_every_flow(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _patch_many_flows(monkeypatch, n=30)
    response = client.get("/api/delegation?range=all&limit=1")
    assert response.status_code == 200
    data = response.json()
    assert data["overhead"]["flow_count"] == data["flows_total"]
    assert data["overhead"]["flow_count"] == len(fake)
    assert len(data["flows"]) == 1  # capped; different from flow_count is OK


def test_delegation_returns_the_most_expensive_flows_first(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _patch_many_flows(monkeypatch, n=30)
    response = client.get("/api/delegation?range=all&limit=5")
    assert response.status_code == 200
    data = response.json()
    costs = [f["cost_usd"] for f in data["flows"]]
    assert costs == sorted(costs, reverse=True)
    # Every omitted flow must be cheaper than (or equal to) every returned one.
    returned_ids = {f["session_id"] for f in data["flows"]}
    omitted = [f for f in fake if f["session_id"] not in returned_ids]
    if costs and omitted:
        assert max(f["cost_usd"] for f in omitted) <= min(costs)


def test_delegation_response_stays_under_the_size_budget(
    client: TestClient,
) -> None:
    """Default response body must stay small enough for the dashboard.

    Fixture baseline is ~5 KB uncompressed.  The budget is that baseline plus
    headroom so an accidental field bloat (or uncapped real-data flows leaking
    into tests) fails loudly.  Purpose: catch careless payload growth.
    """
    response = client.get("/api/delegation?range=all")
    assert response.status_code == 200
    # Fixture-measured ~4685 B; ~2× headroom so real regressions still fail.
    assert len(response.content) <= 10_000


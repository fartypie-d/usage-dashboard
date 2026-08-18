"""Unit tests for app/metrics/delegation pure functions.

Inline dict / Record helpers only — no file I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.metrics.delegation import agents, flow, overhead
from app.sources.claude_jsonl import Record


def _record(
    *,
    agent: str | None,
    model: str = "claude-sonnet-4",
    session_id: str = "s1",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read_tokens: int = 20,
    cache_write_tokens: int = 10,
) -> Record:
    """Build a minimal Record for delegation unit tests."""
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


def _flow(
    *,
    cost_usd: float,
    self_cost_usd: float,
    setup_cost_usd: float = 0.0,
    two_hop_count: int = 0,
) -> dict:
    """Minimal flow dict matching the shape produced by delegation_flow.flows()."""
    return {
        "cost_usd": cost_usd,
        "self": {"cost_usd": self_cost_usd, "tokens": 0, "turns": 0},
        "setup_cost_usd": setup_cost_usd,
        "two_hop_count": two_hop_count,
    }


# ---------------------------------------------------------------------------
# overhead()
# ---------------------------------------------------------------------------


def test_overhead_computes_shares_from_two_flows() -> None:
    """Hand-checked values for two healthy flows."""
    # flow1: total 100, self 60 → delegated 40, setup 10
    # flow2: total  50, self 30 → delegated 20, setup  5
    # totals: 150 / 60 / setup 15 → share 0.4 / setup_share 0.25 / work 45
    flows = [
        _flow(cost_usd=100.0, self_cost_usd=60.0, setup_cost_usd=10.0),
        _flow(cost_usd=50.0, self_cost_usd=30.0, setup_cost_usd=5.0),
    ]
    result, warnings = overhead(flows, two_hop_count=0)

    assert result["total_flow_cost_usd"] == 150.0
    assert result["delegated_cost_usd"] == 60.0
    assert result["delegation_share"] == 0.4
    assert result["setup_cost_usd"] == 15.0
    assert result["work_cost_usd"] == 45.0
    assert result["setup_share"] == 0.25
    assert result["flow_count"] == 2
    assert result["two_hop_count"] == 0
    assert warnings == []


def test_overhead_with_no_flows_returns_zeros() -> None:
    result, warnings = overhead([], two_hop_count=0)

    assert result["total_flow_cost_usd"] == 0.0
    assert result["delegated_cost_usd"] == 0.0
    assert result["delegation_share"] == 0.0
    assert result["setup_cost_usd"] == 0.0
    assert result["work_cost_usd"] == 0.0
    assert result["setup_share"] == 0.0
    assert result["flow_count"] == 0
    assert result["two_hop_count"] == 0
    assert warnings == []


def test_overhead_does_not_divide_by_zero_when_total_cost_is_zero() -> None:
    flows = [_flow(cost_usd=0.0, self_cost_usd=0.0, setup_cost_usd=0.0)]
    result, warnings = overhead(flows, two_hop_count=0)

    assert result["delegation_share"] == 0.0
    assert result["total_flow_cost_usd"] == 0.0
    assert warnings == []


def test_overhead_returns_zero_setup_share_when_nothing_was_delegated() -> None:
    # Root-only cost: cost == self → delegated 0
    flows = [_flow(cost_usd=80.0, self_cost_usd=80.0, setup_cost_usd=0.0)]
    result, warnings = overhead(flows, two_hop_count=0)

    assert result["delegated_cost_usd"] == 0.0
    assert result["setup_share"] == 0.0
    assert result["work_cost_usd"] == 0.0
    assert warnings == []


def test_overhead_uses_the_two_hop_count_argument() -> None:
    flows = [_flow(cost_usd=10.0, self_cost_usd=5.0, two_hop_count=99)]
    result, _warnings = overhead(flows, two_hop_count=7)

    # Must use the keyword argument, not re-sum flow internals.
    assert result["two_hop_count"] == 7


def test_overhead_shares_are_always_between_zero_and_one() -> None:
    """P1 sign-bug regression guard — shares must never go negative or > 1."""
    cases = [
        [],
        [_flow(cost_usd=0.0, self_cost_usd=0.0)],
        [_flow(cost_usd=100.0, self_cost_usd=100.0)],
        [_flow(cost_usd=100.0, self_cost_usd=40.0, setup_cost_usd=20.0)],
        # damaged inputs that trigger clamps
        [_flow(cost_usd=10.0, self_cost_usd=50.0)],
        [_flow(cost_usd=100.0, self_cost_usd=40.0, setup_cost_usd=80.0)],
    ]
    for flows in cases:
        result, _warnings = overhead(flows, two_hop_count=0)
        assert 0.0 <= result["delegation_share"] <= 1.0
        assert 0.0 <= result["setup_share"] <= 1.0


def test_overhead_clamps_negative_delegated_cost_and_warns() -> None:
    # self.cost_usd > cost_usd → delegated clamped to 0.0 + warning
    flows = [_flow(cost_usd=10.0, self_cost_usd=50.0, setup_cost_usd=0.0)]
    result, warnings = overhead(flows, two_hop_count=0)

    assert result["delegated_cost_usd"] == 0.0
    assert result["work_cost_usd"] == 0.0
    assert len(warnings) == 1
    assert isinstance(warnings[0], str)
    assert warnings[0]  # non-empty Korean message describing the mismatch


def test_overhead_clamps_setup_over_delegated_and_warns() -> None:
    # setup 80 > delegated 60 → setup_share 1.0, work 0.0, warning
    flows = [_flow(cost_usd=100.0, self_cost_usd=40.0, setup_cost_usd=80.0)]
    result, warnings = overhead(flows, two_hop_count=0)

    assert result["delegated_cost_usd"] == 60.0
    assert result["setup_cost_usd"] == 80.0
    assert result["setup_share"] == 1.0
    assert result["work_cost_usd"] == 0.0
    assert len(warnings) == 1
    assert isinstance(warnings[0], str)
    assert warnings[0]


def test_overhead_returns_no_warnings_for_healthy_input() -> None:
    flows = [
        _flow(cost_usd=100.0, self_cost_usd=60.0, setup_cost_usd=10.0),
        _flow(cost_usd=50.0, self_cost_usd=30.0, setup_cost_usd=5.0),
    ]
    _result, warnings = overhead(flows, two_hop_count=2)
    assert warnings == []


def test_overhead_work_cost_is_delegated_minus_setup() -> None:
    """Invariant on healthy input: work == delegated - setup."""
    flows = [
        _flow(cost_usd=200.0, self_cost_usd=80.0, setup_cost_usd=30.0),
        _flow(cost_usd=100.0, self_cost_usd=70.0, setup_cost_usd=10.0),
    ]
    result, warnings = overhead(flows, two_hop_count=0)
    assert warnings == []
    assert result["work_cost_usd"] == round(
        result["delegated_cost_usd"] - result["setup_cost_usd"], 2
    )


# ---------------------------------------------------------------------------
# flow() / agents() characterization (safety net for this change)
# ---------------------------------------------------------------------------


def test_flow_aggregates_tokens_per_agent_and_excludes_direct() -> None:
    records = [
        _record(agent="web-ui", session_id="s1", input_tokens=100, output_tokens=50,
                cache_read_tokens=20, cache_write_tokens=10),
        _record(agent="web-ui", session_id="s1", input_tokens=100, output_tokens=50,
                cache_read_tokens=20, cache_write_tokens=10),
        _record(agent="build", session_id="s2", input_tokens=200, output_tokens=100,
                cache_read_tokens=0, cache_write_tokens=0),
        _record(agent=None, session_id="s3", input_tokens=999, output_tokens=999),
    ]
    result = flow(records)

    assert len(result) == 2
    by_agent = {item["agent"]: item for item in result}
    assert set(by_agent) == {"web-ui", "build"}
    # tokens = input+output+cache_read+cache_write = 180 each for web-ui
    assert by_agent["web-ui"]["tokens"] == 360
    assert by_agent["web-ui"]["calls"] == 2
    assert by_agent["build"]["tokens"] == 300
    assert by_agent["build"]["calls"] == 1
    # sorted by tokens desc
    assert result[0]["tokens"] >= result[1]["tokens"]


def test_agents_includes_model_breakdown_and_avg_turns() -> None:
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
    # 2 sessions → avg_turns = 3/2 = 1.5
    assert web_ui["avg_turns"] == 1.5
    assert isinstance(web_ui["cost_usd"], float)
    assert isinstance(web_ui["models"], list)
    assert len(web_ui["models"]) == 2
    model_names = {m["model"] for m in web_ui["models"]}
    assert model_names == {"qwen3.7-plus", "kimi-k2.7-code"}
    for m in web_ui["models"]:
        assert {"model", "calls", "tokens", "cost_usd"} <= set(m.keys())

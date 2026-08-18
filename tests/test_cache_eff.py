from datetime import datetime

import pytest

from app.metrics.cache_eff import cache_metrics
from app.sources.claude_jsonl import Record


def make_record(
    project: str,
    model: str,
    session_id: str,
    input_tokens: int = 1000,
    cache_read: int = 0,
    output_tokens: int = 10,
    cache_write: int = 0
) -> Record:
    return Record(
        project=project,
        model=model,
        timestamp=datetime(2026, 7, 24),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        session_id=session_id,
        source_file="test.jsonl",
        source="claude",
    )

def test_cache_read_ratio_formula():
    # cache_read / (input + cache_read)
    r1 = make_record("p1", "claude-opus-4", "s1", input_tokens=80, cache_read=20)
    res = cache_metrics([r1])
    assert res["by_project_model"][0]["cache_read_ratio"] == 0.2

def test_by_project_model_groups_correctly():
    records = [
        make_record("p1", "claude-opus-4", "s1", 100, 100),
        make_record("p1", "claude-opus-4", "s2", 200, 200),
        make_record("p2", "claude-opus-4", "s3", 50, 50),
    ]
    res = cache_metrics(records)
    groups = res["by_project_model"]
    assert len(groups) == 2
    p1_group = next(g for g in groups if g["project"] == "p1")
    assert p1_group["message_count"] == 2
    assert p1_group["cache_read_ratio"] == 0.5

def test_worst_sessions_sorted_by_ratio_asc():
    records = [
        make_record("p1", "claude-opus-4", "s_good", 100, 900), # ratio 0.9
        make_record("p1", "claude-opus-4", "s_bad", 900, 100),  # ratio 0.1
        make_record("p1", "claude-opus-4", "s_mid", 500, 500),  # ratio 0.5
    ]
    res = cache_metrics(records)
    worst = res["worst_sessions"]
    assert worst[0]["session_id"] == "s_bad"
    assert worst[1]["session_id"] == "s_mid"
    assert worst[2]["session_id"] == "s_good"

def test_savings_now_uses_cache_read_price_difference():
    # For opus-4: input 15.0, cache_read 1.5 per 1M. Diff = 13.5 per 1M.
    # 100,000 cache read tokens = 0.1M * 13.5 = 1.35 USD
    r = make_record("p1", "claude-opus-4", "s1", input_tokens=100, cache_read=100_000)
    res = cache_metrics([r])
    assert pytest.approx(res["savings_now_usd"]) == 1.35

def test_savings_potential_zero_when_all_at_average():
    # Both sessions have ratio 0.5 -> average is 0.5.
    # Neither is below average, so potential is 0.
    records = [
        make_record("p1", "claude-opus-4", "s1", 1000, 1000),
        make_record("p1", "claude-opus-4", "s2", 500, 500),
    ]
    res = cache_metrics(records)
    assert res["savings_potential_usd"] == 0.0

def test_cache_metrics_handles_zero_tokens_gracefully():
    # input=0, cache_read=0
    r = make_record("p1", "claude-opus-4", "s1", input_tokens=0, cache_read=0)
    res = cache_metrics([r])
    assert res["by_project_model"][0]["cache_read_ratio"] == 0.0

def test_cache_metrics_handles_unknown_model():
    r = make_record("p1", "unknown-model", "s1", 1000, 1000)
    res = cache_metrics([r])
    assert "unknown model: unknown-model" in res.get("warnings", [])
    assert res["savings_now_usd"] == 0.0

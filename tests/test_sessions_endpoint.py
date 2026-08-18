from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.main import app
from app.metrics.session_health import session_health
from app.sources.claude_jsonl import Record

client = TestClient(app)


def test_sessions_endpoint_returns_200_and_top_level_keys() -> None:
    response = client.get("/api/sessions?range=7d")
    assert response.status_code == 200
    data = response.json()
    assert "range" in data
    assert "source" in data
    assert "sessions" in data
    assert "warnings" in data
    assert "source_freshness" in data
    assert set(data["source_freshness"]) == {"claude", "opencode"}
    assert isinstance(data["sessions"], list)
    assert isinstance(data["warnings"], list)


def test_sessions_endpoint_echoes_range_query_param() -> None:
    for rng in ("7d", "30d", "all"):
        res = client.get(f"/api/sessions?range={rng}")
        assert res.status_code == 200
        assert res.json()["range"] == rng


def test_sessions_endpoint_rejects_invalid_range() -> None:
    res = client.get("/api/sessions?range=invalid_range")
    assert res.status_code == 400


def test_context_growth_is_cumulative_and_ordered_by_timestamp() -> None:
    base_time = datetime(2026, 7, 24, 10, 0, 0, tzinfo=UTC)
    r1 = Record(
        project="p1",
        model="claude-opus-4",
        timestamp=base_time + timedelta(seconds=10),
        input_tokens=1000,
        output_tokens=100,
        cache_read_tokens=4000,
        cache_write_tokens=0,
        session_id="s1",
        source_file="test.jsonl",
        source="claude",
    )
    r2 = Record(
        project="p1",
        model="claude-opus-4",
        timestamp=base_time,  # earlier timestamp
        input_tokens=500,
        output_tokens=50,
        cache_read_tokens=1500,
        cache_write_tokens=0,
        session_id="s1",
        source_file="test.jsonl",
        source="claude",
    )
    r3 = Record(
        project="p1",
        model="claude-opus-4",
        timestamp=base_time + timedelta(seconds=20),
        input_tokens=2000,
        output_tokens=200,
        cache_read_tokens=8000,
        cache_write_tokens=0,
        session_id="s1",
        source_file="test.jsonl",
        source="claude",
    )

    res = session_health([r1, r2, r3])
    assert len(res) == 1
    s = res[0]
    assert s["session_id"] == "s1"
    assert s["turns"] == 3
    # Ordered by timestamp asc: r2 (2000), r1 (5000), r3 (10000)
    # Cumulative growth: [2000, 7000, 17000]
    assert s["context_growth"] == [2000, 7000, 17000]


def test_health_label_derived_from_flags() -> None:
    base_time = datetime(2026, 7, 24, 10, 0, 0, tzinfo=UTC)

    # Session OK (normal growth, under 200k, no spike, no compaction)
    ok_records = [
        Record(
            project="p1",
            model="m1",
            timestamp=base_time + timedelta(seconds=i),
            input_tokens=1000,
            output_tokens=100,
            cache_read_tokens=1000,
            cache_write_tokens=0,
            session_id="s_ok",
            source_file="f1",
            source="claude",
        )
        for i in range(3)
    ]

    # Session WARN (token spike: 100 -> 5000 is 50x spike, but total under 200k)
    warn_records = [
        Record(
            project="p1",
            model="m1",
            timestamp=base_time,
            input_tokens=50,
            output_tokens=10,
            cache_read_tokens=50,
            cache_write_tokens=0,
            session_id="s_warn",
            source_file="f1",
            source="claude",
        ),
        Record(
            project="p1",
            model="m1",
            timestamp=base_time + timedelta(seconds=1),
            input_tokens=2500,
            output_tokens=10,
            cache_read_tokens=2500,
            cache_write_tokens=0,
            session_id="s_warn",
            source_file="f1",
            source="claude",
        ),
    ]

    # Session DANGER (token spike AND split_recommended > 200k)
    danger_records = [
        Record(
            project="p1",
            model="m1",
            timestamp=base_time,
            input_tokens=1000,
            output_tokens=10,
            cache_read_tokens=0,
            cache_write_tokens=0,
            session_id="s_danger",
            source_file="f1",
            source="claude",
        ),
        Record(
            project="p1",
            model="m1",
            timestamp=base_time + timedelta(seconds=1),
            input_tokens=205_000,  # >200k and spike (205k >= 3 * 1k)
            output_tokens=10,
            cache_read_tokens=0,
            cache_write_tokens=0,
            session_id="s_danger",
            source_file="f1",
            source="claude",
        ),
    ]

    res = session_health(ok_records + warn_records + danger_records)
    health_by_id = {s["session_id"]: s for s in res}

    assert health_by_id["s_ok"]["health"] == "ok"

    assert health_by_id["s_warn"]["token_spike"] is True
    assert health_by_id["s_warn"]["split_recommended"] is False
    assert health_by_id["s_warn"]["health"] == "warn"

    assert health_by_id["s_danger"]["token_spike"] is True
    assert health_by_id["s_danger"]["split_recommended"] is True
    assert health_by_id["s_danger"]["health"] == "danger"


def test_session_model_uses_most_common_model() -> None:
    base_time = datetime(2026, 7, 24, 10, 0, 0, tzinfo=UTC)
    recs = [
        Record(
            project="p1",
            model="claude-opus-4",
            timestamp=base_time,
            input_tokens=10,
            output_tokens=10,
            cache_read_tokens=0,
            cache_write_tokens=0,
            session_id="s1",
            source_file="f1",
            source="claude",
        ),
        Record(
            project="p1",
            model="claude-sonnet-4",
            timestamp=base_time + timedelta(seconds=1),
            input_tokens=10,
            output_tokens=10,
            cache_read_tokens=0,
            cache_write_tokens=0,
            session_id="s1",
            source_file="f1",
            source="claude",
        ),
        Record(
            project="p1",
            model="claude-opus-4",
            timestamp=base_time + timedelta(seconds=2),
            input_tokens=10,
            output_tokens=10,
            cache_read_tokens=0,
            cache_write_tokens=0,
            session_id="s1",
            source_file="f1",
            source="claude",
        ),
    ]

    res = session_health(recs)
    assert len(res) == 1
    assert res[0]["model"] == "claude-opus-4"


def test_compaction_turns_detection() -> None:
    base_time = datetime(2026, 7, 24, 10, 0, 0, tzinfo=UTC)
    recs = [
        Record(
            project="p1",
            model="m1",
            timestamp=base_time,
            input_tokens=10000,
            output_tokens=100,
            cache_read_tokens=90000,  # turn 0 prompt context = 100,000
            cache_write_tokens=0,
            session_id="s_compact",
            source_file="f1",
            source="claude",
        ),
        Record(
            project="p1",
            model="m1",
            timestamp=base_time + timedelta(seconds=1),
            input_tokens=5000,
            output_tokens=100,
            cache_read_tokens=15000,  # turn 1 prompt context = 20,000 (80% drop <= 70% threshold)
            cache_write_tokens=0,
            session_id="s_compact",
            source_file="f1",
            source="claude",
        ),
    ]

    res = session_health(recs)
    assert len(res) == 1
    assert res[0]["compaction_turns"] == [1]
    assert res[0]["health"] == "warn"


def test_summary_and_delegation_still_work() -> None:
    res_summary = client.get("/api/summary?range=7d")
    assert res_summary.status_code == 200

    res_delegation = client.get("/api/delegation?range=7d")
    assert res_delegation.status_code == 200


def test_session_health_empty_records_returns_empty_list() -> None:
    """session_health([]) should short-circuit to an empty list."""
    assert session_health([]) == []

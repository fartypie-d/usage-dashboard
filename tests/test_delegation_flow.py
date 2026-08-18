"""Tests for app/metrics/delegation_flow — 세션별 위임 흐름 재구성."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.metrics.delegation_flow import MAX_DEPTH, flows
from app.sources.claude_jsonl import Record

BASE = datetime(2026, 7, 21, 10, 0, tzinfo=UTC)

ROOT = "proj/root.jsonl"
KID_A = "proj/root/subagents/a.jsonl"
KID_B = "proj/root/subagents/b.jsonl"
KID_C = "proj/root/subagents/c.jsonl"


def _rec(
    *,
    source_file: str,
    session_id: str = "root-1",
    minute: int = 0,
    agent: str | None = None,
    source: str = "claude",
    cwd: str | None = "/anon/flowproj",
    parent_session_id: str | None = None,
    dispatcher_file: str | None = None,
    model: str = "claude-sonnet-4",
    input_tokens: int = 100,
    output_tokens: int = 10,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Record:
    return Record(
        project="flowproj",
        model=model,
        timestamp=BASE + timedelta(minutes=minute),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        session_id=session_id,
        source_file=source_file,
        source=source,
        agent=agent,
        cwd=cwd,
        parent_session_id=parent_session_id,
        dispatcher_file=dispatcher_file,
    )


def test_subagents_sharing_the_parent_session_id_stay_distinct_nodes() -> None:
    """세 자식이 부모의 sessionId를 물려받아도 별개 노드여야 한다 (D2b 회귀)."""
    records = [
        _rec(source_file=ROOT, minute=0),
        _rec(source_file=ROOT, minute=60),
        _rec(source_file=KID_A, minute=10, agent="python-reviewer",
             parent_session_id="root-1", dispatcher_file=ROOT),
        _rec(source_file=KID_B, minute=20, agent="security-reviewer",
             parent_session_id="root-1", dispatcher_file=ROOT),
    ]

    result, warnings = flows(records)

    assert warnings == []
    assert len(result) == 1
    flow = result[0]
    assert flow["node_id"] == ROOT
    assert flow["child_count"] == 2
    assert [c["node_id"] for c in flow["children"]] == [KID_A, KID_B]
    assert [c["depth"] for c in flow["children"]] == [1, 1]


def test_two_hop_child_is_attached_to_its_dispatcher_not_the_root() -> None:
    records = [
        _rec(source_file=ROOT, minute=0),
        _rec(source_file=KID_A, minute=10, agent="python-reviewer",
             parent_session_id="root-1", dispatcher_file=ROOT),
        _rec(source_file=KID_C, minute=15, agent="silent-failure-hunter",
             parent_session_id="root-1", dispatcher_file=KID_A),
    ]

    (flow,), warnings = flows(records)

    assert warnings == []
    kids = {c["node_id"]: c for c in flow["children"]}
    assert kids[KID_C]["parent_node_id"] == KID_A
    assert kids[KID_C]["depth"] == 2
    assert flow["two_hop_count"] == 1
    assert flow["child_count"] == 2


def test_orphan_subagent_is_dropped_and_warned() -> None:
    records = [
        _rec(source_file=ROOT, minute=0),
        _rec(source_file=KID_A, minute=10, agent="python-reviewer",
             parent_session_id="root-1", dispatcher_file=ROOT),
        _rec(source_file="other/x/subagents/y.jsonl", minute=12,
             agent="ghost", session_id="missing-1", parent_session_id="missing-1"),
    ]

    (flow,), warnings = flows(records)

    assert flow["child_count"] == 1
    assert warnings == ["부모를 찾지 못한 위임 세션 1개 — 타임라인에서 제외됩니다"]


def test_chain_deeper_than_max_depth_is_truncated_and_warned() -> None:
    records = [_rec(source_file=ROOT, minute=0)]
    previous = ROOT
    for level in range(1, MAX_DEPTH + 2):  # MAX_DEPTH+1 단계 → 마지막 하나가 잘린다
        path = f"proj/root/subagents/level{level}.jsonl"
        records.append(
            _rec(source_file=path, minute=level, agent=f"a{level}",
                 parent_session_id="root-1", dispatcher_file=previous)
        )
        previous = path

    (flow,), warnings = flows(records)

    assert flow["child_count"] == MAX_DEPTH
    assert max(c["depth"] for c in flow["children"]) == MAX_DEPTH
    assert f"깊이 상한({MAX_DEPTH})을 넘어 잘린 위임 1개" in warnings


def test_session_without_children_produces_no_flow() -> None:
    result, warnings = flows([_rec(source_file=ROOT, minute=0)])

    assert result == []
    assert warnings == []


def test_flows_are_sorted_by_total_cost_descending() -> None:
    cheap_root, cheap_kid = "proj/cheap.jsonl", "proj/cheap/subagents/k.jsonl"
    rich_root, rich_kid = "proj/rich.jsonl", "proj/rich/subagents/k.jsonl"
    records = [
        _rec(source_file=cheap_root, session_id="cheap-1", minute=0, input_tokens=10),
        _rec(source_file=cheap_kid, session_id="cheap-1", minute=1, agent="x",
             parent_session_id="cheap-1", dispatcher_file=cheap_root, input_tokens=10),
        _rec(source_file=rich_root, session_id="rich-1", minute=0,
             model="claude-opus-4", input_tokens=100_000),
        _rec(source_file=rich_kid, session_id="rich-1", minute=1, agent="y",
             parent_session_id="rich-1", dispatcher_file=rich_root, input_tokens=100_000),
    ]

    result, _warnings = flows(records)

    assert [f["node_id"] for f in result] == [rich_root, cheap_root]


def test_opencode_session_inside_the_root_window_is_inferred_as_a_child() -> None:
    records = [
        _rec(source_file=ROOT, minute=0),
        _rec(source_file=ROOT, minute=60),
        _rec(source_file=KID_A, minute=10, agent="python-reviewer",
             parent_session_id="root-1", dispatcher_file=ROOT),
        _rec(source_file="/anon/opencode.db", session_id="oc-in", minute=12,
             agent="dash-backend", source="opencode", model="gemini-3-pro"),
        _rec(source_file="/anon/opencode.db", session_id="oc-in", minute=35,
             agent="dash-backend", source="opencode", model="gemini-3-pro"),
    ]

    (flow,), _warnings = flows(records)

    kids = {c["node_id"]: c for c in flow["children"]}
    assert set(kids) == {KID_A, "oc-in"}
    assert kids["oc-in"]["inferred"] is True
    assert kids["oc-in"]["source"] == "opencode"
    assert kids["oc-in"]["depth"] == 1
    assert kids[KID_A]["inferred"] is False


def test_opencode_session_outside_the_window_or_cwd_is_not_attached() -> None:
    records = [
        _rec(source_file=ROOT, minute=0),
        _rec(source_file=ROOT, minute=60),
        _rec(source_file=KID_A, minute=10, agent="python-reviewer",
             parent_session_id="root-1", dispatcher_file=ROOT),
        # 시간 창 밖
        _rec(source_file="/anon/opencode.db", session_id="oc-late", minute=120,
             agent="w", source="opencode", model="gemini-3-flash"),
        # cwd 불일치
        _rec(source_file="/anon/opencode.db", session_id="oc-elsewhere", minute=20,
             agent="w", source="opencode", model="gemini-3-flash", cwd="/anon/other"),
    ]

    (flow,), _warnings = flows(records)

    assert [c["node_id"] for c in flow["children"]] == [KID_A]


def test_opencode_session_attaches_to_the_tightest_containing_root() -> None:
    wide, narrow = "proj/wide.jsonl", "proj/narrow.jsonl"
    records = [
        _rec(source_file=wide, session_id="wide-1", minute=0),
        _rec(source_file=wide, session_id="wide-1", minute=120),
        _rec(source_file="proj/wide/subagents/k.jsonl", session_id="wide-1", minute=5,
             agent="x", parent_session_id="wide-1", dispatcher_file=wide),
        _rec(source_file=narrow, session_id="narrow-1", minute=10),
        _rec(source_file=narrow, session_id="narrow-1", minute=40),
        _rec(source_file="proj/narrow/subagents/k.jsonl", session_id="narrow-1", minute=12,
             agent="y", parent_session_id="narrow-1", dispatcher_file=narrow),
        _rec(source_file="/anon/opencode.db", session_id="oc-in", minute=20,
             agent="z", source="opencode", model="gemini-3-pro"),
    ]

    result, _warnings = flows(records)
    owner = {
        f["node_id"]: [c["node_id"] for c in f["children"]] for f in result
    }

    assert "oc-in" in owner[narrow]
    assert "oc-in" not in owner[wide]


def test_overlapping_siblings_share_a_parallel_group() -> None:
    records = [
        _rec(source_file=ROOT, minute=0),
        _rec(source_file=ROOT, minute=60),
        # a: 10~30, b: 20~40 → 겹침
        _rec(source_file=KID_A, minute=10, agent="a", parent_session_id="root-1",
             dispatcher_file=ROOT),
        _rec(source_file=KID_A, minute=30, agent="a", parent_session_id="root-1",
             dispatcher_file=ROOT),
        _rec(source_file=KID_B, minute=20, agent="b", parent_session_id="root-1",
             dispatcher_file=ROOT),
        _rec(source_file=KID_B, minute=40, agent="b", parent_session_id="root-1",
             dispatcher_file=ROOT),
    ]

    (flow,), _warnings = flows(records)
    kids = {c["node_id"]: c for c in flow["children"]}

    assert kids[KID_A]["parallel_group"] == kids[KID_B]["parallel_group"] == 1
    assert flow["max_parallel"] == 2


def test_touching_intervals_are_not_parallel() -> None:
    """a가 끝나는 순간 b가 시작하는 건 겹침이 아니다."""
    records = [
        _rec(source_file=ROOT, minute=0),
        _rec(source_file=ROOT, minute=60),
        _rec(source_file=KID_A, minute=10, agent="a", parent_session_id="root-1",
             dispatcher_file=ROOT),
        _rec(source_file=KID_A, minute=20, agent="a", parent_session_id="root-1",
             dispatcher_file=ROOT),
        _rec(source_file=KID_B, minute=20, agent="b", parent_session_id="root-1",
             dispatcher_file=ROOT),
        _rec(source_file=KID_B, minute=30, agent="b", parent_session_id="root-1",
             dispatcher_file=ROOT),
    ]

    (flow,), _warnings = flows(records)
    kids = {c["node_id"]: c for c in flow["children"]}

    assert kids[KID_A]["parallel_group"] is None
    assert kids[KID_B]["parallel_group"] is None
    assert flow["max_parallel"] == 1


def test_only_child_has_no_parallel_group_but_counts_toward_max_parallel() -> None:
    """부모가 다르면 형제가 아니다 — 그룹은 없지만 동시 실행 수에는 들어간다."""
    records = [
        _rec(source_file=ROOT, minute=0),
        _rec(source_file=ROOT, minute=60),
        _rec(source_file=KID_A, minute=10, agent="a", parent_session_id="root-1",
             dispatcher_file=ROOT),
        _rec(source_file=KID_A, minute=30, agent="a", parent_session_id="root-1",
             dispatcher_file=ROOT),
        _rec(source_file=KID_C, minute=15, agent="c", parent_session_id="root-1",
             dispatcher_file=KID_A),
        _rec(source_file=KID_C, minute=25, agent="c", parent_session_id="root-1",
             dispatcher_file=KID_A),
    ]

    (flow,), _warnings = flows(records)
    kids = {c["node_id"]: c for c in flow["children"]}

    assert kids[KID_C]["depth"] == 2
    assert kids[KID_A]["parallel_group"] is None
    assert kids[KID_C]["parallel_group"] is None
    assert flow["max_parallel"] == 2


def test_fixture_corpus_produces_the_expected_flow() -> None:
    from pathlib import Path

    from app.sources.claude_jsonl import parse_directory
    from app.sources.opencode_db import read_records

    fixtures = Path(__file__).parent / "fixtures"
    claude_records, _cw = parse_directory(fixtures / "claude_projects")
    oc_records, _ow = read_records(fixtures / "opencode.db")

    result, _warnings = flows(claude_records + oc_records)
    by_id = {f["node_id"]: f for f in result}
    root_rel = str(Path("proj_flow01/root-sess-0001.jsonl"))

    flow = by_id[root_rel]
    assert flow["session_id"] == "root-sess-0001"
    assert flow["cwd"] == "/anon/flowproj"
    assert flow["child_count"] == 4
    assert flow["two_hop_count"] == 1
    assert flow["max_parallel"] == 4

    kids = {c["node_id"]: c for c in flow["children"]}
    a = str(Path("proj_flow01/root-sess-0001/subagents/child-a.jsonl"))
    b = str(Path("proj_flow01/root-sess-0001/subagents/child-b.jsonl"))
    c = str(Path("proj_flow01/root-sess-0001/subagents/child-c.jsonl"))

    assert kids[a]["agent"] == "python-reviewer"
    assert kids[b]["agent"] == "security-reviewer"
    assert kids[c]["agent"] == "silent-failure-hunter"
    assert kids[c]["parent_node_id"] == a
    assert kids[c]["depth"] == 2
    assert kids["oc-inside-01"]["inferred"] is True
    assert "oc-outside-01" not in kids

    # a, b, oc-inside-01은 루트의 형제이고 셋 다 겹친다 → 같은 그룹.
    assert kids[a]["parallel_group"] == kids[b]["parallel_group"]
    assert kids[a]["parallel_group"] == kids["oc-inside-01"]["parallel_group"]
    # c는 a의 외동 자식이므로 병렬 그룹이 없다.
    assert kids[c]["parallel_group"] is None


def test_instant_session_counts_toward_max_parallel() -> None:
    """레코드가 하나뿐인 순간 세션(start == end)도 살아 있던 것으로 센다."""
    records = [
        _rec(source_file=ROOT, minute=0),
        _rec(source_file=ROOT, minute=60),
        # 레코드 1개 → start == end == 10분
        _rec(source_file=KID_A, minute=10, agent="a", parent_session_id="root-1",
             dispatcher_file=ROOT),
    ]

    (flow,), _warnings = flows(records)

    assert flow["child_count"] == 1
    assert flow["max_parallel"] == 1


def test_instant_session_overlapping_a_sibling_is_counted() -> None:
    """긴 형제가 살아 있는 동안 순간 세션이 끼어들면 동시 실행 수는 2다."""
    records = [
        _rec(source_file=ROOT, minute=0),
        _rec(source_file=ROOT, minute=60),
        _rec(source_file=KID_A, minute=10, agent="a", parent_session_id="root-1",
             dispatcher_file=ROOT),
        _rec(source_file=KID_A, minute=30, agent="a", parent_session_id="root-1",
             dispatcher_file=ROOT),
        # 레코드 1개 → a의 구간 한가운데의 순간 세션
        _rec(source_file=KID_B, minute=20, agent="b", parent_session_id="root-1",
             dispatcher_file=ROOT),
    ]

    (flow,), _warnings = flows(records)
    kids = {c["node_id"]: c for c in flow["children"]}

    assert flow["max_parallel"] == 2
    assert kids[KID_A]["parallel_group"] == kids[KID_B]["parallel_group"] == 1


def test_flow_setup_cost_counts_only_cache_write_tokens() -> None:
    """자손의 input/output/cache_read만 커도 cache_write가 0이면 setup_cost는 0."""
    records = [
        _rec(source_file=ROOT, minute=0),
        _rec(
            source_file=KID_A,
            minute=10,
            agent="python-reviewer",
            parent_session_id="root-1",
            dispatcher_file=ROOT,
            input_tokens=100_000,
            output_tokens=50_000,
            cache_read_tokens=200_000,
            cache_write_tokens=0,
        ),
    ]

    (flow,), _warnings = flows(records)

    assert flow["setup_cost_usd"] == 0.0


def test_flow_setup_cost_excludes_the_root_node() -> None:
    """루트의 cache_write는 위임 부대비용이 아니므로 setup_cost에서 제외한다."""
    records = [
        _rec(source_file=ROOT, minute=0, cache_write_tokens=100_000),
        _rec(
            source_file=KID_A,
            minute=10,
            agent="python-reviewer",
            parent_session_id="root-1",
            dispatcher_file=ROOT,
            cache_write_tokens=0,
        ),
    ]

    (flow,), _warnings = flows(records)

    assert flow["setup_cost_usd"] == 0.0


def test_flow_cost_minus_self_equals_descendant_cost_sum() -> None:
    """불변식: cost_usd - self.cost_usd == Σ children[].cost_usd (D2 수식 계약)."""
    records = [
        _rec(source_file=ROOT, minute=0, input_tokens=1_000),
        _rec(
            source_file=KID_A,
            minute=10,
            agent="python-reviewer",
            parent_session_id="root-1",
            dispatcher_file=ROOT,
            input_tokens=5_000,
            cache_write_tokens=10_000,
        ),
        _rec(
            source_file=KID_B,
            minute=20,
            agent="security-reviewer",
            parent_session_id="root-1",
            dispatcher_file=ROOT,
            input_tokens=3_000,
            cache_write_tokens=8_000,
        ),
        _rec(
            source_file=KID_C,
            minute=15,
            agent="silent-failure-hunter",
            parent_session_id="root-1",
            dispatcher_file=KID_A,
            input_tokens=2_000,
            cache_write_tokens=4_000,
        ),
    ]

    (flow,), _warnings = flows(records)

    descendant_sum = sum(float(c["cost_usd"]) for c in flow["children"])
    assert float(flow["cost_usd"]) - float(flow["self"]["cost_usd"]) == pytest.approx(
        descendant_sum, abs=0.01
    )


def test_flow_delegation_share_is_within_zero_and_one() -> None:
    """모든 흐름의 delegation_share는 [0.0, 1.0] 구간에 있다."""
    records = [
        _rec(source_file=ROOT, minute=0, input_tokens=1_000),
        _rec(
            source_file=KID_A,
            minute=10,
            agent="python-reviewer",
            parent_session_id="root-1",
            dispatcher_file=ROOT,
            input_tokens=5_000,
            cache_write_tokens=10_000,
        ),
        _rec(
            source_file=KID_B,
            minute=20,
            agent="security-reviewer",
            parent_session_id="root-1",
            dispatcher_file=ROOT,
            input_tokens=500,
        ),
    ]

    result, _warnings = flows(records)

    assert result
    for flow in result:
        share = float(flow["delegation_share"])
        assert 0.0 <= share <= 1.0


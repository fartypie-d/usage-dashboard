"""Pure functions for delegation-chain metrics.

The metrics in this module operate on normalized :class:`Record` rows (or on
pre-built flow dicts for :func:`overhead`) and are completely free of side
effects.

Algorithm decisions documented here:

* ``overhead`` aggregates cost-allocation metrics over the flow dicts produced
  by ``delegation_flow.flows``.  It does **not** import that module — callers
  pass the dict array in, so unit tests need no file I/O.
* ``two_hop_count`` is injected by the caller (``main.py`` sums
  ``flow["two_hop_count"]`` across the same array) rather than re-walked here.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TypedDict

from app.metrics.common import record_cost, total_tokens
from app.sources.claude_jsonl import Record


class _ModelStats(TypedDict):
    """Per-model accumulator used inside :func:`agents`."""

    calls: int
    tokens: int
    cost_usd: float


def _new_model_stats() -> _ModelStats:
    """Return a fresh zero-initialised model-stats entry."""
    return {"calls": 0, "tokens": 0, "cost_usd": 0.0}


def flow(records: list[Record]) -> list[dict[str, object]]:
    """Return per-agent token/call totals for delegated records.

    ``agent is None`` records are excluded. Result is sorted by total tokens
    descending.
    """
    stats: dict[str, dict[str, int]] = {}
    for rec in records:
        if rec.agent is None:
            continue
        if rec.agent not in stats:
            stats[rec.agent] = {"tokens": 0, "calls": 0}
        stats[rec.agent]["tokens"] += total_tokens(rec)
        stats[rec.agent]["calls"] += 1

    return [
        {"agent": agent, **values}
        for agent, values in sorted(
            stats.items(), key=lambda item: item[1]["tokens"], reverse=True
        )
    ]


def agents(records: list[Record]) -> list[dict[str, object]]:
    """Return per-agent metrics including a model-level breakdown.

    ``agent is None`` records are excluded. For each delegated agent the
    function returns calls, total tokens, total cost, average turns per
    session, and a ``models`` array grouped by ``model`` and sorted by tokens
    descending.
    """
    by_agent: dict[str, list[Record]] = defaultdict(list)
    for rec in records:
        if rec.agent is None:
            continue
        by_agent[rec.agent].append(rec)

    result: list[dict[str, object]] = []
    for agent_name, recs in by_agent.items():
        # Pre-compute per-record cost once and reuse for both agent total and model breakdown.
        cost_by_rec: dict[int, float] = {id(r): record_cost(r) for r in recs}

        agent_tokens = sum(total_tokens(r) for r in recs)
        total_cost = sum(cost_by_rec[id(r)] for r in recs)
        unique_sessions = len({r.session_id for r in recs})
        avg_turns = len(recs) / unique_sessions if unique_sessions else 0.0

        model_stats: defaultdict[str, _ModelStats] = defaultdict(_new_model_stats)
        for rec in recs:
            mstats = model_stats[rec.model]
            mstats["calls"] += 1
            mstats["tokens"] += total_tokens(rec)
            mstats["cost_usd"] += cost_by_rec[id(rec)]

        models_list = [
            {
                "model": model_name,
                "calls": mstats["calls"],
                "tokens": mstats["tokens"],
                "cost_usd": round(mstats["cost_usd"], 2),
            }
            for model_name, mstats in model_stats.items()
        ]
        models_list.sort(key=lambda item: item["tokens"], reverse=True)

        result.append(
            {
                "agent": agent_name,
                "calls": len(recs),
                "tokens": agent_tokens,
                "cost_usd": round(total_cost, 2),
                "avg_turns": round(avg_turns, 1),
                "models": models_list,
            }
        )

    result.sort(key=lambda item: item["tokens"], reverse=True)
    return result


def overhead(
    flows: list[dict], *, two_hop_count: int
) -> tuple[dict[str, object], list[str]]:
    """Return delegation cost-allocation metrics over pre-built flow dicts.

    Formulas (design D2):

    * ``total_flow_cost_usd`` = Σ flow[\"cost_usd\"]
    * ``delegated_cost_usd``  = Σ max(0, cost − self.cost)
    * ``delegation_share``    = delegated / total   (0 when total is 0)
    * ``setup_cost_usd``      = Σ flow[\"setup_cost_usd\"]
    * ``work_cost_usd``       = max(0, delegated − setup)
    * ``setup_share``         = setup / delegated   (0 when delegated is 0)
    * ``flow_count``          = len(flows)
    * ``two_hop_count``       = caller-supplied value

    Damaged inputs emit Korean warning strings rather than silent clamps:
    self.cost > cost, or setup > delegated.
    """
    warnings: list[str] = []

    total_flow_cost_usd = 0.0
    delegated_cost_usd = 0.0
    setup_cost_usd = 0.0
    damaged_self_count = 0

    for flow_item in flows:
        cost = float(flow_item["cost_usd"])
        self_cost = float(flow_item["self"]["cost_usd"])
        setup = float(flow_item["setup_cost_usd"])

        total_flow_cost_usd += cost
        setup_cost_usd += setup

        raw_child = cost - self_cost
        if raw_child < 0.0:
            damaged_self_count += 1
        delegated_cost_usd += max(0.0, raw_child)

    if damaged_self_count:
        warnings.append(
            f"self.cost_usd가 cost_usd를 초과한 흐름 {damaged_self_count}개 "
            f"— 해당 흐름의 위임 비용을 0으로 클램프했습니다"
        )

    if total_flow_cost_usd > 0.0:
        delegation_share = delegated_cost_usd / total_flow_cost_usd
    else:
        delegation_share = 0.0

    work_cost_usd = max(0.0, delegated_cost_usd - setup_cost_usd)

    if delegated_cost_usd == 0.0:
        setup_share = 0.0
    elif setup_cost_usd > delegated_cost_usd:
        setup_share = 1.0
        work_cost_usd = 0.0
        warnings.append(
            f"setup_cost_usd(${setup_cost_usd:.2f})가 "
            f"delegated_cost_usd(${delegated_cost_usd:.2f})를 초과 — "
            f"setup_share를 1.0, work_cost_usd를 0.0으로 클램프했습니다"
        )
    else:
        setup_share = setup_cost_usd / delegated_cost_usd

    return (
        {
            "total_flow_cost_usd": round(total_flow_cost_usd, 2),
            "delegated_cost_usd": round(delegated_cost_usd, 2),
            "delegation_share": round(delegation_share, 3),
            "setup_cost_usd": round(setup_cost_usd, 2),
            "work_cost_usd": round(work_cost_usd, 2),
            "setup_share": round(setup_share, 3),
            "flow_count": len(flows),
            "two_hop_count": two_hop_count,
        },
        warnings,
    )

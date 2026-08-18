"""KPI calculation module for usage-dashboard backend."""

from __future__ import annotations

from typing import Any

from app.pricing import cost_for
from app.sources.claude_jsonl import Record


def compute_kpi(
    records: list[Record],
    *,
    mismatches_list: list[dict[str, Any]],
    worst_sessions_list: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """Compute KPI metrics from records.

    KPI metrics:
    - total_cost_usd: sum of cost_for across all records
    - total_tokens: sum of input + output + cache_read + cache_write tokens
    - cache_hit_rate: sum(cache_read) / (sum(input) + sum(cache_read)). If denominator is 0, 0.0
    - delegated_session_ratio: (session_ids with at least one record having agent != None)
      / (total unique session_ids). If total unique session_ids is 0, 0.0
    - anomaly_count: len(mismatches_list)
      + len([s for s in worst_sessions_list if s["cache_read_ratio"] < 0.05])

    Pre-computed arguments (avoids duplicate computation when the caller
    already needs mismatches and cache_metrics results):
    - mismatches_list: result of mismatches(records)
    - worst_sessions_list: result of cache_metrics(records)["worst_sessions"]

    Returns:
        tuple of (kpi_dict, warnings)
    """
    total_cost_usd = 0.0
    warnings: list[str] = []

    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_write = 0

    all_sessions: set[str] = set()
    delegated_sessions: set[str] = set()

    for r in records:
        cost, warn = cost_for(
            r.model,
            r.input_tokens,
            r.output_tokens,
            r.cache_read_tokens,
            r.cache_write_tokens,
        )
        total_cost_usd += cost
        if warn:
            warnings.extend(warn)

        total_input += r.input_tokens
        total_output += r.output_tokens
        total_cache_read += r.cache_read_tokens
        total_cache_write += r.cache_write_tokens

        all_sessions.add(r.session_id)
        if r.agent is not None:
            delegated_sessions.add(r.session_id)

    total_tokens = total_input + total_output + total_cache_read + total_cache_write

    cache_denom = total_input + total_cache_read
    cache_hit_rate = (total_cache_read / cache_denom) if cache_denom > 0 else 0.0

    total_session_count = len(all_sessions)
    delegated_session_ratio = (
        (len(delegated_sessions) / total_session_count)
        if total_session_count > 0
        else 0.0
    )

    worst_sessions = worst_sessions_list
    low_cache_worst_count = len(
        [s for s in worst_sessions if s.get("cache_read_ratio", 0.0) < 0.05]
    )
    anomaly_count = len(mismatches_list) + low_cache_worst_count

    kpi = {
        "total_cost_usd": total_cost_usd,
        "total_tokens": total_tokens,
        "cache_hit_rate": cache_hit_rate,
        "delegated_session_ratio": delegated_session_ratio,
        "anomaly_count": anomaly_count,
    }

    return kpi, warnings

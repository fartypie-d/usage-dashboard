"""Model mix, daily cost, and mismatch detection metrics.

This module provides three pure functions for aggregating usage data:
- project_mix: Group records by project, sum costs and tokens
- daily_cost: Group records by UTC date, sum daily costs
- mismatches: Detect expensive models used for simple work

Algorithm Decision (mismatch detection):
    Uses algorithm (a) with relaxed thresholds based on fixture analysis:
    - avg_output_tokens < 100 (simple responses)
    - model prefix-matches EXPENSIVE_MODELS
    Each session is defined by having at least one record, so no separate
    minimum-turns threshold is needed.
    
    Rationale: Fixture data shows 191 records across 191 sessions (1 turn each).
    9 sessions in fixtures where expensive models produce < 100 output
    tokens on average, indicating simple work that could use cheaper models.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from app.metrics.common import record_cost, total_tokens
from app.pricing import cost_for
from app.sources.claude_jsonl import Record

__all__ = ["project_mix", "daily_cost", "mismatches"]

# Expensive models that should be flagged for simple work
EXPENSIVE_MODELS = {"claude-opus-4", "claude-sonnet-4", "qwen3.7-max"}

# Suggested downgrade mapping
SUGGESTED_DOWNGRADE = {
    "claude-opus-4": "claude-haiku-4",
    "claude-sonnet-4": "claude-haiku-4",
    "qwen3.7-max": "qwen3.7-plus",
}

# Mismatch detection thresholds (relaxed for fixture data)
MIN_AVG_OUTPUT_TOKENS = 100


def _is_expensive_model(model: str) -> bool:
    """Check if model prefix-matches any expensive model."""
    return any(model.startswith(prefix) for prefix in EXPENSIVE_MODELS)


def _get_suggested_model(model: str) -> str | None:
    """Get suggested cheaper model based on prefix match."""
    for expensive, suggested in SUGGESTED_DOWNGRADE.items():
        if model.startswith(expensive):
            return suggested
    return None


def _aggregate_project(project: str, records: list[Record]) -> dict[str, Any]:
    """Aggregate cost and tokens for a single project."""
    total_cost = 0.0
    project_tokens = 0
    model_tokens: dict[str, int] = defaultdict(int)

    for rec in records:
        cost = record_cost(rec)
        total_cost += cost
        tokens = total_tokens(rec)
        project_tokens += tokens
        model_tokens[rec.model] += tokens

    by_model = sorted(
        [{"model": m, "tokens": t} for m, t in model_tokens.items()],
        key=lambda x: x["tokens"],
        reverse=True,
    )
    return {
        "project": project,
        "cost_usd": total_cost,
        "total_tokens": project_tokens,
        "by_model": by_model,
    }


def project_mix(records: list[Record]) -> list[dict[str, Any]]:
    """Group records by project and aggregate costs and tokens.

    Returns a list sorted by cost_usd descending. Each entry contains:
    - project: Project name
    - cost_usd: Total cost for all records in this project
    - total_tokens: Sum of input + output + cache_read + cache_write tokens
    - by_model: List of {model, tokens} sorted by tokens descending
    """
    if not records:
        return []

    by_project: dict[str, list[Record]] = defaultdict(list)
    for rec in records:
        by_project[rec.project].append(rec)

    result = [_aggregate_project(p, recs) for p, recs in by_project.items()]
    result.sort(key=lambda x: x["cost_usd"], reverse=True)
    return result


def daily_cost(
    records: list[Record], *, fill_gaps: bool = False
) -> list[dict[str, Any]]:
    """Group records by UTC date and sum daily costs.

    Returns a list sorted by date ascending. Each entry contains:
    - date: ISO date string (YYYY-MM-DD)
    - cost_usd: Total cost for that day

    When fill_gaps is False (default), only days with data are included.
    When fill_gaps is True, every calendar day from the minimum to maximum
    UTC date (inclusive) is present; missing days use cost_usd 0.0.
    Empty records always yield [].
    """
    if not records:
        return []

    # Group by date (UTC)
    by_date: dict[str, float] = defaultdict(float)
    for rec in records:
        date_str = rec.timestamp.astimezone(UTC).strftime("%Y-%m-%d")
        by_date[date_str] += record_cost(rec)

    if not fill_gaps:
        return [
            {"date": date, "cost_usd": cost}
            for date, cost in sorted(by_date.items())
        ]

    min_date = datetime.strptime(min(by_date), "%Y-%m-%d").date()
    max_date = datetime.strptime(max(by_date), "%Y-%m-%d").date()
    result: list[dict[str, Any]] = []
    current = min_date
    while current <= max_date:
        date_str = current.isoformat()
        result.append({"date": date_str, "cost_usd": by_date.get(date_str, 0.0)})
        current += timedelta(days=1)
    return result


def _calculate_session_cost(records: list[Record], model: str) -> float:
    """Calculate total cost for a session using a specific model."""
    total = 0.0
    for rec in records:
        cost, _ = cost_for(
            model,
            rec.input_tokens,
            rec.output_tokens,
            rec.cache_read_tokens,
            rec.cache_write_tokens,
        )
        total += cost
    return total


def _analyze_session(
    session_id: str, records: list[Record]
) -> dict[str, Any] | None:
    """Analyze a session for mismatch. Returns None if not a mismatch."""
    expensive_records = [r for r in records if _is_expensive_model(r.model)]
    if not expensive_records:
        return None

    model = expensive_records[0].model
    suggested = _get_suggested_model(model)
    if not suggested:
        return None

    turns = len(records)
    avg_output = sum(r.output_tokens for r in records) / turns

    if avg_output >= MIN_AVG_OUTPUT_TOKENS:
        return None

    total_cost = _calculate_session_cost(records, model)
    suggested_cost = _calculate_session_cost(records, suggested)
    savings = total_cost - suggested_cost

    severity = (
        "high" if savings >= 5.0 else "med" if savings >= 1.0 else "low"
    )

    return {
        "session_id": session_id,
        "project": records[0].project,
        "model": model,
        "severity": severity,
        "cost_usd": total_cost,
        "tokens": sum(total_tokens(r) for r in records),
        "turns": turns,
        "reason": f"평균 출력 {int(avg_output)} tok · {turns}회 턴",
        "avg_output_tokens": int(avg_output),
        "suggested_model": suggested,
        "estimated_savings_usd": savings,
    }


def mismatches(
    records: list[Record], *, top_n: int = 10
) -> list[dict[str, Any]]:
    """Detect expensive models used for simple work.

    Algorithm: Flags sessions where:
    - Model prefix-matches EXPENSIVE_MODELS
    - avg_output_tokens < MIN_AVG_OUTPUT_TOKENS (100)

    Returns top_n mismatches sorted by severity (high > med > low), then by
    estimated_savings_usd descending. Each entry contains:
    - session_id, project, model, severity, cost_usd, tokens, turns
    - reason: Korean summary
    - avg_output_tokens, suggested_model, estimated_savings_usd
    """
    if not records:
        return []

    by_session: dict[str, list[Record]] = defaultdict(list)
    for rec in records:
        by_session[rec.session_id].append(rec)

    candidates = [
        result
        for session_id, recs in by_session.items()
        if (result := _analyze_session(session_id, recs)) is not None
    ]

    severity_order = {"high": 0, "med": 1, "low": 2}
    candidates.sort(
        key=lambda x: (severity_order[x["severity"]], -x["estimated_savings_usd"])
    )
    return candidates[:top_n]

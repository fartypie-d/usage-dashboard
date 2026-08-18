"""Session health metrics module.

Computes context growth, compaction turns, token spikes, and session health status
for each unique session_id.

Algorithm decisions:
- compaction_turns: Candidate (a) selected. Checked via per-turn prompt context
  (input_tokens + cache_read_tokens). A drop of >30% relative to the previous turn
  (curr_turn < prev_turn * 0.7) indicates context compaction/reset event.
  Fixture analysis confirmed opencode.db data JSON has no explicit 'compaction' event field.
- token_spike: Candidate (a) selected. True if for any turn i >= 1, per-turn prompt context
  (input_tokens + cache_read_tokens) increases by 3x or more relative to previous turn
  (curr_turn >= prev_turn * 3, given prev_turn > 0).
- split_recommended: Candidate (a) selected. True if total cumulative context_growth
  reaches or exceeds 200,000 tokens (context_growth[-1] > 200_000).
- health derived status:
  - "danger": split_recommended AND token_spike
  - "warn": split_recommended OR token_spike OR len(compaction_turns) > 0
  - "ok": otherwise
- model: Counter mode (most common model across records in the session).
- project: Project of the earliest record in the session.
"""

from __future__ import annotations

from collections import Counter
from itertools import accumulate

from app.sources.claude_jsonl import Record


def session_health(records: list[Record]) -> list[dict]:
    """Calculate session health metrics for a list of records.

    Groups records by session_id, sorts each session by timestamp asc,
    and returns session health dictionaries.
    """
    if not records:
        return []

    # Group records by session_id
    grouped: dict[str, list[Record]] = {}
    for r in records:
        grouped.setdefault(r.session_id, []).append(r)

    results: list[dict] = []

    for session_id, session_recs in grouped.items():
        # Sort session records by timestamp asc
        sorted_recs = sorted(session_recs, key=lambda r: r.timestamp)

        # Basic metadata
        first_rec = sorted_recs[0]
        project = first_rec.project or "unknown"

        # Model: Counter mode (most common)
        model_counts = Counter(r.model for r in sorted_recs)
        most_common_model = model_counts.most_common(1)[0][0]

        turns = len(sorted_recs)

        # Per-turn prompt context (input + cache_read)
        per_turn_contexts = [r.input_tokens + r.cache_read_tokens for r in sorted_recs]

        # Cumulative context growth array
        context_growth = list(accumulate(per_turn_contexts))

        # Compaction turns detection
        compaction_turns: list[int] = []
        token_spike = False

        for i in range(1, turns):
            prev_turn = per_turn_contexts[i - 1]
            curr_turn = per_turn_contexts[i]

            # Compaction: drop by >30% (curr < prev * 0.7)
            if prev_turn > 0 and curr_turn < prev_turn * 0.7:
                compaction_turns.append(i)

            # Token spike: 3x or more increase (curr >= prev * 3)
            if prev_turn > 0 and curr_turn >= prev_turn * 3:
                token_spike = True

        # Split recommended: cumulative context > 200,000 tokens
        split_recommended = bool(context_growth and context_growth[-1] > 200_000)

        # Derived health status
        if split_recommended and token_spike:
            health = "danger"
        elif split_recommended or token_spike or len(compaction_turns) > 0:
            health = "warn"
        else:
            health = "ok"

        results.append(
            {
                "session_id": session_id,
                "project": project,
                "model": most_common_model,
                "turns": turns,
                "context_growth": context_growth,
                "compaction_turns": compaction_turns,
                "token_spike": token_spike,
                "split_recommended": split_recommended,
                "health": health,
            }
        )

    return results

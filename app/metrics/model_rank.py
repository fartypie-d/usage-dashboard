"""모델별 비용 순위 — 어떤 모델이 얼마를 쓰고, 그 안에서 어느 agent가 물고 있는가.

``model_mix``는 프로젝트를 1급 축으로 두므로 모델별 비용을 낼 수 없고,
``delegation``은 agent → 모델 방향이라 모델 기준 순위를 얻을 수 없다.
이 모듈이 모델을 1급 축으로 하는 유일한 집계다.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.metrics.common import record_cost, total_tokens
from app.sources.claude_jsonl import Record

__all__ = ["DIRECT_AGENT_LABEL", "model_rank"]

# agent가 없는 레코드(메인 세션에서 직접 실행한 턴)에 붙이는 표시용 라벨.
# 집계 키로는 절대 쓰지 않는다 — 실제로 이 이름을 가진 agent가 생기면 두 데이터가
# 한 행으로 합쳐져 조용히 틀린 숫자가 나온다. 그룹 키는 ``agent is None`` 여부다.
DIRECT_AGENT_LABEL = "직접(메인)"


def model_rank(records: list[Record]) -> list[dict[str, Any]]:
    """Rank models by spend, with a per-agent breakdown inside each model.

    비용은 반올림하지 않는다. 저렴한 모델이 여럿 들어오는데 ``round(0.004, 2)``는
    ``0.0``이 되어 순위와 점유율이 망가지기 때문이다. 포맷은 프론트가 한다.
    """
    model_cost: dict[str, float] = defaultdict(float)
    model_tokens: dict[str, int] = defaultdict(int)
    agent_cost: dict[str, dict[str | None, float]] = defaultdict(lambda: defaultdict(float))
    agent_tokens: dict[str, dict[str | None, int]] = defaultdict(lambda: defaultdict(int))

    for rec in records:
        # 레코드당 비용은 한 번만 계산해 모델 합계와 agent 합계에 함께 태운다.
        cost = record_cost(rec)
        tokens = total_tokens(rec)
        model_cost[rec.model] += cost
        model_tokens[rec.model] += tokens
        agent_cost[rec.model][rec.agent] += cost
        agent_tokens[rec.model][rec.agent] += tokens

    total_cost = sum(model_cost.values())

    rows: list[dict[str, Any]] = []
    for model, cost in model_cost.items():
        by_agent = [
            {
                "agent": DIRECT_AGENT_LABEL if agent is None else agent,
                "cost_usd": a_cost,
                "tokens": agent_tokens[model][agent],
            }
            for agent, a_cost in agent_cost[model].items()
        ]
        by_agent.sort(key=lambda a: (-a["cost_usd"], -a["tokens"], a["agent"]))
        rows.append(
            {
                "model": model,
                "cost_usd": cost,
                "cost_share": cost / total_cost if total_cost > 0 else 0.0,
                "tokens": model_tokens[model],
                "by_agent": by_agent,
            }
        )

    # 3단 타이브레이크 — 동점일 때도 순서가 결정적이어야 테스트가 안정적으로 통과한다.
    rows.sort(key=lambda r: (-r["cost_usd"], -r["tokens"], r["model"]))
    return rows

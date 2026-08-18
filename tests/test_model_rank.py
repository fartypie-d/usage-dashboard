"""Tests for app.metrics.model_rank — 모델별 비용 순위."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from app.sources.claude_jsonl import Record


def _rec(
    *,
    model: str = "claude-opus-4",
    agent: str | None = None,
    project: str = "proj-a",
    timestamp: datetime | None = None,
    input_tokens: int = 1000,
    output_tokens: int = 200,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    session_id: str = "sess-1",
    source_file: str = "f.jsonl",
) -> Record:
    if timestamp is None:
        timestamp = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)
    return Record(
        project=project,
        model=model,
        timestamp=timestamp,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        session_id=session_id,
        source_file=source_file,
        source="claude",
        agent=agent,
    )


def _cost_by_model(rates: dict[str, float]) -> Callable[..., tuple[float, list[str]]]:
    """``cost_for`` 대역 — 모델 이름만 보고 레코드당 고정 비용을 돌려준다.

    실제 단가표에 결합되지 않게 하려는 것. 여기서 검증하려는 것은 합산·점유율·정렬
    로직이지 단가가 아니다.
    """

    def _side_effect(model: str, *_args: int, **_kwargs: int) -> tuple[float, list[str]]:
        return (rates.get(model, 0.0), [])

    return _side_effect


def test_model_rank_groups_by_model_and_sums_cost_and_tokens() -> None:
    from app.metrics.model_rank import model_rank

    records = [
        _rec(
            model="opus",
            input_tokens=1000,
            output_tokens=200,
            cache_read_tokens=300,
            cache_write_tokens=100,
        ),
        _rec(model="opus", input_tokens=2000, output_tokens=400),
        _rec(model="haiku", input_tokens=500, output_tokens=100),
    ]

    with patch(
        "app.metrics.common.cost_for",
        side_effect=_cost_by_model({"opus": 10.0, "haiku": 1.0}),
    ):
        result = model_rank(records)

    assert [r["model"] for r in result] == ["opus", "haiku"]
    assert result[0]["cost_usd"] == pytest.approx(20.0)
    assert result[0]["tokens"] == 1600 + 2400
    assert result[1]["cost_usd"] == pytest.approx(1.0)
    assert result[1]["tokens"] == 600


def test_model_rank_cost_share_uses_total_cost_as_denominator() -> None:
    from app.metrics.model_rank import model_rank

    records = [_rec(model="opus"), _rec(model="haiku"), _rec(model="sonnet")]

    with patch(
        "app.metrics.common.cost_for",
        side_effect=_cost_by_model({"opus": 6.0, "haiku": 1.0, "sonnet": 3.0}),
    ):
        result = model_rank(records)

    shares = {r["model"]: r["cost_share"] for r in result}
    assert shares["opus"] == pytest.approx(0.6)
    assert shares["sonnet"] == pytest.approx(0.3)
    assert shares["haiku"] == pytest.approx(0.1)
    assert sum(shares.values()) == pytest.approx(1.0)


def test_model_rank_zero_total_cost_yields_zero_share_without_error() -> None:
    from app.metrics.model_rank import model_rank

    records = [_rec(model="opus"), _rec(model="haiku")]

    with patch(
        "app.metrics.common.cost_for",
        side_effect=_cost_by_model({}),
    ):
        result = model_rank(records)

    assert len(result) == 2
    assert all(r["cost_share"] == 0.0 for r in result)
    assert all(r["cost_usd"] == 0.0 for r in result)


def test_model_rank_labels_none_agent_with_direct_label() -> None:
    from app.metrics.model_rank import DIRECT_AGENT_LABEL, model_rank

    records = [
        _rec(model="opus", agent=None),
        _rec(model="opus", agent=None),
        _rec(model="opus", agent="web-ui"),
    ]

    with patch(
        "app.metrics.common.cost_for",
        side_effect=_cost_by_model({"opus": 5.0}),
    ):
        result = model_rank(records)

    by_agent = result[0]["by_agent"]
    assert [a["agent"] for a in by_agent] == [DIRECT_AGENT_LABEL, "web-ui"]
    assert by_agent[0]["cost_usd"] == pytest.approx(10.0)
    assert by_agent[0]["tokens"] == 2400
    assert by_agent[1]["cost_usd"] == pytest.approx(5.0)
    assert by_agent[1]["tokens"] == 1200


def test_model_rank_keeps_real_agent_named_like_direct_label_separate() -> None:
    """D3 회귀 방지 — 라벨 문자열을 그룹 키로 쓰면 두 데이터가 조용히 합쳐진다."""
    from app.metrics.model_rank import DIRECT_AGENT_LABEL, model_rank

    records = [
        _rec(model="opus", agent=None, input_tokens=1000, output_tokens=0),
        _rec(model="opus", agent=DIRECT_AGENT_LABEL, input_tokens=3000, output_tokens=0),
    ]

    with patch(
        "app.metrics.common.cost_for",
        side_effect=_cost_by_model({"opus": 2.0}),
    ):
        result = model_rank(records)

    rows = result[0]["by_agent"]
    assert len(rows) == 2
    assert [r["agent"] for r in rows] == [DIRECT_AGENT_LABEL, DIRECT_AGENT_LABEL]
    assert sorted(r["tokens"] for r in rows) == [1000, 3000]


def test_model_rank_sorts_ties_by_tokens_then_name() -> None:
    from app.metrics.model_rank import model_rank

    records = [
        _rec(model="zeta", input_tokens=1000, output_tokens=0),
        _rec(model="alpha", input_tokens=1000, output_tokens=0),
        _rec(model="beta", input_tokens=5000, output_tokens=0),
    ]

    with patch(
        "app.metrics.common.cost_for",
        side_effect=_cost_by_model({"zeta": 1.0, "alpha": 1.0, "beta": 1.0}),
    ):
        result = model_rank(records)

    assert [r["model"] for r in result] == ["beta", "alpha", "zeta"]


def test_model_rank_returns_empty_list_for_no_records() -> None:
    from app.metrics.model_rank import model_rank

    assert model_rank([]) == []


def test_model_rank_includes_unpriced_model_with_zero_cost() -> None:
    """D6 — 단가 미등록 모델도 토큰과 함께 순위 맨 아래에 남긴다."""
    from app.metrics.model_rank import model_rank

    records = [_rec(model="totally-unknown-model-x", input_tokens=9000, output_tokens=1000)]

    result = model_rank(records)

    assert len(result) == 1
    assert result[0]["model"] == "totally-unknown-model-x"
    assert result[0]["cost_usd"] == 0.0
    assert result[0]["cost_share"] == 0.0
    assert result[0]["tokens"] == 10000


def test_model_rank_computes_cost_once_per_record_via_cost_for() -> None:
    from app.metrics.model_rank import model_rank

    records = [
        _rec(
            model="opus",
            input_tokens=1,
            output_tokens=2,
            cache_read_tokens=3,
            cache_write_tokens=4,
        ),
        _rec(
            model="opus",
            input_tokens=5,
            output_tokens=6,
            cache_read_tokens=7,
            cache_write_tokens=8,
        ),
    ]

    with patch("app.metrics.common.cost_for") as mock_cost:
        mock_cost.return_value = (1.5, [])
        result = model_rank(records)

    assert mock_cost.call_count == 2
    assert mock_cost.call_args_list[0].args == ("opus", 1, 2, 3, 4)
    assert result[0]["cost_usd"] == pytest.approx(3.0)

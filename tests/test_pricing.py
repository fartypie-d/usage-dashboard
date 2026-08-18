"""Tests for the model pricing table."""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from app import pricing
from app.pricing import DEFAULT_PRICING, cost_for, load_pricing


@pytest.fixture
def pricing_env(tmp_path: Path) -> Iterator[Path]:
    """USAGE_PRICING_FILE을 임시 경로로 격리하고, 끝나면 전역 캐시를 되돌린다.

    ``refresh_pricing``은 모듈 전역을 갱신하므로 정리하지 않으면 뒤따르는
    테스트가 임시 단가표를 물고 간다.

    함수 스코프 ``monkeypatch``를 받아 ``undo()``하면 autouse
    ``_hermetic_pricing``이 건 setenv까지 함께 되돌아가, 정리 시점에 개발자의
    실제 ``config/pricing.json``이 전역으로 로드된다. 자체 context를 써서
    이 픽스처가 건 setenv만 되돌리면 순서에 의존하지 않는다.
    """
    path = tmp_path / "pricing.json"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("USAGE_PRICING_FILE", str(path))
        yield path
    pricing.refresh_pricing()


def test_cost_for_known_claude_model() -> None:
    """Claude Opus 4 pricing matches official Anthropic rates."""
    cost, warnings = cost_for("claude-opus-4", 1_000_000, 1_000_000)
    expected = 15.0 + 75.0  # $15 input + $75 output per 1M tokens
    assert abs(cost - expected) < 1e-6
    assert warnings == []


def test_cost_for_cache_tokens_uses_reduced_rate() -> None:
    """Anthropic cache read is cheaper than regular input tokens."""
    cache_only, _ = cost_for("claude-opus-4", 0, 0, cache_read_tokens=1_000_000)
    input_only, _ = cost_for("claude-opus-4", 1_000_000, 0)

    # Read price should be 10% of input price for Anthropic Opus 4
    assert abs(cache_only - 1.5) < 1e-6
    assert cache_only != input_only
    assert cache_only < input_only


def test_cost_for_cache_write_uses_125_percent_of_input() -> None:
    """Anthropic cache write is 125% of regular input token price."""
    model = "claude-opus-4"

    cost, warnings = cost_for(model, 0, 0, cache_read_tokens=0, cache_write_tokens=1_000_000)

    input_cost_per_million = 15.0
    expected = input_cost_per_million * 1.25
    assert abs(cost - expected) < 1e-6
    assert warnings == []


def test_cost_for_prefix_match_uses_base_model() -> None:
    """Versioned model identifiers fall back to the longest matching prefix."""
    versioned_model = "claude-opus-4-20241022"
    base_cost, _ = cost_for("claude-opus-4", 1_000_000, 1_000_000)

    versioned_cost, warnings = cost_for(versioned_model, 1_000_000, 1_000_000)

    assert versioned_cost == base_cost
    assert warnings == []


def test_cost_for_unknown_model_returns_zero_with_warning() -> None:
    """Unknown model returns zero cost and a warning containing the model name."""
    cost, warnings = cost_for("claude-nonexistent-9", 1_000_000, 1_000_000)
    assert cost == 0.0
    assert len(warnings) == 1
    assert "claude-nonexistent-9" in warnings[0]


def test_cost_for_zero_tokens_returns_zero() -> None:
    """Zero tokens returns zero cost and no warnings for a known model."""
    cost, warnings = cost_for("claude-opus-4", 0, 0)
    assert cost == 0.0
    assert warnings == []


def test_load_pricing_returns_defaults_when_file_absent(tmp_path: Path) -> None:
    table, warnings = load_pricing(tmp_path / "nope.json")

    assert table == DEFAULT_PRICING
    assert warnings == []


def test_load_pricing_merges_per_model_over_defaults(tmp_path: Path) -> None:
    path = tmp_path / "pricing.json"
    path.write_text(
        json.dumps(
            {
                "models": {
                    "claude-opus-5": {
                        "input": 15.0,
                        "output": 75.0,
                        "cache_read": 1.5,
                        "cache_write": 18.75,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    table, warnings = load_pricing(path)

    assert table["claude-opus-5"]["input"] == 15.0
    # 기본 표의 다른 모델은 그대로 남는다
    assert table["claude-sonnet-4"] == DEFAULT_PRICING["claude-sonnet-4"]
    assert warnings == []


def test_load_pricing_overrides_existing_model(tmp_path: Path) -> None:
    path = tmp_path / "pricing.json"
    path.write_text(
        json.dumps(
            {
                "models": {
                    "claude-opus-4": {
                        "input": 99.0,
                        "output": 99.0,
                        "cache_read": 9.9,
                        "cache_write": 9.9,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    table, _ = load_pricing(path)

    assert table["claude-opus-4"]["input"] == 99.0


def test_load_pricing_warns_and_keeps_defaults_on_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "pricing.json"
    path.write_text("{not json", encoding="utf-8")

    table, warnings = load_pricing(path)

    assert table == DEFAULT_PRICING
    assert len(warnings) == 1
    assert "pricing" in warnings[0].lower()


def test_load_pricing_skips_entry_missing_rate_keys(tmp_path: Path) -> None:
    path = tmp_path / "pricing.json"
    path.write_text(
        json.dumps(
            {
                "models": {
                    "broken-model": {"input": 1.0},
                    "good-model": {
                        "input": 1.0,
                        "output": 2.0,
                        "cache_read": 0.1,
                        "cache_write": 1.0,
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    table, warnings = load_pricing(path)

    assert "broken-model" not in table
    assert table["good-model"]["output"] == 2.0
    assert len(warnings) == 1
    assert "broken-model" in warnings[0]


def test_load_pricing_warns_on_non_numeric_rate(tmp_path: Path) -> None:
    path = tmp_path / "pricing.json"
    path.write_text(
        json.dumps(
            {
                "models": {
                    "bad-rate": {
                        "input": "free",
                        "output": 1.0,
                        "cache_read": 0.1,
                        "cache_write": 1.0,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    table, warnings = load_pricing(path)

    assert "bad-rate" not in table
    assert len(warnings) == 1


def test_load_pricing_rejects_boolean_rate(tmp_path: Path) -> None:
    """isinstance(True, int)이 True이므로 bool은 명시적으로 걸러야 한다."""
    path = tmp_path / "pricing.json"
    path.write_text(
        json.dumps(
            {
                "models": {
                    "bool-rate": {
                        "input": True,
                        "output": 1.0,
                        "cache_read": 0.1,
                        "cache_write": 1.0,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    table, warnings = load_pricing(path)

    assert "bool-rate" not in table
    assert len(warnings) == 1
    assert "bool-rate" in warnings[0]


def test_refresh_pricing_reloads_when_file_changes(pricing_env: Path) -> None:
    """실행 중인 프로세스가 단가 파일 변경을 재시작 없이 반영해야 한다.

    회귀 가드 (2026-07-26): ``PRICING``을 import 시점에 한 번만 바인딩한 탓에
    config/pricing.json에 grok-4.5를 추가해도 24시간째 떠 있던 컨테이너는
    끝까지 "unknown model: grok-4.5"를 뱉으며 465건을 $0으로 계산했다.
    """
    rates = {"input": 7.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}
    pricing_env.write_text(json.dumps({"models": {}}), encoding="utf-8")
    pricing.refresh_pricing()

    before, warnings_before = cost_for("brand-new-model", 1_000_000, 0)
    assert before == 0.0
    assert warnings_before, "등록 전에는 unknown model 경고가 나와야 한다"

    pricing_env.write_text(
        json.dumps({"models": {"brand-new-model": rates}}), encoding="utf-8"
    )
    pricing.refresh_pricing()

    after, warnings_after = cost_for("brand-new-model", 1_000_000, 0)
    assert abs(after - 7.0) < 1e-6
    assert warnings_after == []


def test_refresh_pricing_picks_up_a_file_created_after_startup(
    pricing_env: Path,
) -> None:
    """운영자의 첫 설정(`cp pricing.example.json pricing.json`) 시나리오.

    서버가 이미 떠 있고 단가 파일이 아직 없는 상태에서 시작한다. 이때
    ``_current_stamp``는 ``stat()``의 ``OSError``를 삼키고 ``(-1, -1)`` 지문을
    반환하는데, 파일이 생긴 뒤 이 지문이 실제 mtime/size로 바뀌지 않으면
    재시작 전까지 새 단가를 영원히 못 본다.
    """
    assert not pricing_env.exists()
    pricing.refresh_pricing()

    before, warnings_before = cost_for("late-arriving-model", 1_000_000, 0)
    assert before == 0.0
    assert warnings_before, "파일이 없으면 unknown model 경고가 나와야 한다"

    pricing_env.write_text(
        json.dumps(
            {
                "models": {
                    "late-arriving-model": {
                        "input": 3.0,
                        "output": 0.0,
                        "cache_read": 0.0,
                        "cache_write": 0.0,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    pricing.refresh_pricing()

    after, warnings_after = cost_for("late-arriving-model", 1_000_000, 0)
    assert abs(after - 3.0) < 1e-6
    assert warnings_after == []


def test_pricing_table_loads_lazily_on_first_use(pricing_env: Path) -> None:
    """``_stamp``가 아직 없으면 접근자가 스스로 첫 로드를 수행해야 한다.

    ``refresh_pricing()``을 부르지 않은 코드 경로(예: import 직후 첫 요청)에서
    빈 전역 ``_table``이 그대로 새어 나가면 모든 모델이 미등록으로 계산된다.
    """
    pricing_env.write_text(
        json.dumps(
            {
                "models": {
                    "lazy-model": {
                        "input": 5.0,
                        "output": 0.0,
                        "cache_read": 0.0,
                        "cache_write": 0.0,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    pricing._stamp = None
    pricing._table = {}
    pricing._warnings = []

    assert pricing.pricing_table()["lazy-model"]["input"] == 5.0
    assert pricing.pricing_warnings() == []


def test_load_pricing_warns_when_configured_file_is_missing(pricing_env: Path) -> None:
    """USAGE_PRICING_FILE을 지정했는데 파일이 없으면 조용히 넘어가면 안 된다."""
    assert not pricing_env.exists()

    table, warnings = load_pricing()

    assert table == DEFAULT_PRICING
    assert len(warnings) == 1
    assert str(pricing_env) in warnings[0]


def test_load_pricing_stays_silent_when_default_path_absent(tmp_path: Path) -> None:
    """경로를 명시해 호출한 경우에는 파일이 없어도 경고하지 않는다 (선택 설정)."""
    table, warnings = load_pricing(tmp_path / "nope.json")

    assert table == DEFAULT_PRICING
    assert warnings == []


def test_unpriced_vendor_model_costs_zero_but_warns(pricing_env: Path) -> None:
    """벤더 모델 단가 누락은 조용한 $0이 아니라 경고여야 한다 (2026-07-26 회귀).

    원 사고: grok-4.5가 단가표 어디에도 없어 465건이 경고 없이 $0으로 집계됐다.
    벤더 단가를 ``DEFAULT_PRICING``에서 ``config/pricing.json``으로 옮긴 뒤에는
    코드 쪽 폴백이 사라지므로, 이 경고가 유일한 안전망이다. 경고가 사라지면
    같은 사고가 그대로 재현된다.
    """
    pricing_env.write_text(json.dumps({"models": {}}), encoding="utf-8")
    pricing.refresh_pricing()

    cost, warnings = cost_for("grok-4.5", 1_000_000, 1_000_000)

    assert cost == 0.0
    assert any("grok-4.5" in w for w in warnings), (
        "미등록 모델은 이름이 담긴 경고를 내야 대시보드에서 눈에 띈다"
    )


@pytest.mark.parametrize(
    ("model", "input_rate", "output_rate"),
    [
        ("grok-4.5", 2.00, 6.00),
        ("qwen3.7-plus", 0.40, 1.60),
        ("gemini-3.6-flash", 1.50, 7.50),
    ],
)
def test_vendor_rates_come_from_the_config_file(
    pricing_env: Path, model: str, input_rate: float, output_rate: float
) -> None:
    """벤더 단가는 코드 수정 없이 config/pricing.json만 고쳐 반영돼야 한다."""
    pricing_env.write_text(
        json.dumps(
            {
                "models": {
                    model: {
                        "input": input_rate,
                        "output": output_rate,
                        "cache_read": 0.0,
                        "cache_write": 0.0,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    pricing.refresh_pricing()

    cost, warnings = cost_for(model, 1_000_000, 1_000_000)

    assert warnings == []
    assert abs(cost - (input_rate + output_rate)) < 1e-6


def test_default_pricing_covers_claude_families_only() -> None:
    """``DEFAULT_PRICING``은 Claude 폴백만 남긴다 — 나머지는 설정 파일 소관.

    드리프트 가드: 벤더 단가를 코드에 다시 넣으면 설정 파일과 두 곳에 같은
    숫자가 생겨, 한쪽만 고쳤을 때 어느 쪽이 이기는지 알 수 없게 된다.
    """
    assert all(name.startswith("claude-") for name in DEFAULT_PRICING), (
        f"Claude 외 모델이 코드에 남아 있다: "
        f"{sorted(n for n in DEFAULT_PRICING if not n.startswith('claude-'))}"
    )


def test_load_pricing_warns_on_invalid_utf8(tmp_path: Path) -> None:
    """부적절한 UTF-8 바이트로 인한 크래시 방지 (UnicodeDecodeError)."""
    path = tmp_path / "pricing.json"
    # UTF-8로 디코딩할 수 없는 바이트 시퀀스
    path.write_bytes(b"\xff\xfe\x00broken")

    table, warnings = load_pricing(path)

    assert table == DEFAULT_PRICING
    assert len(warnings) == 1
    assert "pricing" in warnings[0].lower()

"""Per-model token pricing table and cost calculator.

All prices are stored as USD per 1_000_000 tokens.

``DEFAULT_PRICING`` is a **fallback for Anthropic Claude families only**
(source: https://www.anthropic.com/pricing). Every other vendor — Gemini,
Qwen, Kimi, GLM, Grok, … — is managed in ``config/pricing.json`` instead of
here, because those list prices change often and a price update should not
require a code change and a redeploy. A model priced in neither place costs
$0 and raises an ``unknown model`` warning that the dashboard surfaces, so
the gap is visible rather than silent.
"""

import json
import os
from pathlib import Path

RATE_KEYS = ("input", "output", "cache_read", "cache_write")
DEFAULT_PRICING_PATH = Path("config/pricing.json")


DEFAULT_PRICING: dict[str, dict[str, float]] = {
    # Anthropic Claude (per 1M tokens)
    # Source: https://www.anthropic.com/pricing (Opus 4.x / Sonnet 4.x / Haiku 4.x)
    "claude-opus-4": {
        "input": 15.00,
        "output": 75.00,
        "cache_read": 1.50,
        "cache_write": 18.75,
    },
    "claude-sonnet-4": {
        "input": 3.00,
        "output": 15.00,
        "cache_read": 0.30,
        "cache_write": 3.75,
    },
    "claude-haiku-4": {
        "input": 1.00,
        "output": 5.00,
        "cache_read": 0.10,
        "cache_write": 1.25,
    },
    # 그 외 벤더(Gemini·Qwen·Kimi·GLM·Grok 등)는 여기가 아니라
    # config/pricing.json에서 관리한다 — 모듈 docstring 참조.
}


def _valid_rates(entry: object) -> dict[str, float] | None:
    """Return the rate dict if it has all four numeric keys, else None."""
    if not isinstance(entry, dict):
        return None
    rates: dict[str, float] = {}
    for key in RATE_KEYS:
        value = entry.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        rates[key] = float(value)
    return rates


def _pricing_path() -> Path:
    """Resolve the pricing file path from the environment or the default.

    ``or`` 형식을 쓰는 이유: ``USAGE_PRICING_FILE``이 빈 문자열("")로 설정된
    경우 ``Path("")``는 ``PosixPath('.')``(현재 디렉터리)가 되어 mtime 추적이
    cwd를 가리키게 된다. ``or``를 사용하면 빈 문자열을 미설정으로 처리해
    항상 올바른 경로를 반환한다.
    """
    env_path = os.getenv("USAGE_PRICING_FILE")
    return Path(env_path or str(DEFAULT_PRICING_PATH))


def load_pricing(
    path: Path | None = None,
) -> tuple[dict[str, dict[str, float]], list[str]]:
    """Merge per-model overrides from a JSON file over ``DEFAULT_PRICING``.

    The file is optional. Malformed files and invalid entries produce warnings
    rather than exceptions — an unreadable price table must not take the
    dashboard down. Rates are USD per 1,000,000 tokens.
    """
    env_path = os.getenv("USAGE_PRICING_FILE")
    # 운영자가 경로를 명시했는데 파일이 없는 것은 오설정이다. 반면 기본 경로에
    # 파일이 없는 것은 정상(설정 파일은 선택)이므로 경고하지 않는다.
    misconfigured = path is None and bool(env_path)
    if path is None:
        path = _pricing_path()

    table = {name: dict(rates) for name, rates in DEFAULT_PRICING.items()}
    if not path.exists():
        if misconfigured:
            return table, [
                f"pricing 설정 파일을 찾지 못했습니다 ({path}) — 내장 기본 단가표만 사용합니다"
            ]
        return table, []

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return table, [f"pricing 설정을 읽지 못했습니다 ({path}): {exc}"]

    models = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(models, dict):
        return table, [f"pricing 설정에 'models' 객체가 없습니다 ({path})"]

    warnings: list[str] = []
    for name, entry in models.items():
        rates = _valid_rates(entry)
        if rates is None:
            warnings.append(
                f"pricing 항목을 건너뜁니다 — {name}: "
                f"{', '.join(RATE_KEYS)} 네 개의 숫자 값이 모두 필요합니다"
            )
            continue
        table[name] = rates

    return table, warnings


_table: dict[str, dict[str, float]] = {}
_warnings: list[str] = []
_stamp: tuple[str, int, int] | None = None


def _current_stamp() -> tuple[str, int, int]:
    """Identify the pricing file's current state (path, mtime, size).

    ``size``까지 보는 이유는 파일시스템 타임스탬프 해상도가 낮을 때 짧은 간격의
    연속 수정이 같은 mtime을 갖는 경우를 잡기 위해서다.
    """
    path = _pricing_path()
    try:
        st = path.stat()
    except OSError:
        return (str(path), -1, -1)
    return (str(path), st.st_mtime_ns, st.st_size)


def refresh_pricing() -> None:
    """Reload the pricing table if the config file changed since last load.

    가격표를 import 시점에 한 번만 묶어두면, config/pricing.json에 모델을
    추가해도 이미 떠 있는 서버 프로세스는 재시작 전까지 영원히 알지 못한다.
    요청 경계에서 이 함수를 호출해 그 창을 닫는다. 파일이 그대로면 stat 한 번
    외에 아무 일도 하지 않으므로 요청당 비용은 무시할 만하다.
    """
    global _table, _warnings, _stamp

    stamp = _current_stamp()
    if stamp == _stamp:
        return
    _table, _warnings = load_pricing()
    _stamp = stamp


def pricing_table() -> dict[str, dict[str, float]]:
    """Return the active pricing table, loading it on first use."""
    if _stamp is None:
        refresh_pricing()
    return _table


def pricing_warnings() -> list[str]:
    """Return warnings produced while loading the active pricing table."""
    if _stamp is None:
        refresh_pricing()
    return list(_warnings)


def cost_for(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> tuple[float, list[str]]:
    """Calculate the estimated USD cost for a model invocation.

    Prices are looked up from the active table (see ``pricing_table``). If
    ``model`` is not found exactly,
    the longest matching prefix is used. Unknown models return ``0.0`` and a
    warning so that silent under-reporting is avoided.

    Args:
        model: Exact or prefix-matching model identifier.
        input_tokens: Number of input tokens.
        output_tokens: Number of output tokens.
        cache_read_tokens: Number of cache-read tokens.
        cache_write_tokens: Number of cache-write tokens.

    Returns:
        A tuple ``(cost_usd, warnings)``.
    """
    table = pricing_table()
    rates = table.get(model)
    if rates is None:
        matching = [name for name in table if model.startswith(name)]
        if not matching:
            return 0.0, [f"unknown model: {model}"]
        best_match = max(matching, key=len)
        rates = table[best_match]

    cost = (
        input_tokens * rates["input"]
        + output_tokens * rates["output"]
        + cache_read_tokens * rates["cache_read"]
        + cache_write_tokens * rates["cache_write"]
    ) / 1_000_000

    return cost, []

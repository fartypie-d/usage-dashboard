"""진행내역 경고를 코드·심각도로 분류하고 같은 종류끼리 묶는다.

경고를 문자열로 흘려보내면 표시 계층에 묶을 근거가 없다. 구조는 발생 지점에서
만들고, 사람이 읽는 문구(``message``)는 그대로 보존한다.
"""

from __future__ import annotations

from dataclasses import dataclass

SEVERITY_ERROR = "error"
SEVERITY_WARN = "warn"
SEVERITY_INFO = "info"

_SEVERITY_ORDER = {SEVERITY_ERROR: 0, SEVERITY_WARN: 1, SEVERITY_INFO: 2}

# 항목이 2개 이상인 그룹의 요약에 쓰는 코드별 고정 문구. 원래 메시지에서 페이즈
# 번호만 뺀 형태다 — 요약이 원래 문구의 핵심 단어를 잃으면 배너만 보고는 무슨
# 일인지 알 수 없고, 부분 문자열을 단언하는 기존 테스트도 조용히 깨진다.
_GROUP_PHRASE = {
    "phase_gap_internal": "지시서를 찾지 못했습니다 (번호 구멍)",
    "verdict_table_missing": "검수 결과 표 없음 — task 판정 미상",
    "doc_duplicate": "지시서가 둘 이상입니다",
    "frontmatter_missing": "frontmatter 없음 — 파일명에서 복원",
}

_MAX_LISTED_PHASES = 5


@dataclass(frozen=True)
class Warning:
    """경고 하나. frozen 이라 해시 가능하고 dict.fromkeys 로 중복이 걷힌다."""

    code: str
    severity: str
    message: str
    phase: int | None = None


def from_messages(
    messages: list[str], code: str, severity: str = SEVERITY_WARN
) -> list[Warning]:
    """아직 구조가 없는 원천(레지스트리 등)의 문자열 목록을 그대로 감싼다."""
    return [Warning(code=code, severity=severity, message=m) for m in messages]


def group_warnings(warnings: list[Warning]) -> list[dict]:
    """같은 code 끼리 묶어 severity → code 순으로 정렬한 그룹 목록."""
    buckets: dict[str, list[Warning]] = {}
    for warning in warnings:
        buckets.setdefault(warning.code, []).append(warning)

    groups = [_group(code, items) for code, items in buckets.items()]
    groups.sort(key=lambda g: (_SEVERITY_ORDER[g["severity"]], g["code"]))
    return groups


def summaries(groups: list[dict]) -> list[str]:
    """평평한 ``warnings`` 목록 — 그룹이 유일한 진실의 원천이다."""
    return [group["summary"] for group in groups]


def _normalize(severity: str) -> str:
    # 모르는 심각도를 버리면 경고 계층이 경고를 삼킨다. warn 으로 낙하시킨다.
    return severity if severity in _SEVERITY_ORDER else SEVERITY_WARN


def _group(code: str, items: list[Warning]) -> dict:
    # phase 없는 항목은 뒤로. sorted 는 안정 정렬이라 그들끼리는 원래 순서를 지킨다.
    ordered = sorted(items, key=lambda w: (w.phase is None, w.phase if w.phase is not None else 0))
    severity = min(
        (_normalize(w.severity) for w in ordered), key=lambda s: _SEVERITY_ORDER[s]
    )
    return {
        "code": code,
        "severity": severity,
        "count": len(ordered),
        "summary": _summary(code, ordered),
        "items": [w.message for w in ordered],
    }


def _summary(code: str, items: list[Warning]) -> str:
    if len(items) == 1:
        return items[0].message
    if code == "phase_gap_pre_history":
        return _pre_history_summary(items)

    phrase = _GROUP_PHRASE.get(code)
    numbers = [w.phase for w in items if w.phase is not None]
    if phrase is None or not numbers:
        return f"{items[0].message} 외 {len(items) - 1}건"
    return f"{_phase_list(numbers)} — {phrase}, 총 {len(items)}건"


def _phase_list(numbers: list[int]) -> str:
    head = "·".join(str(n) for n in numbers[:_MAX_LISTED_PHASES])
    rest = len(numbers) - _MAX_LISTED_PHASES
    return f"Phase {head}" + (f" 외 {rest}건" if rest > 0 else "")


def _pre_history_summary(items: list[Warning]) -> str:
    numbers = [w.phase for w in items if w.phase is not None]
    if not numbers:
        return f"{items[0].message} 외 {len(items) - 1}건"
    lo, hi = min(numbers), max(numbers)
    # 정의상 1 … (첫 문서화 페이즈 - 1) 이므로 hi + 1 이 문서화 시작점이다.
    return (
        f"Phase {lo}–{hi} 지시서를 찾지 못했습니다 (번호 구멍) — "
        f"문서화 시작(Phase {hi + 1}) 이전 {len(items)}건"
    )

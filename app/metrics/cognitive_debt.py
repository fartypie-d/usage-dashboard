"""인지부채 신호 — phase별 '설명 가능성' 부채를 문서 증거로 근사한다.

점수를 지어내지 않는다: 신호를 나열하고 3단계 등급만 붙인다.
- repaid   (상환됨): 최신 쪽지시험의 미해소 갭 0
- partial  (부분):   시험에 갭 잔존 / 시험 없이 판정·리뷰는 온전
- unrepaid (미상환): 시험 없음 + (판정 미상 task 존재 or 리뷰 부재)

쪽지시험 기록 규약: <docs_root>/quizzes/PHASE<n>_<YYYYMMDD[-HHMMSS]>.md
(리뷰 문서 <docs_root>/reviews/ 와 같은 축 — 레지스트리 root+docs_dir 해석을 재사용)
"""

from __future__ import annotations

import re
from pathlib import Path

from app.metrics.progress_docs import parse_frontmatter
from app.metrics.progress_warnings import SEVERITY_WARN, Warning

_QUIZ_FILE_RE = re.compile(r"^PHASE(\d+)_(\d{8}(?:-\d{6})?)\.md$")


def _stamp_key(name: str) -> str:
    """스탬프 정렬키: bare date (YYYYMMDD) → YYYYMMDD-000000으로 정규화.

    ASCII '.'>'-' 함정 회피 — 같은 날 bare-date와 timestamped를 올바르게 정렬하기 위해,
    bare date는 그 날의 시작(00:00:00)으로 간주한다.
    """
    m = _QUIZ_FILE_RE.match(name)
    if not m:
        return ""
    stamp = m.group(2)
    return stamp if "-" in stamp else stamp + "-000000"


LEVEL_REPAID = "repaid"
LEVEL_PARTIAL = "partial"
LEVEL_UNREPAID = "unrepaid"


def quiz_records(docs_root: Path) -> tuple[dict[int, list[dict]], list[Warning]]:
    """quizzes/ 의 기록을 phase별로, 스탐프 내림차순(최신 우선)으로 모은다.

    같은 날짜에서도 -HHMMSS 타임스탬프가 있는 파일이 bare date보다 최신으로 정렬된다.
    """
    quiz_dir = Path(docs_root) / "quizzes"
    if not quiz_dir.is_dir():
        return {}, []

    warnings: list[Warning] = []
    by_phase: dict[int, list[dict]] = {}
    for path in sorted(quiz_dir.glob("PHASE*.md")):
        m = _QUIZ_FILE_RE.match(path.name)
        if not m:
            continue
        phase = int(m.group(1))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as err:
            warnings.append(Warning(
                code="quiz_read_failed", severity=SEVERITY_WARN,
                message=f"{path.name} 을 읽지 못했습니다 ({err.__class__.__name__})",
                phase=phase,
            ))
            continue
        meta, body = parse_frontmatter(text)
        if not isinstance(meta.get("gaps_unresolved"), int):
            # 갭 수를 모르면 상환 여부를 판정할 수 없다 — 지어내지 않고 제외한다.
            warnings.append(Warning(
                code="quiz_meta_invalid", severity=SEVERITY_WARN,
                message=f"{path.name}: gaps_unresolved 를 읽지 못했습니다 — 지표에서 제외",
                phase=phase,
            ))
            continue
        by_phase.setdefault(phase, []).append({
            "phase": phase,
            "name": path.name,
            "date": meta.get("date"),
            "gaps_found": meta.get("gaps_found"),
            "gaps_unresolved": meta["gaps_unresolved"],
            "model": meta.get("model"),
            "cost": meta.get("cost"),
            "body": body,
        })

    # 각 phase별로 스탐프 내림차순으로 정렬 (최신 우선)
    for records in by_phase.values():
        records.sort(key=lambda r: _stamp_key(r["name"]), reverse=True)

    return by_phase, warnings


def debt_for(phase_row: dict, records: list[dict]) -> dict:
    """phase 행 + 시험 기록 → 부채 신호·등급. 신호는 등급과 독립적으로 전부 나열한다."""
    tasks = phase_row.get("tasks") or []
    unknown = sum(1 for t in tasks if t.get("verdict") is None)
    review_missing = not phase_row.get("review_path")
    latest = records[0] if records else None

    signals: list[str] = []
    if unknown:
        signals.append(f"판정 미상 task {unknown}개")
    if review_missing:
        signals.append("리뷰 문서 없음")

    if latest is None:
        signals.append("쪽지시험 기록 없음")
        level = LEVEL_UNREPAID if (unknown or review_missing) else LEVEL_PARTIAL
    elif latest["gaps_unresolved"] > 0:
        signals.append(f"쪽지시험 미해소 갭 {latest['gaps_unresolved']}개")
        level = LEVEL_PARTIAL
    else:
        level = LEVEL_REPAID

    return {
        "level": level,
        "signals": signals,
        "quiz_count": len(records),
        "latest_quiz": None if latest is None else {
            "date": latest["date"],
            "gaps_found": latest["gaps_found"],
            "gaps_unresolved": latest["gaps_unresolved"],
        },
    }


def attach_debt(phases: list[dict], docs_root: Path) -> tuple[list[dict], list[Warning]]:
    """각 phase 행에 debt 를 붙인 새 목록. 입력은 바꾸지 않는다 (merge_active 관례)."""
    by_phase, warnings = quiz_records(docs_root)
    out = []
    for row in phases:
        new_row = dict(row)
        new_row["debt"] = debt_for(row, by_phase.get(row["phase"], []))
        out.append(new_row)
    return out, warnings


def quiz_details(docs_root: Path, number: int) -> tuple[list[dict], list[Warning]]:
    """상세 패널용 — phase 의 시험 기록 요약만 (최신 우선).

    본문(평가·대화 전사)은 싣지 않는다 — 사용자 결정(2026-08-17): 과거 시험은
    갭 해소율과 재시험 필요 여부만 보이면 된다. 전문은 기록 파일에 보존된다.
    """
    by_phase, warnings = quiz_records(docs_root)
    details = [
        {
            "name": rec["name"],
            "date": rec["date"],
            "gaps_found": rec["gaps_found"],
            "gaps_unresolved": rec["gaps_unresolved"],
        }
        for rec in by_phase.get(number, [])
    ]
    return details, warnings

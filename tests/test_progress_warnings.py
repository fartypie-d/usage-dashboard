"""경고 그룹화 — 순수 함수라 파일 시스템을 건드리지 않는다."""

from __future__ import annotations

from pathlib import Path

from app.metrics.progress_warnings import (
    _GROUP_PHRASE,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARN,
    Warning,
    from_messages,
    group_warnings,
    summaries,
)


def _gap(phase: int) -> Warning:
    return Warning(
        code="phase_gap_internal",
        severity=SEVERITY_WARN,
        message=f"Phase {phase} 지시서를 찾지 못했습니다 (번호 구멍)",
        phase=phase,
    )


def test_single_item_group_keeps_the_original_message() -> None:
    """접을 것이 없는데 새 문구를 지어내면 정보만 잃는다."""
    groups = group_warnings([_gap(12)])

    assert len(groups) == 1
    assert groups[0]["summary"] == "Phase 12 지시서를 찾지 못했습니다 (번호 구멍)"
    assert groups[0]["count"] == 1


def test_multi_item_group_lists_five_phases_then_folds() -> None:
    groups = group_warnings([_gap(n) for n in (2, 3, 4, 5, 7, 8, 9)])

    assert groups[0]["summary"] == (
        "Phase 2·3·4·5·7 외 2건 — 지시서를 찾지 못했습니다 (번호 구멍), 총 7건"
    )
    assert groups[0]["count"] == 7
    assert len(groups[0]["items"]) == 7


def test_summary_keeps_the_keywords_the_original_message_had() -> None:
    """요약이 '구멍'을 잃으면 배너만 보고는 무슨 일인지 알 수 없다."""
    groups = group_warnings([_gap(n) for n in (2, 3)])

    assert "구멍" in groups[0]["summary"]


def test_pre_history_gaps_collapse_into_a_range() -> None:
    warnings = [
        Warning(
            code="phase_gap_pre_history",
            severity=SEVERITY_INFO,
            message=f"Phase {n} 지시서를 찾지 못했습니다 (번호 구멍)",
            phase=n,
        )
        for n in range(1, 117)
    ]

    groups = group_warnings(warnings)

    assert groups[0]["summary"] == (
        "Phase 1–116 지시서를 찾지 못했습니다 (번호 구멍) — "
        "문서화 시작(Phase 117) 이전 116건"
    )
    assert groups[0]["severity"] == SEVERITY_INFO


def test_groups_sort_by_severity_then_code() -> None:
    warnings = [
        Warning(code="frontmatter_missing", severity=SEVERITY_INFO, message="i", phase=1),
        Warning(code="verdict_table_missing", severity=SEVERITY_WARN, message="w", phase=2),
        Warning(code="doc_read_failed", severity=SEVERITY_ERROR, message="e"),
        Warning(code="doc_duplicate", severity=SEVERITY_WARN, message="d", phase=3),
    ]

    codes = [group["code"] for group in group_warnings(warnings)]

    assert codes == [
        "doc_read_failed",
        "doc_duplicate",
        "verdict_table_missing",
        "frontmatter_missing",
    ]


def test_unknown_severity_falls_back_to_warn_and_is_not_dropped() -> None:
    """경고 계층이 경고를 삼키면 안 된다."""
    groups = group_warnings([Warning(code="weird", severity="catastrophe", message="m")])

    assert len(groups) == 1
    assert groups[0]["severity"] == SEVERITY_WARN
    assert groups[0]["items"] == ["m"]


def test_items_are_ordered_by_phase_with_phaseless_last() -> None:
    warnings = [
        Warning(code="x", severity=SEVERITY_WARN, message="no-phase"),
        Warning(code="x", severity=SEVERITY_WARN, message="p9", phase=9),
        Warning(code="x", severity=SEVERITY_WARN, message="p2", phase=2),
    ]

    assert group_warnings(warnings)[0]["items"] == ["p2", "p9", "no-phase"]


def test_phaseless_multi_item_group_falls_back_to_first_message() -> None:
    warnings = from_messages(["레지스트리 파일을 읽지 못했습니다: a", "b"], code="registry")

    assert group_warnings(warnings)[0]["summary"] == (
        "레지스트리 파일을 읽지 못했습니다: a 외 1건"
    )


def test_summaries_is_the_flat_warning_list() -> None:
    groups = group_warnings([_gap(2), _gap(3)])

    assert summaries(groups) == [groups[0]["summary"]]


def test_empty_input_produces_no_groups() -> None:
    assert group_warnings([]) == []
    assert summaries([]) == []


def test_warning_is_hashable_so_dict_fromkeys_dedupes() -> None:
    """main.py 가 dict.fromkeys 로 중복을 걷어내는 기존 방식을 유지한다."""
    assert list(dict.fromkeys([_gap(2), _gap(2)])) == [_gap(2)]


def test_pre_history_gaps_with_no_phase_falls_back_to_first_message() -> None:
    """phase=None 인 phase_gap_pre_history 는 ValueError 를 내지 말고 폴백한다."""
    warnings = [
        Warning(
            code="phase_gap_pre_history",
            severity=SEVERITY_INFO,
            message="문서를 찾지 못했습니다",
            phase=None,
        ),
        Warning(
            code="phase_gap_pre_history",
            severity=SEVERITY_INFO,
            message="다른 문서도 없습니다",
            phase=None,
        ),
    ]

    groups = group_warnings(warnings)

    assert groups[0]["summary"] == "문서를 찾지 못했습니다 외 1건"


def test_group_phrase_values_are_substrings_of_real_scanner_messages(
    tmp_path: Path,
) -> None:
    """_GROUP_PHRASE 의 각 문구가 스캐너가 실제로 내보내는 메시지의 부분 문자열임을 검증한다.

    문구를 테스트 파일에 직접 다시 쓰면 중복이 옮겨갈 뿐이고 아무것도 지키지 못한다.
    진짜 스캐너(progress_docs)가 생성한 Warning.message 에서 부분 문자열 여부를 확인한다.

    이 테스트가 _GROUP_PHRASE 를 순회하므로 새 코드를 추가할 때 자동으로 커버된다.
    """
    from app.metrics.progress_docs import phase_index

    # 각 코드를 실제로 유발하는 최소 픽스처를 tmp_path 에 만든다.
    #
    # phase_gap_internal: 1번과 3번만 있으면 2번이 구멍이 된다.
    # verdict_table_missing: task 헤딩은 있는데 검수 표가 없으면 발생한다.
    # doc_duplicate: 같은 phase 번호로 파일을 2개 만들면 발생한다.
    # frontmatter_missing: frontmatter 가 없는 파일명 규약으로 만들면 발생한다.

    # Phase 1 — frontmatter 없음 (파일명만, 본문도 task 헤딩 없어 verdict_table_missing 안 남)
    (tmp_path / "PHASE1_alpha.md").write_text("# 본문만\n", encoding="utf-8")

    # Phase 3 — 정상 (gap 유발용: 2번이 구멍)
    (tmp_path / "PHASE3_gamma.md").write_text(
        "---\nphase: 3\nstatus: done\n---\n\n# 지시서\n\n## Task 1: 작업\n",
        encoding="utf-8",
    )

    # Phase 4 — task 헤딩 있고 검수 표 없음 → verdict_table_missing
    (tmp_path / "PHASE4_delta.md").write_text(
        "---\nphase: 4\nstatus: done\n---\n\n# 지시서\n\n## Task 1: 검증\n",
        encoding="utf-8",
    )

    # Phase 5 — 중복 (doc_duplicate 유발)
    (tmp_path / "PHASE5_epsilon.md").write_text(
        "---\nphase: 5\nstatus: done\n---\n\n# 지시서\n",
        encoding="utf-8",
    )
    (tmp_path / "PHASE5_zeta.md").write_text(
        "---\nphase: 5\nstatus: done\n---\n\n# 지시서\n",
        encoding="utf-8",
    )

    _, warnings = phase_index(tmp_path)
    messages_by_code: dict[str, list[str]] = {}
    for w in warnings:
        messages_by_code.setdefault(w.code, []).append(w.message)

    missing_codes = [code for code in _GROUP_PHRASE if code not in messages_by_code]
    assert not missing_codes, (
        f"픽스처가 다음 코드를 유발하지 않았다 — 픽스처를 보강하라: {missing_codes}"
    )

    mismatches = []
    for code, phrase in _GROUP_PHRASE.items():
        real_messages = messages_by_code.get(code, [])
        if not any(phrase in msg for msg in real_messages):
            mismatches.append(
                f"code={code!r}: phrase {phrase!r} 가 실제 메시지 어디에도 없다\n"
                f"  실제 메시지: {real_messages}"
            )

    assert not mismatches, "\n".join(mismatches)

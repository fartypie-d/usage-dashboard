"""Tests for app.metrics.progress_docs — docs/ 문서 → phase·task 구조."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.metrics.progress_docs import phase_detail, phase_index

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "progress_docs"


@pytest.fixture()
def index() -> list[dict]:
    phases, _ = phase_index(FIXTURE)
    return phases


def test_all_three_filename_conventions_are_discovered(index: list[dict]) -> None:
    assert [p["phase"] for p in index] == [11, 10, 6, 1]


def test_phase_number_falls_back_to_filename_when_frontmatter_missing(
    index: list[dict],
) -> None:
    phase1 = [p for p in index if p["phase"] == 1][0]
    assert phase1["status"] == "completed"


def test_missing_frontmatter_raises_a_warning() -> None:
    _, warnings = phase_index(FIXTURE)
    assert any(
        w.code == "frontmatter_missing" and "frontmatter" in w.message
        for w in warnings
    )


def test_non_numeric_cost_becomes_null_and_keeps_raw(index: list[dict]) -> None:
    phase11 = [p for p in index if p["phase"] == 11][0]
    assert phase11["cost"] is None
    assert phase11["cost_raw"] == "-"
    assert phase11["compactions"] == 0


def test_commits_are_split_into_a_list(index: list[dict]) -> None:
    phase10 = [p for p in index if p["phase"] == 10][0]
    assert phase10["commits"] == ["a7e6931", "6a9dc68"]


def test_four_column_review_table_yields_verdict_and_commit(index: list[dict]) -> None:
    tasks = {t["n"]: t for t in [p for p in index if p["phase"] == 10][0]["tasks"]}
    assert tasks[1]["verdict"] == "pass"
    assert tasks[1]["commit"] == "6a9dc68"
    assert "🟠 2" in tasks[1]["verdict_raw"]


def test_verdicts_are_found_when_split_across_two_review_sections(
    index: list[dict],
) -> None:
    # 실측 함정: 지시서는 '## 검수 (task마다)'(리뷰어 배정 2열)와 '## 검수 결과'
    # (판정 4열)를 별도 절로 쓴다. 첫 절만 훑으면 판정을 통째로 놓친다.
    phase10 = [p for p in index if p["phase"] == 10][0]
    assert [t["verdict"] for t in phase10["tasks"]] == ["pass", "pass"]


def test_unrelated_numeric_tables_are_not_read_as_verdicts(index: list[dict]) -> None:
    # '## 미해결 · 이월'의 3열 표가 판정으로 오인되면 안 된다.
    phase10 = [p for p in index if p["phase"] == 10][0]
    assert all(t["commit"] for t in phase10["tasks"])


def test_currency_prefixed_cost_is_parsed_as_a_number(index: list[dict]) -> None:
    phase6 = [p for p in index if p["phase"] == 6][0]
    assert phase6["cost"] == 12.50
    assert phase6["cost_raw"] is None


def test_two_column_review_table_leaves_verdict_unknown(index: list[dict]) -> None:
    tasks = {t["n"]: t for t in [p for p in index if p["phase"] == 11][0]["tasks"]}
    assert tasks[1]["verdict"] is None
    assert tasks[1]["commit"] is None


def test_missing_review_table_warns_and_leaves_verdict_unknown() -> None:
    phases, warnings = phase_index(FIXTURE)
    phase6 = [p for p in phases if p["phase"] == 6][0]
    assert phase6["tasks"][0]["verdict"] is None
    assert any(
        w.code == "verdict_table_missing" and w.phase == 6 for w in warnings
    )


def test_task_titles_come_from_the_headings(index: list[dict]) -> None:
    phase10 = [p for p in index if p["phase"] == 10][0]
    assert [t["title"] for t in phase10["tasks"]] == [
        "GZip 압축 활성화",
        "서버측 상한",
    ]


def test_review_doc_is_linked_when_present(index: list[dict]) -> None:
    by_phase = {p["phase"]: p for p in index}
    assert by_phase[10]["review_path"].endswith("PHASE10_response-payload-cap.md")
    assert by_phase[11]["review_path"] is None


def test_number_gaps_are_reported() -> None:
    _, warnings = phase_index(FIXTURE)
    assert any(w.code.startswith("phase_gap") and w.phase == 2 for w in warnings)


def test_index_never_carries_task_bodies(index: list[dict]) -> None:
    for phase in index:
        for task in phase["tasks"]:
            assert "nodes" not in task
            assert "body" not in task


def test_detail_returns_nodes_per_task_and_intro() -> None:
    detail, _ = phase_detail(FIXTURE, 10)
    assert detail["phase"] == 10
    assert detail["intro"][0]["t"] in ("heading", "paragraph")
    task2 = [t for t in detail["tasks"] if t["n"] == 2][0]
    assert any(n["t"] == "table" for n in task2["nodes"])


def test_detail_includes_review_nodes_when_the_doc_exists() -> None:
    detail, _ = phase_detail(FIXTURE, 10)
    assert detail["review"] and detail["review"][0]["t"] == "heading"


def test_detail_review_is_empty_when_absent() -> None:
    detail, _ = phase_detail(FIXTURE, 11)
    assert detail["review"] == []


def test_detail_returns_none_for_unknown_phase() -> None:
    detail, _ = phase_detail(FIXTURE, 99)
    assert detail is None


def test_missing_docs_root_returns_empty_with_warning(tmp_path: Path) -> None:
    phases, warnings = phase_index(tmp_path / "nope")
    assert phases == []
    assert warnings


def test_markdown_failure_degrades_to_raw_text_with_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.metrics.progress_docs as mod

    def boom(_text: str) -> list[dict]:
        raise ValueError("parser exploded")

    monkeypatch.setattr(mod, "markdown_to_nodes", boom)
    detail, warnings = mod.phase_detail(FIXTURE, 10)
    assert detail["tasks"][0]["nodes"][0]["t"] == "code"
    assert any(
        w.code == "markdown_render_failed" and "렌더 실패" in w.message
        for w in warnings
    )


# ── 리뷰 반영 회귀 가드 ──────────────────────────────────────────


def test_lettered_task_heading_is_not_merged_into_the_plain_number(
    tmp_path: Path,
) -> None:
    """'## Task 4b'가 Task 4로 오인되면 번호 중복 + 제목 손상이 난다 (phase 7 실측)."""
    doc = tmp_path / "PHASE20_lettered.md"
    doc.write_text(
        "---\nphase: 20\nstatus: done\n---\n\n"
        "## Task 4: 헬퍼 승격\n본문\n\n"
        "## Task 4b: 나머지 2모듈 정리 (플랜 밖 추가)\n본문\n\n"
        "## 검수 결과\n\n| Task | 에이전트 | 판정 | 커밋 |\n|---|---|---|---|\n"
        "| 4 | dash-backend | ✅ | `abc1234` |\n",
        encoding="utf-8",
    )
    phases, _ = phase_index(tmp_path)
    tasks = phases[0]["tasks"]
    assert [t["label"] for t in tasks] == ["4", "4b"]
    assert tasks[1]["title"] == "나머지 2모듈 정리 (플랜 밖 추가)"
    # 4b 는 4 의 판정을 물려받으면 안 된다 — 서로 다른 작업이다.
    assert tasks[0]["verdict"] == "pass"
    assert tasks[1]["verdict"] is None


def test_prose_style_review_section_yields_verdicts(tmp_path: Path) -> None:
    """표가 아니라 '### Task 8 — ✅ 통과' 산문으로 적은 문서가 있다 (phase 1~3·6~9)."""
    doc = tmp_path / "PHASE21_prose.md"
    doc.write_text(
        "---\nphase: 21\nstatus: done\n---\n\n"
        "## Task 8: 무언가\n본문\n\n"
        "## 검수 결과\n\n### Task 8 — ✅ 통과 (2026-08-02)\n- 위임: dash-backend\n",
        encoding="utf-8",
    )
    phases, _ = phase_index(tmp_path)
    task = phases[0]["tasks"][0]
    assert task["verdict"] == "pass"
    assert "통과" in task["verdict_raw"]


def test_multiple_commits_in_a_verdict_cell_are_all_kept(tmp_path: Path) -> None:
    """반려→재수정으로 커밋이 여러 개인 행이 흔하다 — 첫 개만 남기지 않는다."""
    doc = tmp_path / "PHASE22_multi.md"
    doc.write_text(
        "---\nphase: 22\nstatus: done\n---\n\n"
        "## Task 1: 무언가\n본문\n\n"
        "## 검수 결과\n\n| Task | 에이전트 | 판정 | 커밋 |\n|---|---|---|---|\n"
        "| 1 | dash-backend | ✅ | `73cddc7` + `b862c51` |\n",
        encoding="utf-8",
    )
    phases, _ = phase_index(tmp_path)
    task = phases[0]["tasks"][0]
    assert task["commits"] == ["73cddc7", "b862c51"]
    assert task["commit"] == "73cddc7"


def test_escaped_pipe_does_not_shift_verdict_columns(tmp_path: Path) -> None:
    r"""셀 안의 `\|` 는 열 구분자가 아니다 — 밀리면 판정·커밋 인덱스가 어긋난다."""
    doc = tmp_path / "PHASE23_pipe.md"
    doc.write_text(
        "---\nphase: 23\nstatus: done\n---\n\n"
        "## Task 1: 무언가\n본문\n\n"
        "## 검수 결과\n\n| Task | 에이전트 | 판정 | 커밋 |\n|---|---|---|---|\n"
        "| 1 | `ss -tlnp \\| grep 9280` 확인 | ✅ | `abc1234` |\n",
        encoding="utf-8",
    )
    phases, _ = phase_index(tmp_path)
    task = phases[0]["tasks"][0]
    assert task["verdict"] == "pass"
    assert task["commit"] == "abc1234"


def test_undecodable_bytes_raise_a_warning(tmp_path: Path) -> None:
    """errors='replace' 가 갉아먹은 자리를 조용히 넘기지 않는다."""
    doc = tmp_path / "PHASE24_broken.md"
    doc.write_bytes(
        "---\nphase: 24\nstatus: done\n---\n\n## Task 1: 무언가\n".encode()
        + b"\xff\xfe broken bytes\n"
    )
    _, warnings = phase_index(tmp_path)
    assert any(
        w.code == "doc_encoding_damaged" and "UTF-8" in w.message for w in warnings
    )


# ── 번호 구멍 2종 분리 (Phase 17) ────────────────────────────────


def test_gaps_inside_the_documented_range_are_warnings(tmp_path: Path) -> None:
    """문서화가 진행 중인 구간 한복판의 구멍은 실제 누락일 수 있다."""
    for phase in (5, 8):
        (tmp_path / f"PHASE{phase}_x.md").write_text(
            f"---\nphase: {phase}\n---\n", encoding="utf-8"
        )

    _, warnings = phase_index(tmp_path)

    internal = [w for w in warnings if w.code == "phase_gap_internal"]
    assert sorted(w.phase for w in internal) == [6, 7]
    assert all(w.severity == "warn" for w in internal)


def test_gaps_before_the_first_documented_phase_are_info(tmp_path: Path) -> None:
    """Phase 5 부터 문서화를 시작했다면 1~4 는 누락이 아니라 역사다."""
    for phase in (5, 6):
        (tmp_path / f"PHASE{phase}_x.md").write_text(
            f"---\nphase: {phase}\n---\n", encoding="utf-8"
        )

    _, warnings = phase_index(tmp_path)

    pre = [w for w in warnings if w.code == "phase_gap_pre_history"]
    assert sorted(w.phase for w in pre) == [1, 2, 3, 4]
    assert all(w.severity == "info" for w in pre)
    assert not [w for w in warnings if w.code == "phase_gap_internal"]


def test_no_gap_warnings_when_numbering_is_contiguous(tmp_path: Path) -> None:
    for phase in (1, 2, 3):
        (tmp_path / f"PHASE{phase}_x.md").write_text(
            f"---\nphase: {phase}\n---\n", encoding="utf-8"
        )

    _, warnings = phase_index(tmp_path)

    assert not [w for w in warnings if w.code.startswith("phase_gap")]


def test_parse_frontmatter_public_api_converts_gaps_keys() -> None:
    from app.metrics.progress_docs import parse_frontmatter

    meta, body = parse_frontmatter(
        "---\nphase: 3\ngaps_found: 2\ngaps_unresolved: 0\ncost: $1.25\n---\n본문"
    )

    assert meta["phase"] == 3
    assert meta["gaps_found"] == 2
    assert meta["gaps_unresolved"] == 0
    assert meta["cost"] == 1.25
    assert body == "본문"


def test_parse_frontmatter_keeps_null_for_non_numeric_gaps() -> None:
    from app.metrics.progress_docs import parse_frontmatter

    meta, _ = parse_frontmatter("---\ngaps_unresolved: -\n---\n")

    assert meta["gaps_unresolved"] is None
    assert meta["gaps_unresolved_raw"] == "-"


def test_phase_raw_text_joins_doc_and_review(tmp_path: Path) -> None:
    from app.metrics.progress_docs import phase_raw_text

    (tmp_path / "PHASE1_x.md").write_text("---\nphase: 1\n---\n", encoding="utf-8")
    (tmp_path / "PHASE2_x.md").write_text("---\nphase: 2\n---\n", encoding="utf-8")
    (tmp_path / "PHASE3_demo.md").write_text(
        "---\nphase: 3\n---\n# 지시서 본문\n", encoding="utf-8"
    )
    (tmp_path / "reviews").mkdir()
    (tmp_path / "reviews" / "PHASE3_demo.md").write_text("# 리뷰 본문\n", encoding="utf-8")

    text, warnings = phase_raw_text(tmp_path, 3)

    assert "# 지시서 본문" in text
    assert "# 리뷰 본문" in text
    assert warnings == []


def test_phase_raw_text_unknown_phase_returns_none(tmp_path: Path) -> None:
    from app.metrics.progress_docs import phase_raw_text

    (tmp_path / "PHASE3_demo.md").write_text("---\nphase: 3\n---\nx", encoding="utf-8")

    text, _ = phase_raw_text(tmp_path, 99)

    assert text is None

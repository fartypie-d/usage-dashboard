"""Tests for GET /api/progress and /api/progress/phase/{n}."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "progress_docs"
REGISTRY_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "registry"


@pytest.fixture(autouse=True)
def _docs_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USAGE_DOCS_ROOT", str(FIXTURE))


def _write_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, records: list[dict]
) -> None:
    registry_dir = tmp_path / "registry"
    registry_dir.mkdir()
    for index, record in enumerate(records):
        (registry_dir / f"{index}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )
    monkeypatch.setenv("USAGE_REGISTRY_DIR", str(registry_dir))


def _fixture_project(name: str) -> dict:
    record = json.loads((REGISTRY_FIXTURE / f"{name}.json").read_text(encoding="utf-8"))
    record["root"] = str(FIXTURE.parent)
    record["docs_dir"] = FIXTURE.name
    return record


def test_progress_without_project_keeps_the_existing_contract(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.metrics.cognitive_debt import attach_debt
    from app.metrics.progress_docs import phase_index
    from app.metrics.progress_warnings import group_warnings, summaries

    _write_registry(monkeypatch, tmp_path, [])
    phases, warnings = phase_index(FIXTURE)
    phases, debt_warnings = attach_debt(phases, FIXTURE)
    warnings += debt_warnings

    data = client.get("/api/progress").json()

    assert data["phases"] == phases
    assert data["warnings"] == summaries(group_warnings(warnings))
    assert data["project"] is None


@pytest.mark.parametrize("path", ["/api/progress", "/api/progress/phase/10"])
def test_progress_without_project_excludes_registry_warnings(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    path: str,
) -> None:
    from app.metrics.progress_docs import phase_detail, phase_index
    from app.metrics.progress_warnings import group_warnings, summaries

    missing_registry = tmp_path / "missing-registry"
    monkeypatch.setenv("USAGE_REGISTRY_DIR", str(missing_registry))

    response = client.get(path)

    assert response.status_code == 200
    raw = (
        phase_index(FIXTURE)[1]
        if path == "/api/progress"
        else phase_detail(FIXTURE, 10)[1]
    )
    expected_warnings = summaries(group_warnings(raw))
    assert response.json()["warnings"] == expected_warnings
    assert not any("레지스트리 디렉터리" in warning for warning in expected_warnings)


def test_progress_without_project_surfaces_registry_file_corruption(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_registry(monkeypatch, tmp_path, [_fixture_project("alpha")])
    (tmp_path / "registry" / "broken.json").write_text("{", encoding="utf-8")

    data = client.get("/api/progress").json()

    assert any("레지스트리 파일을 읽지 못했습니다" in warning for warning in data["warnings"])
    assert [item["project"] for item in data["projects"]] == ["alpha"]


def test_progress_lists_registry_projects(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_registry(
        monkeypatch,
        tmp_path,
        [_fixture_project("alpha"), _fixture_project("bravo"), _fixture_project("charlie")],
    )

    projects = client.get("/api/progress").json()["projects"]

    assert [(item["project"], item["active_count"]) for item in projects] == [
        ("alpha", 1),
        ("bravo", 0),
        ("charlie", 1),
    ]


def test_progress_reads_another_projects_docs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_registry(monkeypatch, tmp_path, [_fixture_project("alpha")])

    data = client.get("/api/progress?project=alpha").json()

    assert data["project"] == "alpha"
    assert [phase["phase"] for phase in data["phases"]] == [14, 11, 10, 6, 1]


def test_progress_rejects_an_unknown_project(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_registry(monkeypatch, tmp_path, [_fixture_project("alpha")])

    response = client.get("/api/progress?project=unknown")

    assert response.status_code == 404
    assert "unknown" in str(response.json()["detail"])


def test_progress_marks_active_phases(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = _fixture_project("alpha")
    record["active"] = [{"phase": 10, "slug": "response-payload-cap"}]
    _write_registry(monkeypatch, tmp_path, [record])

    phases = client.get("/api/progress?project=alpha").json()["phases"]

    assert next(phase for phase in phases if phase["phase"] == 10)["active"] is True


def test_progress_warns_about_active_slug_mismatch_and_marks_all_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = _fixture_project("alpha")
    record["active"] = [{"phase": 11, "slug": "stale-claim"}]
    _write_registry(monkeypatch, tmp_path, [record])

    data = client.get("/api/progress?project=alpha").json()
    phase = next(item for item in data["phases"] if item["phase"] == 11)

    assert phase["active"] is True
    assert all("active" in item for item in data["phases"])
    assert (
        f"Phase 11: active 등록 slug(stale-claim)가 문서 slug({phase['slug']})와 다릅니다"
        in data["warnings"]
    )


def test_progress_synthesizes_a_row_for_a_claimed_phase_without_docs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = _fixture_project("alpha")
    record["active"] = [{"phase": 99, "slug": "claimed-work"}]
    _write_registry(monkeypatch, tmp_path, [record])

    phases = client.get("/api/progress?project=alpha").json()["phases"]
    synthetic = next(phase for phase in phases if phase["phase"] == 99)

    assert synthetic["slug"] == "claimed-work"
    assert synthetic["status"] == "claimed"
    assert synthetic["active"] is True
    assert set(synthetic) == set(phases[1])
    # debt는 항상 신호를 매기는 필드라 문서 없는 합성 행에서도 None이 아니다
    # (부채 등급은 task/리뷰 부재 자체를 신호로 삼는다 — cognitive_debt.debt_for).
    assert synthetic["debt"]["level"] == "unrepaid"
    assert all(
        value is None
        for key, value in synthetic.items()
        if key not in {"phase", "slug", "status", "active", "debt"}
    )


def test_progress_warns_when_a_project_root_is_gone(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = _fixture_project("bravo")
    record["root"] = str(tmp_path / "gone")
    _write_registry(monkeypatch, tmp_path, [record])

    data = client.get("/api/progress?project=bravo").json()

    assert data["phases"] == []
    assert any("문서 디렉터리를 찾지 못했습니다" in warning for warning in data["warnings"])


def test_progress_phase_detail_accepts_the_project_parameter(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_registry(monkeypatch, tmp_path, [_fixture_project("alpha")])

    data = client.get("/api/progress/phase/10?project=alpha").json()

    assert data["phase"] == 10
    assert data["project"] == "alpha"


def test_progress_rejects_a_malformed_project_name(client: TestClient) -> None:
    assert client.get("/api/progress?project=../x").status_code == 422


def test_progress_returns_200_and_the_contract_keys(client: TestClient) -> None:
    res = client.get("/api/progress")
    assert res.status_code == 200
    assert set(res.json().keys()) == {
        "phases", "warnings", "warning_groups", "project", "projects", "llm",
    }


def test_progress_lists_phases_newest_first(client: TestClient) -> None:
    data = client.get("/api/progress").json()
    assert [p["phase"] for p in data["phases"]] == [11, 10, 6, 1]


def test_progress_carries_warnings_to_the_response(client: TestClient) -> None:
    data = client.get("/api/progress").json()
    all_items = [item for g in data["warning_groups"] for item in g["items"]]
    assert any("Phase 6" in item for item in all_items)


def test_progress_list_omits_task_bodies(client: TestClient) -> None:
    res = client.get("/api/progress")
    # 크기가 아니라 구조로 막는다 — 픽스처가 작아 바이트 예산만으로는
    # 본문이 실려도 통과할 수 있다 (실측 2,543 B; debt 신호 합류 후 4,475 B).
    assert "nodes" not in res.text
    for phase in res.json()["phases"]:
        for task in phase["tasks"]:
            assert set(task) == {
                "n", "label", "title", "verdict", "verdict_raw", "commit", "commits",
            }
    assert len(res.content) < 6000


def test_phase_detail_returns_nodes(client: TestClient) -> None:
    data = client.get("/api/progress/phase/10").json()
    assert set(data.keys()) == {
        "phase", "intro", "tasks", "review", "warnings", "warning_groups", "project",
        "quizzes",
    }
    assert data["phase"] == 10
    assert any(n["t"] == "table" for t in data["tasks"] for n in t["nodes"])


def test_unknown_phase_returns_404(client: TestClient) -> None:
    assert client.get("/api/progress/phase/99").status_code == 404


def test_missing_docs_root_returns_200_with_warning(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("USAGE_DOCS_ROOT", str(tmp_path / "nope"))
    data = client.get("/api/progress").json()
    assert data["phases"] == []
    assert data["warnings"]


def test_unreadable_phase_doc_reports_the_cause_not_just_not_found(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """'없는 phase'와 '못 읽는 phase'를 같은 404 문구로 뭉개면 원인 추적이 막힌다."""
    doc = tmp_path / "PHASE30_broken.md"
    doc.write_text("---\nphase: 30\n---\n\n## Task 1: x\n", encoding="utf-8")
    doc.chmod(0o000)
    monkeypatch.setenv("USAGE_DOCS_ROOT", str(tmp_path))
    try:
        res = client.get("/api/progress/phase/30")
        assert res.status_code == 404
        detail = res.json()["detail"]
        assert any("읽지 못했습니다" in w for w in detail["warnings"])
    finally:
        doc.chmod(0o644)


def _make_project_docs(root: Path, docs_dir: str, phase: int, slug: str) -> None:
    """tmp_path 아래에 최소 페이즈 문서 1개를 갖춘 프로젝트를 만든다."""
    docs = root / docs_dir
    docs.mkdir(parents=True)
    (docs / f"PHASE{phase}_{slug}.md").write_text(
        "---\n"
        f"phase: {phase}\n"
        "date: 2026-08-09\n"
        "kind: task\n"
        "status: done\n"
        "commits: deadbee\n"
        "---\n"
        f"\n# 작업 지시서 — {slug}\n\n## Task 1: 첫 작업\n",
        encoding="utf-8",
    )


def test_progress_resolves_each_project_with_its_own_docs_dir(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """레지스트리 프로젝트마다 docs_dir가 다를 때 각각 자기 경로를 읽어야 한다.

    실제 레지스트리에는 소문자 ``docs``를 쓰는 프로젝트와 대문자 ``DOCs``를 쓰는
    프로젝트가 섞여 있다. 한 프로젝트만 검증하면 이 차이가 깨져도 못 잡는다.
    """
    lower_root = tmp_path / "lower-project"
    upper_root = tmp_path / "upper-project"
    _make_project_docs(lower_root, "docs", 21, "lower-side")
    _make_project_docs(upper_root, "DOCs", 22, "upper-side")

    _write_registry(
        monkeypatch,
        tmp_path,
        [
            {
                "project": "lowerproj",
                "root": str(lower_root),
                "docs_dir": "docs",
                "next_phase": 22,
                "active": [],
            },
            {
                "project": "upperproj",
                "root": str(upper_root),
                "docs_dir": "DOCs",
                "next_phase": 23,
                "active": [],
            },
        ],
    )

    listing = client.get("/api/progress").json()
    assert {item["project"] for item in listing["projects"]} == {
        "lowerproj",
        "upperproj",
    }

    lower = client.get("/api/progress?project=lowerproj").json()
    upper = client.get("/api/progress?project=upperproj").json()

    assert [phase["phase"] for phase in lower["phases"]] == [21]
    assert [phase["phase"] for phase in upper["phases"]] == [22]
    for data in (lower, upper):
        assert not any(
            "문서 디렉터리를 찾지 못했습니다" in warning for warning in data["warnings"]
        )


# ── 경고 그룹화 (Phase 17) ──────────────────────────────────────


def test_progress_exposes_warning_groups(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_registry(monkeypatch, tmp_path, [])

    data = client.get("/api/progress").json()

    by_code = {g["code"]: g for g in data["warning_groups"]}
    assert by_code["phase_gap_internal"]["count"] == 7
    assert by_code["phase_gap_internal"]["severity"] == "warn"
    assert by_code["verdict_table_missing"]["count"] == 3
    assert by_code["frontmatter_missing"]["severity"] == "info"


def test_flat_warnings_are_derived_from_group_summaries(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """진실의 원천은 그룹 하나다 — 두 목록이 따로 자라면 안 된다."""
    _write_registry(monkeypatch, tmp_path, [])

    data = client.get("/api/progress").json()

    assert data["warnings"] == [g["summary"] for g in data["warning_groups"]]


def test_grouping_collapses_the_warning_wall(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """픽스처의 경고 11건이 그룹 3개로 접힌다."""
    _write_registry(monkeypatch, tmp_path, [])

    data = client.get("/api/progress").json()

    assert len(data["warnings"]) == 3


def test_registry_warnings_join_as_their_own_group(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_registry(monkeypatch, tmp_path, [_fixture_project("alpha")])
    (tmp_path / "registry" / "broken.json").write_text("{", encoding="utf-8")

    data = client.get("/api/progress").json()

    registry = [g for g in data["warning_groups"] if g["code"] == "registry"]
    assert len(registry) == 1
    assert any("레지스트리 파일을 읽지 못했습니다" in item for item in registry[0]["items"])


def test_phase_detail_also_exposes_groups(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_registry(monkeypatch, tmp_path, [])

    data = client.get("/api/progress/phase/10").json()

    assert isinstance(data["warning_groups"], list)
    assert data["warnings"] == [g["summary"] for g in data["warning_groups"]]


def test_unknown_project_404_includes_both_warnings_and_groups(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unknown project 404 response has both warnings and warning_groups with matching invariant."""
    # Create a registry with one valid project and one broken file to generate a warning
    _write_registry(monkeypatch, tmp_path, [_fixture_project("alpha")])
    (tmp_path / "registry" / "broken.json").write_text("{", encoding="utf-8")

    response = client.get("/api/progress?project=unknown")

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "unknown" in detail["message"]
    assert isinstance(detail["warnings"], list)
    assert isinstance(detail["warning_groups"], list)
    # Invariant: warnings must match group summaries
    assert detail["warnings"] == [g["summary"] for g in detail["warning_groups"]]
    # Ensure at least one warning was generated (not vacuous truth)
    assert len(detail["warnings"]) > 0


def test_unknown_phase_404_includes_both_warnings_and_groups(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unknown phase 404 응답도 warnings/warning_groups 불변식을 만족한다.

    unknown-project 404 의 트윈 — ``get_progress_phase`` 가 올리는 404 경로에서도
    ``warnings == [g["summary"] for g in warning_groups]`` 가 성립해야 한다.
    """
    # Broken registry file generates at least one warning so the assertion is not vacuously true.
    _write_registry(monkeypatch, tmp_path, [_fixture_project("alpha")])
    (tmp_path / "registry" / "broken.json").write_text("{", encoding="utf-8")

    response = client.get("/api/progress/phase/99999")

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert isinstance(detail["warnings"], list)
    assert isinstance(detail["warning_groups"], list)
    # Core invariant — groups are the single source of truth
    assert detail["warnings"] == [g["summary"] for g in detail["warning_groups"]]
    # Non-vacuous: the broken registry file must have produced at least one warning
    assert len(detail["warnings"]) > 0


def test_progress_includes_debt_and_llm_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_registry(monkeypatch, tmp_path, [])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    data = client.get("/api/progress").json()

    assert data["llm"]["available"] is False
    for phase in data["phases"]:
        assert phase["debt"]["level"] in ("repaid", "partial", "unrepaid")
        assert isinstance(phase["debt"]["signals"], list)


def test_progress_debt_reflects_quiz_record(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "PHASE1_demo.md").write_text(
        "---\nphase: 1\n---\n## Task 1: 데모\n본문\n", encoding="utf-8"
    )
    (docs / "quizzes").mkdir()
    (docs / "quizzes" / "PHASE1_20260817.md").write_text(
        "---\nphase: 1\ndate: 2026-08-17\ngaps_found: 1\ngaps_unresolved: 0\n---\n# 시험\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("USAGE_DOCS_ROOT", str(docs))

    data = client.get("/api/progress").json()

    row = next(p for p in data["phases"] if p["phase"] == 1)
    assert row["debt"]["level"] == "repaid"
    assert row["debt"]["quiz_count"] == 1


def test_progress_phase_detail_includes_quizzes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "PHASE1_demo.md").write_text(
        "---\nphase: 1\n---\n## Task 1: 데모\n본문\n", encoding="utf-8"
    )
    (docs / "quizzes").mkdir()
    (docs / "quizzes" / "PHASE1_20260817.md").write_text(
        "---\nphase: 1\ndate: 2026-08-17\ngaps_found: 1\ngaps_unresolved: 0\n---\n# 시험 본문\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("USAGE_DOCS_ROOT", str(docs))

    data = client.get("/api/progress/phase/1").json()

    assert len(data["quizzes"]) == 1
    assert data["quizzes"][0]["gaps_unresolved"] == 0
    assert data["quizzes"][0]["gaps_found"] == 1
    assert "nodes" not in data["quizzes"][0]  # 요약 전용 — 본문·전사는 싣지 않는다

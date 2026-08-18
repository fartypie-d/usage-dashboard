import json
from pathlib import Path

from app.sources.orchestrate_registry import load_projects

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "registry"


def test_load_projects_returns_projects_sorted_by_name() -> None:
    projects, warnings = load_projects(FIXTURE_DIR)

    assert [project["project"] for project in projects] == ["alpha", "bravo", "charlie"]
    assert len(warnings) == 1
    assert "broken.json" in warnings[0]


def test_load_projects_skips_a_malformed_json_file_with_a_warning(tmp_path: Path) -> None:
    (tmp_path / "valid.json").write_text(
        json.dumps({"project": "valid", "root": "/tmp/valid"}), encoding="utf-8"
    )
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")

    projects, warnings = load_projects(tmp_path)

    assert [project["project"] for project in projects] == ["valid"]
    assert len(warnings) == 1
    assert "레지스트리 파일을 읽지 못했습니다: broken.json:" in warnings[0]


def test_load_projects_does_not_open_lock_files() -> None:
    projects, warnings = load_projects(FIXTURE_DIR)

    assert len(projects) == 3
    assert all("x.lock" not in message for message in warnings)


def test_load_projects_reports_a_missing_directory(tmp_path: Path) -> None:
    projects, warnings = load_projects(tmp_path / "missing")

    assert projects == []
    assert warnings == [f"레지스트리 디렉터리를 찾지 못했습니다: {tmp_path / 'missing'}"]


def test_load_projects_defaults_docs_dir_when_absent() -> None:
    projects, warnings = load_projects(FIXTURE_DIR)

    charlie = next(project for project in projects if project["project"] == "charlie")
    assert charlie["docs_dir"] == "docs"
    assert len(warnings) == 1
    assert "broken.json" in warnings[0]


def test_load_projects_nullifies_a_non_integer_next_phase(tmp_path: Path) -> None:
    (tmp_path / "project.json").write_text(
        json.dumps({"project": "project", "root": "/tmp/project", "next_phase": "x"}),
        encoding="utf-8",
    )

    projects, warnings = load_projects(tmp_path)

    assert projects[0]["next_phase"] is None
    assert len(warnings) == 1
    assert "next_phase" in warnings[0]


def test_load_projects_skips_an_entry_missing_required_fields(tmp_path: Path) -> None:
    (tmp_path / "missing-fields.json").write_text(
        (FIXTURE_DIR / "review_cases" / "missing-fields.json").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )

    projects, warnings = load_projects(tmp_path)

    assert projects == []
    assert len(warnings) == 1
    assert "필수 필드" in warnings[0]


def test_load_projects_warns_when_active_is_not_a_list(tmp_path: Path) -> None:
    (tmp_path / "active-string.json").write_text(
        (FIXTURE_DIR / "review_cases" / "active-string.json").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )

    projects, warnings = load_projects(tmp_path)

    assert projects[0]["active"] == []
    assert len(warnings) == 1
    assert "active가 목록이 아닙니다" in warnings[0]


def test_load_projects_drops_an_active_item_with_a_non_integer_phase(
    tmp_path: Path,
) -> None:
    (tmp_path / "active-mixed.json").write_text(
        (FIXTURE_DIR / "review_cases" / "active-mixed.json").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )

    projects, warnings = load_projects(tmp_path)

    assert projects[0]["active"] == [{"phase": 14, "slug": "valid"}]
    assert len(warnings) == 1
    assert "phase가 정수가 아닙니다" in warnings[0]


def test_load_projects_defaults_next_phase_to_none_when_absent() -> None:
    projects, warnings = load_projects(FIXTURE_DIR)

    charlie = next(project for project in projects if project["project"] == "charlie")
    assert "next_phase" in charlie
    assert charlie["next_phase"] is None
    assert len(warnings) == 1

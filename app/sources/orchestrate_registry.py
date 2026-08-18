"""Read and normalize orchestrate phase registry records."""

import json
from pathlib import Path


def load_projects(registry_dir: Path) -> tuple[list[dict], list[str]]:
    """Return normalized projects from ``registry_dir`` and any read warnings."""
    projects: list[dict] = []
    warnings: list[str] = []

    if not registry_dir.is_dir():
        return [], [f"레지스트리 디렉터리를 찾지 못했습니다: {registry_dir}"]

    for registry_file in sorted(registry_dir.glob("*.json")):
        try:
            record = json.loads(registry_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            warnings.append(
                f"레지스트리 파일을 읽지 못했습니다: {registry_file.name}: {exc}"
            )
            continue

        if not isinstance(record, dict):
            warnings.append(
                f"레지스트리 파일을 읽지 못했습니다: {registry_file.name}: "
                "JSON 객체가 아닙니다"
            )
            continue

        project = record.get("project")
        root = record.get("root")
        missing_fields = [
            field
            for field, value in (("project", project), ("root", root))
            if not isinstance(value, str)
        ]
        if missing_fields:
            warnings.append(
                f"레지스트리 항목을 건너뛰었습니다: {registry_file.name}: "
                f"필수 필드가 없거나 올바르지 않습니다: {', '.join(missing_fields)}"
            )
            continue

        normalized = dict(record)
        docs_dir = normalized.setdefault("docs_dir", "docs")
        normalized.setdefault("next_phase", None)
        if not isinstance(docs_dir, str):
            warnings.append(
                f"레지스트리 항목의 docs_dir가 올바르지 않습니다: {registry_file.name}"
            )
            normalized["docs_dir"] = "docs"

        next_phase = normalized.get("next_phase")
        if next_phase is not None and (
            not isinstance(next_phase, int) or isinstance(next_phase, bool)
        ):
            warnings.append(
                f"레지스트리 항목의 next_phase가 정수가 아닙니다: {registry_file.name}"
            )
            normalized["next_phase"] = None

        active = normalized.get("active", [])
        if not isinstance(active, list):
            warnings.append(
                f"레지스트리 항목의 active가 목록이 아닙니다: {registry_file.name}"
            )
            normalized["active"] = []
        else:
            valid_active: list[dict] = []
            for index, item in enumerate(active):
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("phase"), int)
                    or isinstance(item.get("phase"), bool)
                ):
                    warnings.append(
                        f"레지스트리 active 항목을 건너뛰었습니다: "
                        f"{registry_file.name}[{index}]: phase가 정수가 아닙니다"
                    )
                    continue
                valid_active.append(item)
            normalized["active"] = valid_active

        projects.append(normalized)

    projects.sort(key=lambda project: project["project"])
    return projects, warnings

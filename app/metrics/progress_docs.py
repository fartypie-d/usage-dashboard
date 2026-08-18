"""docs/ 의 phase 지시서·리뷰 문서 → phase·task 구조.

파일 시스템만 읽는다 (git 호출 없음). 파일명 규약이 phase를 거치며 흘렀으므로
셋을 모두 인식하고, 알 수 없는 값은 지어내지 않고 ``None``으로 남긴 뒤
경고를 올린다.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.markdown_nodes import markdown_to_nodes
from app.metrics.progress_warnings import (
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARN,
    Warning,
)

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
# 'Task 4b'(플랜 밖 추가분) 같은 접미 문자를 번호에서 분리한다. 접미가 붙은 절을
# 그냥 'Task 4'로 읽으면 번호가 중복되고 제목이 'b: …'로 깨진다 (phase 7 실측).
_TASK_HEADING_RE = re.compile(r"^## Task (\d+)([a-z]?)\s*[:：]?\s*(.*)$", re.MULTILINE)
# 표가 아니라 '### Task 8 — ✅ 통과' 형태의 산문으로 판정을 적은 문서가 있다
# (phase 1~3·6~9 실측). 표가 없을 때의 보조 경로다.
_PROSE_VERDICT_RE = re.compile(
    r"^#{3,4} Task (\d+)[a-z]?\s*[—–-]+\s*(✅|❌)(.*)$", re.MULTILINE
)
_SECTION_RE = re.compile(r"^## ", re.MULTILINE)
_REVIEW_HEADING_RE = re.compile(r"^## .*검수.*$", re.MULTILINE)
_TABLE_ROW_RE = re.compile(r"^\|(.+)\|\s*$", re.MULTILINE)
_COMMIT_RE = re.compile(r"`([0-9a-f]{7,40})`")
_ARCHIVE_RE = re.compile(r"^CURRENT_TASK_\d+_phase(\d+)_(\w+)\.md$")
_PHASE_FILE_RE = re.compile(r"^PHASE(\d+)_(.+)\.md$")

_NUMERIC_KEYS = ("cost", "compactions", "interventions", "gaps_found", "gaps_unresolved")
_MIN_VERDICT_COLUMNS = 4
_UNKNOWN_VERDICT = {
    "verdict": None, "verdict_raw": None, "commit": None, "commits": [],
}

# (phase, 문서 경로, 파일명 slug, 파일명 status)
_Entry = tuple[int, Path, str | None, str | None]


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """(메타, 본문). frontmatter가 없으면 ({}, 원문)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    meta: dict = {}
    for line in m.group(1).split("\n"):
        if not line.strip() or line.startswith((" ", "#")):
            continue
        key, _, raw = line.partition(":")
        key, raw = key.strip(), raw.strip().strip('"')
        if key == "commits":
            meta[key] = [c.strip() for c in raw.split(",") if c.strip()]
        elif key == "phase":
            meta[key] = int(raw) if raw.isdigit() else None
        elif key in _NUMERIC_KEYS:
            # 실제 문서는 "21.94" · "$17.5659" · "-" · "$66.28 (내역 설명…)" 을 섞어 쓴다.
            # 통화기호·쉼표만 벗겨 숫자로 떨어지면 숫자로, 아니면 원문을 보존한다
            # (0으로 떨구면 "비용 0원"이라는 거짓이 화면에 뜬다).
            cleaned = raw.lstrip("$").replace(",", "")
            try:
                meta[key] = float(cleaned) if "." in cleaned else int(cleaned)
            except ValueError:
                meta[key] = None
                meta[f"{key}_raw"] = raw
        else:
            meta[key] = raw
    return meta, text[m.end():]


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """공개 API — 쪽지시험 기록 등 다른 모듈이 같은 무의존 파서를 재사용한다."""
    return _parse_frontmatter(text)


def _split_tasks(body: str) -> tuple[str, list[dict]]:
    """(서문, [{n, title, body}]). 절의 끝은 다음 '## '."""
    matches = list(_TASK_HEADING_RE.finditer(body))
    if not matches:
        return body, []
    tasks = []
    for m in matches:
        nxt = _SECTION_RE.search(body, m.end())
        end = nxt.start() if nxt else len(body)
        number, suffix, title = m.group(1), m.group(2), m.group(3)
        tasks.append({
            "n": int(number),
            "label": number + suffix,
            "title": title.strip(),
            "body": body[m.end():end].strip(),
        })
    return body[: matches[0].start()], tasks


def _verdicts(body: str) -> dict[int, dict]:
    """검수 절들의 4열 이상 표 행에서만 판정을 뽑는다. 없으면 빈 dict.

    실제 지시서는 검수를 **두 절로 나눠 쓴다** — ``## 검수 (task마다)``에 리뷰어
    배정(2열), ``## 검수 결과``에 판정(4열). 첫 절만 보면 판정을 통째로 놓치므로
    '검수'가 들어간 헤딩을 전부 훑고, 뒤 절이 앞 절을 덮어쓴다.
    """
    out: dict[int, dict] = {}
    prose: dict[int, dict] = {}
    for head in _REVIEW_HEADING_RE.finditer(body):
        nxt = _SECTION_RE.search(body, head.end())
        section = body[head.end(): nxt.start() if nxt else len(body)]

        for row in _TABLE_ROW_RE.finditer(section):
            # 이스케이프된 파이프(`\|`)는 셀 구분자가 아니다 — 이 저장소 문서가 실제로 쓴다.
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", row.group(1))]
            if len(cells) < _MIN_VERDICT_COLUMNS or not cells[0].isdigit():
                continue
            # 반려→재수정으로 커밋이 여러 개인 행이 흔하다. 하나만 남기지 않는다.
            commits = _COMMIT_RE.findall(cells[3])
            verdict = "pass" if "✅" in cells[2] else "fail" if "❌" in cells[2] else None
            out[int(cells[0])] = {
                "verdict": verdict,
                "verdict_raw": cells[2],
                "commit": commits[0] if commits else None,
                "commits": commits,
            }

        for m in _PROSE_VERDICT_RE.finditer(section):
            prose.setdefault(int(m.group(1)), {
                "verdict": "pass" if m.group(2) == "✅" else "fail",
                "verdict_raw": (m.group(2) + m.group(3)).strip(),
                "commit": None,
                "commits": [],
            })

    # 표가 더 구조적이다 — 산문은 표가 못 채운 번호만 메운다.
    for n, rec in prose.items():
        out.setdefault(n, rec)
    return out


def _read(path: Path, warnings: list[Warning]) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as err:
        warnings.append(Warning(
            code="doc_read_failed",
            severity=SEVERITY_ERROR,
            message=f"{path.name} 을 읽지 못했습니다 ({err.__class__.__name__})",
        ))
        return None
    if "�" in text:
        # errors="replace" 가 조용히 갉아먹은 자리 — OSError 만 알리면 비대칭이다.
        warnings.append(Warning(
            code="doc_encoding_damaged",
            severity=SEVERITY_ERROR,
            message=f"{path.name} 에 UTF-8 로 해석되지 않는 문자가 있습니다 (일부 손상)",
        ))
    return text


def _discover(docs_root: Path) -> tuple[list[_Entry], list[Warning]]:
    """phase 내림차순 목록 + 경고. docs/ 직계가 아카이브를 이긴다."""
    warnings: list[Warning] = []
    if not docs_root.is_dir():
        return [], [Warning(
            code="docs_root_missing",
            severity=SEVERITY_ERROR,
            message=f"문서 디렉터리를 찾지 못했습니다: {docs_root}",
        )]

    found: dict[int, _Entry] = {}

    def consider(phase: int, path: Path, slug: str | None, status: str | None) -> None:
        if phase in found:
            warnings.append(Warning(
                code="doc_duplicate",
                severity=SEVERITY_WARN,
                message=(
                    f"Phase {phase} 지시서가 둘 이상입니다 — {found[phase][1].name} 를 씁니다 "
                    f"(무시: {path.name})"
                ),
                phase=phase,
            ))
            return
        found[phase] = (phase, path, slug, status)

    for path in sorted(docs_root.glob("PHASE*.md")):
        m = _PHASE_FILE_RE.match(path.name)
        if m:
            consider(int(m.group(1)), path, m.group(2), None)

    current = docs_root / "CURRENT_TASK.md"
    if current.is_file():
        text = _read(current, warnings)
        meta, _ = _parse_frontmatter(text or "")
        if meta.get("phase") is not None:
            consider(meta["phase"], current, None, None)
        else:
            warnings.append(Warning(
                code="current_task_phase_unreadable",
                severity=SEVERITY_WARN,
                message="CURRENT_TASK.md 의 frontmatter에서 phase를 읽지 못했습니다",
            ))

    for path in sorted((docs_root / "archive").glob("*.md")):
        m = _ARCHIVE_RE.match(path.name)
        if m:
            consider(int(m.group(1)), path, None, m.group(2))

    numbers = sorted(found)
    if numbers:
        lo, hi = numbers[0], numbers[-1]
        # 문서만 읽으므로 최댓값 너머의 누락은 알 수 없다 — hi 미만만 본다.
        # 첫 문서화 페이즈 이전(< lo)은 누락이 아니라 역사다. 같은 경고로 묶으면
        # 고쳐야 할 구멍 몇 건이 배경 수백 건에 파묻힌다.
        for n in range(1, hi):
            if n in found:
                continue
            pre_history = n < lo
            warnings.append(Warning(
                code="phase_gap_pre_history" if pre_history else "phase_gap_internal",
                severity=SEVERITY_INFO if pre_history else SEVERITY_WARN,
                message=f"Phase {n} 지시서를 찾지 못했습니다 (번호 구멍)",
                phase=n,
            ))
    return [found[n] for n in sorted(found, reverse=True)], warnings


def _review_path(docs_root: Path, phase: int) -> Path | None:
    matches = sorted((docs_root / "reviews").glob(f"PHASE{phase}_*.md"))
    return matches[0] if matches else None


def _nodes_or_raw(text: str, label: str, warnings: list[Warning]) -> list[dict]:
    """마크다운 변환 실패를 백지로 만들지 않는다 — 원문 텍스트 한 노드로 떨어뜨린다."""
    try:
        return markdown_to_nodes(text)
    except Exception as err:  # noqa: BLE001 - 어떤 파서 예외든 절 하나만 잃게 가둔다
        warnings.append(Warning(
            code="markdown_render_failed",
            severity=SEVERITY_ERROR,
            message=f"{label} 마크다운 렌더 실패 ({err.__class__.__name__}) — 원문 표시",
        ))
        return [{"t": "code", "lang": "", "text": text}]


def phase_index(docs_root: Path) -> tuple[list[dict], list[Warning]]:
    """격자용 목록 — phase 메타 + task 헤딩·판정. 본문은 싣지 않는다."""
    docs_root = Path(docs_root)
    entries, warnings = _discover(docs_root)
    phases: list[dict] = []

    for phase, path, slug, name_status in entries:
        text = _read(path, warnings)
        if text is None:
            continue
        meta, body = _parse_frontmatter(text)
        if not meta:
            warnings.append(Warning(
                code="frontmatter_missing",
                severity=SEVERITY_INFO,
                message=f"Phase {phase}: frontmatter 없음 — 파일명에서 복원",
                phase=phase,
            ))
        _, tasks = _split_tasks(body)
        verdicts = _verdicts(body)
        if tasks and not verdicts:
            warnings.append(Warning(
                code="verdict_table_missing",
                severity=SEVERITY_WARN,
                message=f"Phase {phase}: 검수 결과 표 없음 — task 판정 미상",
                phase=phase,
            ))
        review = _review_path(docs_root, phase)

        phases.append({
            "phase": phase,
            "slug": meta.get("slug") or slug,
            "date": meta.get("date"),
            "kind": meta.get("kind"),
            "domain": meta.get("domain"),
            "status": meta.get("status") or name_status,
            "cost": meta.get("cost"),
            "cost_raw": meta.get("cost_raw"),
            "compactions": meta.get("compactions"),
            "interventions": meta.get("interventions"),
            "summary": meta.get("summary"),
            "commits": meta.get("commits") or [],
            "doc_path": str(path),
            "review_path": str(review) if review else None,
            "tasks": [
                {
                    "n": t["n"],
                    "label": t["label"],
                    "title": t["title"],
                    # 'Task 4b'는 'Task 4'의 판정을 물려받으면 안 된다 — 다른 작업이다.
                    **(
                        verdicts.get(t["n"], _UNKNOWN_VERDICT)
                        if t["label"] == str(t["n"])
                        else _UNKNOWN_VERDICT
                    ),
                }
                for t in tasks
            ],
        })

    return phases, warnings


def merge_active(
    phases: list[dict], active_entries: list[dict]
) -> tuple[list[dict], list[Warning]]:
    """활성 등록을 표시하고 문서 없는 등록 행을 합성한다.

    입력은 바꾸지 않아 호출자가 원본 ``phase_index`` 결과를 재사용할 수 있다.
    """
    active_by_phase = {entry["phase"]: entry for entry in active_entries}
    merged = []
    warnings: list[Warning] = []
    for phase in phases:
        row = dict(phase)
        row.setdefault("active", False)
        active_entry = active_by_phase.get(row["phase"])
        if active_entry is not None:
            row["active"] = True
            active_slug = active_entry.get("slug")
            document_slug = row.get("slug")
            if active_slug and document_slug and active_slug != document_slug:
                warnings.append(Warning(
                    code="active_slug_mismatch",
                    severity=SEVERITY_WARN,
                    message=(
                        f"Phase {row['phase']}: active 등록 slug({active_slug})가 "
                        f"문서 slug({document_slug})와 다릅니다"
                    ),
                    phase=row["phase"],
                ))
        merged.append(row)

    phase_keys = (
        "phase",
        "slug",
        "date",
        "kind",
        "domain",
        "status",
        "cost",
        "cost_raw",
        "compactions",
        "interventions",
        "summary",
        "commits",
        "doc_path",
        "review_path",
        "tasks",
    )
    documented_phases = {phase["phase"] for phase in phases}
    for number, entry in active_by_phase.items():
        if number not in documented_phases:
            row = dict.fromkeys(phase_keys)
            row.update(
                phase=number,
                slug=entry.get("slug"),
                status="claimed",
                active=True,
            )
            merged.append(row)

    return sorted(merged, key=lambda phase: phase["phase"], reverse=True), warnings


def phase_raw_text(docs_root: Path, number: int) -> tuple[str | None, list[Warning]]:
    """지시서(+리뷰) 원문 마크다운 — 쪽지시험 LLM 컨텍스트용. 없으면 (None, 경고)."""
    docs_root = Path(docs_root)
    entries, warnings = _discover(docs_root)
    match = next((e for e in entries if e[0] == number), None)
    if match is None:
        return None, warnings
    text = _read(match[1], warnings)
    if text is None:
        return None, warnings
    review = _review_path(docs_root, number)
    if review is not None:
        review_text = _read(review, warnings)
        if review_text:
            text += "\n\n---\n# 검수 문서\n\n" + review_text
    return text, warnings


def phase_detail(docs_root: Path, number: int) -> tuple[dict | None, list[Warning]]:
    """phase 1개의 절별 본문 노드. 없으면 (None, 경고)."""
    docs_root = Path(docs_root)
    entries, warnings = _discover(docs_root)
    match = next((e for e in entries if e[0] == number), None)
    if match is None:
        return None, warnings

    text = _read(match[1], warnings)
    if text is None:
        return None, warnings
    _, body = _parse_frontmatter(text)
    intro, tasks = _split_tasks(body)

    review_nodes: list[dict] = []
    review = _review_path(docs_root, number)
    if review is not None:
        review_text = _read(review, warnings)
        if review_text is not None:
            _, review_body = _parse_frontmatter(review_text)
            review_nodes = _nodes_or_raw(
                review_body, f"Phase {number} 검수 문서", warnings
            )

    return {
        "phase": number,
        "intro": _nodes_or_raw(intro, f"Phase {number} 서문", warnings),
        "tasks": [
            {
                "n": t["n"],
                "label": t["label"],
                "title": t["title"],
                "nodes": _nodes_or_raw(
                    t["body"], f"Phase {number} Task {t['label']}", warnings
                ),
            }
            for t in tasks
        ],
        "review": review_nodes,
    }, warnings

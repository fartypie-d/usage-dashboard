"""FastAPI application — usage-dashboard backend.

Endpoints:
- GET /health
- GET /api/summary?range=15m|1h|24h|7d|30d|all
- GET /api/delegation?range=15m|1h|24h|7d|30d|all
- GET /api/sessions?range=15m|1h|24h|7d|30d|all
- GET /api/work/session/{source}/{session_id}
- GET /api/flow?project=<name>
- POST /api/quiz/start
- POST /api/quiz/reply
- POST /api/quiz/finish

Data sources configurable via USAGE_CLAUDE_ROOT and USAGE_OPENCODE_DB env vars
(default: tests/fixtures/*).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import llm, quiz
from app.metrics.cache_eff import cache_metrics
from app.metrics.cognitive_debt import attach_debt, quiz_details
from app.metrics.common import filter_by_range
from app.metrics.delegation import agents as delegation_agents
from app.metrics.delegation import flow as delegation_flow
from app.metrics.delegation import overhead as delegation_overhead
from app.metrics.delegation_flow import flows as delegation_flows
from app.metrics.flow_audit import audit
from app.metrics.model_mix import daily_cost, mismatches, project_mix
from app.metrics.model_rank import model_rank
from app.metrics.progress_docs import merge_active, phase_detail, phase_index, phase_raw_text
from app.metrics.progress_warnings import (
    SEVERITY_INFO,
    Warning,
    from_messages,
    group_warnings,
    summaries,
)
from app.metrics.session_health import session_health
from app.metrics.summary import compute_kpi
from app.metrics.work_sessions import session_summary
from app.pricing import cost_for, pricing_warnings, refresh_pricing
from app.sources.claude_jsonl import ParserCache, Record, parse_directory
from app.sources.claude_transcript import extract_titles, parse_session_lines
from app.sources.diffs import FileChange, attach_file_index, build_files
from app.sources.opencode_db import read_records
from app.sources.opencode_transcript import session_meta, session_turns
from app.sources.orchestrate_events import project_events
from app.sources.orchestrate_registry import load_projects
from app.sources.transcript_common import project_phase_from_cwd

app = FastAPI(
    title="usage-dashboard",
    version="0.1.0",
)

GZIP_MINIMUM_SIZE = 1000
GIT_LOG_LIMIT = 800
FLOWS_DEFAULT_LIMIT = 20
FLOWS_MAX_LIMIT = 1000

app.add_middleware(GZipMiddleware, minimum_size=GZIP_MINIMUM_SIZE)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def root() -> FileResponse:
    """Serve the dashboard UI (static/index.html)."""
    return FileResponse(STATIC_DIR / "index.html")


VALID_SOURCES = ("all", "claude", "opencode")

# 프로세스 수명 동안 유지되는 파싱 캐시. 없으면 매 요청마다 코퍼스 전체를
# 다시 읽어(로컬 실측 155MB/484파일) /api/sessions가 프런트엔드의 15초 중단
# 한도를 넘겨 "API 응답이 없습니다"로 보인다. mtime이 바뀐 파일만 다시 읽는다.
_PARSER_CACHE = ParserCache()


def _source_paths() -> tuple[Path, Path]:
    """환경 변수로 설정된 (claude_root, opencode_db) 경로."""
    return (
        Path(os.getenv("USAGE_CLAUDE_ROOT", "tests/fixtures/claude_projects")),
        Path(os.getenv("USAGE_OPENCODE_DB", "tests/fixtures/opencode.db")),
    )


def _freshness(records: list[Record]) -> dict[str, int | None]:
    """Return the latest timestamp per source as epoch milliseconds."""
    latest: dict[str, int | None] = {"claude": None, "opencode": None}
    for rec in records:
        if rec.source not in latest:
            continue
        ms = int(rec.timestamp.timestamp() * 1000)
        current = latest[rec.source]
        if current is None or ms > current:
            latest[rec.source] = ms
    return latest


def _load_and_filter(
    range_key: str, source_key: str = "all"
) -> tuple[list[Record], list[str], dict[str, int | None]]:
    """Load records from all sources, filter by range and source.

    Raises HTTPException(400) if range_key or source_key is invalid.

    ``source_freshness`` (third element) is computed on the full record set
    **before** source or range filtering — its purpose is to show "when was
    the last data seen" even when the current live window is empty.
    """
    if source_key not in VALID_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown source: {source_key!r} (expected one of {VALID_SOURCES})",
        )

    # 단가표는 요청 경계에서 갱신한다 — config/pricing.json을 고친 뒤 서버를
    # 재시작하지 않아도 다음 요청부터 반영되도록.
    refresh_pricing()

    claude_root, opencode_db = _source_paths()
    claude_records, claude_warnings = parse_directory(claude_root, cache=_PARSER_CACHE)
    opencode_records, opencode_warnings = read_records(opencode_db)
    all_records = claude_records + opencode_records
    freshness = _freshness(all_records)

    if source_key != "all":
        all_records = [r for r in all_records if r.source == source_key]

    try:
        filtered = filter_by_range(all_records, range_key)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    return filtered, claude_warnings + opencode_warnings, freshness


@app.get("/health")
async def health() -> JSONResponse:
    """Health-check endpoint.

    Returns 200 with ``{"status": "ok"}`` when the service is running.
    """
    return JSONResponse(content={"status": "ok"})


@app.get("/api/summary")
def get_summary(
    range: str = Query(..., alias="range"),
    source: str = Query("all", alias="source"),
) -> JSONResponse:
    """Get dashboard summary metrics for range (15m, 1h, 24h, 7d, 30d, all)."""
    filtered, source_warnings, freshness = _load_and_filter(range, source)

    mm = mismatches(filtered)
    cache_res = cache_metrics(filtered)
    kpi_dict, kpi_warnings = compute_kpi(
        filtered,
        mismatches_list=mm,
        worst_sessions_list=cache_res.get("worst_sessions", []),
    )
    p_mix = project_mix(filtered)
    m_rank = model_rank(filtered)
    d_cost = daily_cost(filtered)

    cache_warnings = cache_res.pop("warnings", []) if "warnings" in cache_res else []

    all_warnings = list(
        dict.fromkeys(
            pricing_warnings() + source_warnings + kpi_warnings + cache_warnings
        )
    )

    generated_at = int(datetime.now(UTC).timestamp() * 1000)

    res_data = {
        "range": range,
        "source": source,
        "generated_at": generated_at,
        "kpi": kpi_dict,
        "project_mix": p_mix,
        "model_rank": m_rank,
        "daily_cost": d_cost,
        "mismatches": mm,
        "cache": cache_res,
        "warnings": all_warnings,
        "source_freshness": freshness,
    }

    return JSONResponse(content=res_data)


@app.get("/api/delegation")
def get_delegation(
    range: str = Query(..., alias="range"),
    source: str = Query("all", alias="source"),
    limit: int = Query(FLOWS_DEFAULT_LIMIT, ge=1, le=FLOWS_MAX_LIMIT),
) -> JSONResponse:
    """Get delegation metrics for range (15m, 1h, 24h, 7d, 30d, all)."""
    filtered, source_warnings, freshness = _load_and_filter(range, source)

    flow_data = delegation_flow(filtered)
    flows_data, flow_warnings = delegation_flows(filtered)
    agents_data = delegation_agents(filtered)
    # overhead + two_hop_count use the full flows_data — cap only the payload.
    overhead_data, overhead_warnings = delegation_overhead(
        flows_data,
        two_hop_count=sum(f["two_hop_count"] for f in flows_data),
    )
    flows_payload = flows_data[:limit]

    # Unknown-model warnings are not preserved by flows()/record_cost; surface
    # them here once per distinct model name (tokens=0 still triggers the check).
    model_warnings: list[str] = []
    for model in dict.fromkeys(rec.model for rec in filtered):
        _, warn = cost_for(model, 0, 0, 0, 0)
        model_warnings.extend(warn)

    warnings_list = list(
        dict.fromkeys(
            pricing_warnings()
            + source_warnings
            + flow_warnings
            + overhead_warnings
            + model_warnings
        )
    )

    return JSONResponse(
        content={
            "range": range,
            "source": source,
            "flow": flow_data,
            "flows": flows_payload,
            "agents": agents_data,
            "overhead": overhead_data,
            "warnings": warnings_list,
            "source_freshness": freshness,
            "flows_total": len(flows_data),
            "flows_limit": limit,
        }
    )


@app.get("/api/sessions")
def get_sessions(
    range: str = Query(..., alias="range"),
    source: str = Query("all", alias="source"),
) -> JSONResponse:
    """Get session health metrics for range (15m, 1h, 24h, 7d, 30d, all)."""
    filtered, source_warnings, freshness = _load_and_filter(range, source)

    sessions_data = session_health(filtered)
    warnings_list = list(dict.fromkeys(pricing_warnings() + source_warnings))

    return JSONResponse(
        content={
            "range": range,
            "source": source,
            "sessions": sessions_data,
            "warnings": warnings_list,
            "source_freshness": freshness,
        }
    )


WORK_SOURCES = ("claude", "opencode")

EMPTY_DIFF_STAT = {"files": 0, "additions": 0, "deletions": 0, "truncated": False}
DIFF_ASSEMBLY_WARNING = (
    "diff 조립에 실패해 파일 목록을 표시하지 못했습니다 ({reason}) — 턴 타임라인은 그대로입니다"
)


def _assemble_diff(
    turns: list[dict[str, Any]], changes: list[FileChange]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[str]]:
    """diff 패널을 조립한다. 실패해도 턴을 잃지 않도록 경고로 강등한다.

    ``diffs.py``는 I/O 없는 순수 연산이라 예외는 곧 로직 버그다. 그래도 상세 응답
    전체(턴 타임라인 포함)를 500으로 날리는 것보다 경고를 실어 200을 유지하는 편이 낫다.
    """
    try:
        files, diff_stat, index_map, warnings = build_files(changes)
        return attach_file_index(turns, index_map), files, diff_stat, warnings
    except Exception as err:  # noqa: BLE001 — 조립 실패를 사용자에게 알리고 계속한다
        reason = f"{err.__class__.__name__}: {err}"
        # change_pos는 attach_file_index가 제거해야 할 임시 키다. 실패 경로에서는
        # attach_file_index가 실행되지 않으므로 여기서 직접 제거한다.
        # 이 경로 자체가 이미 실패 중이므로 stripping이 터져도 턴을 보호한다.
        try:
            clean_turns = [
                {
                    **{k: v for k, v in turn.items() if k != "actions"},
                    "actions": [
                        {k: v for k, v in action.items() if k != "change_pos"}
                        for action in turn.get("actions", [])
                    ],
                }
                for turn in turns
            ]
        except Exception:  # noqa: BLE001 — stripping 실패 시 원본 그대로
            clean_turns = turns
        return clean_turns, [], dict(EMPTY_DIFF_STAT), [DIFF_ASSEMBLY_WARNING.format(reason=reason)]


@app.get("/api/work/session/{source}/{session_id}")
def get_work_session(source: str, session_id: str) -> JSONResponse:
    """작업 브라우저 상세 — 세션 1개의 턴 타임라인 (클릭 시 온디맨드 파싱)."""
    if source not in WORK_SOURCES:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown source: {source!r} (expected one of {WORK_SOURCES})",
        )
    claude_root, opencode_db = _source_paths()

    if source == "claude":
        # 파일 위치는 사용량 파싱 캐시의 Record(session_id → source_file)로 찾는다.
        all_records, corpus_warnings, _ = _load_and_filter("all", "claude")
        session_records = [r for r in all_records if r.session_id == session_id]
        if not session_records:
            detail = f"Unknown claude session: {session_id!r}"
            if corpus_warnings:
                # 인프라 장애(권한·IO 오류)로 인한 미검출을 "세션 없음"과 구분한다.
                detail += f" (corpus warnings: {'; '.join(corpus_warnings)})"
            raise HTTPException(status_code=404, detail=detail)
        rel = session_records[0].source_file
        try:
            with open(claude_root / rel, encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError as err:
            raise HTTPException(
                status_code=404, detail=f"Session file no longer exists: {rel}"
            ) from err
        except OSError as err:
            raise HTTPException(
                status_code=404,
                detail=f"Session file unreadable: {rel} ({err.__class__.__name__})",
            ) from err
        turns, changes, parse_warnings = parse_session_lines(lines, rel)
        parse_warnings = corpus_warnings + parse_warnings
        title = extract_titles(lines).get(session_id)
        project, _, _ = project_phase_from_cwd(
            session_records[0].cwd, session_records[0].project
        )
        meta = {
            **session_summary(session_records, title=title),
            "project": project,
        }
    else:
        meta_row, meta_warnings = session_meta(opencode_db, session_id)
        turns, changes, parse_warnings = session_turns(opencode_db, session_id)
        parse_warnings = meta_warnings + parse_warnings
        if turns is None:
            raise HTTPException(
                status_code=404, detail=f"Unknown opencode session: {session_id!r}"
            )
        if meta_row is None:
            # 세션 행 없이 메시지만 있는 비정상 DB — 죽는 대신 최소 메타로 응답하되 신호를 남긴다.
            parse_warnings = parse_warnings + [
                f"opencode session row missing for {session_id!r} — message-only fallback"
            ]
            meta_row = {
                "id": session_id, "source": "opencode", "title": None,
                "project": "unknown", "phase": None, "phase_slug": None,
                "started_at": None, "ended_at": None,
                "cost_usd": 0.0, "models": [], "agent": None,
                "is_subagent": False,
            }
        meta = meta_row

    turns, files, diff_stat, diff_warnings = _assemble_diff(turns, changes)
    warnings_list = list(dict.fromkeys(parse_warnings + diff_warnings))
    return JSONResponse(
        content={
            "session": meta,
            "turns": turns,
            "files": files,
            "diff_stat": diff_stat,
            "warnings": warnings_list,
        }
    )


def _docs_root() -> Path:
    """문서 루트. 컨테이너·테스트에서 갈아끼울 수 있게 환경변수로 뺀다."""
    return Path(os.getenv("USAGE_DOCS_ROOT", "docs"))


def _registry_dir() -> Path:
    """Orchestrate registry root, configurable for containers and tests."""
    return Path(
        os.getenv(
            "USAGE_REGISTRY_DIR", Path.home() / ".local/state/orchestrate/registry"
        )
    )


def _progress_project(
    project: str | None,
) -> tuple[dict | None, list[dict], list[Warning]]:
    """Resolve a requested project from the registry and retain read warnings."""
    registry_dir = _registry_dir()
    projects, messages = load_projects(registry_dir)
    warnings = from_messages(messages, code="registry")
    if project is None:
        return None, projects, warnings if registry_dir.is_dir() else []

    selected = next((item for item in projects if item["project"] == project), None)
    if selected is None:
        groups = group_warnings(warnings)
        raise HTTPException(
            status_code=404,
            detail={
                "message": f"Unknown project: {project}",
                "warnings": summaries(groups),
                "warning_groups": groups,
            },
        )
    return selected, projects, warnings


def _progress_projects_payload(projects: list[dict]) -> list[dict]:
    """Return the public registry summary without filesystem root paths."""
    return [
        {
            "project": item["project"],
            "docs_dir": item["docs_dir"],
            "next_phase": item["next_phase"],
            "active_count": len(item["active"]),
        }
        for item in projects
    ]


def _progress_docs_root(selected: dict | None) -> Path:
    """선택 프로젝트의 문서 루트 또는 현재 프로젝트의 기본 문서 루트."""
    if selected is None:
        return _docs_root()
    return Path(selected["root"]) / selected["docs_dir"]


def _git_signals(root: Path) -> tuple[list[dict], list[str]]:
    """커밋 제목·시각만 수집한다 — 흐름 추정 판정용. 실패는 경고로 승격."""
    cmd = [
        "git", "-C", str(root), "log", "--format=%H%x09%ct%x09%s",
        "-n", str(GIT_LOG_LIMIT),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], [f"git 신호 수집 실패: {root}: {exc}"]
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        return [], [f"git 신호 수집 실패: {root}: {detail[0] if detail else proc.returncode}"]
    commits = []
    dropped = 0
    for line in proc.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            dropped += 1
            continue
        full_hash, epoch, subject = parts
        commits.append({
            "hash": full_hash[:7],
            "ts_ms": int(epoch) * 1000 if epoch.isdigit() else None,
            "subject": subject,
        })
    messages = []
    if dropped:
        messages.append(f"git 신호 파싱 실패 {dropped}줄 건너뜀")
    if len(commits) >= GIT_LOG_LIMIT:
        messages.append(
            f"git 신호가 최근 {GIT_LOG_LIMIT}커밋으로 제한 — 오래된 페이즈 증거 누락 가능"
        )
    return commits, messages


def _delegation_hints(project_name: str) -> tuple[list[dict], list[str]]:
    """전 기간 레코드를 워크트리 cwd 귀속으로 (프로젝트, phase) 세션 힌트로 접는다."""
    records, warnings, _ = _load_and_filter("all", "all")
    by_session: dict[tuple[str, str], dict] = {}
    for rec in records:
        project, phase, slug = project_phase_from_cwd(rec.cwd, rec.project)
        if project != project_name or phase is None:
            continue
        ts = int(rec.timestamp.timestamp() * 1000)
        hint = by_session.get((rec.source, rec.session_id))
        if hint is None:
            by_session[(rec.source, rec.session_id)] = {
                "phase": phase, "slug": slug, "source": rec.source,
                "session_id": rec.session_id, "agent": rec.agent,
                "start_ms": ts, "end_ms": ts,
            }
        else:
            hint["start_ms"] = min(hint["start_ms"], ts)
            hint["end_ms"] = max(hint["end_ms"], ts)
            hint["agent"] = hint["agent"] or rec.agent
    hints = sorted(by_session.values(), key=lambda h: h["start_ms"])
    return hints, warnings


@app.get("/api/flow")
def get_flow(
    project: str | None = Query(
        None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"
    ),
) -> JSONResponse:
    """작업 흐름 감사 — 페이즈 × 8단계 도달 + task별 위임 디테일."""
    selected, projects, registry_warnings = _progress_project(project)
    phases, warnings = phase_index(_progress_docs_root(selected))
    active_entries = selected["active"] if selected is not None else []
    phases, active_warnings = merge_active(phases, active_entries)
    warnings += active_warnings

    root = Path(selected["root"]) if selected is not None else Path.cwd()
    project_name = selected["project"] if selected is not None else root.name
    events, event_messages = project_events(root, active_entries)
    commits, git_messages = _git_signals(root)
    hints, hint_messages = _delegation_hints(project_name)

    rows, audit_warnings = audit(phases, events, commits, hints)
    groups = group_warnings(list(dict.fromkeys(
        registry_warnings
        + warnings
        + from_messages(audit_warnings, code="flow_audit")
        + from_messages(event_messages, code="events")
        + from_messages(git_messages, code="git_signals", severity=SEVERITY_INFO)
        + from_messages(hint_messages, code="delegation_hints", severity=SEVERITY_INFO)
    )))
    return JSONResponse(content={
        "project": project,
        "projects": _progress_projects_payload(projects),
        "phases": rows,
        "warnings": summaries(groups),
        "warning_groups": groups,
    })


@app.get("/api/progress")
def get_progress(
    project: str | None = Query(
        None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"
    ),
) -> JSONResponse:
    """진행내역 격자 — phase 메타 + task 헤딩·판정 + 인지부채 신호 (본문 없음)."""
    selected, projects, registry_warnings = _progress_project(project)
    docs_root = _progress_docs_root(selected)
    phases, warnings = phase_index(docs_root)
    if selected is not None:
        phases, active_warnings = merge_active(phases, selected["active"])
        warnings += active_warnings
    phases, debt_warnings = attach_debt(phases, docs_root)
    warnings += debt_warnings
    groups = group_warnings(list(dict.fromkeys(registry_warnings + warnings)))
    return JSONResponse(
        content={
            "phases": phases,
            # 평평한 목록은 그룹의 요약에서 파생한다 — 진실의 원천은 하나다.
            "warnings": summaries(groups),
            "warning_groups": groups,
            "project": project,
            "projects": _progress_projects_payload(projects),
            "llm": llm.availability(),
        }
    )


@app.get("/api/progress/phase/{number}")
def get_progress_phase(
    number: int,
    project: str | None = Query(
        None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"
    ),
) -> JSONResponse:
    """phase 1개의 지시서 절별 본문 + 리뷰 문서 + 쪽지시험 기록 (노드 트리)."""
    selected, _, registry_warnings = _progress_project(project)
    docs_root = _progress_docs_root(selected)
    detail, warnings = phase_detail(docs_root, number)
    quizzes, quiz_warnings = quiz_details(docs_root, number)
    warnings += quiz_warnings
    groups = group_warnings(list(dict.fromkeys(registry_warnings + warnings)))
    if detail is None:
        # "그런 phase가 없다"와 "문서는 있는데 못 읽는다"를 같은 404로 뭉개면
        # 운영자가 권한·인코딩 장애를 '없음'으로 오인한다. 원인을 실어 보낸다.
        raise HTTPException(
            status_code=404,
            detail={
                "message": f"Unknown phase: {number}",
                "warnings": summaries(groups),
                "warning_groups": groups,
            },
        )
    detail["quizzes"] = quizzes
    return JSONResponse(
        content={
            **detail,
            "warnings": summaries(groups),
            "warning_groups": groups,
            "project": project,
        }
    )


# ── 쪽지시험 (인지부채 상환) ──────────────────────────────────────────

_QUIZ_PROJECT_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _quiz_params(payload: dict) -> tuple[str | None, int]:
    """body의 project·phase 검증 — Query 패턴 검증의 body 버전."""
    project = payload.get("project")
    if project is not None and (
        not isinstance(project, str) or not _QUIZ_PROJECT_RE.match(project)
    ):
        raise HTTPException(status_code=400, detail="project 형식 오류")
    number = payload.get("phase")
    if not isinstance(number, int) or isinstance(number, bool) or number < 0:
        raise HTTPException(status_code=400, detail="phase는 0 이상의 정수여야 합니다")
    return project, number


def _quiz_setup(project: str | None, number: int) -> tuple[Any, Path, str]:
    """(client, docs_root, phase 원문). 키 부재 503 · 없는 phase 404."""
    client, reason = llm.client_from_env()
    if client is None:
        raise HTTPException(status_code=503, detail={"message": reason})
    selected, _, registry_warnings = _progress_project(project)
    docs_root = _progress_docs_root(selected)
    context, warnings = phase_raw_text(docs_root, number)
    if context is None:
        groups = group_warnings(list(dict.fromkeys(registry_warnings + warnings)))
        raise HTTPException(
            status_code=404,
            detail={
                "message": f"Unknown phase: {number}",
                "warnings": summaries(groups),
                "warning_groups": groups,
            },
        )
    return client, docs_root, context


def _prior_usage(payload: dict) -> tuple[int, int]:
    """클라이언트가 누적해 보낸 사용량 — 경계 검증: 음수·비정수·bool은 400."""
    raw = payload.get("usage")
    if raw is None:
        return 0, 0
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="usage는 객체여야 합니다")
    totals = []
    for key in ("input_tokens", "output_tokens"):
        value = raw.get(key, 0)
        if value is None:
            value = 0
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise HTTPException(status_code=400, detail=f"usage.{key}는 0 이상의 정수여야 합니다")
        totals.append(value)
    return totals[0], totals[1]


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _quiz_sse(client: object, system: str, messages: list[dict]) -> StreamingResponse:
    """LLM 스트림 → SSE. 이미 200이 나간 뒤의 실패는 error 이벤트로 강등한다."""

    def gen():
        try:
            for kind, payload in llm.stream_text(client, system=system, messages=messages):
                if kind == "delta":
                    yield _sse("delta", {"text": payload})
                else:
                    yield _sse("done", {"usage": payload})
        except Exception as err:  # noqa: BLE001 - 스트림 중 예외는 이벤트가 유일한 통로다
            yield _sse("error", {"message": f"LLM 호출 실패: {err.__class__.__name__}: {err}"})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/quiz/start")
def quiz_start(payload: dict = Body(...)) -> StreamingResponse:
    """쪽지시험 시작 — phase 문서를 컨텍스트로 첫 개방형 질문을 스트리밍."""
    project, number = _quiz_params(payload)
    client, _, context = _quiz_setup(project, number)
    return _quiz_sse(
        client,
        quiz.system_prompt(context),
        [{"role": "user", "content": quiz.START_MESSAGE}],
    )


@app.post("/api/quiz/reply")
def quiz_reply(payload: dict = Body(...)) -> StreamingResponse:
    """대화 계속 — 서버 무상태: 클라이언트가 전사를 매번 보낸다."""
    project, number = _quiz_params(payload)
    client, _, context = _quiz_setup(project, number)
    try:
        messages = quiz.validate_transcript(payload.get("transcript"))
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    if not messages or messages[-1]["role"] != "user":
        raise HTTPException(status_code=400, detail="transcript는 사용자 답변으로 끝나야 합니다")
    return _quiz_sse(client, quiz.system_prompt(context), messages)


@app.post("/api/quiz/finish")
def quiz_finish(payload: dict = Body(...)) -> JSONResponse:
    """시험 종료 — LLM 갭 평가 → 기록 마크다운 조립 → quizzes/ 저장 (실패 시 강등)."""
    project, number = _quiz_params(payload)
    client, docs_root, context = _quiz_setup(project, number)
    try:
        messages = quiz.validate_transcript(payload.get("transcript"))
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    if not messages:
        raise HTTPException(status_code=400, detail="빈 전사로는 시험을 마칠 수 없습니다")
    prior_in, prior_out = _prior_usage(payload)

    try:
        evaluation, usage = llm.complete_text(
            client,
            system=quiz.system_prompt(context),
            messages=messages + [{"role": "user", "content": quiz.SUMMARY_PROMPT}],
            max_tokens=llm.EVAL_MAX_TOKENS,
        )
    except Exception as err:  # noqa: BLE001 - 외부 API 실패는 502로 경계에서 알린다
        raise HTTPException(
            status_code=502,
            detail={"message": f"LLM 평가 실패: {err.__class__.__name__}: {err}"},
        ) from err

    truncated = usage.get("stop_reason") == "max_tokens"
    gaps = quiz.parse_summary(evaluation)
    total_in = prior_in + usage.get("input_tokens", 0)
    total_out = prior_out + usage.get("output_tokens", 0)
    refresh_pricing()
    cost, _ = cost_for(llm.model_name(), total_in, total_out)

    now = datetime.now(UTC)
    markdown = quiz.record_markdown(
        phase=number, model=llm.model_name(), date_str=now.strftime("%Y-%m-%d"),
        evaluation_text=evaluation, gaps=gaps,
        usage={"input_tokens": total_in, "output_tokens": total_out}, cost=cost,
    )
    path = quiz.unique_record_path(docs_root, number, now)
    error = quiz.save_record(path, markdown)
    return JSONResponse(content={
        "saved": error is None,
        "path": str(path),
        "error": error,
        "markdown": markdown,
        "gaps": gaps,
        "usage": {"input_tokens": total_in, "output_tokens": total_out},
        "cost": cost,
        "truncated": truncated,
    })

"""/api/flow 통합 테스트 — 픽스처 레지스트리·events·docs 기반."""
import json
import subprocess
from datetime import UTC, datetime

from fastapi.testclient import TestClient

import app.main as main
from app.main import app
from app.sources.claude_jsonl import Record

client = TestClient(app)


def _setup_project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "PHASE3_demo.md").write_text(
        "---\nslug: demo\nstatus: done\n---\n\n## Task 1: 데모\n\n"
        "## 검수 결과\n\n| task | 에이전트 | 판정 | 커밋 |\n|---|---|---|---|\n"
        "| 1 | worker | ✅ | abc1234 |\n",
        encoding="utf-8",
    )
    (root / ".orchestrate").mkdir()
    (root / ".orchestrate/events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in [
            {"ts": "2026-08-10T10:00:00+09:00", "phase": 3, "event": "phase_claimed"},
            {"ts": "2026-08-10T10:10:00+09:00", "phase": 3, "event": "gate_answered",
             "gate": "gate1", "answer": "approve"},
            {"ts": "2026-08-10T10:20:00+09:00", "phase": 3, "event": "delegation_started",
             "task": "1", "agent": "worker"},
            {"ts": "2026-08-10T11:00:00+09:00", "phase": 3, "event": "phase_closed"},
        ]),
        encoding="utf-8",
    )
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "demo.json").write_text(json.dumps({
        "project": "demo", "root": str(root), "docs_dir": "docs",
        "next_phase": 4, "active": [],
    }), encoding="utf-8")
    monkeypatch.setenv("USAGE_REGISTRY_DIR", str(registry))
    # 무거운 전 기간 레코드 로드는 엔드포인트 형태 테스트와 무관 — 잘라낸다.
    monkeypatch.setattr(main, "_delegation_hints", lambda name: ([], []))
    return root


def test_flow_endpoint_shape(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    res = client.get("/api/flow", params={"project": "demo"})
    assert res.status_code == 200
    body = res.json()
    assert body["project"] == "demo"
    assert [p["project"] for p in body["projects"]] == ["demo"]
    row = body["phases"][0]
    assert row["phase"] == 3
    states = {c["id"]: c["state"] for c in row["stages"]}
    assert states["claim"] == "measured"
    assert states["gate1"] == "measured"
    assert states["close"] == "measured"
    assert states["brief"] == "inferred"      # 지시서 문서 존재
    assert row["tasks"][0]["task"] == "1"
    # 픽스처 root는 git 저장소가 아니다 — git 신호 실패가 경고로 드러나야 한다.
    assert any("git" in g["code"] for g in body["warning_groups"])


def test_flow_endpoint_unknown_project_404(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    res = client.get("/api/flow", params={"project": "nope"})
    assert res.status_code == 404
    assert "warning_groups" in res.json()["detail"]


def test_flow_endpoint_broken_events_warn(tmp_path, monkeypatch):
    root = _setup_project(tmp_path, monkeypatch)
    (root / ".orchestrate/events.jsonl").write_text("{bad\n", encoding="utf-8")
    res = client.get("/api/flow", params={"project": "demo"})
    body = res.json()
    assert any(g["code"] == "events" for g in body["warning_groups"])


def test_flow_endpoint_surfaces_delegation_hint_warnings(tmp_path, monkeypatch):
    real_delegation_hints = main._delegation_hints
    _setup_project(tmp_path, monkeypatch)
    # _setup_project가 _delegation_hints를 스텁으로 갈아끼운다 — 이 테스트는 그 경고 배선
    # 자체가 대상이므로 진짜 함수를 되돌리고, 대신 소스 파싱이 깨진 상황을 주입한다.
    monkeypatch.setattr(main, "_delegation_hints", real_delegation_hints)
    monkeypatch.setattr(
        main,
        "_load_and_filter",
        lambda *_: ([], ["소스 파싱 실패: 위임 힌트 fixture"], {}),
    )

    res = client.get("/api/flow", params={"project": "demo"})

    assert res.status_code == 200
    body = res.json()
    assert any(g["code"] == "delegation_hints" for g in body["warning_groups"])
    assert any("소스 파싱 실패" in warning for warning in body["warnings"])


def _hint_record(
    session_id: str, *, project: str, hour: int, agent: str | None = None
) -> Record:
    return Record(
        project=project,
        model="model",
        timestamp=datetime(2026, 8, 10, hour, tzinfo=UTC),
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=0,
        cache_write_tokens=0,
        session_id=session_id,
        source_file="fixture.jsonl",
        source="claude",
        agent=agent,
        cwd=f"/work/{project}/.claude/worktrees/phase3-audit",
        parent_session_id=None,
    )


def test_delegation_hints_folds_sessions_and_filters_project(monkeypatch):
    records = [
        _hint_record("s1", project="demo", hour=10),
        _hint_record("s1", project="demo", hour=12, agent="worker"),
        _hint_record("other", project="other", hour=11, agent="ignored"),
    ]
    monkeypatch.setattr(main, "_load_and_filter", lambda *_: (records, [], {}))

    hints, warnings = main._delegation_hints("demo")

    assert warnings == []
    assert len(hints) == 1
    assert hints[0]["session_id"] == "s1"
    assert hints[0]["agent"] == "worker"
    assert hints[0]["start_ms"] < hints[0]["end_ms"]


def test_git_signals_parses_a_real_repo_including_tabbed_subjects(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    for index, subject in enumerate(["first", "tab\tinside", "third"]):
        file = tmp_path / f"{index}.txt"
        file.write_text(subject, encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(tmp_path), "-c", "user.email=test@example.com",
             "-c", "user.name=Test", "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "-c", "user.email=test@example.com",
             "-c", "user.name=Test", "commit", "-m", subject],
            check=True, capture_output=True,
        )

    commits, messages = main._git_signals(tmp_path)
    assert commits[0]["subject"] == "third"
    assert commits[0]["ts_ms"] is not None
    assert any(c["subject"] == "tab\tinside" for c in commits)
    assert messages == []


def test_git_signals_skips_short_lines_and_non_numeric_epoch(monkeypatch, tmp_path):
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd[:4] == ["git", "-C", str(tmp_path), "log"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="hash\tbad\tsubject\nshort\n", stderr=""
            )
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    commits, messages = main._git_signals(tmp_path)
    assert commits == [{"hash": "hash", "ts_ms": None, "subject": "subject"}]
    assert messages == ["git 신호 파싱 실패 1줄 건너뜀"]


def test_git_signals_reports_truncation_at_configured_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "GIT_LOG_LIMIT", 2)
    monkeypatch.setattr(
        main.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="a\t1\tone\nb\t2\ttwo\n", stderr=""
        ),
    )
    commits, messages = main._git_signals(tmp_path)
    assert len(commits) == 2
    assert messages == ["git 신호가 최근 2커밋으로 제한 — 오래된 페이즈 증거 누락 가능"]

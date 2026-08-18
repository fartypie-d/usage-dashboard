"""쪽지시험 엔드포인트 — fake LLM 주입, 실 API 호출 없음."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

EVALUATION = "gaps_found: 2\ngaps_unresolved: 1\nsummary: 실패 조건 이해 부족\n\n- 갭: ..."


class FakeStream:
    def __init__(self, chunks: list[str], stop_reason: str = "end_turn") -> None:
        self._chunks = chunks
        self._stop_reason = stop_reason

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    @property
    def text_stream(self):
        yield from self._chunks

    def get_final_message(self) -> SimpleNamespace:
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=100, output_tokens=50),
            stop_reason=self._stop_reason,
        )


class FakeClient:
    def __init__(self, chunks: list[str], stop_reason: str = "end_turn") -> None:
        self.calls: list[dict] = []
        outer = self

        class _Messages:
            def stream(self, **kwargs):
                outer.calls.append(kwargs)
                return FakeStream(chunks, stop_reason)

        self.messages = _Messages()


@pytest.fixture()
def docs_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "PHASE1_demo.md").write_text(
        "---\nphase: 1\n---\n## Task 1: 데모\n지시서 본문\n", encoding="utf-8"
    )
    monkeypatch.setenv("USAGE_DOCS_ROOT", str(docs))
    return docs


def _inject(monkeypatch: pytest.MonkeyPatch, fake: FakeClient) -> None:
    from app import llm

    monkeypatch.setattr(llm, "client_from_env", lambda: (fake, None))


def test_quiz_start_streams_question(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, docs_root: Path
) -> None:
    fake = FakeClient(["왜 대안 ", "X가 아닌가?"])
    _inject(monkeypatch, fake)

    response = client.post("/api/quiz/start", json={"phase": 1})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: delta" in response.text
    assert "X가 아닌가?" in response.text
    assert "event: done" in response.text
    assert '"input_tokens": 100' in response.text
    assert "지시서 본문" in fake.calls[0]["system"]  # phase 원문이 컨텍스트로 들어간다


def test_quiz_without_key_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, docs_root: Path
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    response = client.post("/api/quiz/start", json={"phase": 1})

    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]["message"]


def test_quiz_unknown_phase_returns_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, docs_root: Path
) -> None:
    _inject(monkeypatch, FakeClient(["q"]))

    assert client.post("/api/quiz/start", json={"phase": 99}).status_code == 404


def test_quiz_reply_requires_trailing_user_turn(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, docs_root: Path
) -> None:
    _inject(monkeypatch, FakeClient(["q"]))

    response = client.post("/api/quiz/reply", json={
        "phase": 1,
        "transcript": [{"role": "assistant", "text": "질문?"}],
    })

    assert response.status_code == 400


def test_quiz_reply_streams_followup(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, docs_root: Path
) -> None:
    fake = FakeClient(["꼬리 질문?"])
    _inject(monkeypatch, fake)

    response = client.post("/api/quiz/reply", json={
        "phase": 1,
        "transcript": [
            {"role": "assistant", "text": "질문?"},
            {"role": "user", "text": "답변."},
        ],
    })

    assert response.status_code == 200
    assert "꼬리 질문?" in response.text
    assert fake.calls[0]["messages"][-1] == {"role": "user", "content": "답변."}


def test_quiz_finish_saves_record_and_sums_usage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, docs_root: Path
) -> None:
    _inject(monkeypatch, FakeClient([EVALUATION]))

    response = client.post("/api/quiz/finish", json={
        "phase": 1,
        "transcript": [
            {"role": "assistant", "text": "질문?"},
            {"role": "user", "text": "답변."},
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    })

    data = response.json()
    assert response.status_code == 200
    assert data["saved"] is True
    assert data["gaps"] == {"gaps_found": 2, "gaps_unresolved": 1, "summary": "실패 조건 이해 부족"}
    assert data["usage"] == {"input_tokens": 110, "output_tokens": 55}  # 사전 누적 + finish 호출
    assert data["truncated"] is False
    saved = Path(data["path"])
    assert saved.is_file()
    text = saved.read_text(encoding="utf-8")
    assert "gaps_unresolved: 1" in text
    assert "## 대화 전사" not in text
    assert "### A1" not in text
    assert "실패 조건 이해 부족" in text
    # 저장된 기록이 지표에 바로 반영된다
    progress = client.get("/api/progress").json()
    row = next(p for p in progress["phases"] if p["phase"] == 1)
    assert row["debt"]["level"] == "partial"


def test_quiz_finish_flags_truncation_when_eval_hits_max_tokens(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, docs_root: Path
) -> None:
    _inject(monkeypatch, FakeClient([EVALUATION], stop_reason="max_tokens"))

    response = client.post("/api/quiz/finish", json={
        "phase": 1,
        "transcript": [
            {"role": "assistant", "text": "질문?"},
            {"role": "user", "text": "답변."},
        ],
    })

    data = response.json()
    assert response.status_code == 200
    assert data["truncated"] is True


def test_quiz_finish_rejects_malformed_usage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, docs_root: Path
) -> None:
    _inject(monkeypatch, FakeClient([EVALUATION]))

    response = client.post("/api/quiz/finish", json={
        "phase": 1,
        "transcript": [{"role": "assistant", "text": "질문?"}, {"role": "user", "text": "답."}],
        "usage": {"input_tokens": "abc"},
    })

    assert response.status_code == 400


def test_quiz_finish_degrades_when_save_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, docs_root: Path
) -> None:
    _inject(monkeypatch, FakeClient([EVALUATION]))
    (docs_root / "quizzes").write_text("디렉터리 자리를 파일이 막았다", encoding="utf-8")

    response = client.post("/api/quiz/finish", json={
        "phase": 1,
        "transcript": [{"role": "assistant", "text": "질문?"}, {"role": "user", "text": "답."}],
    })

    data = response.json()
    assert response.status_code == 200
    assert data["saved"] is False
    assert data["error"]
    assert "gaps_unresolved: 1" in data["markdown"]  # 원문은 항상 반환 — 수동 저장 경로


def test_quiz_start_llm_error_becomes_sse_error_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, docs_root: Path
) -> None:
    class BrokenClient:
        class messages:  # noqa: N801 - anthropic 인터페이스 셰임
            @staticmethod
            def stream(**kwargs):
                raise RuntimeError("boom")

    from app import llm

    monkeypatch.setattr(llm, "client_from_env", lambda: (BrokenClient(), None))

    response = client.post("/api/quiz/start", json={"phase": 1})

    assert response.status_code == 200  # SSE는 이미 열렸다 — 이벤트로 강등
    assert "event: error" in response.text
    assert "boom" in response.text


def test_quiz_rejects_bad_params(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, docs_root: Path
) -> None:
    _inject(monkeypatch, FakeClient(["q"]))

    assert client.post("/api/quiz/start", json={"phase": "일"}).status_code == 400
    assert client.post("/api/quiz/start", json={"phase": 1, "project": "../etc"}).status_code == 400

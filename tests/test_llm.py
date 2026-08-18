"""app/llm.py — 클라이언트 팩토리·스트림 래퍼. 실 API 호출 없음(fake 주입)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import llm


class FakeStream:
    """anthropic의 messages.stream() 컨텍스트 매니저 셰임."""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    @property
    def text_stream(self):
        yield from self._chunks

    def get_final_message(self) -> SimpleNamespace:
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=12, output_tokens=34),
            stop_reason="end_turn",
        )


class FakeClient:
    def __init__(self, chunks: list[str]) -> None:
        self.calls: list[dict] = []
        outer = self

        class _Messages:
            def stream(self, **kwargs):
                outer.calls.append(kwargs)
                return FakeStream(chunks)

        self.messages = _Messages()


def test_stream_text_yields_deltas_then_done_with_usage() -> None:
    client = FakeClient(["안녕", "하세요"])

    events = list(llm.stream_text(client, system="s", messages=[{"role": "user", "content": "q"}]))

    assert events[:-1] == [("delta", "안녕"), ("delta", "하세요")]
    assert events[-1] == (
        "done",
        {"input_tokens": 12, "output_tokens": 34, "stop_reason": "end_turn"},
    )
    assert client.calls[0]["model"] == llm.model_name()
    assert client.calls[0]["system"] == "s"
    # claude-sonnet-5는 샘플링 파라미터를 거부한다 — 보내지 않는 것이 계약이다.
    assert "temperature" not in client.calls[0]
    assert "thinking" not in client.calls[0]


def test_complete_text_joins_chunks_and_returns_usage() -> None:
    client = FakeClient(["gaps_found: 1\n", "summary: ok"])

    text, usage = llm.complete_text(client, system="s", messages=[{"role": "user", "content": "q"}])

    assert text == "gaps_found: 1\nsummary: ok"
    assert usage == {"input_tokens": 12, "output_tokens": 34, "stop_reason": "end_turn"}


def test_model_name_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    assert llm.model_name() == llm.DEFAULT_MODEL
    monkeypatch.setenv("USAGE_LLM_MODEL", "claude-opus-5")
    assert llm.model_name() == "claude-opus-5"


def test_client_from_env_without_key_returns_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    client, reason = llm.client_from_env()

    assert client is None
    assert "ANTHROPIC_API_KEY" in reason


def test_availability_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    status = llm.availability()

    assert status["available"] is False
    assert "ANTHROPIC_API_KEY" in status["reason"]


def test_availability_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    assert llm.availability() == {"available": True, "reason": None}


def test_availability_with_bearer_token_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """게이트웨이가 Authorization: Bearer를 요구하는 경우 — SDK가 직접 읽는 변수."""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "gw-token")

    assert llm.availability() == {"available": True, "reason": None}


def test_availability_reason_names_both_auth_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    status = llm.availability()

    assert status["available"] is False
    assert "ANTHROPIC_API_KEY" in status["reason"]
    assert "ANTHROPIC_AUTH_TOKEN" in status["reason"]

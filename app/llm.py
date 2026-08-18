"""Claude API 클라이언트 — 대시보드 유일의 외부 LLM 호출 지점.

환경 변수 (모두 사용자가 직접 설정):
- ``ANTHROPIC_API_KEY``   : 인증 방식 1 — ``x-api-key`` 헤더로 전송. 실제 Anthropic
  키일 필요는 없다 — Anthropic 호환 엔드포인트(게이트웨이)의 키도 그대로 쓴다.
- ``ANTHROPIC_AUTH_TOKEN``: 인증 방식 2 — ``Authorization: Bearer`` 헤더로 전송.
  게이트웨이가 Bearer를 요구할 때 이쪽을 쓴다. **둘 중 하나만** 설정할 것
  (둘 다 있으면 SDK가 두 헤더를 모두 보내 서버가 거부할 수 있다).
- ``ANTHROPIC_BASE_URL``  : 선택. anthropic SDK가 직접 읽는다 — 게이트웨이·프록시
  엔드포인트 교체용. 이 모듈은 값을 만지지 않는다.
- ``USAGE_LLM_MODEL``     : 선택. 기본 ``claude-sonnet-5``.

인증 변수가 둘 다 없으면 LLM 기능이 비활성(503)으로 강등된다.

키는 서버 프로세스에만 존재하고 응답·로그에 싣지 않는다.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

DEFAULT_MODEL = "claude-sonnet-5"
QUIZ_MAX_TOKENS = 4000
# 어댑티브 사고가 max_tokens를 나눠 쓴다 — 평가는 여유를 더 준다.
EVAL_MAX_TOKENS = 8000

_NO_KEY = (
    "ANTHROPIC_API_KEY(또는 ANTHROPIC_AUTH_TOKEN) 미설정 — "
    "LLM 기능(쪽지시험)을 사용할 수 없습니다"
)
_NO_SDK = "anthropic 패키지 미설치 — .venv/bin/pip install anthropic"


def model_name() -> str:
    return os.getenv("USAGE_LLM_MODEL", DEFAULT_MODEL)


def availability() -> dict:
    """프런트가 버튼 활성/비활성을 정할 수 있게 — 클라이언트를 만들지 않는다."""
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        return {"available": False, "reason": _NO_KEY}
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return {"available": False, "reason": _NO_SDK}
    return {"available": True, "reason": None}


def client_from_env() -> tuple[Any | None, str | None]:
    """(클라이언트, 불가 사유). 키 부재·SDK 미설치면 (None, 사유).

    지연 import — 컨테이너 이미지에 SDK가 없어도 나머지 API는 계속 동작해야 한다.
    """
    status = availability()
    if not status["available"]:
        return None, status["reason"]
    import anthropic

    return anthropic.Anthropic(), None


def stream_text(
    client: Any,
    *,
    system: str,
    messages: list[dict],
    max_tokens: int = QUIZ_MAX_TOKENS,
) -> Iterator[tuple[str, Any]]:
    """('delta', 텍스트 조각)… 마지막에 ('done', 사용량 dict).

    API 예외는 여기서 삼키지 않는다 — 호출자(SSE 변환부)가 error 이벤트로 강등한다.
    claude-sonnet-5는 샘플링 파라미터를 거부하므로 model/max_tokens/system/messages만 보낸다.
    """
    with client.messages.stream(
        model=model_name(), max_tokens=max_tokens, system=system, messages=messages
    ) as stream:
        for text in stream.text_stream:
            yield "delta", text
        final = stream.get_final_message()
        yield "done", {
            "input_tokens": final.usage.input_tokens,
            "output_tokens": final.usage.output_tokens,
            "stop_reason": final.stop_reason,
        }


def complete_text(
    client: Any,
    *,
    system: str,
    messages: list[dict],
    max_tokens: int = QUIZ_MAX_TOKENS,
) -> tuple[str, dict]:
    """스트림을 다 모아 (전체 텍스트, 사용량)으로 반환 — finish 평가용."""
    parts: list[str] = []
    usage: dict = {}
    for kind, payload in stream_text(
        client, system=system, messages=messages, max_tokens=max_tokens
    ):
        if kind == "delta":
            parts.append(payload)
        else:
            usage = payload
    return "".join(parts), usage

"""쪽지시험 — 인지부채 상환 대화의 프롬프트·전사 검증·기록 생성 (순수 함수).

LLM 호출은 app/llm.py, 엔드포인트 배선은 app/main.py 소관. 여기는 부작용이
save_record 하나뿐이다. roboco.io '인지부채' 글의 규칙을 SYSTEM_PROMPT 에
인코딩한다: 답은 사용자 입에서, 개방형 질문 → 꼬리 질문, 큰 갭은 주제당 한 번만 확인한다.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

MAX_TURNS = 40
MAX_TEXT_LEN = 8000
MAX_CONTEXT_CHARS = 24000

SYSTEM_PROMPT = """\
너는 '쪽지시험' 면접관이다. 사용자는 AI 에이전트에게 위임해 만든 작업(phase)의
소유자이고, 목표는 사용자의 인지부채 — 산출물과 이해 사이의 격차 — 를 드러내고
갚게 하는 것이다.

규칙:
- 답은 항상 사용자의 입에서 나와야 한다. 네가 먼저 설명하지 마라.
- 개방형 질문으로 시작한다. 예: "왜 대안 X가 아니라 이 설계인가?", "이 변경의 실패 조건은?"
- 답변에 큰 이해 갭이 있을 때만 꼬리 질문을 던진다. 꼬리 질문은 주제당 최대 1회만
  허용한다. 꼬리 질문 1회 후에도 갭이 해소되지 않으면 짧게 해설하고 새 주제로 넘어간다.
- 사용자가 답을 제출할 때마다 기본적으로 새 주제의 질문을 던진다. 주제 3~5개를 다루면
  새 질문 대신 "이제 '시험 종료' 버튼을 눌러 마무리하세요"라고 안내한다.
- 질문은 한 번에 하나만. 짧고 구체적으로. 불편함은 정상이다 — 봐주지 마라.
- 사용자가 "끝"·"종료"라고 말해도 시험을 스스로 마무리하지 말고, "'시험 종료' 버튼을
  눌러 마무리하세요"라고만 안내한다.
- 시험 종료는 사용자가 "시험 종료" 버튼을 눌렀을 때만 일어난다. 네가 스스로 시험을
  종료하거나 평가 완료를 선언하지 마라.

아래는 이 phase의 지시서·검수 문서다. 이 내용을 근거로만 질문하라.

"""

START_MESSAGE = (
    "시험을 시작하라. 이 phase에서 가장 중요한 설계 결정 하나를 골라 "
    "개방형 질문을 하나만 던져라."
)

SUMMARY_PROMPT = """\
지금까지의 쪽지시험 대화를 평가자로서 요약하라. 반드시 아래 형식의 세 줄로 시작한다:
gaps_found: <발견된 갭 개수(정수)>
gaps_unresolved: <끝까지 해소되지 않은 갭 개수(정수)>
summary: <한 문장 요약>

그 다음 줄부터 발견된 갭 각각을 '- 갭: 해소 여부와 근거' 형식으로 나열하라."""


def system_prompt(context: str) -> str:
    if len(context) > MAX_CONTEXT_CHARS:
        context = context[:MAX_CONTEXT_CHARS] + "\n\n(… 이하 생략 — 컨텍스트 한도 초과)"
    return SYSTEM_PROMPT + context


def validate_transcript(raw: object) -> list[dict]:
    """[{'role','text'}] → Claude messages. 위반은 ValueError (경계 검증)."""
    if not isinstance(raw, list):
        raise ValueError("transcript는 배열이어야 합니다")
    if len(raw) > MAX_TURNS:
        raise ValueError(f"transcript는 {MAX_TURNS}턴 이하여야 합니다")
    messages: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("transcript 항목은 객체여야 합니다")
        role, text = item.get("role"), item.get("text")
        if role not in ("assistant", "user"):
            raise ValueError("role은 assistant 또는 user여야 합니다")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text는 비어 있지 않은 문자열이어야 합니다")
        if len(text) > MAX_TEXT_LEN:
            raise ValueError(f"text는 {MAX_TEXT_LEN}자 이하여야 합니다")
        messages.append({"role": role, "content": text})
    return messages


def _int_field(text: str, key: str) -> int | None:
    m = re.search(rf"^{key}:\s*(\d+)\s*$", text, re.MULTILINE)
    return int(m.group(1)) if m else None


def parse_summary(text: str) -> dict:
    """평가 응답의 머리 3줄을 뽑는다. 형식을 안 지키면 None — 지어내지 않는다."""
    m = re.search(r"^summary:\s*(.+)$", text, re.MULTILINE)
    return {
        "gaps_found": _int_field(text, "gaps_found"),
        "gaps_unresolved": _int_field(text, "gaps_unresolved"),
        "summary": m.group(1).strip() if m else None,
    }


def _value_or_dash(value: int | None) -> str:
    # 미상을 0으로 떨구면 "부채 상환"이라는 거짓이 지표에 뜬다 — 대시로 보존한다.
    return str(value) if value is not None else "-"


def record_markdown(
    *,
    phase: int,
    model: str,
    date_str: str,
    evaluation_text: str,
    gaps: dict,
    usage: dict,
    cost: float,
) -> str:
    lines = [
        "---",
        f"phase: {phase}",
        f"date: {date_str}",
        f"model: {model}",
        f"gaps_found: {_value_or_dash(gaps['gaps_found'])}",
        f"gaps_unresolved: {_value_or_dash(gaps['gaps_unresolved'])}",
        f"tokens_in: {usage['input_tokens']}",
        f"tokens_out: {usage['output_tokens']}",
        f"cost: {cost:.4f}",
        "---",
        "",
        f"# Phase {phase} 쪽지시험 ({date_str})",
        "",
        "## 평가",
        "",
        evaluation_text.strip(),
    ]
    return "\n".join(lines) + "\n"


def record_path(docs_root: Path, phase: int, stamp: str) -> Path:
    """stamp 는 YYYYMMDD-HHMMSS — 같은 날 두 번 시험해도 덮어쓰지 않는다."""
    return Path(docs_root) / "quizzes" / f"PHASE{phase}_{stamp}.md"


def unique_record_path(docs_root: Path, phase: int, now: datetime) -> Path:
    """초 단위 충돌 시 1초씩 밀어 파일명 규약(YYYYMMDD-HHMMSS)을 지키며 덮어쓰기를 피한다."""
    path = record_path(docs_root, phase, now.strftime("%Y%m%d-%H%M%S"))
    for offset in range(1, 60):
        if not path.exists():
            return path
        bumped = now + timedelta(seconds=offset)
        path = record_path(docs_root, phase, bumped.strftime("%Y%m%d-%H%M%S"))
    return path


def save_record(path: Path, markdown: str) -> str | None:
    """성공 None, 실패 사유 문자열 — ro 마운트에서도 500이 아니라 강등이어야 한다.

    배타 생성(exclusive create)이라 경합 상황에서도 기존 기록을 덮어쓰지 않는다.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as fh:
            fh.write(markdown)
    except OSError as err:
        return f"{err.__class__.__name__}: {err}"
    return None

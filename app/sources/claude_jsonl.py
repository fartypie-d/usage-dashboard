import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.sources.claude_subagents import (
    FALLBACK_AGENT,
    agent_from_meta,
    build_agent_index,
    is_subagent_file,
    resolve_agent_from_lines,
)

REQUIRED_TOP_FIELDS = ("sessionId", "cwd", "timestamp")
REQUIRED_MESSAGE_FIELDS = ("model", "usage")

# 실데이터에서 정상적으로 대량 발생하는 행 — 경고 없이 건너뛴다.
# type 분포: assistant / user / attachment / last-prompt / ai-title /
# mode / permission-mode / queue-operation 중 토큰을 가진 것은 assistant뿐.
ASSISTANT_TYPE = "assistant"
SKIP_MODELS = frozenset({"<synthetic>"})


@dataclass(frozen=True)
class Record:
    project: str
    model: str
    timestamp: datetime
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    session_id: str
    source_file: str
    source: str
    agent: str | None = None
    cwd: str | None = None
    parent_session_id: str | None = None
    dispatcher_file: str | None = None


class ParserCache:
    """mtime-keyed parse cache, safe to share across request threads.

    FastAPI runs sync handlers in a threadpool, so several requests can scan the
    corpus at once. The lock keeps the mapping consistent; a concurrent cold miss
    may still parse the same file twice, which costs time but never correctness.
    """

    def __init__(self) -> None:
        self._cache: dict[Path, tuple[float, list[Record]]] = {}
        self._lock = threading.Lock()

    def get(self, path: Path, mtime: float) -> list[Record] | None:
        with self._lock:
            entry = self._cache.get(path)
        if entry is not None:
            cached_mtime, records = entry
            if cached_mtime == mtime:
                return records
        return None

    def set(self, path: Path, mtime: float, records: list[Record]) -> None:
        with self._lock:
            self._cache[path] = (mtime, records)


def _parse_iso_timestamp(ts_str: str) -> datetime:
    if ts_str.endswith("Z"):
        ts_str = ts_str[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_line(
    data: dict,
    source_file: str,
    line_num: int,
    agent: str | None,
    parent_session_id: str | None = None,
    dispatcher_file: str | None = None,
) -> tuple[Record | None, str | None]:
    # type이 있고 assistant가 아니면 조용히 스킵.
    # type 자체가 없으면 아래 필수 필드 검사로 떨어뜨려 스키마 드리프트를 경고한다.
    rec_type = data.get("type")
    if rec_type is not None and rec_type != ASSISTANT_TYPE:
        return None, None

    message = data.get("message")
    if message is not None and not isinstance(message, dict):
        return None, (
            f"{source_file}:{line_num}: 'message' is not a dict "
            f"(type={type(message).__name__})"
        )
    message = message or {}

    missing = [f for f in REQUIRED_TOP_FIELDS if not data.get(f)]
    missing += [f for f in REQUIRED_MESSAGE_FIELDS if not message.get(f)]
    if missing:
        return None, (
            f"{source_file}:{line_num}: Missing required field(s): "
            f"{', '.join(missing)}"
        )

    usage = message["usage"]
    if not isinstance(usage, dict):
        return None, (
            f"{source_file}:{line_num}: 'usage' is not a dict "
            f"(type={type(usage).__name__})"
        )

    model = message["model"]
    if model in SKIP_MODELS:
        return None, None

    cwd = data["cwd"]
    rec = Record(
        project=Path(cwd).name if cwd else "unknown",
        model=model,
        timestamp=_parse_iso_timestamp(data["timestamp"]),
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        cache_read_tokens=int(usage.get("cache_read_input_tokens", 0)),
        cache_write_tokens=int(usage.get("cache_creation_input_tokens", 0)),
        session_id=data["sessionId"],
        source_file=source_file,
        source="claude",
        agent=agent,
        cwd=cwd,
        parent_session_id=parent_session_id,
        dispatcher_file=dispatcher_file,
    )
    return rec, None


def parse_directory(
    root: Path, cache: ParserCache | None = None
) -> tuple[list[Record], list[str]]:
    records: list[Record] = []
    warnings: list[str] = []

    if not root.exists() or not root.is_dir():
        warnings.append(f"Directory does not exist or is not a directory: {root}")
        return records, warnings

    jsonl_files = sorted(root.rglob("*.jsonl"))

    # 에이전트 인덱스는 실제로 서브에이전트 파일을 새로 파싱할 때만 만든다.
    # 인덱스 구축은 메인 체인 파일을 전부 훑기 때문에, 캐시가 더운 요청에서
    # 이걸 매번 돌리면 캐시가 아낀 비용을 그대로 되돌려준다.
    agent_index: dict[str, tuple[str, str | None]] | None = None

    def get_agent_index() -> dict[str, tuple[str, str | None]]:
        nonlocal agent_index
        if agent_index is None:
            # 서브에이전트도 다른 서브에이전트를 디스패치하므로 전체 파일을 훑는다.
            agent_index = build_agent_index(jsonl_files)
        return agent_index

    fallback_count = 0

    for jsonl_file in jsonl_files:
        rel = str(jsonl_file.relative_to(root))
        try:
            stat_res = jsonl_file.stat()
            mtime = stat_res.st_mtime
        except FileNotFoundError:
            # 세션 디렉터리는 스캔 중에도 정리된다. rglob이 잡은 뒤 사라진 파일은
            # 정상적인 경합이므로 경고 없이 건너뛴다.
            continue
        except Exception as exc:
            warnings.append(f"{rel}: {exc}")
            continue

        if cache is not None:
            cached_records = cache.get(jsonl_file, mtime)
            if cached_records is not None:
                records.extend(cached_records)
                if cached_records and cached_records[0].agent == FALLBACK_AGENT:
                    fallback_count += 1
                continue

        file_records: list[Record] = []
        try:
            with open(jsonl_file, encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            continue
        except Exception as exc:
            warnings.append(f"{rel}: {exc}")
            continue

        # 에이전트 이름은 방금 읽은 내용에서 복원한다 — 같은 파일을 두 번 열지
        # 않고, 두 읽기 사이에 파일이 지워지는 경합도 함께 사라진다.
        if is_subagent_file(jsonl_file):
            agent, dispatcher = resolve_agent_from_lines(lines, get_agent_index())
            if agent == FALLBACK_AGENT:
                # 프롬프트 매칭 실패 — 하네스가 옆에 쓰는 .meta.json의 agentType으로
                # 이름만이라도 복원한다 (디스패처는 여전히 미상).
                agent = agent_from_meta(jsonl_file) or FALLBACK_AGENT
            parent_session_id = jsonl_file.parent.parent.name
        else:
            agent, dispatcher, parent_session_id = None, None, None

        dispatcher_file: str | None = None
        if dispatcher is not None:
            try:
                dispatcher_file = str(Path(dispatcher).relative_to(root))
            except ValueError:
                # 인덱스는 root를 모르므로 바깥 경로가 들어올 수 있다. 그러면 버린다.
                dispatcher_file = None

        if agent == FALLBACK_AGENT:
            fallback_count += 1

        for line_num, line in enumerate(lines, 1):
            line_str = line.strip()
            if not line_str:
                continue

            try:
                data = json.loads(line_str)
                rec, warn = _parse_line(
                    data, rel, line_num, agent, parent_session_id, dispatcher_file
                )
                if warn:
                    warnings.append(warn)
                if rec:
                    file_records.append(rec)
            except Exception as exc:
                warnings.append(f"{rel}:{line_num}: {exc}")

        if cache is not None:
            cache.set(jsonl_file, mtime, file_records)

        records.extend(file_records)

    if fallback_count:
        warnings.append(
            f"서브에이전트 이름을 복원하지 못한 파일 {fallback_count}개 — "
            f"'{FALLBACK_AGENT}'로 집계됩니다"
        )

    return records, warnings

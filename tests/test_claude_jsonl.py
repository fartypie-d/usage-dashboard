import json
import os
from datetime import UTC
from pathlib import Path
from unittest.mock import patch

import pytest

from app.sources.claude_jsonl import ParserCache, Record, parse_directory

VALID_JSONL_LINE: str = (
    json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-07-24T02:17:08.784Z",
            "cwd": "/anon/proj1",
            "sessionId": "sess-123",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-7",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 5,
                    "cache_creation_input_tokens": 0,
                },
            },
        }
    )
    + "\n"
)


def test_parse_directory_returns_normalized_records(fixtures_dir: Path):
    fixture_dir = fixtures_dir / "claude_projects"
    records, warnings = parse_directory(fixture_dir)

    assert len(records) >= 1
    assert isinstance(warnings, list)

    for rec in records:
        assert isinstance(rec, Record)
        assert isinstance(rec.project, str) and rec.project
        assert isinstance(rec.model, str) and rec.model
        assert rec.timestamp.tzinfo is not None
        assert rec.timestamp.tzinfo == UTC
        assert isinstance(rec.input_tokens, int)
        assert isinstance(rec.output_tokens, int)
        assert isinstance(rec.cache_read_tokens, int)
        assert isinstance(rec.cache_write_tokens, int)
        assert isinstance(rec.session_id, str) and rec.session_id
        assert isinstance(rec.source_file, str) and rec.source_file
        assert rec.agent is None or isinstance(rec.agent, str)


def test_parse_directory_skips_malformed_lines_with_warning(tmp_path: Path):
    broken_file = tmp_path / "broken.jsonl"
    invalid_line = "{\"invalid\n"
    broken_file.write_text(VALID_JSONL_LINE + invalid_line, encoding="utf-8")

    records, warnings = parse_directory(tmp_path)

    assert len(records) == 1
    assert records[0].session_id == "sess-123"
    assert len(warnings) >= 1
    assert any("broken.jsonl:2" in w for w in warnings)


def test_parse_directory_returns_warning_on_missing_root(tmp_path: Path):
    missing_dir = tmp_path / "non_existent"
    records, warnings = parse_directory(missing_dir)

    assert records == []
    assert len(warnings) == 1
    assert "non_existent" in warnings[0] or "does not exist" in warnings[0].lower()


def test_file_deleted_mid_scan_is_skipped_without_warning(tmp_path: Path):
    """A file removed between listing and reading is a benign race, not an error.

    세션 디렉터리는 스캔 도중에도 정리될 수 있다. rglob이 잡은 뒤 사라진 파일은
    조용히 건너뛰어야 하며, 운영자에게 ENOENT를 노출하면 안 된다.
    """
    # Arrange
    live = tmp_path / "live.jsonl"
    live.write_text(VALID_JSONL_LINE, encoding="utf-8")
    doomed = tmp_path / "doomed.jsonl"
    doomed.write_text(VALID_JSONL_LINE, encoding="utf-8")

    real_open = open

    def open_but_doomed_is_gone(file, *args, **kwargs):
        if Path(file) == doomed:
            raise FileNotFoundError(2, "No such file or directory", str(doomed))
        return real_open(file, *args, **kwargs)

    # Act
    with patch("builtins.open", side_effect=open_but_doomed_is_gone):
        records, warnings = parse_directory(tmp_path)

    # Assert
    assert len(records) == 1
    assert warnings == []


def test_vanished_subagent_file_does_not_inflate_fallback_warning(tmp_path: Path):
    """A subagent file that disappears must not be reported as unresolvable."""
    # Arrange
    subagents = tmp_path / "sess" / "subagents"
    subagents.mkdir(parents=True)
    doomed = subagents / "agent-gone.jsonl"
    doomed.write_text(VALID_JSONL_LINE, encoding="utf-8")

    real_open = open

    def open_but_doomed_is_gone(file, *args, **kwargs):
        if Path(file) == doomed:
            raise FileNotFoundError(2, "No such file or directory", str(doomed))
        return real_open(file, *args, **kwargs)

    # Act
    with patch("builtins.open", side_effect=open_but_doomed_is_gone):
        records, warnings = parse_directory(tmp_path)

    # Assert
    assert records == []
    assert not any("복원하지 못한" in w for w in warnings)
    assert warnings == []


def test_unmatched_subagent_falls_back_to_meta_json_agent_type(tmp_path: Path):
    """프롬프트 매칭이 실패해도 옆의 ``.meta.json`` agentType으로 이름을 복원한다."""
    # Arrange — 인덱스에 매칭될 메인 체인 디스패치가 없는 서브에이전트 파일
    subagents = tmp_path / "sess" / "subagents"
    subagents.mkdir(parents=True)
    sub = subagents / "agent-a.jsonl"
    sub.write_text(VALID_JSONL_LINE, encoding="utf-8")
    (subagents / "agent-a.meta.json").write_text(
        json.dumps({"agentType": "general-purpose", "description": "d"}),
        encoding="utf-8",
    )

    # Act
    records, warnings = parse_directory(tmp_path)

    # Assert — 이름은 meta에서, 디스패처는 미상이므로 폴백 경고도 없다
    assert len(records) == 1
    assert records[0].agent == "general-purpose"
    assert not any("복원하지 못한" in w for w in warnings)


def test_unmatched_subagent_without_meta_still_warns(tmp_path: Path):
    """meta.json마저 없으면 기존처럼 폴백 이름 + 경고를 유지한다."""
    subagents = tmp_path / "sess" / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-a.jsonl").write_text(VALID_JSONL_LINE, encoding="utf-8")

    records, warnings = parse_directory(tmp_path)

    assert len(records) == 1
    assert records[0].agent == "claude-subagent"
    assert any("복원하지 못한" in w for w in warnings)


def test_warm_cache_skips_building_the_agent_index(tmp_path: Path):
    """Index building scans every main-chain file — skip it when nothing changed."""
    # Arrange
    (tmp_path / "sess").mkdir()
    (tmp_path / "sess" / "main.jsonl").write_text(VALID_JSONL_LINE, encoding="utf-8")
    subagents = tmp_path / "sess" / "subagents"
    subagents.mkdir()
    (subagents / "agent-a.jsonl").write_text(VALID_JSONL_LINE, encoding="utf-8")

    cache = ParserCache()
    parse_directory(tmp_path, cache=cache)  # cold: index gets built

    # Act
    with patch(
        "app.sources.claude_jsonl.build_agent_index", side_effect=AssertionError
    ) as spy:
        parse_directory(tmp_path, cache=cache)

    # Assert
    spy.assert_not_called()


def test_agent_index_is_still_built_when_a_subagent_file_changes(tmp_path: Path):
    """Correctness guard: a modified subagent file must be re-resolved."""
    # Arrange
    (tmp_path / "sess").mkdir()
    (tmp_path / "sess" / "main.jsonl").write_text(VALID_JSONL_LINE, encoding="utf-8")
    subagents = tmp_path / "sess" / "subagents"
    subagents.mkdir()
    sub = subagents / "agent-a.jsonl"
    sub.write_text(VALID_JSONL_LINE, encoding="utf-8")

    cache = ParserCache()
    parse_directory(tmp_path, cache=cache)

    # Act
    sub.write_text(VALID_JSONL_LINE, encoding="utf-8")
    os.utime(sub, (0, 0))
    with patch(
        "app.sources.claude_jsonl.build_agent_index", return_value={}
    ) as spy:
        parse_directory(tmp_path, cache=cache)

    # Assert
    spy.assert_called_once()


def test_mtime_cache_returns_same_records_on_second_call(tmp_path: Path):
    jsonl_file = tmp_path / "session.jsonl"
    jsonl_file.write_text(VALID_JSONL_LINE, encoding="utf-8")

    cache = ParserCache()
    recs1, warn1 = parse_directory(tmp_path, cache=cache)
    assert len(recs1) == 1

    import builtins
    original_open = builtins.open
    open_count = 0

    def spy_open(*args, **kwargs):
        nonlocal open_count
        open_count += 1
        return original_open(*args, **kwargs)

    with patch("builtins.open", side_effect=spy_open):
        recs2, warn2 = parse_directory(tmp_path, cache=cache)

    assert recs1 == recs2
    assert open_count == 0


def test_parse_directory_only_reads_jsonl_files(tmp_path: Path):
    valid_file = tmp_path / "valid.jsonl"
    txt_file = tmp_path / "readme.txt"
    log_file = tmp_path / "app.log"

    valid_file.write_text(VALID_JSONL_LINE, encoding="utf-8")
    txt_file.write_text("some text\n", encoding="utf-8")
    log_file.write_text("some log\n", encoding="utf-8")

    records, warnings = parse_directory(tmp_path)

    assert len(records) == 1
    assert records[0].source_file == "valid.jsonl"


def test_parse_directory_handles_file_read_error_warning(tmp_path: Path):
    bad_file = tmp_path / "permission_denied.jsonl"
    bad_file.write_text("{}", encoding="utf-8")

    with patch("builtins.open", side_effect=PermissionError("Permission denied")):
        records, warnings = parse_directory(tmp_path)

    assert records == []
    assert len(warnings) == 1
    assert "permission_denied.jsonl" in warnings[0]


def test_parse_directory_handles_stat_error_warning(tmp_path: Path):
    bad_file = tmp_path / "stat_error.jsonl"
    bad_file.write_text("{}", encoding="utf-8")

    def mock_stat(path_obj, **kwargs):
        if path_obj.name == "stat_error.jsonl":
            raise OSError("Stat failed")
        return os_stat_orig(path_obj, **kwargs)

    os_stat_orig = Path.stat
    with patch.object(Path, "stat", autospec=True, side_effect=mock_stat):
        records, warnings = parse_directory(tmp_path)

    assert records == []
    assert len(warnings) == 1
    assert "stat_error.jsonl" in warnings[0]


@pytest.mark.parametrize(
    "missing_field",
    ["model", "sessionId", "cwd", "usage", "timestamp"],
)
def test_parse_directory_warns_on_missing_required_field(
    tmp_path: Path, missing_field: str
):
    base_data = {
        "type": "assistant",
        "sessionId": "sess-123",
        "cwd": "/anon/proj1",
        "timestamp": "2026-07-24T02:17:08.784Z",
        "message": {
            "model": "claude-opus-4-7",
            "usage": {"input_tokens": 10},
        },
    }
    if missing_field == "model":
        del base_data["message"]["model"]
    elif missing_field == "usage":
        del base_data["message"]["usage"]
    else:
        del base_data[missing_field]
    bad_file = tmp_path / f"missing_{missing_field}.jsonl"
    bad_file.write_text(json.dumps(base_data) + "\n", encoding="utf-8")

    records, warnings = parse_directory(tmp_path)

    assert records == []
    assert len(warnings) == 1
    assert missing_field in warnings[0]


def test_parse_directory_warns_on_usage_not_dict(tmp_path: Path):
    bad_data = {
        "type": "assistant",
        "sessionId": "sess-123",
        "cwd": "/anon/proj1",
        "timestamp": "2026-07-24T02:17:08.784Z",
        "message": {
            "model": "claude-opus-4-7",
            "usage": "not-a-dict",
        },
    }
    bad_file = tmp_path / "invalid_usage_type.jsonl"
    bad_file.write_text(json.dumps(bad_data) + "\n", encoding="utf-8")

    records, warnings = parse_directory(tmp_path)

    assert records == []
    assert len(warnings) == 1
    assert "'usage' is not a dict" in warnings[0]


def test_parse_directory_finds_jsonl_in_nested_project_dirs(tmp_path: Path):
    nested = tmp_path / "-home-dev-myproj"
    nested.mkdir()
    (nested / "session-a.jsonl").write_text(VALID_JSONL_LINE, encoding="utf-8")

    records, warnings = parse_directory(tmp_path)

    assert len(records) == 1
    assert records[0].model == "claude-opus-4-7"
    assert records[0].input_tokens == 10


def test_parse_directory_reads_model_and_usage_from_message_object(tmp_path: Path):
    (tmp_path / "s.jsonl").write_text(VALID_JSONL_LINE, encoding="utf-8")

    records, _ = parse_directory(tmp_path)

    assert records[0].output_tokens == 20
    assert records[0].cache_read_tokens == 5
    assert records[0].cache_write_tokens == 0


def test_parse_directory_silently_skips_non_assistant_rows(tmp_path: Path):
    noise = json.dumps({"type": "queue-operation", "sessionId": "s1"}) + "\n"
    (tmp_path / "s.jsonl").write_text(VALID_JSONL_LINE + noise, encoding="utf-8")

    records, warnings = parse_directory(tmp_path)

    assert len(records) == 1
    assert warnings == []


def test_parse_directory_silently_skips_synthetic_model(tmp_path: Path):
    synthetic = json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-07-24T02:17:08.784Z",
            "cwd": "/anon/p",
            "sessionId": "s2",
            "message": {
                "model": "<synthetic>",
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }
    ) + "\n"
    (tmp_path / "s.jsonl").write_text(VALID_JSONL_LINE + synthetic, encoding="utf-8")

    records, warnings = parse_directory(tmp_path)

    assert len(records) == 1
    assert warnings == []


def test_parse_directory_warns_when_type_field_absent_and_fields_missing(
    tmp_path: Path,
):
    drifted = json.dumps({"sessionId": "s3", "cwd": "/anon/p"}) + "\n"
    (tmp_path / "s.jsonl").write_text(drifted, encoding="utf-8")

    records, warnings = parse_directory(tmp_path)

    assert records == []
    assert len(warnings) == 1
    assert "Missing required field" in warnings[0]


def test_parse_directory_labels_subagent_records_with_agent_type(tmp_path: Path):
    sess = tmp_path / "-home-dev-proj" / "sess"
    sub_dir = sess / "subagents"
    sub_dir.mkdir(parents=True)

    prompt = "Audit the pricing module."
    main = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Agent",
                        "input": {"prompt": prompt, "subagent_type": "python-reviewer"},
                    }
                ],
            },
        }
    ) + "\n"
    (sess.parent / "main.jsonl").write_text(main + VALID_JSONL_LINE, encoding="utf-8")

    sub_first = json.dumps(
        {
            "type": "user",
            "isSidechain": True,
            "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
        }
    ) + "\n"
    (sub_dir / "agent-x.jsonl").write_text(sub_first + VALID_JSONL_LINE, encoding="utf-8")

    records, _ = parse_directory(tmp_path)

    agents = {r.agent for r in records}
    assert "python-reviewer" in agents
    assert None in agents  # 메인 체인은 직접 세션으로 남는다


def test_parse_directory_aggregates_subagent_fallbacks_into_one_warning(tmp_path: Path):
    """폴백은 파일별이 아니라 집계 1줄로 보고한다."""
    sub_dir = tmp_path / "proj" / "sess" / "subagents"
    sub_dir.mkdir(parents=True)
    unindexed = json.dumps(
        {
            "type": "user",
            "isSidechain": True,
            "message": {"role": "user", "content": [{"type": "text", "text": "unindexed"}]},
        }
    ) + "\n"
    for name in ("agent-1.jsonl", "agent-2.jsonl", "agent-3.jsonl"):
        (sub_dir / name).write_text(unindexed + VALID_JSONL_LINE, encoding="utf-8")

    records, warnings = parse_directory(tmp_path)

    fallback_warnings = [w for w in warnings if "복원하지 못한" in w]
    assert len(fallback_warnings) == 1
    assert "3개" in fallback_warnings[0]
    assert all(r.agent == "claude-subagent" for r in records)


def test_subagent_record_carries_cwd_and_parent_session_id(tmp_path: Path) -> None:
    session_dir = tmp_path / "proj" / "sess-parent"
    sub_dir = session_dir / "subagents"
    sub_dir.mkdir(parents=True)

    (tmp_path / "proj" / "sess-parent.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "sess-parent",
                "cwd": "/anon/deep/proj",
                "timestamp": "2026-07-21T10:00:00Z",
                "message": {"model": "claude-opus-4", "usage": {"input_tokens": 1}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (sub_dir / "kid.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "sess-parent",
                "cwd": "/anon/deep/proj",
                "timestamp": "2026-07-21T10:10:00Z",
                "message": {"model": "claude-sonnet-4", "usage": {"input_tokens": 2}},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    records, _warnings = parse_directory(tmp_path)
    by_file = {r.source_file: r for r in records}

    root = by_file["proj/sess-parent.jsonl"]
    kid = by_file[str(Path("proj/sess-parent/subagents/kid.jsonl"))]

    assert root.cwd == "/anon/deep/proj"
    assert root.project == "proj"
    assert root.parent_session_id is None
    assert kid.cwd == "/anon/deep/proj"
    assert kid.parent_session_id == "sess-parent"


def test_dispatcher_file_is_relative_and_can_be_a_subagent(fixtures_dir: Path) -> None:
    records, _warnings = parse_directory(fixtures_dir / "claude_projects")
    by_file = {r.source_file: r for r in records}

    root_rel = str(Path("proj_flow01/root-sess-0001.jsonl"))
    a_rel = str(Path("proj_flow01/root-sess-0001/subagents/child-a.jsonl"))
    c_rel = str(Path("proj_flow01/root-sess-0001/subagents/child-c.jsonl"))

    assert by_file[a_rel].agent == "python-reviewer"
    assert by_file[a_rel].dispatcher_file == root_rel
    # child-c는 루트가 아니라 child-a가 디스패치했다 — 유일한 2-hop 신호.
    assert by_file[c_rel].agent == "silent-failure-hunter"
    assert by_file[c_rel].dispatcher_file == a_rel
    assert by_file[root_rel].dispatcher_file is None


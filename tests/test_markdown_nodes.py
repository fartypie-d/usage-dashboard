"""Tests for app.markdown_nodes — 마크다운 → 화이트리스트 노드 트리."""

from __future__ import annotations

from collections.abc import Iterator

from app.markdown_nodes import markdown_to_nodes


def _types(nodes: list[dict]) -> list[str]:
    return [n["t"] for n in nodes]


def _walk(nodes: list[dict]) -> Iterator[dict]:
    for n in nodes:
        yield n
        yield from _walk(n.get("c") or [])


def test_heading_carries_level_and_text() -> None:
    nodes = markdown_to_nodes("## Task 3: 더 보기")
    assert nodes[0]["t"] == "heading"
    assert nodes[0]["level"] == 2
    assert nodes[0]["c"][0] == {"t": "text", "text": "Task 3: 더 보기"}


def test_heading_level_is_clamped_to_four() -> None:
    nodes = markdown_to_nodes("##### 다섯 단계")
    assert nodes[0]["level"] == 4


def test_fence_keeps_language_and_body() -> None:
    nodes = markdown_to_nodes("```python\nx = 1\n```")
    assert nodes[0] == {"t": "code", "lang": "python", "text": "x = 1\n"}


def test_table_rows_are_lifted_out_of_thead_and_tbody() -> None:
    src = "| 항목 | 결과 |\n|---|---|\n| 크기 | 588,965 B |"
    table = markdown_to_nodes(src)[0]
    assert table["t"] == "table"
    assert _types(table["c"]) == ["row", "row"]
    header_cells = table["c"][0]["c"]
    assert header_cells[0]["header"] is True
    assert header_cells[0]["c"][0]["text"] == "항목"
    assert table["c"][1]["c"][1]["header"] is False


def test_nested_list_keeps_structure_and_order_flag() -> None:
    nodes = markdown_to_nodes("1. 바깥\n   - 안쪽")
    outer = nodes[0]
    assert outer["t"] == "list" and outer["ordered"] is True
    inner = [n for n in outer["c"][0]["c"] if n["t"] == "list"][0]
    assert inner["ordered"] is False


def test_inline_code_and_strong_become_nodes() -> None:
    para = markdown_to_nodes("`flows[]` 는 **중요**하다")[0]
    kinds = _types(para["c"])
    assert "codespan" in kinds
    assert "strong" in kinds


def test_raw_html_is_escaped_into_text_not_dropped() -> None:
    nodes = markdown_to_nodes("<script>alert(1)</script>")
    flat = "".join(n.get("text", "") for n in _walk(nodes) if n["t"] == "text")
    assert "<script>" in flat
    assert all(n["t"] != "html" for n in _walk(nodes))


def test_dangerous_scheme_never_produces_a_link_node() -> None:
    # markdown-it 자체가 javascript:·data: 를 링크로 만들지 않고 원문 텍스트로 남긴다.
    # 화이트리스트(_safe_href)는 그 위의 2차 방어선이다.
    for src in ("[클릭](javascript:alert)", "[클릭](data:text/html,x)"):
        nodes = markdown_to_nodes(src)
        assert all(n["t"] != "link" for n in _walk(nodes)), src
        flat = "".join(n.get("text", "") for n in _walk(nodes) if n["t"] == "text")
        assert "클릭" in flat, src


def test_non_whitelisted_scheme_is_stripped_to_plain() -> None:
    # mailto:·ftp: 는 markdown-it이 허용하지만 화이트리스트에 없다 → 링크를 벗긴다.
    for src in ("[메일](mailto:a@b.c)", "[FTP](ftp://h/f)"):
        para = markdown_to_nodes(src)[0]
        assert _types(para["c"]) == ["plain"], src
        assert para["c"][0]["c"][0]["t"] == "text", src


def test_safe_link_keeps_href() -> None:
    para = markdown_to_nodes("[문서](https://example.com/a)")[0]
    link = para["c"][0]
    assert link["t"] == "link"
    assert link["href"] == "https://example.com/a"


def test_anchor_and_relative_links_are_kept() -> None:
    for src, href in (("[앵커](#task-1)", "#task-1"), ("[상대](./docs/a.md)", "./docs/a.md")):
        link = markdown_to_nodes(src)[0]["c"][0]
        assert link["t"] == "link" and link["href"] == href, src


def test_empty_input_returns_empty_list() -> None:
    assert markdown_to_nodes("") == []


def test_image_leaves_a_visible_trace_instead_of_vanishing() -> None:
    """image 토큰은 content 가 alt 뿐이라 alt 가 비면 폴백에도 안 걸려 통째로 사라졌다."""
    for src, expected in (
        ("![](diagram.png)", "diagram.png"),
        ("![구조도](diagram.png)", "구조도"),
    ):
        flat = "".join(
            n.get("text", "") for n in _walk(markdown_to_nodes(src)) if n["t"] == "text"
        )
        assert expected in flat, src
        assert "이미지" in flat, src

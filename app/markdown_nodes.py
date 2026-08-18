"""마크다운 텍스트 → 화이트리스트 노드 트리 (JSON 직렬화 가능).

프런트가 ``createElement``로만 DOM을 만들 수 있도록, HTML 문자열이 아니라
노드 트리를 내려준다. ``html=False``라 raw HTML은 텍스트로 이스케이프되고,
화이트리스트 밖 토큰은 버리지 않고 텍스트로 평탄화한다 (내용 유실을 침묵으로
만들지 않기 위해서다).
"""

from __future__ import annotations

import re

from markdown_it import MarkdownIt

_MD = MarkdownIt("commonmark", {"html": False}).enable("table")

# 상대경로·앵커·http(s)만 허용한다. javascript:·data: 등은 링크를 벗긴다.
_SAFE_HREF_PREFIXES = ("http://", "https://", "#", "/", "./", "../")
_MAX_HEADING_LEVEL = 4
_ALIGN_RE = re.compile(r"text-align:\s*(left|center|right)")

_CLOSING_TYPES = frozenset({
    "heading_close", "paragraph_close", "blockquote_close",
    "bullet_list_close", "ordered_list_close", "list_item_close",
    "table_close", "tr_close", "th_close", "td_close",
})
_TRANSPARENT_TYPES = frozenset({
    "thead_open", "thead_close", "tbody_open", "tbody_close",
})


def _safe_href(href: str) -> str | None:
    h = (href or "").strip()
    return h if h.startswith(_SAFE_HREF_PREFIXES) else None


def _align(token) -> str | None:
    m = _ALIGN_RE.search(token.attrGet("style") or "")
    return m.group(1) if m else None


def markdown_to_nodes(text: str) -> list[dict]:
    """마크다운 → 노드 리스트. 지원하지 않는 토큰은 text 노드로 평탄화한다."""
    root: list[dict] = []
    stack: list[list[dict]] = [root]

    def push(node: dict) -> None:
        stack[-1].append(node)

    def open_node(node: dict) -> None:
        node["c"] = []
        push(node)
        stack.append(node["c"])

    def close_node() -> None:
        if len(stack) > 1:
            stack.pop()

    def inline(children) -> None:
        for ch in children or []:
            t = ch.type
            if t == "text":
                push({"t": "text", "text": ch.content})
            elif t == "code_inline":
                push({"t": "codespan", "text": ch.content})
            elif t == "strong_open":
                open_node({"t": "strong"})
            elif t == "em_open":
                open_node({"t": "em"})
            elif t in ("strong_close", "em_close", "link_close"):
                close_node()
            elif t == "link_open":
                href = _safe_href(ch.attrGet("href") or "")
                open_node({"t": "link", "href": href} if href else {"t": "plain"})
            elif t in ("softbreak", "hardbreak"):
                push({"t": "text", "text": "\n"})
            elif t == "image":
                # image 토큰은 content 에 alt 만 담고 src 는 attrs 에만 있다.
                # alt 가 비면 content 가 falsy 라 아래 폴백에도 안 걸려 통째로 사라진다.
                src = ch.attrGet("src") or ""
                alt = ch.content or ch.attrGet("alt") or ""
                push({"t": "text", "text": f"[이미지: {alt or src}]"})
            elif ch.content:
                push({"t": "text", "text": ch.content})

    for tok in _MD.parse(text or ""):
        t = tok.type
        if t == "heading_open":
            open_node({
                "t": "heading",
                "level": min(int(tok.tag[1:]), _MAX_HEADING_LEVEL),
            })
        elif t == "paragraph_open":
            open_node({"t": "paragraph"})
        elif t == "blockquote_open":
            open_node({"t": "blockquote"})
        elif t == "bullet_list_open":
            open_node({"t": "list", "ordered": False})
        elif t == "ordered_list_open":
            open_node({"t": "list", "ordered": True})
        elif t == "list_item_open":
            open_node({"t": "item"})
        elif t == "table_open":
            open_node({"t": "table"})
        elif t == "tr_open":
            open_node({"t": "row"})
        elif t in ("th_open", "td_open"):
            open_node({"t": "cell", "header": t == "th_open", "align": _align(tok)})
        elif t in _CLOSING_TYPES:
            close_node()
        elif t in _TRANSPARENT_TYPES:
            continue  # 투명 컨테이너 — 행을 표 바로 아래로 올린다
        elif t == "hr":
            push({"t": "hr"})
        elif t == "fence":
            push({"t": "code", "lang": (tok.info or "").strip(), "text": tok.content})
        elif t == "code_block":
            push({"t": "code", "lang": "", "text": tok.content})
        elif t == "inline":
            inline(tok.children)
        elif tok.content:
            push({"t": "text", "text": tok.content})

    return root

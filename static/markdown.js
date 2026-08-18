/**
 * markdown.js — 노드 트리(/api/progress/phase/{n})를 DOM으로 조립한다.
 *
 * innerHTML을 쓰지 않는다. 서버가 내려주는 노드는 화이트리스트를 통과했지만,
 * 이 렌더러가 문자열을 파싱하지 않는 한 XSS 경로 자체가 생기지 않는다.
 * (나중에 세션 발췌 같은 신뢰할 수 없는 텍스트에 재사용할 때를 위해서다.)
 */
"use strict";

function mdEl(tag, attrs, children) {
  const e = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (k === "style" && typeof v === "object") Object.assign(e.style, v);
    else if (k === "className") e.className = v;
    else if (k === "textContent") e.textContent = v;
    else e.setAttribute(k, v);
  }
  if (children) for (const c of children) if (c) e.appendChild(c);
  return e;
}

// 서버 화이트리스트와 같은 규칙. 방어선을 서버에만 두면, 이 렌더러를 다른
// 입력원에 붙이는 순간(예: 세션 발췌) 조용히 뚫린다.
const MD_SAFE_HREF = /^(https?:\/\/|#|\/|\.\/|\.\.\/)/;
const MD_MAX_DEPTH = 40;

function mdSafeHref(href) {
  const h = String(href || "").trim();
  return MD_SAFE_HREF.test(h) ? h : null;
}

function mdChildren(node, depth) {
  if (!Array.isArray(node.c)) return [];
  return node.c.map((c) => renderNode(c, depth + 1)).filter(Boolean);
}

function renderNode(node, depth) {
  if (!node || typeof node !== "object") return null;
  // 깊이 상한 — 순환 참조·비정상 중첩이 스택을 태우지 않게 한다.
  if (depth > MD_MAX_DEPTH) {
    return document.createTextNode(node.text || "…");
  }
  switch (node.t) {
    case "text":
      return document.createTextNode(node.text || "");
    case "plain": {
      // 화이트리스트 밖 스킴이라 링크를 벗긴 자리 — 자식만 펼친다.
      const frag = document.createDocumentFragment();
      for (const c of mdChildren(node, depth)) frag.appendChild(c);
      return frag;
    }
    case "heading":
      return mdEl("h" + (node.level || 2), { className: "md-h" }, mdChildren(node, depth));
    case "paragraph":
      return mdEl("p", { className: "md-p" }, mdChildren(node, depth));
    case "blockquote":
      return mdEl("blockquote", { className: "md-quote" }, mdChildren(node, depth));
    case "list":
      return mdEl(node.ordered ? "ol" : "ul", { className: "md-list" }, mdChildren(node, depth));
    case "item":
      return mdEl("li", null, mdChildren(node, depth));
    case "table":
      // 넓은 표가 페이지를 밀지 않도록 자기 스크롤 컨테이너에 가둔다.
      return mdEl("div", { className: "md-table-wrap" }, [
        mdEl("table", { className: "md-table" }, mdChildren(node, depth)),
      ]);
    case "row":
      return mdEl("tr", null, mdChildren(node, depth));
    case "cell":
      return mdEl(
        node.header ? "th" : "td",
        node.align ? { style: { textAlign: node.align } } : null,
        mdChildren(node, depth),
      );
    case "code":
      return mdEl("pre", { className: "md-pre" }, [
        mdEl("code", { className: "mono", textContent: node.text || "" }),
      ]);
    case "codespan":
      return mdEl("code", { className: "md-code mono", textContent: node.text || "" });
    case "strong":
      return mdEl("strong", null, mdChildren(node, depth));
    case "em":
      return mdEl("em", null, mdChildren(node, depth));
    case "link": {
      const href = mdSafeHref(node.href);
      // 서버가 이미 걸렀지만 여기서도 막는다 — 벗겨야 하면 링크 없이 자식만 남긴다.
      if (!href) {
        const frag = document.createDocumentFragment();
        for (const c of mdChildren(node, depth)) frag.appendChild(c);
        return frag;
      }
      return mdEl("a", {
        className: "md-link",
        href: href,
        rel: "noopener noreferrer",
        target: "_blank",
      }, mdChildren(node, depth));
    }
    case "hr":
      return mdEl("hr", { className: "md-hr" });
    default:
      // 모르는 노드를 조용히 버리지 않는다 — 텍스트라도 남긴다.
      return document.createTextNode(node.text || "");
  }
}

function renderMarkdown(nodes) {
  const frag = document.createDocumentFragment();
  if (!Array.isArray(nodes)) return frag;
  for (const n of nodes) {
    const el = renderNode(n, 0);
    if (el) frag.appendChild(el);
  }
  return frag;
}

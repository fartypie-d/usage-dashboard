/**
 * sessions.js — 세션 상세·diff (별도 페이지) 렌더러.
 *
 * /api/work/session/{source}/{id} 상세를 그린다.
 * 트랜스크립트 원문은 전부 textContent로만 삽입한다 — HTML 문자열 삽입 금지 (스펙 계약,
 * test_static_mount가 이 파일에 금지 API 문자열이 없음을 단언한다).
 * URL 해시 #/session/{source}/{id} 로 상세 상태를 보존한다 (새로고침·공유 가능).
 */
"use strict";

/* ── helpers (support.js와 의도적 중복 — 그쪽은 폴링·Chart.js 부팅이 딸려온다) ── */

function $(id) { return document.getElementById(id); }

function el(tag, attrs, children) {
  const e = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (k === "style" && typeof v === "object") Object.assign(e.style, v);
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "className") e.className = v;
    else if (k === "textContent") e.textContent = v;
    else e.setAttribute(k, v);
  }
  if (children) {
    const arr = Array.isArray(children) ? children : [children];
    for (const c of arr) {
      if (c == null || c === false) continue;
      e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
  }
  return e;
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

function money(v) {
  if (v == null || Number.isNaN(v)) return "—";
  return "$" + Number(v).toFixed(2);
}

function fmtTime(ms) {
  if (ms == null || Number.isNaN(Number(ms))) return "—";
  const d = new Date(Number(ms));
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("ko-KR", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

/* ── state ──────────────────────────────────────────────────────── */

const state = {
  theme: "dark",
  detail: null,        // /api/work/session/... 응답
  tab: "files",        // "files" | "turns"
  openFile: null,      // 펼쳐진 파일 인덱스
  detailKey: null,     // "{source}/{encoded id}" — 같으면 재요청하지 않는다
  pendingTurn: null,   // 대화 탭 렌더 후 스크롤할 턴 인덱스
};

/* ── fetch ──────────────────────────────────────────────────────── */

async function fetchJSON(url) {
  try {
    const r = await fetch(url, { signal: AbortSignal.timeout(15000) });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return await r.json();
  } catch (err) {
    if (err.name === "TimeoutError" || err.name === "AbortError") {
      throw new Error("요청 시간 초과 (15초): " + url);
    }
    throw err;
  }
}

/* ── routing (#/session/{source}/{id}) ──────────────────────────── */

function parseHash() {
  const h = location.hash || "";
  const m = h.match(/^#\/session\/(claude|opencode)\/(.+?)(?:\/(turns|files))?$/);
  if (m) {
    return { view: "detail", source: m[1], id: decodeURIComponent(m[2]), tab: m[3] || "files" };
  }
  return { view: "index" };
}

function onHashChange() {
  const route = parseHash();
  if (route.view === "detail") {
    state.tab = route.tab;
    const key = route.source + "/" + encodeURIComponent(route.id);
    if (state.detailKey === key && state.detail) {
      renderDetail(state.detail);   // 탭 전환만 — 재요청하지 않는다
      return;
    }
    state.detailKey = key;
    state.openFile = null;
    loadDetail(route.source, route.id);
    return;
  }
  state.detailKey = null;
  state.detail = null;
  renderIndexRedirect();
}

function renderIndexRedirect() {
  const rootEl = $("list-view");
  clear(rootEl);
  $("detail-view").classList.add("hidden");
  rootEl.classList.remove("hidden");
  rootEl.appendChild(el("div", {
    style: { padding: "40px", textAlign: "center", color: "var(--muted)" },
  }, [
    el("div", { textContent: "세션 목록은 작업 흐름 페이지로 이동했습니다." }),
    el("a", { className: "btn", href: "/static/flow.html",
              style: { marginTop: "12px", display: "inline-flex" },
              textContent: "작업 흐름 열기 →" }),
  ]));
}

/* ── detail view ────────────────────────────────────────────────── */

async function loadDetail(source, id) {
  $("list-view").classList.add("hidden");
  const container = $("detail-view");
  container.classList.remove("hidden");
  clear(container);
  container.appendChild(el("div", { className: "mono", style: { color: "var(--muted)", fontSize: "12.5px" },
    textContent: "세션 불러오는 중… (" + source + "/" + id + ")" }));
  try {
    state.detail = await fetchJSON(
      "/api/work/session/" + encodeURIComponent(source) + "/" + encodeURIComponent(id)
    );
    renderDetail(state.detail);
  } catch (err) {
    clear(container);
    container.appendChild(el("div", { className: "panel", style: { borderColor: "var(--danger)" } }, [
      el("div", { style: { color: "var(--danger)", fontWeight: "600", marginBottom: "6px" },
        textContent: "세션을 불러오지 못했습니다" }),
      el("div", { style: { fontSize: "12.5px", color: "var(--text-2)" }, textContent: err.message }),
      el("button", { className: "btn", style: { marginTop: "12px" },
         onClick: function () { location.href = "/static/flow.html"; }, textContent: "← 작업 흐름" }),
    ]));
  }
}

function truncatedBadge(flag) {
  if (!flag) return null;
  return el("span", { className: "badge", style: { color: "var(--warn)", borderColor: "var(--warn)" },
    textContent: "잘림" });
}

function turnBlock(turn, index) {
  const rows = [];
  rows.push(el("div", { style: { display: "flex", justifyContent: "space-between", gap: "10px" } }, [
    el("span", { className: "turn-label", textContent: "턴 " + (index + 1) }),
    el("span", { className: "turn-label", textContent: fmtTime(turn.ts) }),
  ]));

  if (turn.instruction == null) {
    rows.push(el("div", { className: "excerpt", style: { color: "var(--muted)", fontStyle: "italic" },
      textContent: "👤 (이전 세션에서 이어짐)" }));
  } else {
    rows.push(el("div", {}, [
      el("div", { className: "turn-label", textContent: "👤 지시" }),
      el("div", { className: "excerpt", style: { fontWeight: "600" }, textContent: turn.instruction }),
      truncatedBadge(turn.instruction_truncated),
    ]));
  }

  const reasoning = Array.isArray(turn.reasoning) ? turn.reasoning : [];
  if (reasoning.length > 0) {
    const body = el("div", { style: { display: "grid", gap: "8px", marginTop: "8px" } },
      reasoning.map(function (r) {
        return el("div", { className: "excerpt", style: { color: "var(--text-2)", background: "var(--panel-2)",
          borderRadius: "8px", padding: "8px 10px" }, textContent: r });
      }));
    const summaryChildren = ["🧠 추론 " + reasoning.length + "개"];
    if (turn.reasoning_truncated) summaryChildren.push(" (일부 잘림)");
    rows.push(el("details", {}, [el("summary", { textContent: summaryChildren.join("") }), body]));
  }

  const actions = Array.isArray(turn.actions) ? turn.actions : [];
  if (actions.length > 0) {
    rows.push(el("div", {}, [
      el("div", { className: "turn-label", textContent: "🤖 작업 " + actions.length + "건" }),
      el("div", { style: { display: "flex", flexWrap: "wrap", gap: "6px", marginTop: "5px" } },
        actions.map(function (a) {
          const label = (a.tool || "(unknown)") + (a.target ? " · " + a.target : "");
          if (typeof a.file_index === "number") {
            return el("button", {
              className: "badge mono",
              style: { cursor: "pointer" },
              textContent: label + " ↗",
              onClick: function () { jumpToFile(a.file_index); },
            });
          }
          return el("span", { className: "badge mono", textContent: label });
        })),
      truncatedBadge(turn.actions_truncated),
    ]));
  }

  if (turn.response) {
    rows.push(el("div", {}, [
      el("div", { className: "turn-label", textContent: "응답" }),
      el("div", { className: "excerpt", style: { color: "var(--text-2)" }, textContent: turn.response }),
      truncatedBadge(turn.response_truncated),
    ]));
  }
  return el("div", { className: "turn", id: "turn-" + index }, rows);
}

function detailHashBase() {
  const route = parseHash();
  if (route.view !== "detail") return "";
  return "#/session/" + route.source + "/" + encodeURIComponent(route.id);
}

function setTab(tab) {
  const base = detailHashBase();
  if (!base) return;
  location.hash = tab === "turns" ? base + "/turns" : base;
}

function detailTabBar(fileCount, turnCount) {
  const pairs = [["files", "변경 파일 " + fileCount], ["turns", "대화 " + turnCount]];
  return el("div", { style: { display: "flex", gap: "8px", margin: "4px 0 6px" } },
    pairs.map(function (pair) {
      const active = state.tab === pair[0];
      return el("button", {
        className: "btn",
        role: "tab",
        "aria-selected": String(active),
        style: active ? { background: "var(--accent)", color: "#fff", borderColor: "var(--accent)" } : {},
        textContent: pair[1],
        onClick: function () { setTab(pair[0]); },
      });
    }));
}

function toggleFile(index) {
  state.openFile = state.openFile === index ? null : index;
  renderDetail(state.detail);
}

function jumpToTurn(turnIndex) {
  state.pendingTurn = turnIndex;
  setTab("turns");
}

function jumpToFile(fileIndex) {
  state.openFile = fileIndex;
  setTab("files");
}

function scrollToPendingTurn() {
  if (state.pendingTurn == null) return;
  const target = $("turn-" + state.pendingTurn);
  state.pendingTurn = null;
  if (target && target.scrollIntoView) target.scrollIntoView({ block: "start" });
}

function renderDetail(data) {
  const container = $("detail-view");
  clear(container);
  if (!data || !data.session) {
    container.appendChild(el("div", { className: "panel", textContent: "세션 데이터가 비어 있습니다." }));
    return;
  }
  const s = data.session;
  container.appendChild(el("div", { style: { display: "flex", alignItems: "center", gap: "12px", flexWrap: "wrap" } }, [
    el("button", { className: "btn", onClick: function () { location.href = "/static/flow.html"; }, textContent: "← 작업 흐름" }),
    el("span", { className: "display", style: { fontWeight: "700", fontSize: "17px" },
      textContent: s.title || "(제목 없음)" }),
    el("span", { className: "badge", textContent: s.source || "?" }),
    el("span", { className: "mono", style: { fontSize: "12px", color: "var(--muted)" },
      textContent: (s.project || "unknown") + " · " + fmtTime(s.started_at) + " · " + money(s.cost_usd) }),
  ]));

  const warnings = Array.isArray(data.warnings) ? data.warnings : [];
  if (warnings.length > 0) {
    container.appendChild(el("div", { className: "panel", style: { borderColor: "var(--warn)", background: "var(--warn-bg)",
      fontSize: "12.5px" }, textContent: "⚠ " + warnings.join(" · ") }));
  }

  const turns = Array.isArray(data.turns) ? data.turns : [];
  const files = Array.isArray(data.files) ? data.files : [];
  container.appendChild(detailTabBar(files.length, turns.length));

  if (state.tab === "files") {
    container.appendChild(renderFilesPanel(data, state.openFile, toggleFile, jumpToTurn));
    return;
  }

  if (turns.length === 0) {
    container.appendChild(el("div", { className: "panel", style: { color: "var(--muted)" },
      textContent: "이 세션에는 표시할 턴이 없습니다." }));
    return;
  }
  turns.forEach(function (turn, i) { container.appendChild(turnBlock(turn, i)); });
  scrollToPendingTurn();
}

/* ── boot ───────────────────────────────────────────────────────── */

function boot() {
  $("btn-theme").addEventListener("click", function () {
    state.theme = state.theme === "dark" ? "light" : "dark";
    $("app").setAttribute("data-theme", state.theme);
    $("btn-theme").textContent = state.theme === "dark" ? "☾ Dark" : "☀ Light";
  });
  window.addEventListener("hashchange", onHashChange);
  onHashChange();
}

boot();

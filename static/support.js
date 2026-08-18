/**
 * support.js — Vanilla JS dashboard wiring for usage-dashboard.
 *
 * Fetches from /api/summary, /api/delegation, /api/sessions and renders
 * the 5 dashboard sections using DOM APIs (no framework, no dc-runtime).
 *
 * Chart library: Chart.js v4 (vendored at /static/vendor/chart.min.js).
 * Sparklines use inline SVG to stay lightweight.
 */
"use strict";

/* ── helpers ─────────────────────────────────────────────────────── */

function $(id) { return document.getElementById(id); }

function el(tag, attrs, children) {
  const e = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (k === "style" && typeof v === "object") Object.assign(e.style, v);
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "className") e.className = v;
    else if (k === "textContent") e.textContent = v;
    else if (k === "innerHTML") e.innerHTML = v;
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
function tok(v) {
  if (v == null || Number.isNaN(v)) return "—";
  var n = Number(v);
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return Math.round(n / 1e3) + "k";
  return "" + n;
}
function pct(v) {
  if (v == null || Number.isNaN(v)) return "—";
  return Math.round(Number(v) * 100) + "%";
}

/* ── model color map ────────────────────────────────────────────── */

const MODEL_COLORS = {};
const MODEL_PALETTE = [
  "#c15f3c", "#6a8caf", "#7c9070", "#d9a441", "#a89984",
  "#8e7cc3", "#c27ba0", "#76a5af", "#e69138", "#6d9eeb",
];
let _colorIdx = 0;
function modelColor(model) {
  if (!model) return "var(--muted)";
  if (MODEL_COLORS[model]) return MODEL_COLORS[model];
  MODEL_COLORS[model] = MODEL_PALETTE[_colorIdx % MODEL_PALETTE.length];
  _colorIdx++;
  return MODEL_COLORS[model];
}
function modelTier(model) {
  if (!model) return "mid";
  const m = model.toLowerCase();
  if (m.includes("opus")) return "high";
  if (m.includes("haiku") || m.includes("gpt-4o-mini")) return "low";
  return "mid";
}

/* ── state ──────────────────────────────────────────────────────── */

const state = {
  range: "24h",
  source: "all",
  theme: "dark",
  mixSort: "cost",
  mixExpanded: false,
  rankRowsOpen: new Set(),   // 펼쳐진 모델 행 (모델명 보관)
  rankListExpanded: false,   // 목록 전체를 Top N 너머까지 펼쳤는가
  flowOpen: new Set(),       // 펼쳐진 위임 흐름 (node_id 보관)
  flowListExpanded: false,   // 흐름 목록을 Top N 너머까지 펼쳤는가
  cacheRatioExpanded: false, // 프로젝트×모델 캐시 read 비율 목록
  lowCacheExpanded: false,   // 캐시 효율 하위 세션 표
  agentsExpanded: false,     // agent별 집계 표
  sessionsExpanded: false,   // 세션 건강도 카드 그리드
  flowListError: null,       // 더 보기 재요청 실패 메시지 (텍스트로 노출)
  loading: true,
  error: false,
  empty: false,
  summary: null,
  delegation: null,
  sessions: null,
  lastUpdated: null,
  refreshing: false,
  warningsDismissed: false,
  consecutiveErrors: 0,
};

let _pollTimer = null;
let _costChart = null;
let _flowChart = null;
let _errorBadge = null;

/* ── theme ──────────────────────────────────────────────────────── */

function initTheme() {
  try {
    if (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) {
      state.theme = "light";
    }
  } catch (_) { /* ignore */ }
  finally { applyTheme(); }
}
function applyTheme() {
  const app = $("app");
  if (app) app.setAttribute("data-theme", state.theme);
  const glyph = $("theme-glyph");
  const label = $("theme-label");
  if (glyph) glyph.textContent = state.theme === "dark" ? "☾" : "☀";
  if (label) label.textContent = state.theme === "dark" ? "Dark" : "Light";
  // Rebuild charts with new colors if data loaded
  if (state.summary) renderCostChart(state.summary.daily_cost || []);
  if (state.delegation) renderFlowChart(state.delegation.flow || []);
}
function toggleTheme() {
  state.theme = state.theme === "dark" ? "light" : "dark";
  applyTheme();
}

/* ── range selector / source toggle / adaptive polling ─────────── */

const RANGE_PRESETS = [
  ["15m", "라이브"], ["1h", "1시간"], ["24h", "24시간"],
  ["7d", "7일"], ["30d", "30일"], ["all", "전체"],
];
// 라이브성 창은 더 자주 갱신한다.
const FAST_RANGES = new Set(["15m", "1h"]);
const FAST_POLL_MS = 15000;
const SLOW_POLL_MS = 60000;

const SOURCE_PRESETS = [["all", "전체"], ["claude", "Claude"], ["opencode", "opencode"]];

// 필터는 페이지를 떠났다 돌아와도 유지한다 — progress.js의 "ud-theme"과 같은 저장소.
const RANGE_STORAGE_KEY = "ud-range";
const SOURCE_STORAGE_KEY = "ud-source";

function restoreFilters() {
  try {
    const r = localStorage.getItem(RANGE_STORAGE_KEY);
    if (RANGE_PRESETS.some(function (p) { return p[0] === r; })) state.range = r;
    const s = localStorage.getItem(SOURCE_STORAGE_KEY);
    if (SOURCE_PRESETS.some(function (p) { return p[0] === s; })) state.source = s;
  } catch (_) { /* 저장소 접근 불가 — 기본값 유지 */ }
}

function saveFilter(key, value) {
  try { localStorage.setItem(key, value); } catch (_) { /* 저장 실패는 무시 */ }
}

function buildToggleGroup(containerId, presets, current, onPick) {
  const group = $(containerId);
  clear(group);
  for (const [val, label] of presets) {
    group.appendChild(el("button", {
      role: "tab",
      className: "btn" + (current === val ? " btn-accent" : ""),
      textContent: label,
      "aria-selected": current === val ? "true" : "false",
      onClick: () => onPick(val),
    }));
  }
}

function buildRangeSelector() {
  buildToggleGroup("range-group", RANGE_PRESETS, state.range, setRange);
}

function buildSourceSelector() {
  buildToggleGroup("source-group", SOURCE_PRESETS, state.source, setSource);
}

function setRange(r) {
  if (r === state.range) return;
  state.range = r;
  saveFilter(RANGE_STORAGE_KEY, r);
  buildRangeSelector();
  restartPolling();
  refreshAll(true);
}

function setSource(s) {
  if (s === state.source) return;
  state.source = s;
  saveFilter(SOURCE_STORAGE_KEY, s);
  buildSourceSelector();
  refreshAll(true);
}

function pollInterval() {
  return FAST_RANGES.has(state.range) ? FAST_POLL_MS : SLOW_POLL_MS;
}

function restartPolling() {
  if (_pollTimer !== null) clearInterval(_pollTimer);
  _pollTimer = setInterval(function () { refreshAll(false); }, pollInterval());
}

/* ── fetch ──────────────────────────────────────────────────────── */

async function fetchJSON(url) {
  try {
    const r = await fetch(url, { signal: AbortSignal.timeout(15000) });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return await r.json();
  } catch (err) {
    // AbortSignal.timeout이 만든 AbortError/TimeoutError는 사용자 메시지로 변환
    if (err.name === "TimeoutError" || err.name === "AbortError") {
      throw new Error("요청 시간 초과 (15초): " + url);
    }
    throw err;
  }
}

async function refreshAll(initial) {
  if (initial) {
    state.loading = true;
    state.error = false;
    state.empty = false;
    state.warningsDismissed = false;
    // 기간·소스 변경/수동 새로고침 시 확장 상태 초기화 — 기본 상한(20)을 다시 받는다.
    state.flowListExpanded = false;
    state.flowListError = null;
    showState("loading");
  } else {
    state.refreshing = true;
    $("refresh-icon").style.animation = "spin .8s linear infinite";
  }
  try {
    const qs = "?range=" + encodeURIComponent(state.range) +
               "&source=" + encodeURIComponent(state.source);
    // 펼친 채 폴링하면 기본 상한(20)으로 데이터가 잘리지 않게 limit=1000을 유지한다.
    const delQs = state.flowListExpanded ? qs + "&limit=1000" : qs;
    const [summary, delegation, sessions] = await Promise.all([
      fetchJSON("/api/summary" + qs),
      fetchJSON("/api/delegation" + delQs),
      fetchJSON("/api/sessions" + qs),
    ]);
    state.summary = summary;
    state.delegation = delegation;
    state.sessions = sessions;
    state.lastUpdated = Date.now();
    state.loading = false;
    state.error = false;
    state.refreshing = false;
    state.consecutiveErrors = 0;
    if (_errorBadge) _errorBadge.classList.add("hidden");

    // Determine empty: all three data arrays empty
    const isEmpty = (!summary.project_mix || summary.project_mix.length === 0)
      && (!sessions.sessions || sessions.sessions.length === 0);
    state.empty = isEmpty;

    $("refresh-icon").style.animation = "";
    renderAll();
    showState(isEmpty ? "empty" : "content");
    updateLastUpdated();
    renderFreshness(summary.source_freshness);
  } catch (err) {
    console.error("Dashboard fetch error:", err);
    state.loading = false;
    state.error = true;
    state.refreshing = false;
    state.consecutiveErrors += 1;
    $("refresh-icon").style.animation = "";
    if (state.consecutiveErrors >= 3 && _errorBadge) {
      _errorBadge.textContent = "연속 " + state.consecutiveErrors + "회 실패";
      _errorBadge.classList.remove("hidden");
    }
    showState("error");
  }
}

function showState(which) {
  $("loading-state").classList.toggle("hidden", which !== "loading");
  $("error-state").classList.toggle("hidden", which !== "error");
  $("empty-state").classList.toggle("hidden", which !== "empty");
  $("content").classList.toggle("hidden", which !== "content");
}

function updateLastUpdated() {
  const lu = state.lastUpdated ? new Date(state.lastUpdated) : null;
  $("last-updated").textContent = lu
    ? "갱신 " + String(lu.getHours()).padStart(2, "0") + ":"
      + String(lu.getMinutes()).padStart(2, "0") + ":"
      + String(lu.getSeconds()).padStart(2, "0")
    : "갱신 대기…";
}

function relTime(ms) {
  if (ms == null) return "없음";
  const diffSec = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (diffSec < 60) return diffSec + "초 전";
  if (diffSec < 3600) return Math.round(diffSec / 60) + "분 전";
  if (diffSec < 86400) return Math.round(diffSec / 3600) + "시간 전";
  return Math.round(diffSec / 86400) + "일 전";
}

function renderFreshness(freshness) {
  const node = $("freshness");
  if (!node) return;
  if (!freshness) { node.textContent = ""; return; }
  node.textContent =
    "Claude " + relTime(freshness.claude) + " · opencode " + relTime(freshness.opencode);
}

/* ── warnings banner ────────────────────────────────────────────── */

function renderWarnings() {
  const all = [];
  const seen = new Set();
  for (const resp of [state.summary, state.delegation, state.sessions]) {
    if (!resp || !resp.warnings) continue;
    for (const w of resp.warnings) {
      if (!seen.has(w)) { seen.add(w); all.push(w); }
    }
  }
  const banner = $("warnings-banner");
  if (all.length === 0 || state.warningsDismissed) {
    banner.classList.add("hidden");
    return;
  }
  clear($("warnings-text"));
  for (let i = 0; i < all.length; i++) {
    if (i > 0) $("warnings-text").appendChild(el("br"));
    $("warnings-text").appendChild(document.createTextNode(all[i]));
  }
  banner.classList.remove("hidden");
}

/* ── render all sections ────────────────────────────────────────── */

function renderAll() {
  renderWarnings();
  if (state.summary) {
    renderKPI(state.summary);
    renderProjectMix(state.summary.project_mix || []);
    renderCostChart(state.summary.daily_cost || []);
    renderMismatches(state.summary.mismatches || []);
    renderModelRank(state.summary.model_rank || []);
    renderCacheSection(state.summary.cache || {});
  }
  if (state.delegation) {
    renderFlowChart(state.delegation.flow || []);
    renderOverhead(state.delegation.overhead || {});
    renderAgentsTable(state.delegation.agents || []);
    renderDelegationTimeline(state.delegation.flows || []);
  }
  if (state.sessions) {
    renderSessions(state.sessions.sessions || []);
  }
}

/* ── KPI strip ──────────────────────────────────────────────────── */

function renderKPI(data) {
  const strip = $("kpi-strip");
  clear(strip);
  const kpi = data.kpi || {};
  const cards = [
    { label: "총 비용", glyph: "$", value: money(kpi.total_cost_usd), sub: state.range + " 추정", color: null, bg: null },
    { label: "총 토큰", glyph: "∑", value: tok(kpi.total_tokens), sub: "input+output+cache", color: null, bg: null },
    { label: "캐시 적중률", glyph: "▣", value: pct(kpi.cache_hit_rate), sub: "read / (input+read)", color: "var(--ok)", bg: null },
    { label: "위임 세션", glyph: "⇄", value: pct(kpi.delegated_session_ratio), sub: "opencode 경유", color: "var(--info)", bg: null },
    { label: "이상치", glyph: "▲", value: "" + (kpi.anomaly_count || 0), sub: "주의 필요 세션", color: "var(--danger)", bg: "var(--danger-bg)", border: "var(--danger)" },
  ];
  for (const c of cards) {
    const card = el("div", {
      style: {
        background: c.bg || "var(--panel)",
        border: "1px solid " + (c.border || "var(--border)"),
        borderRadius: "12px",
        padding: "14px 15px",
        display: "flex",
        flexDirection: "column",
        gap: "5px",
      },
    }, [
      el("div", {
        style: { display: "flex", alignItems: "center", gap: "6px", fontSize: "11.5px", color: "var(--muted)", textTransform: "uppercase", letterSpacing: ".6px" },
        className: "mono",
      }, [
        el("span", { textContent: c.glyph, style: { color: c.color || "var(--accent)", fontWeight: "700" } }),
        document.createTextNode(c.label),
      ]),
      el("div", {
        className: "mono",
        textContent: c.value,
        style: { fontWeight: "600", fontSize: "24px", letterSpacing: "-.5px", lineHeight: "1", color: c.color || "var(--text)" },
      }),
      el("div", { textContent: c.sub, style: { fontSize: "11.5px", color: "var(--text-2)" } }),
    ]);
    strip.appendChild(card);
  }
}

/* ── 공용 접기/펼치기 어포던스 ──────────────────────────────────── */

// 목록이 긴 섹션은 전부 상위 8개만 먼저 보여주고 나머지는 접는다.
const LIST_TOP_N = 8;

// 두 가지 배치. "attached"는 표 하단에 이어 붙어 패널 테두리를 잇고,
// "inline"은 카드 그리드 아래에 여백을 두고 독립적으로 놓인다.
const _FOLD_STYLES = {
  attached: { width: "100%", height: "34px", borderRadius: "0", border: "0", borderTop: "1px solid var(--border)" },
  inline: { width: "100%", marginTop: "12px", height: "32px", justifyContent: "center" },
};

function _foldButton(label, onClickFn, variant) {
  return el("button", {
    className: "btn mono",
    style: _FOLD_STYLES[variant] || _FOLD_STYLES.attached,
    textContent: label,
    onClick: onClickFn,
  });
}

// 상한을 넘는 목록에만 버튼을 붙인다. total <= topN 이면 아무것도 붙지 않는다
// (접힌 것이 없는데 "접기"가 뜨면 눌러도 화면이 그대로여서 고장으로 읽힌다).
// setExpanded 는 state 를 갱신하고 해당 섹션을 다시 그리는 책임까지 진다.
function _appendFoldControls(container, total, expanded, setExpanded, variant, extraStyle) {
  const hidden = expanded ? 0 : Math.max(0, total - LIST_TOP_N);
  let btn = null;
  if (hidden > 0) {
    btn = _foldButton("+" + hidden + "개 더  ▾", () => setExpanded(true), variant);
  } else if (expanded && total > LIST_TOP_N) {
    btn = _foldButton("접기  ▴", () => setExpanded(false), variant);
  }
  if (!btn) return;
  if (extraStyle) Object.assign(btn.style, extraStyle);
  container.appendChild(btn);
}

/* ── project mix (stacked bars) ─────────────────────────────────── */

const MIX_TOP_N = 8;
const MIX_SORTS = [["cost", "비용"], ["tokens", "토큰"], ["name", "이름"]];

function _mixTotalTokens(p) {
  return (p.by_model || []).reduce((a, m) => a + (m.tokens || 0), 0);
}

function _sortMix(mix, mode) {
  const copy = mix.slice();
  if (mode === "name") {
    copy.sort((a, b) => String(a.project).localeCompare(String(b.project)));
  } else if (mode === "tokens") {
    copy.sort((a, b) => _mixTotalTokens(b) - _mixTotalTokens(a));
  } else {
    copy.sort((a, b) => (b.cost_usd || 0) - (a.cost_usd || 0));
  }
  return copy;
}

function _mixRow(p) {
  const total = _mixTotalTokens(p);
  const row = el("div");
  row.appendChild(el("div", {
    style: { display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: "6px", fontSize: "12.5px" },
  }, [
    el("span", { className: "mono", textContent: p.project, style: { fontWeight: "600" } }),
    el("span", { className: "mono", textContent: tok(total) + " · " + money(p.cost_usd), style: { color: "var(--text-2)" } }),
  ]));
  const bar = el("div", {
    style: { display: "flex", height: "22px", borderRadius: "6px", overflow: "hidden", background: "var(--panel-2)" },
    role: "img",
    "aria-label": p.project + " 모델 믹스",
  });
  for (const m of (p.by_model || [])) {
    const frac = total > 0 ? m.tokens / total : 0;
    const segPct = Math.round(frac * 100);
    bar.appendChild(el("div", {
      title: m.model + " — " + segPct + "% (" + tok(m.tokens) + ")",
      style: {
        width: (frac * 100) + "%",
        background: modelColor(m.model),
        display: "grid",
        placeItems: "center",
        fontSize: "10px",
        fontWeight: "600",
        color: "rgba(0,0,0,.62)",
        minWidth: frac > 0 ? "2px" : "0",
      },
      className: "mono",
      textContent: segPct >= 14 ? segPct + "%" : "",
    }));
  }
  row.appendChild(bar);
  return row;
}

function renderMixSortControls() {
  const group = $("mix-sort-group");
  if (!group) return;
  clear(group);
  for (const [val, label] of MIX_SORTS) {
    group.appendChild(el("button", {
      className: "btn" + (state.mixSort === val ? " btn-accent" : ""),
      textContent: label,
      style: { height: "24px", fontSize: "11px", padding: "0 8px" },
      onClick: () => {
        if (state.mixSort === val) return;
        state.mixSort = val;
        renderProjectMix(state.summary ? (state.summary.project_mix || []) : []);
      },
    }));
  }
}

function renderProjectMix(mix) {
  const container = $("project-mix");
  clear(container);
  renderMixSortControls();

  if (!mix.length) {
    container.appendChild(el("div", {
      textContent: "데이터 없음",
      style: { color: "var(--muted)", fontSize: "13px", padding: "20px 0", textAlign: "center" },
    }));
    clear($("model-legend"));
    return;
  }

  const sorted = _sortMix(mix, state.mixSort);
  const shown = state.mixExpanded ? sorted : sorted.slice(0, MIX_TOP_N);
  const hidden = state.mixExpanded ? [] : sorted.slice(MIX_TOP_N);

  const grid = el("div", { style: { display: "grid", gap: "15px" } });
  for (const p of shown) grid.appendChild(_mixRow(p));
  container.appendChild(grid);

  if (hidden.length) {
    const restCost = hidden.reduce((a, p) => a + (p.cost_usd || 0), 0);
    container.appendChild(el("button", {
      className: "btn mono",
      style: { width: "100%", marginTop: "12px", height: "32px", justifyContent: "center" },
      textContent: "기타 " + hidden.length + "개 · " + money(restCost) + "  ▾",
      onClick: () => { state.mixExpanded = true; renderProjectMix(mix); },
    }));
  } else if (state.mixExpanded && sorted.length > MIX_TOP_N) {
    container.appendChild(el("button", {
      className: "btn mono",
      style: { width: "100%", marginTop: "12px", height: "32px", justifyContent: "center" },
      textContent: "접기  ▴",
      onClick: () => { state.mixExpanded = false; renderProjectMix(mix); },
    }));
  }

  // 범례는 표시 중인 프로젝트 기준으로만 만든다.
  // 접힌 항목의 모델을 남기면 대응 막대 없는 색상이 생긴다.
  const legend = $("model-legend");
  clear(legend);
  const allModels = new Set();
  for (const p of shown) for (const m of (p.by_model || [])) allModels.add(m.model);
  for (const m of allModels) {
    legend.appendChild(el("span", {
      style: { display: "inline-flex", alignItems: "center", gap: "6px", fontSize: "11.5px", color: "var(--text-2)" },
      className: "mono",
    }, [
      el("span", { style: { width: "10px", height: "10px", borderRadius: "3px", background: modelColor(m), display: "inline-block" } }),
      document.createTextNode(m + " · " + modelTier(m)),
    ]));
  }
}

/* ── model cost rank (section 02) ───────────────────────────────── */

const RANK_TOP_N = 8;
// 백엔드 app/metrics/model_rank.py의 DIRECT_AGENT_LABEL과 같은 문자열이어야 한다.
// 표시 스타일 분기에만 쓰므로 어긋나도 숫자가 틀리지는 않는다 — 배지 모양만 달라진다.
const DIRECT_AGENT_LABEL = "직접(메인)";

function _rankShareCell(share) {
  const frac = Math.max(0, Math.min(1, Number(share) || 0));
  return el("td", { className: "td-mono" }, [
    el("div", {
      style: { display: "flex", alignItems: "center", gap: "8px", justifyContent: "flex-end" },
    }, [
      el("div", {
        style: { width: "72px", height: "7px", borderRadius: "4px", background: "var(--panel-2)", overflow: "hidden", flex: "0 0 auto" },
        role: "img",
        "aria-label": "점유율 " + pct(frac),
      }, [
        el("div", { style: { width: (frac * 100) + "%", height: "100%", background: "var(--accent)" } }),
      ]),
      el("span", { textContent: pct(frac), style: { minWidth: "32px" } }),
    ]),
  ]);
}

function _toggleRankRow(model, list) {
  const next = new Set(state.rankRowsOpen);
  if (next.has(model)) next.delete(model);
  else next.add(model);
  state.rankRowsOpen = next;
  renderModelRank(list);
}

function _rankAgentRow(a) {
  // 직접(메인)은 실제 agent가 아니므로 색 배지 대신 tag-muted로 시각적으로 구분한다.
  const agentColor = modelColor(a.agent);
  const nameCell = a.agent === DIRECT_AGENT_LABEL
    ? el("span", { className: "tag-muted mono", textContent: a.agent })
    : el("span", {
        className: "badge mono",
        textContent: a.agent || "",
        style: { border: "1px solid " + agentColor, color: agentColor, fontSize: "10.5px" },
      });
  return el("tr", { style: { borderTop: "1px solid var(--border)" } }, [
    el("td", { className: "td", style: { paddingLeft: "36px", color: "var(--text-2)", fontSize: "12px" } }, [nameCell]),
    el("td", { className: "td-mono", style: { color: "var(--text-2)" }, textContent: money(a.cost_usd) }),
    el("td", { className: "td-mono", style: { color: "var(--muted)" }, textContent: "—" }),
    el("td", { className: "td-mono", style: { color: "var(--text-2)" }, textContent: tok(a.tokens) }),
  ]);
}

function _rankFoldButton(label, onClickFn) {
  return _foldButton(label, onClickFn, "attached");
}

function renderModelRank(list) {
  const container = $("model-rank");
  if (!container) return;
  clear(container);
  container.appendChild(el("div", {
    style: { padding: "14px 18px", fontWeight: "600", fontSize: "13.5px", borderBottom: "1px solid var(--border)" },
    textContent: "모델별 지불 순위",
  }));

  if (!list.length) {
    container.appendChild(el("div", {
      textContent: "데이터 없음",
      style: { color: "var(--muted)", fontSize: "13px", padding: "20px", textAlign: "center" },
    }));
    return;
  }

  // 숨겨진 항목을 "기타" 한 행으로 합치지 않는다 — 클릭할 대상도 없으면서
  // 상위 행들의 막대 스케일만 왜곡한다. cost_share는 접힘과 무관하게 전체 대비 값이다.
  const shown = state.rankListExpanded ? list : list.slice(0, RANK_TOP_N);
  const hiddenCount = list.length - shown.length;

  const wrap = el("div", { style: { overflowX: "auto" } });
  const table = el("table", { style: { width: "100%", borderCollapse: "collapse", fontSize: "12.5px", minWidth: "520px" } });
  table.appendChild(el("thead", {}, [
    el("tr", {}, [
      el("th", { className: "th", textContent: "모델", style: { textAlign: "left" } }),
      el("th", { className: "th", textContent: "비용 USD", style: { textAlign: "right" } }),
      el("th", { className: "th", textContent: "점유율", style: { textAlign: "right" } }),
      el("th", { className: "th", textContent: "토큰 합계", style: { textAlign: "right" } }),
    ]),
  ]));

  const tbody = el("tbody");
  for (const r of shown) {
    const open = state.rankRowsOpen.has(r.model);
    const onToggle = () => _toggleRankRow(r.model, list);
    // 접기/펼치기 어포던스는 <tr>가 아니라 셀 안의 진짜 <button>이 갖는다.
    // <tr role="button">은 암묵 role="row"를 덮어써 표 구조(헤더-셀 연결)를 무너뜨리고,
    // <tr>에 붙인 aria-label은 하위 셀 텍스트를 가려 비용·점유율·토큰을 못 읽게 한다.
    // 행 클릭은 마우스 편의로 남기되, 키보드·스크린리더는 버튼이 담당한다.
    tbody.appendChild(el("tr", {
      style: { borderTop: "1px solid var(--border)", cursor: "pointer" },
      onClick: onToggle,
    }, [
      el("td", { className: "td" }, [
        el("span", { style: { display: "inline-flex", alignItems: "center", gap: "7px" } }, [
          el("button", {
            className: "mono",
            textContent: open ? "▾" : "▸",
            "aria-expanded": open ? "true" : "false",
            "aria-label": r.model + " agent별 내역 " + (open ? "접기" : "펼치기"),
            style: {
              background: "none", border: "0", padding: "0", font: "inherit",
              color: "var(--muted)", width: "10px", cursor: "pointer",
            },
            // 행에도 onClick이 걸려 있어, 막지 않으면 한 번의 클릭이 두 번 토글된다.
            onClick: (ev) => { ev.stopPropagation(); onToggle(); },
          }),
          el("span", { style: { width: "9px", height: "9px", borderRadius: "3px", background: modelColor(r.model), display: "inline-block" } }),
          el("span", { className: "mono", textContent: r.model, style: { fontWeight: "600" } }),
        ]),
      ]),
      el("td", { className: "td-mono", textContent: money(r.cost_usd) }),
      _rankShareCell(r.cost_share),
      el("td", { className: "td-mono", textContent: tok(r.tokens) }),
    ]));

    if (!open) continue;
    for (const a of (r.by_agent || [])) tbody.appendChild(_rankAgentRow(a));
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  container.appendChild(wrap);

  if (hiddenCount > 0) {
    container.appendChild(_rankFoldButton("+" + hiddenCount + "개 더  ▾", () => {
      state.rankListExpanded = true;
      renderModelRank(list);
    }));
  } else if (state.rankListExpanded && list.length > RANK_TOP_N) {
    container.appendChild(_rankFoldButton("접기  ▴", () => {
      state.rankListExpanded = false;
      renderModelRank(list);
    }));
  }
}

/* ── daily cost chart (Chart.js line) ───────────────────────────── */

function renderCostChart(daily) {
  const canvas = $("cost-chart");
  const total = (daily || []).reduce((a, d) => a + (d.cost_usd || 0), 0);
  $("cost-total-label").textContent = "합계 " + money(total);

  if (_costChart) { _costChart.destroy(); _costChart = null; }
  // Remove any previous fallback message
  const costParent = canvas.parentElement;
  if (costParent) {
    const prev = costParent.querySelector(".chart-empty-msg");
    if (prev) prev.remove();
  }
  if (!daily || !daily.length) {
    canvas.style.display = "none";
    if (costParent) {
      costParent.appendChild(el("div", {
        className: "chart-empty-msg",
        textContent: "데이터 없음",
        style: { color: "var(--muted)", fontSize: "13px", padding: "20px 0", textAlign: "center" },
      }));
    }
    return;
  }
  canvas.style.display = "block";

  const labels = daily.map(d => d.date ? d.date.slice(5) : "");
  const values = daily.map(d => d.cost_usd || 0);
  const accent = getComputedStyle($("app")).getPropertyValue("--accent").trim() || "#d97757";
  const grid = getComputedStyle($("app")).getPropertyValue("--grid").trim() || "#37352f";
  const muted = getComputedStyle($("app")).getPropertyValue("--muted").trim() || "#8b857a";

  try {
    _costChart = new Chart(canvas, {
      type: "line",
      data: {
        labels,
        datasets: [{
          data: values,
          borderColor: accent,
          backgroundColor: accent + "22",
          fill: true,
          tension: 0.3,
          pointRadius: 0,
          pointHoverRadius: 4,
          borderWidth: 2.2,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: true,
        aspectRatio: 3.2,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: function(ctx) { return "$" + ctx.parsed.y.toFixed(2); },
            },
          },
        },
        scales: {
          x: {
            grid: { color: grid, drawBorder: false },
            ticks: { color: muted, font: { family: "'IBM Plex Mono', monospace", size: 10 }, maxTicksLimit: 8 },
          },
          y: {
            grid: { color: grid, drawBorder: false },
            ticks: {
              color: muted,
              font: { family: "'IBM Plex Mono', monospace", size: 10 },
              callback: function(v) { return "$" + v; },
            },
            beginAtZero: true,
          },
        },
      },
    });
  } catch (err) {
    console.error("Cost chart render failed:", err);
    canvas.style.display = "none";
    const parent = canvas.parentElement;
    if (parent) {
      const msg = el("div", {
        textContent: "차트 렌더링 실패",
        style: { color: "var(--muted)", fontSize: "13px", padding: "20px 0", textAlign: "center" },
      });
      parent.appendChild(msg);
    }
  }
}

/* ── mismatches ─────────────────────────────────────────────────── */

function renderMismatches(list) {
  const section = $("mismatches-section");
  clear(section);
  if (!list.length) {
    section.style.display = "none";
    return;
  }
  section.style.display = "block";

  section.appendChild(el("div", {
    style: { display: "flex", alignItems: "center", gap: "9px", marginBottom: "14px" },
  }, [
    el("span", { textContent: "▲", style: { fontSize: "15px", color: "var(--danger)" } }),
    el("span", { textContent: "미스매치 경고 — 비싼 모델이 저난도 작업 처리", style: { fontWeight: "600", fontSize: "13.5px", color: "var(--danger)", letterSpacing: ".3px" } }),
    el("span", { className: "mono", textContent: list.length + "건", style: { marginLeft: "auto", fontSize: "12px", color: "var(--text-2)" } }),
  ]));

  const grid = el("div", {
    style: { display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(280px,1fr))", gap: "12px" },
  });
  for (const m of list) {
    const sevColor = m.severity === "high" ? "var(--danger)" : m.severity === "med" ? "var(--warn)" : "var(--text-2)";
    const sevTag = m.severity === "high" ? "HIGH" : m.severity === "med" ? "MED" : "LOW";
    const card = el("div", {
      style: { background: "var(--panel)", border: "1px solid var(--border)", borderRadius: "11px", padding: "14px" },
    }, [
      el("div", { style: { display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" } }, [
        el("span", { className: sevColor === "var(--danger)" ? "tag-danger" : "tag-warn", textContent: sevTag }),
        el("span", { className: "mono", textContent: (m.project || "") + " #" + (m.session_id || ""), style: { fontSize: "12px", fontWeight: "600" } }),
        el("span", {
          className: "badge mono",
          textContent: m.model || "",
          style: { border: "1px solid " + modelColor(m.model), color: modelColor(m.model), fontSize: "10.5px" },
        }),
      ]),
      el("div", { textContent: m.reason || "", style: { fontSize: "12.5px", color: "var(--text-2)", lineHeight: "1.45", marginBottom: "10px" } }),
      el("div", { style: { display: "flex", gap: "16px", marginBottom: "10px" } }, [
        metricBox("비용", money(m.cost_usd)),
        metricBox("토큰", tok(m.tokens)),
        metricBox("턴", "" + (m.turns || 0)),
      ]),
      el("div", {
        className: "mono",
        textContent: "→ " + (m.suggested_model || "") + " 처리 시 ≈" + money(m.estimated_savings_usd) + " 절감",
        style: { fontSize: "12px", padding: "7px 10px", borderRadius: "8px", background: "var(--ok-bg)", color: "var(--ok)" },
      }),
    ]);
    grid.appendChild(card);
  }
  section.appendChild(grid);
}

function metricBox(label, value) {
  return el("div", {}, [
    el("div", { className: "mono", textContent: label, style: { fontSize: "10.5px", color: "var(--muted)", textTransform: "uppercase", letterSpacing: ".4px" } }),
    el("div", { className: "mono", textContent: value, style: { fontWeight: "600", fontSize: "14px" } }),
  ]);
}

/* ── cache section ──────────────────────────────────────────────── */

function renderCacheSection(cache) {
  // Savings cards
  const now = $("savings-now");
  clear(now);
  now.appendChild(el("div", { className: "mono", textContent: "캐시로 절감 (추정)", style: { fontSize: "11.5px", color: "var(--ok)", textTransform: "uppercase", letterSpacing: ".5px" } }));
  now.appendChild(el("div", { className: "mono", textContent: money(cache.savings_now_usd), style: { fontWeight: "600", fontSize: "30px", color: "var(--ok)", marginTop: "4px" } }));
  now.appendChild(el("div", { textContent: "전체 캐시 read가 없었을 경우 대비", style: { fontSize: "12px", color: "var(--text-2)", marginTop: "2px" } }));

  const pot = $("savings-potential");
  clear(pot);
  pot.appendChild(el("div", { className: "mono", textContent: "추가 개선 여지", style: { fontSize: "11.5px", color: "var(--warn)", textTransform: "uppercase", letterSpacing: ".5px" } }));
  pot.appendChild(el("div", { className: "mono", textContent: money(cache.savings_potential_usd), style: { fontWeight: "600", fontSize: "30px", color: "var(--warn)", marginTop: "4px" } }));
  pot.appendChild(el("div", { textContent: "하위 세션이 평균 수준 캐시 도달 시", style: { fontSize: "12px", color: "var(--text-2)", marginTop: "2px" } }));

  // Ratio bars
  const bars = $("cache-ratio-bars");
  clear(bars);
  const rows = cache.by_project_model || [];
  if (!rows.length) {
    bars.appendChild(el("div", { textContent: "데이터 없음", style: { color: "var(--muted)", fontSize: "13px", padding: "20px 0", textAlign: "center" } }));
  } else {
    // 비율이 낮은 순으로 이미 정렬되어 오므로 상위 N개 = 가장 개선이 급한 N개다.
    const shown = state.cacheRatioExpanded ? rows : rows.slice(0, LIST_TOP_N);
    const grid = el("div", { style: { display: "grid", gap: "11px" } });
    for (const c of shown) {
      const ratio = c.cache_read_ratio || 0;
      const low = ratio < 0.3;
      const col = ratio < 0.2 ? "var(--danger)" : ratio < 0.3 ? "var(--warn)" : "var(--ok)";
      const row = el("div");
      const header = el("div", {
        style: { display: "flex", alignItems: "center", gap: "8px", fontSize: "12px", marginBottom: "4px" },
      }, [
        el("span", { style: { width: "9px", height: "9px", borderRadius: "3px", background: modelColor(c.model), display: "inline-block" } }),
        el("span", { className: "mono", textContent: c.project + " · " + c.model }),
      ]);
      if (low) {
        const tagText = ratio < 0.2 ? "매우 낮음" : "낮음";
        header.appendChild(el("span", {
          className: ratio < 0.2 ? "tag-danger" : "tag-warn",
          textContent: tagText,
        }));
      }
      header.appendChild(el("span", {
        className: "mono",
        textContent: pct(ratio),
        style: { marginLeft: "auto", fontWeight: "600", color: col },
      }));
      row.appendChild(header);
      row.appendChild(el("div", {
        style: { height: "9px", borderRadius: "5px", background: "var(--panel-2)", overflow: "hidden" },
      }, [
        el("div", { style: { width: (ratio * 100) + "%", height: "100%", background: col, borderRadius: "5px" } }),
      ]));
      grid.appendChild(row);
    }
    bars.appendChild(grid);
    _appendFoldControls(bars, rows.length, state.cacheRatioExpanded, (v) => {
      state.cacheRatioExpanded = v;
      renderCacheSection(cache);
    }, "inline");
  }

  // Low cache table
  renderLowCacheTable(cache.worst_sessions || []);
}

function renderLowCacheTable(worst) {
  const container = $("low-cache-table");
  clear(container);
  const lcHeader = el("div", {
    style: { padding: "14px 18px", fontWeight: "600", fontSize: "13.5px", borderBottom: "1px solid var(--border)" },
  });
  lcHeader.appendChild(document.createTextNode("캐시 효율 하위 세션 "));
  lcHeader.appendChild(el("span", {
    textContent: "— 개선 대상",
    style: { color: "var(--muted)", fontWeight: "400", fontSize: "12px" },
  }));
  container.appendChild(lcHeader);
  if (!worst.length) {
    container.appendChild(el("div", { textContent: "데이터 없음", style: { color: "var(--muted)", fontSize: "13px", padding: "20px", textAlign: "center" } }));
    return;
  }
  const wrap = el("div", { style: { overflowX: "auto" } });
  const table = el("table", { style: { width: "100%", borderCollapse: "collapse", fontSize: "12.5px", minWidth: "520px" } });
  const thead = el("thead", {}, [
    el("tr", {}, [
      el("th", { className: "th", textContent: "세션", style: { textAlign: "left" } }),
      el("th", { className: "th", textContent: "모델", style: { textAlign: "left" } }),
      el("th", { className: "th", textContent: "캐시 비율", style: { textAlign: "right" } }),
      el("th", { className: "th", textContent: "input 토큰", style: { textAlign: "right" } }),
      el("th", { className: "th", textContent: "개선 시 절감", style: { textAlign: "right" } }),
    ]),
  ]);
  table.appendChild(thead);
  const tbody = el("tbody");
  const shownWorst = state.lowCacheExpanded ? worst : worst.slice(0, LIST_TOP_N);
  for (const r of shownWorst) {
    const ratio = r.cache_read_ratio || 0;
    const ratioCol = ratio < 0.2 ? "var(--danger)" : "var(--warn)";
    const tr = el("tr", { style: { borderTop: "1px solid var(--border)" } }, [
      el("td", { className: "td" }, [
        el("span", { className: "mono", textContent: (r.project || "") + " #" + (r.session_id || "") }),
      ]),
      el("td", { className: "td" }, [
        el("span", {
          className: "badge mono",
          textContent: r.model || "",
          style: { border: "1px solid " + modelColor(r.model), color: modelColor(r.model), fontSize: "11px" },
        }),
      ]),
      el("td", { className: "td-mono" }, [
        el("span", { className: "mono", textContent: pct(ratio), style: { fontWeight: "600", color: ratioCol } }),
      ]),
      el("td", { className: "td-mono", textContent: tok(r.input_tokens) }),
      el("td", { className: "td-mono" }, [
        el("span", { textContent: money(r.estimated_savings_usd), style: { color: "var(--ok)" } }),
      ]),
    ]);
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  container.appendChild(wrap);
  _appendFoldControls(container, worst.length, state.lowCacheExpanded, (v) => {
    state.lowCacheExpanded = v;
    renderLowCacheTable(worst);
  }, "attached");
}

/* ── delegation flow chart (Chart.js horizontal bar) ────────────── */

function renderFlowChart(flow) {
  const canvas = $("flow-chart");
  if (_flowChart) { _flowChart.destroy(); _flowChart = null; }
  // Remove any previous fallback message
  const flowParent = canvas.parentElement;
  if (flowParent) {
    const prev = flowParent.querySelector(".chart-empty-msg");
    if (prev) prev.remove();
  }
  if (!flow || !flow.length) {
    canvas.style.display = "none";
    if (flowParent) {
      flowParent.appendChild(el("div", {
        className: "chart-empty-msg",
        textContent: "데이터 없음",
        style: { color: "var(--muted)", fontSize: "13px", padding: "20px 0", textAlign: "center" },
      }));
    }
    return;
  }
  canvas.style.display = "block";

  const labels = flow.map(f => f.agent || "");
  const tokens = flow.map(f => f.tokens || 0);
  const colors = flow.map(f => modelColor(f.agent));
  const muted = getComputedStyle($("app")).getPropertyValue("--muted").trim() || "#8b857a";
  const grid = getComputedStyle($("app")).getPropertyValue("--grid").trim() || "#37352f";

  try {
    _flowChart = new Chart(canvas, {
      type: "bar",
      data: {
        labels,
        datasets: [{
          data: tokens,
          backgroundColor: colors.map(c => c + "88"),
          borderColor: colors,
          borderWidth: 1.5,
          borderRadius: 4,
        }],
      },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: true,
        aspectRatio: 1.8,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: function(ctx) {
                const f = flow[ctx.dataIndex];
                return tok(f.tokens) + " · " + (f.calls || 0) + "건";
              },
            },
          },
        },
        scales: {
          x: {
            grid: { color: grid, drawBorder: false },
            ticks: { color: muted, font: { family: "'IBM Plex Mono', monospace", size: 10 }, callback: function(v) { return tok(v); } },
          },
          y: {
            grid: { display: false },
            ticks: { color: muted, font: { family: "'IBM Plex Mono', monospace", size: 11, weight: "600" } },
          },
        },
      },
    });
  } catch (err) {
    console.error("Flow chart render failed:", err);
    canvas.style.display = "none";
    const parent = canvas.parentElement;
    if (parent) {
      const msg = el("div", {
        textContent: "차트 렌더링 실패",
        style: { color: "var(--muted)", fontSize: "13px", padding: "20px 0", textAlign: "center" },
      });
      parent.appendChild(msg);
    }
  }
}

/* ── overhead cards ─────────────────────────────────────────────── */

function renderOverhead(oh) {
  const container = $("overhead-cards");
  clear(container);
  if (!oh || !oh.flow_count) {
    container.appendChild(el("div", {
      textContent: "위임 흐름 없음",
      style: { color: "var(--muted)", fontSize: "13px", padding: "20px", textAlign: "center" },
    }));
    return;
  }
  const twoHop = oh.two_hop_count || 0;
  const twoHopDanger = twoHop >= 1;
  const cards = [
    {
      label: "위임 비중",
      value: pct(oh.delegation_share),
      sub: money(oh.delegated_cost_usd) + " / " + money(oh.total_flow_cost_usd) + " · 흐름 " + oh.flow_count + "개",
      color: null, bg: "var(--panel)", border: "var(--border)",
    },
    {
      label: "위임 셋업",
      value: money(oh.setup_cost_usd),
      sub: "위임 비용의 " + pct(oh.setup_share) + " · 컨텍스트 재적재(상한)",
      color: "var(--warn)",
      bg: "var(--warn-bg)",
      border: "var(--warn)",
    },
    {
      label: "실작업",
      value: money(oh.work_cost_usd),
      sub: "셋업 제외한 자식 비용",
      color: null, bg: "var(--panel)", border: "var(--border)",
    },
    {
      label: "재위임 (2-hop)",
      value: twoHop + "건",
      sub: "agent가 다시 위임",
      color: twoHopDanger ? "var(--danger)" : null,
      bg: twoHopDanger ? "var(--danger-bg)" : "var(--panel)",
      border: twoHopDanger ? "var(--danger)" : "var(--border)",
    },
  ];
  for (const c of cards) {
    container.appendChild(el("div", {
      style: { background: c.bg, border: "1px solid " + c.border, borderRadius: "12px", padding: "14px 15px" },
    }, [
      el("div", { className: "mono", textContent: c.label, style: { fontSize: "11.5px", color: "var(--muted)", textTransform: "uppercase", letterSpacing: ".4px" } }),
      el("div", { className: "mono", textContent: c.value, style: { fontWeight: "600", fontSize: "22px", lineHeight: "1.1", margin: "3px 0", color: c.color || "var(--text)" } }),
      el("div", { textContent: c.sub, style: { fontSize: "11.5px", color: "var(--text-2)" } }),
    ]));
  }
}

/* ── agents table (with model breakdown sub-rows) ───────────────── */

function renderAgentsTable(agents) {
  const container = $("agents-table");
  clear(container);
  container.appendChild(el("div", {
    style: { padding: "14px 18px", fontWeight: "600", fontSize: "13.5px", borderBottom: "1px solid var(--border)" },
    textContent: "agent별 집계",
  }));
  if (!agents.length) {
    container.appendChild(el("div", { textContent: "데이터 없음", style: { color: "var(--muted)", fontSize: "13px", padding: "20px", textAlign: "center" } }));
    return;
  }
  const wrap = el("div", { style: { overflowX: "auto" } });
  const table = el("table", { style: { width: "100%", borderCollapse: "collapse", fontSize: "12.5px", minWidth: "560px" } });
  table.appendChild(el("thead", {}, [
    el("tr", {}, [
      el("th", { className: "th", textContent: "agent", style: { textAlign: "left" } }),
      el("th", { className: "th", textContent: "건수", style: { textAlign: "right" } }),
      el("th", { className: "th", textContent: "토큰", style: { textAlign: "right" } }),
      el("th", { className: "th", textContent: "비용", style: { textAlign: "right" } }),
      el("th", { className: "th", textContent: "평균 턴수", style: { textAlign: "right" } }),
    ]),
  ]));
  const tbody = el("tbody");
  const shownAgents = state.agentsExpanded ? agents : agents.slice(0, LIST_TOP_N);
  for (const a of shownAgents) {
    // Main agent row
    const tr = el("tr", { style: { borderTop: "1px solid var(--border)" } }, [
      el("td", { className: "td" }, [
        el("span", { style: { display: "inline-flex", alignItems: "center", gap: "7px" } }, [
          el("span", { style: { width: "9px", height: "9px", borderRadius: "3px", background: modelColor(a.agent), display: "inline-block" } }),
          el("span", { className: "mono", textContent: a.agent, style: { fontWeight: "600" } }),
        ]),
      ]),
      el("td", { className: "td-mono", textContent: "" + (a.calls || 0) }),
      el("td", { className: "td-mono", textContent: tok(a.tokens) }),
      el("td", { className: "td-mono", textContent: money(a.cost_usd) }),
      el("td", { className: "td-mono" }, [
        el("span", {
          textContent: (a.avg_turns || 0).toFixed(1),
          style: (a.avg_turns || 0) > 8 ? { color: "var(--warn)", fontWeight: "600" } : {},
        }),
      ]),
    ]);
    tbody.appendChild(tr);

    // Model breakdown sub-rows (if models[] present)
    const models = a.models || [];
    for (const m of models) {
      const sub = el("tr", { style: { borderTop: "1px solid var(--border)" } }, [
        el("td", { className: "td", style: { paddingLeft: "36px", color: "var(--text-2)", fontSize: "12px" } }, [
          el("span", {
            className: "badge mono",
            textContent: m.model || "",
            style: { border: "1px solid " + modelColor(m.model), color: modelColor(m.model), fontSize: "10.5px" },
          }),
        ]),
        el("td", { className: "td-mono", style: { color: "var(--text-2)" }, textContent: "" + (m.calls || 0) }),
        el("td", { className: "td-mono", style: { color: "var(--text-2)" }, textContent: tok(m.tokens) }),
        el("td", { className: "td-mono", style: { color: "var(--text-2)" }, textContent: money(m.cost_usd) }),
        el("td", { className: "td-mono", style: { color: "var(--muted)" }, textContent: "—" }),
      ]);
      tbody.appendChild(sub);
    }
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  container.appendChild(wrap);
  _appendFoldControls(container, agents.length, state.agentsExpanded, (v) => {
    state.agentsExpanded = v;
    renderAgentsTable(agents);
  }, "attached");
}

/* ── sessions (sparklines via inline SVG) ────────────────────────── */

function renderSessions(sessions) {
  const grid = $("sessions-grid");
  clear(grid);
  if (!sessions.length) {
    grid.appendChild(el("div", {
      style: { gridColumn: "1/-1", textAlign: "center", color: "var(--muted)", fontSize: "13px", padding: "40px 0" },
      textContent: "데이터 없음",
    }));
    return;
  }
  const toneCol = { danger: "var(--danger)", warn: "var(--warn)", ok: "var(--ok)" };
  const shownSessions = state.sessionsExpanded ? sessions : sessions.slice(0, LIST_TOP_N);
  for (const s of shownSessions) {
    const health = s.health || "ok";
    const col = toneCol[health] || toneCol.ok;
    const cg = s.context_growth || [];
    const growth = cg.length >= 2 ? cg[cg.length - 1] / (cg[0] || 1) : 1;

    const card = el("div", {
      style: {
        background: "var(--panel)",
        border: "1px solid " + (health === "ok" ? "var(--border)" : col),
        borderRadius: "12px",
        padding: "15px",
      },
    });

    // Header row
    card.appendChild(el("div", { style: { display: "flex", alignItems: "center", gap: "9px", marginBottom: "10px", flexWrap: "wrap" } }, [
      el("span", { style: { width: "9px", height: "9px", borderRadius: "50%", background: col, display: "inline-block" } }),
      el("span", { className: "mono", textContent: (s.project || "") + " #" + (s.session_id || ""), style: { fontWeight: "600", fontSize: "12.5px" } }),
      el("span", {
        className: "badge mono",
        textContent: s.model || "",
        style: { border: "1px solid " + modelColor(s.model), color: modelColor(s.model), fontSize: "10.5px" },
      }),
      el("span", { className: "mono", textContent: (s.turns || 0) + "턴", style: { marginLeft: "auto", fontSize: "11.5px", color: "var(--text-2)" } }),
    ]));

    // Sparkline + growth
    const sparkWrap = el("div", { style: { display: "flex", alignItems: "flex-end", gap: "12px" } });
    const sparkDiv = el("div", { style: { flex: "1", minWidth: "0" } });
    sparkDiv.appendChild(buildSparkline(cg, health, s.compaction_turns || [], s.token_spike));
    sparkWrap.appendChild(sparkDiv);
    sparkWrap.appendChild(el("div", { style: { textAlign: "right" } }, [
      el("div", { className: "mono", textContent: "컨텍스트", style: { fontSize: "10.5px", color: "var(--muted)", textTransform: "uppercase", letterSpacing: ".4px" } }),
      el("div", { className: "mono", textContent: "×" + growth.toFixed(growth >= 10 ? 0 : 1), style: { fontWeight: "600", fontSize: "16px", color: col } }),
    ]));
    card.appendChild(sparkWrap);

    // Badges
    const badges = el("div", { style: { display: "flex", flexWrap: "wrap", gap: "6px", marginTop: "11px" } });
    let hasBadge = false;
    if (s.compaction_turns && s.compaction_turns.length > 0) {
      badges.appendChild(el("span", { className: "tag-muted mono", textContent: "◆ COMPACTION" }));
      hasBadge = true;
    }
    if (s.token_spike) {
      badges.appendChild(el("span", { className: "tag-danger mono", textContent: "▲ 토큰 폭증" }));
      hasBadge = true;
    }
    if (s.split_recommended) {
      badges.appendChild(el("span", { className: "tag-warn mono", textContent: "✂ 세션 분리 권장" }));
      hasBadge = true;
    }
    if (!hasBadge) {
      badges.appendChild(el("span", { className: "tag-ok mono", textContent: "● 안정" }));
    }
    card.appendChild(badges);
    grid.appendChild(card);
  }
  // 컨테이너가 CSS 그리드라 버튼도 그리드 아이템이 된다 — 한 칸에 끼어
  // 카드 폭으로 찌그러지지 않도록 전체 열을 차지시킨다.
  _appendFoldControls(grid, sessions.length, state.sessionsExpanded, (v) => {
    state.sessionsExpanded = v;
    renderSessions(sessions);
  }, "inline", { gridColumn: "1/-1" });
}

/* ── delegation timeline (CSS gantt) ────────────────────────────── */

const FLOW_TOP_N = 8;

function _fmtDuration(sec) {
  if (sec == null || Number.isNaN(sec)) return "—";
  var n = Number(sec);
  if (n < 60) return Math.round(n) + "초";
  if (n < 3600) return Math.round(n / 60) + "분";
  return (n / 3600).toFixed(1) + "시간";
}

function _toggleFlowCard(nodeId, list) {
  const next = new Set(state.flowOpen);
  if (next.has(nodeId)) next.delete(nodeId);
  else next.add(nodeId);
  state.flowOpen = next;
  renderDelegationTimeline(list);
}

function _flowBarPct(flowStartMs, spanMs, childStart, childEnd) {
  // span 0(모든 시각 동일)이어도 최소 폭으로 보이게 한다.
  var cStart = childStart ? Date.parse(childStart) : flowStartMs;
  var cEnd = childEnd ? Date.parse(childEnd) : cStart;
  if (Number.isNaN(cStart)) cStart = flowStartMs;
  if (Number.isNaN(cEnd)) cEnd = cStart;
  if (cEnd < cStart) cEnd = cStart;
  if (spanMs <= 0) return { left: 0, width: 2 };
  var left = ((cStart - flowStartMs) / spanMs) * 100;
  var width = ((cEnd - cStart) / spanMs) * 100;
  if (left < 0) left = 0;
  if (left > 100) left = 100;
  if (width < 1.5) width = 1.5;
  if (left + width > 100) width = Math.max(1.5, 100 - left);
  return { left: left, width: width };
}

function _flowChildRow(child, flowStartMs, spanMs) {
  var depth = Math.max(1, Number(child.depth) || 1);
  var indent = 10 * (depth - 1);
  var agent = child.agent || "unknown";
  var color = modelColor(agent);
  var bar = _flowBarPct(flowStartMs, spanMs, child.start, child.end);
  var barStyle = {
    position: "absolute",
    left: bar.left + "%",
    width: bar.width + "%",
    top: "4px",
    bottom: "4px",
    borderRadius: "4px",
    background: color,
    minWidth: "4px",
  };
  if (child.inferred) {
    barStyle.opacity = "0.45";
    barStyle.border = "1px dashed " + color;
    barStyle.background = "transparent";
  }

  return el("div", {
    style: {
      display: "grid",
      gridTemplateColumns: "minmax(180px,1.2fr) 3fr minmax(120px,auto)",
      gap: "10px",
      alignItems: "center",
      padding: "6px 0",
      borderTop: "1px solid var(--border)",
    },
  }, [
    el("div", {
      style: {
        display: "flex",
        alignItems: "center",
        gap: "6px",
        flexWrap: "wrap",
        paddingLeft: indent + "px",
        minWidth: "0",
      },
    }, [
      el("span", {
        style: {
          width: "8px",
          height: "8px",
          borderRadius: "2px",
          background: color,
          display: "inline-block",
          flex: "0 0 auto",
        },
      }),
      el("span", {
        className: "mono",
        textContent: agent,
        style: { fontWeight: "600", fontSize: "12px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
      }),
      child.inferred && el("span", { className: "tag-muted mono", textContent: "추정" }),
      child.parallel_group != null && el("span", {
        className: "tag-ok mono",
        textContent: "∥" + child.parallel_group,
      }),
    ]),
    el("div", {
      style: {
        position: "relative",
        height: "22px",
        background: "var(--panel-2)",
        borderRadius: "5px",
        overflow: "hidden",
      },
      role: "img",
      "aria-label": agent + " " + _fmtDuration(child.duration_sec),
    }, [
      el("div", { style: barStyle }),
    ]),
    el("div", {
      className: "mono",
      style: {
        fontSize: "11.5px",
        color: "var(--text-2)",
        textAlign: "right",
        whiteSpace: "nowrap",
      },
    }, [
      el("span", { textContent: _fmtDuration(child.duration_sec) }),
      el("span", { textContent: " · ", style: { color: "var(--muted)" } }),
      el("span", { textContent: money(child.cost_usd) }),
      el("span", { textContent: " · ", style: { color: "var(--muted)" } }),
      el("span", { textContent: tok(child.tokens) }),
    ]),
  ]);
}

function _flowCard(flow, list) {
  var nodeId = flow.node_id || "";
  var open = state.flowOpen.has(nodeId);
  var children = flow.children || [];
  var onToggle = function() { _toggleFlowCard(nodeId, list); };

  var flowStartMs = flow.start ? Date.parse(flow.start) : NaN;
  var flowEndMs = flow.end ? Date.parse(flow.end) : NaN;
  if (Number.isNaN(flowStartMs)) flowStartMs = 0;
  if (Number.isNaN(flowEndMs)) flowEndMs = flowStartMs;
  var spanMs = Math.max(0, flowEndMs - flowStartMs);

  var header = el("div", {
    style: {
      display: "flex",
      alignItems: "center",
      gap: "8px",
      flexWrap: "wrap",
      padding: "14px 16px",
      cursor: "pointer",
    },
    onClick: onToggle,
  }, [
    el("button", {
      className: "mono",
      textContent: open ? "▾" : "▸",
      "aria-expanded": open ? "true" : "false",
      "aria-label": (flow.project || "흐름") + " 위임 타임라인 " + (open ? "접기" : "펼치기"),
      style: {
        background: "none", border: "0", padding: "0", font: "inherit",
        color: "var(--muted)", width: "12px", cursor: "pointer", flex: "0 0 auto",
      },
      onClick: function(ev) { ev.stopPropagation(); onToggle(); },
    }),
    el("span", {
      className: "mono",
      textContent: flow.project || "—",
      style: { fontWeight: "600", fontSize: "13px" },
    }),
    el("span", {
      className: "mono",
      textContent: flow.session_id || "",
      style: { fontSize: "11.5px", color: "var(--muted)" },
    }),
    el("span", {
      className: "tag-muted mono",
      textContent: (flow.child_count != null ? flow.child_count : children.length) + "개 위임",
    }),
    (flow.max_parallel || 0) > 1 && el("span", {
      className: "tag-ok mono",
      textContent: "최대 " + flow.max_parallel + " 병렬",
    }),
    (flow.two_hop_count || 0) > 0 && el("span", {
      className: "tag-warn mono",
      textContent: "재위임 " + flow.two_hop_count,
    }),
    el("span", {
      className: "mono",
      style: { marginLeft: "auto", fontSize: "12px", color: "var(--text-2)", whiteSpace: "nowrap" },
      textContent: money(flow.cost_usd) + " · " + _fmtDuration(flow.duration_sec),
    }),
  ]);

  // 흐름별 비용 배분 한 줄 (접힌 상태에서도 노출).
  var delegatedCost = null;
  if (flow.cost_usd != null) {
    if (flow.self && flow.self.cost_usd != null) {
      delegatedCost = Math.max(0, Number(flow.cost_usd) - Number(flow.self.cost_usd));
    } else if (flow.delegation_share != null) {
      delegatedCost = Number(flow.cost_usd) * Number(flow.delegation_share);
    }
  }
  var allocLine = el("div", {
    className: "mono",
    textContent: "위임 " + pct(flow.delegation_share) + " (" + money(delegatedCost) + ") · 셋업 " + money(flow.setup_cost_usd),
    style: {
      fontSize: "11.5px",
      color: "var(--text-2)",
      padding: "0 16px 12px",
      marginTop: "-4px",
    },
  });

  var card = el("div", {
    style: {
      background: "var(--panel)",
      border: "1px solid var(--border)",
      borderRadius: "12px",
      overflow: "hidden",
    },
  }, [header, allocLine]);

  if (open) {
    var body = el("div", { style: { padding: "0 16px 12px" } });
    if (!children.length) {
      body.appendChild(el("div", {
        textContent: "자손 위임 없음",
        style: { color: "var(--muted)", fontSize: "12.5px", padding: "10px 0", textAlign: "center" },
      }));
    } else {
      for (var i = 0; i < children.length; i++) {
        body.appendChild(_flowChildRow(children[i], flowStartMs, spanMs));
      }
    }
    card.appendChild(body);
  }

  return card;
}

function _flowsTotal(flows) {
  // 서버 상한 도입 후 숨은 개수는 flows_total 기준. 옛 응답(필드 없음)은 배열 길이로 퇴화.
  var raw = state.delegation && state.delegation.flows_total;
  if (raw == null) return flows.length;
  var n = Number(raw);
  return Number.isFinite(n) ? n : flows.length;
}

function _loadMoreFlows(btn, flows) {
  // 이미 전부 받았으면 재요청 없이 클라이언트 펼침만.
  if (_flowsTotal(flows) <= flows.length) {
    state.flowListExpanded = true;
    state.flowListError = null;
    renderDelegationTimeline(flows);
    return;
  }
  btn.disabled = true;
  btn.textContent = "불러오는 중…";
  state.flowListError = null;
  var qs = "?range=" + encodeURIComponent(state.range) +
           "&source=" + encodeURIComponent(state.source) +
           "&limit=1000";
  fetchJSON("/api/delegation" + qs).then(function (data) {
    state.delegation = data;
    state.flowListExpanded = true;
    state.flowListError = null;
    renderDelegationTimeline((data && data.flows) || []);
  }).catch(function (err) {
    // 재렌더로 버튼을 원상복구하고 실패를 텍스트로 알린다 (빈 catch 금지).
    var msg = (err && err.message) ? String(err.message) : "알 수 없는 오류";
    state.flowListError = "흐름을 더 불러오지 못했습니다: " + msg;
    renderDelegationTimeline(flows);
  });
}

function renderDelegationTimeline(flows) {
  var container = $("flow-timeline");
  if (!container) return;
  clear(container);

  // 로딩 중(state.delegation 없음)에는 renderAll이 호출하지 않지만,
  // 직접 호출·빈 배열 모두 안전하게 처리한다.
  if (!flows || !flows.length) {
    container.appendChild(el("div", {
      textContent: "이 기간에는 위임 흐름이 없습니다.",
      style: { color: "var(--muted)", fontSize: "13px", padding: "28px 0", textAlign: "center" },
    }));
    return;
  }

  var total = _flowsTotal(flows);
  var shown = state.flowListExpanded ? flows : flows.slice(0, FLOW_TOP_N);
  // 서버 잔여(flows_total - flows.length) + 클라이언트 Top-N 접힘.
  // flows_total 없으면 total === flows.length 로 퇴화해 기존 관용구와 동일.
  var hiddenCount = Math.max(0, total - shown.length);

  for (var i = 0; i < shown.length; i++) {
    container.appendChild(_flowCard(shown[i], flows));
  }

  if (hiddenCount > 0 && !state.flowListExpanded) {
    container.appendChild(el("button", {
      className: "btn mono",
      style: {
        width: "100%", height: "36px", borderRadius: "10px",
        border: "1px solid var(--border)", background: "var(--panel)",
      },
      textContent: "+" + hiddenCount + "개 더  ▾",
      onClick: function (ev) {
        _loadMoreFlows(ev.currentTarget, flows);
      },
    }));
  } else if (state.flowListExpanded && total > FLOW_TOP_N) {
    // 접기는 재요청 없이 클라이언트 슬라이스만.
    container.appendChild(el("button", {
      className: "btn mono",
      style: {
        width: "100%", height: "36px", borderRadius: "10px",
        border: "1px solid var(--border)", background: "var(--panel)",
      },
      textContent: "접기  ▴",
      onClick: function () {
        state.flowListExpanded = false;
        state.flowListError = null;
        renderDelegationTimeline(flows);
      },
    }));
  }

  if (state.flowListError) {
    container.appendChild(el("div", {
      className: "mono",
      textContent: state.flowListError,
      style: {
        color: "var(--danger)", fontSize: "12px",
        textAlign: "center", padding: "8px 0 0",
      },
    }));
  }
}

function buildSparkline(pts, tone, compactions, spike) {
  const w = 200, h = 48, p = 5;
  const toneCol = { danger: "var(--danger)", warn: "var(--warn)", ok: "var(--ok)" };
  const col = toneCol[tone] || toneCol.ok;

  if (!pts || pts.length < 2) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 " + w + " " + h);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "컨텍스트 스파크라인 — 데이터 부족");
    svg.style.cssText = "width:100%;height:44px;display:block;";
    return svg;
  }

  const max = Math.max.apply(null, pts);
  const min = Math.min.apply(null, pts);
  const rng = (max - min) || 1;
  const X = function(i) { return p + i / (pts.length - 1) * (w - 2 * p); };
  const Y = function(v) { return p + (h - 2 * p) - (v - min) / rng * (h - 2 * p); };

  let d = "";
  for (let i = 0; i < pts.length; i++) {
    d += (i === 0 ? "M" : "L") + X(i).toFixed(1) + " " + Y(pts[i]).toFixed(1) + " ";
  }

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 " + w + " " + h);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "컨텍스트 토큰 성장 스파크라인");
  svg.style.cssText = "width:100%;height:44px;display:block;";

  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", d);
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", col);
  path.setAttribute("stroke-width", "2");
  path.setAttribute("stroke-linejoin", "round");
  path.setAttribute("stroke-linecap", "round");
  svg.appendChild(path);

  // Compaction markers
  if (compactions && compactions.length) {
    for (const ci of compactions) {
      if (ci < 0 || ci >= pts.length) continue;
      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("x1", X(ci));
      line.setAttribute("x2", X(ci));
      line.setAttribute("y1", "2");
      line.setAttribute("y2", h - 2);
      line.setAttribute("stroke", "var(--muted)");
      line.setAttribute("stroke-width", "1");
      line.setAttribute("stroke-dasharray", "2 2");
      svg.appendChild(line);
    }
  }

  // End dot
  const li = pts.length - 1;
  const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  dot.setAttribute("cx", X(li));
  dot.setAttribute("cy", Y(pts[li]));
  dot.setAttribute("r", spike ? "4" : "2.6");
  dot.setAttribute("fill", col);
  svg.appendChild(dot);

  return svg;
}

/* ── boot ───────────────────────────────────────────────────────── */

function boot() {
  initTheme();
  restoreFilters();
  buildRangeSelector();
  buildSourceSelector();

  // Consecutive-error badge (inserted next to refresh button)
  _errorBadge = el("span", {
    className: "mono hidden",
    textContent: "",
    style: {
      fontSize: "10.5px",
      fontWeight: "600",
      padding: "1px 6px",
      borderRadius: "5px",
      background: "var(--danger-bg)",
      color: "var(--danger)",
      border: "1px solid var(--danger)",
    },
  });
  const refreshBtn = $("btn-refresh");
  if (refreshBtn && refreshBtn.parentElement) {
    refreshBtn.parentElement.appendChild(_errorBadge);
  }

  $("btn-theme").addEventListener("click", toggleTheme);
  $("btn-refresh").addEventListener("click", function() { refreshAll(true); });
  $("btn-retry").addEventListener("click", function() { refreshAll(true); });
  $("warnings-dismiss").addEventListener("click", function() {
    state.warningsDismissed = true;
    $("warnings-banner").classList.add("hidden");
  });

  // System theme change listener
  try {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function(e) {
      state.theme = e.matches ? "dark" : "light";
      applyTheme();
    });
  } catch (err) {
    console.warn("matchMedia change listener setup failed:", err);
  }

  // Initial fetch
  refreshAll(true);

  // 기간에 따라 15초/60초 적응형 폴링
  restartPolling();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}

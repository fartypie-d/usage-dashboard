/** 작업 흐름 페이지: 백엔드가 판정한 단계 상태를 표와 상세 증거로 표시한다. */
"use strict";

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
  for (const c of (children ? (Array.isArray(children) ? children : [children]) : [])) {
    if (c != null && c !== false) e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return e;
}
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

const STAGE_LABELS = [["design", "설계"], ["brief", "지시서"], ["gate1", "GATE 1"], ["claim", "클레임"], ["delegate", "위임"], ["review", "리뷰"], ["gate2", "GATE 2"], ["close", "종료"]];
const STATE_GLYPH = { measured: "●", inferred: "◪", active: "◐", missing: "○", skipped: "⚠" };
const FLOW_STATUS = { active: ["◐", "진행 중"], closed: ["✓", "종료"], orphan: ["⚠", "고아"] };
const SEVERITY_STYLE = {
  error: { dot: "var(--danger)", border: "var(--danger)", bg: "var(--danger-bg)" },
  warn: { dot: "var(--warn)", border: "var(--warn)", bg: "var(--warn-bg)" },
  info: { dot: "var(--muted)", border: "var(--border-2)", bg: "transparent" },
};
const state = { data: null, project: null, openPhase: null, theme: "dark" };

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) { let detail = res.status + " " + res.statusText; try { detail = (await res.json()).detail?.message || detail; } catch (_e) {} throw new Error(detail); }
  return res.json();
}
function apiURL() { return state.project ? "/api/flow?project=" + encodeURIComponent(state.project) : "/api/flow"; }

function warningGroups(list) {
  return (list || []).map((entry) => typeof entry === "string"
    ? { severity: "warn", count: 1, summary: entry, items: [entry] }
    : entry);
}
function renderWarnings(list) {
  const banner = $("warnings-banner");
  const groups = warningGroups(list);
  clear(banner);
  if (!groups.length) return banner.classList.add("hidden");
  const tone = SEVERITY_STYLE[groups[0].severity] || SEVERITY_STYLE.warn;
  banner.style.borderColor = tone.border;
  banner.style.background = tone.bg;
  for (const group of groups) {
    const style = SEVERITY_STYLE[group.severity] || SEVERITY_STYLE.warn;
    const row = el("div", { style: { padding: "3px 0" } });
    const head = el("div", { style: { display: "flex", alignItems: "center", gap: "7px" } });
    head.appendChild(el("span", { textContent: "●", style: { color: style.dot } }));
    head.appendChild(el("span", { textContent: group.summary || "경고" }));
    if (group.count > 1) head.appendChild(el("span", { className: "badge", textContent: group.count + "건" }));
    row.appendChild(head);
    if ((group.items || []).length > 1) {
      const detail = el("ul", { style: { margin: "4px 0 2px 20px", color: "var(--text-2)" } });
      detail.classList.add("hidden");
      for (const item of group.items) detail.appendChild(el("li", { textContent: item }));
      head.setAttribute("role", "button"); head.setAttribute("tabindex", "0"); head.style.cursor = "pointer";
      head.addEventListener("click", () => detail.classList.toggle("hidden"));
      head.addEventListener("keydown", activateOnKey);
      row.appendChild(detail);
    }
    banner.appendChild(row);
  }
  banner.classList.remove("hidden");
}
function renderProjects() {
  const wrap = $("project-selector-wrap"), selector = $("project-selector"), projects = state.data.projects || [];
  if (!projects.length) return wrap.classList.add("hidden");
  clear(selector);
  for (const item of projects) selector.appendChild(el("option", { value: item.project, textContent: item.project }));
  state.project = state.project || state.data.project || selector.value;
  selector.value = state.project;
  selector.onchange = () => { state.project = selector.value; state.openPhase = null; load(); };
  wrap.classList.remove("hidden");
}
function stageCell(cell) {
  const stateName = cell && cell.state || "missing";
  return el("td", { className: "cell-" + stateName, title: cell && cell.evidence || "" }, el("span", { textContent: STATE_GLYPH[stateName] || "?" }));
}
function renderTable() {
  const wrap = $("flow-table-wrap"); clear(wrap);
  const phases = [...(state.data.phases || [])].sort((a, b) => Number(b.active) - Number(a.active));
  if (!phases.length) return wrap.appendChild(el("div", { style: { padding: "32px", textAlign: "center", color: "var(--muted)" }, textContent: "표시할 페이즈가 없습니다" }));
  const thead = el("thead", null, el("tr", null, [el("th", { textContent: "페이즈" }), ...STAGE_LABELS.map(([, label]) => el("th", { textContent: label }))]));
  const tbody = el("tbody");
  for (const row of phases) {
    const byId = new Map((row.stages || []).map((cell) => [cell.id, cell]));
    const tr = el("tr", { className: "flow-row", "aria-selected": String(state.openPhase === row.phase) });
    const status = FLOW_STATUS[row.flow_status] || ["?", row.flow_status || "미상"];
    const phaseButton = el("button", { className: "flow-phase-button mono", type: "button", onclick: () => togglePhase(row.phase), textContent: (row.active ? "◐ " : "") + row.phase + " " + (row.slug || "") });
    tr.appendChild(el("td", null, [phaseButton, el("span", { className: "badge flow-status flow-status-" + (row.flow_status || "unknown"), textContent: status[0] + " " + status[1] })]));
    for (const [id] of STAGE_LABELS) tr.appendChild(stageCell(byId.get(id)));
    tbody.appendChild(tr);
  }
  wrap.appendChild(el("table", { className: "flow-table" }, [thead, tbody]));
}
function activateOnKey(ev) { if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") { ev.preventDefault(); ev.currentTarget.click(); } }
function togglePhase(phase) { state.openPhase = state.openPhase === phase ? null : phase; renderTable(); renderDetail(); }
function sessionLink(session) {
  if (!session) return null;
  return el("a", {
    className: "md-link", style: { marginLeft: "8px", fontSize: "12px" },
    href: "/static/sessions.html#/session/" + session.source + "/" + encodeURIComponent(session.id),
    textContent: "세션 상세 →",
  });
}
function attemptLine(attempt) {
  const bits = ["위임 " + attempt.task + (attempt.agent ? " · " + attempt.agent : "")];
  if (attempt.model) bits.push(attempt.model); if (attempt.exit != null) bits.push("exit " + attempt.exit);
  if (attempt.verdict) bits.push("리뷰 " + attempt.verdict + (attempt.red != null ? " (red " + attempt.red + " orange " + (attempt.orange ?? 0) + ")" : ""));
  if (attempt.started_ms != null) bits.push(attempt.done_ms != null ? "소요 " + Math.round((attempt.done_ms - attempt.started_ms) / 1000) + "초" : "시작 " + new Date(attempt.started_ms).toISOString());
  return el("div", { className: "attempt" }, [el("span", { textContent: bits.join(" → ") }), sessionLink(attempt.session)]);
}
function renderDetail() {
  const panel = $("phase-detail"); clear(panel);
  const row = (state.data.phases || []).find((phase) => phase.phase === state.openPhase);
  if (!row) return panel.classList.add("hidden");
  panel.classList.remove("hidden");
  panel.appendChild(el("div", { className: "display", style: { fontWeight: "600", fontSize: "16px", marginBottom: "10px" }, textContent: "Phase " + row.phase + " · " + (row.slug || "") + " — 단계 증거" }));
  for (const cell of row.stages || []) {
    const label = (STAGE_LABELS.find(([id]) => id === cell.id) || [cell.id, cell.id])[1];
    panel.appendChild(el("div", { style: { fontSize: "12.5px", margin: "2px 0" } }, [el("span", { className: "mono cell-" + cell.state, textContent: (STATE_GLYPH[cell.state] || "?") + " " + label }), el("span", { style: { color: "var(--text-2)", marginLeft: "8px" }, textContent: cell.evidence || "증거 없음" })]));
  }
  if ((row.tasks || []).length) {
    panel.appendChild(el("div", { className: "display", style: { fontWeight: "600", fontSize: "14px", margin: "14px 0 6px" }, textContent: "위임 task" }));
    for (const task of row.tasks) {
      panel.appendChild(el("div", { style: { fontSize: "13px", marginTop: "8px" } }, [el("span", { className: "mono", textContent: "task " + task.task }), task.commit && el("span", { className: "badge", style: { marginLeft: "8px" }, textContent: task.commit }), sessionLink(task.session)]));
      for (const attempt of task.attempts || []) panel.appendChild(attemptLine(attempt));
    }
  }
}
async function load() {
  $("error-state").classList.add("hidden"); $("flow-content").classList.add("hidden"); $("loading-state").classList.remove("hidden");
  try { state.data = await fetchJSON(apiURL()); }
  catch (err) { $("error-detail").textContent = String(err.message || err); $("error-state").classList.remove("hidden"); return; }
  finally { $("loading-state").classList.add("hidden"); }
  renderProjects(); renderWarnings(state.data.warning_groups || state.data.warnings); renderTable(); renderDetail(); $("flow-content").classList.remove("hidden");
}
function applyTheme() { $("app").setAttribute("data-theme", state.theme); $("btn-theme").textContent = state.theme === "dark" ? "☾ Dark" : "☀ Light"; }
function boot() {
  if (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) state.theme = "light";
  applyTheme(); $("btn-theme").addEventListener("click", () => { state.theme = state.theme === "dark" ? "light" : "dark"; applyTheme(); });
  $("btn-retry").addEventListener("click", load); load();
}
boot();

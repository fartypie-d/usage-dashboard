/**
 * progress.js — 진행내역 페이지.
 * 좌: phase × task 격자 / 우: 선택한 task 절의 지시서 본문 (renderMarkdown).
 */
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

const state = {
  index: null,        // /api/progress 응답
  detail: null,       // /api/progress/phase/{n} 응답
  openPhase: null,    // 펼친 phase 번호
  activeTask: null,   // 선택한 task 번호 (null이면 서문)
  project: null,      // null이면 현재 프로젝트(/api/progress 무파라미터)
  theme: "dark",
  quiz: null,   // { phase, transcript: [{role,text}], usage: {input_tokens,output_tokens}, busy, notice }
};

async function fetchJSON(url) {
  try {
    const r = await fetch(url, { signal: AbortSignal.timeout(15000) });
    if (!r.ok) {
      const body = await r.json().catch(() => null);
      const message = body && body.detail && body.detail.message;
      const error = new Error(message || "HTTP " + r.status);
      error.warnings = body && body.detail && body.detail.warnings;
      error.warning_groups = body && body.detail && body.detail.warning_groups;
      throw error;
    }
    return await r.json();
  } catch (err) {
    if (err.name === "TimeoutError" || err.name === "AbortError") {
      throw new Error("요청 시간 초과 (15초): " + url);
    }
    throw err;
  }
}

const QUIZ_TIMEOUT_MS = 120000;   // LLM 스트림은 15초 한도보다 길다

/** POST + SSE 수신 — EventSource는 GET 전용이라 fetch 스트림으로 읽는다. */
async function postSSE(url, body, onEvent) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(QUIZ_TIMEOUT_MS),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => null);
    throw new Error((detail && detail.detail && (detail.detail.message || detail.detail)) || "HTTP " + r.status);
  }
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      let event = "message", data = "";
      for (const line of chunk.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (data) onEvent(event, JSON.parse(data));
    }
  }
}

async function postJSON(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(QUIZ_TIMEOUT_MS),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => null);
    throw new Error((detail && detail.detail && (detail.detail.message || detail.detail)) || "HTTP " + r.status);
  }
  return await r.json();
}

function showError(message) {
  $("error-state").classList.remove("hidden");
  $("error-detail").textContent = message;
  $("progress-grid").classList.add("hidden");
}

const SEVERITY_STYLE = {
  error: { dot: "var(--danger)", border: "var(--danger)", bg: "var(--danger-bg)" },
  warn:  { dot: "var(--warn)",   border: "var(--warn)",   bg: "var(--warn-bg)" },
  info:  { dot: "var(--muted)",  border: "var(--border-2)", bg: "transparent" },
};
const SEVERITY_RANK = { error: 0, warn: 1, info: 2 };

/** 구버전 정적 파일과 새 API가 섞여도 백지가 되지 않게 문자열도 받는다. */
function asGroups(list) {
  return (list || []).map((entry) =>
    typeof entry === "string"
      ? { code: entry, severity: "warn", count: 1, summary: entry, items: [entry] }
      : entry
  );
}

/** 같은 code 끼리 items 를 합친다. 같은 그룹을 두 번 넣어도 count 가 늘지 않는다. */
function mergeGroups(a, b) {
  const byCode = new Map();
  for (const group of [...asGroups(a), ...asGroups(b)]) {
    const prev = byCode.get(group.code);
    if (!prev) {
      byCode.set(group.code, { ...group, items: [...(group.items || [])] });
      continue;
    }
    const items = [...new Set([...prev.items, ...(group.items || [])])];
    // summary 는 항목이 더 많은 쪽 문구를 쓴다 — 건수의 진실은 배지(count)다.
    // count 는 서버가 보고한 값이 items 샘플보다 클 수 있으므로 max 를 쓴다.
    // 같은 그룹을 두 번 넣어도 count 가 늘지 않는 멱등성을 보장한다.
    const richer = (group.items || []).length > prev.items.length ? group : prev;
    byCode.set(group.code, { ...prev, summary: richer.summary, items, count: Math.max(prev.count, group.count, items.length) });
  }
  return [...byCode.values()].sort(
    (x, y) =>
      (SEVERITY_RANK[x.severity] ?? 1) - (SEVERITY_RANK[y.severity] ?? 1) ||
      x.code.localeCompare(y.code)
  );
}

function renderWarnings(list) {
  const banner = $("warnings-banner");
  const groups = mergeGroups(list, []);
  if (!groups.length) { banner.classList.add("hidden"); return; }

  clear(banner);
  const worst = groups[0].severity;
  const tone = SEVERITY_STYLE[worst] || SEVERITY_STYLE.warn;
  banner.style.borderColor = tone.border;
  banner.style.background = tone.bg;

  for (const group of groups) {
    const style = SEVERITY_STYLE[group.severity] || SEVERITY_STYLE.warn;
    const row = el("div", { style: { padding: "3px 0" } });
    const head = el("div", {
      style: { display: "flex", alignItems: "center", gap: "7px" },
    });
    head.appendChild(el("span", { textContent: "●", style: { color: style.dot } }));
    head.appendChild(el("span", { textContent: group.summary }));
    if (group.count > 1) {
      head.appendChild(el("span", { className: "badge", textContent: group.count + "건" }));
    }
    row.appendChild(head);

    if ((group.items || []).length > 1) {
      const detail = el("ul", {
        style: { margin: "4px 0 2px 20px", color: "var(--text-2)" },
      });
      detail.classList.add("hidden");
      for (const item of group.items) {
        detail.appendChild(el("li", { textContent: item }));
      }
      head.setAttribute("role", "button");
      head.setAttribute("tabindex", "0");
      head.style.cursor = "pointer";
      head.addEventListener("click", () => detail.classList.toggle("hidden"));
      head.addEventListener("keydown", activateOnKey);
      row.appendChild(detail);
    }
    banner.appendChild(row);
  }
  banner.classList.remove("hidden");
}

/** div[role=button]은 Enter/Space가 자동으로 동작하지 않는다 — 직접 붙인다. */
function activateOnKey(ev) {
  if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") {
    ev.preventDefault();
    ev.currentTarget.click();
  }
}

/* ── 좌: phase × task 격자 ──────────────────────────────────────── */

function verdictChip(task) {
  if (task.verdict === "pass") {
    return el("span", {
      className: "badge",
      style: { border: "1px solid var(--ok)", color: "var(--ok)" },
      title: task.verdict_raw || "승인",
      textContent: "✓",
    });
  }
  if (task.verdict === "fail") {
    return el("span", {
      className: "badge",
      style: { border: "1px solid var(--danger)", color: "var(--danger)" },
      title: task.verdict_raw || "반려",
      textContent: "✗",
    });
  }
  // 판정을 모르는 것과 판정이 없는 것을 구분하지 않고 지어내지 않는다.
  return el("span", {
    className: "badge",
    style: { border: "1px dashed var(--border-2)", color: "var(--muted)" },
    textContent: "판정 없음",
  });
}

const DEBT_STYLE = {
  repaid:   { color: "var(--ok)",     label: "부채 상환" },
  partial:  { color: "var(--warn)",   label: "부채 일부" },
  unrepaid: { color: "var(--danger)", label: "부채 미상환" },
};

function debtBadge(phase) {
  const debt = phase.debt;
  if (!debt || !DEBT_STYLE[debt.level]) return null;
  const style = DEBT_STYLE[debt.level];
  return el("span", {
    className: "badge",
    style: { border: "1px solid " + style.color, color: style.color },
    title: (debt.signals || []).join(" · ") || "신호 없음",
    textContent: style.label,
  });
}

function phaseMetaLine(phase) {
  const cost = phase.cost != null
    ? "$" + phase.cost.toFixed(2)
    : (phase.cost_raw ? "비용 " + phase.cost_raw : null);
  return [phase.date, phase.status, cost].filter(Boolean).join(" · ");
}

function phaseRow(phase) {
  const tasks = phase.tasks || [];
  const done = tasks.filter((t) => t.verdict === "pass").length;
  const head = el("div", {
    className: "phase-row",
    role: "button",
    tabindex: "0",
    "aria-expanded": String(state.openPhase === phase.phase),
    onclick: () => togglePhase(phase.phase),
    onkeydown: activateOnKey,
  }, [
    el("div", { style: { display: "flex", alignItems: "baseline", gap: "9px", flexWrap: "wrap" } }, [
      el("span", { className: "mono", style: { color: "var(--accent)", fontWeight: "600" }, textContent: "Phase " + phase.phase }),
      el("span", { style: { fontWeight: "600", fontSize: "13.5px", flex: "1", minWidth: "0" }, textContent: phase.summary || "(요약 없음)" }),
      phase.active === true ? el("span", { className: "badge", style: { border: "1px solid var(--warn)", color: "var(--warn)" }, textContent: "진행 중" }) : null,
      debtBadge(phase),
      el("span", { className: "mono", style: { fontSize: "11.5px", color: "var(--muted)" }, textContent: done + "/" + tasks.length + " task" }),
    ]),
    el("div", { className: "mono", style: { fontSize: "11px", color: "var(--muted)", marginTop: "3px" }, textContent: phaseMetaLine(phase) }),
  ]);

  if (state.openPhase !== phase.phase) return head;

  const chips = tasks.map((t) => el("div", {
    className: "task-chip",
    role: "button",
    tabindex: "0",
    "aria-selected": String(state.activeTask === t.n),
    onclick: () => selectTask(phase.phase, t.n),
    onkeydown: activateOnKey,
  }, [
    el("span", { className: "mono", style: { color: "var(--muted)" }, textContent: t.label || String(t.n) }),
    el("span", { textContent: t.title || "(제목 없음)" }),
    verdictChip(t),
    t.commit ? el("span", { className: "mono", style: { fontSize: "10.5px", color: "var(--muted)" }, textContent: t.commit }) : null,
  ]));

  return el("div", null, [
    head,
    el("div", { style: { display: "flex", flexWrap: "wrap", gap: "6px", padding: "0 14px 14px" } },
      chips.length ? chips : [el("span", { className: "mono", style: { fontSize: "12px", color: "var(--muted)" }, textContent: "task 절이 없습니다" })]),
  ]);
}

function renderIndex() {
  const list = $("phase-list");
  clear(list);
  const phases = state.index.phases || [];
  $("empty-state").classList.add("hidden");
  $("progress-grid").classList.remove("hidden");
  if (!phases.length) {
    $("empty-state").classList.remove("hidden");
    $("progress-grid").classList.add("hidden");
    return;
  }
  list.appendChild(el("div", { style: { padding: "14px 16px" } }, [
    el("span", { className: "display", style: { fontWeight: "600", fontSize: "15px" }, textContent: "Phase " + phases.length + "개" }),
  ]));
  for (const p of phases) list.appendChild(phaseRow(p));
}

function renderProjectSelector() {
  const wrap = $("project-selector-wrap");
  const selector = $("project-selector");
  const projects = state.index && Array.isArray(state.index.projects) ? state.index.projects : [];
  if (!projects.length) {
    wrap.classList.add("hidden");
    return;
  }
  clear(selector);
  selector.appendChild(el("option", { value: "", textContent: "현재 프로젝트" }));
  for (const item of projects) {
    if (!item || !item.project) continue;
    selector.appendChild(el("option", { value: item.project, textContent: item.project }));
  }
  selector.value = state.project || "";
  selector.onchange = () => switchProject(selector.value || null);
  wrap.classList.remove("hidden");
}

async function switchProject(project) {
  if (project === state.project) return;
  const selector = $("project-selector");
  const loadState = $("project-load-state");
  selector.disabled = true;
  loadState.textContent = "프로젝트 진행내역 불러오는 중…";
  try {
    const index = await fetchJSON(project
      ? "/api/progress?project=" + encodeURIComponent(project)
      : "/api/progress");
    state.project = project;
    state.index = index;
    detailReqSeq++;
    state.detail = null;
    state.openPhase = null;
    state.activeTask = null;
    if (location.hash) location.hash = "";
    renderWarnings(index.warning_groups || index.warnings);
    renderProjectSelector();
    renderIndex();
    renderDoc();
    loadState.textContent = "";
  } catch (err) {
    selector.value = state.project || "";
    renderWarnings(mergeGroups(
      (state.index && (state.index.warning_groups || state.index.warnings)) || [],
      err.warning_groups || err.warnings || []
    ));
    loadState.textContent = "프로젝트 진행내역을 불러오지 못했습니다: " + err.message;
  } finally {
    selector.disabled = false;
  }
}

/* ── 우: 문서 본문 ──────────────────────────────────────────────── */

function renderDoc() {
  const view = $("doc-view");
  clear(view);
  if (!state.detail) {
    view.appendChild(el("div", { style: { color: "var(--muted)", fontSize: "13px" }, textContent: "왼쪽에서 phase를 선택하세요." }));
    return;
  }
  const d = state.detail;
  const task = state.activeTask == null ? null : d.tasks.find((t) => t.n === state.activeTask);
  view.appendChild(el("div", {
    className: "mono",
    style: { fontSize: "11.5px", color: "var(--muted)", marginBottom: "10px" },
    textContent: task
      ? "Phase " + d.phase + " · Task " + (task.label || task.n)
      : "Phase " + d.phase + " · 서문",
  }));
  view.appendChild(renderMarkdown(task ? task.nodes : d.intro));

  if (!task && d.review.length) {
    view.appendChild(el("hr", { className: "md-hr" }));
    view.appendChild(el("div", {
      className: "mono",
      style: { fontSize: "11.5px", color: "var(--muted)", marginBottom: "10px" },
      textContent: "검수 문서",
    }));
    view.appendChild(renderMarkdown(d.review));
  }
  if (!task) view.appendChild(quizSection(d));
}

/* ── 쪽지시험 (인지부채 상환) ───────────────────────────────────── */

/** 기록 한 줄 요약 — 갭 해소율 + 재시험 필요 여부만. 미상 값은 지어내지 않는다. */
function quizMetaLine(record) {
  const found = record.gaps_found;
  const unresolved = record.gaps_unresolved;
  const parts = [record.date || record.name];
  if (typeof found === "number" && typeof unresolved === "number") {
    if (found === 0) {
      parts.push("발견된 갭 없음");
    } else {
      const resolved = found - unresolved;
      parts.push("갭 해소 " + resolved + "/" + found + " (" + Math.round((resolved * 100) / found) + "%)");
    }
    parts.push(unresolved > 0 ? "재시험 필요" : "재시험 불필요");
  } else {
    parts.push("판정 미상");
  }
  return parts.join(" · ");
}

function quizSection(detail) {
  const wrap = el("div", null, [
    el("hr", { className: "md-hr" }),
    el("div", { className: "mono", style: { fontSize: "11.5px", color: "var(--muted)", marginBottom: "10px" }, textContent: "쪽지시험 — 인지부채 상환" }),
  ]);

  for (const record of detail.quizzes || []) {
    const line = el("div", { className: "quiz-record mono", textContent: quizMetaLine(record) });
    if ((record.gaps_unresolved ?? 0) > 0) line.style.color = "var(--warn)";
    wrap.appendChild(line);
  }

  const llmStatus = (state.index && state.index.llm) || { available: false, reason: null };
  if (state.quiz && state.quiz.phase === detail.phase) {
    wrap.appendChild(quizChat());
  } else {
    const startBtn = el("button", {
      className: "btn",
      style: { marginTop: "10px" },
      textContent: "시험 시작",
      onclick: () => startQuiz(detail.phase),
    });
    if (!llmStatus.available) {
      startBtn.disabled = true;
      startBtn.style.opacity = "0.5";
      startBtn.title = llmStatus.reason || "LLM 사용 불가";
      wrap.appendChild(el("div", { className: "mono", style: { fontSize: "11.5px", color: "var(--muted)", margin: "8px 0" }, textContent: llmStatus.reason || "" }));
    }
    wrap.appendChild(startBtn);
  }
  return wrap;
}

function quizChat() {
  const quizState = state.quiz;
  const prevInput = document.getElementById("quiz-input");
  const preserved = prevInput ? prevInput.value : "";
  const log = el("div", { style: { margin: "10px 0" } });
  for (const turn of quizState.transcript) {
    log.appendChild(el("div", {
      className: "quiz-turn " + (turn.role === "assistant" ? "q" : "a"),
      textContent: turn.text,
    }));
  }
  if (quizState.notice) {
    log.appendChild(el("div", { className: "mono", style: { fontSize: "12px", color: "var(--warn)", margin: "6px 0" }, textContent: quizState.notice }));
  }
  const input = el("textarea", { className: "quiz-input", id: "quiz-input", placeholder: "답변을 입력하세요… (답은 당신의 입에서 나와야 합니다)" });
  input.value = preserved;
  const send = el("button", { className: "btn", textContent: "제출하기", onclick: () => sendAnswer(input) });
  const finish = el("button", { className: "btn", textContent: "시험 종료", style: { marginLeft: "auto" }, onclick: () => finishQuiz() });
  if (quizState.busy) { send.disabled = true; finish.disabled = true; }
  return el("div", null, [
    log,
    input,
    el("div", { style: { display: "flex", gap: "8px", marginTop: "8px" } }, [send, finish]),
  ]);
}

function quizBody(quizState, extra) {
  return Object.assign({
    project: state.project || undefined,
    phase: quizState.phase,
  }, extra);
}

async function streamQuestion(url, extra) {
  const quizState = state.quiz;   // 세션 캡처 — 이 스트림은 이 세션에만 쓴다 (늦은 state.quiz 참조 금지)
  quizState.busy = true;
  quizState.notice = null;
  renderDoc();
  // 요청 본문은 자리표시자 push "전에" 스냅샷한다 — 전사 배열 참조를 그대로
  // 직렬화하면 아래 빈 assistant 턴이 섞여 서버 검증(400: text 비어 있음)에 걸린다.
  const body = quizBody(quizState, extra);
  if (body.transcript) {
    body.transcript = body.transcript.map((t) => ({ role: t.role, text: t.text }));
  }
  const turn = { role: "assistant", text: "" };
  quizState.transcript.push(turn);
  try {
    await postSSE(url, body, (event, data) => {
      if (event === "delta") {
        turn.text += data.text;
        if (state.quiz === quizState) renderDoc();   // textNode 재생성 — innerHTML 미사용
      } else if (event === "done") {
        quizState.usage.input_tokens += data.usage.input_tokens;
        quizState.usage.output_tokens += data.usage.output_tokens;
      } else if (event === "error") {
        quizState.notice = data.message;
      }
    });
    if (!turn.text) {
      quizState.transcript.pop();   // 빈 질문은 남기지 않는다
      if (!quizState.notice) {
        quizState.notice = "모델이 빈 응답을 반환했습니다 — 다시 시도하세요 (토큰 한도/거부 가능성)";
      }
    }
  } catch (err) {
    quizState.transcript.pop();
    quizState.notice = "요청 실패: " + err.message;
  } finally {
    quizState.busy = false;
    if (state.quiz === quizState) renderDoc();
  }
}

async function startQuiz(phase) {
  state.quiz = { phase, transcript: [], usage: { input_tokens: 0, output_tokens: 0 }, busy: false, notice: null };
  await streamQuestion("/api/quiz/start", {});
}

async function sendAnswer(input) {
  const text = (input.value || "").trim();
  if (!text || state.quiz.busy) return;
  const quizState = state.quiz;
  input.value = "";   // 전송한 답변을 입력창에서 비운다 — 재렌더 보존 로직이 빈 값을 이어간다
  quizState.transcript.push({ role: "user", text });
  await streamQuestion("/api/quiz/reply", { transcript: quizState.transcript });
}

async function finishQuiz() {
  const quizState = state.quiz;   // 세션 캡처 — 이 종료 흐름은 이 세션에만 쓴다
  if (quizState.busy || !quizState.transcript.length) return;
  quizState.busy = true;
  quizState.notice = "평가·저장 중…";
  renderDoc();
  try {
    const result = await postJSON("/api/quiz/finish", quizBody(quizState, {
      transcript: quizState.transcript,
      usage: quizState.usage,
    }));
    if (result.saved) {
      const phase = quizState.phase;
      state.quiz = null;
      // 지표·기록을 다시 읽는다 — 저장된 시험이 배지와 기록 목록에 바로 반영되게.
      state.index = await fetchJSON(state.project
        ? "/api/progress?project=" + encodeURIComponent(state.project)
        : "/api/progress");
      renderIndex();
      await loadPhase(phase, null);
    } else {
      quizState.busy = false;
      const summary = result.gaps && result.gaps.summary;
      quizState.notice = "기록 미저장: " + (result.error || "알 수 없음") + (summary ? " — 평가 요약: " + summary : "");
      if (state.quiz === quizState) renderDoc();
    }
  } catch (err) {
    quizState.busy = false;
    quizState.notice = "시험 종료 실패: " + err.message;
    if (state.quiz === quizState) renderDoc();
  }
}

/* ── 라우팅 (#/phase/11/task/3) ─────────────────────────────────── */

function parseHash() {
  const m = (location.hash || "").match(/^#\/phase\/(\d+)(?:\/task\/(\d+))?$/);
  if (!m) return null;
  return { phase: Number(m[1]), task: m[2] ? Number(m[2]) : null };
}

let detailReqSeq = 0;   // 연타 경합 가드 — 최신 요청의 응답만 반영한다 (sessions.js 관례)

async function loadPhase(number, taskNumber) {
  const seq = ++detailReqSeq;
  try {
    const detail = await fetchJSON("/api/progress/phase/" + number + (state.project ? "?project=" + encodeURIComponent(state.project) : ""));
    if (seq !== detailReqSeq) return;   // 더 새 요청이 이미 떠났다
    state.detail = detail;
    state.openPhase = number;
    state.activeTask = taskNumber;
    renderIndex();
    // 상세 응답의 경고(마크다운 렌더 실패·리뷰 문서 읽기 실패 등)도 화면에 올린다.
    // 목록 경고만 그리면 상세에서 생긴 신호가 조용히 사라진다.
    renderWarnings(mergeGroups(
      (state.index && (state.index.warning_groups || state.index.warnings)) || [],
      detail.warning_groups || detail.warnings || []
    ));
    renderDoc();
  } catch (err) {
    if (seq !== detailReqSeq) return;   // 버려진 요청의 실패로 살아있는 화면을 지우지 않는다
    renderWarnings(mergeGroups(
      (state.index && (state.index.warning_groups || state.index.warnings)) || [],
      err.warning_groups || err.warnings || []
    ));
    showError(err.message);
  }
}

function togglePhase(number) {
  // 해시가 단일 진실 원천이다 — 상태 리셋·재렌더는 onHashChange 한 곳에서만 한다.
  // (여기서 같이 하면 hashchange 로 두 번 그린다)
  location.hash = state.openPhase === number ? "" : "#/phase/" + number;
}

function selectTask(phase, taskNumber) {
  location.hash = "#/phase/" + phase + "/task/" + taskNumber;
}

function onHashChange() {
  const route = parseHash();
  if (!route) {
    state.openPhase = null;
    state.activeTask = null;
    state.detail = null;
    renderIndex();
    renderDoc();
    return;
  }
  loadPhase(route.phase, route.task);
}

/* ── theme ──────────────────────────────────────────────────────── */

function applyTheme() {
  $("app").setAttribute("data-theme", state.theme);
  $("btn-theme").textContent = state.theme === "dark" ? "☾ Dark" : "☀ Light";
  try { localStorage.setItem("ud-theme", state.theme); } catch { /* 저장 실패는 무시 */ }
}

/* ── boot ───────────────────────────────────────────────────────── */

async function boot() {
  try { state.theme = localStorage.getItem("ud-theme") || "dark"; } catch { /* 무시 */ }
  applyTheme();
  $("btn-theme").addEventListener("click", () => {
    state.theme = state.theme === "dark" ? "light" : "dark";
    applyTheme();
  });
  $("btn-retry").addEventListener("click", () => { location.reload(); });
  window.addEventListener("hashchange", onHashChange);

  try {
    state.index = await fetchJSON("/api/progress");
  } catch (err) {
    showError(err.message);
    return;
  }
  renderWarnings(state.index.warning_groups || state.index.warnings);
  renderProjectSelector();
  renderIndex();
  const route = parseHash();
  if (route) await loadPhase(route.phase, route.task);
  else renderDoc();
}

boot();

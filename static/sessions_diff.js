// 작업 브라우저 파일 패널 렌더러 — 렌더 전용. fetch·라우팅·상태 없음.

function diffStatLine(diffStat) {
  const stat = diffStat || { files: 0, additions: 0, deletions: 0 };
  return el("div", { className: "mono", style: { display: "flex", gap: "10px", alignItems: "center", fontSize: "12px" } }, [
    el("span", { textContent: stat.files + "개 파일 변경", style: { color: "var(--text-2)" } }),
    el("span", { textContent: "+" + stat.additions, style: { color: "var(--ok)" } }),
    el("span", { textContent: "-" + stat.deletions, style: { color: "var(--danger)" } }),
    stat.truncated ? el("span", { className: "badge", textContent: "일부 생략" }) : null
  ]);
}

function hunkBlock(hunk) {
  const rows = [el("div", { className: "diff-hunk-header mono", textContent: hunk.header })];
  for (const line of hunk.lines) {
    const head = line.charAt(0);
    const cls = head === "+" ? "diff-add" : head === "-" ? "diff-del" : "diff-ctx";
    rows.push(el("div", { className: "diff-line mono " + cls, textContent: line }));
  }
  if (hunk.truncated) {
    rows.push(el("div", { className: "diff-line mono diff-ctx", textContent: "… 줄 수 상한으로 이후 생략" }));
  }
  return el("div", { style: { overflowX: "auto" } }, rows);
}

function changeBlock(change, onJumpToTurn) {
  const header = el("div", { style: { display: "flex", flexWrap: "wrap", alignItems: "center", gap: "8px", padding: "8px 0" } }, [
    el("span", { className: "badge", textContent: change.tool }),
    el("span", { className: "mono", textContent: "+" + change.additions, style: { color: "var(--ok)", fontSize: "11.5px" } }),
    el("span", { className: "mono", textContent: "-" + change.deletions, style: { color: "var(--danger)", fontSize: "11.5px" } }),
    change.replace_all ? el("span", { className: "badge", textContent: "replace_all" }) : null,
    change.truncated ? el("span", { className: "badge", textContent: "생략됨" }) : null,
    change.failed ? el("span", { className: "badge", style: { color: "var(--danger)" }, textContent: "실패" }) : null,
    onJumpToTurn
      ? el("button", {
          className: "btn",
          textContent: "→ 대화 " + (change.turn + 1) + "번째",
          onClick: function () { onJumpToTurn(change.turn); }
        })
      : null
  ]);
  const hunks = Array.isArray(change.hunks) ? change.hunks : [];
  const body = hunks.length
    ? hunks.map(hunkBlock)
    : [el("div", { className: "excerpt", textContent: "(diff 본문이 없습니다)", style: { color: "var(--muted)" } })];
  return el("div", { style: { borderTop: "1px solid var(--border)" } }, [header].concat(body));
}

function fileRow(file, index, isOpen, onToggleFile, onJumpToTurn, turnCount) {
  const head = el("div", {
    className: "file-row",
    role: "button",
    tabindex: "0",
    onClick: function () { onToggleFile(index); },
    onKeydown: function (e) {
      if (e.key === "Enter" || e.key === " ") {
        if (e.key === " ") e.preventDefault();
        onToggleFile(index);
      }
    }
  }, [
    el("span", { className: "mono", textContent: isOpen ? "▾" : "▸", style: { color: "var(--muted)" } }),
    el("span", { className: "mono", textContent: file.path, style: { flex: "1 1 240px", wordBreak: "break-all" } }),
    file.change_count > 1 ? el("span", { className: "badge", textContent: file.change_count + "회 편집" }) : null,
    el("span", { className: "mono", textContent: "+" + file.additions, style: { color: "var(--ok)", fontSize: "11.5px" } }),
    el("span", { className: "mono", textContent: "-" + file.deletions, style: { color: "var(--danger)", fontSize: "11.5px" } })
  ]);
  const children = [head];
  if (isOpen) {
    const changes = Array.isArray(file.changes) ? file.changes : [];
    const tc = (typeof turnCount === "number") ? turnCount : Infinity;
    for (const change of changes) {
      // 잘린 턴 목록에 없는 인덱스를 가진 변경은 점프 버튼을 숨긴다 (앵커 없음).
      const jumpFn = (onJumpToTurn && typeof change.turn === "number" && change.turn < tc)
        ? onJumpToTurn : null;
      children.push(changeBlock(change, jumpFn));
    }
  }
  return el("div", { style: { borderTop: "1px solid var(--border)" } }, children);
}

function renderFilesPanel(data, openIndex, onToggleFile, onJumpToTurn) {
  const files = (data && data.files) || [];
  // TURNS_MAX 잘림 때 대상 앵커가 없어 클릭이 조용히 무시되는 문제 방지.
  // 실제로 존재하는 턴 수를 알아야 범위 바깥 인덱스를 가진 변경의 점프 버튼을 숨길 수 있다.
  const turnCount = (data && Array.isArray(data.turns)) ? data.turns.length : Infinity;
  if (!files.length) {
    return el("div", { className: "panel" }, [
      el("div", { className: "excerpt", textContent: "이 세션에는 재구성할 수 있는 파일 변경이 없습니다. 대화 탭에서 진행 내용을 확인하세요.", style: { color: "var(--muted)" } })
    ]);
  }
  const rows = files.map(function (file, i) {
    return fileRow(file, i, i === openIndex, onToggleFile, onJumpToTurn, turnCount);
  });
  return el("div", { className: "panel", style: { padding: "0" } }, [
    el("div", { style: { padding: "14px 16px" } }, [diffStatLine(data.diff_stat)])
  ].concat(rows));
}

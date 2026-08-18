"""세션별 위임 흐름(섹션 06)을 재구성하는 순수 함수.

`Record` 목록만 받아 "부모 Claude 세션 1개 = 흐름 1개" 단위로 트리를 세우고,
DFS로 펼친 자식 배열을 돌려준다. 파일 I/O도 전역 상태도 없다.

핵심 결정:

* 노드 식별자는 ``session_id``가 아니다. Claude 서브에이전트 로그는 부모의
  ``sessionId``를 그대로 물려받으므로(실측 392/393) 그걸로는 부모와 자식이
  구분되지 않는다. 파일 경로가 유일 식별자다. 반대로 opencode는 ``source_file``이
  DB 경로 하나로 고정이라 ``session_id``를 쓴다.
* 부모 링크의 1차 신호는 파일 경로(``parent_session_id``)이고, 디스패처가 그
  자체로 서브에이전트일 때만 그쪽이 진짜 부모다 — 이게 유일한 2-hop 신호다.
* 트리는 중첩 dict가 아니라 ``depth``/``parent_node_id``를 단 평탄 배열로 낸다.
  프런트가 간트 행을 그대로 순회할 수 있고, JSON 깊이도 예측 가능해진다.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from app.metrics.common import record_cost, total_tokens
from app.pricing import cost_for
from app.sources.claude_jsonl import Record

MAX_DEPTH = 5

__all__ = ["MAX_DEPTH", "flows"]


@dataclass
class _Node:
    """가변 작업용 노드. 모듈 밖으로 새어나가지 않는다."""

    node_id: str
    session_id: str
    project: str
    cwd: str | None
    source: str
    agent: str | None
    parent_session_id: str | None
    dispatcher_file: str | None
    start: datetime
    end: datetime
    cost_usd: float
    tokens: int
    turns: int
    models: dict[str, int]
    children: list[_Node] = field(default_factory=list)
    inferred: bool = False
    setup_cost_usd: float = 0.0


def _node_id(rec: Record) -> str:
    # Claude 서브에이전트는 부모의 sessionId를 물려받으므로 session_id로는
    # 부모와 자식이 구분되지 않는다. 파일 경로가 유일한 식별자다.
    # 반대로 opencode는 source_file이 DB 경로 하나로 고정이라 session_id를 쓴다.
    return rec.source_file if rec.source == "claude" else rec.session_id


def _fold(records: list[Record]) -> dict[str, _Node]:
    """레코드를 노드 단위로 접는다."""
    nodes: dict[str, _Node] = {}
    for rec in records:
        nid = _node_id(rec)
        node = nodes.get(nid)
        tokens = total_tokens(rec)
        # cost_for 경고는 버린다. _fold는 이미 record_cost(common.py:59)로 같은
        # 레코드의 경고를 같은 방식으로 버리고 있어, 여기서만 모아봤자 cost_usd의
        # 동일 한계는 그대로다 — 일관성을 맞춘다.
        # 미등록 모델 경고는 /api/delegation 핸들러(main.py get_delegation)가
        # 필터된 레코드의 모델 이름을 중복 제거한 뒤 cost_for(model, 0, 0, 0, 0)로
        # 별도 수집해 응답 warnings에 합류시킨다.
        cw_cost, _ = cost_for(rec.model, 0, 0, 0, rec.cache_write_tokens)
        if node is None:
            nodes[nid] = _Node(
                node_id=nid,
                session_id=rec.session_id,
                project=rec.project,
                cwd=rec.cwd,
                source=rec.source,
                agent=rec.agent,
                parent_session_id=rec.parent_session_id,
                dispatcher_file=rec.dispatcher_file,
                start=rec.timestamp,
                end=rec.timestamp,
                cost_usd=record_cost(rec),
                tokens=tokens,
                turns=1,
                models={rec.model: tokens},
                setup_cost_usd=cw_cost,
            )
            continue
        node.start = min(node.start, rec.timestamp)
        node.end = max(node.end, rec.timestamp)
        node.cost_usd += record_cost(rec)
        node.setup_cost_usd += cw_cost
        node.tokens += tokens
        node.turns += 1
        node.models[rec.model] = node.models.get(rec.model, 0) + tokens
        # 첫 레코드에 없던 메타데이터는 뒤 레코드에서 채운다.
        if node.agent is None:
            node.agent = rec.agent
        if node.cwd is None:
            node.cwd = rec.cwd
        if node.parent_session_id is None:
            node.parent_session_id = rec.parent_session_id
        if node.dispatcher_file is None:
            node.dispatcher_file = rec.dispatcher_file
    return nodes


def _parent_of(
    node: _Node, nodes_by_id: dict[str, _Node], roots_by_session: dict[str, _Node]
) -> _Node | None:
    # 1) 디스패처가 그 자체로 서브에이전트면 그쪽이 진짜 부모다 — 유일한 2-hop 신호.
    disp = nodes_by_id.get(node.dispatcher_file) if node.dispatcher_file else None
    if disp is not None and disp.parent_session_id is not None:
        return disp
    # 2) 그 외에는 경로에서 읽은 부모 세션. 실측 393/393으로 신뢰할 수 있다.
    if node.parent_session_id is not None:
        return roots_by_session.get(node.parent_session_id)
    return None


def _attach_claude(
    nodes: dict[str, _Node], roots_by_session: dict[str, _Node]
) -> int:
    """Claude 서브에이전트를 부모에 붙이고, 못 붙인 고아 수를 반환한다."""
    orphans = 0
    for node in nodes.values():
        if node.source != "claude" or node.parent_session_id is None:
            continue
        parent = _parent_of(node, nodes, roots_by_session)
        if parent is None or parent is node:
            orphans += 1
            continue
        parent.children.append(node)
    return orphans


def _walk(root: _Node) -> tuple[list[tuple[_Node, int, str]], int, int]:
    """루트 아래를 DFS로 펼쳐 ``(노드, depth, parent_node_id)`` 목록을 만든다.

    반환: ``(평탄 목록, 깊이 초과로 잘린 수, 순환으로 건너뛴 수)``.
    """
    flat: list[tuple[_Node, int, str]] = []
    visited: set[str] = {root.node_id}
    truncated = 0
    cycles = 0

    def visit(parent: _Node, depth: int) -> None:
        nonlocal truncated, cycles
        for child in sorted(parent.children, key=lambda n: (n.start, n.node_id)):
            if child.node_id in visited:
                cycles += 1
                continue
            if depth > MAX_DEPTH:
                truncated += 1
                continue
            visited.add(child.node_id)
            flat.append((child, depth, parent.node_id))
            visit(child, depth + 1)

    visit(root, 1)
    return flat, truncated, cycles


def _attach_opencode(nodes: dict[str, _Node], root_candidates: list[_Node]) -> None:
    """opencode 세션을 cwd 완전 일치 + 시간 포함으로 루트에 추정 부착한다.

    opencode의 ``session.parent_id``는 실질적으로 비어 있어(실측 1/270) 직접
    링크를 쓸 수 없다. 그래서 "같은 작업 디렉터리에서, 부모 세션이 살아 있는
    동안 통째로 실행된 세션"만 자식으로 본다. 규칙이 엄격한 만큼 놓치는 건
    있어도 잘못 붙는 건 드물다 — 붙은 노드는 ``inferred``로 표시해 사용자가
    추정임을 알 수 있게 한다.

    알려진 한계: git worktree에서 돌린 세션은 cwd가 달라 붙지 않는다.
    """
    for node in nodes.values():
        if node.source != "opencode" or node.cwd is None:
            continue
        matches = [
            root
            for root in root_candidates
            if root.cwd == node.cwd
            and root.start <= node.start
            and node.end <= root.end
        ]
        if not matches:
            continue
        # 가장 짧은 루트 → 더 늦게 시작한 루트 → session_id 사전순.
        best = min(
            matches,
            key=lambda r: (
                (r.end - r.start).total_seconds(),
                -r.start.timestamp(),
                r.session_id,
            ),
        )
        node.inferred = True
        best.children.append(node)


def _flush_component(
    component: list[_Node], groups: dict[str, int | None], next_group: int
) -> int:
    """겹침 성분 하나를 확정한다. 2개 이상일 때만 그룹 번호를 부여한다."""
    if len(component) >= 2:
        for node in component:
            groups[node.node_id] = next_group
        return next_group + 1
    for node in component:
        groups[node.node_id] = None
    return next_group


def _assign_parallel_groups(
    flat: list[tuple[_Node, int, str]],
) -> dict[str, int | None]:
    """같은 부모를 가진 형제들 중 실행 구간이 겹치는 묶음에 그룹 번호를 준다.

    형제를 시작 시각순으로 정렬한 뒤 구간 병합(interval merge)을 한 번 돌린다.
    경계 접촉(``a.end == b.start``)은 겹침으로 보지 않으므로 비교는 strict ``<``.
    그룹 번호는 DFS 순서를 따르므로 같은 입력이면 항상 같은 번호가 나온다.
    """
    by_parent: dict[str, list[_Node]] = defaultdict(list)
    for node, _depth, parent_id in flat:
        by_parent[parent_id].append(node)

    groups: dict[str, int | None] = {}
    next_group = 1
    for siblings in by_parent.values():
        component: list[_Node] = []
        component_end: datetime | None = None
        for node in sorted(siblings, key=lambda n: (n.start, n.node_id)):
            if component_end is not None and node.start < component_end:
                component.append(node)
                component_end = max(component_end, node.end)
                continue
            next_group = _flush_component(component, groups, next_group)
            component = [node]
            component_end = node.end
        next_group = _flush_component(component, groups, next_group)
    return groups


def _max_parallel(nodes_list: list[_Node]) -> int:
    """동시에 살아 있던 자손의 최대 수.

    시작 +1 / 종료 -1 이벤트를 훑는 스윕라인. 같은 시각에서는 정상 종료 → 시작 →
    길이 0 종료 순으로 처리한다. 종료가 시작보다 먼저이므로 경계에서 맞닿기만 한
    두 구간은 동시 실행으로 잡히지 않고, 길이 0 구간(레코드가 하나뿐인 순간 세션)은
    자기 종료가 자기 시작 뒤로 밀려 스스로를 상쇄하지 않는다.
    """
    close_rank, open_rank, point_close_rank = 0, 1, 2
    events: list[tuple[datetime, int, int]] = []
    for node in nodes_list:
        events.append((node.start, open_rank, 1))
        end_rank = point_close_rank if node.start == node.end else close_rank
        events.append((node.end, end_rank, -1))
    events.sort(key=lambda event: (event[0], event[1]))

    current = 0
    best = 0
    for _time, _rank, delta in events:
        current += delta
        best = max(best, current)
    return best


def flows(records: list[Record]) -> tuple[list[dict[str, object]], list[str]]:
    """세션별 위임 흐름 배열과 경고 목록을 반환한다.

    흐름 = 자식이 1개 이상인 루트 세션. 자식이 없는 세션은 타임라인에 그릴 게
    없으므로 제외한다. 비용 내림차순으로 정렬된다.
    """
    nodes = _fold(records)

    # 서브에이전트가 부모의 sessionId를 물려받으므로, session_id → 노드 맵은
    # 반드시 루트(경로상 부모가 없는 Claude 노드)만 담아야 한다.
    roots_by_session = {
        n.session_id: n
        for n in nodes.values()
        if n.source == "claude" and n.parent_session_id is None
    }

    orphans = _attach_claude(nodes, roots_by_session)

    root_candidates = [
        n for n in nodes.values() if n.agent is None and n.parent_session_id is None
    ]
    _attach_opencode(nodes, root_candidates)

    result: list[dict[str, object]] = []
    truncated_total = 0
    cycles_total = 0

    for root in root_candidates:
        if not root.children:
            continue
        flat, truncated, cycles = _walk(root)
        truncated_total += truncated
        cycles_total += cycles
        if not flat:
            continue

        groups = _assign_parallel_groups(flat)
        descendants = [n for n, _depth, _parent in flat]
        start = min([root.start, *(n.start for n in descendants)])
        end = max([root.end, *(n.end for n in descendants)])
        cost_usd = round(root.cost_usd + sum(n.cost_usd for n in descendants), 4)
        self_cost = round(root.cost_usd, 4)
        # 0 나눗셈 방지 + 손상 입력(self > total)에서 음수 share 클램프.
        if cost_usd == 0:
            delegation_share = 0.0
        else:
            delegation_share = round(max(0.0, (cost_usd - self_cost) / cost_usd), 3)
        # 자손 노드만 — 루트 cache_write는 위임 부대비용이 아니다 (스펙 D3).
        setup_cost_usd = round(sum(n.setup_cost_usd for n in descendants), 4)

        result.append(
            {
                "node_id": root.node_id,
                "session_id": root.session_id,
                "project": root.project,
                "cwd": root.cwd,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "duration_sec": int((end - start).total_seconds()),
                "cost_usd": cost_usd,
                "tokens": root.tokens + sum(n.tokens for n in descendants),
                "child_count": len(descendants),
                "max_parallel": _max_parallel(descendants),
                "two_hop_count": sum(1 for _n, depth, _p in flat if depth >= 2),
                "self": {
                    "cost_usd": self_cost,
                    "tokens": root.tokens,
                    "turns": root.turns,
                },
                "children": [
                    {
                        "node_id": node.node_id,
                        "session_id": node.session_id,
                        "parent_node_id": parent_id,
                        "parent_session_id": node.parent_session_id,
                        "agent": node.agent,
                        "source": node.source,
                        "inferred": node.inferred,
                        "depth": depth,
                        "start": node.start.isoformat(),
                        "end": node.end.isoformat(),
                        "duration_sec": int((node.end - node.start).total_seconds()),
                        "cost_usd": round(node.cost_usd, 4),
                        "tokens": node.tokens,
                        "turns": node.turns,
                        "models": [
                            name
                            for name, _tok in sorted(
                                node.models.items(), key=lambda kv: (-kv[1], kv[0])
                            )
                        ],
                        "parallel_group": groups.get(node.node_id),
                    }
                    for node, depth, parent_id in flat
                ],
                "setup_cost_usd": setup_cost_usd,
                "delegation_share": delegation_share,
            }
        )

    result.sort(key=lambda f: (-float(f["cost_usd"]), str(f["node_id"])))

    warnings: list[str] = []
    if orphans:
        warnings.append(f"부모를 찾지 못한 위임 세션 {orphans}개 — 타임라인에서 제외됩니다")
    if truncated_total:
        warnings.append(f"깊이 상한({MAX_DEPTH})을 넘어 잘린 위임 {truncated_total}개")
    if cycles_total:
        warnings.append(f"순환 참조로 건너뛴 위임 {cycles_total}개")
    return result, warnings

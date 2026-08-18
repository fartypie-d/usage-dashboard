# 대시보드 ↔ FastAPI 데이터 계약

> **이 파일이 API 응답 형식의 정본이다.**

UI가 기대하는 JSON 응답 명세. 백엔드는 아래 필드명·구조에 맞춰 구현하면 됨.

**공통 쿼리 파라미터:**
- `range=15m|1h|24h|7d|30d|all` (필수). `15m`·`1h`은 라이브 창.
- `source=all|claude|opencode` (선택, 기본 `all`). 잘못된 값은 400.

**공통 응답 필드:**
- `range: string` — 요청 파라미터 echo
- `source: string` — 요청 파라미터 echo (UI 토글 활성 상태 판정에 사용)
- `warnings: string[]` — 소스 누락·미등록 모델·pricing 파싱 실패 등을 dedup한 배열
- `source_freshness: {claude: ms|null, opencode: ms|null}` — 소스별 최신 데이터의 epoch ms. **range 필터 적용 전** 전체 레코드에서 계산되므로, 라이브 창에 데이터가 없어도 "마지막이 언제였는지" 표시 가능.

`fetch()`는 상대 경로 사용(정적 서빙, 오프라인). 폴링은 range에 따라 적응형:
`15m`·`1h`은 15초, 나머지는 60초.

---

## GET /api/summary?range=30d&source=all — 섹션 1·2·3 (모델 믹스·비용·모델 순위·캐시)

```json
{
  "range": "30d",
  "source": "all",
  "source_freshness": {"claude": 1784872500000, "opencode": 1784872530000},
  "generated_at": 1784872545490,           // epoch ms, "갱신" 표시에 사용
  "kpi": {
    "total_cost_usd": 271.72,
    "total_tokens": 18400000,               // input+output+cache 합
    "cache_hit_rate": 0.61,                 // read / (input + read)
    "delegated_session_ratio": 0.43,
    "anomaly_count": 7
  },
  "project_mix": [
    {
      "project": "proj-alpha",                 // cwd 마지막 세그먼트
      "cost_usd": 142.30,
      "total_tokens": 5200000,
      "by_model": [
        { "model": "claude-opus-4", "tokens": 3200000 },
        { "model": "claude-sonnet-4", "tokens": 900000 },
        { "model": "claude-haiku-4", "tokens": 400000 },
        { "model": "qwen3.7-plus", "tokens": 700000 }
      ]
    }
  ],
  "model_rank": [                           // cost_usd desc → tokens desc → model asc
    {
      "model": "claude-opus-4-7",
      "cost_usd": 142.3016,                 // 반올림 없음. 포맷은 프론트 담당
      "cost_share": 0.524,                  // 0.0~1.0, 분모는 필터된 전체 비용
      "tokens": 5200000,                    // input+output+cache_read+cache_write
      "by_agent": [                         // 같은 정렬 규칙 적용
        { "agent": "직접(메인)", "cost_usd": 100.0016, "tokens": 3000000 },
        { "agent": "web-ui",     "cost_usd": 42.30,    "tokens": 2200000 }
      ]
    }
  ],
  // `agent`가 없는 레코드(메인 세션 직접 실행)는 백엔드가 `직접(메인)`으로 라벨링한다.
  // 집계 키는 라벨이 아니라 `agent is None` 여부이므로, 실제로 같은 이름의 agent가 있어도
  // 두 행으로 분리되어 숫자는 정확하다. 단가 미등록 모델은 `cost_usd: 0`으로 포함된다.
  "daily_cost": [                           // 기간 길이만큼 (7 / 30 / N)
    { "date": "2026-06-25", "cost_usd": 6.30 }
  ],
  "mismatches": [                           // 비싼 모델 × 저난도 작업, severity desc 정렬
    {
      "session_id": "a3f1",
      "project": "proj-alpha",
      "model": "claude-opus-4",
      "severity": "high",                   // high | med | low
      "cost_usd": 6.40,
      "tokens": 310000,
      "turns": 24,
      "reason": "평균 출력 38 tok · 단순 grep/read 22회 반복",
      "avg_output_tokens": 38,
      "suggested_model": "claude-haiku-4",
      "estimated_savings_usd": 6.06
    }
  ],
  "cache": {
    "savings_now_usd": 38.20,               // 캐시 read로 이미 절감한 추정액
    "savings_potential_usd": 22.50,         // 하위 세션이 평균 도달 시 추가 여지
    "by_project_model": [
      {
        "project": "proj-alpha",
        "model": "claude-opus-4",
        "cache_read_ratio": 0.12,           // cache_read / (input + cache_read)
        "message_count": 84
      }
    ],
    "worst_sessions": [
      {
        "session_id": "a3f1",
        "project": "proj-alpha",
        "model": "claude-opus-4",
        "cache_read_ratio": 0.12,
        "input_tokens": 272000,
        "estimated_savings_usd": 4.90
      }
    ]
  }
}
```

## GET /api/delegation?range=30d&source=all&limit=20 — 섹션 4 (위임 체인) · 섹션 6 (위임 타임라인)

| 질의 파라미터 | 기본값 | 범위 | 의미 |
|---|---|---|---|
| `range` | (필수) | — | 기간 |
| `source` | `all` | — | 데이터 소스 |
| `limit` | `20` | `1 ≤ limit ≤ 1000` | **`flows[]`에만** 적용되는 직렬화 개수 상한. 범위 밖은 `422` |

```json
{
  "range": "30d",
  "source": "all",
  "source_freshness": {"claude": 1784872500000, "opencode": 1784872530000},
  "warnings": [],                           // 고아 흐름·미등록 모델·손상 입력 경고 (한국어)
  "flow": [                                 // claude → agent 링크, tokens desc
    { "agent": "web-ui", "tokens": 4200000, "calls": 38 }
  ],
  "flows": [ /* 섹션 6 — 잘린 목록. 아래 "flows[]" 절 참조 */ ],
  "flows_total": 77,                        // 자르기 전 전체 흐름 개수
  "flows_limit": 20,                        // 이번 응답에 적용된 상한 (= 질의 limit)
  "agents": [
    {
      "agent": "web-ui",
      "calls": 38,
      "tokens": 4200000,
      "cost_usd": 44.10,
      "avg_turns": 6.2
    }
  ],
  "overhead": {                             // 비용 배분 (Phase 9 재정의 — 아래 절 참조)
    "total_flow_cost_usd": 2245.63,         // 위임이 있었던 흐름들의 총비용
    "delegated_cost_usd": 383.81,           // 그중 자식에게 간 비용
    "delegation_share": 0.171,              // delegated / total (0~1 비율)
    "setup_cost_usd": 179.27,               // 자식들의 cache_write 비용 합
    "work_cost_usd": 204.55,                // delegated - setup
    "setup_share": 0.467,                   // setup / delegated (0~1 비율)
    "flow_count": 46,                       // 흐름 개수
    "two_hop_count": 1                      // agent가 다시 위임한 건수
  }
}
```

### 응답 크기 제어 — `limit` · `flows_total` · `flows_limit` (Phase 10)

`range=all`에서 `flows[]`가 588,965 B까지 부풀어 단일 응답으로 감당하기 어려워졌다.
두 가지를 도입했다.

1. **GZip 압축** — `GZipMiddleware(minimum_size=1000)`가 `app` 전체에 걸려 있다
   (`app/main.py`). `Accept-Encoding: gzip`을 보내는 클라이언트는 자동으로 압축본을 받는다.
   API JSON뿐 아니라 `/static/*`(63KB `support.js` 포함)도 대상이다.
2. **`flows[]` 서버측 상한** — 기본 20개. `?limit=N`으로 최대 1000까지 올릴 수 있다.

**🔴 계약의 핵심 — 상한은 `flows[]` 직렬화에만 적용된다**

```
flows_data, _ = delegation_flows(records)         # 전량
overhead_data = overhead(flows_data, ...)          # ← 전량으로 계산 (자르기 전)
response["flows"] = flows_data[:limit]             # ← 직렬화 직전에만 자른다
```

따라서 아래가 **정상**이며, 다르지 않으면 그것이 버그다:

| 값 | 기준 |
|---|---|
| `overhead.flow_count`, `overhead.two_hop_count`, `overhead.*_cost_usd` | **전량** (자르지 않음) |
| `flows_total` | **전량** 개수 |
| `len(flows)` | `min(flows_total, flows_limit)` |

- `flow[]`(섹션 4 에이전트 집계)·`agents[]`도 **자르지 않는다.** `flows[]` 하나만 잘린다.
- `flows[]`는 `cost_usd` 내림차순이므로 잘린 목록은 **"가장 비싼 N개"** 다 — 임의 표본이 아니다.
- **잘림은 경고를 내지 않는다.** `flows_total`과 `len(flows)`의 차이가 곧 신호이고,
  경고를 내면 정상 응답마다 warning이 붙는다.
- 회귀 가드: `tests/test_delegation_endpoint.py`의
  `test_capping_flows_does_not_change_the_overhead_metrics`(`limit=1`과 `limit=1000`의
  `overhead`가 동일해야 함)와 `test_delegation_response_stays_under_the_size_budget`(바이트 상한)이
  이 계약을 잠근다.

**프런트 소비 규약** — 숨은 개수는 반드시 `flows_total - flows.length`로 센다.
`flows.length`끼리 빼면 서버 상한 아래에서 항상 0이 되어, 사용자가 20개를 전부라고
오해하게 된다. `static/support.js`의 "더 보기"는 클라이언트 슬라이스가 아니라
`?limit=1000` **재요청**이다 (접기는 재요청 없이 슬라이스로 되돌린다).

**알려진 한계**: `flows_total > 1000`이면 `limit=1000` 재요청 후에도 잔여가 남지만
UI는 더 이상 안내하지 않는다. 실측 77개 기준 발화 경로가 없어 미구현이다.

### `overhead` — 섹션 4 위임 비용 배분 (Phase 9 재정의)

**Phase 9에서 정의가 바뀌었다.** 예전 지표는 "위임한 작업이 직접 처리보다 얼마나 더 비싼가"를
물었으나, 직접·위임 세션이 **둘 다 있어야만** 계산되고 작업당 평균이 서로 다른 모집단을
나눈 값이라 무의미했다. 새 정의는 비용 배분(cost allocation) 질문이다 —
**"위임 흐름에 쓴 돈 중 얼마가 자식에게 갔고, 그중 얼마가 실작업이 아니라 셋업이었나".**

입력은 레코드가 아니라 **`flows[]`** 다 (`app/metrics/delegation.py:overhead(flows, *, two_hop_count)`).

| 필드 | 타입 | 의미 |
|---|---|---|
| `total_flow_cost_usd` | float(2자리) | 위임이 있었던 흐름들의 총비용 (루트 + 모든 자손) |
| `delegated_cost_usd` | float(2자리) | 그중 자식에게 간 비용 = Σ `max(0, cost_usd - self.cost_usd)` |
| `delegation_share` | float(3자리) | `delegated / total` — **0~1 비율값이다** (백분율 아님) |
| `setup_cost_usd` | float(2자리) | 자식들의 cache_write 비용 합 = 컨텍스트 재적재 비용 |
| `work_cost_usd` | float(2자리) | `delegated - setup` (음수면 0) |
| `setup_share` | float(3자리) | `setup / delegated` — **0~1 비율값** |
| `flow_count` | int | 흐름 개수. **`0`이면 프런트는 카드 대신 빈 상태를 그린다** |
| `two_hop_count` | int | Σ `flows[].two_hop_count` = 재위임 건수 |

**계약상 주의**

- **삭제된 3필드에 백워드 호환 별칭이 없다** — `delegation_token_overhead`,
  `direct_cost_per_task_usd`, `delegated_cost_per_task_usd`는 응답에서 완전히 사라졌다.
  `tests/test_delegation_endpoint.py`와 `tests/test_static_mount.py`가 부활을 잠근다.
- `setup_cost_usd`는 **상한**이다. cache_write는 컨텍스트 재적재의 대리 지표일 뿐,
  전부가 순수 셋업 낭비라는 뜻은 아니다. UI 부제에도 "(상한)"으로 표기한다.
- **손상 입력은 조용히 클램프하지 않고 한국어 경고를 낸다** — `self.cost_usd > cost_usd`인
  흐름이 있거나 `setup > delegated`이면 `warnings[]`에 사유가 실린다.
- `delegation_share`·`setup_share`는 **이미 비율**이다. 프런트의 `pct()`가 0~1을 받아
  정수 %로 만든다 — 손으로 `* 100`을 하거나 `"+"` 부호를 붙이면 과거의 `+-56%` 버그가 재발한다.

> ⚠️ **통합 시 필수 추가 (디자인 산출물에 누락됨)** — `agents[]` 각 항목에
> **사용 모델 브레이크다운**이 없다. "에이전트별 사용 모델 브레이크다운" 요구사항에 따라, 같은 agent가 기간 내
> 여러 modelID로 실행됐다면 agent×model 조합별로 분리 집계해야 한다. 예:
> ```json
> "agents": [
>   {
>     "agent": "web-ui",
>     "models": [
>       { "model": "qwencloud/qwen3.7-plus", "calls": 30, "tokens": 3400000, "cost_usd": 35.10 },
>       { "model": "qwencloud/kimi-k2.7-code", "calls": 8, "tokens": 800000, "cost_usd": 9.00 }
>     ],
>     "calls": 38, "tokens": 4200000, "cost_usd": 44.10, "avg_turns": 6.2
>   }
> ]
> ```
> 백엔드(dash-backend) 구현 시 이 필드를 추가하고, UI(dash-ui)에서 agent별
> "현재 사용 모델" 배지 + 세부 테이블로 노출한다.

### `flows[]` — 섹션 6 위임 타임라인 (Phase 7·8)

`flow[]`(에이전트별 집계)와 달리 **부모 세션 1개 = 흐름 1개**를 시간축과 함께 내려준다.
배열은 **`cost_usd` 내림차순**(동률 시 `node_id` 오름차순)이며,
**기본 20개로 잘려 온다** (위 "응답 크기 제어" 절 — 전체 개수는 `flows_total`).
프런트는 `static/support.js`의 `renderDelegationTimeline`이
소비한다 (`state.delegation.flows`).

```json
{
  "node_id": "proj_flow01/root-sess-0001.jsonl",
  "session_id": "root-sess-0001",
  "project": "flowproj",
  "cwd": "/anon/flowproj",
  "start": "2026-07-21T10:00:00+00:00",
  "end": "2026-07-21T11:00:00+00:00",
  "duration_sec": 3600,
  "cost_usd": 0.0404,
  "tokens": 9830,
  "child_count": 4,
  "max_parallel": 4,
  "two_hop_count": 1,
  "setup_cost_usd": 0.0,
  "delegation_share": 0.468,
  "self": { "cost_usd": 0.0215, "tokens": 3750, "turns": 3 },
  "children": [
    {
      "node_id": "proj_flow01/root-sess-0001/subagents/child-a.jsonl",
      "session_id": "root-sess-0001",
      "parent_node_id": "proj_flow01/root-sess-0001.jsonl",
      "parent_session_id": "root-sess-0001",
      "agent": "python-reviewer",
      "source": "claude",
      "inferred": false,
      "depth": 1,
      "start": "2026-07-21T10:10:00+00:00",
      "end": "2026-07-21T10:30:00+00:00",
      "duration_sec": 1200,
      "cost_usd": 0.0044,
      "tokens": 1320,
      "turns": 2,
      "models": ["claude-sonnet-4"],
      "parallel_group": 1
    }
  ]
}
```

**흐름(루트) 필드**

| 필드 | 의미 |
|---|---|
| `node_id` | **노드의 유일 식별자. UI 키는 반드시 이것을 쓴다.** |
| `session_id` | 원본 세션 ID — **유일하지 않다** (아래 주의 참조) |
| `project` / `cwd` | 프로젝트명 / 작업 디렉터리. `cwd`는 `null`일 수 있다 |
| `start` / `end` | 루트 **+ 모든 자손**을 포함한 최소·최대 시각 (ISO 8601, tz 포함). 간트 x축 범위 |
| `duration_sec` | `end - start` 초. **0일 수 있다** (모든 시각이 동일한 흐름) |
| `cost_usd` / `tokens` | 루트 + 모든 자손 **합계** |
| `self` | 루트 자신만의 `cost_usd` / `tokens` / `turns` |
| `child_count` | 자손 총수 (모든 깊이) |
| `max_parallel` | 흐름 전체에서 동시에 살아 있던 자손의 최대 수 |
| `two_hop_count` | 깊이 2 이상 자손 수 = 재위임 횟수 |
| `setup_cost_usd` | 이 흐름 자손들의 cache_write 비용 합 (float 4자리). `overhead.setup_cost_usd`의 흐름별 성분 |
| `delegation_share` | `max(0, (cost_usd - self.cost_usd) / cost_usd)` (float 3자리, 0~1). `cost_usd == 0`이면 `0.0` |
| `children[]` | **중첩이 아니라 DFS 순서로 평탄화된 배열.** 계층은 `depth`·`parent_node_id`로 복원 |

**자손(`children[]`) 필드**

| 필드 | 의미 |
|---|---|
| `agent` | 서브에이전트명. **`null`일 수 있다** (이름 복원 실패 시 UI는 `unknown`으로 표기) |
| `source` | `"claude"` \| `"opencode"` |
| `inferred` | `true`면 opencode 세션을 **cwd + 시각 포함으로 추정** 연결한 것 (UI는 "추정" 배지) |
| `depth` | 1부터. **최대 5** (`MAX_DEPTH`) |
| `parallel_group` | 겹치는 형제 묶음 번호(1부터). 혼자면 **`null`** (UI는 `∥N` 배지) |
| `models[]` | 이 노드가 사용한 모델 ID 목록 |
| `start`/`end`/`duration_sec` | 자손 자신의 구간. `duration_sec`은 0일 수 있다 |

**계약상 주의 (실측 근거)**

- **노드 식별자는 `node_id`(파일 경로)이지 `session_id`가 아니다.** Claude 서브에이전트는
  부모의 `sessionId`를 그대로 물려받아 충돌한다 — 실측 393건 중 392건이 부모와 동일 ID였다.
  접기 상태 Set 등 UI의 모든 키에 `node_id`를 쓴다.
- **구간은 `[start, end)` 반열림.** 경계가 맞닿는 것은 겹침이 아니다. 점 구간은 `[t, t+ε)`로
  다룬다 — 실측 1323노드 중 46개가 길이 0 구간이다.
- **`start`/`end`는 항상 비-null**이다 (백엔드가 `isoformat()`으로 직렬화). 다만 `duration_sec`이
  0일 수 있으므로 **프런트는 0 나눗셈을 방어**해야 한다.
- 흐름이 누락되는 3가지 경우는 모두 **`warnings[]`에 한국어 경고**로 나온다 —
  고아(부모 미발견), 깊이 상한 초과, 순환 참조. 이 경고들은 `/api/delegation`의
  기존 `warnings[]` 배열에 합쳐져 내려간다.
- `overhead.two_hop_count`는 **Phase 9에서 실값으로 채워졌다** — `flows[].two_hop_count`의
  합이다. 오버헤드 지표 전체가 `flows`를 입력으로 재정의됐다 (위 `overhead` 절 참조).

## GET /api/sessions?range=30d&source=all — 섹션 5 (세션 건강도)

```json
{
  "range": "30d",
  "source": "all",
  "source_freshness": {"claude": 1784872500000, "opencode": 1784872530000},
  "warnings": [],
  "sessions": [
    {
      "session_id": "a3f1",
      "project": "proj-alpha",
      "model": "claude-opus-4",
      "turns": 9,
      "context_growth": [18000, 29000, 44000, 66000, 95000, 140000, 205000, 300000, 420000],
                                            // 턴별 input+cache 토큰 누적 (스파크라인)
      "compaction_turns": [],               // compaction 발생한 턴 인덱스 배열
      "token_spike": true,                  // 턴 간 급증 감지
      "split_recommended": true,            // 성장 속도 임계치 초과
      "health": "danger"                    // danger | warn | ok  (색상 아님, 파생 라벨)
    }
  ]
}
```

---

## GET /api/work/sessions?range=30d&source=all — 작업 브라우저 목록

프로젝트별 세션 그룹 (최근 활동 내림차순). `range`·`source` 검증은 다른 엔드포인트와 동일 (400).
`title_index`(claude ai-title)·`session_index`(opencode session 테이블) 경고는 `warnings`에 합류한다.

```jsonc
{
  "range": "30d", "source": "all",
  "projects": [
    {
      "project": "usage-dashboard", "session_count": 3, "cost_usd": 12.4,
      "sessions": [            // started_at 내림차순
        {
          "id": "…", "source": "claude",          // "claude" | "opencode"
          "title": "차트 축 버그 수정",             // ai-title / session.title, 없으면 null
          "phase": 11, "phase_slug": "phase11-work-browser",  // 워크트리 세션만, 아니면 null
          "started_at": 1753174800000, "ended_at": 1753175400000,   // epoch ms
          "cost_usd": 4.2, "models": ["claude-opus-4-8"],
          "agent": null, "is_subagent": false
        }
      ]
    }
  ],
  "warnings": [], "source_freshness": {"claude": null, "opencode": null}
}
```

`project`는 **워크트리 cwd 정규화** 적용 — `<프로젝트>/.claude/worktrees/<slug>` 세션은 원
프로젝트로 귀속되고 slug의 phase 번호가 `phase`/`phase_slug`로 나온다 (작업 브라우저 계층 전용,
기존 사용량 지표의 `project`는 불변).

## GET /api/work/session/{source}/{session_id} — 작업 브라우저 상세 (턴 타임라인)

`source` ∈ `{claude, opencode}`. 미지 source/세션/소실·읽기 불가 파일 → **404**
(corpus 경고가 있으면 404 `detail`에 함께 노출 — 인프라 장애와 "세션 없음"을 구분한다).
클릭 시점 온디맨드 파싱 — 세션 1개(JSONL 파일 1개 / DB 세션 1개)만 읽는다.
세션 행 없이 메시지만 있는 비정상 opencode DB는 최소 메타(`project: "unknown"`) + 경고로 응답한다.

```jsonc
{
  "session": { /* 목록 카드와 같은 11키 + "project" (워크트리 cwd 정규화 적용) */ },
  "turns": [                             // []면 "존재하나 표시할 턴 없음" (404 아님)
    {
      "ts": 1753174800000,               // 지시 시각 (epoch ms, 없으면 null)
      "instruction": "차트가 안 그려져요…", // null이면 "(이전 세션에서 이어짐)" 선행 턴
      "instruction_truncated": false,
      "reasoning": ["원인은 축 범위…"],    // thinking/reasoning 원문 발췌 (턴당 ≤5개, 개당 ≤700자)
      "reasoning_truncated": false,
      "actions": [{"tool": "Edit", "target": "static/chart-page.js"}],  // 턴당 ≤50개
      "actions_truncated": false,
      "response": "y축 범위를 수정했습니다.",   // assistant 텍스트 발췌 (≤1,000자)
      "response_truncated": false
    }
  ],
  "warnings": []
}
```

발췌 상한 (계약 — `app/sources/transcript_common.py` 상수): instruction 2,000자 /
reasoning 700자·5개 / response 1,000자 / actions 50개·target 200자 / turns 500.
초과분은 `*_truncated` 플래그 또는 warnings로 드러난다. LLM 재요약 없음 — 원문 발췌뿐.

## GET /api/progress — 진행내역 격자 (phase × task)

phase 지시서·리뷰 문서를 읽어 내려준다.
**본문(노드)은 싣지 않는다** — 격자를 그리는 데 필요한 메타만 담아 가볍게 유지한다.

**질의 파라미터** (Phase 14):

| 파라미터 | 타입 | 비고 |
|---|---|---|
| `project` | string, 선택 | 오케스트레이트 레지스트리의 프로젝트 이름. `^[A-Za-z0-9._-]+$`·1~64자 밖이면 **422**. **미지 이름은 404** (`detail.message`에 이름, `detail.warnings` 동봉). 생략 시 자기 자신(`USAGE_DOCS_ROOT`, 기본 `docs`) — Phase 13 계약 그대로 |

- `project` 해석: 레지스트리(`USAGE_REGISTRY_DIR`, 기본 `~/.local/state/orchestrate/registry`)
  항목의 `root`/`docs_dir` 를 조합한다. 질의 문자열이 경로 조립에 직접 쓰이는 일은 없다.
- **404와 200+경고의 구분이 계약이다**: 레지스트리에 없는 이름 → 404. 레지스트리엔 있는데
  `root` 디렉터리가 회수됨(워크트리 close 등) → **200 + 빈 `phases` + 경고**.
- 레지스트리 파일 손상은 `warnings[]`로 노출된다. 단 **무파라미터 요청**에서는 "레지스트리
  디렉터리 자체가 없음"(멀티 프로젝트 미설정 = 정상 상태)만 경고를 억제한다 — 디렉터리가
  있는데 개별 파일이 깨진 경우는 무파라미터에서도 경고가 나온다.

| 키 | 타입 | 비고 |
|---|---|---|
| `phases[]` | array | phase 번호 **내림차순** |
| `phases[].phase` | int | 파일명 또는 frontmatter에서 |
| `phases[].slug` | string \| null | `PHASE<n>_<slug>.md` 규약일 때만 |
| `phases[].date`·`kind`·`domain`·`status`·`summary` | string \| null | frontmatter 원문 |
| `phases[].cost` | number \| null | 숫자로 떨어질 때만. 통화기호·쉼표는 벗긴다 |
| `phases[].cost_raw` | string \| null | 숫자가 아닐 때 원문 보존 (예: `"-"`, `"$66.28 (내역…)"`) |
| `phases[].compactions`·`interventions` | number \| null | 위와 같은 규칙 |
| `phases[].commits[]` | string[] | frontmatter `commits`를 쉼표로 분해 |
| `phases[].doc_path` | string | 지시서 경로 |
| `phases[].review_path` | string \| null | `docs/reviews/PHASE<n>_*.md` 가 없으면 null |
| `phases[].tasks[].n` | int | `## Task N` 헤딩 번호 |
| `phases[].tasks[].title` | string | 헤딩의 제목 부분 |
| `phases[].tasks[].verdict` | `"pass"` \| `"fail"` \| null | 검수 표가 **4열 이상**일 때만 채워진다 |
| `phases[].tasks[].verdict_raw` | string \| null | 판정 셀 원문 (아이콘·비고 포함) |
| `phases[].tasks[].commit` | string \| null | 판정 행의 백틱 안 해시 |
| `phases[].active` | bool | **`project` 지정 시에만 모든 행에 존재** — 레지스트리 active 클레임과 일치하면 true. 무파라미터 응답에는 키 자체가 없다 (기존 계약 보존) |
| `warnings[]` | string[] | 문서 규약 균열을 그대로 노출. active 등록 slug ≠ 문서 slug 불일치도 여기로 |
| `project` | string \| null | 이번 요청이 읽은 프로젝트. 무파라미터면 null (Phase 14) |
| `projects[]` | array | 레지스트리 전체 — `{project, docs_dir, next_phase, active_count}`. 레지스트리 불가 시 `[]` (Phase 14) |

**합성 행 (claimed)** — 레지스트리에 active로 등록됐는데 지시서 파일이 아직 없는 phase는
`{"phase": N, "slug": <레지스트리 slug>, "status": "claimed", "active": true, 나머지 메타 전부 null}`
합성 행으로 `phases[]`에 들어간다. 키 집합은 일반 행과 동일하다 — 없는 메타를 지어내지 않는다.
이 행의 상세(`/api/progress/phase/{n}`)는 404가 정상이다 (문서가 없으므로).

**판정 추출 규칙** — 지시서는 검수를 두 절로 나눠 쓴다. `## 검수 (task마다)` 에
리뷰어 배정(2열), `## 검수 결과` 에 판정(4열). '검수'가 든 헤딩을 **전부** 훑고
4열 이상 행만 채택하며, 뒤 절이 앞 절을 덮어쓴다. 2열뿐이거나 표가 없으면
`verdict: null` + warning이다 — 모르는 것을 승인으로 지어내지 않는다.

**누락 phase** — 문서만 읽으므로 지시서가 없는 phase는 존재 자체를 알 수 없다.
찾은 번호가 연속인지만 검사해 `1..max` 사이 구멍을 warning으로 올린다.
최댓값 너머(작업 중인데 문서가 아직 없는 phase)는 검출 대상이 아니다.

실측 (2026-08-08, Phase 14): 무파라미터 최상위 키 = `phases, warnings, project(null), projects` /
`?project=proj-bravo` → phase 61개, claimed 합성 행 3개(183·182·177), 행 키 16개(`active` 포함) /
`?project=nope` → 404 `{"detail":{"message":"Unknown project: nope","warnings":[]}}`.
**컨테이너(9280) 주의**: compose 마운트에 레지스트리·타 프로젝트 루트가 없어 컨테이너에서는
`projects: []` + 자기 자신만 동작한다. 다중 프로젝트 반영에는 마운트 추가 + 재빌드(사용자 승인) 필요.

실측 응답 (2026-08-07, 실제 `docs/` — phase 11개, warnings 8개, 13,108 B — Phase 14 이후
아래 예시에 `project`·`projects` 키가 뒤에 추가된다):

```json
{
  "phases": [
    {
      "phase": 11,
      "slug": "work-browser",
      "date": "2026-08-05",
      "kind": "task",
      "domain": "backend, frontend",
      "status": "done",
      "cost": null,
      "cost_raw": "-",
      "compactions": 0,
      "interventions": 8,
      "summary": "작업 브라우저 신설 — 프로젝트별 세션 목록 + 턴 타임라인(…",
      "commits": ["73cddc7", "b862c51", "…"],
      "doc_path": "docs/PHASE11_work-browser.md",
      "review_path": null,
      "tasks": [
        {
          "n": 1,
          "title": "픽스처 보강",
          "verdict": "pass",
          "verdict_raw": "✅ 재판정 승인 · 🔴 1(ocp-6 JSON 손상 — …)",
          "commit": "73cddc7"
        },
        { "…": "…" }
      ]
    },
    { "…": "…" }
  ],
  "warnings": [
    "Phase 9: 검수 결과 표 없음 — task 판정 미상",
    "Phase 1: frontmatter 없음 — 파일명에서 복원",
    "…"
  ]
}
```

## GET /api/progress/phase/{n} — phase 1개의 본문 (노드 트리)

지시서를 절 단위로 쪼개 **마크다운 노드 트리**로 내려준다. 없는 번호는 404.
`?project=` 질의 파라미터를 `/api/progress`와 동일한 규칙으로 받는다 (Phase 14).

| 키 | 타입 | 비고 |
|---|---|---|
| `phase` | int | |
| `intro[]` | node[] | 첫 `## Task` 앞의 서문 |
| `tasks[].n`·`tasks[].title` | int, string | |
| `tasks[].nodes[]` | node[] | 그 절의 본문 |
| `review[]` | node[] | 리뷰 문서. 없으면 `[]` |
| `warnings[]` | string[] | |
| `project` | string \| null | `/api/progress`와 동일 의미 (Phase 14) |

### 노드 타입

노드는 `{"t": <타입>, "c": [자식…]}` 이고 타입별로 키가 더 붙는다.

| `t` | 추가 키 | 자식 |
|---|---|---|
| `heading` | `level` (1~4, 5 이상은 4로 clamp) | 있음 |
| `paragraph`·`blockquote`·`item`·`row`·`strong`·`em`·`plain` | — | 있음 |
| `list` | `ordered` (bool) | 있음 |
| `table` | — | `row` 들 (thead/tbody는 걷어낸다) |
| `cell` | `header` (bool), `align` (`left`\|`center`\|`right`\|null) | 있음 |
| `link` | `href` | 있음 |
| `code` | `lang`, `text` | 없음 |
| `codespan` | `text` | 없음 |
| `text` | `text` | 없음 |
| `hr` | — | 없음 |

**HTML 문자열이 아니라 노드 트리인 이유**: 프런트(`static/markdown.js`)가
`createElement` 로만 조립해 `innerHTML` 경로를 만들지 않기 위해서다. 같은 렌더러를
나중에 세션 발췌(에이전트가 만든 신뢰할 수 없는 텍스트)에 재사용해도 XSS 표면이 생기지 않는다.

**안전 규칙** — `markdown-it-py` 를 `html=False` 로 돌려 raw HTML은 텍스트로
이스케이프된다. 링크는 `http(s):`·`#`·상대경로만 허용하고, 그 밖(`mailto:`·`ftp:` 등)은
`plain` 으로 감싸 링크를 벗긴다. 화이트리스트 밖 토큰은 버리지 않고 `text` 로 평탄화한다.
마크다운 변환이 예외로 죽으면 그 절만 원문 `code` 노드 하나로 격하하고 warning을 남긴다.

실측 (phase 11: task 9개 / 38,416 B, DOM 1,046노드·5,593자로 렌더).

## 상태 처리 (UI 측)
- **로딩**: 최초 진입/기간 변경 시 스켈레톤.
- **에러**: HTTP 실패 또는 네트워크 오류 → 재시도 버튼.
- **빈 데이터**: `sessions`/`project_mix`가 비면 empty 상태.

## 필드 매핑 참고 (원본 메시지 → 집계)
- `project` = `path.cwd`의 마지막 세그먼트.
- `model` = `modelID` (Claude Code) 또는 opencode의 `modelID`.
- cache ratio = `tokens.cache.read / (tokens.input + tokens.cache.read)`.
- 비용 = 모델별 단가표 × (input, output, cache_read, cache_write) 토큰.
  단가표는 백엔드 상수로 관리하고 응답엔 계산된 `*_usd`만 내려주면 됨.
- 위임 링크 = Claude Code 세션에서 opencode를 호출한 이벤트를 `agent`별로 집계.
- 작업 브라우저 턴 = claude: `message.content[].thinking/text/tool_use` · opencode:
  `part.data(type=reasoning/text/tool)` → `/api/work/session` `turns[]`.

# 배포 가이드 — Usage Dashboard

> Docker 컨테이너 기동부터 tailscale / cloudflare 터널 노출까지 단계별 가이드.

---

## 1. 개요

### 아키텍처

```
┌─────────────────────────────────────────────────────────────────┐
│  호스트 (공유 개발 서버)                                         │
│                                                                 │
│  ┌──────────────────────────────┐                               │
│  │  usage-dashboard 컨테이너     │                               │
│  │  (python:3.12-slim, non-root)│                               │
│  │                              │                               │
│  │  uvicorn 0.0.0.0:9280        │                               │
│  └──────────┬───────────────────┘                               │
│             │ ports: "127.0.0.1:9280:9280"                      │
│             ▼                                                   │
│       127.0.0.1:9280  (localhost only)                          │
│             │                                                   │
│             ▼                                                   │
│  ┌──────────────────────┐    ┌────────────────────────────┐     │
│  │ tailscale serve      │ or │ cloudflared tunnel         │     │
│  │ (tailnet 한정 HTTPS) │    │ (+ Cloudflare Access)      │     │
│  └──────────┬───────────┘    └──────────┬─────────────────┘     │
│             │                           │                       │
└─────────────┼───────────────────────────┼───────────────────────┘
              ▼                           ▼
       tailnet 기기들              공인 도메인 (Access 보호)
```

### 데이터 흐름

| 소스 | 호스트 경로 | 컨테이너 마운트 | 접근 |
|---|---|---|---|
| Claude Code 세션 로그 | `~/.claude/projects/` (JSONL) | `/data/claude-projects` | `:ro` (읽기 전용) |
| opencode 세션 DB | `~/.local/share/opencode/opencode.db` (SQLite WAL) | `/data/opencode.db` | `:ro` (읽기 전용) |

- **FastAPI** (`app/main:app`)가 위 2개 소스를 파싱 → 4개 API 엔드포인트 제공
  - `GET /health` — 헬스체크
  - `GET /api/summary` — 사용량 요약
  - `GET /api/delegation` — 위임 통계
  - `GET /api/sessions` — 세션 목록
- 정적 UI (`static/`)는 `/` 경로에서 직접 서빙

### 보안 원칙

- **앱 자체 인증 없음** — 접근 제어는 전적으로 터널 계층에 위임
  - tailscale 사용 시: tailnet ACL로 접근 허용 기기 제어
  - cloudflare 사용 시: **Cloudflare Access 정책 필수** (이메일 인증 등)
- 호스트 바인딩은 `127.0.0.1`만 — 외부 직접 접근 불가
- 원본 데이터는 항상 `:ro` 마운트 — 컨테이너가 쓰기 불가
- 컨테이너는 non-root (`dashuser`, uid 1001)로 실행

---

## 2. 로컬 기동 (Docker Compose)

### 사전 요구

- Docker Engine 24+ 및 Docker Compose v2+ 설치
- `~/.claude/projects/` 디렉터리에 Claude Code 세션 로그 존재
- `~/.local/share/opencode/opencode.db` 파일 존재

### 이미지 빌드

```bash
docker compose build
```

### 컨테이너 기동 (detached)

```bash
docker compose up -d
```

### 로그 실시간 확인 (30초 후 healthcheck 통과 확인)

```bash
docker compose logs -f usage-dashboard
```

healthcheck는 `start_period: 10s` 후 30초 간격으로 실행되며, 3회 연속 성공 시 `healthy` 상태가 된다.

### 헬스체크 수동 확인

```bash
curl -s http://127.0.0.1:9280/health
# → {"status":"ok"}
```

### API 응답 확인

```bash
curl -s "http://127.0.0.1:9280/api/summary?range=30d" | head -c 200
```

기동 확인 후 브라우저에서 **http://127.0.0.1:9280** 접속 (로컬에서만 접속 가능).

---

## 3. 터널 옵션 A: tailscale serve (기본, 권장)

사설망 한정 노출. 앱 자체 인증 없으므로 **tailscale ACL로 접근 제어**.

### tailscale 데몬 실행 확인

```bash
tailscale status
```

### HTTP → tailnet 노출 (백그라운드)

```bash
tailscale serve --bg http://127.0.0.1:9280
```

### 노출 URL 확인

```bash
tailscale serve status
# → https://<hostname>.<tailnet>.ts.net
```

### 다른 tailnet 기기에서 브라우저 접속

기기가 tailscale ACL 통과 필요. `https://<hostname>.<tailnet>.ts.net`으로 접속.

### 해제

```bash
tailscale serve --https=443 off
# 또는 전체 리셋
tailscale serve reset
```

### 주의

- tailscale ACL에 이 노드로의 접근을 허용한 기기만 접속 가능
- **앱 자체 인증 없음** → tailnet에서 ACL 통과한 기기는 누구나 대시보드 열람 가능 (사설망 신뢰 전제)
- 노드 소유자만 접근 원하면 노드-local로 노출:
  ```bash
  tailscale serve --https=443 --set-path=/ http://127.0.0.1:9280
  ```

---

## 4. 터널 옵션 B: cloudflare-tunnel (+Access 필수)

공인 도메인 노출. **Cloudflare Access 정책 필수** (이메일 인증 등).

> ⚠️ Access 정책 없이 노출 절대 금지 — 공인 도메인 = 인터넷 전체 노출.

### 사전 준비

- `cloudflared` 설치 (https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
- Cloudflare 계정 로그인

```bash
cloudflared tunnel login
```

### 터널 생성

```bash
cloudflared tunnel create usage-dashboard
```

생성된 터널 ID (`<tunnel-id>`)를 기록해 둔다.

### DNS 라우팅 설정

Cloudflare 대시보드에서 zone 관리 중인 도메인에 대해:

```bash
cloudflared tunnel route dns usage-dashboard usage-dash.<your-domain>
```

### 터널 config 파일 작성

```bash
cat > ~/.cloudflared/config.yml <<EOF
tunnel: usage-dashboard
credentials-file: ~/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: usage-dash.<your-domain>
    service: http://127.0.0.1:9280
  - service: http_status:404
EOF
```

`<tunnel-id>`와 `<your-domain>`은 실제 값으로 치환한다.

### credentials 파일 권한 제한

```bash
chmod 600 ~/.cloudflared/<tunnel-id>.json
chmod 600 ~/.cloudflared/config.yml
```

### 터널 실행 (백그라운드, systemd 권장)

```bash
cloudflared tunnel run usage-dashboard
```

systemd 서비스로 등록하려면:

```bash
sudo cloudflared service install
sudo systemctl enable cloudflared
sudo systemctl start cloudflared
```

### Access 정책 설정 (필수)

Cloudflare 대시보드 → **Zero Trust** → **Access** → **Applications**:

| 항목 | 값 |
|---|---|
| Application | `usage-dash.<your-domain>` |
| Policy Action | Allow |
| Include Rule | Emails is `<your-email>` |
| (대안) | GitHub OAuth, SSO 등 조직 정책에 맞는 인증 방식 |

### 주의

- **Access 정책 없이 노출 절대 금지**
- `credentials-file`은 `chmod 600`으로 권한 제한 필수
- 터널 config의 `ingress` 마지막 줄은 반드시 `http_status:404` (catch-all)

---

## 5. 운영

### 로그 확인

```bash
# 전체 로그
docker compose logs usage-dashboard

# 실시간 팔로우 + 최근 100줄
docker compose logs -f --tail=100 usage-dashboard
```

### 재시작 (설정 변경 후)

```bash
docker compose restart
```

### 이미지 갱신 (master 최신 pull 후)

```bash
git pull
docker compose build
docker compose up -d
```

구 이미지는 `docker image prune`으로 정리 가능.

### 컨테이너 내부 접속 (디버그)

```bash
docker compose exec usage-dashboard sh
```

### 마운트 검증 (컨테이너 내부에서)

```bash
docker compose exec usage-dashboard ls -la /data/
```

예상 출력:

```
total 8
drwxr-xr-x  2 root   root   4096  ... claude-projects
-r--r--r--  1 root   root   xxxx  ... opencode.db
```

### 환경 변수 확인

```bash
docker compose exec usage-dashboard env | grep USAGE_
```

예상:

```
USAGE_CLAUDE_ROOT=/data/claude-projects
USAGE_OPENCODE_DB=/data/opencode.db
```

---

## 6. 트러블슈팅

| 증상 | 원인 후보 | 조치 |
|---|---|---|
| healthcheck 실패 | uvicorn 미기동, 포트 충돌 | `docker compose logs usage-dashboard` 확인. 호스트 포트 중복 검사: `ss -tlnp \| grep 9280` |
| `/api/summary` 500 | 마운트 경로 부재, env var 미설정 | `docker compose exec usage-dashboard env \| grep USAGE_` 및 `docker compose exec usage-dashboard ls /data/` |
| `/api/summary` 응답에 `warnings` 포함 | 소스 파일 파싱 실패, 일부 세션 누락 | `warnings` 배열 내용 확인. 원본 데이터 경로·권한 점검 |
| opencode.db 잠금 오류 | 다른 앱 인스턴스가 쓰기 중 | 정상 동작 — `busy_timeout` 5초로 재시도. `read_records` read-only 경로 준수 |
| tailscale 접속 실패 | ACL 거부, tailscale 데몬 미실행 | `tailscale status`로 연결 확인. `sudo systemctl status tailscaled`로 데몬 상태 점검 |
| cloudflare 접속 실패 | 터널 프로세스 죽음, Access 정책 오류 | `cloudflared tunnel info usage-dashboard`로 터널 상태 확인. 브라우저 devtools로 리다이렉트 체인 점검 |
| 컨테이너가 계속 재시작 | 헬스체크 연속 실패 | `docker compose logs --tail=50 usage-dashboard`로 시작 오류 확인. `docker inspect usage-dashboard`로 health state 점검 |
| `Permission denied` (마운트) | 호스트 원본 파일 권한 | `ls -la ~/.claude/projects/` 및 `ls -la ~/.local/share/opencode/opencode.db` 확인 |

---

## 7. 보안 체크리스트

배포 후 아래 항목을 반드시 확인:

- [ ] **컨테이너 non-root 실행**: `docker compose exec usage-dashboard id` → `uid=1001(dashuser)` 포함 확인
- [ ] **마운트 `:ro` 확인**: `docker inspect usage-dashboard | jq '.[0].Mounts'` → 각 마운트에 `"RW": false` 확인
- [ ] **호스트 바인딩 127.0.0.1만**: `ss -tlnp | grep 9280` → `127.0.0.1:9280` 확인 (`0.0.0.0:9280`이면 **위험**)
- [ ] **Cloudflare Access 활성** (옵션 B 사용 시): 시크릿 브라우저(로그인 안 된 상태)로 도메인 접속 → 로그인 화면 나오는지 확인
- [ ] **앱 로그에 자격증명 노출 없음**: `docker compose logs | grep -iE "password\|token\|secret\|key"` → 결과 0줄 확인
- [ ] **credentials 파일 권한** (옵션 B 사용 시): `ls -la ~/.cloudflared/<tunnel-id>.json` → `-rw-------` 확인
- [ ] **불필요한 포트 노출 없음**: `ss -tlnp | grep -E "9280|LISTEN"` → 예상 외 리스너 없는지 확인

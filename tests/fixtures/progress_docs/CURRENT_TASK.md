---
phase: 10
date: 2026-08-02
kind: task
status: done
commits: a7e6931, 6a9dc68
cost: 21.94
compactions: 1
interventions: 5
summary: "응답 크기 제어"
---

# 작업 지시서 — 응답 크기 제어

서문 문단이다.

## Task 1: GZip 압축 활성화

미들웨어를 등록한다.

## Task 2: 서버측 상한

| 항목 | 값 |
|---|---|
| 기본 상한 | 20 |

## 검수 (task마다 — 생략 불가)

| Task | 리뷰어 |
|---|---|
| 1 | `fastapi-reviewer` + `python-reviewer` |
| 2 | `typescript-reviewer` |

## 검수 결과

| Task | 에이전트 | 판정 | 커밋 |
|---|---|---|---|
| 1 | dash-backend | ✅ 🔴 0 · 🟠 2 | `6a9dc68` |
| 2 | dash-backend | ✅ 🔴 0 | `35aecf2` |

## 미해결 · 이월

| # | 내용 | 처리 |
|---|---|---|
| 1 | 크기 예산이 느슨함 | 다음 task로 흡수 |

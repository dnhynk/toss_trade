# HANDOFF-W4

- **ID**: W4 (collector — `tossmon/collector/**`, `tossmon/config.py`, `tests/test_collector*.py`)
- **브랜치/워크트리**: `w4-collector` @ `C:\Users\dongh\orca\workspaces\toss_trade\w4-collector`
- **HEAD**: main(4869293) 에 rebase 완료. 내 커밋 cebf32b 는 **이미 main 에 머지됨** (da19527).

## 직전 태스크 요지
감사 최상위 blocker 2건 수정: (A) 유니버스 필터가 수집 경로에 미적용(F-2) →
`ctx.watch()` tier0 게이트 + `/stocks` 배치 판정 + `symbols` 테이블 시드.
(B) 실시간 재판정의 라벨 오염·중복 방어 관통(F-3/⑨/C-1/I-1) → 검출을 현재
매매일로 제한, None 곡선 5분 재시도, 억제 키를 (symbol, UTC매매일)로 변경.
부수: H-6(세션 전환 시 baseline 무효화), H-7(force dwell 준수·랭킹 점수 분리),
H-9(백필 구멍 반드시 warn), BudgetGuard 재시도·실패 계상. 회귀 테스트 19건.

## 상태: **작업 완료·머지됨. 재개할 것 없음.**
미커밋 잔여는 보고서/핸드오프뿐이며 이 커밋에 포함.

## 미해결 / 코디네이터 대기 질문 (답 안 받음, 차기 태스크 후보)
1. 랭킹 승격의 `promotions.score` 가 0.0 으로 바뀜(순위 인코딩 제거) — W7 분석이 이 컬럼을 쓰면 알림 필요.
2. events 에 (symbol, 매매일) 유니크 추가(audit_b I-1 제안 2)는 계약 C-6 변경이라 **미수행** — W2/코디네이터 결정 사항.

## 다음 사람이 알아야 할 것
- 테스트: `.venv/Scripts/python -m pytest tests/ -q` (시스템 python 은 filelock 없음 — 반드시 venv).
- 방금 실행 결과 **690 passed / 4 FAILED — 전부 `tests/test_api_tokens.py` (W1 영역)**:
  `test_invalidate_forces_reissue`(RuntimeError), `test_live_requires_base_url` 등.
  내 영역(test_collector*) 은 전부 green. 코디네이터 공지의 "694 green" 과 불일치 —
  W1 머지분이 이 환경에서 깨지는지 확인 필요 (라이브 호출은 안 했음, mock 전용).
- 상세 보고서: `W4_REPORT_task_59b9b6e505d3.md` (리포 루트, 이 커밋에 포함).
- 라이브 리스 없음 상태 유지 — `TOSS_LIVE=0` 외 실행 금지.

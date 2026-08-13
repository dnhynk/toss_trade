# W3 — Phase 1-C 본분석 (train+val, 홀드아웃 봉인) — 사용자 승인됨 (08-02)

너는 W3(분석 계층 소유자)다. 네가 만든 코드·네가 리허설한 절차로 **본분석**을 수행한다.
이번 태스크로 `docs/13_trial_registry.md` 와 `docs/15_phase1c_report.md` 소유권을 부여한다.

**정본**: `docs/12_preregistration.md` 최신판(개정 6건 전부 머지됨)을 다시 정독하고
**§8 열람 순서 11단계를 그대로** 따르라. 이견·모호점은 분석을 멈추고 escalation.

## 절대 금지

1. **라이브 호출 금지** — 전 과정 오프라인(아래 DB·캘린더 파일만). 토큰 발급 금지.
2. **홀드아웃(2026-05-01~07-29) 열람 금지** — 이 태스크는 train(~2025-12-31)+val
   (2026-01-01~04-30)까지다. 분석 상한 2026-04-30 을 스크립트 기본값으로 강제하라(§6.2).
   홀드아웃 1회 열람은 **별도 태스크·별도 승인**으로만 한다. 매니페스트의 counts-only
   홀드아웃 수치는 인용 가능(이미 공개된 계수).
3. `docs/12` 수정 금지(W7 소유), §1 예측 인용만. 입력 DB 들은 **읽기 전용**
   (복사본을 떠서 쓰거나 read-only 연결 — 원본 파일 수정·이동 금지).
4. 오염 DB `tossmon_20260730_polluted.db` 는 표적 모집단이 아니다 — 분석에 쓰지 마라.

## 입력 (전부 실재 확인됨)

- 백필: `C:/Users/dongh/orca/workspaces/toss_trade/w4-collector/data/backfill.db`
  (1분봉 1,771,840 / 1,465심볼, 일봉 313,934, symbols 1,707) +
  `data/backfill/backfill_manifest.json|.csv` (커버리지·§7-e adjusted=false 증거·
  day_not_in_calendar 93·retention_exhausted 기록) + `calendar_us.json`
  (네 save_calendar 형식, 2,107일; 매니페스트상 캘린더 6,781일 — 차이 확인해 리포트에)
- 실시간 재수집(Q2·쏠림도 전용): `C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db`
  (07-31 14:25~08-01 08:50, 랭킹 스냅샷 포함). 공백 매니페스트는 `docs/11` §7-b 항목
  3~6 — 랭킹 의존 지표에서 §7-b 규칙대로 제외·계수하라.

## 작업 (§8 순서 안에서)

1. **`docs/13_trial_registry.md` 를 첫 시행 전에 생성** (§5.2 형식 그대로).
   이후 모든 시행을 등록하라 — 훈련 무제한 / 검증 ≤5 / 홀드아웃 1 깔때기, best-of-N
   Bonferroni (§5.3).
2. **오염 처리 패스** — §7 (a)~(h) 전 항목의 탐지·처리·카운트를 부록으로 (G3 조건).
   split_dates 파이프라인 필수(`split_scan_report` 의 n_r_uncomputable·n_widened 포함),
   `day_grouping_calendar` 0.0 건수, A5 혼입 검사는 §7-e 강등 문언대로
   "미실행+사유·대체 안전망 결과(매니페스트 증거+r 분포 스팟체크)" 보고.
3. **이벤트 카탈로그 재유도** — collector events 테이블 금지(§7-a), `candles_1m` 에서
   `detect_events` 로 오프라인 재유도(§2.1 파라미터, calendar= 필수, split_dates= 필수).
4. **러너 강제조항** (docs/14 §VII + 네 리허설 권고): ① calendar= 항상 전달
   ② 다일 프레임에 prev_close_u 명시 인자 금지(사슬) ③ run_all 에 meta=/curve=/calendar=
   항상 전달 ④ **provenance 행이 하나라도 있으면 그 시행은 무효 — 비정상 종료 처리**.
   주 분석 곡선은 `prereg_volume_curve`, 일봉은 `prereg_daily_baseline`.
5. **Q1~Q6 판정** — §3 측정 정의·§4 판정 기준(최소 표본·CI·판정불가 보수 기본값) 그대로.
   Q2 는 실시간 구간만(§6.3 구조적 예외 — 홀드아웃 불가 명시), Q5 는 생존 편향 딱지(§7-g)
   와 비용 1.0% 분해(§2.6) 포함. q6 ±1분 감도 병기(§7-f).
6. **산출물**: `docs/15_phase1c_report.md` — 표본 필터 사유별 카운트(scope 컬럼),
   커버리지 매니페스트 요약, Q1~Q6 판정문(accept/reject/판정불가 + §1 예측 대비),
   G1~G5 게이트 중 현 단계 판정 가능분, 한계·편향 정직 기재. `tools/report.py` 경유
   마크다운 + 필요 시 보조 표. **홀드아웃 관련 수치는 counts-only 외 일절 등장 금지.**

## 보고

- 진행 status 1~2시간 간격(현재 §8 단계 번호 포함). 막히면 escalation.
- `worker_done` 1회(ASCII body), (a)~(e) — (c)에 시행 레지스트리 요약(시행 수·Bonferroni),
  (e)에 홀드아웃 열람 단계로 넘어가기 위한 전제조건 목록.
- 소유 경로(기존 + docs/13 + docs/15) 밖 수정 금지, merge-base diff 증명,
  `feat/analyzer` 에만 커밋, 전체 pytest green 유지.

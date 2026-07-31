# W4 보고 — 유니버스 미적용 + 룩어헤드/중복 (감사 최상위 blocker 2건)

task: task_59b9b6e505d3 / dispatch: ctx_9e071c8c14bb
브랜치: `w4-collector`, 커밋 **cebf32b** (main 79fec4c 위, rebase 완료. **머지 안 함** — 지시대로 커밋까지만)
검증: 전부 mock (`TOSS_LIVE=0`). 라이브 호출 0건, `api_keys` 접근 0건, W5 DB 접근 0건.

## (a) 무엇을 했나

### BLOCKER-A (F-2) — 유니버스 필터를 수집 경로에 연결
- `ctx.watch()` 에 tier0 게이트: `universe/filters.py::passes_tier0` (W2 소유 — **읽기만**, diff 0줄) 통과가 확인된 심볼만 워치리스트에 오른다.
- **StockMeta 확보 설계**: 랭킹 상위(승격 후보) 신규 심볼은 `rankings_once` 안에서 `/stocks` 배치로 발행주식수를 받고, 가격은 랭킹 행의 관측치를 쓴다. 판정은 심볼당 1회 캐시라 정상 상태의 추가 호출은 0. 기동 시 `symbols` 테이블(build_universe 산출)을 Reader 로 읽어 tier≥1 은 워치리스트 시드, 전 행은 통과 캐시 + `shares_out` 로 소비한다 — W2 의 진입점 시그니처와 무관하게 테이블 계약만 쓰므로 `ask` 불필요했다.
- **조용한 필터링 방지**: 거부는 심볼당 1회 info 로그 + `universe_rejected` 카운터, 워치리스트 안 미통과 심볼 수는 텔레메트리 `watch_outside_universe` 게이지(5분마다)로 드러난다. 구 상태파일에서 복원된 대형주는 판정 즉시 **warn + unwatch** (재기동 후 자동 정리 — 라이브 watch=32 의 대형주들이 이 경로로 빠진다). CLI `--symbols` 는 운영자 명시라 게이트 우회(pinned).
- H-7 연동: 랭킹 승격 점수를 스코어 채널에서 분리(0.0 로 전달) — 정원이 찼을 때 실스코어 멤버를 축출하지 못하고 **진입만** 허용.

### BLOCKER-B (F-3/⑨/C-1/I-1) — 실시간 재판정 오염 + 중복 방어 2층 관통
1. **실시간 검출을 현재 매매일로 제한** (`detect_from_ms`): `_detect` 가 캘린더의 현재 매매일 시작으로 판정 구간을 자른다. 전일 이벤트를 당일 거래량이 섞인 곡선(그 t0 기준 미래)으로 재판정해 rvol 17.6→3.4 로 무너뜨리고 UPSERT 로 덮어쓰던 경로가 사라진다. 피처/스코어는 이력이 필요하므로 버퍼 전체를 계속 쓴다.
2. **`build_curve` None 을 1시간 캐시하지 않음**: 실패는 5분(`CURVE_NONE_TTL_MS`) 후 재시도. `rvol_gated=False` 기록 자체는 계약 A1 §6 대로 유지 — 고착만 제거.
3. **중복 방어 2층의 키 분리**: 검출기 억제 키를 `(symbol, t0_ms)` → `(symbol, UTC 매매일)` + 매매일당 `max_per_day` 상한으로 변경. t0 가 어디로 이동해도 DB UNIQUE 와 다른 키라 두 번째 행이 안 생기고, 이동은 warn + `t0_shift_suppressed` 카운터로 드러난다. 덤으로 **재기동 시 events 테이블에서 억제 상태를 복원**(`seed_suppression`) — 감사가 지적한 "재시작하면 더 나빠진다" 구멍도 닫힘.
4. **H-6**: `reconfigure_tiers` 가 세션 전환마다 `baselines`/`prev_close`/`history_days` 를 무효화 — 다음 사이클에 자연 재계산.

### 그 외
- **H-9**: `loaded >= CANDLE_PAGE` 백필 스킵 분기 제거 — 이어받기 지점이 있으면 정전 폭만큼 페이지를 늘려(최대 12) 항상 잇는다. 공백이 1페이지 미만이면 직후 실시간 폴링(count=200)이 덮으므로 스킵(기존 빠른 경로 보존). `stop_at_ms` 에 못 닿으면 **반드시 warn** + `backfill_gaps` 카운터. replay 재시작 테스트에 "구멍 없음" 단언 + (symbol,매매일)당 이벤트 ≤1 불변식 추가.
- **H-7**: `force()` 가 dwell(120s)을 더는 우회하지 않는다 — "강등 1ms 뒤 재승격" 차단. 랭킹 점수 분리는 위 A 참조.
- **BudgetGuard 계상**: client `requests` 카운터 고수위 비교로 **재시도·실패로 끝난 HTTP 시도**까지 계상. `_guarded` 의 finally 경로(`sync_rate_limits`)가 실패 호출분을 흡수한다.

## (b) 어떻게 검증했나
- **회귀 테스트 19건 신규** — `git stash` 로 수정 전 코드에 돌려 **전부 실패함을 확인** 후 수정 코드에서 통과 확인. 태스크가 요구한 5개 시나리오 전부 포함:
  대형주 미등록(`test_large_caps_from_rankings_are_rejected_and_small_caps_pass`), 전일 미재판정(`test_previous_day_events_are_not_rejudged`), None 곡선 비고착(`test_curve_failure_is_retried_quickly...`), t0 이동 무중복(`test_t0_shift_within_a_day...`), 백필 미달 warn(`test_unreachable_resume_point_warns_loudly`).
- 전체 스위트 **653 passed** (수정 전 634 + 신규 19), mock 서버 HTTP 통합·가속 replay 포함.
- 통합 테스트에서 mock 픽스처(실서버 캡처 랭킹)의 메가캡 26종 거부 + 소형주(BTAI·NUKK) 승격이 HTTP 전 구간으로 확인됨.

## (c) 이견·판단 사항
- 감사 항목 중 **재현이 안 되거나 동의하지 않는 것 없음** — 7건 전부 코드에서 메커니즘을 확인하고 수정했다.
- 판단 1: 랭킹 유래 promotions 행의 `score` 가 이제 0.0 으로 기록된다(순위 인코딩 제거). 랭킹은 reason='ranking_entry' 로 식별 가능 — W7 분석이 score 를 쓰고 있었다면 알림 필요.
- 판단 2: 검출기 매매일 키는 UTC 날짜다(docs/07 §3.1 — 토스 매매일=UTC 날짜 1:1). 합성 테스트 캘린더가 UTC 자정을 걸치면 상한이 매매일이 아니라 UTC 일 단위로 걸리는데, detect_events 자체가 매매일당 1건이라 실질 차이 없음.
- 판단 3: audit_b I-1 제안 2(events 에 (symbol,매매일) 유니크 = 계약 C-6 변경)는 **하지 않았다** — 검출기 층 키 변경으로 충분하고, C-6 변경은 W2/코디네이터 결정 사항.
- 잔여 리스크: `/stocks` 가 조용히 누락한 심볼(함정1)은 미통과로 처리(보수적). ReplayClient 처럼 `get_stocks` 가 없는 클라이언트에서는 신규 심볼이 미확인으로 남아 등록되지 않는다(기존 워치리스트는 유지).

## (d) 파일·커밋
- 커밋: **cebf32b** (w4-collector, main 79fec4c 직상단, 단일 커밋)
- 수정: `tossmon/collector/loops.py`, `tossmon/collector/detector.py`, `tests/test_collector_{loops,detector,integration,replay}.py` — **전부 소유 경로**. `universe/filters.py` 등 소유 밖 diff 0줄 (`git diff main --stat` 로 확인 가능).

## (e) 남은 것
- **머지는 코디네이터가** W5 야간 수집 종료(05:00) 후 수행 — 나는 커밋까지만 했다.
- 머지 후 첫 재기동 때: 구 상태파일의 대형주 워치리스트는 첫 랭킹 사이클에서 warn 과 함께 자동 정리된다. `symbols` 테이블이 비어 있으면(W2 build_universe 미실행) 시드는 0건이고 랭킹 유입분만 게이트를 지난다 — W2 진입점이 돌기 시작하면 자동으로 시드가 붙는다.
- 세션 전환마다 베이스라인 재계산으로 CHART 호출이 tier2 심볼당 1건씩 늘어난다(세션당 1회) — 예산 여유(22%) 내이며 replay 에서 budget_shrinks=0 확인.

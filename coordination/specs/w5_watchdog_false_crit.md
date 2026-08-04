= W5 — 워치독이 **정상 상태를 사고로 오인해 수집기를 재시작했다** (오늘 아침 실측)

어제 네가 넣은 수정(`c5a3a5f`, "상태 파일을 1차 소스로")이 **새 실명 지점을 만들었다.**
오늘 08:55 에 그것 때문에 수집기가 불필요하게 재시작됐다. 재시작은 이 프로젝트에서
데이터 손실 사고 1위 원인이다 — 이유 없는 재시작을 매일 한 번씩 하고 있다.

## 확정된 근본 원인 (코디네이터가 로그·상태파일·코드로 교차 확인함)

```
08:49:44  collector_state.json 마지막 저장 (saved_ms=1785800984651, session="after")
08:50:09  collector.log: "session after -> closed"
          "session closed: tier caps {tier2_max:1, tier3_max:1} (demoted 139)"
          -> closed 세션에는 랭킹 루프가 쉰다 (scheduler.py 계약 C-8, 설계대로)
          -> 활동이 없으니 collector_state.json 도 더 이상 저장되지 않는다
08:55:10  collector.log 텔레메트리: session=closed, ranking_snap_age_s=311, budget 전부 0.00
08:55:54  watchdog: "session=after" (src=state, 6분 묵은 스냅샷) -> $openSession = true
          -> ranking_snap_age 382s > 300s -> CRIT "ranking loop has silently stopped ...
             every minute of this is permanent data loss" -> 수집기 재시작
08:56:14  collector 재기동 (resumed from ... saved_ms=1785800984651)
```

**즉 상태 파일은 `closed` 로 들어가는 순간 `session="after"` 인 채로 얼어붙고,
워치독은 그 언 값을 1차 소스로 믿는다.** `watchdog.ps1:792` 의 `$openSession` 가드는
있으나, 가드가 읽는 값 자체가 낡아서 무력하다.

**이건 오늘만의 일이 아니다.** 08:50~09:00 KST 는 토스 `/market-calendar/US` 에
**어떤 세션도 배정돼 있지 않은 구간**이다(after 05:00–08:50, day 09:00–17:00).
DB 실측: 랭킹 폴이 `08-04 08:49:59` 에서 끊기고 `08-04 09:00:00` 에 정확히 재개된다
(그 사이 0건). 08-01 에도 마지막 폴이 `08:49:49` 였다. **매일 재발한다.**

## 해야 할 일

1. **1차 소스의 신선도를 검사하라.** `collector_state.json` 의 `saved_ms` 가
   임계(최소한 `RankingSnapAgeCritS`)보다 낡았으면 그 파일의 `session` 을 신뢰하지 마라.
   낡았으면 텔레메트리로 폴백하거나 `session=unknown` 으로 두고 **stall 판정을 하지 마라.**
2. **`ranking_snap_age_s` 자체를 언 상태 파일에서 계산하지 마라.** 지금은 얼어붙은
   `last_ranking_snap_ms` 와 현재 시각의 차이라서, 수집기가 완벽히 정상이어도 시간이
   갈수록 무조건 커진다. 즉 **이 경보는 closed 구간에서 반드시 발화한다.**
3. **세션 없는 구간(08:50–09:00 KST)을 아는 채로 판정하라.** 캘린더에 세션이 없으면
   랭킹 정지는 정상이다. 하드코딩하지 말고 수집기가 쓰는 것과 같은 근거
   (상태 파일의 `session` 또는 텔레메트리)를 쓰되, **낡음 검사를 통과한 값만** 써라.
4. **재시작 사유를 사후에 판별할 수 있게 하라.** 지금 ALERT 파일만 보면
   "랭킹이 조용히 죽었다"고 읽힌다. 실제로는 정상이었다. 오늘 아침 코디네이터가
   이 한 건에 상당한 시간을 썼다 — **아침에 읽는 사람이 30초 안에 구분할 수 있어야 한다.**

## 함께 고칠 것 — `schema_mismatch` CRIT 도 거짓이다

```
2026-08-03 23:16:12 WARNING tier2:BLRK: schema mismatch (skip): http-404 code=stock-not-found
2026-08-03 23:16:26 WARNING tier2:CABR: schema mismatch (skip): http-404 code=stock-not-found
2026-08-03 23:18:13 WARNING tier2:MACI: ... 23:21:31 tier2:JSM: ... (총 5건, 전부 동일)
```

경보 문구는 **"the API response shape changed. A restart will NOT fix this.
Inspect the endpoint contract before trusting today's data."** 다.
실제로는 **없는/상장폐지된 종목 하나를 조회한 404** 다. 데이터를 의심할 이유가 전혀 없다.

- **카운터 분리 자체는 W4 소유**(`loops.py` 의 `_guarded` 가 `SchemaMismatch` 를 통째로
  `schema_mismatch` 로 센다). **네가 고치지 마라** — 코디네이터가 W4 에 별도 발행했다.
- **네 몫은 경보 쪽**이다: W4 가 `symbol_not_found` 를 분리해 내보내면 그것은
  CRIT 이 아니라 NOTE 로 떨어지게 하고, 진짜 스키마 변경만 CRIT 으로 남겨라.
  W4 수정이 머지되기 전이라도 **문구가 근거보다 강하게 단정하지 않도록** 먼저 낮춰라.

## 규율

- **가동 중 수집기를 직접 죽이거나 띄우지 마라.** STOP 파일 + 워치독 경로만.
- 소유 경로(`ops/**`, `docs/11`)만. 수집기 코드(`tossmon/**`)를 건드려야 한다는 결론이
  나오면 **고치지 말고 보고하라** — W4 로 넘긴다.
- 브랜치 `w5-ops`, pytest green, 콘솔 출력 비ASCII 금지(cp949 로 죽는다).
- **네가 만든 가드를 직접 깨보고 보고하라.** 이 프로젝트에서 "가드를 만들었다"는 보고가
  실제로는 목록에 적힌 것만 검사한 사례가 세 번 있었다. 언 상태 파일을 인위적으로 만들어
  경보가 안 뜨는지, 그리고 **진짜 랭킹 정지에는 여전히 뜨는지** 둘 다 시험하라.
- `worker_done` 1회(ASCII), (a) 무엇을 했나 (b) 어떻게 검증했나 (c) 무엇을 못 했나
  (d) 소유권 밖 발견 (e) 다음 사람이 알아야 할 것.

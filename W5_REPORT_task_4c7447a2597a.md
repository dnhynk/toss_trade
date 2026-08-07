# W5 보고 — 결번된 아침 리포트 따라잡기 + 재시작 경보의 자기평가 문구

task_4c7447a2597a / 브랜치 `w5-ops` / 2026-08-07
소유 범위 준수: 바꾼 파일은 `ops/**`, `tests/test_ops_daily_health_catchup.py`, `docs/34` 뿐.
`tossmon/**` 무수정 (`git diff --name-only 1ffa45d HEAD | grep ^tossmon/` → 0건).

**판정하지 않는다.** 아래 수치에는 전부 측정 조건을 붙였다.

---

## 1. 결번된 아침 리포트 따라잡기

### 무엇을 했나

`ops/daily_health.py` 에 `run_catchup()` 을 붙였다. **기준은 파일의 존재**다 —
`data/daily_health_YYYYMMDD.txt` 가 없는 날을 채운다. 별도의 "마지막 실행" 상태 파일을
두지 않은 이유: 상태 파일은 기계가 죽을 때 같이 죽거나 산출물과 어긋나지만, 산출물
자체는 어긋날 수 없다.

| 요구 | 어떻게 |
|---|---|
| 빠진 날을 채운다 | `main()` 이 오늘 것을 쓴 뒤 `run_catchup()` 실행 (`--date` 를 준 수동 호출에서는 안 돈다 — 사람이 특정 날을 지목한 것이므로) |
| 이미 있는 파일은 절대 안 덮는다 | 두 겹. `missing_labels()` 가 목록에서 빼고, `write_if_absent()` 가 다시 거부한다 |
| 상한을 설정에 노출 + 기본값 근거 | `daily_health.catchup_days` (기본 **7일**). 근거는 `ops/ops_config.example.yaml` 주석과 `opsconfig.DEFAULT_CATCHUP_DAYS` 에 한 줄로 |
| 따라잡은 파일 구분 | 제목에 `[CATCH-UP]`, 머리말에 몇 시간 늦었는지 + 사후 재구성이 **못 보는 것** |
| 데이터가 아예 없는 날 | 파일은 남기되 사유는 판정하지 않는다(기계 꺼짐/수집기 부재/휴장/DB 결손을 못 가른다). "**이 파일의 존재는 '봤다'는 뜻이지 '수집됐다'는 뜻이 아니다**" 를 본문에 적는다 |
| 회귀 테스트 | `tests/test_ops_daily_health_catchup.py` 12건 |

### 기본값 7일의 근거 (요구사항이라 따로)

사후 재구성이 읽는 `collector.log` 의 회전 보존이 `log_retention_days`(기본 14)일이다.
그 절반이면 `[체결 tape gap]` 절이 아직 살아 있다. 1일 상한은 주말을 낀 다일 정전을 못
넘긴다(08-06 은 하루였지만 그것이 상한을 정할 근거는 아니다). 비용 실측: 2GB DB 에서
**하루 재구성 ≈12초**(3창 37초, 아래 실행 로그). 상한을 늘리면 오래된 결번 하나가 정시
리포트를 그만큼 늦춘다.

**상한 밖이라 포기한 결번은 stdout 에 목록으로 찍는다** — 조용히 자르는 상한은 "다 봤다"로
읽힌다.

### 범위에 대해 내가 한 판단 (합의 필요하면 되돌릴 수 있다)

1. **기록열을 과거로 소급 확장하지 않는다.** 채우는 범위는 *가장 오래된 리포트의 다음 날
   ~ 어제* 안의 결번뿐. 이게 없으면 상한 7일만큼, 수집기가 존재하지도 않던 날에 "0건"
   파일을 찍어낸다. 실제로 이 규칙 덕분에 08-01 이전은 안 만들었다.
2. **오늘 것(정시 경로)은 예전처럼 덮어쓴다.** "덮지 마라"는 따라잡기의 규칙으로 읽었다 —
   08:52 실행이 중간에 죽어 부분 파일이 남았을 때 재실행으로 고칠 수 있어야 한다.
   대신 `--date` 로 **지난 날**을 부르면 그것도 사후 재구성이므로 `[CATCH-UP]` 이 붙는다.
3. **ALERT/NOTE/PLANNED/TRADEOFF 목록의 기준 창을 "지금부터 24시간"에서 리포트 창으로
   바꿨다.** 안 바꾸면 08-06 리포트에 08-07 의 ALERT 가 실린다. 정시 경로에서 무해함을
   실측으로 확인했다 — 08-07 정시분과 재생성분의 개수가 2/14/0/28 로 동일.
4. **`last telemetry` 절은 따라잡기 분에서 생략.** 그 꼬리는 생성 시각의 상태다.

### 직접 실행해 확인한 것

```
$ .venv/Scripts/python.exe -c "... DH.missing_labels(cfg.log_dir, today, cfg.daily_health_catchup_days)"
log_dir = data | catchup_days = 7
existing: ['20260801', '20260803', '20260804', '20260805', '20260807']
today label: 20260807
todo, dropped = (['20260802', '20260806'], [])
```

```
$ time .venv/Scripts/python.exe -m ops.daily_health          # 09:55:14 -> 09:55:51
real    0m36.822s
written: data\daily_health_20260807.txt
catch-up: made=2 ['20260802', '20260806'] skipped(exists)=0 failed=[]
```

만들어진 `data/daily_health_20260806.txt` 머리 (결번돼서 아무도 못 봤던 그 창):

```
=== tossmon daily health 20260806 ===  [CATCH-UP]
window (KST): 2026-08-05 09:00 ~ 2026-08-06 08:50
generated: 2026-08-07 09:55:40
!! CATCH-UP — 정시(스케줄러 08:52)에 만들어지지 않은 리포트다. 창이 끝난 지 25.1시간 뒤에 사후 재구성했다.
   결번 사유는 이 파일이 모른다(기계 꺼짐 / 스케줄러 미실행 / 실행 실패). ...
   사후 재구성이 못 보는 것:
   - [체결 tape gap]·config_sig·수집기 재기동 횟수는 collector.log 에만 남는다. 회전 보존 14일 밖이면
     그 절은 비었거나 부분적이다 — '결손 0건'이 아니라 **'못 봤다'**로 읽을 것.
   - 'last telemetry' 절은 싣지 않았다. ...
...
    max_hole        : 295.5min  [trailing_hole]   <= 이 창의 진짜 최대 공백
    trailing_hole   : 295.5min  (2026-08-06 03:54:30 -> 2026-08-06 08:50:00)
```

값이 코디네이터의 수동 재실행분(`data/gap_audit_20260806_recovered.txt`)과 일치한다:
tape gap A=221 / B=83 / C=139, 최대 공백 295.5분. **로그 회전 안이라 tape gap 절이 살아
있었다** — 상한 7일이 그 한계 안이라는 근거이기도 하다.

멱등성 실측 (두 번째 실행):

```
$ md5sum data/daily_health_*.txt > before ; .venv/Scripts/python.exe -m ops.daily_health ; md5sum ... > after ; diff before after
catch-up: made=0 [] skipped(exists)=0 failed=[]
7c7   (오늘 것 20260807 만 다름 — 정시 경로는 예전처럼 덮어쓴다)
```

20260802·20260806 과 기존 파일 전부 md5 동일.

테스트:

```
$ .venv/Scripts/python.exe -m pytest tests/test_ops_daily_health_catchup.py -q
12 passed

# 변이 주입(바닥 규칙 제거 + 덮어쓰기 허용) -> 6 failed / 6 passed. 되돌리면 12 passed.
```

기존 ops 스위트 회귀 없음: `113 passed`
(config / gap_audit / healthcheck / supervisor / rotate_logs / disk_guard /
daily_health_catchup / secret_scan).

### 한 가지 사족

Windows 작업 스케줄러에는 놓친 실행을 부팅 직후 돌리는 `StartWhenAvailable` 옵션이 있고,
08-06 이라면 09:36 에 돌았을 것이다. **안 건드렸다** — `ops/register_task_scheduler.ps1`
재등록이 필요해 요청 범위 밖이고, 다일 정전은 여전히 못 넘긴다. 둘을 같이 쓸지는
**판정하지 않았다**(`docs/34` §9.3 에 사용자 결정으로 남겼다).

---

## 2. 재시작 경보의 자기평가 문구

### 무엇을 했나

`ops/watchdog.ps1` 의 `act` 블록에서 `$judgeLines` 를 사유 계열별로 갈랐다.

- **프로세스 부재**(`process_dead`/`supervisor_dead`/`collector_dead`): 판단 근거가
  `sup`/`col` 카운트임을 적는다. 둘 다 0이면 재시작은 옳고, `session NOT TRUSTED` 줄은
  그것을 약화시키지 않는다("없는 것을 세션 신선도로 변호할 수 없다"). 대신 **`sup` 이나
  `col` 이 0이 아닌데 재시작했다면 그것이 결함**이라고 뒤집어 적는다. 왜 죽었는지는 이
  파일이 모른다는 것도 적는다.
- **진행 정지**(`log_stale`/`counters_frozen`/`ranking_snap_*`/`auth_failures`/`token_dead`):
  **문구 한 글자도 안 바꿨다.** 08-04 에 비싸게 얻은 것이다.
- `ops/watchdog.ps1` 머리말 `FILE GRADE CONTRACT` 에 "새 재시작 사유를 추가하면 계열을
  먼저 정하라 + 목록에 없으면 진행-정지 문구가 조용히 붙는다"를 넣었다. 사유를 추가하는
  사람은 855줄 근처를 고치지 파일 끝의 `act` 블록을 안 볼 수 있다.
- 그 외 리팩터링 없음.

### 테스트를 어떻게 넓혔나

`ops/watchdog_selftest.ps1:367` 은 문구의 **존재**만 봤다 — 08-06 에도 그 문구는 존재했다.

- **T3(프로세스 부재)** 을 08-06 사건 재현으로 바꿨다: state/log 둘 다 3000초 → 세션
  미신뢰, `sup=0 col=0`. 이제 `PROCESS COUNTS ... sup=0 col=0` 을 **요구**하고
  `probably wrong` 을 **금지**한다.
- **T1(진행 정지)** 은 정확히 그 반대: `NOT TRUSTED.*probably wrong` 요구,
  `PROCESS COUNTS` 금지.

### 직접 실행해 확인한 것

```
$ powershell -NoProfile -ExecutionPolicy Bypass -File ops\watchdog_selftest.ps1
  PASS  T1: a progress-stall restart is judged on session freshness
  PASS  T1: and NOT on the process counts
  PASS  T3: the session really was untrusted
  PASS  T3: a process-absence restart is judged on the sup/col counts
  PASS  T3: it does not blame the stale session reading
  PASS  T3: it says an absent process cannot be defended by a session reading
RESULT: 81 passed, 0 failed
```

**실패할 수 있음을 보였다** — 공용 문구로 되돌리는 변이를 넣으면:

```
  FAIL  T3: a process-absence restart is judged on the sup/col counts
  FAIL  T3: it does not blame the stale session reading
  FAIL  T3: it says an absent process cannot be defended by a session reading
RESULT: 78 passed, 3 failed
```

되돌린 뒤 다시 81/0.

---

## 3. 문서

`docs/34` 에 **§9** 를 더했다. 프레임: *§1~§8 은 "어느 접두사를 붙일 것인가"였고, 08-06 이
그 다음 두 층을 보여줬다.*

- §9.1 접두사가 맞아도 **본문이 틀린 행동을 시키면** 등급은 아무 일도 안 한 것과 같다
  (계열 표 + 새 사유 추가자용 강제 규칙 + 변이 실측)
- §9.2 **파일이 없으면 등급을 붙일 대상 자체가 없다.** 무등급이 조용히 무사고로 읽히는
  것이 §1 의 자기모순과 같은 종류의 실패다 (따라잡기 규칙 6개 + 실측)
- §9.3 안 한 것 / 판정 안 한 것
- §6 예시 블록의 `(24h,` 를 `(창 안,` 으로 맞췄다

---

## 4. 남은 것 / 사용자가 봐야 할 것

- **`ops/ops_config.yaml`(로컬 실사용 설정)은 커밋하지 않았다.** 작업 시작 시점에도
  untracked 였다(`?? ops/ops_config.yaml`). 여기에 `daily_health.catchup_days: 7` 을 넣어
  두긴 했지만, 이 파일을 추적할지는 내가 정할 일이 아니라고 봤다. 추적 안 하는 다른
  기계에서는 `opsconfig.DEFAULT_CATCHUP_DAYS`(7)가 그대로 먹는다.
- **`data/daily_health_20260807.txt` 는 08:52 정시 생성분으로 되돌려 놨다.** 검증하느라 두
  번 덮어썼다. 되돌린 이유는 이 작업에서 세운 규칙("아침 리포트는 기록이다")을 스스로
  어기지 않기 위해서다. 새로 만들어진 `daily_health_20260802.txt` /
  `daily_health_20260806.txt` 는 그대로 뒀다(`data/` 는 gitignore 대상이라 커밋 안 됨).
- **08-02 도 결번이었다** — 아무도 몰랐다. 채워 보니 그 창은 `rankings_snap 0건`,
  `candles_1m 62건`, `max_hole 1430.0min [whole_window]` 다. 창 전체가 빈 것은 아니라서
  "데이터가 하나도 없다" 절은 안 붙었다. **무슨 일이 있었는지는 판정하지 않았다** —
  파일이 생겼으니 이제 볼 수 있다.
- 상한 7일은 **가정이 아니라 선택**이다. 로그 회전 보존(14일)을 늘리면 같이 늘릴 수 있다.

---

## 커밋

```
830eeb9 W5: write_if_absent 가 실패해도 임시 조각을 남기지 않게
61a46a8 W5: docs/34 §9 — 등급은 본문이 시키는 행동이고, 파일의 부재도 한 등급이다
4e32ad4 W5: 재시작 자기평가 문구를 사유 계열별로 갈랐다 (프로세스 부재 vs 진행 정지)
67ab0c7 W5: 따라잡기 회귀 테스트 — 채워지는가 / 두 번 돌려도 안 바뀌는가
bf81c3b W5: 결번된 아침 리포트 따라잡기 — 파일 존재 기준, 덮지 않음, 상한 노출
```

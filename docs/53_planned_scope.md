# 53. 계획 표식의 범위 — 창이 아니라 **키**를 덮는다 (2026-08-12, 소유: W5)

사용자 지시: **"계획 표식이 인프라 죽음까지 덮었다. 키 단위로 좁혀라."**

계약 본문 요약은 `ops/watchdog.ps1` 머리말(`PLANNED SCOPE`)에도 같은 내용으로 들어 있다.
등급 4종(`ALERT_`/`PLANNED_`/`NOTE_`/`TRADEOFF_`) 자체의 계약은 `docs/34` 다. **이 문서는
새 등급을 만들지 않는다** — `PLANNED_` 를 **언제 붙일 수 있는가**만 좁힌다.

---

## 1. 무엇이 일어났나

수집기는 사용자 결정으로 08:39 부터 정지 중이었고, 사유가 *"수집 정지"* 인 계획 표식이
`data/ops_state/PLANNED` 에 걸려 있었다(`until=20:10`).

**그 사이 워치독이 08:46:09 부터 10:48:20 까지 2시간 2분 동안 돌지 않았다.**
센티널이 잡았다:

```
data/PLANNED_20260812_104717_watchdog_silent.txt
  [CRIT] key=watchdog_silent
  Watchdog heartbeat is 7263s old (threshold 900 s). Re-kicking task 'tossmon-watchdog'.
```

**접두사가 `PLANNED_` 다.** `docs/34` 표에서 `PLANNED_` 는 *"사람이 일부러 한 것 → 무시"*
다. 워치독을 일부러 죽인 사람은 없다. 그리고 **20:00 에 수집기를 되살릴 주체가 바로 그
워치독**이다 — 그게 죽은 것을 "무시"로 찍으면 **복귀가 조용히 실패한다.**

`docs/34` §1 이 고친 자기모순의 **반대 방향**이다. 그때는 설계된 동작이 `ALERT_` 로
나갔고("ALERT 가 있으면 문제"가 깨졌다), 이번엔 진짜 문제가 `PLANNED_` 안에 숨었다.
같은 약속이 반대쪽에서 깨졌다.

## 2. 원인 — 한 줄

`ops/watchdog.ps1` 의 `Raise-Alert` 안:

```powershell
$planned = $alwaysPlanned -or $script:PlannedNow.active
```

**주기 전체가 통째로 `PLANNED_` 다. 키를 안 본다.** 표식의 사유는 사람이 쓴 자유 문장이라
코드가 읽을 수 없고, 코드는 읽으려고 시도조차 하지 않았다. 그 결과 *"수집을 껐다"* 라는
표식이 **수집과 무관한 모든 것**까지 덮었다.

`watchdog.log` 도 같은 말을 글자 그대로 적고 있었다 — *"alerts this cycle are written as
PLANNED_"*. 버그가 로그에 문장으로 적혀 있었고, 그게 맞는 말이었던 것이 문제였다.

## 3. 규칙 — 무엇이 덮여도 되는가

> **계획 창은 계획된 행위가 스스로 만들어내는 것만 덮는다 = 수집이 일어나지 않는 것.**
> **보는 일(watching) 자체가 실패한 것은 절대 덮지 않는다.**

가르는 질문은 `docs/34` §7 과 같은 모양이다:

> **운영자가 수집기를 세우거나 재시작하면, 이 관측이 직접·당연히 따라 나오는가?**

- **예** → 덮어도 된다. 계획 정지 중에 수집기 프로세스가 없는 것은 **정상이다.**
- **아니오** → 덮지 않는다. 카운터가 **오른** 것, 로그 줄이 **나타난** 것, 태스크가
  **돌다 죽은** 것, 디스크·전원·스키마, 그리고 **워치독·센티널의 생존**은
  수집 정지가 만들어내지 않는다.

### 목록은 **허용목록(allowlist)** 이다

분류되지 않은 키는 **덮지 않는다.** 방향을 이렇게 잡은 근거:

- 잘못 덮으면 → **조용한 2시간**(오늘 실측). 아무도 모른다.
- 잘못 안 덮으면 → 계획 창 동안 **파일 한 건이 시끄럽다.** 아침에 읽고 넘기면 된다.

비대칭이 크다. **애매하면 목록에서 뺀다.** 새 검사를 추가하는 사람이 목록을 잊어도
안전한 쪽으로 실패한다.

## 4. 전수 분류표 — 경보 키 57종

`Raise-Alert` 호출 지점 29곳을 전수로 훑고, 표 기반 로그 패턴 6종·쓰기실패 루프 3종과
동적 키를 **전부 펼쳤다**: 재시작 사유 9종 × (`watch_` + `restarted_`) = 18,
감시 대상 태스크 5종 × `task_result_` = 5. **하나도 빼지 않았다.**

(`docs/34` §4 의 "37종"과 다른 것은 그 뒤로 로그 패턴·태스크 결과 검사가 늘었고,
여기서는 동적 키를 접지 않고 펼쳐 셌기 때문이다.)

### 4.1 덮어도 되는 키 — 18종 (수집 부재)

| 키 | 등급 | 근거 |
|---|---|---|
| `stop_observed` | `NOTE`→`PLANNED` | STOP 파일 자체가 **계획된 행위 그 자체**다. 표식과 무관하게 항상 `PLANNED_`(`alwaysPlanned`) |
| `no_telemetry` | CRIT | 멎은 수집기는 텔레메트리를 안 쓴다. 직접적 귀결 |
| `ranking_snap_stalled` | CRIT | 수집이 꺼져 있으면 랭킹 루프는 못 돈다 |
| `ranking_stall_suppressed` | INFO | 위 판정을 **거절한** 기록. 같은 가족 |
| `watch_process_dead` | CRIT | 감독자·수집기 부재 = 계획 정지의 모습 그 자체 |
| `watch_supervisor_dead` | CRIT | 동일 |
| `watch_collector_dead` | CRIT | 동일 |
| `watch_log_stale` | CRIT | 수집이 멎으면 로그도 멎는다 |
| `watch_counters_frozen` | CRIT | 수집이 멎으면 카운터도 언다 |
| `watch_ranking_snap_never` | CRIT | 위 `ranking_*` 와 같은 근거 |
| `watch_ranking_snap_stalled` | CRIT | 동일 |
| `restarted_process_dead` 외 6종 | WARN | ↓ |

**`restarted_<사유>` 는 `watch_<사유>` 를 따라간다.** 재시작 하나가 파일 두 개를 남기는데
둘을 다르게 매기면 `docs/34` §1 의 자기모순(같은 사건, 두 등급)을 다시 만든다.

> **셀프테스트가 못 건드리는 곳 — 적어 둔다.** `-DryRunRestart` 는 `restarted_*` 경보를
> 쓰기 **전에** 반환한다. 그래서 `restarted_*` 의 등급은 위 규칙과 목록 소속으로만 보장되고
> 실행으로는 검증되지 않는다. 대신 P11 이 **같은 주기에서 두 등급이 갈리는 것**을
> `restart_budget_exhausted` 로 실행 검증한다.

### 4.2 절대 덮지 않는 키 — 39종

**(가) 보는 일 자체가 실패했다 — 오늘 사고의 가족**

| 키 | 등급 | 근거 |
|---|---|---|
| **`watchdog_silent`** | CRIT | ★ 오늘의 사고. 워치독이 **20:00 복귀의 주체**다 |
| **`watchdog_heartbeat_missing`** | CRIT | 같은 가족, 다른 분기(심장박동 파일 자체가 없음) |
| **`sentinel_silent`** | WARN | 상호 감시의 반대쪽. 센티널이 죽으면 **워치독을 되살릴 사람이 없다** |
| **`restart_budget_exhausted`** | CRIT | 워치독이 **재시작을 포기했다**는 선언. 계획 정지 중이든 아니든, 이게 켜져 있으면 복귀가 안 된다 |

**(나) 태스크 실행 실패 — 사용자가 명시**

| 키 | 등급 | 근거 |
|---|---|---|
| `task_result_tossmon_watchdog` | WARN | 수집을 꺼도 태스크가 0 이 아닌 값으로 죽지는 않는다 |
| `task_result_tossmon_sentinel` | WARN | 오늘 08:32:53 에 `0xC000013A` 로 실제로 죽었다 |
| `task_result_tossmon_dailyhealth` | WARN | 오늘 08:57:30 에 같은 코드로 죽었다 |
| `task_result_tossmon_logrotate` | WARN | 동일 가족 |
| `task_result_tossmon_collector_oneshot` | WARN | 동일 가족 |

(같은 키가 "등록 안 됨"(`not registered`)에도 쓰인다 — 둘 다 덮지 않는다.)

**(다) 기계 밑바닥 — 사용자가 명시**

| 키 | 등급 | 근거 |
|---|---|---|
| `disk_warn` / `disk_reclaim` / `disk_critical` | WARN/WARN/CRIT | 수집을 껐다고 디스크가 안 찬다는 보장이 없다. 오히려 계획 창은 에이전트가 스크래치를 쌓는 시간이다 |
| `on_battery` / `battery_low` | WARN/CRIT | 배터리가 다 되면 기계가 죽고 **20:00 복귀도 죽는다** |
| `power_restored` | INFO | 위 짝. 덮으면 전원 이야기가 반쪽만 남는다 |

**(라) 수집기가 돌면서 스스로 신고한 고장 — "부재"가 아니라 "발생"**

| 키 | 등급 | 근거 |
|---|---|---|
| `schema_mismatch` | INFO/WARN/CRIT | 사용자가 명시. 응답 **형태**가 바뀐 것은 정지와 무관 |
| `auth_failures` | CRIT | 멎은 수집기는 실패할 인증 호출이 없다. **오르는** 카운터다 |
| `symbol_not_found` | INFO | 동일 |
| `rankings_clamped` | INFO | 동일 |
| `loop_errors_surge` | WARN | 동일 |
| `event_write_failures` | WARN | 데이터가 DB 에 못 들어간다. 디스크와 이웃 |
| `promotion_write_failures` | WARN | 동일 |
| `rankings_write_failures` | WARN | 동일 |
| `fetch_success_low` | WARN | **애매 → 안 덮는다.** 정지 중엔 세션 게이트에 막혀 발화하지 못하고, 재배포 중 발화했다면 그건 볼 가치가 있다 |
| `candles_flat` | WARN | **애매 → 안 덮는다.** `docs/34` §4 의 남은 1건이라 원인도 아직 안 갈린다. 두 겹으로 애매한 것을 덮을 이유가 없다 |
| `tier2_orderbook_flat` | WARN | **애매 → 안 덮는다.** 진짜 정지·오설정을 뜻하는 판정이다 |
| `tier2_orderbook_yield` | **TRADEOFF** | 덮으면 **등급 하나가 사라진다.** `PLANNED_`(무시)로 바뀌면 사용자가 판단할 기회가 없어진다 — `docs/34` §2 의 4항목이 통째로 죽는다 |
| `tier2_orderbook_no_members` | INFO | 같은 가족. 판정마다 키를 나눈 `docs/34` §3 의 이유를 등급에서도 지킨다 |
| `tier2_orderbook_skipped` | INFO | 동일 |

**(라-2) 재시작 사유 중 목록에 넣지 않은 2종 — 4키**

| 키 | 등급 | 근거 |
|---|---|---|
| `watch_auth_failures` / `restarted_auth_failures` | CRIT/WARN | 멎은 수집기는 실패할 인증 호출이 없다. 이 사유로 재시작이 걸렸다면 수집기가 **돌면서** 인증에 실패한 것이다 |
| `watch_token_dead` / `restarted_token_dead` | CRIT/WARN | 발화 조건이 **열린 세션 + 만료 토큰 + RuntimeError 반복**이다. 정지 상태가 만들어낼 수 있는 모양이 아니다 |

**(마) 로그 줄이 나타났다 — 6종 모두 안 덮는다**

`log_auth_failure`(CRIT) / `log_forbidden`(WARN) / `log_rankings_store`(WARN) /
`log_rankings_clamp`(INFO) / `log_precision_drift`(WARN) / `log_tape_gap`(INFO).

여섯 개 다 *"줄이 하나 나타났다"* 탐지기다. **부재가 아니라 발생**이므로 규칙상 안 덮는다.
`log_tape_gap` 만 재시작 직후에 정상적으로 늘어나는데, 그것도 안 덮는다 — 등급이 `INFO`
(=`NOTE_`, "참고")라 어차피 아침에 문제로 읽히지 않고, 예외를 하나 만들면 규칙의
가르는 힘이 그만큼 줄기 때문이다.

### 4.3 합계

**57 키 = 덮음 18 / 안 덮음 39.**

- 덮음 18 = 고정 키 4(`stop_observed`, `no_telemetry`, `ranking_snap_stalled`,
  `ranking_stall_suppressed`) + 재시작 사유 7종 × 2(`watch_`/`restarted_`).
- 안 덮음 39 = (가) 4 + (나) 5 + (다) 6 + (라) 14 + (라-2) 4 + (마) 6.

애매하다고 판단해 **안 덮는 쪽으로 기울인 것 4건**
(`fetch_success_low`, `candles_flat`, `tier2_orderbook_flat`, `auth_failures` 계열).

## 5. 증명 — 고치기 전 실패를 먼저 보였다

`docs/30` §3 규율. `ops/watchdog_selftest.ps1` 을 104 → **130 단언**으로 넓혔다
(P 계열 26개 신설). 진짜 워치독을 샌드박스에서 `-DryRunRestart` 로 돌린다
(가동 중 수집기 무접촉).

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ops\watchdog_selftest.ps1
```

**수정 전 111 passed / 18 failed → 수정 후 130 passed / 0 failed.**

| 케이스 | 수정 전 | 수정 후 |
|---|---|---|
| **P1 계획 창 + 워치독 심장박동 7263s → `ALERT_`** (오늘 사고 재현) | **FAIL** (`PLANNED_` 냄) | PASS |
| P2 계획 창 + 심장박동 파일 없음 → `ALERT_` | **FAIL** | PASS |
| **P3 계획 창 + 수집기 부재 → 여전히 `PLANNED_`** | **PASS** | **PASS** |
| **P3 `PLANNED_` 머리말이 `window until`·`reason:` 유지** | **PASS** | **PASS** |
| **P4 STOP 기록은 표식 없이도 `PLANNED_`** | **PASS** | **PASS** |
| P5 계획 창 + 태스크 사망 → `ALERT_` | **FAIL** | PASS |
| P6 계획 창 + 디스크 경고 → `ALERT_` | **FAIL** | PASS |
| P7 계획 창 + 센티널 침묵 → `ALERT_` | **FAIL** | PASS |
| P8 거절이 파일과 로그에 보인다 | **FAIL** | PASS |
| **P9 표식 없음 + 워치독 침묵 → 여전히 `ALERT_`** | **PASS** | **PASS** |
| P10 두 언어의 목록이 같다 | **FAIL** (울타리 없음) | PASS |
| P11 **한 주기에서 두 등급이 갈린다** | **FAIL** | PASS |

**대조군이 핵심이다.** `PLANNED_` 를 지키는 대조 단언 **6개**(P3 4, P4 2)와 `ALERT_` 를
지키는 **1개**(P9)가 수정 전후 모두 통과한다 — *경보를 그냥 켠 게 아니라는 증거*다.
이 대조가 없으면 "잘못된 `PLANNED_` 가 사라졌다"와 "계획 창이 아예 망가졌다"를 구분할 수 없다.

**P11 이 이 문서의 한 문장 요약이다.** 한 주기 안에서 수집기 부재는 `PLANNED_` 로,
바닥난 재시작 예산은 `ALERT_` 로 동시에 나간다. 창이 아니라 키를 덮는다는 뜻이다.

**P3 의 머리말 단언은 회귀 방어다.** `gap_audit.planned_windows()` 는 지워진 라이브
마커 대신 `PLANNED_*.txt` 의 머리말(`window until ...` / `reason:`)로 과거 창을 복원한다
(`docs/34` §6). 새로 붙인 *"NOT MASKED"* 머리말은 `PLANNED_` 가 **아닌** 파일에만 붙으므로
그 파서에 닿지 않고, 그 사실을 P3 이 지킨다.

**P10 은 언어 간 표류 방어다.** 목록이 두 곳(PowerShell·Python)에 있는데 어긋나면
`docs/34` §1 의 병이 그대로 재발한다(같은 사건, 읽는 곳에 따라 다른 등급). 두 파일 모두
`PLANNED-SCOPE-LIST-BEGIN/END` 울타리 안에 목록을 두고, P10 이 울타리 안 토큰을 비교한다.

## 6. 오늘 것을 소급해서 보이게 한 방법

`PLANNED_20260812_104717_watchdog_silent.txt` 는 **지우지 않았다.** 이름도 안 바꿨다.
아침 리포트는 기록이고, 뒤늦은 실행이 과거를 다시 쓰지 않는다(`docs/34` §9.2).
`ALERT_` 파일을 새로 만들지도 않았다 — **파일을 만드는 권한은 워치독에만 있다**(`docs/34` §6).

대신 **읽는 쪽에서 다시 판정한다.** `ops/daily_health.py` 가 리포트 창 안의 `PLANNED_`
파일 이름에서 키를 뽑아 허용목록과 대조하고, 목록에 없는 것을 `!!` 줄로 따로 낸다:

```
PLANNED files (창 안, 계획된 정비 — 무시): N
  PLANNED_20260812_104717_watchdog_silent.txt
!! 위 N건 중 1건은 계획 창이 덮을 수 없는 키다 — 계획 정비가 아니라 **진짜 문제**로
   읽어라 (워치독·센티널 생존 / 태스크 실행 실패 / 디스크 / 스키마):
     PLANNED_20260812_104717_watchdog_silent.txt  [key=watchdog_silent]
     ↑ 이 파일들은 접두사만 PLANNED_ 다.
```

**이 선택의 근거 세 가지:**

1. **소급이 공짜다.** 이미 디스크에 있는 파일을 다시 읽는 것이라 옛 워치독이 쓴 것에도
   그대로 적용된다. 실측: 현재 `data/` 의 `PLANNED_` 35건 중 **10건**이 걸린다
   (오늘 2건 + 08-04~08 의 `log_tape_gap` 5건 · `disk_reclaim` 1건 ·
   `tier2_orderbook_skipped` 1건 · `tier2_orderbook_flat` 1건).
2. **아침 점검이 사람이 실제로 읽는 한 장이다.** 사용자 지시의 *"아침 점검이 이걸 놓치지
   않을 방법"* 이 그대로 여기다.
3. **등급 체계를 안 늘렸다.** 새 접두사를 만들지 않았다(`docs/34` §6 의 금지).

> **창 경계는 그대로다.** 오늘 08:52 리포트의 창은 08:50 에 끝났고 문제의 파일은 10:47 에
> 생겼다. 그래서 이 파일은 **내일(08-13) 아침 리포트**에서 `!!` 로 잡힌다 — `docs/34` §6 의
> "창 기준" 규칙을 어기면서까지 오늘 리포트에 소급해 끼워 넣지는 않았다.

## 7. 센티널 `0xC000013A` — 갈랐다 (하나는 갈랐고, 하나는 못 갈랐다)

지시: *"갈라지면 갈라라. 못 갈라도 된다 — 못 갈랐다고 적어라."*

### 7.1 갈랐다 — 워치독 2시간 침묵은 **콘솔 피살이 아니라 Modern Standby** 였다

Windows 시스템 이벤트 로그(`Kernel-Power`) 실측:

```
2026-08-12 08:47:07  506  The system is entering Modern Standby
2026-08-12 08:57:21  507  The system is exiting  Modern Standby
2026-08-12 08:57:21  506  The system is entering Modern Standby
2026-08-12 10:44:20  507  The system is exiting  Modern Standby
```

`data/watchdog.log` 의 구멍과 맞춰 보면 **정확히 겹친다**:

| 시각 | 사실 |
|---|---|
| 08:46:09 | 워치독 마지막 정상 주기 |
| **08:47:07** | **Modern Standby 진입** |
| 08:57:21 | 10초짜리 깨어남 → 이때 `tossmon-dailyhealth` 가 08:57:30 에 돌고 `0xC000013A` 로 죽음 |
| 08:57:21 | 다시 진입 |
| **10:44:20** | **Standby 이탈** |
| 10:47:15 / 10:48:20 | 센티널·워치독이 **정규 주기(:02/:32, 5분)에서 벗어난 시각**에 복귀 = 놓친 실행 따라잡기 |

**결론: 워치독은 죽은 게 아니라 기계가 잤다.** 센티널의 `0xC000013A`(콘솔 제어 이벤트 피살,
08:32:53)와 **원인이 다르다.** 센티널 사망은 Standby 진입 **14분 전**이라 시간상으로도
겹치지 않는다.

### 7.2 못 갈랐다 — 센티널 08:32:53 의 `0xC000013A` 자체

무엇이 콘솔 제어 이벤트를 보냈는지는 **증거가 없다.** 근거:

- `Microsoft-Windows-TaskScheduler/Operational` 로그가 **비어 있다**(`docs/38` §8 과 같은 상태).
- 정황은 하나 있다: 같은 창에 Windows Update 가 `9PLM9XGG6VKS-OpenAI.Codex` 를
  08:31:55~08:32:19 에 설치했고(앱 강제 종료를 동반한다), 센티널은 그 34초 뒤인
  08:32:53 에 죽었다. **정황이지 증거가 아니다** — 판정하지 않는다.
- 사용자가 제시한 가설(에이전트 세션의 `schtasks /run` 에 Ctrl+C 가 전파)도 배제하지
  못한다. 둘 다 같은 신호(콘솔 제어 이벤트)를 만든다.

### 7.3 ★ 진행 중인 사실 — 워치독 태스크가 **지금도** 같은 코드로 죽고 있다

작업 중 실측(읽기 전용 `Get-ScheduledTaskInfo`):

```
tossmon-watchdog  last=2026-08-12 11:30:53  result=3221225786 (0xC000013A)
```

그 11:30:53 실행은 `watchdog.log` 에 **한 줄도 안 남겼다** — 첫 `Write-Log` 전에 죽었다는
뜻이다. 그 결과 심장박동이 11:26 에서 멈췄고, 12:05:05 에 센티널이 다시
`watchdog_silent`(1735s)를 냈다(`PLANNED_20260812_120509_watchdog_silent.txt` — 이 파일도
§6 의 재판정에 걸린다). 12:16:02 → 12:25:59 사이에도 한 주기가 비었다.

이 태스크는 `Principal.LogonType = Interactive` 로 등록돼 있다 — **대화형 세션에서 도는
콘솔 프로세스**이므로 콘솔 제어 이벤트가 닿을 수 있는 자리에 있다. **고치지 않았다**:
태스크 재등록은 지시로 금지돼 있고(`★ 하지 마라`), 원인을 특정하지 못한 채 등록을 바꾸는
것은 이 프로젝트의 디버깅 규율 위반이다. **사용자 안건으로 올린다.**

## 8. 경계 / 하지 않은 것

- **`data/ops_state/STOP`·`PLANNED` 를 건드리지 않았다.** 수집기·`config.yaml` 무접촉,
  `schtasks /run` 0회, 라이브 API 호출 0회, 태스크 재등록·재시작 0회.
- **`PLANNED_20260812_104717_watchdog_silent.txt` 를 지우지도 고치지도 않았다.**
- **옛 `PLANNED_` 파일 10건을 소급 재발행하지 않았다.** 리포트가 읽을 때 다시 판정할 뿐이다.
- **`tossmon/**` 무수정.**
- **Modern Standby 자체는 손대지 않았다.** 계획 정지 중이 아니었다면 이 2시간은 수집
  공백이었을 것이다. 전원 설정을 바꾸는 것은 요청 범위 밖이고, `docs/34` §9.3 의
  `StartWhenAvailable` 건과 같은 성격의 **사용자 결정 사항**이다. 다만 **20:00 복귀가
  기계가 깨어 있음을 전제한다**는 사실은 여기 적어 둔다.
- **`0xC000013A` 의 원인을 판정하지 않았다**(§7.2).

## 9. 혼자 내린 판단 — 자기 신고

지시가 *"네가 정하고 근거를 적어라"* 였던 부분과, 지시에 없는데 내가 정한 부분을 나눠 적는다.

**지시가 위임한 것 (§3·§4 에 근거를 적었다):**

1. 허용목록 방향(분류 안 된 키는 안 덮는다) — 비대칭 손실이 근거.
2. 애매한 4건을 안 덮는 쪽으로 — 지시의 *"애매한 것은 덮지 않는 쪽"* 그대로.
3. 소급 방법으로 **아침 리포트의 재판정**을 고른 것 — §6 의 세 근거.

**지시에 없는데 내가 정한 것:**

4. **`restarted_<사유>` 를 `watch_<사유>` 에 묶었다.** 재시작 하나가 파일 두 개를 남기는데
   등급이 갈리면 자기모순이라는 판단. 셀프테스트로는 검증 못 한다고 §4.1 에 적었다.
5. **거절 머리말을 파일에 넣었다**(*"NOT MASKED BY THE PLANNED MARKER"*). 계획 창 중에
   `ALERT_` 를 본 독자가 "표식이 고장 났나"로 새는 것을 막기 위해서다.
6. **`watchdog.log` 의 "planned window ACTIVE" 문구를 고쳤다.** 옛 문구
   (*"alerts this cycle are written as PLANNED_"*)가 이제 거짓이 되기 때문이다.
7. **셀프테스트 하네스가 실제 태스크를 못 건드리게 막았다.** `Invoke-Watchdog` 에
   `WatchdogTaskName`/`SentinelTaskName` 을 존재하지 않는 이름으로 고정했다. 안 그러면
   센티널 역할 케이스가 **살아 있는 `tossmon-watchdog` 를 `schtasks /Run` 으로 걷어찬다** —
   지시가 금지한 바로 그 행위를 시험이 저지를 뻔했다.
8. **P10(언어 간 표류 방어)을 추가했다.** 목록을 두 곳에 두는 비용을 시험으로 갚는다.

**틀린 단언을 하나 스스로 물렀다.** 처음 P3 에 *"`restarted_process_dead` 도 `PLANNED_` 여야
한다"* 를 넣었는데 `-DryRunRestart` 경로가 그 경보 전에 반환한다는 것을 빨간 실행이
보여줬다. 제품이 아니라 시험이 틀렸으므로 단언을 빼고, **못 덮는 범위를 §4.1 에 적었다.**

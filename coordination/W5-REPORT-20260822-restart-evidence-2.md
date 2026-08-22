# W5 보고 2차 — 예산을 옳은 곳에 쓰고, 테스트를 붙인다 (2026-08-22)

명세: `coordination/specs/w5_restart_evidence_2.md` · 선행 PR #52
워크트리: `C:/Users/dongh/orca/workspaces/toss_trade/w5-ops` · `feat/ops`
배포 규율(§4): **커밋이 곧 배포다.** 아래 게이트 넷을 최종 트리에서 통과시킨 **뒤에** 커밋했다.
마감 **2026-08-24(월) 22:30** 전.

---

## 0. ★ 정렬만으로는 안 됐다 — 대상 집합 자체가 틀려 있었다

명세 §1 은 *"정렬이 없다"* 로 읽었고 그건 맞다. 그런데 고치려고 대상 집합을 실측했더니
**진짜 인터프리터가 애초에 대상에 없었다.**

```
> Get-Item .venv\Scripts\python.exe | Select Length,VersionInfo
Length      : 255200
InternalName: Python Launcher      <- py.exe 다. 인터프리터 복사본이 아니다
```

```
> .venv\Scripts\python.exe -c "import time; time.sleep(20)"   (실측 2026-08-22)
  pid 27592  ppid 30960  thr 2  hnd 54  exe C:\...\w5-ops\.venv\Scripts\python.exe
  pid 32324  ppid 27592  thr 4  hnd 76  exe C:\Users\dongh\AppData\Local\Programs\Python\Python313\python.exe
  - 두 프로세스의 CommandLine 은 완전히 같다. ExecutablePath 만 다르다.
```

`Get-SampleProcs` 는 `$ExeLike`(= `<repo>\.venv\*`)로 걸렀다. **자식(진짜 인터프리터)은
그 필터를 통과하지 못한다.** 즉 PR #52 의 표본은 운영 조건에서 **런처 스텁만** 대상으로
삼았고, 스텁에는 파이썬 상태가 없으므로 py-spy 는 언제나
`Failed to find python version from target process` 를 낸다. **정렬을 아무리 고쳐도
스택은 영원히 안 나온다.**

이것은 워치독이 사슬 넷을 `sup=1 col=1` 로 세는 것과 같은 원인이다 — 세는 대상이
스텁뿐이다. (명세 §0 이 준 *"스텁 셋은 py-spy 가 항상 실패한다"* 와 같은 사실의 뒷면이다.)

**그래서 수리가 둘이다:** (a) 대상 집합을 씨앗의 **부모-자식 폐포**로 넓히고,
(b) 그 집합을 정렬한다. `Get-TossProcs`(재기동 판단·kill 대상)는 **건드리지 않았다** —
sup/col 계수와 kill 집합이 바뀌면 그건 다른 변경이다.

폐포가 옳은 범위인 근거: **`taskkill /T` 가 지우는 트리와 같은 집합이다.** 표본이 덮는
범위와 재기동이 지우는 범위가 일치한다.

---

## 1. 정렬 규칙과 근거

`ops/watchdog.ps1` `Sort-SampleProcs` / `Get-SampleRank`:

| 순위 | 키 | 근거 |
|---|---|---|
| 1 | `$CollectorPattern` 일치 → 0, `$SupervisorPattern` 일치 → 1, 나머지 → 2 | 워치독이 **이미 갖고 있는** 패턴이다. 새 관측 0 개 |
| 2 | **스레드 수 내림차순** | 스텁과 진짜 인터프리터는 명령줄이 같아 패턴으로 못 가른다. §[2] 가 이미 읽는 값이다 |
| 3 | 핸들 수 내림차순 | 스레드가 같을 때의 다음 갈래 (사슬 실측 `56/82/54/240`) |
| 4 | PID 오름차순 | 결정적 순서 — 같은 입력이면 같은 파일 |

**스레드 수는 정렬 키이고 필터가 아니다.** 명세 §1-1 의 경고대로 *"1 스레드면 스텁"* 을
상수로 박지 않았다. 아무것도 버리지 않는다 — 사슬이 **어디서** 끊겼는지는 스텁이 목록에
있어야 보인다. 순서만 바뀐다.

정렬 근거는 표본 파일 §[2] 머리에 찍힌다 (아래 게이트 4 출력).

### 1-1. dry-run 증거

두 하네스 모두 `-DryRunRestart` + 샌드박스 `DataDir`/`StateDir` 이라 라이브 수집기도
라이브 `watchdog_state.json`(재기동 예산)도 건드리지 않는다.

**RUN A — 운영과 같은 `-ExeLike`(`<repo>\.venv\*`)**, 대상은 이 스크립트가 띄운 파이썬
미끼(런처 스텁 + 그 자식 인터프리터, 라이브 사슬과 같은 모양).
`procs=2` 가 곧 폐포가 작동한 증거다 — 씨앗은 스텁 하나뿐이었다.

```
  PID     PPID    PRIO  THR  HND   WS_MB    CPU_S     UP_S     COMMAND
  32164   15748   8     4    76    12.7     0.03      5        "...\w5-ops\.venv\Scripts\python.exe" -c "W5GATE4COL_a37212 = 1; ..."
  15748   18180   8     2    54    4.1      0.02      5        "...\w5-ops\.venv\Scripts\python.exe" -c "W5GATE4COL_a37212 = 1; ..."
...
  --- pid 32164 (timeout 8.0s) ---
  Process 32164: "...\w5-ops\.venv\Scripts\python.exe" -c "W5GATE4COL_a37212 = 1; import time; time.sleep(300)"
  Python v3.13.15 (C:\Users\dongh\AppData\Local\Programs\Python\Python313\python.exe)

  Thread 28900 (idle)
      <module> (<string>:1)
  --- pid 15748 (timeout 8.0s) ---
  Error: Failed to find python version from target process
```

진짜 인터프리터(32164, 4 스레드)가 **먼저**, 스텁(15748, 2 스레드)이 **뒤에 그대로 남아**
있다. 스텁이 목록에서 빠지지 않았다는 것도 같이 보인다.

**RUN B — 라이브 사슬 넷** (`-ExeLike *`: 이 세션 토큰으로는 라이브 프로세스의
`ExecutablePath` 를 못 읽는다):

```
  PID     PPID    PRIO  THR  HND   WS_MB    CPU_S     UP_S     COMMAND
  6292    9316    6     26   240   8.4      1701.39   72007    (command line not readable by this token)
  21684   19340   6     1    82    2        3.47      72007    (command line not readable by this token)
  19340   9636    6     1    56    1.1      0         72007    (command line not readable by this token)
  9316    21684   6     1    54    1.1      0         72007    (command line not readable by this token)
```

**진짜 수집기 6292(26 스레드)가 첫 번째다.** WMI 가 돌려주는 순서(대략 PID 순)라면
19340 이 먼저 왔을 것이다. 개당 상한이 8 초라 매달린 트리에서는 그 차이가 곧
*"스택을 뜨느냐 못 뜨느냐"* 다.

---

## 2. 새 테스트 다섯 — 무엇을 어떻게 깨뜨려 확인했나

`ops/watchdog_selftest.ps1` 에 `RS1`~`RS5` (단정 19 개). **130 → 149.**
전부 **진짜 워치독을 자식 프로세스로 돌리고 디스크에 남은 것을 읽는다.** 샘플러를 목으로
바꾼 케이스는 하나도 없다.

각 케이스가 실제로 회귀를 잡는지 **돌연변이로 확인했다.** 돌연변이는 `ops/watchdog.ps1`
**사본**에 넣고 `ops/watchdog_selftest.ps1 -Watchdog <사본>` 으로 돌렸다 — 라이브 파일은
한 번도 안 건드렸다(§4).

### RS1 — 표본 수집이 실패해도 `RESTART` 는 진행된다 ★ 제일 중요

깨뜨린 방법: `<RepoRoot>\.venv\Scripts\py-spy.exe` 자리에 **실행 파일이 아닌 텍스트
파일**을 심는다. `Get-PySpyExe` 는 `Test-Path` 로만 찾으므로 그걸 잡고,
`[Diagnostics.Process]::Start` 가 스택 루프 안에서 **던진다**(부분 설치·손상된 설치의
실제 모양이다).

돌연변이 **M1** = `catch` 블록 끝에 `throw` 추가(= 다음 사람이 `try` 를 옮긴 것과 같은 효과):

```
  FAIL  Test-RS1-ASampleFailureMustNotStopTheRestart: case ran to completion  Exception calling "Start" with "1" argument(s): "The specified executable is not a valid application for this OS platform."
RESULT: 145 passed, 1 failed
```

### RS2 — 예산 소진이 파일에 남는다

깨뜨린 방법: `-RestartSampleBudgetS 2.5 -RestartSampleIoGapS 0`. 예비분 2.0s 를 빼면
0.5s 라 루프가 요구하는 1.0s 에 못 미친다 — 기계 속도와 무관하게 첫 덤프 전에 포기한다.
py-spy 자리에는 인수와 무관하게 즉시 끝나는 실행 파일(`where.exe`)을 심어, 이 스위트가
py-spy 설치 여부에 의존하지 않게 했다.

돌연변이 **M2** = `BUDGET EXHAUSTED` 줄을 지우고 `break` 만 남긴다(조용히 자르기):

```
  FAIL  RS2: the file says the budget ran out, and after how many stacks  RESTART SAMPLE v1
RESULT: 148 passed, 1 failed
```

### RS3 — py-spy 가 없어도 나머지 셋은 그대로 나온다

깨뜨린 방법: py-spy 를 심지 않고, **그 호출 동안만 `$env:PATH` 를 줄여**
`Get-PySpyExe` 의 PATH 폴백까지 막는다. 안 그러면 이 케이스는 기계 상태에 따라
통과·실패가 갈린다(디스크·작업스케줄러 시임을 둔 것과 같은 이유).

돌연변이 **M3** = py-spy 없으면 표본 자체를 포기(`return ""`) = 하드 의존으로 만들기:

```
  FAIL  RS3: the sample was written anyway  2026-08-22 18:23:10 [watchdog] ALERT[CRIT] key=watch_log_stale file=ALERT_20260822_182310_watch_log_stale.txt bytes=1226
  FAIL  RS3: it says py-spy was not found, and where it looked  
  FAIL  RS3: section 1 (the verdict) survived  
  FAIL  RS3: section 2 (the tree) survived  
  FAIL  RS3: section 4 (IO/handles/threads) survived  
  FAIL  RS3: and the IO delta was actually computed  
RESULT: 143 passed, 6 failed
```

### RS4 / RS5 — §1 의 정렬

- **RS4(등급이 나머지를 이긴다)**: 감시자 미끼를 일부러 **먼저 띄우고(낮은 PID)
  스레드도 더 많게(+10)** 만든다. PID 순서도 스레드 순서도 감시자를 앞에 놓는다 —
  수집기가 앞에 오는 건 **등급 규칙뿐**이다.
- **RS5(같은 등급 안에서는 스레드가 많은 쪽이 먼저)**: **같은 태그**를 단 미끼 둘,
  얇은 쪽을 먼저 띄운다(낮은 PID). 등급으로는 못 가르고 스레드 수로만 갈린다 —
  라이브의 스텁/인터프리터와 정확히 같은 상황이다.

돌연변이 **M4** = `Sort-SampleProcs` 제거(WMI 순서로 되돌리기):

```
  FAIL  RS4: the collector is dumped BEFORE the older, thread-heavier supervisor  colPid=24468@4686 supPid=24172@4627
  FAIL  RS5: the thread-heavy one is dumped first, despite its higher PID  fatPid=15760@4924 thinPid=20804@4865
RESULT: 147 passed, 2 failed
```

---

## 3. 기준선 — **표본 파일에 비교 지침을 싣는다** (셋 중 하나)

고른 것: **표본 파일 §[3] 머리에 판독 지침 블록.** 나머지 둘(별도 문서, 주기적 기준선
수집)은 하지 않았다.

```
[3] PYTHON STACKS - py-spy dump, in the section [2] order
    (no --locals on purpose: ...)
    HOW TO READ IT - one stack alone says little; compare it with the healthy
    shape. Healthy (2026-08-22, collector up 19h, market closed): MainThread
    parked in asyncio _poll/select, two pool threads in _worker. The SAME place
    means the loop is alive and nothing is arriving. A lock / file IO / socket
    recv frame instead means a different fault. Full reference stack:
    coordination/daily/2026-08-22.md section 5-2.
    'Failed to find python version from target process' = a venv launcher stub,
    not a permission problem. It never has a stack; the interpreter above it does.
```

**왜 이것 하나인가:**

- 이 파일을 읽는 사람은 **사고 한가운데**에 있고, 다른 파일을 찾으러 가지 않는다.
  판단 규칙은 이미 열려 있는 파일 안에 있어야 한다.
- **주기적 기준선 수집은 기각.** 라이브 워치독의 주기·디스크에 새 작업을 얹는 것이고,
  정작 정지한 순간에는 돌지도 않는다. 이득 대비 위험이 크다.
- **줄 번호를 싣지 않았다.** `__main__.py:76` 같은 값은 수집기를 고칠 때마다 어긋난다.
  실은 **모양**(어디서 대기 중인가)이고 그건 안 어긋난다. 전문 스택은 좌표만 적었다.
- 마지막 두 줄은 §0 의 스텁 실패 문자열을 미리 해설한다 — 그게 없으면 다음 독자가
  *"권한 문제구나"* 로 잘못 읽는다. 실제로 1 차 때 내가 그 갈래에서 판정을 못 했다.

---

## 4. 게이트 출력 전문

### 게이트 1 — `powershell -File ops/watchdog_selftest.ps1` (exit 0) — **130 → 149**

새 케이스 부분:

```
  PASS  RS1: the sampler really did fail (otherwise this case proves nothing)
  PASS  RS1: no sample file was claimed
  PASS  RS1: THE RESTART STILL HAPPENED
  PASS  RS1: and the log says so in words
  PASS  RS2: a sample file was written anyway
  PASS  RS2: the file says the budget ran out, and after how many stacks
  PASS  RS2: the restart still happened
  PASS  RS3: the sample was written anyway
  PASS  RS3: it says py-spy was not found, and where it looked
  PASS  RS3: section 1 (the verdict) survived
  PASS  RS3: section 2 (the tree) survived
  PASS  RS3: section 4 (IO/handles/threads) survived
  PASS  RS3: and the IO delta was actually computed
  PASS  RS4: both decoys were dumped
  PASS  RS4: the collector is dumped BEFORE the older, thread-heavier supervisor
  PASS  RS5: both decoys were dumped
  PASS  RS5: the thread-heavy one is dumped first, despite its higher PID
  PASS  RS5: the file states the order it used
  PASS  RS5: and says threads are a sort key, not a filter
RESULT: 149 passed, 0 failed
```

전문:

```
watchdog self-test - sandbox root: C:\Users\dongh\AppData\Local\Temp\wdtest_bc323eff
watchdog under test: C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\ops\watchdog.ps1
  PASS  R1: exit code is 0
  PASS  R1: no ALERT files at all
  PASS  R1: summary line reports an OK cycle
  PASS  R1: summary exposes the new trust fields
  PASS  F1: no restart was attempted
  PASS  F1: exit code is not 2 (restart)
  PASS  F1: no ranking_snap_stalled ALERT
  PASS  F1: the frozen 'after' was rejected and the log's 'closed' used
  PASS  F1: watchdog.log records the skipped judgment
  PASS  F2: no restart was attempted
  PASS  F2: no ranking_snap_stalled ALERT
  PASS  F2: blindness is still reported as its own CRIT
  PASS  F3: no restart was attempted
  PASS  F3: no ranking_snap_stalled ALERT
  PASS  F3: the refusal is visible as a NOTE, not silent
  PASS  F4: no restart was attempted
  PASS  F4: no ranking_snap_stalled ALERT
  PASS  F4: the reason names the warmup guard
  PASS  F5: no restart was attempted
  PASS  F5: no ranking_snap_stalled ALERT
  PASS  F5: the reason names the fresher contradicting source
  PASS  F6: no restart was attempted
  PASS  F6: no ranking_snap_stalled ALERT
  PASS  F6: the grace clock restarted at the session change
  PASS  F7: no restart was attempted
  PASS  F7: the newer state observation was kept
  PASS  F7: its session is still not trusted
  PASS  T1: ranking_snap_stalled ALERT was written
  PASS  T1: a restart was performed
  PASS  T1: exit code is 2
  PASS  T1: the restart alert carries the EVIDENCE block
  PASS  T1: the restart alert says how to judge it
  PASS  T1: a progress-stall restart is judged on session freshness
  PASS  T1: and NOT on the process counts
  PASS  T2: a restart was performed for log_stale
  PASS  T2: exit code is 2
  PASS  T3: a restart was performed for process_dead
  PASS  T3: the session really was untrusted
  PASS  T3: the restart alert says how to judge it
  PASS  T3: a process-absence restart is judged on the sup/col counts
  PASS  T3: it does not blame the stale session reading
  PASS  T3: it says an absent process cannot be defended by a session reading
  PASS  S1: not filed as an ALERT
  PASS  S1: filed as a NOTE
  PASS  S1: the body no longer asserts a shape change
  PASS  S1: the body shows the evidence lines
  PASS  S2: still filed as an ALERT
  PASS  S2: level is CRIT
  PASS  S2: the strong wording is kept for the real case
  PASS  S3: filed as an ALERT (WARN, unknown cause)
  PASS  S3: level is WARN, not CRIT
  PASS  S3: the body admits the cause is unknown
  PASS  S4: not filed as an ALERT
  PASS  S4: filed as a NOTE
  PASS  TR1: a TRADEOFF_ file was written
  PASS  TR1: NO ALERT_ file at all - the morning rule survives
  PASS  TR1: reason names budget pressure
  PASS  TR1: says plainly it is not a fault
  PASS  TR2: a TRADEOFF_ file was written
  PASS  TR2: no ALERT_
  PASS  TR2: reason names the 429 cooldown
  PASS  TR2: does NOT claim budget pressure
  PASS  TR3: 1 WHAT was given up names the stream
  PASS  TR3: 2 FOR WHAT carries numbers
  PASS  TR3: 2 FOR WHAT carries the MARKET_DATA budget
  PASS  TR3: 2 FOR WHAT carries tier populations
  PASS  TR3: 3 HOW LONG is a duration in seconds
  PASS  TR3: 4 HOW MUCH is a yield percentage
  PASS  TR3: states the no-escalation rule
  PASS  TR4: still no ALERT_ after 6 flat cycles
  PASS  TR4: still a TRADEOFF_
  PASS  TR5: no ALERT_
  PASS  TR5: no TRADEOFF_ either - nothing was sacrificed
  PASS  TR5: filed as NOTE_
  PASS  TC1: still ALERT_
  PASS  TC1: not filed as TRADEOFF_
  PASS  TC1: body says no skip counter moved
  PASS  TC2: still ALERT_
  PASS  TC2: body names the disabled-config hypothesis
  PASS  TC3: phase 1 is a TRADEOFF_
  PASS  TC3: phase 2 raises a real ALERT_ despite the recent TRADEOFF_
  PASS  LP1: a 25h-old log line raises no ALERT
  PASS  LP2: a 10min-old log line still raises an ALERT
  PASS  LP3: body names when the newest match happened
  PASS  LP3: body states the time bound it used
  PASS  LP4: watchdog.log records the aged-out match
  PASS  LP4: and says how old the newest one was
  PASS  LP5: a fresh CRIT still alerts
  PASS  LP5: a day-old CRIT does not re-fire forever either
  PASS  LP6: 25 tape gaps spread over 100-124min still clear Min=20
  PASS  LP7: an undatable match still alerts
  PASS  LP7: and the body says it could not be dated
  PASS  TS1: a dead sibling task raises an ALERT
  PASS  TS1: body decodes the exit code
  PASS  TS1: body names when it ran
  PASS  TS1: body says what was expected instead
  PASS  TS2: watchdog result=1 raises nothing
  PASS  TS3: watchdog result=2 raises nothing
  PASS  TS4: all-zero results raise nothing
  PASS  TS5: a 5-day-old one-shot failure raises nothing
  PASS  TS5: but watchdog.log records it
  PASS  TS6: running / never-ran codes raise nothing
  PASS  TS7: an unregistered task alerts with its own wording
  PASS  TS8: 0x41306 is decoded as a scheduler termination
  PASS  P1: a dead watchdog is an ALERT even inside a planned window
  PASS  P1: and it is NOT filed as PLANNED
  PASS  P2: a missing watchdog heartbeat is an ALERT inside a planned window
  PASS  P2: and it is NOT filed as PLANNED
  PASS  P3: an absent collector stays PLANNED during the window
  PASS  P3: no ALERT file was written for the planned stop
  PASS  P3: the PLANNED header still carries 'window until' for gap_audit
  PASS  P3: the PLANNED header still carries the reason line
  PASS  P4: stop_observed is PLANNED even with no marker file
  PASS  P4: and it raises no ALERT
  PASS  P5: a failed sibling task is an ALERT inside a planned window
  PASS  P5: and it is NOT filed as PLANNED
  PASS  P6: a disk warning is an ALERT inside a planned window
  PASS  P6: and it is NOT filed as PLANNED
  PASS  P7: a silent sentinel is an ALERT inside a planned window
  PASS  P7: and it is NOT filed as PLANNED
  PASS  P8: the alert says it refused the planned marker
  PASS  P8: it quotes the marker reason so the reader can check it
  PASS  P8: it names the key that was out of scope
  PASS  P8: watchdog.log records the refusal
  PASS  P9: no marker, dead watchdog, still an ALERT
  PASS  P10: watchdog.ps1 declares a fenced scope list
  PASS  P10: daily_health.py declares a fenced scope list
  PASS  P10: the two lists are identical
  PASS  P11: the planned collection stop is still PLANNED
  PASS  P11: an exhausted restart budget in the SAME cycle is an ALERT
  PASS  RS1: the sampler really did fail (otherwise this case proves nothing)
  PASS  RS1: no sample file was claimed
  PASS  RS1: THE RESTART STILL HAPPENED
  PASS  RS1: and the log says so in words
  PASS  RS2: a sample file was written anyway
  PASS  RS2: the file says the budget ran out, and after how many stacks
  PASS  RS2: the restart still happened
  PASS  RS3: the sample was written anyway
  PASS  RS3: it says py-spy was not found, and where it looked
  PASS  RS3: section 1 (the verdict) survived
  PASS  RS3: section 2 (the tree) survived
  PASS  RS3: section 4 (IO/handles/threads) survived
  PASS  RS3: and the IO delta was actually computed
  PASS  RS4: both decoys were dumped
  PASS  RS4: the collector is dumped BEFORE the older, thread-heavier supervisor
  PASS  RS5: both decoys were dumped
  PASS  RS5: the thread-heavy one is dumped first, despite its higher PID
  PASS  RS5: the file states the order it used
  PASS  RS5: and says threads are a sort key, not a filter

RESULT: 149 passed, 0 failed
```

### 게이트 2 — `pytest tests/ -k "ops or watchdog" -q`

```
........................................................................ [ 20%]
........................................................................ [ 41%]
........................................................................ [ 62%]
........................................................................ [ 82%]
...........................................................              [100%]
============================== warnings summary ===============================
tests/test_collector_loops.py::test_every_candle_call_passes_the_contracted_adjustment
  C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\tossmon\collector\detector.py:787: RuntimeWarning: features: 매매일 그룹화가 UTC 날짜 폴백으로 내려갔다 (캘린더 미제공 또는 구간 미포함). 겨울(EST)에는 매매일이 UTC 자정을 넘어 hist_days_available·former-runner 프록시가 이중 계산된다 — calendar= 를 넘겨라 (감사 M-3, docs/07 §3.1a).
    feats = extract_precursor_features(

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
347 passed, 1877 deselected, 1 warning in 10.09s
```

### 게이트 3 — `pytest tests/ -q`

```
........................................................................ [  3%]
........................................................................ [  6%]
........................................................................ [  9%]
........................................................................ [ 12%]
........................................................................ [ 16%]
........................................................................ [ 19%]
...............s........................................................ [ 22%]
........................................................................ [ 25%]
........................................................................ [ 29%]
........................................................................ [ 32%]
........................................................................ [ 35%]
........................................................................ [ 38%]
........................................................................ [ 42%]
........................................................................ [ 45%]
........................................................................ [ 48%]
........................................................................ [ 51%]
........................................................................ [ 55%]
........................................................................ [ 58%]
........................................................................ [ 61%]
........................................................................ [ 64%]
........................................................................ [ 68%]
........................................................................ [ 71%]
........................................................................ [ 74%]
........................................................................ [ 77%]
........................................................................ [ 81%]
........................................................................ [ 84%]
........................................................................ [ 87%]
........................................................................ [ 90%]
........................................................................ [ 94%]
........................................................................ [ 97%]
............................................................             [100%]
============================== warnings summary ===============================
tests/test_a2_collector_alignment.py::test_detector_cutoff_lands_exactly_on_the_t0_bar
tests/test_a2_collector_alignment.py::test_detector_ignores_the_snapshot_stamped_exactly_at_t0
tests/test_a2_collector_alignment.py::test_detector_ignores_the_snapshot_stamped_exactly_at_t0
tests/test_a2_collector_alignment.py::test_the_poison_is_actually_poisonous
tests/test_a2_collector_alignment.py::test_the_poison_is_actually_poisonous
tests/test_collector_detector.py::test_toss_concentration_survives_on_volume_rankings
tests/test_collector_loops.py::test_every_candle_call_passes_the_contracted_adjustment
tests/test_collector_replay.py::test_replay_state_survives_a_mid_session_restart
  C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\tossmon\collector\detector.py:787: RuntimeWarning: features: 매매일 그룹화가 UTC 날짜 폴백으로 내려갔다 (캘린더 미제공 또는 구간 미포함). 겨울(EST)에는 매매일이 UTC 자정을 넘어 hist_days_available·former-runner 프록시가 이중 계산된다 — calendar= 를 넘겨라 (감사 M-3, docs/07 §3.1a).
    feats = extract_precursor_features(

tests/test_a2_collector_alignment.py::test_widening_the_ranking_cut_would_cost_a_measurable_amount
tests/test_a2_collector_alignment.py::test_widening_the_ranking_cut_would_cost_a_measurable_amount
  C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\tests\test_a2_collector_alignment.py:176: RuntimeWarning: features: 매매일 그룹화가 UTC 날짜 폴백으로 내려갔다 (캘린더 미제공 또는 구간 미포함). 겨울(EST)에는 매매일이 UTC 자정을 넘어 hist_days_available·former-runner 프록시가 이중 계산된다 — calendar= 를 넘겨라 (감사 M-3, docs/07 §3.1a).
    return extract_precursor_features(

tests/test_analysis_features.py::test_key_set_is_stable_regardless_of_availability
  C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\tests\test_analysis_features.py:150: RuntimeWarning: features: 매매일 그룹화가 UTC 날짜 폴백으로 내려갔다 (캘린더 미제공 또는 구간 미포함). 겨울(EST)에는 매매일이 UTC 자정을 넘어 hist_days_available·former-runner 프록시가 이중 계산된다 — calendar= 를 넘겨라 (감사 M-3, docs/07 §3.1a).
    poor = F.extract_precursor_features(df, pd.DataFrame(), t0)

tests/test_analysis_features.py::test_windows_min_changes_key_set
  C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\tests\test_analysis_features.py:161: RuntimeWarning: features: 매매일 그룹화가 UTC 날짜 폴백으로 내려갔다 (캘린더 미제공 또는 구간 미포함). 겨울(EST)에는 매매일이 UTC 자정을 넘어 hist_days_available·former-runner 프록시가 이중 계산된다 — calendar= 를 넘겨라 (감사 M-3, docs/07 §3.1a).
    f = F.extract_precursor_features(df, truth["rankings"],

tests/test_analysis_features.py::test_no_toss_ranking_when_symbol_absent
  C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\tests\test_analysis_features.py:230: RuntimeWarning: features: 매매일 그룹화가 UTC 날짜 폴백으로 내려갔다 (캘린더 미제공 또는 구간 미포함). 겨울(EST)에는 매매일이 UTC 자정을 넘어 hist_days_available·former-runner 프록시가 이중 계산된다 — calendar= 를 넘겨라 (감사 M-3, docs/07 §3.1a).
    f = F.extract_precursor_features(df, truth["rankings"], t0, symbol=truth["symbol"])

tests/test_analysis_features.py::test_prior_events_override
  C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\tests\test_analysis_features.py:274: RuntimeWarning: features: 매매일 그룹화가 UTC 날짜 폴백으로 내려갔다 (캘린더 미제공 또는 구간 미포함). 겨울(EST)에는 매매일이 UTC 자정을 넘어 hist_days_available·former-runner 프록시가 이중 계산된다 — calendar= 를 넘겨라 (감사 M-3, docs/07 §3.1a).
    f = F.extract_precursor_features(df, truth["rankings"], t0, prior_events=prior,

tests/test_analysis_fixtures.py::test_toss_concentration_from_real_ranking_shapes
  C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\tests\test_analysis_fixtures.py:210: RuntimeWarning: features: 매매일 그룹화가 UTC 날짜 폴백으로 내려갔다 (캘린더 미제공 또는 구간 미포함). 겨울(EST)에는 매매일이 UTC 자정을 넘어 hist_days_available·former-runner 프록시가 이중 계산된다 — calendar= 를 넘겨라 (감사 M-3, docs/07 §3.1a).
    feats = F.extract_precursor_features(df, rk, t0, symbol=symbol)

tests/test_collector_detector.py::test_toss_concentration_survives_on_volume_rankings
  C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\tests\test_collector_detector.py:766: RuntimeWarning: features: 매매일 그룹화가 UTC 날짜 폴백으로 내려갔다 (캘린더 미제공 또는 구간 미포함). 겨울(EST)에는 매매일이 UTC 자정을 넘어 hist_days_available·former-runner 프록시가 이중 계산된다 — calendar= 를 넘겨라 (감사 M-3, docs/07 §3.1a).
    dead = extract_precursor_features(

tests/test_cutoff_amendment_a2.py::test_extract_reports_cutoff_at_t0_with_zero_lag
  C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\tests\test_cutoff_amendment_a2.py:102: RuntimeWarning: features: 매매일 그룹화가 UTC 날짜 폴백으로 내려갔다 (캘린더 미제공 또는 구간 미포함). 겨울(EST)에는 매매일이 UTC 자정을 넘어 hist_days_available·former-runner 프록시가 이중 계산된다 — calendar= 를 넘겨라 (감사 M-3, docs/07 §3.1a).
    f = F.extract_precursor_features(_bars(), _rankings(), T0_MS, symbol=SYM)

tests/test_cutoff_amendment_a2.py::test_ranking_stays_strict_under_the_observable_candle_default
  C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\tests\test_cutoff_amendment_a2.py:113: RuntimeWarning: features: 매매일 그룹화가 UTC 날짜 폴백으로 내려갔다 (캘린더 미제공 또는 구간 미포함). 겨울(EST)에는 매매일이 UTC 자정을 넘어 hist_days_available·former-runner 프록시가 이중 계산된다 — calendar= 를 넘겨라 (감사 M-3, docs/07 §3.1a).
    f = F.extract_precursor_features(_bars(), _rankings(), T0_MS, symbol=SYM)

tests/test_cutoff_amendment_a2.py::test_ranking_stays_strict_even_when_the_candle_flag_says_include_t0
  C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\tests\test_cutoff_amendment_a2.py:122: RuntimeWarning: features: 매매일 그룹화가 UTC 날짜 폴백으로 내려갔다 (캘린더 미제공 또는 구간 미포함). 겨울(EST)에는 매매일이 UTC 자정을 넘어 hist_days_available·former-runner 프록시가 이중 계산된다 — calendar= 를 넘겨라 (감사 M-3, docs/07 §3.1a).
    f = F.extract_precursor_features(_bars(), _rankings(), T0_MS, symbol=SYM,

tests/test_cutoff_amendment_a2.py::test_ranking_cutoff_is_identical_across_both_candle_modes
  C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\tests\test_cutoff_amendment_a2.py:131: RuntimeWarning: features: 매매일 그룹화가 UTC 날짜 폴백으로 내려갔다 (캘린더 미제공 또는 구간 미포함). 겨울(EST)에는 매매일이 UTC 자정을 넘어 hist_days_available·former-runner 프록시가 이중 계산된다 — calendar= 를 넘겨라 (감사 M-3, docs/07 §3.1a).
    a = F.extract_precursor_features(_bars(), _rankings(), T0_MS, symbol=SYM,

tests/test_cutoff_amendment_a2.py::test_ranking_cutoff_is_identical_across_both_candle_modes
  C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\tests\test_cutoff_amendment_a2.py:133: RuntimeWarning: features: 매매일 그룹화가 UTC 날짜 폴백으로 내려갔다 (캘린더 미제공 또는 구간 미포함). 겨울(EST)에는 매매일이 UTC 자정을 넘어 hist_days_available·former-runner 프록시가 이중 계산된다 — calendar= 를 넘겨라 (감사 M-3, docs/07 §3.1a).
    b = F.extract_precursor_features(_bars(), _rankings(), T0_MS, symbol=SYM,

tests/test_cutoff_amendment_a2.py::test_poisoning_the_t0_snapshot_moves_nothing
  C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\tests\test_cutoff_amendment_a2.py:152: RuntimeWarning: features: 매매일 그룹화가 UTC 날짜 폴백으로 내려갔다 (캘린더 미제공 또는 구간 미포함). 겨울(EST)에는 매매일이 UTC 자정을 넘어 hist_days_available·former-runner 프록시가 이중 계산된다 — calendar= 를 넘겨라 (감사 M-3, docs/07 §3.1a).
    a = F.extract_precursor_features(_bars(), clean, T0_MS, symbol=SYM, include_t0=True)

tests/test_cutoff_amendment_a2.py::test_poisoning_the_t0_snapshot_moves_nothing
  C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\tests\test_cutoff_amendment_a2.py:153: RuntimeWarning: features: 매매일 그룹화가 UTC 날짜 폴백으로 내려갔다 (캘린더 미제공 또는 구간 미포함). 겨울(EST)에는 매매일이 UTC 자정을 넘어 hist_days_available·former-runner 프록시가 이중 계산된다 — calendar= 를 넘겨라 (감사 M-3, docs/07 §3.1a).
    b = F.extract_precursor_features(_bars(), dirty, T0_MS, symbol=SYM, include_t0=True)

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
2219 passed, 1 skipped, 4 deselected, 23 warnings in 208.68s (0:03:28)
```

### 게이트 4 — dry-run 하네스 (RUN A: 운영 `-ExeLike` / RUN B: 라이브 사슬)

```
RUN A decoy launcher pid: 7468
RUN A python.exe seen (ExecutablePath tells stub from interpreter):
  pid 7468   ppid 28360  thr 2   hnd 54   exe C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\.venv\Scripts\python.exe
  pid 7804   ppid 7468   thr 4   hnd 76   exe C:\Users\dongh\AppData\Local\Programs\Python\Python313\python.exe
RUN A watchdog exit=2
--- RUN A : data/watchdog.log ---
2026-08-22 18:37:25 [watchdog] restart-sample: wrote NOTE_20260822_183728_restart_sample.txt bytes=4605 mode=dryrun reason=supervisor_dead procs=2 stacks=2 took=2.13s
2026-08-22 18:37:25 [watchdog] RESTART-DRYRUN reason=supervisor_dead (would run: C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\ops\launch_collector.cmd)
--- RUN A : sample sections [2] and [3] ---
[2] PROCESS TREE - every python.exe under C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\.venv* and their python.exe
    descendants, plus resolved ancestors
    ORDER (the same in [3] and [4]): collector pattern first, then supervisor
    pattern, then the rest; within each, most threads first, then most handles.
    Threads are a SORT KEY, NOT a filter - nothing is dropped. This exists so
    the [3] budget is spent on the interpreter that holds a stack rather than
    on the venv launcher stubs, which WMI would otherwise return first.
  PID     PPID    PRIO  THR  HND   WS_MB    CPU_S     UP_S     COMMAND
  7804    7468    8     4    76    12.9     0.02      5        "C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\.venv\Scripts\python.exe" -c "W5GATE4COL_df3adf = 1; import time; time.sleep(300)" 
  7468    28360   8     2    54    4.1      0.02      5        "C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\.venv\Scripts\python.exe" -c "W5GATE4COL_df3adf = 1; import time; time.sleep(300)" 
  ancestors:
    pid 28360   ppid 32380   powershell.exe   up=6s  "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File C:\Users\dongh\AppD...
    pid 32380   ppid 30832   pwsh.exe         up=345s  "C:\Program Files\WindowsApps\Microsoft.PowerShell_7.6.5.0_x64__8wekyb3d8bbwe\pwsh.exe" -NoProfile -NonInteractive -Exec...
    pid 30832   ppid 29036   claude.exe       up=6828s  "C:\Users\dongh\.local\bin\claude.exe" --dangerously-skip-permissions
    pid 29036   ppid 4760    pwsh.exe         up=6829s  "C:\Program Files\WindowsApps\Microsoft.PowerShell_7.6.5.0_x64__8wekyb3d8bbwe\pwsh.exe" -NoLogo -NoExit -EncodedCommand ...
    pid 4760    ppid 18932   orca-terminal-daemon.exe up=74135s  C:\Users\dongh\AppData\Local\Orca\daemon-host\1.4.187\orca-terminal-daemon.exe C:\Users\dongh\AppData\Local\Orca\daemon-...
    pid 18932   ppid 6284    Orca.exe         up=74140s  "C:\Users\dongh\AppData\Local\Programs\orca\Orca.exe" 

[3] PYTHON STACKS - py-spy dump, in the section [2] order
    (no --locals on purpose: a stack says where it is stuck, and locals can
     carry the API token into a plain-text file under data/)
    HOW TO READ IT - one stack alone says little; compare it with the healthy
    shape. Healthy (2026-08-22, collector up 19h, market closed): MainThread
    parked in asyncio _poll/select, two pool threads in _worker. The SAME place
    means the loop is alive and nothing is arriving. A lock / file IO / socket
    recv frame instead means a different fault. Full reference stack:
    coordination/daily/2026-08-22.md section 5-2.
    'Failed to find python version from target process' = a venv launcher stub,
    not a permission problem. It never has a stack; the interpreter above it does.
  py-spy: C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\.venv\Scripts\py-spy.exe
  --- pid 7804 (timeout 8.0s) ---
  Process 7804: "C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\.venv\Scripts\python.exe" -c "W5GATE4COL_df3adf = 1; import time; time.sleep(300)" 
  Python v3.13.15 (C:\Users\dongh\AppData\Local\Programs\Python\Python313\python.exe)
  
  Thread 21568 (idle)
      <module> (<string>:1)
  --- pid 7468 (timeout 8.0s) ---
  Error: Failed to find python version from target process

  (full file: C:\Users\dongh\AppData\Local\Temp\w5g4a_47d77b65\data\NOTE_20260822_183728_restart_sample.txt)

RUN B watchdog exit=2
--- RUN B : data/watchdog.log ---
2026-08-22 18:37:29 [watchdog] restart-sample: wrote NOTE_20260822_183731_restart_sample.txt bytes=4593 mode=dryrun reason=log_stale procs=4 stacks=4 took=2.12s
2026-08-22 18:37:29 [watchdog] RESTART-DRYRUN reason=log_stale (would run: C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\ops\launch_collector.cmd)
--- RUN B : sample sections [2] and [3] ---
[2] PROCESS TREE - every python.exe under * and their python.exe
    descendants, plus resolved ancestors
    ORDER (the same in [3] and [4]): collector pattern first, then supervisor
    pattern, then the rest; within each, most threads first, then most handles.
    Threads are a SORT KEY, NOT a filter - nothing is dropped. This exists so
    the [3] budget is spent on the interpreter that holds a stack rather than
    on the venv launcher stubs, which WMI would otherwise return first.
  PID     PPID    PRIO  THR  HND   WS_MB    CPU_S     UP_S     COMMAND
  6292    9316    6     26   240   8.5      1701.47   72667    (command line not readable by this token)
  21684   19340   6     1    82    1.9      3.53      72667    (command line not readable by this token)
  19340   9636    6     1    56    1.1      0         72667    (command line not readable by this token)
  9316    21684   6     1    54    1.1      0         72667    (command line not readable by this token)
  ancestors:
    pid 9636    ppid 14760   cmd.exe          up=72667s  (command line not readable by this token)
    pid 14760 - GONE (parent already exited; the chain above it is unreadable)

[3] PYTHON STACKS - py-spy dump, in the section [2] order
    (no --locals on purpose: a stack says where it is stuck, and locals can
     carry the API token into a plain-text file under data/)
    HOW TO READ IT - one stack alone says little; compare it with the healthy
    shape. Healthy (2026-08-22, collector up 19h, market closed): MainThread
    parked in asyncio _poll/select, two pool threads in _worker. The SAME place
    means the loop is alive and nothing is arriving. A lock / file IO / socket
    recv frame instead means a different fault. Full reference stack:
    coordination/daily/2026-08-22.md section 5-2.
    'Failed to find python version from target process' = a venv launcher stub,
    not a permission problem. It never has a stack; the interpreter above it does.
  py-spy: C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\.venv\Scripts\py-spy.exe
  --- pid 6292 (timeout 8.0s) ---
  Error: Failed to open process - check if it is running.
  
  Caused by:
      0: ?≪꽭?ㅺ? 嫄곕??섏뿀?듬땲?? (os error 5)
      1: ?≪꽭?ㅺ? 嫄곕??섏뿀?듬땲?? (os error 5)
  --- pid 21684 (timeout 8.0s) ---
  Error: Failed to open process - check if it is running.
  
  Caused by:
      0: ?≪꽭?ㅺ? 嫄곕??섏뿀?듬땲?? (os error 5)
      1: ?≪꽭?ㅺ? 嫄곕??섏뿀?듬땲?? (os error 5)
  --- pid 19340 (timeout 8.0s) ---
  Error: Failed to open process - check if it is running.
  
  Caused by:
      0: ?≪꽭?ㅺ? 嫄곕??섏뿀?듬땲?? (os error 5)
      1: ?≪꽭?ㅺ? 嫄곕??섏뿀?듬땲?? (os error 5)
  --- pid 9316 (timeout 8.0s) ---
  Error: Failed to open process - check if it is running.
  
  Caused by:
      0: ?≪꽭?ㅺ? 嫄곕??섏뿀?듬땲?? (os error 5)
      1: ?≪꽭?ㅺ? 嫄곕??섏뿀?듬땲?? (os error 5)

  (full file: C:\Users\dongh\AppData\Local\Temp\w5g4b_8953b7b1\data\NOTE_20260822_183731_restart_sample.txt)
```

> RUN B 의 py-spy 오류 안 한국어 문구가 깨져 보이는 것은 **이 콘솔 파이프의 표시**
> 문제다(cp949 → UTF-8). 파일 자체에는 `os error 5` 를 포함한 원문이 그대로 들어 있고,
> 판독에 필요한 부분은 손상되지 않았다. 고치지 않았다 — §6.

### 라이브 워치독이 편집된 파일을 실제로 돌린 기록

커밋 전에 이미 디스크에 있었으므로(§4: 커밋이 곧 배포), 라이브 5 분 주기가 그것을 돌렸다:

```
2026-08-22 18:20:54 [watchdog] OK sup=1 col=1 session=closed age_min=0.4 ... col_up=71672s
2026-08-22 18:25:54 [watchdog] OK sup=1 col=1 session=closed age_min=0.4 ... col_up=71972s
2026-08-22 18:30:54 [watchdog] OK sup=1 col=1 session=closed age_min=0.3 ... col_up=72272s
```

---

## 5. 병합 증거 문자열

| 대상 | 문자열 |
|---|---|
| **`ops/watchdog.ps1`** (1 순위) | `function Sort-SampleProcs` |
| `ops/watchdog.ps1` | `Sort-SampleProcs (Get-SampleProcs)` — 정렬이 실제로 걸려 있는지 |
| **`ops/watchdog_selftest.ps1`** | `Test-RS1-ASampleFailureMustNotStopTheRestart` |
| `ops/watchdog_selftest.ps1` | `RS1`~`RS5` 5 개가 `$cases` 목록에 있는지 |
| `docs/34_alert_grades.md` | `### 10-1.` |
| 런타임 표본 파일 | `ORDER (the same in [3] and [4])` |
| (1 차에서 계속) `ops/watchdog.ps1` | `RESTART SAMPLE v1` |

---

## 6. 안 한 것 / 못 한 것

- **`Get-TossProcs` 를 안 건드렸다.** 폐포는 표본 대상에만 적용했다. 재기동 판단이 쓰는
  `sup`/`col` 계수와 `taskkill` 대상은 1 차와 동일하다 — 그걸 바꾸는 건 자가 복구 경로의
  의미를 바꾸는 별개 변경이고 이 명세의 범위가 아니다.
  결과적으로 로그의 `sup=1 col=1` 은 사슬 넷 중 스텁 둘만 센 값 그대로다. **판정하지 않는다.**
- **예약작업 등록·변경 안 했다** (명세 §5, `/RL HIGHEST` 는 필요 없어졌다).
  **수집기 재기동 · `STOP`/`PLANNED` 생성 안 했다. `tossmon/**` 안 건드렸다.**
- **날짜 정정은 내 워크트리에서 다시 쓰지 않았다.** 코디네이터가 `main` 에 넣은
  여덟 곳(내 1 차 보고 포함)을 그대로 둔다. 이 보고는 처음부터 **08-24(월) 22:30** 으로 썼다.
- **py-spy 오류 메시지의 인코딩은 안 고쳤다.** `Invoke-Bounded` 가 자식의 stdout 을
  기본 인코딩으로 읽어 cp949 원문이 어긋날 수 있다. 표본의 판독에 필요한 부분
  (`os error 5` / `Failed to find python version`)은 ASCII 라 온전하다. 범위 밖으로 뒀다 —
  고치려면 `StandardOutputEncoding` 을 지정해야 하고 그건 라이브 경로의 별개 변경이다.
- **kill 경로는 이번엔 다시 안 돌렸다.** 1 차 PR #52 에서 미끼 대상으로 실측했고
  (`mode=kill`, `alive before: 2` → `alive after: 0`), 이번 변경은 대상 선정·정렬·테스트뿐이라
  kill 경로 자체의 코드는 그대로다. **이번 회차에서는 실행하지 않았음.**
- **라이브 수집기의 스택은 이 세션에서 못 떴다.** 중간 무결성 토큰이라 `os error 5` 다
  (RUN B 그대로). 붙는다는 확인은 코디네이터가 S4U/Limited 로 이미 했다(명세 §0).

---

## 7. 판정 금지

**표본은 여전히 0 개다.** 조용한 정지의 원인에 대해 이 보고는 아무것도 주장하지 않는다.

- RUN B 에서 라이브 수집기 6292 의 CPU 가 1701s / 스레드 26 / 핸들 240 이라는 것은
  **휴장 중(session=closed, uptime 20 시간)의 측정값**이다. 정상·비정상 어느 쪽의 근거도 아니다.
- §3 의 "건강한 모양"은 코디네이터가 **휴장 중**에 뜬 한 개의 기준선이다. 정규장 중의
  건강한 모양이 같은지는 **관측되지 않았다.** 판독 지침에 측정 조건을 같이 적어 둔 이유다.
- 이번 수리는 **관측 도구의 결함 둘**을 고친 것이다. 원인 수리는 표본이 실제로 한 번
  잡힌 뒤 새 명세로 낸다.

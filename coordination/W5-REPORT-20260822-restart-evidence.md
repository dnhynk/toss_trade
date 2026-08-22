# W5 보고 — `RESTART` 직전 표본 (2026-08-22)

명세: `coordination/specs/w5_restart_evidence.md`
워크트리: `C:/Users/dongh/orca/workspaces/toss_trade/w5-ops` · 브랜치 `feat/ops` (기준 `e7c9437`)

---

## 1. 한 것

### 1-1. `ops/watchdog.ps1` — 표본 수집기

| 행 | 무엇 | 왜 |
|---|---|---|
| 132~144 | 파라미터 3개 추가 (`-RestartSampleBudgetS` 20.0 / `-RestartSampleIoGapS` 2.0 / `-RestartSampleMaxProcs` 8) | 시간 상한과 IO 2회 측정 간격, 스택 덤프 개수 상한을 코드에 박지 않고 노출한다 |
| 864~1192 | 표본 블록 (`Write-RestartSample` 외 헬퍼 9개) | 아래 §3 의 파일을 만든다 |
| 1202 | `restart_budget_exhausted` 경로에서 표본 | §5 |
| 1218 | `$DryRunRestart` 분기에서 표본 | §5 |
| **1228** | **`taskkill` 루프 바로 앞** (명세 §2-1 의 삽입점) | 살아 있는 프로세스를 볼 수 있는 마지막 순간 |
| 1206, 1251 | `restart_budget_exhausted` · `restarted_<reason>` 경보 본문에 표본 파일명 한 줄 | 증거 파일을 이름으로 지목하지 않는 경보는 독자에게 grep 을 시킨다 |

표본 파일은 `data/NOTE_<yyyyMMdd_HHmmss>_restart_sample.txt` 다. 명세 §2-2 의 넷이 한
파일에 들어간다 — `[1]` 판단값(`age_min`·`col_up`·`rank_age`·`t2book` + `session`·
`open_for`·`src`·`sup/col`·`problems`), `[2]` 프로세스 트리(PID·부모·우선순위·스레드·
핸들·working set·CPU·uptime·명령줄 + 비-python 조상까지 6단계), `[3]` py-spy 스택,
`[4]` IO/핸들/스레드 **2회 측정과 델타**.

IO 를 두 번 뜨는 이유: 한 번은 프로세스 시작 이후 누계라서 *"10분 전에 IO 를 멈췄다"* 와
*"원래 IO 가 적다"* 를 못 가른다. 두 측정 사이 간격이 파일에 초 단위로 찍힌다.

`Raise-Alert` 를 거치지 않고 직접 쓴다. `Raise-Alert` 는 키 단위로 몇 시간 dedup 하므로,
아침에 재기동이 한 번 있었다는 이유로 다음 재기동의 표본이 억제될 수 있다 — 재현 기회가
두 번뿐이었던 이 문제에서 그건 곧 손실이다. 등급은 `NOTE_` 다: 같은 사건의 고장은 이미
`ALERT_*_watch_<reason>.txt` 로 찍히고, 이 파일은 거기 붙는 증거이지 두 번째 고장이 아니다.

### 1-2. `docs/34_alert_grades.md` §10 추가

새 `NOTE_` 키의 등급 근거, `Raise-Alert` 우회가 의도라는 것, 자가 복구 우선 규칙, 세 경로,
회전 대상이 아니라는 것. 다음 구현자가 이 직접 쓰기를 `Raise-Alert` 호출로 "정리"하면
dedup 이 조용히 되살아난다 — 그것을 막으려고 적었다.

### 1-3. 회전에서 지워지지 않는지 확인

- `ops/rotate_logs.py`: `log_dir.glob("*.log")` 회전, `glob("*.log.*.gz")` 삭제 — `*.txt` 는 대상 밖
- `ops/watchdog.ps1` `Invoke-Reclaim`: `$DataDir` 의 `*.gz` 중 7일 경과분만 삭제 — `*.txt` 는 대상 밖
- `ops/disk_guard.py`: 삭제 코드 없음 (`unlink`/`remove`/`rmtree` 0건)

---

## 2. 게이트 출력 전문

### 게이트 1 — `powershell -File ops/watchdog_selftest.ps1` (exit 0)

```
watchdog self-test - sandbox root: C:\Users\dongh\AppData\Local\Temp\wdtest_d1888d34
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

RESULT: 130 passed, 0 failed
```

### 게이트 2 — `./.venv/Scripts/python.exe -m pytest tests/ -k "ops or watchdog" -q`

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
347 passed, 1877 deselected, 1 warning in 9.46s
```

### 게이트 3 — `./.venv/Scripts/python.exe -m pytest tests/ -q`

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
2219 passed, 1 skipped, 4 deselected, 23 warnings in 208.74s (0:03:28)
```

### 게이트 4 — 표본이 실제로 발동하는가

명세는 `-DryRunRestart` 를 지정했다. **두 경로를 다 돌렸다** — dry-run 은 명세대로,
kill 경로는 `-DryRunRestart` 가 절대 닿지 못하는 곳이고 삽입점이 바로 거기라서다.

두 하네스 모두 **라이브를 건드리지 않는다**: `DataDir`/`StateDir` 이 임시 샌드박스라
라이브 `watchdog_state.json`(재기동 예산!)을 안 만지고, 4b 의 프로세스 패턴은 자기가 띄운
파이썬 미끼에만 걸리며(`ops\.supervisor`/`tossmon\.collector` 는 쓰지 않는다),
`LauncherCmd` 는 샌드박스 안의 아무것도 안 하는 `.cmd` 다.

#### 4a. dry-run 경로 (`-DryRunRestart`, 라이브 수집기 4프로세스를 대상으로)

```
decoy pids: 28916, 28828

watchdog exit=2
--- data/watchdog.log (sandbox) ---
2026-08-22 17:19:13 [watchdog] ALERT[CRIT] key=watch_log_stale file=ALERT_20260822_171913_watch_log_stale.txt bytes=1219
2026-08-22 17:19:13 [watchdog] restart-sample: wrote NOTE_20260822_171916_restart_sample.txt bytes=6742 mode=dryrun reason=log_stale procs=8 stacks=8 took=2.14s
2026-08-22 17:19:13 [watchdog] RESTART-DRYRUN reason=log_stale (would run: C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\ops\launch_collector.cmd)
2026-08-22 17:19:13 [watchdog] sup=8 col=8 session=unknown(raw=day,stale=3004s) age_min=50.1 src=state open_for=7203s col_up=3s free_gb=303.16 power=ac(100%) rank_age=20 auth_fail=0 fetch_pct=100 t2book=124139 problems=log_stale_50.1min
--- files written ---
ALERT_20260822_171913_watch_log_stale.txt  1219 bytes
NOTE_20260822_171916_restart_sample.txt  6742 bytes
SANDBOX=C:\Users\dongh\AppData\Local\Temp\w5gate4_9bb8f067
```

#### 4b. kill 경로 (미끼 프로세스만 죽인다)

```
decoy pids: 11472, 27020
alive before: 2

watchdog exit=2  wall=32s
alive after: 0
--- data/watchdog.log (sandbox) ---
2026-08-22 17:24:49 [watchdog] ALERT[CRIT] key=watch_log_stale file=ALERT_20260822_172450_watch_log_stale.txt bytes=1215
2026-08-22 17:24:49 [watchdog] restart-sample: wrote NOTE_20260822_172452_restart_sample.txt bytes=6830 mode=kill reason=log_stale procs=8 stacks=8 took=2.12s
2026-08-22 17:24:49 [watchdog] RESTART reason=log_stale launcher=C:\Users\dongh\AppData\Local\Temp\w5gate4b_ae127dd4\noop_launcher.cmd
2026-08-22 17:24:49 [watchdog] ALERT[WARN] key=restarted_log_stale file=ALERT_20260822_172521_restarted_log_stale.txt bytes=387
2026-08-22 17:24:49 [watchdog] RESTART outcome=FAILED sup=0 col=0
2026-08-22 17:24:49 [watchdog] sup=2 col=2 session=unknown(raw=day,stale=3003s) age_min=50.1 src=state open_for=7203s col_up=3s free_gb=302.89 power=ac(100%) rank_age=20 auth_fail=0 fetch_pct=100 t2book=124139 problems=log_stale_50.1min
--- files written ---
ALERT_20260822_172450_watch_log_stale.txt  1215 bytes
ALERT_20260822_172521_restarted_log_stale.txt  387 bytes
NOTE_20260822_172452_restart_sample.txt  6830 bytes
SANDBOX=C:\Users\dongh\AppData\Local\Temp\w5gate4b_ae127dd4
```

`alive before: 2` → `alive after: 0` 이고 `restart-sample: ... mode=kill` 줄이
`RESTART reason=` 줄보다 **먼저** 찍혔다 — 표본이 `taskkill` 앞에서 떠졌다는 뜻이다.
워치독 전체 벽시계 32.1s 중 표본은 2.12s.

재기동 경보가 표본 파일을 지목한다:

```
[WARN] 2026-08-22 17:24:49  key=restarted_log_stale

Watchdog restarted the collector. reason=log_stale outcome=FAILED
supervisor_procs=0 collector_procs=0
If outcome=FAILED check data/supervisor.stdout.log and data/collector.log tails.

RESTART SAMPLE: data/NOTE_20260822_172452_restart_sample.txt - stacks, process tree and IO deltas taken while the processes were still alive.
```

---

## 3. 표본 파일 실물

### 3-1. kill 경로 (`mode=kill`, 6830 bytes)

```
RESTART SAMPLE v1
taken   : 2026-08-22 17:24:49
mode    : kill
reason  : log_stale
budget  : 20s wall clock (-RestartSampleBudgetS)
grade   : NOTE_ - EVIDENCE, not a fault. The fault for this same event is
          filed as ALERT_*_watch_log_stale.txt; read them together.

[1] WATCHDOG VERDICT AT THIS MOMENT - the numbers this restart rests on
  age_min   : 50.1   (telemetry age, minutes)
  col_up    : 3s
  rank_age  : 20s
  t2book    : 124139
  session   : unknown  (raw=day)
  open_for  : 7203s
  src       : state
  sup / col : 2 / 2
  problems  : log_stale_50.1min

[2] PROCESS TREE - every python.exe under *, plus resolved ancestors
  PID     PPID    PRIO  THR  HND   WS_MB    CPU_S     UP_S     COMMAND
  19340   9636    6     1    56    1.1      0         68308    (command line not readable by this token)
  21684   19340   6     1    82    1.6      3.23      68308    (command line not readable by this token)
  9316    21684   6     1    54    1.1      0         68308    (command line not readable by this token)
  6292    9316    6     26   240   8        1700.73   68308    (command line not readable by this token)
  11472   29972   8     2    54    4        0         3        "C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\.venv\Scripts\python.exe" -c "W5GATE4B_SUPERVISOR_0c58d1 = 1; import time; time.sleep(300)" 
  27020   29972   8     2    54    4        0.02      3        "C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\.venv\Scripts\python.exe" -c "W5GATE4B_COLLECTOR_073007 = 1; import time; time.sleep(300)" 
  32360   11472   8     4    76    12.6     0.03      3        "C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\.venv\Scripts\python.exe" -c "W5GATE4B_SUPERVISOR_0c58d1 = 1; import time; time.sleep(300)" 
  13040   27020   8     4    76    12.6     0.02      3        "C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\.venv\Scripts\python.exe" -c "W5GATE4B_COLLECTOR_073007 = 1; import time; time.sleep(300)" 
  ancestors:
    pid 9636    ppid 14760   cmd.exe          up=68308s  (command line not readable by this token)
    pid 14760 - GONE (parent already exited; the chain above it is unreadable)
    pid 29972   ppid 19916   powershell.exe   up=5s  "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File C:\Users\dongh\AppD...
    pid 19916   ppid 30832   pwsh.exe         up=323s  "C:\Program Files\WindowsApps\Microsoft.PowerShell_7.6.5.0_x64__8wekyb3d8bbwe\pwsh.exe" -NoProfile -NonInteractive -Exec...
    pid 30832   ppid 29036   claude.exe       up=2472s  "C:\Users\dongh\.local\bin\claude.exe" --dangerously-skip-permissions
    pid 29036   ppid 4760    pwsh.exe         up=2473s  "C:\Program Files\WindowsApps\Microsoft.PowerShell_7.6.5.0_x64__8wekyb3d8bbwe\pwsh.exe" -NoLogo -NoExit -EncodedCommand ...
    pid 4760    ppid 18932   orca-terminal-daemon.exe up=69779s  C:\Users\dongh\AppData\Local\Orca\daemon-host\1.4.187\orca-terminal-daemon.exe C:\Users\dongh\AppData\Local\Orca\daemon-...
    pid 18932   ppid 6284    Orca.exe         up=69784s  "C:\Users\dongh\AppData\Local\Programs\orca\Orca.exe" 

[3] PYTHON STACKS - py-spy dump
    (no --locals on purpose: a stack says where it is stuck, and locals can
     carry the API token into a plain-text file under data/)
  py-spy: C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\.venv\Scripts\py-spy.exe
  --- pid 19340 (timeout 8.0s) ---
  Error: Failed to open process - check if it is running.
  
  Caused by:
      0: 액세스가 거부되었습니다. (os error 5)
      1: 액세스가 거부되었습니다. (os error 5)
  --- pid 21684 (timeout 8.0s) ---
  Error: Failed to open process - check if it is running.
  
  Caused by:
      0: 액세스가 거부되었습니다. (os error 5)
      1: 액세스가 거부되었습니다. (os error 5)
  --- pid 9316 (timeout 8.0s) ---
  Error: Failed to open process - check if it is running.
  
  Caused by:
      0: 액세스가 거부되었습니다. (os error 5)
      1: 액세스가 거부되었습니다. (os error 5)
  --- pid 6292 (timeout 8.0s) ---
  Error: Failed to open process - check if it is running.
  
  Caused by:
      0: 액세스가 거부되었습니다. (os error 5)
      1: 액세스가 거부되었습니다. (os error 5)
  --- pid 11472 (timeout 8.0s) ---
  Error: Failed to find python version from target process
  --- pid 27020 (timeout 8.0s) ---
  Error: Failed to find python version from target process
  --- pid 32360 (timeout 8.0s) ---
  Process 32360: "C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\.venv\Scripts\python.exe" -c "W5GATE4B_SUPERVISOR_0c58d1 = 1; import time; time.sleep(300)" 
  Python v3.13.15 (C:\Users\dongh\AppData\Local\Programs\Python\Python313\python.exe)
  
  Thread 12348 (idle)
      <module> (<string>:1)
  --- pid 13040 (timeout 8.0s) ---
  Process 13040: "C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\.venv\Scripts\python.exe" -c "W5GATE4B_COLLECTOR_073007 = 1; import time; time.sleep(300)" 
  Python v3.13.15 (C:\Users\dongh\AppData\Local\Programs\Python\Python313\python.exe)
  
  Thread 10144 (idle)
      <module> (<string>:1)

[4] IO / HANDLES / THREADS - two readings 2.05s apart (absolute = since
    process start; delta = what it did during those seconds)
  PID     READ_OPS     WRITE_OPS    READ_MB     WRITE_MB    HND     THR    CPU_S
  19340   1            0            0           0           56      1      0
  21684   160          3            1.8         0           82      1      3.23
  9316    1            0            0           0           54      1      0
  6292    3295749      4791376      11947       11207.3     240     26     1700.73
  11472   1            0            0           0           54      2      0
  27020   1            0            0           0           54      2      0.02
  32360   66           0            0.6         0           76      4      0.03
  13040   66           0            0.6         0           76      4      0.02
  DELTA over 2.05s
  PID     d_READ_OPS   d_WRITE_OPS  d_READ_KB   d_WRITE_KB  d_HND   d_THR  d_CPU_S
  19340   0            0            0           0           0       0      0
  21684   0            0            0           0           0       0      0
  9316    0            0            0           0           0       0      0
  6292    0            0            0           0           0       0      0
  11472   0            0            0           0           0       0      0
  27020   0            0            0           0           0       0      0
  32360   0            0            0           0           0       0      0
  13040   0            0            0           0           0       0      0

sample took 2.12s of the 20s budget.
```

### 3-2. dry-run 경로 (`mode=dryrun`, 6742 bytes) — 라이브 수집기 4프로세스 포함

```
RESTART SAMPLE v1
taken   : 2026-08-22 17:19:13
mode    : dryrun
reason  : log_stale
budget  : 20s wall clock (-RestartSampleBudgetS)
grade   : NOTE_ - EVIDENCE, not a fault. The fault for this same event is
          filed as ALERT_*_watch_log_stale.txt; read them together.

[1] WATCHDOG VERDICT AT THIS MOMENT - the numbers this restart rests on
  age_min   : 50.1   (telemetry age, minutes)
  col_up    : 3s
  rank_age  : 20s
  t2book    : 124139
  session   : unknown  (raw=day)
  open_for  : 7203s
  src       : state
  sup / col : 8 / 8
  problems  : log_stale_50.1min

[2] PROCESS TREE - every python.exe under *, plus resolved ancestors
  PID     PPID    PRIO  THR  HND   WS_MB    CPU_S     UP_S     COMMAND
  19340   9636    6     1    56    1.1      0         67972    (command line not readable by this token)
  21684   19340   6     1    82    1.6      3.22      67972    (command line not readable by this token)
  9316    21684   6     1    54    1.1      0         67971    (command line not readable by this token)
  6292    9316    6     26   240   8.1      1700.72   67971    (command line not readable by this token)
  28916   11632   8     2    54    4        0.02      4        "C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\.venv\Scripts\python.exe" -c "W5GATE4_SUP = 1; import time; time.sleep(300)" 
  28828   11632   8     2    54    4        0         4        "C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\.venv\Scripts\python.exe" -c "W5GATE4_COL = 1; import time; time.sleep(300)" 
  30164   28916   8     4    76    12.5     0.02      4        "C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\.venv\Scripts\python.exe" -c "W5GATE4_SUP = 1; import time; time.sleep(300)" 
  11892   28828   8     4    76    12.6     0.03      4        "C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\.venv\Scripts\python.exe" -c "W5GATE4_COL = 1; import time; time.sleep(300)" 
  ancestors:
    pid 9636    ppid 14760   cmd.exe          up=67972s  (command line not readable by this token)
    pid 14760 - GONE (parent already exited; the chain above it is unreadable)
    pid 11632   ppid 7348    powershell.exe   up=5s  "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File C:\Users\dongh\AppD...
    pid 7348    ppid 30832   pwsh.exe         up=5s  "C:\Program Files\WindowsApps\Microsoft.PowerShell_7.6.5.0_x64__8wekyb3d8bbwe\pwsh.exe" -NoProfile -NonInteractive -Exec...
    pid 30832   ppid 29036   claude.exe       up=2136s  "C:\Users\dongh\.local\bin\claude.exe" --dangerously-skip-permissions
    pid 29036   ppid 4760    pwsh.exe         up=2136s  "C:\Program Files\WindowsApps\Microsoft.PowerShell_7.6.5.0_x64__8wekyb3d8bbwe\pwsh.exe" -NoLogo -NoExit -EncodedCommand ...
    pid 4760    ppid 18932   orca-terminal-daemon.exe up=69443s  C:\Users\dongh\AppData\Local\Orca\daemon-host\1.4.187\orca-terminal-daemon.exe C:\Users\dongh\AppData\Local\Orca\daemon-...
    pid 18932   ppid 6284    Orca.exe         up=69447s  "C:\Users\dongh\AppData\Local\Programs\orca\Orca.exe" 

[3] PYTHON STACKS - py-spy dump
    (no --locals on purpose: a stack says where it is stuck, and locals can
     carry the API token into a plain-text file under data/)
  py-spy: C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\.venv\Scripts\py-spy.exe
  --- pid 19340 (timeout 8.0s) ---
  Error: Failed to open process - check if it is running.
  
  Caused by:
      0: 액세스가 거부되었습니다. (os error 5)
      1: 액세스가 거부되었습니다. (os error 5)
  --- pid 21684 (timeout 8.0s) ---
  Error: Failed to open process - check if it is running.
  
  Caused by:
      0: 액세스가 거부되었습니다. (os error 5)
      1: 액세스가 거부되었습니다. (os error 5)
  --- pid 9316 (timeout 8.0s) ---
  Error: Failed to open process - check if it is running.
  
  Caused by:
      0: 액세스가 거부되었습니다. (os error 5)
      1: 액세스가 거부되었습니다. (os error 5)
  --- pid 6292 (timeout 8.0s) ---
  Error: Failed to open process - check if it is running.
  
  Caused by:
      0: 액세스가 거부되었습니다. (os error 5)
      1: 액세스가 거부되었습니다. (os error 5)
  --- pid 28916 (timeout 8.0s) ---
  Error: Failed to find python version from target process
  --- pid 28828 (timeout 8.0s) ---
  Error: Failed to find python version from target process
  --- pid 30164 (timeout 8.0s) ---
  Process 30164: "C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\.venv\Scripts\python.exe" -c "W5GATE4_SUP = 1; import time; time.sleep(300)" 
  Python v3.13.15 (C:\Users\dongh\AppData\Local\Programs\Python\Python313\python.exe)
  
  Thread 15788 (idle)
      <module> (<string>:1)
  --- pid 11892 (timeout 8.0s) ---
  Process 11892: "C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\.venv\Scripts\python.exe" -c "W5GATE4_COL = 1; import time; time.sleep(300)" 
  Python v3.13.15 (C:\Users\dongh\AppData\Local\Programs\Python\Python313\python.exe)
  
  Thread 3560 (idle)
      <module> (<string>:1)

[4] IO / HANDLES / THREADS - two readings 2.05s apart (absolute = since
    process start; delta = what it did during those seconds)
  PID     READ_OPS     WRITE_OPS    READ_MB     WRITE_MB    HND     THR    CPU_S
  19340   1            0            0           0           56      1      0
  21684   160          3            1.8         0           82      1      3.22
  9316    1            0            0           0           54      1      0
  6292    3295749      4791374      11947       11207.3     240     26     1700.72
  28916   1            0            0           0           54      2      0.02
  28828   1            0            0           0           54      2      0
  30164   66           0            0.6         0           76      4      0.02
  11892   66           0            0.6         0           76      4      0.03
  DELTA over 2.05s
  PID     d_READ_OPS   d_WRITE_OPS  d_READ_KB   d_WRITE_KB  d_HND   d_THR  d_CPU_S
  19340   0            0            0           0           0       0      0
  21684   0            0            0           0           0       0      0
  9316    0            0            0           0           0       0      0
  6292    0            0            0           0           0       0      0
  28916   0            0            0           0           0       0      0
  28828   0            0            0           0           0       0      0
  30164   0            0            0           0           0       0      0
  11892   0            0            0           0           0       0      0

sample took 2.14s of the 20s budget.
```

측정 조건: 위 두 파일 모두 이 세션(중간 무결성 대화형 토큰)에서 떴다. 라이브 수집기
4프로세스(19340·21684·9316·6292)에 대한 py-spy 는 `os error 5`(액세스 거부)로 실패했고,
같은 세션이 띄운 파이썬 미끼(25292/22592, 14392 등)에 대해서는 스택이 정상적으로 떠졌다.
이 접근 거부의 범위에 대해서는 §8 을 볼 것.

---

## 4. 시간 예산

**상한 20.0s** (`-RestartSampleBudgetS`, 기본값).

근거:

- kill 경로는 이미 **~28s** 를 쓴다 — 죽인 뒤 `Start-Sleep 3`, 재기동 확인 `Start-Sleep 25`.
- telemetry 는 **300s 주기**로 나온다. 20s 를 더해도 재기동 경로 전체가 ~50s 로 한 telemetry
  주기 안에 들어가므로, **표본이 재기동을 한 주기 놓치게 만들 수 없다.**
- 실측 소요는 **2.12~2.15s** (게이트 4a/4b, python 8프로세스 · 스택 8개 · IO 간격 2.0s).
  그중 2.0s 는 IO 2회 측정 간격이므로 **고정비**고, 나머지가 ~0.13s 다. 20s 는 실측의
  약 10배이고, 목적은 평균 비용이 아니라 **행(hang) 을 자르는 것**이다.
- 예산 안에서 다시 쪼갠다: py-spy 프로세스당 상한 `min(8.0s, 남은 예산 - (IO 간격 + 2.0s))`.
  IO 2회차를 위한 몫을 먼저 떼어 놓으므로, 스택을 다 못 떠도 델타는 남는다. 예산이 떨어지면
  파일에 `BUDGET EXHAUSTED after N stack(s)` 라고 적고 멈춘다 — **조용히 자르지 않는다.**
- 프로세스 수 상한 8 (`-RestartSampleMaxProcs`). 라이브 사슬은 4다. 초과분은 `[2]`·`[4]` 에는
  남고 스택만 빠지며, 그 사실이 파일에 한 줄로 찍힌다.

**예산을 넘든 예외가 나든 `RESTART` 는 그대로 진행된다.** 수집 전체가 하나의
`try/catch` 안에 있고, 세 호출자 중 둘은 반환값을 `Out-Null` 로 버린다. 실패는
`data/watchdog.log` 의 `restart-sample: FAILED ... - RESTART CONTINUES` 한 줄로만 남는다.

---

## 5. `$DryRunRestart` · `restart_budget_exhausted` 경로 결정과 근거

### `$DryRunRestart` → **뜬다** (`mode=dryrun`)

1. 라이브 수집기를 죽이지 않고 표본 수집기를 실행할 수 있는 **유일한 경로**다. 여기서
   건너뛰면 이 블록은 실제 사고가 나야만 시험되는 코드가 된다 — 드물게 돌고 그 한 번에
   맞아야 하는 코드에 그건 나쁜 거래다. 게이트 4a 가 바로 이 경로다.
2. 아무것도 죽이지 않으므로 **수집 공백이 0** 이다. 비용이 없다.
3. 사고 표본과 헷갈리지 않게 파일 머리말에 `mode: dryrun` 이 찍힌다.

부작용 확인: `ops/watchdog_selftest.ps1` 은 dry-run 으로 워치독을 ~60회 돌린다. 표본이
매번 뜨지만 게이트 1 은 130/130 통과하고 (NOTE 단정들은 전부 특정 키 1건 매칭이라
파일이 하나 더 생겨도 안 깨진다), 샌드박스에서는 `RepoRoot` 가 임시 디렉터리라 py-spy 가
발견되지 않아 스택 단계가 즉시 건너뛰어진다.

### `restart_budget_exhausted` → **뜬다** (`mode=budget_exhausted`)

1. 재기동을 **포기하는** 경로다. 멈춘 프로세스가 **살아남는 유일한 경로**이고, 따라서
   사람이 파일과 실물을 나중에 대조할 수 있는 유일한 경로다.
2. 아무것도 죽이지 않으므로 여기서도 **수집 공백이 0** 이다.
3. 이 스크립트가 낼 수 있는 가장 큰 도움 요청이다. 그게 증거가 없는 유일한 경로여서는 안 된다.

---

## 6. py-spy 설치 결과

```
> C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\.venv\Scripts\pip.exe install py-spy
Downloading py_spy-0.4.2-py2.py3-none-win_amd64.whl (1.9 MB)
Successfully installed py-spy-0.4.2

> .venv\Scripts\py-spy.exe --version
py-spy 0.4.2
```

**버전 0.4.2**, 위치 `C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\.venv\Scripts\py-spy.exe`
(이 워크트리의 venv, Python 3.13.15).

`Get-PySpyExe` 는 `<RepoRoot>\.venv\Scripts\py-spy.exe` 를 먼저 보고, 없으면 PATH 를 본다.
둘 다 없으면 표본은 **그래도 떠지고** 파일에 없는 이유와 설치 명령이 적힌다 — py-spy 는
하드 의존이 아니다.

pip 목록은 커밋하지 않았다: `requirements.txt` 류 파일이 이 레포에 없고, `pyproject.toml`
의존성에 진단 도구를 넣는 것은 요청 범위 밖이다.

---

## 7. 병합 증거 문자열

병합 뒤 코디네이터가 grep 할 것:

| 대상 | 문자열 |
|---|---|
| **`ops/watchdog.ps1`** (1순위) | `RESTART SAMPLE v1` |
| `ops/watchdog.ps1` | `Write-RestartSample $reason "kill"` — 삽입점이 `taskkill` 앞에 있는지 |
| `docs/34_alert_grades.md` | `NOTE_<stamp>_restart_sample.txt` |
| 런타임 `data/watchdog.log` | `restart-sample:` |
| 런타임 `data/` | `NOTE_*_restart_sample.txt` |

```
$ grep -c "RESTART SAMPLE v1" ops/watchdog.ps1
1
```

---

## 8. 안 한 것 / 못 한 것

### 8-1. ★ 라이브 수집기에 py-spy 가 붙는지는 **검증하지 못했다**

측정한 것:

- 라이브 사슬 4프로세스(19340·21684·9316·6292)에 대해 이 세션에서
  `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION=0x1000)` 이 **error 5 (액세스 거부)** 다.
  0x0400·0x0010·0x0008 도 전부 5. `ExecutablePath`·`CommandLine` 도 빈 문자열로 온다.
- 이 세션의 토큰은 `Medium Mandatory Level`, 사용자 `WORKSTATION\dongh`.
- 같은 사용자가 같은 세션에서 띄운 파이썬 프로세스에는 py-spy 가 **정상 동작**한다 (§3 참조).
- `tossmon-watchdog` / `tossmon-collector-oneshot` 두 작업 모두
  `LogonType=S4U`, `<RunLevel>` 요소 **없음**(= LeastPrivilege), 같은 사용자 SID.
- `data/watchdog.log` 18878~18880 행: 2026-08-21 18:45:55 에 `sup=1 col=2` 상태에서
  워치독이 `taskkill /F` 를 돌렸고 `RESTART outcome=OK`, 18:50 에 `col_up=291s` 로 새
  프로세스가 올라왔다. 즉 **워치독 자신의 토큰은 이 프로세스들에 대해 종료 권한을 가졌다.**

가져오지 못한 관측: 워치독 자신의 토큰이 py-spy 에 필요한
`PROCESS_QUERY_INFORMATION|VM_READ` 를 갖는지. 그것을 보려면 S4U 컨텍스트에서 py-spy 를
돌려야 하는데, 그건 새 예약작업을 만들거나 라이브 워치독 작업을 킥해야 하고 명세 §3 이
예약작업을 금지한다. **판정하지 않는다.**

이 조건에서도 표본은 스택 없이 나머지 셋을 담고, 실패 원인 문자열이 파일에 그대로 찍힌다
(게이트 4a 의 `[3]` 절이 그 모양이다). 코디네이터가 휴장 창에 한 번 확인하기를 권한다.

### 8-2. 안 한 것

- **조용한 정지 자체는 안 고쳤다.** 명세 §3 대로 관측만 만들었다.
- **`<Priority>` 재등록 / 예약작업 · 라이브 수집기 · `STOP`/`PLANNED` 표식** 전부 안 건드렸다.
- **`tossmon/**` 안 건드렸다.** 변경 파일은 `ops/watchdog.ps1` 과 `docs/34_alert_grades.md` 둘뿐이다.
- **라이브 `ops/watchdog.ps1` 에 반영하지 않았다.** 명세 §5 대로 브랜치에서만 고쳤고
  반영은 코디네이터가 휴장 창에 한다. 다음 정규장은 2026-08-24(월) 22:30.
- **IO 델타의 "정상값"을 정하지 않았다.** 게이트에서 관측된 델타는 전부 0인데, 측정 시각이
  `session=closed` 인 휴장 중이었다. 정규장에서 같은 값이 무엇을 뜻하는지는 표본이 실제로
  한 번 잡힌 뒤의 문제다.
- **`py-spy dump --locals` 는 쓰지 않았다.** 지역변수가 API 토큰을 `data/` 아래 평문
  파일로 끌고 들어올 수 있다. 파일에 그 이유가 적혀 있다.
- **커밋하지 않은 워크트리 잔여물**: `config/config.yaml.bak-20260814-preD21`,
  `ops/ops_config.yaml` 은 착수 전부터 untracked 였고 손대지 않았다 (둘 다 gitignore 대상).

### 8-3. 판정하지 않은 것

표본은 아직 **0 개**다. 조용한 정지의 원인에 대해 이 보고는 아무것도 주장하지 않는다.
게이트 4 의 숫자들은 **표본 수집기가 동작한다**는 증거이지 수집기 장애에 대한 증거가 아니다.
"라이브 수집기 pid 6292 의 IO 델타가 2.06초 동안 0이었다"는 관측은 **휴장 중 측정**이며
그 이상으로 읽지 마라.

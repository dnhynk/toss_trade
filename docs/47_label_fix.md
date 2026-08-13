# 47. 1분봉 라벨 규약 코드 수정 (2026-08-09, 소유: W3)

> **[기록]** 소유 W3 · 2026-08-09 · **과거 판정은 재실행하지 않았다**(D-10, 사용자)
> 상태 표기의 뜻과 전수 목록: [`docs/INDEX.md`](INDEX.md)

<details>
<summary><b>이 문서의 지도 — 절 11개 (453줄)</b></summary>

- 0. 네 줄 요약
- 1. 무엇을 고쳤는가 — 분류별 대조
- 2. 49 밖에서 추가로 찾은 것 — **혼자 판단해 고쳤다, 신고한다**
- 3. 고치지 않고 **보고만** 하는 것
- 4. (라) 판정 불가 5곳 재판정 — **4곳이 갈렸다, 1곳은 여전히 못 가른다**
- 5. 남긴 나-컷 3곳 — 왜 멈췄나
- 6. 깨진 테스트 17개 — **전부 (A)** 이고, 근거를 하나씩 적는다
- 7. 테스트 규약을 구조로 고정했다 — `docs/36` §5-5 의 근본 해결
- 8. 바뀌는 수치 — **적기만 한다. 재실행하지 않았다.**
- 9. 실행한 검증
- 10. 무엇을 하지 않았는가

</details>

> **이 문서는 판정하지 않는다.** `docs/36` 이 센 49개 지점을 고치고, 무엇이 바뀌는지
> **적기만** 한다. **과거 판정은 재실행하지 않았다** (사용자 결정 2026-08-08: "고치고
> 다시 돌리진 마"). 홀드아웃 미열람, 라이브 호출 0회, 수집기 무접촉.
>
> 정본은 `docs/36` §2-2·§3-4 (분류)와 `docs/12` §6.1·§6.2-5 (규약 문언)다.
> `docs/36` §1~5 는 수정하지 않았다.

---

## 0. 네 줄 요약

1. **49개 중 45개를 고쳤다** (나-창 20 · 나-경계 25). 남은 **나-컷 3개는 사전등록
   §2.1 예측 1a 의 문언(`ts_ms < t0_ms`)이 산술을 직접 박고 있어** 올렸고,
   **사용자 결정은 (B) "산술도 고친다" 이되 §9 개정이 먼저**다. 이번에는
   **주석만 고쳤고 산술은 한 글자도 안 바꿨다** (§5-1).
2. **규약을 한 곳에 모았다.** `session.bar_start_ms()` 하나가 "라벨 → 봉이 담는 구간의
   시작"을 정의하고, 캔들 축의 모든 경계·창 판정이 그것을 통과한다. `docs/36` §5-3 이
   걱정한 "정의가 흩어져 한쪽만 고쳐지는 날"을 구조로 막았다.
3. **(라) 판정 불가 5곳 중 4곳이 갈렸다** — 일봉 라벨을 실측으로 확인했다(99.4%),
   결과는 **(다) 지금 맞음**이라 손대지 않았다. 나머지 1곳은 **여전히 판정 불가**다 (§4).
4. **깨진 테스트는 17개, 전부 (A)** = 테스트 쪽이 옛 규약을 박고 있었다 (§6).
   그리고 **합성기와 소비자가 같은 규약을 공유하던 구조를 깼다** — 절대 시각으로 고정한
   독립 테스트 17개를 새로 붙였고, **수정 전 코드에서 12개가 빨개지는 것을 실측 확인**했다.

---

## 1. 무엇을 고쳤는가 — 분류별 대조

`docs/36` §2-2 의 49개 번호를 그대로 쓴다.

| 분류 | docs/36 이 센 수 | 이번에 고친 수 | 남긴 수 |
|---|---:|---:|---:|
| **나-컷** (컷오프) | 3 | **0** | **3** — 사전등록 문언 사안, §5 로 올림 |
| **나-창** (창 전체 밀림) | 20 | **20** | 0 |
| **나-경계** (세션·매매일·날짜 경계) | 25 | **25** | 0 |
| **나-누출** | 1 | (이전 태스크에서 이미 고침) | 0 |
| **합계** | 49 | **45** | 3 (+ 이미 고쳐진 1) |

### 1-1. 규약 정의부 — 새로 만든 것 하나

**`tossmon/analysis/session.py`**

```python
def bar_start_ms(ts_ms: int) -> int:      # 라벨 T → T − 60초
def bar_starts_ms(ts) -> pd.Series:       # 벡터화판
```

`session.py` 를 고른 이유는 그 파일이 스스로 *"**이 파일 하나만 고치면 되도록** 정의를
여기에 모아 둔다(중복 정의 금지)"* 라고 적어 두었기 때문이고, 봉인 경계 수정(`is_holdout`)
이 이미 이 규약을 여기서 쓰고 있었기 때문이다. `is_holdout` 도 하드코딩된 `- 60_000`
대신 이 함수를 타도록 바꿨다.

> **중요 — 이 함수를 테이프·호가에 쓰면 안 된다.** `trades_snap.ts_ms` ·
> `book_snap.snap_ms` 는 라벨이 아니라 **진짜 순간**이다 (`docs/36` §2-0).
> 그래서 `session_of` · `sessions_of` · `session_date` **자체는 보정하지 않았다** —
> 그 함수들은 양쪽 축에서 다 불린다(`design_b` · `exit_value` · `tape_cost` ·
> `tick_instrument` 는 진짜 순간을 넘긴다). 보정은 **캔들을 넘기는 호출부**가 씌운다.

### 1-2. 나-창 20개 — 창을 **내용 구간**으로 재정의했다

밀림의 근원은 `_minute_volumes` 였다. 슬롯 `i` 에 라벨 `t_from + i·60초` 인 봉을 넣고
있었는데, 그 봉의 내용은 한 분 앞선다. 이제 인자를 **내용 구간**으로 읽고 슬롯 `i` 에
라벨 `t_from + (i+1)·60초` 인 봉을 넣는다 — 라벨로는 `(t_from, t_to]` 다.

| # | 파일 | 무엇이 바뀌었나 |
|---|---|---|
| 2 | `features._minute_volumes` | 슬롯 매핑 (위) |
| 5 | `features._first_bar_vol_z_cross` | 히트 시각을 **라벨**로 되돌린다 (`lo + (i+1)·60초`) |
| 7 | `features._expected_window_vol` | 루프를 `range(t_from+60초, t_to+60초)` — `curve_key` 는 라벨을 받는다 |
| 8 | `features._volume_features` | `t_hi = cutoff + MIN_MS` → **`t_hi = cutoff`**. 창이 내용 구간이 되면서 cutoff 봉이 그대로 포함된다 |
| 11 | `features._price_features` VWAP 창 | `end_ms = cutoff + MIN_MS` → `end_ms = cutoff` |
| 12, 14 | `features._print_activity_features` | 같은 `t_hi` 정정 (`no_print_ratio_*`, `dormant_ratio_prior_day`) |
| 19 | `baselines.minute_of_session_volume_curve` | `by_ts.get(start + (m+1)·60초)` — **RVOL 분모 곡선 전체** |
| 20, 21 | `baselines.curve_locate` / `curve_key` | `minute = (bar_start − start)//60초`, 소속도 `bar_start` 기준 |
| 22 | `baselines.rvol` | 누적 하한 `ts >= start` → `ts > start` |
| 23 | `baselines.rvol_series` | 세션 판정·분 위치 전부 `bar_start` 기준 — **RVOL 분자 전체** |
| 36 | `rotation._window_stats` | `[lo, hi)` 를 내용 구간으로 (`lo < ts <= hi`) |
| 43, 46 | `rotation_validate` / `rotation_sweep` SQL 창 | `ts>=? AND ts<?` → `ts>? AND ts<=?` |
| 48 | `execution_measure` 분 조인 | `minute = (ts_ms − 60000)//60000` (캔들만. `snap_ms` 는 그대로) |
| 49 | `execution_measure` `STRICT_PRIOR` | 로직 유지, **주석 정정** — 아래 |

> **`STRICT_PRIOR` 는 조인을 고치자 비로소 주석대로 동작한다.** 예전 주석은
> *"a snapshot inside minute m would otherwise be matched to the bar that **CLOSES at
> m+1**"* 라고 적었는데 이건 **시작 라벨 전제**다. 종료 라벨에서는 조인이 이미 한 분
> 과거를 물고 있었고, `shift(1)` 이 그 위에 한 분을 더 얹어 **2봉 과거**가 돼 있었다.
> 조인을 고치면 조인이 동시간(contemporaneous)이 되고, `shift(1)` 이 정확히
> *"minute m−1 까지의 봉만 본다"* 가 된다. **막으려던 누출은 원래 없었고, 되찾은 것은
> 1봉이다.**

### 1-3. 나-경계 25개 — `start <= ts < end` → `start < ts <= end`

사전등록 §6.1 이 2026-08-07 개정으로 **이미 요구하고 있던 식**이다. 코드가 문언을
따라가지 못하고 있었다.

| # | 파일 | 비고 |
|---|---|---|
| 3 | `features.assign_market_days` | 이진탐색 대상을 `bar_start` 로 |
| 4 | `features._locate_day_start` | UTC 폴백도 `bar_start//DAY_MS` |
| 6 | `features._locate_session_start` | 캘린더 폴백 경로 |
| 9 | `features._volume_features` (`rv.index >= sess_start` → `>`) | 라벨 `sess_start` 봉은 직전 세션 |
| 10 | `features._price_features` (`sess`) | 같음 |
| 13 | `features` `session_first_print_lead_min` | **`bar_start` 로 계산** — 첫 분부터 체결이면 0 (예전엔 1) |
| 15, 16 | `features._history_features` 매매일 그룹화 / 이력 버킷 | UTC 폴백 포함 |
| 17 | `features` `float_rotation_pre` | `>= lo` → `> lo` |
| 18 | `baselines.locate_session` | `bar_start` 기준 |
| 24, 25 | `baselines.session_vwap_u` / `session_vwap_map` | **정규장 종가 경매 봉이 되살아난다** |
| 26 | `baselines.split_ratio_series` 1분봉 버킷 | |
| 27 | `labeling.detect_events` 매매일 슬라이싱 | `searchsorted` 대상을 `ts − 60초` 로 |
| 28 | `labeling._prev_day_close_u` | |
| 29 | `labeling._regular_frame` | `close_ref_u` 가 진짜 마지막 봉이 된다 |
| 30 | `labeling._build_label` (`session`, `t0_min_from_open`) | `t0_min_from_open` 이 1 과대였다 |
| 31 | `labeling._build_label` 정규장 VWAP | `session_vwap_u` 수정으로 자동 |
| 32 | `labeling._build_label` `next_day_gap` | "정규장 시가" 자리에 들어가던 **프리마켓 마지막 분 시가**를 몰아냈다 |
| 33, 34 | `session_of`/`sessions_of`/`session_date`/`is_holdout` 의 **캔들 호출부** | 함수가 아니라 호출부에서 보정 (§1-1 경고) |
| 38, 40 | `rotation_q1` / `decel_entry` `load_candles` | `sessions_of(bar_starts_ms(...))` |
| 39, 41 | `rotation_q1` / `decel_entry` `session_counts` | 같음 |
| 42, 45, 47 | `rotation_validate` / `rotation_sweep` / `rotation_pilot` SQL 날짜 | `date((ts_ms-60000)/1000,'unixepoch')` |
| 44 | `rotation_validate` 정규장 `fill` 비율 | |

`_day_spans` 의 UTC 폴백(`labeling.py`)도 같이 고쳤다 — #27 과 한 몸이라 한쪽만
고치면 캘린더 유무에 따라 결과가 갈린다.

---

## 2. 49 밖에서 추가로 찾은 것 — **혼자 판단해 고쳤다, 신고한다**

`docs/36` 의 49개 목록에 **없던** 지점 하나를 고쳤다.

| 파일:줄 | 무엇 | 왜 고쳤나 |
|---|---|---|
| `features.py:241-245` (`extract_precursor_features` 의 `cutoff_day` 조회) | 분할일 판정용 매매일 소속을 `_lo <= cutoff < _hi` 로 보고 있었다 | **#4 `_locate_day_start` 와 같은 질문에 다른 답을 내게 된다.** 한쪽만 고치면 컷오프가 매매일 경계에 걸린 날 `split_dates` 가 조용히 빗나간다 (3차 감사 F-3 이 막으려던 바로 그 무음 스킵) |

세는 단위가 `docs/36` §2-0 의 "한 코드 위치 × 한 사용 패턴"이므로 이건 **50번째 지점**으로
세는 것이 맞다. `docs/36` 이 놓친 것이다 — 그 문서는 수정하지 않고 여기에 적는다.

---

## 3. 고치지 않고 **보고만** 하는 것

### 3-1. `is_holdout` 이 이제 **캔들 전용**이다

봉인 경계 수정으로 `is_holdout(ts) = session_date(ts − 60초)` 가 됐는데,
`tick_instrument.py:120` 은 이 함수를 **체결 테이프**(`trades_snap`, 진짜 순간)에 쓴다.
진짜 순간에 60초를 빼면 봉인 경계가 60초 어긋난다.

- **크기: 0행.** `trades_snap` 은 2026-07-31 부터라 봉인 구간(~07-29)과 겹치지 않는다.
  경계에 걸릴 수 있는 유일한 시각대(2026-07-30T00:00:00~00:01:00Z)에 테이프가 없다.
- **고치지 않은 이유**: 봉인 **정의**를 손대는 문제이고, 사전등록 §6.2 항목 5 의 구현
  요구 문언이 `session_date(ts_ms − 60_000)` 로 못 박혀 있다. 축별로 갈라 쓰려면
  그 문언을 어떻게 읽을지부터 정해야 한다 — 내가 혼자 정할 일이 아니다.

### 3-2. `rotation_validate` / `rotation_sweep` 의 `pt0 >= win.end_ms` 배제

대조군 t0 를 만들 때 `pt0 >= win.end_ms` 면 버린다. 종료 라벨에서는 라벨 `end_ms` 인
봉이 그 세션의 **마지막 분**이므로 `> win.end_ms` 가 옳다. **1봉 과보수**이고 대조군
후보 수가 세션당 최대 1 줄어들 뿐이라 방향이 안전하다. `docs/36` 의 49 목록에 없고,
고치면 대조군 구성이 바뀌어 **어느 변경이 수치를 움직였는지 못 가르게 된다.**

> `moff`/`pt0` 쌍 자체는 **이미 맞다.** `moff = (t0 − start)//60초` 가 1 과대이고
> `pt0 = start + moff·60초` 가 라벨을 만들 때 그 1이 정확히 상쇄된다. 그래서
> `locate_session` 을 고쳐도 이 쌍은 자기일관을 유지한다 (실측: 관련 테스트 전부 초록).

### 3-3. 태스크가 명시로 뺀 것

- **애프터 세션 종료 08:50 → 09:00 KST** (`docs/36` §3-3 부수 관찰, `docs/40` §6-3):
  손대지 않았다. **별도 태스크**다.
- **`execution_measure.py:199`** (날짜 조건·`drop_holdout` 부재, 위치 기반 `shift(5)` —
  `docs/40` §7): 라벨 문제가 아니라 손대지 않았다.
- **일봉 라벨 규약**: §4 에서 **확인만** 하고 코드는 건드리지 않았다.

---

## 4. (라) 판정 불가 5곳 재판정 — **4곳이 갈렸다, 1곳은 여전히 못 가른다**

`docs/36` §2-4 가 (라)로 남긴 이유는 그때 **1분봉 라벨이 확정 전**이라 기준계열이
없었다는 것이다. 지금은 확정됐으므로(사전등록 §6.1) 그것을 자로 쓸 수 있다.

### 4-1. #55~#58 (일봉 축 4건) → **(다) 지금 맞음**. 실측으로 갈랐다.

네 지점(`et_midnight_ms` · `daily_bar_date_table` · `compute_daily_baseline` as-of 필터 ·
`split_ratio_series` 일봉 쪽)은 **전부 하나의 질문**에 걸려 있다:
`candles_1d.ts_ms` 가 그 매매일의 **00:00 ET**(현재 코드 가정)를 가리키는가?

**시험**: 정규장 1분봉 거래량 합(**확정된 종료 라벨 규약**으로 집계)과 일봉 거래량의
**정확 일치**를 두 가정으로 센다.

| 가정 | 뜻 | 정확 일치 |
|---|---|---:|
| **H_start** (현재 코드) | 일봉 ts 의 UTC 날짜 D = 매매일 D | **167** |
| H_end | 일봉 ts 는 매매일 D−1 의 종료 라벨 | **1** |
| 둘 다 불일치 | 1분봉 백필이 성겨 총량이 안 맞는 날 | 781 |

**판별 가능 949건 중 어느 한쪽만 맞은 168건에서 H_start 167 (99.4%) / H_end 1 (0.6%).**

- 측정 조건: `w5-ops/data/tossmon.db` 읽기 전용(`mode=ro` + `query_only=ON`),
  **2026-05-01 이전만**(홀드아웃 미열람), DST 전환달(3·11월) 전체 제외,
  정규장 창은 `docs/36` §1-1 과 같은 고정 오프셋 근사(EDT 13:30–20:00Z / EST 14:30–21:00Z).
- **네 지점 모두 소비자가 결국 `date` 로 환원**하므로(`daily_bar_date` 는
  `[00:00 ET, +24h)` 버킷) 일봉 ts 의 **하루 안 위치**는 이 네 지점에 영향이 없다.
  갈라야 했던 것은 **어느 날에 속하는가**뿐이고, 그것이 확인됐다.
- **그래서 코드를 고치지 않았다.** (나)가 아니라 (다)였다.

> **한계 — 정직하게 적는다.** 이 시험은 일봉의 **날짜 귀속**을 확인했지 일봉 ts 의
> **시각 라벨 자체**(00:00 ET 인가 16:00 ET 인가)를 확인한 것이 아니다. 위 네 지점에는
> 무관하지만, 일봉 ts 를 **시각으로** 쓰는 코드가 앞으로 생기면 그때 다시 물어야 한다.
> 재현 스크립트는 `scratchpad/daily_label_check.py` (일회성, 저장소에 안 넣음).

### 4-2. #59 `rotation_exit.simulate_signal_exit` → **여전히 판정 불가**

`signal` 계열의 시간축은 **호출자가 정한다**. `docs/36` 시점과 똑같이 **저장소 안에
호출자가 없다** — 전수 grep 결과 `tests/test_rotation_exit.py` 6곳(합성 시리즈)뿐이고
`tossmon/**` 에는 정의만 있고 사용처가 없다. 캔들 축이면 (가), 틱 축이면 교차축이 된다는
분기가 그대로 남아 있다. **손대지 않았다.**

---

## 5. 남긴 나-컷 3곳 — 왜 멈췄나

대상: `features.cut_frame(include_t0=False)` (#1) · `rotation.print_frame(t_to)` (#35) ·
`measure/rule_search.build_events` (#37).

**사전등록 §2.1 P1 '예측 1a' 가 문언으로 산술을 박고 있다:**

> "**엄격 컷오프(`ts_ms < t0_ms`) 기준** T0 이전 거래량 서지 … 검출률 ≤ 0.7"

그리고 `docs/40` §5-4 는 이번 라벨 개정이 **"§1 가설(P1~P6)·§4 판정 기준: 무수정"**
이라고 못 박았다. 즉 이 산술을 바꾸는 것은 **가설 문언 변경 = §9 개정 절차 사안**이고
내 위임 범위 밖이다.

**그리고 이 3곳은 나머지 46곳과 성격이 다르다**:

| | 나-창 / 나-경계 | 나-컷 |
|---|---|---|
| 값이 틀리나 | **그렇다** (다른 60초를 잰다 / 다른 봉을 넣는다) | **아니다** — 1봉 덜 쓸 뿐, 방향은 과보수 |
| 누출 가능성 | 경계는 오배정, 값 오염 | **원리적으로 불가능** (`docs/36` §3-1) |
| 사전등록이 산술을 박았나 | 아니다 (§6.1 은 오히려 새 식을 **요구**한다) | **그렇다** |
| 무엇이 틀렸나 | 코드 | **근거 주석** ("T0 봉 종가는 그 분이 끝난 뒤에만 관측되므로") |

`docs/36` §5-1 도 같은 결론이었다: *"최소 조치는 **주석 정정**이다. 동작을 바꾸는 것은
사전등록 §2 와 얽혀 있으므로 **문서 정정과 분리해서** 사용자 판단을 받아야 한다."*

**그래서 코디네이터에게 올렸고, 회신 전까지 이 3곳은 코드도 주석도 건드리지 않았다** —
주석만 고치는 것도 한쪽을 선택한 셈이 되기 때문이다.

### 5-1. 회신 — **사용자 결정 (B), 단 순서가 있다** (2026-08-09)

> **산술도 `ts <= t0` 로 고친다. 그런데 사전등록 §9 개정이 먼저다.**
> `docs/12` §2.1 P1 예측 1a 문언을 W7 이 고친 뒤에 코드를 고친다 (D-12 때와 같은 순서:
> W7 개정 → main 머지 → W3 코드). **지금 그 3곳의 산술을 고치지 마라.**
> **주석만 고치고**, 주석에 "산술 변경은 §9 개정 후 별도 태스크"라고 적어 둘 것.

그대로 했다. **이번 커밋에서 이 3곳의 산술은 한 글자도 안 바뀌었다.**

| # | 파일 | 주석에 적은 것 |
|---|---|---|
| 1 | `features.py` 모듈 docstring + `cut_frame` | 옛 근거가 시작 라벨 전제라 성립하지 않음 · 종료 라벨에서 두 모드의 성격이 문서와 **반대**(`True` 가 정확, `False` 가 1봉 과보수) · 누출은 원리적으로 불가능 · `cutoff_lag_min ≥ 1` 이 구조적 산물 · **산술 변경은 §9 개정 후 별도 태스크** |
| 35 | `rotation.py` `print_frame` | `t_to` 배타 컷은 누출 방지가 아니라 1봉 과보수 · `t_from` 쪽도 대칭으로 한 봉 더 들임 · 같은 유보 문구 |
| 37 | `measure/rule_search.py` `build_events` | pre `<t0` / post `>=t0` 양쪽의 이동 방향 · 같은 유보 문구 |

**3곳의 산술 수정은 W7 개정이 main 에 들어간 뒤 별도 태스크로 받는다.**

---

## 6. 깨진 테스트 17개 — **전부 (A)** 이고, 근거를 하나씩 적는다

기준은 태스크가 준 그대로다. **(A)** = 테스트가 옛 기대값/옛 규약을 박고 있었다.
**(B)** = 내 수정이 뭔가를 망가뜨렸다.

**(B) 는 하나도 없었다.** 그 판단의 근거는 아래 각 항의 "무엇이 옛 규약이었나"이고,
기대값을 새 출력에 맞춘 항목은 **하나도 없다** — 전부 **입력(픽스처)의 시간축**을
고쳤거나, **경계 의미 자체를 뒤집는 단언**을 새 규약대로 다시 썼다.

### 6-1. 픽스처가 시작 라벨로 봉을 만들고 있었다 (11개) — (A)

합성 프레임을 `session.start_ms + m·60초` 로 만들면 슬롯 m 의 봉이 한 분 앞선
라벨을 갖는다. 픽스처를 `start_ms + (m+1)·60초` 로 옮기면 **기대값은 그대로** 통과한다.
이것이 "기대값을 안 건드렸다"의 실체다.

| 테스트 | 무엇이 옛 규약이었나 |
|---|---|
| `test_analysis_baselines::test_curve_is_mean_per_minute_position` | 봉 라벨 = 슬롯 시작 |
| `…::test_curve_counts_missing_minutes_as_zero` | 같음 |
| `…::test_curve_exclude_dates_keeps_windows_but_drops_average` | 같음 + `curve_locate(curve, d1)` 이 세션 **시작 시각**을 분 0 의 라벨로 봤다 |
| `…::test_rvol_cumulative_definition` | 같음 (조회 라벨도 +1분) |
| `…::test_session_vwap_exact` | 세션 첫 두 분의 봉 라벨을 0·1분으로 봤다 |
| `…::test_session_vwap_zero_volume_prefix` | 같음 |
| `test_analysis_calendar::test_history_features_group_by_market_day_not_utc_date` | `_fill_day` 픽스처가 시작 라벨 |
| `…::test_former_runner_proxy_not_double_counted_in_winter` | 같음 |
| `…::test_summer_unaffected_by_the_fix` | 같음 |
| `…::test_cumulative_rvol_also_length_aware` | `_half_day_fixture` 가 시작 라벨 |
| `…::test_curve_key_helper` | 같음 (분 209 의 봉 라벨은 210분) |

> `test_analysis_calendar` 의 M-3 3건은 **첫 봉이 매매일 시작 시각에 놓여 있어서**
> 새 규약에서 그 봉이 매매일 **밖**으로 나갔고, 그래서 `covered=False` → UTC 날짜
> 폴백 → `hist_days_available = 2.0` 이 됐다. 픽스처를 고치자 1.0 으로 돌아왔다.
> **M-3 회귀 방어는 그대로 살아 있다.**

### 6-2. 단언 자체가 시작 라벨 의미였다 (4개) — (A), 단언을 뒤집었다

여기서는 **기대값을 새 출력에 맞춘 것이 아니라, 무엇이 옳은지가 뒤집혔다.** 그래서
새 단언에 **왜 그것이 옳은지**를 주석으로 남겼다.

| 테스트 | 옛 단언 | 새 단언 | 근거 |
|---|---|---|---|
| `test_analysis_baselines::test_locate_session` | `locate_session(md, regular.start_ms) == "regular"` | `== "pre"`, 그리고 `regular.end_ms` 는 `"regular"` | 라벨 = 개장 시각인 봉의 내용은 프리마켓 마지막 분 |
| `…::test_session_vwap_filters_by_window_and_is_empty_outside` | `index.max() < reg.end_ms` | `index.max() == reg.end_ms` | **정규장 마지막 분(종가 경매) 봉을 예전엔 통째로 버렸다** (`docs/36` §3-3) |
| `test_analysis_calendar::test_market_day_span_helpers` | `assign_market_days([span_start]) == [그 매매일]` | `[None]`, 첫 봉은 `span_start + 1분` | 매매일 **시작 시각**은 라벨이 아니다 |
| `test_analysis_synth::test_all_bars_inside_declared_sessions` (7 파라미터) | `start <= ts < end` | `start < ts <= end` | 사전등록 §6.1 문언 그대로 |

### 6-3. 픽스처가 **세션 경계에 봉을 얹어** 의미가 뒤바뀐 것 (2개) — (A), 제일 위험했던 것

| 테스트 | 무슨 일이 있었나 |
|---|---|
| `test_analysis_prereg::test_prev_close_uses_prior_regular_session_not_after_hours` | 픽스처가 애프터장 첫 봉을 라벨 `after.start_ms`(= `regular.end_ms`)에 놓았다. 새 규약에서 그 봉은 **정규장 종가 경매**가 되므로 "정규장 종가 = 100" 이 아니라 **200**이 됐고 이벤트가 사라졌다. **코드는 옳게 동작했다.** 픽스처를 `start + (m+1)·60초` 로 옮기니 의도(정규장 100 / 애프터 200)가 복원됐다 |
| `test_rotation::test_rotation_scores_ranks_the_mover_top` | 픽스처가 분 0~59 를 라벨 0~59 로 만들어, 급증 시작(분 30)의 봉이 **직전 창**에 걸쳤다 → `rank_delta = 0`. 라벨을 1~60 으로 옮기니 급증이 최근 창 안에만 들어간다 |

> **이 둘이 (B) 로 오인되기 가장 쉬웠다** — 증상이 "이벤트가 사라졌다"·"순위가 안
> 올랐다"라 수정이 뭔가를 깬 것처럼 보인다. 실제로는 픽스처가 세션 경계에 놓은 봉의
> **의미가 규약과 함께 바뀐 것**이다. 기대값(`len == 1`, `rank_delta > 0`)은
> **그대로 두고** 입력만 고쳐서 통과했다는 사실이 (A) 라는 증거다.

### 6-4. 나머지 (2개) — (A)

`test_analysis_synth::test_include_next_day_false` (`ts.max() < after.end_ms` →
`<=`), `…::test_prev_close_comes_from_prior_regular_session` (프레임 필터 뒤집기).

---

## 7. 테스트 규약을 구조로 고정했다 — `docs/36` §5-5 의 근본 해결

`docs/36` §4 의 진단: *합성 생성기와 소비자가 같은 규약을 써서 결함을 통과시켰다.*
두 가지를 했다.

1. **생성기를 종료 라벨로 바꿨다** (`tests/synth.py`: `ts = win.start_ms + (m+1)·60초`,
   그리고 정답 계산부의 세션 소속 필터 7곳). 이제 생성기도 API 사실과 같다.
2. **그것만으로는 부족하다** — 셋이 다 같은 규약이면 여전히 자기일관일 뿐이다. 그래서
   **`tests/test_bar_label_convention.py` 를 새로 붙였다**: 합성기를 **쓰지 않고**,
   모든 시각을 하드코딩한 epoch ms 로 두고, ISO 문자열과 일치하는지 매 실행 자기검사한다
   (`test_hardcoded_constants_are_what_the_comments_say`).

   ```
   REG_OPEN      = 1_780_320_600_000   # 2026-06-01T13:30:00Z (EDT 09:30 ET)
   REG_CLOSE     = 1_780_344_000_000   # 2026-06-01T20:00:00Z (EDT 16:00 ET)
   HOLDOUT_FRONT = 1_777_593_600_000   # 2026-05-01T00:00:00Z
   HOLDOUT_BACK  = 1_785_369_600_000   # 2026-07-30T00:00:00Z
   ```

   17개 테스트가 규약·봉인 양 경계·세션 판정·창 매핑·`close_ref_u`·`t0_min_from_open`·
   `next_day_gap` 을 절대 시각으로 못 박는다.

**검증**: `tossmon/` 변경만 되돌리고(`git stash push -- tossmon/`) 이 파일을 돌리면
**17개 중 12개가 빨개진다**(실행 확인). 나머지 5개는 봉인 경계처럼 이전 태스크에서
이미 고쳐진 항목과 상수 자기검사다.

---

## 8. 바뀌는 수치 — **적기만 한다. 재실행하지 않았다.**

> 사용자 결정(2026-08-08): **"고치고 다시 돌리진 마."** 아래는 *"다시 재면 이 값들이
> 움직인다"* 는 **목록**이지 새 값이 아니다. **"그래서 결과가 이렇게 바뀐다"는 쓰지 않는다.**
> 과거 판정(설계 A·B·회전 논제·감속 진입)은 **"밀린 자로 잰 것"** 으로 남는다.

### 8-1. 피처 (`features.feature_names()` 72개 중)

| 무엇 | 왜 움직이나 |
|---|---|
| `vol_sum_{5,15,30,60}_qu` · `vol_z_{w}` · `vol_ratio_{w}` · `rvol_curve_{w}` (16개) | 창이 60초 제자리로 온다 (`_minute_volumes` + `t_hi`) |
| `vol_slope_30` · `vol_bar_z_max_60` · `daily_vol_z` | 같음 |
| `no_print_ratio_{w}` (4개) · `dormant_ratio_prior_day` | 같음 |
| `vol_surge_lead_min` | 히트 봉의 라벨이 1분 뒤로 (리드타임 1 감소 방향) |
| `rvol_at_cutoff` · `rvol_first_cross_{2,3,5}_lead_min` | 곡선(분모)·`rvol_series`(분자) 양쪽이 같이 움직인다. **비율 자체는 대체로 상쇄되고 세션 가장자리에서만 바뀐다** (`docs/36` §3-2 완화) |
| `dist_from_vwap` | 세션 VWAP 창이 한 봉씩 이동 |
| `session_first_print_lead_min` | **1 과대였다 → 첫 분 체결이면 0** |
| `hist_days_available` · `prior_event_count_20d` · `days_since_prior_event` · `former_runner` | 매매일 경계 봉 1개의 재배정 |
| `float_rotation_pre` | 세션 첫 봉 1개 제외 |
| `day_grouping_calendar` | 매매일 경계 봉이 캘린더에 덮이는지 여부가 바뀔 수 있다 |

**`cutoff_lag_min` · `minutes_since_toss_entry` · `ranking_lead_lag_min` ·
`toss_share*` 는 안 바뀐다** — `docs/36` §2-3 의 (다) 5곳이라 손대지 않았다.
`ret_*` · `atr_pct_*` · `range_pct_*` · `coil_score` · `nr_ratio` ·
`up_bar_ratio_30` · `new_high_count_30` · `minutes_since_last_print` 등 (가) 25곳도
정의상 불변이다.

### 8-2. 라벨 (`labeling.EVENT_COLUMNS`)

| 무엇 | 왜 |
|---|---|
| `ret_close` · `retrace_close` · `vwap_close_rel` · `closed_below_vwap` | `close_ref_u` 가 **정규장 진짜 마지막 봉(종가 경매)**이 된다. 예전엔 끝에서 두 번째 |
| `t0_min_from_open` | **1 과대였다** |
| `next_day_gap` | "정규장 시가" 자리의 **프리마켓 마지막 분 시가**가 빠진다 |
| `session` | 세션 경계에 걸린 T0 의 배정 |
| `float_rotation` · `halt_gap_count` | 매매일/정규장 프레임의 앞뒤 1봉 |
| `rvol_at_t0` · `rvol_gated` → **이벤트 집합 자체** | RVOL 게이트가 바뀌면 T0 후보가 바뀐다 |

> **이벤트 집합이 바뀔 수 있다는 것이 제일 크다.** 게이트(`rvol_at_t0 >= 3.0`)와
> 매매일 슬라이싱이 둘 다 움직이므로, 검출되는 이벤트 수·시각이 달라질 수 있다.
> **얼마나 달라지는지는 세지 않았다** — 그것이 재실행이다.

### 8-3. 러너·측정 산출물

| 무엇 | 왜 |
|---|---|
| `rotation_q1` · `decel_entry` 의 `session` · `cycle_date` · `session_counts` | 세션 경계 분의 재배정. **사이클 경계에서 매매일 클러스터 수가 달라질 수 있다**(`pooled_ci_permitted` 의 입력) |
| `rotation_validate` · `rotation_sweep` 의 `have` 날짜 집합 · 봉 적재 창 · 정규장 `fill` | 날짜 귀속과 창이 한 분씩 이동 |
| `rotation_pilot` 의 일별 봉 적재 | 같음 |
| `execution_measure` 의 `spread_vs_move` (`abs_ret_5m` · `vol5_qu` 매칭) | **2봉 과거 → 1봉 과거** (§1-2). `docs/18` 의 그 표가 움직인다 |
| `rotation.rotation_scores` 를 타는 전부 | 창이 내용 구간이 됐다 |

### 8-4. 안 바뀌는 것

- **틱 해상도 5개 모듈** (`tape_cost` · `tick_instrument` · `tick_tasting` ·
  `design_b` · `exit_value`)과 `tick_resolution` · `tick_stages` · `cross_peak_check` ·
  `vol_matched_placebo` · `density_matched_placebo` · `scale_convention` ·
  `scale_mixed_recount` · `intra_second_bias`: `candles_1m` 을 안 읽는다
  (`docs/36` §2-0). **사정거리 밖이다.**
- 일봉 축 전부 (§4-1 로 (다) 확정).
- 봉인 필터를 통과한 9봉: **이전 태스크에서 이미 막혔다**. 이번 수정으로 추가 변화 없음.

---

## 9. 실행한 검증

```
$ python -m pytest tests/ -q -p no:randomly
1867 passed, 1 skipped, 2 deselected, 46 warnings in 369.85s
```

- **수정 전 기준선**: `1850 passed, 1 skipped, 2 deselected`
- 새로 붙인 `tests/test_bar_label_convention.py` 17개를 더해 **1867**.
- **수정 전 코드에서 새 파일을 돌린 결과**(`git stash push -- tossmon/`):
  `12 failed, 5 passed` — 새 테스트가 실제로 옛 규약을 잡는다는 확인.

---

## 10. 무엇을 하지 않았는가

- **과거 판정을 다시 돌리지 않았다.** 설계 A/B · 회전 논제 · 감속 진입 근처에 가지
  않았다. §8 은 *무엇이 움직이는가* 의 목록이고 새 수치가 아니다.
- **"이제 결과가 이렇게 바뀐다"를 결론으로 쓰지 않았다.**
- **홀드아웃을 열지 않았다.** §4-1 의 일봉 시험은 **2026-05-01 이전만** 읽었다
  (`mode=ro` + `PRAGMA query_only=ON`).
- **라이브 API 호출 0회. 수집기 무접촉.** `tossmon/collector/**`(W4)·`ops/**`(W5)
  미변경.
- **`docs/36` §1~5 를 수정하지 않았다.** §2-2 의 49개 목록도 그대로 뒀다 — 이 문서가
  §2 를 추가하는 형태다.
- **나-컷 3곳의 산술을 바꾸지 않았다.** 주석만 고쳤다 (§5-1, 사용자 결정 순서).
- **사전등록(`docs/12`)을 고치지 않았다.** §2.1 예측 1a 개정은 W7 소유다.

# W4 보고 — 예산의 사건 타임라인을 monotonic 으로 분리 (docs/52 §5.5)

task: task_2989b7ebc8b7 / dispatch: ctx_2be1aa073d5b
브랜치: `w4-collector`. **라이브 호출 0, 수집기 기동 0, `config.yaml` diff 0줄, `limiter.py` diff 0줄,
`test_limiter_vs_counter.py` diff 0줄, 홀드아웃 접근 0.**
검증: 전부 오프라인 (로그는 읽기 전용).

---

## 1. 고치기 전 red — 같은 도구, 같은 명령

`python -m tools.replay_send_time --skip-log` (진짜 송신 첨두 = **10**)

수정 전 코드(HEAD `42f60e4`)에서:

```
        시계  stall |            옛 계상 (완료시각)      |            새 계상 (송신시각)
              (s) | peak중앙 최대     >10    p95   avg | peak중앙 최대     >10    p95   avg
       이상적   0.00 |    10   10   0/174    9.0  6.32 |    10   10   0/174    9.0  6.32
       이상적   0.20 |    10   11  12/174    9.0  6.32 |    10   10   0/174   10.0  6.32
     지터 있음   0.00 |    13   13 174/174    9.0  6.32 |    12   13 174/174   10.0  6.32
     지터 있음   0.20 |    12   13 174/174    9.0  6.32 |    12   13 174/174   10.0  6.32
     지터 있음   0.40 |    11   12 174/174    9.0  6.32 |    11   13 158/174   10.0  6.32
```

**지터 시계에서 새 계상도 174/174 가 한도 10 을 넘는다.** 이것이 red 다.

그리고 08-12 운영 로그를 **직접 세어** 안 닫혔음을 확인했다
(`--log ../w5-ops/data/collector.log --since "2026-08-12 00:00"`, 읽기 전용):

```
표본 104건 (00:00:15 ~ 08:37:17, session=after/regular)
md_peak_1s 분포: 0:1 6:2 7:10 8:22 9:10 10:15  11:34  12:9  13:1
한도 10 초과: 44/104 = 42.3%
지속률 avg 중앙 5.05 / 최대 6.07 req/s (목표 8.50)
```

`clock skew` 경보는 **로그 전체에 딱 1건** (`08:34:53,344 ERROR clock skew -5.1s`).
5초 임계는 그날 한 번 걸렸고 ±180ms 지터는 그 아래를 계속 지나다녔다.

> 코디네이터가 넘긴 수치는 103표본/43% 였다. 구간 경계 한 건 차이이고 분포도
> (11:34 / 12:9 / 13:1) 일치한다. 결론 동일.

---

## 2. 고친 뒤 green — 같은 도구, 같은 명령

도구에 **B-1(수정 전 배선) / B-2(수정 후 배선)** 두 표를 나란히 넣었다. 한 번 실행하면 둘 다 나온다.

```
### B-1. 예산 사건 타임라인 = 서버 보정 벽시계 (수정 전)
     지터 있음   0.00 |    13   13 174/174 |    12   13 174/174
     지터 있음   0.20 |    12   13 174/174 |    12   13 174/174
     지터 있음   0.40 |    11   12 174/174 |    11   13 158/174

### B-2. 예산 사건 타임라인 = 단조 시계 (수정 후, 프로덕션)
     지터 있음   0.00 |    10   10   0/174 |    10   10   0/174
     지터 있음   0.20 |    10   11  18/174 |    10   10   0/174
     지터 있음   0.40 |    10   10   0/174 |    10   10   0/174
```

**새 계상 열: 174/174 → 0/174. 첨두 중앙 12 → 10.**

### 대조군 — 시계가 이상적이면 값이 그대로다

```
### B-1 이상적 0.00 |    10   10   0/174 |    10   10   0/174
### B-2 이상적 0.00 |    10   10   0/174 |    10   10   0/174
### B-2 는 '이상적' 행과 '지터 있음' 행이 **완전히 같다** (사건 시각이 안 움직인다)
```

첨두가 내려간 이유가 "고쳤다" 이지 "다른 걸 망가뜨렸다" 가 아니라는 뜻이다.
단위테스트에도 같은 대조군을 뒀다 (`test_the_control_case_an_ideal_clock_gives_the_same_answer_both_ways`).

**B-2 의 '옛 계상' 열이 stall 0.20 에서 12/174 → 18/174 로 움직인 것은 이 수정과 무관하다**:
그 열은 송신 시각을 못 주는 client 로 내려가는 **폴백 경로**(`on_requests`, 완료 시각 균등분포)이고,
벽시계 경로의 ms 절삭이 사라지며 버킷 경계가 달라진 것이다. 프로덕션 client 는 이 경로로 안 내려간다.

### 단위테스트 red/green (도구와 독립)

`tests/test_budget_event_clock.py` — 진짜 리미터가 만든 송신열(첨두 9) 위에서
**벽시계 배선 첨두 11**, **단조 배선 첨두 9**. 변수는 시계 하나뿐이다.

---

## 3. monotonic 누출 없음 — 테스트로 고정

`time.monotonic()` 의 **원점은 임의값**이고 재시작마다 리셋된다. 고정 방법은
"위험한 필드를 내가 맞히기" 가 아니라 **원점 불변성**이다:

같은 사건열을 원점만 바꿔(0 / 8.64e6 / 1.75e9 / 12345.678) 흘려
`snapshot()` · `counters` · `limiter_peak()` · `ctx.telemetry()` · `ctx.state_snapshot()`
이 **완전히 같은지** 본다. 전부 같다. 추가로 snapshot 의 모든 값이 `< 1e6` (시각 규모가 아님)임도 단언한다.

* `test_monotonic_origin_never_leaks_into_any_guard_output`
* `test_the_monotonic_origin_does_not_leak_through_the_collector_either`

> **예외 하나를 밝힌다: `p95_1s` / `per_second_counts`.** 정렬된 고정 1초 버킷을 세므로
> **버킷 경계 위상**에 의존하고, 위상은 시계 원점이 정한다. 원래부터 위상 의존이고
> (자기 docstring 이 그렇게 말한다) 그래서 판정은 슬라이딩 `peak_1s` 가 한다.
> 테스트는 이 둘만 "경계 하나 차이 이내" 로 본다. 벽시계 시절 위상은 epoch 정렬,
> 지금은 부팅 정렬인데 **서버 창 위상은 어느 쪽과도 무관**하므로 의미가 안 바뀐다.

### 재시작

`_events` 는 프로세스 수명이다 — 상태파일에 예산 시각이 하나도 없다(위 불변 테스트가 같이 확인).
새 프로세스는 빈 창에서 시작하고 창이 덜 찬 동안 지속률은 **과소평가**된다 (기동 버스트로
정원을 안 깎는 안전한 방향, 수정 전부터 그랬다). 옛 시각과 새 원점이 섞일 경로는 없다.
원점이 42초로 점프한 새 가드가 같은 답(첨두 9)을 내는 것을 테스트가 확인한다
(`test_a_restart_starts_the_event_window_from_scratch_and_that_is_correct`).

---

## 4. 벽시계로 남긴 판정 — 목록과 이유

| 자리 | 시계 | 이유 |
|---|---|---|
| `_events` 스탬프 (`on_request`·`on_requests`·`on_sends`) | **단조** | 첨두는 송신 **간격**의 함수다 |
| `_limiter_window` 스탬프·컷오프, `limiter_peak` | **단조** | 예산 첨두와 나란히 읽는 독립 관측 |
| `peak_1s`·`p95_1s`·`measured_rate`·`_live_events` 컷오프 | **단조** | 위 스탬프와 같은 축 |
| `note_session_change` / `in_warmup` (180초) | **벽시계** | "개장으로부터 180초" — 개장은 **서버 시각** 정의 |
| `should_shrink` 쿨다운(30초) · 지속 유지(60초) | **벽시계** | 분 단위 판정, 세션 경계와 맞물림 |
| `on_429` / `should_grow` 회복 대기(300초) · 스텝 간격 | **벽시계** | 위와 같다 |

두 축이 **서로 비교되는 자리는 없다** (직접 확인: `_last_429_s`/`_last_shrink_s`/`_last_grow_s`/
`_measured_over_since`/`_warmup_until_s` 는 전부 벽시계 `now` 하고만 비교된다).

### 변이 게이트 — 되돌림 하나 + **반대 방향 실수** 둘

`python -m tools.mutation_accounting` → **10/10 killed**

| | 심는 결함 | 죽인 테스트 |
|---|---|---|
| D7 | 사건 타임라인을 벽시계로 되돌린다 | `test_the_production_wiring_puts_the_event_timeline_on_a_monotonic_clock` 외 2 |
| D8 | 워밍업(세션 판정)을 단조로 옮긴다 | `test_warmup_follows_the_wall_clock_not_the_monotonic_one` |
| D9 | 축소 쿨다운을 단조로 옮긴다 | `test_the_shrink_cooldown_follows_the_wall_clock` |

---

## 5. W1 소유 파일에 남긴 요구

**`tests/test_limiter_vs_counter.py` 는 한 줄도 안 고쳤고, 고칠 필요도 없었다** (통과 확인).

이유를 실측했다: `test_peak_1s_can_exceed_the_limit_although_the_limiter_never_did` 의
송신 시각열은 `_Bucket(rate=1e9, window_cap=10)` 포화 주입이라 **전부 `t=0.0`** 이다
(직접 확인: `spread = 0.0`). 11건이 어느 시계에서든 한 순간에 놓이므로 시계 분리와 무관하다.

**요구 (지금 필요한 수정 아님, 그 테스트가 자라날 때의 조건):**

> 그 파일의 `BudgetGuard(...)` 는 `clock=` 만 주입한다. 예산의 **사건 타임라인**은
> 이제 별도의 단조 시계(`mono=`, 기본 `time.monotonic`)를 쓴다. 그 테스트가 앞으로
> 송신을 **시간축에 벌려서** 재현하려 하면(지금은 전부 t=0 이라 안 벌어진다)
> `mono=lambda: clock.t` 를 같이 주입해야 의도한 시각열이 예산에 반영된다.
> 안 주면 벽시계 드라이버(`clock.t`)가 조용히 무시되고 모든 사건이 실시간 한 순간에 쌓인다.

---

## 6. 혼자 내린 판단 (자기 신고)

1. **`loops.py` 는 1줄이 아니라 signature 1줄 + 인자 1줄 + 주석 5줄이다.**
   `CollectorContext.create(mono=None)` 파라미터를 새로 뚫었다. 기본값(`time.monotonic`)만으로도
   프로덕션 동작은 맞지만, 그러면 **테스트가 사건 타임라인을 결정론적으로 몰 수 없다**
   (FrozenClock/VirtualClock 을 쓰는 통합테스트 전부가 실시간 `time.monotonic` 위로 떨어진다).
   주입점을 안 뚫으면 회귀 테스트가 성립하지 않아서 뚫었다.

2. **직접 생성 11곳이 아니라 테스트 헬퍼 5곳 + 도구 1곳을 고쳤다.**
   개별 생성마다 고치는 대신 `guard()` / `_build_ctx` / `build_ctx` / `_guard_at_plan` /
   replay `_ctx` 같은 **헬퍼 한 자리**를 고치는 쪽을 택했다 — diff 가 작고, 주입 이유를
   한 곳에만 적으면 된다. 실제로 예상 밖으로 걸린 것은 `test_collector_loops.py`(2건)와
   `test_collector_replay.py`(가속 리플레이 1건)였다. 가속 리플레이에서는 `mono` 를
   `VirtualClock.local_now_ms` 에 묶었다 — 실시간 `time.monotonic` 을 쓰면 60초 창이
   가상시각 `60×scale` 초를 담아 첨두가 통째로 틀린다.

3. **`_measured_over_since`(지속 초과 60초 유지)를 벽시계에 뒀다.** 지시가 "분 단위 판정은
   벽시계" 였고 60초는 분 단위다. 다만 조건 자체(`measured_rate`)는 단조 축에서 계산된다 —
   즉 "단조로 잰 초과가 벽시계로 60초 유지됐나" 다. 5초 skew 가 그 유지 시간을 5초
   늘리거나 줄일 수 있다. 그 정도는 수용 가능하다고 판단했다.

4. **`tools/replay_send_time.py` 의 `table_a` 문구를 정정했다.** 이전 커밋에서 "10 초과
   표본은 새 계상에서 전부 10 이하로 내려온다" 라고 적었는데, 그것은 **두 수정이 다 있을
   때만** 참이다 (B-1 이 반례). 문구에 정정을 명시했다.

5. **`docs/52` §5.4 는 지우지 않고 §5.5 를 더했다.** "지시를 못 받아 안 고쳤다" 는 기록은
   남기는 것이 맞다고 판단했다.

6. **`scheduler.py`(오프셋 지터의 발생지)는 손대지 않았다** — 내 소유가 아니다.
   docs/52 후속 목록에 "`offset_ms` 를 텔레메트리 한 필드로" 를 유효한 채로 남겼다.
   사건 타임라인은 면역이 됐지만 **세션·워밍업·쿨다운 판정은 여전히 그 위에 서 있고**,
   `clock skew` 임계 5초로는 ±180ms 가 안 보인다.

---

## 7. 검증

| | 명령 | 결과 |
|---|---|---|
| 전체 회귀 | `python -m pytest -q` | **1,985 passed / 1 skipped / 4 deselected** (수정 전 1,975 + 신규 10) |
| 변이 게이트 | `python -m tools.mutation_accounting` | **10/10 killed** (신규 D7·D8·D9 포함) |
| 오프라인 재계상 | `python -m tools.replay_send_time --skip-log` | §1·§2 표 |
| 운영 로그 (읽기 전용) | `... --log ../w5-ops/data/collector.log --since "2026-08-12 00:00"` | §1 |

**선언하지 않는 것**: "이제 한도 안이다" 가 아니다. 전부 오프라인 재계상이고 실제 첨두는
배포 후 `md_peak_1s` 를 봐야 안다. 이 수정이 예측하는 것은 하나다 —
**배포 후 `md_peak_1s` 는 10 을 넘지 않는다.** 넘으면 이 진단이 틀린 것이고,
그때 의심할 것은 리미터 밖 송신(다중 기동)이다 (docs/52 §6).

---

## 8. 파일

수정: `tossmon/collector/budget.py`, `tossmon/collector/loops.py`,
`tests/test_collector_budget.py`, `tests/test_collector_loops.py`, `tests/test_collector_replay.py`,
`tests/test_double_billing.py`, `tests/test_send_time_accounting.py`,
`tools/replay_send_time.py`, `tools/mutation_accounting.py`, `docs/52_send_time_accounting.md`
신규: `tests/test_budget_event_clock.py`

diff 0줄: `config/config.yaml`, `tossmon/api/limiter.py`, `tests/test_limiter_vs_counter.py`,
`tossmon/collector/scheduler.py`

# 45. 리미터가 1초 창을 못 지키는가, 계측이 거짓말하는가 — **계측이다**

작성: W1, 2026-08-08
질문: 운영 로그의 `budget: MARKET_DATA 1초에 11~14회 — 공시 한도 10 초과` 580건은
(가) 리미터 결함인가, (나) 계측 과장인가, (다) 창 기준 차이인가.

---

## 0. 측정 조건 (먼저 읽을 것)

| 항목 | 값 |
|---|---|
| 코드 | `feat/core-api` @ `0b02688` (main 에 fast-forward, 163커밋 따라잡음) |
| 로그 | `w5-ops/data/collector.log` 2026-07-31 14:25 ~ 2026-08-07 19:50 KST |
| 상태 | `w5-ops/data/collector_state.json` (읽기 전용) |
| 텔레메트리 표본 | `md_peak_1s` 가 있는 줄 **927개** (5분 간격) |
| 초과 ERROR | `1초에` 줄 **593건** (MARKET_DATA 580 / CHART 13) |
| **라이브 API 호출** | **0건** — 이 판정에 라이브가 필요 없었다 (§1 이 구조적 증명이라) |
| 수집기 재시작·설정 변경 | 없음 |
| 테스트 | `tests/test_limiter_vs_counter.py` (신규 6건), 회귀 287 passed / 1 skipped |

---

## 1. 결론

> ### **(나) 다. 리미터는 멀쩡했고 계측이 틀렸다.**

세 갈래로 갈렸다:

| 가설 | 판정 | 근거 |
|---|---|---|
| **(가)** 리미터가 1초 창을 못 지킨다 | **기각** | §2 — 구조적으로 불가능하고, 테스트로 고정했다 |
| **(나)** 계측이 과장한다 | **확정** | §3 — 결함 2개를 코드에서 특정하고 결정론적으로 재현했다 |
| **(다)** 창 기준이 다르다 (보낸 시각 vs 도착 시각) | **못 갈랐다** | §5 — 그리고 이 질문에는 애초에 답이 아니다 |

**한 줄**: 우리는 초당 10회를 넘긴 적이 없다. 넘겼다고 **센** 것이다.

---

## 2. (가) 기각 — 하드캡은 어떤 1초 구간도 못 넘는다

### 2.1 구조적 증명

`limiter.acquire()` 는 송신 직전에 이렇게 한다 (`limiter.py:107-116, 233-241`):
최근 `WINDOW_HORIZON_S`(=1.15초) 안의 송신이 `window_cap`(=공시 한도) 개면 대기한다.

> 임의의 1.0초 구간 `I=(a, a+1.0]` 을 잡고 그 안의 **마지막** 송신을 `t` 라 하자.
> 송신 시점 `t` 에서 캡은 `(t-1.15, t]` 안의 송신을 `cap` 개 이하로 보장한다.
> `t ∈ I` 이므로 `t ≤ a+1.0`, 따라서 `t-1.15 ≤ a-0.15 < a`.
> 즉 `I ⊆ (t-1.15, t]` 이고, `I` 안의 송신도 **`cap` 개 이하**다. ∎

**1.15초 창을 지키면 1.0초 창은 자동으로 지켜진다.** 위상을 몰라도 안전하다.

### 2.2 테스트로 고정 (신규)

| 테스트 | 무엇을 막나 |
|---|---|
| `test_hard_cap_holds_at_market_data_scale_under_saturating_concurrency` | 한도 **10** 그룹에 동시 22건을 몰아도 1초 구간 최대 ≤ 10. 기존 테스트는 한도 3 에서만 고정하고 있었다 (운영 사고는 한도 10 에서 났다) |
| `test_hard_cap_bounds_every_one_second_window_by_construction` | cap 1·3·5·10 에서 포화 주입해도 ≤ cap |
| `test_retry_after_429_reacquires_the_limiter` | **429 재시도가 캡을 우회하지 않는다** — 송신 2건에 `acquire` 2건 |

버킷을 일부러 과잉 공급(`usage_ratio=1.2`)해 병목에서 뺐다. 그래야 남는 제약이
하드캡뿐이라 하드캡만 시험하게 된다 — 기본값 0.85 에서는 버킷이 먼저 걸려서
하드캡이 발화조차 안 하고, 그러면 "통과"가 아무것도 증명하지 못한다.

### 2.3 우회 경로 전수 조사

`TossClient._request` 의 **재시도 루프 맨 위**에 `await self.limiter.acquire(group)` 가 있다
(`client.py:160-161`). 그래서 429 재시도·인증 재발급·5xx 백오프가 **전부** 캡을 다시 통과한다.
`counters["requests"]` 는 `_send` 안에서 소켓 직전에 오르므로 acquire 와 1:1 이다.

남은 우회로는 하나뿐이다: **`tokens.py:381` 의 토큰 발급**이 자체 `httpx.AsyncClient` 로
리미터 밖에서 나간다. 다만 이것은 AUTH 계열이고 `MARKET_DATA` 카운터를 건드리지 않으므로
**MD 첨두를 만들 수 없다.** (별건으로 남긴다.)

### 2.4 이 기각이 성립하지 않는 유일한 경우

리미터는 **프로세스 안**에서만 유효하다 (`limiter.py:222-224`). 수집기가 두 개 겹쳐 돌면
실제 송신이 한도를 넘을 수 있다. 다만 그랬다면 **서버가 MD 에 429 를 줬어야 하는데 안 준다**
(`docs/37` §3.5: 429 250건 중 250건이 `own_within_limit=True`, 전부 CHART 가 맞았다).
그래서 다중 프로세스는 관측과 모순이다. **데이터로 직접 배제하지는 않았다.**

---

## 3. (나) 확정 — 같은 호출이 두 번 계상된다

### 3.1 계측은 송신을 세지 않는다

`over_limit_1s` 는 리미터를 **안 본다**. `BudgetGuard._events` 를 보는데, 그 이벤트를
넣는 곳은 운영 코드에 딱 둘뿐이다 (`grep`):

```
loops.py:597   after_call()        -> budget.on_requests(group, booked)
loops.py:609   sync_rate_limits()  -> budget.on_requests(group, self._unaccounted_attempts())
```

둘 다 **응답이 돌아온 뒤**에 불린다 (`_poll_trades`: `await get_trades(...)` **다음 줄**이
`ctx.after_call(...)`). 즉 계상 시각은 **송신 시각이 아니라 완료 시각**이다. 리미터가
지키는 것은 송신이고, 우리가 세는 것은 완료다. **처음부터 같은 것을 재고 있지 않았다.**

### 3.2 결함 D1 — `sync_rate_limits` 가 남의 그룹 송신을 가져간다 (무클램프)

```python
def sync_rate_limits(self, group: str = GROUP_MARKET_DATA) -> None:
    self.budget.on_requests(group, self._unaccounted_attempts())   # ← 클램프 없음
```

`_unaccounted_attempts()` 는 **전역** `client.counters["requests"]` 의 증가분이다.
`after_call` 쪽은 2026-08-04 에 `booked = min(attempts, own_max)` 클램프를 받았는데
(`docs/33` 의 CHART 정원 붕괴 대응), **`sync_rate_limits` 는 그 수정을 못 받았다.**

그리고 이 함수는 `_guarded` 의 `finally` 에서 **모든 루프 몸통마다** 불린다
(`loops.py:1314`) — 그 기본 그룹이 `MARKET_DATA` 다.

### 3.3 결함 D2 — 빼앗긴 쪽은 바닥값으로 **또** 계상한다

```python
booked = min(attempts, own_max) if attempts > 0 else max(1, calls)
booked = max(booked, max(1, calls))        # ← 델타가 0 이어도 최소 1건은 계상
```

D1 이 델타를 먼저 가져가면, 진짜 주인은 `attempts=0` 을 보고도 바닥값 1 을 계상한다.
**같은 송신 1건이 두 그룹에 각각 1건씩, 총 2건으로 잡힌다.**

### 3.4 재현 (결정론적, 라이브 0건)

`test_sync_rate_limits_books_other_groups_calls_into_market_data` 가 `loops.py` 실제 코드로
아래 순서를 그대로 밟는다:

1. tier3 가 MARKET_DATA 1건 송신 → 완료 → `after_call(MARKET_DATA)`
2. 그 코루틴이 DB 를 쓰는 **동안** tier2(CHART)가 3건 송신 (아직 응답 전)
3. tier3 의 `_guarded` 가 끝나며 `finally: sync_rate_limits(MARKET_DATA)` — **3건을 MD 에 얹음**
4. 뒤늦게 CHART 완료 3건 → 바닥값으로 **각각 또 1건씩**

| | 실제 송신 | 예산 계상 |
|---|---|---|
| MARKET_DATA | **1** | **4** |
| MARKET_DATA_CHART | 3 | 3 |
| 합 | **4** | **7** |

MD 는 1건 보내고 4건으로 잡혔다. 총계도 4 → 7 로 부풀었다.

### 3.5 운영 숫자와 맞는가 — 맞는다

- MD 첨두의 **최댓값이 정확히 14**다. 모델의 예측: `MD 구조적 상한 10 + CHART 첨두`.
  `md_peak>10` 인 표본에서 **CHART 첨두 중앙값이 4** → `10+4 = 14`. **상한까지 맞는다.**
- 초과 분포가 한도 바로 위에 몰린다: **11회가 458건**, 12→98, 13→21, 14→3.
  리미터가 진짜 깨졌다면 이렇게 한도에 딱 붙어 있을 이유가 없다.
- **CHART 자신도 자기 한도(5)를 넘어 6 으로 3번 찍혔다** — tier2 루프가
  `_guarded(..., GROUP_CHART)` 로 같은 짓을 하기 때문이다. 대칭적으로 나타난다.
- `budget_unattributed_attempts = 171,905` — `after_call` 쪽 클램프가 버린 몫.
  이 숫자가 이만큼 크다는 것 자체가 **전역 델타 귀속이 상시 오작동**이라는 뜻이다.

---

## 4. 이 계측을 쓰는 곳 — 경보만이 아니다 (요청받은 목록)

`peak_1s` 는 **60초 지평의 최악 1초**를 돌려준다. 그런데 그것을 **지속 속도 목표**
(`target = 10 × 0.85 = 8.5 req/s`)에 대고 잰다. **최댓값을 평균 예산에 비교하는 것**이라
리미터가 완벽해도 트립한다 — 8.5 req/s 로 고르게 보내도 어떤 1초에는 9~10 이 들어간다.

| # | 위치 | 무엇을 결정하나 | 운영 실측 (927 표본) |
|---|---|---|---|
| 1 | `budget.py:431` `_note_over_limit` | ERROR 경보 `over_limit_1s` | 580건 — **전부 허위** |
| 2 | `budget.py:616` `should_grow` | **정원 복원을 막는다** (`peak > 8.5×0.70 = 5.95`) | **93.1%** 의 시간 동안 복원 차단 |
| 3 | `loops.py:2151` tier2 호가 게이트 | 호가 스냅을 통째로 스킵 (`peak ≥ 8.5×0.90 = 7.65`) | **68.8%** 의 시간 동안 닫힘 |
| 4 | `budget.py:463` `predicted_rate` | `max(planned, peak_1s)` — **W4 예산 모델 입력** | 첨두가 계획을 늘 이긴다 |
| 5 | `loops.py:890-895` 텔레메트리 | `md/chart/rank_peak_1s`, `over_limit_1s` | 사람이 읽는 숫자 전부 |
| 6 | `loops.py:654` `HTTP-429-DETAIL` | `own_within_limit` 판정 | `docs/37` §3.5 의 250/250 이 이 값 |

**#2·#3 이 진짜 피해다.** `docs/37` §3.2 가 "`day` 세션 정원이 120인데 중앙값 34,
84%를 천장 아래에서" 를 래칫·429 로 설명했는데, **복원을 막는 게이트가 93% 시간 동안
허위로 닫혀 있었다**는 쪽이 더 단순한 설명이다. `docs/37` §4 의 호가 수율 37%
(정규장 0.4~1.2%)도 #3 이 직접 만든다.

### `docs/37` 의 499건 해석 정정

> `docs/37` §3.5: *"초를 깨는 쪽은 MARKET_DATA 인데 벌은 tier2 가 받는다"*

**앞부분이 틀렸다.** MARKET_DATA 는 초를 깬 적이 없다. `MARKET_DATA 1초에 11~14회 494건`은
MD 가 남의 그룹 송신을 계상한 결과다. 뒷부분(벌은 tier2 가 받는다)은 그대로 유효하다 —
오히려 **가해자가 아예 없었는데 tier2 가 맞고 있었다**는 쪽으로 더 나빠진다.

---

## 5. (다) 는 못 갈랐다 — 그리고 이 질문의 답은 아니다

**못 갈랐다.** 429 응답 자체의 `x-ratelimit-*` 를 우리는 **아직 한 번도 본 적이 없다.**

다만 (다)는 애초에 "왜 `over_limit_1s` 가 11~14 를 세나"의 답이 될 수 없다.
(다)는 *서버가* 우리를 어떻게 세는지에 관한 것이고, *우리 카운터*가 우리 송신보다
큰 수를 뱉는 것은 도착 시각과 무관하다. (다)는 **CHART 429 미해결 건**의 가설로 남는다.

### 0단계로 지시받은 계측 배선 — 고쳤다

`loops.py` 의 `HTTP-429-DETAIL` 이 `client.last_headers`(=**마지막 응답**)를 읽고 있었다.
429 뒤 재시도가 200 으로 성공하면 그 200 의 헤더가 찍힌다. **수정 전 테스트 출력**:

```
HTTP-429-DETAIL group=MARKET_DATA caller=MARKET_DATA attributed=False status=200
  peak1s={...} own_within_limit=True md_plus_chart=0
  headers={'x-ratelimit-limit': '10', 'x-ratelimit-remaining': '9'}
```

2026-08-04 운영 로그와 **같은 모양**이다 — `status=200`, `remaining=9`, 그리고
429 를 맞은 것은 CHART 인데 `group=MARKET_DATA` 로 **귀속까지 틀렸다**.

수정 후: `client.last_429`(429 를 받은 그 자리에서 찍히는 원문)를 읽는다.
`(다)` 를 가를 필드가 이제 로그에 남는다 — `under_own_limit`,
`own_in_server_s`(서버 `date` 초 기준 우리 송신 수, **예산 첨두와 독립**),
`limit_hdr`, `err`, `retry_after_s`, `path`.

**다음 429 부터 관측이 시작된다.** 그전에는 (다)를 확정도 기각도 못 한다.

---

## 6. 대조군 — 경보만 끈 것이 아니다

내 수정은 **로그 한 줄의 데이터 출처**만 바꿨다. 리미터·카운터·게이트는 안 건드렸다.
정당한 스로틀링이 살아 있음을 기존 테스트가 그대로 지킨다 (287 passed / 1 skipped):

| 테스트 | 지키는 것 |
|---|---|
| `test_acquire_throttles_to_configured_rate` | 진짜 과부하에서 **여전히 대기시킨다** |
| `test_concurrent_acquires_do_not_exceed_rate` | 동시 요청이 직렬화된다 |
| `test_on_429_blocks_for_retry_after_and_backs_off` | 실제 429 에 **여전히 백오프한다** |
| `test_repeated_429_backoff_is_exponential_and_capped` | 지수 백오프 유지 |
| `test_never_exceeds_declared_limit_in_any_one_second_window` | 1초 창 계약 유지 |

---

## 7. 남은 일 — **내 소유가 아니다**

원인은 `tossmon/collector/budget.py` 와 `loops.py` 의 계상 경로에 있다. 둘 다 **W4 소유**이고,
이번 태스크가 나에게 연 예외는 `last_429` 배선 **한 곳뿐**이라 고치지 않았다.

W4 가 받아야 할 것 (우선순위 순):

1. **D1**: `sync_rate_limits` 의 `on_requests(group, _unaccounted_attempts())` 에
   `after_call` 과 **같은 클램프**를 넣거나, 아예 빼라. 이 경로는 귀속 근거가 없다.
2. **D2**: `booked = max(booked, max(1, calls))` 바닥값이 D1 과 겹쳐 이중 계상을 만든다.
3. **근본**: 계상 시각을 **완료 시각이 아니라 송신 시각**으로. 리미터가 이미 진짜 송신
   시각을 `_Bucket.sent` 에 갖고 있다(`snapshot()["window_used"]`). 예산이 그것을 읽으면
   D1·D2·완료시각 문제가 **한꺼번에** 사라진다 — 그게 원인을 설명하는 수정이다.
4. **#2·#3 게이트**: 60초 최악값을 지속 속도 목표에 비교하는 것 자체를 재검토.
   고쳐도 `peak_1s` 를 쓰는 한 정원 복원은 계속 막힌다.

**폴 주기(4초→2초) 결정의 전제도 다시 봐야 한다**: `docs/43` §9 의 여유 4% 는
`predicted_rate = max(planned, peak_1s)`(#4)를 타므로 **부풀려진 첨두를 물려받았을 수 있다.**
D1·D2 를 고치기 전 숫자로 결정하지 말 것.

---

## 8. 못 밝힌 것

- **(다)** — 429 원문 헤더 관측은 배선만 됐고 아직 데이터가 없다.
- **다중 프로세스 배제** — 관측과 모순이라 배제했을 뿐, 데이터로 직접 확인하지 않았다.
- **D1/D2 가 580건 **전부**를 만들었는지의 정량 배분** — 기여는 구조적으로 확정했고
  상한(14 = 10+4)까지 맞지만, 완료시각 몰림(§3.1)이 몇 %를 보탰는지는 못 나눴다.
  나누려면 송신 시각 계상(§7-3)을 켠 뒤 같은 창을 다시 재야 한다.
- **`tokens.py` 의 리미터 밖 토큰 발급** — MD 첨두와 무관해서 이번엔 안 팠다.

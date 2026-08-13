# 56. 서버가 한도를 올렸는데 우리가 잘랐고, 그 사실이 어느 줄에도 안 남았다

작성: W1, 2026-08-13
질문: 클램프 규칙(감사 B-2)은 옳은데 **왜 이틀 반 동안 아무도 몰랐나**, 그리고 무엇을 남기면
다음번엔 알 수 있나.

**이건 가시성 태스크다. 천장은 올리지 않았고 송신률은 한 건도 안 바뀌었다.**

---

## 0. 측정 조건 (먼저 읽을 것)

| 항목 | 값 |
|---|---|
| 코드 | `feat/core-api` @ `aecacd5` |
| **라이브 API 호출** | **0건** — 헤더를 합성해서 리미터에 먹였다 |
| 수집기 | **건드리지 않았다.** 라이브 그대로. `STOP`/`PLANNED` 없음 |
| 관측 사실 출처 | W4 실측, `docs/55` §2.2 (서버가 08-11 12:51 부터 `/api/v1/candles` 에 20/s) |
| 값 변경 | `SPEC_LIMITS`·`config.example.yaml` **변경 없음** |
| 테스트 | `tests/test_api_limit_clamp_visibility.py` (신규 12건), 회귀 전체 1,448 passed |

---

## 1. 결론

> ### 규칙은 옳았다. **규칙이 작동했다는 사실을 아무 데도 안 적은 것**이 결함이었다.

카운터 `counters["limit_header_clamped"]` 는 **원래 있었다** (감사 B-2 가 넣었다).
문제는 그 값을 **읽는 코드가 한 줄도 없었다**는 것이다 — 텔레메트리에도, 로그에도,
워치독에도 없었다. 관측치를 만들어 놓고 배선하지 않으면 없는 것과 같다.

| 무엇 | 수정 전 | 수정 후 |
|---|---|---|
| 위로 잘린 것 vs 아래로 따라간 것 | 하나만 셌다 (위로만) | `limit_header_clamped` / `limit_header_lowered` 로 갈랐다 |
| 어느 그룹이 잘렸나 | `last_clamped` 에 **마지막 1건만** | `clamped[group]` 그룹별 누적 |
| 얼마나 잘렸나 | 마지막 값만 | `header_max > ceiling` (서버 최대값 대 천장) |
| 텔레메트리 | **없음** | `limit_header_clamped*` 3개 항목 |
| 로그 | **없음** | 상태 전이마다 `LIMIT-CLAMP start/end` 한 줄 |
| **송신률** | 4.25 req/s | **4.25 req/s (불변)** |

---

## 2. 고치기 전 — 빨강

서버가 `MARKET_DATA_CHART`(= `/api/v1/candles`) 에 `X-RateLimit-Limit: 20` 을 주는 응답
5건을 합성해서 먹였다. `aecacd5` 코드 그대로의 출력:

```
[상황] 서버가 MARKET_DATA_CHART 에 X-RateLimit-Limit: 20 을 준다. 천장은 5.0.

[1] 송신률: rate 4.25 -> 4.25 (서버가 허용한 20/s 중 4.25/s 만 쓴다)
    window_cap 5.0 -> 5.0, limits[MARKET_DATA_CHART] = 5.0

[2] 클램프가 일어나는 동안 나온 로그 줄:
    (없음)

[3] 수집기 텔레메트리 줄에서 clamp 관련 항목:
    {'rankings_clamped': 0}                    ← 이름만 비슷한 남의 항목이다

[4] limiter 내부 카운터 (아무도 읽지 않는 자리):
    counters      = {'limit_header_clamped': 5}
    last_clamped  = {'group': 'MARKET_DATA_CHART', 'header': 20.0, 'ceiling': 5.0}
```

**[4] 가 [2]·[3] 으로 나오지 않는다** — 이게 이틀 반의 원인 전부다.
값은 메모리에 있었고, 사람이 볼 수 있는 어떤 표면에도 닿지 않았다.

---

## 3. 무엇을 바꿨나

### 3.1 방향을 갈랐다 (`limiter.py`)

이게 핵심이다. 두 사건은 뜻이 정반대라 **같은 카운터로 세면 안 된다**:

| 카운터 | 조건 | 뜻 | 보고 대상? |
|---|---|---|---|
| `limit_header_clamped` | `header > ceiling` | 서버가 더 주는데 우리가 잘랐다 | **★ 그렇다** (기회 손실) |
| `limit_header_lowered` | `header < ceiling` | 서버가 낮게 줘서 따라 내려갔다 | 아니다 (정상 경로) |
| (계상 없음) | `header == ceiling` | 평시 | 아니다 |

정상 하향은 평상시에 계속 일어난다. 둘을 합쳐 세면 평시 값이 커져서, 진짜 사건인
"서버가 20 을 주는데 우리가 5 로 자른다"가 **그 숫자 안에 묻힌다** — 이번에 실제로
숨은 방식이 그거다. 그래서 갈랐다.

`limit_header_lowered` 를 굳이 남기는 이유는 하나뿐이다: **둘 다 0** 이면 "클램프가
없다"가 아니라 "헤더를 아예 못 읽고 있다"일 수 있는데, 카운터가 하나면 그 둘을 구분할
방법이 없다.

### 3.2 그룹별 + 잘린 크기 (`limiter.py`)

```python
self.clamped: dict[str, dict] = {}   # group -> count / active / ceiling / header / header_max
```

어느 그룹이 잘리는지 모르면 쓸모가 없다 — `MARKET_DATA_CHART` 가 잘리는 것과 `AUTH` 가
잘리는 것은 대응이 완전히 다르다. `header_max` 를 따로 두는 이유는 "5 로 잘렸다"와
"20 이 5 로 잘렸다"가 다른 사실이기 때문이다(전자는 무시해도 되고 후자는 4배다).

### 3.3 텔레메트리 3개 항목 (`limiter.clamp_report()` → `loops.telemetry()`)

실제 출력 (`limit=20` 응답 1,200건 + `MARKET_DATA` 정상 하향 400건을 먹인 뒤):

```
telemetry session=regular config_sig=... rankings_clamped=0 ranking_snap_age_s=-1 ...
  limit_header_clamped=1200 limit_header_clamped_groups=MARKET_DATA_CHART:20>5x1200
  limit_header_lowered=400 precision_rounded=0 ...
  | budget MARKET_DATA_CHART=peak0/p95:0/avg0.00/tgt4.25
```

`limit_header_clamped_groups` 형식은 `그룹:서버최대>천장x횟수`. 한 줄에 그룹·크기·빈도가
다 들어간다. 잘린 적 없으면 `-`. 값에 공백이 없어야 `k=v` 파싱이 안 깨지므로 테스트로
고정했다.

limiter 를 읽지 못하는 경우(구식 클라이언트 주입 등)에는 **키를 지우지 않고**
`-1` / `n/a` 로 적는다. 관측치가 조용히 사라지는 것이 이 태스크의 원인이라, 없어지는 것보다
틀린 값이 낫다.

### 3.4 상태가 바뀔 때만 한 줄 (`limiter.on_clamp_change` → `loops._wire_clamp_log`)

카운터만 있으면 누가 세어보기 전엔 모른다. 그래서 그룹마다 **안 잘림 ↔ 잘림** 전이에서만
한 줄:

```
2026-08-13 ... WARNING LIMIT-CLAMP start group=MARKET_DATA_CHART server=20.0 ceiling=5.0
               — 서버가 천장보다 높은 한도를 주는데 우리는 천장으로 자르고 있다
                 (송신률은 그대로, 판단은 사람이)
2026-08-13 ... INFO    LIMIT-CLAMP end group=MARKET_DATA_CHART ceiling=5.0 clamped_total=1200
```

**매 응답마다 찍지 않는다.** 위 실측에서 응답 1,200건 → 로그 1줄이다. 이 프로젝트는 이미
한 번 당했다: 티어 전이를 매건 INFO 로 찍어서 최근 1,000줄 중 978줄이 티어 줄이 됐고
워치독의 텔레메트리 탐지가 무력화됐다(`notifier.promotion` 주석). 소음이 되는 순간 이
관측도 똑같이 묻힌다.

시작이 `warn`, 끝이 `info` 인 이유: 시작은 사람이 판단해야 하는 사건이다 — 사양이 진짜로
올랐거나, 헤더 의미가 초당→분당으로 바뀌었거나 둘 중 하나이고 **자동으로 따라 올라가면
안 되는** 자리다(그게 감사 B-2 다). 끝은 정상 복귀다.

---

## 4. 왜 콜백인가 (`logging` 을 안 쓴 이유)

`tossmon/api/tokens.py` 가 `logging.getLogger(__name__)` 을 쓰고 있어서 limiter 도 그렇게
할 수 있었다. **안 했다.** 확인한 사실:

- `Notifier` 는 `tossmon.collector` 로거에만 핸들러를 달고 `propagate = False` 로 끊는다
  (`notifier.py:48-69`).
- 컬렉터 어디에도 root 로거 설정(`basicConfig` / root `addHandler`)이 **없다**
  (`grep -rn "basicConfig|addHandler|getLogger()" tossmon/` → notifier 와 universe/\_\_main\_\_ 뿐).

즉 `tossmon.api.limiter` 로 찍은 INFO 는 **`data/collector.log` 에 안 남는다**. 사람이 보는
파일에 안 닿는 로그를 추가하는 건 이번 결함을 그대로 반복하는 것이다. 그래서 limiter 는
훅만 노출하고(`on_clamp_change`), 컬렉터가 자기 `notifier` 로 잇는다.

콜백이 예외를 던져도 요청 경로는 안 죽는다 — 삼키되 `counters["clamp_log_failures"]` 로
센다(삼킨 사실까지 조용해지면 안 된다). 테스트로 고정했다.

---

## 5. 대조군 — 정상 하향에서 보고 대상은 0

이걸 안 하면 정상 동작이 경보로 바뀐다. 서버가 천장(5)보다 낮은 `3` 을 주는 응답 10건:

```
[대조군] 서버가 3/s 를 준다 (천장보다 낮다 = 정상 하향)
    rate          = 2.55 (내려간 것이 맞다 — 규칙은 안 바꿨다. 3 × 0.85)
    counters      = {'limit_header_clamped': 0, 'limit_header_lowered': 10, 'clamp_log_failures': 0}
    per-group     = {'MARKET_DATA_CHART': {'count': 0, 'active': False, 'header_max': 0.0, ...}}
    로그 줄        = (없음)
```

고정한 테스트: `test_normal_downward_header_is_not_reported_as_a_clamp`
(보고 카운터 0 / 그룹 문자열 `-` / 로그 0줄 / 하향 채택은 그대로).

---

## 6. 송신률 불변 — 이 태스크의 실패 조건

가시성만이다. 한 건이라도 달라지면 실패다. 두 방향으로 고정했다.

**금값(golden):** 천장 5, `usage_ratio` 0.85 → `rate` 4.25, `window_cap` 5,
`sustained_rate` 4.25, `limits[CHART]` 5.0. `limit=20` 헤더 **50건**을 먹인 뒤에도 전부 동일.
(`test_clamp_visibility_does_not_move_the_rate_by_one_call`)

**실제 송신 속도:** 버스트를 소진하고 `limit=20` 을 먹인 뒤 5회 획득에 걸린 시간이
0.8초 이상 — 헤더의 20/s 가 새어 들어왔다면 즉시 통과했을 것이다.
(`test_a_clamped_group_still_sends_at_the_ceiling_rate`)

---

## 7. 건드린 파일

| 파일 | 무엇 | 왜 |
|---|---|---|
| `tossmon/api/limiter.py` | 카운터 분리 + `clamped` 그룹별 상태 + `clamp_report()` + `on_clamp_change` 훅 | 소유 범위. 관측 로직 본체 |
| `tossmon/collector/loops.py` | `telemetry()` 에 `**_limiter_clamp(self.client)` 1줄, `create()` 에 `_wire_clamp_log(ctx)` 1줄, 헬퍼 2개 | **배선 최소.** 텔레메트리 줄과 `notifier` 는 여기밖에 없다 |
| `tests/test_api_limit_clamp_visibility.py` | 신규 12건 | 빨강/초록·대조군·송신률 불변 |
| `docs/56_limit_clamp_visibility.md` | 이 문서 | — |

`budget.py`(W4 가 방금 고쳤다)는 **건드리지 않았다**. `SPEC_LIMITS`·`config.example.yaml`
값도 그대로다.

---

## 8. 남은 것 (이 태스크 밖)

- **천장을 올릴지는 판단이 필요하다.** 이 문서는 "서버가 20 을 준다"는 사실을 보이게만
  했다. 그게 (a) 진짜 사양 상향인지 (b) 헤더 의미 변경(분당 쿼터)인지 **아직 안 갈랐다** —
  20/s 를 실제로 시도해 429 가 나는지 보는 것 말고는 가르는 방법이 없고, 그건 라이브
  실험이라 이 태스크 범위 밖이다.
- 워치독(`w5-ops`)이 `limit_header_clamped > 0` 을 판독하게 할지는 W5 소유다.
  지금은 텔레메트리 줄에 값이 있으므로 읽을 수는 있다.

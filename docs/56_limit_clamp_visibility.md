# 56. 서버가 한도를 올렸는데 우리가 잘랐고, 그 사실이 어느 줄에도 안 남았다

> **[현행]** 소유 W1 · 2026-08-13 · ~~★ **텔레메트리 어긋남이 열려 있다** — 로그는 뜨는데 `limit_header_clamped=0`~~
> **정정 (같은 날, §9)**: 어긋난 것이 아니었다 — 그 `0` 은 클램프보다 **3.9 초 앞선 기동
> 스냅샷**(`proc_uptime_s=1`)이다. 다만 그 옆의 `counter_scope` 가 이 값을 **설치 수명**
> 이라고 잘못 선언하고 있었고(실제로는 프로세스 수명), 그게 `0` 을 오독하게 만든 진짜
> 결함이다. **고쳤다.** 판정 근거·배제한 가설·빨강→초록은 **§9**.
> 상태 표기의 뜻과 전수 목록: [`docs/INDEX.md`](INDEX.md)

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
  ~~지금은 텔레메트리 줄에 값이 있으므로 읽을 수는 있다.~~
  **정정 (2026-08-13, §9)**: 값이 줄에 있는 것은 맞지만 **그냥 `> 0` 으로 읽으면 안 된다.**
  이 값은 **프로세스 수명**이라 재기동마다 0 으로 돌아간다. `proc_uptime_s` 를 같이 읽어라
  — 실제로 그것을 안 읽어서 이 값이 "고장난 계측기" 로 보고됐다. 자세한 것은 §9.

---

## 9. 텔레메트리가 로그와 다른 값을 말한다 — 가설 넷 중 (가) 였고, 진짜 거짓말은 그 옆에 있었다

작성: W1, 2026-08-13 (같은 문서에 이어 씀)
질문: `LIMIT-CLAMP` 로그는 뜨는데 텔레메트리는 `limit_header_clamped=0` 이다.
**계측이 고장났나, 읽는 방법이 틀렸나.**

## 9.0 측정 조건 (먼저 읽을 것)

| 항목 | 값 |
|---|---|
| 코드 | `feat/core-api` @ `0c769e1` (`main` 에 fast-forward, 82 커밋 따라잡음) |
| **라이브 API 호출** | **0건** — 판정에 필요 없었다. 관측은 전부 기존 로그 원문이다 |
| 관측 원본 | `w5-ops/data/collector.log` — **읽기 전용.** 수집기가 지금도 쓰고 있어 파일이 자란다. 아래 수치는 전부 **2026-08-13 14:24:49 KST 판(247,645 줄)** 기준이다 |
| 수집기 | **건드리지 않았다.** `STOP`/`PLANNED` 없음. 재시작·설정 변경 없음 |
| 천장·송신률 | `SPEC_LIMITS`·`config` **변경 없음.** 이 절도 가시성만이다 |
| 테스트 | `tests/test_api_limit_clamp_visibility.py` **신규 4건**, 회귀 전체 2,065 passed |

## 9.1 무엇이 관측됐나

`coordination/COORDINATOR-STATE.md` §2:

> `LIMIT-CLAMP` 로그는 뜨는데 **같은 시각** 텔레메트리는 `limit_header_clamped=0`,
> `groups=-` 이다. 텔레메트리가 로그보다 먼저 찍혔을 가능성 — **미규명**.

## 9.2 가설 넷과 **각각을 반증할 관측**

먼저 세운 뒤에 봤다. 순서를 바꾸면 보고 싶은 것만 보게 된다.

| # | 가설 | 참이라면 무엇이 보여야 하나 (= 반증 관측) | 판정 |
|---|---|---|---|
| **(가)** | 텔레메트리 스냅샷 시점이 카운터 증가보다 **앞선다** | `0` 인 줄이 클램프 로그 줄보다 **먼저** 찍혀 있고, 그 뒤의 줄은 전부 0 이 아니다. 뒤에 찍힌 0 이 하나라도 있으면 **기각** | **확정** (§9.3) |
| **(나)** | 카운터를 올리는 코드 경로와 로그를 찍는 경로가 **다르다** | 응답과 텔레메트리를 촘촘히 번갈아 돌리면 "로그는 나갔는데 카운터 0" 인 스냅샷이 잡힌다 | **기각** (§9.4) |
| **(다)** | 방향(`clamped`/`lowered`)을 한 통에 세다가 **상쇄된다** | `clamped` 와 `lowered` 가 같은 카운터를 쓰거나, `lowered` 가 커서 사건이 묻힌다 | **기각** (§9.4) |
| **(라)** | 프로세스/스레드 경계에서 **카운터 인스턴스가 다르다** | 같은 로그 파일에 다른 프로세스가 쓰거나, 로그를 잇는 limiter 와 텔레메트리가 읽는 limiter 가 다른 객체다 | **기각** (§9.4) |

## 9.3 (가) 확정 — 로그 원문이 그대로 말한다

`limit_header_clamped=` 를 실은 텔레메트리 줄은 **27 개**(14:24:49 판)이고,
`LIMIT-CLAMP` 줄은 **2 개**다. 시간순으로 늘어놓으면 이렇다:

```
line 247358  2026-08-13 12:11:00,931  proc_uptime_s=1     limit_header_clamped=0      groups=-
line 247359  2026-08-13 12:11:04,846  WARNING LIMIT-CLAMP start group=MARKET_DATA       server=15.0 ceiling=10.0
line 247360  2026-08-13 12:11:05,027  WARNING LIMIT-CLAMP start group=MARKET_DATA_CHART server=20.0 ceiling=5.0
line 247387  2026-08-13 12:16:01,745  proc_uptime_s=302   limit_header_clamped=887    groups=MARKET_DATA:15>10x718,MARKET_DATA_CHART:20>5x169
line 247399  2026-08-13 12:21:02,116  proc_uptime_s=602   limit_header_clamped=1753   groups=MARKET_DATA:15>10x1441,MARKET_DATA_CHART:20>5x312
   ... (같은 모양으로 단조 증가, 24 줄 더) ...
line 247645  2026-08-13 14:21:39       proc_uptime_s=7840  limit_header_clamped=23050  groups=MARKET_DATA:15>10x19085,MARKET_DATA_CHART:20>5x3965
```

**세 가지가 한꺼번에 나온다.**

1. **`0` 인 줄은 파일 전체에 딱 하나**고(line 247358), 그 줄은 클램프 로그보다
   **3.9 초 앞선다.**
2. 그 줄의 `proc_uptime_s=1` — **프로세스가 1 초 된 시점**이다. 아직 응답을 한 건도
   안 받았다. 0 이 아니면 그게 이상한 것이다.
3. **클램프 로그 줄 뒤에 찍힌 텔레메트리 26 줄 중 `0` 은 0 개다.** 값도 그룹 문자열도
   로그와 정확히 맞는다. (같은 판에서 `limit_header_lowered != 0` 인 줄도 **0 개** — §9.4 의 (다).)

> ### 판정: 계측기는 로그와 어긋나지 않았다. **같은 분(minute)에 찍힌 두 줄을 "같은 시각" 으로 읽은 것**이다.
>
> `12:11:00` 과 `12:11:04` 는 같은 분이지만 그 사이에 **수집기의 첫 클램프 응답**이 있다.
> 반증 관측("뒤에 찍힌 0 이 하나라도 있으면 기각")을 걸었고, 그런 줄은 **0 개**였다.

고정한 테스트: `test_startup_zero_precedes_the_clamp_log_it_does_not_contradict_it`
(라이브의 순서를 그대로 재현한다 — 기동 직후 0 → 첫 클램프 → 로그 줄 → 비-0).

## 9.4 나머지 셋을 무엇으로 배제했나

**(나) 경로가 다르다 — 기각.** `limiter.py:230-242` 에서 계상(`counters += 1`,
`st["count"] += 1`)이 콜백(`_emit_clamp_change`)보다 **먼저**이고 그 사이에 `await` 가
없다. 단일 이벤트루프이므로 다른 코루틴이 그 틈에 끼어들 수 없다 — 즉 *"로그는 나갔는데
카운터는 0"* 인 스냅샷은 **한 프로세스 안에서 존재할 수 없다.**
직접 시험했다: 두 그룹 응답과 `ctx.telemetry()` 를 12 회 번갈아 돌려 로그 줄이 나간
뒤의 모든 스냅샷이 `> 0` 임을 확인 (`test_telemetry_is_never_zero_once_the_clamp_line_has_fired`).
라이브 26 줄도 같은 말을 한다.

**(다) 방향이 상쇄된다 — 기각.** 카운터는 위 §3.1 에서 이미 갈랐고, 라이브 27 줄
**전부** `limit_header_lowered=0` 이다. 상쇄될 값 자체가 없다. (덧붙여 `clamped>0` 이고
`lowered=0` 이라는 조합은 §3.1 이 노린 교차 확인이 작동했다는 뜻이다 — 헤더를 못 읽는
상태라면 **둘 다** 0 이어야 한다.)

**(라) 인스턴스가 다르다 — 기각.** 세 가지로 갈랐다.
- `_wire_clamp_log(ctx)` 는 `ctx.client.limiter` 에 훅을 걸고(`loops.py:1399`),
  `_limiter_clamp(self.client)` 는 **같은** `ctx.client.limiter` 를 읽는다(`loops.py:1381`).
  객체가 하나다.
- 같은 파일에 쓰는 다른 프로세스가 없다: `GroupRateLimiter` 를 만드는 자리는 4 곳이고
  (`collector/__main__.py:33`, `universe/__main__.py:55`, `tools/backfill.py:892`,
  `tools/live_probe.py:2184`) **`collector.log` 에 쓰는 것은 수집기뿐**이다 —
  universe 는 `universe_build.log` 로 간다(`universe/__main__.py:52`).
- 값 자체가 증거다. 26 줄의 `groups` 가 로그 두 줄의 `group`·`server`·`ceiling` 과
  **정확히** 일치한다(`MARKET_DATA:15>10`, `MARKET_DATA_CHART:20>5`). 다른 인스턴스라면
  이 일치가 나올 수 없다.

## 9.5 그런데 계측기는 결백한가 — 아니다. **바로 옆 필드가 거짓말하고 있었다**

(가)가 확정됐다고 여기서 끝내면 *"코디네이터가 잘못 읽었다"* 가 되는데, 그건 이 프로젝트가
같은 실수를 다시 하게 두는 결론이다. **왜 그렇게 읽혔는지**를 봐야 한다.

같은 텔레메트리 줄에는 `counter_scope` 필드가 있다. `docs/52` §7 이 만든 것이고,
만든 이유가 정확히 이것이다 — **"에포크 없는 0 은 아무 뜻도 없다."**

```
counter_scope=proc:http_429,over_limit_1s,precision_parsed,precision_rounded,quota_not_ours,srv_s_unknown;rest:install
```

뜻: 여기 적힌 것은 **프로세스 수명**(재시작마다 0), **나머지는 전부 설치 수명**(상태파일로
누적). 그런데 `limit_header_clamped` 는 이 목록에 **없다.** 즉 이 줄은 클램프 값이
설치 이래 누계라고 **말하고 있었다.**

**사실은 정반대다.** 이 값들은 `GroupRateLimiter.counters` 안에 있고 limiter 는 기동마다
새로 만들어진다(`collector/__main__.py:33`). `collector_state.json` 에 안 들어간다.
**재시작마다 0 이다.**

> ### 이 오선언이 만드는 것이 바로 이번 증상이다
>
> `LIMIT-CLAMP start` 줄은 `collector.log` 에 **영구히** 남고, 카운터는 프로세스와 함께
> 죽는다. 그래서 **재기동 직후에는 "로그에는 start 가 있는데 텔레메트리는 0" 이 실제로
> 나온다.** 이건 모순이 아니라 **분모가 다른 두 값**이다 —
> `docs/52` §7.1 의 `http_429_under_own_limit=431 > http_429=178` 과 **같은 모양**이다.
>
> 그리고 `counter_scope` 가 설치 수명이라고 말하는 한, 읽는 사람은 그 `0` 을
> *"설치 이래 한 번도 안 잘렸다"* 로 읽을 수밖에 없다. 그러면 로그와 어긋난 것처럼 보인다.

**고친 것 (최소 diff, `loops.py` 1 곳):** 세 필드를 `PROC_SCOPED_COUNTERS` 에 넣었다.

```
counter_scope=proc:...,srv_s_unknown,limit_header_clamped,limit_header_lowered,limit_header_clamped_groups;rest:install
```

`limit_header_clamped_groups` 는 카운터가 아니라 문자열이지만 **같은 수명이고 같은 오독을
만든다**(`-`). 그래서 같이 선언했다. 값에 공백이 없으므로 `k=v` 파싱은 그대로다.

### 왜 "수명을 통일" 하지 않았나

`docs/52` §7.2 가 이미 판단한 자리라 다시 열지 않았다: 아래로 통일하면 설치 수명 기록이
사라지고, 위로 통일하면 수집기가 남의 객체 내부 상태를 자기 상태파일에 쓰게 된다.
**표시가 한 필드 값이고 잃는 것이 없다.** 이번 건은 그 판단이 옳았음을 확인해 준다 —
필요한 것은 저장이 아니라 **에포크를 함께 적는 것**이었다.

### 이걸 막았어야 할 테스트가 왜 안 죽었나 — **한 방향만 보고 있었다**

`loops.py` 주석은 *"목록이 드리프트하면 그 테스트가 먼저 죽는다"* 라고 적고 있었다.
`test_telemetry_declares_which_counters_reset_on_restart` 를 열어 보면 **손으로 적은
이름 4 개**(`watched`)만 재고 `reset == watched & declared` 를 단언한다. 이건
*"선언한 것이 정말 리셋되는가"* 만 본다. **"리셋되는 것이 전부 선언됐는가" 는 안 본다.**

이번 드리프트는 정확히 그 안 보는 쪽으로 났다 — `docs/56`(08-13)이 새 필드 3 개를
줄에 실으면서 선언에 등록하지 않았다. **테스트를 안 만든 게 아니라, 만든 테스트가 한
방향짜리였다.** 반대 방향은 새 테스트가 막는다(§9.6 의 4 번째).

## 9.6 빨강 → 초록

수정 **전** (`0c769e1`, `PROC_SCOPED_COUNTERS` 손대기 전):

```
$ .venv/Scripts/python.exe -m pytest tests/test_api_limit_clamp_visibility.py -q
E   AssertionError: `limit_header_clamped` 는 재시작으로 0 이 되는데 `counter_scope` 는
    설치 수명이라고 말한다 — 그러면 0 이 '안 잘렸다' 인지 '방금 떴다' 인지 구분할 수 없다.
    선언: ['http_429', 'over_limit_1s', 'precision_parsed', 'precision_rounded',
           'quota_not_ours', 'srv_s_unknown']
E   AssertionError: limiter 가 싣는데 프로세스 수명으로 선언 안 된 항목:
    ['limit_header_clamped', 'limit_header_clamped_groups', 'limit_header_lowered']
2 failed, 14 passed in 3.97s
```

수정 **후**:

```
$ .venv/Scripts/python.exe -m pytest tests/test_api_limit_clamp_visibility.py tests/test_send_time_accounting.py -q
32 passed in 6.40s

$ .venv/Scripts/python.exe -m pytest -q
2065 passed, 1 skipped, 4 deselected, 24 warnings in 277.94s (0:04:37)
```

(직전 기준은 2,061 passed — 신규 4 건이 더해진 수다.)

| 신규 테스트 | 무엇을 막나 | 수정 전 |
|---|---|---|
| `test_startup_zero_precedes_the_clamp_log_it_does_not_contradict_it` | (가) 를 코드로 고정 — 기동 0 → 로그 → 비-0 순서 | 초록 (증상 재현이 아니라 **가설 확정**용) |
| `test_telemetry_is_never_zero_once_the_clamp_line_has_fired` | (나)·(다) 재발 — 로그 뒤 스냅샷이 0 이면 죽는다 | 초록 (배제 근거를 고정) |
| `test_clamp_counters_reset_on_restart_and_the_line_says_so` | 재시작으로 0 이 되는데 줄이 그 사실을 말하지 않는 것 | **빨강** |
| `test_every_limiter_sourced_field_is_declared_process_scoped` | limiter 가 싣는 항목이 하나 더 늘 때 같은 거짓말이 반복되는 것 | **빨강** |

앞의 둘이 처음부터 초록인 것을 숨기지 않는다 — **어긋남은 코드에 없었기 때문에 빨강으로
만들 수 없다.** 그 둘은 "지금 안 어긋난다"를 못 박는 회귀 방지이고, 빨강→초록은 §9.5 의
오선언 쪽이다.

**§9 에서 건드린 파일 — 셋뿐이다.**

| 파일 | 무엇 | 왜 |
|---|---|---|
| `tossmon/collector/loops.py` | `PROC_SCOPED_COUNTERS` 에 클램프 3 필드 추가 + 위 주석에 기존 테스트의 한계 한 줄 | **동작 코드 0 줄.** 이 튜플은 텔레메트리 문자열에만 들어간다 |
| `tests/test_api_limit_clamp_visibility.py` | 신규 4 건 + 재시작 헬퍼 `_restart` | 가설 확정·배제를 코드로 고정 |
| `docs/56_limit_clamp_visibility.md` | 이 §9 + §8·머리말 정정 병기 | — |

`tossmon/api/limiter.py` 는 **건드리지 않았다** — 거기엔 결함이 없었다.
`SPEC_LIMITS`·`config` 도 그대로다.

## 9.7 덤으로 나온 라이브 사실 — **`MARKET_DATA` 도 잘리고 있다 (15 → 10)**

`docs/56` 본문(§2~§6)은 `MARKET_DATA_CHART` **20 → 5** 하나만 알고 썼다. 로그 원문에는
그룹이 **둘**이다:

| 그룹 | 서버가 주는 값 | 우리 천장 | 12:11 기동 ~ 14:21:39 (2 시간 11 분) 계상 |
|---|---|---|---|
| `MARKET_DATA` | **15.0** | 10.0 | **19,085 건** |
| `MARKET_DATA_CHART` | 20.0 | 5.0 | 3,965 건 |

**아무 조치도 하지 않았다** — 천장을 올리는 것은 `COORDINATOR-STATE` §3-1 에서 이미
기각됐고(*"두 번째 서버 리미터의 정체를 모르는데 천장을 4 배로 올리면 그것을 더 세게
때린다"*), 이 절은 가시성 태스크다. 다만 §8 의 열린 질문 *"서버 20/s 가 진짜 사양
상향인지 헤더 의미 변경인지"* 에 **재료가 하나 늘었다.**

헤더 의미가 **초당→분당 쿼터**로 바뀐 것이라면 값은 공시 한도의 **60 배**여야 한다 —
`MARKET_DATA` 는 600, `MARKET_DATA_CHART` 는 300. 실제로 온 값은 **15 와 20** 이고,
배수도 서로 다르다(1.5 배 / 4.0 배). **분당 쿼터 가설은 이 관측과 맞지 않는다.**

**확정은 아니다.** 서버가 그룹별로 그냥 다른 값을 주는 것일 수도 있고, 우리가 못 보는
세 번째 의미일 수도 있다. §8 이 적은 대로 **20/s 를 실제로 시도해 429 가 나는지 보는
것 말고는 가르는 방법이 없고, 그건 라이브 실험이라 이 태스크 범위 밖이다.**

## 9.8 `docs/58` G-0 의 **0-3** 판정

> 0-3 | 텔레메트리와 로그가 어긋나지 않는다 | **미충족** — `LIMIT-CLAMP` 로그는 뜨는데
> 텔레메트리는 `limit_header_clamped=0`

**충족으로 바꿀 근거가 나왔다.**

1. 관측된 어긋남은 **어긋남이 아니었다** — 3.9 초 앞선 기동 스냅샷이다 (§9.3, 라이브
   텔레메트리 27 줄 + 로그 2 줄 전수 대조. **클램프 로그 뒤의 0 은 0 개**).
2. 한 프로세스 안에서는 **구조적으로 어긋날 수 없다** — 계상과 콜백 사이에 `await` 가
   없다 (§9.4, 테스트로 고정).
3. 재시작 경계에서는 `0` 과 로그 줄이 공존할 수 있는데, **이제 줄이 그 사실을 말한다** —
   `counter_scope` 에 세 필드가 선언됐고 `proc_uptime_s` 가 같은 줄에 있다 (§9.5).

**판정은 코디네이터 몫이다.** 다만 **이것만은 명확히 적는다: 위 셋은 `limit_header_*`
세 필드에 대한 것이지, 텔레메트리 줄 전체에 대한 것이 아니다.** 같은 줄의 다른 값이
로그와 어긋나는지는 이 태스크가 재지 않았다.

## 9.9 안 고친 것 · 남은 것

- **`tests/test_send_time_accounting.py` 의 한 방향 테스트는 그대로 뒀다** (W4 소유).
  `loops.py` 주석에 한 줄로 그 한계를 적고, 반대 방향은 내 테스트 파일에서 막았다.
  **`ctx`·`client`·`budget` 쪽 필드에는 같은 전수 대조가 아직 없다** — 거기서 같은
  드리프트가 나면 여전히 조용하다. W4 안건으로 남긴다.
- **`docs/INDEX.md` 의 `56` 행이 아직 *"텔레메트리 어긋남 열려 있음"*, `COORDINATOR-STATE`
  §2 에도 같은 행이 있다.** 둘 다 코디네이터 소유라 안 고쳤다.
- **`docs/52` §7.2 의 예시 줄**(`counter_scope=proc:http_429,...,precision_rounded;rest:install`)
  은 그날 찍힌 출력이라 지금 값과 다르다. 사양이 아니라 기록이라 안 고쳤다.
- **`MARKET_DATA` 15/s 의 정체** — §9.7. 분당 쿼터 가설과는 안 맞지만 확정은 못 했다.
- **송신률은 한 건도 안 바뀌었다.** 이 절의 코드 변경은 `PROC_SCOPED_COUNTERS` 튜플
  하나뿐이고, 그 값은 텔레메트리 문자열에만 들어간다.

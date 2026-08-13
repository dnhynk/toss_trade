# W1 — 클램프 가시성: 서버가 20 을 주는데 우리가 5 로 자르는 것을 보이게 만든다

테스트: `1,448 passed, 2 deselected` (신규 12건) · 근거 원본: `docs/56_limit_clamp_visibility.md`
**라이브 API 호출 0건 · 수집기 미접촉 · 배포 안 함 · `SPEC_LIMITS`·`config` 값 변경 없음**

---

## 0. 요약 — 3줄

1. **카운터는 원래 있었다.** `counters["limit_header_clamped"]` 는 감사 B-2 가 넣었고 정확히
   동작하고 있었다. **읽는 코드가 한 줄도 없었던 것**이 이틀 반의 원인 전부다.
2. 그래서 (a) 방향을 갈라 세고(`clamped` vs `lowered`), (b) 그룹·크기와 함께 **텔레메트리
   줄에 싣고**, (c) 상태 전이에서만 **로그 한 줄**을 남긴다.
3. **송신률은 4.25 req/s 그대로다.** 금값과 실제 획득 타이밍 둘 다 테스트로 못박았다.

---

## 1. 고치기 전 — 빨강

서버가 `MARKET_DATA_CHART`(`/api/v1/candles`) 에 `X-RateLimit-Limit: 20` 을 주는 응답 5건을
합성해 먹였다. `aecacd5` 코드 그대로:

```
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

**[4] 가 [2]·[3] 어디로도 안 나온다.**

---

## 2. 고친 뒤 — 초록

같은 입력, 같은 스크립트:

```
[1] 송신률: rate 4.25 -> 4.25          ← 그대로

[2] 클램프가 일어나는 동안 나온 로그 줄:
    WARNING tossmon.collector: LIMIT-CLAMP start group=MARKET_DATA_CHART
      server=20.0 ceiling=5.0 — 서버가 천장보다 높은 한도를 주는데 우리는 천장으로
      자르고 있다 (송신률은 그대로, 판단은 사람이)

[3] 수집기 텔레메트리 줄에서 clamp 관련 항목:
    {'limit_header_clamped': 5,
     'limit_header_clamped_groups': 'MARKET_DATA_CHART:20>5x5',
     'limit_header_lowered': 0}

[4] per-group = {'MARKET_DATA_CHART':
      {'count': 5, 'active': True, 'ceiling': 5.0, 'header': 20.0, 'header_max': 20.0}}
```

---

## 3. 대조군 — 정상 하향에서 보고 대상은 0

서버가 천장(5)보다 **낮은** 3 을 주는 응답 10건 (출하 `usage_ratio` 0.85 그대로):

```
rate       = 2.55   (내려간 것이 맞다 — 규칙은 안 바꿨다)
counters   = {'limit_header_clamped': 0, 'limit_header_lowered': 10, 'clamp_log_failures': 0}
per-group  = {'MARKET_DATA_CHART': {'count': 0, 'active': False, 'header_max': 0.0}}
로그 줄     = (없음)
```

고정: `test_normal_downward_header_is_not_reported_as_a_clamp`
(보고 카운터 0 / 그룹 문자열 `-` / 로그 0줄 / 하향 채택은 그대로).

---

## 4. 송신률 불변 — 테스트로 고정

| 테스트 | 무엇을 못박나 |
|---|---|
| `test_clamp_visibility_does_not_move_the_rate_by_one_call` | `limit=20` 헤더 **50건** 뒤에도 `rate` 4.25 / `capacity` 동일 / `window_cap` 5 / `sustained_rate` 4.25 / `limits[CHART]` 5.0 |
| `test_a_clamped_group_still_sends_at_the_ceiling_rate` | 버스트 소진 후 `limit=20` 을 먹인 상태에서 5회 획득 ≥ 0.8초 — 헤더의 20/s 가 송신 경로로 새면 즉시 통과했을 것 |

이 밖에 `budget.py` 는 건드리지 않았고, 텔레메트리의 예산 줄도 그대로다
(`MARKET_DATA_CHART=peak0/p95:0/avg0.00/tgt4.25`).

---

## 5. 텔레메트리 예시 한 줄

응답 1,200건(`limit=20`, CHART) + 400건(`limit=8`, MARKET_DATA 정상 하향) 뒤의 실제 출력
(발췌 — 항목 위치는 `tier2_orderbook_skipped` 와 `precision_rounded` 사이):

```
telemetry session=regular config_sig=rank3:GAIN/1d+MVOLUME/rt+TVOLUME/rt@100,... 
  tier2_orderbook_skipped=0 limit_header_clamped=1200
  limit_header_clamped_groups=MARKET_DATA_CHART:20>5x1200 limit_header_lowered=400
  precision_rounded=0 ... | budget MARKET_DATA_CHART=peak0/p95:0/avg0.00/tgt4.25
```

형식은 `그룹:서버최대>천장x횟수`. 잘린 적 없으면 `-`. 값에 공백이 없어야 `k=v` 파싱이 안
깨지므로 그것도 테스트로 고정했다.
**응답 1,200건 → 로그 1줄이다** (전이에서만 찍는다).

---

## 6. 건드린 파일

| 파일 | 무엇 | 왜 |
|---|---|---|
| `tossmon/api/limiter.py` | 카운터 방향 분리 + `clamped` 그룹별 상태 + `clamp_report()` + `on_clamp_change` 훅 | 소유 범위. 관측 본체 |
| `tossmon/collector/loops.py` | **2줄 배선**: `telemetry()` 에 `**_limiter_clamp(self.client)`, `create()` 에 `_wire_clamp_log(ctx)` + 헬퍼 2개 | 텔레메트리 줄과 `notifier` 가 여기밖에 없다. 로직은 안 옮겼다 |
| `tests/test_api_limit_clamp_visibility.py` | 신규 12건 | 빨강/초록·대조군·송신률 불변·소음 방지 |
| `docs/56_limit_clamp_visibility.md` | 판단 근거 | — |

`budget.py`(W4 가 방금 고쳤다)·`config.example.yaml`·`SPEC_LIMITS` 는 **건드리지 않았다**.

---

## 7. 혼자 내린 판단 (자기 신고)

1. **`logging` 대신 콜백을 썼다.** `tossmon/api/tokens.py` 가 이미 `logging.getLogger(__name__)`
   을 쓰고 있어서 limiter 도 그럴 수 있었다. 확인해 보니 `Notifier` 는 `tossmon.collector`
   로거에만 핸들러를 달고 `propagate=False` 로 끊고, 컬렉터 어디에도 root 로거 설정이 없다
   (`grep -rn "basicConfig|addHandler|getLogger()" tossmon/` → notifier·universe 뿐).
   즉 `tossmon.api.*` 로 찍은 INFO 는 **`data/collector.log` 에 안 남는다.** 사람이 보는
   파일에 안 닿는 로그를 추가하면 이번 결함을 그대로 반복하는 것이라 훅 주입으로 갔다.
   대가: `loops.py` 를 2줄 건드려야 했다.
2. **`limit_header_lowered`(정상 하향)도 셌다.** 태스크는 "소음으로 만들지 마라" 였고 안 세도
   요건은 충족한다. 그래도 넣은 이유: 카운터가 하나뿐이면 `clamped=0` 이 "클램프가 없다"인지
   "헤더를 아예 못 읽고 있다"인지 **구분할 수 없다** — 그 구분이 안 되는 것이 이번 사건의
   구조다. 별도 키라 보고 대상과 섞이지 않는다.
3. **텔레메트리 항목을 3개로 정했다** (`clamped` / `clamped_groups` / `lowered`).
   그룹·크기·빈도를 한 문자열(`GROUP:20>5x1200`)로 접어 항목 수를 늘리지 않았다.
4. **limiter 를 못 읽을 때 키를 지우지 않고 `-1`/`n/a` 로 적는다.** 관측치가 조용히 사라지는
   것이 원인이었으므로, 없어지는 것보다 틀린 값이 낫다고 판단했다.
5. **`header_max` 와 `header` 를 둘 다 남겼다.** 텔레메트리는 누적이라 `header_max`(최대 기회
   손실), 로그 줄은 그 시점 사실이라 `header`.

---

## 8. 남은 것 — 판단이 필요한 자리

- **서버의 20/s 가 (a) 진짜 사양 상향인지 (b) 헤더 의미 변경(분당 쿼터)인지 아직 못 갈랐다.**
  가르는 방법은 실제로 20/s 를 시도해 429 가 나는지 보는 것뿐이고 그건 라이브 실험이다.
  이 태스크는 "보이게 한다"까지다. **천장은 안 올렸다.**
- 워치독(`w5-ops`)이 `limit_header_clamped > 0` 을 판독할지는 W5 소유. 값은 이제 텔레메트리
  줄에 있으므로 읽을 수는 있다.
- 서버가 `X-RateLimit-Limit` 헤더를 **아예 안 보내기 시작하면** `active` 는 마지막 상태로
  굳는다(전이 판정에 헤더가 필요하므로). 카운터가 안 늘어나는 것으로 읽을 수는 있지만
  자동으로 "end" 가 찍히지는 않는다 — 알고 남긴다.

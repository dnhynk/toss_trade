# 10 — 적대적 감사 (W6)

> **[기록]** 소유 W6 · 2026-07-30 · 적대적 감사 1 차. **기준 `e1ed22d`(636 tests) 시점의 코드**다
> 상태 표기의 뜻과 전수 목록: [`docs/INDEX.md`](INDEX.md)

<details>
<summary><b>이 문서의 지도 — 절 16개 (1,132줄)</b></summary>

- 0. 감사 방법과 태도
- ① 토큰 단일성 파괴 경로
- ② rate limit 초과 경로
- ③ 룩어헤드 편향
- ④ 시간대·서머타임·세션 경계
- ⑤ 시크릿 유출 경로
- ⑥ 크래시 후 데이터 무결성
- ⑦ 계약 위반·중복 구현
- ⑧ GET-only 차단 우회 가능성
- ⑨ `events` 중복 — 두 층이 함께 무력화되는 경로
- ⑩ 승격/강등 churn
- ⑪ 유니버스 오염
- §3. 낮음
- §4. 미확인 — 의심되지만 증명하지 못한 것
- §5. Phase 2 착수 전 반드시 해소해야 할 것
- 부록 — 감사 실행 환경

</details>

> 소유: W6. **읽기 전용 감사** — 이 문서 외 어떤 파일도 수정하지 않았다.
> 감사 기준: `main` = `e1ed22d` (636 tests green, 샌드박스에서 재확인 — `636 passed in 178.96s`).
> 라이브 호출 없음. `api_keys` 읽지 않음. 재현은 전부 리포 밖 샌드박스 사본에서 실행했고
> 검증용 테스트는 커밋하지 않는다.

## 0. 감사 방법과 태도

"이 코드가 맞다"를 확인하지 않았다. **"어떤 입력·타이밍·순서에서 틀리는가"**를 찾았다.
각 항목은 실제로 실행한 재현과 그 출력을 인용한다. 실행으로 증명하지 못한 것은
결함 목록에 올리지 않고 **§4 미확인**에 따로 뒀다.

심각도 기준은 **"실현되면 무엇을 잃는가"**이며, 원칙은 **조용히 틀리는 것 > 시끄럽게 죽는 것**이다.
크래시는 재시작하면 되지만, 조용히 틀린 데이터는 Phase 2 의 백테스트를 통째로 무효화하고
그 사실을 아무도 모른다.

| 심각도 | 정의 |
|---|---|
| **치명** | 되돌릴 수 없는 손실(라이브 중단, 잘못된 모집단의 데이터 축적) 또는 수집 데이터 자체의 무결성 파괴 |
| **높음** | 조용한 오작동. 관측은 계속되지만 결과가 틀리거나, 안전장치가 실제로는 작동하지 않음 |
| **중간** | 특정 조건(계절·세션·설정)에서만 틀리거나, 영향이 국소적 |
| **낮음** | 현재 도달 불가능하거나 실질 피해가 없으나 방어선이 얇음 |
| **미확인** | 의심되지만 증명 못 함 (§4) |

### 요약

| # | 항목 | 심각도 | 한 줄 |
|---|---|---|---|
| F-1 | ① 토큰 | **치명** | 파일락이 CWD 가 다르면 서로를 못 본다. 워크트리가 7개다 |
| F-2 | ⑪ 유니버스 | **치명** | `build_universe` 는 호출자가 없다. 감시 대상에 가격·시총 필터가 한 번도 적용되지 않는다 |
| F-3 | ③⑨ 룩어헤드 | **치명** | 전일 이벤트를 자기오염된 곡선으로 재판정하고 UPSERT 로 깨끗한 기록을 덮어쓴다. 중복 방어 2층이 함께 뚫린다 |
| H-1 | ①② 토큰 | 높음 | 동시 401 → 토큰 다중 발급 (5개 중 2회 실측). AUTH 는 rate limit 밖 |
| H-2 | ② 한도 | 높음 | 헤더 자기보정에 상한이 없다. 한 번에 7 → 420 req/s |
| H-3 | ② 한도 | 높음 | 1초 최대 호출 = 2×rate = **공시 한도의 1.4배** (5/s 그룹에 7콜) |
| H-4 | ② 한도 | 높음 | 429 감속이 성공 응답 5번이면 소멸한다 (≈1초) |
| H-5 | ⑦⑧ 계약 | 높음 | `ForbiddenEndpoint` 가 `_guarded` 에 삼켜진다 — 주문 차단 위반이 warn 한 줄 |
| H-6 | ③⑦ 분석 | 높음 | `baselines`/`prev_close` 가 심볼당 1회만 계산되고 영원히 갱신되지 않는다 |
| H-7 | ⑩ churn | 높음 | 랭킹 승격이 dwell 을 무시한다. 랭킹 점수가 실제 스코어를 압도해 표적을 축출한다 |
| H-8 | ⑥ 운영 | 높음 | `ops` 기본 `collector_cmd` 가 없는 모듈을 가리킨다 → 조용한 무수집 |
| H-9 | ⑥ 재시작 | 높음 | 이어받기가 `candles_1m` 에 영구 구멍을 남기고 **아무것도 경고하지 않는다** (실측 40분·100분) |
| M-1 … M-12 | | 중간 | 본문 각 절 |
| L-1 … L-7 | | 낮음 | §3 |

---

## ① 토큰 단일성 파괴 경로

### F-1 (치명) — 파일락은 CWD 가 다른 두 프로세스를 막지 못한다

`config/config.example.yaml:9` 의 `token_state_path: "data/token_state.json"` 는 **상대경로**이고,
`tossmon/config.py:161-165` 의 `_as_path` 는 `Path(raw)` 를 그대로 돌려준다 — 절대경로 변환이 없다.
`tossmon/api/tokens.py:43` 은 그 경로에 `.lock` 을 붙여 락 파일을 만든다.
따라서 **락 파일의 실제 위치는 프로세스의 CWD 가 결정한다.**

이 리포는 워크트리가 7개다 (`w1-core-api` … `w7-prereg`). 각 워크트리에 자기 `data/` 가 생긴다.
즉 `w5-ops` 에서 도는 collector 와 `w1-core-api` 에서 실행한 프로브는 **서로 다른 락 파일**을 잡는다.
두 번째 프로세스는 `RuntimeError` 를 내지 않고 조용히 리스를 획득하고, 발급하고,
**첫 번째 프로세스의 토큰을 죽인다.**

재현 (`tests/test_w6_token.py::test_lease_does_not_protect_across_cwd`):

```
LOCK A: ...\test_lease_does_not_protect_ac0\wsA\data\token_state.json.lock
LOCK B: ...\test_lease_does_not_protect_ac0\wsB\data\token_state.json.lock
BOTH HELD SIMULTANEOUSLY: True True
```

**왜 치명인가**: 이건 되돌릴 수 없다. 남의 토큰이 죽으면 그 프로세스의 모든 요청이 401 이 되고,
`_request` 가 `invalidate()` → 재발급을 시도하며 서로의 토큰을 번갈아 죽이는 **인증 폭풍**이 된다
(H-1 참조). 랭킹 스냅샷은 사후 조회가 불가능하므로(A2 §4) 그 시간의 데이터는 영구 손실이다.

**증폭 요인**: `tools/live_probe.py:806` 은 `TokenManager(..., live=True)` 를 **하드코딩**하고,
`:859` 의 `--state` 기본값이 상대경로 `data/token_state.json` 이다. `--live` + `TOSS_LIVE=1`
이중 게이트는 "라이브로 나갈 것인가"만 막을 뿐 **"누구의 리스를 침범하는가"는 전혀 막지 않는다.**
`docs/11_live_rehearsal.md:181-184` 에서 W5 가 "프로브를 띄우면 후자가 즉시 RuntimeError 로 죽는다"고
적은 전제는 **두 프로세스가 같은 디렉터리에서 실행될 때만** 참이다.

**제안**
1. `config.py::_as_path` 가 `keys_path`/`token_state_path`/`db_path` 를 **config 파일 위치 기준으로
   절대경로화**한다. 이것 하나로 CWD 의존성이 사라진다.
2. 락 파일 경로를 리포 루트가 아니라 **사용자 홈 아래 고정 경로**(예: `~/.tossmon/<client_id_hash>.lock`)로
   둔다 — 리스는 워크트리가 아니라 **API 자격증명 단위**로 유일해야 하는 자원이다.
3. 상태파일에 소유자 정보(pid, CWD, 시작 시각)를 같이 적고, `get()` 이 낯선 소유자를 보면 경보한다.

### H-1 (높음) — 동시 401 이 토큰을 여러 번 발급한다 + AUTH 는 rate limit 밖이다

`tossmon/api/client.py:111-117`: `AuthExpired` 를 받은 각 요청이 `tokens.invalidate()` 를 부르고
재시도한다. `invalidate()`(`tokens.py:69-78`)는 `_token=None` + 상태파일 삭제이고,
`get()`(`:51-67`)은 `_alock` 안에서 재발급한다. 두 호출이 **서로 다른 락 구간**이라
`invalidate → get → invalidate → get` 인터리브가 가능하다. 루프가 5개(`loops.py:1185-1195`)이므로
동시 401 이 5건 뜨는 것은 정상 상황이다.

재현 (`tests/test_w6_token.py::test_concurrent_invalidate_get_issues_multiple_tokens`) — 5개 동시:

```
AssertionError: 2 issuances; earlier tokens are dead
assert 2 == 1
```

발급이 2회 일어났다. 이 API 는 client 당 토큰이 1개이므로 **1회차 토큰은 그 순간 죽는다.**
그 토큰을 들고 있던 in-flight 요청은 401 → 또 invalidate → 또 발급, 으로 자가증식한다.

게다가 `_issue()`(`tokens.py:162-211`)는 **`GroupRateLimiter` 를 전혀 거치지 않는다.**
`endpoints.py:50` 은 `AUTH: 5.0` req/s 를 정의하지만 이 경로에 적용되는 곳이 없다:

```
AssertionError: AUTH 발급 경로에 rate limit 이 없다
```

**제안**: `invalidate()` 에 "무효화한 토큰 값"을 인자로 받아, 이미 다른 태스크가 더 새 토큰을
발급했으면 무시하는 CAS 방식으로 바꾼다(`invalidate(stale_token: str)`). 그리고 `_issue()` 앞에
`limiter.acquire("AUTH")` 를 넣는다.

### 확인했고 문제 없던 것 (①)

- `live=False` 는 락을 잡지 않고 `MOCK_TOKEN` 을 준다 — 리스 없는 워커 다수가 mock 을 공유하는
  정상 동작 (`tokens.py:53-54`).
- 상태파일에 유효 토큰이 있으면 재발급하지 않는다 (`tokens.py:61-64`).
- `_write_state` 는 `mkstemp` + `os.replace` 로 원자적이다 (`tokens.py:120-133`).
- **테스트 스위트는 안전하다.** `live=True` 로 `TokenManager` 를 만드는 테스트가 10곳 있으나
  (`tests/test_api_tokens.py`) 전부 `tmp_path` 의 키/상태파일을 쓰고, 발급하는 것들은
  `monkeypatch.setenv("TOSS_BASE_URL", mock_server.url)` 로 mock 을 가리킨다.
  실 `api_keys` 를 읽거나 운영 리스를 잡을 수 있는 테스트는 없다.
- 강제 종료 시 stale lock 은 남지 않는다 — OS 가 프로세스 종료 시 반납한다
  (W5 가 `docs/11` §5 에서 실측 확인).

---

## ② rate limit 초과 경로

### H-2 (높음) — 헤더 자기보정에 상한이 없다

`tossmon/api/limiter.py:107-113`: `X-RateLimit-Limit` 이 오면 **무조건 서버값을 채택**한다.
공시 한도나 config 값과 대조하는 상한이 없다.

```python
if limit is not None and limit > 0:
    target = limit * self.usage_ratio
    b.rate = max(target, 1e-3)
    self.limits[group] = limit          # config 값을 영구히 덮어쓴다
```

`X-RateLimit-Limit` 의 **단위가 초당인지 분당인지 실측된 바 없다**(docs/06 에 값 자체가 없다).
분당 규약(매우 흔하다)이면 10 req/s = 600 req/min 이므로:

재현 (`tests/test_w6_limits_allowlist.py`):

```
rate before: 7.0 -> after: 420.0
limits dict now: {'MARKET_DATA': 600.0}
...
RANKING rate: {'rate': 70000.0, ...}   # X-RateLimit-Limit: 100000 한 번에
```

응답 헤더 한 번에 60배로 올라가고, 그 뒤로는 한도를 초당 420회로 알고 밀어붙인다.
`usage_ratio=0.7` 안전 마진은 여기서 아무 역할도 못 한다 — 분모 자체가 바뀐다.

**제안**: `b.rate = min(target, declared_limit * usage_ratio)` — **서버 헤더는 내리는 방향으로만
채택한다.** 헤더가 공시값보다 크면 채택하지 말고 경보한다(단위 오해 신호다).
`self.limits[group] = limit` 로 config 를 영구 덮어쓰는 것도 제거한다.

### H-3 (높음) — 1초 최대 호출이 공시 한도의 1.4배다

`limiter.py:33-35`: `capacity = max(rate, 1.0)` 이고 `tokens = capacity` 로 **가득 찬 상태에서 시작**한다.
따라서 임의의 1초 구간 최대 통과량은 `capacity + rate = 2 × (공시한도 × 0.7) = 공시한도 × 1.4` 다.

재현 (`tests/test_w6_burst.py`):

```
MARKET_DATA_CHART: declared=5.0/s usage_ratio=0.7 -> observed 7 requests in the first second
MARKET_DATA:       declared=10.0/s usage_ratio=0.7 -> observed 14 requests in the first second
tokens after draining: 0.00 -> after 100s idle: 7.00
```

유휴 뒤에 버킷이 만충으로 재무장하는 것까지 확인했다. 서버 한도 구현이 고정창(fixed window)이면
이 버스트는 그대로 429 다.

**언제 터지는가** — 세 안전장치가 동시에 눈이 머는 순간이 실재한다:
1. 세션 재개 시 `reconfigure_tiers` 가 정원을 되돌리고 대량 재승격이 일어난다 (M-5).
2. 각 재승격은 `_ensure_history` → `_backfill_1m` 최대 3페이지 (`loops.py:92, 1015`) 를 유발한다.
3. 그 순간 limiter 버킷은 유휴로 만충이고 (위), `BudgetGuard.measured_rate` 는 60초 창이 비어
   있어 **과소평가**한다 (`budget.py:167-182`, 주석이 스스로 인정한다).

**제안**: `capacity` 를 `max(1.0, rate * 0.5)` 수준으로 낮추고, 시작 토큰을 0 또는 1로 둔다.
버스트 흡수는 `BudgetGuard` 가 아니라 여기서 해야 한다.

### H-4 (높음) — 429 감속이 1초면 소멸한다

`limiter.py:122-124`: `update_from_headers` 는 **성공 응답마다** `backoff *= 0.8` 을 한다.
그런데 `update_from_headers` 는 `client._send`(`client.py:149`)에서 **모든 응답에 대해** 호출된다.
`on_429` 가 걸어둔 `backoff=2.0` 은 성공 응답 5번이면 1.0(=감속 없음)으로 돌아간다.

```
backoff 429 -> 2.0 after 5 successes -> 1.0
```

MARKET_DATA 는 초당 7콜을 하므로 **429 감속의 실효 수명은 약 1초**다. `MAX_BACKFILL=8.0` 상한은
사실상 도달할 수 없다. `blocked_until`(Retry-After 대기)만 실제로 작동하고, 지수 감속은 장식이다.

**제안**: 회복을 응답 건수가 아니라 **경과 시간** 기준으로 한다 (예: 마지막 429 이후 60초마다 ×0.8).

### 확인했고 문제 없던 것 (②)

- `acquire` 가 대기 중에도 `b.lock` 을 잡고 있어 같은 그룹의 동시 요청이 한도를 넘겨 몰리지 않는다
  (`limiter.py:88`). 여러 루프의 동시 acquire 는 올바르게 직렬화된다.
- `_request` 의 재시도 루프는 매 시도마다 `limiter.acquire` 를 다시 거친다 (`client.py:107-108`) —
  재시도가 한도를 우회하지 않는다.
- `BudgetGuard` 의 계획 검증(`validate_plan`)은 `config.example.yaml` 하단 산식과 일치하며
  기동 시점에 초과를 잡는다.

---

## ③ 룩어헤드 편향

### F-3 (치명) — 전일 이벤트를 자기오염된 곡선으로 재판정하고, 깨끗한 기록을 덮어쓴다

W3 가 경고한 "베이스라인에 이벤트 당일 포함" 자기오염이 **실제 호출 경로에서 발생한다.**
단 오늘 이벤트가 아니라 **버퍼에 남아 있는 전일 이벤트**에서다.

메커니즘 (전부 코드로 확인):

1. `loops.py:949-959` `_curve_for` 는 **오늘만** 제외한다 — `exclude = (today.date,)`.
2. tier2 버퍼는 최대 4일치를 들고 있다 (`HISTORY_DAYS=3` `loops.py:94`,
   `MAX_BARS_PER_SYMBOL=4*1440` `loops.py:95`).
3. `detector.py:597-615` `evaluate()` 는 **매 사이클 버퍼 전체를 재스캔**한다.
   → D+1 에는 D 일의 이벤트가 **D 일의 폭등 거래량이 들어간 곡선**으로 재판정된다.
   그 거래량은 그 t0 기준으로 **미래**다.
4. `rvol_at_t0` 가 바뀌므로 `label_hash`(`detector.py:495-505`)가 달라지고 억제가 풀린다.
   `writer.py:284-291` 의 `ON CONFLICT DO UPDATE` 가 **깨끗한 D 일 기록을 오염된 값으로 덮어쓴다.**

재현 (`scratchpad/lookahead/repro2_live_curve_contamination.py`):

```
--- on day D itself (today=D excluded) -> contract-clean
    event-day(D) event: t0_ms=1780584960000 rvol_at_t0=17.644 rvol_gated=True
--- on day D+1 (today=D+1 excluded; day D INCLUDED in curve)
    event-day(D) event: t0_ms=1780584960000 rvol_at_t0=3.419  rvol_gated=True
--- day D volume after t0 = 65.0% of day D total
--- rvol at t0 bar: dirty=3.419  clean=17.644  gate rvol_min=3.0
--- label_hash D-event: dirty=dc679652b259 clean=3a87fb7a90cb DIFFERENT -> re-emitted
```

`rvol_at_t0` 가 17.6 → 3.4 로 무너진다. 게이트가 3.0 이므로 **조금만 약한 이벤트는 재스캔에서
조용히 사라진다** — 실제로 `instant seed=4` 는 3.004 로 게이트에 0.004 차이로 걸쳐 있었다.

### ⑨ 중복 방어 2층이 함께 뚫리는 경로 — 같은 재현에서 나왔다

W4 의 경고("UPSERT 가 순수 중복을 흡수하므로 행 수로는 억제 회귀를 못 잡는다")보다 나쁘다.
**T0 자체가 이동한다:**

```
instant seed=1  D: (1780587000000, 7.456)  D+1(live): (1780587120000, 3.042)  <-- T0 SHIFTED
instant seed=6  D: (1780587000000, 8.674)  D+1(live): (1780587060000, 3.033)  <-- T0 SHIFTED
```

T0 가 1~2분 옮겨가면:
- **해시 억제층**(`detector._emitted`, 키 = `(symbol, t0_ms)`) — 키가 다르므로 신규로 본다.
- **UNIQUE + UPSERT 층**(`schema.sql:83-96` + `writer.py:284`, 키 = `(symbol, t0_ms)`) —
  키가 다르므로 **새 행을 만든다.**

두 층이 같은 키를 쓰기 때문에 **한 층을 뚫는 입력은 반드시 다른 층도 뚫는다.**
이것이 "두 층이 모두 무력화되는 경로"의 답이다. 다층 방어가 아니라 같은 방어가 두 번 있는 것이다.

재시작하면 더 나빠진다: `state_snapshot`(`loops.py:616-629`)이 `detector._emitted` 를 저장하지 않으므로,
재기동 후 버퍼의 모든 날이 오염된 게이트 값으로 새로 기록된다.

**제안**
1. **실시간 `detect_events` 를 현재 매매일로 제한한다** (`df[ts >= day_start]`). 전일 이벤트를
   다시 판정할 이유가 없다 — 이미 기록됐다.
2. 굳이 재판정한다면 **판정 대상 날짜를 곡선에서 제외**한다 (leave-one-day-out).
3. `record_event` 의 라벨 갱신이 `rvol_at_t0` 처럼 **검출 시점에만 의미가 있는 값**을 덮어쓰지 않게 한다.
4. 중복 방어 2층 중 하나는 **다른 키**를 써야 한다 (예: `(symbol, 매매일, kind)` 단위 상한).

### H-6 (높음) — `baselines`/`prev_close` 가 심볼당 한 번만 계산되고 영원히 갱신되지 않는다

`loops.py:1049-1050` 만이 `ctx.baselines` / `ctx.prev_close` 에 쓴다. 이 함수(`_refresh_baseline`)는
`loops.py:974` `if ctx.baselines.get(symbol) is None:` 일 때만 호출된다.
그리고 **어디에서도 `baselines`/`prev_close` 를 지우지 않는다** — `drop_symbol_state`(`:577-578`)는
`buffers`/`curves` 만 pop 하고, `reconfigure_tiers`(`:1172`)는 `curves` 만 clear 한다.

```
$ grep -n "baselines\|prev_close" tossmon/collector/loops.py
295:    baselines: dict[str, dict] = ...
296:    prev_close: dict[str, int] = ...
931:        ... baseline=ctx.baselines.get(symbol),
933:        prev_close_u=ctx.prev_close.get(symbol), ...
965:    if ctx.baselines.get(symbol) is not None and len(buf) > 0:
974:    if ctx.baselines.get(symbol) is None:
1049:    ctx.baselines[symbol] = compute_daily_baseline(...)
1050:    ctx.prev_close[symbol] = int(rows[-1].close_u)
```

즉 **승격 시점의 ADV20·ATR20·vol_z 파라미터·전일종가가 프로세스 수명 내내 얼어붙는다.**
Phase 1 의 목표가 "무인 다일 운영"인데, 가동 2일차부터 `day` 트리거(`close/prev_close ≥ 0.30`)의
분모가 틀린 날의 종가다.

`labeling.py:199-217` 은 스칼라 `prev_close_u` 를 **프레임의 모든 매매일에 적용**하므로,
버퍼에 3일이 있으면 3일 전 봉의 트리거도 어제 종가로 판정된다 — 이건 미래 정보다.
재현 (`scratchpad/lookahead/repro4_prev_close_lookahead.py`): day1 이 종일 $1.40 로 평탄한데
day2 종가 $1.00 을 prev_close 로 주면 **평탄한 day1 에 `day` 이벤트가 생긴다.**

```
prev_close_u = day2 close ($1.00), applied to ALL days:
        t0_ms kind session  rvol_gated
1779973200000  day unknown       False
prev_close_u = day1's true prior close ($1.40): no events
```

관련하여 `ctx.history_days` 도 `loops.py:1144-1145` 에서 **한 번만** 채워진다 —
며칠 가동하면 곡선 분모가 점점 옛날 3일에 고정된다.

**제안**: 세션/매매일 전환 시 `curves` 와 함께 `baselines`·`prev_close`·`history_days` 도 무효화한다
(`reconfigure_tiers` 에 한 줄). `detect_events` 에는 스칼라 대신 **매매일별 prev_close** 를 넘기거나
`None` 을 넘겨 프레임 내 폴백(`labeling.py:214-215`)을 쓰게 한다.

### M-9 (중간) — 계약 A1 §1 의 "함수가 잘라서 검증한다"는 부분적으로만 참이다

`extract_precursor_features` 는 `df_1m`(`features.py:172`), `rankings`(`:173`),
`prior_events`(`:482`)를 실제로 자른다 — 미래 행을 넣어도 결과가 동일함을 확인했다.
그러나 **`curve` 와 `baseline` 은 함수 밖에서 계산되어 들어오므로 함수가 자를 수 없다.**
오염된 곡선/베이스라인을 주면 결과가 크게 달라진다:

```
C  curve-with-event-day        clean/dirty
     rvol_at_cutoff                 17.15 / 3.107
     rvol_first_cross_3_lead_min    86.0  / 2.0
     rvol_first_cross_5_lead_min    86.0  / nan
D  baseline-with-event-day-bar
     daily_vol_z                    4.664 / 1.757
     gap_from_prev_close            0.2852 / -0.3574
```

`rvol_first_cross_*_lead_min` 은 **검증질문 1(전조 리드타임)의 답 그 자체**다. 86분이 2분/NaN 이 된다.
현재 리포 안의 호출자는 깨끗하지만, 계약이 "함수가 강제한다"고 적혀 있어 후속 연구 코드가
이 보증을 믿고 오염된 곡선을 넣을 위험이 있다.

**제안**: 곡선에 기여한 날짜를 `curve.attrs` 에 실어 보내고, 함수가 `t0_ms` 이후 날짜가 있으면
`ValueError` 를 던진다. 최소한 계약 문구를 "df/rankings/prior_events 만 강제"로 정정한다.

### M-8 (중간) — `evaluate._gate` 는 컬럼이 없으면 조용히 통과시킨다

`evaluate.py:63-64`: `rvol_gated` 컬럼이 없으면 전부 통과하고 `n_ungated_excluded=0` 을 보고한다.
그런데 `events` 테이블은 `rvol_gated` 를 `meta_json` 안에만 저장하고
(`schema.sql:83-94`), `Reader.read_events`(`reader.py:64-71`)는 펼치지 않는다.
`expand_meta_json` 없이 `q1..q6` 를 부르면 **A1 §6 의 게이트가 통째로 무력화되며 그 사실이 보고되지 않는다.**

```
=== HAZARD: same events but WITHOUT the rvol_gated column ===
  q3_daymarket_persistence   n_ungated_excluded=0  persist_rate=0.5
  q5_expectancy              n_ungated_excluded=0  mean_gross=-0.30
```

출하 경로(`report.py:205`)는 `expand_meta_json` 을 먼저 부르므로 안전하다. **제안**: 컬럼이 없으면
`gate_policy="unavailable"` 을 스탬프하거나 예외를 던진다.

### 확인했고 문제 없던 것 (③)

- `include_t0=False` 는 `ts_ms < t0_ms` 를 엄격히 지킨다 (df/rankings/prior_events 전부 검증).
- `include_t0=True` 는 T0 봉 **하나만** 추가로 본다 — 계약 A1 §1 이 W4 에 허용한 그대로다.
- `detect_events` 의 트리거는 후행 전용이다: `_rolling_min_close` 가 trailing 임을 수치로 확인
  (`[100,90,80,10] w=2min → index1=90`), rvol 게이트는 t 까지 누적.
- `q1~q6` + `base_rate_comparison` **전부** `_gate` 를 호출한다 (`evaluate.py:138,192,247,291,362,435,474`).
- pandas 롤링 연산 2곳뿐이며 둘 다 현재 행을 포함하지 않는다 (`true_range_u` 는 `shift(1)`).
- `_curve_for` 의 1시간 캐시가 날을 넘겨 재사용되어도 캐시된 곡선 자체에 새 날 봉이 없고,
  `reconfigure_tiers` 가 세션 전환마다 캐시를 비운다 — 누수 없음.

---

## ④ 시간대·서머타임·세션 경계

세션 판정은 계약대로 `/market-calendar/US` 만 쓴다. 하드코딩된 KST/ET 오프셋이 로직에 쓰인 곳은
**없다** — 리포 전역 grep 결과 히트는 전부 표시·mock·테스트 픽스처였다
(`tools/mock_server.py:214-217`, `ops/rotate_logs.py:36`, `tests/synth.py:44-49`).

### M-2 (중간) — `iso_to_ms` 가 반올림 대신 절삭한다

`tossmon/api/models.py:62`: `return int(dt.timestamp() * 1000)`.
`datetime.timestamp()` 가 float 이라 일부 밀리초에서 `...357.9998` 이 나오고 `int()` 가 357 로 깎는다.

```
tested 2000000 random ms values; mismatches: 6
repr(dt.timestamp()) = 2186171553.358; t*1000 = 2186171553357.9998; int() = 2186171553357
minute-aligned round-trip mismatches: 0 /500000
second-aligned  round-trip mismatches: 0 /500000
```

봉 타임스탬프(분 정렬)와 `nextBefore` 는 영향 없음(0/500k). 영향 받는 것은 `/trades`·`/prices` 의
실제 ms 타임스탬프 — `last_trade_ms` 비교나 등가 조인이 1ms 어긋날 수 있다.
**제안**: `round(dt.timestamp()*1000)` 또는 정수 연산.

### M-3 (중간, 2026-11-01 부터 발현) — 겨울(EST)에 UTC 날짜 기준 매매일이 갈라진다

`labeling.py:11-13` 과 `features.py:95` 의 "토스 4세션은 UTC 00:00~22:00 에 들어가므로 UTC 날짜 =
매매일" 이라는 주석은 **서머타임 기간에만 참**이다. EST 에서는 애프터장이 UTC 자정을 넘는다.

```
EDT summer: trading day spans UTC 2026-07-15 00:00 .. 2026-07-15 23:49  same-UTC-date=True
EST winter: trading day spans UTC 2026-01-15 01:00 .. 2026-01-16 00:49  same-UTC-date=False
_day_spans(calendar=None) -> 2 'trading days'   # 하나의 매매일이 둘로 쪼개진다
```

세 곳이 걸린다. 앞의 둘은 캘린더가 없을 때만 쓰는 폴백이지만,
**`features.py:476-502` `_history_features` 는 캘린더를 줘도 항상 UTC 날짜를 쓴다:**

```
bars all belong to ONE trading day (2026-01-15 EST).
hist_days_available = 2.0        (should be 1.0)
prior_event_count_20d proxy = 2.0  (한 번의 급등이 두 번으로 계산됨)
```

겨울에는 매일 `hist_days_available` 가 부풀고 former-runner 프록시가 이중 계산된다.
**제안**: `_history_features` 에 캘린더/매매일 span 을 넘겨 `UsMarketDay` 기준으로 묶는다.

### M-4 (중간) — 반일장(조기폐장)이 분-of-session 곡선을 오염시킨다

`baselines.py:201-211` 은 `(session, minute_offset)` 로 색인한다. 서머타임에는 안전함을 확인했으나
(11/1 DST 종료를 걸친 4일 캘린더에서 버킷 정렬 유지) **세션 길이 차이에는 무방비**다.

```
== 반일장(13:00 ET 조기폐장, 210분)이 곡선에 섞였을 때 ==
  curve[('regular', 209)] = 2080000000 (반일장 없을 때 100000000)
  rvol_bar at minute 209 of a normal day: 0.0481   (정상값 1.0)
== 반일장 당일을 정상일 곡선으로 볼 때 ==
  rvol_bar at half-day CLOSE = 100.0
```

추수감사절 다음날·크리스마스 이브에 종가 경매 스파이크가 `rvol_bar` 100배로 찍혀 허위 버스트
신호가 되고, 반대로 그 반일장이 `HISTORY_DAYS=3` 창 안에 들어오면 정상일의 같은 분이 20배 죽는다.
누적 기준 게이트(`rvol_series`)는 1.47배로 영향이 작다.
**제안**: 조기폐장일을 분별 평균에서 제외하거나, 세션 끝에서부터의 분 인덱스도 함께 쓴다.

### 확인했고 문제 없던 것 (④)

- 세션 구간은 정확히 반열림 `[start, end)` 다. 20,000 시각 표본에서 **중복 소유 0, 미아 0**.
  경계 ms 는 다음 세션 소유, `after.end` 는 `closed`.
- 유일한 공백은 08:50–09:00 KST(매매일 사이)로 실제 휴장이다 — 연속 거래 구간을 버리지 않는다.
- `day=None`, 전 세션 None, 캘린더 값 None 전부 `closed` 로 안전 처리.
- `exclude_today_1d_cutoff` 는 EDT/EST/DST 월요일/정규장 없는 날 전부 올바르다 (당일 제거, 전일 유지).
- `iso_to_ms` 는 naive 문자열을 `SchemaMismatch` 로 거부하고, `.123`/`.123456`/`Z`/`+0900`/`+09` 를 모두 처리한다.
- `next_before_ms - 1`(`loops.py:1029`) 의 ms 왕복은 정확하다.
- KST/ET 변환은 표시 레이어에만 있다.

---

## ⑤ 시크릿 유출 경로

**시크릿 값 자체가 유출되는 것으로 증명된 경로는 없다.** 다만 한 곳이 서버 응답에 좌우된다.

### M-1 (중간) — 토큰 엔드포인트의 `error_description` 이 그대로 로그에 실린다

`tokens.py:196-203`: 200 이 아닌 응답의 `error` / `error_description` 을 예외 메시지에 그대로 넣는다.
그 예외는 `_guarded`(`loops.py:731-733`)를 거쳐 `notifier.warn` → `data/collector.log`
(32MB × 4 회전) 와 콘솔에 평문으로 남는다.

재현 (`tests/test_w6_secrets.py`) — 서버가 에코하는 상황을 가정:

```
EXC: token issuance rejected (400): invalid_client client myclientid secret SUPERSECRET_... rejected
request body keys: ['client_id', 'client_secret', 'grant_type']
```

**증명된 것**: 서버가 제어하는 임의 텍스트가 검증 없이 로그 파일에 평문으로 들어간다
(개행을 넣으면 로그 라인 위조도 된다).
**증명하지 못한 것**: 토스 서버가 실제로 `error_description` 에 client_secret 을 에코하는지.
그래서 §4 미확인에도 같이 적는다. 일부 OAuth 구현이 `invalid_client` 에 제출값을 되돌려 준다.

**제안**: `error_description` 을 메시지에 넣지 말고 길이·해시만 남기거나, 방금 로드한
client_id/secret 문자열을 메시지에서 마스킹한다. 비용이 거의 없는 수정이다.

### 확인했고 문제 없던 것 (⑤)

- `_read_keys` 의 KeyError 메시지는 **키 이름만** 담는다 (`tokens.py:157-160`):
  `... missing CLIENT_ID/CLIENT_SECRET (keys found: ['WRONGKEY'])` — 값 없음.
  기존 테스트(`test_keys_error_does_not_leak_secret`)도 이걸 지킨다.
- httpx 전송 예외 체인에 폼 바디가 실리지 않는다 — `ConnectError` 로 검증: `chain contains secret: False`.
- `last_headers` 는 **응답** 헤더만 담는다 (`client.py:151`). `Authorization` 요청 헤더는 저장되지 않는다.
- `_err_code`(`client.py:374-384`)는 `code`/`message` 필드만 뽑는다.
- `.gitignore` 가 `api_keys`, `data/token_state.json`, `data/`, `*.key`, `.env*` 를 막는다.
- `ops/hooks/pre_commit_secret_scan.py` 의 패턴·차단 파일명 목록은 적절하다.
  **단, 이 워크트리에는 훅이 설치돼 있지 않다** (`git config core.hooksPath` 미설정,
  워크트리라 `.git` 이 파일이라 `.git/hooks/` 도 없다) — 낮음, 아래 L-2.

---

## ⑥ 크래시 후 데이터 무결성

**WAL 과 부분 쓰기는 안전하다** (아래 "확인했고 문제 없던 것" 참조 — 실제로 자식 프로세스를
`os._exit(9)` 로 트랜잭션 중간에 죽여 검증했다). 문제는 DB 가 아니라 **이어받기 로직**에 있다.

### H-9 (높음) — 재시작 이어받기가 `candles_1m` 에 영구적이고 조용한 구멍을 남긴다

원인이 둘이고, 둘 다 `resume_point_ms` 를 무력화한다.

**(a) 백필 스킵 분기.** `loops.py:969-973` `_ensure_history`:

```python
loaded = _load_history_from_db(ctx, symbol, now)
if loaded < CANDLE_PAGE:                      # 200
    await _backfill_1m(ctx, symbol, stop_at_ms=ctx.resume_point_ms(symbol))
```

재시작 후 버퍼는 비어 있고 DB 에서 3일치를 읽어온다. 정전 전에 tier2 로 200분 넘게 있던 심볼은
DB 에 이미 200봉 이상이 있으므로 **`loaded >= 200` 이 되어 백필이 통째로 건너뛰어진다.**
`resume_point_ms` 와 `stop_at_ms` 는 이 분기에서 **아예 조회조차 되지 않는다.**
실시간 폴링(`loops.py:910`, `count=200`)은 최신 200봉만 덮으므로 200 세션분을 넘는 정전은
**영구 구멍**이 된다.

**(b) 페이지 상한.** `MAX_BACKFILL_PAGES=3`(`loops.py:92`)이 백필을 600봉으로 자르는데,
`_backfill_1m`(`:1004-1031`)은 **`stop_at_ms` 에 실제로 닿았는지 확인하지 않고** 끝난다.

재현 (리포의 `ReplayClient` 로 실제 `load_state` → `tier2_symbol_once` 경로를 4사이클 구동):

```
--- case1: loaded>=200 -> backfill SKIPPED ---
outage: crashed at min 320, restarted at min 560 (240 market minutes lost)
backfill counter: backfill_bars=NOT RUN  history_from_db=1
candles_1m rows=421; MISSING minutes in DB: 40 (320..359)
notifier alerts=0 warns=0 -> gap reported? NO

--- case2: backfill runs but capped at 3 pages (600 bars) ---
outage: crashed at min 250, restarted at min 950 (700 market minutes lost)
backfill counter: backfill_bars=600  history_from_db=1
candles_1m rows=751; MISSING minutes in DB: 100 (250..349)
notifier alerts=0 warns=0 -> gap reported? NO
```

**아무도 모른다.** `ops/healthcheck.py:89` 는 테이블별 `COUNT/MAX` 만 본다 — 구멍은 카운트로 안 보인다.
유일하게 드러나는 곳은 사후 리포트 `tools/dryrun_night.py:301-305` 의 "결측 구간" 절이며,
그것도 세션이 끝난 뒤 누가 읽어야 한다.
그리고 재시작 회귀 테스트 `tests/test_collector_replay.py:191-231` 은 `bars_after > bars_before`(228행)
**만** 확인한다 — 구멍 없음을 확인하는 단언이 없다. 그 단언이 있었으면 잡혔을 결함이다.

어젯밤 재시작 2회(`docs/11` §5)에서 구멍이 안 난 것은 정지 시간이 수 분이었기 때문이다.
문서의 "카운트가 끊김 없이 이어졌다"(`docs/11:228-229`)는 **카운트**를 본 것이고,
카운트는 구멍을 볼 수 없다.

**제안**: 이어받기 시 `gap = now - resume_point` 를 계산해 `loaded` 와 무관하게
`_backfill_1m(pages=ceil(gap/200), stop_at_ms=resume_point)` 를 강제한다.
`_backfill_1m` 이 `oldest <= stop_at_ms` 에 닿지 못하고 끝나면 **반드시 `notifier.warn`** 한다.
그리고 replay 테스트에 "구멍 없음" 단언을 추가한다 (1분봉은 320~1702일 보관되므로
**탐지만 되면 나중에 메울 수 있다** — 탐지가 없는 것이 문제의 본질이다).

### M-12 (중간) — `last_ranking_snap_ms` 는 저장되지만 복원되지도, 읽히지도 않는다

`state_snapshot`(`loops.py:626`)이 저장하는데 `load_state`(`:645-674`)가 복원하지 않고,
사실 **코드 전체에서 읽는 곳이 없다** (setter 는 `:233` 하나뿐).

```
state file keys: [..., 'last_ranking_snap_ms', ...]
  session              : 'closed'   <- saved 'regular', NOT restored
  rankings.last_snap_ms: None       <- saved 1784999970000, NOT restored
```

대비되는 것이 `last_trade_ms` 다 — 이건 복원되고(`:662`) 테이프 공백 경고를 구동한다(`:1111-1117`).
그런데 **`rankings_snap` 은 "사후 조회가 불가능한 유일한 데이터"**(`loops.py:5`)인데도
정전 구간에 대한 경고가 없다. 되돌릴 수 없는 손실이 조용히 지나간다.

**제안**: 이어받기 시 `warn(f"rankings hole {last_ranking_snap_ms}..{now} — 복구 불가")`.

### H-8 (높음) — `ops` 기본 `collector_cmd` 가 존재하지 않는 모듈을 가리킨다

`ops/opsconfig.py:63-64`:

```python
if not collector_cmd:
    collector_cmd = ["python", "-m", "tossmon.collector.main"]
```

그런 모듈은 없다. 진입점은 `tossmon/collector/__main__.py` 이므로 `python -m tossmon.collector` 다.

```
$ python -m tossmon.collector.main --help
No module named tossmon.collector.main
$ python -m tossmon.collector --help
usage: __main__.py [-h] [--config CONFIG] ...
```

`ops_config.yaml` 이 없거나 `collector_cmd` 키가 빠지면 supervisor 는 즉사하는 자식을
반복 spawn 하다가 재시작 폭주 가드(5회/600초)에 걸려 **조용히 포기한다.**
`config.yaml` → `config.example.yaml` 폴백과 같은 함정이며, 무인 운영에서 밤새 아무것도
수집되지 않는 결말이다.

**제안**: 기본값을 `["python", "-m", "tossmon.collector", "--config", "config/config.yaml"]` 로
고치고, `_spawn` 직후 자식이 N초 안에 비정상 종료하면 "설정 오류"로 구분해 경보한다.

### L-7 (낮음) — `tiers.seed` 가 score=0.0 으로 복원한다

`detector.py:331-338`: 이어받은 심볼은 전부 `score=0.0` 이 된다
(`seeded AAA: tier=3 score=0.0 changed_ms==saved_ms`).
`set_capacity` 는 점수 오름차순으로 강등하므로 **재시작 직후 첫 정원 조정에서 복원된 심볼이
먼저 잘려나간다** — 그것도 임의 순서로. 그리고 `changed_ms=saved_ms` 라 `hysteresis_s` 동안
정당한 이동도 막힌다. 데이터 손상은 아니고 거동 편향이다.

### 확인했고 문제 없던 것 (⑥)

- **WAL 과 트랜잭션 경계는 안전하다.** 모든 Store 쓰기가 배치당 1 트랜잭션이다
  (`with self._conn:` — `writer.py:59,104,130,159,186,222,252,277`; `record_event` 는 INSERT+SELECT 가
  같은 트랜잭션 `:277-309`; 마이그레이션은 `BEGIN IMMEDIATE…COMMIT`; retention 은 아카이브 검증 후 DELETE).
  `executemany` 도중 자식을 `os._exit(9)` 로 죽여 검증: 트랜잭션 안에서 550행이 보이던 상태에서
  부모가 관측한 결과는 `rows=50, integrity=ok`. 리더의 스냅샷 격리도 확인
  (`reader sees mid-transaction row? 0`). 프라그마는 `wal / synchronous=1(NORMAL)`.
  프로세스 강제 종료에는 안전하고, 정전 시 **커밋된** 최근 트랜잭션 유실 가능성은 WAL+NORMAL 의
  알려진 트레이드오프다 (손상은 아니다).
- 중복 수집은 **과다 조회뿐**이고 데이터 오염이 없다 — 상태파일 힌트가 DB 보다 최대 60초 낡아도
  `resume_point_ms` 가 버퍼/DB 의 마지막 봉을 우선하고, PK UPSERT 가 흡수한다. 예산만 조금 낭비된다.
- `ctx.counters` 는 텔레메트리 전용이다 (읽는 곳은 `loops.py:508-511` 뿐) — 복원된 카운터가
  거동을 좌우하지 않는다. 로직을 가르는 유일한 복원 맵은 `missing_streak`(5회 누락 시 drop)인데
  재시작을 건너 의미가 일관되게 유지된다.
- `drop_symbol_state` 가 `detector._emitted` 를 지우지 않는다는 주석은 사실이다 —
  `set_capacity` → `flush_changes` → `_record_change` 강등 경로에서도 검출 이력이 살아남음을 확인.
  `_evict` 의 상한도 오래된 것부터 정확히 동작한다.
- `rankings_once` 가 랭킹 4종을 각각 다른 `snap_ms` 로 4개 트랜잭션에 쓴다(`loops.py:779-785`).
  사이클 중간에 죽으면 그 분의 뒤쪽 종류를 잃지만 각 페이지는 자기완결적이다 (설계상 낮음).

### M-5 (중간) — 세션 경계마다 티어 상태가 통째로 날아가고, 재개해도 복구되지 않는다

`SESSION_TIER_SCALE[CLOSED] = 0.0`(`loops.py:118`) 이고
`reconfigure_tiers`(`:1167-1168`)는 `scale > 0` 이 아니면 정원을 **1** 로 만든다.

```
tier2 members before close: 40
caps applied at close: {'tier2_max': 1, 'tier3_max': 1}
tier changes written to promotions table: 39
reasons: {'session_change'}
tier2 members after close: 1
--- 재개(정원 240 으로 복원) 후 ---
tier2 members after reopen: 1
changes emitted by raising the cap: 0
```

두 가지 결과:
1. **`promotions` 테이블 오염**: 한 번의 세션 종료가 39행(운영 규모면 수백 행)을
   `reason='session_change'` 로 적는다. 라이브의 강등 221건은 상당 부분 이것으로 보인다.
   나중에 "승격 → 이벤트 리드타임"을 평가할 때 `reason` 으로 걸러야 하는데 강제하는 장치가 없다.
2. **재개 직후 백필 폭풍**: 강등 시 `_record_change` → `drop_symbol_state`(`:567`)가
   `buffers`/`curves` 를 버리므로, 재승격된 심볼마다 `_ensure_history` 가 다시 돈다
   (DB 읽기 + 부족하면 최대 3페이지 API 백필). H-3 에서 짚은 **세 안전장치가 동시에 눈머는 순간**이
   바로 여기다.

**제안**: `closed` 에서는 정원을 건드리지 말고 루프만 쉬게 한다 (이미 `collecting()` 이 막는다).
정원 축소로 인한 강등은 `promotions` 가 아니라 별도 카운터로 남긴다.

---

## ⑦ 계약 위반·중복 구현

### 중복 구현: **없다** (W4 는 계약을 지켰다)

계약 A1 §1 이 요구한 대로 `tossmon/collector/detector.py` 는 분석 레이어를 재사용한다 —
`extract_precursor_features(..., include_t0=True)`(`detector.py:589-592`),
`detect_events`(`:612-615`), `rvol_series`(`:598`),
`minute_of_session_volume_curve`(`build_curve`, `:670-684` — 얇은 가드 래퍼일 뿐 계산 없음).
이벤트 트리거·RVOL·수익률·세션 배정·VWAP·곡선 생성 중 재구현된 것은 하나도 없다.
`confirm_score`/`activity_score` 는 새 스코어지만 **티어 승격에만** 쓰이고 이벤트 라벨에 닿지 않는다.

**그러나 발산은 존재한다** — 코드 중복이 아니라 **입력 배관**에서다. 그것이 H-6 이다.
실시간과 오프라인이 같은 함수를 부르지만 다른 `prev_close_u` 를 먹어서 다른 답을 낸다:

```
OFFLINE  detect_events(df, params)                       -> 1 event
REALTIME detect_events(df, params, prev_close_u=1310000) -> 0 events
```

Phase 2 백테스트가 오프라인 라벨로 검증되면, **라이브가 절대 만들지 않는 이벤트로 전략을 검증하게 된다.**

### H-5 (높음) — `ForbiddenEndpoint` 가 삼켜진다

계약 C-5 표(`docs/04_contracts.md:168`)는 `ForbiddenEndpoint | 없음 | 코드 버그. 잡지 말 것` 이다.
그런데 `errors.py:45` 에서 `ForbiddenEndpoint(TossApiError)` 이고,
`loops.py:731` 이 `except (TossApiError, OSError)` 로 잡는다.

```
MRO: ['ForbiddenEndpoint', 'TossApiError', 'Exception', 'BaseException', 'object']
guarded returned: False
collector stopped: None
counters: {'api_errors': 1}
notifications: [('warn', 'tier3:trades:XYZ: ForbiddenEndpoint: endpoint not allowlisted: POST /api/v1/orders')]
```

**주문 계열 엔드포인트에 도달했다는 사실이 32MB 회전 로그의 warn 한 줄로 끝나고 수집은 계속된다.**
같은 문제가 `scheduler.py:230`·`:270` 에도 있다.
가장 잡히면 안 되는 예외가 "일상 api_errors" 로 분류되는 것은 가드레일 설계 실패다.

**제안**: `_guarded` 의 `TossApiError` 팔 **앞에** `except ForbiddenEndpoint: raise`
(또는 `ctx.shutdown`)를 넣는다. 근본적으로는 `ForbiddenEndpoint` 를 `TossApiError` 가 아니라
`Exception` 직속으로 옮긴다 — 계약이 "잡지 말라"고 한 것을 타입 체계가 강제하게.

### M-6 (중간) — 계약 A1 §2 를 벗어난 시그니처 2건

A1 §2 는 `*` 뒤 **기본값 None 키워드 전용** 추가만 허용한다.
- `TierStateMachine.on_new_data(self, symbol, score, ts_ms, reason: str = "score")`
  — `detector.py:341-342`. positional-or-keyword 추가이고 기본값이 None 이 아니다.
  C-8 이 이 메서드 시그니처를 명시적으로 고정한 대상이다.
- `compute_daily_baseline(df_1d, window_days: int = 20)` — `baselines.py:78-79`. 같은 문제.

(문자 그대로는 `detect_events(..., max_per_day=1, halt_gap_min=5)`,
`extract_precursor_features(..., toss_type=..., market_type=...)`,
`q1..q6(..., gate="exclude")` 도 "기본값 None" 조건을 어기지만 키워드 전용이라 호환성은 100% 다.
`gate` 는 A1 §6 이 요구한 동작 자체다.)
호출 호환성은 전부 유지된다. **제안**: `reason`/`window_days` 를 `*` 뒤로 옮기거나 계약 문구를 완화 승인.

### M-7 (중간) — 라이브 호스트 하드코딩 + 문자열 분할 회피

`tools/live_probe.py:43`:

```python
LIVE_BASE_URL = "https://" + "openapi.tossinvest.com"   # 리스 보유 워커 전용 (계약 C-11 §3)
```

계약 C-11 §3 은 "`openapi.tossinvest.com` 문자열이 코드에 하드코딩되면 반송"이다.
스킴과 호스트를 쪼갠 것은 규칙 준수가 아니라 **탐지 회피**다 (전체 URL grep 을 빠져나간다).
`:803` 에는 어차피 평문 리터럴이 있고, `:860` 에서 argparse 기본값으로 쓰인다.
C-9 는 base URL 에 기본값이 없어야 한다고 명시한다.
**제안**: `--base-url` 기본값을 `os.environ.get("TOSS_BASE_URL")` 로 하고 미설정이면 거부, 상수는 삭제.

### M-10 (중간) — `dryrun_night` 상수가 자기가 미러링한다는 config 와 불일치

`tools/dryrun_night.py:40-46` 의 `EXPECTED_INTERVAL_S` 는 `trades_snap: 8`, `orderbook_snap: 8` 인데
`config.example.yaml:50-51` 은 `tier3_trades_s: 4`, `tier3_orderbook_s: 16` 이다.
결과: 테이프 공백 12~24초를 놓치고, 호가 공백 24~48초를 허위 경보한다.
"config.py 가 아직 스텁" 이라는 주석도 오래됐다. **제안**: `PollingConfig` 를 읽는다.

### 확인했고 문제 없던 것 (⑦)

- C-4 의 모든 공개 시그니처가 위치인자·이름·기본값까지 일치한다 (M-6 의 2건 제외).
- C-5 재시도 책임 배치가 정확하다: 429/401/5xx 재시도는 `client.py:107-133` 에만 있고,
  루프는 재시도하지 않는다. `SchemaMismatch` 는 어디서도 재시도되지 않는다.
  `Forbidden` 은 `ctx.shutdown` + alert 이고 스케줄러는 재-raise 한다.
- A4 반올림이 실제로 동작하고 관측 가능하다:
  `dec_to_u('0.10461614') = 104616`, `precision_stats: {'rounded': 4, 'parsed': 5, 'max_digits': 8}`.
  `SchemaMismatch` 는 `''`/`'abc'`/`'-1.0'`/`'NaN'`/float 입력에만.
- **A5 준수 완전**: 모든 `get_candles` 호출부 검사 결과 1분봉=`adjusted=False`, 일봉=`True`.
  `candle_adjusted()` 가 미지 interval 에 예외를 던지고 회귀 테스트가 있다.
  (예외 1건: `tools/live_probe.py:242` 가 항상 `"true"` — 도구라 계약 위반은 아니나
  A5 이후 파이프라인이 쓰지 않는 계열을 측정하게 된다. 낮음.)
- **A3 준수 완전**: `orderbook_snap.imbalance_signed`(`schema.sql:79`), writer 가
  `(bid-ask)/(bid+ask)`, 잔량 0 이면 NULL. **부호형에 `>0.5` 규칙을 적용하는 소비자는 없다** (전역 grep).
- C-10 의존성 고정 준수. numpy/scipy/requests/aiohttp/uvicorn/flask 임포트 0건.
  mock 서버는 표준 `http.server`.
- C-9 config 키가 `config.example.yaml` 과 1:1 이고, 폐기된 `tier3_micro_s` 를 능동적으로 거부한다.

---

## ⑧ GET-only 차단 우회 가능성

**주문 계열 엔드포인트에 도달하는 경로는 찾지 못했다.** allowlist 에 주문 관련 항목이 없고
(`endpoints.py:12-27`), 래퍼 메서드도 없으며, mock 서버에도 없다.
명백한 우회 시도는 전부 fail-closed 였다:

```
evasions that passed: []
# 시도: //api/v1/prices, /API/V1/PRICES, /api/v1/prices/../../orders,
#      /api/v1/orders, /api/v1/order, /api/v1/orders/conditional, /api/v1/prices%2f..%2forders
```

리다이렉트도 막힌다 — httpx `follow_redirects` 기본값이 `False` 이고, 3xx 응답은 `_classify` 에서
JSON 파싱 실패로 `SchemaMismatch` 가 된다.

### M-11 (중간) — 1층 관문은 경로 정규화를 하지 않는다. 2층이 막아준다

`endpoints.py:75-85` `canonical_path` 는 쿼리스트링만 떼고 **dot segment 를 제거하지 않는다.**
템플릿 정규식이 `[^/]+` 라 `..` 이 `{symbol}` 로 매칭된다:

```
'/api/v1/stocks/../warnings'      -> canonical '/api/v1/stocks/{symbol}/warnings' allowed=True
'/api/v1/stocks/..%2f../warnings' -> canonical '/api/v1/stocks/{symbol}/warnings' allowed=True
'/api/v1/stocks/./warnings'       -> canonical '/api/v1/stocks/{symbol}/warnings' allowed=True
'/api/v1/stocks/%2e%2e/warnings'  -> canonical '/api/v1/stocks/{symbol}/warnings' allowed=True
```

**이중 차단이 여기서 값을 한다.** httpx 가 전송 전에 경로를 정규화하고, `_GuardedTransport` 가
정규화된 경로를 다시 검사해 막는다:

```
layer1 (check_allowed) -> /api/v1/stocks/{symbol}/warnings
result: blocked by layer2: endpoint not allowlisted: GET /api/v1/warnings
```

즉 **두 층이 서로 다른 문자열을 검사하고 있다** (1층=정규화 전, 2층=정규화 후).
지금은 그 불일치가 유리하게 작동하지만, 설계상 의도된 것이 아니다.
현재 실제 도달 가능성은 없다 — `{symbol}` 템플릿을 쓰는 클라이언트 메서드가 아예 없고
(`get_warnings` 미구현), 심볼은 전부 쿼리 파라미터로 들어간다.

**위험은 조합에 있다**: H-5 때문에 2층이 실제로 발동해도 warn 한 줄로 끝난다.
1층이 뚫리고 2층이 조용히 삼켜지면 방어선 두 개가 다 무의미해진다.

**제안**: `canonical_path` 에서 `.`/`..` 세그먼트를 만나면 정규화하지 말고 **즉시 거부**한다
(정규화는 우회 표면을 늘린다). 그리고 H-5 를 함께 고친다.

---

## ⑨ `events` 중복 — 두 층이 함께 무력화되는 경로

**있다. §③ F-3 에서 증명했다.** 요약하면:

두 방어층이 **같은 키 `(symbol, t0_ms)`** 를 쓴다:
- 해시 억제: `detector._emitted[(symbol, t0_ms)]` (`detector.py:572, 628`)
- DB: `UNIQUE(symbol, t0_ms)` + `ON CONFLICT DO UPDATE` (`migrations.py:14-25`, `writer.py:284`)

따라서 **T0 를 움직이는 입력은 두 층을 동시에 통과한다.** 그리고 T0 는 실제로 움직인다 —
전일 이벤트를 자기오염된 곡선으로 재판정하면 RVOL 게이트 통과 지점이 1~2분 밀린다:

```
instant seed=1  D: (1780587000000, 7.456)  D+1(live): (1780587120000, 3.042)  <-- T0 SHIFTED
```

같은 실제 사건이 `(SYM, 1780587000000)` 와 `(SYM, 1780587120000)` 두 행으로 남고,
행 수로도 해시로도 잡히지 않는다.

### t0 는 곡선 오염 없이도 움직인다 — **억제층이 살아 있는 같은 프로세스에서도** 중복이 난다

더 나쁜 사실이 별도 재현에서 나왔다. `detect_events` 는 `max_per_day=1` 일 때
**보이는 범위에서 첫 트리거 봉**을 t0 로 고른다 (`labeling.py:248-256`, 249행 break).
따라서 t0 는 **버퍼 커버리지의 함수**이고, 커버리지는 계속 변한다 —
재시작 시 3일 DB 재적재(`loops.py:993`), 강등→재승격 재적재,
그리고 정상 운영 중 `SymbolBuffer` 의 앞쪽 트림(`MAX_BARS_PER_SYMBOL`, `loops.py:177-181`).

같은 프로세스, 억제기 완전 정상, 버퍼 앞쪽만 트림된 상황:

```
cycle N   : t0_min=60  is_new=True
cycle N+k : t0_min=120 is_new=True   <- 억제기 ALIVE, 그래도 방출 (새 키)
rows for the same pump, same process: 2
detector counters: {'scored': 2, 'events': 2, 'errors': 0, 'suppressed': 0, 'updated': 0}
```

`suppressed=0`. 억제기는 자기가 무슨 일이 일어나는지 모른다.
재시작을 거치면 당연히 더 쉽게 난다:

```
=== t0 shift across restart -> duplicate row ===
pre-restart  emit: t0_min=120 kind=win is_new=True row_id=1
post-restart emit: t0_min= 60 kind=win is_new=True row_id=2
events rows for the SAME pump: 2
```

리플레이 테스트의 행수 불변식(`tests/test_collector_replay.py:146-159`)은 이것을 구조적으로 못 본다 —
중복이 **다른 t0 키** 아래 쌓이는 동안 `rows == uniq` 는 계속 참이다.
W4 의 경고("행 수로는 억제 회귀를 못 잡는다")가 정확히 옳았고, 실제로는 그보다 넓다.

### 재시작이 억제층을 비우는 별도 피해 — 중복 알림과 라벨 퇴행

`state_snapshot`(`loops.py:616-629`)은 `detector._emitted` 를 저장하지 않는다.
재기동 후 버퍼 안의 모든 이벤트가 `is_new=True` 로 다시 방출된다. t0 가 같으면 UNIQUE 가
행은 흡수하지만, **그 외 부작용은 흡수되지 않는다:**

```
post-restart: rows=1 (UNIQUE absorbed) BUT is_new=True detect_lag_min=8 <= 15
              -> announce_event takes the ALERT branch AGAIN (loops.py:476)
  label_revision wiped 1->0, detected_ms overwritten 1784973900000->1784974080000
```

1. **운영자 알림이 다시 울린다** — `EVENT_ALERT_MAX_LAG_MIN=15` 안의 이벤트는 재기동마다 재알림.
   지연 가드는 *오래된* 것만 죽인다.
2. **`detected_ms`/`detect_lag_min` 이 재시작 시각으로 덮어써진다** — 검출 지연 분석이 조용히 오염된다.
   이 프로젝트의 핵심 질문이 "전조를 몇 분 먼저 잡았는가"인데 그 측정값이 망가진다.
3. `counters["events"]` 가 이중 계상된다 (복원된 카운터 + 재검출).

**제안**: (1) 실시간 검출을 당일로 제한, (2) 두 층 중 하나를 **매매일 단위 키**로
(`max_per_day=1` 이 이미 함의하는 바다 — INSERT 전에 같은 (symbol, 매매일) 이벤트를 조회해
`|t0_new − t0_old| <= window_min` 이면 병합), (3) `_emitted` 를 상태파일에 저장하거나
재기동 시 `SELECT symbol, t0_ms FROM events WHERE t0_ms >= now-3d` 로 예열하고 첫 재스캔을
갱신으로 취급, (4) `record_event` 가 **라벨 개정 이력**을 남겨 덮어쓰기를 사후 추적 가능하게.

---

## ⑩ 승격/강등 churn

라이브의 강등 221 / 승격 266 은 **히스테리시스가 작동하지 않아서**가 맞다. 원인이 둘이다.

### H-7 (높음) — 랭킹 승격이 dwell 을 무시하고, 랭킹 점수가 실제 스코어를 압도한다

`TierStateMachine` 은 플래핑 방지를 3겹으로 설계했다 (`detector.py:285-294`):
승격선>강등선 밴드 / 연속 하회 요구 / 변경 후 dwell.
그런데 `_ranking_triggers`(`loops.py:799`)가 부르는 `force()`(`detector.py:357-366`)는
`_change(..., ignore_dwell=True)` 로 **3번째 겹을 건너뛴다.**
랭킹 루프는 12초 주기 × 4종이므로, 강등된 심볼은 **즉시** 다시 올라온다.

```
re-promoted 1ms after demotion: 2       # 강등 1ms 뒤 재승격 성공
```

전체 왕복 재현 (랭킹 12초 / tier2 캔들 110초, 실제 설정값):

```
tier transitions: [(1,2,'ranking_entry'), (2,1,'score_decay'),
                   (1,2,'ranking_entry'), (2,1,'score_decay'),
                   (1,2,'ranking_entry'), (2,1,'score_decay'),
                   (1,2,'ranking_entry'), (2,1,'score_decay')]
promotions=4 demotions=4
```

**"랭킹은 올려라, 스코어는 내려라"가 영구 대립한다.** 두 승격 경로 사이에 중재가 없다.

두 번째 문제는 점수 크기다. `loops.py:798` 의 `score = 0.5 + 0.3*(10-rank)/10` 은 **0.50~0.77** 이다.
실제 `precursor_score`/`confirm_score` 는 현실적으로 0.3~0.6 이다. 즉 랭킹 유래 심볼이
**항상 더 높은 점수를 갖는다.** `EVICTION_MARGIN=0.08` 은 이 격차 앞에서 무의미하고,
`set_capacity` 가 최약체부터 내릴 때 랭킹 유래 심볼이 **가장 마지막에** 밀린다.

```
tier2 before: ['BTAI', 'CRKN', 'SNTI']          # 실제 표적, score 0.40
evicted by ranking-forced large caps: [('SNTI','evicted'), ('CRKN','evicted'), ('BTAI','evicted')]
tier2 after:  ['AMD', 'ASML', 'NOK']            # score 0.80
```

**제안**
1. `force()` 에도 dwell 을 적용한다 (또는 `ranking_entry` 재승격에 쿨다운을 둔다).
2. 랭킹 점수를 **스코어 채널에 섞지 말고** 별도 플래그로 둔다. 랭킹은 "볼 이유"이지
   "얼마나 유망한가"가 아니다. 정원 경쟁·축출 정렬은 실제 스코어로만 해야 한다.
3. 랭킹 승격은 tier2 **진입**만 허용하고, 정원이 찼을 때 기존 멤버를 밀어내지 못하게 한다.

### M-5 (중간) — 세션 경계 일괄 강등 → ⑥ 참조

### tier3 정원(20)에 대하여

**노이즈가 tier3 를 직접 차지하지는 않는다.** `force()` 는 tier 2 까지만 올리고, tier3 진입은
`on_new_data` 의 실제 스코어(≥0.60)를 거친다. 대형주는 실제 스코어가 낮아 tier3 에 못 간다.

**그러나 간접 경로가 결정적이다**: tier3 후보는 **tier2 멤버 중에서만** 나온다
(`run_tier2_candles` 가 `at_least(2)` 만 폴링하고, 그래야 `_detect` → `on_new_data` 가 돈다).
tier2 에서 밀려난 종목은 봉 데이터를 못 받고 → 스코어가 갱신되지 않고 → **tier3 에 영원히 도달할 수 없다.**
즉 tier2 정원 잠식이 곧 tier3 봉쇄다. 위의 축출 재현이 그 경로다.

---

## ⑪ 유니버스 오염

### F-2 (치명) — 필터가 있지만 수집 경로에 한 번도 적용되지 않는다

라이브 로그의 NOK·AMD·ASML·META 는 오작동이 아니라 **설계된 대로의 결과**다.

`_ranking_triggers`(`loops.py:790-801`)는 랭킹 4종의 상위 10위를 그대로 워치리스트에 넣는다:

```python
for row in page.rows:
    if row.rank > RANKING_PROMOTE_TOP:
        continue
    ctx.watch(row.symbol)                       # 필터 없음
    if toss and ctx.tiers.tier_of(row.symbol) < 2:
        score = 0.5 + 0.3 * (RANKING_PROMOTE_TOP - row.rank) / RANKING_PROMOTE_TOP
        ctx.tiers.force(row.symbol, 2, "ranking_entry", score, snap_ms)
```

`ctx.watch`(`:582-591`)가 확인하는 것은 `len(watchlist) >= tier1_max` 뿐이다.
가격·시총·보통주·ACTIVE 검사가 **없다**.

재현:

```
watchlist: ['NOK', 'AMD', 'ASML', 'META', 'SNTI']
tier2 members: ['AMD', 'ASML', 'META', 'NOK', 'SNTI']
```

**더 근본적인 사실**: 필터 자체는 `universe/filters.py::passes_tier0` 에 제대로 구현돼 있고
($0.10–$20, 시총 $10M–$300M, 보통주, ACTIVE, ETF/ETN 제외) `build_universe` 가 그것을 쓴다.
그런데—

```
$ grep -rn "build_universe" --include=*.py . | grep -v tests
./tossmon/universe/build.py:29:async def build_universe(...)
./tossmon/universe/__init__.py:3,9
```

**`build_universe` 는 호출자가 없다.** `__main__` 도, CLI 도, ops 스크립트도, 스케줄 등록도 없다
(`ops/register_task_scheduler.ps1` 포함). 그리고 collector 는 `Reader.symbols()` 를 부르지 않는다
— `symbols` 테이블을 읽는 코드가 수집 경로에 전혀 없다.
collector 의 워치리스트 출처는 `--symbols` CLI 인자와 **랭킹 누적, 둘뿐이다.**
`docs/11` §5 의 `watch=32` 는 전부 랭킹에서 온 것이다.

### 영향 평가

1. **모집단이 반대다.** `MARKET_TRADING_AMOUNT` 상위 10위는 정의상 미국 시장 거래대금 1~10위,
   즉 메가캡이다. 이 전략의 논지는 "동전주 가격대에서 벌어지는 현상"(docs/02 §2.4)인데
   수집 대상이 그 반대편 극단이다.
2. **tier2/tier3 정원 잠식** — H-7 에서 증명한 축출 경로. 그리고 tier2 축출 = tier3 봉쇄.
3. **베이스라인·이벤트 통계 왜곡.** 이벤트 임계(`ret_min=0.15`, `day_ret_min=0.30`, `rvol_min=3.0`)는
   동전주용이다. 대형주는 이 임계를 거의 못 넘으므로 **이벤트가 나지 않는 종목에 예산을 쓴다.**
   반대로 `q6_time_of_day`·기저율 대조 같은 통계는 대형주가 섞인 모집단 위에서 계산된다.
4. **예산 낭비가 정량적이다.** tier2 는 라운드로빈이므로 대형주 1종목당
   CHART 예산의 `1/N` 을 가져간다. 라이브 32종목 중 상당수가 대형주였다.
5. **A4 정밀도 텔레메트리 해석이 틀어진다.** 대형주는 소수 2자리라 `max_digits` 상승 경보의
   기준선이 낮아져 표적 종목군의 반올림을 덜 민감하게 본다.

**제안**
1. `ctx.watch()` 안에서 `passes_tier0` 게이트를 건다. 랭킹에서 처음 보는 심볼은
   `/stocks` + `/prices` 로 메타를 확인한 뒤 통과시킨다 (배치 200 이므로 비용이 작다).
   최소한 **가격 상한**($20)만이라도 즉시 적용하면 메가캡은 전부 걸러진다.
2. `build_universe` 에 진입점을 만들고 일 1회 실행을 ops 스케줄에 등록한다.
   collector 기동 시 `Reader.symbols(tier=1)` 을 워치리스트 시드로 읽는다.
3. 랭킹은 **시드가 아니라 승격 트리거**로만 쓴다 — 이미 유니버스 안에 있는 종목이
   랭킹에 들어왔을 때만 승격한다. 이것이 docs/03 §1 의 깔때기 구조 원래 의도다.
4. 텔레메트리에 `watch_outside_universe` 카운터를 추가해 이 사고가 다시 조용히 일어나지 않게 한다.

---

## §3. 낮음

- **L-1** — `client.py` 의 `_track_rounding` 은 모듈 전역 카운터의 증분을 가져오므로,
  같은 asyncio 루프에서 두 client 가 동시에 정규화하면 귀속이 섞인다.
  주석이 "단일 루프에서 정규화는 동기 구간"이라 안전하다고 하는데 맞다 — 다만 client 가
  둘 이상이 되는 순간 조용히 틀린다. 현재 client 는 1개.
- **L-2** — pre-commit 시크릿 훅이 이 워크트리에 설치돼 있지 않다
  (`git config core.hooksPath` 미설정). 워크트리마다 수동 설치가 필요한 구조라 누락되기 쉽다.
- **L-3** — `tools/live_probe.py:242` 가 1분봉을 `adjusted=true` 로 받는다 (A5 이후 파이프라인이
  쓰지 않는 계열). 보관기간·정밀도 프로브 결론이 실제 수집 계열과 다른 것을 측정한다.
- **L-4** — `baselines.py:56-58`(`true_range_u`), `labeling.py:89-91`(`_rolling_min_close`)이
  pandas 를 거치며 float64 중간값을 만든다. 손실은 2^53 마이크로달러(≈$9.0e9/주) 초과에서만
  가능하므로 실질 피해 없음. 계약 C-2 문자 위반.
- **L-5** — `save_state`(`loops.py:631-643`)가 쓰기 실패해도 `_last_saved_ms` 를 먼저 갱신하므로
  다음 시도가 60초 뒤로 밀린다. 디스크 문제 시 상태 유실 창이 커진다.
- **L-6** — 계약 C-6 은 "쓰기 주체는 collector 단일 프로세스뿐"인데
  `universe/build.py:50,57,90` 과 `store/retention.py:53-54`(CLI, `mode=rw`)도 쓴다.
  WAL + busy_timeout 으로 완화되지만 계약 문구와 다르고 동시 실행을 막는 장치가 없다.

---

## §4. 미확인 — 의심되지만 증명하지 못한 것

> 아래는 **결함이 아니다.** 증거가 부족해 판정을 보류한 것이다. 결함처럼 취급하면 진짜 결함이 묻힌다.

1. **`X-RateLimit-Limit` 의 단위.** H-2 의 심각도는 이 헤더가 초당인지 분당인지에 달려 있다.
   `docs/06_live_facts.md` 에 실측값이 없다. **분당이면 H-2 는 즉시 발현하는 사고이고,
   초당이면 방어 부재로만 남는다.** 다음 라이브 리스 때 헤더 원본을 한 번 찍어 확정할 것.
   (단위와 무관하게 상한 클램프는 필요하다.)

2. **토스 토큰 엔드포인트가 `error_description` 에 자격증명을 에코하는가.** M-1 의 코드 경로는
   증명했지만(서버 문자열이 그대로 로그로 간다), 실제로 시크릿이 담기는지는 라이브 호출 없이
   확인 불가. 잘못된 secret 으로 401 을 한 번 유도하면 확정된다.

3. **토큰 상태파일의 실효 권한.** 측정값은 `0o666` 이었으나 **Windows 에서 `st_mode` 는 NTFS ACL 을
   반영하지 않으므로 이 숫자는 무의미하다.** 실효 ACL 은 확인하지 못했다. POSIX 배포라면
   `mkstemp`(0600) + `os.replace` 로 안전할 것으로 보이나 이것도 검증하지 않았다.
   다중 사용자 호스트로 옮길 계획이 있으면 그때 확인할 것.

4. **`rvol_series` 가 NaN 인 봉의 처리.** `labeling.py:258-259` 는 rvol 계열이 주어졌으나 해당 봉의
   값이 NaN 이면 후보를 **조용히 버리는** 것으로 보인다 (A1 §6 은 "게이트 미가용" 전체 상황만 다룬다).
   실시간은 3일 곡선, 오프라인은 20일 곡선이라 실시간에서만 버려지는 이벤트가 있을 수 있다.
   구성해서 실행하지 않았다.

5. **`market_day_at` 이 None 을 반환하는 경합.** 세션 종료 직전 ≤10분 구간에서 `today()` 폴백이
   잘못된 날을 가리킬 가능성. 코드상 도달 경로를 확정하지 못했다(세션이 CLOSED 가 아니면
   `now` 는 어떤 날의 bounds 안에 있어야 한다). 낮음으로 추정.

6. **정전(전원 차단) 시 WAL 거동.** 프로세스 강제 종료는 안전함을 실증했으나
   (⑥ "확인했고 문제 없던 것"), `synchronous=NORMAL` 이라 **전원이 나가면 최근 커밋된
   트랜잭션 일부가 사라질 수 있다.** 이건 WAL+NORMAL 의 알려진 트레이드오프이지 버그가 아니고,
   손상(corruption)도 아니다. 무인 운영 호스트가 UPS 없이 도는지, 그 손실 폭을 감내할지는
   운영 판단이라 여기서 결론내지 않는다.

7. **`ops_config.yaml` 의 존재 여부.** H-8 의 발현 조건이다. 리포에는 `.example` 만 있다.
   운영 머신에 실제 파일이 있고 `collector_cmd` 가 채워져 있으면 H-8 은 잠복 상태다.

8. **재시작 직후 예산 창 초기화의 실제 영향.** `BudgetGuard` 의 60초 슬라이딩 창이 재기동 시
   비어 있어 잠깐 `usage_ratio` 를 넘길 수 있다. H-3 와 같은 뿌리지만 재시작 경로에서의
   초과 폭은 측정하지 않았다. 낮음으로 추정.

9. **`_atomic_write_json` 이 저장 중 강제 종료되면 `.tmp` 잔여 파일이 남는가.** 실행하지 않았다.
   남더라도 기능 영향은 없고 디스크만 조금 먹는다.

10. **라이브 강등 221 / 승격 266 의 정확한 분해.** H-7(랭킹 플래핑)과 M-5(세션 경계 일괄 강등)가
   각각 몇 건인지는 `promotions` 테이블의 `reason` 별 집계로 확정할 수 있으나,
   그 DB 는 W5 소유라 이번 감사에서 열지 않았다. **먼저 이 집계를 내는 것을 권한다** —
   두 원인의 비중에 따라 수정 우선순위가 달라진다.

---

## §5. Phase 2 착수 전 반드시 해소해야 할 것

심각도 순. 위쪽 3개는 **해소 전 Phase 2 착수 불가**로 본다.

### 반드시 (Phase 2 blocker)

1. **F-2 유니버스 (치명)** — 지금 쌓이는 데이터는 **잘못된 모집단**의 것이다.
   Phase 2 가 이 데이터로 전략을 검증하면 검증 자체가 무의미하다.
   `ctx.watch()` 에 tier0 필터를 걸고, `build_universe` 에 진입점을 만들어 스케줄에 올리고,
   collector 가 `symbols` 테이블을 시드로 읽게 한다. **이미 수집된 야간 데이터는
   대형주를 분리해 재집계하거나 폐기 판단이 필요하다.**

2. **F-3 / ⑨ 룩어헤드 + 중복 (치명)** — 실시간 검출을 현재 매매일로 제한한다.
   이것 하나로 자기오염 재판정, 깨끗한 기록 덮어쓰기, T0 이동 유령 중복이 동시에 사라진다.
   그 다음 두 중복 방어층 중 하나를 다른 키로 바꾼다.
   **Phase 2 백테스트의 라벨 신뢰도가 여기에 걸려 있다.**

3. **F-1 / H-1 토큰 (치명)** — 경로 절대화 + 리스를 자격증명 단위로.
   `invalidate()` 를 CAS 로. `_issue()` 에 AUTH limiter 적용.
   Phase 2 는 주문이 붙는 단계이므로 **토큰이 죽는 순간의 비용이 지금과 비교가 안 된다.**

### 강력히 권고 (Phase 2 전)

4. **H-9 재시작 구멍 탐지** — 지금은 정전이 200 세션분을 넘으면 `candles_1m` 에 구멍이 남고
   **아무 신호도 나지 않는다.** 1분봉은 320~1702일 보관되므로 **탐지만 되면 나중에 메울 수 있다** —
   그래서 우선순위는 "백필을 완벽하게"가 아니라 **"구멍을 반드시 경고하게"** 다.
   `_backfill_1m` 이 `stop_at_ms` 에 못 닿으면 warn, replay 테스트에 구멍 단언 추가.
   Phase 2 는 재시작이 더 잦아지므로 이게 먼저다.
5. **H-6 베이스라인 동결** — 세션 전환 시 `baselines`/`prev_close`/`history_days` 무효화.
   한 줄이고, 안 고치면 다일 운영에서 라벨이 계속 틀린다. F-3 와 같은 뿌리(실시간/오프라인 발산)다.
6. **H-5 `ForbiddenEndpoint` 삼킴** — Phase 2 에 주문 코드가 들어오면 이 가드레일이 유일한
   최종 방어선이 된다. 지금 warn 한 줄인 상태로 그 단계에 가면 안 된다.
   `ForbiddenEndpoint` 를 `TossApiError` 밖으로 옮기는 것이 가장 확실하다.
7. **H-2 / H-3 / H-4 rate limit** — 헤더 상한 클램프, 버킷 capacity 축소, 429 회복을 시간 기준으로.
   세 개가 합쳐져 "usage_ratio 0.7" 이라는 안전 마진이 실제로는 존재하지 않는다.
8. **H-7 churn** — `force()` 에 dwell 적용 + 랭킹 점수를 스코어 채널에서 분리.
   F-2 를 고치면 대형주가 사라져 증상은 줄지만, **플래핑 메커니즘 자체는 남는다.**
9. **H-8 `collector_cmd`** — 한 줄 수정. 무인 운영의 조용한 실패 모드다.
10. **미확인 1번(`X-RateLimit-Limit` 단위) 실측** — 다음 라이브 리스에서 헤더 한 줄만 찍으면
    H-2 의 심각도가 확정된다. 가장 싼 미확인 해소다.
11. **미확인 10번(승격/강등 분해)** — `promotions` 테이블을 `reason` 별로 집계한다.
    H-7 과 M-5 의 비중에 따라 위 8번의 우선순위가 달라진다. W5 소유 DB 라 이번엔 열지 않았다.

### 정리해도 좋은 것 (Phase 2 중)

12. M-2 `iso_to_ms` 반올림 / M-3 겨울 UTC 날짜 분할 / M-4 반일장 곡선 오염
    — **M-3 는 2026-11-01 부터 발현하므로 그 전에는 끝내야 한다.**
13. M-1 시크릿 로그 경로(값 유출은 미확인이나 수정 비용이 거의 없다), M-5 세션 경계 일괄 강등,
    M-8 `_gate` 무언 통과, M-9 계약 문구 정정, M-6 시그니처, M-7 하드코딩 호스트,
    M-10 dryrun 상수, M-11 경로 정규화, M-12 랭킹 공백 경고, L-1~L-7.

---

## 부록 — 감사 실행 환경

- 리포는 읽기만 했다. 실행 검증은 `scratchpad/sandbox` 의 리포 사본에서 했고, 그 안의
  검증 테스트(`test_w6_*.py`)와 재현 스크립트는 **커밋하지 않는다** (코드 수정 금지 원칙).
- 기준선 재확인: 샌드박스에서 `636 passed in 178.96s`.
- 이 워크트리의 Python 환경에는 `filelock` 이 없어 `pytest` 가 수집 단계에서 실패한다.
  (전역 site-packages 의 무관한 `tests` 패키지가 리포의 `tests/` 를 가리는 문제도 있다.)
  운영·CI 환경과 다른 점이라 참고로 남긴다.

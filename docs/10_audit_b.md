# 10-B — 적대적 감사 (W6b, 독립 감사자 B)

> 감사 대상: `main` = `e1ed22d` (636 tests green, 재확인함 — `636 passed in 145.51s`).
> 방식: **반증 지향**. "맞다"를 확인하지 않고 "어떤 입력·타이밍·순서에서 틀리는가"를 찾았다.
> 라이브 호출 없음. 검증은 전부 mock(`127.0.0.1:8899`) 또는 순수 함수 재현.
> 재현 스크립트는 스크래치패드에서만 실행했고 **커밋하지 않았다**(코드 수정 금지 원칙).
> 산출물은 이 파일 하나다. 다른 감사자의 결과물(`docs/10_audit.md`, `w6-audit` 브랜치)은 열지 않았다.

**심각도 기준**: "이 결함이 실현되면 무엇을 잃는가". **조용히 틀리는 것 > 시끄럽게 죽는 것.**

| 등급 | 뜻 |
|---|---|
| 치명 | 되돌릴 수 없다. 라이브 수집 전체가 끊기거나 데이터가 복구 불가능하게 오염된다 |
| 높음 | 조용히 틀린 데이터가 쌓인다. 또는 야간 수집이 통째로 멈춘다 |
| 중간 | 측정값이 편향되거나 예산/커버리지가 계획과 다르게 소모된다 |
| 낮음 | 문서·방어선 위생. 지금 손해는 없지만 다음 사고를 못 막는다 |
| 미확인 | 의심되지만 증명하지 못했다 → §12 로 분리 |

---

## 0. 요약

확정 결함 **19건** (치명 1 / 높음 7 / 중간 8 / 낮음 3), 미확인 **7건**.
"뚫리지 않음"을 확인한 항목도 명시했다 — 음성 결과도 정보다(§3-C, §7-G5, §8).

가장 무거운 셋:

1. **A-1 [치명]** 토큰 리스가 **CWD 종속**이다. 서로 다른 작업 디렉터리에서 뜬 두 컬렉터는
   각자 다른 락 파일을 잡고 **둘 다 토큰을 발급**한다 → 상호 살해. 지금 이 프로젝트가 쓰는
   멀티 worktree 배치가 정확히 그 조건이다.
2. **C-1 [높음]** 승격 직후·세션 재개 직후에 **RVOL 게이트가 조용히 꺼진다**. 곡선이 `None`
   이 되어 `detect_events` 가 가격 조건만으로 이벤트를 낸다. `events` 테이블과 알림 채널이
   게이트 미적용 이벤트로 오염된다.
3. **K-1 [높음]** 랭킹 출현 종목 누적 경로에 **유니버스 필터가 없다.** NOK·AMD·ASML·META 는
   버그가 아니라 이 코드 경로의 **설계된 동작**이다. 워치리스트는 단조 증가하고 상태파일로
   재시작을 넘어 영속한다.

---

## 1. ① 토큰 단일성 파괴 경로

### A-1 [치명] `token_state_path` 가 상대경로라 리스가 CWD 종속이다

**근거**
- `config/config.example.yaml:8` — `token_state_path: "data/token_state.json"` (상대경로)
- `tossmon/config.py:161-165` `_as_path()` — `Path(raw)` 만 한다. `resolve()`·`앵커 기준 결합` 없음
- `tossmon/api/tokens.py:43` — `self._lock_path = self.state_path.with_suffix(... + ".lock")`
- `tossmon/api/tokens.py:95-107` `_acquire_lease()` — `filelock.FileLock(str(self._lock_path))`
- `ops/supervisor.py:84-88` — `subprocess.Popen(self.cfg.collector_cmd, ...)` 에 **`cwd=` 인자가 없다.**
  자식은 supervisor 의 CWD 를 상속한다. `ops/ops_config.example.yaml` 의
  `collector_cmd`·`db_path`·`state_dir` 도 전부 상대경로다.

**재현 시나리오**
> worktree A(`.../w6-audit`)와 worktree B(`.../w5-live`)에 각각 `config/config.yaml`(`live: true`)이
> 있다. 두 곳에서 `python -m tossmon.collector` 를 띄운다. A 는
> `.../w6-audit/data/token_state.json.lock` 을, B 는 `.../w5-live/data/token_state.json.lock` 을
> 잡는다. **두 락은 서로 다른 파일이므로 둘 다 성공한다.** 둘 다 `POST /oauth2/token` 을 하고,
> 두 번째 발급이 첫 번째 토큰을 즉시 죽인다. A 의 모든 요청이 401 → `invalidate()` → 재발급 →
> B 의 토큰 사망 → 무한 상호 살해. `docs/08` 의 "리스 보유자 1명" 규율은 **사람의 약속일 뿐
> 코드가 강제하지 않는다.**

**실측 (mock 서버, `live=True`)**
```
E1 두 TokenManager 가 동시에 리스를 보유:
   procA: token='mock-access-token' lock=...\e1_42yid2k5\procA\data\token_state.json.lock
   procB: token='mock-access-token' lock=...\e1_42yid2k5\procB\data\token_state.json.lock
   -> 서로 다른 락 파일: True
```
(같은 상대경로 `data/token_state.json` 을 준 두 TokenManager 가 CWD 만 다르면 둘 다 리스를
획득하고 둘 다 실발급까지 도달했다.)

**왜 치명인가** `tokens.py:8-9` 의 주석이 스스로 밝힌 대로 "발급을 강행하면 그 프로세스의
토큰이 즉시 죽는다". 이 결함은 그 강행을 **막지 못하는 조건**을 만든다. 그리고 이건 되돌릴 수
없다 — 밤새 수집이 통째로 빈다.

**제안**
1. `TokenManager.__init__` 에서 `self.state_path = Path(state_path).resolve()` 로 정규화.
   그것만으로는 worktree 가 다르면 여전히 갈라지므로,
2. **락 파일을 리포 밖 고정 경로**(예: `%LOCALAPPDATA%/tossmon/token.lease`)로 옮기거나,
   `config` 에 `token_lease_path` 절대경로 키를 추가한다(계약 C-9 변경이므로 `ask` 필요).
3. 락을 잡을 때 상태파일에 **보유자 지문**(pid + `Path.cwd()` + 시작 시각)을 남기고,
   기동 시 다른 보유자 지문이 살아 있으면 경보 후 종료한다. 락만으로는 경로 분기를 못 잡는다.

---

### A-2 [높음] `invalidate()` 에 compare-and-swap 이 없다 — 뒤늦은 401 이 방금 발급한 토큰을 죽인다

**근거** `tossmon/api/tokens.py:69-78`
```python
async def invalidate(self) -> None:
    async with self._alock:
        self._token = None
        self._expires_at_ms = 0
        if self.live:
            try: self.state_path.unlink()
```
어떤 토큰이 401 을 맞았는지 확인하지 않고 **무조건** 상태를 버린다.
`tossmon/api/client.py:111-117` 의 AuthExpired 분기가 이걸 호출한다.

**재현 시나리오**
> `run_all` 은 5 개 루프를 **하나의 TossClient/TokenManager 위에서 동시에** 돌린다
> (`loops.py:1185-1195`). 루프 1 이 토큰 T0 로 보낸 요청이 네트워크에서 지연되는 동안
> 루프 2 가 401 을 받아 재발급해 T1 을 얻는다. 그 뒤 루프 1 의 지연된 응답이 401 로 돌아온다.
> 루프 1 은 `invalidate()` 를 호출해 **유효한 T1 의 상태파일을 지우고**, 다음 `get()` 이 T2 를
> 발급한다 → T1 사망 → 루프 2 의 다음 요청이 401 → ... 401 한 건이 폭풍의 씨앗이 된다.

**실측**
```
E2 issues=2  t1=mock-access-token#1  t2=mock-access-token#2  같은 토큰인가=False
E3 동시 401 5건 -> 발급 2회, 최종 토큰 ['mock-access-token#1', 'mock-access-token#2']
```
E3 에서 **두 개의 서로 다른 토큰이 동시에 루프들 사이에 살아 있다.** `#1` 을 든 루프는 다음
요청에서 반드시 401 을 맞고, `auth_retried >= 1` 이라 그대로 예외로 빠져나간다.

**제안** `invalidate(token: str | None = None)` 로 확장해, 상태파일의 토큰이 인자와 다르면
아무것도 하지 않는다(ABA 방지). `client._send` 는 자기가 실제로 쓴 토큰을 기억했다가 넘긴다.
계약 C-4 의 시그니처 확장이므로 `ask` 필요.

---

### A-3 [높음] `TOSS_LIVE` 미설정이면 yaml 이 이긴다 — 계약 C-9 와 어긋난다

**근거**
- 계약 `docs/04_contracts.md:348` — "`TOSS_LIVE` env: **`1`이 아니면** TokenManager가 실발급을 거부"
- 구현 `tossmon/config.py:318-320`
  ```python
  live = env.get(ENV_LIVE)
  if live is not None:            # ← env 가 없으면 아무것도 안 한다
      api["live"] = live.strip() == "1"
  ```

**재현 시나리오**
> 리스 없는 워커가 W5 worktree 에서 `config/config.yaml`(`live: true`)을 복사해 온다
> (`config/config.yaml` 은 gitignore 라 각 worktree 가 자기 것을 갖는 게 정상이다).
> `TOSS_LIVE` 를 설정하지 않고 `python -m tossmon.collector` 를 실행 → **라이브로 뜬다.**
> A-1 과 결합하면 그 즉시 W5 의 토큰이 죽는다.

코드 주석(`config.py:316-317`)은 "TOSS_LIVE=0 으로 막을 수 있어야 한다"는 **양방향** 오버라이드를
의도로 밝히고 있다. 의도는 이해되지만 계약 문구는 fail-closed 를, 구현은 fail-open 을 택했다.
토큰 살해가 되돌릴 수 없는 실패인 이상 fail-closed 가 맞다.

**제안** 둘 중 하나로 계약과 구현을 일치시킨다(코디네이터 결정 필요):
(a) `live = env.get(ENV_LIVE); api["live"] = (live is not None and live.strip() == "1")`
    — env 미설정 = 라이브 금지. 계약 문구 그대로.
(b) 계약 C-9 문구를 "env 가 있으면 양방향 오버라이드"로 개정.
(a) 를 권한다. 실수의 방향이 한쪽으로만 열려 있어야 한다.

---

### A-4 [중간] 토큰 발급이 limiter 를 통과하지 않고 raw httpx 를 쓴다

**근거** `tossmon/api/tokens.py:172-184` — `check_allowed("POST", TOKEN_PATH)` 는 하지만
`limiter.acquire("AUTH")` 는 없고, `httpx.AsyncClient(timeout=15.0)` 은 `_GuardedTransport`
가 아니다. `AUTH` 공시 한도는 5 req/s(`endpoints.py:50`).

**재현 시나리오** A-2 의 폭풍이 발생하면 N 개 루프가 각자 `_issue()` 를 호출하고, 그
POST 들은 **어떤 rate 제어도 없이** 연속 발사된다. 429 를 맞으면 `TransientHTTP(429, ...)`
로 올라가 `_request` 의 transient 분기가 다시 재시도한다.

**제안** `_issue()` 진입 전에 `await limiter.acquire("AUTH")`. 경로가 상수라 allowlist 우회
위험은 없지만, transport 이중 차단의 **유일한 예외**라는 사실은 코드 주석에 명시할 값어치가 있다.

---

## 2. ② rate limit 초과 경로

### B-1 [높음] 토큰버킷 용량 == rate ⇒ 유휴 직후 첫 1초에 목표의 2배가 통과한다

**근거** `tossmon/api/limiter.py:32-34`
```python
self.rate = max(rate, 1e-3)
self.capacity = max(rate, 1.0)      # capacity == rate
self.tokens = self.capacity         # 시작부터 가득
```
용량 C, 충전율 R 인 버킷은 시간 t 에 `C + R·t` 개를 통과시킨다. `C == R` 이므로
t=1s 에서 **2R**. MARKET_DATA 는 R=7.0 → 1초에 14 회. 공시 한도는 10/s 다.

**실측**
```
E4 유휴 후 첫 1.0초 통과 요청 = 14 (공시 한도 10/s, 목표 7/s, capacity=7.0)
```

**재현 시나리오** (유휴는 예외가 아니라 **정상 운용**이다)
> 세션이 닫히면 `_loop` 은 `ctx.collecting()` 이 False 라 `IDLE_SLEEP_S` 로만 돈다
> (`loops.py:748-752`) → MARKET_DATA 버킷은 몇 시간 동안 아무도 안 쓴다 → tokens 가 capacity
> 로 가득 찬다. 세션이 열리는 순간 tier1 스윕 + tier3 의 40 개 due 항목(20 심볼 × trades/book,
> `loops.py:1077-1092`)이 **한꺼번에** 준비 상태가 되고, 첫 7 건이 즉시 나간 뒤 충전분 7 건이
> 뒤따른다 → 그 1 초에 14 회. 공시 10/s 를 40% 초과 → 429.

**제안** `capacity` 를 `max(1.0, rate * burst_frac)` (예: `burst_frac=0.3`) 로 낮추거나,
버킷을 빈 상태(`tokens = 0`)로 시작한다. 세션 재개 시 `blocked_until = now + 1/rate` 로
워밍업을 넣는 것도 가능하다. **429 는 사고**라는 프로젝트 원칙상 버스트 여유를 남길 이유가 없다.

---

### B-2 [높음] 헤더 자기보정이 한도를 **위로** 무제한 끌어올린다

**근거** `tossmon/api/limiter.py:107-113`
```python
if limit is not None and limit > 0:
    target = limit * self.usage_ratio
    ...
    b.rate = max(target, 1e-3); b.capacity = max(target, 1.0)
    self.limits[group] = limit          # 영구 기록
```
`Remaining` 은 주석대로 **아래로만** 반영하는데(`b.tokens = min(...)`, 116-118행),
`Limit` 은 상한 없이 **양방향** 채택한다. 공시값(`SPEC_LIMITS`)과의 대조도 없다.

**실측**
```
E5 rate 7.0 -> 420.0 (limits[MARKET_DATA]=600.0)
```
`X-RateLimit-Limit: 600` 헤더 한 줄로 유효 rate 가 60 배가 됐다.

**재현 시나리오**
> `docs/06 §9-1` 실측상 현재 `x-ratelimit-limit` 은 **초당** 한도(10/3)다. 서버가 이 헤더의
> 의미를 분당 쿼터(600/min = 10/s, 같은 뜻)로 바꾸는 것은 API 에서 흔한 변경이고 **응답
> 스키마는 그대로다.** 그 순간 limiter 는 420 req/s 를 허용하고, 즉시 429 폭풍 →
> `on_429` 백오프는 걸리지만 `update_from_headers` 가 성공 응답마다 백오프를 0.8 배씩 풀고
> (`limiter.py:123-124`) rate 는 여전히 420 이라 회복되지 않는다.

**왜 높음인가** BudgetGuard 는 `self.limits` 사본을 따로 들고 있어(`budget.py:115`) 오염되지
않고 티어 축소를 지시하겠지만, 축소 대상은 tier2/tier3 심볼 수뿐이다. 랭킹 루프는 축소 대상이
아니고(`budget.py:228-232`), limiter 가 문을 열어둔 이상 초과는 계속된다.

**제안** 채택값에 상한을 건다: `limit = min(limit, SPEC_LIMITS.get(group, DEFAULT_LIMIT))`
— 즉 **서버값은 한도를 내리는 데만 쓴다**. 공시값보다 큰 값이 오면 채택하지 말고 경보한다
(`Remaining` 처리와 대칭이 맞다). 코드 상단 주석의 "공시값과 다르면 서버값을 채택한다"는
규약 자체를 재검토 대상으로 본다.

---

### B-3 [높음] 재시도·실패 호출이 BudgetGuard 에 **한 번도** 계상되지 않는다

**근거**
- `budget.py:8-10` 은 이렇게 주장한다: "실사용 관측(`on_request`) — ... **재시도·백필처럼
  계획에 없는 호출이 예산을 먹는 경우를 이쪽이 잡는다.**"
- 그런데 `on_request` 의 유일한 호출자는 `loops.py:375` `ctx.after_call()` 이고,
  `after_call` 은 **client 호출이 성공적으로 반환된 뒤** 루프가 부른다.
- `client.py:107-133` `_request` 는 한 번의 논리 호출 안에서 최대
  **1(auth) + 1(429) + 3(transient) = 5 회**의 HTTP 요청을 낼 수 있다. 전부 1 회로 계상된다.
- 재시도까지 실패해 예외로 빠져나가면 `after_call` 이 아예 실행되지 않는다
  (`_guarded` 의 `finally` 는 `sync_rate_limits` 만 부른다, `loops.py:739-741`) →
  **0 회로 계상**.

**실측**
```
E11 after_call 이 client.counters['retries'] 를 읽는가: False
    _request 재시도 분기가 budget 을 건드리는가: False
```

**재현 시나리오**
> 5xx 가 잦은 시간대. 모든 MARKET_DATA 호출이 3 회 재시도 끝에 성공한다. 실제 HTTP 는
> 초당 ~20 회인데 `measured_rate` 는 7 회로 보인다 → `should_shrink()` 는 아무것도 안 한다 →
> "한도 70% 초과 예측 시 자동 축소"라는 C-8 방어선이 **가장 필요한 순간에 눈을 감는다.**
> 백필은 페이지마다 `after_call` 을 부르므로 계상되지만(`loops.py:1019`), 재시도는 아니다.

**제안** `after_call` 대신 `TossClient` 가 자기 전송 지점(`_send` 진입)에서 콜백으로
`budget.on_request(group)` 을 부르게 한다. 그러면 재시도·실패 호출까지 정확히 1:1 로 잡힌다.
지금은 회계 주체(루프)와 발신 주체(client)가 달라 구조적으로 어긋난다.

---

### B-4 [중간] churn 이 계획에 없는 CHART 백필을 반복 유발한다

**근거** `loops.py:566-579` — 강등 시 `to_tier < 2` 면 `drop_symbol_state()` 가 버퍼·곡선을 버린다.
`loops.py:962-976` `_ensure_history` 는 버퍼가 비면 다시 DB 로드 + (부족하면) 최대
`MAX_BACKFILL_PAGES=3` 페이지 API 백필을 한다.

**실측 (세션 종료 시 강등 규모)**
```
E10 closed 전환: tier2+ 40->2, tier3 10->1, 강등 47건 (scale=0.0, caps={'tier2_max': 1, 'tier3_max': 1})
```
`SESSION_TIER_SCALE[CLOSED] = 0.0` (`loops.py:117-118`) → `reconfigure_tiers` 가 정원을
1/1 로 만들어 tier2·tier3 를 **통째로** 비운다. 하루 세션 전환은 4~5 회다.

**재현 시나리오**
> 정규장 종료 → 47 건 강등, 47 개 심볼의 버퍼 소멸 → 애프터마켓 개장 → 랭킹 트리거가
> 다시 승격 → 각 심볼이 `_ensure_history` 에서 최대 3 페이지 백필. `config.example.yaml:62-66`
> 이 확보해 둔 CHART 여유는 **0.77 req/s(22%)** 뿐이고, 그 여유의 용도로 명시된 것이 바로
> 이 "승격 직후 백필"이다. 라이브 관측 승격 266 회는 이 예산으로 감당할 수 없다 →
> limiter 가 대기시키고 → tier2 라운드로빈이 밀리고 → `measured_rate` 가 headroom 을 넘어
> BudgetGuard 가 tier2 를 **또** 줄인다 → 커버리지가 조용히 무너진다.

**제안** 강등 시 버퍼를 버리지 말고 (a) 상한이 걸린 LRU 로 유예 보관하거나,
(b) `SESSION_TIER_SCALE[CLOSED]` 를 정원 축소가 아니라 **폴링 중지**로 구현한다
(정원을 건드리지 않으면 강등도, 재백필도 없다). 지금 구조는 "예산을 아끼려고 정원을 줄였는데
그 결과 예산을 더 쓴다".

---

### B-5 [중간] `tools/live_probe.py` 는 **기본 실행만으로** 라이브 429 를 의도적으로 유발한다

**근거** `tools/live_probe.py:634-651` `probe_force_429` — raw `httpx.AsyncClient` 로
limiter·allowlist 를 **둘 다** 우회해 `/exchange-rate` 를 12 회 연속 때린다.
`tools/live_probe.py:768-785` `PROBES` 에 `"force429"` 가 등록돼 있고,
`:793` `names = list(PROBES) if args.probe == "all"`, `:855` `--probe` 의 **기본값이 `"all"`** 이다.

**재현 시나리오**
> 다음 라이브 리스 보유자가 `docs/06 §11` 의 미확인 항목을 채우려고
> `python tools/live_probe.py --keys api_keys` 를 인자 없이 실행한다 → `force429` 가 돌아
> MARKET_INFO 그룹에 의도적 429 를 만든다. 같은 시각 컬렉터가 돌고 있으면 그 그룹의 페널티를
> 공유한다(같은 client 자격증명 = 서버 측 같은 버킷).

**제안** `force429` 를 `PROBES` 에서 빼고 `--probe force429` 로 **명시했을 때만** 실행되게
한다(`ALL_PROBES = [n for n in PROBES if n != "force429"]`). 그리고 raw httpx 사용부에
`check_allowed()` 를 넣어 이중 차단의 예외를 없앤다.

---

## 3. ③ 룩어헤드 편향

### C-1 [높음] 승격 직후·세션 재개 직후 **RVOL 게이트가 조용히 꺼진다**

**근거**
- `loops.py:949-959` `_curve_for` → `detector.build_curve(df, calendar, exclude_dates=(today,))`
- `baselines.py:196-217` `minute_of_session_volume_curve` — `md.date in skip` 이면 continue,
  그리고 **`if not any(minute_vols): continue`** (해당 날짜 세션에 봉이 하나도 없으면 제외)
- ⇒ 버퍼에 **오늘 봉만** 있으면: 오늘은 `skip` 으로 제외, 이전 날들은 봉이 없어 제외 →
  `day_cnt` 가 비어 curve 가 empty → `build_curve` 가 `None` 반환 (`detector.py:684`)
- `detector.py:596-598` — `rv = None` 이 그대로 `detect_events(rvol_series=None)` 로 간다
- `labeling.py:245-259` — `gated = rv_map is not None` → **False** → RVOL 조건을 건너뛴다

**실측**
```
E6 build_curve(오늘 봉만, 오늘 제외) = None
   curve 없음 -> detect_events 결과 1행, rvol_gated=[False]
```
(오늘 120 봉만 있고 100 번째 봉에서 +20% 급등하는 합성 데이터. RVOL 3배 조건 없이 이벤트가 났다.)

**재현 시나리오** (희귀 경로가 아니라 **정상 경로**다)
> 새 심볼이 랭킹으로 승격된다 → DB 에 그 심볼 이력이 없다 → `_backfill_1m` 이 최대 3 페이지
> (=200 봉 × 3, 거래 없는 분은 봉이 없으므로 실제 커버 구간은 종목마다 다르다)를 가져온다 →
> 저유동성 동전주면 3 페이지가 하루를 못 넘길 수도, 며칠을 덮을 수도 있다. **하루를 못 넘기면
> 곡선이 `None`** 이다. 게다가 `_curve_for` 는 `ctx.curves[symbol] = (now_ms, None)` 로
> `None` 을 **캐시**하고 `CURVE_TTL_MS = 1시간` 동안 재시도하지 않는다(`loops.py:951-958`).
> ⇒ 승격 후 최소 1 시간 동안, 그 종목의 이벤트는 전부 **가격 조건만으로** 잡힌다.
> `event_ret_min=0.15`(30 분 +15%)는 동전주에서 RVOL 게이트 없이는 흔한 사건이다.

**왜 높음인가**
- 계약 A1 §6 이 `rvol_gated=False` 를 허용하고 `evaluate._gate` 가 기본 제외하므로 **분석
  단계는 방어된다.** 그러나 방어되지 않는 것이 셋 있다:
  1. `events` 테이블에 게이트 미적용 행이 실제로 쌓인다(사후 필터링에 의존).
  2. `announce_event`(`loops.py:452-487`)는 `rvol_gated` 를 **보지 않는다** → 무인 야간 운영의
     `alert` 채널이 오탐으로 오염된다. "alert 는 사람이 반드시 봐야 하는 것만"이라는 설계 취지가 깨진다.
  3. I-1 참조 — 게이트가 나중에 켜지면 **같은 급등에 대해 t0 가 이동해 중복 행이 생긴다.**

**제안**
1. `_curve_for` 에서 `None` 을 캐시하지 않는다(성공한 곡선만 TTL 캐시).
2. `EventDetector.evaluate` 가 곡선 없이 이벤트를 냈으면 **info 로만** 기록하고 `alert` 하지 않는다.
3. `_ensure_history` 가 곡선을 만들 수 있을 만큼(최소 이전 1 매매일) 이력을 확보하기 전에는
   `detect_events` 를 호출하지 않는 것도 선택지다 — "데이터가 없을수록 보수적"이라는
   `detector.py:23-24` 의 자기 규약과 일관된다.

---

### C-2 [높음] `prev_close`·`baseline` 이 **한 번만** 계산되고 영원히 갱신되지 않는다

**근거**
- `loops.py:962-976` `_ensure_history` — `if ctx.baselines.get(symbol) is None: await _refresh_baseline(...)`
- `loops.py:1049-1050` — `ctx.baselines[symbol] = ...`, `ctx.prev_close[symbol] = ...`
- **어디에서도 `baselines`/`prev_close` 를 pop 하거나 clear 하지 않는다.**
  `drop_symbol_state`(`loops.py:569-579`)는 `buffers`·`curves` 만 버린다.

**실측**
```
E13 baselines/prev_close 쓰기 지점: ['ctx.baselines[symbol] = compute_daily_baseline(candles_frame(rows))',
                                     'ctx.prev_close[symbol] = int(rows[-1].close_u)']
    비우는 지점: (없음)
```

**재현 시나리오**
> 심볼 X 가 월요일에 승격된다 → `prev_close` = 금요일 종가로 고정. 프로세스가 죽지 않고
> 목요일까지 돈다. 목요일의 `day_ret = close/금요일종가 - 1` 이다. 그 사이 X 가 +25% 누적
> 상승했다면 목요일 개장가만으로 이미 `event_day_ret_min=0.30` 에 근접하고, 당일 +5% 만 더
> 오르면 **`kind='day'` 이벤트가 허위로 발생**한다. 반대로 X 가 하락 추세였다면 진짜 +30% 날을
> **놓친다**. `atr20_pct`·`daily_vol_z`·`gap_from_prev_close` 도 같은 기준선에 매달려 있다.
> (역설적으로 어젯밤의 계획 외 재시작 2 회는 인메모리 baseline 을 지워서 이 결함을 **가려줬다.**
>  무중단 운영이 길어질수록 오염이 커지는 구조다.)

**제안** `_refresh_baseline` 에 갱신 조건을 붙인다: `baselines[symbol]` 에 계산 시각을 함께
저장하고, `scheduler.market_day_at(now).date` 가 바뀌면 재계산. `reconfigure_tiers` 가
`ctx.curves.clear()` 하는 지점(`loops.py:1172`)에서 `baselines`/`prev_close` 도 같이
무효화하는 것이 가장 작은 수정이다 — 곡선과 완전히 같은 이유로 "날이 바뀌면 다시 만든다".

---

### C-3 [뚫리지 않음] W3 가 경고한 "베이스라인에 이벤트 당일 포함" 은 실제 호출 경로에서 **발생하지 않는다**

반증을 시도했고 실패했다. 명시해 둔다:

| 경로 | 확인 결과 | 근거 |
|---|---|---|
| RVOL 분모 곡선 | 당일 제외됨 | `loops.py:955-957` 이 `exclude_dates=(today.date,)` 를 넘기고, `baselines.py:199-200` 이 그 날짜의 세션을 평균에서 뺀다(윈도우 등록만 유지) |
| 일봉 베이스라인 | 당일 봉 잘림 | `loops.py:1043-1046` `exclude_today_1d_cutoff` = 정규장 시작 −24h. 서머타임 ±1h 로는 뒤집히지 않는다(`scheduler.py:104-106` 의 논증이 맞다) |
| 피처 컷오프 | 엄격 | `features.py:50-56` `cut_frame` — `include_t0=False` 면 `ts < t0`, `True` 면 `ts <= t0`. 랭킹도 `snap_ms` 로 동일 적용(`features.py:173`) |
| 실시간 `include_t0=True` | 계약 준수 | 계약 A1 §1 이 W4 실시간 경로에 명시 허용. `detector.py:589-592` 가 자체 구현 없이 이 플래그를 쓴다 |

다만 `rvol_series(pre, curve)` 의 분모(`cum_curve`)가 **1~2 일 평균**뿐인 경우가 흔하다
(버퍼가 3 일치, 당일 제외) — 룩어헤드는 아니지만 RVOL 값의 분산이 매우 크다. §12 미확인 참조.

---

## 4. ④ 시간대·서머타임·세션 경계

### D-1 [중간] "UTC 날짜 = 매매일" 가정이 **미국 표준시(EST) 기간에 깨진다**

**근거**
- `features.py:92-102` `_locate_day_start` 폴백 — `(cutoff // 86_400_000) * 86_400_000`
  주석: "토스 4세션은 UTC 00:00~22:00 에 들어가므로 UTC 날짜 = 매매일"
- `labeling.py:11-12`, `:77-79` `_day_spans` 폴백 — 동일 가정
- **폴백이 아닌 곳에서도 쓴다**: `features.py:479` `hist_days_available`,
  `:489-493` former-runner 프록시가 `calendar` 유무와 무관하게 `t // day_ms` 로 버킷을 나눈다

**실측**
```
E8 2026-07-30: after 종료 19:50 ET = 2026-07-30 23:50:00+00:00 (UTC 날짜 2026-07-30)
E8 2026-12-15: after 종료 19:50 ET = 2026-12-16 00:50:00+00:00 (UTC 날짜 2026-12-16)
```
여름(EDT)에는 하루가 UTC 한 날짜에 들어가지만, **겨울(EST)에는 애프터마켓이 UTC 자정을 넘는다.**

**재현 시나리오**
> 2026-11-01 이후. 어떤 심볼의 애프터마켓 봉(21:00~01:00 UTC)이 두 UTC 날짜로 쪼개진다.
> - `hist_days_available` 이 실제보다 크게 나온다.
> - former-runner 프록시(`prior_event_count_20d`)가 "00:00~00:50 UTC 조각"을 독립된 하루로 보고
>   그 날의 저가→고가 상승률을 계산한다 → 50 분짜리 표본이 20 일 이벤트 카운트에 섞인다.
> - `calendar` 없이 `detect_events` 를 부르는 경로(연구용 오프라인 백필)에서는 매매일 자체가
>   두 조각으로 갈라져 `max_per_day=1` 이 **하루에 2 건**을 허용한다.
>
> 오늘(2026-07-30)은 EDT 라 어젯밤 리허설에서는 **원리상 드러날 수 없었다.**

**제안** UTC 날짜 버킷팅을 전부 `calendar` 기반 매매일 경계로 바꾼다. `calendar` 가 없으면
`hist_days_available` 등을 NaN 으로 두는 편이 조용히 틀리는 것보다 낫다
(`detector.py:23-24` 의 "미가용은 0점" 규약과 같은 정신). 최소한 주석의 단정
("UTC 날짜 = 매매일이 성립한다")을 "**EDT 기간에만** 성립한다"로 고쳐야 한다.

---

### D-2 [중간] `TierStateMachine.last_ts_ms` 가 **두 개의 시계**를 섞어 담는다 → 정상 폴링 중인 종목이 stale 강등된다

**근거**
- 벽시계를 쓰는 곳: `force()` ← `_ranking_triggers`(`loops.py:799`, `snap_ms`),
  `_on_price`(`loops.py:879`, `now_ms`), `seed()`(`loops.py:661`, `saved_ms`)
- **봉 시각**을 쓰는 곳: `on_new_data(symbol, result.score, result.ts_ms)`(`loops.py:936`)
  — `result.ts_ms` 는 `df_1m["ts_ms"]` 의 마지막 값, 즉 **마지막 완성봉 시각**(`detector.py:587`)
- 비교하는 곳: `sweep(now_ms)`(`detector.py:370-383`) — `cutoff = now_ms - stale_demote_s*1000`,
  호출자는 `ctx.clock.now_ms()`(`loops.py:822`) = **벽시계**

**실측**
```
E9 폴링 성공 중인데 stale 강등? [('DORM', 2, 1, 'stale')]
```
(tier2 심볼을 1 시간 전 승격 → 폴링은 계속 성공하지만 12 분간 체결이 없어 마지막 봉이 12 분 전 →
`stale_demote_s = tier2_candle_s × 6 = 660s = 11분` 초과 → 강등.)

**재현 시나리오**
> `docs/06 §1-1` 이 실측한 그대로: 소형주는 프리마켓에 체결이 없어 봉이 아예 안 생긴다
> (`함정5`). tier2 로 올라온 동전주가 11 분간 조용하면 **폴링이 정상인데도** `stale` 로 강등되고,
> 버퍼가 버려지고(B-4), 다음 랭킹 스냅샷에서 다시 승격되어 재백필한다.
> 계약 A2 §3 은 "조용하던 동전주가 깨어나는 순간을 잡는 것이 이 전략의 핵심"이라고 못박았는데,
> 이 로직은 **깨어나기 직전의 종목을 정확히 골라 내쫓는다.**

**제안** stale 판정에 쓸 필드를 분리한다: `last_data_ms`(폴링 성공 시각, 벽시계) 와
`last_bar_ms`(마지막 봉, 시장 시각). `sweep` 은 `last_data_ms` 를 본다 — "수집 실패·거래 정지"
를 잡겠다는 원래 의도(`loops.py:114-115`)에 맞는 건 그쪽이다.

---

### D-3 [중간] 세션 전환마다 tier2/tier3 가 통째로 비워진다

**근거** `loops.py:117-118` `SESSION_TIER_SCALE = {..., CLOSED: 0.0}`,
`loops.py:1165-1170` — `scale > 0` 이 아니면 caps 를 **1** 로 만든다.
`detector.py:385-402` `set_capacity` 는 `ignore_dwell=True` 로 초과분을 전부 강등한다.

**실측** E10 — tier2+ 40→2, tier3 10→1, **강등 47 건이 한 틱에**.

이것이 ⑩ churn 의 최대 단일 원인으로 보인다(§10 참조). 하루 4~5 회 세션 전환 × 정원 전체
= 라이브 관측 "강등 221 / 승격 266" 의 상당 부분을 설명한다.

---

## 5. ⑤ 시크릿 유출 경로

**확인 결과: 유출 경로를 하나도 증명하지 못했다.** 반증 시도 내역:

| 의심 경로 | 판정 | 근거 |
|---|---|---|
| `_read_keys` 예외 메시지 | 안전 | `tokens.py:157-160` — `sorted(data)` 는 **키 이름만**. 값은 담기지 않는다 |
| OAuth2 에러 응답 | 안전 | `tokens.py:192-203` — `body.get("error")`/`error_description` 만 읽는다 |
| 일반 에러 envelope | 안전 | `client.py:374-384` `_err_code` — `error.code`/`error.message` 만 |
| Notifier | 안전 | `notifier.py` 는 문자열을 받아 그대로 로깅. 시크릿을 넘기는 호출부 없음(grep 확인) |
| `client.last_headers` | 안전 | **응답** 헤더다. `telemetry()` 에 포함되지 않는다 |
| live_probe 픽스처 | 안전 | `live_probe.py:59-60` `MASK_FIELDS`/`MASK_HEADERS` 에 `access_token`·`authorization`·`requestId` 포함 |
| 커밋 | 1차 방어선만 작동 | `.gitignore` 에 `api_keys`, `config/config.yaml`, `data/token_state.json`, `data/`, `*.log` 전부 있음 |

### E-1 [낮음] pre-commit 시크릿 스캐너가 **설치돼 있지 않다**

**근거** 이 worktree 에서:
```
$ git config core.hooksPath      → (미설정, exit 1)
$ ls .git/hooks/pre-commit       → 없음
```
`docs/09 §2` 는 방어선을 2 단계로 설계했지만 2 단계(훅)는 수동 설치이고 지금 꺼져 있다.
`.gitignore` 는 `git add -f` 로 우회 가능하다는 것을 문서 스스로 밝히고 있다.

**제안** worktree 별 설치를 `docs/08` 런북의 기동 체크리스트에 넣고, `ops/healthcheck.py` 가
훅 설치 여부를 한 줄 리포트한다(라이브 API 를 안 쓰므로 헬스체크의 설계 원칙과 충돌하지 않는다).

### E-2 [낮음] 토큰 상태파일 권한은 POSIX 만 보장된다

`tokens.py:120-133` `_write_state` — `tempfile.mkstemp` 는 POSIX 에서 0600 을 만들고
`os.replace` 가 그 모드를 유지한다. **Windows 에서는 디렉터리 ACL 을 상속**하므로 0600 에
해당하는 보장이 없다. 이 프로젝트는 Windows 단일 사용자 환경이라 실피해는 낮다.
실제 ACL 은 확인하지 않았다 → §12.

---

## 6. ⑥ 크래시 후 데이터 무결성

**먼저 뚫리지 않은 것**: 저장 계층의 멱등성은 견고하다.
`candles_1m/1d` UPSERT(`writer.py:60-73`), `trades_snap` INSERT OR IGNORE(`:186-192`),
`rankings_snap` UNIQUE(snap_ms, type, duration, rank)(`:166-171`), `events` UPSERT(`:284-292`).
`_atomic_write_json`(`loops.py:698-710`)·`_write_state`(`tokens.py:120-133`) 둘 다
mkstemp + `os.replace` 원자 교체다. WAL 중 강제 종료로 인한 **부분 쓰기는 재현하지 못했다.**

### F-1 [중간] 이벤트 억제 이력이 상태파일에 없다 — 재시작마다 **검출 지연 측정값이 파괴된다**

**근거**
```
E12 state_snapshot 에 detector 억제 이력(_emitted)이 있는가: False
    load_state 가 복원하는가: False
```
`detector.py:572` `self._emitted` 는 인메모리 dict 다. `loops.py:616-629` `state_snapshot` 에 없다.

**재현 시나리오** (어젯밤 계획 외 재시작 2 회가 정확히 이 경로다)
> 재시작 → 버퍼를 DB 에서 복원 → `detect_events` 가 그 안의 이벤트를 **전부 다시** 낸다 →
> `previous is None` 이므로 전부 `is_new=True`(`detector.py:635`) →
> `record_event` 는 `(symbol, t0_ms)` UPSERT 라 **행 수는 안 늘어난다**.
> 하지만 `meta_json` 이 통째로 덮어써진다(`writer.py:291`):
> - `detected_ms` = 재시작 후 시각
> - `detect_lag_min` = 재시작 후 지연 (원래 값 소실)
> - `label_revision` = 0 으로 되돌아감
>
> **즉 "이 전조를 우리가 몇 분 만에 잡았는가"라는 Phase 1 의 핵심 산출물이 재시작 한 번에
> 조용히 지워진다.** W4 가 경고한 "UPSERT 가 순수 중복을 흡수하므로 행 수로는 억제 회귀를
> 못 잡는다"의 정확한 실현이다 — 행 수는 멀쩡하고 값만 틀린다.

**제안**
- `state_snapshot` 에 `{f"{sym}|{t0}": digest}` 를 담는다(상한 `seen_limit` 로 이미 유계).
- 또는 `record_event` 의 UPSERT 에서 `meta_json` 을 통째로 덮어쓰지 말고,
  `detected_ms`/`detect_lag_min` 은 **최초값을 보존**한다(`COALESCE` 또는 `json_patch`).
  후자가 근본 처방이다 — 억제층이 어떤 이유로 실패해도 최초 검출 시각은 보존된다.

### F-2 [중간] 상태 저장 주기 60 초 + Windows 강제 종료 ⇒ 테이프 구간 누락 판정이 사후 무력화된다

**근거** `loops.py:102` `STATE_SAVE_S = 60.0`. `ops/supervisor.py:107-114` 는
`self._child.terminate()` 를 쓰고, 자기 docstring(`:12-15`)이 밝히듯 Windows 에서 이는
`TerminateProcess` **강제 종료**라 `__main__.py:55-63` 의 `finally`(=`save_state(force=True)`,
`tokens.release()`)가 **실행되지 않는다.**

**재현 시나리오**
> 크래시 시점의 `last_trade_ms` 가 최대 60 초 과거로 되돌아간 채 저장된다. 재시작 후 첫
> `/trades` 응답의 최소 ts 가 그 과거값보다 작거나 같으면 `_poll_trades` 의 gap 판정
> (`loops.py:1111-1116`)이 **참이 되지 않는다** → 다운타임 동안 실제로 놓친 체결이
> `tape_gaps` 에 잡히지 않는다. A2 §2 가 요구한 "구간 누락 판정"이 **재시작 직후에만**
> 조용히 실패한다. 하필 누락이 가장 큰 순간이다.

**제안** 재시작 직후 첫 폴링에서는 gap 판정 대신 `resumes` 카운터와 함께
`state.saved_ms → 지금` 구간을 **무조건 누락 구간으로 기록**한다. 모르는 것을 "없음"으로
처리하지 않는다.

### F-3 [중간] `retention.archive_and_prune` 이 컬렉터 DB 에 **두 번째 writer** 를 연다

**근거** `store/retention.py:53-55` — `mode=rw` 로 연다. `:81-90` — DELETE 후
`PRAGMA wal_checkpoint(TRUNCATE)` + `VACUUM`. 계약 C-6(`docs/04:172`)은
"**쓰기 주체는 collector 단일 프로세스뿐**"이다.

**재현 시나리오**
> 운영자가 야간 수집 중에 아카이브를 돌린다. DELETE 는 `busy_timeout=30000` 안에 통과할 수
> 있지만 `VACUUM` 은 배타 락을 요구해 30 초 대기 후 `SQLITE_BUSY` 로 예외를 던진다.
> 그 예외는 `finally`(conn.close)만 지나 `main()` 밖으로 나간다 —
> **삭제와 아카이브는 이미 커밋됐으므로 데이터 정합성은 유지되지만**, 운영자는 traceback 만
> 보고 "아카이브 실패"로 오해해 재실행할 수 있다. 재실행하면 같은 `cutoff_ms` 파일에 병합되므로
> 손실은 없다. 위험은 정합성보다 **계약 위반의 선례**다 — 지금은 VACUUM 이 막아주지만,
> 누가 VACUUM 을 빼면 조용히 동시 쓰기가 된다.

docstring 은 "offline maintenance window" 를 전제하지만 **강제 장치가 없다**
(STOP 파일 확인도, 락도 없다). `register_task_scheduler.ps1` 이 등록하는 3 개 작업에
retention 은 없으므로 현재는 수동 실행뿐이다.

**제안** `archive_and_prune` 진입 시 `ops/state/STOP` 존재 또는 `token_state.json.lock`
미보유를 확인하고, 아니면 즉시 종료한다. 아니면 컬렉터의 상태파일 락을 공유 자원으로 쓴다.

---

## 7. ⑦ 계약 위반·중복 구현

### G-5 [뚫리지 않음] W4 검출기는 W3 분석 함수를 **재구현하지 않았다**

명시적으로 확인했다. `EventDetector` 는 전부 재사용한다:

| W4 사용처 | 재사용 대상 (W3) |
|---|---|
| `detector.py:589-592` | `analysis.features.extract_precursor_features(include_t0=True)` — 계약 A1 §1 이 요구한 플래그 사용 |
| `detector.py:612-615` | `analysis.labeling.detect_events` |
| `detector.py:598` | `analysis.baselines.rvol_series` |
| `detector.py:680-682` | `analysis.baselines.minute_of_session_volume_curve` |
| `loops.py:1049` | `analysis.baselines.compute_daily_baseline` |

`precursor_score`/`confirm_score`/`activity_score`/`tape_stats` 는 **새 표면**이지 중복이 아니다
(계약 C-8 이 `precursor_score` 를 W4 소유로 명시).

### G-1 [낮음] 계약 C-6 표가 A3 개정을 반영하지 않았다 — 폐기된 컬럼명이 남아 있다

`docs/04_contracts.md:180` 은 여전히 `orderbook_snap(..., imbalance REAL)` 이고,
같은 문서 `:311-318` A3 는 `imbalance_signed` 로 **개명**했다(schema.sql 도 그렇다).
A3 가 존재하는 이유가 "비율형/부호형 혼동으로 **조용히 틀리는** 것을 막기 위해"인데,
C-6 만 읽은 사람은 폐기된 이름과 함께 잘못된 중립점(0.5)을 가져간다.

**제안** C-6 표의 해당 줄을 `imbalance_signed REAL` 로 고치고 "정의는 A3 참조" 한 줄 추가.

### G-2 [낮음] 계약 C-6 에 `events` 의 UNIQUE 제약이 없다

`docs/04:181` — `events(id PK, symbol, t0_ms, ...)`. UNIQUE(symbol, t0_ms) 는 스키마 v2
마이그레이션(`migrations.py:14-25`)에만 있다. **중복 방지 2 층 중 1 층의 근거가 계약에 없다.**
계약이 정본이라면, 계약에 없는 불변식은 다음 개정에서 조용히 사라질 수 있다.

### G-3 [낮음, A-3 중복] 계약 C-9 의 `TOSS_LIVE` 문구와 구현이 다르다 — §1 A-3 참조.

### G-4 [낮음] ALLOWLIST 가 Phase 1 이 실제로 쓰는 것보다 넓다

`endpoints.py:12-27` 의 14 개 중 `TossClient` 메서드가 없는 것: `/price-limits`,
`/stocks/{symbol}/warnings`, `/market-calendar/KR`, `/accounts`, `/commissions`.
(`live_probe` 는 `c._request` 로 직접 호출한다.) 계약 C-4 가 명시 허용하므로 **위반은 아니다.**
다만 `docs/06 §10` 이 `/price-limits` 는 "미국 전략에 무용", `/warnings` 는 "KRX 전용"으로
실측 판정했으므로, 넓은 allowlist 는 순수한 blast radius 다.

**제안** Phase 2 착수 전에 미사용 항목을 빼고, 필요해지면 계약 개정으로 다시 넣는다.

---

## 8. ⑧ GET-only 차단 우회 가능성

### [뚫리지 않음] 이중 차단은 시도한 모든 각도에서 fail-closed 였다

반증 시도와 결과:

| 시도 | 결과 | 근거 |
|---|---|---|
| `..`/`//` 경로 정규화 우회 | **불가** — `canonical_path` 는 정규화를 **아예 하지 않는다**(`endpoints.py:75-85`: 쿼리 제거 + 선행 `/` 보정 + 후행 `/` 제거만). 정규화하지 않으므로 비허용 형태는 frozenset 에 없어 그대로 `ForbiddenEndpoint`. 정규화하지 않는 것이 여기서는 정답이다 |
| 관문/전송이 서로 다른 경로를 볼 가능성 | **불가** — httpx 는 `base_url.raw_path + url.raw_path` 단순 결합만 하고 `..` 를 해석하지 않는다. `_GuardedTransport`(`client.py:70-72`)는 **최종** `request.url.path` 를 재검사한다. base_url 에 경로 접두사가 붙으면 전송 레벨에서 **시끄럽게** 터진다 |
| 대소문자 | **불가** — 메서드만 `upper()`, 경로는 그대로 → 비허용 |
| 쿼리스트링 / 후행 슬래시 | **불가** — 양쪽 레이어가 같은 `canonical_path` 를 쓴다. 기존 테스트가 이미 커버(`test_api_allowlist.py:74`) |
| 템플릿 매칭 남용 | **불가** — `_tmpl_regex`(`endpoints.py:65-67`)의 자리표시자가 `[^/]+` 라 세그먼트를 못 넘는다 |
| 리다이렉트 | **불가** — `follow_redirects` 를 어디서도 지정하지 않으므로 httpx 기본 `False`. 설령 켜도 각 홉이 transport 를 다시 지난다 |
| 주문 계열 도달 | **불가** — 어떤 형태로도 frozenset 에 없다. 기존 테스트 `test_allowlist_contains_no_trading_endpoint` / `test_check_allowed_blocks_trading` 이 이미 방어 |

**단 하나의 실질 예외**: `_GuardedTransport` 를 쓰지 않는 raw `httpx.AsyncClient` 가 둘 있다.
- `tokens.py:175` — 경로가 상수이고 직전에 `check_allowed` 를 명시 호출한다. 허용 가능.
- `tools/live_probe.py:640` — **`check_allowed` 호출 없음.** §2 B-5 참조.
  현재 하드코딩된 경로가 허용 엔드포인트라 지금 당장의 위반은 없지만, 리포 안에 존재하는
  "이중 차단을 우회하는 방법"의 동작하는 예제다.

---

## 9. ⑨ events 중복 — 두 층이 **모두** 무력화되는 경로

두 층은 **똑같은 키 `(symbol, t0_ms)` 위에 서 있다**:
- 층 1: `UNIQUE(symbol, t0_ms)` + UPSERT (`migrations.py:24`, `writer.py:284`)
- 층 2: `EventDetector._emitted[(symbol, t0_ms)] = label_hash(row)` (`detector.py:628-634`)

⇒ **t0 를 움직이는 모든 것이 두 층을 동시에 뚫는다.**

### I-1 [높음] t0 이동으로 같은 급등이 두 행이 된다

`labeling.py:248-268` `_label_day` 는 매매일 안에서 조건을 만족하는 **가장 이른 봉**을
t0 로 잡고 `max_per_day=1` 에서 멈춘다. t0 를 바꾸는 입력이 둘 있다:

**(a) RVOL 게이트가 켜지고 꺼진다 (C-1 과 직결)**
> 승격 직후 곡선이 `None` → `gated=False` → 가격 조건만으로 **이른 봉** t0=A 에 이벤트 기록.
> 1 시간 뒤 백필/DB 로드로 이전 매매일 봉이 확보되어 곡선이 생긴다 → `gated=True` →
> 봉 A 는 RVOL 3 배를 못 넘겨 탈락 → t0 가 **더 늦은 봉 B** 로 이동 →
> `(symbol, B)` 는 새 키이므로 **층 1 도 층 2 도 막지 않는다** → 행이 2 개가 된다.
> 행 A 는 `rvol_gated=False` 로 영원히 남는다.

**(b) `prev_close_u` 가 바뀐다**
> `labeling.py:213-217` — `prev_close_u` 가 None 이면 버퍼의 직전 봉 종가, 그것도 없으면
> 당일 첫 봉 시가로 대체한다. `_ensure_history` 가 baseline 을 채우기 전/후로 이 값이 달라지고,
> C-2 때문에 장기 운용에서는 계속 낡은 값이 쓰인다. `day_ret` 이 달라지면 더 **이른** 봉이
> 자격을 얻어 t0 가 앞으로 이동한다 → 같은 이유로 새 행.

**행 수로는 절대 못 잡는다** — W4 의 경고가 여기서 한 걸음 더 나아간다:
UPSERT 는 *같은 키* 중복을 흡수해 보이지 않게 하고, *다른 키* 중복은 애초에 아무도 검사하지 않는다.

**제안**
1. **불변식 검사를 추가한다**: 임의의 `(symbol, 매매일)` 에 대해 `events` 행 수 ≤ `max_per_day`.
   지금 이걸 검사하는 코드도 테스트도 없다. 리플레이 테스트에 넣으면 회귀를 잡는다.
2. 억제 키를 `(symbol, 매매일)` 로 올리고, 같은 날 더 나은 t0 를 찾으면 **기존 행을 갱신**한다
   (t0 를 옮기는 UPDATE). 그러려면 `events` 에 `(symbol, trading_day)` 유니크가 필요하다 —
   계약 C-6 변경이므로 `ask`.
3. 최소 조치: `rvol_gated=False` 인 이벤트를 **애초에 기록하지 않는다**(C-1 제안 3과 동일).
   경로 (a)가 통째로 사라진다.

### I-2 [중간, F-1 중복] 재시작이 층 2 를 통째로 지운다 — 행 수는 그대로, 라벨만 오염. §6 F-1 참조.

---

## 10. ⑩ 승격/강등 churn

**히스테리시스는 실제로 작동한다 — 단, 스코어 경로에서만.**
`detector.py:286-293` 이 선언한 3 겹(밴드 / 연속 미달 / dwell)은 `on_new_data` → `_maybe_demote`
→ `_change` 경로에만 적용된다. 그런데 **`ignore_dwell=True` 로 이 방어를 건너뛰는 경로가 셋** 있다:

| 경로 | 코드 | 빈도 |
|---|---|---|
| `force()` (랭킹 진입 승격) | `detector.py:366` | **12 초마다 × 랭킹 4 종** (`polling.ranking_snap_s: 12`) |
| `sweep()` stale 강등 | `detector.py:381` | tier1 스윕마다(45 초) |
| `set_capacity()` 정원 축소 강등 | `detector.py:400-401` | 세션 전환마다 + BudgetGuard 지시마다 |

### J-1 [높음] 세 우회 경로가 서로를 먹여 살리는 플래핑 고리를 만든다

**재현 시나리오** (관측된 강등 221 / 승격 266 의 구조적 설명)
> 1. 세션 종료 → `set_capacity(1, 1)` → tier2/tier3 **전원 강등**(실측 E10: 47 건/틱),
>    버퍼·곡선 소멸.
> 2. 12 초 뒤 다음 세션의 첫 랭킹 스냅샷 → `force(..., ignore_dwell=True)` 로 상위 10 종목
>    **즉시 재승격** (dwell 무시).
> 3. 재승격 종목은 `_ensure_history` 에서 재백필(B-4) → CHART 예산 초과 → BudgetGuard 가
>    tier2 정원 축소 지시 → `set_capacity` → **또 강등**.
> 4. 강등된 동전주 중 조용한 종목은 11 분 뒤 `sweep` 이 stale 로 다시 내린다(D-2).
> 5. 랭킹은 계속 같은 종목을 올린다 → 2 로 돌아간다.

**tier3 정원(20)을 노이즈가 차지하는 경로**: 랭킹 강제 승격은 tier2 까지만 올린다
(`_ranking_triggers` 는 `force(..., 2, ...)`) 이므로 tier3 직접 잠식은 **없다**.
그러나 tier3 는 세션 전환마다 정원 1 로 붕괴했다가(E10) 다시 채워지므로,
**어떤 종목도 tier3 에서 테이프를 오래 축적하지 못한다** — tier3 의 존재 이유가
"사후 복원 불가능한 테이프를 조밀하게 모으는 것"인데, 하루 4~5 번 리셋된다.

**제안**
1. `set_capacity` 의 강등에도 **최소 dwell** 을 적용하거나, 세션 전환 시 정원을 줄이는 대신
   **폴링을 멈춘다**(D-3/B-4 제안과 동일).
2. `force()` 의 `ignore_dwell=True` 를 "티어가 오르는 경우에만" 으로 제한하는 것은 이미 맞지만,
   **같은 심볼을 N 분 안에 재승격하지 않는 쿨다운**을 추가한다
   (BudgetGuard 의 `SHRINK_COOLDOWN_S` 와 같은 개념).
3. `promotions` 테이블에 `reason` 이 이미 있으므로, churn 진단 지표
   (`(symbol) 별 하루 tier 변경 횟수`)를 텔레메트리에 추가하면 회귀를 볼 수 있다.
   현재 `telemetry()` 는 `promotions` 누적만 낸다(`loops.py:509`).

### J-2 [중간] tier2 라운드로빈 커서가 **매 반복 재정렬되는 리스트**를 인덱싱한다

**근거** `loops.py:894-905`
```python
symbols = sorted(ctx.tiers.at_least(2))    # 매 반복 새로 만든다
cursor %= len(symbols); symbol = symbols[cursor]; cursor += 1
```
승격/강등으로 집합이 바뀌면 같은 `cursor` 가 다른 심볼을 가리킨다 → **어떤 심볼은 건너뛰어지고
어떤 심볼은 연속으로 폴링된다.** churn 이 심할수록 회전이 무작위에 가까워진다.
건너뛰어진 심볼은 `last_ts_ms` 가 갱신되지 않아 D-2 의 stale 강등 대상이 된다 → J-1 고리에 합류.

(메커니즘은 코드로 확정했으나 **누락률은 측정하지 않았다** — 정량은 §12.)

**제안** 커서를 인덱스가 아니라 **마지막으로 폴링한 심볼 이름**으로 들고, 정렬 리스트에서
그 다음 항목을 고른다(`bisect`). 집합이 바뀌어도 회전이 유지된다.

---

## 11. ⑪ 유니버스 오염 — **확인됨**

### K-1 [높음] 랭킹 출현 누적 경로에 가격·시총·종목종류 필터가 **하나도** 없다

**근거**
- `loops.py:790-801` `_ranking_triggers`
  ```python
  for row in page.rows:
      if row.rank > RANKING_PROMOTE_TOP: continue
      ctx.watch(row.symbol)                       # ← 무조건 워치리스트에 넣는다
      if toss and ctx.tiers.tier_of(row.symbol) < 2:
          ... ctx.tiers.force(row.symbol, 2, "ranking_entry", score, snap_ms)
  ```
- `loops.py:582-591` `CollectorContext.watch` — 검사는 **`tier1_max` 정원뿐**.
  `price_min_u`/`price_max_u`/`mcap_min_u`/`mcap_max_u` 를 읽지 않는다.
- `universe/filters.py:8-23` `passes_tier0` 는 존재하지만 **`universe/build.py` 에서만** 쓰인다.

**실측**
```
E7 CollectorContext.watch 안에 price/mcap 필터가 있는가: False
   _ranking_triggers 안에 필터가 있는가: False
   passes_tier0 참조처: ['build']
```

**재현 시나리오**
> `RANKING_TYPES` 에는 `MARKET_TRADING_AMOUNT` 가 있다(`loops.py:62-65`). 미국 시장
> **거래대금 상위 10 종목은 정의상 초대형주다.** 12 초마다 스냅샷 → 매번 NVDA/TSLA/AMD/META/
> ASML/NOK 가 `ctx.watch()` 된다. 즉 **NOK·AMD·ASML·META 가 워치리스트에 오른 것은 버그가
> 아니라 이 코드 경로의 설계된 동작**이다.
> 게다가 `TOSS_SECURITIES_*` 상위 10 은 추가로 `force(..., 2, "ranking_entry")` 로
> **tier2 강제 승격**된다 — 토스 개인 투자자 거래대금 상위도 대형주가 다수다.

**영향 (관측 가능한 것만)**
1. **영속 오염.** 워치리스트에서 심볼을 빼는 유일한 경로는 `unwatch` 인데, 이는
   `/prices` 응답에서 **5 회 연속 누락**될 때만 발동한다(`loops.py:845-851`). 대형주는 절대
   누락되지 않는다 → **영구히 남는다.** 게다가 `state_snapshot["watchlist"]`(`loops.py:621`)로
   저장되고 `load_state`(`:657-659`)가 복원하므로 **재시작으로도 안 지워진다.**
   워치리스트는 `tier1_max=1500` 까지 **단조 증가**한다.
2. **tier1 예산 잠식.** 스윕은 200 개 배치 단위라(`loops.py:819`) 대형주가 200 개마다 한 배치를
   더 만든다. 1500 정원이 차면 진짜 표적(동전주)이 `watch()` 에서 **거부**된다
   (`loops.py:587-588`: 정원이 차면 `return False`) — 오염이 표적을 **밀어낸다**.
3. **tier2 정원·CHART 예산 잠식.** `ranking_entry` 강제 승격 + 재승격마다 백필(B-4).
4. **`promotions` 테이블 오염.** 두 경로(precursor/confirm)의 리드타임을 사후 평가하겠다는
   `detector.py:14-15` 의 목적을 위해 남기는 표인데, `ranking_entry` 대형주 행이 대다수가 된다.
5. **`events` 라벨은 비교적 안전.** 대형주가 30 분 +15% / 당일 +30% 를 넘는 일은 드물다.
   즉 이 결함은 **라벨을 거짓으로 만들기보다 예산과 정원을 갉아먹는다.** 과장하지 않고 그대로 적는다.
6. **베이스라인 왜곡은 제한적.** RVOL 곡선·일봉 베이스라인은 전부 심볼별이라 교차 오염이 없다.
   다만 `toss_share`(`features.py:451-462`)는 TOSS/MARKET 랭킹에 **둘 다** 등장한 스냅샷만
   쓰므로, 두 랭킹을 대형주가 함께 채우면 표본이 대형주 쪽으로 쏠린다.

**제안**
1. `CollectorContext.watch()` 안에서 `universe` 설정으로 걸러낸다. 랭킹 행은 `last_u` 를 이미
   갖고 있으므로(`RankingRow.last_u`) **가격 필터는 즉시 적용 가능**하다:
   `if not (uni.price_min_u <= row.last_u <= uni.price_max_u): return False`.
2. 시총 필터는 `sharesOutstanding` 이 필요하다. `symbols` 테이블에 tier0 빌드 결과가 있으므로
   (`store.upsert_symbols(..., tier=0)`), 랭킹 신규 심볼은 **tier0 명단에 있을 때만** 워치한다.
   이것이 가장 작고 확실한 처방이다 — `universe/build.py` 가 이미 `passes_tier0` 를 통과시킨
   집합을 DB 에 갖고 있는데 컬렉터가 그걸 안 본다.
3. 필터에 걸려 거부된 심볼 수를 카운터로 남긴다(`watchlist_rejected_price` 등) —
   조용히 거르는 것도 조용히 통과시키는 것만큼 위험하다.

---

## 12. 미확인 (의심되지만 증명하지 못함)

의도적으로 결함으로 올리지 않았다. 확인 방법을 함께 적는다.

| # | 항목 | 왜 미확인인가 | 확인 방법 |
|---|---|---|---|
| U-1 | B-2 의 전제: 서버가 `X-RateLimit-Limit` 의미를 창(window) 쿼터로 바꿀 가능성 | `docs/06 §9-1` 은 현재 **초당**임을 실측했다. 미래 변경 가능성은 추측이다 | 결함(B-2, 상한 없음)은 전제와 무관하게 유효하므로 그쪽만 고치면 된다 |
| U-2 | `data/token_state.json` 의 Windows ACL | POSIX 0600 은 코드로 확정. Windows 상속 ACL 은 실측 안 함 | `icacls data\token_state.json` 로 1 초 확인 |
| U-3 | `_curve_for` 의 `market_day_at(now) or today()` 폴백이 **잘못된 날짜**를 제외할 가능성 (애프터마켓 종료~데이마켓 개시 사이 약 10 분) | 그 구간은 `closed` 라 tier2 루프가 idle 이므로 `_detect` 가 안 불릴 **것 같다**. 증명 못 함 | 가상 시계로 그 10 분에 `_detect` 를 강제 호출해 `exclude_dates` 를 확인 |
| U-4 | 손상된 `api_keys` 의 `json.JSONDecodeError` 가 파일 내용을 traceback 에 실을 가능성 | `str(JSONDecodeError)` 에는 위치만 들어간다. `e.doc` 을 찍는 로깅 경로는 못 찾았지만 전 경로를 감사하지 않았다 | `_read_keys` 를 `try/except (ValueError)` 로 감싸 메시지를 자체 문구로 대체하면 논점 자체가 사라진다 |
| U-5 | API 가 가격을 **JSON 숫자**로 주는 엔드포인트/세션이 있는지 | 그러면 `dec_to_u` 가 `float` 를 거부해(`models.py:110-111`) 전 파싱이 실패한다. `docs/06` 실측은 전부 문자열 | 다음 라이브 프로브에서 `type(raw)` 를 기록 |
| U-6 | J-2(라운드로빈 커서 표류)의 **실제 누락률** | 메커니즘은 코드로 확정. 정량은 측정 안 함 | 리플레이에서 심볼별 폴링 간격 분포를 낸다 |
| U-7 | WAL 상태에서 강제 종료 시 **부분 쓰기** 발생 여부 | 재현하지 못했다. SQLite WAL + `synchronous=NORMAL` 은 전원 손실에 취약할 수 있으나 `TerminateProcess` 수준에서는 안전해야 한다 | 전원 손실 시뮬레이션이 필요 — Phase 1 범위 밖으로 본다 |

---

## 13. Phase 2 착수 전 반드시 해소해야 할 것 (심각도 순)

| 순위 | 항목 | 왜 지금인가 |
|---|---|---|
| 1 | **A-1** 토큰 리스를 CWD 비종속 절대경로로 옮기고, 보유자 지문을 상태파일에 남긴다 | 되돌릴 수 없는 유일한 결함. 워커가 늘어날수록 확률이 오른다 |
| 2 | **A-3** `TOSS_LIVE` 미설정 = 라이브 금지 (또는 계약 C-9 개정) | A-1 의 방아쇠. fail-open 을 fail-closed 로 |
| 3 | **C-1** 곡선 `None` 캐시 금지 + 게이트 미적용 이벤트를 alert 하지 않기(가능하면 기록도 안 하기) | Phase 2 가 학습/평가할 이벤트 집합이 지금 이 순간 오염되고 있다 |
| 4 | **I-1** `(symbol, 매매일)` 당 이벤트 수 불변식 테스트 추가 | 중복이 **행 수로 안 보이는** 구조라 회귀를 잡을 방법이 지금 없다 |
| 5 | **K-1** 워치리스트에 tier0/가격 필터 적용 + 거부 카운터 | 오염이 상태파일로 영속하므로, 고치기 전에 쌓이는 만큼 나중에 손으로 지워야 한다 |
| 6 | **C-2** baseline/prev_close 를 매매일 경계에서 무효화 | Phase 2 는 무중단 장기 운용을 전제할 텐데, 운용이 길수록 틀린다 |
| 7 | **A-2** `invalidate(token)` compare-and-swap | 401 한 건이 폭풍이 되는 경로를 닫는다 |
| 8 | **B-3** `budget.on_request` 를 client 전송 지점으로 옮긴다 | 예산 방어선이 가장 필요한 순간(재시도 폭주)에 눈을 감는다 |
| 9 | **B-1** 버킷 capacity 를 rate 미만으로 | 세션 개장마다 한도를 40% 초과한다 |
| 10 | **B-2** 헤더 채택에 공시값 상한 | 한 줄 수정으로 60배 사고 가능성을 없앤다 |
| 11 | **D-2** stale 판정을 `last_data_ms`(벽시계)로 분리 | 전략의 표적 종목을 골라서 내쫓고 있다 |
| 12 | **D-1** UTC 날짜 버킷 → 캘린더 매매일 | **2026-11-01 이후 자동으로 틀리기 시작한다.** 여름에 테스트한 코드는 이 결함을 못 본다 |
| 13 | **F-1** 억제 이력 영속화 또는 `detected_ms` 보존 | Phase 1 의 핵심 산출물(검출 지연)이 재시작마다 지워진다 |
| 14 | **J-1 / D-3 / B-4** 세션 전환을 정원 축소가 아니라 폴링 중지로 | 셋이 같은 뿌리다. 하나 고치면 셋이 낫는다 |
| 15 | **B-5** `probe_force_429` 를 `--probe all` 에서 제외 | 다음 리스 보유자가 인자 없이 실행하면 라이브에 429 를 만든다 |
| 16 | **G-1 / G-2 / G-4** 계약 문서를 A3·스키마 v2 와 일치시키고 allowlist 축소 | 계약이 정본인 체제에서 계약이 틀리면 다음 워커가 틀린다 |
| 17 | **F-3** retention 에 컬렉터 실행 중 가드 | 지금은 VACUUM 이 우연히 막아준다. 우연에 기대지 않는다 |
| 18 | **E-1** pre-commit 훅을 기동 체크리스트에 넣는다 | 2 차 방어선이 꺼져 있다 |

---

## 14. 감사 방법·재현 환경

- 테스트: `pytest -q` → **636 passed** (2:25). 격리 venv(스크래치패드)에 `pyproject.toml` 의
  런타임+dev 의존성만 설치. 시스템 `site-packages` 의 `tests` 패키지가 리포의 `tests/` 를
  가리는 문제가 있어 격리가 필요했다.
- 재현 스크립트 2 개(`exp.py`, `exp2.py`)를 스크래치패드에서 실행. **리포에 어떤 파일도
  추가·수정하지 않았다** — 이 문서 하나가 유일한 변경이다.
- 라이브 호출 0 회. `api_keys` 를 읽지 않았다. 토큰 실험은 `tools/mock_server.py`
  (`127.0.0.1:8899`)와 스크래치패드의 더미 키 파일만 사용했다.
- 실측 인용(E1~E13)은 전부 위 스크립트의 실제 출력이다. 추론만 있고 실측이 없는 항목은
  그렇게 명시했거나 §12 로 분리했다.

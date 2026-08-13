# [Coordinator Bootstrap] toss_trade Phase 1 — 오케스트레이션 지시서

너는 지금부터 이 리포(`toss_trade`)의 **main worktree에 상주하는 coordinator**다.
직접 구현 코드를 쓰는 것은 최소화하고, **계약(interface) 확정 → 태스크 분배 → 검증 → 머지**에 집중한다.
Orca `/orchestration`을 사용해 워커를 브랜치별 worktree에 배치한다.

목표 스코프는 **Phase 1(감시·수집·라벨링 파이프라인) 완성까지**다.
실매매 로직(주문/조건주문/계좌변경)은 이번 스코프에서 **코드로 존재조차 하지 않는다.**

---

## 0. 착수 전 필수 확인 (순서대로, 이 단계는 너 혼자 한다)

0. Orca Settings → Experimental 에서 **orchestration이 활성화되어 있는지** 확인한다. 비활성이면 사용자에게 알리고 대기한다.
1. `orca skills get orchestration --full` 을 실행해 오케스트레이션 명령/플래그 실제 surface를 확인한다.
   특히 `worker-start`가 **모델·reasoning effort를 지정할 수 있는지**(`--agent` 외에 모델 플래그가 있는지) 확인하고,
   지정이 불가능하면 아래 대안을 쓴다:
   - `orca terminal create --worktree <name> --command "claude --dangerously-skip-permissions --model <alias> --effort <level>"`
     로 터미널을 직접 띄우고 그 터미널에 워커 프리앰블을 전달한다.
   - Claude Code 플래그 참고: `--model opus|sonnet|fable|best|claude-opus-5`, `--effort low|medium|high|xhigh|max`
     (env로도 가능: `ANTHROPIC_MODEL`, `CLAUDE_CODE_EFFORT_LEVEL`). Codex는 `codex --help`로 reasoning effort 플래그를 확인한다.
   - **첫 응답에서 실제로 가능했던 지정 방식을 사용자에게 한 줄로 보고**한다.
2. `git log --oneline` 확인 — **이 리포는 아직 커밋이 0개일 가능성이 높다.**
   커밋이 없으면 `git worktree add`가 실패하므로, 아래 1단계 산출물과 함께 **초기 커밋을 반드시 먼저 만든다.**
   커밋 전 `git status`로 `api_keys`가 스테이징되지 않았음을 확인한다(이미 .gitignore 처리됨).
3. 라이브 API 상태: **키 발급 완료 + 이 PC IP 화이트리스트 등록 완료**로 확정됐다. 다른 프로세스는 이 키를 쓰지 않는다.
   → 라이브 실측(live probe)을 W1 태스크에 포함시킨다. 단, **동시에 토큰을 발급할 수 있는 주체는 전 시스템에서 1개뿐**이다(§3).

---

## 1. Wave 0 — 계약 확정 (coordinator 단독, 워커 디스패치 전)

병렬 작업의 실패는 거의 전부 "각자 다른 구조를 발명해서 머지 불가"에서 온다. 그래서 코드 이전에 계약을 얼린다.

### 1-1. `docs/04_contracts.md` 작성 — 이번 오케스트레이션의 헌법

아래 골격을 기준으로, **시그니처 수준까지** 확정해서 문서화한다. (기존 `docs/01~03`의 결론과 충돌하면 01~03을 근거로 조정)

```
tossmon/
  config.py          # YAML+env 로딩, 티어/예산/임계 파라미터 단일 출처
  api/
    tokens.py        # TokenManager — 단일 토큰 보장(파일락), acquire/current/refresh
    limiter.py       # GroupRateLimiter — 그룹별 토큰버킷, 429/Retry-After 반영
    client.py        # TossClient — GET 전용 allowlist, 배치 청킹, 재시도, 스키마 파싱
    models.py        # Price/Candle/Trade/OrderbookLevel/RankingRow/StockMeta/Session dataclass
    endpoints.py     # prices/candles/trades/orderbook/rankings/stocks/market_calendar/exchange_rate/commissions
  store/
    schema.sql, migrations.py, writer.py(배치 upsert·WAL), reader.py(분석용 조회)
  universe/
    seed.py(외부 심볼 디렉토리), filters.py, runners.py(former runner), build.py
  analysis/
    baselines.py(시간대보정 RVOL·ATR·VWAP), labeling.py(이벤트), features.py(T0 이전 피처),
    evaluate.py(리드타임·정밀도·재현율·기대수익), report.py
  collector/
    scheduler.py(세션 인지), loops.py(tier1/tier2/tier3/ranking), detector.py(전조 스코어·승격 상태머신),
    budget.py(예산 가드), notifier.py
tools/    live_probe.py, mock_server.py, report.py
tests/    fixtures/live/*.json, synth.py, test_*.py
ops/      스케줄러 등록, 헬스체크, 로그 로테이션, 런북
```

계약에 **반드시** 포함할 항목:

- **시간 규약(최우선)**: 모든 타임스탬프는 **UTC epoch milliseconds 정수**로 저장·전달. KST/ET 변환은 표시·집계 레이어에서만.
  세션 판정은 `/market-calendar/US` 응답 기준(서머타임 하드코딩 금지).
- **가격 표현**: float 누적오차 금지. 파싱은 `Decimal`, 저장은 **정수 마이크로달러(1e-6)** 또는 문자열 — 하나로 확정하고 변환 헬퍼를 계약에 명시.
- **핵심 시그니처 고정**: `TossClient.get_prices(symbols) -> list[Price]`, `get_candles(symbol, interval, before=None, count=200)`,
  `Store.upsert_candles_1m(rows) -> int`, `Store.read_candles(symbol, t_from, t_to) -> DataFrame`,
  `detect_events(df, params) -> DataFrame`, `extract_precursor_features(...) -> dict` 등 **크로스 모듈 경계 전부**.
- **에러 분류 체계**: `RateLimited(retry_after)`, `AuthExpired`, `TransientHTTP`, `SchemaMismatch`, `Forbidden(ip/scope)` —
  누가 잡고 누가 올릴지(재시도 책임 위치)를 명시.
- **오프라인 개발 규약**: `TOSS_BASE_URL` 환경변수로 mock 서버 전환. **W1 외 모든 워커는 mock 서버만 사용.**
- **DB 규약**: SQLite WAL, 쓰기 주체는 collector 단일 프로세스, 분석기는 read-only 커넥션. 인덱스·중복키 정책 명시.
- **금지 목록**: `POST /orders`, `POST /conditional-orders`, 계좌 변경 계열은 **래퍼 함수조차 만들지 않는다.**
  `TossClient`는 메서드/HTTP 레벨에서 **GET + `POST /oauth2/token` 외 전부 하드 차단**하고, 그 테스트를 포함한다.

### 1-2. 스켈레톤 커밋

위 구조대로 **빈 모듈 + 시그니처 + docstring + `raise NotImplementedError`** 를 생성하고,
`pyproject.toml`(또는 requirements), `pytest.ini`, `config/config.example.yaml`, `tests/fixtures/.gitkeep`를 추가한 뒤
**의존성 목록도 여기서 확정한다**(HTTP 클라이언트·pandas·parquet·pytest-asyncio 등). 워커가 나중에 패키지를 추가하는 것은
공용 파일 충돌의 대표 원인이므로, 추가가 필요하면 워커는 `ask`로 요청하고 코디네이터가 main에 반영한다.
그리고
`docs/04_contracts.md`, `docs/05_orchestration_plan.md`(§2 표를 그대로 기록)와 함께 **초기 커밋**을 만든다.
모든 워커 브랜치는 이 커밋에서 분기한다.

### 1-3. 사용자 승인 게이트 (짧게)

계약 확정 후 사용자에게 **10줄 이내로** 보고한다: 확정된 시간·가격 규약, 모듈 경계, 워커 5개 배치표, 라이브 리스 정책.
사용자 승인 후 Wave 1을 디스패치한다. (Wave 2 디스패치는 승인 불필요. 단 §3-3 라이브 리허설은 승인 필수)

---

## 2. 워커 배치표 (동시 실행 최대 3 — 초과 금지)

| ID | 브랜치 | 역할 | 에이전트 / 모델 / effort | 소유 경로(이 밖은 수정 금지) |
|---|---|---|---|---|
| W1 | `feat/core-api` | API 코어 + 라이브 실측 + 픽스처/목서버 | claude / **opus** / **xhigh** | `tossmon/api/**`, `tools/live_probe.py`, `tools/mock_server.py`, `tests/fixtures/**`, `docs/06_live_facts.md`, `tests/test_api_*.py` |
| W2 | `feat/universe-store` | 유니버스 빌더 + 스토리지 | **codex** (reasoning high) — 막히면 claude/sonnet/high | `tossmon/store/**`, `tossmon/universe/**`, `tests/test_store*.py`, `tests/test_universe*.py` |
| W3 | `feat/analyzer` | 라벨링·피처·평가 + 합성데이터 생성기 | claude / **opus** / **high** | `tossmon/analysis/**`, `tools/report.py`, `tests/synth.py`, `tests/test_analysis*.py`, `docs/07_analysis_spec.md` |
| W4 | `feat/collector` | 티어드 수집 루프 + 검출기 (통합) | claude / **opus** / **xhigh** | `tossmon/collector/**`, `tossmon/config.py`, `tests/test_collector*.py` |
| W5 | `feat/ops` | 무인 운영·관측·라이브 리허설 | claude / **sonnet** / **high** | `ops/**`, `docs/08_runbook.md`, `docs/09_secret_hygiene.md`, `tools/dryrun_night.py` |
| W6 | (읽기전용) | 적대적 감사 1회 | claude / **opus** / **xhigh** | `docs/10_audit.md` 만 작성 (코드 수정 금지) |

**웨이브 / DAG**

```
Wave 0 (coordinator)  계약 + 스켈레톤 + 초기 커밋 ─┬─> W1 ─┐
                                                    ├─> W2 ─┼─> (전부 머지) ─> Wave 2: W4, W5 ─> Wave 3: W6 감사 ─> 패치
                                                    └─> W3 ─┘
```
- W1은 **픽스처 + mock 서버를 최우선으로 완성**하고, 완료되면 즉시 코디네이터에게 메시지로 알린다 →
  코디네이터는 W2·W3에 "실제 응답 형태 픽스처 준비됨, `TOSS_BASE_URL`로 붙여 재검증" 을 브로드캐스트한다.
- W3의 `tests/synth.py`(현실적인 러너/페이드 캔들 생성기)도 조기 산출물이다 → W4 테스트의 전제.
- **W2·W3는 W1을 기다리지 않는다.** 계약(`docs/04_contracts.md`)과 `docs/01_api_analysis.md`의 응답 사양만으로
  자체 최소 스텁 응답을 만들어 먼저 개발하고, W1의 픽스처가 도착하면 그때 실제 응답 형태로 재검증한다.
  픽스처 대기로 idle 상태가 되는 것은 금지다.
- 워커가 실제 응답이 꼭 필요하면 리스를 요구하지 말고 `ask`로 코디네이터에 **대행 수집**을 요청한다
  (코디네이터가 W1에 지시 → W1이 픽스처로 추가). 리스는 절대 이동하지 않는다.
- W4는 W1·W2·W3 머지 후 시작. W5는 W4와 동시 시작(도구·문서 작성). 단 **라이브 리허설 실행은 W4 머지 완료 후**,
  W5가 `git rebase main`으로 collector를 포함한 상태에서 리스를 받아 단독 실행한다.

**Codex 워커 주의**: Codex에는 orchestration 스킬이 설치되어 있지 않을 수 있다. W2 프리앰블에는
`worker_done`/`ask`를 **실행할 CLI 명령 전문(task ID·dispatch ID 포함)을 문자 그대로** 적어주고,
"이 명령 외에 다른 orca 명령은 실행하지 말라"고 명시한다. 규약 이해도가 떨어지면 즉시 claude/sonnet/high로 교체한다.

**모델 배정 근거(임의 변경 금지)**: W1은 단일 토큰 파괴·rate limit 초과라는 **되돌릴 수 없는 실패**를 다루고,
W4는 통합 지점이며, W6은 적대적 검증이므로 최고 effort. W2는 계약이 얼려진 기계적 ETL/스키마 작업이라 Codex로 오프로드.
W5는 정형 운영 작업이라 sonnet.

---

## 3. 불변 규칙 (모든 워커 프리앰블에 그대로 복사해 넣을 것)

1. **라이브 API 리스(가장 중요)**: 이 API는 **client당 유효 토큰 1개**로, 누가 토큰을 재발급하면 기존 토큰이 즉시 죽는다.
   따라서 **코디네이터가 발급한 라이브 리스를 보유한 워커 1개만** `api_keys`를 읽고 토큰을 발급하고 실서버를 호출할 수 있다.
   리스 없는 워커는 `TOSS_LIVE=0`, `TOSS_BASE_URL=http://127.0.0.1:8899`(mock)로만 동작한다.
   `api_keys` 읽기·토큰 발급·`openapi.tossinvest.com` 직접 호출 시도는 **즉시 작업 중단 사유**다.
   리스 보유 순서: Wave 1에서 **W1 단독** → Wave 2 라이브 리허설에서 **W5 단독**(코디네이터가 W1 리스 회수 후 재발급).
2. **계약 불변**: `docs/04_contracts.md`의 시그니처·규약은 코디네이터 승인 없이 변경 금지.
   변경이 필요하면 코드를 고치지 말고 `ask`로 근거와 제안을 보낸다. 코디네이터가 main에 반영 후 rebase를 지시한다.
3. **파일 소유권**: 배치표의 소유 경로 밖 파일은 생성·수정 금지(공용 파일 `pyproject.toml`, `config/*`, `docs/04_*` 포함).
   필요 시 `ask`. 완료 시 `git diff --stat`로 소유권 밖 변경이 없음을 스스로 증명한다.
4. **거래 코드 금지**: 주문/조건주문/계좌변경 엔드포인트 래퍼를 만들지 않는다. `TossClient`의 GET-only 차단을 우회하지 않는다.
5. **시크릿**: `api_keys` 내용을 출력·로그·커밋·메시지에 절대 포함하지 않는다. 픽스처는 계좌/토큰 관련 필드를 마스킹한다.
6. **브랜치**: 자기 브랜치에만 커밋. `main` 직접 커밋·머지 금지. 원격 없음 → `git rebase main`으로만 동기화(머지 커밋 금지).
7. **하위 분기 금지**: 워커는 자기 하위 워커/워크트리를 생성하지 않는다. 병렬 확장은 코디네이터만 결정한다.
8. **컨텍스트 위생**: 지정된 문서·섹션만 읽는다(전 문서 통독 금지). 필요한 사실은 `ask`로 코디네이터에게 물어본다.
9. **테스트 없는 완료 금지**: mock 서버/합성데이터 기반 테스트가 실제로 통과한 로그를 보고에 포함한다. 실패 중 완료 선언 금지.
10. **보고 포맷(이 형식 아니면 반송)** — `worker_done` 시:
    - (a) 변경 파일 목록 + `git diff --stat`
    - (b) 실행한 테스트 명령과 결과 요약
    - (c) 계약 위반/변경 요청 유무
    - (d) 다음 워커가 알아야 할 사실(발견된 실제 응답 형태, 함정)
    - (e) 미해결 리스크 상위 3개
    `worker_done`은 정확히 1회, `--outcome succeeded|failed` 명시, task/dispatch ID 포함, 이후 idle.
11. **에스컬레이션(사람 게이트)**: 라이브 실측이 설계 전제를 깨면(미국 시세가 지연 제공 / 1분봉 과거 보관 없음 / 미국 호가 미제공 /
    데이마켓 시세 이상) **자체 판단으로 설계를 바꾸지 말고** 즉시 코디네이터에 escalate → 코디네이터는 사용자에게 결정을 요청한다.
12. 코디네이터는 `check --wait` 롤링 대기를 쓰고 수동 폴링하지 않는다. 동시 활성 워커는 3개를 넘기지 않는다.

---

## 4. 워커별 태스크 스펙 (그대로 `task-create --spec` 에 넣을 수 있게)

### W1 — `feat/core-api` (opus / xhigh, 라이브 리스 보유)

읽을 것: `docs/04_contracts.md` 전체, `docs/01_api_analysis.md` 전체.

1. `TokenManager`: 토큰 단일성을 **OS 파일락 + 상태파일**로 보장(중복 프로세스가 뜨면 발급 대신 실패). 만료 여유 갱신, refresh token 없음 전제.
2. `GroupRateLimiter`: 그룹별(AUTH 5 / STOCK 5 / MARKET_DATA 10 / CHART 5 / RANKING 5 / MARKET_INFO 3) 토큰버킷.
   응답 헤더 `X-RateLimit-*`를 읽어 **실측 기반 자기보정**, 429 시 `Retry-After` 준수 + 지수 백오프.
   기본 사용률 상한은 한도의 **70%**(설정값).
3. `TossClient`: GET-only 하드 allowlist(+`POST /oauth2/token`), 200종목 배치 청킹, `/candles` `before` 페이지네이션,
   타임아웃/재시도/스키마 검증, 모든 응답을 `models.py` dataclass로 정규화(시간=UTC ms 정수, 가격=계약된 정수 표현).
4. **`tools/live_probe.py` + `docs/06_live_facts.md`** — 라이브 리스로 **최소 호출 수**로 아래를 실측 확정:
   - 미국 시세 실시간 여부/지연(같은 심볼을 시간차 폴링해 timestamp 진행 확인)
   - `/candles 1m` **과거 보관 기간**(`before` 역방향 페이지네이션 한계) ← Phase 1 설계의 핵심 변수
   - 미국 `/orderbook` 레벨 수, `/trades` 실제 반환 건수·지연
   - `TOP_GAINERS realtime` 400 여부, 거래대금/거래량 랭킹 realtime 갱신 주기
   - 데이마켓/프리마켓 시간대의 `/prices` timestamp 거동, `/market-calendar/US` 실제 응답
   - `/commissions` 실제 수수료율, `/exchange-rate` 스프레드 관측
   - rate limit 헤더 실제 포맷, 429 재현 시 헤더값
   각 항목은 **"확인됨/미확인 + 근거 응답 스니펫"** 으로 기록. 추측을 사실로 쓰지 않는다.
5. **`tests/fixtures/live/*.json`**: 위 호출의 실응답을 마스킹해 저장(엔드포인트별 정상/에러/429/빈응답).
6. **`tools/mock_server.py`**: 픽스처 기반 로컬 서버(`--port 8899`). 동일 경로/헤더 재현 + `--inject 429|latency|schema-drift` 모드.
   **이것이 다른 모든 워커의 개발 기반이므로 최우선으로 완성하고 즉시 코디네이터에 알린다.**
7. 테스트: GET-only 차단, 토큰 단일성(2중 실행 시 실패), 429 백오프, 배치 청킹 경계(199/200/201종목), 페이지네이션, 스키마 드리프트.

### W2 — `feat/universe-store` (codex / reasoning high)

읽을 것: `docs/04_contracts.md`, `docs/03_phase1_monitor_design.md` §1~2, `docs/02_theory_background.md` §4.3·§2.4.

1. `store/`: `schema.sql`(symbols, candles_1m, candles_1d, rankings_snap, trades_snap, orderbook_snap, events, promotions, filings)
   + 버전 마이그레이션. WAL, 적절한 PK/유니크(중복 폴링 멱등성 필수: 예 `candles_1m(symbol, ts)` upsert),
   조회 인덱스, `trades_snap` 중복 제거 키(symbol+ts+price+volume).
2. `writer.py`: 배치 upsert, 트랜잭션, 초당 수천 행 쓰기에서의 커밋 배칭, 크래시 후 재시작 안전성.
   `reader.py`: 기간·심볼 슬라이스 → DataFrame(read-only 커넥션).
3. 보관 정책: 일정 기간 경과분 Parquet 아카이브 + DB 슬림화 스크립트(용량 추정치를 문서화 — 1분봉 1,500종목×390분 규모 근거 계산).
4. `universe/`: 외부 심볼 디렉토리(NASDAQ Trader 등) 수집·파싱 → `/stocks` 배치 200으로 메타 보강(mock 사용) →
   필터(보통주, ACTIVE, 가격 $0.1~$20, 시총 $10M~$300M 중심, ETF/ETN 제외) → tier0/tier1 산출.
   `runners.py`: 일봉으로 former runner(최근 N개월 일중 ±30%) 탐지. EDGAR 희석 태깅은 **인터페이스와 stub까지만**(스코프 밖 구현 금지).
5. 테스트: 스키마 마이그레이션 왕복, upsert 멱등성, 대량 쓰기 성능 스모크, 필터 경계값, 심볼 디렉토리 파싱 이상케이스.

### W3 — `feat/analyzer` (opus / high)

읽을 것: `docs/04_contracts.md`, `docs/03_phase1_monitor_design.md` §3, `docs/02_theory_background.md` §1·§2.4·§4.

1. **`tests/synth.py` 최우선**: 현실적인 1분봉/랭킹 합성 생성기 — coil→폭발형, 즉발형, 페이드형(HOD 조기형성·VWAP 상실),
   덤프형(피크 후 급락), 노이즈형. 라벨 정답을 함께 반환해 검출기 평가의 ground truth로 쓴다. 완성 즉시 코디네이터에 알린다.
2. `baselines.py`: **시간대 보정 RVOL**(분 단위 시간대별 평균 대비), ATR, 세션별 VWAP, 20일 거래량 z-score.
3. `labeling.py`: 이벤트 정의(30분 +15% 또는 당일 +30% AND RVOL≥3~5) 구현 + 파라미터화.
   라벨 필드 전부: T0, HOD 시각/수익률, 피크 후 30분·종가 되돌림, 지속시간, VWAP 대비 종가, 플로트 로테이션(가용 시),
   세션, **토스 랭킹 최초 진입 시각의 T0 대비 리드/래그**, 다음날 갭.
4. `features.py`: **T0 이전 구간만** 사용(룩아헤드 편향 금지 — 이를 검증하는 테스트를 반드시 작성).
   T-5/-15/-30/-60분 거래량 z-score·RVOL 궤적, 가격 궤적 형태, 토스 쏠림도(TOSS/MARKET 거래대금 비율) 레벨·기울기, 이력 피처.
5. `evaluate.py`: 신호 임계값별 리드타임 분포, 정밀도/재현율, 기대수익 분포(왕복 비용 1% 차감 시나리오 포함),
   §3의 **검증 질문 6개에 각각 대응하는 산출 함수**를 명시적으로 만든다. `report.py`로 마크다운 리포트 생성.
6. `docs/07_analysis_spec.md`: 각 지표의 정의식·경계조건·알려진 편향을 수식 수준으로 기록.
7. 테스트: 합성데이터에서 라벨 재현, 룩어헤드 없음 증명, 시간대 보정 정확성, 결측·홀트(캔들 공백) 처리.

### W4 — `feat/collector` (opus / xhigh, Wave 2)

읽을 것: `docs/04_contracts.md`, `docs/06_live_facts.md`(W1 실측), `docs/03_phase1_monitor_design.md` 전체.

1. `scheduler.py`: `/market-calendar/US` 기반 세션 인지 루프(데이/프리/정규/애프터), 서머타임 자동 대응, 세션 전환 시 티어 재구성.
2. `loops.py`: tier1 가격 스윕 / tier2 1분봉 / tier3 마이크로(trades+orderbook) / 랭킹 4종 스냅샷 — asyncio 단일 프로세스.
3. `detector.py`: 전조 스코어 → 티어 승격/강등 상태머신(`promotions` 기록), 이벤트 실시간 감지 → `events` 기록.
   피처 계산은 **W3의 `analysis` 함수를 재사용**한다(중복 구현 금지).
4. `budget.py`: 그룹별 실사용량 관측 → 한도 70% 초과 예측 시 **자동으로 티어 크기 축소**(안전 강등). 초과는 버그가 아니라 사고로 취급.
5. 견고성: 크래시/재시작 시 이어받기(마지막 수집 지점 복원), 네트워크 단절 복구, 로컬-서버 시간 오차 처리,
   장시간 무인 실행 메모리 안정성. `notifier.py`는 콘솔+파일 로그(텔레그램은 인터페이스만).
6. 테스트: mock 서버 + `tests/synth.py`로 **가속 리플레이 통합 테스트**(정규장 1세션을 수십 초로 압축), 429 주입 시 예산 가드 동작,
   승격/강등 히스테리시스(플래핑 방지), 재시작 이어받기.

### W5 — `feat/ops` (sonnet / high, Wave 2)

1. `ops/`: Windows 상시 실행/작업 스케줄러 등록 스크립트와 절차, 프로세스 감시·자동 재시작, 로그 로테이션,
   헬스체크(마지막 수집 시각·초당 호출수·DB 증가율·429 카운트를 한 화면으로), 디스크 용량 경보.
2. `docs/08_runbook.md`: 무인 야간 운영 절차, 장애 유형별 대응, 세션 시간표(KST), 중단·재개 체크리스트.
3. `docs/09_secret_hygiene.md` + pre-commit 시크릿 스캔 훅: `api_keys`·토큰 문자열 커밋 차단.
4. `tools/dryrun_night.py`: **라이브 리허설 1세션** 자동 리포트(수집 커버리지, rate limit 여유, 결측 구간, 이벤트 후보 목록).
   실행은 **코디네이터의 라이브 리스 부여 + 사용자 승인 후**에만.

### W6 — 적대적 감사 (opus / xhigh, 읽기 전용, 코드 수정 금지)

머지 완료된 `main`에 대해 다음을 **반증하려는 태도로** 감사하고 `docs/10_audit.md`에 근거(파일:라인)와 함께 기록:
① 토큰 단일성이 깨질 수 있는 경로(테스트·스크립트·재시작 경합 포함) ② rate limit 초과 가능 경로 ③ 룩어헤드 편향
④ 시간대/서머타임/세션 경계 버그 ⑤ 시크릿 유출 경로 ⑥ 크래시 후 데이터 무결성 ⑦ 계약 위반·중복 구현
⑧ GET-only 차단 우회 가능성. 각 항목은 **재현 시나리오(입력→잘못된 결과)** 를 포함해야 하며, 확신 없는 추측은 "미확인"으로 분류한다.
코디네이터는 감사 결과를 심각도 순으로 정리해 패치 태스크로 전환한다.

---

## 5. 머지·검증 게이트 (코디네이터가 매 브랜치마다 수행)

머지 전 5개를 모두 통과해야 한다. 하나라도 실패하면 재작업 메시지(구체적 실패 지점 + 기대 동작)를 보낸다.

1. `pytest` 전체 통과 로그 확인 (워커 주장 신뢰 금지 — 코디네이터가 직접 재실행)
2. `git diff --stat main...<branch>` — **소유권 밖 파일 변경 0건**
3. `docs/04_contracts.md` 시그니처 준수 여부 diff 리뷰 (임의 변경은 반송)
4. 금지사항 스캔: 주문 계열 엔드포인트 문자열, `api_keys` 참조, 하드코딩 시크릿, 라이브 URL 직접 호출
5. 머지 후 통합 스모크: mock 서버 기동 → collector 가속 리플레이 → DB 생성 → analyzer 리포트 생성까지 **엔드투엔드 1회**

머지는 `git merge --no-ff feat/<x>`로 main에서 수행. 충돌 발생 시 워커에게 `git rebase main` 후 재보고를 지시한다.

---

## 6. 최종 산출물 (Phase 1 완료 정의)

- mock 서버만으로 CI처럼 전 파이프라인이 도는 상태 + 라이브 1세션 리허설 리포트
- `docs/06_live_facts.md`의 실측 항목이 전부 "확인됨"으로 채워짐 (특히 1분봉 보관 기간)
- 정규장 1세션 무인 수집 후 `events` 테이블에 라벨된 이벤트가 쌓이고, `analyzer` 리포트가 **검증 질문 6개에 수치로 답**함
- `docs/10_audit.md`의 심각도 높은 항목이 모두 해소 또는 명시적 수용

---

## 7. 지금 할 일

1. §0 확인 3개 실행 → 실제 가능한 모델/effort 지정 방식 한 줄 보고
2. Wave 0 수행(계약 + 스켈레톤 + 초기 커밋)
3. 사용자에게 10줄 이내 승인 요청
4. 승인 후: run 생성 → W1·W2·W3 태스크 생성(의존성 설정) → worktree/에이전트 배치표대로 워커 기동 →
   §3 불변 규칙을 각 워커 프리앰블에 **그대로** 포함 → `check --wait` 롤링 대기

과도한 분기는 금지다. 워커를 늘리고 싶어지면, 먼저 계약을 더 좁게 다시 써서 기존 워커가 해결할 수 있는지 검토한다.

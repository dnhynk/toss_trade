# 04 — 계약 (Contracts) : 이번 오케스트레이션의 헌법

> 이 문서의 시그니처·규약은 **코디네이터 승인 없이 변경 금지**.
> 변경이 필요하면 코드를 고치지 말고 `ask`로 근거와 제안을 보낼 것.
> 근거 문서: `docs/01_api_analysis.md`(API 사실), `docs/03_phase1_monitor_design.md`(설계).

## C-1. 시간 규약 (최우선)

- 모든 타임스탬프는 **UTC epoch milliseconds, Python `int`** (`ts_ms`). 저장·전달·비교 전부.
- API 응답의 ISO 8601(+09:00 등) 문자열은 파싱 즉시 `ts_ms`로 변환한다. 변환 헬퍼는 `tossmon/api/models.py`:
  - `iso_to_ms(s: str) -> int` — 오프셋 포함 ISO → UTC ms. naive 문자열은 `SchemaMismatch`.
  - `ms_to_iso_kst(ts_ms: int) -> str`, `ms_to_iso_et(ts_ms: int) -> str` — **표시/로그 전용**.
- KST/ET/서머타임 하드코딩 금지. 세션 판정은 `/market-calendar/US` 응답(`UsMarketDay`)만 사용.
- DB의 시간 컬럼은 전부 `INTEGER`(ms)이며 컬럼명은 `*_ms` 접미사.

## C-2. 가격·수량 표현

- float 금지 (계산 중간값 포함). API decimal 문자열은 `decimal.Decimal`로만 파싱.
- 저장·전달 단위 (모두 Python `int`):
  - 가격: **마이크로달러** `price_u` (1 USD = 1_000_000). KRW가 등장할 일은 없다(US 전용).
  - 수량: **마이크로주** `qty_u` (1주 = 1_000_000; 소수점 체결 대응).
  - 금액: **마이크로달러** `amount_u`.
- 헬퍼 (`tossmon/api/models.py`):
  - `dec_to_u(s: str | Decimal) -> int` — 문자열/Decimal → 마이크로 단위 int (초과 정밀도는 `SchemaMismatch`).
  - `u_to_dec(u: int) -> Decimal`
- 비율(RVOL, 등락률, z-score 등)은 분석 레이어에서 `float` 허용 (저장 원본은 항상 int).

## C-3. 데이터 모델 (`tossmon/api/models.py`, 전부 `@dataclass(frozen=True, slots=True)`)

```python
Price(symbol: str, ts_ms: int | None, last_u: int)
Candle(symbol: str, ts_ms: int, open_u: int, high_u: int, low_u: int, close_u: int, vol_qu: int)
CandlePage(candles: list[Candle], next_before_ms: int | None)
Trade(symbol: str, ts_ms: int, price_u: int, qty_u: int)
OrderbookLevel(price_u: int, qty_u: int)
Orderbook(symbol: str, ts_ms: int | None, bids: list[OrderbookLevel], asks: list[OrderbookLevel])
RankingRow(rank: int, symbol: str, last_u: int, base_u: int, change_rate: float | None,
           vol_qu: int, amount_u: int)
RankingPage(ranking_type: str, duration: str, ranked_at_ms: int | None, rows: list[RankingRow])
StockMeta(symbol: str, name: str, market: str, security_type: str, is_common: bool,
          status: str, list_date: str | None, shares_outstanding_qu: int)
SessionWindow(start_ms: int, end_ms: int)
UsMarketDay(date: str, day: SessionWindow | None, pre: SessionWindow | None,
            regular: SessionWindow | None, after: SessionWindow | None)
```

## C-4. API 레이어 시그니처 (`tossmon/api/`)

```python
# tokens.py — 단일 토큰 보장. OS 파일락(filelock) + 상태파일(json: token, expires_at_ms).
class TokenManager:
    def __init__(self, keys_path: Path, state_path: Path, live: bool): ...
    async def get(self) -> str            # 유효 토큰 반환. 만료 60s 전 선제 재발급.
    async def invalidate(self) -> None    # AuthExpired 수신 시 호출 → 다음 get()이 재발급
    # live=False면 발급 시도 자체가 RuntimeError. 파일락 획득 실패(다른 프로세스 보유)면 즉시 예외 — 발급 강행 금지.

# limiter.py — 그룹별 토큰버킷. 기본 사용률 = 공시 한도 × usage_ratio(기본 0.7).
class GroupRateLimiter:
    def __init__(self, limits: dict[str, float], usage_ratio: float = 0.7): ...
    async def acquire(self, group: str) -> None          # 슬롯 확보까지 대기
    def update_from_headers(self, group: str, headers: Mapping[str, str]) -> None
    def on_429(self, group: str, retry_after_s: float) -> None

# client.py — GET-only 하드 차단. 배치 청킹. 재시도. 모델 정규화.
class TossClient:
    def __init__(self, base_url: str, tokens: TokenManager, limiter: GroupRateLimiter,
                 timeout_s: float = 10.0): ...
    async def get_prices(self, symbols: Sequence[str]) -> list[Price]           # 자동 200개 청킹
    async def get_candles(self, symbol: str, interval: Literal["1m", "1d"],
                          count: int = 200, before_ms: int | None = None,
                          adjusted: bool = True) -> CandlePage
    async def get_trades(self, symbol: str, count: int = 50) -> list[Trade]
    async def get_orderbook(self, symbol: str) -> Orderbook
    async def get_rankings(self, ranking_type: str, duration: str = "realtime",
                           market: str = "US", count: int = 100,
                           exclude_caution: bool = False) -> RankingPage
    async def get_stocks(self, symbols: Sequence[str]) -> list[StockMeta]       # 자동 200개 청킹
    async def get_us_calendar(self, date: str | None = None) -> dict[str, UsMarketDay]
        # keys: "previous" | "today" | "next"
    async def get_exchange_rate(self) -> Decimal                                 # KRW per USD
    async def aclose(self) -> None
```

- **HTTP 허용 목록 (이 밖은 메서드/전송 레벨 이중 차단, 위반 시 `ForbiddenEndpoint` raise)**:
  `POST /oauth2/token` + GET `/api/v1/prices`, `/candles`, `/trades`, `/orderbook`, `/price-limits`,
  `/rankings`, `/stocks`, `/stocks/{symbol}/warnings`, `/market-calendar/KR`, `/market-calendar/US`,
  `/exchange-rate`, `/accounts`, `/commissions`.
  주문·조건주문·잔고변경 계열은 **GET 포함 어떤 메서드도 금지**(래퍼 자체를 만들지 않는다).
- 전송 차단 구현: 단일 `_request(method, path, ...)` 관문에서 allowlist 검사. 테스트 필수.

## C-5. 에러 분류 (`tossmon/api/errors.py`)와 재시도 책임

```python
class TossApiError(Exception): ...
class RateLimited(TossApiError):      # .retry_after_s: float
class AuthExpired(TossApiError): ...
class TransientHTTP(TossApiError):    # .status: int  (5xx, 타임아웃, 연결오류)
class SchemaMismatch(TossApiError):   # .detail: str  (필수필드 결손, 파싱 불가)
class Forbidden(TossApiError):        # .reason: str  (403: IP 미등록/권한)
class ForbiddenEndpoint(TossApiError):# allowlist 위반 — 잡지 말 것(버그)
```

| 예외 | 처리 주체 | 정책 |
|---|---|---|
| RateLimited | **client 내부** | limiter.on_429 반영 후 Retry-After 대기, 1회 재시도. 재실패 시 caller로 전파 |
| AuthExpired | **client 내부** | tokens.invalidate() 후 1회 재시도. 재실패 시 전파 |
| TransientHTTP | **client 내부** | 지수 백오프(0.5/1/2s + jitter) 최대 3회. 재실패 시 전파 |
| SchemaMismatch | **caller(루프)** | 재시도 금지. 로그 + 해당 심볼 스킵 + 카운터 증가 |
| Forbidden | **caller → 최상위** | 치명. 수집 중단 + notifier 경보 (IP/권한 문제) |
| ForbiddenEndpoint | 없음 | 코드 버그. 테스트로만 발생해야 함 |

## C-6. 스토리지 계약 (`tossmon/store/`)

- SQLite, `PRAGMA journal_mode=WAL`. **쓰기 주체는 collector 단일 프로세스뿐.**
  분석기·리포트는 read-only URI(`file:...?mode=ro`)로만 연다.
- 테이블(전부 `schema.sql`에, 버전은 `meta(schema_version)`):
  - `symbols(symbol PK, name, market, security_type, status, list_date, shares_outstanding_qu, tier, is_former_runner, updated_ms)`
  - `candles_1m(symbol, ts_ms, open_u, high_u, low_u, close_u, vol_qu, PRIMARY KEY(symbol, ts_ms))` — upsert 멱등
  - `candles_1d(symbol, ts_ms, ..., 동일, PRIMARY KEY(symbol, ts_ms))`
  - `rankings_snap(id INTEGER PK AUTOINC, snap_ms, ranking_type, duration, rank, symbol, last_u, vol_qu, amount_u, UNIQUE(snap_ms, ranking_type, duration, rank))`
  - `trades_snap(symbol, ts_ms, price_u, qty_u, PRIMARY KEY(symbol, ts_ms, price_u, qty_u))` — 폴링 중복 무시(INSERT OR IGNORE)
  - `orderbook_snap(id PK, symbol, snap_ms, ts_ms, bid1_u, bid1_qu, ask1_u, ask1_qu, depth_json, spread_u, imbalance REAL)`
  - `events(id PK, symbol, t0_ms, kind, peak_ms, peak_ret REAL, ret_30m REAL, ret_close REAL, session, meta_json)`
  - `promotions(id PK, symbol, ts_ms, from_tier, to_tier, reason, score REAL)`
  - `filings(symbol, kind, filed_ms, meta_json, PRIMARY KEY(symbol, kind, filed_ms))` — Phase 1은 스텁
- 시그니처:

```python
class Store:  # writer.py
    def __init__(self, db_path: Path, read_only: bool = False): ...
    def upsert_candles_1m(self, rows: Iterable[Candle]) -> int      # 반환: 신규/갱신 행 수
    def upsert_candles_1d(self, rows: Iterable[Candle]) -> int
    def upsert_symbols(self, rows: Iterable[StockMeta], tier: int | None = None) -> int
    def insert_rankings(self, snap_ms: int, page: RankingPage) -> int
    def insert_trades(self, trades: Iterable[Trade]) -> int          # 중복은 조용히 무시
    def insert_orderbook(self, snap_ms: int, ob: Orderbook) -> int
    def record_promotion(self, symbol: str, ts_ms: int, from_tier: int, to_tier: int,
                         reason: str, score: float) -> None
    def record_event(self, ev: dict) -> int
    def close(self) -> None

class Reader:  # reader.py (read-only 커넥션)
    def read_candles_1m(self, symbol: str, t_from_ms: int, t_to_ms: int) -> pd.DataFrame
    def read_candles_1d(self, symbol: str, t_from_ms: int, t_to_ms: int) -> pd.DataFrame
    def read_rankings(self, ranking_type: str, t_from_ms: int, t_to_ms: int) -> pd.DataFrame
    def read_events(self, t_from_ms: int, t_to_ms: int) -> pd.DataFrame
    def symbols(self, tier: int | None = None) -> pd.DataFrame
```

- DataFrame 규약: 시간 컬럼은 `ts_ms`(int64) 그대로. index 변환·tz 부여는 각 분석 함수 내부에서만.

## C-7. 분석 계약 (`tossmon/analysis/`)

```python
# baselines.py
compute_daily_baseline(df_1d: pd.DataFrame) -> dict        # {adv20_qu, atr20_u, vol_z_params...}
minute_of_session_volume_curve(df_1m: pd.DataFrame, calendar: list[UsMarketDay]) -> pd.Series
rvol(df_1m: pd.DataFrame, curve: pd.Series, ts_ms: int) -> float
session_vwap_u(df_1m: pd.DataFrame, session: SessionWindow) -> pd.Series

# labeling.py
@dataclass EventParams(window_min: int = 30, ret_min: float = 0.15, day_ret_min: float = 0.30,
                       rvol_min: float = 3.0)
detect_events(df_1m: pd.DataFrame, params: EventParams) -> pd.DataFrame
  # 반환 컬럼: t0_ms, kind, peak_ms, peak_ret, ret_30m, ret_close, session — docs/03 §3 라벨 전부

# features.py — 룩어헤드 금지: t0_ms 이전 데이터만 입력으로 받는다(강제: 함수가 잘라서 검증)
extract_precursor_features(df_1m: pd.DataFrame, rankings: pd.DataFrame,
                           t0_ms: int, windows_min: tuple[int, ...] = (5, 15, 30, 60)) -> dict[str, float]

# evaluate.py — docs/03 §3 검증 질문 6개와 1:1 대응하는 함수 6개 + 종합
q1_volume_leadtime(events, feats) -> pd.DataFrame
q2_ranking_lead_lag(events, rankings) -> pd.DataFrame
q3_daymarket_persistence(events) -> pd.DataFrame
q4_dump_speed(events, df_1m) -> pd.DataFrame
q5_expectancy(events, feats, cost_roundtrip: float = 0.01) -> pd.DataFrame
q6_time_of_day(events) -> pd.DataFrame
```

## C-8. 컬렉터 계약 (`tossmon/collector/`)

```python
# scheduler.py
current_session(cal: dict[str, UsMarketDay], now_ms: int) -> str   # "day"|"pre"|"regular"|"after"|"closed"
# loops.py — asyncio 단일 프로세스, 각 루프는 독립 task
run_tier1_price_sweep(client, store, cfg) / run_tier2_candles(...) / run_tier3_micro(...) / run_rankings(...)
# detector.py
class TierStateMachine:  # 승격/강등 + 히스테리시스(플래핑 방지), promotions 기록
    def on_new_data(self, symbol: str, score: float, ts_ms: int) -> int | None  # 새 tier 또는 None
precursor_score(feats: dict[str, float]) -> float
# budget.py
class BudgetGuard:
    def on_request(self, group: str) -> None
    def should_shrink(self) -> dict[str, int] | None   # 그룹별 초과 예측 시 티어 축소 지시
```

## C-9. 오프라인 개발 규약

- `TOSS_BASE_URL` env로 서버 전환. 기본값 없음 — **설정 파일에 명시 필수**.
  mock: `http://127.0.0.1:8899` (W1의 `tools/mock_server.py`).
- `TOSS_LIVE` env: `1`이 아니면 TokenManager가 실발급을 거부하고 mock 고정 토큰 사용.
- **W1 외 모든 워커는 mock만 사용한다.** W1 픽스처 도착 전에는 `docs/01_api_analysis.md`의
  응답 사양으로 자체 스텁 JSON을 만들어 개발한다 (픽스처 도착 후 재검증).
- 설정 단일 출처: `tossmon/config.py`가 `config/config.yaml`(+env 오버라이드)을 로드한
  `Config` dataclass 하나. 워커는 설정 키를 임의 추가하지 말고 `ask`.

## C-10. 의존성 (고정 — 추가는 코디네이터 승인 후 main에서만)

- 런타임: `httpx`, `pyyaml`, `filelock`, `pandas`, `pyarrow`
- 개발: `pytest`, `pytest-asyncio`, `anyio`
- 표준 라이브러리 우선 (sqlite3, decimal, dataclasses, zoneinfo). uvicorn/flask 금지 —
  mock 서버는 `http.server` 또는 `asyncio` 표준 라이브러리로.

## C-11. 금지 목록 (재확인)

1. 주문/조건주문/계좌변경 코드 절대 금지 (래퍼·문자열 상수 포함).
2. `api_keys` 읽기는 **라이브 리스 보유 워커의 TokenManager 경로**로만. 내용 출력·로그·커밋 금지.
3. 라이브 호출은 리스 보유 워커만. 그 외 `openapi.tossinvest.com` 문자열이 코드에 하드코딩되면 반송.
4. 소유 경로 밖 파일 수정 금지. `pyproject.toml`·`config/*`·`docs/04_*` 변경은 `ask`.
5. float 가격 연산, naive datetime, KST 하드코딩 — 리뷰 반송 사유.

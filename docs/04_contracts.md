# 04 — 계약 (Contracts) : 이번 오케스트레이션의 헌법

> **[현행]** 소유 코디 · 2026-08-09 · ★ **계약 정본. 다른 문서와 상충하면 이 문서가 이긴다**
> 상태 표기의 뜻과 전수 목록: [`docs/INDEX.md`](INDEX.md)

<details>
<summary><b>이 문서의 지도 — 절 12개 (463줄)</b></summary>

- C-1. 시간 규약 (최우선)
- C-2. 가격·수량 표현
- C-3. 데이터 모델 (`tossmon/api/models.py`, 전부 `@dataclass(frozen=True, slots=True)`)
- C-4. API 레이어 시그니처 (`tossmon/api/`)
- C-5. 에러 분류 (`tossmon/api/errors.py`)와 재시도 책임
- C-6. 스토리지 계약 (`tossmon/store/`)
- C-7. 분석 계약 (`tossmon/analysis/`)
- C-8. 컬렉터 계약 (`tossmon/collector/`)
- C-9. 오프라인 개발 규약
- C-10. 의존성 (고정 — 추가는 코디네이터 승인 후 main에서만)
- C-11. 금지 목록 (재확인)
- C-2 개정 (2026-08-03) — 랭킹 `tradingAmount` 는 KRW 표기 (예외)

</details>

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

### C-2 개정 A4 (2026-07-30, W5 라이브 발견 → 코디네이터 결정) — 초과 정밀도는 반올림한다

**발견**: 라이브 `/candles 1m` 응답에서 BTAI(주당 $0.10)의 `openPrice`가 `"0.10461614"` —
소수 **8자리**였다. 기존 `dec_to_u`는 1e-6 초과 정밀도를 `SchemaMismatch`로 거부하고,
C-5에 따라 caller가 그 심볼을 스킵한다.

**왜 심각한가**: 스킵되는 것이 하필 **이 전략의 표적 종목군(저가 동전주)** 이다.
$1 미만 종목은 미국 규정상 호가 단위가 더 세분화되고(sub-penny), 파생값(수정주가)은
임의 정밀도를 가질 수 있다. 그대로 두면 Tier 1/2/3 전 구간에서 동전주만 조용히 누락되어
first_print 감지·RVOL·이벤트 라벨링이 **표적 종목에서만 무력화**된다.
조용한 누락은 에러보다 나쁘다 — 아무도 모르는 채로 전략이 빈다.

**결정**:
1. **단위는 마이크로달러(1e-6) 유지.** 나노달러로 바꾸지 않는다 — 가격×수량 누적이
   int64를 1000배 빨리 넘긴다(W3가 이미 VWAP에서 오버플로를 잡았다). 또한 sub-penny
   최소호가단위(Rule 612, $1 미만 $0.0001)보다 마이크로달러가 이미 100배 미세하므로
   **거래 가능한 가격을 표현하기에 충분하다.**
2. **`dec_to_u`는 초과 정밀도를 `SchemaMismatch`로 던지지 않고 `ROUND_HALF_EVEN`으로
   반올림한다.** 반올림이 발생하면 카운터를 증가시켜 관측 가능하게 한다(조용히 넘어가지 않는다).
   `SchemaMismatch`는 **파싱 자체가 불가능한 값**(빈 문자열, 숫자 아님, 음수 가격 등)에만 쓴다.
3. 반올림 오차의 크기: $0.10 종목에서 1e-6은 상대오차 0.001%다. RVOL·수익률·이벤트 검출에
   영향이 없다. **단, 주문 가격 계산에는 절대 쓰지 마라** — Phase 1에 주문 코드는 없고,
   Phase 2에서 주문가를 만들 때는 원문 문자열을 별도로 보존해야 한다.
4. `adjusted=false`로 도피하지 않는다. 우리 유니버스는 역분할 종목이 핵심이라
   (docs/02 §2.4) 과거 베이스라인에는 수정주가가 필요하다.

**적용 범위**: 가격·금액·수량 파싱 전 경로(`prices/candles/trades/orderbook/rankings`).

**W5 진단 결과 (확정)**: 원인은 **수정주가(`adjusted=true`)의 분할 보정 곱셈**이다.
- 같은 CRKN 봉(2025-03-05): `adjusted=true` → `0.10461614`(8자리),
  `adjusted=false` → `1.9889`(4자리). 비율 ≈ 19.01.
- 범위: `/prices`·`/trades`·`/orderbook`과 분할 이력이 없는 종목의 캔들은 전부 4자리 이하로 깨끗.
  **분할 이력이 있는 종목의 `adjusted=true` 캔들에 한정**된다.
- 그러나 이 조건은 예외가 아니라 **표적 종목의 정상 상태**다 — 저가 러너는 리버스 스플릿을
  거친 종목이 다수다(docs/02 §2.4). 따라서 A4의 반올림 정책은 그대로 유효하고 필수다.
  (최초 보고의 심볼 지목은 BTAI였으나 실제는 CRKN — W5가 정정했다.)

### C-2 개정 A5 (2026-07-30) — 1분봉은 원주가, 일봉은 수정주가

A4 진단이 드러낸 **분석상의 함정**: 수정주가는 과거 가격을 분할비로 나눈 값이라
**그 시점의 명목 가격을 지운다.** CRKN은 2025-03-05에 실제로는 $1.99에 거래됐는데
수정주가로는 $0.10로 보인다. 이 프로젝트의 논지는 "동전주 가격대에서 벌어지는 현상"인데,
수정주가로 과거를 보면 **당시 $2 종목을 동전주로 오분류**한다. 나아가 미국 시장의
가격대별 제도(sub-penny 호가, LULD 밴드 $0.75/$3 경계)도 전부 명목 가격 기준이다.

**결정**:
- **1분봉은 `adjusted=false`(원주가)로 수집·저장한다.** 명목 가격대와 그 시점의 미시구조
  체제를 보존해야 하기 때문이다. 분할은 장중에 일어나지 않으므로 **일중 상대 변화
  (수익률·RVOL·VWAP 괴리)는 원주가로도 완전히 동일**하다 — 잃는 것이 없다.
- **일봉은 `adjusted=true`(수정주가)로 수집·저장한다.** 20일 베이스라인·ATR·former runner
  탐지처럼 **분할을 가로지르는 다일 계산**은 연속성이 필요하다.
- **금지**: 1분봉(원주가)과 일봉(수정주가)을 **가격 수준으로 직접 비교하지 마라.**
  다일 가격 비교는 일봉으로만 한다. 비율·수익률 비교는 각 계열 안에서만.
- 거래량도 분할 보정의 영향을 받으므로 같은 규칙을 따른다(1분봉 원, 일봉 수정).

**적용**: `TossClient.get_candles`의 `adjusted` 기본값은 바꾸지 않는다(호출자가 명시).
collector와 universe 빌더가 호출 시점에 `interval`에 맞춰 명시적으로 넘긴다.

### C-4/C-5/C-9 개정 A6 (2026-07-31, 적대적 감사 반영 → 코디네이터 승인)

감사 2건이 **독립적으로** 확인한 치명 결함: 토큰 리스가 `{state_path}.lock`(상대경로) 기반이라
**CWD 종속**이었다. 워크트리마다 다른 락을 잡아 두 프로세스가 동시에 토큰을 발급하는 것이
실측 재현됐다 — **client당 토큰 1개라는 이 프로젝트의 최우선 제약을 지키는 장치가 애초에
작동하지 않고 있었다.**

1. **`ForbiddenEndpoint`는 `TossApiError`를 상속하지 않는다** (`Exception` 직속).
   계약이 "잡지 말 것"이라고 쓴 것을 **타입이 강제**하게 한다. 상위 `except TossApiError`에
   삼켜지면 Phase 2에서 주문 차단 가드레일이 warn 한 줄로 무력화된다.
   **주의**: 이 변경으로 `except (TossApiError, ...)`가 더 이상 잡지 않으므로,
   호출 루프는 `except ForbiddenEndpoint`를 **명시적으로** 두어 shutdown+경보해야 한다
   (안 두면 미처리 예외로 루프가 죽는다).
2. **`TokenManager.invalidate(token=None)`** — CAS. 인자로 받은 토큰이 현재 토큰과 같을 때만
   무효화한다. 무조건 삭제하면 다른 요청이 방금 발급한 유효 토큰까지 날린다.
3. **`TokenManager.__init__(..., limiter=None)`** — 토큰 발급이 AUTH rate limit을 우회하던 것을
   막는다. `TossClient`가 자동 주입하므로 기존 호출자는 변경 불필요.
4. **리스는 자격증명 단위**로 잡는다 — `sha256(client_id)` 유도 + **리포 밖 고정 위치**.
   상태파일 경로를 다르게 준 두 프로세스도 같은 락을 잡아야 한다.
   위치 재정의는 **`TOSSMON_LEASE_DIR`** env (미설정 시 `LOCALAPPDATA`(win) /
   `XDG_STATE_HOME`(posix) → `~/.local/state/tossmon`). 테스트 격리에 필수.
5. **헤더 기반 rate 상향의 천장**은 `max(SPEC_LIMITS[group], config값)`이다.
   감사 제안(`min(limit, SPEC_LIMITS)`)보다 완화한 것인데, 운영자가 config에 공시값보다 높게
   잡은 것은 **사람의 의도적 결정**이라 코드가 조용히 덮어쓰면 안 되기 때문이다.
   서버 헤더가 천장을 넘기는 것만 막는다는 목적은 동일하게 달성된다.

**전환 규칙 (중요)**: 리스 위치가 바뀌었으므로 **구코드 프로세스의 락은 신코드에게 보이지 않는다.**
구코드 라이브 프로세스를 **완전히 종료하고 락 해제를 확인한 뒤에만** 신코드 라이브 프로세스를
띄운다. 이 절차를 어기면 고친 것과 정확히 같은 상호 토큰 살해가 재발한다.

### C-4 개정 A7 (2026-07-31) — 허용 목록 축소, 상태파일 권한

1. **`/api/v1/market-calendar/KR` 를 ALLOWLIST 에서 제거한다.**
   이 프로젝트는 미국 주식 전용이고(docs/00 §1), 감사 G-4 가 지적한 대로 쓰이지 않는 항목은
   공격 표면일 뿐이다. W1 이 감사의 주장을 검증해 **실제로 미사용인 것은 이 하나뿐**임을
   확인했다(나머지 4개는 `live_probe` 가 `_request` 로 사용 중). 필요해지면 계약 개정으로 다시 넣는다.
2. **토큰 상태파일 권한을 코드로 보장한다.** W1 이 `icacls` 로 실측해 현재 world 접근이 없음을
   확인했으나, 그건 **상속 ACL 이라 코드의 보장이 아니다**(감사 E-2). 소유자 전용으로 명시 설정한다.
   - POSIX: `0600`
   - Windows: 소유자/SYSTEM 만 남기는 ACL
   - **실패해도 토큰 흐름을 죽이지 않는다** — 권한 설정 실패는 경고로 남기고 진행한다.
     플랫폼별 ACL 조작은 그 자체가 깨질 수 있으므로, 보안 강화가 가용성을 무너뜨리지 않게 한다.

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
    def __init__(self, keys_path: Path, state_path: Path, live: bool, limiter=None): ...
    async def get(self) -> str            # 유효 토큰 반환. 만료 60s 전 선제 재발급.
    async def invalidate(self, token: str | None = None) -> None   # CAS. A6 참조
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
  `/rankings`, `/stocks`, `/stocks/{symbol}/warnings`, `/market-calendar/US`,
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
class ForbiddenEndpoint(Exception): # allowlist 위반 — 잡지 말 것(버그). A6: TossApiError 밖
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
detect_events(df_1m: pd.DataFrame, params: EventParams, *,
              calendar=None, rvol_series=None, prev_close_u=None,
              shares_outstanding_qu=None, rankings=None) -> pd.DataFrame
  # 반환 컬럼: t0_ms, kind, peak_ms, peak_ret, ret_30m, ret_close, session — docs/03 §3 라벨 전부

# features.py — 룩어헤드 금지: t0_ms 이전 데이터만 입력으로 받는다(강제: 함수가 잘라서 검증)
extract_precursor_features(df_1m: pd.DataFrame, rankings: pd.DataFrame,
                           t0_ms: int, windows_min: tuple[int, ...] = (5, 15, 30, 60), *,
                           curve=None, baseline=None, shares_outstanding_qu=None,
                           prior_events=None, include_t0: bool = False) -> dict[str, float]

# evaluate.py — docs/03 §3 검증 질문 6개와 1:1 대응하는 함수 6개 + 종합
q1_volume_leadtime(events, feats) -> pd.DataFrame
q2_ranking_lead_lag(events, rankings) -> pd.DataFrame
q3_daymarket_persistence(events) -> pd.DataFrame
q4_dump_speed(events, df_1m) -> pd.DataFrame
q5_expectancy(events, feats, cost_roundtrip: float = 0.01) -> pd.DataFrame
q6_time_of_day(events) -> pd.DataFrame
```

### C-7 개정 A1 (2026-07-30, W3 요청 → 코디네이터 승인)

1. **룩어헤드 컷오프**: 아래 **C-7 개정 A2 (2026-08-09)** 로 대체됨. 원문은 이력으로 남긴다:
   > ~~`extract_precursor_features`는 **엄격히 `ts_ms < t0_ms`** (T0 봉 자체 제외).
   > 랭킹도 `snap_ms < t0_ms`. … 단 `include_t0: bool = False` 키워드를 제공한다 —
   > **W4 실시간 검출기는 T0 봉 종료 시점에 판정하므로 T0 봉을 정당하게 볼 수 있다.**~~
2. **선택 인자 확장**: 위치인자 시그니처는 불변, `*` 뒤 기본값 None 키워드 전용 인자만 추가 —
   계약 변경이 아닌 확장으로 인정한다 (호출 호환성 100% 유지가 조건).
3. **`events.kind`** = 트리거 종류 `'win' | 'day' | 'both'`. 형태 분류는 `shape`,
   결과 분류는 `outcome`(hold/fade/dump) 별도 컬럼.

### C-7 개정 A2 (2026-08-09, 사용자 승인 — 컷오프를 **관측 가능성** 기준으로)

**왜 고치는가**: A1 §1 의 근거 문장(*"W4 실시간 검출기는 T0 봉 종료 시점에 판정하므로"*)은
**봉 라벨이 시작 시각이라는 전제**에서 나왔다. 그 전제가 틀렸다 —
`candles_1m.ts_ms` 는 **종료 시각 라벨**이고(`docs/12` §6.1, 2026-08-07 개정,
세 갈래 독립 검증 98.9%/99.1%), `ts_ms = T` 인 봉은 `[T−60초, T)` 를 담아 **`T` 에 이미 완결**이다.
즉 **T0 봉은 실시간 검출기에만이 아니라 누구에게나 t0 에 관측 가능**하다.

**개정 내용 — 봉과 스냅을 갈라서 정한다:**

1. **캔들(구간, 종료 라벨)**: `extract_precursor_features` 의 컷오프는 **`ts_ms <= t0_ms`**.
   근거는 위 관측 가능성이다. `include_t0` 키워드는 **유지**하되 그 뜻이 바뀐다 —
   이제 "실시간이냐 연구냐"의 구분이 아니라 **`cutoff_mode` 표기용**이며,
   기본값은 관측 가능 컷오프다(`docs/12` §1 P1 예측 1a 개정 상자의 표기 규약을 따른다).

2. **랭킹(순간, 도착 지연 있음)**: **`snap_ms < t0_ms` 엄격을 유지한다.**
   봉과 달리 랭킹은 **구간이 아니라 순간**이고, W1 실측으로 **도착 시 중앙 16.1초 늙어 있다**
   (`docs/35`, 서버 10초 격자·순위열 기준). 즉 `snap_ms = t0` 인 스냅은 **t0 에 손에 없다.**
   **여기서 `<=` 로 넓히면 그것은 진짜 룩어헤드다.**

3. **미래 데이터를 넣어도 결과가 동일함을 증명하는 테스트**는 그대로 의무다.
   단 그 테스트는 **절대 시각으로 고정**해야 한다 — 합성기와 소비자가 같은 규약을 쓰면
   라벨 결함을 통과시킨다는 것이 `docs/36` §4 에서 실증됐다.

**방향 고지**: 이 개정은 **자기유리**다(이벤트당 캔들 1봉 증가).
`docs/48`(W7 개정문)이 유리해질 수 있는 판정 7개를 열거했고,
그중 §3 Q1 재현율은 **동어반복이 된다**는 것이 함께 기록됐다.
**사후 개정 중 처음으로 자기불리 방패가 없는 건이므로, 인용할 때 이 문단을 함께 인용할 것.**

**승인**: 사용자(2026-08-09). 지시도 사용자다 — 코디네이터가 발의하지 않았다.
관련: `docs/12` §1 P1 예측 1a 개정 · `docs/48_cutoff_amendment.md`
4. **추가 라벨 컬럼 허용**: C-7 명시 7컬럼 유지 + `hod_ms/hod_ret, retrace_30m/retrace_close,
   duration_min, time_to_peak_min, vwap_close_rel/closed_below_vwap, float_rotation,
   ranking_first_entry_ms/ranking_lead_lag_min, next_day_gap, t0_min_from_open, rvol_at_t0,
   halt_gap_count, shape, outcome`.
   의미 고정: **`ret_30m` = T0+30분 수익률(진입 기준)**, 피크 후 되돌림은 `retrace_*`로 분리.
5. **RVOL 정의**: 이벤트 게이트의 RVOL은 **세션 누적** 기준
   (세션시작~t 누적거래량 / 같은 (세션, 분위치)의 평균 누적거래량). docs/02 §4.1 스캐너
   임계값(3~5x)이 누적 기준 지표이기 때문. 단일 분봉 기준은 `rvol_bar`로 별도 제공.
6. **RVOL 게이트 미가용 시**: 예외를 던지지 않고 가격 조건만으로 검출하되
   `rvol_at_t0=NaN, rvol_gated=False`로 명시한다.
   **추가 의무**: `evaluate.py`의 q1~q6는 `rvol_gated=False` 이벤트를 기본 집계에서 제외하거나
   최소한 별도 열로 분리 보고해야 한다 — 게이트 미적용 이벤트가 정밀도/재현율 통계를
   조용히 오염시키는 것을 막기 위함.

### C-6/C-8 개정 A2 (2026-07-30, W1 라이브 실측 결과 반영 — 코디네이터 결정)

근거: `docs/06_live_facts.md`(W1 실측 53콜, 프리마켓 세션). 설계 전제 2건이 깨졌고 1건이 유리하게 해소됐다.

1. **미국 호가는 최우선 1레벨뿐** (실측: 6종목 전부 bids 1 / asks 1, depth 파라미터 없음).
   - `orderbook_snap.depth_json`은 1레벨만 담긴다 (스키마 변경 없음 — 향후 확장 여지 유지).
   - ~~`imbalance` = `bid1_qu / (bid1_qu + ask1_qu)`~~ → **개정 A3 참조 (부호형으로 변경)**.
   - `spread_u` = `ask1_u - bid1_u`. 상대 스프레드는 분석 레이어에서 `spread_u / mid`로 계산.
   - **Tier 3 감시 비중 재배분**: 호가 폴링 주기를 늘리고(예: 15~20s) 그만큼 `/trades` 폴링을
     조밀하게(예: 3~5s) 한다. config 키는 `polling.tier3_trades_s` / `polling.tier3_orderbook_s`
     로 분리한다 (구 `tier3_micro_s` 폐기 — 두 주기는 목적·제약이 달라 하나로 묶으면 튜닝이 엉킨다).
     **예산 제약**: MARKET_DATA 7.0 req/s(70%) 안에서 `tier3_max/trades_s + tier3_max/orderbook_s
     + tier1스윕 ≤ 7.0` 이어야 한다. 기본값 20종목 + 4s/16s = 6.42 req/s.
     `tier3_max=30`은 9.54 req/s로 **예산 초과**이므로 기본값을 20으로 낮췄다.
     BudgetGuard는 이 산식을 런타임에 재검증하고 초과 예측 시 tier3를 자동 축소해야 한다. 근거는 문헌 정합적이다 — La Morgia et al.의 최상위 피처는
     호가가 아니라 **테이프의 시장가 매수 버스트(rush orders)** 이고, 미국 소형주 L2는
     시장분절로 신뢰도가 낮다(docs/02 §4.2). 호가 1레벨은 스프레드/잔량비 용도로만 유지.
2. **`/trades`는 최대 50건 → 테이프는 표본이다.**
   - 체결 크기 분포·버스트 지표는 **표본 통계**로 해석하고, 그렇게 문서화한다.
   - 수집 시 구간 누락 여부를 판정할 수 있도록 `trades_snap` 적재 시 응답의 최고/최저 ts를 로그에 남긴다.
     (직전 폴링의 최신 ts보다 이번 응답의 최소 ts가 크면 = 그 사이 체결을 놓친 것)
3. **소형주 `/prices.timestamp`는 체결이 없으면 null 또는 과거 시각 고정** (실측: SNTI/CRKN null,
   BTAI 74분 고정, 같은 배치의 AAPL은 실시간).
   - **Tier 1 스윕의 활동 신호 정의 변경(승인)**: `lastPrice 변화` → **`timestamp 전진 OR lastPrice 변화`**.
   - 파생 상태값 3종을 detector가 사용한다:
     `no_print`(timestamp is null), `staleness_s`(now - timestamp), `first_print`(null→값 전이).
     **`first_print`와 `staleness_s` 급감은 그 자체로 "휴면 종목의 활동 개시" 신호다** — 조용하던
     동전주가 깨어나는 순간을 잡는 것이 이 전략의 핵심이므로 Tier 1 승격 트리거 1순위로 둔다.
   - `Price.ts_ms`가 None인 것은 정상이며 오류가 아니다. 파싱에서 예외를 던지지 말 것.
4. **1분봉 과거 보관 = 최소 1024일** (실측). Phase 1 계획 변경은 `docs/03` §6 참조.
5. **비용 가정 확정**: `/commissions` 단위는 시장별로 다르다 — KR `0.00015`=비율(0.015%),
   US `0.1`=**퍼센트(0.1%)**. US 0.1%는 2026년 상시 적용 확정(0.25% 인상 계획 철회).
   `q5_expectancy(cost_roundtrip=0.01)` 기본값 1%는 왕복 수수료 0.2% + 환전 스프레드 +
   저유동성 슬리피지를 포함한 **보수적 총비용**으로 유지한다. 수수료만의 값(0.002)과
   혼동하지 말 것 — 리포트에 내역을 분해해 표기한다.

### C-6 개정 A3 (2026-07-30, W4 발견 → 코디네이터 결정) — 호가 불균형 표현

W4가 A2 §1(비율형 `[0,1]`)과 `store/writer.py` 구현(부호형 `[-1,1]`)의 불일치를 발견했다.
두 값은 `signed = 2*ratio - 1` 로 상호 변환되어 정보 손실은 없으나, **중립점이 다르다**
(비율형 0.5 = 부호형 0). 저장값에 A2의 "0.5 초과면 매수 우위" 규칙을 그대로 적용하면
부호형에서는 매수 잔량 75%를 기준으로 삼게 되어 **조용히 틀린다.**

**결정: 부호형을 정본으로 하고, 컬럼명이 규약을 스스로 드러내도록 이름을 바꾼다.**

- 컬럼명 `orderbook_snap.imbalance` → **`imbalance_signed`**
- 정의: `(bid_qty_u - ask_qty_u) / (bid_qty_u + ask_qty_u)`, 범위 `[-1, 1]`,
  **중립 = 0**, 양수 = 매수 우위. 잔량이 0이면 NULL.
- 미국 호가는 1레벨뿐이므로 `bid_qty_u == bid1_qu` 이다 (A2 §1). 다레벨이 제공되는 시장으로
  확장될 경우 전 레벨 합산을 유지한다.
- 비율형이 필요한 소비자는 `ratio = (signed + 1) / 2` 로 변환한다.

**부호형을 택한 이유**: 중립점이 0이라 z-score·부호 기반 규칙·스코어 가중에 그대로 쓸 수 있고,
0.5를 빼는 것을 잊는 종류의 실수가 원천 차단된다. 구현이 이미 부호형이라 변경 비용도 작다.

**적용 시점**: 수집이 시작되기 전이라 **저장된 데이터가 없다.** 마이그레이션 없이
`schema.sql`을 직접 수정한다(스키마 버전 유지). 데이터가 쌓인 뒤였다면 이 선택은 불가능했다 —
규약 불일치는 발견 즉시 처리해야 하는 이유다.

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

## C-2 개정 (2026-08-03) — 랭킹 `tradingAmount` 는 KRW 표기 (예외)

**실측 확정(W1, docs/06 §13, 라이브 0콜)**: `RankingRow.amount_u` 는 **마이크로달러가 아니라
마이크로원(KRW)** 이다. 근거: `amount_u/1e6 ÷ (수량 × 달러가)` 비율 중앙값 **1,441** 이 같은
세션 실측 환율 1438.56 과 0.65% 이내로 일치하고, **날짜에 따라 이동한다**(07-31 1445.4 →
08-03 1432.8) — 고정 상수 가설이 배제된다. 코디네이터 독립 재현 완료.

- **C-2 의 '금액은 마이크로달러' 및 'KRW 는 등장하지 않는다' 규정에 대한 명시적 예외**다.
  이것은 토스 API 사양이며 우리 규약의 위반이 아니라 예외로 기록한다.
- **달러 금액이 필요하면 `vol_qu × last_u` 를 직접 계산하라.** 환율을 저장하지 않으므로
  이미 쌓인 `amount_u` 를 달러로 사후 복원할 수 없다.
- **`amount_u` 와 `vol_qu` 는 둘 다 누적이 아니다** — `duration=realtime` 은 롤링 윈도우
  집계이고 창 길이는 미확정. 연속 차분의 **28.2% 가 음수**다. **차분을 '구간 거래대금/
  거래량' 으로 쓰는 것을 금지한다.**
- 같은 스냅샷 내 **종목 간 상대 비교(순위·비율)는 유효**하다 — 환율이 공통이므로.
- 필드명 개정(`amount_u` → `amount_krw_u` 등)은 C-3 필드라 W1/W2/W4 동시 개정이 필요하다.
  **지금은 개정하지 않고** docstring·회귀 테스트로 의미를 고정한다(변경 비용 > 현재 위험).
- 환율 스냅샷 저장은 **하지 않는다** — 달러 금액이 필요하면 위 직접 계산으로 충분하다.

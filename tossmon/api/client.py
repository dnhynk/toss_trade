"""TossClient — GET-only 하드 차단, 배치 청킹, 재시도, 모델 정규화 (계약 C-4·C-5).

모든 요청은 단일 _request() 관문을 지나며 endpoints.ALLOWLIST 를 검사한다.
관문을 우회한 요청도 전송 레벨(GuardedTransport)에서 한 번 더 차단된다 — 이중 차단.

정규화 규약 (계약이 명시하지 않아 W1 이 확정, docs/06_live_facts.md §정규화 참조):
- 캔들: API 는 최신→과거 순으로 주지만 `CandlePage.candles` 는 **과거→최신(ts_ms 오름차순)** 으로 뒤집는다.
- 체결: `Trade` 리스트도 **ts_ms 오름차순**.
- 호가: `bids` 는 가격 내림차순, `asks` 는 가격 오름차순 — 양쪽 모두 **index 0 == 최우선호가**.
- 400/404 응답은 재시도 대상이 아니므로 `SchemaMismatch(detail="http-400 code=...")` 로 올린다
  (계약 C-5 표에 해당 칸이 없어 "재시도 금지 + caller 가 로그·스킵" 정책이 같은 SchemaMismatch 에 매핑).
- 마이크로달러보다 미세한 가격(동전주의 소수 8자리)은 거부하지 않고 반올림한다 (계약 C-2 개정 A4).
  반올림 건수는 `counters["precision_rounded"]` 에 누적되어 조용히 넘어가지 않는다.

429 진단 (2026-08-04, docs/06 §9-3):
`last_headers` 는 "마지막 응답" 이라 429 뒤에 성공 응답이 하나만 지나가도 덮어써진다.
429 는 여기서 1회 재시도되므로 그 재시도가 성공하면 기록은 **항상 200 쪽**이 된다 —
운영 로그에 `HTTP-429-DETAIL ... status=200` 이 남은 것이 그래서였다.
그래서 429 는 받은 **그 자리에서** `last_429` / `recent_429s` / `RateLimited.evidence` 에
찍고, 이후 어떤 응답도 그것을 덮어쓰지 못한다. 429 를 진단할 때 `last_*` 를 보지 말 것.
"""
from __future__ import annotations

import asyncio
import random
import time
from collections import OrderedDict, deque
from contextlib import contextmanager
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Mapping, Sequence

import httpx

from . import models
from .endpoints import canonical_path, check_allowed, group_of
from .errors import (
    AuthExpired,
    Forbidden,
    ForbiddenEndpoint,
    RateLimited,
    SchemaMismatch,
    TransientHTTP,
)
from .limiter import GroupRateLimiter
from .models import (
    Candle,
    CandlePage,
    Orderbook,
    OrderbookLevel,
    Price,
    RankingPage,
    RankingRow,
    SessionWindow,
    StockMeta,
    Trade,
    UsMarketDay,
    dec_to_u,
    iso_to_ms,
    ms_to_iso,
)
from .tokens import TokenManager

BATCH_MAX = 200         # /prices · /stocks 배치 상한
CANDLE_MAX = 200         # /candles 호출당 최대 봉 수
TRADES_MAX = 50          # /trades 호출당 최대 건수
RANKING_MAX = 100        # /rankings 상위 100

TRANSIENT_BACKOFF_S = (0.5, 1.0, 2.0)
MAX_TRANSIENT_RETRIES = 3

# 429 기록 보관 개수. 한 번의 사고에서 여러 발이 나면 첫 발이 가장 중요한데 하나만 들고 있으면
# 그 첫 발이 밀려난다.
RECENT_429_KEEP = 8

# `date` 헤더 기준 초별 요청 수를 몇 초치나 들고 있을지. 429 원인 판별(우리 초과인가 아닌가)에만
# 쓰므로 몇 초면 충분하다.
SEC_COUNT_KEEP = 16

# 429 진단에 남기는 응답 헤더. 시크릿이 실릴 수 있는 헤더는 애초에 목록에 없다
# (응답 헤더이지만 set-cookie 류를 통째로 로그에 흘리지 않기 위해 allowlist 로 간다).
DIAG_HEADER_PREFIXES = ("x-ratelimit", "ratelimit", "retry-after", "x-request-id", "date")

# Retry-After 가 없을 때의 대기값. 서버 창이 **벽시계 초에 정렬된 고정 1초**라
# (docs/06 §9-4) 1.0초를 기다리면 어느 시점에서 재든 반드시 다음 창으로 넘어간다.
DEFAULT_RETRY_AFTER_S = 1.0
# Retry-After 가 HTTP-date 로 올 때의 상한. 스펙상 가능하지만 실측 미관측이라,
# 시계 어긋남으로 터무니없는 값이 나와도 수집이 멈추지 않도록 자른다.
MAX_RETRY_AFTER_S = 60.0

_CAL_KEYS = (("previous", "previousBusinessDay"), ("today", "today"), ("next", "nextBusinessDay"))
_SESSIONS = (("day", "dayMarket"), ("pre", "preMarket"),
             ("regular", "regularMarket"), ("after", "afterMarket"))


class GuardedTransport(httpx.AsyncHTTPTransport):
    """전송 레벨 2차 차단. _request 관문을 우회한 요청은 여기서 ForbiddenEndpoint."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        check_allowed(request.method, request.url.path)
        return await super().handle_async_request(request)


class TossClient:
    def __init__(self, base_url: str, tokens: TokenManager, limiter: GroupRateLimiter,
                 timeout_s: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.tokens = tokens
        self.limiter = limiter
        self.timeout_s = timeout_s
        # AUTH 그룹 rate limit 을 토큰 발급 경로에도 적용한다 (감사 A-4).
        # TokenManager 시그니처를 바꾸지 않고도 기존 호출자가 자동으로 이득을 보도록 주입한다.
        if getattr(tokens, "limiter", None) is None:
            try:
                tokens.limiter = limiter
            except AttributeError:
                pass
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout_s,
            transport=GuardedTransport(),
        )
        # 관측용 카운터 (BudgetGuard·리포트에서 읽는다).
        # precision_rounded: 계약 A4 의 초과 정밀도 반올림 건수 (조용한 반올림 방지).
        self.counters: dict[str, int] = {
            "requests": 0, "retries": 0, "http_429": 0, "http_5xx": 0, "auth_refresh": 0,
            "precision_rounded": 0,
            # 429 인데 Retry-After 가 없던 건수. 실측상 **오는 경우와 안 오는 경우가 둘 다 있다**
            # (docs/06 §9-3) — 0 이 아니면 기본 대기값(1.0s)에 의존하고 있다는 뜻이다.
            "http_429_no_retry_after": 0,
            # 429 를 받았는데 **그 서버 초에 우리가 보낸 요청이 한도 미만**이었던 건수.
            # 0 이 아니면 429 의 원인이 이 클라이언트 밖에 있다 (다른 프로세스 / 계정 전체 한도).
            "http_429_under_own_limit": 0,
        }
        # 진단 전용: 마지막 응답의 상태/헤더. 동시 요청 중에는 어느 요청의 것인지 보장하지 않는다
        # (tools/live_probe.py 처럼 순차 실행하는 경우에만 의미가 있다).
        #
        # ⚠️ 429 진단에 이 둘을 쓰지 말 것. 429 뒤에 성공 응답이 하나만 지나가도 덮어써져
        # `status=200` 인 "429 기록" 이 남는다 (2026-08-04 실제로 그렇게 오독했다).
        # 429 는 아래 `last_429` / `recent_429s` 를 볼 것 — 그쪽은 429 응답 **그 자리에서**
        # 찍히고 이후 응답이 절대 덮어쓰지 않는다.
        self.last_status: int | None = None
        self.last_headers: dict[str, str] = {}

        # 그룹별 송신 수 (누적). **예산 계상의 유일한 귀속 근거**다 (docs/46).
        # `counters["requests"]` 는 전역이라 그 증가분에는 동시에 도는 다른 그룹의 송신이
        # 섞여 있다. 그 델타를 호출한 그룹에 얹던 것이 D1·D2 의 이중 계상이었다.
        # 여기서는 추정이 없다 — `group` 은 `_request` 가 allowlist 로 정한 값이고,
        # 이 카운터는 소켓 직전에 `requests` 와 **같은 자리에서** 오른다.
        self.sent_by_group: dict[str, int] = {}

        # 마지막 429 응답의 완전한 기록(상태·헤더·본문 error code). 200 이 덮어쓰지 않는다.
        self.last_429: dict[str, Any] | None = None
        self.recent_429s: deque[dict[str, Any]] = deque(maxlen=RECENT_429_KEEP)
        # (group, 서버 date 초) → 우리가 그 초에 보낸 요청 수. 429 가 우리 탓인지 판별용.
        self._sec_counts: OrderedDict[tuple[str, str], int] = OrderedDict()

    # ---- 단일 전송 관문 --------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        """유일한 전송 관문. allowlist 검사 → limiter.acquire → 재시도 정책(C-5)."""
        canon = check_allowed(method, path)       # 위반이면 ForbiddenEndpoint (잡지 말 것)
        group = group_of(canon)
        auth_retried = rl_retried = 0
        transient_retried = 0

        while True:
            await self.limiter.acquire(group)
            used: list[str] = []
            try:
                return await self._send(method, path, group, used, **kwargs)
            except AuthExpired:
                if auth_retried >= 1:
                    raise
                auth_retried += 1
                self.counters["auth_refresh"] += 1
                self.counters["retries"] += 1
                # 우리가 **실제로 쓴** 토큰만 무효화한다 (감사 A-2). 그 사이 다른 루프가
                # 새 토큰을 발급했다면 그것은 유효하므로 건드리면 안 된다.
                await self.tokens.invalidate(used[0] if used else None)
            except RateLimited as exc:
                self.counters["http_429"] += 1
                self.limiter.on_429(group, exc.retry_after_s)
                if rl_retried >= 1:
                    raise
                rl_retried += 1
                self.counters["retries"] += 1
                await asyncio.sleep(exc.retry_after_s)
            except TransientHTTP:
                self.counters["http_5xx"] += 1
                if transient_retried >= MAX_TRANSIENT_RETRIES:
                    raise
                delay = TRANSIENT_BACKOFF_S[min(transient_retried, len(TRANSIENT_BACKOFF_S) - 1)]
                transient_retried += 1
                self.counters["retries"] += 1
                await asyncio.sleep(delay + random.uniform(0.0, delay / 2))

    async def _send(self, method: str, path: str, group: str,
                    used: list[str] | None = None, **kwargs) -> dict:
        token = await self.tokens.get()
        if used is not None:
            used.append(token)      # CAS 무효화용 — 이 요청이 실제로 쓴 토큰 (감사 A-2)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        headers.update(kwargs.pop("headers", None) or {})
        self.counters["requests"] += 1
        self.sent_by_group[group] = self.sent_by_group.get(group, 0) + 1
        try:
            resp = await self._http.request(method, path, headers=headers, **kwargs)
        except ForbiddenEndpoint:
            raise
        except httpx.TimeoutException as exc:
            raise TransientHTTP(0, f"timeout: {type(exc).__name__}") from exc
        except httpx.HTTPError as exc:
            raise TransientHTTP(0, f"transport error: {type(exc).__name__}") from exc

        own_in_second = self._count_in_server_second(group, resp.headers)
        self.limiter.update_from_headers(group, resp.headers,
                                         status=resp.status_code)
        self.last_status = resp.status_code
        self.last_headers = dict(resp.headers)
        if resp.status_code == 429:
            # **429 응답 그 자리에서** 기록한다. 뒤따르는 200 이 덮어쓸 수 없는 자리에.
            self._record_429(method, path, group, resp, own_in_second)
        return _classify(resp, self.last_429 if resp.status_code == 429 else None)

    # ---- 429 진단 --------------------------------------------------------

    def _count_in_server_second(self, group: str, headers: Mapping[str, str]) -> int:
        """이 응답이 속한 **서버 초**에 우리가 보낸 요청 수 (이 요청 포함).

        서버 창이 벽시계 1초 고정이므로(docs/06 §9-4), `date` 헤더의 초가 곧 창 이름이다.
        이 수가 그룹 한도보다 작은데도 429 가 났다면 원인은 이 클라이언트 밖에 있다 —
        같은 자격증명을 쓰는 다른 프로세스이거나, 그룹별이 아닌 다른 한도다.
        (경계에서 렌더된 응답은 `date` 와 카운터가 한 초 어긋날 수 있으므로 ±1 의 오차가 있다.)
        """
        date = headers.get("date") or headers.get("Date")
        if not date:
            return 0
        key = (group, str(date))
        self._sec_counts[key] = self._sec_counts.get(key, 0) + 1
        self._sec_counts.move_to_end(key)
        while len(self._sec_counts) > SEC_COUNT_KEEP:
            self._sec_counts.popitem(last=False)
        return self._sec_counts[key]

    def _record_429(self, method: str, path: str, group: str,
                    resp: httpx.Response, own_in_second: int) -> None:
        headers = {k: v for k, v in resp.headers.items()
                   if k.lower().startswith(DIAG_HEADER_PREFIXES)}
        raw_retry = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
        if raw_retry is None:
            self.counters["http_429_no_retry_after"] += 1
        limit_hdr = _int_or_none(resp.headers.get("x-ratelimit-limit"))
        under_own = bool(limit_hdr and own_in_second and own_in_second < limit_hdr)
        if under_own:
            self.counters["http_429_under_own_limit"] += 1
        rec: dict[str, Any] = {
            "at_ms": int(time.time() * 1000),
            "method": method.upper(),
            "path": canonical_path(path),
            "group": group,
            "status": resp.status_code,
            "headers": headers,
            "retry_after_present": raw_retry is not None,
            "retry_after_s": _retry_after(resp.headers),
            "error_code": _err_code(resp),
            "own_requests_in_that_server_second": own_in_second,
            "limit_header": limit_hdr,
            # True 면 "우리가 그 초에 한도만큼 쏘지 않았는데 429" — 원인이 밖에 있다는 신호.
            "under_own_limit": under_own,
        }
        self.last_429 = rec
        self.recent_429s.append(rec)

    @contextmanager
    def _track_rounding(self):
        """이 블록에서 일어난 초과 정밀도 반올림을 client 카운터에 귀속시킨다 (계약 A4).

        `dec_to_u` 는 모듈 함수라 client 를 모른다. 정규화 구간을 감싸 모듈 카운터의
        증분만 가져온다. 단일 asyncio 루프에서 정규화는 동기 구간이라 교차 오염이 없다.
        """
        before = models.precision_rounded_total()
        try:
            yield
        finally:
            self.counters["precision_rounded"] += (
                models.precision_rounded_total() - before)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- 시세 -----------------------------------------------------------

    async def get_prices(self, symbols: Sequence[str]) -> list[Price]:
        """자동 200개 청킹."""
        out: list[Price] = []
        for chunk in _chunks(symbols, BATCH_MAX):
            body = await self._request("GET", "/api/v1/prices",
                                       params={"symbols": ",".join(chunk)})
            with self._track_rounding():
                for item in _as_list(body, "prices"):
                    out.append(Price(
                        symbol=_req_str(item, "symbol", "prices"),
                        ts_ms=_opt_ms(item.get("timestamp")),
                        last_u=_req_u(item, "lastPrice", "prices"),
                    ))
        return out

    async def get_candles(self, symbol: str, interval: Literal["1m", "1d"],
                          count: int = 200, before_ms: int | None = None,
                          adjusted: bool = True) -> CandlePage:
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "count": min(int(count), CANDLE_MAX),
            "adjusted": "true" if adjusted else "false",
        }
        if before_ms is not None:
            params["before"] = ms_to_iso(before_ms)
        body = await self._request("GET", "/api/v1/candles", params=params)
        result = _as_obj(body, "candles")
        raw = result.get("candles")
        if not isinstance(raw, list):
            raise SchemaMismatch("candles: result.candles is not a list")
        with self._track_rounding():
            candles = [
                Candle(
                    symbol=symbol,
                    ts_ms=_req_ms(item, "timestamp", "candles"),
                    open_u=_req_u(item, "openPrice", "candles"),
                    high_u=_req_u(item, "highPrice", "candles"),
                    low_u=_req_u(item, "lowPrice", "candles"),
                    close_u=_req_u(item, "closePrice", "candles"),
                    vol_qu=_req_u(item, "volume", "candles"),
                )
                for item in raw
            ]
        candles.sort(key=lambda c: c.ts_ms)   # 과거 → 최신
        return CandlePage(candles=candles, next_before_ms=_opt_ms(result.get("nextBefore")))

    async def get_trades(self, symbol: str, count: int = 50) -> list[Trade]:
        body = await self._request("GET", "/api/v1/trades",
                                   params={"symbol": symbol,
                                           "count": min(int(count), TRADES_MAX)})
        with self._track_rounding():
            trades = [
                Trade(
                    symbol=symbol,
                    ts_ms=_req_ms(item, "timestamp", "trades"),
                    price_u=_req_u(item, "price", "trades"),
                    qty_u=_req_u(item, "volume", "trades"),
                )
                for item in _as_list(body, "trades")
            ]
        trades.sort(key=lambda t: t.ts_ms)
        return trades

    async def get_orderbook(self, symbol: str) -> Orderbook:
        body = await self._request("GET", "/api/v1/orderbook", params={"symbol": symbol})
        result = _as_obj(body, "orderbook")
        with self._track_rounding():
            bids = _levels(result.get("bids"), "orderbook.bids")
            asks = _levels(result.get("asks"), "orderbook.asks")
        bids.sort(key=lambda lv: lv.price_u, reverse=True)   # index 0 = 최우선 매수
        asks.sort(key=lambda lv: lv.price_u)                 # index 0 = 최우선 매도
        return Orderbook(symbol=symbol, ts_ms=_opt_ms(result.get("timestamp")),
                         bids=bids, asks=asks)

    # ---- 랭킹·종목 ------------------------------------------------------

    async def get_rankings(self, ranking_type: str, duration: str = "realtime",
                           market: str = "US", count: int = 100,
                           exclude_caution: bool = False) -> RankingPage:
        body = await self._request("GET", "/api/v1/rankings", params={
            "type": ranking_type,
            "marketCountry": market,
            "duration": duration,
            "count": min(int(count), RANKING_MAX),
            "excludeInvestmentCaution": "true" if exclude_caution else "false",
        })
        result = _as_obj(body, "rankings")
        raw = result.get("rankings")
        if not isinstance(raw, list):
            raise SchemaMismatch("rankings: result.rankings is not a list")
        rows: list[RankingRow] = []
        with self._track_rounding():
            for item in raw:
                price = item.get("price")
                if not isinstance(price, dict):
                    raise SchemaMismatch("rankings: row.price missing")
                change = price.get("changeRate")
                rows.append(RankingRow(
                    rank=_req_int(item, "rank", "rankings"),
                    symbol=_req_str(item, "symbol", "rankings"),
                    last_u=_req_u(price, "lastPrice", "rankings.price"),
                    base_u=_req_u(price, "basePrice", "rankings.price"),
                    change_rate=None if change is None else float(Decimal(str(change))),
                    vol_qu=_req_u(item, "tradingVolume", "rankings"),
                    amount_u=_req_u(item, "tradingAmount", "rankings"),
                ))
        return RankingPage(
            ranking_type=ranking_type,
            duration=duration,
            ranked_at_ms=_opt_ms(result.get("rankedAt")),
            rows=rows,
        )

    async def get_stocks(self, symbols: Sequence[str]) -> list[StockMeta]:
        """자동 200개 청킹."""
        out: list[StockMeta] = []
        for chunk in _chunks(symbols, BATCH_MAX):
            body = await self._request("GET", "/api/v1/stocks",
                                       params={"symbols": ",".join(chunk)})
            with self._track_rounding():
                for item in _as_list(body, "stocks"):
                    list_date = item.get("listDate")
                    out.append(StockMeta(
                        symbol=_req_str(item, "symbol", "stocks"),
                        name=_req_str(item, "name", "stocks"),
                        market=_req_str(item, "market", "stocks"),
                        security_type=_req_str(item, "securityType", "stocks"),
                        is_common=bool(item.get("isCommonShare", False)),
                        status=_req_str(item, "status", "stocks"),
                        list_date=None if list_date is None else str(list_date),
                        shares_outstanding_qu=_req_u(item, "sharesOutstanding", "stocks"),
                    ))
        return out

    # ---- 시장 정보 ------------------------------------------------------

    async def get_us_calendar(self, date: str | None = None) -> dict[str, UsMarketDay]:
        """keys: "previous" | "today" | "next"."""
        params = {"date": date} if date else None
        body = await self._request("GET", "/api/v1/market-calendar/US", params=params)
        result = _as_obj(body, "market-calendar/US")
        out: dict[str, UsMarketDay] = {}
        for out_key, api_key in _CAL_KEYS:
            node = result.get(api_key)
            if not isinstance(node, dict):
                raise SchemaMismatch(f"market-calendar/US: missing {api_key}")
            out[out_key] = UsMarketDay(
                date=_req_str(node, "date", "market-calendar/US"),
                **{name: _window(node.get(api_name)) for name, api_name in _SESSIONS},
            )
        return out

    async def get_exchange_rate(self) -> Decimal:
        """KRW per USD (참고용 표시 환율)."""
        body = await self._request("GET", "/api/v1/exchange-rate",
                                   params={"baseCurrency": "USD", "quoteCurrency": "KRW"})
        result = _as_obj(body, "exchange-rate")
        rate = result.get("rate")
        if rate is None:
            raise SchemaMismatch("exchange-rate: missing rate")
        try:
            return Decimal(str(rate))
        except ArithmeticError as exc:
            raise SchemaMismatch(f"exchange-rate: bad rate {rate!r}") from exc


# ---- 응답 분류 ----------------------------------------------------------


def _classify(resp: httpx.Response, evidence: dict | None = None) -> dict:
    """HTTP 응답 → envelope dict 또는 계약 C-5 예외."""
    status = resp.status_code
    if status == 429:
        raise RateLimited(_retry_after(resp.headers), "rate limited", evidence=evidence)
    if status == 401:
        raise AuthExpired(f"401 {_err_code(resp)}")
    if status == 403:
        raise Forbidden(f"403 {_err_code(resp)}")
    if status >= 500:
        raise TransientHTTP(status, f"{status} {_err_code(resp)}")
    if status >= 400:
        # 400/404/409/415/422: 재시도해도 같은 결과 → caller 가 로그+스킵 (계약 C-5 SchemaMismatch 정책)
        raise SchemaMismatch(f"http-{status} {_err_code(resp)}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise SchemaMismatch(f"non-json body ({resp.headers.get('content-type')})") from exc
    if not isinstance(body, dict):
        raise SchemaMismatch(f"envelope is {type(body).__name__}, expected object")
    return body


def _retry_after(headers: Mapping[str, str]) -> float:
    """Retry-After → 초. 실측은 정수 초 문자열이고, **없는 경우도 있다** (docs/06 §9-3).

    없으면 1.0초를 쓴다. 서버 창이 벽시계 초에 정렬된 고정 1초라 1.0초를 기다리면
    어느 위상에서 재든 반드시 다음 창으로 넘어간다 (docs/06 §9-4).
    HTTP-date 형식은 스펙상 가능하지만 미관측이다 — 들어오면 해석하되 상한을 건다.
    """
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return DEFAULT_RETRY_AFTER_S
    text = str(raw).strip()
    try:
        return min(max(float(text), 0.0), MAX_RETRY_AFTER_S)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER_S
    if when is None:
        return DEFAULT_RETRY_AFTER_S
    delta = when.timestamp() - time.time()
    return min(max(delta, 0.0), MAX_RETRY_AFTER_S)


def _int_or_none(raw: Any) -> int | None:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _err_code(resp: httpx.Response) -> str:
    """에러 envelope 에서 code/message 만 뽑는다 (시크릿 미포함 필드)."""
    try:
        err = resp.json().get("error")
    except (ValueError, AttributeError):
        return "<unparseable>"
    if isinstance(err, dict):
        return f"code={err.get('code')} message={err.get('message')}"
    if isinstance(err, str):
        return f"error={err}"
    return "<no error field>"


# ---- 파싱 헬퍼 ----------------------------------------------------------


def _chunks(symbols: Sequence[str], size: int) -> list[list[str]]:
    items = [str(s) for s in symbols if str(s).strip()]
    return [items[i:i + size] for i in range(0, len(items), size)]


def _result(body: dict, where: str) -> Any:
    if "result" not in body:
        raise SchemaMismatch(f"{where}: envelope has no 'result'")
    return body["result"]


def _as_list(body: dict, where: str) -> list[dict]:
    result = _result(body, where)
    if not isinstance(result, list):
        raise SchemaMismatch(f"{where}: result is {type(result).__name__}, expected list")
    for item in result:
        if not isinstance(item, dict):
            raise SchemaMismatch(f"{where}: list item is {type(item).__name__}")
    return result


def _as_obj(body: dict, where: str) -> dict:
    result = _result(body, where)
    if not isinstance(result, dict):
        raise SchemaMismatch(f"{where}: result is {type(result).__name__}, expected object")
    return result


def _req_str(item: Mapping[str, Any], key: str, where: str) -> str:
    val = item.get(key)
    if not isinstance(val, str) or not val:
        raise SchemaMismatch(f"{where}: missing/blank field {key!r}")
    return val


def _req_int(item: Mapping[str, Any], key: str, where: str) -> int:
    val = item.get(key)
    if isinstance(val, bool) or not isinstance(val, int):
        raise SchemaMismatch(f"{where}: field {key!r} is not an int ({val!r})")
    return val


def _req_u(item: Mapping[str, Any], key: str, where: str) -> int:
    if key not in item or item[key] is None:
        raise SchemaMismatch(f"{where}: missing field {key!r}")
    try:
        return dec_to_u(item[key])
    except SchemaMismatch as exc:
        raise SchemaMismatch(f"{where}.{key}: {exc.detail}") from exc


def _req_ms(item: Mapping[str, Any], key: str, where: str) -> int:
    if key not in item or item[key] is None:
        raise SchemaMismatch(f"{where}: missing field {key!r}")
    try:
        return iso_to_ms(item[key])
    except SchemaMismatch as exc:
        raise SchemaMismatch(f"{where}.{key}: {exc.detail}") from exc


def _opt_ms(raw: Any) -> int | None:
    return None if raw is None else iso_to_ms(raw)


def _levels(raw: Any, where: str) -> list[OrderbookLevel]:
    if not isinstance(raw, list):
        raise SchemaMismatch(f"{where}: not a list")
    out: list[OrderbookLevel] = []
    for item in raw:
        if not isinstance(item, dict):
            raise SchemaMismatch(f"{where}: level is {type(item).__name__}")
        out.append(OrderbookLevel(price_u=_req_u(item, "price", where),
                                  qty_u=_req_u(item, "volume", where)))
    return out


def _window(raw: Any) -> SessionWindow | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SchemaMismatch(f"session window is {type(raw).__name__}")
    return SessionWindow(start_ms=_req_ms(raw, "startTime", "session"),
                         end_ms=_req_ms(raw, "endTime", "session"))


__all__ = ["TossClient", "canonical_path"]

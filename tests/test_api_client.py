"""TossClient 테스트 — 배치 청킹, 페이지네이션, 재시도 정책(C-5), 스키마 드리프트, 정규화."""
from __future__ import annotations

import json
import time
from decimal import Decimal

import httpx
import pytest

from tests.test_api_support import LIMITS, MockServer, client, make_client, mock_server  # noqa: F401
from tossmon.api.client import BATCH_MAX, TossClient
from tossmon.api.errors import (
    AuthExpired,
    Forbidden,
    RateLimited,
    SchemaMismatch,
    TransientHTTP,
)
from tossmon.api.limiter import GroupRateLimiter
from tossmon.api.models import iso_to_ms
from tossmon.api.tokens import TokenManager
from tossmon.store.writer import Store


# ---- 배치 청킹 경계 (199 / 200 / 201) -----------------------------------


@pytest.mark.parametrize("n,expected_calls", [(1, 1), (199, 1), (200, 1), (201, 2), (400, 2), (401, 3)])
async def test_get_prices_chunking_boundaries(client, n, expected_calls):  # noqa: F811
    symbols = [f"S{i:04d}" for i in range(n)]
    before = client.counters["requests"]
    prices = await client.get_prices(symbols)
    assert client.counters["requests"] - before == expected_calls
    assert len(prices) == n, "청킹 과정에서 종목이 유실됐다"
    assert [p.symbol for p in prices] == symbols, "청크 순서가 보존되지 않았다"


@pytest.mark.parametrize("n,expected_calls", [(199, 1), (200, 1), (201, 2)])
async def test_get_stocks_chunking_boundaries(client, n, expected_calls):  # noqa: F811
    symbols = [f"T{i:04d}" for i in range(n)]
    before = client.counters["requests"]
    metas = await client.get_stocks(symbols)
    assert client.counters["requests"] - before == expected_calls
    assert len(metas) == n


async def test_chunk_never_exceeds_batch_max(mock_server, tmp_path):  # noqa: F811
    """실제로 전송된 symbols 파라미터가 200개를 넘지 않아야 한다 (서버 400 유발 방지)."""
    seen: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(len(request.url.params["symbols"].split(",")))
        return httpx.Response(200, json={"result": []})

    c = make_client(mock_server.url, tmp_path)
    c._http = httpx.AsyncClient(base_url=mock_server.url,
                                transport=httpx.MockTransport(handler))
    try:
        await c.get_prices([f"S{i}" for i in range(1001)])
    finally:
        await c.aclose()
    assert seen == [BATCH_MAX] * 5 + [1]
    assert max(seen) <= BATCH_MAX


async def test_empty_and_blank_symbols_make_no_request(client):  # noqa: F811
    before = client.counters["requests"]
    assert await client.get_prices([]) == []
    assert await client.get_prices(["", "  "]) == []
    assert client.counters["requests"] == before


# ---- 페이지네이션 -------------------------------------------------------


def _bar(ts: str, close: str = "10.00", vol: str = "100") -> dict:
    return {"timestamp": ts, "openPrice": "10.00", "highPrice": "10.50",
            "lowPrice": "9.50", "closePrice": close, "volume": vol, "currency": "USD"}


async def test_candle_pagination_sends_before_and_parses_next(mock_server, tmp_path):  # noqa: F811
    """`before` 는 UTC ISO 로 나가고, nextBefore 는 next_before_ms 로 파싱된다."""
    requests: list[dict] = []
    page1 = {"result": {"candles": [_bar("2026-07-30T18:16:00.000+09:00"),
                                    _bar("2026-07-30T18:15:00.000+09:00")],
                        "nextBefore": "2026-07-30T18:15:00.000+09:00"}}
    page2 = {"result": {"candles": [_bar("2026-07-30T18:15:00.000+09:00"),
                                    _bar("2026-07-30T18:14:00.000+09:00")],
                        "nextBefore": None}}

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(dict(request.url.params))
        return httpx.Response(200, json=page2 if "before" in request.url.params else page1)

    c = make_client(mock_server.url, tmp_path)
    c._http = httpx.AsyncClient(base_url=mock_server.url,
                                transport=httpx.MockTransport(handler))
    try:
        first = await c.get_candles("AAPL", "1m", count=200)
        assert first.next_before_ms == iso_to_ms("2026-07-30T18:15:00.000+09:00")
        assert "before" not in requests[0]
        assert requests[0]["interval"] == "1m"
        assert requests[0]["count"] == "200"

        second = await c.get_candles("AAPL", "1m", count=200,
                                     before_ms=first.next_before_ms)
        assert second.next_before_ms is None, "마지막 페이지는 next_before_ms=None"
    finally:
        await c.aclose()

    sent = requests[1]["before"]
    assert sent.endswith("+00:00"), f"before 가 UTC 오프셋이 아니다: {sent}"
    assert iso_to_ms(sent) == first.next_before_ms, "before 직렬화가 시각을 바꿨다"

    # 실측 사실: before 는 inclusive → 경계 봉이 중복된다 (docs/06 §2-1)
    overlap = {c_.ts_ms for c_ in first.candles} & {c_.ts_ms for c_ in second.candles}
    assert overlap, "이 픽스처는 inclusive 경계 중복을 재현해야 한다"


async def test_candles_are_returned_oldest_first(client):  # noqa: F811
    page = await client.get_candles("SNTI", "1m", count=200)
    ts = [c.ts_ms for c in page.candles]
    assert ts == sorted(ts), "캔들이 오름차순이 아니다 (docs/06 §12 정규화 규약)"
    assert len(ts) == len(set(ts)), "중복 봉이 있다"


async def test_count_is_clamped_to_api_max(mock_server, tmp_path):  # noqa: F811
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["count"])
        if request.url.path.endswith("/candles"):
            return httpx.Response(200, json={"result": {"candles": [], "nextBefore": None}})
        return httpx.Response(200, json={"result": []})

    c = make_client(mock_server.url, tmp_path)
    c._http = httpx.AsyncClient(base_url=mock_server.url,
                                transport=httpx.MockTransport(handler))
    try:
        await c.get_candles("AAPL", "1m", count=5000)
        await c.get_trades("AAPL", count=5000)
    finally:
        await c.aclose()
    assert seen[0] == "200", "캔들 count 가 200 으로 제한되지 않았다"
    assert seen[1] == "50", "체결 count 가 50 으로 제한되지 않았다"


# ---- 재시도 정책 (계약 C-5) ---------------------------------------------


async def test_429_retries_once_then_propagates(client):  # noqa: F811
    """RateLimited: limiter 반영 + Retry-After 대기 + 1회 재시도, 재실패 시 전파."""
    before = client.counters["requests"]
    t0 = time.monotonic()
    with pytest.raises(RateLimited) as ei:
        await client._request("GET", "/api/v1/prices", params={"symbols": "AAPL"},
                              headers={"X-Mock-Inject": "429"})
    elapsed = time.monotonic() - t0

    assert ei.value.retry_after_s == 2.0, "Retry-After 파싱 실패"
    assert client.counters["requests"] - before == 2, "재시도 횟수가 1회가 아니다"
    assert elapsed >= 2.0, "Retry-After 만큼 기다리지 않았다"
    assert client.limiter.snapshot("MARKET_DATA")["backoff"] > 1.0, "limiter 에 429 가 반영되지 않았다"


async def test_401_invalidates_token_and_retries_once(client):  # noqa: F811
    before = client.counters["requests"]
    with pytest.raises(AuthExpired):
        await client._request("GET", "/api/v1/prices", params={"symbols": "AAPL"},
                              headers={"X-Mock-Inject": "401"})
    assert client.counters["requests"] - before == 2
    assert client.counters["auth_refresh"] == 1


async def test_5xx_backs_off_three_times(client):  # noqa: F811
    before = client.counters["requests"]
    t0 = time.monotonic()
    with pytest.raises(TransientHTTP) as ei:
        await client._request("GET", "/api/v1/prices", params={"symbols": "AAPL"},
                              headers={"X-Mock-Inject": "500"})
    elapsed = time.monotonic() - t0
    assert ei.value.status == 500
    assert client.counters["requests"] - before == 4, "초기 1회 + 재시도 3회여야 한다"
    assert elapsed >= 3.5, f"0.5+1+2 백오프가 적용되지 않았다 ({elapsed:.1f}s)"


async def test_403_is_fatal_and_not_retried(client):  # noqa: F811
    before = client.counters["requests"]
    with pytest.raises(Forbidden):
        await client._request("GET", "/api/v1/prices", params={"symbols": "AAPL"},
                              headers={"X-Mock-Inject": "403"})
    assert client.counters["requests"] - before == 1, "403 은 재시도하면 안 된다 (치명)"


async def test_400_is_schema_mismatch_and_not_retried(client):  # noqa: F811
    """TOP_GAINERS+realtime 400 — 재시도 금지, caller 가 로그+스킵 (docs/06 §12)."""
    before = client.counters["requests"]
    with pytest.raises(SchemaMismatch) as ei:
        await client.get_rankings("TOP_GAINERS", duration="realtime")
    assert client.counters["requests"] - before == 1
    assert "http-400" in ei.value.detail
    assert "unsupported-ranking-duration" in ei.value.detail


async def test_timeout_becomes_transient_http(mock_server, tmp_path):  # noqa: F811
    c = make_client(mock_server.url, tmp_path, timeout_s=0.05)
    try:
        with pytest.raises(TransientHTTP):
            await c._request("GET", "/api/v1/prices", params={"symbols": "AAPL"},
                             headers={"X-Mock-Inject": "latency"})
    finally:
        await c.aclose()


# ---- 스키마 드리프트 ----------------------------------------------------


async def test_schema_drift_server_mode_breaks_every_endpoint(tmp_path):
    """`--inject schema-drift` 서버 모드: symbol→ticker rename + float 가격 + timestamp 제거.

    드리프트를 만나면 조용히 잘못된 값을 만들지 말고 SchemaMismatch 로 터져야 한다.
    """
    srv = MockServer(inject="schema-drift")
    c = make_client(srv.url, tmp_path)
    try:
        with pytest.raises(SchemaMismatch):
            await c.get_prices(["SNTI"])
        with pytest.raises(SchemaMismatch):
            await c.get_candles("SNTI", "1m", count=10)
        with pytest.raises(SchemaMismatch):
            await c.get_rankings("MARKET_TRADING_AMOUNT", duration="realtime")
        with pytest.raises(SchemaMismatch):
            await c.get_orderbook("SNTI")
    finally:
        await c.aclose()
        srv.close()


@pytest.mark.parametrize("body,where", [
    ({"nope": []}, "envelope has no 'result'"),
    ({"result": "oops"}, "expected list"),
    ({"result": [{"timestamp": None, "lastPrice": "1.0"}]}, "symbol"),
    ({"result": [{"symbol": "A", "timestamp": None}]}, "lastPrice"),
    ({"result": [{"symbol": "A", "timestamp": None, "lastPrice": 1.87}]}, "lastPrice"),
    ({"result": [{"symbol": "A", "timestamp": "2026-01-01T00:00:00", "lastPrice": "1"}]}, "naive"),
])
async def test_missing_or_wrong_typed_fields_raise(mock_server, tmp_path, body, where):  # noqa: F811
    async def handler(request):
        return httpx.Response(200, json=body)

    c = make_client(mock_server.url, tmp_path)
    c._http = httpx.AsyncClient(base_url=mock_server.url,
                                transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(SchemaMismatch) as ei:
            await c.get_prices(["A"])
        assert where in str(ei.value)
    finally:
        await c.aclose()


async def test_non_json_body_raises_schema_mismatch(mock_server, tmp_path):  # noqa: F811
    async def handler(request):
        return httpx.Response(200, text="<html>maintenance</html>",
                              headers={"content-type": "text/html"})

    c = make_client(mock_server.url, tmp_path)
    c._http = httpx.AsyncClient(base_url=mock_server.url,
                                transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(SchemaMismatch, match="non-json"):
            await c.get_prices(["A"])
    finally:
        await c.aclose()


# ---- 정규화 (계약 C-1/C-2/C-3) -----------------------------------------


async def test_prices_normalise_to_micro_dollars(client):  # noqa: F811
    (p,) = await client.get_prices(["SNTI"])
    assert isinstance(p.last_u, int)
    assert p.ts_ms is None or isinstance(p.ts_ms, int)


async def test_null_timestamp_is_preserved_as_none(mock_server, tmp_path):  # noqa: F811
    """실측 사실: 소형주는 프리마켓에 timestamp=null (docs/06 §1-1)."""
    async def handler(request):
        return httpx.Response(200, json={"result": [
            {"symbol": "SNTI", "timestamp": None, "lastPrice": "0.347", "currency": "USD"}]})

    c = make_client(mock_server.url, tmp_path)
    c._http = httpx.AsyncClient(base_url=mock_server.url,
                                transport=httpx.MockTransport(handler))
    try:
        (p,) = await c.get_prices(["SNTI"])
        assert p.ts_ms is None
        assert p.last_u == 347_000
    finally:
        await c.aclose()


async def test_orderbook_best_level_is_index_zero(mock_server, tmp_path):  # noqa: F811
    """API 는 양쪽 다 가격 내림차순으로 준다 — asks 는 뒤집어 index0=최우선이어야 한다."""
    async def handler(request):
        return httpx.Response(200, json={"result": {
            "timestamp": "2026-07-30T18:19:18.000+09:00", "currency": "USD",
            "asks": [{"price": "666.99", "volume": "10"}, {"price": "666.77", "volume": "140"}],
            "bids": [{"price": "666.69", "volume": "140"}, {"price": "666.50", "volume": "20"}]}})

    c = make_client(mock_server.url, tmp_path)
    c._http = httpx.AsyncClient(base_url=mock_server.url,
                                transport=httpx.MockTransport(handler))
    try:
        ob = await c.get_orderbook("QQQ")
        assert ob.asks[0].price_u == 666_770_000, "최우선 매도가 index0 이 아니다"
        assert ob.bids[0].price_u == 666_690_000, "최우선 매수가 index0 이 아니다"
        assert ob.asks[0].price_u > ob.bids[0].price_u
    finally:
        await c.aclose()


async def test_trades_get_symbol_injected_and_sorted(client):  # noqa: F811
    """응답에 symbol 필드가 없다 — 클라이언트가 주입해야 한다 (docs/06 §4)."""
    trades = await client.get_trades("SNTI", count=50)
    assert trades and all(t.symbol == "SNTI" for t in trades)
    assert [t.ts_ms for t in trades] == sorted(t.ts_ms for t in trades)


async def test_rankings_nested_price_block_is_flattened(client):  # noqa: F811
    page = await client.get_rankings("MARKET_TRADING_AMOUNT", duration="realtime")
    assert page.rows and page.rows[0].rank == 1
    row = page.rows[0]
    assert isinstance(row.last_u, int) and isinstance(row.base_u, int)
    assert isinstance(row.amount_u, int) and isinstance(row.vol_qu, int)
    assert row.change_rate is None or isinstance(row.change_rate, float)


@pytest.mark.parametrize("ranked_at", ["2026-08-27T12:34:56+09:00", None])
async def test_ranked_at_round_trips_from_response_to_store(
        mock_server, tmp_path, ranked_at):  # noqa: F811
    """_opt_ms 의 정수와 NULL 경로가 모두 실제 저장까지 이어져야 한다."""
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "result": {
                "rankedAt": ranked_at,
                "rankings": [{
                    "rank": 1,
                    "symbol": "ABCD",
                    "price": {
                        "lastPrice": "1.00",
                        "basePrice": "0.90",
                        "changeRate": "0.10",
                    },
                    "tradingVolume": "3",
                    "tradingAmount": "4",
                }],
            }
        })

    c = make_client(mock_server.url, tmp_path)
    c._http = httpx.AsyncClient(
        base_url=mock_server.url, transport=httpx.MockTransport(handler))
    try:
        page = await c.get_rankings("MARKET_TRADING_VOLUME", duration="realtime")
    finally:
        await c.aclose()

    expected = None if ranked_at is None else iso_to_ms(ranked_at)
    assert page.ranked_at_ms == expected
    with Store(tmp_path / "ranked-at.db") as store:
        assert store.insert_rankings(1_788_000_000_000, page) == 1
        stored = store._conn.execute(
            "SELECT ranked_at_ms FROM rankings_snap"
        ).fetchone()[0]
    assert stored == expected


async def test_calendar_keys_and_session_windows(client):  # noqa: F811
    cal = await client.get_us_calendar()
    assert set(cal) == {"previous", "today", "next"}
    today = cal["today"]
    assert today.regular is not None
    assert today.regular.start_ms < today.regular.end_ms
    assert today.pre.end_ms == today.regular.start_ms, "프리마켓 종료 == 정규장 시작"


async def test_exchange_rate_is_decimal_not_float(client):  # noqa: F811
    rate = await client.get_exchange_rate()
    assert isinstance(rate, Decimal), "계약 C-2: 환율은 float 금지"
    assert rate > 0


async def test_unknown_symbols_silently_vanish_from_batch(mock_server, tmp_path):  # noqa: F811
    """실측 사실: 미존재 심볼은 404 가 아니라 응답에서 빠진다 (docs/06 §10)."""
    async def handler(request):
        return httpx.Response(200, json={"result": [
            {"symbol": "AAPL", "timestamp": None, "lastPrice": "1", "currency": "USD"}]})

    c = make_client(mock_server.url, tmp_path)
    c._http = httpx.AsyncClient(base_url=mock_server.url,
                                transport=httpx.MockTransport(handler))
    try:
        got = await c.get_prices(["AAPL", "ZZZZNOPE"])
        assert [p.symbol for p in got] == ["AAPL"]
    finally:
        await c.aclose()

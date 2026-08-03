"""계약 C-2 개정 A4 — 초과 정밀도는 거부가 아니라 반올림.

배경: 라이브에서 CRKN 2025-03-05T08:00 KST 1분봉이 adjusted=true 일 때
`openPrice = "0.10461614"`(소수 8자리)로 왔다. 리버스 스플릿 보정 곱셈이 임의 정밀도를
만든다. 이전 동작(SchemaMismatch)은 C-5 정책상 caller 가 그 심볼을 스킵하게 만드는데,
스킵되는 것이 하필 이 전략의 표적 종목군(저가 동전주)이라 **표적에서만 조용히 데이터가
비는** 최악의 실패가 된다. 조용한 누락은 에러보다 나쁘다.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from tests.test_api_support import make_client, mock_server  # noqa: F401
from tossmon.api.errors import SchemaMismatch
from tossmon.api.models import (
    dec_to_u,
    precision_rounded_total,
    precision_stats,
    reset_precision_stats,
    u_to_dec,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "live"

# 라이브 실관측값 (W5 리허설, 심볼 정정 후)
CRKN_ADJUSTED = "0.10461614"     # adjusted=true  — 소수 8자리
CRKN_RAW = "1.9889"              # adjusted=false — 소수 4자리


@pytest.fixture(autouse=True)
def _clean_counter():
    reset_precision_stats()
    yield
    reset_precision_stats()


# ---- 핵심 회귀: 실제로 터진 값 -----------------------------------------


def test_the_value_that_broke_production_parses():
    """CRKN openPrice='0.10461614' 가 예외 없이 마이크로 단위로 반올림된다."""
    got = dec_to_u(CRKN_ADJUSTED)
    assert got == 104_616, "0.10461614 → 0.104616 (마이크로 격자 반올림)"
    assert u_to_dec(got) == Decimal("0.104616")


def test_the_value_that_broke_production_is_counted():
    """반올림은 관측 가능해야 한다 — 조용히 넘어가면 안 된다."""
    assert precision_rounded_total() == 0
    dec_to_u(CRKN_ADJUSTED)
    stats = precision_stats()
    assert stats["rounded"] == 1
    assert stats["parsed"] == 1
    assert stats["last_raw"] == CRKN_ADJUSTED
    assert stats["max_digits"] == 8


def test_raw_counterpart_needs_no_rounding():
    """같은 봉의 adjusted=false 값은 4자리라 반올림이 일어나지 않는다 (계약 A5 대조군)."""
    assert dec_to_u(CRKN_RAW) == 1_988_900
    assert precision_rounded_total() == 0


def test_rounding_error_is_negligible_for_a_coin_stock():
    """A4 §3 의 주장 검증: $0.10 종목에서 반올림 상대오차는 0.001% 미만."""
    exact = Decimal(CRKN_ADJUSTED)
    rounded = u_to_dec(dec_to_u(CRKN_ADJUSTED))
    rel_err = abs(rounded - exact) / exact * 100
    assert rel_err < Decimal("0.001"), f"상대오차 {rel_err}% 가 너무 크다"


# ---- 경계값 ------------------------------------------------------------


@pytest.mark.parametrize("raw,expect_u,should_round", [
    ("0.123456", 123_456, False),        # 정확히 6자리 — 반올림 없음
    ("0.000001", 1, False),              # 마이크로 1단위
    ("0.1234564", 123_456, True),        # 7자리 내림
    ("0.1234566", 123_457, True),        # 7자리 올림
    ("0.104616142857", 104_616, True),   # 12자리 (분할비가 나누어떨어지지 않는 경우)
    ("0.0000004", 0, True),              # 마이크로 미만 → 0
    ("338.7488", 338_748_800, False),    # 정상 대형주 가격
])
def test_precision_boundaries(raw, expect_u, should_round):
    assert dec_to_u(raw) == expect_u
    assert (precision_rounded_total() == 1) is should_round


@pytest.mark.parametrize("raw,expect_u", [
    ("0.0000005", 0),        # 정확히 중간 → 짝수(0)로
    ("0.0000015", 2),        # 정확히 중간 → 짝수(2)로
    ("0.0000025", 2),        # 정확히 중간 → 짝수(2)로
    ("0.0000035", 4),        # 정확히 중간 → 짝수(4)로
])
def test_round_half_even_not_half_up(raw, expect_u):
    """ROUND_HALF_EVEN(은행가 반올림) — 계약 A4 가 명시한 모드.

    ROUND_HALF_UP 이면 0.0000005→1, 0.0000015→2, 0.0000025→3 이 된다.
    HALF_EVEN 은 중간값을 짝수로 보내 대량 집계 시 편향이 누적되지 않는다.
    """
    assert dec_to_u(raw) == expect_u


def test_half_even_has_no_systematic_bias():
    """중간값을 많이 모아도 절사/올림 편향이 생기지 않아야 한다."""
    mids = [f"0.000000{d}5" for d in range(0, 10)]
    total_rounded = sum(dec_to_u(m) for m in mids)
    exact = sum(Decimal(m) for m in mids)
    # HALF_UP 이면 항상 위로 쏠려 exact*1e6 보다 확실히 커진다
    assert abs(Decimal(total_rounded) - exact.scaleb(6)) <= Decimal("5")


# ---- 여전히 거부해야 하는 값 -------------------------------------------


@pytest.mark.parametrize("bad", ["", "   ", "abc", "1.2.3", "$1", "NaN", "Infinity"])
def test_unparseable_still_raises(bad):
    with pytest.raises(SchemaMismatch):
        dec_to_u(bad)


@pytest.mark.parametrize("bad", [None, 1.87, [], {}, True])
def test_wrong_types_still_raise(bad):
    """float 은 계약 C-2 위반(정밀도 손실), None/컨테이너는 파싱 불가."""
    with pytest.raises(SchemaMismatch):
        dec_to_u(bad)


@pytest.mark.parametrize("bad", ["-1.5", "-0.0000001", "-1000"])
def test_negative_still_raises(bad):
    """가격·수량·금액에 음수는 유효하지 않다 (계약 A4)."""
    with pytest.raises(SchemaMismatch, match="negative"):
        dec_to_u(bad)


def test_bad_values_do_not_increment_the_counter():
    for bad in ("", "abc", "-1.5"):
        with pytest.raises(SchemaMismatch):
            dec_to_u(bad)
    assert precision_stats()["parsed"] == 0
    assert precision_stats()["rounded"] == 0


# ---- 픽스처 회귀: client 전 메서드가 스킵 없이 동작 ---------------------


def _fixture_body(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["body"]


def _client_on(mock_server, tmp_path, body: dict):  # noqa: F811
    async def handler(request):
        return httpx.Response(200, json=body)

    c = make_client(mock_server.url, tmp_path)
    c._http = httpx.AsyncClient(base_url=mock_server.url,
                                transport=httpx.MockTransport(handler))
    return c


async def test_fixture_candles_adjusted_no_symbol_is_skipped(mock_server, tmp_path):  # noqa: F811
    """A4 이전이라면 이 픽스처에서 SchemaMismatch → 심볼 통째로 스킵됐다."""
    c = _client_on(mock_server, tmp_path,
                   _fixture_body("lowprice_candles_1m_adjusted.json"))
    try:
        page = await c.get_candles("CRKN", "1m", count=200, adjusted=True)
    finally:
        await c.aclose()

    assert len(page.candles) == 3, "봉이 유실됐다 — 스킵이 발생했다"
    oldest = page.candles[0]          # ts_ms 오름차순
    newest = page.candles[-1]
    assert oldest.open_u == 104_616   # "0.104616142857" → 반올림
    assert newest.open_u == 104_616   # "0.10461614"     → 반올림
    assert all(cd.open_u > 0 and cd.close_u > 0 for cd in page.candles)
    assert c.counters["precision_rounded"] > 0, "client 카운터에 반올림이 기록되지 않았다"


async def test_fixture_candles_raw_is_clean(mock_server, tmp_path):  # noqa: F811
    """adjusted=false 원주가는 4자리라 반올림이 0건이어야 한다 (계약 A5 대조군)."""
    c = _client_on(mock_server, tmp_path, _fixture_body("lowprice_candles_1m_raw.json"))
    try:
        page = await c.get_candles("CRKN", "1m", count=200, adjusted=False)
    finally:
        await c.aclose()

    assert len(page.candles) == 2
    assert page.candles[-1].open_u == 1_988_900        # $1.9889 — 동전주가 아니다
    assert c.counters["precision_rounded"] == 0


async def test_fixture_prices_all_symbols_survive(mock_server, tmp_path):  # noqa: F811
    c = _client_on(mock_server, tmp_path, _fixture_body("lowprice_prices_subpenny.json"))
    try:
        prices = await c.get_prices(["CRKN", "SNTI", "AAPL"])
    finally:
        await c.aclose()

    assert [p.symbol for p in prices] == ["CRKN", "SNTI", "AAPL"], "종목이 누락됐다"
    by = {p.symbol: p for p in prices}
    assert by["CRKN"].last_u == 104_616
    assert by["SNTI"].last_u == 3_748        # "0.00374829"
    assert by["SNTI"].ts_ms is None          # 체결 없는 동전주 (계약 A2)
    assert by["AAPL"].last_u == 338_748_800
    assert c.counters["precision_rounded"] == 2


async def test_fixture_trades_including_fractional_quantity(mock_server, tmp_path):  # noqa: F811
    c = _client_on(mock_server, tmp_path, _fixture_body("lowprice_trades_subpenny.json"))
    try:
        trades = await c.get_trades("CRKN", count=50)
    finally:
        await c.aclose()

    assert len(trades) == 3, "체결이 유실됐다"
    assert {t.symbol for t in trades} == {"CRKN"}
    qtys = sorted(t.qty_u for t in trades)
    assert qtys[0] == 333_333          # "0.3333333333" 주 → 마이크로주 반올림
    assert c.counters["precision_rounded"] > 0


async def test_fixture_orderbook_survives(mock_server, tmp_path):  # noqa: F811
    c = _client_on(mock_server, tmp_path, _fixture_body("lowprice_orderbook_subpenny.json"))
    try:
        ob = await c.get_orderbook("CRKN")
    finally:
        await c.aclose()

    assert len(ob.bids) == 1 and len(ob.asks) == 1     # 미국 호가 1레벨 (계약 A2)
    assert ob.asks[0].price_u == 105_000               # "0.10499999" → 반올림
    assert ob.bids[0].price_u == 103_333               # "0.10333331" → 반올림
    assert ob.asks[0].price_u > ob.bids[0].price_u
    assert c.counters["precision_rounded"] > 0


async def test_fixture_rankings_survives(mock_server, tmp_path):  # noqa: F811
    c = _client_on(mock_server, tmp_path, _fixture_body("lowprice_rankings_subpenny.json"))
    try:
        page = await c.get_rankings("MARKET_TRADING_AMOUNT", duration="realtime")
    finally:
        await c.aclose()

    assert len(page.rows) == 2, "랭킹 행이 유실됐다"
    assert page.rows[0].symbol == "CRKN"
    assert page.rows[0].last_u == 104_616
    assert page.rows[0].amount_u == 4_932_981_023      # "4932.98102299" → 반올림
    assert c.counters["precision_rounded"] > 0


async def test_fixture_stocks_survives(mock_server, tmp_path):  # noqa: F811
    c = _client_on(mock_server, tmp_path, _fixture_body("lowprice_stocks_subpenny.json"))
    try:
        (meta,) = await c.get_stocks(["CRKN"])
    finally:
        await c.aclose()

    assert meta.symbol == "CRKN"
    assert meta.shares_outstanding_qu == 310_450_000_432_100
    assert c.counters["precision_rounded"] == 0   # 소수 4자리 → 마이크로 격자 안


# ---- client 카운터 자체 -------------------------------------------------


async def test_client_counter_starts_at_zero_and_is_per_client(mock_server, tmp_path):  # noqa: F811
    body = _fixture_body("lowprice_prices_subpenny.json")
    a = _client_on(mock_server, tmp_path, body)
    b = _client_on(mock_server, tmp_path, body)
    try:
        assert a.counters["precision_rounded"] == 0
        await a.get_prices(["CRKN"])
        assert a.counters["precision_rounded"] == 2
        assert b.counters["precision_rounded"] == 0, "카운터가 client 간에 샜다"
        await b.get_prices(["CRKN"])
        assert b.counters["precision_rounded"] == 2
    finally:
        await a.aclose()
        await b.aclose()


async def test_client_counter_accumulates_across_calls(mock_server, tmp_path):  # noqa: F811
    c = _client_on(mock_server, tmp_path, _fixture_body("lowprice_prices_subpenny.json"))
    try:
        await c.get_prices(["CRKN"])
        first = c.counters["precision_rounded"]
        await c.get_prices(["CRKN"])
        assert c.counters["precision_rounded"] == first * 2
    finally:
        await c.aclose()

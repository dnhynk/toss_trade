from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

from tossmon.api.models import Candle, CandlePage, Price, StockMeta
from tossmon.config import UniverseConfig
from tossmon.store.reader import Reader
from tossmon.store.writer import Store
from tossmon.universe import build as build_module
from tossmon.universe.build import build_universe
from tossmon.universe.filters import market_cap_u, passes_tier0
from tossmon.universe.runners import detect_former_runners, tag_dilution_stub
from tossmon.universe.seed import parse_directory_file


@pytest.fixture
def cfg() -> UniverseConfig:
    return UniverseConfig(
        price_min_u=100_000,
        price_max_u=20_000_000,
        mcap_min_u=10_000_000_000_000,
        mcap_max_u=300_000_000_000_000,
        tier1_max=200,
        tier2_max=50,
        tier3_max=10,
    )


def _meta(symbol: str = "ABCD", shares: int = 10_000_000) -> StockMeta:
    return StockMeta(
        symbol=symbol,
        name=f"{symbol} Corp",
        market="NASDAQ",
        security_type="STOCK",
        is_common=True,
        status="ACTIVE",
        list_date="2020-01-01",
        shares_outstanding_qu=shares * 1_000_000,
    )


@pytest.mark.parametrize(
    ("price_u", "shares", "expected"),
    [
        (100_000, 100_000_000, True),   # exact $0.10 and $10M
        (20_000_000, 15_000_000, True), # exact $20 and $300M
        (99_999, 100_000_000, False),
        (20_000_001, 15_000_000, False),
        (100_000, 99_999_999, False),
        (20_000_000, 15_000_001, False),
    ],
)
def test_tier0_filter_boundaries(cfg, price_u, shares, expected):
    meta = _meta(shares=shares)
    price = Price(meta.symbol, None, price_u)
    assert passes_tier0(meta, price, cfg) is expected


def test_filter_rejects_non_common_inactive_and_funds(cfg):
    price = Price("ABCD", None, 1_000_000)
    base = _meta(shares=20_000_000)
    assert market_cap_u(base, price) == 20_000_000_000_000
    assert not passes_tier0(replace(base, is_common=False), price, cfg)
    assert not passes_tier0(replace(base, status="HALTED"), price, cfg)
    assert not passes_tier0(
        replace(base, security_type="ETF", is_common=True), price, cfg
    )
    assert not passes_tier0(
        replace(base, security_type="ETN", is_common=True), price, cfg
    )


def test_parse_nasdaq_directory_handles_footer_etf_tests_and_duplicates(tmp_path):
    directory = tmp_path / "nasdaqlisted.txt"
    directory.write_text(
        "\ufeffSymbol|Security Name|Market Category|Test Issue|Financial Status|"
        "Round Lot Size|ETF|NextShares\n"
        "GOOD|Good Inc|Q|N|N|100|N|N\n"
        "GOOD|Duplicate|Q|N|N|100|N|N\n"
        "TEST|Test issue|Q|Y|N|100|N|N\n"
        "FUND|Fund ETF|G|N|N|100|Y|N\n"
        "BAD SYMBOL|Malformed|Q|N|N|100|N|N\n"
        "File Creation Time: 0730202618|||||||\n",
        encoding="utf-8",
    )
    assert parse_directory_file(directory) == ["GOOD"]


def test_parse_otherlisted_uses_act_symbol_and_rejects_bad_header(tmp_path):
    directory = tmp_path / "otherlisted.txt"
    directory.write_text(
        "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|"
        "Test Issue|NASDAQ Symbol\n"
        "BRK.B|Berkshire|N|BRK.B|N|100|N|BRK.B\n",
        encoding="utf-8",
    )
    assert parse_directory_file(directory) == ["BRK.B"]
    directory.write_text("Unknown|ETF\nX|N\n", encoding="utf-8")
    with pytest.raises(ValueError, match="symbol column"):
        parse_directory_file(directory)


def test_former_runner_detection_and_dilution_stub():
    day = 86_400_000
    frame = pd.DataFrame(
        [
            {"symbol": "UP", "ts_ms": day, "open_u": 100, "high_u": 130, "low_u": 90},
            {"symbol": "UP", "ts_ms": 2 * day, "open_u": 100, "high_u": 129, "low_u": 70},
            {"symbol": "FLAT", "ts_ms": 2 * day, "open_u": 100, "high_u": 129, "low_u": 71},
            {"symbol": "OLD", "ts_ms": 0, "open_u": 100, "high_u": 200, "low_u": 50},
        ]
    )
    result = detect_former_runners(frame, lookback_days=1)
    assert result.to_dict("records") == [
        {"symbol": "UP", "event_count": 2, "last_event_ms": 2 * day}
    ]
    assert tag_dilution_stub(["UP", "UP", "FLAT"]) == {"UP": {}, "FLAT": {}}


class FakeClient:
    def __init__(self):
        self.stock_batch_sizes: list[int] = []
        self.price_batch_sizes: list[int] = []

    async def get_stocks(self, symbols):
        self.stock_batch_sizes.append(len(symbols))
        return [_meta(symbol, shares=20_000_000) for symbol in symbols]

    async def get_prices(self, symbols):
        self.price_batch_sizes.append(len(symbols))
        return [Price(symbol, 1, 1_000_000) for symbol in symbols]

    async def get_candles(self, symbol, interval, count=200, before_ms=None, adjusted=True):
        high = 1_400_000 if symbol == "S000" else 1_100_000
        row = Candle(symbol, 1, 1_000_000, high, 900_000, 1_000_000, 1_000_000)
        return CandlePage([row], None)


@pytest.mark.asyncio
async def test_build_batches_200_and_persists_tiers(monkeypatch, tmp_path, cfg):
    symbols = [f"S{i:03d}" for i in range(201)]
    monkeypatch.setattr(build_module, "fetch_symbol_directory", lambda _path: symbols)
    client = FakeClient()
    db_path = tmp_path / "monitor.db"
    with Store(db_path) as store:
        summary = await build_universe(client, store, cfg)

    assert client.stock_batch_sizes == [200, 1]
    assert client.price_batch_sizes == [200, 1]
    assert summary == {"tier0": 201, "tier1": 200, "former_runners": 1}
    with Reader(db_path) as reader:
        symbols_frame = reader.symbols()
        assert len(symbols_frame) == 201
        assert symbols_frame.loc[
            symbols_frame["symbol"] == "S000", "is_former_runner"
        ].item() == 1
        assert len(reader.symbols(tier=1)) == 200

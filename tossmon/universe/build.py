"""유니버스 빌드 파이프라인 — 소유: W2.

seed → /stocks 메타 보강(mock) → 필터 → former runner 태깅 → symbols 테이블 tier0/tier1 기록.
일 1회 실행 전제.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pandas as pd

from ..api.client import TossClient
from ..api.models import Price, StockMeta
from ..config import UniverseConfig
from ..store.writer import Store
from .filters import market_cap_u, passes_tier0
from .runners import detect_former_runners, tag_dilution_stub
from .seed import fetch_symbol_directory

DEFAULT_CACHE_DIR = Path(".cache") / "nasdaq-trader"


def _chunks(rows: list[str], size: int = 200):
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


async def build_universe(client: TossClient, store: Store, cfg: UniverseConfig) -> dict:
    """반환: {"tier0": n, "tier1": n, "former_runners": n} 요약."""
    seed_symbols = await asyncio.to_thread(fetch_symbol_directory, DEFAULT_CACHE_DIR)

    metas: list[StockMeta] = []
    prices: list[Price] = []
    # Keep batching at this boundary even though TossClient also guarantees it;
    # fakes and alternative clients then receive the same <=200 request shape.
    for chunk in _chunks(seed_symbols):
        metas.extend(await client.get_stocks(chunk))
        prices.extend(await client.get_prices(chunk))

    meta_by_symbol = {meta.symbol: meta for meta in metas}
    price_by_symbol = {price.symbol: price for price in prices}
    eligible: list[tuple[StockMeta, Price, int]] = []
    for symbol in seed_symbols:
        meta = meta_by_symbol.get(symbol)
        price = price_by_symbol.get(symbol)
        if meta is not None and price is not None and passes_tier0(meta, price, cfg):
            eligible.append((meta, price, market_cap_u(meta, price)))

    store.upsert_symbols((meta for meta, _, _ in eligible), tier=0)

    daily: list[dict[str, int | str]] = []
    for meta, _, _ in eligible:
        page = await client.get_candles(
            meta.symbol, interval="1d", count=200, adjusted=True
        )
        store.upsert_candles_1d(page.candles)
        daily.extend(
            {
                "symbol": candle.symbol,
                "ts_ms": candle.ts_ms,
                "open_u": candle.open_u,
                "high_u": candle.high_u,
                "low_u": candle.low_u,
            }
            for candle in page.candles
        )

    if daily:
        former_frame = detect_former_runners(pd.DataFrame.from_records(daily))
        former_symbols = set(former_frame["symbol"].tolist())
    else:
        former_symbols = set()
    # The EDGAR phase is intentionally only an interface call.  The returned
    # empty tags are not persisted as filings.
    tag_dilution_stub(sorted(meta_by_symbol))
    store.set_former_runners(former_symbols)

    # Every tier0 name matches the low-price/low-cap thesis.  Prefer names with
    # observed runner history, then the smaller capitalizations, up to budget.
    tier1_ranked = sorted(
        eligible,
        key=lambda item: (
            item[0].symbol not in former_symbols,
            item[2],
            item[0].symbol,
        ),
    )
    tier1 = tier1_ranked[:cfg.tier1_max]
    store.upsert_symbols((meta for meta, _, _ in tier1), tier=1)
    return {
        "tier0": len(eligible),
        "tier1": len(tier1),
        "former_runners": len(former_symbols),
    }

"""유니버스 빌드 파이프라인 — 소유: W2.

seed → /stocks 메타 보강(mock) → 필터 → former runner 태깅 → symbols 테이블 tier0/tier1 기록.
일 1회 실행 전제.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pandas as pd

from ..api.client import TossClient
from ..api.errors import SchemaMismatch
from ..api.models import Price, StockMeta
from ..config import UniverseConfig
from ..store.writer import Store
from .filters import market_cap_u, passes_tier0
from .runners import detect_former_runners, tag_dilution_stub
from .seed import TOSS_SYMBOL_RE, fetch_symbol_directory

log = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path(".cache") / "nasdaq-trader"


def _chunks(rows: list[str], size: int = 200):
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


async def build_universe(client: TossClient, store: Store, cfg: UniverseConfig) -> dict:
    """반환: {"tier0", "tier1", "former_runners", "rejected_charset",
    "skipped_batches", "skipped_symbols"} 요약.

    `rejected_charset` 은 토스 API 문자셋(`seed.TOSS_SYMBOL_RE`) 밖이라 배치 전송 전에
    제외된 심볼 수 — `seed.py` 가 이미 걸러내므로 정상 경로에서는 보통 0이지만, 심볼
    소스가 바뀌거나 캐시가 오염돼도 배치 하나가 통째로 죽는 사고(라이브에서 실제 발생)를
    막는 두 번째 방어선이다. `skipped_batches`/`skipped_symbols` 는 방어를 통과했는데도
    `/stocks`·`/prices` 가 `SchemaMismatch`(계약 C-5: 재시도 금지, caller가 로그+스킵)로
    거부한 배치 — 그 배치만 스킵하고 나머지 배치는 계속 진행한다.
    """
    seed_symbols = await asyncio.to_thread(fetch_symbol_directory, DEFAULT_CACHE_DIR)

    valid_symbols = [s for s in seed_symbols if TOSS_SYMBOL_RE.fullmatch(s)]
    rejected_charset = len(seed_symbols) - len(valid_symbols)
    if rejected_charset:
        log.warning(
            "build_universe: %d seed symbol(s) rejected before /stocks — outside "
            "Toss API charset [A-Za-z0-9.-]", rejected_charset,
        )
    seed_symbols = valid_symbols

    metas: list[StockMeta] = []
    prices: list[Price] = []
    skipped_batches = 0
    skipped_symbols = 0
    # Keep batching at this boundary even though TossClient also guarantees it;
    # fakes and alternative clients then receive the same <=200 request shape.
    for chunk in _chunks(seed_symbols):
        try:
            chunk_metas = await client.get_stocks(chunk)
            chunk_prices = await client.get_prices(chunk)
        except SchemaMismatch as exc:
            # 계약 C-5: SchemaMismatch 는 재시도 금지, caller(이 루프)가 로그+스킵+카운터.
            # 배치 하나를 통째로 버려도 나머지 배치는 계속 진행한다 — 그렇지 않으면
            # 배치 200개 중 불량 심볼 1개가 유니버스 빌드 전체를 죽인다(라이브에서 실제 발생).
            skipped_batches += 1
            skipped_symbols += len(chunk)
            log.warning(
                "build_universe: batch of %d symbols skipped (%s) — starting %s",
                len(chunk), exc.detail, chunk[0],
            )
            continue
        metas.extend(chunk_metas)
        prices.extend(chunk_prices)

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
        "rejected_charset": rejected_charset,
        "skipped_batches": skipped_batches,
        "skipped_symbols": skipped_symbols,
    }

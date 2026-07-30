"""유니버스 필터 — 소유: W2. 기준: docs/03 §1 Tier 0 (보통주, ACTIVE, $0.1~$20, 시총 $10M~$300M, ETF/ETN 제외)."""
from __future__ import annotations

from ..api.models import Price, StockMeta
from ..config import UniverseConfig


def passes_tier0(meta: StockMeta, price: Price, cfg: UniverseConfig) -> bool:
    raise NotImplementedError


def market_cap_u(meta: StockMeta, price: Price) -> int:
    """lastPrice × sharesOutstanding (마이크로달러)."""
    raise NotImplementedError

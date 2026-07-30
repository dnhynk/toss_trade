"""유니버스 필터 — 소유: W2. 기준: docs/03 §1 Tier 0 (보통주, ACTIVE, $0.1~$20, 시총 $10M~$300M, ETF/ETN 제외)."""
from __future__ import annotations

from ..api.models import Price, StockMeta
from ..config import UniverseConfig


def passes_tier0(meta: StockMeta, price: Price, cfg: UniverseConfig) -> bool:
    if meta.symbol != price.symbol:
        return False
    security_type = meta.security_type.strip().upper()
    if not meta.is_common or security_type not in {"STOCK", "FOREIGN_STOCK"}:
        return False
    if "ETF" in security_type or "ETN" in security_type:
        return False
    if meta.status.strip().upper() != "ACTIVE":
        return False
    if meta.shares_outstanding_qu <= 0:
        return False
    if not cfg.price_min_u <= price.last_u <= cfg.price_max_u:
        return False
    cap_u = market_cap_u(meta, price)
    return cfg.mcap_min_u <= cap_u <= cfg.mcap_max_u


def market_cap_u(meta: StockMeta, price: Price) -> int:
    """lastPrice × sharesOutstanding (마이크로달러)."""
    if meta.shares_outstanding_qu < 0 or price.last_u < 0:
        raise ValueError("price and shares outstanding must be non-negative")
    # micro-USD/share × micro-shares ÷ 1e6 = micro-USD
    return (price.last_u * meta.shares_outstanding_qu) // 1_000_000

"""데이터 모델·변환 헬퍼 — 계약 C-1(시간)·C-2(가격)·C-3(모델).

시간: UTC epoch ms int. 가격/금액: 마이크로달러 int. 수량: 마이크로주 int.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

MICRO = 1_000_000


def iso_to_ms(s: str) -> int:
    """오프셋 포함 ISO 8601 → UTC epoch ms. naive 문자열은 SchemaMismatch."""
    raise NotImplementedError


def ms_to_iso_kst(ts_ms: int) -> str:
    """표시/로그 전용. 저장·비교에 사용 금지."""
    raise NotImplementedError


def ms_to_iso_et(ts_ms: int) -> str:
    """표시/로그 전용."""
    raise NotImplementedError


def dec_to_u(s: str | Decimal) -> int:
    """decimal 문자열 → 마이크로 단위 int. 1e-6 초과 정밀도는 SchemaMismatch."""
    raise NotImplementedError


def u_to_dec(u: int) -> Decimal:
    raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Price:
    symbol: str
    ts_ms: int | None
    last_u: int


@dataclass(frozen=True, slots=True)
class Candle:
    symbol: str
    ts_ms: int
    open_u: int
    high_u: int
    low_u: int
    close_u: int
    vol_qu: int


@dataclass(frozen=True, slots=True)
class CandlePage:
    candles: list[Candle]
    next_before_ms: int | None


@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    ts_ms: int
    price_u: int
    qty_u: int


@dataclass(frozen=True, slots=True)
class OrderbookLevel:
    price_u: int
    qty_u: int


@dataclass(frozen=True, slots=True)
class Orderbook:
    symbol: str
    ts_ms: int | None
    bids: list[OrderbookLevel]
    asks: list[OrderbookLevel]


@dataclass(frozen=True, slots=True)
class RankingRow:
    rank: int
    symbol: str
    last_u: int
    base_u: int
    change_rate: float | None
    vol_qu: int
    amount_u: int


@dataclass(frozen=True, slots=True)
class RankingPage:
    ranking_type: str
    duration: str
    ranked_at_ms: int | None
    rows: list[RankingRow]


@dataclass(frozen=True, slots=True)
class StockMeta:
    symbol: str
    name: str
    market: str
    security_type: str
    is_common: bool
    status: str
    list_date: str | None
    shares_outstanding_qu: int


@dataclass(frozen=True, slots=True)
class SessionWindow:
    start_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class UsMarketDay:
    date: str
    day: SessionWindow | None
    pre: SessionWindow | None
    regular: SessionWindow | None
    after: SessionWindow | None

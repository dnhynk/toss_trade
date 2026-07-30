"""TossClient — GET-only 하드 차단, 배치 청킹, 재시도, 모델 정규화 (계약 C-4·C-5).

모든 요청은 단일 _request() 관문을 지나며 endpoints.ALLOWLIST 를 검사한다.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal, Sequence

from .limiter import GroupRateLimiter
from .models import (
    CandlePage,
    Orderbook,
    Price,
    RankingPage,
    StockMeta,
    Trade,
    UsMarketDay,
)
from .tokens import TokenManager


class TossClient:
    def __init__(self, base_url: str, tokens: TokenManager, limiter: GroupRateLimiter,
                 timeout_s: float = 10.0):
        self.base_url = base_url
        self.tokens = tokens
        self.limiter = limiter
        self.timeout_s = timeout_s

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        """유일한 전송 관문. allowlist 검사 → limiter.acquire → 재시도 정책(C-5)."""
        raise NotImplementedError

    async def get_prices(self, symbols: Sequence[str]) -> list[Price]:
        """자동 200개 청킹."""
        raise NotImplementedError

    async def get_candles(self, symbol: str, interval: Literal["1m", "1d"],
                          count: int = 200, before_ms: int | None = None,
                          adjusted: bool = True) -> CandlePage:
        raise NotImplementedError

    async def get_trades(self, symbol: str, count: int = 50) -> list[Trade]:
        raise NotImplementedError

    async def get_orderbook(self, symbol: str) -> Orderbook:
        raise NotImplementedError

    async def get_rankings(self, ranking_type: str, duration: str = "realtime",
                           market: str = "US", count: int = 100,
                           exclude_caution: bool = False) -> RankingPage:
        raise NotImplementedError

    async def get_stocks(self, symbols: Sequence[str]) -> list[StockMeta]:
        """자동 200개 청킹."""
        raise NotImplementedError

    async def get_us_calendar(self, date: str | None = None) -> dict[str, UsMarketDay]:
        """keys: "previous" | "today" | "next"."""
        raise NotImplementedError

    async def get_exchange_rate(self) -> Decimal:
        """KRW per USD (참고용 표시 환율)."""
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError

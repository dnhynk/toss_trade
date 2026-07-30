"""Store (쓰기) — 계약 C-6. WAL, 배치 upsert, 멱등. 쓰기 주체는 collector 단일 프로세스. 소유: W2."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from ..api.models import Candle, Orderbook, RankingPage, StockMeta, Trade


class Store:
    def __init__(self, db_path: Path, read_only: bool = False):
        self.db_path = db_path
        self.read_only = read_only

    def upsert_candles_1m(self, rows: Iterable[Candle]) -> int:
        raise NotImplementedError

    def upsert_candles_1d(self, rows: Iterable[Candle]) -> int:
        raise NotImplementedError

    def upsert_symbols(self, rows: Iterable[StockMeta], tier: int | None = None) -> int:
        raise NotImplementedError

    def insert_rankings(self, snap_ms: int, page: RankingPage) -> int:
        raise NotImplementedError

    def insert_trades(self, trades: Iterable[Trade]) -> int:
        """중복(PK 충돌)은 조용히 무시하고 신규 건수만 반환."""
        raise NotImplementedError

    def insert_orderbook(self, snap_ms: int, ob: Orderbook) -> int:
        raise NotImplementedError

    def record_promotion(self, symbol: str, ts_ms: int, from_tier: int, to_tier: int,
                         reason: str, score: float) -> None:
        raise NotImplementedError

    def record_event(self, ev: dict) -> int:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

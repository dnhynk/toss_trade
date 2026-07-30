"""Store (쓰기) — 계약 C-6. WAL, 배치 upsert, 멱등. 쓰기 주체는 collector 단일 프로세스. 소유: W2."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

from ..api.models import Candle, Orderbook, RankingPage, StockMeta, Trade
from .migrations import apply_migrations


class Store:
    def __init__(self, db_path: Path, read_only: bool = False):
        self.db_path = Path(db_path)
        self.read_only = read_only
        self._closed = False
        if read_only:
            uri = f"file:{quote(self.db_path.resolve().as_posix(), safe='/:')}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, timeout=30.0)
            self._conn.execute("PRAGMA query_only=ON")
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path, timeout=30.0)
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            apply_migrations(self._conn)

    def _require_writer(self) -> None:
        if self._closed:
            raise RuntimeError("store is closed")
        if self.read_only:
            raise PermissionError("store was opened read-only")

    def _upsert_candles(self, table: str, rows: Iterable[Candle]) -> int:
        self._require_writer()
        # One polling response can overlap both the previous response and
        # itself.  Last value wins per key before the single DB transaction.
        unique = {(row.symbol, row.ts_ms): row for row in rows}
        if not unique:
            return 0
        values = [
            (
                row.symbol,
                row.ts_ms,
                row.open_u,
                row.high_u,
                row.low_u,
                row.close_u,
                row.vol_qu,
            )
            for row in unique.values()
        ]
        before = self._conn.total_changes
        with self._conn:
            self._conn.executemany(
                f"""
                INSERT INTO {table}
                    (symbol, ts_ms, open_u, high_u, low_u, close_u, vol_qu)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, ts_ms) DO UPDATE SET
                    open_u=excluded.open_u,
                    high_u=excluded.high_u,
                    low_u=excluded.low_u,
                    close_u=excluded.close_u,
                    vol_qu=excluded.vol_qu
                """,
                values,
            )
        return self._conn.total_changes - before

    def upsert_candles_1m(self, rows: Iterable[Candle]) -> int:
        return self._upsert_candles("candles_1m", rows)

    def upsert_candles_1d(self, rows: Iterable[Candle]) -> int:
        return self._upsert_candles("candles_1d", rows)

    def upsert_symbols(self, rows: Iterable[StockMeta], tier: int | None = None) -> int:
        self._require_writer()
        unique = {row.symbol: row for row in rows}
        if not unique:
            return 0
        updated_ms = time.time_ns() // 1_000_000
        values = [
            (
                row.symbol,
                row.name,
                row.market,
                row.security_type,
                row.status,
                row.list_date,
                row.shares_outstanding_qu,
                0 if tier is None else tier,
                updated_ms,
            )
            for row in unique.values()
        ]
        tier_update = "tier=symbols.tier" if tier is None else "tier=excluded.tier"
        before = self._conn.total_changes
        with self._conn:
            self._conn.executemany(
                f"""
                INSERT INTO symbols
                    (symbol, name, market, security_type, status, list_date,
                     shares_outstanding_qu, tier, updated_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    name=excluded.name,
                    market=excluded.market,
                    security_type=excluded.security_type,
                    status=excluded.status,
                    list_date=excluded.list_date,
                    shares_outstanding_qu=excluded.shares_outstanding_qu,
                    {tier_update},
                    updated_ms=excluded.updated_ms
                """,
                values,
            )
        return self._conn.total_changes - before

    def set_former_runners(self, symbols: Iterable[str]) -> int:
        """현재 former-runner 집합을 원자적으로 교체한다."""
        self._require_writer()
        wanted = sorted(set(symbols))
        before = self._conn.total_changes
        with self._conn:
            self._conn.execute(
                "UPDATE symbols SET is_former_runner = 0 WHERE is_former_runner <> 0"
            )
            self._conn.executemany(
                "UPDATE symbols SET is_former_runner = 1 WHERE symbol = ?",
                ((symbol,) for symbol in wanted),
            )
        return self._conn.total_changes - before

    def insert_rankings(self, snap_ms: int, page: RankingPage) -> int:
        self._require_writer()
        unique = {row.rank: row for row in page.rows}
        values = [
            (
                snap_ms,
                page.ranking_type,
                page.duration,
                row.rank,
                row.symbol,
                row.last_u,
                row.vol_qu,
                row.amount_u,
            )
            for row in unique.values()
        ]
        if not values:
            return 0
        before = self._conn.total_changes
        with self._conn:
            self._conn.executemany(
                """
                INSERT INTO rankings_snap
                    (snap_ms, ranking_type, duration, rank, symbol, last_u,
                     vol_qu, amount_u)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snap_ms, ranking_type, duration, rank) DO UPDATE SET
                    symbol=excluded.symbol,
                    last_u=excluded.last_u,
                    vol_qu=excluded.vol_qu,
                    amount_u=excluded.amount_u
                """,
                values,
            )
        return self._conn.total_changes - before

    def insert_trades(self, trades: Iterable[Trade]) -> int:
        """중복(PK 충돌)은 조용히 무시하고 신규 건수만 반환."""
        self._require_writer()
        values = {
            (row.symbol, row.ts_ms, row.price_u, row.qty_u) for row in trades
        }
        if not values:
            return 0
        before = self._conn.total_changes
        with self._conn:
            self._conn.executemany(
                """
                INSERT OR IGNORE INTO trades_snap(symbol, ts_ms, price_u, qty_u)
                VALUES (?, ?, ?, ?)
                """,
                values,
            )
        return self._conn.total_changes - before

    def insert_orderbook(self, snap_ms: int, ob: Orderbook) -> int:
        self._require_writer()
        bids = [
            {"price_u": level.price_u, "qty_u": level.qty_u}
            for level in ob.bids
        ]
        asks = [
            {"price_u": level.price_u, "qty_u": level.qty_u}
            for level in ob.asks
        ]
        bid1 = ob.bids[0] if ob.bids else None
        ask1 = ob.asks[0] if ob.asks else None
        spread_u = (
            ask1.price_u - bid1.price_u if bid1 is not None and ask1 is not None else None
        )
        bid_qty = sum(level.qty_u for level in ob.bids)
        ask_qty = sum(level.qty_u for level in ob.asks)
        total_qty = bid_qty + ask_qty
        imbalance = (bid_qty - ask_qty) / total_qty if total_qty else None
        with self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO orderbook_snap
                    (symbol, snap_ms, ts_ms, bid1_u, bid1_qu, ask1_u, ask1_qu,
                     depth_json, spread_u, imbalance)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ob.symbol,
                    snap_ms,
                    ob.ts_ms,
                    bid1.price_u if bid1 else None,
                    bid1.qty_u if bid1 else None,
                    ask1.price_u if ask1 else None,
                    ask1.qty_u if ask1 else None,
                    json.dumps(
                        {"bids": bids, "asks": asks},
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    spread_u,
                    imbalance,
                ),
            )
        return int(cursor.lastrowid)

    def record_promotion(self, symbol: str, ts_ms: int, from_tier: int, to_tier: int,
                         reason: str, score: float) -> None:
        self._require_writer()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO promotions
                    (symbol, ts_ms, from_tier, to_tier, reason, score)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (symbol, ts_ms, from_tier, to_tier, reason, score),
            )

    def record_event(self, ev: dict) -> int:
        self._require_writer()
        missing = {"symbol", "t0_ms", "kind"} - ev.keys()
        if missing:
            raise ValueError(f"event missing required keys: {sorted(missing)}")
        meta = ev.get("meta_json")
        if meta is not None and not isinstance(meta, str):
            meta = json.dumps(meta, separators=(",", ":"), sort_keys=True)
        with self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO events
                    (symbol, t0_ms, kind, peak_ms, peak_ret, ret_30m,
                     ret_close, session, meta_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ev["symbol"],
                    ev["t0_ms"],
                    ev["kind"],
                    ev.get("peak_ms"),
                    ev.get("peak_ret"),
                    ev.get("ret_30m"),
                    ev.get("ret_close"),
                    ev.get("session"),
                    meta,
                ),
            )
        return int(cursor.lastrowid)

    def close(self) -> None:
        if not self._closed:
            self._conn.close()
            self._closed = True

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

from __future__ import annotations

import sqlite3
import time

import pytest

from tossmon.api.models import (
    Candle,
    Orderbook,
    OrderbookLevel,
    RankingPage,
    RankingRow,
    StockMeta,
    Trade,
)
from tossmon.store.migrations import SCHEMA_VERSION, apply_migrations
from tossmon.store.reader import Reader
from tossmon.store.writer import Store


def candle(symbol: str, ts_ms: int, close_u: int = 1_000_000) -> Candle:
    return Candle(
        symbol=symbol,
        ts_ms=ts_ms,
        open_u=900_000,
        high_u=1_100_000,
        low_u=800_000,
        close_u=close_u,
        vol_qu=10_000_000,
    )


def test_schema_migration_is_repeatable_and_reopen_safe(tmp_path):
    db_path = tmp_path / "monitor.db"
    conn = sqlite3.connect(db_path)
    assert apply_migrations(conn) == SCHEMA_VERSION
    assert apply_migrations(conn) == SCHEMA_VERSION
    version = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()[0]
    assert int(version) == SCHEMA_VERSION
    conn.close()

    store = Store(db_path)
    journal = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert journal.lower() == "wal"
    store.close()
    reopened = Store(db_path)
    assert reopened._conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] == 0
    reopened.close()


def test_migration_rejects_newer_schema(tmp_path):
    conn = sqlite3.connect(tmp_path / "future.db")
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO meta VALUES ('schema_version', '999')")
    conn.commit()
    with pytest.raises(RuntimeError, match="newer"):
        apply_migrations(conn)
    conn.close()


def test_candle_upsert_is_idempotent_and_reader_is_read_only(tmp_path):
    db_path = tmp_path / "monitor.db"
    with Store(db_path) as store:
        assert store.upsert_candles_1m([candle("ABCD", 1000)]) == 1
        assert store.upsert_candles_1m([candle("ABCD", 1000, 1_050_000)]) == 1
        assert store._conn.execute("SELECT COUNT(*) FROM candles_1m").fetchone()[0] == 1

    with Reader(db_path) as reader:
        frame = reader.read_candles_1m("ABCD", 1000, 1000)
        assert frame["ts_ms"].dtype == "int64"
        assert frame.loc[0, "close_u"] == 1_050_000
        with pytest.raises(sqlite3.OperationalError):
            reader._conn.execute("DELETE FROM candles_1m")

    with Store(db_path, read_only=True) as readonly:
        with pytest.raises(PermissionError):
            readonly.upsert_candles_1m([candle("ABCD", 2000)])


def test_all_snapshot_writes_and_deduplication(tmp_path):
    db_path = tmp_path / "monitor.db"
    meta = StockMeta(
        symbol="ABCD",
        name="ABCD Inc.",
        market="NASDAQ",
        security_type="STOCK",
        is_common=True,
        status="ACTIVE",
        list_date="2020-01-01",
        shares_outstanding_qu=20_000_000_000_000,
    )
    trade = Trade("ABCD", 1000, 1_000_000, 2_000_000)
    row = RankingRow(1, "ABCD", 1_000_000, 900_000, 0.1, 3_000_000, 4_000_000)
    page = RankingPage("TOP_GAINERS", "1d", 1000, [row])
    orderbook = Orderbook(
        "ABCD",
        1000,
        [OrderbookLevel(990_000, 3_000_000)],
        [OrderbookLevel(1_010_000, 1_000_000)],
    )

    with Store(db_path) as store:
        assert store.upsert_symbols([meta], tier=1) == 1
        assert store.insert_trades([trade, trade]) == 1
        assert store.insert_trades([trade]) == 0
        assert store.insert_rankings(1000, page) == 1
        assert store.insert_rankings(1000, page) == 1
        orderbook_id = store.insert_orderbook(1001, orderbook)
        assert orderbook_id > 0
        values = store._conn.execute(
            "SELECT spread_u, imbalance_signed FROM orderbook_snap WHERE id=?",
            (orderbook_id,),
        ).fetchone()
        assert values[0] == 20_000
        assert values[1] == pytest.approx(0.5)
        store.record_promotion("ABCD", 1002, 0, 1, "former runner", 0.9)
        event_id = store.record_event(
            {"symbol": "ABCD", "t0_ms": 1003, "kind": "runner", "meta_json": {"x": 1}}
        )
        assert event_id > 0

    with Reader(db_path) as reader:
        assert len(reader.symbols(tier=1)) == 1
        assert len(reader.read_rankings("TOP_GAINERS", 1000, 1000)) == 1
        assert len(reader.read_events(1003, 1003)) == 1


def test_imbalance_signed_sign_convention(tmp_path):
    """계약 C-6 개정 A3: 중립 0, 양수 = 매수 우위, 음수 = 매도 우위."""
    db_path = tmp_path / "monitor.db"

    def imbalance_for(bid_qty_u: int, ask_qty_u: int) -> float | None:
        orderbook = Orderbook(
            "ABCD",
            1000,
            [OrderbookLevel(990_000, bid_qty_u)] if bid_qty_u else [],
            [OrderbookLevel(1_010_000, ask_qty_u)] if ask_qty_u else [],
        )
        with Store(db_path) as store:
            orderbook_id = store.insert_orderbook(1001, orderbook)
            return store._conn.execute(
                "SELECT imbalance_signed FROM orderbook_snap WHERE id=?",
                (orderbook_id,),
            ).fetchone()[0]

    assert imbalance_for(3_000_000, 1_000_000) == pytest.approx(0.5)
    assert imbalance_for(1_000_000, 1_000_000) == pytest.approx(0.0)
    assert imbalance_for(1_000_000, 3_000_000) == pytest.approx(-0.5)
    assert imbalance_for(0, 0) is None


def test_bulk_candle_write_performance_smoke(tmp_path):
    rows = [candle(f"S{i % 100:03d}", i) for i in range(10_000)]
    with Store(tmp_path / "bulk.db") as store:
        started = time.perf_counter()
        assert store.upsert_candles_1m(rows) == len(rows)
        elapsed = time.perf_counter() - started
    # A deliberately generous CI threshold: still demonstrates at least
    # 1,000 rows/s and catches accidental per-row commits.
    assert elapsed < 10.0

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
import tossmon.store.migrations as migrations
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


def test_events_upsert_updates_existing_row_with_latest_label(tmp_path):
    """검출기가 같은 (symbol, t0_ms) 를 재검출해도 행은 하나이고 라벨은 최신값이다."""
    db_path = tmp_path / "monitor.db"
    with Store(db_path) as store:
        first_id = store.record_event({"symbol": "ABCD", "t0_ms": 1000, "kind": "runner"})
        second_id = store.record_event(
            {
                "symbol": "ABCD",
                "t0_ms": 1000,
                "kind": "runner",
                "peak_ms": 1500,
                "peak_ret": 0.42,
                "ret_30m": 0.2,
                "ret_close": 0.3,
                "session": "regular",
                "meta_json": {"x": 2},
            }
        )
        assert second_id == first_id
        assert store._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        row = store._conn.execute(
            "SELECT peak_ms, peak_ret, ret_30m, ret_close, session, meta_json"
            " FROM events WHERE id=?",
            (first_id,),
        ).fetchone()
        assert row[0] == 1500
        assert row[1] == pytest.approx(0.42)
        assert row[2] == pytest.approx(0.2)
        assert row[3] == pytest.approx(0.3)
        assert row[4] == "regular"
        assert row[5] == '{"x":2}'


def test_events_different_t0_ms_are_separate_rows(tmp_path):
    db_path = tmp_path / "monitor.db"
    with Store(db_path) as store:
        store.record_event({"symbol": "ABCD", "t0_ms": 1000, "kind": "runner"})
        store.record_event({"symbol": "ABCD", "t0_ms": 2000, "kind": "runner"})
        assert store._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2


def test_events_migration_dedupes_preserving_latest_label(tmp_path, monkeypatch):
    """라이브에서 관측된 버그의 회귀 테스트: 구버전(v1) DB에 이미 쌓인 중복
    events 행을 마이그레이션이 보존적으로 정리하는지 검증한다 — 그룹당 id가
    가장 큰(가장 나중에 검출되어 라벨이 가장 완성된) 행만 남아야 한다."""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    monkeypatch.setattr(migrations, "SCHEMA_VERSION", 1)
    assert migrations.apply_migrations(conn) == 1

    conn.execute(
        "INSERT INTO events (symbol, t0_ms, kind) VALUES ('DFNS', 1785378480000, 'runner')"
    )
    conn.execute(
        "INSERT INTO events (symbol, t0_ms, kind) VALUES ('DFNS', 1785378480000, 'runner')"
    )
    conn.execute(
        """
        INSERT INTO events (symbol, t0_ms, kind, peak_ms, peak_ret, ret_close)
        VALUES ('DFNS', 1785378480000, 'runner', 1785378600000, 0.55, 0.4)
        """
    )
    conn.execute(
        "INSERT INTO events (symbol, t0_ms, kind) VALUES ('GCTK', 1785378840000, 'runner')"
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 4

    monkeypatch.undo()
    assert migrations.apply_migrations(conn) == migrations.SCHEMA_VERSION

    rows = conn.execute(
        "SELECT symbol, peak_ms, peak_ret, ret_close FROM events ORDER BY symbol"
    ).fetchall()
    assert len(rows) == 2
    dfns = next(r for r in rows if r[0] == "DFNS")
    assert dfns[1] == 1785378600000
    assert dfns[2] == pytest.approx(0.55)
    assert dfns[3] == pytest.approx(0.4)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO events (symbol, t0_ms, kind) VALUES ('GCTK', 1785378840000, 'runner')"
        )
    conn.close()


def test_bulk_candle_write_performance_smoke(tmp_path):
    rows = [candle(f"S{i % 100:03d}", i) for i in range(10_000)]
    with Store(tmp_path / "bulk.db") as store:
        started = time.perf_counter()
        assert store.upsert_candles_1m(rows) == len(rows)
        elapsed = time.perf_counter() - started
    # A deliberately generous CI threshold: still demonstrates at least
    # 1,000 rows/s and catches accidental per-row commits.
    assert elapsed < 10.0

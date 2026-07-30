"""tools/dryrun_night.py 테스트 — 소유: W5. 라이브 API 미사용, DB만으로 검증."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools import dryrun_night as dn  # noqa: E402
from tossmon.api.models import (  # noqa: E402
    Candle,
    Orderbook,
    OrderbookLevel,
    RankingPage,
    RankingRow,
    Trade,
)
from tossmon.store.writer import Store  # noqa: E402

MIN_MS = 60_000
SEC_MS = 1_000


def _seed_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "tossmon.db"
    store = Store(db_path)
    try:
        # BBB: 0~89분 빈틈없이 채워진 종목 (커버리지 100%, 이벤트 없음)
        dense = [
            Candle(symbol="BBB", ts_ms=m * MIN_MS, open_u=1_000_000, high_u=1_000_000,
                   low_u=1_000_000, close_u=1_000_000, vol_qu=100)
            for m in range(90)
        ]
        store.upsert_candles_1m(dense)

        # AAA: 0~39분 평평, 40~46분 공백(6분 갭 — 5분 기준 초과), 47분에 +20% 점프 후 유지
        # (부동소수 경계 오차를 피하려고 ret_min=0.15 보다 확실히 위인 +20%를 쓴다)
        rows = [
            Candle(symbol="AAA", ts_ms=m * MIN_MS, open_u=1_000_000, high_u=1_000_000,
                   low_u=1_000_000, close_u=1_000_000, vol_qu=100)
            for m in range(40)
        ]
        rows += [
            Candle(symbol="AAA", ts_ms=m * MIN_MS, open_u=1_200_000, high_u=1_200_000,
                   low_u=1_200_000, close_u=1_200_000, vol_qu=500)
            for m in range(47, 90)
        ]
        store.upsert_candles_1m(rows)

        # trades_snap: 4초 간격, 40s~100s 구간은 공백(60s, 24s 임계 초과)
        trades = [Trade(symbol="AAA", ts_ms=t * SEC_MS, price_u=1_000_000, qty_u=1_000)
                  for t in range(0, 41, 4)]
        trades += [Trade(symbol="AAA", ts_ms=t * SEC_MS, price_u=1_200_000, qty_u=1_000)
                  for t in range(100, 121, 4)]
        store.insert_trades(trades)

        # rankings_snap: 12초 간격, 한 번 60초 공백
        for t in list(range(0, 41, 12)) + list(range(100, 121, 12)):
            page = RankingPage(ranking_type="MARKET_TRADING_AMOUNT", duration="realtime",
                               ranked_at_ms=t * SEC_MS,
                               rows=[RankingRow(rank=1, symbol="AAA", last_u=1_000_000,
                                                base_u=1_000_000, change_rate=0.0,
                                                vol_qu=100, amount_u=100_000_000)])
            store.insert_rankings(t * SEC_MS, page)

        # orderbook_snap: 8초 간격 정상 (공백 없음)
        for t in range(0, 41, 8):
            ob = Orderbook(symbol="AAA", ts_ms=t * SEC_MS,
                           bids=[OrderbookLevel(price_u=999_000, qty_u=100)],
                           asks=[OrderbookLevel(price_u=1_001_000, qty_u=100)])
            store.insert_orderbook(t * SEC_MS, ob)
    finally:
        store.close()
    return db_path


def test_candle_coverage_detects_gap_and_full_coverage(tmp_path):
    db_path = _seed_db(tmp_path)
    start_ms, end_ms = 0, 90 * MIN_MS
    coverage = dn.build_coverage(db_path, start_ms, end_ms)

    assert coverage.table_counts["candles_1m"] == 90 + (40 + 43)  # BBB 90 + AAA (40+43)
    bbb = coverage.candle_coverage["BBB"]
    assert bbb["coverage_pct"] == 100.0
    assert bbb["gaps"] == []

    aaa = coverage.candle_coverage["AAA"]
    assert aaa["bars"] == 83
    assert len(aaa["gaps"]) == 1
    gap_start, gap_end, gap_min = aaa["gaps"][0]
    assert gap_start == 39 * MIN_MS
    assert gap_end == 47 * MIN_MS
    assert gap_min == 8.0


def test_snapshot_gaps_flag_trades_and_rankings_but_not_orderbook(tmp_path):
    db_path = _seed_db(tmp_path)
    coverage = dn.build_coverage(db_path, 0, 121 * SEC_MS)

    assert "AAA" in coverage.snapshot_gaps["trades_snap"]
    # rankings_snap 은 심볼이 아니라 ranking_type 별로 묶인다(한 스냅샷에 여러 심볼이 섞이므로).
    assert "MARKET_TRADING_AMOUNT" in coverage.snapshot_gaps["rankings_snap"]
    assert "orderbook_snap" not in coverage.snapshot_gaps or not coverage.snapshot_gaps["orderbook_snap"]


def test_find_event_candidates_detects_price_jump(tmp_path):
    db_path = _seed_db(tmp_path)
    events = dn.find_event_candidates(db_path, 0, 90 * MIN_MS)
    assert not events.empty
    aaa_events = events[events["symbol"] == "AAA"]
    assert len(aaa_events) == 1
    row = aaa_events.iloc[0]
    assert row["t0_ms"] == 47 * MIN_MS
    assert row["kind"] == "win"
    assert row["rvol_gated"] == False  # noqa: E712 — 베이스라인 없이 가격 조건만 (A1 §6)
    assert "BBB" not in set(events["symbol"])  # 평평한 종목은 후보 아님


def test_find_event_candidates_empty_db_returns_empty(tmp_path):
    db_path = tmp_path / "empty.db"
    Store(db_path).close()
    events = dn.find_event_candidates(db_path, 0, 1000)
    assert events.empty


def test_render_markdown_end_to_end(tmp_path):
    db_path = _seed_db(tmp_path)
    start_ms, end_ms = 0, 90 * MIN_MS
    coverage = dn.build_coverage(db_path, start_ms, end_ms)
    events = dn.find_event_candidates(db_path, start_ms, end_ms)
    log_stats = dn.scan_logs(tmp_path / "no_logs", window_s=300)
    report = dn.render_markdown(start_ms, end_ms, coverage, events, log_stats, dn.now_ms())
    assert "라이브 리허설 리포트" in report
    assert "candles_1m 채움률" in report
    assert "AAA" in report
    assert "rvol_gated=False" in report


def test_main_writes_report_file(tmp_path, monkeypatch):
    db_path = _seed_db(tmp_path)
    ops_cfg = tmp_path / "ops_config.yaml"
    ops_cfg.write_text(
        f"""
db_path: "{db_path.as_posix()}"
log_dir: "{(tmp_path / 'logs').as_posix()}"
""",
        encoding="utf-8",
    )
    out_path = tmp_path / "report.md"
    rc = dn.main([
        "--config", str(ops_cfg),
        "--start", "1970-01-01T00:00:00+00:00",
        "--end", "1970-01-01T01:30:00+00:00",
        "--out", str(out_path),
    ])
    assert rc == 0
    assert out_path.exists()
    text = out_path.read_text(encoding="utf-8")
    assert "라이브 리허설 리포트" in text

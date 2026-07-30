"""ops/healthcheck.py 테스트 — 소유: W5. 라이브 API 미사용, DB/로그/디스크만 다룬다."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from ops import healthcheck as hc
from ops.opsconfig import DiskThresholds, OpsConfig
from tossmon.api.models import Candle
from tossmon.store.writer import Store


def _make_store(tmp_path: Path) -> tuple[Store, Path]:
    db_path = tmp_path / "tossmon.db"
    return Store(db_path), db_path


def test_collect_db_stats_missing_db_returns_none(tmp_path):
    assert hc.collect_db_stats(tmp_path / "nope.db") is None


def test_collect_db_stats_counts_and_last_ts(tmp_path):
    store, db_path = _make_store(tmp_path)
    try:
        rows = [
            Candle(symbol="AAPL", ts_ms=1_000_000, open_u=1, high_u=2, low_u=1, close_u=2, vol_qu=10),
            Candle(symbol="AAPL", ts_ms=1_060_000, open_u=2, high_u=3, low_u=2, close_u=3, vol_qu=20),
        ]
        assert store.upsert_candles_1m(rows) == 2
    finally:
        store.close()

    stats = hc.collect_db_stats(db_path)
    assert stats is not None
    assert stats["candles_1m"].count == 2
    assert stats["candles_1m"].last_ts_ms == 1_060_000
    # 데이터가 없는 테이블도 스키마엔 존재하므로 count=0, last_ts=None 으로 나와야 한다.
    assert stats["trades_snap"].count == 0
    assert stats["trades_snap"].last_ts_ms is None


def test_compute_growth_first_run_is_none(tmp_path):
    current = {"candles_1m": hc.TableStat(count=5, last_ts_ms=1000)}
    growth = hc.compute_growth(current, tmp_path / "state.json", now=2000)
    assert growth["candles_1m"] is None
    assert (tmp_path / "state.json").exists()


def test_compute_growth_second_run_computes_rate(tmp_path):
    state_path = tmp_path / "state.json"
    first = {"candles_1m": hc.TableStat(count=5, last_ts_ms=1000)}
    hc.compute_growth(first, state_path, now=0)

    second = {"candles_1m": hc.TableStat(count=25, last_ts_ms=61000)}
    growth = hc.compute_growth(second, state_path, now=10_000)  # 10s later
    assert growth["candles_1m"] is not None
    assert growth["candles_1m"].rows_per_sec == pytest.approx(2.0, rel=1e-6)  # 20 rows / 10s


def test_scan_logs_no_dir_is_unavailable(tmp_path):
    stats = hc.scan_logs(tmp_path / "nope", window_s=300, now=time.time())
    assert stats.count_429 is None
    assert stats.request_lines is None


def test_scan_logs_counts_429_and_requests(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    p = log_dir / "collector.log"
    p.write_text(
        "2026-07-30 request ok\n"
        "2026-07-30 429 Too Many Requests\n"
        "2026-07-30 another request\n",
        encoding="utf-8",
    )
    stats = hc.scan_logs(log_dir, window_s=300, now=time.time())
    assert stats.count_429 == 1
    # "Requests"(429 메시지 안)도 request 어휘로 잡힌다 — 최선노력 스캔이라 과집계 방향으로 안전.
    assert stats.request_lines == 3
    assert stats.files_scanned == 1


def test_scan_logs_ignores_stale_files(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    p = log_dir / "old.log"
    p.write_text("429 429 429\n", encoding="utf-8")
    old_mtime = time.time() - 10_000
    import os

    os.utime(p, (old_mtime, old_mtime))
    stats = hc.scan_logs(log_dir, window_s=300, now=time.time())
    assert stats.files_scanned == 0
    assert stats.count_429 is None


def test_check_disk_status_thresholds(tmp_path):
    # 극단적으로 큰 임계값을 줘서 실제 디스크 여유와 무관하게 결정적으로 CRIT 되게 한다.
    disks = hc.check_disk([tmp_path], warn_free_gb=1e12, critical_free_gb=1e12)
    assert disks
    assert disks[0].status == hc.STATUS_CRIT

    disks_ok = hc.check_disk([tmp_path], warn_free_gb=0.0, critical_free_gb=0.0)
    assert disks_ok[0].status == hc.STATUS_OK


def test_staleness_status_thresholds():
    now = 100 * 60_000
    status, age = hc.staleness_status(now - 60_000, now, warn_min=5, crit_min=15)
    assert status == hc.STATUS_OK and age == pytest.approx(1.0)

    status, age = hc.staleness_status(now - 6 * 60_000, now, warn_min=5, crit_min=15)
    assert status == hc.STATUS_WARN

    status, age = hc.staleness_status(now - 20 * 60_000, now, warn_min=5, crit_min=15)
    assert status == hc.STATUS_CRIT

    # 데이터가 아직 없는 테이블(count=0)은 WARN이 아니라 OK — 수집 미시작/휴장일 수 있음.
    status, age = hc.staleness_status(None, now, warn_min=5, crit_min=15)
    assert status == hc.STATUS_OK and age is None


def test_build_report_no_db_is_warn(tmp_path):
    cfg = OpsConfig(
        db_path=tmp_path / "nope.db",
        log_dir=tmp_path / "logs",
        state_dir=tmp_path / "state",
        archive_dir=tmp_path / "archive",
        disk=DiskThresholds(warn_free_gb=0.0, critical_free_gb=0.0),
        stale_minutes_warn=5,
        stale_minutes_critical=15,
        log_retention_days=14,
        log_max_bytes=1000,
        collector_cmd=["python", "-c", "pass"],
        max_restarts_per_window=5,
        restart_window_s=600,
        restart_backoff_base_s=1.0,
        restart_backoff_cap_s=10.0,
    )
    report = hc.build_report(cfg, now=hc.now_ms())
    assert report.db_present is False
    assert report.overall_status == hc.STATUS_WARN
    text = hc.render_text(report)
    assert "healthcheck" in text
    assert "없음" in text


def test_build_report_flags_stale_table_that_has_data(tmp_path):
    db_path = tmp_path / "tossmon.db"
    store = Store(db_path)
    now = hc.now_ms()
    try:
        store.upsert_candles_1m([
            Candle(symbol="AAPL", ts_ms=now - 20 * 60_000, open_u=1, high_u=1, low_u=1,
                   close_u=1, vol_qu=1),
        ])
    finally:
        store.close()

    cfg = OpsConfig(
        db_path=db_path,
        log_dir=tmp_path / "logs",
        state_dir=tmp_path / "state",
        archive_dir=tmp_path / "archive",
        disk=DiskThresholds(warn_free_gb=0.0, critical_free_gb=0.0),
        stale_minutes_warn=5,
        stale_minutes_critical=15,
        log_retention_days=14,
        log_max_bytes=1000,
        collector_cmd=["python", "-c", "pass"],
        max_restarts_per_window=5,
        restart_window_s=600,
        restart_backoff_base_s=1.0,
        restart_backoff_cap_s=10.0,
    )
    report = hc.build_report(cfg, now=now)
    assert report.tables["candles_1m"]["status"] == hc.STATUS_CRIT
    assert report.overall_status == hc.STATUS_CRIT
    # 데이터가 아예 없던 다른 폴링 테이블(trades_snap 등)은 여전히 OK 여야 한다(오탐 방지).
    assert report.tables["trades_snap"]["status"] == hc.STATUS_OK


def test_build_report_with_fresh_data_is_ok(tmp_path):
    db_path = tmp_path / "tossmon.db"
    store = Store(db_path)
    now = hc.now_ms()
    try:
        store.upsert_candles_1m([
            Candle(symbol="AAPL", ts_ms=now - 30_000, open_u=1, high_u=1, low_u=1, close_u=1, vol_qu=1),
        ])
    finally:
        store.close()

    cfg = OpsConfig(
        db_path=db_path,
        log_dir=tmp_path / "logs",
        state_dir=tmp_path / "state",
        archive_dir=tmp_path / "archive",
        disk=DiskThresholds(warn_free_gb=0.0, critical_free_gb=0.0),
        stale_minutes_warn=5,
        stale_minutes_critical=15,
        log_retention_days=14,
        log_max_bytes=1000,
        collector_cmd=["python", "-c", "pass"],
        max_restarts_per_window=5,
        restart_window_s=600,
        restart_backoff_base_s=1.0,
        restart_backoff_cap_s=10.0,
    )
    report = hc.build_report(cfg, now=now)
    assert report.db_present is True
    assert report.tables["candles_1m"]["status"] == hc.STATUS_OK
    assert report.overall_status == hc.STATUS_OK

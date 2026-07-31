"""tools/dryrun_night.py 테스트 — 소유: W5. 라이브 API 미사용, DB만으로 검증."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

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


def _write_collector_log(log_dir: Path, lines: list[str]) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    p = log_dir / "collector.log"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_parse_telemetry_lines_extracts_fields_and_budget(tmp_path):
    lines = [
        "2026-07-30 22:30:05,001 INFO    collector start base_url=https://openapi.tossinvest.com "
        "live=True db=data/tossmon.db watch=0",
        "2026-07-30 22:35:00,123 INFO    telemetry session=regular watch=42 tier2=5 tier3=2 "
        "events=0 promotions=1 tape_gaps=0 api_errors=0 precision_rounded=3 precision_parsed=120 "
        "precision_rounded_pct=2.5 precision_max_digits=7 | budget MARKET_DATA=1.20/7.00 "
        "MARKET_DATA_CHART=0.50/3.50 RANKING=0.33/3.50",
        "2026-07-30 22:40:00,456 INFO    telemetry session=regular watch=45 tier2=6 tier3=3 "
        "events=1 promotions=2 tape_gaps=0 api_errors=0 precision_rounded=5 precision_parsed=240 "
        "precision_rounded_pct=2.1 precision_max_digits=8 | budget MARKET_DATA=1.50/7.00 "
        "MARKET_DATA_CHART=0.60/3.50 RANKING=0.33/3.50",
    ]
    _write_collector_log(tmp_path, lines)

    entries = dn.parse_telemetry_lines(tmp_path)
    assert len(entries) == 2
    assert entries[0]["fields"]["session"] == "regular"
    assert entries[0]["fields"]["watch"] == "42"
    assert entries[0]["budget"]["MARKET_DATA"] == "1.20/7.00"
    assert entries[1]["fields"]["precision_max_digits"] == "8"

    assert dn.count_collector_starts(tmp_path) == 1


def test_count_collector_starts_detects_restart(tmp_path):
    lines = [
        "2026-07-30 22:30:05,001 INFO    collector start base_url=https://openapi.tossinvest.com "
        "live=True db=data/tossmon.db watch=0",
        "2026-07-30 23:10:00,000 INFO    collector start base_url=https://openapi.tossinvest.com "
        "live=True db=data/tossmon.db watch=12",
    ]
    _write_collector_log(tmp_path, lines)
    assert dn.count_collector_starts(tmp_path) == 2


def test_parse_telemetry_lines_missing_log_returns_empty(tmp_path):
    assert dn.parse_telemetry_lines(tmp_path) == []
    assert dn.count_collector_starts(tmp_path) == 0


def test_summarize_telemetry_tracks_max_digits_and_last_sample(tmp_path):
    lines = [
        "2026-07-30 22:35:00,123 INFO    telemetry session=regular watch=42 tier2=5 tier3=2 "
        "events=0 promotions=1 tape_gaps=0 api_errors=0 precision_rounded=3 precision_parsed=120 "
        "precision_rounded_pct=2.5 precision_max_digits=8 | budget MARKET_DATA=1.20/7.00",
        "2026-07-30 22:40:00,456 INFO    telemetry session=regular watch=45 tier2=6 tier3=3 "
        "events=1 promotions=2 tape_gaps=0 api_errors=0 precision_rounded=5 precision_parsed=240 "
        "precision_rounded_pct=2.1 precision_max_digits=4 | budget MARKET_DATA=1.50/7.00",
    ]
    _write_collector_log(tmp_path, lines)
    entries = dn.parse_telemetry_lines(tmp_path)
    summary = dn.summarize_telemetry(entries, dn.count_collector_starts(tmp_path))
    assert summary.samples == 2
    # 최고치(8)는 두 번째(마지막) 샘플의 4가 아니라 전체 중 최댓값이어야 한다.
    assert summary.max_precision_digits == 8
    assert summary.last_fields["watch"] == "45"
    assert summary.collector_starts == 0


def test_render_markdown_includes_telemetry_section(tmp_path):
    db_path = _seed_db(tmp_path)
    lines = [
        "2026-07-30 22:35:00,123 INFO    telemetry session=regular watch=42 tier2=5 tier3=2 "
        "events=0 promotions=1 tape_gaps=0 api_errors=0 precision_rounded=3 precision_parsed=120 "
        "precision_rounded_pct=2.5 precision_max_digits=8 | budget MARKET_DATA=1.20/7.00",
    ]
    _write_collector_log(tmp_path, lines)
    coverage = dn.build_coverage(db_path, 0, 90 * MIN_MS)
    events = dn.find_event_candidates(db_path, 0, 90 * MIN_MS)
    log_stats = dn.scan_logs(tmp_path, window_s=300)
    entries = dn.parse_telemetry_lines(tmp_path)
    telemetry = dn.summarize_telemetry(entries, dn.count_collector_starts(tmp_path))
    report = dn.render_markdown(0, 90 * MIN_MS, coverage, events, log_stats, dn.now_ms(), telemetry)
    assert "Collector 텔레메트리" in report
    assert "MARKET_DATA: 1.20/7.00" in report
    assert "재시작 없음" in report


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


# --------------------------------------------------------------------------- #
# W5 후속 — 무인 야간 리허설 최종 리포트 확장 (세션 전환/예산/승격 정밀도/rvol_gated/재시작)
# --------------------------------------------------------------------------- #

def test_parse_session_transitions_extracts_pairs_with_timestamp(tmp_path):
    _write_collector_log(tmp_path, [
        "2026-07-30 22:30:01,940 INFO    session pre → regular",
        "2026-07-31 05:00:02,100 INFO    session regular → after",
    ])
    out = dn.parse_session_transitions(tmp_path)
    assert len(out) == 2
    assert out[0]["from"] == "pre" and out[0]["to"] == "regular"
    assert out[0]["ts"].hour == 22 and out[0]["ts"].minute == 30
    assert out[1]["from"] == "regular" and out[1]["to"] == "after"


def test_restart_gaps_first_start_has_none_gap_second_has_measured_gap(tmp_path):
    _write_collector_log(tmp_path, [
        "2026-07-30 21:35:30,058 INFO    collector start base_url=x live=True db=y watch=0",
        "2026-07-30 21:50:00,000 INFO    tier ↑ AAA: 1→2 reason=ranking_entry score=0.5",
        "2026-07-30 22:07:41,979 INFO    collector start base_url=x live=True db=y watch=40",
    ])
    out = dn.restart_gaps(tmp_path)
    assert len(out) == 2
    assert out[0]["gap_min"] is None and out[0]["note"] == "최초 시작"
    # 마지막 활동(21:50:00) -> 재시작(22:07:41.979) = 17분 41.979초 ≈ 17.7분
    assert out[1]["gap_min"] == pytest.approx(17.7, abs=0.05)


def test_budget_actual_summary_computes_min_max_and_target():
    entries = [
        {"budget": {"MARKET_DATA": "1.20/7.00", "RANKING": "0.33/3.50"}},
        {"budget": {"MARKET_DATA": "0.32/7.00", "RANKING": "0.33/3.50"}},
        {"budget": {"MARKET_DATA": "1.50/7.00", "RANKING": "0.33/3.50"}},
    ]
    out = dn.budget_actual_summary(entries)
    assert out["MARKET_DATA"]["min"] == pytest.approx(0.32)
    assert out["MARKET_DATA"]["max"] == pytest.approx(1.50)
    assert out["MARKET_DATA"]["target"] == pytest.approx(7.00)
    assert out["MARKET_DATA"]["samples"] == 3
    assert out["RANKING"]["min"] == out["RANKING"]["max"] == pytest.approx(0.33)


def test_tape_gap_rate_by_session_computes_delta_over_time():
    from datetime import datetime

    entries = [
        {"ts": datetime(2026, 7, 30, 21, 35), "fields": {"session": "pre", "tape_gaps": "0"}},
        {"ts": datetime(2026, 7, 30, 22, 30), "fields": {"session": "pre", "tape_gaps": "36"}},
        {"ts": datetime(2026, 7, 30, 22, 30), "fields": {"session": "regular", "tape_gaps": "36"}},
        {"ts": datetime(2026, 7, 30, 23, 35), "fields": {"session": "regular", "tape_gaps": "262"}},
    ]
    out = dn.tape_gap_rate_by_session(entries)
    # pre: 0 -> 36 over 55 minutes
    assert out["pre"]["delta_gaps"] == 36
    assert out["pre"]["span_min"] == pytest.approx(55.0)
    # regular: 36 -> 262 over 65 minutes
    assert out["regular"]["delta_gaps"] == 226
    assert out["regular"]["span_min"] == pytest.approx(65.0)
    assert out["regular"]["rate_per_min"] > out["pre"]["rate_per_min"]


def test_count_fake_budget_errors_detects_backwards_comparison(tmp_path):
    _write_collector_log(tmp_path, [
        "2026-07-31 12:34:11,398 ERROR   budget: RANKING predicted 0.33 req/s > target 3.50 "
        "— 랭킹은 축소 대상이 아니다. 주기/한도를 재검토하라",
        "2026-07-31 12:35:00,000 ERROR   budget: RANKING predicted 5.00 req/s > target 3.50 "
        "— 랭킹은 축소 대상이 아니다. 주기/한도를 재검토하라",
    ])
    out = dn.count_fake_budget_errors(tmp_path)
    assert out["total"] == 2
    assert out["fake"] == 1  # 0.33 <= 3.50 인데 ERROR
    assert out["real"] == 1  # 5.00 > 3.50 은 진짜


def test_promotion_reason_stats_led_to_event_and_residency(tmp_path):
    db_path = tmp_path / "tossmon.db"
    store = Store(db_path)
    try:
        # AAA: ranking_entry 로 승격, 재직 중 이벤트 발생 (10분 체류)
        store.record_promotion("AAA", 0, 1, 2, "ranking_entry", 0.5)
        store.record_event({"symbol": "AAA", "t0_ms": 300_000, "kind": "win"})
        store.record_promotion("AAA", 600_000, 2, 1, "score_decay", 0.1)

        # BBB: ranking_entry 로 승격, 이벤트 없이 2분 체류
        store.record_promotion("BBB", 0, 1, 2, "ranking_entry", 0.5)
        store.record_promotion("BBB", 120_000, 2, 1, "score_decay", 0.1)

        # CCC: first_print 로 승격, 리포트 종료 시점까지 강등 안 됨(열린 에피소드)
        store.record_promotion("CCC", 0, 1, 2, "first_print", 0.8)
    finally:
        store.close()

    df = dn.promotion_reason_stats(db_path, 0, 600_000)
    ranking = df[df["reason"] == "ranking_entry"].iloc[0]
    assert ranking["promotions"] == 2
    assert ranking["led_to_event_pct"] == pytest.approx(50.0)
    assert ranking["avg_residency_min"] == pytest.approx(6.0)  # (10+2)/2
    assert ranking["residency_samples"] == 2

    first_print = df[df["reason"] == "first_print"].iloc[0]
    assert first_print["promotions"] == 1
    assert first_print["avg_residency_min"] == pytest.approx(10.0)  # 0 ~ 600_000ms(end) = 10분
    assert first_print["led_to_event_pct"] == pytest.approx(0.0)


def test_event_rvol_gate_breakdown_counts_true_false_unknown(tmp_path):
    db_path = tmp_path / "tossmon.db"
    store = Store(db_path)
    try:
        store.record_event({"symbol": "AAA", "t0_ms": 1000, "kind": "win",
                            "meta_json": {"rvol_gated": True}})
        store.record_event({"symbol": "BBB", "t0_ms": 2000, "kind": "day",
                            "meta_json": {"rvol_gated": False}})
        store.record_event({"symbol": "CCC", "t0_ms": 3000, "kind": "win"})  # meta_json 없음
    finally:
        store.close()

    out = dn.event_rvol_gate_breakdown(db_path, 0, 10_000)
    assert out["total"] == 3
    assert out["gated_true"] == 1
    assert out["gated_false"] == 1
    assert out["unknown"] == 1


def test_render_markdown_includes_data_caveat_and_new_sections(tmp_path):
    db_path = _seed_db(tmp_path)
    coverage = dn.build_coverage(db_path, 0, 90 * MIN_MS)
    events = dn.find_event_candidates(db_path, 0, 90 * MIN_MS)
    log_stats = dn.scan_logs(tmp_path, window_s=300)
    report = dn.render_markdown(
        0, 90 * MIN_MS, coverage, events, log_stats, dn.now_ms(),
        session_transitions=[{"ts": None, "from": "pre", "to": "regular"}],
        budget_summary={"MARKET_DATA": {"min": 0.3, "max": 1.5, "target": 7.0, "samples": 10}},
        tape_gap_rates={"regular": {"delta_gaps": 226, "span_min": 65.0, "rate_per_min": 3.47,
                                    "samples": 12}},
        promotion_stats=dn.pd.DataFrame([{"reason": "ranking_entry", "promotions": 100,
                                          "led_to_event_pct": 12.0, "avg_residency_min": 6.0,
                                          "median_residency_min": 5.0, "residency_samples": 100}]),
        rvol_gate={"total": 3, "gated_true": 1, "gated_false": 1, "unknown": 1},
        fake_budget_errors={"total": 2, "fake": 1, "real": 1},
        restarts=[{"restart_at": None, "gap_min": None, "note": "최초 시작"}],
        data_caveat="유니버스 필터가 수집 경로에 적용되지 않았다 — 이 데이터는 표적 모집단이 아니다.",
    )
    assert "데이터 한계" in report
    assert "유니버스 필터가 수집 경로에 적용되지 않았다" in report
    assert "세션 전환 이력" in report
    assert "그룹별 예산 실측 vs 계산치" in report
    assert "승격 사유별 정밀도" in report
    assert "rvol_gated" in report
    assert "재시작 이력" in report
    assert "테이프 갭 세션별 발생률" in report
    assert "BudgetGuard RANKING 비교 로직 역전" in report

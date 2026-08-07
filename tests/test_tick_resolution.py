"""0단계 관문 (docs/41) — **계측기가 자기 자신을 속이지 않는지.**

## 이 파일이 지키는 것

1. **초 안의 순서를 시간으로 쓰지 않는다.** 저장 순서를 넣으면 하락 0건이 나온다는
   사실 자체를 고정한다 — 이게 무너지면 슈팅 길이·매수매도 판정이 전부 흔들린다.
2. **슈팅 길이는 초 막대 위에서만 잰다.** 같은 초 안의 행 여러 개가 지속시간 0초짜리
   슈팅을 만들어내면 안 된다.
3. **랭킹 나이 추정에 검정력이 있다.** 심어둔 지연을 되찾아야 하고, 위약에서는 사라져야 한다.
4. **경계를 코드가 들고 있다.** 세션 띠·관측 창 시작이 상수로 있고 리포트에 실린다.
5. **판정 금지.** 관문은 스트림의 물리적 한계만 말한다.
"""
from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from tossmon.analysis.measure import tick_resolution as TR

U = 1_000_000
BASE = TR.WINDOW_START_MS + 40 * 3600 * 1000        # 창 안, regular 근처


def _db(tmp_path, trades=(), books=(), ranks=()):
    """최소 스키마의 임시 DB. 실제 스키마와 **같은 PK** 를 쓴다 — 정렬 artefact 재현용."""
    p = tmp_path / "t.db"
    conn = sqlite3.connect(p)
    conn.executescript("""
        CREATE TABLE trades_snap (symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL,
            price_u INTEGER NOT NULL, qty_u INTEGER NOT NULL,
            PRIMARY KEY (symbol, ts_ms, price_u, qty_u)) WITHOUT ROWID;
        CREATE TABLE orderbook_snap (id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL, snap_ms INTEGER NOT NULL, ts_ms INTEGER);
        CREATE TABLE rankings_snap (id INTEGER PRIMARY KEY AUTOINCREMENT,
            snap_ms INTEGER NOT NULL, ranking_type TEXT NOT NULL, duration TEXT NOT NULL,
            rank INTEGER NOT NULL, symbol TEXT NOT NULL, last_u INTEGER NOT NULL,
            vol_qu INTEGER NOT NULL, amount_u INTEGER NOT NULL);
    """)
    conn.executemany("INSERT OR IGNORE INTO trades_snap VALUES (?,?,?,?)", trades)
    conn.executemany("INSERT INTO orderbook_snap (symbol, snap_ms, ts_ms) "
                     "VALUES (?,?,?)", books)
    conn.executemany("INSERT INTO rankings_snap (snap_ms, ranking_type, duration, "
                     "rank, symbol, last_u, vol_qu, amount_u) "
                     "VALUES (?,?,?,?,?,?,1,1)", ranks)
    conn.commit()
    conn.close()
    return p


# --------------------------------------------------------------------------- #
# 1. 경계는 코드가 들고 있다
# --------------------------------------------------------------------------- #
def test_session_bands_are_absolute_not_session_date():
    """세션 구분은 ET 벽시계 상수다 — `session_date` 가 바뀌어도 이 값은 안 흔들린다."""
    names = [n for n, _, _ in TR.SESSION_BANDS]
    assert names == ["pre", "regular", "after", "overnight"]
    # 09:30 ET 는 regular 의 첫 초, 09:29:59 는 pre 의 마지막 초
    day0 = TR.WINDOW_START_MS - 4 * 3600 * 1000        # 그날 00:00 ET
    at = lambda h, m, s=0: day0 + int((h * 3600 + m * 60 + s) * 1000)
    got = TR.session_of(np.array([at(9, 29, 59), at(9, 30), at(15, 59),
                                  at(16, 0), at(21, 0), at(2, 0)]))
    assert list(got) == ["pre", "regular", "regular", "after",
                         "overnight", "overnight"]


def test_window_start_excludes_the_narrow_era():
    """08-04 이전을 섞지 않는다 — 창 시작이 상수로 박혀 있다."""
    assert TR.WINDOW_START_MS == 1_785_830_400_000


def test_pct_table_refuses_to_invent_a_median():
    assert TR.pct_table([]) == {"n": 0}
    t = TR.pct_table([1.0, 2.0, 3.0])
    assert t["n"] == 3 and t["p50"] == 2.0


# --------------------------------------------------------------------------- #
# 2. 초 안의 순서는 시간이 아니다 — 이 사실을 계측기가 붙잡고 있는가
# --------------------------------------------------------------------------- #
def test_stored_order_inside_a_second_is_price_ascending(tmp_path):
    """가설: PK 순서 탓에 같은 초가 가격 오름차순으로 나온다.

    반증 관측: 일부러 **내림차순으로 넣는다.** 그래도 오름차순으로 읽히면 가설이 참이다.
    """
    rows = [("AAA", BASE, int(px * U), 1) for px in (3.0, 2.0, 1.0)]
    p = _db(tmp_path, trades=rows)
    conn = TR.open_ro(p)
    got = [r[0] for r in conn.execute(
        "SELECT price_u FROM trades_snap ORDER BY symbol, ts_ms")]
    assert got == sorted(got)                       # 넣은 순서와 무관하게 오름차순


def test_artifact_measure_reports_zero_downticks_and_a_fair_control(tmp_path):
    rows = []
    for sec in range(300):                          # 500건 넘겨 임계 통과
        for px in (1.00, 1.02, 1.01):               # 한 초에 3건, 넣는 순서는 뒤죽박죽
            rows.append(("AAA", BASE + sec * 1000, int(px * U), 1))
    p = _db(tmp_path, trades=rows)
    conn = TR.open_ro(p)
    got = TR.intra_second_order_artifact(conn, min_trades=100)
    assert got["stored_order"]["down"] == 0         # 저장 순서에는 하락이 없다
    assert got["stored_order"]["up_share"] > 0.6
    assert got["shuffled"]["down"] > 0              # 섞으면 하락이 생긴다
    assert got["rows_in_multi_trade_seconds_share"] == pytest.approx(1.0)


def test_second_bars_do_not_expose_open_or_close(tmp_path):
    """초 막대에는 open/close 가 **없어야** 한다 — 있으면 순서를 쓴 것이다."""
    rows = [("AAA", BASE, int(px * U), 2) for px in (1.0, 3.0)]
    p = _db(tmp_path, trades=rows)
    ts, n, lo, hi, vwap = TR.second_bars(TR.open_ro(p), "AAA")
    assert list(n) == [2] and lo[0] == 1.0 * U and hi[0] == 3.0 * U
    assert vwap[0] == pytest.approx(2.0 * U)        # 수량 동일 -> 산술 평균


# --------------------------------------------------------------------------- #
# 3. 슈팅 길이 — 초 막대 위에서만, 그리고 눈금이 만들어낸 0초가 없어야 한다
# --------------------------------------------------------------------------- #
def _ramp(symbol, start_sec, prices):
    return [(symbol, BASE + int((start_sec + i) * 1000), int(px * U), 1)
            for i, px in enumerate(prices)]


def test_shot_duration_is_measured_in_seconds_not_rows(tmp_path):
    """한 초 안에 여러 행이 있어도 지속시간 0초짜리 슈팅이 생기면 안 된다."""
    rows = []
    for sec in range(600):                          # 조용한 배경
        rows.append(("AAA", BASE + sec * 1000, int(1.00 * U), 1))
    # 한 초 안에 +5% 가 통째로 들어간다 (행 단위로 읽으면 0초 슈팅이 된다)
    for px in (1.00, 1.03, 1.05):
        rows.append(("AAA", BASE + 300_000, int(px * U), 1))
    p = _db(tmp_path, trades=rows)
    got = TR.shot_durations(TR.open_ro(p), min_trades=100)
    if got["n_shots"]:
        assert got["duration_s"]["min"] >= 1.0      # 0초 슈팅이 없다
    assert got["intra_second_rise_share"] > 0        # 대신 초 안 상승으로 세어 둔다


def test_shot_duration_recovers_a_planted_ramp(tmp_path):
    rows = [("AAA", BASE + s * 1000, int(1.00 * U), 1) for s in range(400)]
    rows += _ramp("AAA", 500, [1.00, 1.004, 1.008, 1.012])     # 3초 만에 +1.2%
    rows += [("AAA", BASE + s * 1000, int(1.012 * U), 1) for s in range(510, 700)]
    p = _db(tmp_path, trades=rows)
    got = TR.shot_durations(TR.open_ro(p), min_trades=100)
    assert got["n_shots"] >= 1
    assert got["duration_s"]["min"] == pytest.approx(3.0)


def test_shot_price_mode_is_recorded_so_the_number_is_readable(tmp_path):
    rows = [("AAA", BASE + s * 1000, int(1.00 * U), 1) for s in range(600)]
    p = _db(tmp_path, trades=rows)
    got = TR.shot_durations(TR.open_ro(p), min_trades=100, price_mode="lo_to_hi")
    assert got["price_mode"] == "lo_to_hi"
    with pytest.raises(ValueError):
        TR.shot_durations(TR.open_ro(p), min_trades=100, price_mode="close")


# --------------------------------------------------------------------------- #
# 4. 포화 — 상한이 물린 것을 세는가
# --------------------------------------------------------------------------- #
def test_saturation_counts_buckets_at_the_cap(tmp_path):
    rows = [("AAA", BASE, int((1.0 + i / 1000) * U), 1)
            for i in range(TR.TRADES_COUNT_CAP)]      # 한 4초 칸에 정확히 50건
    rows += [("BBB", BASE, int(1.0 * U), 1)]
    p = _db(tmp_path, trades=rows)
    got = TR.tape_saturation(TR.open_ro(p))
    assert got["buckets_at_cap"] == 1
    assert got["n_buckets"] == 2
    assert got["rows_in_capped_buckets_share"] == pytest.approx(50 / 51)


def test_tape_gap_log_separates_reentry_from_a_real_hole(tmp_path):
    log = tmp_path / "c.log"
    log.write_text(
        "2026-08-07 01:00:00,000 WARNING tape gap AAA: prev_max=1786000000000 "
        "< this_min=1786000005000 (n=50) - x\n"
        "2026-08-07 01:00:01,000 WARNING tape gap BBB: prev_max=1785000000000 "
        "< this_min=1786000005000 (n=12) - x\n", encoding="utf-8")
    got = TR.tape_gaps_from_log(log)
    assert got["lines"] == 2
    assert got["reentry_excluded"] == 1              # 하루 넘는 구멍은 결손이 아니다
    assert got["within_session_gaps"] == 1
    assert got["at_cap"] == 1
    assert got["hole_s"]["p50"] == pytest.approx(5.0)


def test_missing_log_is_said_out_loud_not_silently_zero(tmp_path):
    got = TR.tape_gaps_from_log(tmp_path / "nope.log")
    assert got["available"] is False


# --------------------------------------------------------------------------- #
# 5. 랭킹 나이 — 심은 지연을 되찾고, 위약에서는 사라지는가
# --------------------------------------------------------------------------- #
def test_ranking_age_recovers_a_planted_delay(tmp_path):
    lag_s = 7
    prices = [1.0 + (i % 37) / 100.0 for i in range(1200)]      # 매 초 값이 바뀐다
    trades = [("AAA", BASE + i * 1000, int(px * U), 1)
              for i, px in enumerate(prices)]
    ranks = [(BASE + i * 1000, "TOSS_SECURITIES_TRADING_VOLUME", "realtime", 1,
              "AAA", int(prices[i - lag_s] * U))
             for i in range(200, 1100)]
    p = _db(tmp_path, trades=trades, ranks=ranks)
    conn = TR.open_ro(p)
    got = TR.ranking_price_age(conn, top_symbols=5)
    assert got["best_offset_s"] == lag_s
    assert got["best_match_rate"] > 0.9
    pb = TR.ranking_price_age(conn, top_symbols=5, placebo=True)
    assert pb["best_match_rate"] < 0.3               # 시각을 끊으면 봉우리가 사라진다


# --------------------------------------------------------------------------- #
# 6. 관문 — 랭킹은 **순위열**로 판정되어야 한다
# --------------------------------------------------------------------------- #
def _skeleton(shot_p50, order_poll_s=12.4, price_age_s=1):
    return {
        "shot_durations": {"duration_s": {"p50": shot_p50}},
        "trade_observation_lag": {"delay_p50_s": 2.5},
        "cadence": {"orderbook": {"tier3_lane_interval_s": {"p50": 4.0}},
                    "rankings": {"TOSS_SECURITIES_TRADING_VOLUME":
                                 {"p50": order_poll_s}}},
        "orderbook_lag": {"active_symbols": {"p01": 0.5}},
        "ranking_price_age": {"best_offset_s": price_age_s},
        "watch_width": {"symbols_per_minute": {"p50": 4.0}},
    }


def test_gate_reports_ranking_order_separately_from_its_price_field():
    got = TR.gate_contrast(_skeleton(30.0))
    fields = [s["field"] for s in got["streams"] if s["stream"] == "rankings_snap"]
    assert len(fields) == 2                          # 가격 필드와 순위열을 따로 적는다
    order = [s for s in got["streams"] if "order" in s["field"]][0]
    price = [s for s in got["streams"] if "lastPrice" in s["field"]][0]
    assert order["lag_p50_s"] > price["lag_p50_s"] + 10
    assert order["measured_here"] is False           # 남의 실측임을 숨기지 않는다


def test_gate_closes_when_every_stream_is_slower_than_the_shot():
    got = TR.gate_contrast(_skeleton(1.0))           # 1초짜리 슈팅
    assert got["trigger_capable"] == []
    assert all(s["shorter_than_shot_p50"] is False for s in got["streams"])


def test_gate_always_carries_the_coverage_caveat():
    """지연을 통과해도 안 보고 있으면 못 잡는다 — 관문이 이 문장을 빠뜨리면 안 된다."""
    got = TR.gate_contrast(_skeleton(30.0))
    assert "watch_width" in got["coverage_caveat"]


# --------------------------------------------------------------------------- #
# 7. 판정 금지
# --------------------------------------------------------------------------- #
def test_module_makes_no_open_or_closed_claim():
    src = (TR.__doc__ or "") + (TR.gate_contrast.__doc__ or "")
    for banned in ("성립한다", "후보를 닫", "전략이 유효"):
        assert banned not in src

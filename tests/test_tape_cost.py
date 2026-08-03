"""체결 테이프 비용 러너 (docs/23 §10-R) — 정의·정합·교란 통제의 정직성.

## 이 파일이 지키는 것

1. **유효 스프레드의 2배 인자를 고정한다.** 이 프로젝트에서 이미 인자 실수(100배
   `vol_qu`)가 났다. 정의가 바뀌면 여기가 깨져야 한다.
2. **정합은 체결 직전 호가만 쓴다.** 체결 이후 호가를 끌어다 쓰면 미래를 보게 된다.
3. **간극 초과는 버리고 센다.** 조용히 줄지 않는다.
4. **크기 비교는 통제 없이 결론내지 않는다** — 같은 종목·같은 세션에서 크기만 다른
   체결을 짝짓고, 체결 직전 변동성 차이도 함께 낸다(역인과 함정).
"""
from __future__ import annotations

import json
import pathlib
import sqlite3

import numpy as np
import pandas as pd
import pytest

from tossmon.analysis import session as SS
from tossmon.analysis.measure import design_b as D
from tossmon.analysis.measure import tape_cost as TC

U = 1_000_000


# --------------------------------------------------------------------------- #
# 1. 정의 — 2배 인자
# --------------------------------------------------------------------------- #
def test_effective_spread_factor_is_two_and_is_declared():
    """**왕복이면 양쪽에서 지불하므로 2다.** 상수와 계산이 함께 움직여야 한다."""
    assert TC.EFFECTIVE_SPREAD_FACTOR == 2.0
    # 중간값 $10.00, 체결 $10.05 -> 편도 0.5%, 왕복 1.0%
    got = TC.effective_spread(pd.Series([10.05 * U]), pd.Series([10.00 * U]))
    assert float(got.iloc[0]) == pytest.approx(0.01)


def test_effective_spread_is_symmetric_in_direction():
    """매수든 매도든 **지불한 크기**는 같다 — 절댓값이기 때문이다."""
    buy = TC.effective_spread(pd.Series([10.05 * U]), pd.Series([10.0 * U]))
    sell = TC.effective_spread(pd.Series([9.95 * U]), pd.Series([10.0 * U]))
    assert float(buy.iloc[0]) == pytest.approx(float(sell.iloc[0]))


def test_effective_spread_is_nan_without_a_usable_mid():
    got = TC.effective_spread(pd.Series([10.0 * U]), pd.Series([0.0]))
    assert float(got.iloc[0]) != float(got.iloc[0])


def test_direction_signs_are_lee_ready_style():
    d = TC.trade_direction(pd.Series([10.05 * U, 9.95 * U, 10.0 * U]),
                           pd.Series([10.0 * U] * 3))
    assert list(d) == [1.0, -1.0, 0.0]


def test_the_factor_of_two_is_documented_where_a_reader_will_see_it():
    src = pathlib.Path(TC.__file__).read_text(encoding="utf-8")
    assert "2 x |P - M| / M" in src
    assert "round-trip" in src.lower()


def test_notional_uses_the_micro_unit_contract():
    """`price_u` 마이크로USD x `qty_u` 마이크로주 -> 1e12 로 나눈다 (계약 C-2)."""
    assert float(TC.trade_notional_usd(pd.Series([3 * U]),
                                       pd.Series([100 * U])).iloc[0]) == pytest.approx(300.0)


# --------------------------------------------------------------------------- #
# 2. 구간
# --------------------------------------------------------------------------- #
def test_buckets_are_half_open_and_two_thousand_has_its_own_cell():
    assert TC.bucket_of(100.0) == "$100-500"
    assert TC.bucket_of(499.99) == "$100-500"
    assert TC.bucket_of(2000.0) == "$2000-5000"
    assert TC.bucket_of(99.0) is None            # 구간 밖은 조용히 끼워넣지 않는다
    assert TC.bucket_for_clip(2000) == "$2000-5000"


# --------------------------------------------------------------------------- #
# 3. 시각 정합
# --------------------------------------------------------------------------- #
def _l1(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["symbol", "snap_ms", "bid1_u", "ask1_u"])
    df["bid1_qu"] = 100.0 * U
    df["ask1_qu"] = 100.0 * U
    df["mid_u"] = (df["bid1_u"] + df["ask1_u"]) / 2.0
    return df


def _tape(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["symbol", "ts_ms", "price_u", "qty_u"])
    df["notional_usd"] = TC.trade_notional_usd(df["price_u"], df["qty_u"])
    df["price_usd"] = df["price_u"] / U
    df["band"] = "$2-5"
    df["session"] = SS.sessions_of(df["ts_ms"])
    return df


def test_alignment_uses_the_quote_before_the_trade_not_after():
    """체결 **이후** 호가를 쓰면 미래를 보게 된다."""
    l1 = _l1([("A", 0, 3.00 * U, 3.02 * U), ("A", 10_000, 9.00 * U, 9.02 * U)])
    tape = _tape([("A", 5_000, 3.02 * U, 100 * U)])
    got = TC.align_trades_to_l1(tape, l1, max_gap_s=20)["aligned"]
    assert len(got) == 1
    assert float(got.iloc[0]["mid_u"]) == pytest.approx(3.01 * U)   # 9.01 이 아니다


def test_trades_beyond_the_gap_are_dropped_and_counted():
    """조용히 줄지 않는다 — 버린 수를 반드시 낸다."""
    l1 = _l1([("A", 0, 3.00 * U, 3.02 * U)])
    tape = _tape([("A", 5_000, 3.02 * U, 100 * U),
                  ("A", 900_000, 3.02 * U, 100 * U)])     # 15분 뒤 -> 버린다
    got = TC.align_trades_to_l1(tape, l1, max_gap_s=20)
    assert got["n_trades"] == 2 and got["n_aligned"] == 1
    assert got["n_dropped_no_quote"] == 1


def test_symbols_without_any_quote_are_dropped_not_defaulted():
    l1 = _l1([("A", 0, 3.00 * U, 3.02 * U)])
    tape = _tape([("B", 1_000, 3.02 * U, 100 * U)])
    got = TC.align_trades_to_l1(tape, l1, max_gap_s=20)
    assert got["n_aligned"] == 0 and got["n_dropped_no_quote"] == 1


def test_crossed_quotes_do_not_produce_a_mid():
    """ask < bid 면 중간값이 의미를 잃는다 — 섞으면 비용이 낙관 쪽으로 밀린다."""
    l1 = _l1([("A", 0, 3.10 * U, 3.00 * U)])
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE orderbook_snap (symbol TEXT, snap_ms INTEGER, "
                 "bid1_u INTEGER, bid1_qu REAL, ask1_u INTEGER, ask1_qu REAL)")
    conn.execute("INSERT INTO orderbook_snap VALUES ('A',0,3100000,100.0,3000000,100.0)")
    conn.commit()
    got = TC.load_l1(conn)
    conn.close()
    assert float(got.iloc[0]["mid_u"]) != float(got.iloc[0]["mid_u"])   # NaN
    assert len(l1) == 1


def test_gap_sensitivity_reports_every_requested_gap():
    l1 = _l1([("A", 0, 3.00 * U, 3.02 * U)])
    tape = _tape([("A", 3_000, 3.02 * U, 100 * U)])
    got = TC.gap_sensitivity(tape, l1, gaps=(5, 30))
    assert [g["max_gap_s"] for g in got] == [5, 30]


# --------------------------------------------------------------------------- #
# 4. 교란 통제 — 역인과 함정
# --------------------------------------------------------------------------- #
def test_size_contrast_pairs_only_symbols_present_in_both_buckets():
    """같은 종목에서 크기만 다른 체결을 비교해야 한다."""
    rows = []
    for sym, bucket_usd, spread in (("A", 200, 0.01), ("A", 3000, 0.03),
                                    ("B", 200, 0.02)):
        for _ in range(TC.MIN_TRADES_PER_SYMBOL_BUCKET):
            rows.append({"symbol": sym, "session": "regular", "band": "$2-5",
                         "notional_usd": bucket_usd, "eff_spread": spread,
                         "pre_trade_vol": 0.001,
                         "bucket": TC.bucket_of(float(bucket_usd))})
    got = TC.within_symbol_size_contrast(pd.DataFrame(rows))
    reg = got["by_session"]["regular"]
    assert reg["n_symbols"] == 1                        # A 만 두 구간에 있다
    assert reg["median_within_symbol_delta"] == pytest.approx(0.02)
    assert reg["powered"] is False                      # n<10 이면 판정하지 않는다


def test_size_contrast_also_reports_the_pre_trade_volatility_difference():
    """**통제 없이 '큰 클립이 비싸다'고 쓰지 않기 위한 열이다.**

    큰 체결이 더 요동치던 순간에 몰려 있으면 스프레드 차이는 크기 탓이 아닐 수 있다.
    """
    rows = []
    for bucket_usd, spread, vol in ((200, 0.01, 0.001), (3000, 0.03, 0.009)):
        for _ in range(TC.MIN_TRADES_PER_SYMBOL_BUCKET):
            rows.append({"symbol": "A", "session": "regular", "band": "$2-5",
                         "notional_usd": bucket_usd, "eff_spread": spread,
                         "pre_trade_vol": vol,
                         "bucket": TC.bucket_of(float(bucket_usd))})
    got = TC.within_symbol_size_contrast(pd.DataFrame(rows))["by_session"]["regular"]
    assert got["median_pre_trade_vol_delta"] == pytest.approx(0.008)


def test_pre_trade_volatility_uses_only_quotes_before_the_trade():
    l1 = _l1([("A", i * 1000, 3.0 * U, 3.02 * U) for i in range(10)]
             + [("A", 20_000, 9.0 * U, 9.02 * U)])
    aligned = pd.DataFrame([{"symbol": "A", "ts_ms": 9_500}])
    got = TC.add_pre_trade_volatility(aligned, l1, window_s=300)
    assert float(got.iloc[0]["pre_trade_vol"]) == pytest.approx(0.0, abs=1e-12)


# --------------------------------------------------------------------------- #
# 5. 비교표 — 못 낸 칸을 지어내지 않는다
# --------------------------------------------------------------------------- #
def test_comparison_leaves_unmeasurable_walk_cells_as_nan():
    realized = [{"session": "regular", "bucket": "$2000-5000", "n": 50,
                 "effective_spread_median": 0.02, "powered": True}]
    walk = {"regular": {"by_clip": {2000: {"measurable": False,
                                           "median": float("nan")}}}}
    got = [c for c in TC.tape_vs_walk_the_book(realized, walk)
           if c["session"] == "regular" and c["clip"] == 2000][0]
    assert got["walk_round_trip"] != got["walk_round_trip"]        # NaN
    assert got["tape_round_trip"] == pytest.approx(0.02 + D.COMMISSION_ROUND_TRIP)


def test_comparison_withholds_the_tape_cell_when_underpowered():
    realized = [{"session": "regular", "bucket": "$2000-5000", "n": 5,
                 "effective_spread_median": 0.02, "powered": False}]
    got = [c for c in TC.tape_vs_walk_the_book(realized, {})
           if c["session"] == "regular" and c["clip"] == 2000][0]
    assert got["tape_round_trip"] != got["tape_round_trip"]        # NaN


def test_tape_cost_adds_commission_so_it_is_comparable_to_walk_the_book():
    src = pathlib.Path(TC.__file__).read_text(encoding="utf-8")
    assert "COMMISSION_ROUND_TRIP" in src


# --------------------------------------------------------------------------- #
# 6. 러너 전체 — 합성 DB
# --------------------------------------------------------------------------- #
def _tiny_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db = tmp_path / "tape.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE orderbook_snap (symbol TEXT, snap_ms INTEGER, "
                 "bid1_u INTEGER, bid1_qu REAL, ask1_u INTEGER, ask1_qu REAL, "
                 "depth_json TEXT)")
    conn.execute("CREATE TABLE trades_snap (symbol TEXT, ts_ms INTEGER, "
                 "price_u INTEGER, qty_u REAL)")
    base = 1_785_500_000_000            # regular session
    ob, tp = [], []
    for sym in ("AAA", "BBB"):
        for i in range(40):
            ts = base + i * 16_000
            ob.append((sym, ts, int(3.00 * U), 500.0 * U, int(3.02 * U),
                       500.0 * U, None))
            # 작은 체결과 큰 체결을 번갈아 -> 두 구간이 모두 채워진다
            for usd, px in ((200, 3.01), (3000, 3.03)):
                tp.append((sym, ts + 3_000, int(px * U),
                           float(usd / px) * U))
    conn.executemany("INSERT INTO orderbook_snap VALUES (?,?,?,?,?,?,?)", ob)
    conn.executemany("INSERT INTO trades_snap VALUES (?,?,?,?)", tp)
    conn.commit()
    conn.close()
    return db


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("tape")
    db = _tiny_db(tmp)
    out = tmp / "out"
    assert TC.main(db, out_dir=out) == 0
    return json.loads((out / "tape_cost.json").read_text(encoding="utf-8")), out


def test_runner_reports_alignment_accounting(report):
    rep, _ = report
    al = rep["alignment"]
    assert al["n_trades"] == al["n_aligned"] + al["n_dropped_no_quote"]


def test_runner_rows_match_the_manifest_exactly(report):
    rep, _ = report
    assert rep["realized"], "runner produced no rows"
    for row in rep["realized"]:
        assert set(row) == set(TC.REPORTED_FIELDS), row.get("bucket")


def test_runner_states_the_tier3_selection_bias(report):
    """편향을 산출물에 **박아** 둔다 — 표만 떼어 인용되는 것을 막는다."""
    rep, _ = report
    assert "tier3" in rep["bias"] and "UNOBSERVED" in rep["bias"]


def test_runner_keeps_every_session_visible(report):
    rep, _ = report
    assert {r["session"] for r in rep["realized"]} == set(SS.SESSIONS)

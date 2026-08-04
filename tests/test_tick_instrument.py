"""틱 해상도 계측기 (docs/28) — 자가 정확하고, **판정하지 않는지**.

## 이 파일이 지키는 것

1. **틱 규칙의 정의** — 상승=매수, 하락=매도, **동일가는 직전 판정 유지**.
   첫 구간은 `0`(미판정)이며 0 으로 채우지 않고 **세어서 보고**한다.
2. **동시점 반응은 순환이다** — 틱 규칙이 가격으로 방향을 정하므로, 같은 창의
   가격 변화와 불균형은 정의상 붙어 있다. 산출물이 그 사실을 **표시**해야 한다.
3. **건수가 주 지표** — 사용자 근거: 큰 주문은 잘게 쪼개져 나온다.
4. **판정 금지** — 모듈이 "신호가 있다/없다" 를 말하지 않는다.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3

import numpy as np
import pandas as pd
import pytest

from tossmon.analysis import session as SS
from tossmon.analysis.measure import tick_instrument as TI

U = 1_000_000
BASE = 1_785_500_000_000        # regular session


# --------------------------------------------------------------------------- #
# 1. 틱 규칙의 정의
# --------------------------------------------------------------------------- #
def test_uptick_is_buy_and_downtick_is_sell():
    got = TI.tick_classify(np.array([10.0, 11.0, 9.0]) * U)
    assert got[1] == 1 and got[2] == -1


def test_equal_price_carries_the_previous_side_forward():
    """동일가는 **직전 판정 유지** — 0 으로 두면 활동이 사라진다."""
    got = TI.tick_classify(np.array([10.0, 11.0, 11.0, 11.0]) * U)
    assert list(got[1:]) == [1, 1, 1]


def test_the_leading_run_is_unclassified_not_zero_filled():
    """직전 판정이 없는 앞 구간은 **미판정**이고, 그 사실을 셀 수 있어야 한다."""
    got = TI.tick_classify(np.array([10.0, 10.0, 10.0]) * U)
    assert list(got) == [0, 0, 0]


def test_classification_needs_no_quote_so_it_covers_every_trade():
    got = TI.tick_classify(np.arange(1, 51, dtype="float64") * U)
    assert (got[1:] == 1).all()


def test_quote_rule_is_price_versus_mid():
    got = TI.quote_classify([10.5 * U, 9.5 * U, 10.0 * U], [10.0 * U] * 3)
    assert list(got) == [1, -1, 0]


def test_quote_rule_is_unclassified_without_a_usable_mid():
    got = TI.quote_classify([10.0 * U, 10.0 * U], [0.0, float("nan")])
    assert list(got) == [0, 0]


# --------------------------------------------------------------------------- #
# 2. 동시점 반응은 순환임을 산출물이 말해야 한다
# --------------------------------------------------------------------------- #
def test_same_window_response_is_flagged_as_circular():
    """**틱 규칙은 가격으로 방향을 정한다** — 같은 창 비교는 정의상 붙어 있다."""
    src = pathlib.Path(TI.__file__).read_text(encoding="utf-8")
    assert "mechanically_circular" in src
    assert "circular" in src.lower()


def test_a_pure_uptrend_makes_imbalance_and_same_window_return_move_together():
    """순환이 실제로 일어남을 자료로 보인다 — 그래서 다음 창을 봐야 한다."""
    ticks = _ticks([("A", i, 10.0 + 0.01 * i, 1.0) for i in range(60)])
    f = TI.window_frame(ticks, 10)
    assert (f["imbalance"] > 0.9).all()
    assert (f["ret_bp"] > 0).all()


def test_next_window_return_is_not_taken_across_a_gap():
    """다음 창이 **연속하지 않으면** 이어 붙이지 않는다."""
    ticks = _ticks([("A", i, 10.0, 1.0) for i in range(5)]
                   + [("A", 500 + i, 10.0, 1.0) for i in range(5)])
    f = TI.window_frame(ticks, 10).sort_values("bucket")
    assert np.isnan(float(f["fwd_ret_bp"].iloc[-1]))


# --------------------------------------------------------------------------- #
# 3. 건수가 주 지표
# --------------------------------------------------------------------------- #
def test_count_is_primary_and_volume_is_secondary():
    src = pathlib.Path(TI.__file__).read_text(encoding="utf-8")
    assert "trade COUNT (volume is secondary)" in src
    assert "trades_per_s_median" in str(TI.REPORTED_FIELDS)


def test_intensity_counts_trades_not_size():
    """같은 거래량이라도 **건수가 많으면 강도가 높다** — 주포는 쪼개서 낸다."""
    few_big = _ticks([("A", i * 5, 10.0, 100.0) for i in range(12)])
    many_small = _ticks([("B", i, 10.0, 1.0) for i in range(60)])
    a = TI.window_frame(few_big, 10)["trades_per_s"].median()
    b = TI.window_frame(many_small, 10)["trades_per_s"].median()
    assert b > a


# --------------------------------------------------------------------------- #
# 4. 판정 금지
# --------------------------------------------------------------------------- #
def test_module_emits_no_verdict_keys():
    """이 작업은 계측기를 만드는 것까지다 — 게이트도 판정 키도 내보내지 않는다.

    단어 검색이 아니라 **구조**를 본다: 면책 문구에 "signal exists" 가 들어가는 것은
    정상이고(판정을 안 한다는 선언이다), 금지해야 하는 것은 **판정을 담은 키**다.
    """
    src = pathlib.Path(TI.__file__).read_text(encoding="utf-8")
    for banned_key in ('"verdict"', '"proceed"', '"gate"', '"stopped_at"',
                       '"above_zero"', '"crosses_zero"'):
        assert banned_key not in src, banned_key


def test_module_carries_an_explicit_no_verdict_disclaimer():
    src = pathlib.Path(TI.__file__).read_text(encoding="utf-8")
    assert "no_verdict_note" in src
    assert "does not" in src


def test_every_reported_row_carries_its_sample_size():
    assert "n_windows" in TI.REPORTED_FIELDS
    assert "observed_enough" in TI.REPORTED_FIELDS


# --------------------------------------------------------------------------- #
# 5. 표본·경계 규율
# --------------------------------------------------------------------------- #
def _ticks(rows) -> pd.DataFrame:
    df = pd.DataFrame([{"symbol": s, "ts_ms": BASE + int(sec * 1000),
                        "price_u": int(px * U), "qty_u": q * U}
                       for s, sec, px, q in rows])
    df["session"] = SS.sessions_of(df["ts_ms"])
    df["cycle_date"] = df["ts_ms"].map(lambda m: SS.session_date(int(m)))
    df["era"] = SS.eras_of(df["ts_ms"])
    df["side_tick"] = df.groupby("symbol")["price_u"].transform(
        lambda s: pd.Series(TI.tick_classify(s.to_numpy()), index=s.index))
    return df


def test_thin_symbol_days_are_excluded_and_counted():
    thin = _ticks([("A", i, 10.0, 1.0) for i in range(10)])
    inv = TI.symbol_day_inventory(thin)
    assert inv and inv[0]["usable"] is False
    assert TI.usable_symbol_days(thin).empty


def test_windows_never_pool_across_a_collector_boundary():
    """경계를 넘어 뭉치면 표본 구성 변화를 시장 변화로 오독한다."""
    src = pathlib.Path(TI.__file__).read_text(encoding="utf-8")
    assert '"era"' in src
    rows = [("A", i, 10.0, 1.0) for i in range(4)]
    f = TI.window_frame(_ticks(rows), 10)
    assert "era" in f.columns


def test_days_needed_inverts_the_usable_rate():
    inv = [{"symbol": f"S{i}", "cycle_date": "2026-08-03", "usable": True}
           for i in range(10)]
    got = TI.days_needed_for_symbol_days(inv, target=30)
    assert got["reachable"] and got["cycles_needed"] == 3


def test_days_needed_says_unreachable_with_no_usable_days():
    got = TI.days_needed_for_symbol_days(
        [{"symbol": "A", "cycle_date": "2026-08-03", "usable": False}])
    assert got["reachable"] is False


def test_agreement_reports_how_narrow_the_comparable_slice_is():
    """일치율만 인용하면 **얼마나 좁은 구간의 이야기인지** 사라진다."""
    ticks = _ticks([("A", i, 10.0 + 0.01 * i, 1.0) for i in range(30)])
    quotes = pd.DataFrame({"symbol": ["A"], "snap_ms": [BASE],
                           "bid1_u": [10.0 * U], "ask1_u": [10.02 * U],
                           "mid_u": [10.01 * U], "rel_spread": [0.002]})
    got = TI.classifier_agreement(ticks, quotes, max_gap_s=2)
    if got.get("available"):
        assert "comparable_share" in got and "caveat" in got
        assert got["n_comparable"] <= got["n_trades_total"]


# --------------------------------------------------------------------------- #
# 6. 러너 전체
# --------------------------------------------------------------------------- #
def _tiny_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db = tmp_path / "tick.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE trades_snap (symbol TEXT, ts_ms INTEGER, "
                 "price_u INTEGER, qty_u REAL)")
    conn.execute("CREATE TABLE orderbook_snap (symbol TEXT, snap_ms INTEGER, "
                 "bid1_u INTEGER, ask1_u INTEGER)")
    rng = np.random.default_rng(7)
    held = int(pd.Timestamp("2026-07-28T18:00:00").value // 1_000_000) - SS.KST_OFFSET_MS
    tr, ob = [], []
    for si, sym in enumerate(("AAA", "BBB")):
        px = 10.0
        for i in range(900):
            px *= float(np.exp(rng.normal(0, 0.0008)))
            ts = BASE + i * 1000
            tr.append((sym, ts, int(px * U), 1.0 * U))
            if i % 16 == 0:
                ob.append((sym, ts, int(px * 0.999 * U), int(px * 1.001 * U)))
        tr.append((sym, held + si * 1000, int(10 * U), 1.0 * U))   # 봉인 구간
    conn.executemany("INSERT INTO trades_snap VALUES (?,?,?,?)", tr)
    conn.executemany("INSERT INTO orderbook_snap VALUES (?,?,?,?)", ob)
    conn.commit()
    conn.close()
    return db


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("tick")
    db = _tiny_db(tmp)
    out = tmp / "out"
    assert TI.main(db, out_dir=out) == 0
    return json.loads((out / "tick_instrument.json").read_text(encoding="utf-8")), out


def test_runner_blocks_the_holdout(report):
    rep, _ = report
    assert rep["holdout"]["n_rows_dropped"] == 2


def test_runner_rows_match_the_manifest_exactly(report):
    rep, _ = report
    assert rep["intensity"]
    for row in rep["intensity"]:
        assert set(row) == set(TI.REPORTED_FIELDS), row


def test_runner_states_its_measurement_conditions(report):
    """조건 없는 숫자는 이 모듈에서 나가지 않는다."""
    rep, _ = report
    mc = rep["measurement_conditions"]
    for k in ("windows_s", "side_rule", "quote_max_gap_s",
              "min_trades_per_symbol_day", "intensity_primary"):
        assert k in mc


def test_runner_reports_every_requested_window(report):
    rep, _ = report
    assert {r["window_s"] for r in rep["intensity"]} == set(TI.WINDOWS_S)


def test_runner_counts_unclassified_trades(report):
    rep, _ = report
    assert 0.0 <= rep["unclassified_share"] <= 1.0


def test_runner_refuses_to_state_a_verdict(report):
    rep, _ = report
    assert "does not" in rep["no_verdict_note"]

"""집행 현실성 측정식 테스트 — tossmon/analysis/execution.py (docs/18)."""
from __future__ import annotations

import json
import math

import pytest

from tossmon.analysis import execution as X

MICRO = X.MICRO


def depth(bids, asks):
    """(price_usd, shares) 목록 → depth_json 문자열."""
    def side(rows):
        return [{"price_u": int(p * MICRO), "qty_u": int(q * MICRO)} for p, q in rows]
    return json.dumps({"bids": side(bids), "asks": side(asks)})


# --------------------------------------------------------------------------- #
# 파싱
# --------------------------------------------------------------------------- #
def test_parse_depth_sorts_bids_desc_and_asks_asc():
    j = depth([(1.00, 100), (1.02, 50)], [(1.10, 30), (1.05, 20)])
    bids, asks = X.parse_depth(j)
    assert [p for p, _ in bids] == [1_020_000, 1_000_000]
    assert [p for p, _ in asks] == [1_050_000, 1_100_000]


@pytest.mark.parametrize("bad", [None, "", "not json", "{}", '{"bids":[{"x":1}]}'])
def test_parse_depth_is_total_on_bad_input(bad):
    assert X.parse_depth(bad) == ([], [])


def test_parse_depth_drops_nonpositive_levels():
    j = json.dumps({"bids": [{"price_u": 0, "qty_u": 5}, {"price_u": 10, "qty_u": 0},
                             {"price_u": 100, "qty_u": 7}], "asks": []})
    bids, _ = X.parse_depth(j)
    assert bids == [(100, 7)]


# --------------------------------------------------------------------------- #
# 스프레드
# --------------------------------------------------------------------------- #
def test_relative_spread_basic():
    # bid 0.99 / ask 1.01 -> mid 1.00, spread 0.02 -> 2%
    assert X.relative_spread(990_000, 1_010_000) == pytest.approx(0.02)


@pytest.mark.parametrize("b,a", [(0, 100), (100, 0), (None, 100), (200, 100)])
def test_relative_spread_nan_on_invalid(b, a):
    assert math.isnan(X.relative_spread(b, a))


# --------------------------------------------------------------------------- #
# 호가 걷기
# --------------------------------------------------------------------------- #
def test_walk_book_single_level_when_it_covers_the_order():
    _b, asks = X.parse_depth(depth([], [(1.00, 1000)]))
    w = X.walk_book(asks, 500)
    assert w["levels_used"] == 1 and not w["exhausted"]
    assert w["avg_px_u"] == pytest.approx(1_000_000)
    assert w["shares"] == pytest.approx(500)


def test_walk_book_climbs_levels_and_averages_up():
    # $100 at $1.00 (100 shares) then $2.00 level
    _b, asks = X.parse_depth(depth([], [(1.00, 100), (2.00, 1000)]))
    w = X.walk_book(asks, 300)          # $100 at 1.00, $200 at 2.00
    assert w["levels_used"] == 2 and not w["exhausted"]
    assert w["shares"] == pytest.approx(200)          # 100 + 100
    assert w["avg_px_u"] == pytest.approx(1_500_000)  # 300/200


def test_walk_book_marks_exhausted_and_does_not_invent_fills():
    _b, asks = X.parse_depth(depth([], [(1.00, 10)]))   # only $10 available
    w = X.walk_book(asks, 500)
    assert w["exhausted"] is True
    assert w["filled_usd"] == pytest.approx(10.0)
    assert w["shares"] == pytest.approx(10)


def test_walk_book_respects_max_levels():
    _b, asks = X.parse_depth(depth([], [(1.0 + i / 100, 1) for i in range(10)]))
    w = X.walk_book(asks, 1000, max_levels=2)
    assert w["levels_used"] == 2 and w["exhausted"] is True


def test_walk_book_empty_or_zero_target():
    assert math.isnan(X.walk_book([], 100)["avg_px_u"])
    assert math.isnan(X.walk_book([(1, 1)], 0)["avg_px_u"])


def test_walk_book_uses_python_ints_no_overflow():
    """가격×수량이 int64 를 넘어도 정확해야 한다 (구 VWAP 결함의 회귀)."""
    big = [(10**8 * MICRO, 10**8 * MICRO)]
    w = X.walk_book(big, 1e9)
    assert w["avg_px_u"] == pytest.approx(10**8 * MICRO, rel=1e-9)


# --------------------------------------------------------------------------- #
# 편도 / 왕복 비용
# --------------------------------------------------------------------------- #
def test_cross_cost_buy_equals_half_spread_for_tiny_order():
    bids, asks = X.parse_depth(depth([(0.98, 1000)], [(1.02, 1000)]))
    m = X.mid_u(bids[0][0], asks[0][0])
    c = X.cross_cost(asks, m, 10, side="buy")
    # avg_px uses floor division on micro-units, so allow that rounding
    assert c["cost"] == pytest.approx(0.02, rel=1e-4)   # (1.02-1.00)/1.00


def test_cross_cost_sell_is_positive_cost():
    bids, asks = X.parse_depth(depth([(0.98, 1000)], [(1.02, 1000)]))
    m = X.mid_u(bids[0][0], asks[0][0])
    assert X.cross_cost(bids, m, 10, side="sell")["cost"] == pytest.approx(0.02, rel=1e-4)


def test_cross_cost_rejects_bad_side():
    with pytest.raises(ValueError):
        X.cross_cost([(1, 1)], 1.0, 1, side="hold")


def test_round_trip_cross_is_full_spread_plus_commission():
    bids, asks = X.parse_depth(depth([(0.98, 10000)], [(1.02, 10000)]))
    r = X.round_trip_cost(bids, asks, 100, exit_mode="cross", commission=0.002)
    assert r["total"] == pytest.approx(0.02 + 0.02 + 0.002, rel=1e-4)


def test_round_trip_passive_exit_earns_the_half_spread():
    bids, asks = X.parse_depth(depth([(0.98, 10000)], [(1.02, 10000)]))
    r = X.round_trip_cost(bids, asks, 100, exit_mode="passive", commission=0.002)
    # entry +2%, exit -2% (earns half spread on the other side), fee 0.2%
    assert r["exit"] == pytest.approx(-0.02)
    assert r["total"] == pytest.approx(0.002, rel=1e-3)


def test_round_trip_mid_exit_is_entry_plus_commission():
    bids, asks = X.parse_depth(depth([(0.98, 10000)], [(1.02, 10000)]))
    r = X.round_trip_cost(bids, asks, 100, exit_mode="mid", commission=0.002)
    assert r["total"] == pytest.approx(0.02 + 0.002, rel=1e-4)


def test_round_trip_cost_grows_with_size_when_book_is_thin():
    bids, asks = X.parse_depth(depth([(0.98, 100), (0.90, 10000)],
                                     [(1.02, 100), (1.10, 10000)]))
    small = X.round_trip_cost(bids, asks, 50, exit_mode="cross")
    big = X.round_trip_cost(bids, asks, 5000, exit_mode="cross")
    assert big["total"] > small["total"]
    assert big["entry_levels"] > small["entry_levels"]


def test_round_trip_nan_when_book_missing():
    r = X.round_trip_cost([], [], 100)
    assert math.isnan(r["total"])


def test_round_trip_rejects_bad_exit_mode():
    bids, asks = X.parse_depth(depth([(1, 10)], [(2, 10)]))
    with pytest.raises(ValueError):
        X.round_trip_cost(bids, asks, 10, exit_mode="teleport")


# --------------------------------------------------------------------------- #
# 규모 / 밴드 / 요약
# --------------------------------------------------------------------------- #
def test_top_of_book_usd_uses_the_thinner_side():
    t = X.top_of_book_usd(1_000_000, 100 * MICRO, 1_010_000, 10 * MICRO)
    assert t["bid_usd"] == pytest.approx(100.0)
    assert t["ask_usd"] == pytest.approx(10.1)
    assert t["min_usd"] == pytest.approx(10.1)


def test_depth_to_usd_sums_levels():
    _b, asks = X.parse_depth(depth([], [(1.0, 100), (2.0, 100)]))
    assert X.depth_to_usd(asks) == pytest.approx(300.0)
    assert math.isnan(X.depth_to_usd([]))


@pytest.mark.parametrize("px,band", [(0.25, "$0.10-0.50"), (0.75, "$0.50-1"),
                                     (1.5, "$1-2"), (3.0, "$2-5"),
                                     (10.0, "$5-20"), (50.0, "$20+")])
def test_price_band(px, band):
    assert X.price_band(px) == band


def test_price_band_unknown_on_invalid():
    assert X.price_band(float("nan")) == "unknown"
    assert X.price_band(0) == "unknown"


def test_summarize_reports_zero_rather_than_faking_a_value():
    s = X.summarize([float("nan"), float("nan")])
    assert s["n"] == 0 and math.isnan(s["median"])
    s2 = X.summarize([1, 2, 3, 4])
    assert s2["n"] == 4 and s2["median"] == pytest.approx(2.5)


def test_round_trip_return_shape_is_stable_when_book_missing():
    """호출부가 분기하지 않도록 결측 경로도 키 구성이 같아야 한다."""
    ok = X.round_trip_cost(*X.parse_depth(depth([(1, 10)], [(2, 10)])), 10)
    missing = X.round_trip_cost([], [], 10)
    assert set(ok) == set(missing)


# --------------------------------------------------------------------------- #
# 감사 4차 회귀 — B1 / B2 / C1 / A3
# --------------------------------------------------------------------------- #
def test_b1_crossed_book_is_rejected_like_relative_spread_does():
    """ask < bid 인 크로스 호가는 두 함수가 **같이** 막아야 한다."""
    bids, asks = X.parse_depth(depth([(1.10, 100)], [(0.90, 100)]))
    assert math.isnan(X.relative_spread(bids[0][0], asks[0][0]))
    r = X.round_trip_cost(bids, asks, 50)
    assert math.isnan(r["total"])          # 음의 진입비용이 집계에 섞이면 안 된다


def test_b1_locked_book_is_still_allowed():
    """ask == bid (락)은 스프레드 0 이며 유효하다 — 과잉 차단하지 않는다."""
    bids, asks = X.parse_depth(depth([(1.00, 100)], [(1.00, 100)]))
    assert X.round_trip_cost(bids, asks, 50)["total"] == pytest.approx(0.002)


def test_b2_exit_size_awareness_is_reported():
    bids, asks = X.parse_depth(depth([(0.98, 100)], [(1.02, 100)]))
    assert X.round_trip_cost(bids, asks, 50, exit_mode="cross")["exit_size_aware"]
    assert not X.round_trip_cost(bids, asks, 50, exit_mode="mid")["exit_size_aware"]
    assert not X.round_trip_cost(bids, asks, 50, exit_mode="passive")["exit_size_aware"]


def test_c1_breakeven_is_c_over_one_minus_c_not_identity():
    assert X.breakeven_pct(0.0322) == pytest.approx(0.0322 / 0.9678)
    assert X.breakeven_pct(0.0322) > 0.0322            # 항등함수가 아니다
    assert X.breakeven_pct(0.0694) - 0.0694 == pytest.approx(0.0052, abs=1e-4)
    assert math.isnan(X.breakeven_pct(float("nan")))
    assert math.isnan(X.breakeven_pct(1.0))            # 비용 100% 는 회수 불가


def test_a3_size_cost_curve_reports_unmeasurable_rather_than_a_constant():
    """1레벨짜리 얇은 호가창에서는 크기 효과가 **측정 불가**임을 드러내야 한다."""
    thin = [{"depth_json": depth([(0.98, 40)], [(1.02, 40)])}]   # ask 측 약 $40
    c = X.size_cost_curve(thin, notionals=(100, 1000))
    assert (c["n_usable"] == 0).all()
    assert (~c["measurable"]).all()
    assert c["median_round_trip"].isna().all()          # 상수 곡선을 지어내지 않는다


def test_a3_size_cost_curve_measures_when_the_book_is_deep_enough():
    deep = [{"depth_json": depth([(0.98, 100000)], [(1.02, 100000)])}]
    c = X.size_cost_curve(deep, notionals=(100, 1000))
    assert (c["n_usable"] == 1).all() and c["measurable"].all()
    assert c["median_round_trip"].notna().all()

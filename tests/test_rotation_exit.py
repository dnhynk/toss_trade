"""랭킹 기반 동적 이탈 신호 테스트 — tossmon/analysis/rotation_exit.py (docs/22)."""
from __future__ import annotations

import math

import pandas as pd
import pytest

from tossmon.analysis import rotation_exit as RX

MIN_MS = RX.MIN_MS
U = 1_000_000
S = 1000                      # 1 second in ms


def ranks(rows, ranking_type="TOSS"):
    """(snap_s, symbol, rank, amount) → rankings_snap 모양 프레임."""
    return pd.DataFrame({
        "snap_ms": [r[0] * S for r in rows],
        "ranking_type": [ranking_type] * len(rows),
        "symbol": [r[1] for r in rows],
        "rank": [r[2] for r in rows],
        "amount_u": [r[3] for r in rows],
    })


def path(rows, *, start_min=0):
    return pd.DataFrame({
        "ts_ms": [(start_min + i) * MIN_MS for i in range(len(rows))],
        "high_u": [int(h * U) for h, _l, _c in rows],
        "low_u": [int(l * U) for _h, l, _c in rows],
        "close_u": [int(c * U) for _h, _l, c in rows],
    })


# --------------------------------------------------------------------------- #
# 랭킹 시계열 — 결측 규율
# --------------------------------------------------------------------------- #
def test_rank_series_leaves_absence_as_nan_by_default():
    rk = ranks([(0, "A", 5, 100), (0, "B", 6, 90), (60, "B", 5, 90)])
    s = RX.rank_series(rk, "A", "TOSS")
    assert s.iloc[0] == 5.0
    assert math.isnan(s.iloc[1])          # 랭킹 밖 = 결측, 값으로 채우지 않는다


def test_rank_series_can_treat_absence_as_worst_when_asked_explicitly():
    rk = ranks([(0, "A", 5, 100), (60, "B", 5, 90)])
    s = RX.rank_series(rk, "A", "TOSS", absent_as_worst=True)
    assert s.iloc[1] == float(RX.RANK_ABSENT)


def test_rank_series_empty_on_unknown_type():
    assert RX.rank_series(ranks([(0, "A", 1, 1)]), "A", "NOPE").empty


# --------------------------------------------------------------------------- #
# 순위 속도 (사용자 직관 3)
# --------------------------------------------------------------------------- #
def test_rank_drop_speed_positive_when_rank_worsens():
    rk = ranks([(0, "A", 5, 100), (60, "A", 11, 100)])       # 1분에 6계단 하락
    s = RX.rank_series(rk, "A", "TOSS")
    assert RX.rank_drop_speed(s, 60 * S, window_s=300) == pytest.approx(6.0)


def test_rank_rise_speed_is_the_mirror_of_drop_speed():
    rk = ranks([(0, "A", 11, 100), (60, "A", 5, 100)])
    s = RX.rank_series(rk, "A", "TOSS")
    assert RX.rank_rise_speed(s, 60 * S, window_s=300) == pytest.approx(6.0)
    assert RX.rank_drop_speed(s, 60 * S, window_s=300) == pytest.approx(-6.0)


def test_rank_speed_nan_with_a_single_observation():
    rk = ranks([(0, "A", 5, 100)])
    assert math.isnan(RX.rank_drop_speed(RX.rank_series(rk, "A", "TOSS"), 0))


def test_rank_speed_uses_only_the_past():
    rk = ranks([(0, "A", 5, 100), (60, "A", 5, 100), (120, "A", 50, 100)])
    s = RX.rank_series(rk, "A", "TOSS")
    # t=60s 시점에서는 아직 급락(120s)이 보이면 안 된다
    assert RX.rank_drop_speed(s, 60 * S, window_s=300) == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# 유동성 점유율 (사용자 직관 4)
# --------------------------------------------------------------------------- #
def test_amount_share_is_the_cohort_fraction():
    rk = ranks([(0, "A", 1, 300), (0, "B", 2, 700)])
    s = RX.amount_share_series(rk, "A", "TOSS")
    assert s.iloc[0] == pytest.approx(0.30)


def test_share_decline_below_one_when_liquidity_leaves():
    rk = ranks([(0, "A", 1, 500), (0, "B", 2, 500),
                (60, "A", 1, 100), (60, "B", 2, 900)])
    s = RX.amount_share_series(rk, "A", "TOSS")
    assert RX.share_decline(s, 60 * S, 0) == pytest.approx(0.20)


def test_share_gain_above_one_when_liquidity_arrives():
    rk = ranks([(0, "A", 1, 100), (0, "B", 2, 900),
                (60, "A", 1, 500), (60, "B", 2, 500)])
    s = RX.amount_share_series(rk, "A", "TOSS")
    assert RX.share_gain(s, 60 * S, window_s=300) == pytest.approx(5.0)


def test_share_helpers_nan_on_empty():
    e = pd.Series(dtype="float64")
    assert math.isnan(RX.share_decline(e, 0, 0))
    assert math.isnan(RX.share_gain(e, 0))


# --------------------------------------------------------------------------- #
# 토스-시장 괴리 / 인계
# --------------------------------------------------------------------------- #
def test_divergence_positive_when_toss_ranks_it_higher():
    t = RX.rank_series(ranks([(0, "A", 3, 1)], "T"), "A", "T")
    m = RX.rank_series(ranks([(0, "A", 30, 1)], "M"), "A", "M")
    assert RX.toss_market_divergence(t, m, 0) == pytest.approx(27.0)


def test_divergence_release_speed_positive_when_crowd_leaves():
    t = RX.rank_series(ranks([(0, "A", 3, 1), (300, "A", 25, 1)], "T"), "A", "T")
    m = RX.rank_series(ranks([(0, "A", 30, 1), (300, "A", 30, 1)], "M"), "A", "M")
    # 괴리 27 -> 5, 5분간 -> 4.4/분
    assert RX.divergence_release_speed(t, m, 300 * S, window_s=300) == \
        pytest.approx(4.4, rel=1e-3)


def test_divergence_nan_when_either_side_missing():
    t = RX.rank_series(ranks([(0, "A", 3, 1)], "T"), "A", "T")
    assert math.isnan(RX.toss_market_divergence(t, pd.Series(dtype="float64"), 0))


def test_handoff_pressure_counts_new_entrants_excluding_self():
    rk = ranks([(0, "A", 1, 1), (0, "B", 2, 1),
                (300, "A", 1, 1), (300, "C", 2, 1)])
    assert RX.handoff_pressure(rk, "A", "TOSS", 300 * S, top_n=20,
                               window_s=600) == pytest.approx(1.0)


def test_handoff_pressure_nan_with_one_snapshot():
    rk = ranks([(0, "A", 1, 1)])
    assert math.isnan(RX.handoff_pressure(rk, "A", "TOSS", 0, window_s=600))


# --------------------------------------------------------------------------- #
# 신호 기반 이탈 — rules.ExitRule 과 같은 틀
# --------------------------------------------------------------------------- #
def test_signal_exit_fires_when_threshold_crossed():
    p = path([(1.0, 1.0, 1.0), (1.1, 1.0, 1.05), (1.1, 1.0, 1.02)])
    sig = pd.Series({0: 0.0, 1 * MIN_MS: 0.0, 2 * MIN_MS: 9.0})
    r = RX.simulate_signal_exit(p, 1.0 * U, 0,
                                sig, RX.SignalExitRule("x", "rank_drop_speed", 5.0))
    assert r["reason"] == "signal:rank_drop_speed"
    assert r["gross"] == pytest.approx(0.02)


def test_signal_exit_below_direction():
    p = path([(1.0, 1.0, 1.0), (1.1, 1.0, 1.05)])
    sig = pd.Series({0: 1.0, 1 * MIN_MS: 0.3})
    r = RX.simulate_signal_exit(p, 1.0 * U, 0,
                                sig, RX.SignalExitRule("x", "share_decline", 0.5,
                                                       direction="below"))
    assert r["reason"] == "signal:share_decline"


def test_signal_exit_never_uses_a_future_signal_value():
    p = path([(1.0, 1.0, 1.0), (1.0, 1.0, 1.0)])
    sig = pd.Series({5 * MIN_MS: 99.0})        # 지평 밖 미래에만 신호
    r = RX.simulate_signal_exit(p, 1.0 * U, 0,
                                sig, RX.SignalExitRule("x", "s", 1.0, horizon_min=1))
    assert r["reason"] == "horizon"
    assert r["signal_defined"] is False


def test_signal_exit_falls_back_when_signal_never_defined():
    p = path([(1.50, 1.00, 1.45), (1.45, 1.10, 1.15)])
    r = RX.simulate_signal_exit(p, 1.0 * U, 0, pd.Series(dtype="float64"),
                                RX.SignalExitRule("x", "s", 1.0, fallback_trail=0.20))
    assert r["reason"].startswith("fallback_trail")
    assert r["signal_defined"] is False


def test_signal_exit_returns_the_same_shape_as_rules_simulate_exit():
    from tossmon.analysis.rules import ExitRule, simulate_exit
    p = path([(1.0, 1.0, 1.0), (1.1, 1.0, 1.05)])
    a = simulate_exit(p, 1.0 * U, 0, ExitRule("t", horizon_min=60))
    b = RX.simulate_signal_exit(p, 1.0 * U, 0, pd.Series({0: 0.0}),
                                RX.SignalExitRule("x", "s", 99.0))
    assert set(a).issubset(set(b))          # 같은 집계에 그대로 들어간다


def test_signal_exit_nan_on_empty_path():
    r = RX.simulate_signal_exit(pd.DataFrame(columns=["ts_ms", "high_u", "low_u",
                                                      "close_u"]),
                                1.0 * U, 0, pd.Series(dtype="float64"),
                                RX.SignalExitRule("x", "s", 1.0))
    assert math.isnan(r["gross"]) and r["reason"] == "no_path"


# --------------------------------------------------------------------------- #
# 필요 표본 추정
# --------------------------------------------------------------------------- #
def test_required_days_scales_with_variance():
    a = RX.required_days(0.10, 0.02, 8.0)
    b = RX.required_days(0.20, 0.02, 8.0)
    assert b["n_needed"] > a["n_needed"]
    assert b["days_needed"] >= a["days_needed"]


def test_required_days_respects_the_frozen_min_n_floor():
    r = RX.required_days(0.001, 0.05, 8.0, min_n=30)
    assert r["n_needed"] == 30              # 통계 요구량이 작아도 최소 표본이 바닥
    assert r["days_needed"] == 4            # ceil(30/8)


def test_required_days_rejects_invalid_inputs():
    assert RX.required_days(0.0, 0.02, 8.0)["days_needed"] is None
    assert RX.required_days(0.1, 0.02, 0.0)["days_needed"] is None


# --------------------------------------------------------------------------- #
# 계약 C-2 개정 — amount_u 는 마이크로원(KRW). 비율로만 쓰는 것을 강제한다.
# --------------------------------------------------------------------------- #
def test_amount_share_is_invariant_to_the_currency_scale():
    """환율이 분자·분모에 공통이라 약분된다 — KRW 표기가 점유율을 오염시키지 않는다.

    전 종목 `amount_u` 에 임의 상수(환율)를 곱해도 점유율이 그대로여야 한다.
    이 성질이 깨지면 amount_u 를 금액으로 읽고 있다는 뜻이다.
    """
    base = ranks([(0, "A", 1, 300), (0, "B", 2, 700)])
    scaled = base.copy()
    scaled["amount_u"] = scaled["amount_u"] * 1440        # USD -> KRW 환산과 동형
    a = RX.amount_share_series(base, "A", "TOSS")
    b = RX.amount_share_series(scaled, "A", "TOSS")
    assert a.iloc[0] == pytest.approx(b.iloc[0])
    assert a.iloc[0] == pytest.approx(0.30)


def test_share_decline_is_also_currency_scale_invariant():
    rows = [(0, "A", 1, 500), (0, "B", 2, 500),
            (60, "A", 1, 100), (60, "B", 2, 900)]
    base = ranks(rows)
    scaled = base.copy()
    scaled["amount_u"] = scaled["amount_u"] * 1440
    a = RX.share_decline(RX.amount_share_series(base, "A", "TOSS"), 60 * S, 0)
    b = RX.share_decline(RX.amount_share_series(scaled, "A", "TOSS"), 60 * S, 0)
    assert a == pytest.approx(b)


def test_module_does_not_expose_any_dollar_reading_of_amount_u():
    """금액 해석 헬퍼가 없어야 한다 — 있으면 누군가 KRW 를 달러로 읽게 된다."""
    import tossmon.analysis.rotation_exit as m
    assert not [n for n in dir(m) if "usd" in n.lower() or "dollar" in n.lower()]

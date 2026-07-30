"""전조 피처 검증 — 소유: W3.

이 파일의 최우선 목적은 **룩어헤드 부재의 증명**이다 (계약 A1 §1).
"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from tests import synth
from tossmon.analysis import baselines as B
from tossmon.analysis import features as F


@pytest.fixture(scope="module")
def coil() -> tuple[pd.DataFrame, dict, pd.Series, dict]:
    df, truth = synth.make_scenario("coil_pop", seed=7)
    curve = B.minute_of_session_volume_curve(df, truth["baseline_calendar"])
    base = B.compute_daily_baseline(truth["df_1d"])
    return df, truth, curve, base


def _extract(df, truth, curve, base, t0, **kw):
    return F.extract_precursor_features(
        df, truth["rankings"], t0, curve=curve, calendar=truth["calendar"],
        baseline=base, shares_outstanding_qu=truth["shares_outstanding_qu"],
        symbol=truth["symbol"], **kw)


# --------------------------------------------------------------------------- #
# 룩어헤드 부재 증명
# --------------------------------------------------------------------------- #
def test_no_lookahead_future_bars_are_ignored(coil) -> None:
    """T0 이후 봉을 넣든 빼든 결과 dict 이 **완전히 동일**해야 한다."""
    df, truth, curve, base = coil
    t0 = truth["t0_expected_ms"]
    full = _extract(df, truth, curve, base, t0)
    truncated_df = df[df["ts_ms"] < t0]
    trunc = _extract(truncated_df, truth, curve, base, t0)
    assert full.keys() == trunc.keys()
    for k in full:
        a, b = full[k], trunc[k]
        assert (a == b) or (a != a and b != b), f"{k}: {a} != {b} (룩어헤드!)"


def test_no_lookahead_future_bars_can_be_corrupted(coil) -> None:
    """T0 이후 데이터를 극단적으로 변조해도 피처가 흔들리지 않아야 한다."""
    df, truth, curve, base = coil
    t0 = truth["t0_expected_ms"]
    poisoned = df.copy()
    fut = poisoned["ts_ms"] >= t0
    for col, mult in (("close_u", 50), ("high_u", 50), ("low_u", 50), ("open_u", 50),
                      ("vol_qu", 1000)):
        poisoned.loc[fut, col] = poisoned.loc[fut, col] * mult
    a = _extract(df, truth, curve, base, t0)
    b = _extract(poisoned, truth, curve, base, t0)
    for k in a:
        x, y = a[k], b[k]
        assert (x == y) or (x != x and y != y), f"{k} 가 미래 데이터에 반응했다"


def test_no_lookahead_in_rankings(coil) -> None:
    """T0 이후 랭킹 스냅샷을 변조해도 토스 쏠림도 피처가 변하지 않아야 한다."""
    df, truth, curve, base = coil
    t0 = truth["t0_expected_ms"]
    rk = truth["rankings"].copy()
    fut = rk["snap_ms"] >= t0
    rk.loc[fut, "amount_u"] = rk.loc[fut, "amount_u"] * 999
    rk.loc[fut, "rank"] = 1
    a = F.extract_precursor_features(df, truth["rankings"], t0, curve=curve,
                                     calendar=truth["calendar"], symbol=truth["symbol"])
    b = F.extract_precursor_features(df, rk, t0, curve=curve,
                                     calendar=truth["calendar"], symbol=truth["symbol"])
    for k in ("toss_share", "toss_share_max", "toss_share_slope_30", "toss_rank_best",
              "market_rank_best", "minutes_since_toss_entry", "ranking_snaps_pre"):
        x, y = a[k], b[k]
        assert (x == y) or (x != x and y != y), f"{k} 가 미래 랭킹에 반응했다"


def test_cutoff_is_strictly_before_t0(coil) -> None:
    df, truth, curve, base = coil
    t0 = truth["t0_expected_ms"]
    f = _extract(df, truth, curve, base, t0)
    assert f["cutoff_ms"] < t0
    assert f["cutoff_lag_min"] >= 1


def test_include_t0_admits_the_t0_bar_only(coil) -> None:
    """A1 §1: include_t0=True 는 T0 봉까지만 포함한다 (그 이후는 여전히 금지)."""
    df, truth, curve, base = coil
    t0 = truth["t0_expected_ms"]
    strict = _extract(df, truth, curve, base, t0)
    live = _extract(df, truth, curve, base, t0, include_t0=True)
    assert live["cutoff_ms"] == float(t0)
    assert strict["cutoff_ms"] < float(t0)
    assert live["n_bars_pre"] == strict["n_bars_pre"] + 1
    assert live["include_t0"] == 1.0 and strict["include_t0"] == 0.0

    # T0 봉 '이후'는 include_t0=True 에서도 반영되지 않아야 한다
    poisoned = df.copy()
    fut = poisoned["ts_ms"] > t0
    poisoned.loc[fut, "vol_qu"] = poisoned.loc[fut, "vol_qu"] * 500
    live2 = _extract(poisoned, truth, curve, base, t0, include_t0=True)
    for k in live:
        x, y = live[k], live2[k]
        assert (x == y) or (x != x and y != y), f"{k} 가 T0 이후 데이터에 반응했다"


def test_cut_frame_boundaries() -> None:
    df = pd.DataFrame({"ts_ms": [10, 20, 30], "v": [1, 2, 3]})
    assert list(F.cut_frame(df, 20)["ts_ms"]) == [10]
    assert list(F.cut_frame(df, 20, include_t0=True)["ts_ms"]) == [10, 20]
    assert F.cut_frame(None, 20).empty
    assert F.cut_frame(df.iloc[0:0], 20).empty


# --------------------------------------------------------------------------- #
# 키 계약
# --------------------------------------------------------------------------- #
def test_key_set_is_stable_regardless_of_availability(coil) -> None:
    """입력이 부실해도 키 집합은 동일해야 한다 (하류가 키 존재를 가정한다)."""
    df, truth, curve, base = coil
    t0 = truth["t0_expected_ms"]
    rich = _extract(df, truth, curve, base, t0)
    poor = F.extract_precursor_features(df, pd.DataFrame(), t0)
    empty = F.extract_precursor_features(df.iloc[0:0], pd.DataFrame(), t0)
    expected = set(F.feature_names())
    assert set(rich) == expected
    assert set(poor) == expected
    assert set(empty) == expected
    assert all(isinstance(v, float) for v in rich.values())


def test_windows_min_changes_key_set() -> None:
    df, truth = synth.make_scenario("coil_pop", seed=1, history_days=1)
    f = F.extract_precursor_features(df, truth["rankings"],
                                     truth["t0_expected_ms"], windows_min=(10, 20))
    assert "vol_z_10" in f and "vol_z_20" in f
    assert "vol_z_5" not in f
    assert set(f) == set(F.feature_names((10, 20)))


def test_empty_input_returns_all_nan_but_metadata(coil) -> None:
    df, truth, curve, base = coil
    t0 = truth["t0_expected_ms"]
    f = F.extract_precursor_features(df.iloc[0:0], pd.DataFrame(), t0)
    assert f["t0_ms"] == float(t0)
    assert f["n_bars_pre"] == 0.0
    assert math.isnan(f["close_cut_u"])


# --------------------------------------------------------------------------- #
# 피처 의미 검증
# --------------------------------------------------------------------------- #
def test_coil_pop_shows_volume_precursor_but_instant_does_not() -> None:
    """coil→폭발형은 T0 이전에 이미 RVOL 이 높고, 즉발형은 정상이다."""
    out = {}
    for kind in ("coil_pop", "instant", "noise"):
        df, truth = synth.make_scenario(kind, seed=3)
        curve = B.minute_of_session_volume_curve(df, truth["baseline_calendar"])
        t0 = truth["t0_expected_ms"] or (truth["market_day"].regular.start_ms
                                         + 200 * 60_000)
        out[kind] = F.extract_precursor_features(
            df, truth["rankings"], t0, curve=curve, calendar=truth["calendar"],
            symbol=truth["symbol"])
    assert out["coil_pop"]["rvol_at_cutoff"] > 3.0
    assert out["instant"]["rvol_at_cutoff"] < 3.0, "즉발형에 전조가 있으면 시나리오가 틀렸다"
    assert out["noise"]["rvol_at_cutoff"] < 3.0
    assert out["coil_pop"]["vol_z_15"] > out["noise"]["vol_z_15"]


def test_rvol_cross_lead_is_within_the_t0_session() -> None:
    """누적 RVOL 리드타임은 T0 세션 안에서만 측정된다 (세션 상대량이므로)."""
    df, truth = synth.make_scenario("coil_pop", seed=3)
    curve = B.minute_of_session_volume_curve(df, truth["baseline_calendar"])
    t0 = truth["t0_expected_ms"]
    f = F.extract_precursor_features(df, truth["rankings"], t0, curve=curve,
                                     calendar=truth["calendar"], symbol=truth["symbol"])
    reg = truth["market_day"].regular
    max_lead = (t0 - reg.start_ms) // 60_000
    lead = f["rvol_first_cross_3_lead_min"]
    assert lead == lead
    assert 0 <= lead <= max_lead


def test_toss_concentration_features(coil) -> None:
    df, truth, curve, base = coil
    f = _extract(df, truth, curve, base, truth["t0_expected_ms"])
    assert f["toss_in_ranking"] == 1.0
    assert f["ranking_snaps_pre"] > 0
    assert 0 < f["toss_share"] < 1
    assert f["toss_share_slope_30"] > 0, "coil_pop 은 쏠림도가 상승하는 시나리오"
    assert f["minutes_since_toss_entry"] >= 0


def test_no_toss_ranking_when_symbol_absent() -> None:
    df, truth = synth.make_scenario("noise", seed=3)
    t0 = truth["market_day"].regular.start_ms + 100 * 60_000
    f = F.extract_precursor_features(df, truth["rankings"], t0, symbol=truth["symbol"])
    assert f["toss_in_ranking"] == 0.0
    assert math.isnan(f["toss_share"])


def test_price_trajectory_features_are_sane(coil) -> None:
    df, truth, curve, base = coil
    f = _extract(df, truth, curve, base, truth["t0_expected_ms"])
    assert f["ret_60"] > f["ret_5"] > 0, "폭발 구간이면 장기 윈도우 수익률이 더 크다"
    assert 0 < f["range_pct_60"] < 5
    assert -1 < f["dist_from_hod"] <= 0
    assert 0 <= f["up_bar_ratio_30"] <= 1
    assert f["gap_from_prev_close"] > 0


def test_print_activity_proxies_detect_dormancy() -> None:
    """A2 §3 first_print/no_print/staleness 의 캔들 프록시."""
    df, truth = synth.make_scenario("daymarket", seed=3)
    curve = B.minute_of_session_volume_curve(df, truth["baseline_calendar"])
    t0 = truth["t0_expected_ms"]
    f = F.extract_precursor_features(df, truth["rankings"], t0, curve=curve,
                                     calendar=truth["calendar"], symbol=truth["symbol"])
    assert f["no_print_ratio_60"] > 0, "얇은 데이마켓은 미체결 분이 존재한다"
    assert f["minutes_since_last_print"] >= 0
    assert f["session_print_age_min"] >= 0
    assert 0 <= f["dormant_ratio_prior_day"] <= 1


def test_history_features_use_only_past_days() -> None:
    df, truth = synth.make_scenario("coil_pop", seed=3, history_days=5)
    curve = B.minute_of_session_volume_curve(df, truth["baseline_calendar"])
    f = F.extract_precursor_features(df, truth["rankings"], truth["t0_expected_ms"],
                                     curve=curve, calendar=truth["calendar"],
                                     symbol=truth["symbol"])
    assert f["hist_days_available"] >= 5
    assert f["prior_event_count_20d"] == 0.0, "이력일은 조용해야 한다"
    assert f["former_runner"] == 0.0


def test_prior_events_override() -> None:
    df, truth = synth.make_scenario("coil_pop", seed=3, history_days=1)
    t0 = truth["t0_expected_ms"]
    prior = pd.DataFrame({"t0_ms": [t0 - 3 * 86_400_000, t0 - 10 * 86_400_000,
                                    t0 + 86_400_000]})
    f = F.extract_precursor_features(df, truth["rankings"], t0, prior_events=prior,
                                     symbol=truth["symbol"])
    assert f["prior_event_count_20d"] == 2.0, "미래 이벤트는 세지 않는다"
    assert f["former_runner"] == 1.0
    assert 2.9 < f["days_since_prior_event"] < 3.1


def test_float_rotation_pre_is_partial() -> None:
    df, truth = synth.make_scenario("coil_pop", seed=3)
    curve = B.minute_of_session_volume_curve(df, truth["baseline_calendar"])
    f = F.extract_precursor_features(
        df, truth["rankings"], truth["t0_expected_ms"], curve=curve,
        calendar=truth["calendar"], symbol=truth["symbol"],
        shares_outstanding_qu=truth["shares_outstanding_qu"])
    assert 0 < f["float_rotation_pre"] < truth["float_rotation"]


def test_missing_bars_do_not_crash_features() -> None:
    df, truth = synth.make_scenario("halt_gap", seed=3)
    thin = synth.drop_random_bars(df, frac=0.3, seed=5)
    curve = B.minute_of_session_volume_curve(thin, truth["baseline_calendar"])
    f = F.extract_precursor_features(thin, truth["rankings"], truth["t0_expected_ms"],
                                     curve=curve, calendar=truth["calendar"],
                                     symbol=truth["symbol"])
    assert set(f) == set(F.feature_names())
    assert f["no_print_ratio_30"] > 0

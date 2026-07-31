"""이벤트 라벨링 검증 — 소유: W3.

중점: 합성데이터 ground truth 재현, 결측·홀트(캔들 공백) 처리, RVOL 게이트 규약(A1 §6).
"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from tests import synth
from tossmon.analysis import baselines as B
from tossmon.analysis import labeling as L

MIN_MS = L.MIN_MS
TOSS = "TOSS_SECURITIES_TRADING_AMOUNT"


def _label(kind: str, seed: int = 1, **kw):
    """한 시나리오를 전체 파이프라인으로 라벨링 → (df, truth, 이벤트 당일 events)."""
    df, truth = synth.make_scenario(kind, seed=seed, **kw)
    curve = B.minute_of_session_volume_curve(df, truth["baseline_calendar"])
    rv = B.rvol_series(df, curve, calendar=truth["calendar"])
    events = L.detect_events(
        df, L.EventParams(), calendar=truth["calendar"], rvol_series=rv,
        prev_close_u={(truth["symbol"], truth["market_day"].date):
                                       truth["prev_close_u"]},
        shares_outstanding_qu=truth["shares_outstanding_qu"],
        rankings=truth["rankings"], ranking_type=TOSS)
    md = truth["market_day"]
    same_day = events[(events["t0_ms"] >= md.day.start_ms)
                      & (events["t0_ms"] < md.after.end_ms)]
    return df, truth, same_day.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# 스키마·계약
# --------------------------------------------------------------------------- #
def test_columns_match_contract_order() -> None:
    _df, _truth, ev = _label("coil_pop")
    assert list(ev.columns) == L.EVENT_COLUMNS
    # 계약 C-7 명시 7컬럼이 앞에 그대로 있어야 한다
    assert list(ev.columns)[:7] == ["t0_ms", "kind", "peak_ms", "peak_ret", "ret_30m",
                                    "ret_close", "session"]


def test_empty_input_returns_empty_frame_with_columns() -> None:
    out = L.detect_events(pd.DataFrame(), L.EventParams())
    assert list(out.columns) == L.EVENT_COLUMNS
    assert out.empty


def test_default_event_params_match_docs() -> None:
    p = L.EventParams()
    assert (p.window_min, p.ret_min, p.day_ret_min, p.rvol_min) == (30, 0.15, 0.30, 3.0)


# --------------------------------------------------------------------------- #
# ground truth 재현
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", [k for k in synth.SCENARIO_KINDS if k != "noise"])
def test_t0_reproduces_ground_truth(kind: str) -> None:
    """검출 T0 가 합성데이터의 독립 산출 T0 와 tolerance 내에서 일치."""
    _df, truth, ev = _label(kind, seed=1)
    assert len(ev) == 1, f"{kind}: 매매일당 1건이어야 한다"
    got = int(ev.iloc[0]["t0_ms"])
    exp = truth["t0_expected_ms"]
    assert abs(got - exp) <= truth["t0_tolerance_min"] * MIN_MS, \
        f"{kind}: T0 오차 {(got - exp) // MIN_MS}분"


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_noise_is_not_detected(seed: int) -> None:
    _df, _truth, ev = _label("noise", seed=seed)
    assert ev.empty, "노이즈 시나리오 검출 = 오탐"


@pytest.mark.parametrize("kind", ["coil_pop", "fade", "dump", "daymarket"])
def test_session_label_matches_truth(kind: str) -> None:
    _df, truth, ev = _label(kind)
    assert ev.iloc[0]["session"] == truth["session_expected"]


@pytest.mark.parametrize("kind", ["coil_pop", "fade", "dump", "instant"])
def test_peak_and_returns_match_truth(kind: str) -> None:
    _df, truth, ev = _label(kind)
    r = ev.iloc[0]
    assert int(r["hod_ms"]) == truth["hod_ms"], "HOD 시각은 독립 계산과 정확히 일치"
    assert r["peak_ret"] == pytest.approx(truth["peak_ret_from_t0"], abs=0.02)
    assert r["ret_close"] == pytest.approx(truth["ret_close_from_t0"], abs=0.02)
    if truth["ret_30m_from_t0"] is not None:
        assert r["ret_30m"] == pytest.approx(truth["ret_30m_from_t0"], abs=0.02)


@pytest.mark.parametrize("kind", ["coil_pop", "fade", "dump"])
def test_vwap_and_float_rotation_match_truth(kind: str) -> None:
    _df, truth, ev = _label(kind)
    r = ev.iloc[0]
    assert bool(r["closed_below_vwap"]) is truth["closed_below_vwap"]
    assert r["vwap_close_rel"] == pytest.approx(truth["vwap_close_rel"], abs=1e-6)
    assert r["float_rotation"] == pytest.approx(truth["float_rotation"], rel=1e-9)


def test_next_day_gap_matches_truth() -> None:
    _df, truth, ev = _label("dump")
    assert ev.iloc[0]["next_day_gap"] == pytest.approx(truth["next_day_gap"], abs=1e-9)


def test_ranking_lead_lag_matches_configured_value() -> None:
    _df, truth, ev = _label("coil_pop")
    r = ev.iloc[0]
    assert int(r["ranking_first_entry_ms"]) == truth["ranking_first_entry_ms"][TOSS]
    expected = truth["ranking_lead_lag_min"][TOSS]
    assert r["ranking_lead_lag_min"] == pytest.approx(expected, abs=1.0)
    assert r["ranking_lead_lag_min"] < 0, "coil_pop 은 랭킹 선행 케이스"


def test_kind_reflects_trigger_type() -> None:
    """A1 §3: kind ∈ {win, day, both}."""
    kinds = set()
    for k in ("coil_pop", "instant", "fade", "dump", "daymarket"):
        _df, _t, ev = _label(k)
        kinds.add(ev.iloc[0]["kind"])
    assert kinds <= {"win", "day", "both"}
    _df, _t, ev = _label("fade")
    assert ev.iloc[0]["kind"] in ("day", "both"), "프리마켓 갭업은 당일 조건이 먼저 걸린다"


def test_shape_and_outcome_classification() -> None:
    _df, _t, coil = _label("coil_pop")
    _df, _t, inst = _label("instant")
    _df, _t, dump = _label("dump")
    assert coil.iloc[0]["shape"] == "coil"
    assert inst.iloc[0]["shape"] == "instant"
    assert dump.iloc[0]["outcome"] == "dump"
    assert coil.iloc[0]["outcome"] == "hold"
    assert set(pd.concat([coil, inst, dump])["outcome"]) <= {"hold", "fade", "dump"}


def test_time_to_peak_and_duration() -> None:
    _df, _t, ev = _label("dump")
    r = ev.iloc[0]
    assert r["time_to_peak_min"] > 0
    assert r["duration_min"] > 0, "덤프형은 T0 종가를 다시 하회한다"
    _df, _t, hold = _label("coil_pop")
    assert math.isnan(hold.iloc[0]["duration_min"]), \
        "끝까지 T0 위에 있으면 지속시간은 우측 절단(NaN)"


def test_retrace_columns_are_relative_to_peak() -> None:
    _df, _t, ev = _label("dump")
    r = ev.iloc[0]
    assert r["retrace_close"] < 0
    assert r["retrace_close"] <= r["retrace_30m"] or math.isnan(r["retrace_30m"])
    # ret_30m 은 진입(T0) 기준, retrace_30m 은 피크 기준 — 서로 다른 수치여야 한다
    assert r["ret_30m"] != r["retrace_30m"]


def test_t0_min_from_open_sign() -> None:
    _df, _t, pre_ev = _label("fade")
    _df, _t, reg_ev = _label("coil_pop")
    assert pre_ev.iloc[0]["t0_min_from_open"] < 0, "프리마켓 T0 는 개장 전 = 음수"
    assert reg_ev.iloc[0]["t0_min_from_open"] > 0


# --------------------------------------------------------------------------- #
# 홀트 / 결측 처리
# --------------------------------------------------------------------------- #
def test_halt_gap_count_matches_synth_injection() -> None:
    _df, truth, ev = _label("halt_gap")
    injected = [n for _ts, n in truth["gaps"]["regular"] if n >= 5]
    assert int(ev.iloc[0]["halt_gap_count"]) == len(injected)


def test_dump_halt_counted() -> None:
    _df, _t, ev = _label("dump")
    assert int(ev.iloc[0]["halt_gap_count"]) >= 1


def test_rolling_window_is_time_based_not_bar_based() -> None:
    """봉 공백이 있어도 30분 윈도우는 '시간'으로 잘려야 한다."""
    base = 1_780_000_000_000 - 1_780_000_000_000 % MIN_MS
    rows = []
    # 0분: 100 → 이후 60분 공백 → 61분: 130 (+30%지만 30분 윈도우 밖)
    rows.append((base, 100, 100, 100, 100, 10))
    for m in range(61, 66):
        rows.append((base + m * MIN_MS, 129, 131, 129, 130, 10))
    df = pd.DataFrame([{"symbol": "T", "ts_ms": t, "open_u": o, "high_u": h,
                        "low_u": lo, "close_u": c, "vol_qu": v}
                       for t, o, h, lo, c, v in rows]).astype({"ts_ms": "int64"})
    # prev_close 를 높게 둬서 '당일 +30%' 조건이 끼어들지 않게 한다 (윈도우 조건만 시험)
    ev = L.detect_events(df, L.EventParams(), prev_close_u=1_000)
    assert ev.empty, "공백 건너뛴 봉이 같은 윈도우로 묶이면 시간 기준이 아니다"

    # 같은 상승을 30분 안에 두면 검출돼야 한다
    rows2 = [(base, 100, 100, 100, 100, 10)]
    for m in range(20, 25):
        rows2.append((base + m * MIN_MS, 129, 131, 129, 130, 10))
    df2 = pd.DataFrame([{"symbol": "T", "ts_ms": t, "open_u": o, "high_u": h,
                         "low_u": lo, "close_u": c, "vol_qu": v}
                        for t, o, h, lo, c, v in rows2]).astype({"ts_ms": "int64"})
    ev2 = L.detect_events(df2, L.EventParams(), prev_close_u=1_000)
    assert len(ev2) == 1
    assert int(ev2.iloc[0]["t0_ms"]) == base + 20 * MIN_MS


def test_random_missing_bars_still_detects() -> None:
    df, truth = synth.make_scenario("coil_pop", seed=4)
    thin = synth.drop_random_bars(df, frac=0.25, seed=9)
    curve = B.minute_of_session_volume_curve(thin, truth["baseline_calendar"])
    rv = B.rvol_series(thin, curve, calendar=truth["calendar"])
    ev = L.detect_events(thin, L.EventParams(), calendar=truth["calendar"],
                         rvol_series=rv, prev_close_u={(truth["symbol"], truth["market_day"].date):
                                       truth["prev_close_u"]})
    md = truth["market_day"]
    ev = ev[(ev["t0_ms"] >= md.day.start_ms) & (ev["t0_ms"] < md.after.end_ms)]
    assert len(ev) == 1
    assert abs(int(ev.iloc[0]["t0_ms"]) - truth["t0_expected_ms"]) <= 15 * MIN_MS


def test_thin_session_only_symbol_does_not_crash() -> None:
    """데이마켓만 체결된 종목(정규장 전무) — VWAP·종가 참조가 없어도 죽지 않아야 한다."""
    df, truth = synth.make_scenario("daymarket", seed=2)
    md = truth["market_day"]
    day_only = df[(df["ts_ms"] >= md.day.start_ms) & (df["ts_ms"] < md.day.end_ms)]
    ev = L.detect_events(day_only, L.EventParams(), calendar=truth["calendar"],
                         prev_close_u={(truth["symbol"], truth["market_day"].date):
                                       truth["prev_close_u"]})
    assert len(ev) >= 1
    r = ev.iloc[0]
    assert r["session"] == "day"
    assert not math.isnan(r["vwap_close_rel"]), "정규장이 없으면 매매일 전체 VWAP 로 대체"


# --------------------------------------------------------------------------- #
# RVOL 게이트 규약 (A1 §6)
# --------------------------------------------------------------------------- #
def test_ungated_path_is_flagged_not_silent() -> None:
    df, truth = synth.make_scenario("coil_pop", seed=1)
    ev = L.detect_events(df, L.EventParams(), calendar=truth["calendar"],
                         prev_close_u={(truth["symbol"], truth["market_day"].date):
                                       truth["prev_close_u"]})
    assert not ev.empty
    assert (~ev["rvol_gated"]).all()
    assert ev["rvol_at_t0"].isna().all()


def test_gated_path_records_rvol() -> None:
    _df, _t, ev = _label("coil_pop")
    assert ev.iloc[0]["rvol_gated"]
    assert ev.iloc[0]["rvol_at_t0"] >= 3.0


def test_rvol_gate_can_suppress_detection() -> None:
    """RVOL 임계를 비현실적으로 높이면 아무것도 안 잡혀야 한다."""
    df, truth = synth.make_scenario("coil_pop", seed=1)
    curve = B.minute_of_session_volume_curve(df, truth["baseline_calendar"])
    rv = B.rvol_series(df, curve, calendar=truth["calendar"])
    ev = L.detect_events(df, L.EventParams(rvol_min=10_000.0),
                         calendar=truth["calendar"], rvol_series=rv,
                         prev_close_u={(truth["symbol"], truth["market_day"].date):
                                       truth["prev_close_u"]})
    assert ev.empty


# --------------------------------------------------------------------------- #
# 파라미터화 / 다심볼 / 매매일 경계
# --------------------------------------------------------------------------- #
def test_params_change_detection() -> None:
    df, truth = synth.make_scenario("noise", seed=1)
    curve = B.minute_of_session_volume_curve(df, truth["baseline_calendar"])
    rv = B.rvol_series(df, curve, calendar=truth["calendar"])
    loose = L.detect_events(df, L.EventParams(ret_min=0.02, day_ret_min=0.03,
                                              rvol_min=1.0),
                            calendar=truth["calendar"], rvol_series=rv,
                            prev_close_u={(truth["symbol"], truth["market_day"].date):
                                       truth["prev_close_u"]})
    assert not loose.empty, "임계를 낮추면 노이즈도 잡혀야 한다 (파라미터화 확인)"


def test_max_per_day() -> None:
    df, truth = synth.make_scenario("coil_pop", seed=1)
    curve = B.minute_of_session_volume_curve(df, truth["baseline_calendar"])
    rv = B.rvol_series(df, curve, calendar=truth["calendar"])
    kw = dict(calendar=truth["calendar"], rvol_series=rv,
              prev_close_u={(truth["symbol"], truth["market_day"].date):
                                       truth["prev_close_u"]})
    one = L.detect_events(df, L.EventParams(), max_per_day=1, **kw)
    many = L.detect_events(df, L.EventParams(), max_per_day=5, **kw)
    assert len(many) >= len(one)


def test_multi_symbol_independent() -> None:
    bundle = synth.make_dataset({"coil_pop": 2, "noise": 1}, seed=5)
    ev = L.detect_events(bundle.df_1m, L.EventParams(),
                         calendar=bundle.calendar,
                         prev_close_u={(t["symbol"], t["market_day"].date):
                                       t["prev_close_u"] for t in bundle.truths})
    assert set(ev["symbol"]) <= set(bundle.symbols)
    coil_syms = [t["symbol"] for t in bundle.truths if t["kind"] == "coil_pop"]
    assert set(coil_syms) <= set(ev["symbol"])


def test_works_without_calendar_using_utc_dates() -> None:
    """calendar 없이도 UTC 날짜 = 매매일 로 동작 (docs/07 §3.1)."""
    df, truth = synth.make_scenario("coil_pop", seed=1)
    ev = L.detect_events(df, L.EventParams(), prev_close_u={(truth["symbol"], truth["market_day"].date):
                                       truth["prev_close_u"]})
    assert not ev.empty
    assert (ev["session"] == "unknown").all()
    day_ms = 86_400_000
    exp_day = truth["t0_expected_ms"] // day_ms
    assert exp_day in set(ev["t0_ms"] // day_ms)


def test_prev_close_fallback_chain() -> None:
    """prev_close_u 미지정 → 이전 봉 종가 → 당일 첫 시가."""
    df, truth = synth.make_scenario("coil_pop", seed=1, history_days=0)
    ev = L.detect_events(df, L.EventParams(), calendar=truth["calendar"])
    assert not ev.empty, "전일 종가가 없어도 검출은 되어야 한다"

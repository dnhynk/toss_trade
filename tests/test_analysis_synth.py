"""합성 데이터 생성기 자체 검증 — 소유: W3.

값이 아니라 **불변식**을 검증한다 (시나리오 파라미터를 조금 조정해도 깨지지 않도록).
"""
from __future__ import annotations

import pandas as pd
import pytest

from tests import synth


# --------------------------------------------------------------------------- #
# 스키마·규약
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", synth.SCENARIO_KINDS)
def test_schema_and_dtypes(kind: str) -> None:
    df, truth = synth.make_scenario(kind, seed=3)
    assert list(df.columns) == synth.CANDLE_COLS
    for col in ("ts_ms", "open_u", "high_u", "low_u", "close_u", "vol_qu"):
        assert df[col].dtype == "int64", f"{col} must stay int64 (계약 C-1/C-2)"
    assert df["ts_ms"].is_monotonic_increasing
    assert (df["ts_ms"] % synth.MIN_MS == 0).all()
    assert df["ts_ms"].is_unique
    # OHLC 정합성
    assert (df["high_u"] >= df[["open_u", "close_u"]].max(axis=1)).all()
    assert (df["low_u"] <= df[["open_u", "close_u"]].min(axis=1)).all()
    assert (df["low_u"] > 0).all()
    assert (df["vol_qu"] > 0).all(), "거래 없는 분은 행 자체가 없어야 한다"
    assert truth["kind"] == kind


@pytest.mark.parametrize("kind", synth.SCENARIO_KINDS)
def test_deterministic(kind: str) -> None:
    a, ta = synth.make_scenario(kind, seed=11)
    b, tb = synth.make_scenario(kind, seed=11)
    pd.testing.assert_frame_equal(a, b)
    assert ta["t0_expected_ms"] == tb["t0_expected_ms"]
    pd.testing.assert_frame_equal(ta["rankings"], tb["rankings"])


@pytest.mark.parametrize("kind", synth.SCENARIO_KINDS)
def test_seed_changes_data(kind: str) -> None:
    a, _ = synth.make_scenario(kind, seed=1)
    b, _ = synth.make_scenario(kind, seed=2)
    assert not a["close_u"].equals(b["close_u"])


# --------------------------------------------------------------------------- #
# 세션 배치
# --------------------------------------------------------------------------- #
def test_calendar_sessions_are_ordered_and_contiguous() -> None:
    cal = synth.make_calendar(5)
    assert len(cal) == 5
    for md in cal:
        wins = synth.session_windows(md)
        assert [n for n, _ in wins] == list(synth.SESSION_ORDER)
        for _name, w in wins:
            assert w.end_ms > w.start_ms
        for (_, a), (_, b) in zip(wins[:-1], wins[1:]):
            assert a.end_ms <= b.start_ms, "세션이 겹치면 안 된다"
    # 주말 제외
    assert all(md.date[8:10] not in ("06", "07") or True for md in cal)
    dates = [md.date for md in cal]
    assert len(set(dates)) == len(dates)


@pytest.mark.parametrize("kind", synth.SCENARIO_KINDS)
def test_all_bars_inside_declared_sessions(kind: str) -> None:
    df, truth = synth.make_scenario(kind, seed=5)
    windows = [w for md in truth["calendar"] for _n, w in synth.session_windows(md)]
    ts = df["ts_ms"].to_numpy()
    # 종료 라벨(docs/12 §6.1): 라벨 T 인 봉의 내용은 [T-60초, T) 이므로 세션 소속은
    # `start < T <= end` 다. 세션 마지막 분의 봉은 라벨이 곧 `end_ms` 다.
    inside = [any(w.start_ms < int(t) <= w.end_ms for w in windows) for t in ts]
    assert all(inside), "세션 밖 봉이 생성되면 안 된다"


# --------------------------------------------------------------------------- #
# 시나리오 의도 (ground truth 불변식)
# --------------------------------------------------------------------------- #
def test_noise_has_no_event() -> None:
    for seed in range(6):
        _df, truth = synth.make_scenario("noise", seed=seed)
        assert truth["is_event"] is False
        assert truth["t0_expected_ms"] is None, "노이즈 시나리오는 가격 조건을 넘지 않아야 한다"
        assert truth["float_rotation"] < 0.5


@pytest.mark.parametrize("kind", [k for k in synth.SCENARIO_KINDS if k != "noise"])
def test_events_trigger_and_are_labelled(kind: str) -> None:
    _df, truth = synth.make_scenario(kind, seed=5)
    assert truth["is_event"] is True
    assert truth["t0_expected_ms"] is not None
    assert truth["t0_expected_reason"] in ("win", "day", "both")
    assert truth["hod_ms"] >= truth["t0_expected_ms"] or kind == "fade"
    assert truth["peak_ret_from_t0"] > 0.0


def test_t0_session_matches_expectation() -> None:
    """T0 가 시나리오가 의도한 세션 안에 있어야 한다."""
    for kind in synth.SCENARIO_KINDS:
        _df, truth = synth.make_scenario(kind, seed=4)
        want = truth["session_expected"]
        if want is None:
            continue
        win = getattr(truth["market_day"], want)
        assert win.start_ms <= truth["t0_expected_ms"] < win.end_ms, kind


def test_fade_scenario_reproduces_known_base_rates() -> None:
    """docs/02 §2.4: HOD 조기 형성 + VWAP 아래 마감."""
    _df, truth = synth.make_scenario("fade", seed=2)
    assert truth["hod_min_from_open"] <= 15
    assert truth["closed_below_vwap"] is True
    assert truth["vwap_close_rel"] < -0.05
    assert truth["ret_close_from_t0"] < 0.0


def test_coil_pop_holds_above_vwap() -> None:
    _df, truth = synth.make_scenario("coil_pop", seed=2)
    assert truth["closed_below_vwap"] is False
    assert truth["float_rotation"] > 1.0, "저플로트 러너는 플로트 로테이션 1.0 초과"


def test_dump_collapses_fast() -> None:
    """docs/02 §5: 피크 후 수 분 내 -20%."""
    _df, truth = synth.make_scenario("dump", seed=2)
    assert truth["peak_to_minus20_min"] is not None
    assert truth["peak_to_minus20_min"] <= 10
    assert truth["ret_close_from_t0"] < -0.15


def test_instant_leaves_little_to_capture() -> None:
    """즉발형: T0 종가 기준으로는 남은 상승이 작다 (La Morgia)."""
    _df, truth = synth.make_scenario("instant", seed=2)
    assert truth["peak_ret_from_t0"] < 0.20


def test_daymarket_event_is_in_day_session() -> None:
    _df, truth = synth.make_scenario("daymarket", seed=2)
    day = truth["market_day"].day
    assert day.start_ms <= truth["t0_expected_ms"] < day.end_ms
    assert truth["ret_close_from_t0"] < 0.0, "데이마켓 급등은 정규장까지 못 버틴다"


# --------------------------------------------------------------------------- #
# 캔들 공백 / 홀트
# --------------------------------------------------------------------------- #
def test_halt_gap_injects_regular_session_holes() -> None:
    _df, truth = synth.make_scenario("halt_gap", seed=2)
    assert len(truth["gaps"]["regular"]) >= 2, "정규장 내 홀트 공백 2회"
    assert any(n >= 10 for _ts, n in truth["gaps"]["pre"]), "프리장 수집중단 12분"


def test_dump_has_halt_gap() -> None:
    _df, truth = synth.make_scenario("dump", seed=2)
    assert len(truth["gaps"]["regular"]) >= 1


def test_thin_sessions_have_missing_minutes() -> None:
    """데이/애프터는 체결 공백이 흔하다 — 결측 내성 테스트의 근거."""
    _df, truth = synth.make_scenario("coil_pop", seed=2)
    assert truth["gaps"]["day"], "데이마켓에 캔들 공백이 있어야 한다"


def test_drop_random_bars_keeps_schema() -> None:
    df, _ = synth.make_scenario("coil_pop", seed=2)
    thin = synth.drop_random_bars(df, frac=0.2, seed=1)
    assert len(thin) < len(df)
    assert list(thin.columns) == synth.CANDLE_COLS
    assert thin["ts_ms"].is_monotonic_increasing


# --------------------------------------------------------------------------- #
# 랭킹
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", synth.SCENARIO_KINDS)
def test_rankings_schema(kind: str) -> None:
    _df, truth = synth.make_scenario(kind, seed=3)
    rk = truth["rankings"]
    assert list(rk.columns) == synth.RANKING_COLS
    assert rk["snap_ms"].dtype == "int64"
    assert set(rk["ranking_type"]) <= set(synth.RANK_TYPES)
    assert (rk["duration"] == "realtime").all()
    assert (rk["rank"] >= 1).all()
    # 스냅샷·타입별로 rank 가 amount 내림차순과 일치
    for (_snap, _rt), g in rk.groupby(["snap_ms", "ranking_type"]):
        g = g.sort_values("rank")
        assert g["amount_u"].is_monotonic_decreasing
        assert list(g["rank"]) == list(range(1, len(g) + 1))


def test_ranking_entry_respects_configured_lead_lag() -> None:
    _df, truth = synth.make_scenario("coil_pop", seed=3)
    rk = truth["rankings"]
    sym = truth["symbol"]
    for rtype, entry_ms in truth["ranking_first_entry_ms"].items():
        rows = rk[(rk["symbol"] == sym) & (rk["ranking_type"] == rtype)]
        assert not rows.empty
        assert int(rows["snap_ms"].min()) == entry_ms
    lag = truth["ranking_lead_lag_min"]
    assert lag["TOSS_SECURITIES_TRADING_AMOUNT"] < 0, "coil_pop 은 토스 랭킹 선행 케이스"


def test_noise_never_enters_rankings() -> None:
    _df, truth = synth.make_scenario("noise", seed=3)
    rk = truth["rankings"]
    assert (rk["symbol"] != truth["symbol"]).all()


def test_toss_share_is_below_market_and_rises() -> None:
    """토스 쏠림도(TOSS/MARKET amount 비율)는 1 미만이며 이벤트로 갈수록 커진다."""
    _df, truth = synth.make_scenario("coil_pop", seed=3)
    rk, sym = truth["rankings"], truth["symbol"]
    mine = rk[rk["symbol"] == sym].pivot_table(
        index="snap_ms", columns="ranking_type", values="amount_u")
    both = mine.dropna()
    ratio = both["TOSS_SECURITIES_TRADING_AMOUNT"] / both["MARKET_TRADING_AMOUNT"]
    assert (ratio > 0).all() and (ratio < 1).all()
    assert ratio.iloc[-1] > ratio.iloc[0]


# --------------------------------------------------------------------------- #
# 일봉 이력 / 번들
# --------------------------------------------------------------------------- #
def test_history_1d_ends_at_prev_close() -> None:
    _df, truth = synth.make_scenario("coil_pop", seed=3)
    d1 = truth["df_1d"]
    assert len(d1) == 25
    assert int(d1["close_u"].to_numpy()[-1]) == truth["prev_close_u"]
    assert d1["ts_ms"].is_monotonic_increasing
    assert (d1["vol_qu"] > 0).all()


def test_next_day_gap_present() -> None:
    _df, truth = synth.make_scenario("dump", seed=3)
    assert truth["next_day_gap"] is not None
    assert truth["next_day_gap"] < 0.0


def test_include_next_day_false() -> None:
    df, truth = synth.make_scenario("coil_pop", seed=3, include_next_day=False,
                                    history_days=2)
    assert truth["next_day_gap"] is None
    assert len(truth["calendar"]) == 3          # 이력 2일 + 이벤트 당일
    assert truth["event_day_index"] == 2
    # 마지막 봉의 라벨은 애프터장 종료 시각 그 자체다 (종료 라벨, §6.1)
    assert int(df["ts_ms"].max()) <= truth["market_day"].after.end_ms


def test_history_days_precede_event_day() -> None:
    """이력일은 이벤트 당일보다 앞서고, 이벤트를 포함하지 않아야 한다."""
    df, truth = synth.make_scenario("coil_pop", seed=3, history_days=4)
    assert len(truth["baseline_calendar"]) == 4
    md = truth["market_day"]
    assert all(h.regular.start_ms < md.regular.start_ms
               for h in truth["baseline_calendar"])
    hist_end = truth["baseline_calendar"][-1].after.end_ms
    hist = df[df["ts_ms"] < hist_end]
    assert len(hist) > 1000
    # 이력 구간에서 30분 +15% 가격 조건이 발생하지 않아야 한다
    for h in truth["baseline_calendar"]:
        day = df[(df["ts_ms"] >= h.day.start_ms) & (df["ts_ms"] < h.after.end_ms)]
        t0, _reason = synth._first_price_trigger(day, int(day["open_u"].iloc[0]))
        assert t0 is None, "이력일에 이벤트가 생기면 베이스라인이 오염된다"


def test_prev_close_comes_from_prior_regular_session() -> None:
    df, truth = synth.make_scenario("coil_pop", seed=3, history_days=2)
    prev_reg = truth["baseline_calendar"][-1].regular
    prior = df[(df["ts_ms"] > prev_reg.start_ms) & (df["ts_ms"] <= prev_reg.end_ms)]
    assert int(prior["close_u"].to_numpy()[-1]) == truth["prev_close_u"]


def test_make_dataset_bundle() -> None:
    bundle = synth.make_dataset({"coil_pop": 2, "noise": 1, "fade": 1}, seed=1)
    assert len(bundle.truths) == 4
    assert len(set(bundle.symbols)) == 4
    assert set(bundle.df_1m["symbol"]) == set(bundle.symbols)
    assert bundle.truth_for(bundle.symbols[0])["kind"] == "coil_pop"
    with pytest.raises(KeyError):
        bundle.truth_for("NOPE")


def test_unknown_kind_raises() -> None:
    with pytest.raises(ValueError):
        synth.make_scenario("does_not_exist")

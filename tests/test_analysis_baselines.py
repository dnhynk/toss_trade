"""베이스라인 지표 검증 — 소유: W3.

중점: **시간대 보정의 정확성**, int64 오버플로 부재, 결측·홀트 내성.
"""
from __future__ import annotations

import math
from decimal import Decimal

import pandas as pd
import pytest

from tests import synth
from tossmon.analysis import baselines as B
from tossmon.api.models import MICRO, SessionWindow, UsMarketDay

MIN_MS = B.MIN_MS


def _candles(rows: list[tuple[int, int, int, int, int, int]],
             symbol: str = "TEST") -> pd.DataFrame:
    """(ts_ms, o, h, l, c, v) 튜플 목록 → 규약 준수 DataFrame."""
    return pd.DataFrame(
        [{"symbol": symbol, "ts_ms": t, "open_u": o, "high_u": h, "low_u": lo,
          "close_u": c, "vol_qu": v} for t, o, h, lo, c, v in rows]
    ).astype({"ts_ms": "int64", "open_u": "int64", "high_u": "int64", "low_u": "int64",
              "close_u": "int64", "vol_qu": "int64"})


# --------------------------------------------------------------------------- #
# compute_daily_baseline
# --------------------------------------------------------------------------- #
def test_daily_baseline_known_values() -> None:
    """손으로 계산 가능한 입력으로 정의식을 고정한다."""
    rows = [(i * 86_400_000, 100, 110, 90, 100 + i, (i + 1) * MICRO) for i in range(5)]
    b = B.compute_daily_baseline(_candles(rows), window_days=5)
    assert b["n_days"] == 5
    assert b["close_last_u"] == 104
    # 거래량 1..5 (마이크로주) → 평균 3
    assert b["adv20_qu"] == 3 * MICRO
    assert b["vol_mean_qu"] == pytest.approx(3 * MICRO)
    # 표본표준편차 ddof=1 → sqrt(2.5)
    assert b["vol_std_qu"] == pytest.approx(math.sqrt(2.5) * MICRO)
    # TR: 첫 봉은 H-L=20, 이후 max(20, |110-c_prev|, |90-c_prev|)
    tr = [20] + [max(110 - 90, abs(110 - (100 + i - 1)), abs(90 - (100 + i - 1)))
                 for i in range(1, 5)]
    assert b["atr20_u"] == sum(tr) // 5
    assert b["atr20_pct"] == pytest.approx(b["atr20_u"] / 104)


def test_daily_baseline_empty_and_single() -> None:
    b = B.compute_daily_baseline(pd.DataFrame())
    assert b["n_days"] == 0 and b["adv20_qu"] is None
    assert math.isnan(b["logvol_std"])

    one = _candles([(0, 100, 120, 80, 110, 7 * MICRO)])
    b1 = B.compute_daily_baseline(one)
    assert b1["n_days"] == 1
    assert b1["adv20_qu"] == 7 * MICRO
    assert math.isnan(b1["vol_std_qu"]), "표본 1개면 표준편차는 정의되지 않는다"
    assert b1["atr20_u"] == 40


def test_daily_baseline_uses_only_last_window_and_sorts() -> None:
    rows = [(i * 86_400_000, 100, 100, 100, 100, (i + 1) * MICRO) for i in range(30)]
    df = _candles(rows).sample(frac=1.0, random_state=0)      # 순서 뒤섞기
    b = B.compute_daily_baseline(df, window_days=10)
    assert b["n_days"] == 10
    assert b["adv20_qu"] == sum(range(21, 31)) * MICRO // 10      # 25.5M 마이크로주
    assert b["close_last_u"] == 100


def test_daily_volume_z_log_and_linear() -> None:
    rows = [(i * 86_400_000, 100, 100, 100, 100, (100 + i) * MICRO) for i in range(20)]
    b = B.compute_daily_baseline(_candles(rows))
    z_typical = B.daily_volume_z(b, 110 * MICRO)
    z_spike = B.daily_volume_z(b, 10_000 * MICRO)
    assert abs(z_typical) < 1.0
    assert z_spike > 5.0
    assert B.daily_volume_z(b, 110 * MICRO, log=False) != z_typical
    assert math.isnan(B.daily_volume_z(b, 0))
    assert math.isnan(B.daily_volume_z({}, 100))


def test_multi_symbol_input_rejected() -> None:
    a = _candles([(0, 1, 1, 1, 1, 1)], symbol="AAA")
    b = _candles([(0, 1, 1, 1, 1, 1)], symbol="BBB")
    with pytest.raises(ValueError):
        B.compute_daily_baseline(pd.concat([a, b], ignore_index=True))


# --------------------------------------------------------------------------- #
# 시간대 보정 곡선
# --------------------------------------------------------------------------- #
def _one_session_day(date: str, start_ms: int, n_min: int) -> UsMarketDay:
    return UsMarketDay(date=date, day=None, pre=None,
                       regular=SessionWindow(start_ms=start_ms,
                                             end_ms=start_ms + n_min * MIN_MS),
                       after=None)


def test_curve_is_mean_per_minute_position() -> None:
    """분 위치별 평균: 2일치 (10, 20) → (15). 정확한 값으로 고정."""
    d0, d1 = 0, 86_400_000
    rows = []
    for base, mult in ((d0, 1), (d1, 2)):
        for m in range(3):
            rows.append((base + m * MIN_MS, 100, 100, 100, 100, (m + 1) * 10 * mult))
    cal = [_one_session_day("2026-06-01", d0, 3), _one_session_day("2026-06-02", d1, 3)]
    curve = B.minute_of_session_volume_curve(_candles(rows), cal)
    # 색인은 (세션, 세션길이분, 분위치) — 길이가 키에 들어간다 (감사 M-4)
    assert list(curve.index) == [("regular", 3, 0), ("regular", 3, 1), ("regular", 3, 2)]
    assert curve.index.names == ["session", "session_len_min", "minute"]
    assert curve.loc[("regular", 3, 0)] == pytest.approx((10 + 20) / 2)
    assert curve.loc[("regular", 3, 1)] == pytest.approx((20 + 40) / 2)
    assert curve.loc[("regular", 3, 2)] == pytest.approx((30 + 60) / 2)
    cum = curve.attrs[B.CURVE_CUM_KEY]
    assert cum.loc[("regular", 3, 2)] == pytest.approx(
        ((10 + 20 + 30) + (20 + 40 + 60)) / 2)
    assert curve.attrs[B.CURVE_DAYS_KEY][("regular", 3, 0)] == 2


def test_curve_counts_missing_minutes_as_zero() -> None:
    """봉이 없는 분은 거래량 0 — 분모가 줄어들면 RVOL 이 구조적으로 부풀려진다."""
    d0, d1 = 0, 86_400_000
    rows = [(d0 + 0 * MIN_MS, 1, 1, 1, 1, 100), (d0 + 1 * MIN_MS, 1, 1, 1, 1, 100),
            (d1 + 0 * MIN_MS, 1, 1, 1, 1, 100)]      # 2일차 1분째 봉 없음
    cal = [_one_session_day("2026-06-01", d0, 2), _one_session_day("2026-06-02", d1, 2)]
    curve = B.minute_of_session_volume_curve(_candles(rows), cal)
    assert curve.loc[("regular", 2, 1)] == pytest.approx(50.0)     # (100 + 0)/2
    assert curve.attrs[B.CURVE_DAYS_KEY][("regular", 2, 1)] == 2


def test_curve_skips_fully_empty_sessions() -> None:
    """수집 중단 세션(전부 결측)은 평균에서 제외되지만 세션 윈도우는 등록된다."""
    d0, d1 = 0, 86_400_000
    rows = [(d0 + m * MIN_MS, 1, 1, 1, 1, 100) for m in range(2)]
    cal = [_one_session_day("2026-06-01", d0, 2), _one_session_day("2026-06-02", d1, 2)]
    curve = B.minute_of_session_volume_curve(_candles(rows), cal)
    assert curve.loc[("regular", 2, 0)] == pytest.approx(100.0)
    assert curve.attrs[B.CURVE_DAYS_KEY][("regular", 2, 0)] == 1
    starts = [s for s, _e, _n, _d in curve.attrs[B.CURVE_SESSION_KEY]]
    assert d1 in starts, "빈 세션도 위치 판정용으로는 등록돼야 한다"


def test_curve_exclude_dates_keeps_windows_but_drops_average() -> None:
    d0, d1 = 0, 86_400_000
    rows = ([(d0 + m * MIN_MS, 1, 1, 1, 1, 100) for m in range(2)]
            + [(d1 + m * MIN_MS, 1, 1, 1, 1, 9999) for m in range(2)])
    cal = [_one_session_day("2026-06-01", d0, 2), _one_session_day("2026-06-02", d1, 2)]
    curve = B.minute_of_session_volume_curve(_candles(rows), cal,
                                            exclude_dates=["2026-06-02"])
    assert curve.loc[("regular", 2, 0)] == pytest.approx(100.0), "제외일은 분모에 못 들어간다"
    assert B.curve_locate(curve, d1) == ("regular", 0, d1, d1 + 2 * MIN_MS)


def test_curve_handles_holiday_all_none_sessions() -> None:
    """market_calendar_us_holiday 픽스처처럼 4세션 전부 null 인 날."""
    holiday = UsMarketDay(date="2026-07-04", day=None, pre=None, regular=None,
                          after=None)
    rows = [(m * MIN_MS, 1, 1, 1, 1, 10) for m in range(2)]
    cal = [holiday, _one_session_day("2026-07-06", 0, 2)]
    curve = B.minute_of_session_volume_curve(_candles(rows), cal)
    assert len(curve) == 2
    assert B.session_windows(holiday) == []


def test_curve_separates_days_of_different_session_length() -> None:
    """세션 길이가 다르면 **버킷이 갈린다** (감사 M-4).

    이 테스트는 이전 구현의 의도를 뒤집은 것이다 — 예전에는 길이가 달라도 같은
    `(session, minute)` 버킷을 공유했고, 그것이 바로 반일장 오염의 원인이었다.
    """
    d0, d1 = 0, 86_400_000
    rows = ([(d0 + m * MIN_MS, 1, 1, 1, 1, 100) for m in range(5)]
            + [(d1 + m * MIN_MS, 1, 1, 1, 1, 100) for m in range(2)])
    cal = [_one_session_day("2026-06-01", d0, 5), _one_session_day("2026-06-02", d1, 2)]
    curve = B.minute_of_session_volume_curve(_candles(rows), cal)
    cnt = curve.attrs[B.CURVE_DAYS_KEY]
    assert cnt[("regular", 5, 0)] == 1, "긴 날과 짧은 날이 같은 버킷을 쓰면 안 된다"
    assert cnt[("regular", 2, 0)] == 1
    assert ("regular", 5, 4) in cnt
    assert ("regular", 2, 4) not in cnt, "짧은 날에는 minute 4 가 존재하지 않는다"
    assert curve.attrs[B.CURVE_LENGTHS_KEY]["regular"] == {5: 1, 2: 1}


# --------------------------------------------------------------------------- #
# RVOL
# --------------------------------------------------------------------------- #
def test_rvol_cumulative_definition() -> None:
    """RVOL = 누적거래량 / 같은 분위치의 평균 누적거래량. 정확한 값으로 고정."""
    d0, d1, d2 = 0, 86_400_000, 2 * 86_400_000
    rows = []
    for base, v in ((d0, 100), (d1, 100)):
        rows += [(base + m * MIN_MS, 1, 1, 1, 1, v) for m in range(3)]
    rows += [(d2 + m * MIN_MS, 1, 1, 1, 1, 300) for m in range(3)]   # 3배
    cal = [_one_session_day(f"2026-06-0{i + 1}", d, 3)
           for i, d in enumerate((d0, d1, d2))]
    df = _candles(rows)
    curve = B.minute_of_session_volume_curve(df, cal, exclude_dates=["2026-06-03"])
    assert B.rvol(df, curve, d2) == pytest.approx(3.0)
    assert B.rvol(df, curve, d2 + 2 * MIN_MS) == pytest.approx(3.0)
    assert B.rvol_bar(df, curve, d2 + 1 * MIN_MS) == pytest.approx(3.0)
    assert math.isnan(B.rvol(df, curve, d2 + 99 * MIN_MS)), "세션 밖은 NaN"


def test_rvol_series_matches_scalar_rvol() -> None:
    df, truth = synth.make_scenario("coil_pop", seed=2, history_days=2)
    curve = B.minute_of_session_volume_curve(df, truth["baseline_calendar"])
    s = B.rvol_series(df, curve, calendar=truth["calendar"])
    reg = truth["market_day"].regular
    for off in (0, 30, 120, 300):
        ts = reg.start_ms + off * MIN_MS
        if ts in s.index:
            assert s.loc[ts] == pytest.approx(B.rvol(df, curve, ts,
                                                     calendar=truth["calendar"]))


def test_rvol_series_resets_each_session() -> None:
    """세션이 바뀌면 누적이 리셋돼야 한다."""
    df, truth = synth.make_scenario("noise", seed=2, history_days=2)
    curve = B.minute_of_session_volume_curve(df, truth["baseline_calendar"])
    s = B.rvol_series(df, curve, calendar=truth["calendar"]).dropna()
    md = truth["market_day"]
    for win in (md.pre, md.regular, md.after):
        seg = s[(s.index >= win.start_ms) & (s.index < win.start_ms + 5 * MIN_MS)]
        if len(seg):
            assert seg.iloc[0] < 12.0, "세션 첫 봉의 누적 RVOL 이 폭주하면 리셋 실패"


def test_rvol_calendar_override_locates_new_day() -> None:
    """이력일로 만든 곡선을 '오늘'에 적용하는 정상 운용 경로."""
    df, truth = synth.make_scenario("coil_pop", seed=2)
    curve = B.minute_of_session_volume_curve(df, truth["baseline_calendar"])
    t0 = truth["t0_expected_ms"]
    assert math.isnan(B.rvol(df, curve, t0)), "이벤트 당일은 곡선 attrs 에 없다"
    assert B.rvol(df, curve, t0, calendar=truth["calendar"]) > 1.0


def test_rvol_empty_curve_is_nan() -> None:
    df, truth = synth.make_scenario("noise", seed=2, history_days=1)
    curve = B.minute_of_session_volume_curve(df.iloc[0:0], [])
    assert math.isnan(B.rvol(df, curve, int(df["ts_ms"].iloc[0])))
    assert B.rvol_series(df.iloc[0:0], curve).empty


# --------------------------------------------------------------------------- #
# 세션 VWAP
# --------------------------------------------------------------------------- #
def test_session_vwap_exact() -> None:
    rows = [(0, 100, 120, 80, 100, 10), (MIN_MS, 100, 200, 100, 150, 30)]
    df = _candles(rows)
    win = SessionWindow(start_ms=0, end_ms=10 * MIN_MS)
    vw = B.session_vwap_u(df, win)
    tp0 = (120 + 80 + 100) // 3                      # 100
    tp1 = (200 + 100 + 150) // 3                     # 150
    assert list(vw.index) == [0, MIN_MS]
    assert vw.iloc[0] == tp0
    assert vw.iloc[1] == (tp0 * 10 + tp1 * 30) // 40
    assert vw.dtype == "int64", "가격은 int 로 유지 (계약 C-2)"


def test_session_vwap_no_int64_overflow() -> None:
    """tp_u*vol_qu 는 봉당 ~1e17 — 390봉이면 int64 상한을 넘는다."""
    n = 390
    price_u = 4_000_000
    vol = 20_000 * MICRO
    rows = [(m * MIN_MS, price_u, price_u, price_u, price_u, vol) for m in range(n)]
    df = _candles(rows)
    naive = int(df["high_u"].iloc[0]) * int(df["vol_qu"].sum())
    assert naive > 2 ** 63, "이 테스트가 오버플로 영역을 다루는지 확인"
    vw = B.session_vwap_u(df, SessionWindow(start_ms=0, end_ms=n * MIN_MS))
    assert (vw == price_u).all(), "임의정밀도 누적이 아니면 여기서 깨진다"


def test_session_vwap_zero_volume_prefix() -> None:
    rows = [(0, 100, 100, 100, 100, 0), (MIN_MS, 100, 100, 100, 100, 10)]
    vw = B.session_vwap_u(_candles(rows), SessionWindow(start_ms=0, end_ms=5 * MIN_MS))
    assert vw.iloc[0] == 100
    assert vw.iloc[1] == 100


def test_session_vwap_filters_by_window_and_is_empty_outside() -> None:
    df, truth = synth.make_scenario("coil_pop", seed=2, history_days=1)
    reg = truth["market_day"].regular
    vw = B.session_vwap_u(df, reg)
    assert len(vw) > 100
    assert int(vw.index.min()) >= reg.start_ms
    assert int(vw.index.max()) < reg.end_ms
    far = SessionWindow(start_ms=reg.end_ms + 10 ** 9, end_ms=reg.end_ms + 2 * 10 ** 9)
    assert B.session_vwap_u(df, far).empty


def test_session_vwap_map_concatenates_sessions() -> None:
    df, truth = synth.make_scenario("coil_pop", seed=2, history_days=1)
    s = B.session_vwap_map(df, truth["calendar"], sessions=("pre", "regular"))
    assert s.index.is_monotonic_increasing
    assert len(s) > 300
    assert B.session_vwap_map(df, [], sessions=("regular",)).empty


def test_session_vwap_matches_synth_independent_measurement() -> None:
    """synth 가 독립 계산한 정규장 VWAP 과 일치해야 한다."""
    df, truth = synth.make_scenario("fade", seed=2)
    vw = B.session_vwap_u(df, truth["market_day"].regular)
    assert int(vw.iloc[-1]) == truth["regular_vwap_last_u"]


# --------------------------------------------------------------------------- #
# ATR / TR
# --------------------------------------------------------------------------- #
def test_true_range_first_bar_and_gaps() -> None:
    rows = [(0, 100, 110, 90, 105, 1), (MIN_MS, 200, 210, 190, 205, 1)]
    tr = B.true_range_u(_candles(rows))
    assert int(tr.iloc[0]) == 20
    assert int(tr.iloc[1]) == max(210 - 190, abs(210 - 105), abs(190 - 105))
    assert tr.dtype == "int64"


def test_atr_none_on_empty() -> None:
    assert B.atr_u(pd.DataFrame()) is None
    assert B.atr_u(None) is None


def test_locate_session() -> None:
    md = synth.make_calendar(1)[0]
    assert B.locate_session(md, md.regular.start_ms)[0] == "regular"
    assert B.locate_session(md, md.pre.start_ms)[0] == "pre"
    assert B.locate_session(md, md.regular.end_ms - 1)[0] == "regular"
    assert B.locate_session(md, md.after.end_ms + MIN_MS) is None


def test_prices_stay_integral_no_float_arithmetic() -> None:
    """계약 C-2: 가격 연산에 float 이 끼면 여기서 드러난다."""
    df, truth = synth.make_scenario("dump", seed=2, history_days=1)
    vw = B.session_vwap_u(df, truth["market_day"].regular)
    assert vw.dtype == "int64"
    assert all(isinstance(int(v), int) for v in vw.iloc[:5])
    b = B.compute_daily_baseline(truth["df_1d"])
    assert isinstance(b["atr20_u"], int)
    assert isinstance(b["adv20_qu"], int)
    # Decimal 환산이 깨지지 않는지 (마이크로달러 → 달러)
    assert Decimal(int(vw.iloc[-1])) / Decimal(MICRO) > 0

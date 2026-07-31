"""사전등록(`docs/12_preregistration.md`) 정합 회귀 테스트 — 소유: W3.

    §2.2  곡선은 평가일 D 기준 **엄격히 과거 20 매매일**, (세션,길이) 버킷당 관측 10일 이상
    §2.3  1분봉(원주가) / 일봉(수정주가) **가격 수준 직접 비교 금지**
    §2.3  전일 종가 대체 사슬 — **당일 첫 시가 대체 금지**
    §2.7  분석 표본 유니버스 필터 + 사유별 제외 카운트

각 테스트는 **수정 전 구현에서 실패한다** (stash 검증 완료).
"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from tests import synth
from tossmon.analysis import baselines as B
from tossmon.analysis import evaluate as E
from tossmon.analysis import features as F
from tossmon.analysis import labeling as L
from tossmon.api.models import SessionWindow, UsMarketDay

MIN_MS = B.MIN_MS
DAY_MS = 86_400_000
MICRO = 1_000_000


def _flat_day(md: UsMarketDay, vol: int, close_u: int = 1_000_000,
              symbol: str = "S") -> list[dict]:
    rows = []
    for _name, w in synth.session_windows(md):
        for m in range((w.end_ms - w.start_ms) // MIN_MS):
            rows.append({"symbol": symbol, "ts_ms": w.start_ms + m * MIN_MS,
                         "open_u": close_u, "high_u": close_u, "low_u": close_u,
                         "close_u": close_u, "vol_qu": vol})
    return rows


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows).astype({"ts_ms": "int64", "open_u": "int64",
                                      "high_u": "int64", "low_u": "int64",
                                      "close_u": "int64", "vol_qu": "int64"})


# =========================================================================== #
# §2.2 — 곡선 20일 창
# =========================================================================== #
def _volume_ramp_calendar(n_days: int = 30):
    """n일 캘린더 + 날짜가 갈수록 거래량이 커지는 봉. 창이 좁을수록 평균이 커진다."""
    cal = synth.make_calendar(n_days, start="2026-06-01")
    rows: list[dict] = []
    for i, md in enumerate(cal):
        rows.extend(_flat_day(md, vol=(i + 1) * 1_000_000))
    return _frame(rows), cal


def test_curve_window_days_uses_only_recent_20_market_days() -> None:
    """30일 캘린더를 줘도 분모는 **최근 20 매매일**만이어야 한다 (사전등록 §2.2)."""
    df, cal = _volume_ramp_calendar(30)
    curve = B.minute_of_session_volume_curve(df, cal)          # window_days 기본 20
    key = ("regular", 390, 0)
    # 최근 20일 = 인덱스 10..29 → 거래량 11..30 (백만주) → 평균 20.5
    assert curve.attrs[B.CURVE_DAYS_KEY][key] == 20, "창 밖 날이 분모에 들어갔다"
    assert curve.loc[key] == pytest.approx(20.5 * 1_000_000)

    wide = B.minute_of_session_volume_curve(df, cal, window_days=30)
    assert wide.attrs[B.CURVE_DAYS_KEY][key] == 30
    assert wide.loc[key] == pytest.approx(15.5 * 1_000_000)


def test_curve_as_of_date_is_strictly_past() -> None:
    """평가일 D 자신은 분모에서 **구조적으로** 빠진다 (자기오염 금지의 기간 분리 강제)."""
    df, cal = _volume_ramp_calendar(25)
    d = cal[24].date
    curve = B.minute_of_session_volume_curve(df, cal, as_of_date=d)
    used = {date for _s, _e, _n, date in curve.attrs[B.CURVE_SESSION_KEY]}
    assert d in used, "세션 윈도우 등록은 유지된다(위치 판정용)"
    key = ("regular", 390, 0)
    assert curve.attrs[B.CURVE_DAYS_KEY][key] == 20
    # D(=25번째, 거래량 25M)를 뺀 최근 20일 = 5..24번째 → 거래량 5..24 → 평균 14.5
    assert curve.loc[key] == pytest.approx(14.5 * 1_000_000)


def test_curve_window_interacts_with_min_days_per_length_bucket() -> None:
    """20일 창 안에서 (세션,길이) 버킷별 관측일을 세고 10일 미만이면 버린다."""
    cal = synth.make_calendar(24, start="2026-06-01",
                              half_days=[f"2026-06-{d:02d}" for d in (3, 4, 5)])
    rows: list[dict] = []
    for md in cal:
        rows.extend(_flat_day(md, vol=1_000_000))
    df = _frame(rows)
    curve = B.minute_of_session_volume_curve(df, cal, min_days=B.PREREG_MIN_DAYS)
    lengths = curve.attrs[B.CURVE_LENGTHS_KEY]["regular"]
    assert lengths.get(210, 0) < B.PREREG_MIN_DAYS, "반일장은 창 안 관측이 부족하다"
    assert ("regular", 210) in curve.attrs[B.CURVE_DROPPED_KEY]
    assert ("regular", 210, 0) not in curve.index
    assert ("regular", 390, 0) in curve.index


def test_prereg_volume_curve_applies_both_frozen_values() -> None:
    df, cal = _volume_ramp_calendar(30)
    curve = B.prereg_volume_curve(df, cal, as_of_date=cal[29].date)
    key = ("regular", 390, 0)
    assert curve.attrs[B.CURVE_DAYS_KEY][key] == B.PREREG_WINDOW_DAYS
    assert B.PREREG_WINDOW_DAYS == 20 and B.PREREG_MIN_DAYS == 10
    # D 제외 후 최근 20일 = 9..28번째 → 거래량 10..29 → 평균 19.5
    assert curve.loc[key] == pytest.approx(19.5 * 1_000_000)


def test_window_days_none_or_zero_keeps_all_days() -> None:
    df, cal = _volume_ramp_calendar(25)
    curve = B.minute_of_session_volume_curve(df, cal, window_days=0)
    assert curve.attrs[B.CURVE_DAYS_KEY][("regular", 390, 0)] == 25


# =========================================================================== #
# §2.3 — 원주가 × 수정주가 혼합 제거
# =========================================================================== #
def test_gap_from_prev_close_requires_explicit_unadjusted_prev_close() -> None:
    """일봉(수정주가) baseline 으로는 갭을 계산하지 않는다 (사전등록 §2.3)."""
    df, truth = synth.make_scenario("coil_pop", seed=1)
    curve = B.minute_of_session_volume_curve(df, truth["baseline_calendar"])
    base = B.compute_daily_baseline(truth["df_1d"])
    assert base["close_last_u"], "이 테스트는 baseline 에 종가가 있을 때를 가정한다"
    t0 = truth["t0_expected_ms"]

    without = F.extract_precursor_features(df, truth["rankings"], t0, curve=curve,
                                           calendar=truth["calendar"], baseline=base,
                                           symbol=truth["symbol"])
    assert math.isnan(without["gap_from_prev_close"]), \
        "prev_close_u 없이 baseline(수정주가)으로 갭을 계산하면 §2.3 위반"

    withv = F.extract_precursor_features(df, truth["rankings"], t0, curve=curve,
                                         calendar=truth["calendar"], baseline=base,
                                         symbol=truth["symbol"],
                                         prev_close_u=truth["prev_close_u"])
    assert withv["gap_from_prev_close"] == pytest.approx(
        withv["close_cut_u"] / truth["prev_close_u"] - 1.0)


def test_baseline_still_feeds_ratio_features_only() -> None:
    """baseline 은 ATR%·거래량 z 같은 **비율** 지표에만 쓰인다."""
    df, truth = synth.make_scenario("coil_pop", seed=1)
    curve = B.minute_of_session_volume_curve(df, truth["baseline_calendar"])
    base = B.compute_daily_baseline(truth["df_1d"])
    f = F.extract_precursor_features(df, truth["rankings"], truth["t0_expected_ms"],
                                     curve=curve, calendar=truth["calendar"],
                                     baseline=base, symbol=truth["symbol"])
    assert f["atr20_pct"] == pytest.approx(base["atr20_pct"])
    assert not math.isnan(f["daily_vol_z"])


def test_split_would_not_create_a_fake_gap() -> None:
    """수정주가와 원주가가 크게 어긋난 상황(분할)에서도 가짜 갭이 생기지 않는다."""
    df, truth = synth.make_scenario("coil_pop", seed=2)
    curve = B.minute_of_session_volume_curve(df, truth["baseline_calendar"])
    base = dict(B.compute_daily_baseline(truth["df_1d"]))
    base["close_last_u"] = int(base["close_last_u"] // 10)     # 10:1 분할 모사
    f = F.extract_precursor_features(df, truth["rankings"], truth["t0_expected_ms"],
                                     curve=curve, calendar=truth["calendar"],
                                     baseline=base, symbol=truth["symbol"])
    assert math.isnan(f["gap_from_prev_close"]), "수정주가가 갭 계산에 새어들면 안 된다"


# =========================================================================== #
# §2.3 — 전일 종가 대체 사슬
# =========================================================================== #
def _two_day_frame(prev_regular_close: int, prev_after_close: int,
                   day_open: int, day_close: int):
    """직전 매매일(정규장 + 애프터) + 평가일. 각 종가를 직접 지정한다."""
    cal = synth.make_calendar(2, start="2026-06-01")
    prev, cur = cal[0], cal[1]
    rows: list[dict] = []
    for m in range(3):
        ts = prev.regular.start_ms + m * MIN_MS
        c = prev_regular_close
        rows.append({"symbol": "S", "ts_ms": ts, "open_u": c, "high_u": c,
                     "low_u": c, "close_u": c, "vol_qu": 1_000_000})
    for m in range(3):
        ts = prev.after.start_ms + m * MIN_MS
        c = prev_after_close
        rows.append({"symbol": "S", "ts_ms": ts, "open_u": c, "high_u": c,
                     "low_u": c, "close_u": c, "vol_qu": 1_000_000})
    # 평가일 봉은 **전부 같은 종가**다 — 그래야 윈도우 조건(+15%/30분)이 절대 걸리지 않고
    # 당일 조건(+30%)만 시험된다. 첫 봉의 시가만 day_open 으로 둔다(금지된 대체의 미끼).
    for m in range(5):
        ts = cur.regular.start_ms + m * MIN_MS
        rows.append({"symbol": "S", "ts_ms": ts,
                     "open_u": day_open if m == 0 else day_close, "high_u": day_close,
                     "low_u": min(day_open, day_close) if m == 0 else day_close,
                     "close_u": day_close, "vol_qu": 1_000_000})
    return _frame(rows), cal


def _on_day(ev: pd.DataFrame, md: UsMarketDay) -> pd.DataFrame:
    """그 매매일 안에 떨어진 이벤트만."""
    if ev.empty:
        return ev
    wins = synth.session_windows(md)
    lo, hi = wins[0][1].start_ms, wins[-1][1].end_ms
    return ev[(ev["t0_ms"] >= lo) & (ev["t0_ms"] < hi)]


def test_prev_close_uses_prior_regular_session_not_after_hours() -> None:
    """직전 매매일 **정규장** 마지막 봉을 쓴다 — 애프터장 봉이 아니다 (§2.3)."""
    # 정규장 종가 100, 애프터 종가 200. 평가일 종가 131 → 정규장 기준 +31% (검출),
    # 애프터 기준 -34% (미검출). 어느 쪽을 썼는지가 결과로 갈린다.
    df, cal = _two_day_frame(prev_regular_close=100, prev_after_close=200,
                             day_open=100, day_close=131)
    ev = L.detect_events(df, L.EventParams(), calendar=cal)
    cur = _on_day(ev, cal[1])
    assert len(cur) == 1, "정규장 종가(100) 기준으로 평가일에 당일 +30% 가 잡혀야 한다"
    assert cur.iloc[0]["kind"] in ("day", "both")
    assert _on_day(ev, cal[0]).empty, \
        "직전 매매일에는 이벤트가 없어야 한다 (당일 첫 시가 대체가 살아 있으면 생긴다)"


def test_prev_close_falls_back_to_last_bar_when_no_regular_session() -> None:
    """직전 매매일에 정규장 봉이 없으면 그날 마지막 봉 종가를 쓴다."""
    df, cal = _two_day_frame(prev_regular_close=100, prev_after_close=100,
                             day_open=100, day_close=131)
    df = df[~((df["ts_ms"] >= cal[0].regular.start_ms)
              & (df["ts_ms"] < cal[0].regular.end_ms))]        # 정규장 봉 제거
    ev = L.detect_events(df, L.EventParams(), calendar=cal)
    cur = _on_day(ev, cal[1])
    assert len(cur) == 1, "정규장 봉이 없으면 그날 마지막 봉(100) 종가를 쓴다"
    assert cur.iloc[0]["kind"] in ("day", "both")
    assert _on_day(ev, cal[0]).empty


def test_day_condition_skipped_when_no_prior_day_bars() -> None:
    """직전 매매일 봉이 아예 없으면 당일 조건을 **판정하지 않는다** (§2.3).

    당일 첫 시가로 대체하면 +31% 가 잡히므로, 잡히면 금지된 대체를 쓴 것이다.
    """
    df, cal = _two_day_frame(prev_regular_close=100, prev_after_close=100,
                             day_open=100, day_close=131)
    only_cur = df[df["ts_ms"] >= cal[1].day.start_ms]
    ev = L.detect_events(only_cur, L.EventParams(), calendar=cal)
    assert ev.empty, "당일 첫 시가 대체가 살아 있으면 여기서 이벤트가 잡힌다"


def test_window_condition_still_works_without_prev_close() -> None:
    """전일 종가가 없어도 윈도우 조건은 그대로 살아 kind='win' 으로 남는다 (§2.3)."""
    cal = synth.make_calendar(1, start="2026-06-01")
    reg = cal[0].regular
    rows = []
    for m in range(10):
        c = 100 if m < 5 else 116          # 30분 내 +16% (윈도우 조건 충족)
        rows.append({"symbol": "S", "ts_ms": reg.start_ms + m * MIN_MS, "open_u": 100,
                     "high_u": c, "low_u": 100, "close_u": c, "vol_qu": 1_000_000})
    ev = L.detect_events(_frame(rows), L.EventParams(), calendar=cal)
    assert len(ev) == 1
    assert ev.iloc[0]["kind"] == "win", "전일 종가 없이 'day'/'both' 가 나오면 안 된다"
    assert math.isnan(ev.iloc[0]["hod_ret"]), "전일 종가 기준 지표는 NaN 이어야 한다"


def test_explicit_prev_close_wins_over_chain() -> None:
    df, cal = _two_day_frame(prev_regular_close=100, prev_after_close=100,
                             day_open=100, day_close=131)
    ev = L.detect_events(df, L.EventParams(), calendar=cal, prev_close_u=1_000)
    assert ev.empty, "명시 인자가 사슬보다 우선해야 한다 (1000 기준이면 -87%)"


# =========================================================================== #
# §2.7 — 표본 필터
# =========================================================================== #
def _meta(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _events(specs: list[tuple[str, int]]) -> pd.DataFrame:
    return pd.DataFrame([{"symbol": s, "t0_ms": t, "kind": "win", "peak_ms": t,
                          "peak_ret": 0.2, "ret_30m": 0.1, "ret_close": 0.0,
                          "session": "regular", "rvol_gated": True}
                         for s, t in specs])


def _bars_at(specs: list[tuple[str, int, int]]) -> pd.DataFrame:
    return _frame([{"symbol": s, "ts_ms": t, "open_u": c, "high_u": c, "low_u": c,
                    "close_u": c, "vol_qu": 1_000_000} for s, t, c in specs])


def test_sample_filter_keeps_only_qualifying_events() -> None:
    """§2.7 세 조건을 전부 만족하는 이벤트만 남고 사유별로 카운트된다."""
    t = 1_780_000_000_000
    events = _events([("GOOD", t), ("ETF1", t), ("DEAD", t), ("CHEAP", t),
                      ("PRICEY", t), ("HUGE", t), ("NOMETA", t)])
    df_1m = _bars_at([
        ("GOOD", t, 5 * MICRO), ("ETF1", t, 5 * MICRO), ("DEAD", t, 5 * MICRO),
        ("CHEAP", t, 50_000), ("PRICEY", t, 25 * MICRO), ("HUGE", t, 5 * MICRO),
        ("NOMETA", t, 5 * MICRO),
    ])
    meta = _meta([
        # 5달러 x 1000만주 = 5천만달러 (범위 안)
        {"symbol": "GOOD", "security_type": "STOCK", "status": "ACTIVE",
         "is_common": 1, "shares_outstanding_qu": 10_000_000 * MICRO},
        {"symbol": "ETF1", "security_type": "ETF", "status": "ACTIVE",
         "is_common": 0, "shares_outstanding_qu": 10_000_000 * MICRO},
        {"symbol": "DEAD", "security_type": "STOCK", "status": "DELISTED",
         "is_common": 1, "shares_outstanding_qu": 10_000_000 * MICRO},
        {"symbol": "CHEAP", "security_type": "STOCK", "status": "ACTIVE",
         "is_common": 1, "shares_outstanding_qu": 10_000_000 * MICRO},
        {"symbol": "PRICEY", "security_type": "STOCK", "status": "ACTIVE",
         "is_common": 1, "shares_outstanding_qu": 10_000_000 * MICRO},
        # 5달러 x 2억주 = 10억달러 (시총 초과)
        {"symbol": "HUGE", "security_type": "STOCK", "status": "ACTIVE",
         "is_common": 1, "shares_outstanding_qu": 200_000_000 * MICRO},
    ])
    kept, reasons = E.apply_sample_filter(events, meta, df_1m=df_1m)
    assert list(kept["symbol"]) == ["GOOD"]
    by = dict(zip(reasons["reason"], reasons["n"]))
    assert by["not_common_stock"] == 1
    assert by["not_active"] == 1
    assert by["price_out_of_range"] == 2       # CHEAP($0.05), PRICEY($25)
    assert by["mcap_out_of_range"] == 1
    assert by["meta_missing"] == 1
    assert set(E.EXCLUSION_REASONS) <= set(reasons["reason"]), "0건 사유도 행으로 남는다"
    ev_rows = reasons[reasons["scope"] == "event"]
    assert set(ev_rows["reason"]) == set(E.EXCLUSION_REASONS)
    assert int(reasons["n_total"].iloc[0]) == 7
    assert int(reasons["n_kept"].iloc[0]) == 1


def test_sample_filter_price_boundaries_are_inclusive() -> None:
    t = 1_780_000_000_000
    events = _events([("LO", t), ("HI", t)])
    df_1m = _bars_at([("LO", t, E.SAMPLE_PRICE_MIN_U), ("HI", t, E.SAMPLE_PRICE_MAX_U)])
    # 경계 가격에서 시총이 범위에 들도록 주식수를 맞춘다
    meta = _meta([
        {"symbol": "LO", "security_type": "STOCK", "status": "ACTIVE", "is_common": 1,
         "shares_outstanding_qu": 1_000_000_000 * MICRO},     # $0.10 x 10억 = $1억
        {"symbol": "HI", "security_type": "FOREIGN_STOCK", "status": "ACTIVE",
         "is_common": 1, "shares_outstanding_qu": 5_000_000 * MICRO},   # $20 x 500만 = $1억
    ])
    kept, _r = E.apply_sample_filter(events, meta, df_1m=df_1m)
    assert set(kept["symbol"]) == {"LO", "HI"}, "경계값은 포함이다 ([$0.10, $20.00])"


def test_sample_filter_marks_missing_t0_price() -> None:
    """T0 종가를 못 구하면 검증 불가이므로 보수적으로 제외하고 사유를 남긴다."""
    t = 1_780_000_000_000
    events = _events([("GOOD", t)])
    meta = _meta([{"symbol": "GOOD", "security_type": "STOCK", "status": "ACTIVE",
                   "is_common": 1, "shares_outstanding_qu": 10_000_000 * MICRO}])
    kept, reasons = E.apply_sample_filter(events, meta, df_1m=None)
    assert kept.empty
    assert dict(zip(reasons["reason"], reasons["n"]))["t0_price_unavailable"] == 1


def test_sample_filter_counts_half_day_length_sample() -> None:
    """반일장(세션 길이 표본 부족)도 사유로 집계된다."""
    def one(date: str, base: int, reg_min: int) -> UsMarketDay:
        return UsMarketDay(date=date, day=None, pre=None,
                           regular=SessionWindow(start_ms=base,
                                                 end_ms=base + reg_min * MIN_MS),
                           after=None)
    cal, rows = [], []
    plan = [(f"2026-11-{d:02d}", 390) for d in range(2, 15)] + [("2026-11-27", 210)]
    for i, (date, reg) in enumerate(plan):
        base = i * DAY_MS
        cal.append(one(date, base, reg))
        for m in range(reg):
            rows.append({"symbol": "HD", "ts_ms": base + m * MIN_MS, "open_u": 5 * MICRO,
                         "high_u": 5 * MICRO, "low_u": 5 * MICRO, "close_u": 5 * MICRO,
                         "vol_qu": 1_000_000})
    df = _frame(rows)
    curve = B.minute_of_session_volume_curve(df, cal, min_days=B.PREREG_MIN_DAYS)
    assert ("regular", 210) in curve.attrs[B.CURVE_DROPPED_KEY]

    half_t0 = (len(plan) - 1) * DAY_MS + 100 * MIN_MS
    events = _events([("HD", half_t0)])
    meta = _meta([{"symbol": "HD", "security_type": "STOCK", "status": "ACTIVE",
                   "is_common": 1, "shares_outstanding_qu": 10_000_000 * MICRO}])
    kept, reasons = E.apply_sample_filter(events, meta, df_1m=df, curve=curve,
                                          calendar=cal)
    assert kept.empty
    assert dict(zip(reasons["reason"], reasons["n"]))["half_day_length_sample"] == 1


def test_sample_filter_empty_events() -> None:
    kept, reasons = E.apply_sample_filter(pd.DataFrame(), pd.DataFrame())
    assert kept.empty
    assert set(reasons["reason"]) == set(E.EXCLUSION_REASONS)
    assert (reasons["n"] == 0).all()


def test_run_all_exposes_sample_filter_section() -> None:
    t = 1_780_000_000_000
    events = _events([("GOOD", t), ("ETF1", t)])
    df_1m = _bars_at([("GOOD", t, 5 * MICRO), ("ETF1", t, 5 * MICRO)])
    meta = _meta([
        {"symbol": "GOOD", "security_type": "STOCK", "status": "ACTIVE", "is_common": 1,
         "shares_outstanding_qu": 10_000_000 * MICRO},
        {"symbol": "ETF1", "security_type": "ETF", "status": "ACTIVE", "is_common": 0,
         "shares_outstanding_qu": 10_000_000 * MICRO},
    ])
    res = E.run_all(events, pd.DataFrame(), pd.DataFrame(), df_1m, meta=meta)
    sf = res["sample_filter"]
    assert int(sf["n_kept"].iloc[0]) == 1
    assert dict(zip(sf["reason"], sf["n"]))["not_common_stock"] == 1
    # q1~q6 는 걸러진 표본으로 계산돼야 한다
    assert res["q3_daymarket_persistence"].loc[0, "n"] == 1.0


def test_run_all_without_meta_marks_filter_not_applied() -> None:
    """필터를 안 돌린 것과 제외 0건은 리포트에서 구분돼야 한다."""
    t = 1_780_000_000_000
    events = _events([("GOOD", t)])
    res = E.run_all(events, pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
    sf = res["sample_filter"]
    assert list(sf["reason"]) == ["(filter_not_applied)"]
    assert "적용하지 않았다" in sf["note"].iloc[0]


# =========================================================================== #
# §7-f / §2.6 — 경계 감도 의무 병기 + half_peak 판정 금지
# =========================================================================== #
def test_q6_reports_boundary_sensitivity(pytestconfig=None) -> None:
    """봉 타임스탬프 미확정 → Q6 버킷 경계 ±1분 감도를 **의무 병기**한다 (§7-f)."""
    t = 1_780_000_000_000
    rows = []
    for mfo in (0, 14, 15, 100):        # 14/15 는 경계(15분)에 걸쳐 있다
        rows.append({"symbol": f"S{mfo}", "t0_ms": t + mfo * MIN_MS, "kind": "win",
                     "peak_ms": t + mfo * MIN_MS, "peak_ret": 0.2, "ret_30m": 0.1,
                     "ret_close": 0.0, "session": "regular", "rvol_gated": True,
                     "hod_ms": t + mfo * MIN_MS, "t0_min_from_open": float(mfo),
                     "time_to_peak_min": 0.0, "outcome": "hold"})
    q6 = E.q6_time_of_day(pd.DataFrame(rows))
    cols = {"n_minus", "n_plus", "n_boundary_sensitive", "boundary_sensitive"}
    assert cols <= set(q6.columns), "±1분 감도 컬럼이 없으면 §7-f 미이행"
    first = q6[q6["bucket"] == "open_0_15"].iloc[0]
    assert first["n"] == 2.0                      # mfo 0, 14
    assert first["boundary_sensitive"], "경계에 걸친 이벤트가 있으면 감도 플래그가 켜져야 한다"
    assert first["n_boundary_sensitive"] >= 1
    # 경계에서 먼 이벤트만 있는 버킷은 감도 없음
    mid = q6[q6["bucket"] == "mid_30_180"].iloc[0]
    assert mid["n"] == 1.0 and not mid["boundary_sensitive"]


def test_half_peak_is_report_only_and_excluded_from_decisions() -> None:
    """§2.6: half_peak 은 실행 가능성 상한 보고 전용 — 판정·선택에 쓰지 않는다."""
    assert "half_peak" in E.EXIT_POLICIES, "보고에는 계속 나온다"
    assert "half_peak" not in E.DECISION_POLICIES, "판정용 정책 목록에 있으면 §2.6 위반"
    assert "half_peak" in E.REPORT_ONLY_POLICIES
    assert set(E.DECISION_POLICIES) | set(E.REPORT_ONLY_POLICIES) == set(E.EXIT_POLICIES)

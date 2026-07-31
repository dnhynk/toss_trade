"""캘린더 관련 오염 회귀 테스트 — 소유: W3.

감사(`docs/10_audit.md`) M-3·M-4 의 재현 시나리오를 그대로 테스트로 옮긴 것이다.
**두 결함은 사전등록(`docs/12`)의 백필 구간(2025-12-31 이전 train / 2026-01~04 val /
2026-05~07 holdout)에 겨울과 반일장이 대량 포함되므로 지금 당장 분석을 오염시킨다.**

    M-3  겨울(EST)에는 애프터장이 UTC 자정을 넘어 하나의 매매일이 두 UTC 날짜로 쪼개진다
         → `hist_days_available` 부풀림, former-runner 프록시 이중 계산
    M-4  반일장(조기폐장, 정규장 210분)이 정상일(390분)과 같은 `minute` 버킷을 공유하면
         종가 경매 스파이크가 정상일 분모를 오염시킨다 → `rvol_bar` 가 20~100배 왜곡

이 파일의 각 테스트는 **수정 전 구현에서 실패한다.**
"""
from __future__ import annotations

import math
import warnings

import pandas as pd
import pytest

from tests import synth
from tossmon.analysis import baselines as B
from tossmon.analysis import features as F
from tossmon.api.models import SessionWindow, UsMarketDay

MIN_MS = B.MIN_MS
DAY_MS = 86_400_000


def _bars(symbol: str, spec: list[tuple[int, int]]) -> pd.DataFrame:
    """(ts_ms, vol_qu) 목록 → 규약 준수 1분봉 (가격은 평평하게)."""
    return pd.DataFrame(
        [{"symbol": symbol, "ts_ms": t, "open_u": 1_000_000, "high_u": 1_050_000,
          "low_u": 950_000, "close_u": 1_000_000, "vol_qu": v} for t, v in spec]
    ).astype({"ts_ms": "int64", "open_u": "int64", "high_u": "int64",
              "low_u": "int64", "close_u": "int64", "vol_qu": "int64"})


def _fill_day(md: UsMarketDay, step: int = 7, vol: int = 10_000_000) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for _name, w in synth.session_windows(md):
        for m in range(0, (w.end_ms - w.start_ms) // MIN_MS, step):
            out.append((w.start_ms + m * MIN_MS, vol))
    return out


# =========================================================================== #
# M-3 — 겨울(EST) 매매일이 UTC 날짜로 갈라지는 문제
# =========================================================================== #
def test_winter_trading_day_spans_two_utc_dates() -> None:
    """전제 확인: EST 에서는 매매일이 UTC 자정을 넘는다 (여름에는 안 넘는다)."""
    summer = synth.make_calendar(1, start="2026-07-15", et_offset_h=-4)[0]
    winter = synth.make_calendar(1, start="2026-01-15", et_offset_h=-5)[0]

    def spans_one_utc_date(md: UsMarketDay) -> bool:
        wins = synth.session_windows(md)
        return wins[0][1].start_ms // DAY_MS == (wins[-1][1].end_ms - 1) // DAY_MS

    assert spans_one_utc_date(summer), "여름(EDT)은 UTC 날짜 하나에 들어간다"
    assert not spans_one_utc_date(winter), \
        "겨울(EST)은 UTC 자정을 넘는다 — 이것이 M-3 의 원인이다"


def test_history_features_group_by_market_day_not_utc_date() -> None:
    """겨울 하루치 봉인데 `hist_days_available` 가 2.0 이 되면 M-3 재발이다."""
    cal = synth.make_calendar(1, start="2026-01-15", et_offset_h=-5)
    md = cal[0]
    df = _bars("WIN", _fill_day(md))
    assert df["ts_ms"].floordiv(DAY_MS).nunique() == 2, "봉이 두 UTC 날짜에 걸쳐 있어야 한다"

    wins = synth.session_windows(md)
    t0 = wins[-1][1].end_ms + 20 * 60 * MIN_MS      # 다음날 시점에서 이력을 본다
    feats = F.extract_precursor_features(df, pd.DataFrame(), t0, calendar=cal,
                                         symbol="WIN")
    assert feats["hist_days_available"] == 1.0, "하나의 매매일은 하루로 세야 한다"
    assert feats["day_grouping_calendar"] == 1.0


def test_former_runner_proxy_not_double_counted_in_winter() -> None:
    """한 번의 급등이 겨울에 두 번으로 계산되면 former-runner 축이 오염된다."""
    cal = synth.make_calendar(2, start="2026-01-15", et_offset_h=-5)
    spec = _fill_day(cal[0])
    rows = []
    for i, (ts, vol) in enumerate(spec):
        # 이력일(cal[0]) 안에서 저가→고가 +40% 를 만든다 (프록시 임계 15% 초과)
        hi = 1_400_000 if i >= len(spec) // 2 else 1_000_000
        rows.append({"symbol": "WIN", "ts_ms": ts, "open_u": 1_000_000,
                     "high_u": hi, "low_u": 1_000_000, "close_u": hi, "vol_qu": vol})
    # 컷오프가 **다음 매매일**에 놓이도록 평범한 하루를 덧붙인다
    for ts, vol in _fill_day(cal[1]):
        rows.append({"symbol": "WIN", "ts_ms": ts, "open_u": 1_400_000,
                     "high_u": 1_400_000, "low_u": 1_400_000, "close_u": 1_400_000,
                     "vol_qu": vol})
    df = pd.DataFrame(rows).astype({"ts_ms": "int64"})

    t0 = synth.session_windows(cal[1])[-1][1].end_ms
    feats = F.extract_precursor_features(df, pd.DataFrame(), t0, calendar=cal,
                                         symbol="WIN")
    assert feats["prior_event_count_20d"] == 1.0, \
        "겨울 매매일이 둘로 쪼개지면 한 번의 급등이 2.0 으로 이중 계산된다"
    assert feats["former_runner"] == 1.0


def test_utc_fallback_is_flagged_and_warns() -> None:
    """캘린더 없이 부르면 조용히 틀리지 말고 **드러내야** 한다."""
    cal = synth.make_calendar(1, start="2026-01-15", et_offset_h=-5)
    df = _bars("WIN", _fill_day(cal[0]))
    t0 = synth.session_windows(cal[0])[-1][1].end_ms + 20 * 60 * MIN_MS

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        feats = F.extract_precursor_features(df, pd.DataFrame(), t0, symbol="WIN")
    assert feats["day_grouping_calendar"] == 0.0, "폴백 사실이 결과에 드러나야 한다"
    assert any(issubclass(w.category, RuntimeWarning) for w in caught)
    assert any("M-3" in str(w.message) for w in caught)


def test_summer_unaffected_by_the_fix() -> None:
    """여름에는 원래 맞았으므로 값이 달라지면 안 된다 (회귀 방지)."""
    cal = synth.make_calendar(1, start="2026-07-15", et_offset_h=-4)
    df = _bars("SUM", _fill_day(cal[0]))
    t0 = synth.session_windows(cal[0])[-1][1].end_ms + 20 * 60 * MIN_MS
    with_cal = F.extract_precursor_features(df, pd.DataFrame(), t0, calendar=cal,
                                            symbol="SUM")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        without = F.extract_precursor_features(df, pd.DataFrame(), t0, symbol="SUM")
    assert with_cal["hist_days_available"] == 1.0
    assert without["hist_days_available"] == 1.0, "여름은 UTC 폴백도 같은 답을 낸다"


def test_market_day_span_helpers() -> None:
    cal = synth.make_calendar(3, start="2026-01-15", et_offset_h=-5)
    spans = F.market_day_spans(cal)
    assert len(spans) == 3
    assert [s[2] for s in spans] == [md.date for md in cal]
    for lo, hi, _d in spans:
        assert hi > lo
    mid = (spans[1][0] + spans[1][1]) // 2
    assert F.assign_market_days([spans[0][0], mid, spans[2][1] + DAY_MS],
                                spans) == [spans[0][2], spans[1][2], None]
    assert F.assign_market_days([1, 2], []) == [None, None]


def test_holiday_days_are_skipped_in_spans() -> None:
    holiday = UsMarketDay(date="2026-01-19", day=None, pre=None, regular=None,
                          after=None)
    cal = [holiday] + synth.make_calendar(1, start="2026-01-20", et_offset_h=-5)
    spans = F.market_day_spans(cal)
    assert [s[2] for s in spans] == ["2026-01-20"], "휴장일은 매매일 span 이 없다"


# =========================================================================== #
# M-4 — 반일장(조기폐장)이 분-of-session 곡선을 오염시키는 문제
# =========================================================================== #
def _half_day_fixture() -> tuple[pd.DataFrame, list[UsMarketDay]]:
    """정상일 3일(390분) + 반일장 1일(210분, 종가 경매 100배 스파이크)."""
    def one(date: str, base: int, reg_min: int) -> UsMarketDay:
        return UsMarketDay(date=date, day=None, pre=None,
                           regular=SessionWindow(start_ms=base,
                                                 end_ms=base + reg_min * MIN_MS),
                           after=None)
    rows, cal = [], []
    plan = [("2026-11-24", 390), ("2026-11-25", 390), ("2026-11-26", 390),
            ("2026-11-27", 210)]                      # 추수감사절 다음날 = 반일장
    for i, (date, reg) in enumerate(plan):
        base = i * DAY_MS
        cal.append(one(date, base, reg))
        for m in range(reg):
            spike = (reg == 210 and m == reg - 1)     # 반일장 종가 경매
            rows.append((base + m * MIN_MS, 10_000_000_000 if spike else 100_000_000))
    return _bars("HD", rows), cal


def test_half_day_does_not_contaminate_normal_day_curve() -> None:
    """반일장 종가 스파이크가 정상일 minute 209 분모를 부풀리면 M-4 재발이다."""
    df, cal = _half_day_fixture()
    curve = B.minute_of_session_volume_curve(df, cal)
    assert curve.loc[("regular", 390, 209)] == pytest.approx(100_000_000.0), \
        "정상일 분모에 반일장이 섞이면 안 된다"
    assert B.rvol_bar(df, curve, 209 * MIN_MS) == pytest.approx(1.0), \
        "정상일 minute 209 의 rvol_bar 는 1.0 이어야 한다 (오염 시 0.04 로 죽는다)"


def test_half_day_close_is_not_a_false_burst() -> None:
    """반일장 종가를 정상일 분모로 보면 허위 버스트(100배)가 만들어진다."""
    df, cal = _half_day_fixture()
    curve = B.minute_of_session_volume_curve(df, cal)
    half_close = 3 * DAY_MS + 209 * MIN_MS
    rv = B.rvol_bar(df, curve, half_close)
    assert rv == pytest.approx(1.0), \
        "반일장은 자기 길이 버킷을 쓰므로 종가가 허위 버스트가 되지 않는다"


def test_session_length_is_part_of_the_curve_key() -> None:
    df, cal = _half_day_fixture()
    curve = B.minute_of_session_volume_curve(df, cal)
    assert curve.index.names == ["session", "session_len_min", "minute"]
    lengths = curve.attrs[B.CURVE_LENGTHS_KEY]["regular"]
    assert lengths == {390: 3, 210: 1}, "반일장이 분리됐다는 사실과 건수가 드러나야 한다"
    assert ("regular", 210, 209) in curve.index
    assert ("regular", 390, 209) in curve.index
    assert ("regular", 210, 300) not in curve.index, "반일장에는 minute 300 이 없다"


def test_min_days_drops_sparse_length_buckets() -> None:
    """관측이 부족한 길이(반일장)는 버려져 RVOL 미가용 → 게이트가 자동으로 닫힌다."""
    df, cal = _half_day_fixture()
    curve = B.minute_of_session_volume_curve(df, cal, min_days=3)
    assert ("regular", 210, 209) not in curve.index
    assert ("regular", 390, 209) in curve.index, "정상일 버킷은 3일 관측이라 남는다"
    assert ("regular", 210) in curve.attrs[B.CURVE_DROPPED_KEY]
    half_close = 3 * DAY_MS + 209 * MIN_MS
    assert math.isnan(B.rvol_bar(df, curve, half_close))
    assert math.isnan(B.rvol(df, curve, half_close))


def test_cumulative_rvol_also_length_aware() -> None:
    """누적 게이트(`rvol_series`)도 같은 키를 써야 한다."""
    df, cal = _half_day_fixture()
    curve = B.minute_of_session_volume_curve(df, cal, min_days=3)
    rv = B.rvol_series(df, curve)
    normal = rv[(rv.index >= 0) & (rv.index < DAY_MS)].dropna()
    assert len(normal) == 390
    assert normal.iloc[-1] == pytest.approx(1.0, abs=0.01)
    half = rv[rv.index >= 3 * DAY_MS]
    assert half.isna().all(), "관측 부족한 반일장은 누적 RVOL 도 미가용"


def test_curve_key_helper() -> None:
    df, cal = _half_day_fixture()
    curve = B.minute_of_session_volume_curve(df, cal)
    assert B.curve_key(curve, 209 * MIN_MS) == ("regular", 390, 209)
    assert B.curve_key(curve, 3 * DAY_MS + 209 * MIN_MS) == ("regular", 210, 209)
    assert B.curve_key(curve, 99 * DAY_MS) is None


def test_expected_window_vol_uses_length_aware_key() -> None:
    """features 의 기대거래량 합도 길이 인지 키를 써야 한다."""
    df, cal = _half_day_fixture()
    curve = B.minute_of_session_volume_curve(df, cal)
    t0 = 210 * MIN_MS                       # 정상일 장중
    feats = F.extract_precursor_features(df, pd.DataFrame(), t0, curve=curve,
                                         calendar=cal, symbol="HD")
    assert feats["rvol_curve_30"] == pytest.approx(1.0, abs=0.01), \
        "정상일 기대거래량이 반일장 스파이크로 오염되면 1.0 에서 벗어난다"


# =========================================================================== #
# synth 의 겨울/반일장 캘린더 지원
# =========================================================================== #
def test_synth_half_day_calendar() -> None:
    cal = synth.make_calendar(3, start="2026-11-25", half_days=["2026-11-27"])
    by_date = {md.date: md for md in cal}
    normal = by_date["2026-11-25"].regular
    half = by_date["2026-11-27"].regular
    assert (normal.end_ms - normal.start_ms) // MIN_MS == 390
    assert (half.end_ms - half.start_ms) // MIN_MS == 210, "반일장 정규장은 210분"
    # 애프터장이 앞당겨지고 세션은 여전히 겹치지 않는다
    after = by_date["2026-11-27"].after
    assert after.start_ms == half.end_ms
    for md in cal:
        wins = synth.session_windows(md)
        for (_na, a), (_nb, b) in zip(wins[:-1], wins[1:]):
            assert a.end_ms <= b.start_ms


def test_synth_winter_scenario_runs_end_to_end() -> None:
    """겨울 오프셋으로도 시나리오 생성·검출이 정상 동작해야 한다."""
    df, truth = synth.make_scenario("coil_pop", seed=1, et_offset_h=-5,
                                    cal_start="2026-01-12")
    assert truth["t0_expected_ms"] is not None
    curve = B.minute_of_session_volume_curve(df, truth["baseline_calendar"])
    feats = F.extract_precursor_features(df, truth["rankings"],
                                         truth["t0_expected_ms"], curve=curve,
                                         calendar=truth["calendar"],
                                         symbol=truth["symbol"])
    assert feats["day_grouping_calendar"] == 1.0
    assert feats["hist_days_available"] == float(truth["history_days"]) + 1.0

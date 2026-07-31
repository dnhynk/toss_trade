"""W1 실측/픽스처 응답 형태에 대한 재검증 + 대용량 성능 — 소유: W3.

**값이 아니라 스키마에만 의존한다** (픽스처는 라이브 캡처본으로 교체될 예정이며,
가격·거래량 값을 하드코딩하면 교체 시 깨진다). 여기서 검증하는 것은:

1. `/candles` 는 **최신순(newest-first)** 으로 오고, 분석 함수들이 그 순서에서도 옳게 동작하는가
2. ISO 8601(+09:00) → `ts_ms` 변환 후 4세션 경계가 실제 캘린더 응답과 맞는가
3. 조기폐장/휴장(4세션 전부 null)에서 죽지 않는가
4. `/rankings` 2종(MARKET/TOSS)에서 토스 쏠림도가 계산되는가 (비율이 1을 넘어도 허용)
5. 1024일 백필 규모(수만~수십만 봉)에서 파이프라인이 선형적으로 동작하는가

픽스처는 W1 소유이며 이 브랜치에 커밋되지 않는다 — 없으면 skip 한다.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from tests import synth
from tossmon.analysis import baselines as B
from tossmon.analysis import evaluate as E
from tossmon.analysis import features as F
from tossmon.analysis import labeling as L
from tossmon.api.models import MICRO, SessionWindow, UsMarketDay

FIXTURES = Path(__file__).parent / "fixtures" / "live"
MIN_MS = B.MIN_MS


def _load(name: str) -> dict:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"W1 픽스처 없음: {path.name} (로컬 개발용, 이 브랜치에 커밋되지 않음)")
    return json.loads(path.read_text(encoding="utf-8"))


def _iso_ms(s: str) -> int:
    """테스트 자체 파서 — W1 의 iso_to_ms 구현에 의존하지 않는다."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    assert dt.tzinfo is not None, "오프셋 없는 naive 시각은 계약 C-1 위반"
    return int(dt.timestamp() * 1000)


def _u(s: str) -> int:
    return int((Decimal(str(s)) * MICRO).to_integral_value())


def _candles_df(fixture: str, symbol: str = "FIX") -> pd.DataFrame:
    body = _load(fixture)["body"]["result"]["candles"]
    rows = [{"symbol": symbol, "ts_ms": _iso_ms(c["timestamp"]),
             "open_u": _u(c["openPrice"]), "high_u": _u(c["highPrice"]),
             "low_u": _u(c["lowPrice"]), "close_u": _u(c["closePrice"]),
             "vol_qu": _u(c["volume"])} for c in body]
    return pd.DataFrame(rows).astype({"ts_ms": "int64", "open_u": "int64",
                                      "high_u": "int64", "low_u": "int64",
                                      "close_u": "int64", "vol_qu": "int64"})


def _window(d: dict | None) -> SessionWindow | None:
    if not d:
        return None
    return SessionWindow(start_ms=_iso_ms(d["startTime"]), end_ms=_iso_ms(d["endTime"]))


def _market_day(d: dict) -> UsMarketDay:
    return UsMarketDay(date=d["date"], day=_window(d.get("dayMarket")),
                       pre=_window(d.get("preMarket")),
                       regular=_window(d.get("regularMarket")),
                       after=_window(d.get("afterMarket")))


def _calendar(fixture: str = "market_calendar_us.json") -> list[UsMarketDay]:
    res = _load(fixture)["body"]["result"]
    keys = [k for k in ("previousBusinessDay", "today", "nextBusinessDay") if k in res]
    return sorted((_market_day(res[k]) for k in keys), key=lambda m: m.date)


# --------------------------------------------------------------------------- #
# 1. 최신순 정렬 내성
# --------------------------------------------------------------------------- #
def test_candles_fixture_is_newest_first() -> None:
    """전제 확인: API 는 최신순으로 준다 (분석 함수는 스스로 정렬해야 한다)."""
    df = _candles_df("candles_1m_us.json")
    assert len(df) > 1
    assert df["ts_ms"].iloc[0] > df["ts_ms"].iloc[-1], "픽스처가 최신순이 아니다"


def test_baselines_are_order_independent() -> None:
    df_1d = _candles_df("candles_1d_us.json")
    asc = B.compute_daily_baseline(df_1d.sort_values("ts_ms"))
    as_given = B.compute_daily_baseline(df_1d)
    assert asc == as_given, "최신순 입력에서 베이스라인이 달라지면 정렬 누락"


def test_session_vwap_order_independent() -> None:
    df = _candles_df("candles_1m_us.json")
    win = SessionWindow(start_ms=int(df["ts_ms"].min()),
                        end_ms=int(df["ts_ms"].max()) + MIN_MS)
    a = B.session_vwap_u(df, win)
    b = B.session_vwap_u(df.sort_values("ts_ms"), win)
    pd.testing.assert_series_equal(a, b)
    assert a.index.is_monotonic_increasing


def test_detect_events_order_independent() -> None:
    df = _candles_df("candles_1m_us.json")
    prev = int(df["close_u"].iloc[-1])
    a = L.detect_events(df, L.EventParams(), prev_close_u=prev)
    b = L.detect_events(df.sort_values("ts_ms"), L.EventParams(), prev_close_u=prev)
    pd.testing.assert_frame_equal(a, b)


def test_true_range_needs_sorted_input_and_atr_sorts_it() -> None:
    df = _candles_df("candles_1d_us.json")
    assert B.atr_u(df) == B.atr_u(df.sort_values("ts_ms"))


# --------------------------------------------------------------------------- #
# 2. 실제 캘린더 응답
# --------------------------------------------------------------------------- #
def test_real_calendar_session_layout() -> None:
    cal = _calendar()
    assert cal
    for md in cal:
        wins = B.session_windows(md)
        assert [n for n, _w in wins] == ["day", "pre", "regular", "after"]
        for _n, w in wins:
            assert w.end_ms > w.start_ms
        for (_na, a), (_nb, b) in zip(wins[:-1], wins[1:]):
            assert a.end_ms <= b.start_ms, "세션이 겹치면 안 된다"
    # 정규장은 6.5시간 (조기폐장 아닌 날)
    reg = cal[-1].regular
    assert (reg.end_ms - reg.start_ms) // MIN_MS in (390, 210)


def test_real_calendar_market_day_fits_one_utc_date() -> None:
    """docs/07 §3.1 의 전제: 4세션 전체가 하루 UTC 날짜 안에 들어간다."""
    for md in _calendar():
        wins = B.session_windows(md)
        lo, hi = wins[0][1].start_ms, wins[-1][1].end_ms
        assert lo // 86_400_000 == (hi - 1) // 86_400_000, \
            f"{md.date}: 매매일이 UTC 날짜를 넘어간다 — calendar 없는 경로의 전제가 깨진다"


def test_holiday_calendar_all_sessions_none() -> None:
    cal = _calendar("market_calendar_us_holiday.json")
    holidays = [md for md in cal if B.session_windows(md) == []]
    assert holidays, "휴장일 픽스처에는 4세션 전부 null 인 날이 있어야 한다"
    df = _candles_df("candles_1m_us.json")
    curve = B.minute_of_session_volume_curve(df, cal)      # 죽지 않아야 한다
    assert isinstance(curve, pd.Series)
    ev = L.detect_events(df, L.EventParams(), calendar=cal,
                         prev_close_u=int(df["close_u"].iloc[-1]))
    assert isinstance(ev, pd.DataFrame)


def test_curve_and_rvol_on_real_calendar() -> None:
    cal = _calendar()
    df = _candles_df("candles_1m_us.json")
    curve = B.minute_of_session_volume_curve(df, cal)
    rv = B.rvol_series(df, curve, calendar=cal)
    assert len(rv) == len(df)
    inside = rv.dropna()
    if len(inside):
        assert (inside > 0).all()


# --------------------------------------------------------------------------- #
# 3. 랭킹 2종 → 토스 쏠림도
# --------------------------------------------------------------------------- #
def _rankings_df(fixture: str, ranking_type: str) -> pd.DataFrame:
    res = _load(fixture)["body"]["result"]
    snap = _iso_ms(res["rankedAt"])
    rows = [{"snap_ms": snap, "ranking_type": ranking_type, "duration": "realtime",
             "rank": int(r["rank"]), "symbol": r["symbol"],
             "last_u": _u(r["price"]["lastPrice"]), "vol_qu": _u(r["tradingVolume"]),
             "amount_u": _u(r["tradingAmount"])} for r in res["rankings"]]
    return pd.DataFrame(rows).astype({"snap_ms": "int64", "rank": "int64",
                                      "last_u": "int64", "vol_qu": "int64",
                                      "amount_u": "int64"})


def test_ranking_fixture_schema_matches_our_convention() -> None:
    rk = _rankings_df("rankings_market_amount_realtime.json", "MARKET_TRADING_AMOUNT")
    assert list(rk.columns) == synth.RANKING_COLS
    assert (rk["rank"] >= 1).all()
    assert rk["amount_u"].max() > 0


def test_toss_concentration_from_real_ranking_shapes() -> None:
    """TOSS/MARKET 비율 계산이 실제 응답 형태에서 동작. 값(1 초과 포함)은 검증하지 않는다."""
    mkt = _rankings_df("rankings_market_amount_realtime.json",
                       "MARKET_TRADING_AMOUNT")
    toss = _rankings_df("rankings_toss_amount_realtime.json",
                        "TOSS_SECURITIES_TRADING_AMOUNT")
    snap = int(min(mkt["snap_ms"].min(), toss["snap_ms"].min()))
    toss = toss.assign(snap_ms=snap)
    mkt = mkt.assign(snap_ms=snap)
    rk = pd.concat([mkt, toss], ignore_index=True)

    symbol = str(toss["symbol"].iloc[0])
    df = _candles_df("candles_1m_us.json", symbol=symbol)
    t0 = snap + 5 * MIN_MS
    feats = F.extract_precursor_features(df, rk, t0, symbol=symbol)
    assert feats["toss_in_ranking"] == 1.0
    assert feats["ranking_snaps_pre"] >= 2
    assert feats["toss_share"] > 0, "쏠림도는 계산돼야 한다 (1 초과 여부는 데이터 문제)"
    assert feats["toss_rank_best"] >= 1
    assert feats["market_rank_best"] >= 1


def test_empty_rankings_fixture_is_handled() -> None:
    res = _load("rankings_empty.json")["body"]["result"]
    rows = res.get("rankings", [])
    assert rows == [], "빈 랭킹 픽스처 전제"
    rk = pd.DataFrame(columns=synth.RANKING_COLS)
    q2 = E.q2_ranking_lead_lag(pd.DataFrame(columns=L.EVENT_COLUMNS), rk)
    assert q2.loc[0, "verdict"] == "unavailable"


def test_empty_candles_fixture_is_handled() -> None:
    body = _load("candles_empty.json")["body"]["result"]["candles"]
    assert body == []
    df = pd.DataFrame(columns=synth.CANDLE_COLS)
    assert B.compute_daily_baseline(df)["n_days"] == 0
    assert L.detect_events(df, L.EventParams()).empty
    f = F.extract_precursor_features(df, pd.DataFrame(), 1_780_000_000_000)
    assert f["n_bars_pre"] == 0.0


# --------------------------------------------------------------------------- #
# 4. 대용량(1024일 백필) 성능 — A2 §4
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("history_days", [40])
def test_pipeline_scales_to_backfill_volume(history_days: int) -> None:
    """수만 봉에서 전 파이프라인이 초 단위로 끝나야 한다 (2차 복잡도 방지).

    1분봉 보관이 1024일이므로 실제 입력은 수십만 봉이 된다. 여유 있는 절대 상한만 두어
    머신 부하에 흔들리지 않게 하되, 2차 복잡도가 들어오면 반드시 걸리도록 한다.
    """
    df, truth = synth.make_scenario("coil_pop", seed=1, history_days=history_days)
    assert len(df) > 40_000, "성능 테스트가 충분히 큰 입력을 쓰는지 확인"

    t = time.perf_counter()
    curve = B.minute_of_session_volume_curve(df, truth["baseline_calendar"])
    rv = B.rvol_series(df, curve, calendar=truth["calendar"])
    events = L.detect_events(df, L.EventParams(), calendar=truth["calendar"],
                             rvol_series=rv, prev_close_u={(truth["symbol"], truth["market_day"].date):
                                       truth["prev_close_u"]},
                             shares_outstanding_qu=truth["shares_outstanding_qu"])
    feats = F.extract_precursor_features(df, truth["rankings"],
                                         truth["t0_expected_ms"], curve=curve,
                                         calendar=truth["calendar"],
                                         symbol=truth["symbol"])
    elapsed = time.perf_counter() - t

    assert not events.empty
    assert feats["n_bars_pre"] > 0
    assert elapsed < 30.0, f"{len(df)} 봉 처리에 {elapsed:.1f}s — 2차 복잡도 의심"


def test_detect_events_cost_grows_subquadratically() -> None:
    """매매일 수가 2배가 되어도 시간이 4배가 되지 않아야 한다 (일×봉 이중 스캔 방지)."""
    def run(days: int) -> float:
        df, truth = synth.make_scenario("noise", seed=1, history_days=days)
        t = time.perf_counter()
        L.detect_events(df, L.EventParams(), calendar=truth["calendar"],
                        prev_close_u={(truth["symbol"], truth["market_day"].date):
                                       truth["prev_close_u"]})
        return time.perf_counter() - t

    small = run(10)
    large = run(40)
    assert large < max(1.0, small * 12.0), f"small={small:.3f}s large={large:.3f}s"

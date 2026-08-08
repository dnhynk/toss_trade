"""회전 인지형 전조 측정식 테스트 — tossmon/analysis/rotation.py (docs/17).

핵심은 **구 측정식의 실패 양식이 재현되지 않음**을 회귀로 못박는 것이다:
무체결 분을 0 으로 채우지 않고, 척도(밀도)를 바꿔도 값이 변하지 않으며,
정의되지 않으면 NaN 을 돌려준다.
"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from tossmon.analysis import rotation as R

MIN_MS = R.MIN_MS


def bars(ts_min, vols, *, close_u=10_000, symbol="AAA"):
    """분 오프셋 목록으로 1분봉 프레임을 만든다 (ts=0 기준)."""
    return pd.DataFrame({
        "symbol": [symbol] * len(ts_min),
        "ts_ms": [int(t) * MIN_MS for t in ts_min],
        "close_u": [close_u] * len(ts_min),
        "vol_qu": list(vols),
    })


# --------------------------------------------------------------------------- #
# 금지 규칙 1 — 결측을 값으로 채우지 않는다
# --------------------------------------------------------------------------- #
def test_print_frame_drops_zero_volume_bars_and_never_fills_gaps():
    df = bars([0, 1, 2, 3, 4], [10, 0, 0, 5, 0])
    p = R.print_frame(df)
    assert p["ts_ms"].tolist() == [0, 3 * MIN_MS]
    # 사이의 무체결 분이 0 행으로 들어오지 않는다
    assert len(p) == 2


def test_print_frame_observable_cutoff_is_inclusive():
    """계약 A2 §1: `t_to` 는 포함적이다 — 라벨 `t_to` 봉의 내용은 전부 `t_to` 이전이다.

    (A2) 이 파일에서 **기대값이 실제로 바뀐 유일한 곳**이다. 함수 계약 자체가 개정됐고
    옛 이름(`strict_cutoff_is_exclusive`)이 옛 계약을 선언하고 있었다.
    절대 시각으로 고정한 판본은 `test_cutoff_amendment_a2.py` 에 있다.
    """
    df = bars([0, 1, 2], [1, 1, 1])
    p = R.print_frame(df, t_to=2 * MIN_MS)
    assert p["ts_ms"].tolist() == [0, MIN_MS, 2 * MIN_MS]


def test_print_frame_amount_uses_python_ints_no_overflow():
    # int64 를 넘기는 크기 (구 VWAP 결함의 회귀)
    df = bars([0, 1], [10**12, 10**12], close_u=10**8)
    p = R.print_frame(df)
    assert p["amount_u12"].tolist() == [10**20, 10**20]


# --------------------------------------------------------------------------- #
# 금지 규칙 2 — 사건 시간, 척도 불변
# --------------------------------------------------------------------------- #
def test_print_intensity_ratio_detects_acceleration():
    # 기준 20개는 10분 간격, 최근 5개는 1분 간격 -> 약 10배 가속
    ts = list(range(0, 210, 10)) + [210, 211, 212, 213, 214]
    p = R.print_frame(bars(ts, [5] * len(ts)))
    pir = R.print_intensity_ratio(p, recent_prints=5, baseline_prints=20)
    assert pir == pytest.approx(10.0, rel=0.2)
    assert pir > 1.0


def test_print_intensity_ratio_is_scale_invariant_in_density():
    """같은 가속 패턴이면 테이프가 10배 희소해도 값이 같아야 한다.

    이것이 구 측정식이 실패한 지점의 정확한 반대 조건이다.
    """
    dense = list(range(0, 21)) + [21, 22, 23, 24, 25]
    sparse = [t * 10 for t in dense]
    p_d = R.print_frame(bars(dense, [5] * len(dense)))
    p_s = R.print_frame(bars(sparse, [5] * len(sparse)))
    a = R.print_intensity_ratio(p_d, recent_prints=5, baseline_prints=20)
    b = R.print_intensity_ratio(p_s, recent_prints=5, baseline_prints=20)
    assert a == pytest.approx(b)


def test_print_size_ratio_is_scale_invariant_in_volume_units():
    ts = list(range(30))
    vols = [10] * 25 + [100] * 5
    a = R.print_size_ratio(R.print_frame(bars(ts, vols)),
                           recent_prints=5, baseline_prints=20)
    b = R.print_size_ratio(R.print_frame(bars(ts, [v * 1000 for v in vols])),
                           recent_prints=5, baseline_prints=20)
    assert a == pytest.approx(b) and a == pytest.approx(10.0)


def test_dormancy_wake_ratio_flags_long_silence():
    ts = list(range(0, 22)) + [81]          # 마지막 프린트 전 60분 침묵
    p = R.print_frame(bars(ts, [5] * len(ts)))
    assert R.dormancy_wake_ratio(p, baseline_prints=20) == pytest.approx(60.0)


# --------------------------------------------------------------------------- #
# 금지 규칙 3 — 정의되지 않으면 NaN
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fn", [R.print_intensity_ratio, R.print_size_ratio])
def test_event_time_measures_return_nan_when_too_few_prints(fn):
    p = R.print_frame(bars([0, 1, 2], [1, 1, 1]))
    assert math.isnan(fn(p, recent_prints=5, baseline_prints=20))


def test_measures_return_nan_on_empty_frame():
    empty = R.print_frame(pd.DataFrame())
    assert math.isnan(R.print_intensity_ratio(empty))
    assert math.isnan(R.dormancy_wake_ratio(empty))
    assert math.isnan(R.print_size_ratio(empty))


def test_print_size_ratio_nan_rather_than_divide_by_zero():
    ts = list(range(30))
    p = R.print_frame(bars(ts, [0] * 25 + [7] * 5))   # 기준 블록이 전부 무체결
    assert math.isnan(R.print_size_ratio(p, recent_prints=5, baseline_prints=20))


# --------------------------------------------------------------------------- #
# 횡단면 회전
# --------------------------------------------------------------------------- #
def _cohort_day(n_symbols=12, *, mover="MOVE"):
    """직전 창과 최근 창이 같은 배경 종목들 + 최근 창에서만 튀는 종목 하나."""
    # 봉 라벨은 **종료 시각**이다 (docs/12 §6.1) — 라벨 m 인 봉은 분 m-1 을 담는다.
    # 그래서 분 0..59 를 채우려면 라벨은 1..60 이어야 하고, 그래야 급증(분 30~59)이
    # 최근 창 [30분, 60분) 안에만 들어간다.
    frames = []
    for i in range(n_symbols):
        sym = f"BG{i:02d}"
        ts = list(range(1, 61))
        frames.append(bars(ts, [10] * 60, symbol=sym))
    ts = list(range(1, 61))
    vols = [10] * 30 + [500] * 30            # 최근 창에서만 급증
    frames.append(bars(ts, vols, symbol=mover))
    return pd.concat(frames, ignore_index=True)


def test_rotation_scores_ranks_the_mover_top():
    day = _cohort_day()
    sc = R.rotation_scores(day, 60 * MIN_MS, window_min=30, min_prints=3,
                           min_cohort=5)
    assert not sc.empty
    top = sc.sort_values("share_delta", ascending=False).iloc[0]
    assert top["symbol"] == "MOVE"
    assert top["share_delta_pct"] == pytest.approx(1.0)
    assert top["rank_delta"] > 0            # 순위가 올라갔다


def test_rotation_scores_excludes_symbols_missing_from_either_window():
    """한쪽 창에만 관측된 종목은 코호트에서 빠진다 (0 으로 채우지 않는다)."""
    day = _cohort_day()
    late = bars(list(range(31, 61)), [999] * 30, symbol="LATE")  # 최근 창에만 존재
    day = pd.concat([day, late], ignore_index=True)
    sc = R.rotation_scores(day, 60 * MIN_MS, window_min=30, min_prints=3,
                           min_cohort=5)
    assert "LATE" not in set(sc["symbol"])


def test_rotation_scores_returns_empty_below_min_cohort():
    day = _cohort_day(n_symbols=2)
    sc = R.rotation_scores(day, 60 * MIN_MS, window_min=30, min_cohort=10)
    assert sc.empty
    assert list(sc.columns) == list(R.ROTATION_COLUMNS)


def test_rotation_score_at_is_nan_for_symbol_outside_cohort():
    day = _cohort_day()
    assert math.isnan(R.rotation_score_at(day, "NOPE", 60 * MIN_MS,
                                          window_min=30, min_cohort=5))


def test_rotation_percentile_is_invariant_to_a_symbols_own_density():
    """코호트 백분위는 종목 자신의 절대 밀도가 아니라 **동료 대비 변화**만 본다."""
    day = _cohort_day()
    a = R.rotation_score_at(day, "MOVE", 60 * MIN_MS, metric="rank_delta_pct",
                            window_min=30, min_prints=3, min_cohort=5)
    # 같은 상대 패턴을 유지한 채 전 종목의 거래량을 100배로
    day2 = day.copy()
    day2["vol_qu"] = day2["vol_qu"] * 100
    b = R.rotation_score_at(day2, "MOVE", 60 * MIN_MS, metric="rank_delta_pct",
                            window_min=30, min_prints=3, min_cohort=5)
    assert a == pytest.approx(b)


def test_rotation_score_series_respects_strict_cutoff():
    day = _cohort_day()
    s = R.rotation_score_series(day, "MOVE", 60 * MIN_MS, scan_min=20, step_min=5,
                                window_min=30, min_prints=3, min_cohort=5)
    assert len(s) > 0
    assert max(s.index) < 60 * MIN_MS


def test_first_cross_lead_min_and_nan_when_never_crossed():
    idx = [t * MIN_MS for t in (0, 10, 20)]
    s = pd.Series([0.1, 0.95, 0.99], index=idx)
    assert R.first_cross_lead_min(s, 30 * MIN_MS, threshold=0.9) == 20.0
    flat = pd.Series([0.1, 0.2], index=idx[:2])
    assert math.isnan(R.first_cross_lead_min(flat, 30 * MIN_MS, threshold=0.9))


# --------------------------------------------------------------------------- #
# 실시간 전용 축
# --------------------------------------------------------------------------- #
def test_ranking_top_changes_detects_enter_and_exit():
    rk = pd.DataFrame({
        "snap_ms": [0, 0, 1000, 1000],
        "ranking_type": ["T"] * 4,
        "rank": [1, 2, 1, 2],
        "symbol": ["A", "B", "A", "C"],
    })
    ch = R.ranking_top_changes(rk, top_n=2, ranking_type="T")
    ev = set(zip(ch["symbol"], ch["event"]))
    assert ("C", "enter") in ev and ("B", "exit") in ev
    assert ("A", "enter") not in ev          # 첫 스냅은 기준선일 뿐


def test_rotation_handoffs_pairs_exit_with_later_entry():
    ch = pd.DataFrame({"snap_ms": [0, 30_000], "symbol": ["B", "C"],
                       "event": ["exit", "enter"], "rank": [float("nan"), 2.0]})
    h = R.rotation_handoffs(ch, within_s=120)
    assert len(h) == 1
    assert h.iloc[0]["exit_symbol"] == "B" and h.iloc[0]["enter_symbol"] == "C"
    assert h.iloc[0]["lag_s"] == pytest.approx(30.0)


def test_rotation_handoffs_ignores_same_symbol_reentry():
    ch = pd.DataFrame({"snap_ms": [0, 30_000], "symbol": ["B", "B"],
                       "event": ["exit", "enter"], "rank": [float("nan"), 2.0]})
    assert R.rotation_handoffs(ch, within_s=120).empty


def test_spread_compression_detects_narrowing_and_nan_when_thin():
    rows = []
    for i in range(20):                       # 기준 창: 넓은 스프레드
        rows.append({"symbol": "A", "snap_ms": i * 60_000,
                     "bid1_u": 9_000, "ask1_u": 11_000})
    for i in range(20, 40):                   # 최근 창: 좁은 스프레드
        rows.append({"symbol": "A", "snap_ms": i * 60_000,
                     "bid1_u": 9_900, "ask1_u": 10_100})
    ob = pd.DataFrame(rows)
    v = R.spread_compression(ob, "A", 40 * 60_000, window_min=20, min_snaps=5)
    assert v == pytest.approx(10.0, rel=0.05) and v > 1.0
    assert math.isnan(R.spread_compression(ob, "A", 40 * 60_000, window_min=20,
                                           min_snaps=50))


# --------------------------------------------------------------------------- #
# 검증 도구
# --------------------------------------------------------------------------- #
def test_density_independence_fails_on_the_old_measures_pattern():
    """구 vol_surge_lead_min 의 실측 패턴(0.876→0.280 단조 감소)은 반드시 탈락."""
    detected, fill = [], []
    for rate, f in ((0.876, 0.1), (0.542, 0.35), (0.487, 0.7), (0.280, 0.99)):
        n = 200
        detected += [1.0] * int(rate * n) + [0.0] * (n - int(rate * n))
        fill += [f] * n
    rep = R.density_independence(detected, fill)
    assert rep["passed"] is False
    assert rep["monotone"] is True


def test_density_independence_passes_when_flat():
    detected, fill = [], []
    for i, f in enumerate((0.1, 0.35, 0.7, 0.99)):
        n = 200
        rate = 0.50 if i % 2 == 0 else 0.52      # 평평하고 비단조
        detected += [1.0] * int(rate * n) + [0.0] * (n - int(rate * n))
        fill += [f] * n
    rep = R.density_independence(detected, fill)
    assert rep["passed"] is True
    assert abs(rep["spearman_rho"]) < 0.20


def test_window_edge_mass_matches_the_old_measures_diagnostic():
    leads = [58, 59, 60, 57] + [5, 10, 20, 30]
    assert R.window_edge_mass(leads, scan_min=60, edge_frac=0.10) == pytest.approx(0.5)
    assert math.isnan(R.window_edge_mass([], scan_min=60))


def test_spread_compression_event_time_works_where_clock_windows_cannot():
    """실측 조건 재현: 심볼당 스냅이 25건뿐이고 20분 안에 몰려 있다.

    시계 기준 30분 창은 산출 불가(NaN)지만 사건 시간 블록은 산출된다.
    """
    rows = []
    for i in range(20):                      # 기준: 넓은 스프레드
        rows.append({"symbol": "A", "snap_ms": i * 16_000,
                     "bid1_u": 9_000, "ask1_u": 11_000})
    for i in range(20, 25):                  # 최근: 좁은 스프레드
        rows.append({"symbol": "A", "snap_ms": i * 16_000,
                     "bid1_u": 9_900, "ask1_u": 10_100})
    ob = pd.DataFrame(rows)
    t0 = 25 * 16_000
    assert math.isnan(R.spread_compression(ob, "A", t0, window_min=30, min_snaps=5))
    v = R.spread_compression_event_time(ob, "A", t0, recent_snaps=5,
                                        baseline_snaps=20)
    assert v == pytest.approx(10.0, rel=0.05)


def test_spread_compression_event_time_nan_when_too_few_snaps():
    ob = pd.DataFrame([{"symbol": "A", "snap_ms": i * 1000,
                        "bid1_u": 99, "ask1_u": 101} for i in range(5)])
    assert math.isnan(R.spread_compression_event_time(ob, "A", 10_000,
                                                      recent_snaps=5,
                                                      baseline_snaps=20))


# --------------------------------------------------------------------------- #
# 감사 4차 회귀 — B10 / B11
# --------------------------------------------------------------------------- #
def test_b10_amount_column_carries_its_unit_in_the_name():
    """`amount_u12` = USD x 1e12. 이름에 단위가 없으면 모듈 밖에서 오독된다."""
    p = R.print_frame(bars([0], [2], close_u=6_996_000))
    assert "amount_u12" in p.columns and "amount" not in p.columns
    assert p["amount_u12"].iloc[0] == 6_996_000 * 2
    assert R.amount_usd(p["amount_u12"].iloc[0]) == pytest.approx(
        6.996 * 2 / 1_000_000)


def test_b10_rotation_scores_expose_the_unit_suffixed_columns():
    day = _cohort_day()
    sc = R.rotation_scores(day, 60 * MIN_MS, window_min=30, min_prints=3,
                           min_cohort=5)
    assert "amount_u12" in sc.columns and "amount_u12_prev" in sc.columns
    assert "amount" not in sc.columns


def test_b10_amount_usd_is_nan_on_garbage():
    assert math.isnan(R.amount_usd(None))


def test_b11_crossed_book_is_nan_in_spread_compression_like_execution():
    """한 모듈은 막고 다른 모듈은 음수를 통과시키던 불일치를 없앤다."""
    from tossmon.analysis.execution import relative_spread
    rows = [{"symbol": "A", "snap_ms": i * 16_000, "bid1_u": 11_000,
             "ask1_u": 9_000} for i in range(40)]          # 전부 크로스
    ob = pd.DataFrame(rows)
    assert math.isnan(relative_spread(11_000, 9_000))
    assert math.isnan(R.spread_compression_event_time(ob, "A", 40 * 16_000))
    assert math.isnan(R.spread_compression(ob, "A", 40 * 60_000, window_min=20,
                                           min_snaps=5))


def test_b11_crossed_books_are_counted_not_silently_dropped():
    ob = pd.DataFrame([{"symbol": "A", "snap_ms": 0, "bid1_u": 11_000,
                        "ask1_u": 9_000},
                       {"symbol": "A", "snap_ms": 1, "bid1_u": 9_000,
                        "ask1_u": 11_000}])
    assert R.crossed_book_count(ob) == 1
    assert R.crossed_book_count(pd.DataFrame()) == 0


def test_b11_locked_book_is_still_valid():
    rows = [{"symbol": "A", "snap_ms": i * 16_000, "bid1_u": 10_000,
             "ask1_u": 10_000} for i in range(20)]
    rows += [{"symbol": "A", "snap_ms": (20 + i) * 16_000, "bid1_u": 10_000,
              "ask1_u": 10_000} for i in range(5)]
    ob = pd.DataFrame(rows)
    # 스프레드 0 은 유효하나 최근 블록 중앙값이 0 이라 비를 낼 수 없다 -> NaN (0 나눗셈 아님)
    assert math.isnan(R.spread_compression_event_time(ob, "A", 25 * 16_000))


def test_b11_crossed_book_count_survives_missing_quotes():
    """NaN 은 truthy 라 순진한 `if b and a` 가 통과시킨다 — 실 tier2 데이터에서 발현했다."""
    ob = pd.DataFrame({"bid1_u": [float("nan"), 11_000, None, 9_000],
                       "ask1_u": [10_000, 9_000, None, 11_000]})
    assert R.crossed_book_count(ob) == 1        # 두 번째 행만 크로스

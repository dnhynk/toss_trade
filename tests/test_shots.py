"""연속 슈팅 구조 테스트 — tossmon/analysis/shots.py (docs/23)."""
from __future__ import annotations

import math

import pandas as pd
import pytest

from tossmon.analysis import shots as S

S12 = 13_000          # 실측 스냅 주기 (~13초)


def series(prices, *, step_ms=S12, start=0):
    return pd.Series({start + i * step_ms: float(p) * 1_000_000
                      for i, p in enumerate(prices)}, dtype="float64")


def ranks(rows, ranking_type="TOSS"):
    return pd.DataFrame({"snap_ms": [r[0] for r in rows],
                         "ranking_type": [ranking_type] * len(rows),
                         "symbol": [r[1] for r in rows],
                         "last_u": [int(r[2] * 1_000_000) for r in rows]})


# --------------------------------------------------------------------------- #
# 가격 계열
# --------------------------------------------------------------------------- #
def test_price_series_drops_absent_snaps_rather_than_filling():
    rk = ranks([(0, "A", 1.0), (S12, "B", 5.0), (2 * S12, "A", 1.1)])
    s = S.price_series(rk, "A", "TOSS")
    assert list(s.index) == [0, 2 * S12]         # B 스냅을 A 의 결측으로 채우지 않는다
    assert len(s) == 2


def test_price_series_empty_on_unknown_symbol_or_type():
    rk = ranks([(0, "A", 1.0)])
    assert S.price_series(rk, "ZZ", "TOSS").empty
    assert S.price_series(rk, "A", "NOPE").empty
    assert S.price_series(pd.DataFrame(), "A", "TOSS").empty


def test_price_series_ignores_nonpositive_prices():
    rk = ranks([(0, "A", 1.0), (S12, "A", 0.0)])
    assert len(S.price_series(rk, "A", "TOSS")) == 1


# --------------------------------------------------------------------------- #
# 슈팅 검출
# --------------------------------------------------------------------------- #
def test_detects_a_single_shot():
    sh = S.detect_shots(series([1.00, 1.01, 1.05, 1.05]), min_rise=0.02)
    assert len(sh) == 1
    assert sh.iloc[0]["rise"] == pytest.approx(0.05)
    assert sh.iloc[0]["ordinal"] == 1


def test_ignores_a_rise_below_threshold():
    assert S.detect_shots(series([1.00, 1.005, 1.01]), min_rise=0.02).empty


def test_detects_consecutive_shots_and_numbers_them():
    # 두 번의 임펄스 사이에 눌림 (스냅 간격 13초, 분리 24초 요구)
    px = [1.00, 1.05, 1.02, 1.02, 1.02, 1.08, 1.08]
    sh = S.detect_shots(series(px), min_rise=0.02, min_separation_s=24)
    assert len(sh) >= 2
    assert list(sh["ordinal"])[:2] == [1, 2]
    assert sh.iloc[1]["gap_prev_s"] > 0


def test_shot_must_complete_within_max_span():
    """천천히 오르는 것은 슈팅이 아니다 — 사용자 관찰의 핵심(1분 미만)."""
    slow = series([1.00 + 0.004 * i for i in range(20)])     # 20*13s 에 걸쳐 +8%
    assert S.detect_shots(slow, min_rise=0.02, max_span_s=60).empty


def test_gap_in_coverage_breaks_the_series_instead_of_bridging():
    """랭킹에서 빠진 구간을 이어붙여 가짜 슈팅을 만들지 않는다."""
    s = pd.Series({0: 1.00e6, 600_000: 1.20e6}, dtype="float64")   # 10분 공백
    assert S.detect_shots(s, min_rise=0.02, max_gap_s=40).empty


def test_shot_frame_keeps_columns_when_empty():
    sh = S.detect_shots(pd.Series(dtype="float64"))
    assert list(sh.columns) == list(S.SHOT_COLUMNS)


def test_detect_shots_uses_no_future_information():
    """앞부분만 준 결과가 전체를 준 결과의 접두여야 한다."""
    px = [1.00, 1.05, 1.02, 1.02, 1.02, 1.09, 1.09]
    full = S.detect_shots(series(px), min_rise=0.02)
    part = S.detect_shots(series(px[:3]), min_rise=0.02)
    assert len(part) >= 1
    assert part.iloc[0]["start_ms"] == full.iloc[0]["start_ms"]
    assert part.iloc[0]["rise"] == pytest.approx(full.iloc[0]["rise"])


# --------------------------------------------------------------------------- #
# 요약 / 연속성
# --------------------------------------------------------------------------- #
def test_shot_summary_reports_zero_rather_than_nan_count():
    e = S.shot_summary(S.detect_shots(pd.Series(dtype="float64")))
    assert e["n_shots"] == 0 and math.isnan(e["median_rise"])


def test_shot_summary_compounds_total_rise():
    sh = S.detect_shots(series([1.00, 1.05, 1.02, 1.02, 1.02, 1.08]), min_rise=0.02)
    s = S.shot_summary(sh)
    assert s["n_shots"] == len(sh) and s["total_rise"] > 0


def test_next_shot_within_detects_continuation():
    sh = S.detect_shots(series([1.00, 1.05, 1.02, 1.02, 1.02, 1.09]), min_rise=0.02)
    first_peak = int(sh.iloc[0]["peak_ms"])
    assert S.next_shot_within(sh, first_peak, within_s=120) is True
    assert S.next_shot_within(sh, int(sh.iloc[-1]["peak_ms"]), within_s=120) is False
    assert S.next_shot_within(pd.DataFrame(columns=S.SHOT_COLUMNS), 0, 60) is False


def test_continuation_table_reports_conditional_probability():
    sh = S.detect_shots(series([1.00, 1.05, 1.02, 1.02, 1.02, 1.09]), min_rise=0.02)
    t = S.continuation_table(sh)
    assert set(["ordinal", "n", "p_next"]).issubset(t.columns)
    assert t.iloc[0]["p_next"] == pytest.approx(1.0)      # 1번 뒤에 2번이 왔다


# --------------------------------------------------------------------------- #
# 슈팅 기반 이탈
# --------------------------------------------------------------------------- #
def test_exit_into_shot_1_sells_at_the_first_shot_peak():
    s = series([1.00, 1.05, 1.02, 1.02, 1.02, 1.09])
    sh = S.detect_shots(s, min_rise=0.02)
    r = S.simulate_shot_exit(s, 0, 1.00e6, sh,
                             S.ShotExitRule("x", mode="into_shot_n", n=1))
    assert r["reason"] == "shot#1"
    assert r["gross"] == pytest.approx(0.05)


def test_exit_into_shot_2_rides_the_second_impulse():
    s = series([1.00, 1.05, 1.02, 1.02, 1.02, 1.09])
    sh = S.detect_shots(s, min_rise=0.02)
    r = S.simulate_shot_exit(s, 0, 1.00e6, sh,
                             S.ShotExitRule("x", mode="into_shot_n", n=2))
    assert r["reason"] == "shot#2"
    assert r["gross"] > 0.05          # 두 번째까지 들고 있으면 더 벌었다


def test_exit_into_shot_n_falls_back_to_horizon_when_n_never_arrives():
    s = series([1.00, 1.05, 1.04])
    sh = S.detect_shots(s, min_rise=0.02)
    r = S.simulate_shot_exit(s, 0, 1.00e6, sh,
                             S.ShotExitRule("x", mode="into_shot_n", n=3))
    assert r["reason"] == "horizon"


def test_exit_on_shot_fail_leaves_when_the_sequence_stops():
    # 슈팅 1회 뒤 계속 조용 -> fail_after_s 뒤 청산
    s = series([1.00, 1.05] + [1.04] * 20)
    sh = S.detect_shots(s, min_rise=0.02)
    r = S.simulate_shot_exit(s, 0, 1.00e6, sh,
                             S.ShotExitRule("x", mode="on_shot_fail",
                                            fail_after_s=60))
    assert r["reason"] == "shot_fail"
    assert r["exit_min"] >= 1


def test_exit_rules_share_the_shape_of_rules_simulate_exit():
    from tossmon.analysis.rules import ExitRule, simulate_exit
    p = pd.DataFrame({"ts_ms": [0, 60_000], "high_u": [1_000_000, 1_100_000],
                      "low_u": [1_000_000, 1_000_000],
                      "close_u": [1_000_000, 1_050_000]})
    a = simulate_exit(p, 1.0e6, 0, ExitRule("t", horizon_min=60))
    s = series([1.00, 1.05, 1.02, 1.02, 1.02, 1.09])
    b = S.simulate_shot_exit(s, 0, 1.00e6, S.detect_shots(s, min_rise=0.02),
                             S.ShotExitRule("x", n=1))
    assert set(a).issubset(set(b))          # 같은 순위표에 그대로 들어간다


def test_shot_exit_rejects_unknown_mode():
    s = series([1.00, 1.05])
    with pytest.raises(ValueError):
        S.simulate_shot_exit(s, 0, 1.0e6, S.detect_shots(s, min_rise=0.02),
                             S.ShotExitRule("x", mode="teleport"))


def test_shot_exit_nan_on_empty_series():
    r = S.simulate_shot_exit(pd.Series(dtype="float64"), 0, 1.0e6,
                             pd.DataFrame(columns=S.SHOT_COLUMNS),
                             S.ShotExitRule("x"))
    assert math.isnan(r["gross"]) and r["reason"] == "no_path"


def test_shot_exit_ignores_shots_before_entry():
    s = series([1.00, 1.05, 1.02, 1.02, 1.02, 1.09])
    sh = S.detect_shots(s, min_rise=0.02)
    late = int(sh.iloc[0]["peak_ms"]) + 1
    r = S.simulate_shot_exit(s, late, float(s.loc[s.index[s.index >= late][0]]), sh,
                             S.ShotExitRule("x", mode="into_shot_n", n=1))
    assert r["n_shots_after"] < len(sh)      # 진입 전 슈팅은 세지 않는다


# --------------------------------------------------------------------------- #
# 필요 표본
# --------------------------------------------------------------------------- #
def test_required_days_inverts_the_observation_rate():
    assert S.required_days(10.0, min_n=30)["days_needed"] == 3
    assert S.required_days(0.0)["days_needed"] is None


# --------------------------------------------------------------------------- #
# 측정 층 분류 (docs/23 §8) — 모집단 오류 방지
# --------------------------------------------------------------------------- #
def test_target_stratum_is_price_2_to_5_with_section_2_7_mcap():
    # $3.00 x 20M shares = $60M mcap -> section 2.7 band, price in $2-5
    assert S.symbol_stratum(3.00, 20_000_000 * 1_000_000) == "target"


def test_section_2_7_price_outside_the_target_band():
    assert S.symbol_stratum(0.50, 100_000_000 * 1_000_000) == "sec27_other_price"
    assert S.symbol_stratum(12.0, 5_000_000 * 1_000_000) == "sec27_other_price"


def test_mcap_outside_section_2_7_is_split_out():
    assert S.symbol_stratum(3.00, 1_000_000_000 * 1_000_000) == "univ_mcap_out"
    assert S.symbol_stratum(3.00, 1_000_000 * 1_000_000) == "univ_mcap_out"


def test_symbols_without_share_data_are_not_guessed():
    """유니버스 테이블에 없으면 시총을 추정하지 않는다 — 대형주가 여기 들어온다."""
    assert S.symbol_stratum(200.0, None) == "not_in_universe"
    assert S.symbol_stratum(200.0, float("nan")) == "not_in_universe"
    assert S.symbol_stratum(3.0, 0) == "univ_no_shares"


def test_target_band_boundaries_are_half_open():
    assert S.symbol_stratum(2.00, 20_000_000 * 1_000_000) == "target"
    assert S.symbol_stratum(5.00, 20_000_000 * 1_000_000) == "sec27_other_price"


# --------------------------------------------------------------------------- #
# 다계열 병합 — 슈팅 이중계산 금지
# --------------------------------------------------------------------------- #
def ranks2(rows):
    """(snap_ms, ranking_type, symbol, price$) 목록."""
    return pd.DataFrame({"snap_ms": [r[0] for r in rows],
                         "ranking_type": [r[1] for r in rows],
                         "symbol": [r[2] for r in rows],
                         "last_u": [int(r[3] * 1_000_000) for r in rows]})


def test_multi_series_unions_snapshots_from_both_rankings():
    rk = ranks2([(0, S.TOSS_VOLUME, "A", 1.00), (S12, S.TOSS_AMOUNT, "A", 1.05)])
    s = S.price_series_multi(rk, "A")
    assert list(s.index) == [0, S12]
    assert s.attrs["n_by_type"][S.TOSS_VOLUME] == 1
    assert s.attrs["n_by_type"][S.TOSS_AMOUNT] == 1


def test_multi_series_collapses_a_duplicated_snapshot_to_one_value():
    """같은 시각이 두 랭킹에 있으면 한 값만 남아야 한다 — 안 그러면 슈팅을 두 번 센다."""
    rk = ranks2([(0, S.TOSS_VOLUME, "A", 1.00), (0, S.TOSS_AMOUNT, "A", 1.00)])
    s = S.price_series_multi(rk, "A")
    assert len(s) == 1 and s.attrs["conflicts"] == 0


def test_multi_series_counts_conflicting_prices_rather_than_hiding_them():
    rk = ranks2([(0, S.TOSS_VOLUME, "A", 1.00), (0, S.TOSS_AMOUNT, "A", 1.20)])
    s = S.price_series_multi(rk, "A")
    assert len(s) == 1
    assert s.attrs["conflicts"] == 1
    assert s.iloc[0] == pytest.approx(1.10 * 1_000_000)      # 중앙값


def test_multi_series_does_not_double_count_shots():
    """두 계열에 동일 계열이 통째로 중복돼도 슈팅 수가 늘면 안 된다."""
    px = [1.00, 1.05, 1.02, 1.02, 1.02, 1.09]
    rows = []
    for i, p in enumerate(px):
        rows.append((i * S12, S.TOSS_VOLUME, "A", p))
        rows.append((i * S12, S.TOSS_AMOUNT, "A", p))
    merged = S.price_series_multi(ranks2(rows), "A")
    single = S.price_series(ranks2([r for r in rows
                                    if r[1] == S.TOSS_VOLUME]), "A", S.TOSS_VOLUME)
    assert len(S.detect_shots(merged)) == len(S.detect_shots(single))


def test_multi_series_empty_and_unknown_symbol():
    assert S.price_series_multi(pd.DataFrame(), "A").empty
    rk = ranks2([(0, S.TOSS_VOLUME, "A", 1.0)])
    assert S.price_series_multi(rk, "ZZ").empty


def test_volume_ranking_is_the_default_primary_series():
    assert S.DEFAULT_RANKING_TYPES[0] == S.TOSS_VOLUME


# --------------------------------------------------------------------------- #
# 포착 가능한 수익 — 사후 상한과의 분리 + 룩어헤드 회귀
# --------------------------------------------------------------------------- #
def test_shot_records_the_moment_it_became_knowable():
    """detect_ms 는 임계를 처음 넘은 봉 — 시작점(저점)보다 늦어야 한다."""
    sh = S.detect_shots(series([1.00, 1.01, 1.05, 1.06]), min_rise=0.02)
    r = sh.iloc[0]
    assert r["detect_ms"] > r["start_ms"]
    assert r["detect_u"] > r["start_u"]
    assert r["rise_after_detect"] < r["rise"]     # 남은 상승폭 < 전구간 상한


def test_price_at_never_reads_the_future():
    s = series([1.00, 2.00, 3.00])
    assert S.price_at(s, S12 - 1) == pytest.approx(1.00e6)
    assert S.price_at(s, S12) == pytest.approx(2.00e6)
    assert math.isnan(S.price_at(s, -1))


def test_sell_on_downtick_exits_at_the_first_lower_quote():
    s = series([1.00, 1.10, 1.20, 1.15, 1.30])
    px, ts = S.sell_on_downtick(s, 0)
    assert px == pytest.approx(1.15e6)            # 이후 1.30 은 보지 않는다
    assert ts == 3 * S12


def test_sell_on_downtick_falls_back_to_last_quote_when_monotone():
    s = series([1.00, 1.10, 1.20])
    px, _ = S.sell_on_downtick(s, 0)
    assert px == pytest.approx(1.20e6)


def test_capturable_return_is_below_the_hindsight_rise():
    """핵심 회귀: 포착 가능 수익이 사후 전구간 상한보다 작아야 한다."""
    s = series([1.00, 1.01, 1.05, 1.08, 1.06])
    sh = S.detect_shots(s, min_rise=0.02)
    r = S.capturable_shot_return(s, sh.iloc[0], delay_s=0)
    assert r["captured"] < r["hindsight_rise"]


def test_capturable_return_shrinks_as_detection_delay_grows():
    s = series([1.00, 1.01, 1.05, 1.08, 1.09, 1.07])
    sh = S.detect_shots(s, min_rise=0.02)
    a = S.capturable_shot_return(s, sh.iloc[0], delay_s=0)["captured"]
    b = S.capturable_shot_return(s, sh.iloc[0], delay_s=2 * 13)["captured"]
    assert b <= a


def test_capturable_entry_never_uses_the_shot_low():
    """진입가는 detect 시점 가격이어야 한다 — 저점 진입이면 룩어헤드다."""
    s = series([1.00, 1.01, 1.05, 1.06])
    sh = S.detect_shots(s, min_rise=0.02)
    r = S.capturable_shot_return(s, sh.iloc[0], delay_s=0)
    assert r["entry_u"] == pytest.approx(float(sh.iloc[0]["detect_u"]))
    assert r["entry_u"] > float(sh.iloc[0]["start_u"])


def test_capturable_return_nan_when_no_quote_exists():
    s = pd.Series(dtype="float64")
    sh = S.detect_shots(series([1.00, 1.05]), min_rise=0.02)
    assert math.isnan(S.capturable_shot_return(s, sh.iloc[0])["captured"])


def test_oversold_entry_requires_a_drop_then_an_uptick():
    s = series([1.00, 1.00, 0.90, 0.88, 0.92])
    px, ts = S.find_oversold_entry(s, drop=0.05, lookback_s=600)
    assert px == pytest.approx(0.92e6)            # 반등 확인 봉
    assert ts == 4 * S12


def test_oversold_entry_nan_when_no_drop():
    s = series([1.00, 1.01, 1.02])
    assert math.isnan(S.find_oversold_entry(s, drop=0.05)[0])


def test_oversold_entry_uses_only_past_quotes():
    """앞부분만 준 결과가 전체를 준 결과와 같아야 한다."""
    px = [1.00, 1.00, 0.90, 0.88, 0.92, 1.50]
    full = S.find_oversold_entry(series(px), drop=0.05)
    part = S.find_oversold_entry(series(px[:5]), drop=0.05)
    assert full == part


def test_design_b_sells_into_a_shot_while_already_holding():
    s = series([1.00, 1.00, 0.90, 0.88, 0.92, 0.94, 1.00, 1.00])
    sh = S.detect_shots(s, min_rise=0.02)
    e_px, e_ms = S.find_oversold_entry(s, drop=0.05)
    r = S.shot_exit_from_entry(s, sh, e_px, e_ms, n=1)
    assert r["reason"] == "shot#1" and r["ret"] > 0


def test_design_b_reports_no_shot_as_its_own_category_not_a_loss():
    s = series([1.00, 1.00, 0.90, 0.88, 0.92])
    e_px, e_ms = S.find_oversold_entry(s, drop=0.05)
    r = S.shot_exit_from_entry(s, pd.DataFrame(columns=S.SHOT_COLUMNS), e_px, e_ms)
    assert r["reason"] == "no_shot"
    assert math.isnan(r["ret"])                   # 손실 0 이 아니라 미도래


def test_design_b_ignores_shots_that_peaked_before_entry():
    s = series([1.00, 1.06, 1.00, 0.90, 0.88, 0.92])
    sh = S.detect_shots(s, min_rise=0.02)
    e_px, e_ms = S.find_oversold_entry(s, drop=0.05)
    r = S.shot_exit_from_entry(s, sh, e_px, e_ms, n=1)
    assert r["reason"] == "no_shot"               # 진입 전 고점은 팔 수 없다

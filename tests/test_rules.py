"""조건부 규칙 탐색 도구 테스트 — tossmon/analysis/rules.py (docs/19)."""
from __future__ import annotations

import math

import pandas as pd
import pytest

from tossmon.analysis import rules as R

MIN_MS = R.MIN_MS
U = R.MICRO


def path(rows, *, start_min=0):
    """(high, low, close) $ 목록 → 1분봉 경로 (ts=start_min 부터 1분 간격)."""
    return pd.DataFrame({
        "ts_ms": [(start_min + i) * MIN_MS for i in range(len(rows))],
        "high_u": [int(h * U) for h, _l, _c in rows],
        "low_u": [int(l * U) for _h, l, _c in rows],
        "close_u": [int(c * U) for _h, _l, c in rows],
    })


# --------------------------------------------------------------------------- #
# 비용 모델
# --------------------------------------------------------------------------- #
def test_cost_for_uses_the_measured_band_values():
    assert R.cost_for("$2-5", "measured_cross") == pytest.approx(0.0322)
    assert R.cost_for("$0.50-1", "measured_cross") == pytest.approx(0.0878)


def test_cost_for_falls_back_to_pooled_for_unknown_band():
    assert R.cost_for("nonsense", "measured_cross") == R.COST_CROSS["unknown"]


def test_frozen_cost_is_flat_regardless_of_band():
    assert R.cost_for("$2-5", "frozen_1pct") == pytest.approx(0.010)
    assert R.cost_for("$0.50-1", "frozen_1pct") == pytest.approx(0.010)


def test_measured_cross_is_never_cheaper_than_mid():
    for band in R.COST_CROSS:
        assert R.COST_CROSS[band] >= R.COST_MID[band]


def test_cost_for_rejects_unknown_model():
    with pytest.raises(ValueError):
        R.cost_for("$2-5", "wishful")


# --------------------------------------------------------------------------- #
# 거래대금 기울기
# --------------------------------------------------------------------------- #
def _prints(ts_min, amounts):
    return pd.DataFrame({"ts_ms": [t * MIN_MS for t in ts_min],
                         "amount_u12": [int(a) for a in amounts]})


def test_log_amount_rate_ratio_positive_when_flow_accelerates():
    ts = list(range(40))
    amt = [100] * 30 + [1000] * 10
    v = R.log_amount_rate_ratio(_prints(ts, amt), recent_prints=10,
                                baseline_prints=30)
    assert v > 0 and v == pytest.approx(math.log(10), rel=0.25)


def test_log_amount_rate_ratio_is_scale_invariant():
    ts = list(range(40))
    amt = [100] * 30 + [1000] * 10
    a = R.log_amount_rate_ratio(_prints(ts, amt), recent_prints=10, baseline_prints=30)
    b = R.log_amount_rate_ratio(_prints(ts, [x * 10_000 for x in amt]),
                                recent_prints=10, baseline_prints=30)
    assert a == pytest.approx(b)


def test_log_amount_rate_ratio_nan_when_too_few_prints():
    assert math.isnan(R.log_amount_rate_ratio(_prints([0, 1, 2], [1, 1, 1])))


def test_log_amount_rate_ratio_negative_when_flow_dries_up():
    ts = list(range(40))
    amt = [1000] * 30 + [10] * 10
    assert R.log_amount_rate_ratio(_prints(ts, amt), recent_prints=10,
                                   baseline_prints=30) < 0


# --------------------------------------------------------------------------- #
# 이탈 시뮬레이션
# --------------------------------------------------------------------------- #
def test_exit_on_target():
    p = path([(1.00, 0.99, 1.00), (1.20, 1.00, 1.15)])
    r = R.simulate_exit(p, 1.0 * U, 0, R.ExitRule("t", target=0.10, horizon_min=60))
    assert r["reason"] == "target" and r["gross"] == pytest.approx(0.10)


def test_exit_on_stop():
    p = path([(1.00, 0.99, 1.00), (1.01, 0.80, 0.85)])
    r = R.simulate_exit(p, 1.0 * U, 0, R.ExitRule("s", stop=0.10, horizon_min=60))
    assert r["reason"] == "stop" and r["gross"] == pytest.approx(-0.10)


def test_stop_wins_over_target_in_the_same_bar():
    """봉 내부 순서를 모르므로 손절이 먼저 닿았다고 본다 (보수적)."""
    p = path([(1.30, 0.80, 1.00)])
    r = R.simulate_exit(p, 1.0 * U, 0,
                        R.ExitRule("b", target=0.10, stop=0.10, horizon_min=60))
    assert r["reason"] == "stop"


def test_trailing_stop_triggers_from_the_running_peak():
    # 1.50 까지 올랐다가 되밀림 -> 20% 트레일 = 1.20 에서 이탈
    p = path([(1.50, 1.00, 1.45), (1.45, 1.10, 1.15)])
    r = R.simulate_exit(p, 1.0 * U, 0, R.ExitRule("tr", trail=0.20, horizon_min=60))
    assert r["reason"] == "trail" and r["exit_u"] == pytest.approx(1.20 * U)


def test_trailing_peak_updates_only_after_the_bar_is_judged():
    """같은 봉에서 신고가를 찍고 되밀린 경우를 유리하게 세지 않는다."""
    p = path([(2.00, 1.00, 1.05)])       # 고가 2.00, 저가 1.00
    r = R.simulate_exit(p, 1.0 * U, 0, R.ExitRule("tr", trail=0.20, horizon_min=60))
    # 진입 시점 peak=1.00 이므로 이 봉에서는 트레일이 걸리지 않는다
    assert r["reason"] == "horizon"


def test_exit_on_time():
    p = path([(1.0, 1.0, 1.0), (1.1, 1.0, 1.05), (1.2, 1.0, 1.10)])
    r = R.simulate_exit(p, 1.0 * U, 0, R.ExitRule("t", time_min=2, horizon_min=60))
    assert r["reason"] == "time" and r["exit_min"] == 2


def test_exit_falls_back_to_horizon_close():
    p = path([(1.0, 1.0, 1.0), (1.02, 0.99, 1.01)])
    r = R.simulate_exit(p, 1.0 * U, 0, R.ExitRule("h", horizon_min=60))
    assert r["reason"] == "horizon" and r["gross"] == pytest.approx(0.01)


def test_horizon_truncates_and_ignores_later_bars():
    p = path([(1.0, 1.0, 1.0), (1.0, 1.0, 1.0), (9.0, 1.0, 9.0)])
    r = R.simulate_exit(p, 1.0 * U, 0, R.ExitRule("h", horizon_min=1))
    assert r["gross"] == pytest.approx(0.0)      # 3번째 봉(2분)은 지평 밖


def test_exit_reports_mfe_and_mae():
    p = path([(1.30, 0.90, 1.00)])
    r = R.simulate_exit(p, 1.0 * U, 0, R.ExitRule("h", horizon_min=60))
    assert r["mfe"] == pytest.approx(0.30) and r["mae"] == pytest.approx(-0.10)


def test_exit_nan_when_no_path():
    r = R.simulate_exit(pd.DataFrame(columns=["ts_ms", "high_u", "low_u", "close_u"]),
                        1.0 * U, 0, R.ExitRule("h"))
    assert math.isnan(r["exit_u"]) and r["reason"] == "no_path"


# --------------------------------------------------------------------------- #
# 개미털기 진입
# --------------------------------------------------------------------------- #
def test_dip_entry_waits_for_drop_then_bounce_confirmation():
    # 1.00 -> 저가 0.80 (20% 하락) -> 종가 상승 확인 봉에서 진입
    p = path([(1.00, 1.00, 1.00), (1.00, 0.80, 0.85), (0.95, 0.84, 0.90)])
    e = R.find_dip_entry(p, 1.0 * U, 0, dip=0.15, window_min=60)
    assert e["entry_u"] == pytest.approx(0.90 * U)
    assert e["wait_min"] == 2


def test_dip_entry_nan_when_dip_never_happens():
    p = path([(1.0, 0.99, 1.0)] * 5)
    assert math.isnan(R.find_dip_entry(p, 1.0 * U, 0, dip=0.15)["entry_u"])


def test_dip_entry_nan_when_no_bounce_confirmation():
    p = path([(1.0, 1.0, 1.0), (1.0, 0.5, 0.6), (0.6, 0.4, 0.5)])
    assert math.isnan(R.find_dip_entry(p, 1.0 * U, 0, dip=0.15)["entry_u"])


def test_dip_entry_respects_the_window():
    rows = [(1.0, 1.0, 1.0)] * 10 + [(1.0, 0.5, 0.6), (0.7, 0.6, 0.7)]
    assert math.isnan(R.find_dip_entry(path(rows), 1.0 * U, 0, dip=0.15,
                                       window_min=5)["entry_u"])


# --------------------------------------------------------------------------- #
# 통계 / 순위
# --------------------------------------------------------------------------- #
def test_bootstrap_ci_is_deterministic_under_the_frozen_seed():
    v = [0.01 * i for i in range(50)]
    assert R.bootstrap_ci_mean(v) == R.bootstrap_ci_mean(v)


def test_bootstrap_ci_brackets_the_mean():
    v = [0.05, 0.03, -0.01, 0.02, 0.04] * 20
    lo, hi = R.bootstrap_ci_mean(v)
    assert lo < sum(v) / len(v) < hi


def test_bootstrap_ci_nan_for_tiny_sample():
    assert math.isnan(R.bootstrap_ci_mean([0.1])[0])


def test_bonferroni_ci_is_wider_than_the_plain_ci():
    v = [0.05, -0.02, 0.03, 0.01] * 25
    lo, hi = R.bootstrap_ci_mean(v)
    blo, bhi = R.bonferroni_ci_mean(v, n_candidates=5)
    assert blo < lo and bhi > hi


def test_rank_rules_is_mechanical_and_excludes_small_samples():
    rs = [
        R.RuleResult("small", "u", "e", "x", n=10, ci_low=0.99, mean=0.99),
        R.RuleResult("best", "u", "e", "x", n=40, ci_low=0.05, mean=0.06),
        R.RuleResult("mid", "u", "e", "x", n=50, ci_low=0.02, mean=0.03),
        R.RuleResult("worst", "u", "e", "x", n=60, ci_low=-0.10, mean=-0.05),
    ]
    top = R.rank_rules(rs, top_k=2)
    assert [r.name for r in top] == ["best", "mid"]   # small(n=10) 은 자격 미달


def test_rank_rules_breaks_ties_deterministically():
    rs = [R.RuleResult("a", "u", "e", "x", n=30, ci_low=0.01, mean=0.02),
          R.RuleResult("b", "u", "e", "x", n=99, ci_low=0.01, mean=0.02)]
    assert [r.name for r in R.rank_rules(rs, top_k=2)] == ["b", "a"]


def test_eligibility_threshold_is_the_frozen_30():
    assert R.RuleResult("x", "u", "e", "x", n=29).eligible is False
    assert R.RuleResult("x", "u", "e", "x", n=30).eligible is True

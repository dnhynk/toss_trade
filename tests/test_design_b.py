"""설계 B 정직 러너 테스트 — tossmon/analysis/measure/design_b.py.

감사 5차 H-2 가 지적한 공백을 메운다: **설계 B 이탈에 접두사 불변성 검사가 하나도
없었고**, 그래서 "그날 전체 최고가에 매도" 같은 노골적 룩어헤드가 48건 스위트를
통과했다. 여기의 접두사 불변성 테스트는 그런 변이를 잡도록 픽스처를 설계했다 —
**미래 봉이 답을 바꿀 수 있는 모양**이어야 한다(H-3).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from tossmon.analysis.measure import design_b as D

S12 = 13_000
U = 1_000_000


def series(prices, *, step_ms=S12, start=0):
    return pd.Series({start + i * step_ms: float(p) * U
                      for i, p in enumerate(prices)}, dtype="float64")


# --------------------------------------------------------------------------- #
# 이탈 규칙이 슈팅 정의를 참조하지 않는다 (C-2 의 본질)
# --------------------------------------------------------------------------- #
def _code_lines(mod) -> str:
    """모듈 소스에서 독스트링·주석을 걷어낸 실행 코드만. (설명문이 검사에 걸리지 않게)"""
    import ast
    import pathlib
    # CRLF 정규화 — `ast.get_docstring` 은 개행을 LF 로 돌려주므로, 원본이 CRLF 면
    # `replace` 가 조용히 빗나간다. 감사 5차 H-2 가 겪은 바로 그 함정이다.
    src = pathlib.Path(mod.__file__).read_text(encoding="utf-8").replace("\r\n", "\n")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            d = ast.get_docstring(node, clean=False)
            if d:
                src = src.replace(d, "")
    return " ".join(ln.split("#")[0] for ln in src.splitlines())


def test_no_exit_rule_references_the_shot_definition():
    """실행 코드에 `detect_shots` 호출·`peak_u` 사용이 없어야 한다.

    있으면 슈팅 임계가 답에 새어 감사 5차 C-2 가 되풀이된다. 독스트링에서 그 이름을
    *설명*하는 것은 무해하므로 코드만 본다.
    """
    code = _code_lines(D)
    # 호출·접근 **형태**를 본다. 이름만으로 보면 "부재를 검사하는 코드"까지 걸린다
    # (`min_rise_independence` 가 그 문자열을 담고 있다).
    assert "detect_shots(" not in code            # 호출 없음
    assert ".detect_shots" not in code            # 속성 접근 없음
    assert '["peak_u"]' not in code               # 사후 고점을 이탈가로 쓰지 않는다
    assert ".peak_u" not in code


def test_exit_rule_table_covers_every_declared_family():
    names = set(D.exit_rules().keys())
    for fam in ("time_", "downtick_", "trail_", "target_", "hold_"):
        assert any(n.startswith(fam) for n in names), fam


# --------------------------------------------------------------------------- #
# 접두사 불변성 — H-2 가 없다고 지적한 바로 그 검사
# --------------------------------------------------------------------------- #
#: 미래에 훨씬 높은 봉이 있는 계열. 사후 고점을 보는 구현이면 답이 달라진다.
LOOKAHEAD_TRAP = [1.00, 1.02, 1.01, 1.03, 0.99, 5.00, 5.00]


@pytest.mark.parametrize("rule", list(D.exit_rules().keys()))
def test_every_exit_is_prefix_invariant(rule):
    """이탈이 t 에 확정됐다면, t 이후 봉을 더 줘도 답이 바뀌면 안 된다.

    픽스처에 **미래의 5.00** 을 넣어 위반이 표현 가능하게 했다(H-3).
    """
    fn = D.exit_rules()[rule]
    full = series(LOOKAHEAD_TRAP)
    entry_u = float(full.iloc[0])
    a = fn(full, 0, entry_u)
    # 이탈이 확정된 시점까지만 잘라서 다시 계산
    cut = full[full.index <= a["exit_ms"]]
    b = fn(cut, 0, entry_u)
    assert b["exit_u"] == pytest.approx(a["exit_u"]), rule
    assert b["exit_ms"] == a["exit_ms"], rule


def test_prefix_trap_would_catch_a_hindsight_max_exit():
    """픽스처가 실제로 위반을 표현할 수 있는지 확인한다 (H-3 의 교훈).

    '지평 전체 최고가에 판다'는 가짜 규칙은 접두사 불변성을 깨야 한다.
    """
    full = series(LOOKAHEAD_TRAP)

    def hindsight_max(s, entry_ms, entry_u):
        w = s[s.index >= entry_ms]
        return {"exit_u": float(w.max()), "exit_ms": int(w.idxmax()), "filled": True}

    a = hindsight_max(full, 0, float(full.iloc[0]))
    cut = full[full.index <= 3 * S12]          # 5.00 이 오기 전까지
    b = hindsight_max(cut, 0, float(full.iloc[0]))
    assert b["exit_u"] != pytest.approx(a["exit_u"])   # 픽스처가 위반을 잡는다


# --------------------------------------------------------------------------- #
# 개별 이탈 규칙의 의미
# --------------------------------------------------------------------------- #
def test_time_exit_takes_the_last_quote_at_or_before_the_deadline():
    s = series([1.00, 1.05, 1.10, 1.20])
    r = D.exit_after_seconds(s, 0, 26)          # 스냅 13s -> index 2 까지
    assert r["exit_u"] == pytest.approx(1.10 * U)


def test_downtick_1_exits_on_the_first_lower_quote():
    s = series([1.00, 1.10, 1.05, 1.30])
    r = D.exit_on_downticks(s, 0, n=1)
    assert r["exit_u"] == pytest.approx(1.05 * U) and r["filled"]


def test_downtick_2_needs_two_in_a_row():
    s = series([1.00, 1.10, 1.05, 1.20, 1.15, 1.12])
    r = D.exit_on_downticks(s, 0, n=2)
    assert r["exit_u"] == pytest.approx(1.12 * U)   # 1.05 는 단발이라 통과


def test_trailing_uses_the_running_peak_not_the_final_peak():
    s = series([1.00, 1.10, 1.09, 2.00])
    r = D.exit_trailing(s, 0, trail=0.005)
    # 1.10 까지 오른 뒤 1.09 (-0.9%) -> 이탈. 뒤의 2.00 은 보지 않는다.
    assert r["exit_u"] == pytest.approx(1.09 * U) and r["filled"]


def test_trailing_peak_updates_after_judging_the_bar():
    s = series([1.00, 1.00, 1.00])
    assert not D.exit_trailing(s, 0, trail=0.005)["filled"]


def test_target_fills_at_the_limit_price_not_the_quote():
    s = series([1.00, 1.50])
    r = D.exit_target(s, 0, 1.00 * U, target=0.02)
    assert r["exit_u"] == pytest.approx(1.02 * U)   # 지정가에 체결, 1.50 아님
    assert r["filled"]


def test_target_unfilled_falls_back_to_horizon_and_is_reported():
    s = series([1.00, 1.005, 1.004])
    r = D.exit_target(s, 0, 1.00 * U, target=0.03)
    assert not r["filled"]
    assert r["exit_u"] == pytest.approx(1.004 * U)


def test_exits_nan_on_empty_series():
    e = pd.Series(dtype="float64")
    for fn in (lambda: D.exit_after_seconds(e, 0, 30),
               lambda: D.exit_on_downticks(e, 0),
               lambda: D.exit_trailing(e, 0, trail=0.01),
               lambda: D.exit_target(e, 0, U, target=0.01)):
        assert math.isnan(fn()["exit_u"])


# --------------------------------------------------------------------------- #
# 모든 진입을 계상한다 / 위약 / 군집
# --------------------------------------------------------------------------- #
def test_evaluate_counts_losing_entries_too():
    """손실 건을 빼고 세면 다시 선택 편의가 생긴다."""
    s = series([1.00, 0.90, 0.85])
    df = D.evaluate({"A": s}, [{"symbol": "A", "signal_ms": 0, "signal_u": 1.0 * U}],
                    entry_delay_s=0)
    assert len(df) == 1
    assert df["hold_horizon"].iloc[0] < 0        # 손실이 그대로 계상된다


def test_evaluate_skips_entries_with_no_fillable_quote():
    df = D.evaluate({"A": series([1.0])}, [{"symbol": "A", "signal_ms": -10**9,
                                            "signal_u": 1.0}], entry_delay_s=0)
    assert df.empty                              # 체결가 없음 -> 거래 없음


def test_entry_delay_moves_the_fill_price():
    s = series([1.00, 1.20, 1.30])
    e = [{"symbol": "A", "signal_ms": 0, "signal_u": 1.0 * U}]
    a = D.evaluate({"A": s}, e, entry_delay_s=0)["entry_u"].iloc[0]
    b = D.evaluate({"A": s}, e, entry_delay_s=13)["entry_u"].iloc[0]
    assert b > a                                 # 신호봉에 즉시 체결되지 않는다


def test_placebo_keeps_entry_times_and_only_shuffles_symbols():
    ents = [{"symbol": "A", "signal_ms": 100, "signal_u": 1.0},
            {"symbol": "B", "signal_ms": 200, "signal_u": 2.0}]
    pl = D.placebo_entries(ents, ["A", "B", "C"], seed=1)
    assert [p["signal_ms"] for p in pl] == [100, 200]
    assert all(p["symbol"] in {"A", "B", "C"} for p in pl)


def test_placebo_is_deterministic_per_seed():
    ents = [{"symbol": "A", "signal_ms": i, "signal_u": 1.0} for i in range(20)]
    a = D.placebo_entries(ents, ["A", "B", "C"], seed=7)
    b = D.placebo_entries(ents, ["A", "B", "C"], seed=7)
    assert [x["symbol"] for x in a] == [x["symbol"] for x in b]


def test_at_least_five_placebo_seeds_are_configured():
    assert len(D.PLACEBO_SEEDS) >= 5


def test_day_cluster_floor_is_five():
    assert D.MIN_DAY_CLUSTERS == 5


# --------------------------------------------------------------------------- #
# 비용은 항상 차감
# --------------------------------------------------------------------------- #
def test_summarize_headline_is_net_of_cost():
    vals = [0.02, 0.04, 0.05, 0.06, 0.08] * 4        # 분산이 있어야 CI 가 선다
    df = pd.DataFrame({"r": vals, "r__filled": [True] * len(vals)})
    s = D.summarize(df, "r", clip=100)
    mean = sum(vals) / len(vals)
    assert s["gross_mean"] == pytest.approx(mean)
    assert s["net_mean"] == pytest.approx(mean - D.CLIP_COSTS[100])
    assert s["ci"][0] < s["net_mean"] < s["ci"][1]   # CI 도 비용 차감 기준


def test_summarize_reports_fill_rate():
    df = pd.DataFrame({"r": [0.01, 0.02], "r__filled": [True, False]})
    assert D.summarize(df, "r")["fill_rate"] == pytest.approx(0.5)


def test_summarize_handles_tiny_samples_without_inventing_a_ci():
    s = D.summarize(pd.DataFrame({"r": [0.01]}), "r")
    assert math.isnan(s["gross_mean"])


def test_clip_costs_match_docs21():
    assert D.CLIP_COSTS == {100: 0.0238, 500: 0.0445, 1000: 0.0491, 2000: 0.0644}

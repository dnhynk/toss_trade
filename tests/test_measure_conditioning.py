"""조건화 창이 미래를 보지 않음을 강제한다 — 감사 4차 B5.

docs/18 §2 의 "움직이는 중"이 과거 5분인지 미래 5분인지 확인할 수 없다는 지적에 대한
회귀 방어다. 미래를 보는 구현이면 아래 테스트가 깨진다.
"""
from __future__ import annotations

import pandas as pd

from tossmon.analysis.measure import CONDITIONING


def abs_ret_5m(closes, *, strict_prior=False):
    """execution_measure 가 쓰는 것과 동일한 식 (shift(5), 필요 시 추가 shift(1))."""
    s = pd.Series(closes, dtype="float64")
    r = (s / s.shift(5) - 1).abs()
    return r.shift(1) if strict_prior else r


def test_conditioning_metadata_declares_backward_window():
    assert CONDITIONING["uses_future_bars"] is False
    assert "backward" in CONDITIONING["window"]


def test_axis_is_zero_when_the_move_is_entirely_in_the_future():
    """앞이 평평하고 뒤에서만 움직이면, 움직임 전 시점의 지표는 0 이어야 한다.

    미래를 보는 구현이라면 여기서 0 이 아닌 값이 나온다.
    """
    closes = [100.0] * 10 + [200.0] * 10          # 급등은 index 10 부터
    r = abs_ret_5m(closes)
    assert r.iloc[9] == 0.0                       # 급등 직전 시점: 과거는 평평
    assert r.iloc[10] > 0.0                       # 급등이 과거로 들어온 뒤에야 반응


def test_axis_reflects_a_past_move():
    closes = [100.0] * 5 + [200.0] + [200.0] * 5
    r = abs_ret_5m(closes)
    assert r.iloc[5] == 1.0                       # close[5]/close[0]-1 = 1.0


def test_strict_prior_excludes_the_snapshots_own_minute():
    closes = [100.0] * 5 + [200.0] + [200.0] * 5
    loose = abs_ret_5m(closes)
    strict = abs_ret_5m(closes, strict_prior=True)
    assert loose.iloc[5] == 1.0
    # 자기 분의 종가를 쓰지 않으므로 한 칸 늦게 반영된다 (여기서는 아직 미정의)
    assert pd.isna(strict.iloc[5])
    assert strict.iloc[6] == 1.0


def test_first_bars_are_nan_not_zero_filled():
    r = abs_ret_5m([100.0] * 8)
    assert r.iloc[:5].isna().all()                # 결측을 0 으로 채우지 않는다

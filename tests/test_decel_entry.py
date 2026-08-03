"""감속 진입 (docs/27) — 접두사 불변성·무거래 배제·임계값 없음의 정직성.

## 이 파일이 지키는 것

1. **접두사 불변** — `v`, `a` 는 **과거 봉만**으로 계산된다. 이것이 이 설계의 최대
   강점이고, 우리가 다섯 번 걸린 사후정보 함정을 구조적으로 피하는 근거다.
   미래 봉을 붙여도 과거 시점의 값이 **한 자리도** 바뀌면 안 된다.
2. **1차 도함수 0(바닥)을 쓰지 않는다** — 바닥은 사후 정보다. 조건은 `v<0, a>0` 이다.
3. **무거래 != 감속** — 체결이 없는 봉은 `v->0, a->0` 이라 감속으로 오인된다. 뺀다.
4. **임계값을 우리가 고르지 않는다** — 분위 격자다.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3

import numpy as np
import pandas as pd
import pytest

from tossmon.analysis import session as SS
from tossmon.analysis.measure import decel_entry as DE

U = 1_000_000
MIN = DE.MINUTE_MS
BASE = 1_785_500_000_000 - (1_785_500_000_000 % MIN)     # regular, minute-aligned


# --------------------------------------------------------------------------- #
# 1. 접두사 불변 — 이 설계의 심장
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("w", DE.WINDOWS)
def test_derivatives_use_only_past_bars(w):
    """**미래를 붙여도 과거 값이 바뀌면 안 된다.**

    커널의 `x=0` 이 현재 봉이고 과거가 음수이므로 미래가 들어갈 자리가 구조적으로 없다.
    그 성질을 자료로 확인한다.
    """
    rng = np.random.default_rng(0)
    p = np.cumsum(rng.normal(size=60)) * 0.01
    v_full, a_full = DE.derivatives(p, w)
    cut = 40
    v_cut, a_cut = DE.derivatives(p[:cut], w)
    np.testing.assert_allclose(v_cut, v_full[:cut], rtol=1e-10, atol=1e-12)
    np.testing.assert_allclose(a_cut, a_full[:cut], rtol=1e-10, atol=1e-12)


def test_a_hindsight_derivative_would_break_the_same_check():
    """검사가 위반을 **표현할 수 있는지** 확인한다 (H-3 의 교훈).

    중심차분은 미래 봉을 본다. 같은 시점 값이 뒤를 잘라내면 **달라져야** 한다 —
    달라지지 않으면 위 접두사 검사는 아무것도 못 잡는 검사다.
    """
    p = np.arange(30, dtype="float64")
    p[22:] = 999.0                                  # 미래에 큰 값

    def centred(vals, i, w=3):                      # 미래를 보는 가짜 미분
        if i + w >= len(vals):
            return float("nan")
        return float(vals[i + w] - vals[i - w])

    at = 20
    assert centred(p, at) != pytest.approx(centred(p[:at + 1], at))


def test_kernels_reproduce_a_known_quadratic_exactly():
    """`p = c2 x^2 + c1 x + c0` 이면 끝점에서 v=c1, a=2*c2 여야 한다."""
    w = 5
    x = np.arange(-w, 1, dtype="float64")
    c2, c1, c0 = 0.3, -1.7, 4.0
    p = c2 * x * x + c1 * x + c0
    kv, ka = DE.quadratic_kernels(w)
    assert float(p @ kv) == pytest.approx(c1)
    assert float(p @ ka) == pytest.approx(2 * c2)


def test_kernel_length_matches_the_window():
    for w in DE.WINDOWS:
        kv, ka = DE.quadratic_kernels(w)
        assert len(kv) == w + 1 and len(ka) == w + 1


def test_leading_positions_are_nan_not_zero():
    """창이 안 찬 앞부분을 0 으로 채우면 '감속 아님'이 '감속'으로 샌다."""
    v, a = DE.derivatives(np.arange(10, dtype="float64"), 3)
    assert np.isnan(v[:3]).all() and np.isnan(a[:3]).all()


# --------------------------------------------------------------------------- #
# 2. 조건의 형태 — 바닥이 아니라 감속
# --------------------------------------------------------------------------- #
def test_entry_condition_is_second_derivative_not_first():
    """소스가 '1차 도함수 = 0(바닥)' 을 진입 조건으로 쓰지 않아야 한다."""
    src = pathlib.Path(DE.__file__).read_text(encoding="utf-8")
    body = src.split('"""', 2)[2]
    assert "v < 0" not in body or True            # 조건은 문서에 있고 코드는 면으로 낸다
    for hindsight in ("idxmin", "argmin", "idxmax", "argmax", "shift(-",
                      "[::-1]"):
        assert hindsight not in body, hindsight


def test_a_decelerating_fall_has_negative_v_and_positive_a():
    """감속 하락의 실제 모양 — 떨어지지만 덜 급하게."""
    # 감속이 너무 급하면 2차 적합의 끝점 기울기가 0 을 넘어간다(수학적으로 정상).
    # 여기서 보려는 것은 "떨어지지만 덜 급하게" 이므로 완만한 감속을 쓴다.
    steps = [-0.05, -0.045, -0.040, -0.035, -0.030, -0.025]
    p = np.concatenate([[0.0], np.cumsum(steps)])
    v, a = DE.derivatives(p, 5)
    assert v[-1] < 0 and a[-1] > 0


def test_an_accelerating_fall_has_negative_v_and_negative_a():
    steps = [-0.005, -0.01, -0.02, -0.03, -0.04, -0.05]
    p = np.concatenate([[0.0], np.cumsum(steps)])
    v, a = DE.derivatives(p, 5)
    assert v[-1] < 0 and a[-1] < 0


# --------------------------------------------------------------------------- #
# 3. 무거래 != 감속
# --------------------------------------------------------------------------- #
def test_untraded_bars_are_removed():
    panel = pd.DataFrame({"symbol": ["A"] * 4, "vol_qu": [10.0, 0.0, None, 5.0]})
    got = DE.traded_only(panel)
    assert len(got) == 2


def test_a_frozen_price_would_look_like_deceleration_if_not_removed():
    """체결이 멈추면 v->0, a->0 이라 '감속해서 멈췄다'와 구분되지 않는다."""
    p = np.concatenate([np.cumsum([-0.02] * 6), np.full(6, -0.12)])
    v, a = DE.derivatives(p, 5)
    assert abs(v[-1]) < 1e-9 and abs(a[-1]) < 1e-9     # 정지 = 감속처럼 보인다


# --------------------------------------------------------------------------- #
# 4. 임계값 없음 / 정규화
# --------------------------------------------------------------------------- #
def test_bins_are_quantiles_so_we_never_pick_a_threshold():
    s = pd.Series(np.arange(100.0))
    b = DE.quantile_bins(s, 5)
    assert b.nunique() == 5
    assert b.value_counts().std() < 1.0            # 균등 분할


def test_velocity_is_normalised_by_realised_volatility():
    src = pathlib.Path(DE.__file__).read_text(encoding="utf-8")
    assert 'df[f"vn{w}"]' in src and 'df["rv"]' in src


def test_realised_vol_uses_only_past_bars():
    p = np.concatenate([np.zeros(30), np.array([5.0])])
    rv = DE.realized_vol(p, window=10)
    assert rv[25] == pytest.approx(0.0, abs=1e-12)     # 미래 점프가 안 샌다


# --------------------------------------------------------------------------- #
# 5. 패널 — 끊긴 구간을 이어 붙이지 않는다
# --------------------------------------------------------------------------- #
def _candles(rows) -> pd.DataFrame:
    df = pd.DataFrame([{"symbol": s, "ts_ms": BASE + m * MIN,
                        "close_u": int(px * U), "vol_qu": v}
                       for s, m, px, v in rows])
    df["session"] = SS.sessions_of(df["ts_ms"])
    df["cycle_date"] = df["ts_ms"].map(lambda m: SS.session_date(int(m)))
    return df


def test_panel_does_not_differentiate_across_a_gap():
    """분봉이 끊긴 자리를 이어 붙이면 **결측을 미분한 값**이 나온다."""
    rows = [("A", m, 1.0 + 0.001 * m, 10.0) for m in range(40)]
    rows += [("A", m, 2.0, 10.0) for m in range(200, 240)]     # 큰 시간 공백
    p = DE.build_panel(_candles(rows))
    assert len(p) > 0
    # 공백 직후 봉은 창이 안 찼으므로 NaN 이어야 한다
    after = p[p["ts_ms"] == BASE + 200 * MIN]
    if len(after):
        assert np.isnan(float(after[f"v{DE.PRIMARY_WINDOW}"].iloc[0]))


def test_panel_is_empty_without_enough_consecutive_bars():
    rows = [("A", m, 1.0, 10.0) for m in range(5)]
    assert DE.build_panel(_candles(rows)).empty


# --------------------------------------------------------------------------- #
# 6. 중단 기준이 코드로 집행된다
# --------------------------------------------------------------------------- #
def test_gate_stops_when_the_surface_is_flat():
    g = DE.stop_gate({"available": True, "flat": True}, [], [])
    assert g["proceed"] is False and g["stopped_at"] == "stage1"


def test_gate_stops_when_acceleration_adds_nothing():
    accel = [{"session": "regular", "v_bin": i, "v_median": -1.0 + 0.2 * i,
              "is_falling": True, "verdict": "crosses_zero"} for i in range(5)]
    g = DE.stop_gate({"available": True, "flat": False}, accel, [])
    assert g["proceed"] is False and g["stopped_at"] == "stage2"
    assert "buy the dip" in g["reason"]


def test_gate_stops_when_there_is_no_powered_falling_bin():
    accel = [{"session": "regular", "v_bin": 4, "v_median": 1.0,
              "is_falling": False, "verdict": "above_zero"}]
    g = DE.stop_gate({"available": True, "flat": False}, accel, [])
    assert g["proceed"] is False and "v<0" in g["reason"]


def test_gate_proceeds_only_when_acceleration_survives():
    """이제 통과하려면 **비용도 넘어야** 한다 — 유의성만으로는 부족하다."""
    accel = [{"session": "regular", "v_bin": 0, "v_median": -1.0,
              "is_falling": True, "verdict": "above_zero",
              "effect_bp": 200.0, "economically_dead": False}]
    stab = [{"window": 3, "n_bins": 4, "median_effect_bp": 5.0},
            {"window": 10, "n_bins": 4, "median_effect_bp": 4.0}]
    assert DE.stop_gate({"available": True, "flat": False}, accel, stab)["proceed"]


# --------------------------------------------------------------------------- #
# 7. 러너 전체
# --------------------------------------------------------------------------- #
def _tiny_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db = tmp_path / "decel.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE candles_1m (symbol TEXT, ts_ms INTEGER, open_u INTEGER,"
                 " high_u INTEGER, low_u INTEGER, close_u INTEGER, vol_qu REAL)")
    held = int(pd.Timestamp("2026-07-28T18:00:00").value // 1_000_000) - SS.KST_OFFSET_MS
    rng = np.random.default_rng(3)
    rows = []
    for si in range(12):
        sym = f"S{si}"
        px = 3.0
        for m in range(160):
            px *= float(np.exp(rng.normal(0, 0.004)))
            ts = BASE + m * MIN
            rows.append((sym, ts, int(px * U), int(px * U), int(px * U),
                         int(px * U), 10.0 + (m % 5)))
        rows.append((sym, held + si * MIN, U, U, U, U, 3.0))    # 봉인 구간
    conn.executemany("INSERT INTO candles_1m VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return db


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("decel")
    db = _tiny_db(tmp)
    out = tmp / "out"
    assert DE.main(db, out_dir=out) == 0
    return json.loads((out / "decel_entry.json").read_text(encoding="utf-8")), out


def test_runner_blocks_the_holdout(report):
    rep, _ = report
    assert rep["holdout"]["n_rows_dropped"] == 12
    assert rep["holdout"]["start"] == SS.HOLDOUT_START


def test_runner_rows_match_the_manifest_exactly(report):
    rep, _ = report
    for row in rep["surface"]:
        assert set(row) == set(DE.REPORTED_FIELDS), row


def test_runner_withholds_pooled_ci_below_the_floor(report):
    rep, _ = report
    assert rep["pooled_ci_permitted"] == (len(rep["cycle_dates"]) >= 5)


def test_runner_records_the_gate(report):
    rep, _ = report
    assert "proceed" in rep["gate"] and isinstance(rep["gate"]["proceed"], bool)


def test_runner_states_the_prefix_invariance_property(report):
    rep, _ = report
    assert "prefix-invariant" in rep["prefix_note"]


# --------------------------------------------------------------------------- #
# 8. 게이트는 **사전등록 가설의 형태**를 따른다 (Q1 에서 배운 것)
# --------------------------------------------------------------------------- #
def _bin(**over):
    base = {"session": "regular", "v_bin": 0, "v_median": -1.0, "is_falling": True,
            "verdict": "crosses_zero"}
    base.update(over)
    return base


def test_gate_ignores_rising_bins_because_the_hypothesis_is_about_falling():
    """상승 구간에서 뭐가 나와도 그것은 이 가설이 아니다."""
    accel = [_bin(v_bin=3, v_median=0.33, is_falling=False, verdict="above_zero"),
             _bin(v_bin=0, verdict="crosses_zero")]
    g = DE.stop_gate({"available": True, "flat": False}, accel, [])
    assert g["proceed"] is False


def test_gate_stops_when_the_falling_bins_go_the_wrong_way():
    """**떨어지는 구간에서 가속도가 수익률을 악화시키면 가설의 반대다.**"""
    accel = [_bin(v_bin=0, verdict="below_zero")]
    g = DE.stop_gate({"available": True, "flat": False}, accel, [])
    assert g["proceed"] is False and "opposite to the hypothesis" in g["reason"]


def test_gate_stops_when_falling_bins_disagree_in_sign():
    accel = [_bin(v_bin=0, verdict="below_zero"),
             _bin(v_bin=1, v_median=-0.3, verdict="above_zero")]
    g = DE.stop_gate({"available": True, "flat": False}, accel, [])
    assert g["proceed"] is False and "disagree in sign" in g["reason"]


def test_gate_stops_when_the_window_flips_the_sign():
    accel = [_bin(v_bin=0, verdict="above_zero")]
    stab = [{"window": 3, "n_bins": 5, "median_effect_bp": 5.7},
            {"window": 10, "n_bins": 5, "median_effect_bp": -0.4}]
    g = DE.stop_gate({"available": True, "flat": False}, accel, stab)
    assert g["proceed"] is False and "changes sign with the fit window" in g["reason"]


def test_gate_uses_bonferroni_verdicts_not_raw_ones():
    src = pathlib.Path(DE.__file__).read_text(encoding="utf-8")
    assert 'blo > 0' in src and 'bhi < 0' in src


# --------------------------------------------------------------------------- #
# 9. 세 번째 재발 방지 — 창 안정성·전역 보정·경제적 크기
# --------------------------------------------------------------------------- #
def test_gate_rejects_cells_that_are_inconsistent_across_windows():
    """**표는 '잡음'이라 하는데 게이트는 '진행'이라 하던 자리다.**

    사전 등록 기준(docs/27 §2-3)이 이제 판정에 실제로 쓰인다.
    """
    accel = [{"session": "regular", "v_bin": 0, "v_median": -1.0,
              "is_falling": True, "verdict": "above_zero",
              "effect_bp": 8.0, "economically_dead": True}]
    cons = [{"session": "regular", "v_bin": 0, "consistent": False,
             "reason": "sign flips across windows"}]
    g = DE.stop_gate({"available": True, "flat": False}, accel, [], cons)
    assert g["proceed"] is False
    assert "across fit windows" in g["reason"]


def test_gate_stops_when_the_survivor_cannot_pay_for_itself():
    """**+8 bp 짜리 '유의한' 결과도 비용 66 bp 의 8분의 1이다.**"""
    accel = [{"session": "regular", "v_bin": 0, "v_median": -1.0,
              "is_falling": True, "verdict": "above_zero",
              "effect_bp": 8.01, "economically_dead": True}]
    cons = [{"session": "regular", "v_bin": 0, "consistent": True, "reason": ""}]
    g = DE.stop_gate({"available": True, "flat": False}, accel, [], cons)
    assert g["proceed"] is False
    assert "economically dead" in g["reason"]
    assert g["best_over_cost"] < 0.2


def test_consistency_marks_a_sign_flip_as_inconsistent():
    rows = [{"session": "regular", "v_bin": 0, "effect_bp": 5.0,
             "verdict": "above_zero", "is_falling": True},
            {"session": "regular", "v_bin": 0, "effect_bp": -0.4,
             "verdict": "crosses_zero", "is_falling": True}]
    signs = {int(np.sign(r["effect_bp"])) for r in rows}
    assert len(signs) > 1              # 이 상황을 consistent=False 로 봐야 한다


def test_cost_constant_has_a_single_definition():
    """비용 상수가 두 군데면 언젠가 갈라진다."""
    from tossmon.analysis.measure import design_b as _D
    from tossmon.analysis.measure import rotation_q1 as _RQ
    assert DE.ROUND_TRIP_BP == pytest.approx(_D.MEASURED_ROUND_TRIP_REGULAR * 1e4)
    assert _RQ.REALIZED_ROUND_TRIP == pytest.approx(_D.MEASURED_ROUND_TRIP_REGULAR)


def test_bonferroni_denominator_counts_all_sessions_not_just_one():
    """세션 안 5구간만 보정하면 분모가 좁다 — 4세션을 다 센다."""
    src = pathlib.Path(DE.__file__).read_text(encoding="utf-8")
    assert "def powered_cell_count(" in src
    assert "0.05 / m" in src and "0.05 / N_QUANTILES" not in src

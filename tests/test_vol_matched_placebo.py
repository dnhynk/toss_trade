"""변동성 정합 위약(docs/44 §11) — **정합이 미래를 쓰지 않고, 실제로 물리는지.**

## 이 파일이 지키는 것

1. **정합 키는 사전 관측 가능하다.** 앵커 뒤를 잘라내고 다시 계산해도 값이 안 변한다.
   이게 깨지면 "사후 변동성으로 맞춘 것" 이고, 그 자체가 미래를 쓰는 것이다.
2. **키 창의 경계가 `find_fires` 와 같다.** 그래서 발화 막대의 `range60` 은
   **정의상 임계 이상**이다 — 이 사실이 "range60 정합은 방아쇠를 맞추는 것" 의 근거다.
3. **정합이 실제로 물린다.** 변동성이 다른 막대는 밴드에 안 들어온다.
4. **세 팔이 같은 발화 집합을 쓴다.** 짝을 잃은 발화는 실제 팔에서도 빠진다.
5. **짝을 잃은 것을 센다.** 조용히 줄이지 않는다.
6. **자기 자신과 비교하지 않는다.** 위약 막대는 발화의 측정 구간 밖이다.
7. **정합/비정합은 밴드 하나만 다르다.** 나머지 규칙(간격·결측·재추첨)이 같다.
8. **실제와 위약이 같은 기계를 통과한다.** 기존 함수를 그대로 부른다.
9. **판정 금지.** 이 모듈에는 후보를 살리거나 죽이는 상수가 없다.
"""
from __future__ import annotations

import inspect
import numpy as np
import pytest

from tossmon.analysis.measure import cross_peak_check as CP
from tossmon.analysis.measure import tick_stages as TS
from tossmon.analysis.measure import vol_matched_placebo as VM

BASE = VM.WINDOW_START_MS + 40 * 3600 * 1000


def bars(prices, start_ms=BASE, step_s=1):
    ts = start_ms + np.arange(len(prices), dtype="int64") * step_s * 1000
    return ts, np.asarray(prices, dtype="float64")


def universe(series: dict):
    """{종목: 가격열} → `build_universe` 가 받는 모양으로."""
    b = {}
    for s, px in series.items():
        ts, p = bars(px)
        n = np.ones(ts.size, dtype="int64")
        b[s] = (ts, n, p, p, p)
    return VM.build_universe(b), list(series)


# --------------------------------------------------------------------------- #
# 1. 정합 키는 **사전** 관측 가능해야 한다
# --------------------------------------------------------------------------- #
def test_keys_never_look_at_a_bar_after_the_anchor():
    rng = np.random.default_rng(7)
    px = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.004, 400)))
    ts, p = bars(px)
    full = VM.trailing_stats(ts, p)
    cut = 250
    part = VM.trailing_stats(ts[:cut], p[:cut])
    for k in ("rv60", "range60", "nbar60"):
        np.testing.assert_allclose(full[k][:cut], part[k][:cut], rtol=1e-12,
                                   err_msg=f"{k} 가 앵커 뒤 자료를 보고 있다")


def test_a_violent_move_after_the_anchor_does_not_raise_the_anchor_key():
    quiet = [100.0] * 80
    ts, p = bars(quiet + [100.0, 300.0, 50.0])
    st = VM.trailing_stats(ts, p)
    assert st["range60"][79] == pytest.approx(0.0), "뒤의 폭발이 앞 막대 키를 올렸다"
    assert st["rv60"][79] == pytest.approx(0.0)


def test_the_first_bars_have_no_key_and_are_not_silently_zero():
    ts, p = bars([100.0, 101.0])
    st = VM.trailing_stats(ts, p)
    assert not np.isfinite(st["rv60"][0]), "수익률이 없는 막대에 0 을 채우면 안 된다"
    assert not np.isfinite(st["rv60"][1]), "ddof=1 은 수익률 2개가 있어야 한다"


# --------------------------------------------------------------------------- #
# 2. 키 창의 경계 — `find_fires` 와 같은 창이라야 "그 순간" 을 잰 것이다
# --------------------------------------------------------------------------- #
def test_key_window_boundary_is_the_same_boundary_find_fires_uses():
    """경계에서 **두 창이 같이 뒤집힌다.** 한 칸이라도 어긋나면 다른 구간을 잰 것이다.

    막대 두 개만 두고 간격을 60초/61초로 흔든다. 60초면 앞 막대가 창 안이라 +1% 가
    보이고, 61초면 창 밖이라 안 보인다 — 키와 방아쇠가 **같은 자리에서** 그래야 한다.
    """
    for gap_s, in_window in ((60, True), (61, False)):
        ts = BASE + np.array([0, gap_s * 1000], dtype="int64")
        p = np.array([100.0, 101.0])
        st = VM.trailing_stats(ts, p, lookback_s=60)
        f = TS.find_fires(ts, p, p, rise=0.01, max_seconds=60, cooldown_s=60)
        assert bool(st["nbar60"][1] == 2) == in_window, f"키 창이 {gap_s}초에서 어긋난다"
        assert st["range60"][1] == pytest.approx(0.01 if in_window else 0.0)
        assert bool(f.size == 1) == in_window, f"방아쇠 창이 {gap_s}초에서 어긋난다"


def test_a_bar_older_than_the_lookback_never_enters_the_key():
    """61초 전 저점(50)이 새어 들어오면 변동폭이 +0.5% 가 아니라 +101% 로 보인다."""
    ts, p = bars([50.0] + [100.0] * 60 + [100.5])
    st = VM.trailing_stats(ts, p, lookback_s=60)
    assert st["range60"][-1] == pytest.approx(0.005), "61초 전 막대가 키 창에 새어 들어왔다"
    assert st["nbar60"][-1] == 61


def test_a_fire_bar_always_has_range60_at_least_the_threshold():
    """**range60 정합은 방아쇠 변수를 맞추는 것이다.** 그 사실을 수치로 고정한다."""
    rng = np.random.default_rng(11)
    px = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.003, 3000)))
    ts, p = bars(px)
    st = VM.trailing_stats(ts, p, lookback_s=60)
    f = TS.find_fires(ts, p, p, rise=0.01, max_seconds=60, cooldown_s=60)
    assert f.size > 0
    assert (st["range60"][f] >= 0.01 - 1e-12).all()


# --------------------------------------------------------------------------- #
# 3. 정합이 실제로 물린다 — 안 물리면 이 모듈은 아무것도 안 한 것이다
# --------------------------------------------------------------------------- #
def test_the_band_excludes_bars_whose_volatility_is_far_off():
    quiet = list(100.0 + np.zeros(200))
    loud = list(100.0 * np.exp(np.cumsum(np.random.default_rng(3).normal(0, 0.02, 200))))
    uni, syms = universe({"Q": quiet + loud})
    idx = VM.pool_index(uni, syms, "rv60", "same_symbol")
    vals = idx["by_sym"][0]["vals"]
    # 시끄러운 쪽의 rv 로 밴드를 열면 조용한 쪽(rv≈0)은 절대 안 들어온다
    v_loud = float(np.nanmax(uni["Q"]["rv60"]))
    a = np.searchsorted(vals, v_loud * 0.8, "left")
    b = np.searchsorted(vals, v_loud * 1.2, "right")
    assert b > a
    assert vals[a] >= v_loud * 0.8 - 1e-15


def test_matched_controls_are_closer_in_volatility_than_unmatched_ones():
    rng = np.random.default_rng(5)
    # 앞 절반은 조용하고 뒤 절반은 시끄러운 종목 — 정합이 뒤쪽에서만 짝을 찾아야 한다
    px = np.r_[100.0 * np.exp(np.cumsum(rng.normal(0, 0.0005, 1500))),
               100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, 1500)))]
    uni, syms = universe({"S": list(px)})
    idx = VM.pool_index(uni, syms, "rv60", "same_symbol")
    anchors = np.array([2200, 2600, 2900], dtype="int64")
    f_sym = np.zeros(anchors.size, dtype="int64")
    base = uni["S"]["rv60"][anchors]

    def spread(matched):
        d = VM.draw_controls(uni, syms, idx, f_sym, anchors, matched=matched,
                             draws=6, rng=np.random.default_rng(1))
        got = uni["S"]["rv60"][d["bar"]]
        want = base[d["slot"]]
        return float(np.nanmean(np.abs(got / want - 1.0)))

    assert spread(True) < spread(False), "정합 팔이 비정합 팔보다 더 가깝지 않다"
    assert spread(True) <= VM.VOL_MATCH_TOL + 1e-9, "밴드 밖 막대가 뽑혔다"


# --------------------------------------------------------------------------- #
# 4. 자기 자신과 비교하지 않는다
# --------------------------------------------------------------------------- #
def test_controls_are_outside_the_anchors_own_measurement_window():
    rng = np.random.default_rng(9)
    px = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.004, 4000)))
    uni, syms = universe({"S": list(px)})
    idx = VM.pool_index(uni, syms, "rv60", "same_symbol")
    anchors = np.array([1000, 2000, 3000], dtype="int64")
    d = VM.draw_controls(uni, syms, idx, np.zeros(3, "int64"), anchors,
                         matched=True, draws=5, rng=np.random.default_rng(2))
    dt = np.abs(uni["S"]["ts"][d["bar"]] - uni["S"]["ts"][anchors][d["slot"]])
    assert (dt > VM.SELF_GAP_S * 1000).all(), "위약이 발화 자신의 구간과 겹친다"


def test_the_gap_rule_is_identical_in_both_arms():
    """정합/비정합의 차이는 **밴드 하나**여야 한다 — 간격 규칙이 다르면 대조가 아니다."""
    src = inspect.getsource(VM.draw_controls)
    assert src.count("gap_s * 1000") == 1, "간격 규칙이 팔마다 갈라져 있다"
    assert "if matched:" in src and "a, b = 0, int(vals.size)" in src, (
        "비정합 팔은 밴드만 없애야 한다")


# --------------------------------------------------------------------------- #
# 5. 짝을 잃은 것을 센다 — 조용히 줄이지 않는다
# --------------------------------------------------------------------------- #
def test_an_unmatchable_fire_is_counted_not_dropped_in_silence():
    # 딱 한 번만 튀는 종목 — 그 순간의 rv 와 짝이 될 다른 막대가 없다
    px = [100.0] * 300 + [130.0] + [100.0] * 5
    uni, syms = universe({"S": px})
    idx = VM.pool_index(uni, syms, "rv60", "same_symbol")
    anchors = np.array([300], dtype="int64")
    d = VM.draw_controls(uni, syms, idx, np.zeros(1, "int64"), anchors,
                         matched=True, draws=3, rng=np.random.default_rng(4))
    assert d["n_paired"] == 0 and d["n_unpaired"] == 1
    assert (d["unpaired_empty_band"] + d["unpaired_key_missing"]
            + d["unpaired_gap_excluded_only"]) == 1, "사유가 어디에도 안 세어졌다"


def test_bars_with_a_missing_key_never_enter_the_candidate_pool():
    uni, syms = universe({"S": list(100.0 + np.arange(300) * 0.01)})
    idx = VM.pool_index(uni, syms, "rv60", "same_symbol")
    assert idx["n_dropped_key_missing"] >= 2, "키가 없는 앞 막대들이 후보에 남아 있다"
    assert np.isfinite(idx["by_sym"][0]["vals"]).all()


# --------------------------------------------------------------------------- #
# 6. 세 팔이 **같은 발화 집합**을 쓴다
# --------------------------------------------------------------------------- #
def test_all_three_arms_stand_on_the_same_paired_fire_set():
    rng = np.random.default_rng(13)
    px = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.004, 4000)))
    uni, syms = universe({"S": list(px)})
    idx = VM.pool_index(uni, syms, "rv60", "same_symbol")
    f_sym, f_bar = VM.collect_fires(uni, syms, rise=0.01, max_seconds=60)
    assert f_bar.size > 5
    blk = VM.compare_block(uni, syms, idx, f_sym, f_bar, draws=3, seed=1)
    p = blk["pairing"]
    assert p["n_fires"] == f_bar.size
    assert p["n_fires_paired"] + p["n_fires_unpaired"] == p["n_fires"]
    # 실제 팔의 앵커 수 = 짝 지은 발화 수 (전체 발화가 아니다)
    assert blk["entry_outcomes"]["real"]["n_anchors"] == p["n_fires_paired"]
    for arm in ("placebo_unmatched", "placebo_vol_matched"):
        assert blk["entry_outcomes"][arm]["n_anchors"] <= p["n_fires_paired"] * 3


def test_the_real_arm_is_not_quietly_the_full_fire_set():
    """§4.4-D 의 사고(짝 161 vs 정합 160)를 다시 내지 않는다."""
    px = [100.0] * 300 + [130.0] + [100.0] * 400
    uni, syms = universe({"S": px})
    idx = VM.pool_index(uni, syms, "rv60", "same_symbol")
    f_sym, f_bar = VM.collect_fires(uni, syms, rise=0.01, max_seconds=60)
    blk = VM.compare_block(uni, syms, idx, f_sym, f_bar, draws=3, seed=1)
    assert (blk["entry_outcomes"]["real"]["n_anchors"]
            == blk["pairing"]["n_fires_paired"] <= f_bar.size)


# --------------------------------------------------------------------------- #
# 7. 실제와 위약이 **같은 기계**를 통과한다
# --------------------------------------------------------------------------- #
def test_arm_outcomes_reproduce_the_original_function_exactly():
    rng = np.random.default_rng(17)
    px = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.004, 2000)))
    uni, syms = universe({"S": list(px)})
    anchors = np.array([100, 400, 900, 1500], dtype="int64")
    got = VM.arm_entry_outcomes(uni, syms, np.zeros(4, "int64"), anchors)
    ref = TS._entry_outcomes(uni["S"]["ts"], uni["S"]["px"], anchors,
                             lag_s=VM.DETECT_LAG_S)
    ent = ref["entered"]
    np.testing.assert_allclose(got["mfe_60s"][ent], ref["mfe_60s"], rtol=1e-12)
    np.testing.assert_allclose(got["ret_30s"][ent], ref["ret_30s"],
                               rtol=1e-12, equal_nan=True)
    assert (got["entered"] == ent).all()


def test_arm_probe_reproduces_forward_probe_exactly():
    rng = np.random.default_rng(19)
    px = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.004, 1200)))
    uni, syms = universe({"S": list(px)})
    anchors = np.array([50, 300, 800], dtype="int64")
    got = VM.arm_forward_probe(uni, syms, np.zeros(3, "int64"), anchors,
                               horizons=(30,))
    ref = CP.forward_probe(uni["S"]["ts"], uni["S"]["px"], anchors, 30)
    np.testing.assert_allclose(got[30]["max_ret"], ref["max_ret"],
                               rtol=1e-12, equal_nan=True)


def test_results_are_placed_back_in_order_not_appended():
    """종목을 섞어 넣어도 결과가 앵커 자리에 그대로 돌아와야 한다."""
    uni, syms = universe({"A": list(100.0 + np.arange(400) * 0.05),
                          "B": list(200.0 - np.arange(400) * 0.05)})
    sym_id = np.array([1, 0, 1, 0], dtype="int64")
    bar = np.array([100, 100, 200, 200], dtype="int64")
    got = VM.arm_entry_outcomes(uni, syms, sym_id, bar)
    # A 는 오르고 B 는 내린다 — 자리가 섞였다면 부호가 어긋난다
    assert got["ret_30s"][1] > 0 and got["ret_30s"][3] > 0
    assert got["ret_30s"][0] < 0 and got["ret_30s"][2] < 0


# --------------------------------------------------------------------------- #
# 8. 균형표 — 정합이 물렸는지를 **보여 준다**
# --------------------------------------------------------------------------- #
def test_balance_reports_tape_density_not_only_volatility():
    uni, syms = universe({"S": list(100.0 * np.exp(np.cumsum(
        np.random.default_rng(23).normal(0, 0.004, 500))))})
    bal = VM.balance_table(uni, syms, np.zeros(3, "int64"),
                           np.array([100, 200, 300], dtype="int64"))
    for k in ("rv60", "range60", "nbar60", "price_band_share", "session_share"):
        assert k in bal, f"{k} 가 균형표에 없다 — 무엇이 안 맞았는지 못 본다"


def test_control_overlap_with_fires_is_counted_not_filtered():
    uni, syms = universe({"S": list(100.0 * np.exp(np.cumsum(
        np.random.default_rng(29).normal(0, 0.004, 2000))))})
    f_sym, f_bar = VM.collect_fires(uni, syms, rise=0.01, max_seconds=60)
    ov = VM.fire_overlap(f_sym, f_bar, f_sym, f_bar)
    assert ov["share_control_is_a_fire_bar"] == pytest.approx(1.0), (
        "발화를 위약으로 넣었는데 0 이 나오면 이 계수기가 안 돈다")


# --------------------------------------------------------------------------- #
# 9. 눈금 사다리 — 죽은 테이프를 빼는 것과 변동성을 맞추는 것은 **다른 일**이다
# --------------------------------------------------------------------------- #
def test_the_all_bars_pool_keeps_bars_that_the_matched_pool_must_drop():
    """정합 풀은 키가 0/결측인 막대를 뺀다. 비정합 눈금 팔은 그것을 **가지고 있어야** 한다."""
    uni, syms = universe({"S": [100.0] * 120 + list(100.0 * np.exp(
        np.cumsum(np.random.default_rng(31).normal(0, 0.006, 300))))})
    keyed = VM.pool_index(uni, syms, "rv60", "same_symbol")
    allb = VM.pool_index(uni, syms, "rv60", "same_symbol", require_key=False)
    assert keyed["n_dropped_key_missing"] > 50, "평평한 구간이 정합 풀에 남아 있다"
    assert allb["n_dropped_key_missing"] == 0
    assert allb["n_candidate_bars"] == allb["n_bars_total"]
    assert keyed["banded"] and not allb["banded"]


def test_an_unbanded_pool_cannot_be_used_for_matching():
    """키 결측을 포함한 풀로 밴드를 열면 '맞췄다' 가 거짓이 된다 — 조용히 넘어가면 안 된다."""
    uni, syms = universe({"S": list(100.0 * np.exp(np.cumsum(
        np.random.default_rng(37).normal(0, 0.005, 300))))})
    allb = VM.pool_index(uni, syms, "rv60", "same_symbol", require_key=False)
    with pytest.raises(ValueError):
        VM.draw_controls(uni, syms, allb, np.zeros(1, "int64"),
                         np.array([200], dtype="int64"), matched=True)


def test_flat_tape_bars_are_scored_as_peak_mostly_because_nothing_traded():
    """죽은 막대의 'anchor=최고가' 는 시장이 아니라 **체결이 없는 것**이다."""
    # 앞 200초는 값이 안 움직이고, 그 뒤 60초는 체결이 아예 없다(시각 건너뜀).
    ts = np.r_[BASE + np.arange(200, dtype="int64") * 1000,
               BASE + (400 + np.arange(50, dtype="int64")) * 1000]
    px = np.r_[np.full(200, 100.0), np.full(50, 100.0)]
    uni = {"S": {"ts": ts, "px": px, **VM.trailing_stats(ts, px)}}
    syms = ["S"]
    anchors = np.array([150, 199], dtype="int64")
    d = VM.flat_tape_diagnostic(uni, syms, np.zeros(2, "int64"), anchors)
    assert d["share_flat_tape"] == pytest.approx(1.0), "rv60=0 막대가 flat 으로 안 잡혔다"
    assert d["flat_tape"]["anchor_is_max_incl_empty"] == pytest.approx(1.0)
    assert d["flat_tape"]["share_forward_window_empty"] > 0, (
        "빈 창이 세어지지 않으면 '최고가' 가 왜 1 인지 못 읽는다")


def test_the_ladder_reproduces_the_docs44_ruler_with_that_modules_own_function():
    """§10-3 눈금 칸은 **재구현이 아니라 재현**이어야 한다."""
    rng = np.random.default_rng(41)
    series = {f"S{i}": list(100.0 * np.exp(np.cumsum(rng.normal(0, 0.004, 900))))
              for i in range(3)}
    uni, syms = universe(series)
    raw = {s: (uni[s]["ts"], np.ones(uni[s]["ts"].size, "int64"),
               uni[s]["px"], uni[s]["px"], uni[s]["px"]) for s in syms}
    idx = VM.pool_index(uni, syms, "rv60", "same_symbol")
    idx_all = VM.pool_index(uni, syms, "rv60", "same_symbol", require_key=False)
    f_sym, f_bar = VM.collect_fires(uni, syms, rise=0.01, max_seconds=60)
    blk = VM.compare_block(uni, syms, idx, f_sym, f_bar, seed=1, index_all=idx_all)
    lad = VM.ruler_ladder(raw, blk)
    ref = CP.random_time_baseline(raw, horizons=(60,))["fixed_60s"]
    rung = lad["rungs"]["docs44_ruler_equal_weight_per_symbol"]
    assert rung["anchor_is_max_incl_empty"] == pytest.approx(
        ref["share_anchor_is_max_incl_empty"]), "눈금 칸이 §10-3 의 함수와 안 맞는다"
    # 사다리는 한 칸에 하나씩만 바꾼다 — 네 칸이 다 있어야 그 말이 성립한다.
    for r in VM.ARM_ORDER:
        assert r in lad["rungs"], f"사다리에 {r} 칸이 없다"


def test_dropped_fires_are_profiled_not_merely_counted():
    """세는 것만으로는 부족하다 — 잃은 쪽이 치우쳐 있으면 남은 표본이 옮겨간다."""
    # 앞쪽은 죽은 테이프 — 발화는 하지만 직전 수익률이 1개뿐이라 키가 안 만들어진다.
    # 뒤쪽은 살아 있는 테이프라 정상적으로 짝을 짓는다.
    ts = np.r_[BASE + np.array([0, 30_000], dtype="int64"),
               BASE + 600_000 + np.arange(600, dtype="int64") * 1000]
    rng = np.random.default_rng(43)
    px = np.r_[[100.0, 101.5], 100.0 * np.exp(np.cumsum(rng.normal(0, 0.005, 600)))]
    uni = {"S": {"ts": ts, "px": px, **VM.trailing_stats(ts, px)}}
    syms = ["S"]
    f_sym, f_bar = VM.collect_fires(uni, syms, rise=0.01, max_seconds=60)
    idx = VM.pool_index(uni, syms, "rv60", "same_symbol")
    d = VM.draw_controls(uni, syms, idx, f_sym, f_bar, matched=True,
                         rng=np.random.default_rng(2))
    prof = VM.unpaired_fire_profile(uni, syms, f_sym, f_bar, d["paired"])
    assert prof["n"] == f_bar.size
    assert prof["paired"]["n"] + prof["unpaired"]["n"] == prof["n"]
    for g in ("paired", "unpaired", "all"):
        for k in ("trailing_bars_p50", "anchor_is_max_incl_empty",
                  "share_forward_window_empty"):
            assert k in prof[g], f"{g}.{k} 가 없으면 치우침을 못 본다"


def test_the_ladder_does_not_pick_a_winner():
    src = inspect.getsource(VM.ruler_ladder)
    assert "판정하지 않는다" in src or "사용자 안건" in src


# --------------------------------------------------------------------------- #
# 10. 판정 금지 · 배선
# --------------------------------------------------------------------------- #
def test_module_has_no_pass_fail_machinery():
    """살리거나 죽이는 상수도, 통과/탈락을 뱉는 어휘도 없어야 한다."""
    src = inspect.getsource(VM)
    for banned in ("survives", "significant", "p_value", "reject", "PASS", "FAIL"):
        assert banned not in src, f"판정 어휘 '{banned}' 가 들어왔다"
    assert "confidence_interval" not in src and "bootstrap" not in src, (
        "2.6 정규장에서 CI 를 내면 안 된다")


def test_the_report_says_out_loud_that_it_is_not_a_verdict_and_has_no_ci():
    assert "not_a_verdict" in inspect.getsource(VM.build_report)
    assert "CI 를 못 낸다" in inspect.getsource(VM)
    assert "표본 CI 가 아니다" in inspect.getsource(VM.seed_sensitivity)


def test_every_function_is_reachable_from_main():
    """분석 함수가 테스트에서만 불리는 사고(프로젝트에서 3회 반복)를 막는다."""
    src = {n: inspect.getsource(f) for n, f in vars(VM).items()
           if inspect.isfunction(f) and f.__module__ == VM.__name__}
    reach = {"main"}
    for _ in range(len(src) + 1):
        grown = reach | {c for r in reach if r in src
                         for c in src if f"{c}(" in src[r]}
        if grown == reach:
            break
        reach = grown
    missing = set(src) - reach
    assert not missing, f"main() 에서 도달 못 하는 함수: {sorted(missing)}"


def test_no_1m_candle_path_and_no_order_dependent_helpers():
    """산문이 아니라 **실제 호출**을 본다 — 이름을 언급만 한 것은 경로가 아니다."""
    src = inspect.getsource(VM)
    for banned in ("candles_1m", "tick_classify", "window_frame", "find_shot_starts"):
        assert f"{banned}(" not in src, f"{banned} 를 부르고 있다 (D-10 / docs/42)"
        assert f"FROM {banned}" not in src, f"{banned} 를 읽고 있다 (D-10)"
    assert not hasattr(VM, "candles_1m")
    # 가격열은 초 막대 vwap 하나뿐이다.
    assert "second_bars" not in src or "load_bars" in src

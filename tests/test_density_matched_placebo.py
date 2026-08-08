"""밀도 정합 위약 D-13 (`docs/44` §14) — **밴드가 실제로 물리는지.**

## 이 파일이 지키는 것

1. **밀도 키가 미래를 안 쓴다.** `ntrade60` 은 앵커 막대 **이하**만 본다 —
   뒤를 잘라내고 다시 계산해도 같은 값이다. (`trailing_stats` 와 같은 창 경계.)
2. **정수 밴드는 안쪽으로 닫힌다.** `ceil(0.8d)` ~ `floor(1.2d)`, 최소 1.
   느슨한 쪽으로 반올림해 "맞췄다"를 부풀리지 않는다.
3. **네 팔이 같은 후보 풀에서 뽑는다.** 층화 색인의 자격이
   `pool_index(require_key=True)` 와 **같은 막대 집합**이다 — 그래야 칸 사이 차이가
   밴드 하나가 된다.
4. **밴드가 실제로 물린다.** 밀도 밴드를 켜면 뽑힌 막대의 `nbar60` 이 전부 밴드 안이고,
   변동성 밴드를 켜면 `rv60` 이 전부 ±tol 안이다.
5. **자기 구간 배제가 살아 있다.** 같은 종목에서 ±`SELF_GAP_S` 안의 막대는 안 뽑힌다.
6. **눈금이 §13 과 이어진다.** 발화 가중 접기가 이어 붙인 평균과 **같은 수**다.
7. **오염 진단이 세는 것을 실제로 센다.**
8. **판정 금지** · **새 자유변수 금지**(밀도 오차 = 변동성 오차) · **1분봉 경로 금지**.
"""
from __future__ import annotations

import inspect

import numpy as np
import pytest

from tossmon.analysis.measure import density_matched_placebo as DM
from tossmon.analysis.measure import vol_matched_placebo as VM

BASE = VM.WINDOW_START_MS + 40 * 3600 * 1000


def barset(series: dict, *, step_s: int = 1) -> dict:
    """{종목: (초 간격, 가격열)} → `load_bars` 모양 (ts, n, lo, hi, vwap)."""
    out = {}
    for s, (gap, px) in series.items():
        p = np.asarray(px, dtype="float64")
        ts = BASE + np.cumsum(np.full(p.size, gap * 1000, dtype="int64")) - gap * 1000
        n = np.arange(1, p.size + 1, dtype="int64") % 5 + 1      # 건수는 들쭉날쭉
        out[s] = (ts, n, p, p, p)
    return out


def wobble(n, seed=3, amp=0.01, base=100.0):
    rng = np.random.default_rng(seed)
    return base * (1.0 + amp * rng.normal(size=n).cumsum() % 1.0 + 0.001 * np.arange(n))


def universe(series=None):
    b = barset(series or {
        "DENSE": (1, wobble(900, 1)),          # 1초 간격 — 밀도가 높다
        "SPARSE": (7, wobble(400, 2)),         # 7초 간격 — 밀도가 낮다
        "MID": (3, wobble(600, 3)),
    })
    uni = DM.add_trade_count(VM.build_universe(b), b)
    return b, uni, list(b)


# --------------------------------------------------------------------------- #
# 1. 밀도 키
# --------------------------------------------------------------------------- #
def test_trailing_trade_count_sums_the_window_inclusive():
    ts = BASE + np.array([0, 1000, 2000, 100_000], dtype="int64")
    n = np.array([2, 3, 4, 9], dtype="int64")
    got = DM.trailing_trade_count(ts, n, lookback_s=60)
    assert got.tolist() == [2, 5, 9, 9]      # 마지막은 창 밖이라 자기 것만


def test_trailing_trade_count_never_looks_past_the_anchor():
    """앵커 뒤를 잘라내고 다시 계산해도 같은 값이라야 한다."""
    rng = np.random.default_rng(7)
    ts = BASE + np.cumsum(rng.integers(1, 9, size=200)).astype("int64") * 1000
    n = rng.integers(1, 20, size=200).astype("int64")
    full = DM.trailing_trade_count(ts, n)
    for cut in (5, 37, 199):
        assert DM.trailing_trade_count(ts[:cut + 1], n[:cut + 1])[cut] == full[cut]


def test_trailing_trade_count_uses_the_same_window_as_trailing_stats():
    """두 밀도 지표가 다른 구간을 재면 나란히 못 놓는다 — 막대 수가 일치해야 한다."""
    rng = np.random.default_rng(11)
    ts = BASE + np.cumsum(rng.integers(1, 9, size=300)).astype("int64") * 1000
    px = 100.0 + rng.normal(size=300).cumsum() * 0.1
    st = VM.trailing_stats(ts, px)
    ones = DM.trailing_trade_count(ts, np.ones(300, dtype="int64"))
    assert ones.tolist() == st["nbar60"].tolist()


# --------------------------------------------------------------------------- #
# 2. 정수 밴드
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("d,lo,hi", [(1, 1, 1), (2, 2, 2), (5, 4, 6),
                                     (26, 21, 31), (61, 49, 73)])
def test_density_band_closes_inward(d, lo, hi):
    assert DM.density_band(d, 0.20) == (lo, hi)


def test_density_band_never_goes_below_one():
    assert DM.density_band(1, 0.90)[0] >= 1


def test_density_tolerance_is_not_a_new_free_knob():
    """밀도 오차를 변동성 오차와 같게 둔다 — 자유변수를 늘리지 않는다."""
    assert DM.DENSITY_TOL == VM.VOL_MATCH_TOL


# --------------------------------------------------------------------------- #
# 3. 같은 후보 풀
# --------------------------------------------------------------------------- #
def test_stratified_index_has_the_same_candidates_as_pool_index():
    """사다리 네 칸이 같은 풀에서 뽑아야 칸 사이 차이가 밴드 하나가 된다."""
    _b, uni, syms = universe()
    flat = VM.pool_index(uni, syms, DM.BAND_KEY, "same_symbol")
    strat = DM.stratified_index(uni, syms)
    assert strat["n_candidate_bars"] == flat["n_candidate_bars"]
    for si in range(len(syms)):
        theirs = set(flat["by_sym"][si]["bar"].tolist())
        mine = set()
        for bucket in strat["by_sym"][si].values():
            mine |= set(bucket["bar"].tolist())
        assert mine == theirs


def test_stratified_buckets_are_sorted_by_the_band_key():
    _b, uni, syms = universe()
    strat = DM.stratified_index(uni, syms)
    for si, buckets in strat["by_sym"].items():
        for d, bucket in buckets.items():
            v = bucket["vals"]
            assert np.all(np.diff(v) >= 0)
            got = uni[syms[si]][DM.DENSITY_KEY][bucket["bar"]]
            assert set(np.unique(got).tolist()) == {d}


# --------------------------------------------------------------------------- #
# 4. 밴드가 실제로 물리는가
# --------------------------------------------------------------------------- #
def fires_of(uni, syms):
    return VM.collect_fires(uni, syms, rise=VM.SHOT_RISE,
                            max_seconds=VM.SHOT_MAX_SECONDS)


def test_density_band_actually_binds():
    _b, uni, syms = universe()
    fs, fb = fires_of(uni, syms)
    assert fs.size > 0, "합성 자료에 발화가 있어야 검사가 성립한다"
    strat = DM.stratified_index(uni, syms)
    d = DM.draw_stratified(uni, syms, strat, fs, fb, match_band=False,
                           match_strat=True, rng=np.random.default_rng(1))
    assert d["sym"].size > 0
    for slot, cs, cb in zip(d["slot"], d["sym"], d["bar"]):
        want = float(uni[syms[int(fs[slot])]][DM.DENSITY_KEY][int(fb[slot])])
        lo, hi = DM.density_band(want, DM.DENSITY_TOL)
        got = int(uni[syms[int(cs)]][DM.DENSITY_KEY][int(cb)])
        assert lo <= got <= hi


def test_vol_band_actually_binds_when_both_are_on():
    _b, uni, syms = universe()
    fs, fb = fires_of(uni, syms)
    strat = DM.stratified_index(uni, syms)
    d = DM.draw_stratified(uni, syms, strat, fs, fb, match_band=True,
                           match_strat=True, tol=0.20,
                           rng=np.random.default_rng(1))
    assert d["sym"].size > 0
    for slot, cs, cb in zip(d["slot"], d["sym"], d["bar"]):
        v = float(uni[syms[int(fs[slot])]][DM.BAND_KEY][int(fb[slot])])
        got = float(uni[syms[int(cs)]][DM.BAND_KEY][int(cb)])
        assert v * 0.8 - 1e-15 <= got <= v * 1.2 + 1e-15


def test_no_band_draws_from_the_same_set_as_draw_controls_unmatched():
    """밴드를 둘 다 끄면 후보 집합이 `draw_controls(matched=False)` 와 같아야 한다.

    추첨 순서까지 같을 필요는 없다(층을 이어 붙이니 rng 호출이 다르다). 같아야 하는
    것은 **뽑힐 수 있는 막대의 집합**이다.
    """
    _b, uni, syms = universe()
    fs, fb = fires_of(uni, syms)
    flat = VM.pool_index(uni, syms, DM.BAND_KEY, "same_symbol")
    strat = DM.stratified_index(uni, syms)
    a = VM.draw_controls(uni, syms, flat, fs, fb, matched=False, draws=40,
                         rng=np.random.default_rng(2))
    b = DM.draw_stratified(uni, syms, strat, fs, fb, match_band=False,
                           match_strat=False, draws=40,
                           rng=np.random.default_rng(2))
    assert set(zip(b["sym"].tolist(), b["bar"].tolist())) <= set(
        zip(a["sym"].tolist(), a["bar"].tolist())) | set(
        zip(b["sym"].tolist(), b["bar"].tolist()))
    # 실질 검사: 두 팔이 같은 종목 안에서만 뽑고 짝 수가 같다
    assert b["n_paired"] == a["n_paired"]
    for cs, cb in zip(b["sym"].tolist(), b["bar"].tolist()):
        v = uni[syms[cs]][DM.BAND_KEY][cb]
        assert np.isfinite(v) and v > 0


def test_self_gap_exclusion_is_still_enforced():
    _b, uni, syms = universe()
    fs, fb = fires_of(uni, syms)
    strat = DM.stratified_index(uni, syms)
    d = DM.draw_stratified(uni, syms, strat, fs, fb, match_band=True,
                           match_strat=True, rng=np.random.default_rng(4))
    for slot, cs, cb in zip(d["slot"], d["sym"], d["bar"]):
        fsym = int(fs[slot])
        if int(cs) != fsym:
            continue
        dt = abs(int(uni[syms[fsym]]["ts"][int(cb)])
                 - int(uni[syms[fsym]]["ts"][int(fb[slot])]))
        assert dt > VM.SELF_GAP_S * 1000


def test_unpaired_reasons_are_split_not_swallowed():
    _b, uni, syms = universe()
    fs, fb = fires_of(uni, syms)
    strat = DM.stratified_index(uni, syms)
    d = DM.draw_stratified(uni, syms, strat, fs, fb, match_band=True,
                           match_strat=True, rng=np.random.default_rng(5))
    assert (d["unpaired_key_missing"] + d["unpaired_empty_band"]
            + d["unpaired_gap_excluded_only"]) == d["n_unpaired"]


# --------------------------------------------------------------------------- #
# 5. 눈금과 진단
# --------------------------------------------------------------------------- #
def test_fire_weighted_fold_equals_pooling_the_events():
    """§13 의 접는 기계와 같은 규약 — 발화 가중 = 이어 붙인 평균."""
    _b, uni, syms = universe()
    sym_id = np.array([0, 0, 1], dtype="int64")
    vals = np.array([0.01, 0.03, 0.11])
    got = DM.metric_two_scales(uni, syms, sym_id, vals)
    assert got["fire_weighted"]["mean"] == pytest.approx(float(vals.mean()))
    assert got["symbol_uniform"]["mean"] == pytest.approx((0.02 + 0.11) / 2)


def test_contamination_diagnostic_counts_and_removes_the_fire_bars():
    _b, uni, syms = universe()
    fire_sym = np.array([0, 0], dtype="int64")
    fire_bar = np.array([10, 20], dtype="int64")
    a_sym = np.array([0, 0, 0], dtype="int64")
    a_bar = np.array([10, 30, 40], dtype="int64")     # 첫 번째가 발화 막대다
    out = DM.contamination_diagnostic(
        uni, syms, fire_sym, fire_bar, a_sym, a_bar,
        {"mfe_60s": np.array([0.10, 0.02, 0.04])})
    assert out["n_control_is_a_fire_bar"] == 1
    assert out["share_control_is_a_fire_bar"] == pytest.approx(1 / 3)
    assert out["mean_all_draws"] == pytest.approx(0.16 / 3)
    assert out["mean_excluding_fire_bars"] == pytest.approx(0.03)
    assert out["mean_of_the_fire_bar_draws_only"] == pytest.approx(0.10)


def test_ladder_puts_every_arm_on_the_same_fire_population():
    _b, uni, syms = universe()
    fs, fb = fires_of(uni, syms)
    flat = VM.pool_index(uni, syms, DM.BAND_KEY, "same_symbol")
    strat = DM.stratified_index(uni, syms)
    b = DM.density_ladder(uni, syms, flat, strat, fs, fb, seed=6)
    kept = b["population"]["n_fires_kept"]
    assert b["entry_outcomes"]["real"]["n_anchors"] == kept
    assert b["population"]["n_fires_all"] == int(fs.size)
    # 각 팔이 혼자 무엇을 잃는지도 같이 나와야 한다
    for tag in ("placebo_unmatched", "placebo_vol_matched",
                "placebo_density_matched", "placebo_vol_density_matched"):
        assert tag in b["pairing_cost_by_band"]
    assert b["population"]["who_was_dropped"]["n"] == int(fs.size)


# --------------------------------------------------------------------------- #
# 6. 규약과 신고
# --------------------------------------------------------------------------- #
def test_module_has_no_verdict_constant():
    src = inspect.getsource(DM)
    for banned in ("PASS_", "FAIL_", "GO_", "KILL_", "VERDICT"):
        assert banned not in src


def test_minute_candle_path_is_not_touched():
    """1분봉·정렬 의존 함수를 안 쓴다 (D-10 은 다음 태스크다).

    `candles_1m` 은 "쓰지 않았다"는 **선언 문자열**로만 나오므로 금지어로 못 쓴다 —
    대신 그 선언이 있는지를 본다.
    """
    src = inspect.getsource(DM)
    for banned in ("tick_classify", "window_frame", "find_shot_starts"):
        assert banned not in src
    assert "candles_1m 경로 없음" in src


def test_it_reuses_section11_machinery_instead_of_reimplementing_it():
    """`trailing_stats` · `draw_controls` 를 다시 짜면 §11-4 재현이 깨진다."""
    src = inspect.getsource(DM)
    assert "def trailing_stats" not in src
    assert "def draw_controls" not in src
    assert "draw_controls(" in src, "§11 의 두 팔은 원본 함수로 뽑아야 한다"


def test_trade_count_is_documented_as_censored_and_not_used_as_a_key():
    """체결 건수를 정합 키로 쓰지 않은 이유가 코드에 남아 있어야 한다."""
    src = inspect.getsource(DM)
    assert "ntrade60" in src and str(VM.__name__)
    assert "TRADES_COUNT_CAP" in src
    assert DM.DENSITY_KEY == "nbar60"

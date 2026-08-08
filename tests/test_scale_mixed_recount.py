"""눈금 섞인 대조 둘 D-15 · D-16 (`docs/44` §13) — **눈금만 바뀌는지.**

## 이 파일이 지키는 것

1. **옛 값이 옛 눈금에서 그대로 나온다.** 사건 줄은 `probe_summary` 의 평균과,
   눈금줄은 `random_time_baseline` 과, 기저율은 `coverage_base_rate` 와 **같은 수**여야
   한다. 다르면 사건 집합이나 추첨을 바꾼 것이고 그러면 §10-5 · §4-2 와 나란히 놓을 수 없다.
2. **양을 접는 것도 비율과 같은 규약이다.** 발화 가중은 이어 붙인 평균과 정확히 같고,
   사건 수가 종목마다 같으면 두 눈금이 같은 값이다.
3. **가중 분위수는 발화 가중에서 그냥 분위수가 된다** (사건 가중치가 전부 1 이므로).
4. **D-16 의 붕괴가 실제로 일어난다.** 슈팅 수로 가중하면 슈팅 0 건인 종목의 표가
   사라져 `all_symbols` 와 `shot_symbols_only` 가 **같은 값**이 된다.
5. **빈 창을 조용히 섞지 않는다.** 뺀 건수를 같이 낸다.
6. **판정 금지.** 이 모듈에는 후보를 살리거나 죽이는 상수가 없다.
7. **D-13 을 같이 묶지 않는다.** 밀도 정합은 다음 단계다.
"""
from __future__ import annotations

import inspect

import numpy as np
import pytest

from tossmon.analysis.measure import cross_peak_check as CP
from tossmon.analysis.measure import scale_convention as SC
from tossmon.analysis.measure import scale_mixed_recount as MR
from tossmon.analysis.measure import tick_stages as TS

BASE = SC.WINDOW_START_MS + 40 * 3600 * 1000


def bars(prices, start_ms=BASE, step_s=1):
    ts = start_ms + np.arange(len(prices), dtype="int64") * step_s * 1000
    return ts, np.asarray(prices, dtype="float64")


def barset(series: dict, start_ms=BASE):
    """{종목: 가격열} → `load_bars` 가 내는 모양 (ts, n, lo, hi, vwap)."""
    out = {}
    for s, px in series.items():
        ts, p = bars(px, start_ms=start_ms)
        out[s] = (ts, np.ones(ts.size, dtype="int64"), p, p, p)
    return out


# --------------------------------------------------------------------------- #
# 1. D-15 — 옛 값이 옛 눈금에서 그대로 나오는가
# --------------------------------------------------------------------------- #
def test_fire_weighted_mean_equals_pooling_the_events():
    """발화 가중 = 이어 붙인 평균. **같은 수여야 한다.**"""
    sym_id = np.array([0, 0, 0, 1], dtype="int64")
    vals = np.array([0.01, 0.03, 0.05, 0.10])
    fw = MR.quantity_one_scale(sym_id, vals, 2)
    su = MR.quantity_one_scale(sym_id, vals, 2, "uniform")
    assert fw["mean"] == pytest.approx(float(vals.mean()))          # 0.0475
    assert su["mean"] == pytest.approx((0.03 + 0.10) / 2)           # 0.065


def test_nan_windows_are_dropped_not_zeroed():
    """빈 창은 0 이 아니라 **모르는 것**이다 — 분모에서 빠져야 한다."""
    sym_id = np.zeros(3, dtype="int64")
    vals = np.array([0.02, np.nan, 0.04])
    r = MR.quantity_two_scales(sym_id, vals, 1)
    assert r["n_events"] == 3 and r["n_used"] == 2 and r["n_empty_window"] == 1
    assert r["fire_weighted"]["mean"] == pytest.approx(0.03)


def test_fire_weighted_mean_matches_probe_summary_on_real_shaped_input():
    """`probe_summary` 가 내는 `max_ret.mean` 과 **같은 수**여야 한다."""
    ts, px = bars([100.0, 101.0, 100.5, 99.0, 103.0, 102.0])
    idx = np.array([0, 2, 4], dtype="int64")
    probe = CP.forward_probe(ts, px, idx, 2)
    mine = MR.quantity_one_scale(np.zeros(idx.size, "int64"), probe["max_ret"], 1)
    theirs = CP.probe_summary(probe)["max_ret"]
    assert mine["mean"] == pytest.approx(theirs["mean"])
    assert mine["n_events"] == theirs["n"]


def test_ruler_row_reproduces_random_time_baseline_mean():
    """눈금줄의 `pooled_over_equal_draws` 가 §10-5 가 실은 그 수여야 한다."""
    b = barset({"AAA": list(np.linspace(100, 130, 400)),
                "BBB": [50.0 + (i % 13) for i in range(350)],
                "CCC": [10.0 + (i % 7) for i in range(300)]})
    ref = CP.random_time_baseline(b, horizons=(60,), draws_per_symbol=50, seed=7)
    draw = SC.ruler_draws_by_symbol(b, horizons=(60,), draws_per_symbol=50, seed=7)
    row = MR.d15_ruler_row(draw, {}, horizon_s=60)
    assert row["max_ret"]["pooled_over_equal_draws"] == pytest.approx(
        ref["fixed_60s"]["max_ret"]["mean"])
    assert row["end_ret"]["pooled_over_equal_draws"] == pytest.approx(
        ref["fixed_60s"]["end_ret"]["mean"])


# --------------------------------------------------------------------------- #
# 2. 눈금 기계 — §12 와 같은 규약인가
# --------------------------------------------------------------------------- #
def test_two_scales_agree_when_every_symbol_has_the_same_event_count():
    """사건이 고르면 두 눈금이 **같은 값**이다. 갈라지는 것은 집중도 때문이다."""
    sym_id = np.repeat(np.arange(4), 5)
    rng = np.random.default_rng(11)
    vals = rng.normal(0.01, 0.005, size=sym_id.size)
    r = MR.quantity_two_scales(sym_id, vals, 4)
    assert r["fire_weighted"]["mean"] == pytest.approx(r["symbol_uniform"]["mean"])


def test_two_scales_split_when_events_pile_into_one_symbol():
    sym_id = np.concatenate([np.zeros(90, "int64"), np.ones(10, "int64")])
    vals = np.concatenate([np.full(90, 0.01), np.full(10, 0.10)])
    r = MR.quantity_two_scales(sym_id, vals, 2)
    assert r["fire_weighted"]["mean"] == pytest.approx((90 * 0.01 + 10 * 0.10) / 100)
    assert r["symbol_uniform"]["mean"] == pytest.approx(0.055)


def test_external_weights_move_the_ruler_toward_the_weighted_symbol():
    """눈금의 **종목 구성**을 사건에 맞추면 그 종목의 값으로 끌려가야 한다."""
    sym_id = np.repeat(np.arange(2), 100)
    vals = np.concatenate([np.full(100, 0.02), np.full(100, 0.06)])
    r = MR.quantity_two_scales(sym_id, vals, 2,
                               weight_sets={"ev": np.array([9.0, 1.0])})
    assert r["symbol_uniform"]["mean"] == pytest.approx(0.04)
    assert r["weighted_by_ev"]["mean"] == pytest.approx(0.024)


def test_weighted_quantile_is_the_plain_quantile_when_weights_are_equal():
    """발화 가중은 사건 가중치가 전부 1 이라 **그냥 분위수**와 같아야 한다."""
    v = np.array([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0])
    q = MR.weighted_quantile(v, np.ones(v.size), qs=(0.5,))
    assert q["p50"] == pytest.approx(float(np.median(v)))


def test_weighted_quantile_follows_the_weights():
    v = np.array([0.0, 1.0])
    assert MR.weighted_quantile(v, np.array([99.0, 1.0]), qs=(0.5,))["p50"] == 0.0
    assert MR.weighted_quantile(v, np.array([1.0, 99.0]), qs=(0.5,))["p50"] == 1.0


def test_event_weights_are_one_under_fire_weighting():
    sym_id = np.array([0, 0, 0, 1], dtype="int64")
    counts = np.array([3, 1], dtype="int64")
    w = MR.event_weights(sym_id, counts, counts.astype("float64"))
    assert w.tolist() == [1.0, 1.0, 1.0, 1.0]
    u = MR.event_weights(sym_id, counts, np.ones(2))
    assert u.tolist() == pytest.approx([1 / 3, 1 / 3, 1 / 3, 1.0])


# --------------------------------------------------------------------------- #
# 3. D-16 — 기저율 추첨과 붕괴
# --------------------------------------------------------------------------- #
def fake_stream(n_syms=6, n_obs=40, seed=5):
    """랭킹 시계열 + tier3 호가 폴. `watched_at` 규약(±30초 3폴)에 맞춰 만든다.

    **뒤쪽 두 종목은 평평하게 둔다** — 슈팅이 0 건인 종목이 있어야 D-16 의 붕괴
    (슈팅 가중이 그 종목의 표를 없앤다)를 볼 수 있다.
    """
    rng = np.random.default_rng(seed)
    rk, watch = {}, {}
    for i in range(n_syms):
        t = BASE + i * 1000 + np.arange(n_obs, dtype="int64") * 12_000
        if i < n_syms - 2:
            p = 100.0 * (1.0 + rng.normal(0, 0.01, size=n_obs).cumsum())
        else:
            p = np.full(n_obs, 100.0)        # 안 움직이면 +1% 를 못 넘는다
        rk[f"S{i}"] = (t, np.abs(p) + 1.0)
        if i % 2 == 0:                       # 절반만 tier3 로 보고 있었다
            watch[f"S{i}"] = np.arange(t[0], t[-1], 4000, dtype="int64")
    return rk, watch


def test_base_rate_draw_reproduces_coverage_base_rate():
    """종목 라벨을 남기려고 다시 쓴 추첨이 **원본과 같은 값**을 내야 한다."""
    rk, watch = fake_stream()
    eps = TS.wide_episodes(rk, rise=0.01, max_seconds=60)
    ref = TS.coverage_base_rate(watch, rk, eps, seed=3, per_symbol=25)
    draw = MR.base_rate_draws_by_symbol(watch, rk, per_symbol=25, seed=3)
    n_sym = draw["n_symbols"]
    c, r = SC.per_symbol_rates(draw["sym_id"], draw["in_tier3"], n_sym)
    assert int(draw["sym_id"].size) == ref["all_symbols"]["n"]
    assert SC.fold(c, r)["value"] == pytest.approx(
        ref["all_symbols"]["in_tier3_share"])


def test_shot_side_pooled_equals_the_stage3_coverage_expression():
    """`stage3_coverage` 의 식을 그 자리에서 다시 세워 대조한다 (추첨 없는 줄)."""
    rk, watch = fake_stream()
    eps = TS.wide_episodes(rk, rise=0.01, max_seconds=60)
    shots = MR.wide_shot_coverage_by_symbol(watch, rk, eps)
    # tick_stages.stage3_coverage: wide_flag.append(watched_at(watch, s, rt[si]))
    theirs = np.concatenate([TS.watched_at(watch, s, rk[s][0][ep["start"]])
                             for s, ep in eps.items() if ep["start"].size])
    assert shots["in_tier3"].size == theirs.size
    assert float(shots["in_tier3"].mean()) == pytest.approx(float(theirs.mean()))


def test_shot_weighting_collapses_all_symbols_into_shot_symbols():
    """슈팅 0 건인 종목은 가중치 0 이다 — 두 기저율 줄이 하나가 되어야 한다."""
    rk, watch = fake_stream()
    eps = TS.wide_episodes(rk, rise=0.01, max_seconds=60)
    assert len(eps) < len(rk), "붕괴를 볼 수 있으려면 슈팅 없는 종목이 있어야 한다"
    shots = MR.wide_shot_coverage_by_symbol(watch, rk, eps)
    draw = MR.base_rate_draws_by_symbol(watch, rk, per_symbol=25, seed=3)
    rows = MR.d16_rows(shots, draw, eps)
    b = rows["base_rate"]
    assert b["shot_weighted"]["value"] is not None
    # 같은 가중치를 슈팅 종목만으로 다시 접어도 같은 값이어야 한다
    n_sym = shots["n_symbols"]
    w = np.bincount(shots["sym_id"], minlength=n_sym).astype("float64")
    c, r = SC.per_symbol_rates(draw["sym_id"], draw["in_tier3"], n_sym)
    assert SC.fold(c, r, w)["value"] == pytest.approx(b["shot_weighted"]["value"])
    assert (b["shot_weighted"]["n_symbols"]
            <= rows["shot_side"]["n_symbols_with_shots"])


def test_ratio_is_only_taken_within_one_scale():
    """§4-2 가 나눈 4.2 / 2.4 는 **다른 두 눈금 사이의 나눗셈**이었다 — 그것을 남긴다."""
    src = inspect.getsource(MR.d16_ratio)
    assert "mixed_scale_as_published" in src
    assert '"shot_weighted", "shot_weighted"' in src, "정본은 양팔 다 슈팅 가중이다"


def test_d15_comparison_never_subtracts_across_scales():
    src = inspect.getsource(MR.d15_comparison)
    assert "mixed_scale_as_published" in src
    # 발화 가중 팔의 눈금은 반드시 그 사건 종류로 가중된 눈금줄이라야 한다
    assert '("fire_weighted", f"weighted_by_{kind}")' in src


# --------------------------------------------------------------------------- #
# 4. 규약과 신고
# --------------------------------------------------------------------------- #
def test_empty_window_count_is_always_reported():
    r = MR.quantity_two_scales(np.zeros(2, "int64"),
                               np.array([0.01, np.nan]), 1)
    assert r["n_empty_window"] == 1
    assert r["share_empty_window"] == pytest.approx(0.5)


def test_module_has_no_verdict_constant():
    """판정 금지 — 후보를 살리거나 죽이는 문턱이 이 모듈에 없다."""
    src = inspect.getsource(MR)
    for banned in ("PASS_", "FAIL_", "GO_", "KILL_", "VERDICT"):
        assert banned not in src


def test_d13_tape_density_is_not_silently_bundled_in():
    """1단계는 눈금만 바꾼다 — 밀도 정합을 같이 하면 어느 쪽이 움직였는지 못 가른다."""
    src = inspect.getsource(MR)
    assert "trailing_stats" not in src and "vol_matched" not in src
    assert "D-13" in src


def test_base_rate_per_symbol_matches_the_original_default():
    """`per_symbol` 을 바꾸면 §4-2 의 재현이 아니다."""
    sig = inspect.signature(TS.coverage_base_rate)
    assert MR.BASE_RATE_PER_SYMBOL == sig.parameters["per_symbol"].default


def test_minute_candle_path_is_not_touched():
    """1분봉 경로·정렬 의존 함수를 안 쓴다 (D-10 은 다음 태스크다)."""
    src = inspect.getsource(MR)
    for banned in ("tick_classify", "window_frame", "find_shot_starts", "minute"):
        assert banned not in src

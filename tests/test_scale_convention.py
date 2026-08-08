"""눈금 규약 D-14(docs/44 §12) — **눈금만 바뀌고 사건 집합은 안 바뀌는지.**

## 이 파일이 지키는 것

1. **발화 가중은 이어 붙인 값과 정확히 같다.** `fold(weights=None)` 이
   `probe_summary` 의 비율과 **같은 수**여야 한다. 다르면 사건 집합을 바꾼 것이고
   그러면 §10 과 나란히 놓을 수 없다.
2. **눈금 추첨은 `random_time_baseline` 을 재현한다.** 종목 라벨을 남기려고 다시
   쓴 추첨이 원본과 **같은 값**을 내야 한다 — 비슷한 다른 값이면 재현이 아니다.
3. **두 눈금의 차이는 오직 가중치다.** 종목별 비율 `r_s` 는 한 번만 재고 접기만
   달라진다. 사건 수가 종목마다 같으면 **두 눈금이 같은 값**이어야 한다.
4. **가중치는 실제로 물린다.** 한쪽 종목에 사건을 몰아 주면 발화 가중은 그 종목의
   비율로 끌려가고 종목당 균등은 안 끌려간다.
5. **집중도가 갈라짐의 원인이다.** 사건이 고르면 두 눈금이 붙고, 몰리면 벌어진다.
6. **빈 창 규약이 둘 다 나온다.** `incl_empty` 를 조용히 기본으로 삼지 않는다.
7. **재접기와 실제 추첨이 같은 양을 잰다.** 가중치 재접기 값이 종목마다 사건 수만큼
   실제로 뽑아 이어 붙인 값과 (추첨 잡음 안에서) 같아야 한다.
8. **판정 금지.** 이 모듈에는 후보를 살리거나 죽이는 상수가 없다.
"""
from __future__ import annotations

import inspect

import numpy as np
import pytest

from tossmon.analysis.measure import cross_peak_check as CP
from tossmon.analysis.measure import scale_convention as SC

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
# 1. 발화 가중 = 이어 붙인 값. **같은 수여야 한다**
# --------------------------------------------------------------------------- #
def test_fire_weighted_fold_equals_pooling_the_events():
    # 종목 A 는 사건 4건 중 3건이 참, 종목 B 는 1건 중 0건이 참.
    sym_id = np.array([0, 0, 0, 0, 1], dtype="int64")
    flag = np.array([1, 1, 1, 0, 0], dtype=bool)
    counts, rates = SC.per_symbol_rates(sym_id, flag, 2)
    assert counts.tolist() == [4, 1]
    assert rates[0] == pytest.approx(0.75) and rates[1] == pytest.approx(0.0)
    # 이어 붙이면 3/5 = 0.6 이다. 발화 가중이 그 값이어야 한다.
    assert SC.fold(counts, rates)["value"] == pytest.approx(float(flag.mean()))
    # 종목당 균등은 (0.75 + 0.0)/2 = 0.375 로 **다른 값**이다.
    assert SC.fold(counts, rates, "uniform")["value"] == pytest.approx(0.375)


def test_fire_weighted_matches_probe_summary_on_real_shaped_input():
    """`probe_summary` 와 **같은 규약**(동률·빈 창)을 쓰는지까지 같이 본다."""
    ts, px = bars([100.0, 100.0, 101.0, 100.5, 99.0, 99.0])
    idx = np.array([0, 2, 4], dtype="int64")
    probe = CP.forward_probe(ts, px, idx, 2)
    sym_id = np.zeros(idx.size, dtype="int64")
    mine = SC.fold(*SC.per_symbol_rates(
        sym_id, SC.anchor_is_max_flags(probe)["is_max_incl_empty"], 1))
    theirs = CP.probe_summary(probe)
    assert mine["value"] == pytest.approx(theirs["share_anchor_is_max_incl_empty"])
    assert mine["n_events"] == theirs["n"]


def test_ties_are_not_a_rise_in_the_flags():
    """동률(같은 가격)은 '더 높다' 가 아니다 — 1단계 argmax 규약과 같아야 한다."""
    ts, px = bars([100.0, 100.0, 100.0])
    probe = CP.forward_probe(ts, px, np.array([0]), 2)
    assert bool(SC.anchor_is_max_flags(probe)["is_max_incl_empty"][0]) is True


# --------------------------------------------------------------------------- #
# 2. 눈금 추첨이 원본을 재현하는가
# --------------------------------------------------------------------------- #
def test_ruler_draw_reproduces_random_time_baseline():
    """종목 라벨을 남기려고 다시 쓴 추첨이 **원본과 같은 값**을 내야 한다."""
    b = barset({"AAA": list(np.linspace(100, 130, 400)),
                "BBB": list(np.linspace(50, 40, 350)),
                "CCC": [10.0 + (i % 7) for i in range(300)]})
    ref = CP.random_time_baseline(b, horizons=(60,), draws_per_symbol=50, seed=7)
    mine = SC.ruler_draws_by_symbol(b, horizons=(60,), draws_per_symbol=50, seed=7)
    row = SC.ruler_row(b, mine, {}, horizon_s=60)
    assert row["n_draws"] == ref["n_draws"]
    assert row["pooled_over_equal_draws"] == pytest.approx(
        ref["fixed_60s"]["share_anchor_is_max_incl_empty"])


def test_ruler_draw_keeps_the_symbol_label_it_drew_from():
    b = barset({"AAA": list(np.linspace(100, 130, 400)),
                "BBB": list(np.linspace(50, 40, 350))})
    d = SC.ruler_draws_by_symbol(b, horizons=(60,), draws_per_symbol=25, seed=3)
    assert np.bincount(d["sym_id"], minlength=2).tolist() == [25, 25]


# --------------------------------------------------------------------------- #
# 3. 두 눈금의 차이는 **오직 가중치**다
# --------------------------------------------------------------------------- #
def test_the_two_scales_agree_when_every_symbol_has_the_same_event_count():
    """사건이 고르면 두 눈금이 **같은 값**이다. 갈라지는 것은 집중도 때문이다."""
    sym_id = np.repeat(np.arange(4), 5)
    rng = np.random.default_rng(11)
    flag = rng.integers(0, 2, size=sym_id.size).astype(bool)
    counts, rates = SC.per_symbol_rates(sym_id, flag, 4)
    both = SC.both_scales(counts, rates, min_events=0)
    assert both["fire_weighted"]["value"] == pytest.approx(
        both["symbol_uniform"]["value"])


def test_the_two_scales_split_when_events_pile_into_one_symbol():
    # 종목 0 에 사건 90건(비율 0.1), 종목 1 에 10건(비율 0.9)
    sym_id = np.concatenate([np.zeros(90, "int64"), np.ones(10, "int64")])
    flag = np.concatenate([np.arange(90) < 9, np.arange(10) < 9]).astype(bool)
    counts, rates = SC.per_symbol_rates(sym_id, flag, 2)
    fw = SC.fold(counts, rates)["value"]
    su = SC.fold(counts, rates, "uniform")["value"]
    assert fw == pytest.approx(0.18)      # (9 + 9) / 100
    assert su == pytest.approx(0.50)      # (0.1 + 0.9) / 2
    assert abs(fw - su) > 0.3, "몰리면 두 눈금이 크게 갈라져야 한다"


def test_external_weights_move_the_ruler_toward_the_weighted_symbol():
    """눈금의 **종목 구성**을 사건에 맞추면 그 종목의 비율로 끌려가야 한다."""
    sym_id = np.repeat(np.arange(2), 100)
    flag = np.concatenate([np.ones(100), np.zeros(100)]).astype(bool)
    counts, rates = SC.per_symbol_rates(sym_id, flag, 2)
    assert SC.fold(counts, rates, "uniform")["value"] == pytest.approx(0.5)
    assert SC.fold(counts, rates, np.array([9.0, 1.0]))["value"] == pytest.approx(0.9)
    assert SC.fold(counts, rates, np.array([1.0, 9.0]))["value"] == pytest.approx(0.1)


def test_symbols_with_no_events_get_no_vote_in_either_scale():
    sym_id = np.array([0, 0, 2], dtype="int64")
    flag = np.array([1, 0, 1], dtype=bool)
    counts, rates = SC.per_symbol_rates(sym_id, flag, 4)   # 1, 3 번 종목은 사건 0
    assert SC.fold(counts, rates, "uniform")["n_symbols"] == 2
    # 사건 없는 종목의 rate 는 nan 이고, 그것이 값에 섞이면 안 된다
    assert SC.fold(counts, rates, "uniform")["value"] == pytest.approx(0.75)


def test_min_events_guard_drops_the_thin_symbols_from_the_denominator():
    sym_id = np.concatenate([np.zeros(50, "int64"), np.ones(1, "int64")])
    flag = np.concatenate([np.zeros(50), np.ones(1)]).astype(bool)
    counts, rates = SC.per_symbol_rates(sym_id, flag, 2)
    plain = SC.fold(counts, rates, "uniform")
    guarded = SC.fold(counts, rates, "uniform", min_events=20)
    assert plain["value"] == pytest.approx(0.5) and plain["n_symbols"] == 2
    assert guarded["value"] == pytest.approx(0.0) and guarded["n_symbols"] == 1


# --------------------------------------------------------------------------- #
# 4. 재접기와 실제 추첨이 같은 양을 잰다
# --------------------------------------------------------------------------- #
def test_reweighting_and_actually_drawing_by_counts_estimate_the_same_thing():
    """가중치 재접기 ≈ 종목마다 사건 수만큼 실제로 뽑기. 어긋나면 둘 중 하나가 틀렸다."""
    b = barset({"AAA": [100.0 + (i % 11) for i in range(600)],
                "BBB": list(np.linspace(50, 70, 600)),
                "CCC": [20.0 - (i % 5) for i in range(600)]})
    counts = np.array([200.0, 20.0, 20.0])          # 종목 0 에 몰린 구성
    d = SC.ruler_draws_by_symbol(b, horizons=(60,), draws_per_symbol=300, seed=5)
    reweighted = SC.ruler_row(b, d, {"ev": counts},
                              horizon_s=60)["incl_empty"]["weighted_by_ev"]["value"]
    drawn = SC.ruler_draws_matched_counts(b, counts, list(b), horizon_s=60,
                                          multiplier=10, seed=5)
    pooled = CP.probe_summary(drawn["probe"])["share_anchor_is_max_incl_empty"]
    assert reweighted == pytest.approx(pooled, abs=0.05)


# --------------------------------------------------------------------------- #
# 5. 규약과 신고 — 조용히 기본값을 정하지 않는다
# --------------------------------------------------------------------------- #
def test_both_empty_window_conventions_are_reported():
    ts, px = bars([100.0, 101.0, 99.0])
    probe = CP.forward_probe(ts, px, np.array([0, 2]), 2)   # 두 번째는 빈 창
    row = SC.row_two_scales(np.zeros(2, "int64"), probe, 1)
    assert row["n_empty_window"] == 1
    for conv in ("incl_empty", "excl_empty"):
        for scale in ("fire_weighted", "symbol_uniform"):
            assert row[conv][scale]["value"] is not None
    # 빈 창을 '앵커가 정점' 으로 세는 쪽이 더 크거나 같아야 한다
    assert (row["incl_empty"]["fire_weighted"]["value"]
            >= row["excl_empty"]["fire_weighted"]["value"])


def test_ruler_row_reports_the_fire_weighted_column_separately_from_pooled():
    """눈금줄의 `fire_weighted` 칸은 의미가 없다 — 발화 가중은 `weighted_by_*` 다."""
    b = barset({"AAA": [100.0 + (i % 9) for i in range(400)],
                "BBB": list(np.linspace(10, 12, 400))})
    d = SC.ruler_draws_by_symbol(b, horizons=(60,), draws_per_symbol=100, seed=2)
    row = SC.ruler_row(b, d, {"ev": np.array([10.0, 1.0])}, horizon_s=60)
    assert row["pooled_over_equal_draws"] == pytest.approx(
        row["incl_empty"]["fire_weighted"]["value"])
    assert "weighted_by_ev" in row["incl_empty"]
    assert "의미가 없다" in row["note"]


def test_concentration_is_reported_so_the_split_is_explainable():
    counts = np.array([100, 1, 1, 1], dtype="int64")
    c = SC.concentration(counts)
    assert c["n_symbols_with_events"] == 4
    assert c["top1_share"] == pytest.approx(100 / 103)
    assert c["share_of_symbols_under_20_events"] == pytest.approx(0.75)


def test_gap_is_only_taken_within_one_scale():
    """§10-7 이 뺀 30.7 − 31.1 은 **다른 두 눈금 사이의 뺄셈**이었다 — 그것을 남긴다."""
    src = inspect.getsource(SC.gap_table)
    assert "mixed_scale_as_published" in src
    assert 'fold(counts, rates, "uniform")' not in src, "격차는 미리 낸 값에서만 뺀다"


def test_module_has_no_verdict_constant():
    """판정 금지 — 후보를 살리거나 죽이는 문턱이 이 모듈에 없다."""
    src = inspect.getsource(SC)
    for banned in ("PASS_", "FAIL_", "GO_", "KILL_", "VERDICT"):
        assert banned not in src


def test_d13_tape_density_is_not_silently_bundled_in():
    """눈금 하나만 바꾼다 — 밀도 정합을 같이 하면 어느 쪽이 움직였는지 못 가른다."""
    src = inspect.getsource(SC)
    assert "trailing_stats" not in src and "vol_matched" not in src
    assert "D-13" in src

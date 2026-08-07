"""1~3단계 (docs/44) — **탐지기가 자기 답을 만들어내지 않는지.**

## 이 파일이 지키는 것

1. **탐지는 미래를 쓰지 않는다.** `find_fires` 는 발화 시점 이후의 값을 보면 안 된다 —
   잘라낸 자료로 돌려도 같은 자리에서 발화해야 한다. 이게 2단계 전체의 전제다.
2. **에피소드와 발화는 다른 것이다.** 에피소드 시작은 지나고 보니 저점이라 실시간에
   못 쓴다. 둘이 같아지면 어딘가에서 미래가 새고 있는 것이다.
3. **진입은 지연을 먹는다.** 1초 격자 위에서 2.56초는 +3초로 올림된다 — 발화 막대에
   진입하는 일이 있어선 안 된다.
4. **가격 조회가 미래를 안 쓴다.** `px_at` 은 그 시점 **이하**의 마지막 값만 쓰고,
   너무 묵으면 모른다고 답해야 한다.
5. **tier3 판정이 두 레인을 가른다.** 4초 레인은 소속, 600초 레인은 비소속.
6. **판정 금지.** 이 모듈은 관측만 낸다 — 후보를 살리거나 죽이는 상수가 없어야 한다.
"""
from __future__ import annotations

import numpy as np
import pytest

from tossmon.analysis.measure import tick_stages as TS

U = 1_000_000
BASE = TS.WINDOW_START_MS + 40 * 3600 * 1000


def ramp(prices, start_ms=BASE, step_s=1):
    """1초 격자 위의 초 막대 (ts, vwap) 를 만든다."""
    ts = start_ms + np.arange(len(prices), dtype="int64") * step_s * 1000
    return ts, np.asarray(prices, dtype="float64")


# --------------------------------------------------------------------------- #
# 1. 창 표현 — 초 막대는 1초 격자라 창 안 막대 수가 유계다
# --------------------------------------------------------------------------- #
def test_forward_window_is_bounded_by_the_one_second_grid():
    ts, px = ramp([100.0] * 200)
    end, k = TS.forward_window(ts, 60)
    # 1초 격자에서 60초 창은 자기 자신 + 60개를 넘을 수 없다
    assert k <= 61
    assert (end - np.arange(ts.size)).max() == k


def test_window_matrix_masks_outside_the_window():
    ts, px = ramp([1.0, 2.0, 3.0, 4.0])
    end, k = TS.forward_window(ts, 2)          # 2초 창 = 자기 + 2개
    m = TS.window_matrix(ts, px, end, k)
    assert m[0, 0] == 1.0 and m[1, 0] == 2.0 and m[2, 0] == 3.0
    assert np.isneginf(m[:, 3][1:]).all()      # 마지막 막대 앞에는 아무것도 없다


# --------------------------------------------------------------------------- #
# 2. 에피소드 — docs/29 와 같은 규칙
# --------------------------------------------------------------------------- #
def test_episode_finds_start_cross_and_peak_in_order():
    # 100 에서 출발해 10초에 걸쳐 +2% 까지 오른다
    px = [100.0, 100.2, 100.4, 100.6, 100.8, 101.0, 101.4, 101.8, 102.0, 101.5]
    ts, v = ramp(px)
    ep = TS.find_episodes(ts, v, v, rise=0.01, max_seconds=60)
    assert ep["start"].size == 1
    s, c, p = int(ep["start"][0]), int(ep["cross"][0]), int(ep["peak"][0])
    assert s == 0
    assert v[c] / v[s] - 1.0 >= 0.01           # 교차는 임계를 넘은 첫 막대
    assert v[c - 1] / v[s] - 1.0 < 0.01        # 그 직전은 아직 아니다
    assert s < c <= p
    assert p == 8                              # 정점은 102.0


def test_cross_equals_peak_when_one_bar_clears_the_threshold_and_then_falls():
    """★ docs/44 §2-3 의 57.6% 가 재는 것이 바로 이 모양이다.

    한 막대가 임계를 넘으면서 동시에 꼭대기면, **탐지 시점에 남은 상승은 0** 이다.
    지연을 0 으로 만들어도 그렇다. 이게 시장이 아니라 정의의 성질임을 여기 고정한다.
    """
    px = [100.0, 100.3, 101.6, 100.9, 100.4]      # 2번 막대에서 넘고 그게 곧 정점
    ts, v = ramp(px)
    ep = TS.find_episodes(ts, v, v, rise=0.01, max_seconds=60)
    assert ep["cross"][0] == ep["peak"][0] == 2
    assert v[ep["peak"][0]] / v[ep["cross"][0]] - 1.0 == 0.0


def test_episode_respects_the_max_seconds_ceiling():
    # 60초를 넘겨야만 +1% 가 되는 완만한 상승 -> 잡히면 안 된다
    px = list(np.linspace(100.0, 101.0, 120))
    ts, v = ramp(px)
    ep = TS.find_episodes(ts, v, v, rise=0.01, max_seconds=60)
    rises = [v[p] / v[s] - 1.0 for s, p in zip(ep["start"], ep["peak"])]
    assert all(r >= 0.01 for r in rises)
    assert all((ts[p] - ts[s]) <= 60_000 for s, p in zip(ep["start"], ep["peak"]))


def test_episode_does_not_overlap_itself():
    px = [100.0, 101.5, 100.0, 101.5, 100.0, 101.5]
    ts, v = ramp(px)
    ep = TS.find_episodes(ts, v, v, rise=0.01, max_seconds=60)
    peaks, starts = ep["peak"], ep["start"]
    assert (starts[1:] >= peaks[:-1]).all()    # 다음 시작은 앞 정점 이후


# --------------------------------------------------------------------------- #
# 3. ★ 발화는 미래를 쓰지 않는다 — 2단계 전체가 여기 걸려 있다
# --------------------------------------------------------------------------- #
def test_fires_are_causal_truncating_the_future_does_not_move_them():
    rng = np.random.default_rng(7)
    px = 100.0 * np.cumprod(1.0 + rng.normal(0, 0.002, 400))
    ts, v = ramp(px)
    full = TS.find_fires(ts, v, v, rise=0.01, max_seconds=60, cooldown_s=60)
    assert full.size > 0, "표본이 발화를 하나도 안 내면 이 테스트가 무의미하다"
    for f in full[:5]:
        cut = int(f) + 1                        # 발화 막대까지만 보여준다
        part = TS.find_fires(ts[:cut], v[:cut], v[:cut],
                             rise=0.01, max_seconds=60, cooldown_s=60)
        assert part.size and part[-1] == f, (
            "미래를 잘라냈더니 발화 위치가 달라졌다 = 탐지기가 미래를 보고 있다")


def test_fire_differs_from_episode_start_because_the_low_is_only_known_later():
    px = [100.0, 100.2, 100.5, 101.2, 101.4]
    ts, v = ramp(px)
    ep = TS.find_episodes(ts, v, v, rise=0.01, max_seconds=60)
    fires = TS.find_fires(ts, v, v, rise=0.01, max_seconds=60, cooldown_s=60)
    assert int(ep["start"][0]) == 0             # 저점은 0번
    assert int(fires[0]) == 3                   # 발화는 임계를 넘은 곳에서만
    assert int(fires[0]) == int(ep["cross"][0])


def test_fire_cooldown_suppresses_re_firing_inside_one_move():
    px = [100.0] + list(np.linspace(101.0, 106.0, 30))
    ts, v = ramp(px)
    hot = TS.find_fires(ts, v, v, rise=0.01, max_seconds=60, cooldown_s=60)
    assert hot.size == 1, "한 번의 상승이 여러 발화를 내면 표본이 부풀려진다"


# --------------------------------------------------------------------------- #
# 4. 진입 — 지연을 반드시 먹는다
# --------------------------------------------------------------------------- #
def test_entry_never_lands_on_the_firing_bar():
    ts, v = ramp(list(np.linspace(100.0, 110.0, 60)))
    fires = np.array([10], dtype="int64")
    res = TS._entry_outcomes(ts, v, fires, lag_s=TS.DETECT_LAG_S)
    assert res["n_entered"] == 1
    # 1초 격자에서 2.56초는 +3초로 올림된다
    assert res["entry_delay_actual_s"][0] >= TS.DETECT_LAG_S
    assert res["entry_delay_actual_s"][0] == 3.0


def test_entry_slippage_is_positive_while_price_is_still_rising():
    ts, v = ramp(list(np.linspace(100.0, 110.0, 60)))
    res = TS._entry_outcomes(ts, v, np.array([10], "int64"), lag_s=TS.DETECT_LAG_S)
    assert res["slip_from_fire"][0] > 0, "오르는 중이면 지연 동안 값이 달아나야 한다"


def test_mfe_and_mae_bracket_zero_at_entry():
    rng = np.random.default_rng(3)
    px = 100.0 * np.cumprod(1.0 + rng.normal(0, 0.003, 300))
    ts, v = ramp(px)
    res = TS._entry_outcomes(ts, v, np.arange(5, 100, 10, dtype="int64"),
                             lag_s=TS.DETECT_LAG_S)
    assert (res["mfe_60s"] >= -1e-12).all()
    assert (res["mae_60s"] <= 1e-12).all()


# --------------------------------------------------------------------------- #
# 5. 가격 조회 — 과거만 본다
# --------------------------------------------------------------------------- #
def test_px_at_uses_only_the_past():
    ts, v = ramp([10.0, 20.0, 30.0])
    got = TS.px_at(ts, v, np.array([ts[1] + 500]))
    assert got[0] == 20.0, "다음 막대를 끌어오면 미래를 쓰는 것이다"


def test_px_at_refuses_to_answer_when_too_stale():
    ts, v = ramp([10.0, 20.0])
    fresh = TS.px_at(ts, v, np.array([ts[1] + 5_000]), stale_cap_s=30)
    stale = TS.px_at(ts, v, np.array([ts[1] + 60_000]), stale_cap_s=30)
    assert fresh[0] == 20.0
    assert np.isnan(stale[0]), "모르는 것은 모른다고 해야 한다"


# --------------------------------------------------------------------------- #
# 6. tier3 소속 — 두 레인을 가른다
# --------------------------------------------------------------------------- #
def test_watched_at_separates_the_4s_lane_from_the_600s_lane():
    fast = {"A": BASE + np.arange(0, 120_000, 4_000, dtype="int64")}   # tier3
    slow = {"B": BASE + np.arange(0, 3_600_000, 600_000, dtype="int64")}  # tier2
    t = np.array([BASE + 60_000])
    assert TS.watched_at(fast, "A", t)[0]
    assert not TS.watched_at(slow, "B", t)[0]


def test_watched_at_is_false_for_a_symbol_we_never_polled():
    assert not TS.watched_at({}, "ZZZ", np.array([BASE]))[0]


# --------------------------------------------------------------------------- #
# 7. 가격대 · 세션 — 갈라 내는 축이 실제로 갈린다
# --------------------------------------------------------------------------- #
def test_price_bands_cover_the_line_without_gaps():
    got = TS.price_band_of(np.array([0.5, 1.0, 2.9, 3.0, 9.9, 10.0, 250.0]))
    assert list(got) == ["under_1", "1_to_3", "1_to_3", "3_to_10",
                         "3_to_10", "over_10", "over_10"]


# --------------------------------------------------------------------------- #
# 8. ★ 판정 금지 — 이 모듈에는 후보를 살리거나 죽이는 상수가 없어야 한다
# --------------------------------------------------------------------------- #
def test_module_sweeps_thresholds_rather_than_fixing_one():
    assert len(TS.THRESHOLD_SWEEP) >= 3, (
        "임계를 하나로 박으면 답이 우리 정의를 따라가는지 볼 수 없다")
    assert TS.SHOT_RISE in TS.THRESHOLD_SWEEP


def test_detect_lag_is_the_measured_one_not_an_idealised_zero():
    assert TS.DETECT_LAG_S == pytest.approx(2.56)
    assert TS.DETECT_LAG_WORST_S > TS.DETECT_LAG_S


def test_window_start_is_inherited_not_redefined():
    from tossmon.analysis.measure import tick_resolution as TR
    assert TS.WINDOW_START_MS == TR.WINDOW_START_MS, (
        "08-04 이전이 섞이면 폭이 10배 다른 자료를 한 표에 넣게 된다")

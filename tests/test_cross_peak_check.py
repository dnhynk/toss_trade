"""교차=정점 재검(docs/44 §10) — **자가 답을 만들지 않는지.**

## 이 파일이 지키는 것

1. **앵커 자신은 빼고 잰다.** "그 막대가 정점인가" 는 "그 뒤에 더 높은 막대가 있는가"
   와 같은 물음이다. 자신을 넣으면 답이 자동으로 정해진다.
2. **고정 창은 에피소드 끝을 참조하지 않는다.** 뒤 자료를 얼마나 붙이든 창 안 답이
   안 바뀌어야 한다.
3. **에피소드식 창은 1단계와 정확히 같은 답을 낸다.** `ci == pi` 를 재현하지 못하면
   두 값을 나란히 놓을 수 없다.
4. **경계가 답을 만든다는 것을 실제로 잡아낸다.** 창 밖에서 계속 오르는 계열에서
   에피소드 규칙은 "정점" 이라 하고 고정 창은 "아니다" 라고 해야 한다.
5. **빈 창은 모르는 것이다.** 창 안에 막대가 없으면 두 규약을 다 내놓아야 한다.
6. **동률은 상승이 아니다.** 같은 가격은 "더 높다" 로 세지 않는다.
7. **판정 금지.** 이 모듈에는 후보를 살리거나 죽이는 상수가 없다.
"""
from __future__ import annotations

import numpy as np
import pytest

from tossmon.analysis.measure import cross_peak_check as CP
from tossmon.analysis.measure import tick_stages as TS

BASE = CP.WINDOW_START_MS + 40 * 3600 * 1000


def ramp(prices, start_ms=BASE, step_s=1):
    ts = start_ms + np.arange(len(prices), dtype="int64") * step_s * 1000
    return ts, np.asarray(prices, dtype="float64")


# --------------------------------------------------------------------------- #
# 1. forward_probe — 앵커 자신은 빼고, 창 경계는 포함이다
# --------------------------------------------------------------------------- #
def test_probe_excludes_the_anchor_bar_itself():
    ts, px = ramp([100.0, 99.0, 98.0])
    r = CP.forward_probe(ts, px, np.array([0]), 2)
    # 앵커(100)를 넣었다면 max_ret 이 0 이 됐을 것이다 — 빼야 음수가 나온다
    assert r["max_ret"][0] == pytest.approx(-0.01)
    assert r["n_bars"][0] == 2


def test_probe_window_edge_is_inclusive_and_the_next_bar_is_not():
    ts, px = ramp([100.0, 101.0, 102.0, 103.0])
    r5 = CP.forward_probe(ts, px, np.array([0]), 2)   # 정확히 2초까지
    assert r5["n_bars"][0] == 2 and r5["max_ret"][0] == pytest.approx(0.02)
    r6 = CP.forward_probe(ts, px, np.array([0]), 3)
    assert r6["n_bars"][0] == 3 and r6["max_ret"][0] == pytest.approx(0.03)


def test_probe_end_ret_is_the_last_bar_in_the_window_not_the_max():
    ts, px = ramp([100.0, 110.0, 90.0])
    r = CP.forward_probe(ts, px, np.array([0]), 2)
    assert r["max_ret"][0] == pytest.approx(0.10)
    assert r["end_ret"][0] == pytest.approx(-0.10), "그냥 수익률은 최대치가 아니다"
    assert r["t_max_s"][0] == 1.0


def test_probe_reports_an_empty_window_as_unknown_not_as_peak():
    ts = np.array([BASE, BASE + 300_000], dtype="int64")     # 5분 침묵
    px = np.array([100.0, 200.0])
    r = CP.forward_probe(ts, px, np.array([0]), 30)
    assert r["n_bars"][0] == 0
    assert np.isnan(r["max_ret"][0]), "체결이 없는 것을 '정점' 으로 세면 안 된다"
    s = CP.probe_summary(r)
    assert s["share_empty_window"] == 1.0
    # 두 규약이 서로 다른 답을 낸다 — 하나만 실으면 안 된다
    assert s["share_anchor_is_max_incl_empty"] == 1.0
    assert "share_anchor_is_max_excl_empty" not in s


def test_a_tie_is_not_higher():
    ts, px = ramp([100.0, 100.0, 100.0])
    s = CP.probe_summary(CP.forward_probe(ts, px, np.array([0]), 2))
    assert s["share_higher_later_excl_empty"] == 0.0, (
        "1단계 argmax 는 동률에서 앞을 고른다 — 같은 규약이라야 나란히 놓을 수 있다")


def test_probe_takes_a_per_anchor_horizon_array():
    ts, px = ramp([100.0] * 5 + [200.0])
    r = CP.forward_probe(ts, px, np.array([0, 0]), np.array([2.0, 60.0]))
    assert r["n_bars"][0] == 2 and r["max_ret"][0] == pytest.approx(0.0)
    assert r["n_bars"][1] == 5 and r["max_ret"][1] == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# 2. 고정 창은 **에피소드 끝을 안 쓴다** — 뒤를 잘라 붙여도 창 안 답이 안 변한다
# --------------------------------------------------------------------------- #
def test_fixed_window_answer_does_not_depend_on_what_happens_later():
    head = [100.0, 100.5, 101.0, 100.8, 100.6]
    ts_a, px_a = ramp(head)
    ts_b, px_b = ramp(head + [500.0] * 50)        # 창 밖에서 폭등시켜도
    a = CP.forward_probe(ts_a, px_a, np.array([0]), 4)
    b = CP.forward_probe(ts_b, px_b, np.array([0]), 4)
    assert a["max_ret"][0] == pytest.approx(b["max_ret"][0])
    assert a["end_ret"][0] == pytest.approx(b["end_ret"][0])


# --------------------------------------------------------------------------- #
# 3. ★ 에피소드식 창이 1단계의 `ci == pi` 를 **정확히** 재현한다
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
def test_episode_style_window_reproduces_stage1_cross_is_peak(seed):
    rng = np.random.default_rng(seed)
    px = 100.0 * np.cumprod(1.0 + rng.normal(0, 0.004, 4000))
    ts, v = ramp(px)
    ep = TS.find_episodes(ts, v, v, rise=0.01, max_seconds=60)
    si, ci, pi = ep["start"], ep["cross"], ep["peak"]
    assert si.size > 5, "표본이 없으면 재현을 확인할 수 없다"
    rem = 60.0 - (ts[ci] - ts[si]) / 1000.0
    r = CP.forward_probe(ts, v, ci, rem)
    mine = ~(r["max_ret"] > 0)                     # 빈 창(nan) → 정점으로 센다
    assert np.array_equal(mine, ci == pi), (
        "에피소드식 창이 1단계와 다른 답을 내면 두 값을 나란히 놓을 수 없다")


# --------------------------------------------------------------------------- #
# 4. ★ 순환을 실제로 잡아낸다 — 창 밖에서 계속 오르는 계열
# --------------------------------------------------------------------------- #
def late_cross_then_keeps_rising():
    """**교차가 창 끝에 붙는** 계열. 창 안에서는 교차가 정점이고, 창 밖에서는 더 오른다.

    0~58초: 100.0 → 100.986 (아직 +1% 미만)  /  59초: 101.05 (교차, 창 안 최고가)
    60초: 101.00 (창이 여기서 닫힌다)        /  61초 이후: 계속 오른다
    """
    px = list(100.0 + np.arange(0, 59) * 0.017) + [101.05, 101.00]
    px += list(101.0 + np.arange(1, 61) * 0.05)
    return ramp(px)


def test_the_60s_ceiling_manufactures_cross_is_peak():
    ts, v = late_cross_then_keeps_rising()
    ep = TS.find_episodes(ts, v, v, rise=0.01, max_seconds=60)
    assert ep["start"].size >= 1
    i, c, p = int(ep["start"][0]), int(ep["cross"][0]), int(ep["peak"][0])
    assert (i, c, p) == (0, 59, 59), "교차가 창 끝(59초)에 붙어야 이 시험이 성립한다"
    rem = 60.0 - (ts[c] - ts[i]) / 1000.0
    ep_probe = CP.forward_probe(ts, v, np.array([c]), rem)
    fixed = CP.forward_probe(ts, v, np.array([c]), 60)
    assert c == p, "창이 닫혀서 교차가 곧 정점이 됐다 (에피소드 규칙)"
    assert ep_probe["n_bars"][0] == 1 and not (ep_probe["max_ret"][0] > 0)
    assert fixed["n_bars"][0] == 60 and fixed["max_ret"][0] > 0, (
        "같은 앵커인데 고정 창에서는 더 오른다 — 그 차이가 곧 경계가 만든 몫이다")


def test_boundary_audit_counts_the_flip():
    ts, v = late_cross_then_keeps_rising()
    bars = {"SYN": (ts, np.ones(ts.size, "int64"), v, v, v)}
    anch = CP.collect_anchors(bars, rise=0.01, max_seconds=60, horizons=(30, 60))
    audit = CP.boundary_audit(anch, max_seconds=60, horizons=(30, 60))
    assert audit["flipped_by_fixed_window"]["fixed_60s"]["n_flipped"] >= 1
    assert audit["remaining_window_shares"]["le_2s"] > 0, (
        "교차 뒤 남은 관측 시간이 2초 이하인 몫 — 이 칸이 순환의 자리다")


# --------------------------------------------------------------------------- #
# 5. trailing_min_index — 발화가 기준으로 삼은 그 저점
# --------------------------------------------------------------------------- #
def test_trailing_min_index_finds_the_low_the_detector_used():
    ts, px = ramp([105.0, 100.0, 101.0, 102.0, 106.0])
    got = CP.trailing_min_index(ts, px, np.array([4]), 60)
    assert int(got[0]) == 1


def test_trailing_min_index_breaks_ties_toward_the_earlier_bar():
    ts, px = ramp([100.0, 100.0, 105.0])
    got = CP.trailing_min_index(ts, px, np.array([2]), 60)
    assert int(got[0]) == 0, ("이른 저점을 고르면 창이 더 일찍 닫힌다 — "
                              "'발화도 정점' 쪽에 유리한 보수적 선택이다")


def test_trailing_min_index_respects_the_lookback():
    ts, px = ramp([50.0] + [100.0] * 10 + [102.0])
    got = CP.trailing_min_index(ts, px, np.array([11]), 5)   # 5초만 뒤를 본다
    assert int(got[0]) >= 6, "창 밖의 저점을 끌어오면 안 된다"


# --------------------------------------------------------------------------- #
# 6. collect_anchors — 두 앵커를 **다시 구현하지 않는다**
# --------------------------------------------------------------------------- #
def test_anchors_are_the_same_events_tick_stages_produces():
    rng = np.random.default_rng(7)
    px = 100.0 * np.cumprod(1.0 + rng.normal(0, 0.004, 1500))
    ts, v = ramp(px)
    bars = {"SYN": (ts, np.ones(ts.size, "int64"), v, v, v)}
    anch = CP.collect_anchors(bars, rise=0.01, max_seconds=60, horizons=(30,))
    ep = TS.find_episodes(ts, v, v, rise=0.01, max_seconds=60)
    fires = TS.find_fires(ts, v, v, rise=0.01, max_seconds=60, cooldown_s=60)
    assert anch["stage1_cross"]["n"] == int(ep["cross"].size)
    assert anch["causal_fire"]["n"] == int(fires.size)


def test_causal_fire_anchor_never_looks_into_the_future():
    """발화 앵커는 `find_fires` 그대로다 — 자료를 잘라도 자리가 안 움직여야 한다."""
    rng = np.random.default_rng(11)
    px = 100.0 * np.cumprod(1.0 + rng.normal(0, 0.004, 1200))
    ts, v = ramp(px)
    full = TS.find_fires(ts, v, v, rise=0.01, max_seconds=60, cooldown_s=60)
    cut = 800
    part = TS.find_fires(ts[:cut], v[:cut], v[:cut], rise=0.01, max_seconds=60,
                         cooldown_s=60)
    assert np.array_equal(part, full[full < cut])


# --------------------------------------------------------------------------- #
# 7. 판정 금지 — 상수와 조건이 관측용이라는 것
# --------------------------------------------------------------------------- #
def test_horizons_span_more_than_one_clock():
    assert len(CP.HORIZONS_S) >= 3, "창 하나만 보면 '창 탓' 을 가릴 수 없다"
    assert CP.SHOT_MAX_SECONDS in CP.HORIZONS_S, (
        "에피소드식 창의 최대 길이와 같은 고정 창이 있어야 두 칸이 비교된다")


def test_window_start_is_inherited_not_redefined():
    from tossmon.analysis.measure import tick_resolution as TR
    assert CP.WINDOW_START_MS == TR.WINDOW_START_MS


def test_module_reuses_the_published_detectors():
    assert CP.find_episodes is TS.find_episodes
    assert CP.find_fires is TS.find_fires


# --------------------------------------------------------------------------- #
# 8. 눈금 — 무작위 막대 기준선이 앵커와 **같은 기계**를 통과한다
# --------------------------------------------------------------------------- #
def test_random_baseline_is_seeded_and_uses_the_same_probe():
    rng = np.random.default_rng(5)
    px = 100.0 * np.cumprod(1.0 + rng.normal(0, 0.004, 900))
    ts, v = ramp(px)
    bars = {"SYN": (ts, np.ones(ts.size, "int64"), v, v, v)}
    a = CP.random_time_baseline(bars, horizons=(30,), draws_per_symbol=200)
    b = CP.random_time_baseline(bars, horizons=(30,), draws_per_symbol=200)
    assert a["fixed_30s"] == b["fixed_30s"], "씨앗이 고정이라야 다시 낼 수 있다"
    assert a["fixed_30s"]["n"] == 200
    assert 0.0 < a["fixed_30s"]["share_anchor_is_max_incl_empty"] < 1.0


def test_random_baseline_goes_through_the_same_machine_as_the_anchors():
    """기준선과 앵커가 다른 요약을 내면 나란히 놓을 수 없다 — 같은 칸이 나와야 한다."""
    rng = np.random.default_rng(9)
    px = 100.0 * np.cumprod(1.0 + rng.normal(0, 0.004, 900))
    ts, v = ramp(px)
    bars = {"SYN": (ts, np.ones(ts.size, "int64"), v, v, v)}
    base = CP.random_time_baseline(bars, horizons=(30,), draws_per_symbol=200)
    anch = CP.collect_anchors(bars, rise=0.01, max_seconds=60, horizons=(30,))
    got = CP.probe_summary(anch["causal_fire"]["fixed"][30])
    assert set(base["fixed_30s"]) == set(got), (
        "같은 probe_summary 를 통과해야 눈금으로 쓸 수 있다")

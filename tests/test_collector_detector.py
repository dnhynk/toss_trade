"""검출기 (계약 C-8) — 두 경로 스코어, A2 §3 활동 상태, 티어 히스테리시스, 이벤트 기록.

스코어 임계는 synth 7종을 실제로 통과시켜 잡은 값이다. 이 테스트가 그 스펙이다.
"""
from __future__ import annotations

import json
import math

import pytest

from tests import synth
from tests.test_collector_helpers import MIN_MS
from tossmon.analysis.baselines import compute_daily_baseline, minute_of_session_volume_curve
from tossmon.analysis.features import extract_precursor_features, feature_names
from tossmon.api.models import Price
from tossmon.collector.detector import (DEFAULT_THRESHOLDS, EVENT_CORE_COLUMNS,
                                        PriceActivityTracker, TierStateMachine,
                                        activity_score, confirm_score, event_record,
                                        precursor_score, score_paths)

TIER2_UP = DEFAULT_THRESHOLDS[2][0]
TIER3_UP = DEFAULT_THRESHOLDS[3][0]


# --------------------------------------------------------------------------- #
# 스코어 — synth 시나리오로 캘리브레이션
# --------------------------------------------------------------------------- #
def _scores_at(kind: str, offset_min: int = 0, seed: int = 1):
    df, truth = synth.make_scenario(kind, seed=seed)
    t0 = truth.get("t0_expected_ms") or truth["hod_ms"]
    curve = minute_of_session_volume_curve(
        df, truth["calendar"], exclude_dates=(truth["market_day"].date,))
    feats = extract_precursor_features(
        df, truth["rankings"], t0 + offset_min * MIN_MS, include_t0=True,
        symbol=truth["symbol"], curve=curve, calendar=truth["calendar"],
        baseline=compute_daily_baseline(truth["df_1d"]),
        shares_outstanding_qu=truth["shares_outstanding_qu"])
    return precursor_score(feats), confirm_score(feats), feats


def test_scores_are_bounded_and_key_agnostic():
    """피처가 통째로 비어도 스코어는 [0,1] 이고 예외가 없어야 한다."""
    empty = {k: float("nan") for k in feature_names()}
    assert precursor_score(empty) == 0.0 and confirm_score(empty) == 0.0
    assert 0.0 <= precursor_score({}) <= 1.0                 # 키가 없어도 안전
    p, c, _ = _scores_at("coil_pop")
    assert 0.0 <= p <= 1.0 and 0.0 <= c <= 1.0


def test_noise_scenario_scores_below_promotion_threshold():
    """오탐 방지: 이벤트가 아닌 날은 tier2 승격선(0.35)에도 못 미쳐야 한다."""
    p, c, _ = _scores_at("noise")
    assert max(p, c) < TIER2_UP


def test_coil_pop_is_caught_by_the_precursor_path():
    """전조형: T0 에서 전조 스코어가 확인 스코어보다 크고 tier3 선을 넘는다."""
    p, c, feats = _scores_at("coil_pop")
    assert p >= TIER3_UP
    assert p > c
    assert score_paths(feats)[1] == "precursor"
    assert feats["rvol_at_cutoff"] > 3.0                     # 전조가 실재한다


def test_instant_scenario_is_invisible_before_t0_but_confirmed_at_t0():
    """즉발형은 원리상 전조 탐지 불가 — 확인 경로가 유일한 통로다 (W3 인수인계 §5)."""
    p_before, c_before, _ = _scores_at("instant", offset_min=-5)
    assert max(p_before, c_before) < TIER2_UP                # T0 이전엔 아무 신호도 없다

    p, c, feats = _scores_at("instant")
    assert c >= TIER3_UP
    assert c > p                                             # 확인 경로가 이긴다
    assert score_paths(feats)[1] == "confirm"


def test_coil_score_sign_is_used_in_opposite_directions():
    """`coil_score` 는 양수=수축(전조), 강한 음수=방금 폭발(확인)."""
    base = {k: float("nan") for k in feature_names()}
    coiling = {**base, "coil_score": 1.5}
    popping = {**base, "coil_score": -2.5}
    assert precursor_score(coiling) > precursor_score(popping)
    assert confirm_score(popping) > confirm_score(coiling)


def test_event_scenarios_all_beat_the_noise_scenario():
    noise = max(_scores_at("noise")[:2])
    for kind in ("coil_pop", "instant", "fade", "dump", "daymarket", "halt_gap"):
        best = max(_scores_at(kind)[:2])
        assert best > noise + 0.2, kind


# --------------------------------------------------------------------------- #
# A2 §3 — /prices 파생 상태
# --------------------------------------------------------------------------- #
def price(symbol="SNTI", ts_ms=None, last_u=347_000):
    return Price(symbol=symbol, ts_ms=ts_ms, last_u=last_u)


def test_first_print_needs_a_null_to_value_transition():
    tr = PriceActivityTracker()
    first = tr.update(price(ts_ms=None), now_ms=1_000_000)
    assert first.no_print and not first.first_print          # 첫 관측은 전이가 아니다
    assert math.isnan(first.staleness_s)

    second = tr.update(price(ts_ms=999_000), now_ms=1_000_000)
    assert second.first_print is True                        # null → 값 전이
    assert activity_score(second) == 1.0                     # A2 §3 1순위 트리거


def test_first_observation_with_a_timestamp_is_not_first_print():
    tr = PriceActivityTracker()
    state = tr.update(price(ts_ms=999_000), now_ms=1_000_000)
    assert state.first_print is False
    assert state.staleness_s == pytest.approx(1.0)


def test_staleness_collapse_counts_as_awakening():
    """74분 고정돼 있던 소형주의 staleness 가 급감하면 '깨어남' 이다."""
    tr = PriceActivityTracker()
    now = 10_000_000
    tr.update(price(ts_ms=now - 4_440_000), now_ms=now)      # 74분 정체
    awake = tr.update(price(ts_ms=now + 59_000, last_u=350_000), now_ms=now + 60_000)
    assert awake.awakened is True
    assert activity_score(awake) >= 0.85


def test_last_price_change_alone_is_weaker_than_timestamp_advance():
    """함정2: 체결이 없어도 lastPrice 는 온다 — 가격 변화만으로 확신하지 않는다."""
    tr = PriceActivityTracker()
    now = 5_000_000
    tr.update(price(symbol="A", ts_ms=now - 1_800_000), now_ms=now)
    stale_move = tr.update(price(symbol="A", ts_ms=now - 1_800_000, last_u=360_000),
                           now_ms=now + 1000)
    tr.update(price(symbol="B", ts_ms=now - 30_000), now_ms=now)
    fresh = tr.update(price(symbol="B", ts_ms=now + 30_000), now_ms=now + 60_000)

    assert stale_move.price_changed and not stale_move.ts_advanced
    assert fresh.ts_advanced
    assert activity_score(fresh) > activity_score(stale_move)


def test_tracker_prunes_dropped_symbols():
    tr = PriceActivityTracker()
    for sym in ("A", "B", "C"):
        tr.update(price(symbol=sym, ts_ms=1), now_ms=2)
    assert len(tr) == 3
    assert tr.prune({"A"}) == 2
    assert len(tr) == 1


# --------------------------------------------------------------------------- #
# 티어 상태머신
# --------------------------------------------------------------------------- #
HYST_S = 120
HYST_MS = HYST_S * 1000


def machine(**kw):
    kw.setdefault("tier2_max", 10)
    kw.setdefault("tier3_max", 2)
    return TierStateMachine(HYST_S, **kw)


def test_promotion_climbs_one_tier_at_a_time():
    sm = machine()
    assert sm.on_new_data("AAA", 0.9, 0) == 2                # 1 → 2
    assert sm.on_new_data("AAA", 0.9, HYST_MS // 2) is None  # dwell 중
    assert sm.on_new_data("AAA", 0.9, HYST_MS + 1) == 3      # 2 → 3
    assert sm.tier_of("AAA") == 3
    assert [c.to_tier for c in sm.drain_changes()] == [2, 3]


def test_demotion_requires_sustained_weakness():
    sm = machine()
    sm.on_new_data("AAA", 0.9, 0)
    sm.drain_changes()
    t = HYST_MS + 1
    assert sm.on_new_data("AAA", 0.05, t) is None            # 첫 하회는 기록만
    assert sm.on_new_data("AAA", 0.05, t + HYST_MS + 1) == 1
    assert sm.tier_of("AAA") == 1


def test_recovery_inside_the_band_prevents_flapping():
    """강등선 위로 한 번 돌아오면 카운트가 리셋된다 — 이것이 플래핑 방지의 핵심이다."""
    sm = machine()
    sm.on_new_data("AAA", 0.9, 0)
    t = HYST_MS + 1
    sm.on_new_data("AAA", 0.05, t)                           # 하회 시작
    sm.on_new_data("AAA", 0.30, t + 60_000)                  # 밴드 안으로 복귀
    assert sm.on_new_data("AAA", 0.05, t + 2 * HYST_MS) is None
    assert sm.tier_of("AAA") == 2


def test_scores_inside_the_hysteresis_band_do_nothing():
    sm = machine()
    sm.on_new_data("AAA", 0.9, 0)
    sm.on_new_data("AAA", 0.9, HYST_MS + 1)                  # tier3
    sm.drain_changes()
    # 0.30 은 tier3 강등선(0.42) 아래지만 tier2 승격선(0.35) 근처 — 밴드 안이면 유지
    assert sm.on_new_data("AAA", 0.45, 3 * HYST_MS) is None
    assert sm.tier_of("AAA") == 3


def test_capacity_eviction_needs_a_margin():
    sm = machine(tier3_max=1)
    sm.on_new_data("AAA", 0.9, 0)
    sm.on_new_data("AAA", 0.9, HYST_MS + 1)                  # AAA → tier3 (정원 1)
    sm.drain_changes()

    sm.on_new_data("BBB", 0.9, 0)                            # BBB → tier2
    assert sm.on_new_data("BBB", 0.92, HYST_MS + 1) is None  # 마진 부족 → 자리 못 뺏음
    assert sm.tier_of("AAA") == 3 and sm.tier_of("BBB") == 2

    sm.on_new_data("AAA", 0.60, 2 * HYST_MS)                 # AAA 약해짐
    assert sm.on_new_data("BBB", 0.95, 3 * HYST_MS) == 3
    assert sm.tier_of("AAA") == 2                            # 밀려남
    assert any(c.reason == "evicted" for c in sm.drain_changes())


def test_set_capacity_demotes_the_weakest_first():
    sm = machine(tier3_max=3)
    for i, score in enumerate((0.95, 0.85, 0.75)):
        sym = f"S{i}"
        sm.on_new_data(sym, score, 0)
        sm.on_new_data(sym, score, HYST_MS + 1)
    sm.drain_changes()
    assert len(sm.members(3)) == 3

    changes = sm.set_capacity(tier3_max=1, ts_ms=5 * HYST_MS)
    assert {c.symbol for c in changes} == {"S1", "S2"}        # 약한 둘부터
    assert sm.members(3) == ["S0"]
    assert all(c.reason == "budget_shrink" for c in changes)
    assert len(sm.drain_changes()) == len(changes)            # 기록은 호출측이 한 번만


def test_force_bypasses_score_but_not_reality():
    """first_print 는 스코어와 무관하게 tier2 로 올린다 (A2 §3)."""
    sm = machine()
    assert sm.force("SNTI", 2, "first_print", 1.0, 0) == 2
    assert sm.force("SNTI", 2, "first_print", 1.0, 1000) is None   # 이미 그 티어
    change = sm.drain_changes()[0]
    assert change.reason == "first_print" and change.to_tier == 2


def test_sweep_demotes_symbols_whose_data_dried_up():
    sm = machine(stale_demote_s=300)
    sm.on_new_data("AAA", 0.9, 0)
    sm.drain_changes()
    assert sm.sweep(100_000) == []                            # 아직 신선
    stale = sm.sweep(1_000_000)
    assert [c.symbol for c in stale] == ["AAA"]
    assert stale[0].reason == "stale"


def test_seed_restores_tier_without_recording_a_promotion():
    """재시작 이어받기는 승격 이력이 아니다 — promotions 에 가짜 행을 남기면 안 된다."""
    sm = machine()
    sm.seed("AAA", 3, ts_ms=1234)
    assert sm.tier_of("AAA") == 3
    assert sm.drain_changes() == []
    assert sm.reason_of("AAA") == "resume"


# --------------------------------------------------------------------------- #
# 이벤트 기록 (계약 C-6 + A1 §4)
# --------------------------------------------------------------------------- #
def test_event_record_puts_extra_labels_into_meta_json():
    """C-6 events 는 7컬럼뿐 — A1 §4 추가 라벨 18종은 meta_json 으로 간다 (W3 §6)."""
    row = {"t0_ms": 111, "kind": "win", "peak_ms": 222, "peak_ret": 0.31,
           "ret_30m": 0.12, "ret_close": -0.05, "session": "regular", "symbol": "ABCD",
           "hod_ms": 222, "hod_ret": 0.31, "retrace_30m": -0.2, "retrace_close": -0.4,
           "duration_min": 45, "time_to_peak_min": 12, "vwap_close_rel": -0.08,
           "closed_below_vwap": True, "float_rotation": 1.4,
           "ranking_first_entry_ms": 100, "ranking_lead_lag_min": -9,
           "next_day_gap": float("nan"), "t0_min_from_open": 31, "rvol_at_t0": 6.2,
           "halt_gap_count": 1, "shape": "coil_pop", "outcome": "fade",
           "rvol_gated": True}
    rec = event_record(row, "ABCD", extra_meta={"realtime": True, "score_path": "confirm"})

    assert set(EVENT_CORE_COLUMNS) <= set(rec)
    assert rec["symbol"] == "ABCD" and rec["t0_ms"] == 111
    meta = json.loads(rec["meta_json"])
    for key in ("hod_ms", "retrace_close", "vwap_close_rel", "float_rotation",
                "ranking_lead_lag_min", "t0_min_from_open", "rvol_at_t0", "rvol_gated",
                "halt_gap_count", "shape", "outcome"):
        assert key in meta, key
    assert meta["next_day_gap"] is None                       # NaN → null (표준 JSON)
    assert "NaN" not in rec["meta_json"]
    assert meta["realtime"] is True and meta["score_path"] == "confirm"
    assert "symbol" not in meta                               # 중복 저장 안 함

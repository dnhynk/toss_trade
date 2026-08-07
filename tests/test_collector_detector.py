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
from tossmon.analysis.labeling import EventParams
from tossmon.collector import detector as detector_module
from tossmon.collector.detector import (ACTIVITY_ENTRY_SCORE, DEFAULT_THRESHOLDS,
                                        EVENT_CORE_COLUMNS,
                                        EventDetector, PriceActivityTracker,
                                        TierStateMachine, activity_score, confirm_score,
                                        event_record, label_hash, precursor_score,
                                        score_paths)

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
# 재검출 억제 — 같은 이벤트를 매 사이클 다시 기록하지 않는다 (라이브 실측 40% 중복)
# --------------------------------------------------------------------------- #
def _row(t0=111, **over):
    row = {"t0_ms": t0, "kind": "win", "peak_ms": None, "peak_ret": None,
           "ret_30m": None, "ret_close": None, "session": "regular", "symbol": "ABCD",
           "shape": "coil_pop", "outcome": None, "rvol_gated": True}
    row.update(over)
    return row


def test_label_hash_ignores_our_own_bookkeeping():
    """해시에 detected_ms/score_* 가 섞이면 매 사이클 값이 달라져 억제가 무력화된다."""
    assert label_hash(_row()) == label_hash(_row())
    assert label_hash(_row()) != label_hash(_row(peak_ret=0.31))
    # event_record 의 메타는 해시 입력이 아니다 — 같은 라벨이면 같은 해시여야 한다
    rec_a = event_record(_row(), "ABCD", extra_meta={"detected_ms": 1, "score_path": "a"})
    rec_b = event_record(_row(), "ABCD", extra_meta={"detected_ms": 999, "score_path": "b"})
    assert rec_a["meta_json"] != rec_b["meta_json"]         # 메타는 다르지만
    assert label_hash(_row()) == label_hash(_row())         # 라벨 해시는 같다


class _StubEvents:
    """detect_events 를 대신해 지정한 행을 돌려주는 검출기 (억제 로직만 시험한다)."""

    def __init__(self, detector, rows):
        self.detector = detector
        self.rows = rows

    def emit(self, now_ms=1_000_000):
        import pandas as pd

        found = pd.DataFrame(self.rows)
        original = detector_module.detect_events
        detector_module.detect_events = lambda *a, **k: found
        try:
            return self.detector._new_events(
                "ABCD", pd.DataFrame(), rankings=None, calendar=None, rvol=None,
                prev_close_u=None, shares_outstanding_qu=None,
                scores=(0.5, 0.4, "precursor"), now_ms=now_ms)
        finally:
            detector_module.detect_events = original


def _detector():
    return EventDetector(EventParams(), notifier=None)


def test_identical_relabel_is_suppressed():
    det = _detector()
    stub = _StubEvents(det, [_row()])
    first = stub.emit()
    assert len(first) == 1 and first[0].is_new is True

    for _ in range(5):                                  # 매 사이클 같은 라벨로 재검출
        assert stub.emit() == []
    assert det.counters["suppressed"] == 5
    assert det.counters["events"] == 1                  # 최초 1건뿐
    assert det.counters["updated"] == 0


def test_changed_labels_are_re_emitted_as_updates():
    """T0 시점엔 peak/ret 이 미확정이다 — 장이 진행되며 채워지면 갱신해야 한다."""
    det = _detector()
    stub = _StubEvents(det, [_row()])
    stub.emit()

    stub.rows = [_row(peak_ms=222, peak_ret=0.31)]       # 라벨이 실제로 바뀌었다
    update = stub.emit()
    assert len(update) == 1
    assert update[0].is_new is False                     # 최초 검출이 아니라 갱신
    assert det.counters["updated"] == 1
    assert det.counters["events"] == 1

    assert stub.emit() == []                             # 같은 갱신은 다시 억제


def test_emission_memory_is_bounded():
    det = EventDetector(EventParams(), seen_limit=10)
    stub = _StubEvents(det, [])
    for i in range(25):                                  # 25개 매매일에 하루 1건씩
        stub.rows = [_row(t0=i * detector_module.DAY_MS)]
        stub.emit()
    assert det.emitted_count() == 10                     # 오래된 매매일부터 버린다


# --------------------------------------------------------------------------- #
# 감사 ⑨/I-1 — t0 이동이 중복 방어 2층을 동시에 뚫던 경로
# --------------------------------------------------------------------------- #
def test_t0_shift_within_a_day_does_not_create_a_second_event():
    """RVOL 게이트가 꺼졌다 켜지면 t0 가 다른 봉으로 이동한다 (감사 I-1).

    억제 키가 (symbol, t0_ms) 였을 때는 DB UNIQUE 와 같은 키라 둘 다 뚫려
    같은 급등이 두 행이 됐다. 매매일 키 + max_per_day 상한이면 새 행이 안 생긴다.
    """
    det = _detector()
    t0_a = 100 * MIN_MS
    stub = _StubEvents(det, [_row(t0=t0_a, rvol_gated=False)])
    first = stub.emit()
    assert len(first) == 1 and first[0].is_new is True

    # 1시간 뒤 곡선이 생겨 게이트가 켜졌고, 봉 A 는 RVOL 미달 → t0 가 봉 B 로 이동
    stub.rows = [_row(t0=t0_a + 2 * MIN_MS, rvol_gated=True, rvol_at_t0=5.0)]
    assert stub.emit() == []                             # 두 번째 행을 만들지 않는다
    assert det.counters["t0_shift_suppressed"] == 1
    assert det.counters["events"] == 1

    # 다음 매매일의 이벤트는 새 이벤트다 — 상한은 매매일 단위다
    stub.rows = [_row(t0=t0_a + detector_module.DAY_MS)]
    assert len(stub.emit()) == 1


def test_seeded_suppression_survives_a_restart():
    """DB 에 이미 있는 이벤트는 재기동 후 재검출돼도 '신규' 가 아니다 (감사 F-3).

    억제 집합은 상태파일에 저장되지 않는다 — events 테이블에서 되살리지 않으면
    재기동 직후 버퍼의 모든 이벤트가 신규로 알림·기록된다.
    """
    det = _detector()
    det.seed_suppression("ABCD", 111)
    stub = _StubEvents(det, [_row(t0=111)])
    out = stub.emit()
    assert len(out) == 1
    assert out[0].is_new is False                        # 갱신이지 신규가 아니다
    assert det.counters["events"] == 0 and det.counters["updated"] == 1

    # t0 가 이동한 재검출도 매매일 상한에 걸린다
    stub.rows = [_row(t0=111 + 3 * MIN_MS)]
    assert stub.emit() == []
    assert det.counters["t0_shift_suppressed"] == 1


def test_force_respects_dwell_after_a_recent_demotion():
    """감사 H-7/J-1: 랭킹 스냅샷(12초)이 강등 1ms 뒤 재승격시키는 플래핑 회귀.

    `force()` 가 dwell 을 우회하면 "랭킹은 올려라, 스코어는 내려라" 가 영구 왕복한다.
    """
    sm = machine(stale_demote_s=1)
    sm.on_new_data("AAA", 0.9, 0)                        # → tier2
    demoted = sm.sweep(HYST_MS + 200_000)                # 데이터 끊김 → tier1
    assert [c.to_tier for c in demoted] == [1]
    t_after = HYST_MS + 200_001
    assert sm.force("AAA", 2, "ranking_entry", 0.0, t_after) is None   # dwell 중
    assert sm.tier_of("AAA") == 1

    t_late = HYST_MS + 200_000 + HYST_MS + 1             # dwell 경과
    assert sm.force("AAA", 2, "ranking_entry", 0.0, t_late) == 2


def test_force_with_zero_score_cannot_evict_real_members():
    """감사 H-7: 랭킹 점수는 스코어 채널이 아니다 — 정원이 찼으면 진입하지 못한다."""
    sm = machine(tier2_max=1)
    sm.on_new_data("REAL", 0.40, 0)                      # 실제 스코어로 tier2 점유
    assert sm.tier_of("REAL") == 2
    assert sm.force("RANKED", 2, "ranking_entry", 0.0, 1000) is None
    assert sm.tier_of("RANKED") == 1                     # 축출 없음
    assert sm.score_of("RANKED") == 0.0                  # 스코어 채널 오염 없음


def test_forget_is_explicit_and_not_used_for_demotion():
    """강등에서 이력을 지우면 재승격 직후 전부 재검출된다 — 그게 라이브 버그였다."""
    det = _detector()
    stub = _StubEvents(det, [_row()])
    stub.emit()
    assert stub.emit() == []
    assert det.forget("ABCD") == 1                       # 명시 호출로만 지워진다
    assert len(stub.emit()) == 1                         # 지운 뒤에는 다시 잡힌다


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


# --------------------------------------------------------------------------- #
# 티어 요동 (2026-08-03 정규장 실측: 승격 분당 136회, 1→2/2→1 이 1:1 로 왕복)
# --------------------------------------------------------------------------- #
def test_activity_promotion_never_evicts_an_existing_member():
    """활동 신호(compete=False)는 **빈자리에만** 들어간다 — 최약체를 밀어내지 않는다.

    `activity_score` 는 정상 거래 종목이면 거의 1.000 이라 변별력이 없다. 경쟁시키면
    매 스윕마다 축출이 일어나 1↔2 왕복이 영구히 돈다 (라이브 실측).
    """
    sm = machine(tier2_max=2)
    sm.force("AAA", 2, "score", 0.40, 0)                  # 정원 2 를 채운다
    sm.force("BBB", 2, "score", 0.40, 0)
    sm.drain_changes()
    assert sorted(sm.members(2)) == ["AAA", "BBB"]

    # 활동 신호 1.000 이 들어오려 해도 자리가 없으면 못 들어간다
    assert sm.force("HOT", 2, "price_activity", 1.000, 1000, compete=False) is None
    assert sm.tier_of("HOT") == 1
    assert sorted(sm.members(2)) == ["AAA", "BBB"]         # 축출 0
    assert sm.drain_changes() == []


def test_activity_promotion_does_not_poison_the_score_channel():
    """비경쟁 승격은 st.score 를 올리지 않는다 — 진짜 표적(0.3~0.6)이 최약체가 되면 안 된다."""
    sm = machine(tier2_max=5)
    assert sm.force("HOT", 2, "price_activity", 1.000, 0, compete=False) == 2
    assert sm.score_of("HOT") == 0.0                       # 스코어 채널 오염 없음
    # 기록(promotions 테이블)에는 실제 트리거 강도가 남는다 — 관측은 잃지 않는다
    ch = sm.drain_changes()[0]
    assert ch.reason == "price_activity" and ch.score == pytest.approx(1.0)


def test_activity_promotion_takes_a_free_slot():
    """자리가 있으면 정상적으로 들어간다 (기능을 죽이는 게 아니라 축출만 막는다)."""
    sm = machine(tier2_max=2)
    assert sm.force("AAA", 2, "first_print", 1.0, 0, compete=False) == 2
    assert sm.tier_of("AAA") == 2


def test_eviction_does_not_target_a_symbol_still_in_dwell():
    """방금 티어가 바뀐 심볼은 축출 대상이 아니다 — 축출↔재승격 핑퐁의 씨앗."""
    sm = machine(tier2_max=1)
    sm.on_new_data("WEAK", 0.40, 0)                        # tier2 (방금 변경)
    sm.drain_changes()
    # 훨씬 높은 실스코어라도 dwell 중인 최약체는 밀어내지 못한다
    assert sm.on_new_data("STRONG", 0.95, 1000) is None
    assert sm.tier_of("WEAK") == 2 and sm.tier_of("STRONG") == 1
    # dwell 이 지나면 정상적으로 교체된다
    assert sm.on_new_data("STRONG", 0.95, HYST_MS + 2000) == 2
    assert sm.tier_of("WEAK") == 1


def test_tier3_refills_whenever_a_slot_is_free_and_a_symbol_qualifies():
    """**구멍 메우기 회귀** (2026-08-04): tier3 에 빈자리가 있고 자격(>=0.60) 종목이
    있으면 **반드시** 승격돼야 한다. 이 단언이 없어서 tier3 고사를 테스트가 못 잡았다."""
    sm = machine(tier3_max=5)
    sm.on_new_data("HOT", 0.90, 0)                         # 1 -> 2
    sm.drain_changes()
    assert sm.tier_of("HOT") == 2
    assert sm.on_new_data("HOT", 0.90, HYST_MS + 1) == 3    # 2 -> 3 (빈자리 5)
    assert sm.tier_of("HOT") == 3
    assert len(sm.members(3)) == 1


def test_full_tier2_of_failing_occupants_still_admits_a_new_candidate():
    """tier2 가 닫힌 집합이 되면 안 된다 — tier2 는 tier3 의 **유일한 진입로**다.

    2026-08-04 라이브: 활동 승격을 빈자리 전용으로 바꾸자 evicted=0 이 되고 tier2 가
    고정 집합이 되면서 tier3 이 10 -> 3 으로 말라죽었다.
    """
    sm = machine(tier2_max=3)
    for i in range(3):                                     # 정원을 채운다
        sm.force(f"DUD{i}", 2, "price_activity", ACTIVITY_ENTRY_SCORE, 0)
    sm.drain_changes()
    for i in range(3):                                     # 봉 데이터로 약함이 드러난다
        sm.on_new_data(f"DUD{i}", 0.05, HYST_MS + 1)
    sm.drain_changes()

    ts = 3 * HYST_MS
    assert sm.force("NEW", 2, "price_activity", ACTIVITY_ENTRY_SCORE, ts) == 2
    assert sm.tier_of("NEW") == 2                          # 회전이 살아 있다
    assert len(sm.members(2)) == 3                         # 정원은 지킨다


def test_activity_entry_cannot_evict_a_promising_member():
    """유지선(0.22) 이상 실스코어를 가진 표적은 활동 신호가 밀어내지 못한다 (8/03 결함)."""
    sm = machine(tier2_max=1)
    sm.on_new_data("REAL", 0.50, 0)                        # 진짜 표적이 자리를 잡는다
    sm.drain_changes()
    ts = 2 * HYST_MS
    assert sm.force("ACT", 2, "price_activity", ACTIVITY_ENTRY_SCORE, ts) is None
    assert sm.tier_of("REAL") == 2 and sm.tier_of("ACT") == 1


def test_activity_entries_do_not_evict_each_other():
    """활동 진입끼리는 동점 + 마진이라 서로 못 밀어낸다 — 진동의 씨앗을 없앤다."""
    sm = machine(tier2_max=1)
    sm.force("A", 2, "price_activity", ACTIVITY_ENTRY_SCORE, 0)
    sm.drain_changes()
    ts = 2 * HYST_MS
    assert sm.force("B", 2, "price_activity", ACTIVITY_ENTRY_SCORE, ts) is None
    assert sm.tier_of("A") == 2 and sm.tier_of("B") == 1


def test_healthy_turnover_no_oscillation_and_real_promotions_happen():
    """시뮬 성공 기준 재정의 (코디네이터 지시): **전이 0 은 성공이 아니다.**

    건강한 시스템은 (1) 같은 종목이 왕복하지 않으면서 (2) 진짜 뜨거운 종목을 **여전히
    승격시킨다**. 둘 다 확인한다 — 8/03 수정은 (1)만 보고 (2)를 잃어 tier3 를 죽였다.
    """
    from collections import Counter

    sm = TierStateMachine(HYST_S, tier2_max=10, tier3_max=3)
    ups: Counter = Counter()
    downs: Counter = Counter()
    for sweep in range(1, 21):                             # 45초 스윕 20회
        ts = sweep * 45_000
        # tier2 멤버는 봉 데이터로 실스코어를 받는다 — HOT 만 강하고 나머지는 약하다
        for sym in list(sm.members(2)):
            sm.on_new_data(sym, 0.90 if sym == "ACT0" else 0.05, ts)
        # 정상 거래 중인 40 종목이 매 스윕 진입을 시도한다
        for i in range(40):
            sm.force(f"ACT{i}", 2, "price_activity", ACTIVITY_ENTRY_SCORE, ts)
        for ch in sm.drain_changes():
            (ups if ch.to_tier > ch.from_tier else downs)[ch.symbol] += 1

    # (1) 진동 없음 — 어떤 종목도 왕복(승격+강등 반복)하지 않는다
    round_trips = {s: min(ups[s], downs[s]) for s in set(ups) | set(downs)}
    assert max(round_trips.values(), default=0) <= 1, f"진동 재발: {round_trips}"
    # (2) 정당한 신규 승격이 실제로 일어난다 — 동결이 아니다
    assert len(ups) >= 5, f"신규 승격이 사실상 없다 (동결 신호): {len(ups)}"
    # (3) 진짜 뜨거운 종목은 tier3 까지 간다 — 파이프라인 전체가 살아 있다
    assert sm.tier_of("ACT0") == 3, "뜨거운 종목이 tier3 에 도달하지 못했다"


def test_proven_weak_symbol_is_not_immediately_resampled_by_activity():
    """약함이 입증돼 내려온 종목은 쿨다운 동안 활동 신호로 다시 올라오지 않는다."""
    sm = machine(tier2_max=1)
    sm.force("W", 2, "price_activity", ACTIVITY_ENTRY_SCORE, 0)
    sm.drain_changes()
    t = HYST_MS + 1
    sm.on_new_data("W", 0.05, t)                            # 하회 시작
    assert sm.on_new_data("W", 0.05, t + HYST_MS + 1) == 1  # score_decay 로 강등
    sm.drain_changes()
    down_ms = t + HYST_MS + 1

    # 쿨다운 중에는 활동 신호가 다시 못 올린다 (재표집 = 예산 낭비이자 진동)
    assert sm.force("W", 2, "price_activity", ACTIVITY_ENTRY_SCORE,
                    down_ms + 60_000) is None
    assert sm.tier_of("W") == 1
    # 쿨다운이 지나면 다시 표집 대상이다
    later = down_ms + detector_module.ACTIVITY_REENTRY_COOLDOWN_S * 1000 + 1
    assert sm.force("W", 2, "price_activity", ACTIVITY_ENTRY_SCORE, later) == 2


def test_real_score_beats_the_reentry_cooldown():
    """증거는 쿨다운을 이긴다 — 실제 검출 스코어 경로는 막히지 않는다."""
    sm = machine(tier2_max=2)
    sm.force("W", 2, "price_activity", ACTIVITY_ENTRY_SCORE, 0)
    sm.drain_changes()
    t = HYST_MS + 1
    sm.on_new_data("W", 0.05, t)
    sm.on_new_data("W", 0.05, t + HYST_MS + 1)              # 강등 (쿨다운 무장)
    sm.drain_changes()
    # 같은 종목이 진짜로 터지면 실스코어 경로로 즉시 다시 올라온다
    assert sm.on_new_data("W", 0.90, t + 2 * HYST_MS + 2) == 2


def test_stale_demotion_does_not_arm_the_reentry_cooldown():
    """데이터가 없어서 내려온 것은 약함의 증거가 아니다 — 즉시 재표집 가능해야 한다."""
    sm = machine(tier2_max=2, stale_demote_s=300)
    sm.force("S", 2, "price_activity", ACTIVITY_ENTRY_SCORE, 0)
    sm.drain_changes()
    stale = sm.sweep(HYST_MS + 400_000)                     # 데이터 끊김 -> stale 강등
    assert [c.reason for c in stale] == ["stale"]
    sm.drain_changes()
    ts = HYST_MS + 400_000 + HYST_MS + 1
    assert sm.force("S", 2, "price_activity", ACTIVITY_ENTRY_SCORE, ts) == 2


# --------------------------------------------------------------------------- #
# tier3 정원 채우기 (2026-08-04: 절대 임계 0.60 은 개장 직후에만 넘어 정원이 장 내내 빔)
# --------------------------------------------------------------------------- #
def test_fill_to_capacity_promotes_best_measured_candidates():
    """빈 tier3 정원을 **측정된** 상위 점수 후보로 채운다 — 빈 슬롯은 순손실이다."""
    sm = machine(tier2_max=10, tier3_max=3)
    for sym, sc in (("A", 0.55), ("B", 0.50), ("C", 0.45), ("D", 0.30)):
        sm.force(sym, 2, "price_activity", ACTIVITY_ENTRY_SCORE, 0)
        sm.on_new_data(sym, sc, 0)                          # 봉 데이터로 측정됐다
    sm.drain_changes()
    assert sm.members(3) == []

    filled = sm.fill_to_capacity(3, HYST_MS + 1)
    assert sorted(c.symbol for c in filled) == ["A", "B", "C"]   # 상위 3개, 점수순
    assert "D" not in sm.members(3)                         # 0.30 은 유지선(0.42) 미달
    assert len(sm.members(3)) == 3
    assert all(c.reason == "capacity_fill" for c in filled)


def test_fill_to_capacity_ignores_unmeasured_symbols():
    """활동 신호로만 들어온 미측정 종목은 올리지 않는다 — 근거 없는 승격 금지."""
    sm = machine(tier2_max=10, tier3_max=3)
    sm.force("UNMEASURED", 2, "price_activity", ACTIVITY_ENTRY_SCORE, 0)
    sm.drain_changes()
    assert sm.fill_to_capacity(3, HYST_MS + 1) == []
    assert sm.members(3) == []


def test_fill_to_capacity_respects_capacity_and_dwell():
    sm = machine(tier2_max=10, tier3_max=2)
    for sym in ("A", "B", "C"):
        sm.force(sym, 2, "price_activity", ACTIVITY_ENTRY_SCORE, 0)
        sm.on_new_data(sym, 0.50, 0)
    sm.drain_changes()
    assert sm.fill_to_capacity(3, 1000) == []               # dwell 중이면 안 올린다
    filled = sm.fill_to_capacity(3, HYST_MS + 1)
    assert len(filled) == 2                                 # 정원 2 를 넘지 않는다
    assert sm.fill_to_capacity(3, 2 * HYST_MS) == []        # 이미 가득 차면 무동작


def test_fill_to_capacity_leaves_slots_empty_when_only_noise_qualifies():
    """노이즈만 있으면 빈자리로 둔다 — 아무거나 올리지는 않는다."""
    sm = machine(tier2_max=10, tier3_max=3)
    sm.force("N", 2, "price_activity", ACTIVITY_ENTRY_SCORE, 0)
    sm.on_new_data("N", 0.30, 0)             # tier3 유지선(0.42) 미만 = 노이즈
    sm.drain_changes()
    assert sm.fill_to_capacity(3, HYST_MS + 1) == []


# --------------------------------------------------------------------------- #
# 랭킹 타입 — 수집 목록이 바뀌면 쏠림도 피처가 조용히 0 이 된다 (2026-08-04)
# --------------------------------------------------------------------------- #
def test_detector_passes_the_volume_ranking_types_it_actually_collects():
    """★ 호출부가 랭킹 타입을 명시해 넘기는지 고정한다.

    안 넘기면 features.py 기본값(금액 2종)이 쓰이고, 수집은 거래량 2종만 하므로
    두 프레임이 비어 **예외 없이** 0 이 된다. 에러가 없어서 아무도 모르는 종류의 사고다.
    """
    assert detector_module.RANKING_TOSS_TYPE == "TOSS_SECURITIES_TRADING_VOLUME"
    assert detector_module.RANKING_MARKET_TYPE == "MARKET_TRADING_VOLUME"

    seen: dict = {}

    def spy(*a, **kw):
        seen.update(kw)
        return {name: 0.0 for name in feature_names()}

    det = EventDetector(EventParams(), notifier=None)
    df, truth = synth.make_scenario("coil_pop", seed=1)
    original = detector_module.extract_precursor_features
    detector_module.extract_precursor_features = spy
    try:
        det.evaluate("AAA", df, rankings=truth["rankings"])
    finally:
        detector_module.extract_precursor_features = original

    assert seen.get("toss_type") == "TOSS_SECURITIES_TRADING_VOLUME"
    assert seen.get("market_type") == "MARKET_TRADING_VOLUME"


def test_detector_ranking_types_match_what_the_collector_polls():
    """detector 는 순환 참조 때문에 loops 를 import 하지 않는다 — 어긋남은 여기서 잡는다.

    맞춰야 할 상대는 수집 목록(`RANKING_TYPES`) 이 아니라 **피처 목록**
    (`FEATURE_RANKING_TYPES`) 이다. 2026-08-07 부터 수집은 `TOP_GAINERS`(1d)까지 3종인데,
    토스 쏠림도는 두 목록의 **순위 대비**라 같은 집계창(둘 다 realtime)이라야 뜻이 있다.
    수집 목록으로 맞추면 1d 를 쏠림도 분모에 넣으라는 말이 된다.
    """
    from tossmon.collector.loops import FEATURE_RANKING_TYPES, RANKING_TYPES

    assert set(FEATURE_RANKING_TYPES) == {detector_module.RANKING_TOSS_TYPE,
                                          detector_module.RANKING_MARKET_TYPE}
    # 피처 목록은 수집 목록의 부분집합이어야 한다 — 안 받는 목록으로 피처를 만들 수는 없다.
    assert set(FEATURE_RANKING_TYPES) <= set(RANKING_TYPES)


def test_toss_concentration_survives_on_volume_rankings():
    """★ kwarg 전달이 아니라 **피처가 실제로 살아있는지**를 본다.

    가중치 0.18(toss_share 0.10 + toss_share_slope_30 0.04 + toss_in_ranking 0.04)이
    걸려 있다. tier3 임계 0.60 은 이 항이 살아 있을 때 잡은 값이다.
    """
    df, truth = synth.make_scenario("coil_pop", seed=1, symbol="AAA")
    rk = truth["rankings"].copy()
    # synth 는 아직 금액 2종만 만든다(W3 소유, 미수정) — 수집 목록 이름으로 맞춘다.
    rk["ranking_type"] = rk["ranking_type"].replace({
        "MARKET_TRADING_AMOUNT": "MARKET_TRADING_VOLUME",
        "TOSS_SECURITIES_TRADING_AMOUNT": "TOSS_SECURITIES_TRADING_VOLUME"})

    det = EventDetector(EventParams(), notifier=None)
    got = det.evaluate("AAA", df, rankings=rk)
    assert got is not None
    feats = got.feats

    assert feats["toss_in_ranking"] == 1.0                  # 목록에서 심볼을 찾았다
    assert feats["toss_share"] > 0.0                        # 비율이 계산됐다
    assert not math.isnan(feats["toss_rank_best"])

    # 옛 기본값(금액 2종)으로 부르면 같은 데이터에서 전부 0 이 된다 — 대조군.
    dead = extract_precursor_features(
        df, rk, int(df["ts_ms"].to_numpy()[-1]), include_t0=True, symbol="AAA")
    assert dead["toss_in_ranking"] == 0.0
    assert dead["toss_share"] == 0.0 or math.isnan(dead["toss_share"])

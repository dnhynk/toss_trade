"""예산 가드 (계약 C-8) — 계획 산식 재검증, 실사용 관측, 초과 예측 시 안전 강등.

`config/config.example.yaml` 하단 산식과 같은 답이 나오는지 코드로 못박는다.
설정이 예산을 넘긴 채 기동되는 것이 이 프로젝트에서 가장 비싼 사고이기 때문이다.
"""
from __future__ import annotations

import pytest

from tests.test_collector_helpers import FrozenClock, make_config
from tossmon.collector.budget import (GROUP_CHART, GROUP_MARKET_DATA, GROUP_RANKING,
                                      SHRINK_TIER, BudgetGuard, TierPlan)

LIMITS = {"MARKET_DATA": 10, "MARKET_DATA_CHART": 5, "RANKING": 5, "MARKET_INFO": 3}


def plan(tier3=20, tier2=300, tier1=1500, trades_s=4, book_s=16):
    return TierPlan(tier1_symbols=tier1, tier2_symbols=tier2, tier3_symbols=tier3,
                    tier1_sweep_s=45, tier2_candle_s=90, tier3_trades_s=trades_s,
                    tier3_orderbook_s=book_s, ranking_snap_s=12)


def guard(p=None, **kw):
    g = BudgetGuard(LIMITS, 0.7, clock=kw.pop("clock", None), **kw)
    g.set_plan(p if p is not None else plan())
    return g


def test_plan_rates_reproduce_the_config_arithmetic():
    """config 주석의 6.42 req/s 가 코드에서도 같은 값으로 나와야 한다."""
    rates = plan().rates()
    assert rates[GROUP_MARKET_DATA] == pytest.approx(8 / 45 + 20 / 4 + 20 / 16, rel=1e-9)
    assert rates[GROUP_MARKET_DATA] == pytest.approx(6.4278, abs=1e-3)
    assert rates[GROUP_CHART] == pytest.approx(300 / 90, abs=1e-6)
    assert rates[GROUP_RANKING] == pytest.approx(4 / 12, abs=1e-6)
    assert guard().validate_plan() == {}                     # 예산 안


def test_from_config_matches_shipped_defaults():
    cfg = make_config()
    p = TierPlan.from_config(cfg, tier1_symbols=1500, tier2_symbols=300, tier3_symbols=20)
    assert p.rates()[GROUP_MARKET_DATA] == pytest.approx(6.4278, abs=1e-3)


def test_tier3_30_would_overrun_market_data():
    """코디네이터가 잡아낸 초과 — tier3_max=30 이면 9.55 req/s 로 7.0 을 넘는다."""
    over = guard(plan(tier3=30)).validate_plan()
    assert set(over) == {GROUP_MARKET_DATA}
    assert over[GROUP_MARKET_DATA] == pytest.approx(9.5528 - 7.0, abs=1e-3)


def test_alternative_combo_25_symbols_5s_20s_fits():
    assert guard(plan(tier3=25, trades_s=5, book_s=20)).validate_plan() == {}


def test_should_shrink_brings_the_plan_back_under_budget():
    g = guard(plan(tier3=30), clock=FrozenClock(0))
    orders = g.should_shrink()
    assert orders and GROUP_MARKET_DATA in orders
    assert SHRINK_TIER[GROUP_MARKET_DATA] == 3               # tier3 를 줄이라는 지시
    shrunk = plan(tier3=30 - orders[GROUP_MARKET_DATA])
    assert shrunk.rates()[GROUP_MARKET_DATA] <= 7.0
    assert BudgetGuard(LIMITS, 0.7).limit_of(GROUP_MARKET_DATA) == 10


def test_a_short_burst_does_not_shrink_anything():
    """백필 4연발처럼 수 ms 안에 몰린 버스트로 티어를 줄이면 안 된다.

    순간 버스트를 흡수하는 것은 limiter(대기)의 일이다. 가드는 지속 사용률만 본다.
    """
    clock = FrozenClock(0)
    g = guard(clock=clock)
    for _ in range(8):
        g.on_request(GROUP_CHART)
        clock.advance(0.002)
    assert g.measured_rate(GROUP_CHART) < 1.0
    assert g.should_shrink() is None


def test_measured_usage_triggers_shrink_even_when_the_plan_looks_fine():
    """재시도·백필처럼 계획에 없는 호출은 실측에만 나타난다."""
    clock = FrozenClock(0)
    g = guard(plan(), clock=clock)                            # 계획은 6.43 (예산 안)
    for _ in range(600):                                      # 창(60s)을 가득 채운다
        g.on_request(GROUP_MARKET_DATA)
        clock.advance(0.1)                                    # 10 req/s 를 60초간
    assert g.measured_rate(GROUP_MARKET_DATA) == pytest.approx(10.0, rel=0.05)
    assert g.predicted_rate(GROUP_MARKET_DATA) > g.target(GROUP_MARKET_DATA)
    orders = g.should_shrink()
    assert orders and orders[GROUP_MARKET_DATA] >= 1


def test_shipped_defaults_do_not_shrink_at_startup():
    """계획이 예산 안이면(CHART 3.33 ≤ 3.5) 기동만으로 축소되지 않는다.

    여유분(headroom)은 계획이 아니라 **실측**에 적용한다 — 승격 직후 백필·재시도처럼
    계획에 없는 CHART 호출이 그 여유를 쓰기 때문이다.
    """
    g = guard(clock=FrozenClock(0))
    assert g.validate_plan() == {}
    assert g.should_shrink() is None


def test_measured_chart_usage_still_triggers_shrink():
    """CHART 여유가 4.8% 밖에 없으므로 백필이 조금만 겹쳐도 가드가 움직여야 한다."""
    clock = FrozenClock(0)
    g = guard(clock=clock)
    for _ in range(240):
        g.on_request(GROUP_CHART)
        clock.advance(0.25)                                   # 4 req/s 를 60초간
    assert g.measured_rate(GROUP_CHART) == pytest.approx(4.0, rel=0.05)
    orders = g.should_shrink()
    assert orders and orders[GROUP_CHART] >= 1


def test_measured_rate_forgets_outside_the_window():
    clock = FrozenClock(0)
    g = guard(clock=clock)
    for _ in range(120):
        g.on_request(GROUP_CHART)
        clock.advance(0.5)
    assert g.measured_rate(GROUP_CHART) == pytest.approx(2.0, rel=0.05)
    clock.advance(120)                                        # 윈도우(60s) 밖으로
    assert g.measured_rate(GROUP_CHART) == 0.0


def test_429_forces_shrink_and_is_counted_as_an_incident():
    clock = FrozenClock(0)
    g = guard(clock=clock)
    assert g.should_shrink() is None                          # 평시엔 지시 없음
    g.on_429(GROUP_MARKET_DATA)
    orders = g.should_shrink()
    assert orders and orders[GROUP_MARKET_DATA] >= 1          # 사고 → 즉시 축소
    assert g.rate_limited[GROUP_MARKET_DATA] == 1


def test_shrink_has_a_cooldown_to_avoid_flapping():
    clock = FrozenClock(0)
    g = guard(plan(tier3=30), clock=clock)
    assert g.should_shrink()
    assert g.should_shrink() is None                           # 쿨다운 중
    clock.advance(31)
    assert g.should_shrink()


def test_ranking_is_never_shrunk_only_alerted():
    """랭킹은 과거 조회가 불가능한 유일한 데이터 — 축소 대상이 아니다."""
    class Rec:
        def __init__(self):
            self.alerts = []

        def alert(self, msg):
            self.alerts.append(msg)

        def warn(self, msg):
            pass

    rec = Rec()
    g = BudgetGuard(LIMITS, 0.7, clock=FrozenClock(0), notifier=rec)
    g.set_plan(plan().__class__(**{**plan().__dict__, "ranking_snap_s": 0.5}))
    orders = g.should_shrink()
    assert orders is None or GROUP_RANKING not in orders
    assert rec.alerts and "RANKING" in rec.alerts[0]


def test_snapshot_reports_target_and_usage():
    g = guard(clock=FrozenClock(0))
    g.on_request(GROUP_RANKING)
    snap = g.snapshot()
    assert snap[GROUP_MARKET_DATA]["target"] == pytest.approx(7.0)
    assert snap[GROUP_RANKING]["requests"] == 1.0
    assert "MARKET_DATA=" in g.describe()


def test_unknown_group_falls_back_to_spec_limits():
    g = BudgetGuard({}, 0.7)
    assert g.limit_of("MARKET_INFO") == 3.0                   # SPEC_LIMITS 보강
    assert g.limit_of("NOPE") == 1.0                          # 보수적 기본값

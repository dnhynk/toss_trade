"""예산 가드 (계약 C-8) — 계획 산식 재검증, 실사용 관측, 초과 예측 시 안전 강등.

`config/config.example.yaml` 하단 산식과 같은 답이 나오는지 코드로 못박는다.
설정이 예산을 넘긴 채 기동되는 것이 이 프로젝트에서 가장 비싼 사고이기 때문이다.
"""
from __future__ import annotations

import pytest

from tests.test_collector_helpers import FrozenClock, make_config
from tossmon.collector.budget import (GROUP_CHART, GROUP_MARKET_DATA, GROUP_RANKING,
                                      MEASURED_SUSTAIN_S, MEASURED_WARMUP_S,
                                      RECOVER_AFTER_S, SHRINK_TIER, BudgetGuard,
                                      TierPlan)

LIMITS = {"MARKET_DATA": 10, "MARKET_DATA_CHART": 5, "RANKING": 5, "MARKET_INFO": 3}


#: 출하 설정에서 직접 읽는다 (값을 복제하면 config 변경이 테스트에 안 보인다).
SHIPPED = make_config()


def plan(tier3=None, tier2=None, tier1=None, trades_s=None, book_s=None,
         candle_s=None, ranking_types=None):
    uni, poll = SHIPPED.universe, SHIPPED.polling
    from tossmon.collector.loops import RANKING_TYPES as POLLED
    return TierPlan(
        ranking_types=len(POLLED) if ranking_types is None else ranking_types,
        tier1_symbols=uni.tier1_max if tier1 is None else tier1,
        tier2_symbols=uni.tier2_max if tier2 is None else tier2,
        tier3_symbols=uni.tier3_max if tier3 is None else tier3,
        tier1_sweep_s=poll.tier1_sweep_s,
        tier2_candle_s=poll.tier2_candle_s if candle_s is None else candle_s,
        tier3_trades_s=poll.tier3_trades_s if trades_s is None else trades_s,
        tier3_orderbook_s=poll.tier3_orderbook_s if book_s is None else book_s,
        ranking_snap_s=poll.ranking_snap_s)


def guard(p=None, **kw):
    g = BudgetGuard(LIMITS, 0.7, clock=kw.pop("clock", None), **kw)
    g.set_plan(p if p is not None else plan())
    return g


def test_plan_rates_reproduce_the_config_arithmetic():
    """config 주석의 5.18 req/s 가 코드에서도 같은 값으로 나와야 한다."""
    rates = plan().rates()
    assert rates[GROUP_MARKET_DATA] == pytest.approx(8 / 45 + 10 / 4 + 10 / 4, rel=1e-9)
    assert rates[GROUP_MARKET_DATA] == pytest.approx(5.1778, abs=1e-3)
    assert rates[GROUP_CHART] == pytest.approx(300 / 110, abs=1e-6)
    assert rates[GROUP_RANKING] == pytest.approx(2 / 12, abs=1e-6)
    assert guard().validate_plan() == {}                     # 예산 안


def test_chart_has_real_headroom_for_promotion_backfill():
    """CHART 여유는 **승격 직후 백필**을 위한 것이다 (main 8056da9).

    여유가 5% 수준이면 백필이 겹칠 때마다 가드가 tier2 를 줄여 커버리지가 조용히 무너진다.
    승격 1건당 백필은 1분봉 3페이지 + 일봉 1 = 4콜이다.
    """
    g = guard()
    target, planned = g.target(GROUP_CHART), g.planned_rate(GROUP_CHART)
    headroom = (target - planned) / target
    assert headroom >= 0.15, f"CHART 여유 {headroom:.1%} — 백필이 겹치면 tier2 가 줄어든다"
    # 여유분으로 흡수할 수 있는 동시 승격 건수 (윈도우 60s 기준)
    assert (target - planned) * g.window_s / 4 >= 10


def test_from_config_matches_shipped_defaults():
    uni = SHIPPED.universe
    p = TierPlan.from_config(SHIPPED, tier1_symbols=uni.tier1_max,
                             tier2_symbols=uni.tier2_max, tier3_symbols=uni.tier3_max)
    assert p.rates()[GROUP_MARKET_DATA] == pytest.approx(5.1778, abs=1e-3)
    # from_config 의 기본 ranking_types 는 budget 상수, plan() 은 실제 목록을 센다.
    assert p.rates() == plan().rates()


def test_tier3_30_would_overrun_market_data():
    """코디네이터가 잡아낸 초과 — tier3_max=30 이면 9.55 req/s 로 7.0 을 넘는다."""
    g = guard(plan(tier3=30, book_s=16))
    over = g.validate_plan()
    assert set(over) == {GROUP_MARKET_DATA}
    # 검증 천장은 이제 축소 판정과 **같은** target x HEADROOM (7.0 x 0.95 = 6.65) 이다.
    assert over[GROUP_MARKET_DATA] == pytest.approx(9.5528 - 6.65, abs=1e-3)


def test_alternative_combo_25_symbols_5s_20s_fits():
    assert guard(plan(tier3=25, trades_s=5, book_s=20)).validate_plan() == {}


def test_should_shrink_brings_the_plan_back_under_budget():
    g = guard(plan(tier3=30, book_s=16), clock=FrozenClock(0))
    orders = g.should_shrink()
    assert orders and GROUP_MARKET_DATA in orders
    assert SHRINK_TIER[GROUP_MARKET_DATA] == 3               # tier3 를 줄이라는 지시
    shrunk = plan(tier3=30 - orders[GROUP_MARKET_DATA], book_s=16)
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


def _drive(g, clock, group, rate_per_s, seconds, *, evaluate_every_s=10.0):
    """`seconds` 동안 `rate_per_s` 로 호출하며 주기적으로 should_shrink 를 평가한다.

    측정 기반 축소는 이제 **지속성**(MEASURED_SUSTAIN_S)을 요구하므로, 한 번 재고 끝내는
    호출 패턴으로는 재현되지 않는다 — 실제 루프처럼 계속 돌려야 한다.
    """
    orders = []
    step = 1.0 / rate_per_s
    next_eval = 0.0
    elapsed = 0.0
    while elapsed < seconds:
        g.on_request(group)
        clock.advance(step)
        elapsed += step
        if elapsed >= next_eval:
            next_eval += evaluate_every_s
            got = g.should_shrink()
            if got:
                orders.append(got)
    return orders


def test_measured_usage_triggers_shrink_even_when_the_plan_looks_fine():
    """재시도·백필처럼 계획에 없는 호출은 실측에만 나타난다 (지속되면 축소한다)."""
    clock = FrozenClock(0)
    g = guard(plan(), clock=clock)                            # 계획은 6.43 (예산 안)
    orders = _drive(g, clock, GROUP_MARKET_DATA, 10.0, 180.0)  # 10 req/s 를 3분간
    assert g.measured_rate(GROUP_MARKET_DATA) == pytest.approx(10.0, rel=0.05)
    assert orders, "지속 과부하인데도 축소가 한 번도 일어나지 않았다"
    assert any(GROUP_MARKET_DATA in o for o in orders)


def test_shipped_defaults_do_not_shrink_at_startup():
    """계획이 예산 안이면(CHART 3.33 ≤ 3.5) 기동만으로 축소되지 않는다.

    여유분(headroom)은 계획이 아니라 **실측**에 적용한다 — 승격 직후 백필·재시도처럼
    계획에 없는 CHART 호출이 그 여유를 쓰기 때문이다.
    """
    g = guard(clock=FrozenClock(0))
    assert g.validate_plan() == {}
    assert g.should_shrink() is None


def test_measured_chart_usage_still_triggers_shrink():
    """여유를 다 먹을 만큼 실사용이 지속되면(4 req/s > 3.325) 가드가 움직여야 한다."""
    clock = FrozenClock(0)
    g = guard(clock=clock)
    orders = _drive(g, clock, GROUP_CHART, 4.0, 180.0)
    assert g.measured_rate(GROUP_CHART) == pytest.approx(4.0, rel=0.05)
    assert orders and any(GROUP_CHART in o for o in orders)


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
    g = guard(plan(tier3=30, book_s=16), clock=clock)
    assert g.should_shrink()
    assert g.should_shrink() is None                           # 쿨다운 중
    clock.advance(31)
    assert g.should_shrink()


class Rec:
    """경보 문구까지 검증하기 위한 기록형 notifier."""

    def __init__(self):
        self.alerts: list[str] = []
        self.warns: list[str] = []

    def alert(self, msg):
        self.alerts.append(msg)

    def warn(self, msg):
        self.warns.append(msg)


def test_ranking_is_never_shrunk_only_alerted():
    """랭킹은 과거 조회가 불가능한 유일한 데이터 — 축소 대상이 아니다."""
    rec = Rec()
    g = BudgetGuard(LIMITS, 0.7, clock=FrozenClock(0), notifier=rec)
    g.set_plan(plan().__class__(**{**plan().__dict__, "ranking_snap_s": 0.5}))
    orders = g.should_shrink()
    assert orders is None or GROUP_RANKING not in orders
    assert rec.alerts and "RANKING" in rec.alerts[0]
    assert "predicted" in rec.alerts[0]                       # 진짜 초과 → 초과 문구가 맞다


def test_ranking_within_budget_never_raises_a_false_alert():
    """라이브 관측 회귀: "RANKING predicted 0.33 > target 3.50" 은 거짓 ERROR 였다.

    무인 운영에서 가짜 경보는 경보 무시 습관을 만들어 진짜 경보를 묻는다
    (W5 healthcheck 오탐과 같은 지적). 예산 이내면 어떤 경보도 나면 안 된다.
    """
    rec = Rec()
    g = BudgetGuard(LIMITS, 0.7, clock=FrozenClock(0), notifier=rec)
    g.set_plan(plan())                                        # RANKING 0.33 vs target 3.50
    assert g.planned_rate(GROUP_RANKING) < g.target(GROUP_RANKING)
    assert g.should_shrink() is None
    assert rec.alerts == []


def test_429_on_ranking_alerts_the_429_once_not_a_false_overrun():
    """429 로 진입한 경보는 429 라고 말해야 한다 — "predicted > target" 은 거짓이 된다.

    그리고 forced 플래그는 1회 경보 후 소거된다 — 예전에는 소거되지 않아
    같은 거짓 ERROR 가 매 사이클 반복됐다.
    """
    rec = Rec()
    g = BudgetGuard(LIMITS, 0.7, clock=FrozenClock(0), notifier=rec)
    g.set_plan(plan())                                        # 예산 이내 (0.33 < 3.50)
    g.on_429(GROUP_RANKING)

    assert g.should_shrink() is None                          # 랭킹 축소 지시는 없다
    assert len(rec.alerts) == 1
    assert "429" in rec.alerts[0]
    assert "> target" not in rec.alerts[0]                    # 거짓 초과 문구 금지
    assert g.rate_limited[GROUP_RANKING] == 1                 # 사고 자체는 기록된다

    for _ in range(3):                                        # 반복 호출에도 도배하지 않는다
        assert g.should_shrink() is None
    assert len(rec.alerts) == 1


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


# --------------------------------------------------------------------------- #
# 정원 축소 래칫 해제 (2026-08-04 실측: 429 8회에 tier2 300->76, tier3 20->2, 복귀 없음)
# --------------------------------------------------------------------------- #
def test_capacity_recovers_after_a_quiet_period():
    """429 없이 조용하면 깎였던 정원을 되돌린다 — 축소만 있고 회복이 없으면 래칫이다."""
    clock = FrozenClock(0)
    g = guard(plan(tier3=10), clock=clock)
    g.on_429(GROUP_MARKET_DATA)
    assert g.should_shrink()                              # 사고 -> 축소
    assert g.should_grow() is None                        # 사고 직후에는 안 올린다

    clock.advance(RECOVER_AFTER_S + 1)                    # 조용한 구간 경과
    grows = g.should_grow()
    assert grows and GROUP_MARKET_DATA in grows
    assert grows[GROUP_MARKET_DATA] >= 1


def test_recovery_waits_while_429s_keep_coming():
    clock = FrozenClock(0)
    g = guard(clock=clock)
    g.on_429(GROUP_MARKET_DATA)
    g.should_shrink()
    clock.advance(RECOVER_AFTER_S + 1)
    g.on_429(GROUP_MARKET_DATA)                           # 또 맞았다
    assert g.should_grow() is None                        # 타이머가 리셋된다


def test_recovery_holds_off_when_usage_is_still_tight():
    """실사용이 빡빡하면 되돌리지 않는다 — 되돌리는 순간 429 를 다시 부른다."""
    clock = FrozenClock(0)
    g = guard(clock=clock)
    g.on_429(GROUP_MARKET_DATA)
    g.should_shrink()
    clock.advance(RECOVER_AFTER_S + 1)
    for _ in range(360):                                  # 6 req/s (target 7.0 의 86%)
        g.on_request(GROUP_MARKET_DATA)
        clock.advance(1.0 / 6)
    assert g.measured_rate(GROUP_MARKET_DATA) > 7.0 * 0.70
    assert g.should_grow() is None


def test_recovery_steps_are_spaced_not_continuous():
    clock = FrozenClock(0)
    g = guard(clock=clock)
    g.on_429(GROUP_MARKET_DATA)
    g.should_shrink()                                     # 먼저 깎여야 되돌릴 게 있다
    clock.advance(RECOVER_AFTER_S + 1)
    assert g.should_grow()                                # 1스텝
    assert g.should_grow() is None                        # 곧바로 또 올리지 않는다
    clock.advance(RECOVER_AFTER_S + 1)
    assert g.should_grow()                                # 간격을 두면 다음 스텝


def test_single_429_no_longer_cuts_a_fifth_of_capacity():
    """429 한 건에 정원 20% 를 깎던 것이 300->76 래칫의 원인이었다."""
    clock = FrozenClock(0)
    g = guard(plan(tier2=120), clock=clock)
    g.on_429(GROUP_CHART)
    orders = g.should_shrink()
    assert orders and orders[GROUP_CHART] <= 120 * 0.12    # 24 -> 12 수준


# --------------------------------------------------------------------------- #
# 개장마다 tier3 가 깎이던 경로 (429 무관 measured-overshoot)
# 실측: 07-31 22:52 20->15 / 08-03 22:34 20->14(32초 만에 점수 0.65 축출) / 08-04 02:48 20->16
# --------------------------------------------------------------------------- #
def test_open_burst_right_after_a_session_change_does_not_shrink():
    """★ 개장 직후 정상 상태는 정원을 깎지 않는다 — 그 순간 측정치는 원래 튄다."""
    clock = FrozenClock(0)
    g = guard(clock=clock)
    g.note_session_change()                                   # 세션 전환 (개장)
    assert g.in_warmup()
    # 전환 직후 측정치가 목표를 넘겨 튄다 (개장 버스트)
    orders = _drive(g, clock, GROUP_MARKET_DATA, 10.0, MEASURED_WARMUP_S - 20)
    assert orders == [], f"워밍업 중에 축소가 일어났다: {orders}"
    assert g.measured_rate(GROUP_MARKET_DATA) > g.shrink_ceiling(GROUP_MARKET_DATA)


def test_real_sustained_overload_still_shrinks_after_warmup():
    """★ 진짜 과부하는 여전히 잡는다 — 워밍업은 유예이지 면제가 아니다."""
    clock = FrozenClock(0)
    g = guard(clock=clock)
    g.note_session_change()
    orders = _drive(g, clock, GROUP_MARKET_DATA, 10.0, MEASURED_WARMUP_S + 120)
    assert orders, "워밍업이 끝났는데도 지속 과부하를 못 잡았다"
    assert any(GROUP_MARKET_DATA in o for o in orders)


def test_a_single_measurement_spike_does_not_shrink():
    """한 번 튀는 것으로 깎지 않는다 (지속성 요구) — 개장 축출의 직접 원인이었다."""
    clock = FrozenClock(0)
    g = guard(clock=clock)
    for _ in range(200):                                      # 짧고 굵은 버스트
        g.on_request(GROUP_MARKET_DATA)
        clock.advance(0.01)
    assert g.should_shrink() is None                          # 지속되지 않았다
    clock.advance(MEASURED_SUSTAIN_S / 2)
    assert g.should_shrink() is None


def test_429_still_shrinks_immediately_even_in_warmup():
    """429 는 진짜 사고다 — 워밍업·지속성과 무관하게 즉시 반응한다."""
    clock = FrozenClock(0)
    g = guard(clock=clock)
    g.note_session_change()
    assert g.in_warmup()
    g.on_429(GROUP_MARKET_DATA)
    orders = g.should_shrink()
    assert orders and GROUP_MARKET_DATA in orders


# --------------------------------------------------------------------------- #
# 기준 일치 — "검증은 통과했는데 축소 트리거 바로 아래" 상태를 드러낸다
# --------------------------------------------------------------------------- #
def test_plan_and_shrink_use_the_same_ceiling():
    g = guard()
    for group in (GROUP_MARKET_DATA, GROUP_CHART):
        assert g.shrink_ceiling(group) == pytest.approx(g.target(group) * g.headroom)
        assert g.plan_ceiling(group) < g.shrink_ceiling(group)


def test_reserve_deficit_flags_a_plan_with_no_room_left():
    """여유를 못 남기는 계획은 조용히 넘어가면 안 된다 (구 출하 설정 20종목/16s)."""
    g = guard(plan(tier3=20, book_s=16))
    deficit = g.reserve_deficit()
    assert GROUP_MARKET_DATA in deficit                       # 계획 6.43 > 천장 5.95
    assert deficit[GROUP_MARKET_DATA] == pytest.approx(6.4278 - 7.0 * 0.85, abs=1e-2)
    assert GROUP_CHART not in deficit                         # CHART 는 여유가 있다
    assert g.validate_plan() == {}                            # 하드 위반은 아니다


def test_a_plan_with_real_reserve_reports_no_deficit():
    g = guard(plan(tier3=12, trades_s=5, book_s=20))
    assert g.reserve_deficit() == {}
    assert g.validate_plan() == {}


# --------------------------------------------------------------------------- #
# 2026-08-04 사용자 결정: 랭킹 2종 + tier3 10종목 × 호가 4초
#
# 이 블록은 "배포된 설정이 기동 즉시 축소를 부르지 않는다" 를 못박는다. 값을 복제하지
# 않고 `SHIPPED`(= config.example.yaml)에서 읽는 이유는, 설정만 되돌려놓고 테스트는
# 초록인 상태를 만들지 않기 위해서다.
# --------------------------------------------------------------------------- #
def test_shipped_config_boots_without_shrinking_and_keeps_reserve():
    """★ 요구사항 1 — 출하 설정이 **경고 없이** 통과해야 한다."""
    g = guard()                                               # SHIPPED 기반 계획
    assert g.validate_plan() == {}                            # 하드 위반 없음
    assert g.reserve_deficit() == {}                          # 경고도 없음 (여유 확보)
    assert g.should_shrink() is None                          # 기동 즉시 축소 없음

    rate = g.plan.rates()[GROUP_MARKET_DATA]
    assert rate == pytest.approx(5.178, abs=1e-3)             # tier1 .178 + 2.5 + 2.5
    assert g.shrink_ceiling(GROUP_MARKET_DATA) - rate == pytest.approx(1.472, abs=1e-3)
    assert g.plan_ceiling(GROUP_MARKET_DATA) - rate == pytest.approx(0.772, abs=1e-3)


def test_shipped_tier3_settings_are_the_decided_ones():
    """설정이 조용히 되돌아가면 위 여유 계산이 무의미해진다 — 값 자체를 고정한다."""
    assert SHIPPED.universe.tier3_max == 10
    assert SHIPPED.polling.tier3_orderbook_s == 4
    assert SHIPPED.polling.tier3_trades_s == 4                # 체결 주기는 건드리지 않았다


def test_ranking_budget_counts_two_lists_not_four():
    """랭킹 2종화는 RANKING 그룹 호출률을 절반으로 만든다 (디스크가 목적, 예산은 덤)."""
    two = guard(plan()).plan.rates()[GROUP_RANKING]
    four = guard(plan(ranking_types=4)).plan.rates()[GROUP_RANKING]
    assert two == pytest.approx(2 / SHIPPED.polling.ranking_snap_s)
    assert four == pytest.approx(2 * two)


def test_budget_ranking_count_matches_the_list_the_collector_actually_polls():
    """예산이 세는 종류 수와 수집기가 실제로 도는 목록이 어긋나면 계획이 거짓말이 된다."""
    from tossmon.collector import budget as B
    from tossmon.collector.loops import RANKING_TYPES as POLLED

    assert B.RANKING_TYPES == len(POLLED)
    # 실경로는 상수가 아니라 실제 목록을 세어 넘긴다 — 드리프트가 아예 불가능해야 한다.
    plan_from_cfg = TierPlan.from_config(SHIPPED, tier1_symbols=1, tier2_symbols=1,
                                         tier3_symbols=1, ranking_types=len(POLLED))
    assert plan_from_cfg.ranking_types == len(POLLED)


def test_quote_density_is_what_the_decision_bought():
    """호가 1건당 지나가는 체결 수 — 이 변경의 목적 자체를 숫자로 남긴다."""
    poll = SHIPPED.polling
    # 종목당 호가 주기 / 체결 주기 = 호가 사이에 들어오는 체결 폴 수.
    polls_between_quotes = poll.tier3_orderbook_s / poll.tier3_trades_s
    assert polls_between_quotes == 1.0                        # 16/4 = 4 였다


# --------------------------------------------------------------------------- #
# 1초 고정 창 (2026-08-04) — 서버는 1초 창으로 재는데 우리는 60초 평균으로 쟀다
#
# 이 블록의 핵심은 **"평균은 낮은데 1초 버스트가 있는"** 상황이다. 예전 모델은 이걸
# 못 봤고, 그래서 "한도의 1/5 인데 429" 가 설명되지 않았다.
# --------------------------------------------------------------------------- #
def _burst(g, clock, group, per_second, seconds, *, spread_s=0.05):
    """매 초 시작에 `per_second` 회를 몰아 쏘고 나머지 시간은 쉰다.

    60초 평균은 `per_second` 로 낮게 나오지만, 실제로는 매 초 앞머리에 몰려 있다 —
    서버의 1초 창에서는 그 순간이 전부다.
    """
    orders = []
    for _ in range(int(seconds)):
        for _ in range(per_second):
            g.on_request(group)
            clock.advance(spread_s / max(per_second, 1))
        clock.advance(1.0 - spread_s)
        got = g.should_shrink()          # 실제 루프처럼 계속 평가해야 지속 조건이 선다
        if got:
            orders.append(got)
    return orders


def test_a_low_average_can_hide_a_one_second_burst():
    """★ 필수 회귀 — 평균 7.0 은 안전해 보이지만 1초에 15회가 몰려 있다."""
    clock = FrozenClock(0)
    g = guard(clock=clock)
    # 15회씩 몰아 쏘되 나머지 시간을 쉬어서 60초 평균을 낮게 만든다.
    for _ in range(30):
        for _ in range(15):
            g.on_request(GROUP_MARKET_DATA)
            clock.advance(0.002)
        clock.advance(1.97)                       # 초당 15회 -> 2초에 15회 = 평균 7.5

    avg = g.measured_rate(GROUP_MARKET_DATA)
    peak = g.peak_1s(GROUP_MARKET_DATA)
    assert avg == pytest.approx(7.5, rel=0.15)                # 평균은 "한도 10 의 75%"
    assert peak >= 15                                          # 그러나 1초에 15회
    assert peak > g.limit_of(GROUP_MARKET_DATA)                # 공시 한도 초과다
    # 옛 모델의 판정 근거(평균)는 천장 6.65 를 넘지만, 진짜 위반은 첨두 쪽이고
    # 그 차이가 2배다 — 이것이 "1/5 을 쓰는데 429" 의 정체다.
    assert peak > avg * 1.9


def test_an_evenly_spread_stream_is_not_treated_as_a_violation():
    """반대 방향: 같은 평균이라도 고르게 퍼져 있으면 서버는 불만이 없다."""
    clock = FrozenClock(0)
    g = guard(clock=clock)
    for _ in range(300):                                       # 5 req/s 를 60초 균등
        g.on_request(GROUP_MARKET_DATA)
        clock.advance(0.2)
    assert g.measured_rate(GROUP_MARKET_DATA) == pytest.approx(5.0, rel=0.1)
    assert g.peak_1s(GROUP_MARKET_DATA) <= 6                   # 첨두도 평균 근처
    assert not g.over_limit_1s(GROUP_MARKET_DATA)


def test_peak_is_measured_sliding_so_a_burst_across_the_boundary_still_counts():
    """정렬 버킷이면 경계에 걸친 버스트가 반으로 쪼개져 숨는다 — 슬라이딩이라 안 숨는다."""
    clock = FrozenClock(0)
    g = guard(clock=clock)
    clock.advance(0.9)
    for _ in range(8):                                         # 0.9s ~ 1.1s 에 8회
        g.on_request(GROUP_CHART)
        clock.advance(0.025)
    assert g.peak_1s(GROUP_CHART) == 8                          # 한 창에 8회로 보인다
    # 정렬 버킷으로 세면 (0초대 4 + 1초대 4) 로 쪼개져 첨두를 놓쳤을 것이다.
    assert max(g.per_second_counts(GROUP_CHART)) < 8


def test_shrink_now_follows_the_one_second_peak_not_the_average():
    """★ 판정 근거 전환 — 평균이 천장 아래여도 첨두가 넘으면 (지속되면) 깎는다."""
    clock = FrozenClock(0)
    g = guard(clock=clock)
    orders = _burst(g, clock, GROUP_MARKET_DATA, 9, 120)        # 평균 9, 첨두 9
    assert g.peak_1s(GROUP_MARKET_DATA) >= 9
    assert orders, "첨두가 천장(6.65)을 넘어 지속됐는데 축소가 없었다"
    assert any(GROUP_MARKET_DATA in o for o in orders)


def test_a_single_burst_does_not_shrink_without_persistence():
    """버스트 한 번으로 정원을 깎지 않는다 (어제 넣은 지속 조건은 그대로 유지된다)."""
    clock = FrozenClock(0)
    g = guard(clock=clock)
    for _ in range(15):
        g.on_request(GROUP_MARKET_DATA)
        clock.advance(0.002)
    assert g.peak_1s(GROUP_MARKET_DATA) >= 15                   # 첨두는 확실히 넘었고
    assert g.should_shrink() is None                            # 그래도 즉시 깎지는 않는다


def test_open_warmup_still_suppresses_peak_based_shrink():
    """개장 워밍업도 그대로 유지된다 — 첨두 기준으로 바뀌어도 마찬가지다."""
    clock = FrozenClock(0)
    g = guard(clock=clock)
    g.note_session_change()
    orders = _burst(g, clock, GROUP_MARKET_DATA, 9, 120)
    assert g.in_warmup()                                        # 120s < 180s 워밍업
    assert orders == [], "워밍업 중에는 첨두가 넘어도 깎지 않는다"


def test_exceeding_the_declared_limit_alerts_once_per_episode():
    """한도 초과는 정원이 아니라 리미터 문제다 — 경보하되 에피소드당 1회만."""
    clock = FrozenClock(0)
    rec = Rec()
    g = guard(clock=clock, notifier=rec)
    for _ in range(14):                                          # 한도 10 을 넘긴다
        g.on_request(GROUP_MARKET_DATA)
        clock.advance(0.002)
    g.should_shrink()
    g.should_shrink()
    g.should_shrink()
    over = [a for a in rec.alerts if "리미터" in a]
    assert len(over) == 1                                        # 반복 경보 없음
    assert g.counters["over_limit_1s"] == 1
    assert "1초에 14회" in over[0]


def test_grow_is_blocked_by_a_high_peak_even_when_the_average_is_low():
    """회복도 첨두를 본다 — 평균만 보면 버스트 중에 정원을 되돌린다."""
    clock = FrozenClock(0)
    g = guard(clock=clock)
    g._last_shrink_s[GROUP_MARKET_DATA] = 0.0                    # 깎인 적이 있다고 표시
    clock.advance(RECOVER_AFTER_S + 10)
    for _ in range(6):                                           # 첨두 6 > 7.0 x 0.70
        g.on_request(GROUP_MARKET_DATA)
        clock.advance(0.01)
    assert g.measured_rate(GROUP_MARKET_DATA) < 1.0              # 평균은 거의 0
    assert g.should_grow() is None                               # 그래도 되돌리지 않는다


def test_usage_ratio_above_what_the_limiter_can_honour_is_flagged():
    """★ 0.769 의 정체 — 토큰버킷 버스트 항이 정하는 상한이다.

    유휴 직후 1초 통과량 = rate x (1 + BURST_FRACTION). 이것이 공시 한도를 넘으면
    정원을 아무리 줄여도 소용없다 (버스트는 예산이 아니라 리미터가 만든다).
    """
    from tossmon.api.limiter import BURST_FRACTION

    rec = Rec()
    safe = 1.0 / (1.0 + BURST_FRACTION)
    assert BudgetGuard(LIMITS, 0.70).max_safe_usage_ratio() == pytest.approx(safe)
    assert safe == pytest.approx(0.769, abs=1e-3)

    ok = BudgetGuard(LIMITS, 0.70, notifier=rec)
    assert ok.check_usage_ratio() is None
    assert rec.alerts == []

    bad = BudgetGuard(LIMITS, 0.85, notifier=rec)
    assert bad.check_usage_ratio() == pytest.approx(0.85 - safe, abs=1e-3)
    assert rec.alerts and "하드캡" in rec.alerts[0]

    # 실제로 한도를 넘는지 산술로 확인한다 (경보 문구가 아니라 사실을 고정한다).
    limit = 10.0
    worst_at_085 = limit * 0.85 * (1 + BURST_FRACTION)
    assert worst_at_085 > limit                       # 11.05 > 10
    worst_at_safe = limit * safe * (1 + BURST_FRACTION)
    assert worst_at_safe == pytest.approx(limit)      # 딱 경계

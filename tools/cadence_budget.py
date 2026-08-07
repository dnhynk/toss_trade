"""폴 주기별 MARKET_DATA 예산표 — **코드가 실제로 쓰는 천장으로** 다시 계산한다 (docs/46 §6).

`docs/43` §9 의 표는 계획 부하를 `목표 8.5` 에만 대고 잤다 ("여유 4%"). 그런데 축소를
결정하는 것은 목표가 아니라 `BudgetGuard` 의 **두 천장**이고, 그 둘은 목표보다 낮다:

    축소 천장  shrink_ceiling = target × HEADROOM              (= 8.08)
    계획 천장  plan_ceiling   = target × (HEADROOM − RESERVE)   (= 7.23)

숫자를 여기 박아넣지 않는다 — `TierPlan` / `BudgetGuard` 를 그대로 불러 계산하므로
설정이나 상수가 바뀌면 이 표가 따라 움직인다. 표와 코드가 어긋날 자리가 없다.

라이브 호출 0. 재실행:
    python -m tools.cadence_budget
"""
from __future__ import annotations

import argparse

from tossmon.api import limiter as lim
from tossmon.collector.budget import (GROUP_MARKET_DATA, BudgetGuard, TierPlan)

#: 현행 설정 (`config/config.example.yaml`). tier2 호가는 **계획 밖**이라 TierPlan 에
#: 안 들어간다 — 그 몫은 PLAN_RESERVE_FRAC 여유에서 나간다 (아래에서 따로 더한다).
LIMITS = {"MARKET_DATA": 10, "MARKET_DATA_CHART": 5, "RANKING": 5}
USAGE_RATIO = 0.85
TIER1, TIER2, TIER3 = 1500, 300, 10
TIER1_SWEEP_S, TIER2_CANDLE_S, TIER3_BOOK_S = 45.0, 110.0, 4.0
TIER2_ORDERBOOK_S, RANKING_SNAP_S, RANKING_TYPES = 600.0, 12.0, 3

CADENCES = (8.0, 6.0, 4.0, 3.0, 2.0, 1.0)


def _plan(trades_s: float) -> TierPlan:
    return TierPlan(tier1_symbols=TIER1, tier2_symbols=TIER2, tier3_symbols=TIER3,
                    tier1_sweep_s=TIER1_SWEEP_S, tier2_candle_s=TIER2_CANDLE_S,
                    tier3_trades_s=trades_s, tier3_orderbook_s=TIER3_BOOK_S,
                    ranking_snap_s=RANKING_SNAP_S, ranking_types=RANKING_TYPES)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tier2-orderbook-s", type=float, default=TIER2_ORDERBOOK_S)
    args = ap.parse_args()

    guard = BudgetGuard(dict(LIMITS), USAGE_RATIO)
    target = guard.target(GROUP_MARKET_DATA)
    shrink_c = guard.shrink_ceiling(GROUP_MARKET_DATA)
    plan_c = guard.plan_ceiling(GROUP_MARKET_DATA)
    # 리미터가 **실제로 낼 수 있는** 지속 속도 (버킷 rate 와 하드캡 중 작은 쪽).
    sustained_cap = min(LIMITS["MARKET_DATA"] * USAGE_RATIO,
                        LIMITS["MARKET_DATA"] / lim.WINDOW_HORIZON_S)
    book2 = TIER2 / args.tier2_orderbook_s          # tier2 호가 (계획 밖, 게이트가 열렸을 때)

    print(f"# 한도 {LIMITS['MARKET_DATA']:.0f} req/s × usage_ratio {USAGE_RATIO} "
          f"= 목표 {target:.2f} req/s")
    print(f"# 리미터 지속 상한 min(rate {LIMITS['MARKET_DATA'] * USAGE_RATIO:.2f}, "
          f"cap/horizon {LIMITS['MARKET_DATA'] / lim.WINDOW_HORIZON_S:.2f}) "
          f"= {sustained_cap:.2f} req/s")
    print(f"# 축소 천장 {shrink_c:.2f}   계획 천장 {plan_c:.2f}   "
          f"tier2 호가(계획 밖) {book2:.2f}")
    print(f"# tier1 {TIER1}종/{TIER1_SWEEP_S:.0f}s, tier3 {TIER3}종, "
          f"호가 {TIER3_BOOK_S:.0f}s, tier2 {TIER2}종/{args.tier2_orderbook_s:.0f}s")
    print()
    head = (f"{'P(s)':>5} {'계획':>7} {'+tier2호가':>10} {'목표대비':>9} "
            f"{'계획천장대비':>13} {'축소천장대비':>13} {'남는여유':>9}  판정")
    print(head)
    print("-" * len(head))

    for p in CADENCES:
        plan = _plan(p)
        guard.set_plan(plan)
        planned = plan.rates()[GROUP_MARKET_DATA]
        total = planned + book2
        # 판정은 코드에 묻는다 — 손으로 다시 세지 않는다.
        over_plan = guard.validate_plan().get(GROUP_MARKET_DATA)
        deficit = guard.reserve_deficit().get(GROUP_MARKET_DATA)
        if over_plan:
            verdict = f"기동 즉시 축소 (계획이 축소 천장을 {over_plan:+.2f} 넘는다)"
        elif total > shrink_c:
            verdict = f"tier2 호가까지 켜면 축소 천장 초과 ({total - shrink_c:+.2f})"
        elif deficit:
            verdict = f"계획 여유 부족 {deficit:+.2f} — 재시도·백필이 축소를 부른다"
        else:
            verdict = "통과"
        print(f"{p:>5.0f} {planned:>7.2f} {total:>10.2f} {total / target:>8.0%} "
              f"{total / plan_c:>12.0%} {total / shrink_c:>12.0%} "
              f"{shrink_c - total:>+9.2f}  {verdict}")

    print()
    print("읽는 법: '계획' 은 TierPlan.rates() (tier2 호가 제외), '+tier2호가' 는 게이트가")
    print("열렸을 때의 실제 부하다. 축소를 결정하는 것은 목표(8.50)가 아니라 축소 천장이고,")
    print("설정이 지켜야 하는 것은 계획 천장이다 — 그 차이가 재시도·승격 백필·세션 전환")
    print("베이스라인 갱신의 몫이다.")


if __name__ == "__main__":
    main()

"""예산 가드 — 계약 C-8. 소유: W4. 한도 70% 초과 예측 시 티어 자동 축소.

**초과는 버그가 아니라 사고로 취급한다.** 그래서 이 모듈은 두 방향에서 본다:

1. **계획 검증** (`validate_plan`) — 설정값만으로 계산한 초당 호출수가 예산을 넘는지
   기동 시점에 확인한다. `config/config.example.yaml` 하단의 산식과 1:1 이며,
   설정이 예산을 넘긴 채로 기동되면 경보 + 자동 축소한다 (코디네이터 지시).
2. **실사용 관측** (`on_request`) — 슬라이딩 윈도우로 실제 초당 호출수를 재고,
   계획과 실측 중 큰 쪽을 "예측"으로 삼는다. 재시도·백필처럼 계획에 없는 호출이
   예산을 먹는 경우를 이쪽이 잡는다.

`GroupRateLimiter` 와 역할이 다르다: limiter 는 **호출을 늦춰서** 한도를 지키고(대기),
BudgetGuard 는 **감시 대상을 줄여서** 지연 자체가 생기지 않게 한다. 늦추기만 하면 큐가 밀려
tier3 폴링 주기가 조용히 무너지므로 둘 다 필요하다.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Mapping

from ..api.endpoints import DEFAULT_LIMIT, SPEC_LIMITS

GROUP_MARKET_DATA = "MARKET_DATA"
GROUP_CHART = "MARKET_DATA_CHART"
GROUP_RANKING = "RANKING"

#: 그룹 → 그 그룹이 먹여 살리는 티어 (축소 지시의 대상).
SHRINK_TIER: dict[str, int] = {GROUP_MARKET_DATA: 3, GROUP_CHART: 2}

#: 배치 상한 (/prices, /stocks). client.BATCH_MAX 와 같은 값.
BATCH_MAX = 200
#: 랭킹 스냅샷 종류 수 (MARKET/TOSS × AMOUNT/VOLUME).
RANKING_TYPES = 4

#: 실사용 관측 윈도우 (초).
WINDOW_S = 60.0
#: **실측** 사용률이 예산의 이 비율을 넘으면 축소한다 (한도에 닿기 전에 움직인다).
#: 계획(설정값)에는 이 여유를 적용하지 않는다 — 설정이 예산 안이면 그대로 인정하고,
#: 여유분은 계획에 없는 호출(재시도·승격 직후 백필)을 위해 남겨 둔다.
HEADROOM = 0.95
#: 축소 후 목표 사용률 — 경계에 딱 붙이면 곧바로 다시 넘는다.
SHRINK_TO = 0.9
#: 같은 그룹에 축소를 다시 지시하기까지의 최소 간격 (초). 플래핑 방지.
SHRINK_COOLDOWN_S = 30.0
#: 429 를 맞으면 이 비율만큼 추가로 줄인다 (사고 대응).
#: 0.2 는 과했다 — 429 한 건에 정원 120 이 24 씩 깎여 8회 만에 300->76 이 됐다
#: (2026-08-04 실측). 회복 경로가 생겼으니 한 번에 크게 자를 이유가 없다.
RATE_LIMITED_SHRINK_FRAC = 0.10

#: 마지막 429 이후 이만큼 조용하면 정원을 한 단계 **되돌린다**.
#: 이것이 없으면 축소는 일방통행 래칫이 되어, 429 한 건의 대가를 세션 내내 치른다.
RECOVER_AFTER_S = 300.0
#: 회복 1스텝에 되돌리는 폭 (그 티어 상한 대비).
RECOVER_STEP_FRAC = 0.25
#: 회복은 실사용이 목표의 이 비율 아래일 때만 — 빡빡한데 되돌리면 429 를 다시 부른다.
RECOVER_USAGE_MAX = 0.70


@dataclass(frozen=True)
class TierPlan:
    """설정값 → 그룹별 계획 호출률 (req/s).

    산식은 `config/config.example.yaml` 하단 주석과 동일하다. 숫자를 두 곳에 두지 않으려고
    주석이 아니라 여기서 계산한다 — 설정을 바꾸면 이 계산이 따라 움직이고,
    예산을 넘기면 `validate_plan()` 이 기동 시점에 잡는다.
    """
    tier1_symbols: int
    tier2_symbols: int
    tier3_symbols: int
    tier1_sweep_s: float
    tier2_candle_s: float
    tier3_trades_s: float
    tier3_orderbook_s: float
    ranking_snap_s: float
    ranking_types: int = RANKING_TYPES
    batch_max: int = BATCH_MAX

    @classmethod
    def from_config(cls, cfg, *, tier1_symbols: int, tier2_symbols: int,
                    tier3_symbols: int) -> "TierPlan":
        polling = cfg.require_polling()
        return cls(
            tier1_symbols=int(tier1_symbols),
            tier2_symbols=int(tier2_symbols),
            tier3_symbols=int(tier3_symbols),
            tier1_sweep_s=float(polling.tier1_sweep_s),
            tier2_candle_s=float(polling.tier2_candle_s),
            tier3_trades_s=float(polling.tier3_trades_s),
            tier3_orderbook_s=float(polling.tier3_orderbook_s),
            ranking_snap_s=float(polling.ranking_snap_s),
        )

    def rates(self) -> dict[str, float]:
        batches = math.ceil(self.tier1_symbols / self.batch_max) if self.tier1_symbols else 0
        return {
            GROUP_MARKET_DATA: (batches / self.tier1_sweep_s
                                + self.tier3_symbols / self.tier3_trades_s
                                + self.tier3_symbols / self.tier3_orderbook_s),
            GROUP_CHART: self.tier2_symbols / self.tier2_candle_s,
            GROUP_RANKING: self.ranking_types / self.ranking_snap_s,
        }

    def per_symbol_cost(self, group: str) -> float:
        """그 그룹에서 심볼 1개를 줄일 때 절약되는 req/s."""
        if group == GROUP_MARKET_DATA:
            return 1.0 / self.tier3_trades_s + 1.0 / self.tier3_orderbook_s
        if group == GROUP_CHART:
            return 1.0 / self.tier2_candle_s
        return 0.0

    def symbols_of(self, group: str) -> int:
        if group == GROUP_MARKET_DATA:
            return self.tier3_symbols
        if group == GROUP_CHART:
            return self.tier2_symbols
        return 0


class BudgetGuard:
    def __init__(self, limits: dict[str, float], usage_ratio: float = 0.7, *,
                 window_s: float = WINDOW_S, headroom: float = HEADROOM,
                 clock=None, notifier=None):
        self.limits = dict(limits)
        self.usage_ratio = float(usage_ratio)
        self.window_s = float(window_s)
        self.headroom = float(headroom)
        self.clock = clock
        self.notifier = notifier
        self.plan: TierPlan | None = None
        self.counters: dict[str, int] = {}
        self.rate_limited: dict[str, int] = {}
        self._events: dict[str, deque[float]] = {}
        self._last_shrink_s: dict[str, float] = {}
        self._last_429_s: dict[str, float] = {}
        self._last_grow_s: dict[str, float] = {}
        self._forced: dict[str, float] = {}      # 429 로 강제 축소해야 할 비율

    # ---- 시간 ----------------------------------------------------------

    def _now_s(self) -> float:
        if self.clock is not None:
            return self.clock.now_ms() / 1000.0
        import time

        return time.monotonic()

    # ---- 한도 ----------------------------------------------------------

    def limit_of(self, group: str) -> float:
        if group in self.limits:
            return float(self.limits[group])
        return float(SPEC_LIMITS.get(group, DEFAULT_LIMIT))

    def target(self, group: str) -> float:
        """이 그룹에 허용된 초당 호출수 (= 공시 한도 × usage_ratio)."""
        return self.limit_of(group) * self.usage_ratio

    # ---- 관측 ----------------------------------------------------------

    def on_request(self, group: str) -> None:
        now = self._now_s()
        self.counters[group] = self.counters.get(group, 0) + 1
        q = self._events.setdefault(group, deque())
        q.append(now)
        cutoff = now - self.window_s
        while q and q[0] < cutoff:
            q.popleft()

    def on_429(self, group: str) -> None:
        """429 는 사고다 — 다음 `should_shrink()` 에서 강제로 줄인다."""
        self._last_429_s[group] = self._now_s()
        self.rate_limited[group] = self.rate_limited.get(group, 0) + 1
        self._forced[group] = max(self._forced.get(group, 0.0), RATE_LIMITED_SHRINK_FRAC)
        if self.notifier is not None:
            self.notifier.warn(f"budget: 429 on {group} "
                               f"(count={self.rate_limited[group]}) — forcing tier shrink")

    def measured_rate(self, group: str) -> float:
        """윈도우 평균 **지속** 사용률 (req/s).

        분모는 관측 구간이 아니라 **윈도우 전체**다. 이유: 백필 4연발처럼 수 ms 안에 몰린
        버스트를 관측 구간으로 나누면 수백 req/s 가 나와 티어가 통째로 날아간다.
        순간 버스트를 흡수하는 것은 `GroupRateLimiter`(대기)의 일이고, 여기서 봐야 하는 것은
        "이 페이스를 계속 유지하면 한도를 넘는가" 다. 창이 덜 찬 기동 직후에는 과소평가되는데,
        그쪽이 안전한 방향이다 (기동 버스트로 티어를 줄이지 않는다).
        """
        q = self._events.get(group)
        if not q:
            return 0.0
        cutoff = self._now_s() - self.window_s
        while q and q[0] < cutoff:
            q.popleft()
        return len(q) / self.window_s

    # ---- 계획 ----------------------------------------------------------

    def set_plan(self, plan: TierPlan | None) -> None:
        self.plan = plan

    def planned_rate(self, group: str) -> float:
        return self.plan.rates().get(group, 0.0) if self.plan is not None else 0.0

    def predicted_rate(self, group: str) -> float:
        """예측 사용률 = max(계획, 실측). 재시도·백필은 실측에만, 티어 확대는 계획에만 나타난다."""
        return max(self.planned_rate(group), self.measured_rate(group))

    def validate_plan(self) -> dict[str, float]:
        """설정값만으로 예산 초과를 예측한다. 반환: {group: 초과 req/s} (없으면 빈 dict)."""
        if self.plan is None:
            return {}
        over: dict[str, float] = {}
        for group, rate in self.plan.rates().items():
            target = self.target(group)
            if rate > target:
                over[group] = rate - target
        return over

    # ---- 축소 지시 ------------------------------------------------------

    def should_shrink(self) -> dict[str, int] | None:
        """그룹별 초과 예측 시 `{group: 줄일 심볼 수}`. 아니면 None.

        대상 티어는 `SHRINK_TIER` (MARKET_DATA→tier3, MARKET_DATA_CHART→tier2).
        랭킹은 과거 조회가 불가능한 유일한 데이터라 **축소 대상이 아니다** — 넘치면 경보만 낸다.
        """
        if self.plan is None:
            return None
        now = self._now_s()
        out: dict[str, int] = {}
        for group in (GROUP_MARKET_DATA, GROUP_CHART, GROUP_RANKING):
            target = self.target(group)
            planned = self.planned_rate(group)
            measured = self.measured_rate(group)
            predicted = max(planned, measured)
            forced = self._forced.get(group, 0.0)
            # 계획은 한도 자체로, 실측은 여유분(headroom)으로 판정한다.
            over = planned > target or measured > target * self.headroom
            if not over and not forced:
                continue
            if group not in SHRINK_TIER:
                # 축소 불가 그룹(랭킹 — 과거 조회가 불가능한 유일한 데이터)은 자동으로
                # 할 수 있는 것이 없다. **사람이 개입해야 하는 상황에서만** 경보한다:
                #   (a) over   — 계획/실측이 실제로 예산을 넘었다 → 주기(ranking_snap_s)나
                #                한도 설정을 재검토해야 한다
                #   (b) forced — 서버가 429 를 반환했다 → 예산 이내였는데도 맞았다면
                #                우리가 아는 한도 인식 자체가 틀렸다는 뜻이다
                # 예전에는 (b)로 진입해도 (a)의 "predicted > target" 문구로 경보해
                # "0.33 > 3.50" 같은 **거짓 ERROR** 가 났고, forced 가 소거되지 않아
                # 같은 경보가 매 사이클 반복됐다 — 가짜 경보는 경보 무시 습관을 만들어
                # 진짜 경보를 묻는다 (W5 healthcheck 오탐과 같은 지적).
                self._forced.pop(group, None)          # 1회 경보 후 소거 (반복 방지)
                if self.notifier is not None:
                    if over:
                        self.notifier.alert(
                            f"budget: {group} predicted {predicted:.2f} req/s > target "
                            f"{target:.2f} — 랭킹은 축소 대상이 아니다. 주기/한도를 재검토하라")
                    else:
                        self.notifier.alert(
                            f"budget: 429 on {group} (usage {predicted:.2f}/{target:.2f} "
                            f"req/s, 예산 이내) — 랭킹은 축소 대상이 아니며 한도 인식이 "
                            "틀렸을 수 있다. 주기/한도를 재검토하라")
                continue
            last = self._last_shrink_s.get(group)
            if last is not None and now - last < SHRINK_COOLDOWN_S:
                continue
            n = self._shrink_symbols(group, predicted, target, forced)
            if n > 0:
                out[group] = n
                self._last_shrink_s[group] = now
                self._forced.pop(group, None)
        return out or None

    def should_grow(self) -> dict[str, int] | None:
        """429 없이 조용했고 여유도 있으면 그룹별 **되돌릴 심볼 수**를 돌려준다.

        축소만 있고 회복이 없으면 429 한 건이 세션 전체의 수집 범위를 깎는다
        (2026-08-04 실측: 429 8회에 tier2 300->76, tier3 20->2, 자동 복귀 없음).
        회복 조건은 보수적이다 — 마지막 429 이후 `RECOVER_AFTER_S`, 실사용이 목표의
        `RECOVER_USAGE_MAX` 미만, 그리고 계획도 목표 이내일 때만 한 스텝 올린다.
        """
        if self.plan is None:
            return None
        now = self._now_s()
        out: dict[str, int] = {}
        for group in (GROUP_MARKET_DATA, GROUP_CHART):
            if group not in self._last_shrink_s:
                continue                                  # 깎인 적이 없으면 되돌릴 것도 없다
            last429 = self._last_429_s.get(group)
            if last429 is not None and now - last429 < RECOVER_AFTER_S:
                continue                                  # 아직 사고 직후다
            if self._forced.get(group):
                continue                                  # 처리 안 된 축소 지시가 남아 있다
            last_grow = self._last_grow_s.get(group)
            if last_grow is not None and now - last_grow < RECOVER_AFTER_S:
                continue                                  # 스텝 간 최소 간격
            target = self.target(group)
            if target <= 0:
                continue
            if self.measured_rate(group) > target * RECOVER_USAGE_MAX:
                continue                                  # 지금도 빡빡하다
            if self.planned_rate(group) > target:
                continue                                  # 계획 자체가 초과 상태
            have = self.plan.symbols_of(group)
            if have <= 0:
                continue
            out[group] = max(1, math.ceil(have * RECOVER_STEP_FRAC))
            self._last_grow_s[group] = now
        return out or None

    def _shrink_symbols(self, group: str, predicted: float, target: float,
                        forced: float) -> int:
        assert self.plan is not None
        cost = self.plan.per_symbol_cost(group)
        have = self.plan.symbols_of(group)
        if cost <= 0 or have <= 0:
            return 0
        excess = max(predicted - target * SHRINK_TO, 0.0)
        n = math.ceil(excess / cost) if excess > 0 else 0
        if forced:
            n = max(n, math.ceil(have * forced))
        return int(max(0, min(n, have - 1 if have > 1 else have)))

    # ---- 관측 덤프 ------------------------------------------------------

    def snapshot(self) -> dict[str, dict[str, float]]:
        groups = set(self.counters) | set(self._events) | set(
            self.plan.rates() if self.plan is not None else {})
        return {
            group: {
                "limit": self.limit_of(group),
                "target": self.target(group),
                "planned": self.planned_rate(group),
                "measured": self.measured_rate(group),
                "requests": float(self.counters.get(group, 0)),
                "http_429": float(self.rate_limited.get(group, 0)),
            }
            for group in sorted(groups)
        }

    def describe(self) -> str:
        parts = [f"{g}={s['measured']:.2f}/{s['target']:.2f}" for g, s in self.snapshot().items()]
        return "budget " + " ".join(parts)


def limits_from(mapping: Mapping[str, float] | None) -> dict[str, float]:
    return {str(k): float(v) for k, v in (mapping or {}).items()}


__all__ = ["BudgetGuard", "TierPlan", "SHRINK_TIER", "GROUP_CHART", "GROUP_MARKET_DATA",
           "GROUP_RANKING", "limits_from"]

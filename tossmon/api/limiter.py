"""GroupRateLimiter — 그룹별 토큰버킷 (계약 C-4).

기본 사용률 = 공시 한도 × usage_ratio(0.7). X-RateLimit-* 헤더로 자기보정,
429 시 Retry-After 준수.

자기보정 규칙 (overview.md "Rate Limits" 절 + 감사 B-1/B-2/H-4 반영):
- `X-RateLimit-Limit`      : 서버가 알려주는 초당 한도. **한도를 내리는 데만 쓴다.**
                             공시값(SPEC_LIMITS)보다 큰 값은 채택하지 않는다 — 헤더 의미가
                             바뀌면(예: 분당 쿼터 600) rate 가 60배로 뛰어 429 폭풍이 난다(감사 B-2).
- `X-RateLimit-Remaining`  : 버킷 잔량. 우리 추정 잔량보다 작으면 **서버값으로 끌어내린다**
                             (다른 프로세스가 같은 client 를 쓰고 있을 수 있으므로 위로는 올리지 않는다).
- `X-RateLimit-Reset`      : 토큰 1개 재충전까지 예상 초. Remaining=0 일 때 다음 시도 시각으로 쓴다.
- 429 `Retry-After`        : 그 시간만큼 그룹 전체를 정지 + 지수 백오프 배수(2x, 최대 8x).
                             회복은 **경과 시간 기준**이다 — 응답 건수 기준이면 초당 7콜 하는
                             그룹에서 감속 수명이 1초로 소멸한다(감사 H-4).

버스트 정책 (감사 B-1): 용량 == rate 이면 유휴 직후 첫 1초에 `capacity + rate` = 2×rate 가
통과한다(MARKET_DATA 실측 14회, 공시 10/s 초과). 이 프로젝트는 **429 를 사고로 규정**하므로
버스트 여유를 남길 이유가 없다. 용량을 rate 의 일부로 줄이고 버킷을 **빈 상태로 시작**한다.
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Mapping

from .endpoints import DEFAULT_LIMIT, SPEC_LIMITS

# 429 이후 rate 에 곱하는 감속 배수의 상한/회복.
MAX_BACKOFF = 8.0
RECOVER_FACTOR = 0.8      # 회복 1스텝당 배수를 이만큼씩 1.0 쪽으로 되돌린다.
RECOVER_INTERVAL_S = 60.0  # 회복 1스텝의 최소 경과 시간 (감사 H-4: 건수 기준 금지).

# 버킷 용량 = rate × 이 비율 (최소 1.0). 유휴 후 첫 1초 통과량 = capacity + rate.
BURST_FRACTION = 0.3


class _Bucket:
    __slots__ = ("rate", "capacity", "tokens", "last", "blocked_until", "backoff",
                 "last_429", "last_recover", "lock")

    def __init__(self, rate: float) -> None:
        self.rate = max(rate, 1e-3)
        self.capacity = _capacity_for(self.rate)
        # 빈 상태로 시작한다 — 기동 직후 버스트를 막는다 (감사 B-1).
        self.tokens = 0.0
        self.last = time.monotonic()
        self.blocked_until = 0.0
        self.backoff = 1.0
        self.last_429 = 0.0
        self.last_recover = time.monotonic()
        self.lock = asyncio.Lock()

    def effective_rate(self) -> float:
        return max(self.rate / self.backoff, 1e-3)

    def refill(self, now: float) -> None:
        elapsed = max(now - self.last, 0.0)
        self.last = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.effective_rate())

    def recover(self, now: float) -> None:
        """429 감속을 경과 시간 기준으로 푼다 (감사 H-4)."""
        if self.backoff <= 1.0:
            return
        steps = int((now - self.last_recover) // RECOVER_INTERVAL_S)
        if steps <= 0:
            return
        self.last_recover = now
        self.backoff = max(1.0, self.backoff * (RECOVER_FACTOR ** steps))


def _capacity_for(rate: float) -> float:
    return max(1.0, rate * BURST_FRACTION)


class GroupRateLimiter:
    def __init__(self, limits: dict[str, float], usage_ratio: float = 0.7):
        self.limits = dict(limits)
        self.usage_ratio = float(usage_ratio)
        self._buckets: dict[str, _Bucket] = {}
        # 관측: 서버 헤더가 공시 한도를 넘겨 클램프된 횟수. 0 이 아니면 API 사양 변경 신호다.
        self.counters: dict[str, int] = {"limit_header_clamped": 0}
        self.last_clamped: dict | None = None

    # ---- internals ------------------------------------------------------

    def _declared_limit(self, group: str) -> float:
        """공시 한도. config 에 없는 그룹은 스펙 표 → 보수적 기본값 순으로 보강."""
        if group in self.limits:
            return float(self.limits[group])
        return float(SPEC_LIMITS.get(group, DEFAULT_LIMIT))

    def _bucket(self, group: str) -> _Bucket:
        b = self._buckets.get(group)
        if b is None:
            b = _Bucket(self._declared_limit(group) * self.usage_ratio)
            self._buckets[group] = b
        return b

    def _spec_ceiling(self, group: str) -> float:
        """서버 헤더로도 넘을 수 없는 공시 한도 상한 (감사 B-2).

        config 로 받은 값과 스펙 표 중 **큰 쪽**을 천장으로 쓴다 — 운영자가 config 로
        공시값보다 높게 잡았다면 그건 사람의 의도적 결정이므로 존중하되, 서버 헤더 한 줄이
        그 위로 밀어올리는 것은 막는다.
        """
        candidates = [float(SPEC_LIMITS.get(group, DEFAULT_LIMIT))]
        if group in self.limits:
            candidates.append(float(self.limits[group]))
        return max(candidates)

    def snapshot(self, group: str) -> dict[str, float]:
        """관측/테스트용 상태 덤프."""
        b = self._bucket(group)
        return {
            "rate": b.rate,
            "effective_rate": b.effective_rate(),
            "capacity": b.capacity,
            "tokens": b.tokens,
            "backoff": b.backoff,
            "blocked_for_s": max(b.blocked_until - time.monotonic(), 0.0),
        }

    # ---- contract surface ----------------------------------------------

    async def acquire(self, group: str) -> None:
        """해당 그룹 슬롯 확보까지 대기."""
        b = self._bucket(group)
        # 락을 대기 중에도 잡고 있어야 같은 그룹의 동시 요청이 한도를 넘겨 몰리지 않는다.
        async with b.lock:
            while True:
                now = time.monotonic()
                if now < b.blocked_until:
                    await asyncio.sleep(b.blocked_until - now)
                    continue
                b.refill(now)
                if b.tokens >= 1.0:
                    b.tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - b.tokens) / b.effective_rate())

    def update_from_headers(self, group: str, headers: Mapping[str, str]) -> None:
        """X-RateLimit-Limit/Remaining/Reset 실측 반영."""
        b = self._bucket(group)
        limit = _num(headers, "X-RateLimit-Limit")
        remaining = _num(headers, "X-RateLimit-Remaining")
        reset = _num(headers, "X-RateLimit-Reset")

        if limit is not None and limit > 0:
            # 서버값은 한도를 **내리는 데만** 쓴다 (감사 B-2). 헤더 의미가 초당→분당 쿼터로
            # 바뀌기만 해도(600 = 10/s, 같은 뜻) rate 가 60배로 뛰기 때문이다.
            ceiling = self._spec_ceiling(group)
            effective_limit = min(limit, ceiling)
            if limit > ceiling:
                self.counters["limit_header_clamped"] += 1
                self.last_clamped = {"group": group, "header": limit, "ceiling": ceiling}
            target = effective_limit * self.usage_ratio
            if abs(target - b.rate) > 1e-9:
                b.rate = max(target, 1e-3)
                b.capacity = _capacity_for(b.rate)
                b.tokens = min(b.tokens, b.capacity)
            # 기록도 클램프된 값으로 — 다음 버킷 생성이 오염되지 않게.
            self.limits[group] = effective_limit

        if remaining is not None:
            # 서버 잔량은 우리 추정치의 상한. 위로 올리지 않는다.
            allowed = remaining * self.usage_ratio
            b.tokens = min(b.tokens, max(allowed, 0.0))
            if remaining <= 0 and reset is not None and reset > 0:
                b.blocked_until = max(b.blocked_until, time.monotonic() + reset)

        # 429 감속 회복은 **경과 시간** 기준이다 (감사 H-4). 성공 응답 건수로 풀면
        # 초당 7콜 하는 그룹에서 감속 수명이 1초가 된다.
        b.recover(time.monotonic())

    def on_429(self, group: str, retry_after_s: float) -> None:
        b = self._bucket(group)
        now = time.monotonic()
        wait = max(float(retry_after_s), 0.0)
        jitter = random.uniform(0.0, 0.25)
        b.blocked_until = max(b.blocked_until, now + wait + jitter)
        b.tokens = 0.0
        b.backoff = min(MAX_BACKOFF, b.backoff * 2.0)
        b.last_429 = now
        # 회복 타이머를 지금부터 다시 센다 — 429 직후 곧바로 풀리지 않도록.
        b.last_recover = now


def _num(headers: Mapping[str, str], name: str) -> float | None:
    """헤더 값을 float 로. 대소문자·공백·비수치 값에 관대하게."""
    raw = headers.get(name)
    if raw is None:
        lowered = {k.lower(): v for k, v in headers.items()}
        raw = lowered.get(name.lower())
    if raw is None:
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None

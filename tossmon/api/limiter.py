"""GroupRateLimiter — 그룹별 토큰버킷 (계약 C-4).

기본 사용률 = 공시 한도 × usage_ratio(0.7). X-RateLimit-* 헤더로 자기보정,
429 시 Retry-After 준수.

자기보정 규칙 (overview.md "Rate Limits" 절 기준):
- `X-RateLimit-Limit`      : 현재 허용된 초당 요청 수. 공시값과 다르면 **서버값을 채택**한다.
- `X-RateLimit-Remaining`  : 버킷 잔량. 우리 추정 잔량보다 작으면 **서버값으로 끌어내린다**
                             (다른 프로세스가 같은 client 를 쓰고 있을 수 있으므로 위로는 올리지 않는다).
- `X-RateLimit-Reset`      : 토큰 1개 재충전까지 예상 초. Remaining=0 일 때 다음 시도 시각으로 쓴다.
- 429 `Retry-After`        : 그 시간만큼 그룹 전체를 정지 + 지수 백오프 배수(2x, 최대 8x)를 걸고
                             성공 응답이 이어지면 서서히 회복한다.
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Mapping

from .endpoints import DEFAULT_LIMIT, SPEC_LIMITS

# 429 이후 rate 에 곱하는 감속 배수의 상한/회복률.
MAX_BACKOFF = 8.0
RECOVER_FACTOR = 0.8  # 성공할 때마다 배수를 이만큼씩 1.0 쪽으로 되돌린다.


class _Bucket:
    __slots__ = ("rate", "capacity", "tokens", "last", "blocked_until", "backoff", "lock")

    def __init__(self, rate: float) -> None:
        self.rate = max(rate, 1e-3)
        self.capacity = max(rate, 1.0)
        self.tokens = self.capacity
        self.last = time.monotonic()
        self.blocked_until = 0.0
        self.backoff = 1.0
        self.lock = asyncio.Lock()

    def effective_rate(self) -> float:
        return max(self.rate / self.backoff, 1e-3)

    def refill(self, now: float) -> None:
        elapsed = max(now - self.last, 0.0)
        self.last = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.effective_rate())


class GroupRateLimiter:
    def __init__(self, limits: dict[str, float], usage_ratio: float = 0.7):
        self.limits = dict(limits)
        self.usage_ratio = float(usage_ratio)
        self._buckets: dict[str, _Bucket] = {}

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
            target = limit * self.usage_ratio
            if abs(target - b.rate) > 1e-9:
                b.rate = max(target, 1e-3)
                b.capacity = max(target, 1.0)
                b.tokens = min(b.tokens, b.capacity)
            self.limits[group] = limit  # 실측 한도를 기록 (다음 버킷 생성에도 반영)

        if remaining is not None:
            # 서버 잔량은 우리 추정치의 상한. 위로 올리지 않는다.
            allowed = remaining * self.usage_ratio
            b.tokens = min(b.tokens, max(allowed, 0.0))
            if remaining <= 0 and reset is not None and reset > 0:
                b.blocked_until = max(b.blocked_until, time.monotonic() + reset)

        # 정상 응답이 이어지면 429 감속을 서서히 푼다.
        if b.backoff > 1.0:
            b.backoff = max(1.0, b.backoff * RECOVER_FACTOR)

    def on_429(self, group: str, retry_after_s: float) -> None:
        b = self._bucket(group)
        wait = max(float(retry_after_s), 0.0)
        jitter = random.uniform(0.0, 0.25)
        b.blocked_until = max(b.blocked_until, time.monotonic() + wait + jitter)
        b.tokens = 0.0
        b.backoff = min(MAX_BACKOFF, b.backoff * 2.0)


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

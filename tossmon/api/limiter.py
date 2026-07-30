"""GroupRateLimiter — 그룹별 토큰버킷 (계약 C-4).

기본 사용률 = 공시 한도 × usage_ratio(0.7). X-RateLimit-* 헤더로 자기보정,
429 시 Retry-After 준수.
"""
from __future__ import annotations

from typing import Mapping


class GroupRateLimiter:
    def __init__(self, limits: dict[str, float], usage_ratio: float = 0.7):
        self.limits = limits
        self.usage_ratio = usage_ratio

    async def acquire(self, group: str) -> None:
        """해당 그룹 슬롯 확보까지 대기."""
        raise NotImplementedError

    def update_from_headers(self, group: str, headers: Mapping[str, str]) -> None:
        """X-RateLimit-Limit/Remaining/Reset 실측 반영."""
        raise NotImplementedError

    def on_429(self, group: str, retry_after_s: float) -> None:
        raise NotImplementedError

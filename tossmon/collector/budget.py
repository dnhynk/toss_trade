"""예산 가드 — 계약 C-8. 소유: W4. 한도 70% 초과 예측 시 티어 자동 축소 (초과는 사고로 취급)."""
from __future__ import annotations


class BudgetGuard:
    def __init__(self, limits: dict[str, float], usage_ratio: float = 0.7):
        self.limits = limits
        self.usage_ratio = usage_ratio

    def on_request(self, group: str) -> None:
        raise NotImplementedError

    def should_shrink(self) -> dict[str, int] | None:
        """그룹별 초과 예측 시 {group: 축소 폭} 지시. 아니면 None."""
        raise NotImplementedError

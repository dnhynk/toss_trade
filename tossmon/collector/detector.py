"""검출기 — 계약 C-8. 소유: W4. 피처 계산은 tossmon.analysis 재사용 (중복 구현 금지)."""
from __future__ import annotations


def precursor_score(feats: dict[str, float]) -> float:
    raise NotImplementedError


class TierStateMachine:
    """티어 승격/강등 + 히스테리시스 (플래핑 방지). promotions 기록은 호출측(Store)."""

    def __init__(self, hysteresis_s: int):
        self.hysteresis_s = hysteresis_s

    def on_new_data(self, symbol: str, score: float, ts_ms: int) -> int | None:
        """새 tier 를 반환하거나, 변경 없으면 None."""
        raise NotImplementedError

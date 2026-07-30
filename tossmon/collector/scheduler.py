"""세션 스케줄러 — 계약 C-8. 소유: W4. 세션 판정은 /market-calendar/US 응답만 사용 (하드코딩 금지)."""
from __future__ import annotations

from ..api.models import UsMarketDay


def current_session(cal: dict[str, UsMarketDay], now_ms: int) -> str:
    """"day" | "pre" | "regular" | "after" | "closed"."""
    raise NotImplementedError

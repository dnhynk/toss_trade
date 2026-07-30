"""유니버스 빌드 파이프라인 — 소유: W2.

seed → /stocks 메타 보강(mock) → 필터 → former runner 태깅 → symbols 테이블 tier0/tier1 기록.
일 1회 실행 전제.
"""
from __future__ import annotations

from ..api.client import TossClient
from ..config import UniverseConfig
from ..store.writer import Store


async def build_universe(client: TossClient, store: Store, cfg: UniverseConfig) -> dict:
    """반환: {"tier0": n, "tier1": n, "former_runners": n} 요약."""
    raise NotImplementedError

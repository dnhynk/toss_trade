"""수집 루프 — 계약 C-8. 소유: W4. asyncio 단일 프로세스, 루프별 독립 task.

크래시/재시작 이어받기, 네트워크 단절 복구, 장시간 무인 실행 안정성 필수.
"""
from __future__ import annotations

from ..api.client import TossClient
from ..config import Config
from ..store.writer import Store


async def run_tier1_price_sweep(client: TossClient, store: Store, cfg: Config) -> None:
    raise NotImplementedError


async def run_tier2_candles(client: TossClient, store: Store, cfg: Config) -> None:
    raise NotImplementedError


async def run_tier3_micro(client: TossClient, store: Store, cfg: Config) -> None:
    raise NotImplementedError


async def run_rankings(client: TossClient, store: Store, cfg: Config) -> None:
    raise NotImplementedError

"""former runner 탐지 — 소유: W2. 최근 N개월 일봉에서 일중 ±30% 이상 경험 종목 태깅.

EDGAR 희석 태깅은 인터페이스/스텁까지만 (Phase 1 스코프 밖 — 구현 금지, 계약 C-6 filings).
"""
from __future__ import annotations

import pandas as pd


def detect_former_runners(df_1d: pd.DataFrame, lookback_days: int = 180,
                          intraday_move_min: float = 0.30) -> pd.DataFrame:
    """반환: symbol, 최근 러너 이벤트 횟수, 마지막 이벤트 ts_ms."""
    raise NotImplementedError


def tag_dilution_stub(symbols: list[str]) -> dict[str, dict]:
    """EDGAR 희석 태깅 스텁 — Phase 2 인터페이스만. 항상 빈 dict 값 반환."""
    raise NotImplementedError

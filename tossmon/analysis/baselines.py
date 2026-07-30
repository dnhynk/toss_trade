"""베이스라인 지표 — 계약 C-7. 소유: W3. 정의식은 docs/07_analysis_spec.md 에 기록할 것."""
from __future__ import annotations

import pandas as pd

from ..api.models import SessionWindow, UsMarketDay


def compute_daily_baseline(df_1d: pd.DataFrame) -> dict:
    """{adv20_qu, atr20_u, vol_z(mean/std) ...} — 20일 롤링."""
    raise NotImplementedError


def minute_of_session_volume_curve(df_1m: pd.DataFrame,
                                   calendar: list[UsMarketDay]) -> pd.Series:
    """세션 내 분 위치별 평균 거래량 곡선 (시간대 보정 RVOL의 분모)."""
    raise NotImplementedError


def rvol(df_1m: pd.DataFrame, curve: pd.Series, ts_ms: int) -> float:
    """시간대 보정 상대거래량."""
    raise NotImplementedError


def session_vwap_u(df_1m: pd.DataFrame, session: SessionWindow) -> pd.Series:
    """세션별 VWAP (마이크로달러) 시계열."""
    raise NotImplementedError

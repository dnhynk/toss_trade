"""전조 피처 추출 — 계약 C-7. 소유: W3.

룩어헤드 금지: t0_ms 이후 데이터가 섞이면 안 된다. 함수가 직접 잘라내고,
이를 검증하는 테스트가 반드시 존재해야 한다.
"""
from __future__ import annotations

import pandas as pd


def extract_precursor_features(df_1m: pd.DataFrame, rankings: pd.DataFrame,
                               t0_ms: int,
                               windows_min: tuple[int, ...] = (5, 15, 30, 60)) -> dict[str, float]:
    """T-window 별 거래량 z·RVOL 궤적, 가격 궤적 형태, 토스 쏠림도 레벨·기울기, 이력 피처."""
    raise NotImplementedError

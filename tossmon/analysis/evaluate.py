"""평가 — 계약 C-7. 소유: W3. docs/03 §3 검증 질문 6개와 1:1 대응."""
from __future__ import annotations

import pandas as pd


def q1_volume_leadtime(events: pd.DataFrame, feats: pd.DataFrame) -> pd.DataFrame:
    """거래량 이상의 가격 대비 리드타임 분포, 임계값별 정밀도/재현율."""
    raise NotImplementedError


def q2_ranking_lead_lag(events: pd.DataFrame, rankings: pd.DataFrame) -> pd.DataFrame:
    """토스 랭킹 진입 시각의 T0 대비 리드/래그 분포."""
    raise NotImplementedError


def q3_daymarket_persistence(events: pd.DataFrame) -> pd.DataFrame:
    """데이마켓 급등의 정규장 지속/소멸."""
    raise NotImplementedError


def q4_dump_speed(events: pd.DataFrame, df_1m: pd.DataFrame) -> pd.DataFrame:
    """피크→-20% 도달 시간 분포."""
    raise NotImplementedError


def q5_expectancy(events: pd.DataFrame, feats: pd.DataFrame,
                  cost_roundtrip: float = 0.01) -> pd.DataFrame:
    """전조 스코어 조건부 기대수익 분포 (왕복 비용 차감)."""
    raise NotImplementedError


def q6_time_of_day(events: pd.DataFrame) -> pd.DataFrame:
    """개장 15분 / 10:00 ET 전후 / 마감 전 신호 성능 차이."""
    raise NotImplementedError

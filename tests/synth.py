"""합성 데이터 생성기 — 소유: W3 (최우선 산출물).

현실적인 1분봉/랭킹 시나리오: coil→폭발형, 즉발형, 페이드형(HOD 조기·VWAP 상실),
덤프형, 노이즈형. 라벨 정답을 함께 반환해 검출기 평가의 ground truth 로 사용.
"""
from __future__ import annotations

import pandas as pd


def make_scenario(kind: str, seed: int = 0) -> tuple[pd.DataFrame, dict]:
    """(df_1m, truth_labels) 반환. kind: coil_pop|instant|fade|dump|noise."""
    raise NotImplementedError

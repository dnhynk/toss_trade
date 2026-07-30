"""이벤트 라벨링 — 계약 C-7. 소유: W3. 라벨 필드 정의: docs/03 §3."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class EventParams:
    window_min: int = 30
    ret_min: float = 0.15
    day_ret_min: float = 0.30
    rvol_min: float = 3.0


def detect_events(df_1m: pd.DataFrame, params: EventParams) -> pd.DataFrame:
    """급등 이벤트 검출 + 라벨 전부.

    반환 컬럼: t0_ms, kind, peak_ms, peak_ret, ret_30m, ret_close, session,
    (가용 시) vwap_close_rel, float_rotation, ranking_first_entry_ms.
    """
    raise NotImplementedError

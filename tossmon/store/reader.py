"""Reader (읽기 전용) — 계약 C-6. read-only URI 커넥션. DataFrame 의 시간 컬럼은 ts_ms int64. 소유: W2."""
from __future__ import annotations

from pathlib import Path

import pandas as pd


class Reader:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def read_candles_1m(self, symbol: str, t_from_ms: int, t_to_ms: int) -> pd.DataFrame:
        raise NotImplementedError

    def read_candles_1d(self, symbol: str, t_from_ms: int, t_to_ms: int) -> pd.DataFrame:
        raise NotImplementedError

    def read_rankings(self, ranking_type: str, t_from_ms: int, t_to_ms: int) -> pd.DataFrame:
        raise NotImplementedError

    def read_events(self, t_from_ms: int, t_to_ms: int) -> pd.DataFrame:
        raise NotImplementedError

    def symbols(self, tier: int | None = None) -> pd.DataFrame:
        raise NotImplementedError

"""Reader (읽기 전용) — 계약 C-6. read-only URI 커넥션. DataFrame 의 시간 컬럼은 ts_ms int64. 소유: W2."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

import pandas as pd


class Reader:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        uri = f"file:{quote(self.db_path.resolve().as_posix(), safe='/:')}?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True, timeout=30.0)
        self._conn.execute("PRAGMA query_only=ON")

    def _read(
        self, sql: str, params: tuple[object, ...], time_columns: tuple[str, ...]
    ) -> pd.DataFrame:
        frame = pd.read_sql_query(sql, self._conn, params=params)
        for column in time_columns:
            if column in frame:
                frame[column] = frame[column].astype("int64")
        return frame

    def read_candles_1m(self, symbol: str, t_from_ms: int, t_to_ms: int) -> pd.DataFrame:
        return self._read(
            """
            SELECT symbol, ts_ms, open_u, high_u, low_u, close_u, vol_qu
            FROM candles_1m
            WHERE symbol = ? AND ts_ms BETWEEN ? AND ?
            ORDER BY ts_ms
            """,
            (symbol, t_from_ms, t_to_ms),
            ("ts_ms",),
        )

    def read_candles_1d(self, symbol: str, t_from_ms: int, t_to_ms: int) -> pd.DataFrame:
        return self._read(
            """
            SELECT symbol, ts_ms, open_u, high_u, low_u, close_u, vol_qu
            FROM candles_1d
            WHERE symbol = ? AND ts_ms BETWEEN ? AND ?
            ORDER BY ts_ms
            """,
            (symbol, t_from_ms, t_to_ms),
            ("ts_ms",),
        )

    def read_rankings(self, ranking_type: str, t_from_ms: int, t_to_ms: int) -> pd.DataFrame:
        return self._read(
            """
            SELECT id, snap_ms, ranking_type, duration, rank, symbol, last_u,
                   vol_qu, amount_u
            FROM rankings_snap
            WHERE ranking_type = ? AND snap_ms BETWEEN ? AND ?
            ORDER BY snap_ms, duration, rank
            """,
            (ranking_type, t_from_ms, t_to_ms),
            ("snap_ms",),
        )

    def read_events(self, t_from_ms: int, t_to_ms: int) -> pd.DataFrame:
        return self._read(
            """
            SELECT id, symbol, t0_ms, kind, peak_ms, peak_ret, ret_30m,
                   ret_close, session, meta_json
            FROM events
            WHERE t0_ms BETWEEN ? AND ?
            ORDER BY t0_ms, id
            """,
            (t_from_ms, t_to_ms),
            ("t0_ms",),
        )

    def symbols(self, tier: int | None = None) -> pd.DataFrame:
        if tier is None:
            return self._read(
                "SELECT * FROM symbols ORDER BY symbol", (), ("updated_ms",)
            )
        return self._read(
            "SELECT * FROM symbols WHERE tier = ? ORDER BY symbol",
            (tier,),
            ("updated_ms",),
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Reader":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

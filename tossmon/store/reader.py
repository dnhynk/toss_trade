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
        """symbols 테이블 조회 — 컬렉터 워치리스트 시드 계약 (docs/10_audit.md F-2).

        필터링은 쓰기 시점에 끝나 있다: 이 테이블에 있는 행은 이미
        `universe.build_universe` 에서 `filters.passes_tier0` 를 통과한 것들뿐이다
        (보통주, status=ACTIVE, ETF/ETN 제외, 가격·시총 범위 내) — 읽는 쪽이
        status/security_type 을 다시 검사할 필요는 없다.

        `tier` 는 `build_universe` 가 매기는 두 값 중 하나다:
          - 0: Tier 0 전체 유니버스 (필터 통과 전원, 수천 종목)
          - 1: Tier 1 광역 워치 — former runner 우선 + 시총 오름차순으로 골라
               `tier1_max` 개로 자른 부분집합 (docs/03 §1).
               **컬렉터가 워치리스트 시드로 읽어야 하는 값은 이것이다.**
        tier 2/3 는 `build_universe` 가 쓰지 않는다 — 수집 도중 컬렉터의 승격
        로직(promotions)이 매기는 값이라, 여기서 tier=2/3 로 조회하면 항상 빈
        프레임이 돌아온다.

        신선도: `updated_ms` 는 마지막 upsert 시각이다. `build_universe` 는 일 1회
        실행을 전제하므로, 시드를 읽는 쪽에서 `MAX(updated_ms)` 가 예상 주기보다
        훨씬 오래됐다면(예: 24~48h 초과) 유니버스 빌드가 멈췄다는 신호로 보고
        경고해야 한다 — 이 메서드는 신선도를 강제하지 않으므로 그 판단은 호출측
        (컬렉터, W4) 책임이다.
        """
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

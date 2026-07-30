from __future__ import annotations

import pandas as pd

from tossmon.api.models import Candle
from tossmon.store.retention import archive_and_prune
from tossmon.store.writer import Store


def _candle(ts_ms: int) -> Candle:
    return Candle("ABCD", ts_ms, 1, 2, 1, 2, 3)


def test_archive_then_prune_is_restart_safe(tmp_path):
    db_path = tmp_path / "monitor.db"
    archive_dir = tmp_path / "archive"
    with Store(db_path) as store:
        store.upsert_candles_1m([_candle(100), _candle(200), _candle(300)])

    assert archive_and_prune(db_path, archive_dir, 250) == {"candles_1m": 2}
    archive_path = archive_dir / "candles_1m_before_250.parquet"
    archived = pd.read_parquet(archive_path)
    assert archived["ts_ms"].tolist() == [100, 200]

    with Store(db_path) as store:
        assert store._conn.execute(
            "SELECT ts_ms FROM candles_1m ORDER BY ts_ms"
        ).fetchall() == [(300,)]
        # Simulate a late overlapping row; merging must not duplicate the
        # already archived primary key.
        store.upsert_candles_1m([_candle(200), _candle(225)])

    assert archive_and_prune(db_path, archive_dir, 250) == {"candles_1m": 2}
    archived = pd.read_parquet(archive_path)
    assert archived["ts_ms"].tolist() == [100, 200, 225]

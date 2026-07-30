"""오래된 시계열을 Parquet으로 옮긴 뒤 SQLite를 슬림화한다.

아카이브 파일 교체가 끝난 뒤에만 DB 행을 트랜잭션으로 삭제한다. 따라서
파일 쓰기 중 크래시는 DB를 건드리지 않고, 파일 교체 직후 크래시는 다음
실행에서 같은 키를 병합/중복 제거한 뒤 안전하게 재개된다.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from urllib.parse import quote

import pandas as pd

ARCHIVABLE_TABLES = {
    "candles_1m": ("symbol", "ts_ms"),
    "candles_1d": ("symbol", "ts_ms"),
    "trades_snap": ("symbol", "ts_ms", "price_u", "qty_u"),
}


def _read_old_rows(
    conn: sqlite3.Connection, table: str, cutoff_ms: int
) -> pd.DataFrame:
    return pd.read_sql_query(
        f"SELECT * FROM {table} WHERE ts_ms < ? ORDER BY ts_ms",
        conn,
        params=(cutoff_ms,),
    )


def archive_and_prune(
    db_path: Path,
    archive_dir: Path,
    cutoff_ms: int,
    tables: tuple[str, ...] = ("candles_1m",),
) -> dict[str, int]:
    """``cutoff_ms`` 이전 행을 Parquet에 보존하고 DB에서 제거한다.

    반환값은 테이블별 삭제 행 수다. 파일은 테이블과 cutoff별로 결정적인
    이름을 사용하며, 재실행 시 기존 파일과 PK 기준으로 병합한다.
    """
    unknown = set(tables) - ARCHIVABLE_TABLES.keys()
    if unknown:
        raise ValueError(f"unsupported archive tables: {sorted(unknown)}")
    if cutoff_ms < 0:
        raise ValueError("cutoff_ms must be non-negative")

    db_path = Path(db_path)
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    uri = f"file:{quote(db_path.resolve().as_posix(), safe='/:')}?mode=rw"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.execute("PRAGMA busy_timeout=30000")
    results: dict[str, int] = {}
    try:
        for table in tables:
            fresh = _read_old_rows(conn, table, cutoff_ms)
            if fresh.empty:
                results[table] = 0
                continue

            final_path = archive_dir / f"{table}_before_{cutoff_ms}.parquet"
            if final_path.exists():
                archived = pd.read_parquet(final_path)
                fresh = pd.concat([archived, fresh], ignore_index=True)
                fresh = fresh.drop_duplicates(
                    subset=list(ARCHIVABLE_TABLES[table]), keep="last"
                )
                fresh = fresh.sort_values("ts_ms", kind="stable")

            temp_path = final_path.with_suffix(".parquet.tmp")
            fresh.to_parquet(temp_path, index=False, engine="pyarrow")
            check = pd.read_parquet(temp_path, columns=list(ARCHIVABLE_TABLES[table]))
            if len(check) != len(fresh):
                temp_path.unlink(missing_ok=True)
                raise RuntimeError(f"archive verification failed for {table}")
            temp_path.replace(final_path)

            with conn:
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE ts_ms < ?", (cutoff_ms,)
                )
            results[table] = cursor.rowcount
        # This command is intended for an offline maintenance window.  A full
        # VACUUM is required because existing DBs normally use auto_vacuum=NONE;
        # DELETE alone would only put pages on SQLite's internal freelist.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
    finally:
        conn.close()
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path", type=Path)
    parser.add_argument("archive_dir", type=Path)
    parser.add_argument("cutoff_ms", type=int)
    parser.add_argument(
        "--tables",
        nargs="+",
        choices=sorted(ARCHIVABLE_TABLES),
        default=["candles_1m"],
    )
    args = parser.parse_args(argv)
    result = archive_and_prune(
        args.db_path, args.archive_dir, args.cutoff_ms, tuple(args.tables)
    )
    for table, count in result.items():
        print(f"{table}: archived and removed {count:,} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

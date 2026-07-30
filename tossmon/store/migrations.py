"""스키마 버전 마이그레이션 — 계약 C-6.

``meta`` 테이블의 ``schema_version`` 값을 기준으로 순차 적용한다.  v1은
``schema.sql`` 전체이며, 이후 버전은 이 모듈의 ``MIGRATIONS``에 추가한다.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

MIGRATIONS: dict[int, str] = {
    2: """
-- v2: events(symbol, t0_ms) 유니크화. 검출기가 매 사이클 버퍼를 재스캔하며
-- 같은 이벤트를 재검출해 중복 행이 쌓였다 — 보존 마이그레이션: 그룹별
-- id 최대(최신 검출, 가장 완성된 라벨) 행만 남기고 나머지를 제거한 뒤
-- 재발 방지를 위해 유니크 인덱스를 건다.
DELETE FROM events
WHERE id NOT IN (
    SELECT MAX(id) FROM events GROUP BY symbol, t0_ms
);
DROP INDEX IF EXISTS ix_events_symbol_t0;
CREATE UNIQUE INDEX ix_events_symbol_t0 ON events (symbol, t0_ms);
""",
}


def _current_version(conn: sqlite3.Connection) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    if not exists:
        return 0
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("invalid meta.schema_version") from exc


def apply_migrations(conn: sqlite3.Connection, schema_dir: Path | None = None) -> int:
    """현재 버전 → 최신 버전 순차 적용. 반환: 적용 후 버전."""
    schema_dir = schema_dir or Path(__file__).parent
    current = _current_version(conn)
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema v{current} is newer than supported v{SCHEMA_VERSION}"
        )

    for target in range(current + 1, SCHEMA_VERSION + 1):
        if target == 1:
            sql = (schema_dir / "schema.sql").read_text(encoding="utf-8")
        else:
            sql = MIGRATIONS[target]
        # executescript commits any pending transaction first.  The explicit
        # transaction in the script keeps DDL and the version marker atomic.
        try:
            conn.executescript(
                "BEGIN IMMEDIATE;\n"
                f"{sql}\n"
                "INSERT INTO meta(key, value) VALUES ('schema_version', "
                f"'{target}') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value;\n"
                "COMMIT;"
            )
        except Exception:
            conn.rollback()
            raise
    return _current_version(conn)

"""스키마 버전 마이그레이션 — 계약 C-6.

``meta`` 테이블의 ``schema_version`` 값을 기준으로 순차 적용한다.  v1은
``schema.sql`` 전체이며, 이후 버전은 이 모듈의 ``MIGRATIONS``에 추가한다.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

MIGRATIONS: dict[int, str] = {}


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

"""스키마 버전 마이그레이션 — 계약 C-6. meta(schema_version) 기준 순차 적용. 소유: W2."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1


def apply_migrations(conn: sqlite3.Connection, schema_dir: Path | None = None) -> int:
    """현재 버전 → 최신 버전 순차 적용. 반환: 적용 후 버전."""
    raise NotImplementedError

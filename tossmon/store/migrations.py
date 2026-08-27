"""스키마 버전 마이그레이션 — 계약 C-6.

``meta`` 테이블의 ``schema_version`` 값을 기준으로 순차 적용한다.  v1은
``schema.sql`` 전체이며, 이후 버전은 이 모듈의 ``MIGRATIONS``에 추가한다.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 4

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
    3: """
-- v3: 테이프 결손을 **위치를 가진 사건**으로 남긴다 (docs/43). 지금까지 결손은
-- 누적 카운터 `tape_gaps` 와 로그 한 줄뿐이었고, 로그는 32MB×4 로 회전해
-- 체결 보존기간보다 짧다 — 데이터가 남아 있는데 그 데이터의 결손 표시가 먼저
-- 사라진다. 순수 추가이므로 기존 행·읽기 경로에 영향이 없다.
--
-- 한 행 = "심볼 S 의 체결 중 **열린 구간** (gap_lo_ms, gap_hi_ms) 안의 것을 우리는
-- 갖고 있지 않다". `n_raw >= 50` 이면 원인이 `/trades` 한 응답 50건 상한이다
-- (실측 99.7%, docs/41 §4-2). `prev_poll_ms` 는 그 구간에 폴링이 **연속이었는지**를
-- 하류가 임계 없이 가르게 한다 — NULL 이면 수집기 재기동 직후라 보증할 수 없다.
-- 읽는 법은 docs/43 §4.
CREATE TABLE IF NOT EXISTS tape_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    poll_ms INTEGER NOT NULL,
    prev_poll_ms INTEGER,
    gap_lo_ms INTEGER NOT NULL,
    gap_hi_ms INTEGER NOT NULL,
    span_hi_ms INTEGER NOT NULL,
    n_raw INTEGER NOT NULL,
    n_stored INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tape_gaps_symbol_ms ON tape_gaps (symbol, gap_lo_ms);
CREATE INDEX IF NOT EXISTS ix_tape_gaps_ms ON tape_gaps (gap_lo_ms);
""",
    4: """
-- v4: API 응답의 rankedAt 을 버리지 않고 서버 랭킹 발행 시각으로 저장한다.
-- 기존 행에는 원값이 없으므로 NULL 허용이며, 테이블 재작성 없이 열 하나만 더한다.
ALTER TABLE rankings_snap ADD COLUMN ranked_at_ms INTEGER;
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
        # schema.sql 도 최신 rankings_snap 모양을 선언하므로 새 DB 는 v1 생성 때 이미
        # 이 열을 갖는다. 기존 v3 DB 에만 ALTER 를 실행하고, 새 DB 는 버전 표식만 전진한다.
        if target == 4 and any(
            row[1] == "ranked_at_ms"
            for row in conn.execute("PRAGMA table_info(rankings_snap)")
        ):
            sql = ""
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

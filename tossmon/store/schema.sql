-- tossmon 스키마 v1 — 계약 C-6. 변경은 migrations.py 버전 증가로만.
-- 시간: *_ms INTEGER (UTC epoch ms). 가격/금액: *_u INTEGER (마이크로달러). 수량: *_qu INTEGER (마이크로주).

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS symbols (
    symbol TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    market TEXT NOT NULL,
    security_type TEXT NOT NULL,
    status TEXT NOT NULL,
    list_date TEXT,
    shares_outstanding_qu INTEGER NOT NULL,
    tier INTEGER NOT NULL DEFAULT 0,
    is_former_runner INTEGER NOT NULL DEFAULT 0,
    updated_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS candles_1m (
    symbol TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    open_u INTEGER NOT NULL,
    high_u INTEGER NOT NULL,
    low_u INTEGER NOT NULL,
    close_u INTEGER NOT NULL,
    vol_qu INTEGER NOT NULL,
    PRIMARY KEY (symbol, ts_ms)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS candles_1d (
    symbol TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    open_u INTEGER NOT NULL,
    high_u INTEGER NOT NULL,
    low_u INTEGER NOT NULL,
    close_u INTEGER NOT NULL,
    vol_qu INTEGER NOT NULL,
    PRIMARY KEY (symbol, ts_ms)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS rankings_snap (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snap_ms INTEGER NOT NULL,
    ranking_type TEXT NOT NULL,
    duration TEXT NOT NULL,
    rank INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    last_u INTEGER NOT NULL,
    vol_qu INTEGER NOT NULL,
    amount_u INTEGER NOT NULL,
    UNIQUE (snap_ms, ranking_type, duration, rank)
);
CREATE INDEX IF NOT EXISTS ix_rankings_symbol_ms ON rankings_snap (symbol, snap_ms);

CREATE TABLE IF NOT EXISTS trades_snap (
    symbol TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    price_u INTEGER NOT NULL,
    qty_u INTEGER NOT NULL,
    PRIMARY KEY (symbol, ts_ms, price_u, qty_u)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS orderbook_snap (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    snap_ms INTEGER NOT NULL,
    ts_ms INTEGER,
    bid1_u INTEGER, bid1_qu INTEGER,
    ask1_u INTEGER, ask1_qu INTEGER,
    depth_json TEXT NOT NULL,
    spread_u INTEGER,
    imbalance REAL
);
CREATE INDEX IF NOT EXISTS ix_ob_symbol_ms ON orderbook_snap (symbol, snap_ms);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    t0_ms INTEGER NOT NULL,
    kind TEXT NOT NULL,
    peak_ms INTEGER,
    peak_ret REAL,
    ret_30m REAL,
    ret_close REAL,
    session TEXT,
    meta_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_symbol_t0 ON events (symbol, t0_ms);

CREATE TABLE IF NOT EXISTS promotions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    from_tier INTEGER NOT NULL,
    to_tier INTEGER NOT NULL,
    reason TEXT NOT NULL,
    score REAL
);
CREATE INDEX IF NOT EXISTS ix_promotions_symbol_ms ON promotions (symbol, ts_ms);

CREATE TABLE IF NOT EXISTS filings (
    symbol TEXT NOT NULL,
    kind TEXT NOT NULL,
    filed_ms INTEGER NOT NULL,
    meta_json TEXT,
    PRIMARY KEY (symbol, kind, filed_ms)
) WITHOUT ROWID;

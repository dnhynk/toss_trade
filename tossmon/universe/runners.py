"""former runner 탐지 — 소유: W2. 최근 N개월 일봉에서 일중 ±30% 이상 경험 종목 태깅.

EDGAR 희석 태깅은 인터페이스/스텁까지만 (Phase 1 스코프 밖 — 구현 금지, 계약 C-6 filings).
"""
from __future__ import annotations

import pandas as pd


def detect_former_runners(df_1d: pd.DataFrame, lookback_days: int = 180,
                          intraday_move_min: float = 0.30) -> pd.DataFrame:
    """반환: symbol, 최근 러너 이벤트 횟수, 마지막 이벤트 ts_ms."""
    columns = ["symbol", "event_count", "last_event_ms"]
    required = {"symbol", "ts_ms", "open_u", "high_u", "low_u"}
    missing = required - set(df_1d.columns)
    if missing:
        raise ValueError(f"daily candles missing columns: {sorted(missing)}")
    if lookback_days <= 0:
        raise ValueError("lookback_days must be positive")
    if intraday_move_min < 0:
        raise ValueError("intraday_move_min must be non-negative")
    if df_1d.empty:
        return pd.DataFrame(
            {
                "symbol": pd.Series(dtype="object"),
                "event_count": pd.Series(dtype="int64"),
                "last_event_ms": pd.Series(dtype="int64"),
            }
        )

    frame = df_1d.loc[:, list(required)].copy()
    frame["ts_ms"] = frame["ts_ms"].astype("int64")
    latest_ms = int(frame["ts_ms"].max())
    cutoff_ms = latest_ms - lookback_days * 86_400_000
    frame = frame[(frame["ts_ms"] >= cutoff_ms) & (frame["open_u"] > 0)]
    up_move = (frame["high_u"] - frame["open_u"]) / frame["open_u"]
    down_move = (frame["low_u"] - frame["open_u"]) / frame["open_u"]
    events = frame[(up_move >= intraday_move_min) | (down_move <= -intraday_move_min)]
    if events.empty:
        return pd.DataFrame(
            {
                "symbol": pd.Series(dtype="object"),
                "event_count": pd.Series(dtype="int64"),
                "last_event_ms": pd.Series(dtype="int64"),
            }
        )
    result = (
        events.groupby("symbol", sort=True)["ts_ms"]
        .agg(event_count="size", last_event_ms="max")
        .reset_index()
    )
    result["event_count"] = result["event_count"].astype("int64")
    result["last_event_ms"] = result["last_event_ms"].astype("int64")
    return result[columns]


def tag_dilution_stub(symbols: list[str]) -> dict[str, dict]:
    """EDGAR 희석 태깅 스텁 — Phase 2 인터페이스만. 항상 빈 dict 값 반환."""
    return {symbol: {} for symbol in dict.fromkeys(symbols)}

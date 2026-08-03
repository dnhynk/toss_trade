"""연속 슈팅 구조 측정 (docs/23) — 실시간 수집 전용, 수집일이 늘면 자동 재실행.

1분봉으로는 원리상 불가능하다. `rankings_snap` 의 ~13초 `last_u` 계열을 쓴다.
날짜를 하드코딩하지 않으므로 수집이 쌓이면 그대로 다시 돌리면 된다.

실행: `python -m tossmon.analysis.measure.shot_structure`

라이브 0콜. 수집 DB 는 **읽기 전용**(가동 중이므로 스냅샷 복사본 권장). 콘솔 ASCII.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from tossmon.analysis import shots as S

TOSS = "TOSS_SECURITIES_TRADING_AMOUNT"
MARKET = "MARKET_TRADING_AMOUNT"

#: 기본 입력 — 호출부가 바꿀 수 있다.
DEFAULT_DB = Path(r"C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db")


def ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)


def load_day(conn, day: str, ranking_type: str = TOSS) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT snap_ms, ranking_type, rank, symbol, last_u FROM rankings_snap "
        "WHERE ranking_type=? AND date(snap_ms/1000,'unixepoch')=? "
        "ORDER BY symbol, snap_ms", conn, params=(ranking_type, day))


def shots_for_day(rk: pd.DataFrame, *, min_snaps: int = 20, **kw) -> pd.DataFrame:
    """그날 관측된 전 심볼의 슈팅. 관측이 `min_snaps` 미만인 심볼은 **제외하고 센다**."""
    out, skipped = [], 0
    for sym, g in rk.groupby("symbol", sort=False):
        s = S.price_series(g, sym, g["ranking_type"].iloc[0])
        if len(s) < min_snaps:
            skipped += 1
            continue
        sh = S.detect_shots(s, symbol=sym, **kw)
        if len(sh):
            out.append(sh)
    res = pd.concat(out, ignore_index=True) if out else \
        pd.DataFrame(columns=list(S.SHOT_COLUMNS))
    res.attrs["symbols_skipped_thin"] = skipped
    return res


def main(db: Path = DEFAULT_DB) -> int:
    conn = ro(db)
    try:
        days = [r[0] for r in conn.execute(
            "SELECT DISTINCT date(snap_ms/1000,'unixepoch') FROM rankings_snap "
            "ORDER BY 1")]
        print(f"ranking collection days: {len(days)} -> {days}")
        res: dict = {"days": days, "per_day": {}}
        allshots = []
        for day in days:
            rk = load_day(conn, day)
            if rk.empty:
                continue
            n_sym = rk["symbol"].nunique()
            sh = shots_for_day(rk)
            allshots.append(sh.assign(day=day))
            per_sym = sh.groupby("symbol").size() if len(sh) else pd.Series(dtype=int)
            res["per_day"][day] = {
                "symbols_observed": int(n_sym),
                "symbols_thin_skipped": int(sh.attrs.get("symbols_skipped_thin", 0)),
                "symbols_with_shots": int(per_sym.size),
                "shots": int(len(sh)),
                "shots_per_symbol_median": (float(per_sym.median())
                                            if per_sym.size else 0.0),
            }
            print(f"  {day}: symbols {n_sym:>4}, with shots {per_sym.size:>4}, "
                  f"shots {len(sh):>5}")
        shots = pd.concat(allshots, ignore_index=True) if allshots else pd.DataFrame()
        if shots.empty:
            print("no shots detected")
            return 0

        print("\n=== shots per (symbol, day)")
        per = shots.groupby(["day", "symbol"]).size()
        print(f"  median {per.median():.0f}  p75 {per.quantile(.75):.0f}  "
              f"p90 {per.quantile(.90):.0f}  max {per.max()}")
        print(f"  share with >=2 shots: {(per >= 2).mean():.3f}   "
              f">=3: {(per >= 3).mean():.3f}   >=5: {(per >= 5).mean():.3f}")

        print("\n=== shot size / duration / spacing")
        for col, lab in (("rise", "rise"), ("span_s", "span_s"),
                         ("gap_prev_s", "gap_prev_s")):
            v = pd.to_numeric(shots[col], errors="coerce").dropna()
            print(f"  {lab:11} n={len(v):>5} median {v.median():>8.3f} "
                  f"p25 {v.quantile(.25):>8.3f} p75 {v.quantile(.75):>8.3f}")

        print("\n=== conditional structure by ordinal (does another shot come?)")
        tab = S.continuation_table(shots)
        print(tab.head(8).to_string(index=False,
                                    float_format=lambda x: f"{x:.4f}"))
        res["continuation"] = json.loads(tab.to_json())

        print("\n=== last shot vs middle shot (is the final one marked?)")
        shots = shots.sort_values(["day", "symbol", "start_ms"])
        shots["is_last"] = ~shots.duplicated(["day", "symbol"], keep="last")
        cmp = shots.groupby("is_last").agg(
            n=("rise", "size"), median_rise=("rise", "median"),
            median_span=("span_s", "median"), median_gap=("gap_prev_s", "median"))
        print(cmp.to_string(float_format=lambda x: f"{x:.4f}"))
        res["last_vs_middle"] = json.loads(cmp.to_json())

        res["shots_per_symbol_day"] = {
            "median": float(per.median()), "p75": float(per.quantile(.75)),
            "share_ge2": float((per >= 2).mean()),
            "share_ge3": float((per >= 3).mean())}
        res["total_shots"] = int(len(shots))
        res["required_days"] = S.required_days(len(shots) / max(1, len(days)))
        print(f"\nrequired days (n>=30 shots): {res['required_days']}")
        out = Path(__file__).resolve().parent / "shot_structure.json"
        out.write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
        print(f"wrote {out}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB))

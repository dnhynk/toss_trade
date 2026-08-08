"""1-day pilot of the live-only rotation axes (docs/17 section 5).

Live-only because the backfill has no rankings and no orderbook. The collection window
is currently ONE market day (2026-07-31), so everything here is descriptive - no
verdicts. The script is written to re-run unchanged as collection days accumulate:
point LIVE_DB at the snapshot and it will process every market day it finds.

Reads a SNAPSHOT copy of the live DB (the collector is running; we never hold a lock
on the production file). Live calls: 0. Console ASCII.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(r"C:/Users/dongh/orca/workspaces/toss_trade/w3-analyzer")
sys.path.insert(0, str(REPO))
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pandas as pd  # noqa: E402

from tossmon.analysis import rotation as R  # noqa: E402

LIVE_DB = HERE / "live_snapshot.db"
OUT = HERE / "rotation"

TOSS_TYPE = "TOSS_SECURITIES_TRADING_AMOUNT"
MARKET_TYPE = "MARKET_TRADING_AMOUNT"
TOP_N = 20
HANDOFF_WITHIN_S = 120
#: rotation score params must match the backfill validation so the two are comparable
WINDOW_MIN, STEP_MIN, MIN_PRINTS, MIN_COHORT = 30, 5, 3, 10
THRESHOLD = 0.90
LEAD_SCAN_MIN = 60


def ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    conn = ro(LIVE_DB)
    try:
        days = [r[0] for r in conn.execute(
            "SELECT DISTINCT date(snap_ms/1000,'unixepoch') FROM rankings_snap "
            "ORDER BY 1")]
        print(f"ranking collection days available: {len(days)} -> {days}")

        res: dict = {"days": days, "top_n": TOP_N,
                     "handoff_within_s": HANDOFF_WITHIN_S, "per_day": {}}

        for day in days:
            print(f"\n===== {day} =====")
            rk = pd.read_sql_query(
                "SELECT snap_ms, ranking_type, rank, symbol, amount_u "
                "FROM rankings_snap WHERE date(snap_ms/1000,'unixepoch')=? "
                "ORDER BY snap_ms, rank", conn, params=(day,))
            block: dict = {"ranking_rows": int(len(rk))}

            for label, rtype in (("toss", TOSS_TYPE), ("market", MARKET_TYPE)):
                ch = R.ranking_top_changes(rk, top_n=TOP_N, ranking_type=rtype)
                hf = R.rotation_handoffs(ch, within_s=HANDOFF_WITHIN_S)
                n_enter = int((ch["event"] == "enter").sum())
                n_exit = int((ch["event"] == "exit").sum())
                n_snap = int(rk[rk["ranking_type"] == rtype]["snap_ms"].nunique())
                churn = (n_enter / n_snap) if n_snap else float("nan")
                block[label] = {
                    "snapshots": n_snap, "enters": n_enter, "exits": n_exit,
                    "churn_per_snapshot": churn,
                    "distinct_symbols_entering": int(
                        ch[ch["event"] == "enter"]["symbol"].nunique()),
                    "handoffs": int(len(hf)),
                    "handoff_lag_s_median": (float(hf["lag_s"].median())
                                             if len(hf) else None),
                    "distinct_handoff_pairs": (int(len(hf.groupby(
                        ["exit_symbol", "enter_symbol"]))) if len(hf) else 0),
                }
                print(f"-- {label} top{TOP_N}: {n_snap} snaps, {n_enter} enters / "
                      f"{n_exit} exits, churn/snap={churn:.4f}, "
                      f"handoffs={len(hf)}")
                if len(ch):
                    ch.to_csv(OUT / f"changes_{label}_{day}.csv", index=False)
                if len(hf):
                    top = (hf.groupby(["exit_symbol", "enter_symbol"]).size()
                           .sort_values(ascending=False).head(5))
                    print(f"   most frequent handoff pairs: {top.to_dict()}")

            # --- does the bar-only rotation score LEAD the Toss ranking entry? ---
            ch_toss = R.ranking_top_changes(rk, top_n=TOP_N, ranking_type=TOSS_TYPE)
            enters = (ch_toss[ch_toss["event"] == "enter"]
                      .sort_values("snap_ms").groupby("symbol").first().reset_index())
            print(f"-- first-entry events into toss top{TOP_N}: {len(enters)}")
            # 봉의 날짜 소속은 라벨이 아니라 담는 구간으로 (docs/12 §6.1)
            bars = pd.read_sql_query(
                "SELECT symbol, ts_ms, close_u, vol_qu FROM candles_1m "
                "WHERE date((ts_ms-60000)/1000,'unixepoch')=? ORDER BY ts_ms",
                conn, params=(day,))
            leads = []
            for _i, e in enters.iterrows():
                t0 = int(e["snap_ms"])
                s = R.rotation_score_series(bars, e["symbol"], t0,
                                            scan_min=LEAD_SCAN_MIN, step_min=STEP_MIN,
                                            metric="rank_delta_pct",
                                            window_min=WINDOW_MIN,
                                            min_prints=MIN_PRINTS,
                                            min_cohort=MIN_COHORT)
                leads.append({
                    "symbol": e["symbol"], "entry_ms": t0,
                    "defined_pts": int(s.notna().sum()),
                    "max_score": (float(s.max()) if s.notna().any() else None),
                    "lead_min": R.first_cross_lead_min(s, t0, threshold=THRESHOLD),
                })
            ld = pd.DataFrame(leads)
            if len(ld):
                ld.to_csv(OUT / f"rotation_lead_vs_ranking_{day}.csv", index=False)
                defined = ld[ld["defined_pts"] > 0]
                det = defined["lead_min"].notna()
                block["rotation_vs_ranking"] = {
                    "n_entries": int(len(ld)),
                    "n_defined": int(len(defined)),
                    "detect_rate_given_defined": (float(det.mean())
                                                  if len(defined) else None),
                    "median_lead_min": (float(defined.loc[det, "lead_min"].median())
                                        if det.any() else None),
                    "edge_mass": R.window_edge_mass(defined.loc[det, "lead_min"],
                                                    LEAD_SCAN_MIN),
                }
                print(f"   rotation score defined for {len(defined)}/{len(ld)}; "
                      f"crossed {THRESHOLD} before entry in "
                      f"{det.mean() if len(defined) else float('nan'):.3f}; "
                      f"median lead "
                      f"{block['rotation_vs_ranking']['median_lead_min']} min")

            # --- spread trajectory around ranking entry (docs/16 open question) --
            ob = pd.read_sql_query(
                "SELECT symbol, snap_ms, bid1_u, ask1_u FROM orderbook_snap "
                "WHERE date(snap_ms/1000,'unixepoch')=? ORDER BY snap_ms",
                conn, params=(day,))
            # Ranking entrants barely overlap the orderbook universe (4 of 145 on
            # the pilot day), and per-symbol orderbook spans only ~20 min, so the
            # docs/16 question is answered at TIER PROMOTION instead - that is the
            # moment orderbook collection actually starts. Event-time blocks, not
            # clock windows (the clock version yields 0 of 143 by construction).
            promo = pd.read_sql_query(
                "SELECT symbol, ts_ms, from_tier, to_tier FROM promotions "
                "WHERE date(ts_ms/1000,'unixepoch')=? ORDER BY ts_ms",
                conn, params=(day,))
            comp = []
            for _i, e in enters.iterrows():
                comp.append({"symbol": e["symbol"], "at": "ranking_entry",
                             "compression": R.spread_compression_event_time(
                                 ob, e["symbol"], int(e["snap_ms"]))})
            for _i, e in promo.iterrows():
                comp.append({"symbol": e["symbol"], "at": "promotion",
                             "compression": R.spread_compression_event_time(
                                 ob, e["symbol"], int(e["ts_ms"]))})
            cd = pd.DataFrame(comp)
            block["promotions"] = int(len(promo))
            if len(cd):
                for at, g in cd.groupby("at"):
                    nd = int(g["compression"].notna().sum())
                    print(f"   spread at {at}: computable {nd}/{len(g)}"
                          + (f", median compression={g['compression'].median():.3f}"
                             f", share compressing="
                             f"{(g['compression'] > 1).mean():.3f}" if nd else ""))
                    block.setdefault("spread_by_event", {})[at] = {
                        "n": int(len(g)), "computable": nd,
                        "median_compression": (float(g["compression"].median())
                                               if nd else None),
                        "share_compressing": (float((g["compression"] > 1).mean())
                                              if nd else None)}
            n_def = int(cd["compression"].notna().sum()) if len(cd) else 0
            block["spread"] = {
                "orderbook_rows": int(len(ob)),
                "orderbook_symbols": int(ob["symbol"].nunique()) if len(ob) else 0,
                "entries_with_spread": n_def,
                "median_compression": (float(cd["compression"].median())
                                       if n_def else None),
                "share_compressing": (float((cd["compression"] > 1).mean())
                                      if n_def else None),
            }
            print(f"-- orderbook: {len(ob)} snaps / "
                  f"{ob['symbol'].nunique() if len(ob) else 0} symbols; "
                  f"spread computable at {n_def} of {len(cd)} entries; "
                  f"median compression={block['spread']['median_compression']}")
            if len(cd):
                cd.to_csv(OUT / f"spread_at_entry_{day}.csv", index=False)
            res["per_day"][day] = block
    finally:
        conn.close()

    (OUT / "pilot.json").write_text(json.dumps(res, indent=1, default=str),
                                    encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

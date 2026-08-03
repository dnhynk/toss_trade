"""Threshold sweep for the surviving cross-sectional measure (docs/17 section 4).

Only rank_delta_pct survived the density test, so this asks the natural follow-up:
does a stricter percentile threshold buy precision, and does it reduce the window
dependence of the lead statistic? Reuses the same DayGrid as the validation harness.

Live calls: 0. Input DB read-only. Console ASCII.
"""
from __future__ import annotations
import importlib.util, json, sqlite3, sys
from pathlib import Path
REPO = Path(r"C:/Users/dongh/orca/workspaces/toss_trade/w3-analyzer")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO)); sys.path.insert(0, str(HERE))
import numpy as np, pandas as pd
from tossmon.analysis import baselines as B, rotation as R
from phase1c import VAL_END

spec = importlib.util.spec_from_file_location("rv2", HERE / "rotation_validate2.py")
rv2 = importlib.util.module_from_spec(spec); spec.loader.exec_module(rv2)

OUT = HERE / "rotation"
BACKFILL = Path(r"C:/Users/dongh/orca/workspaces/toss_trade/w4-collector/data/backfill.db")
MIN_MS = 60_000
SCAN = 120
THRESHOLDS = (0.90, 0.95, 0.98)


def leads_multi(grid, symbol, t0, scan):
    """One pass -> first-cross lead for every threshold + definedness."""
    out = {t: float("nan") for t in THRESHOLDS}
    defined = 0
    t = t0 - scan * MIN_MS
    while t < t0:
        v = grid.rotation_pct(symbol, t)[0]      # rank_delta_pct
        if v == v:
            defined += 1
            for thr in THRESHOLDS:
                if v >= thr and out[thr] != out[thr]:
                    out[thr] = float((t0 - t) // MIN_MS)
        t += rv2.STEP_MIN * MIN_MS
    return defined, out


def main() -> int:
    ev = pd.read_parquet(HERE / "phase1c_v2" / "ev_v3.parquet")
    cal = [md for md in B.load_calendar(HERE / "phase1c_v2" / "calendar_used.json")
           if md.date <= VAL_END]
    by_date = {md.date: md for md in cal}
    dates = [md.date for md in cal]; idx = {d: i for i, d in enumerate(dates)}
    assert (ev["period"] != "holdout").all(), "HOLDOUT LEAKED"

    plan: dict[str, list[dict]] = {}
    for _i, e in ev.iterrows():
        plan.setdefault(e["market_date"], []).append(
            {"symbol": e["symbol"], "t0": int(e["t0_ms"]), "is_event": 1,
             "fill": float(e["fill"])})
    conn = rv2.ro(BACKFILL)
    try:
        for sym, grp in ev.groupby("symbol", sort=True):
            edays = set(grp["market_date"])
            have = {r[0] for r in conn.execute(
                "SELECT DISTINCT date(ts_ms/1000,'unixepoch') FROM candles_1m "
                "WHERE symbol=?", (sym,))}
            for _i, e in grp.iterrows():
                i0 = idx.get(e["market_date"])
                loc0 = B.locate_session(by_date[e["market_date"]], int(e["t0_ms"]))
                if i0 is None or loc0 is None:
                    continue
                sess, win0 = loc0
                moff = (int(e["t0_ms"]) - win0.start_ms) // MIN_MS
                for d in [x for x in reversed(dates[max(0, i0 - rv2.PRIOR_SPAN):i0])
                          if x in have and x not in edays and x <= VAL_END
                          ][:rv2.MAX_CONTROL_DAYS]:
                    w = getattr(by_date[d], sess, None)
                    if w is None:
                        continue
                    pt0 = w.start_ms + moff * MIN_MS
                    if pt0 < w.end_ms:
                        plan.setdefault(d, []).append(
                            {"symbol": sym, "t0": int(pt0), "is_event": 0,
                             "fill": float("nan")})
        rows = []
        for n, date in enumerate(sorted(plan), 1):
            md = by_date.get(date)
            if md is None:
                continue
            wins = B.session_windows(md)
            lo = int(wins[0][1].start_ms) - (SCAN + 2 * rv2.WINDOW_MIN) * MIN_MS
            bars = pd.read_sql_query(
                "SELECT symbol, ts_ms, close_u, vol_qu FROM candles_1m "
                "WHERE ts_ms>=? AND ts_ms<? ORDER BY ts_ms", conn,
                params=(lo, int(wins[-1][1].end_ms)))
            if bars.empty:
                continue
            grid = rv2.DayGrid(bars)
            for job in plan[date]:
                if job["symbol"] not in grid.sym_index:
                    continue
                d_pts, lds = leads_multi(grid, job["symbol"], job["t0"], SCAN)
                r = {"is_event": job["is_event"], "fill": job["fill"],
                     "defined": d_pts > 0}
                for thr in THRESHOLDS:
                    r[f"lead_{thr}"] = lds[thr]
                rows.append(r)
            if n % 40 == 0:
                print(f"  ..{n}/{len(plan)} days", flush=True)
    finally:
        conn.close()

    df = pd.DataFrame(rows)
    e, c = df[df.is_event == 1], df[df.is_event == 0]
    base = len(e) / (len(e) + len(c))
    print(f"\nevents={len(e)} controls={len(c)}  chance precision={base:.4f}")
    print(f"{'thr':>6}{'det|def':>9}{'ctl':>8}{'lift':>7}{'prec':>8}"
          f"{'prec/chance':>13}{'medlead':>9}{'edge':>7}")
    res = {"scan_min": SCAN, "chance_precision": base, "rows": []}
    for thr in THRESHOLDS:
        col = f"lead_{thr}"
        ed, cd = e[e.defined], c[c.defined]
        det = float(ed[col].notna().mean()); ctl = float(cd[col].notna().mean())
        tp, fp = int(ed[col].notna().sum()), int(cd[col].notna().sum())
        prec = tp / (tp + fp) if (tp + fp) else float("nan")
        ml = float(ed[col].median()) if ed[col].notna().any() else float("nan")
        edge = R.window_edge_mass(ed[col].dropna(), SCAN)
        rep = R.density_independence(ed[col].notna().astype(float), ed["fill"])
        print(f"{thr:>6.2f}{det:>9.3f}{ctl:>8.3f}{det / ctl if ctl else float('nan'):>7.2f}"
              f"{prec:>8.4f}{prec / base:>13.2f}{ml:>9.1f}{edge:>7.3f}"
              f"   density={'PASS' if rep['passed'] else 'FAIL'} "
              f"rates={[round(x, 3) for x in rep['quantile_rates']]}")
        res["rows"].append({"threshold": thr, "detect_given_defined": det,
                            "control_rate": ctl, "lift": (det / ctl) if ctl else None,
                            "precision": prec, "precision_over_chance": prec / base,
                            "median_lead_min": ml, "edge_mass": edge,
                            "density": rep})
    (OUT / "sweep.json").write_text(json.dumps(res, indent=1, default=str),
                                    encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

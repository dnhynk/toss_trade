"""Validation harness for the rotation-aware precursor measures (docs/17) - fast path.

Same checks as the first draft, restructured so it finishes: day frames are loaded
ONCE per market date (the first version re-queried a whole day per control row), and
each day's bars are reduced to numpy arrays so the window sums are vectorised.

Runs on the BACKFILL (train+val; holdout sealed at 2026-04-30). The cross-sectional
cohort is reconstructible from 1m bars alone, so the density and negative-control
checks run on n=904 events rather than on the single live pilot day.

Checks (docs/17 section 3):
  1. density independence - detect rate must NOT be monotone in event-day fill
  2. negative control  - same measure on non-event (symbol, day) -> precision
  3. window dependence - lead mass at the scan edge; widen and re-measure

Live calls: 0. Input DB read-only. Console ASCII.
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

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from tossmon.analysis import baselines as B  # noqa: E402
from tossmon.analysis import rotation as R  # noqa: E402
from phase1c import VAL_END  # noqa: E402

V2 = HERE / "phase1c_v2"
OUT = HERE / "rotation"
BACKFILL = Path(r"C:/Users/dongh/orca/workspaces/toss_trade/w4-collector/data/backfill.db")

MIN_MS = 60_000
WINDOW_MIN = 30
STEP_MIN = 5
MIN_PRINTS = 3
MIN_COHORT = 10
THRESHOLD = 0.90
SCANS = (60, 120)
PRIOR_SPAN = 25
MAX_CONTROL_DAYS = 5


def ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


class DayGrid:
    """One market day's prints as flat arrays -> fast repeated window aggregation.

    Only bars with volume are kept (금지 규칙 1: no zero-filling).
    """

    def __init__(self, bars: pd.DataFrame):
        v = pd.to_numeric(bars["vol_qu"], errors="coerce").fillna(0)
        d = bars[v.reindex(bars.index) >= R.PRINT_MIN_VOL_QU]
        d = d.sort_values("ts_ms")
        self.ts = d["ts_ms"].to_numpy(dtype="int64")
        codes, uniq = pd.factorize(d["symbol"], sort=False)
        self.sym_code = codes.astype("int64")
        self.symbols = list(uniq)
        self.sym_index = {s: i for i, s in enumerate(self.symbols)}
        # amount in float: only used for shares/ranks, never for exact accounting
        self.amount = (d["close_u"].to_numpy(dtype="float64")
                       * d["vol_qu"].to_numpy(dtype="float64"))
        self.n_sym = len(self.symbols)

    def window(self, lo: int, hi: int):
        """(n_prints per symbol, amount per symbol) over [lo, hi)."""
        i, j = np.searchsorted(self.ts, lo), np.searchsorted(self.ts, hi)
        if j <= i:
            return None, None
        c = self.sym_code[i:j]
        cnt = np.bincount(c, minlength=self.n_sym)
        amt = np.bincount(c, weights=self.amount[i:j], minlength=self.n_sym)
        return cnt, amt

    def rotation_pct(self, symbol: str, t_ms: int) -> tuple[float, float]:
        """(rank_delta_pct, share_delta_pct) for one symbol; NaN if outside cohort."""
        si = self.sym_index.get(symbol)
        if si is None:
            return float("nan"), float("nan")
        w = WINDOW_MIN * MIN_MS
        c_now, a_now = self.window(t_ms - w, t_ms)
        c_prev, a_prev = self.window(t_ms - 2 * w, t_ms - w)
        if c_now is None or c_prev is None:
            return float("nan"), float("nan")
        cohort = (c_now >= MIN_PRINTS) & (c_prev >= MIN_PRINTS)
        if cohort.sum() < MIN_COHORT or not cohort[si]:
            return float("nan"), float("nan")
        an, ap = a_now[cohort], a_prev[cohort]
        tn, tp = an.sum(), ap.sum()
        if not (tn > 0 and tp > 0):
            return float("nan"), float("nan")
        pos = int(cohort[:si].sum())                # index of our symbol inside cohort
        share_d = an / tn - ap / tp
        # rank 1 = largest amount; improvement = prev_rank - now_rank
        rank_now = (-an).argsort().argsort() + 1.0
        rank_prev = (-ap).argsort().argsort() + 1.0
        rank_d = rank_prev - rank_now
        pct = lambda arr: float((arr <= arr[pos]).sum()) / len(arr)  # noqa: E731
        return pct(rank_d), pct(share_d)

    def lead(self, symbol: str, t0: int, scan_min: int, which: int) -> tuple:
        """(defined_points, max_score, lead_min) scanning [t0-scan, t0)."""
        best, lead, defined = float("nan"), float("nan"), 0
        t = t0 - scan_min * MIN_MS
        while t < t0:
            v = self.rotation_pct(symbol, t)[which]
            if v == v:
                defined += 1
                best = v if best != best else max(best, v)
                if v >= THRESHOLD and lead != lead:
                    lead = float((t0 - t) // MIN_MS)
            t += STEP_MIN * MIN_MS
        return defined, best, lead


def measure_event_time(sym_bars: pd.DataFrame, t0: int) -> dict:
    p = R.print_frame(sym_bars, t_to=t0)
    return {"n_prints_pre": int(len(p)), "pir": R.print_intensity_ratio(p),
            "psr": R.print_size_ratio(p), "dwr": R.dormancy_wake_ratio(p)}


def row_for(grid: DayGrid, sym_bars: pd.DataFrame, symbol: str, t0: int) -> dict:
    r = measure_event_time(sym_bars, t0)
    for scan in SCANS:
        for which, name in ((0, "rank_delta_pct"), (1, "share_delta_pct")):
            d, b, ld = grid.lead(symbol, t0, scan, which)
            r[f"{name}_defined_{scan}"] = d > 0
            r[f"{name}_max_{scan}"] = b
            r[f"{name}_lead_{scan}"] = ld
    return r


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    ev = pd.read_parquet(V2 / "ev_v3.parquet")
    cal = [md for md in B.load_calendar(V2 / "calendar_used.json") if md.date <= VAL_END]
    by_date = {md.date: md for md in cal}
    dates = [md.date for md in cal]
    idx = {d: i for i, d in enumerate(dates)}
    assert (ev["period"] != "holdout").all(), "HOLDOUT LEAKED"
    assert ev["market_date"].max() <= VAL_END, "date cap violated"

    # ---- plan every (symbol, day, t0) we need, events and controls together ----
    plan: dict[str, list[dict]] = {}
    for _i, e in ev.iterrows():
        plan.setdefault(e["market_date"], []).append(
            {"symbol": e["symbol"], "t0": int(e["t0_ms"]), "is_event": 1,
             "period": e["period"], "fill": float(e["fill"]),
             "event_date": e["market_date"]})

    conn = ro(BACKFILL)
    try:
        for sym, grp in ev.groupby("symbol", sort=True):
            event_days = set(grp["market_date"])
            have = {r[0] for r in conn.execute(
                "SELECT DISTINCT date(ts_ms/1000,'unixepoch') FROM candles_1m "
                "WHERE symbol=?", (sym,))}
            for _i, e in grp.iterrows():
                i0 = idx.get(e["market_date"])
                if i0 is None:
                    continue
                loc0 = B.locate_session(by_date[e["market_date"]], int(e["t0_ms"]))
                if loc0 is None:
                    continue
                sess_name, win0 = loc0
                moff = (int(e["t0_ms"]) - win0.start_ms) // MIN_MS
                picked = [d for d in reversed(dates[max(0, i0 - PRIOR_SPAN):i0])
                          if d in have and d not in event_days and d <= VAL_END
                          ][:MAX_CONTROL_DAYS]
                for d in picked:
                    win = getattr(by_date[d], sess_name, None)
                    if win is None:
                        continue
                    pt0 = win.start_ms + moff * MIN_MS
                    if pt0 >= win.end_ms:
                        continue
                    plan.setdefault(d, []).append(
                        {"symbol": sym, "t0": int(pt0), "is_event": 0,
                         "period": e["period"], "fill": float("nan"),
                         "event_date": e["market_date"]})

        print(f"plan: {sum(len(v) for v in plan.values())} rows over "
              f"{len(plan)} market days")

        rows: list[dict] = []
        max_scan = max(SCANS)
        for n, date in enumerate(sorted(plan), 1):
            md = by_date.get(date)
            if md is None:
                continue
            wins = B.session_windows(md)
            lo = int(wins[0][1].start_ms) - (max_scan + 2 * WINDOW_MIN) * MIN_MS
            hi = int(wins[-1][1].end_ms)
            bars = pd.read_sql_query(
                "SELECT symbol, ts_ms, close_u, vol_qu FROM candles_1m "
                "WHERE ts_ms>=? AND ts_ms<? ORDER BY ts_ms", conn, params=(lo, hi))
            if bars.empty:
                continue
            grid = DayGrid(bars)
            by_sym = {s: g for s, g in bars.groupby("symbol", sort=False)}
            reg = md.regular
            for job in plan[date]:
                sb = by_sym.get(job["symbol"])
                if sb is None:
                    continue
                r = row_for(grid, sb, job["symbol"], job["t0"])
                fill = job["fill"]
                if fill != fill and reg is not None:      # controls: compute own fill
                    nmin = (reg.end_ms - reg.start_ms) // MIN_MS
                    nb = int(((sb["ts_ms"] >= reg.start_ms)
                              & (sb["ts_ms"] < reg.end_ms)).sum())
                    fill = nb / nmin if nmin else float("nan")
                r.update({"symbol": job["symbol"], "market_date": date,
                          "is_event": job["is_event"], "period": job["period"],
                          "fill": fill})
                rows.append(r)
            if n % 25 == 0:
                print(f"  ..{n}/{len(plan)} days, rows={len(rows)}", flush=True)
    finally:
        conn.close()

    df = pd.DataFrame(rows)
    df.to_parquet(OUT / "measured.parquet")
    ev_df = df[df["is_event"] == 1]
    ctl = df[df["is_event"] == 0]
    print(f"\nmeasured: {len(ev_df)} event rows, {len(ctl)} control rows")

    res: dict = {"params": {"window_min": WINDOW_MIN, "step_min": STEP_MIN,
                            "min_prints": MIN_PRINTS, "min_cohort": MIN_COHORT,
                            "threshold": THRESHOLD, "scans": list(SCANS)},
                 "n_events": int(len(ev_df)), "n_controls": int(len(ctl)),
                 "scans": {}}

    for scan in SCANS:
        print(f"\n===== scan_min={scan} =====")
        block: dict = {}
        for name in ("rank_delta_pct", "share_delta_pct"):
            dcol, lcol = f"{name}_defined_{scan}", f"{name}_lead_{scan}"
            defined = ev_df[dcol].astype(float)
            sub = ev_df[ev_df[dcol]]
            det = sub[lcol].notna().astype(float)
            rep_def = R.density_independence(defined, ev_df["fill"])
            rep_det = R.density_independence(det, sub["fill"])
            edge = R.window_edge_mass(sub[lcol].dropna(), scan)
            cs = ctl[ctl[dcol]]
            tp, fp = int(det.sum()), int(cs[lcol].notna().sum())
            prec = tp / (tp + fp) if (tp + fp) else float("nan")
            block[name] = {
                "definedness": float(defined.mean()),
                "definedness_density": rep_def,
                "detect_given_defined": (float(det.mean()) if len(sub) else None),
                "detect_density": rep_det, "window_edge_mass": edge,
                "control_n_defined": int(len(cs)),
                "control_detect_rate": (float(cs[lcol].notna().mean())
                                        if len(cs) else None),
                "tp": tp, "fp": fp, "precision": prec,
                "median_lead_min": (float(sub[lcol].median())
                                    if sub[lcol].notna().any() else None)}
            print(f"-- {name}")
            print(f"   definedness {defined.mean():.3f} | detect|def "
                  f"{det.mean():.3f} (n={len(sub)}) | median lead "
                  f"{block[name]['median_lead_min']} min | edge {edge:.3f}")
            print(f"   DENSITY  rates={[round(x, 3) for x in rep_det['quantile_rates']]}"
                  f" rho={rep_det['spearman_rho']:+.3f} mono={rep_det['monotone']} -> "
                  f"{'PASS' if rep_det['passed'] else 'FAIL'}")
            print(f"   CONTROL  detect={block[name]['control_detect_rate']} "
                  f"precision={prec:.4f} (TP={tp} FP={fp})")
        res["scans"][str(scan)] = block

    print("\n===== event-time measures (scan-independent) =====")
    et: dict = {}
    for name, thr in (("pir", 2.0), ("psr", 2.0), ("dwr", 3.0)):
        v = ev_df[name]
        sub = ev_df[v.notna()]
        det = (sub[name] >= thr).astype(float)
        rep = R.density_independence(det, sub["fill"])
        cv = ctl[ctl[name].notna()]
        tp, fp = int(det.sum()), int((cv[name] >= thr).sum())
        prec = tp / (tp + fp) if (tp + fp) else float("nan")
        et[name] = {"threshold": thr, "definedness": float(v.notna().mean()),
                    "detect_given_defined": float(det.mean()) if len(sub) else None,
                    "density": rep, "control_n_defined": int(len(cv)),
                    "control_detect_rate": (float((cv[name] >= thr).mean())
                                            if len(cv) else None),
                    "tp": tp, "fp": fp, "precision": prec}
        print(f"-- {name} (>={thr}) defined={v.notna().mean():.3f} "
              f"detect|def={det.mean():.3f} "
              f"rates={[round(x, 3) for x in rep['quantile_rates']]} "
              f"rho={rep['spearman_rho']:+.3f} -> "
              f"{'PASS' if rep['passed'] else 'FAIL'} precision={prec:.4f}")
    res["event_time"] = et

    (OUT / "validation.json").write_text(json.dumps(res, indent=1, default=str),
                                         encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

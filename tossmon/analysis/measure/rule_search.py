"""Preregistration section 4.5 step 2 - conditional rule search (docs/19).

TRAIN ONLY (<= 2025-12-31). The validation period is NOT touched here; this script
produces the ranking table and the mechanical top-5 selection, nothing else.
Holdout stays sealed.

Costs come from the docs/18 measured model (band-conditional, "while moving"), with
the frozen 1.0% carried alongside per preregistration discipline.

Live calls: 0. Input DB read-only. Console ASCII.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from itertools import product
from pathlib import Path

REPO = Path(r"C:/Users/dongh/orca/workspaces/toss_trade/w3-analyzer")
sys.path.insert(0, str(REPO))
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from tossmon.analysis import execution as X  # noqa: E402
from tossmon.analysis import rotation as ROT  # noqa: E402
from tossmon.analysis import rules as R  # noqa: E402

V2 = HERE / "phase1c_v2"
OUT = HERE / "rules"
BACKFILL = Path(r"C:/Users/dongh/orca/workspaces/toss_trade/w4-collector/data/backfill.db")

MIN_MS = 60_000
TRAIN_END = "2025-12-31"
HORIZON_MIN = 390          # same horizon Q4 used
PRE_MIN = 240              # pre-T0 window for the slope signal


def ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)


# --------------------------------------------------------------------------- #
# 1. per-event attributes + paths
# --------------------------------------------------------------------------- #
def event_frames(conn: sqlite3.Connection, sym: str,
                 t0: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """One event's (pre, post) candle frames under contract C-7 amendment A2.

    Both sides are CANDLES, and `ts_ms` is an END-time label (prereg 6.1): the bar
    labelled T holds `[T-60s, T)`. So the two boundaries are not symmetric in form
    even though they come from one rule - "the bar labelled t0 belongs to t0":

        pre  `ts_ms <= t0`  the t0 bar is complete at t0, so it is observable and
                            belongs to the signal (A2 1). This is the amended cutoff.
        post `ts_ms >  t0`  the t0 bar's CONTENT precedes t0, so it is not outcome.
                            Keeping it here would have let the entry bar's own
                            high/low fill a target with a minute that happened
                            before entry, and would double-count it against `pre`.

    `t0_u` (the entry price) is the t0 bar's close and now comes from `pre`.
    No ranking frame is read here; A2 2's `snap_ms < t0` does not apply.

    Split out of `build_events` so the boundary can be pinned by an absolute-time
    test (`tests/test_cutoff_amendment_a2.py`) without the parquet fixtures -
    docs/36 4 showed a shared-convention synthesiser passes label defects through.
    """
    pre = pd.read_sql_query(
        "SELECT ts_ms, close_u, vol_qu FROM candles_1m WHERE symbol=? "
        "AND ts_ms>=? AND ts_ms<=? ORDER BY ts_ms", conn,
        params=(sym, t0 - PRE_MIN * MIN_MS, t0))
    post = pd.read_sql_query(
        "SELECT ts_ms, open_u, high_u, low_u, close_u, vol_qu FROM candles_1m "
        "WHERE symbol=? AND ts_ms>? AND ts_ms<=? ORDER BY ts_ms", conn,
        params=(sym, t0, t0 + HORIZON_MIN * MIN_MS))
    return pre, post


def build_events() -> tuple[pd.DataFrame, dict]:
    ev = pd.read_parquet(V2 / "ev_v3.parquet")
    ev = ev[ev["period"] == "train"].copy()
    assert (ev["market_date"] <= TRAIN_END).all(), "TRAIN CAP VIOLATED"
    assert (ev["period"] != "holdout").all(), "HOLDOUT LEAKED"
    ft = pd.read_parquet(V2 / "feats.parquet")

    conn = ro(BACKFILL)
    paths: dict[tuple[str, int], pd.DataFrame] = {}
    rows = []
    try:
        for _i, e in ev.iterrows():
            sym, t0 = e["symbol"], int(e["t0_ms"])
            # observability cutoff, contract C-7 amendment A2 (see `event_frames`):
            # pre `ts_ms <= t0`, post `ts_ms > t0`.
            pre, post = event_frames(conn, sym, t0)
            if post.empty:
                continue
            # the entry price is the t0 bar's close - that bar now lives in `pre`
            t0_row = pre[pre["ts_ms"] == t0]
            if t0_row.empty:
                continue
            t0_u = float(t0_row["close_u"].to_numpy()[0])
            paths[(sym, t0)] = post

            prints = ROT.print_frame(pre.assign(symbol=sym))
            slope = R.log_amount_rate_ratio(prints, recent_prints=10,
                                            baseline_prints=30)
            # pre-T0 price response over the last 30 minutes (observable at T0)
            p30 = pre[pre["ts_ms"] >= t0 - 30 * MIN_MS]
            ret30 = (float(p30["close_u"].to_numpy()[-1])
                     / float(p30["close_u"].to_numpy()[0]) - 1.0) if len(p30) >= 2 \
                else float("nan")
            # halt proxy: largest gap between consecutive prints before T0
            gaps = ROT.inter_print_gaps_min(prints["ts_ms"]) if len(prints) > 1 \
                else np.empty(0)
            max_gap = float(gaps.max()) if gaps.size else float("nan")
            # flow available AFTER T0 (capacity proxy - no absolute size assumed).
            # `post` starts strictly after t0, so this is the 30 bars covering
            # [t0, t0+30min) - the minutes you could actually have traded into.
            flow = post[post["ts_ms"] <= t0 + 30 * MIN_MS]
            flow_usd = [(int(c) * int(v)) / R.MICRO / R.MICRO
                        for c, v in zip(flow["close_u"], flow["vol_qu"]) if v > 0]
            rows.append({
                "symbol": sym, "market_date": e["market_date"], "t0_ms": t0,
                "session": e["session"], "kind": e["kind"], "t0_u": t0_u,
                "t0_price": t0_u / R.MICRO,
                "band": X.price_band(t0_u / R.MICRO),
                "fill": float(e["fill"]), "slope": slope, "ret30_pre": ret30,
                "max_pre_gap_min": max_gap,
                "flow_usd_per_min": (float(np.median(flow_usd)) if flow_usd
                                     else float("nan")),
                "n_prints_pre": int(len(prints)),
            })
    finally:
        conn.close()
    df = pd.DataFrame(rows)
    diag = {"train_events": int(len(ev)), "with_path": int(len(df)),
            "slope_defined": int(df["slope"].notna().sum()),
            "ret30_defined": int(df["ret30_pre"].notna().sum())}
    return df, {"diag": diag, "paths": paths}


# --------------------------------------------------------------------------- #
# 2. rule grid
# --------------------------------------------------------------------------- #
def universes(df: pd.DataFrame) -> dict:
    return {
        "U0_all": pd.Series(True, index=df.index),
        "U1_$2-5": df["band"] == "$2-5",
        "U2_>=$1": df["band"].isin(["$1-2", "$2-5", "$5-20", "$20+"]),
        "U3_$2-5_regular": (df["band"] == "$2-5") & (df["session"] == "regular"),
    }


def entries(df: pd.DataFrame) -> dict:
    s = df["slope"]
    med = float(s.median()) if s.notna().any() else float("nan")
    return {
        "E0_any": {"mask": pd.Series(True, index=df.index), "dip": None},
        "E1_slope_med": {"mask": s >= med, "dip": None},
        "E2_slope_0.5": {"mask": s >= 0.5, "dip": None},
        "E3_slope_hi_price_flat": {"mask": (s >= med) & (df["ret30_pre"] <= 0.05),
                                   "dip": None},
        "E4_no_day_session": {"mask": df["session"] != "day", "dip": None},
        "E5_halt_gap": {"mask": df["max_pre_gap_min"] >= 5, "dip": None},
        "E6_dip15": {"mask": pd.Series(True, index=df.index), "dip": 0.15},
        "E7_dip25": {"mask": pd.Series(True, index=df.index), "dip": 0.25},
        "E8_dip15_slope": {"mask": s >= med, "dip": 0.15},
        # pass 2 - stronger selectivity
        "E9_slope_q75": {"mask": s >= float(s.quantile(0.75)) if s.notna().any()
                         else pd.Series(False, index=df.index), "dip": None},
        "E10_dip10": {"mask": pd.Series(True, index=df.index), "dip": 0.10},
        "E11_halt_and_slope": {"mask": (df["max_pre_gap_min"] >= 5) & (s >= med),
                               "dip": None},
    }


EXITS = [
    R.ExitRule("X1_time5", horizon_min=390, time_min=5),
    R.ExitRule("X2_time15", horizon_min=390, time_min=15),
    R.ExitRule("X3_time30", horizon_min=390, time_min=30),
    R.ExitRule("X4_tgt10_h60", horizon_min=60, target=0.10),
    R.ExitRule("X5_tgt20_h60", horizon_min=60, target=0.20),
    R.ExitRule("X6_trail10_h60", horizon_min=60, trail=0.10),
    R.ExitRule("X7_trail20_h60", horizon_min=60, trail=0.20),
    R.ExitRule("X8_tgt15_stop10_h60", horizon_min=60, target=0.15, stop=0.10),
    R.ExitRule("X9_trail15_stop10_h60", horizon_min=60, trail=0.15, stop=0.10),
    R.ExitRule("X10_tgt10_stop7_h30", horizon_min=30, target=0.10, stop=0.07),
    R.ExitRule("X11_trail20_stop10_h390", horizon_min=390, trail=0.20, stop=0.10),
    # pass 2 - Q4 says the cycle is ~30 min, so test much faster exits
    R.ExitRule("X12_time1", horizon_min=390, time_min=1),
    R.ExitRule("X13_time2", horizon_min=390, time_min=2),
    R.ExitRule("X14_time3", horizon_min=390, time_min=3),
    R.ExitRule("X15_trail5_h30", horizon_min=30, trail=0.05),
    R.ExitRule("X16_trail7_h30", horizon_min=30, trail=0.07),
    R.ExitRule("X17_tgt5_h15", horizon_min=15, target=0.05),
    R.ExitRule("X18_tgt5_stop5_h30", horizon_min=30, target=0.05, stop=0.05),
    R.ExitRule("X19_tgt3_h10", horizon_min=10, target=0.03),
    R.ExitRule("X20_trail5_stop5_h15", horizon_min=15, trail=0.05, stop=0.05),
]


def evaluate(df, paths, umask, ename, espec, xr) -> R.RuleResult | None:
    sel = df[umask & espec["mask"].fillna(False)]
    if len(sel) == 0:
        return None
    nets = {m: [] for m in R.COST_MODELS}
    gross, holds, flows, reasons, skipped = [], [], [], {}, 0
    for _i, e in sel.iterrows():
        p = paths.get((e["symbol"], int(e["t0_ms"])))
        if p is None:
            skipped += 1
            continue
        entry_u, entry_ms = e["t0_u"], int(e["t0_ms"])
        if espec["dip"] is not None:
            d = R.find_dip_entry(p, e["t0_u"], entry_ms, dip=espec["dip"],
                                 window_min=60)
            if not (d["entry_u"] == d["entry_u"]):
                skipped += 1                     # dip never confirmed -> no trade
                continue
            entry_u, entry_ms = d["entry_u"], int(d["entry_ms"])
        r = R.simulate_exit(p, entry_u, entry_ms, xr)
        if not (r["gross"] == r["gross"]):
            skipped += 1
            continue
        gross.append(r["gross"])
        holds.append(r["exit_min"])
        flows.append(e["flow_usd_per_min"])
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        for m in R.COST_MODELS:
            nets[m].append(r["gross"] - R.cost_for(e["band"], m))
    if not gross:
        return None
    primary = nets["measured_cross"]
    lo, hi = R.bootstrap_ci_mean(primary)
    res = R.RuleResult(
        name=f"{ename}|{xr.name}", universe="", entry=ename, exit_rule=xr.name,
        n=len(primary), n_skipped=skipped,
        mean=float(np.mean(primary)), median=float(np.median(primary)),
        ci_low=lo, ci_high=hi,
        win_rate=float(np.mean([1.0 if x > 0 else 0.0 for x in primary])),
        flow_usd_per_min=float(np.nanmedian(flows)) if flows else float("nan"),
        hold_min=float(np.median(holds)), exit_reasons=reasons)
    res.extra = {  # type: ignore[attr-defined]
        "gross_mean": float(np.mean(gross)),
        "net_mid_mean": float(np.mean(nets["measured_mid"])),
        "net_mid_ci_low": R.bootstrap_ci_mean(nets["measured_mid"])[0],
        "net_frozen_mean": float(np.mean(nets["frozen_1pct"])),
        "net_frozen_ci_low": R.bootstrap_ci_mean(nets["frozen_1pct"])[0],
        "bonferroni_ci_low": R.bonferroni_ci_mean(primary)[0],
    }
    return res


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    df, aux = build_events()
    paths, diag = aux["paths"], aux["diag"]
    print(f"train events: {diag}")
    print(f"bands: {df['band'].value_counts().to_dict()}")
    print(f"slope: median {df['slope'].median():.3f} "
          f"defined {df['slope'].notna().mean():.3f}")

    # docs/17 obligation: the slope signal must not be a density proxy
    dens = ROT.density_independence(df["slope"].notna().astype(float), df["fill"])
    print(f"\nslope DEFINEDNESS vs fill: rates="
          f"{[round(x, 3) for x in dens['quantile_rates']]} "
          f"rho={dens['spearman_rho']:+.3f} passed={dens['passed']}")
    sub = df[df["slope"].notna()]
    fired = (sub["slope"] >= float(df["slope"].median())).astype(float)
    dens2 = ROT.density_independence(fired, sub["fill"])
    print(f"slope FIRING (given defined) vs fill: rates="
          f"{[round(x, 3) for x in dens2['quantile_rates']]} "
          f"rho={dens2['spearman_rho']:+.3f} passed={dens2['passed']} "
          f"-> {'PASS' if dens2['passed'] else 'FAIL'}")

    U, E = universes(df), entries(df)
    results = []
    for (uname, umask), (ename, espec), xr in product(U.items(), E.items(), EXITS):
        r = evaluate(df, paths, umask, ename, espec, xr)
        if r is None:
            continue
        r.universe = uname
        r.name = f"{uname}|{ename}|{xr.name}"
        results.append(r)
    print(f"\nevaluated {len(results)} rule combinations "
          f"({len(U)} universes x {len(E)} entries x {len(EXITS)} exits)")

    rows = []
    for r in results:
        rows.append({"rule": r.name, "universe": r.universe, "entry": r.entry,
                     "exit": r.exit_rule, "n": r.n, "skipped": r.n_skipped,
                     "eligible": r.eligible, "mean_net": r.mean,
                     "median_net": r.median, "ci_low": r.ci_low,
                     "ci_high": r.ci_high, "win_rate": r.win_rate,
                     "hold_min": r.hold_min,
                     "flow_usd_per_min": r.flow_usd_per_min,
                     **{k: v for k, v in r.extra.items()},  # type: ignore
                     "exit_reasons": json.dumps(r.exit_reasons)})
    tab = pd.DataFrame(rows).sort_values("ci_low", ascending=False)
    tab.to_csv(OUT / "all_rules.csv", index=False)
    elig = tab[tab["eligible"]]
    print(f"eligible (train n>=30): {len(elig)} of {len(tab)}")
    print(f"positive CI lower bound: {int((elig['ci_low'] > 0).sum())}")

    top = R.rank_rules(results, top_k=5)
    print("\n===== MECHANICAL TOP 5 (by bootstrap 95% CI lower bound, n>=30) =====")
    for i, r in enumerate(top, 1):
        print(f"{i}. {r.name}\n   n={r.n} ci_low={r.ci_low:+.4f} "
              f"mean={r.mean:+.4f} med={r.median:+.4f} win={r.win_rate:.3f} "
              f"hold={r.hold_min:.0f}m flow=${r.flow_usd_per_min:,.0f}/min "
              f"bonferroni_lo={r.extra['bonferroni_ci_low']:+.4f}\n"  # type: ignore
              f"   exits={r.exit_reasons}")

    print("\n===== best 15 by CI lower bound (all eligible) =====")
    cols = ["rule", "n", "ci_low", "mean_net", "median_net", "win_rate",
            "hold_min", "flow_usd_per_min", "net_mid_ci_low", "net_frozen_ci_low"]
    print(elig[cols].head(15).to_string(index=False,
                                        float_format=lambda x: f"{x:.4f}"))

    print("\n===== exit-rule comparison (median CI lower bound across all combos) =====")
    ex = tab[tab["eligible"]].groupby("exit").agg(
        n_combos=("rule", "size"), med_ci_low=("ci_low", "median"),
        best_ci_low=("ci_low", "max"), med_hold=("hold_min", "median")).sort_values(
        "med_ci_low", ascending=False)
    print(ex.to_string(float_format=lambda x: f"{x:.4f}"))

    print("\n===== universe comparison =====")
    un = tab[tab["eligible"]].groupby("universe").agg(
        n_combos=("rule", "size"), med_ci_low=("ci_low", "median"),
        best_ci_low=("ci_low", "max"),
        med_flow=("flow_usd_per_min", "median")).sort_values("med_ci_low",
                                                            ascending=False)
    print(un.to_string(float_format=lambda x: f"{x:.4f}"))

    print("\n===== entry comparison =====")
    en = tab[tab["eligible"]].groupby("entry").agg(
        n_combos=("rule", "size"), med_ci_low=("ci_low", "median"),
        best_ci_low=("ci_low", "max"), med_n=("n", "median")).sort_values(
        "med_ci_low", ascending=False)
    print(en.to_string(float_format=lambda x: f"{x:.4f}"))

    out = {"diag": diag, "density_slope_defined": dens, "density_slope_firing": dens2,
           "n_combos": len(results), "n_eligible": int(len(elig)),
           "n_positive_ci_low": int((elig["ci_low"] > 0).sum()),
           "top5": [{"rule": r.name, "n": r.n, "ci_low": r.ci_low, "mean": r.mean,
                     "median": r.median, "win_rate": r.win_rate,
                     "hold_min": r.hold_min,
                     "flow_usd_per_min": r.flow_usd_per_min,
                     "exit_reasons": r.exit_reasons,
                     **r.extra}  # type: ignore
                    for r in top]}
    (OUT / "search.json").write_text(json.dumps(out, indent=1, default=str),
                                     encoding="utf-8")
    df.to_parquet(OUT / "train_attrs.parquet")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Revision 8 B/C re-ladder runner (``docs/74``).

The decision rule was committed in ``G2G3-PREREG`` revision 8 before this
module existed.  Route B is the primary, verdict-eligible path: it keeps the
revision-3 paired mean-difference estimator and replaces the ``cvol5`` band
with exact ``cnbar5`` matching.  Route C is a falsification-only outcome
regression over all zero-preserving design rows.  C may kill B, but no C value
can ever be returned as the number to quote.

The command deliberately runs one route at a time so the data execution order
is observable::

    python -m tossmon.analysis.measure.bc_reladder DB --route B --out DIR
    python -m tossmon.analysis.measure.bc_reladder DB --route C --out DIR
    python -m tossmon.analysis.measure.bc_reladder --route decision --out DIR

Only exploration era B can be opened.  ``candle_ruler.count`` owns the window
and reaches ``ranking_forward_path.arm_window``; all database connections go
through read-only helpers.  The console is ASCII only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from tossmon.analysis import hires_events as HE
from tossmon.analysis.measure import candle_ladder as CL
from tossmon.analysis.measure import candle_ruler as CR
from tossmon.analysis.measure import e2_design_funnel as EF
from tossmon.analysis.measure import ranking_forward_path as RFP
from tossmon.analysis.measure import ruler_bias as RB

OUT_DIR = RFP.OUT_DIR
ERA = "B"
ROUTE_B = "B"
ROUTE_C = "C"
STRATUM = "first_in_regular"
DECLARED = EF.DECLARED

SELF_GAP_S = CR.CANDLE_SELF_GAP_S
RV_TOL = CR.CANDLE_RV_TOL
DRAWS = RFP.MATCH_DRAWS
SEED = RFP.SEED
BOOTSTRAP_N = RFP.BOOTSTRAP_N
MIN_SESSION_CLUSTERS = RFP.MIN_SESSION_CLUSTERS
N_COMPARISONS = 1

# Revision 8 keeps this line and labels it honestly: it is not remeasured on
# the 57/79 populations and is not known to be conservative.
PASS_LINE = CL.FIRST_ENTRY_BIAS
PASS_LINE_NOTE = (
    "+0.91pp: extrapolated from 18 first_in_regular tape-minute pairs; "
    "bias of the 22 newcomers is unknown; not claimed conservative"
)

BLIND_SPOT = (
    "The placebo region consists of candle minutes without tape. Differential "
    "candle-vs-tape ruler bias is therefore unobservable there in principle "
    "(docs/70 s8); this limitation applies to both B and C."
)


def _dist(values) -> dict:
    a = np.asarray(values, dtype="float64")
    a = a[np.isfinite(a)]
    if a.size == 0:
        return {"n": 0, "min": None, "p25": None, "median": None,
                "mean": None, "p75": None, "max": None}
    return {
        "n": int(a.size), "min": float(a.min()),
        "p25": float(np.percentile(a, 25)), "median": float(np.median(a)),
        "mean": float(a.mean()), "p75": float(np.percentile(a, 75)),
        "max": float(a.max()),
    }


def _counts(values) -> dict:
    return {str(k): int(v) for k, v in
            pd.Series(values, dtype="object").value_counts(sort=False, dropna=False).items()}


def _guard_era(era: str) -> None:
    if era != ERA:
        raise ValueError("B/C re-ladder is exploration era B only")


def _guard_sessions(counted: dict) -> None:
    used = set(counted["sessions_used"])
    allowed = set(RFP.EXPLORATION_B_SESSIONS)
    if not used <= allowed:
        raise ValueError(
            "sessions outside exploration era B reached the re-ladder: "
            f"{sorted(used - allowed)}")
    if int(counted["until_ms"]) >= HE.iso_ms(RFP.CONFIRMATION_FLOOR_UTC):
        raise ValueError("re-ladder window reached the confirmation floor")


def _event_forward(cand: dict | None, t0_ms: int) -> dict:
    if cand is None:
        return {"n_bars": 0, "max_ret": np.nan, "entry_u": np.nan,
                "entry_lag_s": np.nan}
    out = RB.bar_forward_at(cand, np.asarray([int(t0_ms)], dtype="int64"))
    return {"n_bars": int(out["n_bars"][0]),
            "max_ret": float(out["max_ret"][0]),
            "entry_u": float(out["entry_u"][0]),
            "entry_lag_s": float(out["entry_lag_s"][0])}


# --------------------------------------------------------------------------- #
# Route B: primary, same paired estimator as revision 3
# --------------------------------------------------------------------------- #
def _route_b_candidate_mask(key_rv, key_nbar, tau_ms, t0_ms: int,
                            event_rv: float, event_nbar: int) -> dict:
    """Frozen B funnel: gap -> key -> crv5 band -> exact cnbar5.

    There is intentionally no volume argument.  A source mutation that routes
    ``cvol5`` into this function changes its interface as well as its behavior.
    """
    tau = np.asarray(tau_ms, dtype="int64")
    rv = np.asarray(key_rv, dtype="float64")
    nbar = np.asarray(key_nbar, dtype="int64")
    gap = np.abs(tau - int(t0_ms)) > int(SELF_GAP_S) * RFP.SEC_MS
    key = gap & np.isfinite(rv) & (rv > 0) & (nbar > 0)
    event_key_ok = bool(np.isfinite(event_rv) and event_rv > 0 and int(event_nbar) > 0)
    if event_key_ok:
        rv_band = key & CR.in_band(rv, float(event_rv), RV_TOL)
        exact = rv_band & (nbar == int(event_nbar))
    else:
        rv_band = np.zeros(tau.size, dtype=bool)
        exact = np.zeros(tau.size, dtype=bool)
    return {"gap": gap, "key": key, "rv": rv_band, "all": exact,
            "event_key_ok": event_key_ok}


def _b_first_empty(row: dict) -> str:
    if not bool(row["event_key_ok"]):
        return "event_key_missing"
    if int(row["n_cand_gap"]) == 0:
        return "outside_gap_empty"
    if int(row["n_cand_key"]) == 0:
        return "candidate_key_empty"
    if int(row["n_cand_rv"]) == 0:
        return "crv5_band_empty"
    if int(row["n_cand"]) == 0:
        return "cnbar5_exact_empty"
    return "paired"


def run_b(db: Path, *, era: str = ERA, progress: bool = False) -> dict:
    """Run primary route B only.  No route-C function is called from here."""
    _guard_era(era)
    counted = CR.count(db, era=era, progress=progress)
    _guard_sessions(counted)
    tags = counted["tags"]
    conn = HE.open_ro(db)
    event_rows: list[dict] = []
    draw_parts: list[pd.DataFrame] = []
    try:
        for session_meta in counted["sessions"]:
            session = session_meta["session"]
            tagged = tags[tags.session == session] if len(tags) else tags
            first = tagged[CR.strata_masks(tagged)[STRATUM]] if len(tagged) else tagged
            fwd = RB.load_candle_ohlc(
                conn, session_meta["open_ms"], session_meta["close_ms"])
            keys_by_symbol = CR.load_candles(
                conn, session_meta["open_ms"], session_meta["close_ms"])
            rng = np.random.default_rng(SEED)
            for _, event in first.iterrows():
                symbol = str(event.symbol)
                t0_ms = int(event.t0_ms)
                event_forward = _event_forward(fwd.get(symbol), t0_ms)
                event_key_ok = bool(
                    np.isfinite(float(event.crv5)) and float(event.crv5) > 0
                    and int(event.cnbar5) > 0)
                row = {
                    "session": session, "symbol": symbol, "t0_ms": t0_ms,
                    "crv5": float(event.crv5), "cnbar5": int(event.cnbar5),
                    "event_key_ok": event_key_ok,
                    "bar_n_bars": event_forward["n_bars"],
                    "bar_max_ret": event_forward["max_ret"],
                    "bar_entry_u": event_forward["entry_u"],
                    "bar_entry_lag_s": event_forward["entry_lag_s"],
                    "n_cand_gap": 0, "n_cand_key": 0, "n_cand_rv": 0,
                    "n_cand": 0, "n_draws": 0, "paired": False,
                }
                candle_keys = keys_by_symbol.get(symbol)
                candle_fwd = fwd.get(symbol)
                if candle_keys is not None and candle_fwd is not None and candle_keys["ts"].size:
                    tau_all = CL.placebo_taus(candle_keys["ts"])
                    keys = CR.candle_keys_at(candle_keys, tau_all)
                    masks = _route_b_candidate_mask(
                        keys[CR.KEY_RV], keys[CR.KEY_NBAR], tau_all, t0_ms,
                        float(event.crv5), int(event.cnbar5))
                    row["event_key_ok"] = bool(masks["event_key_ok"])
                    row["n_cand_gap"] = int(masks["gap"].sum())
                    row["n_cand_key"] = int(masks["key"].sum())
                    row["n_cand_rv"] = int(masks["rv"].sum())
                    row["n_cand"] = int(masks["all"].sum())
                    picks = CL.draw_from(rng, np.flatnonzero(masks["all"]))
                    if picks.size:
                        tau_pick = tau_all[picks]
                        placebo = RB.bar_forward_at(candle_fwd, tau_pick)
                        row["n_draws"] = int(picks.size)
                        row["paired"] = True
                        draw_parts.append(pd.DataFrame({
                            "session": session, "symbol": symbol,
                            "event_t0_ms": t0_ms, "tau_ms": tau_pick,
                            "crv5": np.asarray(keys[CR.KEY_RV], dtype="float64")[picks],
                            "cnbar5": np.asarray(keys[CR.KEY_NBAR], dtype="int64")[picks],
                            "bar_n_bars": placebo["n_bars"],
                            "bar_max_ret": placebo["max_ret"],
                            "bar_entry_u": placebo["entry_u"],
                            "bar_entry_lag_s": placebo["entry_lag_s"],
                        }))
                row["first_empty"] = _b_first_empty(row)
                event_rows.append(row)
            if progress:
                current = [r for r in event_rows if r["session"] == session]
                paired = sum(bool(r["paired"]) for r in current)
                print(f"  B {session} first={len(current):>3} paired={paired:>3}", flush=True)
    finally:
        conn.close()

    events = pd.DataFrame(event_rows)
    draws = pd.concat(draw_parts, ignore_index=True) if draw_parts else pd.DataFrame()
    real_by_session: dict[str, np.ndarray] = {}
    placebo_by_session: dict[str, np.ndarray] = {}
    if len(events):
        for session, sub in events[events.paired].groupby("session"):
            real_by_session[str(session)] = sub.bar_max_ret.to_numpy(dtype="float64")
    if len(draws):
        for session, sub in draws.groupby("session"):
            placebo_by_session[str(session)] = sub.bar_max_ret.to_numpy(dtype="float64")
    stat = RFP.cluster_bootstrap_diff(
        real_by_session, placebo_by_session, n_comparisons=N_COMPARISONS)
    return {
        "db": str(db), "era": era,
        "since_ms": counted["since_ms"], "since_utc": counted["since_utc"],
        "until_ms": counted["until_ms"], "until_utc": counted["until_utc"],
        "db_max_snap_utc": counted["db_max_snap_utc"],
        "sessions_used": counted["sessions_used"], "events": events,
        "draws": draws, "stat": stat,
    }


def build_b_report(result: dict) -> dict:
    """Build the route-B report without consulting any route-C result."""
    events = result["events"]
    draws = result["draws"]
    stat = dict(result["stat"])
    paired = events[events.paired] if len(events) else events
    real_dist = _dist(paired.bar_max_ret if len(paired) else [])
    placebo_dist = _dist(draws.bar_max_ret if len(draws) else [])
    median_diff = (None if real_dist["median"] is None or placebo_dist["median"] is None
                   else real_dist["median"] - placebo_dist["median"])
    ci = stat.get("ci95")
    clears = bool(ci is not None and float(ci[0]) > PASS_LINE)
    per_session = []
    for session in result["sessions_used"]:
        ev = events[events.session == session] if len(events) else events
        pe = ev[ev.paired] if len(ev) else ev
        dr = draws[draws.session == session] if len(draws) else draws
        rv = pe.bar_max_ret.to_numpy(dtype="float64") if len(pe) else np.zeros(0)
        pv = dr.bar_max_ret.to_numpy(dtype="float64") if len(dr) else np.zeros(0)
        rv, pv = rv[np.isfinite(rv)], pv[np.isfinite(pv)]
        per_session.append({
            "session": session, "n_events": int(len(ev)),
            "n_paired": int(len(pe)), "n_real_effective": int(rv.size),
            "n_draws": int(len(dr)), "n_placebo_effective": int(pv.size),
            "mean_diff": float(rv.mean() - pv.mean()) if rv.size and pv.size else None,
        })
    n_events = int(len(events))
    n_paired = int(events.paired.sum()) if len(events) else 0
    n_real_effective = int(np.isfinite(paired.bar_max_ret).sum()) if len(paired) else 0
    n_draw_effective = int(np.isfinite(draws.bar_max_ret).sum()) if len(draws) else 0
    return {
        "labels": list(RFP.LABELS), "conditions": HE.MEASUREMENT_CONDITIONS,
        "db": result["db"],
        "window": {"since_utc": result["since_utc"], "until_utc": result["until_utc"],
                   "db_max_snap_utc": result["db_max_snap_utc"]},
        "arm": {"name": "exploration", "exploration_era": result["era"],
                "sessions_used": result["sessions_used"],
                "sessions_allowed": list(RFP.EXPLORATION_B_SESSIONS),
                "confirmation_floor_utc": RFP.CONFIRMATION_FLOOR_UTC,
                "window_rule": "candle_ruler.count -> ranking_forward_path.arm_window"},
        "holdout": {"window": ["2026-05-01", "2026-07-29"]},
        "design": {
            "route": ROUTE_B, "role": "PRIMARY - the only verdict-eligible route",
            "estimator": "revision-3 paired mean difference: mean(real paired events) "
                         "minus mean(placebo draws)",
            "matching": f"crv5 +/-{int(RV_TOL * 100)}% AND exact cnbar5; no cvol5",
            "self_gap_s_strict": SELF_GAP_S, "draws_per_event": DRAWS,
            "seed": SEED, "n_comparisons": N_COMPARISONS,
            "pass_line": PASS_LINE, "pass_line_note": PASS_LINE_NOTE,
            "third_exposure": "exploration B forward outcomes exposed for the third time: "
                              "ruler calibration, original ladder, this re-ladder",
        },
        "funnel": {
            "n_design_events": n_events,
            "n_paired": n_paired,
            "n_excluded_unpaired": n_events - n_paired,
            "excluded_unpaired_share": ((n_events - n_paired) / n_events if n_events else None),
            "first_empty": _counts(events.first_empty) if len(events) else {},
            "n_real_effective": n_real_effective,
            "n_real_missing_forward": n_paired - n_real_effective,
            "n_draws": int(len(draws)), "n_placebo_effective": n_draw_effective,
            "n_placebo_missing_forward": int(len(draws)) - n_draw_effective,
            "candidate_counts": ({name: _dist(events[col]) for name, col in
                                  (("outside_gap", "n_cand_gap"),
                                   ("key", "n_cand_key"),
                                   ("rv_band", "n_cand_rv"),
                                   ("rv_and_exact_nbar", "n_cand"))}
                                 if len(events) else {}),
        },
        "headline": {
            **stat, "real": real_dist, "placebo": placebo_dist,
            "median_diff": median_diff, "clears_pass_line": clears,
            "ci_low_minus_pass_line": (None if ci is None else float(ci[0]) - PASS_LINE),
        },
        "selection": {
            "paired_crv5": _dist(paired.crv5 if len(paired) else []),
            "excluded_crv5": _dist(events.loc[~events.paired, "crv5"] if len(events) else []),
            "paired_cnbar5": _dist(paired.cnbar5 if len(paired) else []),
            "excluded_cnbar5": _dist(events.loc[~events.paired, "cnbar5"] if len(events) else []),
        },
        "per_session": per_session,
        "blind_spot": BLIND_SPOT,
    }


# --------------------------------------------------------------------------- #
# Route C: falsification-only control outcome regression
# --------------------------------------------------------------------------- #
def _route_c_design_mask(key_rv, key_vol, key_nbar, tau_ms, t0_ms: int) -> np.ndarray:
    tau = np.asarray(tau_ms, dtype="int64")
    rv = np.asarray(key_rv, dtype="float64")
    vol = np.asarray(key_vol, dtype="float64")
    nbar = np.asarray(key_nbar, dtype="int64")
    gap = np.abs(tau - int(t0_ms)) > int(SELF_GAP_S) * RFP.SEC_MS
    return (gap & np.isfinite(rv) & (rv >= 0)
            & np.isfinite(vol) & (vol >= 0) & (nbar >= 0))


def _features(rv, vol) -> np.ndarray:
    rv = np.asarray(rv, dtype="float64")
    vol = np.asarray(vol, dtype="float64")
    return np.column_stack([np.log1p(rv), np.log1p(vol)])


def _c_group_summaries(events: pd.DataFrame, controls: pd.DataFrame) -> list[dict]:
    groups: list[dict] = []
    if events.empty or controls.empty:
        return groups
    valid_events = events[
        events.design_complete
        & np.isfinite(events.bar_max_ret.to_numpy(dtype="float64"))]
    for _, event in valid_events.iterrows():
        control = controls[controls.group_id == event.group_id]
        control = control[np.isfinite(control.bar_max_ret.to_numpy(dtype="float64"))]
        if control.empty:
            continue
        x = control[["x1", "x2"]].to_numpy(dtype="float64")
        y = control.bar_max_ret.to_numpy(dtype="float64")
        x_bar = x.mean(axis=0)
        y_bar = float(y.mean())
        xc = x - x_bar
        yc = y - y_bar
        n = int(len(control))
        groups.append({
            "group_id": str(event.group_id), "session": str(event.session),
            "symbol": str(event.symbol), "n_controls": n,
            "x_bar": x_bar, "y_bar": y_bar,
            # Division by n makes every group's control weights sum to one.
            "sxx": (xc.T @ xc) / float(n),
            "sxy": (xc.T @ yc) / float(n),
            "event_x": np.asarray([float(event.x1), float(event.x2)]),
            "event_y": float(event.bar_max_ret),
            "within_local_range": bool(event.within_local_range),
        })
    return groups


def _estimate_c_groups(groups: list[dict], session_weights: dict[str, int] | None = None,
                       *, include_effects: bool = False) -> dict:
    active = []
    for group in groups:
        weight = 1 if session_weights is None else int(session_weights.get(group["session"], 0))
        if weight > 0:
            active.append((group, weight))
    if not active:
        return {"estimate": None, "beta": [None, None], "rank": 0,
                "n_events": 0, "effects": pd.DataFrame() if include_effects else None}
    sxx = sum((weight * group["sxx"] for group, weight in active),
              np.zeros((2, 2), dtype="float64"))
    sxy = sum((weight * group["sxy"] for group, weight in active),
              np.zeros(2, dtype="float64"))
    beta, _resid, rank, _sing = np.linalg.lstsq(sxx, sxy, rcond=None)
    effect_values = []
    effect_weights = []
    effect_rows = []
    for group, weight in active:
        prediction = group["y_bar"] + float((group["event_x"] - group["x_bar"]) @ beta)
        effect = float(group["event_y"] - prediction)
        effect_values.append(effect)
        effect_weights.append(weight)
        if include_effects:
            effect_rows.append({
                "group_id": group["group_id"], "session": group["session"],
                "symbol": group["symbol"], "n_controls": group["n_controls"],
                "predicted_placebo": prediction, "event_max_ret": group["event_y"],
                "effect": effect, "within_local_range": group["within_local_range"],
            })
    estimate = float(np.average(effect_values, weights=effect_weights))
    return {"estimate": estimate, "beta": beta.tolist(), "rank": int(rank),
            "n_events": int(sum(effect_weights)),
            "effects": pd.DataFrame(effect_rows) if include_effects else None}


def _cluster_bootstrap_c(events: pd.DataFrame, controls: pd.DataFrame, *,
                         n_boot: int = BOOTSTRAP_N, seed: int = SEED,
                         min_clusters: int = MIN_SESSION_CLUSTERS) -> dict:
    groups = _c_group_summaries(events, controls)
    point = _estimate_c_groups(groups, include_effects=True)
    effects = point["effects"]
    sessions = sorted(set(effects.session)) if len(effects) else []
    out = {
        "mean_diff": point["estimate"], "beta": point["beta"],
        "design_rank": point["rank"], "n_events": int(len(effects)),
        "n_clusters": int(len(sessions)), "clusters": sessions,
        "ci95": None, "ci_bonferroni": None, "ci_withheld": None,
        "effects": effects,
    }
    if len(sessions) < int(min_clusters):
        out["ci_withheld"] = (
            f"trading-day clusters {len(sessions)} < {min_clusters}: no pooled CI is produced")
        return out
    rng = np.random.default_rng(seed)
    picks = rng.integers(0, len(sessions), size=(int(n_boot), len(sessions)))
    boot = np.full(int(n_boot), np.nan, dtype="float64")
    for i, pick in enumerate(picks):
        counts = np.bincount(pick, minlength=len(sessions))
        weights = {session: int(counts[j]) for j, session in enumerate(sessions)}
        boot[i] = _estimate_c_groups(groups, weights)["estimate"]
    boot = boot[np.isfinite(boot)]
    if boot.size != int(n_boot):
        raise ValueError(f"C bootstrap produced {boot.size}/{n_boot} finite estimates")
    lo, hi = np.quantile(boot, [0.025, 0.975])
    out["ci95"] = [float(lo), float(hi)]
    out["ci_bonferroni"] = [float(lo), float(hi)]
    return out


def run_c(db: Path, *, era: str = ERA, progress: bool = False) -> dict:
    """Run falsification-only route C over the complete zero-preserving design."""
    _guard_era(era)
    counted = CR.count(db, era=era, progress=progress)
    _guard_sessions(counted)
    tags = counted["tags"]
    conn = HE.open_ro(db)
    event_rows: list[dict] = []
    control_parts: list[pd.DataFrame] = []
    try:
        for session_meta in counted["sessions"]:
            session = session_meta["session"]
            tagged = tags[tags.session == session] if len(tags) else tags
            first = tagged[CR.strata_masks(tagged)[STRATUM]] if len(tagged) else tagged
            fwd = RB.load_candle_ohlc(
                conn, session_meta["open_ms"], session_meta["close_ms"])
            keys_by_symbol = CR.load_candles(
                conn, session_meta["open_ms"], session_meta["close_ms"])
            for _, event in first.iterrows():
                symbol = str(event.symbol)
                t0_ms = int(event.t0_ms)
                group_id = f"{session}|{symbol}|{t0_ms}"
                event_rv = float(event.crv5)
                event_vol = float(event.cvol5)
                design_complete = bool(
                    np.isfinite(event_rv) and event_rv >= 0
                    and np.isfinite(event_vol) and event_vol >= 0)
                event_x = (_features([event_rv], [event_vol])[0]
                           if design_complete else np.asarray([np.nan, np.nan]))
                event_forward = _event_forward(fwd.get(symbol), t0_ms)
                n_design = 0
                n_outcome = 0
                within = False
                candle_keys = keys_by_symbol.get(symbol)
                candle_fwd = fwd.get(symbol)
                if candle_keys is not None and candle_keys["ts"].size:
                    tau_all = CL.placebo_taus(candle_keys["ts"])
                    keys = CR.candle_keys_at(candle_keys, tau_all)
                    design = _route_c_design_mask(
                        keys[CR.KEY_RV], keys[CR.KEY_VOL], keys[CR.KEY_NBAR],
                        tau_all, t0_ms)
                    idx = np.flatnonzero(design)
                    n_design = int(idx.size)
                    rv = np.asarray(keys[CR.KEY_RV], dtype="float64")[idx]
                    vol = np.asarray(keys[CR.KEY_VOL], dtype="float64")[idx]
                    if design_complete and idx.size:
                        within = bool(rv.min() <= event_rv <= rv.max()
                                      and vol.min() <= event_vol <= vol.max())
                    if idx.size:
                        if candle_fwd is None:
                            outcome = np.full(idx.size, np.nan)
                            n_bars = np.zeros(idx.size, dtype="int64")
                        else:
                            forward = RB.bar_forward_at(candle_fwd, tau_all[idx])
                            outcome = np.asarray(forward["max_ret"], dtype="float64")
                            n_bars = np.asarray(forward["n_bars"], dtype="int64")
                        x = _features(rv, vol)
                        n_outcome = int(np.isfinite(outcome).sum())
                        control_parts.append(pd.DataFrame({
                            "group_id": group_id, "session": session,
                            "symbol": symbol, "event_t0_ms": t0_ms,
                            "tau_ms": tau_all[idx], "crv5": rv, "cvol5": vol,
                            "x1": x[:, 0], "x2": x[:, 1],
                            "bar_n_bars": n_bars, "bar_max_ret": outcome,
                        }))
                event_rows.append({
                    "group_id": group_id, "session": session, "symbol": symbol,
                    "t0_ms": t0_ms, "crv5": event_rv, "cvol5": event_vol,
                    "x1": float(event_x[0]), "x2": float(event_x[1]),
                    "design_complete": design_complete,
                    "bar_n_bars": event_forward["n_bars"],
                    "bar_max_ret": event_forward["max_ret"],
                    "bar_entry_u": event_forward["entry_u"],
                    "bar_entry_lag_s": event_forward["entry_lag_s"],
                    "n_controls_design": n_design,
                    "n_controls_outcome": n_outcome,
                    "within_local_range": within,
                })
            if progress:
                current = [r for r in event_rows if r["session"] == session]
                effective = sum(np.isfinite(float(r["bar_max_ret"]))
                                and r["n_controls_outcome"] > 0 for r in current)
                print(f"  C {session} first={len(current):>3} effective={effective:>3}",
                      flush=True)
    finally:
        conn.close()
    events = pd.DataFrame(event_rows)
    controls = pd.concat(control_parts, ignore_index=True) if control_parts else pd.DataFrame()
    stat = _cluster_bootstrap_c(events, controls)
    return {
        "db": str(db), "era": era,
        "since_ms": counted["since_ms"], "since_utc": counted["since_utc"],
        "until_ms": counted["until_ms"], "until_utc": counted["until_utc"],
        "db_max_snap_utc": counted["db_max_snap_utc"],
        "sessions_used": counted["sessions_used"], "events": events,
        "controls": controls, "stat": stat,
    }


def build_c_report(result: dict) -> dict:
    """Build the route-C falsification report.  It is never verdict-eligible."""
    events = result["events"]
    controls = result["controls"]
    stat = result["stat"]
    effects = stat["effects"]
    ci = stat.get("ci95")
    clears = bool(ci is not None and float(ci[0]) > PASS_LINE)
    per_session = []
    for session in result["sessions_used"]:
        ev = events[events.session == session] if len(events) else events
        eff = effects[effects.session == session] if len(effects) else effects
        per_session.append({
            "session": session, "n_events": int(len(ev)),
            "n_effective": int(len(eff)),
            "mean_diff": float(eff.effect.mean()) if len(eff) else None,
        })
    design_complete = events[events.design_complete] if len(events) else events
    n_design = int(len(design_complete))
    n_outside = int((~design_complete.within_local_range).sum()) if len(design_complete) else 0
    effective_groups = set(effects.group_id) if len(effects) else set()
    n_event_outcome_missing = int((~np.isfinite(
        design_complete.bar_max_ret.to_numpy(dtype="float64"))).sum()) if len(design_complete) else 0
    n_no_control_outcome = int(sum(
        str(row.group_id) not in effective_groups
        and np.isfinite(float(row.bar_max_ret))
        for _, row in design_complete.iterrows()))
    return {
        "labels": list(RFP.LABELS), "conditions": HE.MEASUREMENT_CONDITIONS,
        "db": result["db"],
        "window": {"since_utc": result["since_utc"], "until_utc": result["until_utc"],
                   "db_max_snap_utc": result["db_max_snap_utc"]},
        "arm": {"name": "exploration", "exploration_era": result["era"],
                "sessions_used": result["sessions_used"],
                "sessions_allowed": list(RFP.EXPLORATION_B_SESSIONS),
                "confirmation_floor_utc": RFP.CONFIRMATION_FLOOR_UTC,
                "window_rule": "candle_ruler.count -> ranking_forward_path.arm_window"},
        "holdout": {"window": ["2026-05-01", "2026-07-29"]},
        "design": {
            "route": ROUTE_C, "role": "FALSIFICATION ONLY - never verdict-eligible",
            "estimator": "control-only outcome regression with symbol-session/event fixed "
                         "effects; equal total control weight per event; mean event residual",
            "formula": "y = alpha_g + beta1*log1p(crv5) + beta2*log1p(cvol5)",
            "zeros_preserved": True, "bands": None, "draws": None,
            "self_gap_s_strict": SELF_GAP_S, "seed": SEED,
            "bootstrap": "trading-day cluster percentile; model refit in every draw",
            "pass_line": PASS_LINE, "pass_line_note": PASS_LINE_NOTE,
            "cannot_be_promoted": True,
            "third_exposure": "exploration B forward outcomes exposed for the third time: "
                              "ruler calibration, original ladder, this re-ladder",
        },
        "funnel": {
            "n_design_population": int(len(events)),
            "n_design_complete": n_design,
            "n_effective": int(len(effects)),
            "n_event_outcome_missing": n_event_outcome_missing,
            "n_no_finite_control_outcome": n_no_control_outcome,
            "n_controls_design": int(len(controls)),
            "n_controls_effective": int(np.isfinite(
                controls.bar_max_ret.to_numpy(dtype="float64")).sum()) if len(controls) else 0,
            "n_outside_local_rectangular_support": n_outside,
            "outside_local_rectangular_support_share": n_outside / n_design if n_design else None,
            "controls_per_event_design": _dist(
                design_complete.n_controls_design if len(design_complete) else []),
            "controls_per_event_outcome": _dist(
                design_complete.n_controls_outcome if len(design_complete) else []),
        },
        "headline": {
            "mean_diff": stat["mean_diff"], "ci95": ci,
            "ci_bonferroni": stat.get("ci_bonferroni"),
            "n_events": stat["n_events"], "n_clusters": stat["n_clusters"],
            "clusters": stat["clusters"], "beta": stat["beta"],
            "design_rank": stat["design_rank"],
            "ci_withheld": stat.get("ci_withheld"),
            "clears_pass_line_for_table_only": clears,
            "ci_low_minus_pass_line": (None if ci is None else float(ci[0]) - PASS_LINE),
        },
        "per_session": per_session,
        "blind_spot": BLIND_SPOT,
        "citation_rule": "C can falsify B but can never supply the quoted/verdict number; "
                         "promotion requires a new preregistration and a new sample",
    }


# --------------------------------------------------------------------------- #
# Frozen agreement and decision table
# --------------------------------------------------------------------------- #
def decision_rule(b_point, b_ci, c_point, c_ci, *, pass_line: float = PASS_LINE) -> dict:
    """Apply revision 8 exactly; a non-null quote can only equal B's estimate."""
    b = None if b_point is None else float(b_point)
    c = None if c_point is None else float(c_point)
    b_interval = None if b_ci is None else [float(b_ci[0]), float(b_ci[1])]
    c_interval = None if c_ci is None else [float(c_ci[0]), float(c_ci[1])]
    b_clears = bool(b_interval is not None and b_interval[0] > float(pass_line))
    c_clears = bool(c_interval is not None and c_interval[0] > float(pass_line))
    agrees = bool(
        b is not None and c is not None and b > 0 and c > 0
        and b_interval is not None and c_interval is not None
        and b_interval[0] <= c <= b_interval[1]
        and c_interval[0] <= b <= c_interval[1])
    c_smaller_or_negative = bool(
        c is not None and (c <= 0 or (b is not None and c < b and not agrees)))
    number_to_quote = None
    if b_clears and agrees:
        cell = "B_CLEARS_C_AGREES"
        conclusion = "strongest evidence; quote B only"
        number_to_quote = b
    elif b_clears and c_smaller_or_negative:
        cell = "B_CLEARS_C_KILLS"
        conclusion = "the 22 excluded events kill B; claim nothing"
    elif b_clears:
        cell = "B_CLEARS_C_DISAGREES"
        conclusion = "C does not agree; claim nothing and never substitute C"
    elif c_clears:
        cell = "B_MISSES_C_CLEARS_FINDING_ONLY"
        conclusion = "effect may live where B cannot see; finding only, new preregistration needed"
    else:
        cell = "BOTH_MISS_AXIS_CLOSED"
        conclusion = "both miss; close this axis"
    # Runtime defense as well as a mutation-tested behavioral contract.
    if number_to_quote is not None and number_to_quote != b:
        raise AssertionError("route C leaked into the route-B quote path")
    return {
        "cell": cell, "conclusion": conclusion,
        "b_clears": b_clears, "c_clears_for_table_only": c_clears,
        "agreement": agrees, "c_smaller_or_negative": c_smaller_or_negative,
        "pass_line": float(pass_line), "pass_line_note": PASS_LINE_NOTE,
        "b_point": b, "b_ci95": b_interval, "c_point": c, "c_ci95": c_interval,
        "number_to_quote": number_to_quote,
        "number_to_quote_source": ROUTE_B if number_to_quote is not None else None,
        "c_can_be_promoted": False, "blind_spot": BLIND_SPOT,
    }


def _jsonable_report(report: dict) -> dict:
    return EF._jsonable(report)


def _write_json(path: Path, report: dict) -> None:
    path.write_text(json.dumps(_jsonable_report(report), indent=1), encoding="utf-8")


def _pp(value) -> str:
    return "-" if value is None else f"{100.0 * float(value):+.3f}"


def _print_b(report: dict) -> None:
    headline = report["headline"]
    funnel = report["funnel"]
    print("=" * 78)
    print("B/C RE-LADDER - ROUTE B (PRIMARY, ONLY VERDICT-ELIGIBLE PATH)")
    print("=" * 78)
    print(f"sessions: {','.join(report['arm']['sessions_used'])}")
    print(f"events: {funnel['n_design_events']} paired: {funnel['n_paired']} "
          f"excluded: {funnel['n_excluded_unpaired']}")
    print(f"effective real/draws: {funnel['n_real_effective']}/{funnel['n_placebo_effective']}")
    print(f"mean diff: {_pp(headline['mean_diff'])} pp")
    if headline["ci95"]:
        print(f"ci95: [{_pp(headline['ci95'][0])}, {_pp(headline['ci95'][1])}] pp")
    else:
        print(f"ci95: withheld ({headline['ci_withheld']})")
    print(f"pass line: {_pp(PASS_LINE)} pp - 18-pair extrapolation, newcomers unknown")
    print(f"clears: {str(headline['clears_pass_line']).lower()}")
    print("C has not been run by this command.")


def _print_c(report: dict) -> None:
    headline = report["headline"]
    funnel = report["funnel"]
    print("=" * 78)
    print("B/C RE-LADDER - ROUTE C (FALSIFICATION ONLY, NEVER A VERDICT PATH)")
    print("=" * 78)
    print(f"sessions: {','.join(report['arm']['sessions_used'])}")
    print(f"design/effective: {funnel['n_design_population']}/{funnel['n_effective']}")
    print(f"local-support extrapolation: {funnel['n_outside_local_rectangular_support']}")
    print(f"mean diff: {_pp(headline['mean_diff'])} pp")
    if headline["ci95"]:
        print(f"ci95: [{_pp(headline['ci95'][0])}, {_pp(headline['ci95'][1])}] pp")
    else:
        print(f"ci95: withheld ({headline['ci_withheld']})")
    print(f"pass line: {_pp(PASS_LINE)} pp - table classification only")
    print("C cannot be promoted or quoted as the verdict number.")


def _parse_args(argv: list[str]) -> dict:
    db = HE.DB
    route = None
    out_dir = OUT_DIR / "bc_reladder"
    b_report = None
    c_report = None
    args = list(argv[1:])
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--route":
            route = args[i + 1].upper(); i += 2
        elif arg == "--out":
            out_dir = Path(args[i + 1]); i += 2
        elif arg == "--b-report":
            b_report = Path(args[i + 1]); i += 2
        elif arg == "--c-report":
            c_report = Path(args[i + 1]); i += 2
        elif arg == "--era":
            _guard_era(args[i + 1]); i += 2
        else:
            db = Path(arg); i += 1
    if route not in {ROUTE_B, ROUTE_C, "DECISION"}:
        raise ValueError("--route must be B, C, or decision")
    return {"db": db, "route": route, "out_dir": out_dir,
            "b_report": b_report, "c_report": c_report}


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    out_dir = args["out_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    if args["route"] == ROUTE_B:
        print("running route B first ...", flush=True)
        result = run_b(args["db"], progress=True)
        report = build_b_report(result)
        _write_json(out_dir / "bc_reladder_b.json", report)
        if len(result["events"]):
            result["events"].to_csv(out_dir / "bc_reladder_b_events.csv", index=False)
        if len(result["draws"]):
            result["draws"].to_csv(out_dir / "bc_reladder_b_draws.csv", index=False)
        _print_b(report)
        return 0
    if args["route"] == ROUTE_C:
        print("running route C after B ...", flush=True)
        result = run_c(args["db"], progress=True)
        report = build_c_report(result)
        _write_json(out_dir / "bc_reladder_c.json", report)
        if len(result["events"]):
            result["events"].to_csv(out_dir / "bc_reladder_c_events.csv", index=False)
        if len(result["controls"]):
            result["controls"].to_csv(out_dir / "bc_reladder_c_controls.csv", index=False)
        if len(result["stat"]["effects"]):
            result["stat"]["effects"].to_csv(
                out_dir / "bc_reladder_c_effects.csv", index=False)
        _print_c(report)
        return 0

    b_path = args["b_report"] or out_dir / "bc_reladder_b.json"
    c_path = args["c_report"] or out_dir / "bc_reladder_c.json"
    b_report = json.loads(b_path.read_text(encoding="utf-8"))
    c_report = json.loads(c_path.read_text(encoding="utf-8"))
    decision = decision_rule(
        b_report["headline"]["mean_diff"], b_report["headline"]["ci95"],
        c_report["headline"]["mean_diff"], c_report["headline"]["ci95"])
    _write_json(out_dir / "bc_reladder_decision.json", decision)
    print(f"decision cell: {decision['cell']}")
    print(f"quote source: {decision['number_to_quote_source'] or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""Count-only redesign of the candle matching axis (``docs/72``).

This module asks whether the 43 events excluded by the frozen candle ladder can
re-enter under three *candidate* designs.  It does not amend revisions 3, 5, or
6, does not draw placebos, and never computes an outcome, return, interval, or
ladder statistic.

The input is exploration era B only.  ``candle_ruler.count`` owns the arm
window and reaches ``ranking_forward_path.arm_window``; the database is opened
through ``hires_events.open_ro``.  Consequently the holdout and the
confirmation arm remain outside the row-level queries.

Routes counted here:

* A: keep ``crv5`` +/-20% and widen only the ``cvol5`` factor, using the width
  grid that revision 5 already counted (1.2, 1.5, 2.0, 3.0, no volume band).
* B: replace ``cvol5`` with the already-recorded ``cnbar5`` and require an
  exact active-minute count, while retaining the ``crv5`` band.
* C: stop requiring an event-level pair.  Count zero-aware 2-D pre-state
  strata and regression-complete rows.  These are different estimands and are
  deliberately reported only as sample availability.

Run:

``python -m tossmon.analysis.measure.matching_redesign [db] --out DIR --name NAME``

The console is ASCII only.  JSON and CSV contain counts and pre-anchor state;
there are no forward-path fields.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from tossmon.analysis import hires_events as HE
from tossmon.analysis import session as SS
from tossmon.analysis.measure import candle_ruler as CR
from tossmon.analysis.measure import e2_design_funnel as EF
from tossmon.analysis.measure import ranking_forward_path as RFP

OUT_DIR = RFP.OUT_DIR
SEC_MS = RFP.SEC_MS
MIN_MS = SS.MIN_MS

DECLARED = EF.DECLARED
STRATUM = "first_in_regular"
ERA = "B"

# Frozen baseline inputs.  The redesign only counts alternatives beside them.
SELF_GAP_S = CR.CANDLE_SELF_GAP_S
RV_TOL = CR.CANDLE_RV_TOL
BASELINE_VOL_FACTOR = CR.CANDLE_VOL_FACTOR

# Reuse the diagnostic width grid from revision 5; no outcome was used to pick it.
VOLUME_FACTORS = CR.VOL_FACTOR_SWEEP

# Sensitivity counts only.  Zero is always its own cell, then positive values
# are split by candidate-pool quantiles.  No one grid is selected here.
STRATA_GRIDS = (2, 3, 4)

NO_OUTCOMES = (
    "sample availability only - no outcome, return, confidence interval, "
    "ladder statistic, or route selection; route C is a different estimand "
    "and is not comparable to revision 3"
)


def _factor_label(factor) -> str:
    return "none" if factor is None else f"x{float(factor):.1f}"


def _dist(values) -> dict:
    a = np.asarray(values, dtype="float64")
    a = a[np.isfinite(a)]
    if a.size == 0:
        return {"n": 0, "total": 0, "min": None, "p25": None, "median": None,
                "p75": None, "max": None, "n_zero": 0}
    return {
        "n": int(a.size),
        "total": int(a.sum()),
        "min": float(a.min()),
        "p25": float(np.percentile(a, 25)),
        "median": float(np.median(a)),
        "p75": float(np.percentile(a, 75)),
        "max": float(a.max()),
        "n_zero": int((a == 0).sum()),
    }


def _value_dist(values) -> dict:
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


def _candidate_masks(keys: dict, tau_ms: np.ndarray, t0_ms: int, *,
                     event_rv: float, event_vol: float, event_nbar: int) -> dict:
    """Return cumulative masks for the frozen funnel and routes A/B/C.

    The frozen order is gap -> candidate key -> crv5 band -> cvol5 band.  An
    invalid event key is kept separate from an empty candidate stage so the
    first condition that empties each event remains visible.
    """
    tau = np.asarray(tau_ms, dtype="int64")
    rv = np.asarray(keys[CR.KEY_RV], dtype="float64")
    vol = np.asarray(keys[CR.KEY_VOL], dtype="float64")
    nbar = np.asarray(keys[CR.KEY_NBAR], dtype="int64")

    gap = np.abs(tau - int(t0_ms)) > int(SELF_GAP_S) * SEC_MS
    candidate_key = gap & np.isfinite(rv) & (rv > 0) & np.isfinite(vol) & (vol > 0)
    event_key = bool(np.isfinite(event_rv) and event_rv > 0
                     and np.isfinite(event_vol) and event_vol > 0)
    if event_key:
        rv_band = candidate_key & CR.in_band(rv, float(event_rv), RV_TOL)
    else:
        rv_band = np.zeros(tau.size, dtype=bool)

    volume = {}
    for factor in VOLUME_FACTORS:
        volume[_factor_label(factor)] = (
            rv_band & CR.in_factor_band(vol, float(event_vol), factor)
            if event_key else np.zeros(tau.size, dtype=bool)
        )

    # Route B replaces volume magnitude with exact active-minute density.
    event_nbar_key = bool(np.isfinite(event_rv) and event_rv > 0
                          and int(event_nbar) > 0)
    nbar_key = gap & np.isfinite(rv) & (rv > 0) & (nbar > 0)
    if event_nbar_key:
        nbar_exact = (nbar_key & CR.in_band(rv, float(event_rv), RV_TOL)
                      & (nbar == int(event_nbar)))
    else:
        nbar_exact = np.zeros(tau.size, dtype=bool)

    # Route C accepts observed zeros.  candle_arrays has already sanitized bad
    # OHLC/volume rows, so finite non-negative values form a valid design row.
    regression = (gap & np.isfinite(rv) & (rv >= 0)
                  & np.isfinite(vol) & (vol >= 0) & (nbar >= 0))
    return {
        "gap": gap, "candidate_key": candidate_key, "event_key": event_key,
        "rv_band": rv_band, "volume": volume,
        "event_nbar_key": event_nbar_key, "nbar_exact": nbar_exact,
        "regression": regression,
    }


def _nearest_ratio(candidate_vol: np.ndarray, event_vol: float) -> float | None:
    v = np.asarray(candidate_vol, dtype="float64")
    v = v[np.isfinite(v) & (v > 0)]
    if v.size == 0 or not np.isfinite(event_vol) or event_vol <= 0:
        return None
    raw = v / float(event_vol)
    factor = np.maximum(raw, 1.0 / raw)
    return float(raw[int(np.argmin(factor))])


def _minimum_factor(candidate_vol: np.ndarray, event_vol: float) -> float | None:
    ratio = _nearest_ratio(candidate_vol, event_vol)
    if ratio is None:
        return None
    return float(max(ratio, 1.0 / ratio))


def _ratio_directions(values) -> dict:
    a = np.asarray(values, dtype="float64")
    a = a[np.isfinite(a)]
    return {
        "candidate_below_event": int((a < 1.0).sum()),
        "exact": int((a == 1.0).sum()),
        "candidate_above_event": int((a > 1.0).sum()),
    }


def _positive_edges(values: np.ndarray, n_bins: int) -> np.ndarray:
    """Candidate-pool quantile edges; zero is handled as a separate cell."""
    v = np.asarray(values, dtype="float64")
    v = v[np.isfinite(v) & (v > 0)]
    if v.size == 0 or int(n_bins) <= 1:
        return np.zeros(0, dtype="float64")
    q = np.arange(1, int(n_bins), dtype="float64") / float(n_bins)
    return np.unique(np.quantile(v, q))


def _zero_aware_bins(values, edges: np.ndarray) -> np.ndarray:
    """Map zero to cell 0 and positive values to quantile cells 1..K."""
    v = np.atleast_1d(np.asarray(values, dtype="float64"))
    out = np.full(v.shape, -1, dtype="int64")
    out[np.isfinite(v) & (v == 0)] = 0
    pos = np.isfinite(v) & (v > 0)
    out[pos] = 1 + np.searchsorted(np.asarray(edges, dtype="float64"),
                                   v[pos], side="right")
    return out


def _sample(mask, baseline: np.ndarray) -> dict:
    m = np.asarray(mask, dtype=bool)
    b = np.asarray(baseline, dtype=bool)
    return {
        "n_events": int(m.sum()),
        "share": float(m.mean()) if m.size else None,
        "gained_from_baseline_excluded": int((m & ~b).sum()),
        "lost_from_baseline": int((~m & b).sum()),
        "net_change": int(m.sum() - b.sum()),
    }


def _counts(values) -> dict:
    s = pd.Series(values, dtype="object")
    return {str(k): int(v) for k, v in s.value_counts(dropna=False, sort=False).items()}


def _profile(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return {"n": 0}
    return {
        "n": int(len(frame)), "n_symbols": int(frame.symbol.nunique()),
        "key_ok": int(frame.key_ok.sum()),
        "has_bar_last": int(frame.has_bar_last.sum()),
        "old_ruler_observable": int(frame.pre_match60.sum()),
        "crv5": _value_dist(frame.crv5),
        "cvol5": _value_dist(frame.cvol5),
        "cnbar5": _value_dist(frame.cnbar5),
        "seconds_from_regular_open": _value_dist(frame.seconds_from_regular_open),
        "price_tier": _counts(frame.tier),
        "seat_at_t0": _counts(frame.seat_at_t0),
        "tier_at_t0": _counts(frame.tier_at_t0),
        "tier_floor_prior5": _counts(frame.tier_floor_prior5),
        "sessions": _counts(frame.session),
    }


def _first_empty(row: dict) -> str:
    if not bool(row["key_ok"]):
        return "event_key_missing"
    if int(row["n_gap"]) == 0:
        return "outside_gap_empty"
    if int(row["n_key"]) == 0:
        return "candidate_key_empty"
    if int(row["n_rv"]) == 0:
        return "crv5_band_empty"
    if int(row["n_baseline"]) == 0:
        return "cvol5_band_empty"
    return "paired"


def count(db: Path, *, era: str = ERA, progress: bool = False) -> dict:
    """Count the frozen funnel and the three recovery routes in era B only."""
    if era != ERA:
        raise ValueError("matching redesign is exploration era B only")
    counted = CR.count(db, era=era, progress=progress)
    tags = counted["tags"]
    allowed = set(RFP.EXPLORATION_B_SESSIONS)
    used = set(counted["sessions_used"])
    if not used <= allowed:
        raise ValueError(f"sessions outside exploration era B reached the redesign: {sorted(used - allowed)}")

    conn = HE.open_ro(db)
    rows: list[dict] = []
    pools: list[dict] = []
    try:
        for sess in counted["sessions"]:
            session = sess["session"]
            tagged = tags[tags.session == session] if len(tags) else tags
            if tagged.empty:
                continue
            first = tagged[CR.strata_masks(tagged)[STRATUM]].copy()
            candles = CR.load_candles(conn, sess["open_ms"], sess["close_ms"])
            for _, event in first.iterrows():
                symbol = str(event.symbol)
                t0_ms = int(event.t0_ms)
                candle = candles.get(symbol)
                if candle is None:
                    tau = np.zeros(0, dtype="int64")
                    keys = {CR.KEY_RV: np.zeros(0), CR.KEY_VOL: np.zeros(0),
                            CR.KEY_NBAR: np.zeros(0, dtype="int64")}
                else:
                    # Revision 6: a placebo tau is the candidate bar's content start.
                    tau = np.asarray(candle["ts"], dtype="int64") - MIN_MS
                    keys = CR.candle_keys_at(candle, tau)
                masks = _candidate_masks(
                    keys, tau, t0_ms, event_rv=float(event.crv5),
                    event_vol=float(event.cvol5), event_nbar=int(event.cnbar5))
                base_mask = masks["volume"][_factor_label(BASELINE_VOL_FACTOR)]
                rv_vol = np.asarray(keys[CR.KEY_VOL], dtype="float64")[masks["rv_band"]]
                exact = (masks["rv_band"]
                         & (np.asarray(keys[CR.KEY_VOL], dtype="float64")
                            == float(event.cvol5)))
                reg = masks["regression"]
                reg_rv = np.asarray(keys[CR.KEY_RV], dtype="float64")[reg]
                reg_vol = np.asarray(keys[CR.KEY_VOL], dtype="float64")[reg]
                within = bool(
                    reg_rv.size
                    and np.isfinite(float(event.crv5)) and float(event.crv5) >= 0
                    and np.isfinite(float(event.cvol5)) and float(event.cvol5) >= 0
                    and reg_rv.min() <= float(event.crv5) <= reg_rv.max()
                    and reg_vol.min() <= float(event.cvol5) <= reg_vol.max()
                )
                row = {
                    "session": session, "symbol": symbol, "t0_ms": t0_ms,
                    "seconds_from_regular_open": (t0_ms - int(sess["open_ms"])) / 1000.0,
                    "crv5": float(event.crv5), "cvol5": float(event.cvol5),
                    "cnbar5": int(event.cnbar5), "key_ok": bool(event.key_ok),
                    "has_bar_last": bool(event.has_bar_last),
                    "pre_match60": bool(event.pre_match60), "tier": str(event.tier),
                    "seat_at_t0": str(event.seat_at_t0),
                    "tier_at_t0": int(event.tier_at_t0),
                    "tier_floor_prior5": int(event.tier_floor_prior5),
                    "n_gap": int(masks["gap"].sum()),
                    "n_key": int(masks["candidate_key"].sum()),
                    "n_rv": int(masks["rv_band"].sum()),
                    "n_baseline": int(base_mask.sum()),
                    "paired_baseline": bool(base_mask.any()),
                    "n_exact_cvol5": int(exact.sum()),
                    "min_cvol5_factor_after_crv5": _minimum_factor(rv_vol, float(event.cvol5)),
                    "nearest_cvol5_ratio_after_crv5": _nearest_ratio(rv_vol, float(event.cvol5)),
                    "n_cnbar5_exact": int(masks["nbar_exact"].sum()),
                    "paired_cnbar5_exact": bool(masks["nbar_exact"].any()),
                    "n_regression_controls": int(reg.sum()),
                    "regression_complete": bool(
                        reg.any() and np.isfinite(float(event.crv5)) and float(event.crv5) >= 0
                        and np.isfinite(float(event.cvol5)) and float(event.cvol5) >= 0),
                    "regression_within_local_range": within,
                }
                for factor in VOLUME_FACTORS:
                    label = _factor_label(factor)
                    m = masks["volume"][label]
                    row[f"n_volume_{label}"] = int(m.sum())
                    row[f"paired_volume_{label}"] = bool(m.any())
                row["first_empty"] = _first_empty(row)
                event_index = len(rows)
                rows.append(row)
                pools.append({
                    "event_index": event_index,
                    "tau": np.asarray(tau, dtype="int64")[reg],
                    "rv": reg_rv, "vol": reg_vol,
                })
            if progress:
                done = [r for r in rows if r["session"] == session]
                paired = sum(bool(r["paired_baseline"]) for r in done)
                print(f"  {session} first={len(done):>3} paired={paired:>3}", flush=True)
    finally:
        conn.close()

    events = pd.DataFrame(rows)
    if events.empty:
        return {**counted, "events": events, "strata": {}, "unique_regression_controls": 0}

    all_rv = np.concatenate([p["rv"] for p in pools if p["rv"].size])
    all_vol = np.concatenate([p["vol"] for p in pools if p["vol"].size])
    strata = {}
    for grid in STRATA_GRIDS:
        rv_edges = _positive_edges(all_rv, grid)
        vol_edges = _positive_edges(all_vol, grid)
        counts = np.zeros(len(events), dtype="int64")
        for pool in pools:
            i = int(pool["event_index"])
            er = _zero_aware_bins([events.at[i, "crv5"]], rv_edges)[0]
            ev = _zero_aware_bins([events.at[i, "cvol5"]], vol_edges)[0]
            if er < 0 or ev < 0 or not pool["rv"].size:
                continue
            pr = _zero_aware_bins(pool["rv"], rv_edges)
            pv = _zero_aware_bins(pool["vol"], vol_edges)
            counts[i] = int(((pr == er) & (pv == ev)).sum())
        col = f"n_strata_q{grid}"
        events[col] = counts
        events[f"supported_strata_q{grid}"] = counts > 0
        strata[str(grid)] = {
            "positive_quantile_bins_per_axis": int(grid),
            "zero_is_own_cell": True,
            "rv_edges": rv_edges.tolist(), "vol_edges": vol_edges.tolist(),
            "candidate_counts": _dist(counts),
        }

    unique_controls = {
        (str(events.at[p["event_index"], "session"]),
         str(events.at[p["event_index"], "symbol"]), int(tau))
        for p in pools for tau in p["tau"]
    }
    return {
        **counted, "events": events, "strata": strata,
        "unique_regression_controls": int(len(unique_controls)),
    }


def build_report(result: dict) -> dict:
    """Build the single count-only report consumed by ``docs/72``."""
    events = result["events"]
    if events.empty:
        baseline = np.zeros(0, dtype=bool)
    else:
        baseline = events.paired_baseline.to_numpy(dtype=bool)

    route_a = []
    for factor in VOLUME_FACTORS:
        label = _factor_label(factor)
        mask = (events[f"paired_volume_{label}"].to_numpy(dtype=bool)
                if len(events) else np.zeros(0, dtype=bool))
        route_a.append({
            "volume_factor": factor, "label": label, **_sample(mask, baseline),
            "candidate_counts": _dist(events[f"n_volume_{label}"] if len(events) else []),
        })

    nbar_mask = (events.paired_cnbar5_exact.to_numpy(dtype=bool)
                 if len(events) else np.zeros(0, dtype=bool))
    regression = (events.regression_complete.to_numpy(dtype=bool)
                  if len(events) else np.zeros(0, dtype=bool))
    regression_range = (events.regression_within_local_range.to_numpy(dtype=bool)
                        if len(events) else np.zeros(0, dtype=bool))
    strata_routes = []
    for grid in STRATA_GRIDS:
        mask = (events[f"supported_strata_q{grid}"].to_numpy(dtype=bool)
                if len(events) else np.zeros(0, dtype=bool))
        strata_routes.append({
            "positive_quantile_bins_per_axis": grid,
            "zero_is_own_cell": True,
            **_sample(mask, baseline),
            "candidate_counts": _dist(events[f"n_strata_q{grid}"] if len(events) else []),
        })

    stage_names = ("n_gap", "n_key", "n_rv", "n_baseline")
    first_empty = (_counts(events.first_empty) if len(events) else {})
    paired = events[events.paired_baseline] if len(events) else events
    unpaired = events[~events.paired_baseline] if len(events) else events
    exact_mask = ((events.n_exact_cvol5 > 0).to_numpy(dtype=bool)
                  if len(events) else np.zeros(0, dtype=bool))
    rv_support = ((events.n_rv > 0).to_numpy(dtype=bool)
                  if len(events) else np.zeros(0, dtype=bool))
    cvol_empty = ((events.first_empty == "cvol5_band_empty").to_numpy(dtype=bool)
                  if len(events) else np.zeros(0, dtype=bool))

    per_session = []
    for session in result["sessions_used"]:
        sub = events[events.session == session] if len(events) else events
        row = {
            "session": session, "n_events": int(len(sub)),
            "baseline": int(sub.paired_baseline.sum()) if len(sub) else 0,
            "cnbar5_exact": int(sub.paired_cnbar5_exact.sum()) if len(sub) else 0,
            "regression_complete": int(sub.regression_complete.sum()) if len(sub) else 0,
            "regression_within_local_range": (
                int(sub.regression_within_local_range.sum()) if len(sub) else 0),
        }
        for factor in VOLUME_FACTORS:
            label = _factor_label(factor)
            row[f"volume_{label}"] = int(sub[f"paired_volume_{label}"].sum()) if len(sub) else 0
        for grid in STRATA_GRIDS:
            row[f"strata_q{grid}"] = int(sub[f"supported_strata_q{grid}"].sum()) if len(sub) else 0
        per_session.append(row)

    return {
        "labels": list(RFP.LABELS),
        "conditions": HE.MEASUREMENT_CONDITIONS,
        "db": result["db"],
        "window": {"since_utc": result["since_utc"], "until_utc": result["until_utc"]},
        "arm": {
            "name": "exploration", "exploration_era": result["era"],
            "sessions_used": result["sessions_used"],
            "sessions_allowed": list(RFP.EXPLORATION_B_SESSIONS),
            "confirmation_floor_utc": RFP.CONFIRMATION_FLOOR_UTC,
            "window_rule": "candle_ruler.count -> ranking_forward_path.arm_window",
        },
        "holdout": {"window": [SS.HOLDOUT_START, SS.HOLDOUT_END]},
        "design": {
            "declared_cell": list(DECLARED), "stratum": STRATUM,
            "frozen_baseline": {
                "self_gap_s": SELF_GAP_S, "crv5_tolerance": RV_TOL,
                "cvol5_factor": BASELINE_VOL_FACTOR,
                "candidate_tau": "bar content start (T_b - 60s)",
            },
            "status": NO_OUTCOMES,
        },
        "wall": {
            "same_as_docs66": False,
            "reason": (
                "docs/66 concerns an after-anchor realised quantity whose pair gap cannot "
                "be predicted away by any pre-anchor axis; cvol5 is itself observed before "
                "tau on both arms, so its target is defined. The empirical obstacle here is "
                "sparse overlap at an event-selected local volume extreme, not a missing target."
            ),
            "n_event_key_valid": int(events.key_ok.sum()) if len(events) else 0,
            "n_events_with_crv5_support": int(rv_support.sum()),
            "n_events_with_exact_cvol5_candidate_after_crv5": int(exact_mask.sum()),
            "n_exact_cvol5_candidates": int(events.n_exact_cvol5.sum()) if len(events) else 0,
            "minimum_cvol5_factor_after_crv5": _value_dist(
                events.min_cvol5_factor_after_crv5 if len(events) else []),
            "nearest_cvol5_ratio_after_crv5": {
                "all_crv5_supported": _value_dist(
                    events.nearest_cvol5_ratio_after_crv5 if len(events) else []),
                "all_crv5_supported_direction": _ratio_directions(
                    events.nearest_cvol5_ratio_after_crv5 if len(events) else []),
                "first_empty_at_cvol5": _value_dist(
                    events.loc[cvol_empty, "nearest_cvol5_ratio_after_crv5"]
                    if len(events) else []),
                "first_empty_at_cvol5_minimum_factor": _value_dist(
                    events.loc[cvol_empty, "min_cvol5_factor_after_crv5"]
                    if len(events) else []),
                "first_empty_at_cvol5_direction": _ratio_directions(
                    events.loc[cvol_empty, "nearest_cvol5_ratio_after_crv5"]
                    if len(events) else []),
            },
        },
        "funnel": {
            "n_events": int(len(events)),
            "stages": {name: _dist(events[name] if len(events) else [])
                       for name in stage_names},
            "first_empty": first_empty,
            "baseline_paired": int(baseline.sum()),
            "baseline_excluded": int((~baseline).sum()),
        },
        "selection": {
            "paired": _profile(paired),
            "excluded": _profile(unpaired),
        },
        "routes": {
            "A_widen_cvol5": route_a,
            "B_replace_cvol5_with_cnbar5_exact": {
                **_sample(nbar_mask, baseline),
                "candidate_counts": _dist(events.n_cnbar5_exact if len(events) else []),
                "axis": "crv5 +/-20% AND exact cnbar5; cvol5 is not matched",
            },
            "C_use_unpaired_differently": {
                "warning": (
                    "stratification and regression are different estimands; these sample "
                    "counts are not comparable to the revision 3 paired estimator"
                ),
                "stratification": strata_routes,
                "regression_complete": {
                    **_sample(regression, baseline),
                    "candidate_rows": int(events.n_regression_controls.sum()) if len(events) else 0,
                    "unique_candidate_minutes": result["unique_regression_controls"],
                    "zero_handling": "crv5 and cvol5 zeros retained; non-negative finite design",
                },
                "regression_within_local_rectangular_support": {
                    **_sample(regression_range, baseline),
                    "definition": (
                        "event crv5 and cvol5 each lie within the same-symbol/session "
                        "outside-gap candidate minimum and maximum"
                    ),
                },
            },
        },
        "per_session": per_session,
    }


def print_report(report: dict) -> None:
    """Print an ASCII-only sample summary."""
    print("=" * 78)
    print("MATCHING REDESIGN - SAMPLE COUNTS ONLY (docs/72)")
    print("=" * 78)
    arm = report["arm"]
    print(f"  arm     : exploration era {arm['exploration_era']}  sessions "
          f"{len(arm['sessions_used'])} {','.join(arm['sessions_used'])}")
    print(f"  window  : {report['window']['since_utc']} .. {report['window']['until_utc']}")
    print(f"  confirm : floor {arm['confirmation_floor_utc']} (not opened)")
    funnel = report["funnel"]
    print(f"  sample  : events {funnel['n_events']}  baseline paired "
          f"{funnel['baseline_paired']}  excluded {funnel['baseline_excluded']}")
    print("\n[1] wall")
    print(f"  same as docs/66: {str(report['wall']['same_as_docs66']).lower()}")
    print("  reason: cvol5 is observed before tau; the issue is sparse overlap, not")
    print("          an unreachable realised-depth target")
    print("\n[2] frozen funnel")
    for name in ("n_gap", "n_key", "n_rv", "n_baseline"):
        d = funnel["stages"][name]
        print(f"  {name:<12} p50 {d['median']:>7.1f}  p25 {d['p25']:>7.1f}  "
              f"p75 {d['p75']:>7.1f}  zero {d['n_zero']:>3}")
    print("  first empty: " + "  ".join(f"{k}={v}" for k, v in funnel["first_empty"].items()))
    print("\n[3] route A - widen cvol5 (crv5 remains +/-20%)")
    for row in report["routes"]["A_widen_cvol5"]:
        print(f"  {row['label']:<5} events {row['n_events']:>3}  gained "
              f"{row['gained_from_baseline_excluded']:>3}  lost {row['lost_from_baseline']:>3}")
    b = report["routes"]["B_replace_cvol5_with_cnbar5_exact"]
    print("\n[4] route B - replace cvol5 with exact cnbar5")
    print(f"  events {b['n_events']:>3}  gained {b['gained_from_baseline_excluded']:>3}  "
          f"lost {b['lost_from_baseline']:>3}")
    c = report["routes"]["C_use_unpaired_differently"]
    print("\n[5] route C - different estimand, sample only")
    for row in c["stratification"]:
        print(f"  strata q{row['positive_quantile_bins_per_axis']}: events "
              f"{row['n_events']:>3}  gained {row['gained_from_baseline_excluded']:>3}")
    reg = c["regression_complete"]
    rng = c["regression_within_local_rectangular_support"]
    print(f"  regression complete: events {reg['n_events']:>3}  gained "
          f"{reg['gained_from_baseline_excluded']:>3}  candidate rows {reg['candidate_rows']}")
    print(f"  regression local range: events {rng['n_events']:>3}  gained "
          f"{rng['gained_from_baseline_excluded']:>3}")
    print("\nNo route is selected. No outcome, return, CI, or ladder was computed.")


def main(argv: list) -> int:
    db = HE.DB
    out_dir = OUT_DIR
    name = "matching_redesign_era_b"
    args = list(argv[1:])
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--era":
            if args[i + 1] != ERA:
                raise ValueError("matching redesign is exploration era B only")
            i += 2
        elif arg == "--out":
            out_dir = Path(args[i + 1]); i += 2
        elif arg == "--name":
            name = args[i + 1]; i += 2
        else:
            db = Path(arg); i += 1
    print("matching redesign, era B - counting sample only ...", flush=True)
    result = count(db, era=ERA, progress=True)
    report = EF._jsonable(build_report(result))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.json"
    path.write_text(json.dumps(report, indent=1), encoding="utf-8")
    if len(result["events"]):
        result["events"].to_csv(out_dir / f"{name}_events.csv", index=False)
    print_report(report)
    print(f"-> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

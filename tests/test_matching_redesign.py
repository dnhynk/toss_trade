"""Guards for the count-only matching redesign used by ``docs/72``.

The tests pin the sample funnel and the three alternative sample routes.  They
never construct a forward path or an outcome.  Boundary values are absolute so
mutations to the gap, the frozen ``crv5`` band, the volume factor, the exact
``cnbar5`` replacement, or zero handling make a test red.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tossmon.analysis.measure import candle_ruler as CR
from tossmon.analysis.measure import matching_redesign as MR
from tossmon.analysis.measure import ranking_forward_path as RFP

MS = 1000
T0 = 1_800_000_000_000


def _keys(rv, vol, nbar):
    return {
        CR.KEY_RV: np.asarray(rv, dtype="float64"),
        CR.KEY_VOL: np.asarray(vol, dtype="float64"),
        CR.KEY_NBAR: np.asarray(nbar, dtype="int64"),
    }


def test_the_frozen_baseline_is_reused_and_the_runner_is_era_b_only(tmp_path) -> None:
    assert MR.SELF_GAP_S == CR.CANDLE_SELF_GAP_S == 600
    assert MR.RV_TOL == CR.CANDLE_RV_TOL == 0.20
    assert MR.BASELINE_VOL_FACTOR == CR.CANDLE_VOL_FACTOR == 1.2
    assert MR.VOLUME_FACTORS == CR.VOL_FACTOR_SWEEP == (1.2, 1.5, 2.0, 3.0, None)
    assert MR.STRATUM == "first_in_regular"
    assert MR.ERA == "B"
    assert MR.DECLARED == ("TOSS_SECURITIES_TRADING_VOLUME", "realtime",
                           "E1_new_entry", "N10")
    with pytest.raises(ValueError, match="era B only"):
        MR.count(tmp_path / "must-not-open.db", era="A")


def test_funnel_order_volume_widths_and_zero_aware_regression_are_absolute() -> None:
    # Index 0 is exactly 600s away and must fail the strict gap.  Index 4 has
    # zero crv5: it fails the matching key but remains a valid regression row.
    tau = np.asarray([T0 + 600 * MS, T0 + 601 * MS, T0 + 602 * MS,
                      T0 + 603 * MS, T0 + 604 * MS], dtype="int64")
    masks = MR._candidate_masks(
        _keys([1.0, 1.0, 1.0, 1.3, 0.0],
              [100.0, 100.0, 130.0, 100.0, 100.0],
              [5, 5, 4, 5, 5]),
        tau, T0, event_rv=1.0, event_vol=100.0, event_nbar=5)

    assert masks["gap"].tolist() == [False, True, True, True, True]
    assert masks["candidate_key"].tolist() == [False, True, True, True, False]
    assert masks["rv_band"].tolist() == [False, True, True, False, False]
    assert masks["volume"]["x1.2"].tolist() == [False, True, False, False, False]
    assert masks["volume"]["x1.5"].tolist() == [False, True, True, False, False]
    assert masks["volume"]["none"].tolist() == [False, True, True, False, False]
    assert masks["nbar_exact"].tolist() == [False, True, False, False, False]
    assert masks["regression"].tolist() == [False, True, True, True, True]


def test_route_b_replaces_volume_magnitude_with_exact_active_minute_density() -> None:
    tau = np.asarray([T0 + 601 * MS, T0 + 602 * MS], dtype="int64")
    masks = MR._candidate_masks(
        _keys([1.0, 1.0], [10_000.0, 100.0], [5, 4]), tau, T0,
        event_rv=1.0, event_vol=100.0, event_nbar=5)
    # Candidate 0 is far outside the volume band but shares exact cnbar5.
    # Candidate 1 is an exact volume match but has a different cnbar5.
    assert masks["volume"]["x1.2"].tolist() == [False, True]
    assert masks["nbar_exact"].tolist() == [True, False]


def test_an_invalid_event_key_is_not_misreported_as_an_empty_candidate_stage() -> None:
    tau = np.asarray([T0 + 601 * MS], dtype="int64")
    masks = MR._candidate_masks(_keys([1.0], [100.0], [5]), tau, T0,
                                event_rv=0.0, event_vol=100.0, event_nbar=5)
    assert masks["candidate_key"].tolist() == [True]
    assert masks["event_key"] is False
    assert not masks["rv_band"].any()
    row = {"key_ok": False, "n_gap": 1, "n_key": 1, "n_rv": 0, "n_baseline": 0}
    assert MR._first_empty(row) == "event_key_missing"


def test_minimum_volume_factor_is_log_symmetric() -> None:
    assert MR._minimum_factor(np.asarray([50.0, 130.0]), 100.0) == pytest.approx(1.3)
    assert MR._nearest_ratio(np.asarray([50.0, 130.0]), 100.0) == pytest.approx(1.3)
    assert MR._nearest_ratio(np.asarray([80.0, 150.0]), 100.0) == pytest.approx(0.8)
    assert MR._minimum_factor(np.asarray([40.0]), 100.0) == pytest.approx(2.5)
    assert MR._minimum_factor(np.asarray([]), 100.0) is None


def test_zero_is_its_own_stratification_cell() -> None:
    edges = MR._positive_edges(np.asarray([0.0, 1.0, 2.0, 3.0, 4.0]), 2)
    assert edges.tolist() == [2.5]
    bins = MR._zero_aware_bins([np.nan, 0.0, 1.0, 2.5, 4.0], edges)
    assert bins.tolist() == [-1, 0, 1, 2, 2]


def _row(i: int) -> dict:
    baseline = i == 0
    row = {
        "session": "2026-08-18", "symbol": f"S{i}", "t0_ms": T0 + i,
        "seconds_from_regular_open": 100.0 + i,
        "crv5": [0.03, 0.07, 0.0][i], "cvol5": [300.0, 500.0, 0.0][i],
        "cnbar5": [5, 4, 0][i], "key_ok": i != 2,
        "has_bar_last": i != 2, "pre_match60": i == 0,
        "tier": "u5", "seat_at_t0": "lane", "tier_at_t0": 3,
        "tier_floor_prior5": 1, "n_gap": 10, "n_key": 8,
        "n_rv": 4 if i != 2 else 0, "n_baseline": 1 if baseline else 0,
        "paired_baseline": baseline, "n_exact_cvol5": 1 if baseline else 0,
        "min_cvol5_factor_after_crv5": [1.0, 1.4, np.nan][i],
        "nearest_cvol5_ratio_after_crv5": [1.0, 1.4, np.nan][i],
        "n_cnbar5_exact": 1 if i < 2 else 0,
        "paired_cnbar5_exact": i < 2,
        "n_regression_controls": 10,
        "regression_complete": True,
        "regression_within_local_range": i < 2,
        "first_empty": ["paired", "cvol5_band_empty", "event_key_missing"][i],
    }
    factor_support = {
        "x1.2": i == 0, "x1.5": i < 2, "x2.0": i < 2,
        "x3.0": True, "none": True,
    }
    for label, supported in factor_support.items():
        row[f"n_volume_{label}"] = 1 if supported else 0
        row[f"paired_volume_{label}"] = supported
    strata_support = {2: i < 2, 3: i == 0, 4: True}
    for grid, supported in strata_support.items():
        row[f"n_strata_q{grid}"] = 2 if supported else 0
        row[f"supported_strata_q{grid}"] = supported
    return row


def _result() -> dict:
    return {
        "db": "mode=ro fixture", "era": "B",
        "since_utc": "2026-08-18T00:00:00Z",
        "until_utc": "2026-08-25T23:59:59Z",
        "sessions_used": ["2026-08-18"],
        "events": pd.DataFrame([_row(i) for i in range(3)]),
        "unique_regression_controls": 30,
    }


def test_route_sample_accounting_reports_gains_losses_and_no_choice() -> None:
    report = MR.build_report(_result())
    a = {row["label"]: row for row in report["routes"]["A_widen_cvol5"]}
    assert a["x1.2"]["n_events"] == 1
    assert a["x1.5"]["n_events"] == 2
    assert a["x1.5"]["gained_from_baseline_excluded"] == 1
    assert a["x3.0"]["n_events"] == 3
    b = report["routes"]["B_replace_cvol5_with_cnbar5_exact"]
    assert b["n_events"] == 2 and b["gained_from_baseline_excluded"] == 1
    c = report["routes"]["C_use_unpaired_differently"]
    assert c["regression_complete"]["n_events"] == 3
    assert c["regression_complete"]["candidate_rows"] == 30
    assert c["regression_within_local_rectangular_support"]["n_events"] == 2
    assert "different estimand" in c["warning"]


def test_report_has_no_outcome_fields_and_console_is_ascii(capsys) -> None:
    report = MR.build_report(_result())
    text = json.dumps(report).lower()
    for key in CR.FORBIDDEN_KEYS:
        assert key not in text
    assert report["wall"]["same_as_docs66"] is False
    assert report["arm"]["sessions_allowed"] == list(RFP.EXPLORATION_B_SESSIONS)
    assert report["arm"]["confirmation_floor_utc"] == "2026-08-26T00:00:00Z"
    MR.print_report(report)
    out = capsys.readouterr().out
    out.encode("ascii")
    assert "SAMPLE COUNTS ONLY" in out
    assert "No route is selected" in out


def test_source_cannot_reach_the_forward_ruler() -> None:
    source = Path(MR.__file__).read_text(encoding="utf-8")
    assert "ruler_bias" not in source
    assert "bar_forward_at" not in source
    assert "cluster_bootstrap" not in source

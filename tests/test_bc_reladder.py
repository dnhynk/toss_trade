"""Guards for revision-8 route B (primary) and route C (falsification only).

The important separation is behavioral, not a label: B has no ``cvol5`` input
and uses the frozen paired estimator, while C can never populate the number to
quote.  The last mutation test rewrites the quote assignment from B to C and
proves that the runtime/test contract turns red.
"""
from __future__ import annotations

import inspect
import json

import numpy as np
import pandas as pd
import pytest

from tossmon.analysis.measure import bc_reladder as BC
from tossmon.analysis.measure import candle_ladder as CL
from tossmon.analysis.measure import candle_ruler as CR
from tossmon.analysis.measure import ranking_forward_path as RFP

MS = 1000
T0 = 1_800_000_000_000
SESSIONS = tuple(f"2026-08-{day:02d}" for day in (18, 19, 20, 21, 24, 25))


def test_frozen_constants_and_roles_are_exact() -> None:
    assert BC.ERA == "B"
    assert BC.ROUTE_B == "B" and BC.ROUTE_C == "C"
    assert BC.STRATUM == "first_in_regular"
    assert BC.SELF_GAP_S == CR.CANDLE_SELF_GAP_S == 600
    assert BC.RV_TOL == CR.CANDLE_RV_TOL == 0.20
    assert BC.DRAWS == RFP.MATCH_DRAWS == 3
    assert BC.SEED == RFP.SEED == 20260818
    assert BC.PASS_LINE == CL.FIRST_ENTRY_BIAS == pytest.approx(0.0091)
    assert "18" in BC.PASS_LINE_NOTE and "not claimed conservative" in BC.PASS_LINE_NOTE


def test_route_b_is_strict_gap_rv_band_and_exact_nbar_with_no_volume_input() -> None:
    assert "vol" not in inspect.signature(BC._route_b_candidate_mask).parameters
    tau = np.asarray([T0 + 600 * MS, T0 + 601 * MS, T0 + 602 * MS,
                      T0 + 603 * MS], dtype="int64")
    mask = BC._route_b_candidate_mask(
        [1.0, 1.0, 1.3, 1.0], [5, 5, 5, 4], tau, T0, 1.0, 5)
    assert mask["gap"].tolist() == [False, True, True, True]
    assert mask["key"].tolist() == [False, True, True, True]
    assert mask["rv"].tolist() == [False, True, False, True]
    assert mask["all"].tolist() == [False, True, False, False]
    source = inspect.getsource(BC.run_b)
    assert "KEY_VOL" not in source and "cvol5" not in source


def test_route_b_invalid_event_key_does_not_hide_candidate_support() -> None:
    tau = np.asarray([T0 + 601 * MS], dtype="int64")
    mask = BC._route_b_candidate_mask([1.0], [5], tau, T0, 0.0, 5)
    assert mask["key"].tolist() == [True]
    assert mask["event_key_ok"] is False
    assert not mask["rv"].any() and not mask["all"].any()


def test_route_c_preserves_zero_and_uses_the_same_strict_gap() -> None:
    tau = np.asarray([T0 + 600 * MS, T0 + 601 * MS, T0 + 602 * MS], dtype="int64")
    mask = BC._route_c_design_mask(
        [0.0, 0.0, np.nan], [0.0, 100.0, 1.0], [0, 0, 1], tau, T0)
    assert mask.tolist() == [False, True, False]
    features = BC._features([0.0, 1.0], [0.0, 9.0])
    assert np.allclose(features, [[0.0, 0.0], [np.log(2), np.log(10)]])


def _regression_frames(n_sessions: int = 6, effects=None):
    effects = effects or [0.01 + 0.001 * i for i in range(n_sessions)]
    event_rows = []
    control_rows = []
    points = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)]
    for i in range(n_sessions):
        session = SESSIONS[i]
        group_id = f"{session}|S{i}|{i}"
        alpha = 0.05 * i
        event_x = np.asarray([0.25, 0.75])
        predicted = alpha + 2.0 * event_x[0] + 3.0 * event_x[1]
        event_rows.append({
            "group_id": group_id, "session": session, "symbol": f"S{i}",
            "design_complete": True, "bar_max_ret": predicted + effects[i],
            "x1": event_x[0], "x2": event_x[1], "within_local_range": i != 0,
            "n_controls_design": 4, "n_controls_outcome": 4,
        })
        for x1, x2 in points:
            control_rows.append({
                "group_id": group_id, "session": session, "symbol": f"S{i}",
                "x1": x1, "x2": x2, "bar_max_ret": alpha + 2.0 * x1 + 3.0 * x2,
            })
    return pd.DataFrame(event_rows), pd.DataFrame(control_rows)


def test_c_regression_recovers_the_frozen_formula_and_event_equal_mean() -> None:
    effects = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06]
    events, controls = _regression_frames(effects=effects)
    groups = BC._c_group_summaries(events, controls)
    estimate = BC._estimate_c_groups(groups, include_effects=True)
    assert estimate["beta"] == pytest.approx([2.0, 3.0])
    assert estimate["rank"] == 2
    assert estimate["estimate"] == pytest.approx(np.mean(effects))
    assert estimate["effects"].effect.tolist() == pytest.approx(effects)


def test_c_control_pool_size_does_not_reweight_an_event() -> None:
    events, controls = _regression_frames()
    original = BC._estimate_c_groups(BC._c_group_summaries(events, controls))["estimate"]
    first = controls[controls.group_id == events.iloc[0].group_id]
    duplicated = pd.concat([controls, first, first, first], ignore_index=True)
    changed = BC._estimate_c_groups(BC._c_group_summaries(events, duplicated))["estimate"]
    assert changed == pytest.approx(original)


def test_c_bootstrap_resamples_sessions_refits_and_withholds_below_five() -> None:
    events, controls = _regression_frames()
    full = BC._cluster_bootstrap_c(events, controls, n_boot=200)
    assert full["n_clusters"] == 6 and full["ci95"] is not None
    assert full["design_rank"] == 2
    thin_events = events.iloc[:4].copy()
    thin_controls = controls[controls.group_id.isin(thin_events.group_id)].copy()
    thin = BC._cluster_bootstrap_c(thin_events, thin_controls, n_boot=20)
    assert thin["n_clusters"] == 4 and thin["ci95"] is None
    assert "< 5" in thin["ci_withheld"]


def _b_result(diff: float = 0.02) -> dict:
    event_rows = []
    draw_rows = []
    real = {}
    placebo = {}
    for i, session in enumerate(SESSIONS):
        event_rows.append({
            "session": session, "symbol": f"S{i}", "t0_ms": T0 + i,
            "crv5": 1.0, "cnbar5": 5, "event_key_ok": True,
            "bar_n_bars": 5, "bar_max_ret": 0.01 + diff,
            "bar_entry_u": 1.0, "bar_entry_lag_s": 10.0,
            "n_cand_gap": 10, "n_cand_key": 9, "n_cand_rv": 8,
            "n_cand": 7, "n_draws": 3, "paired": True, "first_empty": "paired",
        })
        for draw in range(3):
            draw_rows.append({
                "session": session, "symbol": f"S{i}", "event_t0_ms": T0 + i,
                "tau_ms": T0 + 700_000 + draw, "crv5": 1.0, "cnbar5": 5,
                "bar_n_bars": 5, "bar_max_ret": 0.01,
                "bar_entry_u": 1.0, "bar_entry_lag_s": 10.0,
            })
        real[session] = np.asarray([0.01 + diff])
        placebo[session] = np.asarray([0.01] * 3)
    return {
        "db": "mode=ro fixture", "era": "B",
        "since_utc": "2026-08-18T00:00:00Z",
        "until_utc": "2026-08-25T23:59:59Z", "db_max_snap_utc": None,
        "sessions_used": list(SESSIONS), "events": pd.DataFrame(event_rows),
        "draws": pd.DataFrame(draw_rows),
        "stat": RFP.cluster_bootstrap_diff(real, placebo),
    }


def test_b_report_is_the_paired_estimator_and_uses_the_extrapolated_line() -> None:
    report = BC.build_b_report(_b_result())
    assert report["design"]["route"] == "B"
    assert report["headline"]["mean_diff"] == pytest.approx(0.02)
    assert report["headline"]["ci95"] == pytest.approx([0.02, 0.02])
    assert report["headline"]["clears_pass_line"] is True
    assert report["funnel"]["n_paired"] == 6
    assert "18" in report["design"]["pass_line_note"]


def test_decision_table_has_all_frozen_cells_and_never_quotes_c() -> None:
    agree = BC.decision_rule(0.020, [0.015, 0.025], 0.019, [0.014, 0.024])
    assert agree["cell"] == "B_CLEARS_C_AGREES"
    assert agree["number_to_quote"] == pytest.approx(0.020)
    assert agree["number_to_quote_source"] == "B"

    killed = BC.decision_rule(0.020, [0.015, 0.025], 0.005, [-0.002, 0.010])
    assert killed["cell"] == "B_CLEARS_C_KILLS" and killed["number_to_quote"] is None

    finding = BC.decision_rule(0.005, [-0.003, 0.008], 0.020, [0.012, 0.028])
    assert finding["cell"] == "B_MISSES_C_CLEARS_FINDING_ONLY"
    assert finding["number_to_quote"] is None and finding["c_can_be_promoted"] is False

    closed = BC.decision_rule(0.005, [-0.003, 0.008], 0.006, [-0.002, 0.010])
    assert closed["cell"] == "BOTH_MISS_AXIS_CLOSED" and closed["number_to_quote"] is None

    larger_disagreement = BC.decision_rule(
        0.020, [0.015, 0.025], 0.040, [0.030, 0.050])
    assert larger_disagreement["cell"] == "B_CLEARS_C_DISAGREES"
    assert larger_disagreement["number_to_quote"] is None


def test_small_c_inside_both_intervals_is_predeclared_agreement() -> None:
    result = BC.decision_rule(0.020, [0.010, 0.030], 0.018, [0.015, 0.025])
    assert result["agreement"] is True
    assert result["cell"] == "B_CLEARS_C_AGREES"
    assert result["number_to_quote"] == pytest.approx(0.020)


def test_route_b_main_never_calls_route_c(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(BC, "run_b", lambda *_args, **_kwargs: _b_result())

    def forbidden(*_args, **_kwargs):
        raise AssertionError("route C leaked into route B execution")

    monkeypatch.setattr(BC, "run_c", forbidden)
    assert BC.main(["prog", "unused.db", "--route", "B", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "bc_reladder_b.json").exists()
    assert not (tmp_path / "bc_reladder_c.json").exists()


def test_console_output_is_ascii(capsys) -> None:
    BC._print_b(BC.build_b_report(_b_result()))
    capsys.readouterr().out.encode("ascii")


def test_wrong_era_dies_before_any_database_open(tmp_path) -> None:
    with pytest.raises(ValueError, match="era B only"):
        BC.run_b(tmp_path / "must-not-open.db", era="A")
    with pytest.raises(ValueError, match="era B only"):
        BC.run_c(tmp_path / "must-not-open.db", era="A")


def test_mutation_promoting_c_to_the_quote_path_turns_red() -> None:
    """Mutation gate: B assignment -> C assignment must be rejected.

    This is the requested guard separating "run both" from "take two shots".
    It mutates the real function source in memory; no production file is edited.
    """
    source = inspect.getsource(BC.decision_rule)
    mutant = source.replace("number_to_quote = b\n", "number_to_quote = c\n", 1)
    assert mutant != source, "mutation site disappeared"
    namespace = {
        "PASS_LINE": BC.PASS_LINE, "PASS_LINE_NOTE": BC.PASS_LINE_NOTE,
        "BLIND_SPOT": BC.BLIND_SPOT, "ROUTE_B": BC.ROUTE_B,
    }
    exec(mutant, namespace)
    with pytest.raises(AssertionError, match="route C leaked"):
        namespace["decision_rule"](0.020, [0.015, 0.025], 0.019, [0.014, 0.024])


def test_reports_serialize_without_promoting_c() -> None:
    events, controls = _regression_frames()
    stat = BC._cluster_bootstrap_c(events, controls, n_boot=100)
    result = {
        "db": "mode=ro fixture", "era": "B",
        "since_utc": "2026-08-18T00:00:00Z",
        "until_utc": "2026-08-25T23:59:59Z", "db_max_snap_utc": None,
        "sessions_used": list(SESSIONS), "events": events,
        "controls": controls, "stat": stat,
    }
    report = BC.build_c_report(result)
    encoded = json.dumps(BC._jsonable_report(report))
    assert "FALSIFICATION ONLY" in encoded
    assert report["design"]["cannot_be_promoted"] is True
    assert report["funnel"]["n_outside_local_rectangular_support"] == 1

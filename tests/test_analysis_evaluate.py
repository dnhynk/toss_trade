"""평가·리포트 검증 — 소유: W3.

중점: 검증질문 6개의 계약 준수, RVOL 게이트 오염 차단(A1 §6), 랭킹 미가용 정상경로(A2 §4),
비용 내역 분해(A2 §5).
"""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest

from tests import synth
from tossmon.analysis import baselines as B
from tossmon.analysis import evaluate as E
from tossmon.analysis import features as F
from tossmon.analysis import labeling as L
from tossmon.analysis import report as R

TOSS = "TOSS_SECURITIES_TRADING_AMOUNT"


def _pipeline(counts: dict[str, int] | None = None, seed: int = 2):
    """합성 번들 → 이벤트 + 피처(대조표본 포함)."""
    counts = counts or {"coil_pop": 2, "instant": 1, "fade": 1, "dump": 1, "noise": 2,
                        "daymarket": 1, "halt_gap": 1}
    bundle = synth.make_dataset(counts, seed=seed)
    evs, fts = [], []
    for t in bundle.truths:
        sym = t["symbol"]
        d = bundle.df_1m[bundle.df_1m["symbol"] == sym]
        curve = B.minute_of_session_volume_curve(d, t["baseline_calendar"])
        rv = B.rvol_series(d, curve, calendar=t["calendar"])
        base = B.compute_daily_baseline(t["df_1d"])
        ev = L.detect_events(d, L.EventParams(), calendar=t["calendar"], rvol_series=rv,
                             prev_close_u=t["prev_close_u"],
                             shares_outstanding_qu=t["shares_outstanding_qu"],
                             rankings=t["rankings"], ranking_type=TOSS)
        md = t["market_day"]
        ev = ev[(ev["t0_ms"] >= md.day.start_ms) & (ev["t0_ms"] < md.after.end_ms)]
        evs.append(ev)
        t0 = int(ev.iloc[0]["t0_ms"]) if len(ev) else md.regular.start_ms + 200 * 60_000
        f = F.extract_precursor_features(d, t["rankings"], t0, curve=curve,
                                         calendar=t["calendar"], baseline=base,
                                         symbol=sym,
                                         shares_outstanding_qu=t["shares_outstanding_qu"])
        f["symbol"] = sym
        f["is_event"] = 1.0 if len(ev) else 0.0
        fts.append(f)
    return bundle, pd.concat(evs, ignore_index=True), pd.DataFrame(fts)


@pytest.fixture(scope="module")
def pipe():
    return _pipeline()


# --------------------------------------------------------------------------- #
# 게이트 규약 (A1 §6)
# --------------------------------------------------------------------------- #
def test_every_result_declares_gate_policy(pipe) -> None:
    bundle, events, feats = pipe
    res = E.run_all(events, feats, bundle.rankings, bundle.df_1m)
    assert set(res) == {"q1_volume_leadtime", "q2_ranking_lead_lag",
                        "q3_daymarket_persistence", "q4_dump_speed", "q5_expectancy",
                        "q6_time_of_day", "base_rates", "sample_filter"}
    for name, df in res.items():
        if name == "sample_filter":
            # 표본 필터는 게이트 **이전** 단계라 gate_policy 를 달지 않는다
            assert {"reason", "n", "n_total", "n_kept"} <= set(df.columns)
            continue
        assert "gate_policy" in df.columns, name
        assert "n_ungated_excluded" in df.columns, name
        assert (df["gate_policy"] == "exclude").all(), name


def test_ungated_events_are_excluded_by_default(pipe) -> None:
    """게이트 미적용 이벤트가 통계를 조용히 오염시키면 안 된다 (A1 §6)."""
    _bundle, events, feats = pipe
    poison = events.copy()
    poison["rvol_gated"] = False
    poison["ret_close"] = 99.0                     # 섞이면 즉시 드러나는 값
    mixed = pd.concat([events, poison], ignore_index=True)

    clean = E.q3_daymarket_persistence(events)
    got = E.q3_daymarket_persistence(mixed)
    assert got.loc[0, "median_ret_close"] == clean.loc[0, "median_ret_close"]
    assert got.loc[0, "n_ungated_excluded"] == len(poison)
    assert got.loc[0, "n"] == clean.loc[0, "n"]


def test_gate_separate_includes_everything(pipe) -> None:
    _bundle, events, _feats = pipe
    poison = events.copy()
    poison["rvol_gated"] = False
    mixed = pd.concat([events, poison], ignore_index=True)
    sep = E.q3_daymarket_persistence(mixed, gate="separate")
    assert sep.loc[0, "n"] == float(len(mixed))
    assert sep.loc[0, "n_ungated_excluded"] == len(poison)
    assert (sep["gate_policy"] == "separate").all()


# --------------------------------------------------------------------------- #
# q1
# --------------------------------------------------------------------------- #
def test_q1_leadtime_and_precision_recall(pipe) -> None:
    _bundle, events, feats = pipe
    q1 = E.q1_volume_leadtime(events, feats)
    assert "vol_surge_lead_min" in set(q1["metric"])
    pr = q1[q1["metric"].str.startswith("precision_recall")]
    assert len(pr) == len(F.RVOL_CROSS_THRESHOLDS)
    assert pr["precision"].notna().all(), "is_event 대조표본이 있으면 정밀도가 계산돼야 한다"
    assert ((pr["precision"] >= 0) & (pr["precision"] <= 1)).all()
    assert ((pr["recall"] >= 0) & (pr["recall"] <= 1)).all()
    lead = q1[q1["metric"] == "rvol_first_cross_3_lead_min"].iloc[0]
    assert 0 <= lead["detect_rate"] <= 1


def test_q1_without_controls_marks_no_controls(pipe) -> None:
    _bundle, events, feats = pipe
    q1 = E.q1_volume_leadtime(events, feats.drop(columns=["is_event"]))
    pr = q1[q1["metric"].str.startswith("precision_recall")]
    assert (pr["note"] == "no_controls").all()
    assert pr["precision"].isna().all()


def test_q1_precision_is_perfect_on_synthetic_separation(pipe) -> None:
    """합성데이터에서는 노이즈의 사전 RVOL 이 낮으므로 오탐이 없어야 한다."""
    _bundle, events, feats = pipe
    q1 = E.q1_volume_leadtime(events, feats)
    row = q1[q1["metric"] == "precision_recall@rvol_at_cutoff>=3"].iloc[0]
    assert row["fp"] == 0.0
    assert row["recall"] > 0.4, "coil 형은 사전 RVOL 로 잡혀야 한다"


def test_q1_empty_inputs() -> None:
    q1 = E.q1_volume_leadtime(pd.DataFrame(columns=L.EVENT_COLUMNS), pd.DataFrame())
    assert len(q1) > 0
    assert (q1["n_events"] == 0).all()


# --------------------------------------------------------------------------- #
# q2 — 랭킹 미가용이 정상 경로 (A2 §4)
# --------------------------------------------------------------------------- #
def test_q2_empty_rankings_is_normal_path(pipe) -> None:
    _bundle, events, _feats = pipe
    for empty in (pd.DataFrame(), None):
        q2 = E.q2_ranking_lead_lag(events, empty)
        assert len(q2) == 1
        assert q2.loc[0, "available"] is False or q2.loc[0, "available"] == False  # noqa: E712
        assert q2.loc[0, "verdict"] == "unavailable"
        assert "과거 조회 불가" in q2.loc[0, "note"]
        assert math.isnan(q2.loc[0, "lead_share"])


def test_q2_lead_lag_per_type(pipe) -> None:
    bundle, events, _feats = pipe
    q2 = E.q2_ranking_lead_lag(events, bundle.rankings)
    assert set(q2["ranking_type"]) == set(synth.RANK_TYPES)
    for _i, row in q2.iterrows():
        assert row["available"]
        assert 0 <= row["entry_rate"] <= 1
        assert row["verdict"] in ("lead", "lag", "mixed")
        assert row["lead_share"] + row["lag_share"] <= 1.0 + 1e-9


def test_q2_detects_lag_when_rankings_follow_price() -> None:
    """랭킹이 T0 뒤에만 등장하면 verdict='lag' (Barber 2022 해석 경로)."""
    bundle, events, _feats = _pipeline({"instant": 3, "dump": 2}, seed=3)
    q2 = E.q2_ranking_lead_lag(events, bundle.rankings)
    toss = q2[q2["ranking_type"] == TOSS].iloc[0]
    assert toss["lag_share"] > 0.6
    assert toss["verdict"] == "lag"
    assert toss["leadlag_median"] > 0


def test_q2_detects_lead_when_rankings_precede_price() -> None:
    bundle, events, _feats = _pipeline({"coil_pop": 3, "halt_gap": 2}, seed=3)
    q2 = E.q2_ranking_lead_lag(events, bundle.rankings)
    toss = q2[q2["ranking_type"] == TOSS].iloc[0]
    assert toss["lead_share"] > 0.6
    assert toss["verdict"] == "lead"
    assert toss["leadlag_median"] < 0


# --------------------------------------------------------------------------- #
# q3
# --------------------------------------------------------------------------- #
def test_q3_sessions_and_persistence(pipe) -> None:
    _bundle, events, _feats = pipe
    q3 = E.q3_daymarket_persistence(events)
    assert q3.loc[0, "session"] == "all"
    assert q3.loc[0, "n"] == float(len(events))
    sessions = set(q3["session"])
    assert "day" in sessions or "pre" in sessions
    for _i, row in q3.iterrows():
        if row["n"] > 0:
            assert 0 <= row["persist_rate"] <= 1
            assert 0 <= row["closed_below_vwap_share"] <= 1


def test_q3_daymarket_does_not_persist() -> None:
    """데이마켓 급등은 정규장까지 못 버틴다 (합성 시나리오의 설계 의도)."""
    _bundle, events, _feats = _pipeline({"daymarket": 3}, seed=4)
    q3 = E.q3_daymarket_persistence(events)
    day = q3[q3["session"] == "day"].iloc[0]
    assert day["n"] == 3.0
    assert day["persist_rate"] == 0.0
    assert day["median_ret_close"] < 0


def test_q3_empty() -> None:
    q3 = E.q3_daymarket_persistence(pd.DataFrame(columns=L.EVENT_COLUMNS))
    assert q3.loc[0, "n"] == 0.0


# --------------------------------------------------------------------------- #
# q4
# --------------------------------------------------------------------------- #
def test_q4_dump_speed_and_censoring(pipe) -> None:
    bundle, events, _feats = pipe
    q4 = E.q4_dump_speed(events, bundle.df_1m)
    row20 = q4[q4["metric"] == "peak_to_-20pct"].iloc[0]
    assert row20["n"] == float(len(events))
    assert row20["reached"] <= row20["n"]
    assert 0 <= row20["reach_rate"] <= 1
    assert row20["minutes_n"] == row20["reached"], "미도달은 분포에서 제외(우측 절단)"
    assert row20["horizon_min"] == 390.0
    halt = q4[q4["metric"] == "halt_gaps"].iloc[0]
    assert 0 <= halt["halt_any_share"] <= 1


def test_q4_horizon_bounds_the_search() -> None:
    """지평을 좁히면 도달률이 떨어진다 (다음날 하락이 섞이지 않는다는 증거)."""
    bundle, events, _feats = _pipeline({"coil_pop": 2, "dump": 2}, seed=5)
    narrow = E.q4_dump_speed(events, bundle.df_1m, horizon_min=10)
    wide = E.q4_dump_speed(events, bundle.df_1m, horizon_min=10_000)
    n_narrow = narrow[narrow["metric"] == "peak_to_-20pct"].iloc[0]["reached"]
    n_wide = wide[wide["metric"] == "peak_to_-20pct"].iloc[0]["reached"]
    assert n_narrow <= n_wide


def test_q4_dump_scenario_collapses_fast() -> None:
    bundle, events, _feats = _pipeline({"dump": 3}, seed=6)
    q4 = E.q4_dump_speed(events, bundle.df_1m)
    row = q4[q4["metric"] == "peak_to_-20pct"].iloc[0]
    assert row["reach_rate"] == 1.0
    assert row["minutes_median"] <= 10, "문헌: 피크 후 수 분 내 붕괴"


def test_q4_no_candles() -> None:
    _bundle, events, _feats = _pipeline({"dump": 1}, seed=7)
    q4 = E.q4_dump_speed(events, pd.DataFrame())
    assert (q4["n"] == 0).all()


# --------------------------------------------------------------------------- #
# q5 — 비용 분해 (A2 §5)
# --------------------------------------------------------------------------- #
def test_q5_cost_decomposition_sums_to_total(pipe) -> None:
    _bundle, events, feats = pipe
    q5 = E.q5_expectancy(events, feats)
    row = q5.iloc[0]
    assert row["cost_roundtrip"] == 0.01
    assert row["cost_commission"] == 0.002, "US 0.1%/체결 → 왕복 0.2%"
    total = row["cost_commission"] + row["cost_fx"] + row["cost_slippage"]
    assert total == pytest.approx(row["cost_roundtrip"])


def test_q5_net_is_gross_minus_cost(pipe) -> None:
    _bundle, events, feats = pipe
    q5 = E.q5_expectancy(events, feats, 0.01)
    for _i, r in q5[q5["n"] > 0].iterrows():
        assert r["mean_net"] == pytest.approx(r["mean_gross"] - 0.01)
        assert r["median_net"] == pytest.approx(r["median_gross"] - 0.01)


def test_q5_higher_cost_reduces_expectancy(pipe) -> None:
    _bundle, events, feats = pipe
    cheap = E.q5_expectancy(events, feats, 0.002).iloc[0]
    dear = E.q5_expectancy(events, feats, 0.05, cost_commission=0.002,
                           cost_fx=0.003).iloc[0]
    assert dear["mean_net"] < cheap["mean_net"]
    assert dear["cost_slippage"] == pytest.approx(0.045)


def test_q5_flags_inconsistent_cost_breakdown(pipe) -> None:
    _bundle, events, feats = pipe
    q5 = E.q5_expectancy(events, feats, 0.001, cost_commission=0.002, cost_fx=0.003)
    assert (q5["note"] == "cost_commission+cost_fx > cost_roundtrip").all()
    assert (q5["cost_slippage"] == 0.0).all()


def test_q5_policies_and_buckets(pipe) -> None:
    _bundle, events, feats = pipe
    q5 = E.q5_expectancy(events, feats)
    assert set(q5["policy"]) == set(E.EXIT_POLICIES)
    assert "all" in set(q5["bucket"])
    assert len(set(q5["bucket"])) > 1, "스코어 버킷이 생성돼야 한다"
    assert (q5["score_col"] != "").all()


def test_q5_uses_explicit_score_column(pipe) -> None:
    _bundle, events, feats = pipe
    f = feats.copy()
    f["score"] = f["rvol_at_cutoff"]
    q5 = E.q5_expectancy(events, f, score_col="score")
    assert (q5["score_col"] == "score").all()


def test_q5_empty() -> None:
    q5 = E.q5_expectancy(pd.DataFrame(columns=L.EVENT_COLUMNS), pd.DataFrame())
    assert set(q5["policy"]) == set(E.EXIT_POLICIES)
    assert (q5["n"] == 0).all()


# --------------------------------------------------------------------------- #
# q6
# --------------------------------------------------------------------------- #
def test_q6_buckets_cover_all_events(pipe) -> None:
    _bundle, events, _feats = pipe
    q6 = E.q6_time_of_day(events)
    assert list(q6["bucket"]) == [b for b, _lo, _hi in E.TOD_BUCKETS]
    assert q6["n"].sum() == float(len(events)), "모든 이벤트가 정확히 한 버킷에 속해야 한다"


def test_q6_pre_and_day_events_land_before_open() -> None:
    _bundle, events, _feats = _pipeline({"fade": 2, "daymarket": 2}, seed=8)
    q6 = E.q6_time_of_day(events)
    pre = q6[q6["bucket"] == "pre_or_day"].iloc[0]
    assert pre["n"] == 4.0


def test_q6_hod_shares_are_fractions(pipe) -> None:
    _bundle, events, _feats = pipe
    q6 = E.q6_time_of_day(events)
    for _i, r in q6[q6["n"] > 0].iterrows():
        assert 0 <= r["hod_within_15min_share"] <= 1
        assert 0 <= r["hod_before_1000et_share"] <= 1
        assert r["hod_within_15min_share"] <= r["hod_before_1000et_share"] + 1e-9


def test_q6_empty() -> None:
    q6 = E.q6_time_of_day(pd.DataFrame(columns=L.EVENT_COLUMNS))
    assert (q6["n"] == 0).all()


# --------------------------------------------------------------------------- #
# 기저율 대조
# --------------------------------------------------------------------------- #
def test_base_rate_table(pipe) -> None:
    _bundle, events, _feats = pipe
    br = E.base_rate_comparison(events)
    assert set(br["base_rate"]) == set(E.KNOWN_BASE_RATES)
    for _i, r in br.iterrows():
        assert r["known"] == E.KNOWN_BASE_RATES[r["base_rate"]]
        if r["observed"] == r["observed"]:
            assert 0 <= r["observed"] <= 1
            assert r["delta"] == pytest.approx(r["observed"] - r["known"])


def test_base_rate_fade_dominates_on_fade_scenarios() -> None:
    _bundle, events, _feats = _pipeline({"fade": 3, "dump": 2}, seed=9)
    br = E.base_rate_comparison(events).set_index("base_rate")
    assert br.loc["fade_rate", "observed"] == 1.0
    assert br.loc["close_below_vwap", "observed"] == 1.0


# --------------------------------------------------------------------------- #
# 결합 유틸
# --------------------------------------------------------------------------- #
def test_join_events_features(pipe) -> None:
    _bundle, events, feats = pipe
    j = E.join_events_features(events, feats)
    assert len(j) == len(events)
    assert "rvol_at_cutoff" in j.columns
    assert j["rvol_at_cutoff"].notna().any()
    # 중복 컬럼(t0_ms 등)이 _x/_y 로 갈라지지 않아야 한다
    assert not [c for c in j.columns if c.endswith(("_x", "_y"))]


def test_join_without_features(pipe) -> None:
    _bundle, events, _feats = pipe
    assert len(E.join_events_features(events, pd.DataFrame())) == len(events)
    assert E.join_events_features(pd.DataFrame(), pd.DataFrame()).empty


# --------------------------------------------------------------------------- #
# 리포트
# --------------------------------------------------------------------------- #
def test_render_report_contains_all_sections(pipe) -> None:
    bundle, events, feats = pipe
    sections = E.run_all(events, feats, bundle.rankings, bundle.df_1m)
    md = R.render_report(sections, t_from_ms=1_780_000_000_000,
                         t_to_ms=1_780_600_000_000, events=events)
    for key in sections:
        title = R.SECTION_META[key][0]
        assert title in md, key
    assert md.startswith("# ")
    assert "왕복 수수료 0.2%" in md, "비용 혼동 방지 문구가 있어야 한다 (A2 §5)"
    assert "한계와 알려진 편향" in md
    assert "docs/07_analysis_spec.md" in md
    assert f"이벤트 수: **{len(events)}**" in md
    assert "|---" in md, "마크다운 표가 렌더링돼야 한다"


def test_render_report_handles_empty_sections() -> None:
    empty = E.run_all(pd.DataFrame(columns=L.EVENT_COLUMNS), pd.DataFrame(),
                      pd.DataFrame(), pd.DataFrame())
    md = R.render_report(empty, t_from_ms=0, t_to_ms=86_400_000)
    assert "이벤트 수: **0**" in md
    assert "과거 조회 불가" in md


def test_df_to_markdown_formats_nan_and_bools() -> None:
    df = pd.DataFrame([{"a": float("nan"), "b": True, "c": 1234.5678, "d": "x",
                        "e": 1_780_000_000_000.0}])
    md = R.df_to_markdown(df)
    assert "| - |" in md
    assert "yes" in md
    assert "1,235" in md
    assert "1780000000000" in md
    assert R.df_to_markdown(pd.DataFrame()) == "_(데이터 없음)_"


def test_generate_report_survives_missing_db(tmp_path: Path) -> None:
    """Reader 가 미구현이거나 DB 가 비어도 리포트 생성은 죽지 않아야 한다."""
    out = tmp_path / "sub" / "report.md"
    got = R.generate_report(tmp_path / "nope.db", out, 0, 86_400_000)
    assert got == out and out.exists()
    text = out.read_text(encoding="utf-8")
    assert "이벤트 수: **0**" in text
    assert "빈 리포트" in text or "사용할 수 없어" in text
    assert "과거 조회 불가" in text, "Q2 는 랭킹 미가용으로 명시돼야 한다"


def test_expand_meta_json_restores_extra_labels() -> None:
    """C-6 events 테이블은 7컬럼 + meta_json 뿐 — 추가 라벨은 meta 에서 복원돼야 한다."""
    ev = pd.DataFrame([{"symbol": "A", "t0_ms": 1000, "kind": "win", "peak_ms": 2000,
                        "peak_ret": 0.2, "ret_30m": 0.1, "ret_close": -0.1,
                        "session": "regular",
                        "meta_json": '{"t0_min_from_open": 5, "rvol_gated": true,'
                                     ' "hod_ms": 2000, "session": "IGNORED"}'}])
    out = R.expand_meta_json(ev)
    assert out.loc[0, "t0_min_from_open"] == 5
    assert bool(out.loc[0, "rvol_gated"]) is True
    assert out.loc[0, "session"] == "regular", "기존 컬럼은 덮어쓰지 않는다"
    # 깨진 JSON·빈 값에도 죽지 않아야 한다
    broken = pd.DataFrame([{"t0_ms": 1, "meta_json": "{not json"},
                           {"t0_ms": 2, "meta_json": None}])
    assert len(R.expand_meta_json(broken)) == 2
    assert R.expand_meta_json(pd.DataFrame()).empty


def test_generate_report_end_to_end_through_real_store(tmp_path: Path) -> None:
    """W2 Store 에 실제로 적재 → Reader 로 읽어 리포트 생성 (통합 경로)."""
    from tossmon.api.models import Candle
    from tossmon.store.writer import Store

    bundle, events, _feats = _pipeline({"coil_pop": 1, "dump": 1}, seed=11)
    assert len(events) >= 2

    db = tmp_path / "t.db"
    store = Store(db)
    try:
        for sym, g in bundle.df_1m.groupby("symbol"):
            store.upsert_candles_1m(
                Candle(symbol=str(sym), ts_ms=int(r.ts_ms), open_u=int(r.open_u),
                       high_u=int(r.high_u), low_u=int(r.low_u), close_u=int(r.close_u),
                       vol_qu=int(r.vol_qu)) for r in g.itertuples())
        for _i, e in events.iterrows():
            base = {k: e[k] for k in ("symbol", "t0_ms", "kind", "peak_ms", "peak_ret",
                                      "ret_30m", "ret_close", "session")}
            base["meta_json"] = {k: (None if pd.isna(e[k]) else
                                     (bool(e[k]) if isinstance(e[k], bool) else e[k]))
                                 for k in ("t0_min_from_open", "hod_ms", "rvol_gated",
                                           "retrace_close", "halt_gap_count")}
            base["t0_ms"] = int(base["t0_ms"])
            base["peak_ms"] = int(base["peak_ms"])
            store.record_event(base)
    finally:
        store.close()

    lo = int(bundle.df_1m["ts_ms"].min()) - 1
    hi = int(bundle.df_1m["ts_ms"].max()) + 1
    out = R.generate_report(db, tmp_path / "r.md", lo, hi)
    text = out.read_text(encoding="utf-8")
    assert f"이벤트 수: **{len(events)}**" in text
    for key in R.SECTION_META:
        assert R.SECTION_META[key][0] in text
    assert "Q6. 시간대 효과" in text
    # meta_json 복원이 되면 q6 버킷에 이벤트가 실제로 분류된다
    assert "pre_or_day" in text


def test_report_cli_range_resolution() -> None:
    import tools.report as cli

    args = cli.build_parser().parse_args(["--db", "x.db", "--out", "y.md",
                                          "--now-ms", "1000000000", "--days", "2"])
    lo, hi = cli.resolve_range(args)
    assert hi == 1_000_000_000
    assert lo == hi - 2 * cli.DAY_MS

    args2 = cli.build_parser().parse_args(["--db", "x", "--out", "y",
                                           "--from-ms", "5", "--to-ms", "9"])
    assert cli.resolve_range(args2) == (5, 9)

    bad = cli.build_parser().parse_args(["--db", "x", "--out", "y",
                                        "--from-ms", "9", "--to-ms", "5"])
    with pytest.raises(SystemExit):
        cli.resolve_range(bad)


def test_report_cli_missing_db_returns_error_code() -> None:
    import tools.report as cli
    assert cli.main(["--db", "definitely_missing.db", "--out", "o.md"]) == 2

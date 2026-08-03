"""회전 Q0·Q1 (docs/26) — 순환 금지·홀드아웃 봉인·중단 기준의 정직성.

## 이 파일이 지키는 것

1. **달러 점유율을 쓰지 않는다.** `vol_qu x last_u` 는 가격을 품고 있어 수익률
   예측에 쓰면 **순환**이다. 소스에 그 조합이 없음을 강제한다.
2. **홀드아웃이 새지 않는다.** `candles_1m` 은 백필이라 봉인 구간을 담고 있다.
   러너가 `drop_holdout` 을 지나가고 **버린 수를 싣는지** 확인한다.
3. **lag 부호 규약** — `lag > 0` 은 "점유율이 먼저"다. 부호가 뒤집히면 결론이
   정반대가 되므로 합성 자료로 고정한다.
4. **중단 기준이 코드로 집행된다.** 선행 칸이 위약을 못 이기면 `proceed_to_q2` 는
   반드시 `False` 다.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3

import numpy as np
import pandas as pd
import pytest

from tossmon.analysis import session as SS
from tossmon.analysis import shots as S
from tossmon.analysis.measure import rotation_q1 as RQ

U = 1_000_000
MIN = RQ.MINUTE_MS
BASE = 1_785_500_000_000 - (1_785_500_000_000 % MIN)      # regular session, minute-aligned


# --------------------------------------------------------------------------- #
# 1. 순환 금지 — 달러 점유율을 쓰지 않는다
# --------------------------------------------------------------------------- #
def test_dollar_share_is_never_used():
    """`vol_qu x last_u` 는 가격을 품는다 — 수익률 예측에 쓰면 순환이다."""
    src = pathlib.Path(RQ.__file__).read_text(encoding="utf-8")
    for forbidden in ("last_u", "amount_u", "dollar_share", "notional_share"):
        assert forbidden not in src.split('"""', 2)[2], forbidden


def test_share_is_a_quantity_ratio_within_one_minute():
    cd = _candles([("A", 0, 100.0, 10.0), ("B", 0, 100.0, 30.0),
                   ("C", 0, 100.0, 30.0), ("D", 0, 100.0, 20.0),
                   ("E", 0, 100.0, 10.0)])
    p = RQ.build_panel(cd)
    assert float(p[p["symbol"] == "A"]["share"].iloc[0]) == pytest.approx(0.10)


# --------------------------------------------------------------------------- #
# 2. 홀드아웃 봉인
# --------------------------------------------------------------------------- #
def test_holdout_window_matches_the_preregistration():
    assert SS.HOLDOUT_START == "2026-05-01" and SS.HOLDOUT_END == "2026-07-29"


def test_holdout_rows_are_dropped_and_counted():
    """조용히 거르면 봉인이 지켜졌는지 확인할 방법이 없다."""
    inside = int(pd.Timestamp("2026-07-28T18:00:00").value // 1_000_000) - SS.KST_OFFSET_MS
    outside = int(pd.Timestamp("2026-07-31T18:00:00").value // 1_000_000) - SS.KST_OFFSET_MS
    df = pd.DataFrame({"ts_ms": [inside, outside]})
    got = SS.drop_holdout(df)
    assert got["n_dropped"] == 1 and got["n_kept"] == 1


def test_a_date_just_after_the_holdout_survives():
    ts = int(pd.Timestamp("2026-07-30T18:00:00").value // 1_000_000) - SS.KST_OFFSET_MS
    assert SS.is_holdout(ts) is False


def test_a_date_just_inside_the_holdout_end_is_sealed():
    ts = int(pd.Timestamp("2026-07-29T18:00:00").value // 1_000_000) - SS.KST_OFFSET_MS
    assert SS.is_holdout(ts) is True


# --------------------------------------------------------------------------- #
# 3. lag 부호 규약 — 뒤집히면 결론이 정반대가 된다
# --------------------------------------------------------------------------- #
def _candles(rows) -> pd.DataFrame:
    df = pd.DataFrame([{"symbol": s, "ts_ms": BASE + m * MIN,
                        "close_u": int(px * U), "vol_qu": v}
                       for s, m, px, v in rows])
    df["session"] = SS.sessions_of(df["ts_ms"])
    df["cycle_date"] = df["ts_ms"].map(lambda m: SS.session_date(int(m)))
    return df


def test_positive_lag_means_share_leads_price():
    """**이 검사가 결론의 부호를 지킨다.**

    1분에 점유율이 뛰고 2분에 가격이 오르는 자료를 만든다. 점유율이 **먼저**이므로
    `lag=+1` 에서 짝이 맺혀야 하고 `lag=-1` 에서는 안 맺혀야 한다.
    """
    rows = []
    for m in range(6):
        for sym in ("A", "B", "C", "D", "E"):
            bump = 40.0 if (sym == "A" and m == 1) else 10.0
            px = 1.10 if (sym == "A" and m >= 2) else 1.00
            rows.append((sym, m, px, bump))
    p = RQ.build_panel(_candles(rows))
    up = RQ.lagged_pairs(p, +1)
    a_lead = up[(up["symbol"] == "A") & (up["ts_ms"] == BASE + 1 * MIN)]
    assert len(a_lead) == 1
    assert float(a_lead["dshare"].iloc[0]) > 0        # 1분에 점유율 상승
    assert float(a_lead["ret"].iloc[0]) > 0           # 2분에 가격 상승 -> 선행


def test_negative_lag_pairs_share_with_an_earlier_return():
    rows = [(sym, m, 1.0, 10.0) for m in range(4)
            for sym in ("A", "B", "C", "D", "E")]
    p = RQ.build_panel(_candles(rows))
    down = RQ.lagged_pairs(p, -1)
    if len(down):
        # lag=-1 은 dshare(t) 를 ret(t-1) 과 짝짓는다
        assert (down["ts_ms"] >= BASE).all()


def test_lag_range_is_symmetric_around_zero():
    assert min(RQ.LAGS) == -5 and max(RQ.LAGS) == 5 and 0 in RQ.LAGS


# --------------------------------------------------------------------------- #
# 4. 위약 — 같은 분 안에서 라벨만 치환
# --------------------------------------------------------------------------- #
def test_placebo_preserves_the_within_minute_value_multiset():
    pairs = pd.DataFrame({"symbol": list("ABCDE"), "ts_ms": [BASE] * 5,
                          "dshare": [0.1, 0.2, 0.3, -0.1, -0.5],
                          "ret": [0.0] * 5})
    got = RQ.placebo_pairs(pairs, seed=7)
    assert sorted(got["dshare"].tolist()) == sorted(pairs["dshare"].tolist())


def test_placebo_does_not_move_values_across_minutes():
    pairs = pd.DataFrame({"symbol": list("ABCDEABCDE"),
                          "ts_ms": [BASE] * 5 + [BASE + MIN] * 5,
                          "dshare": [1.0] * 5 + [2.0] * 5, "ret": [0.0] * 10})
    got = RQ.placebo_pairs(pairs, seed=3)
    assert set(got[got["ts_ms"] == BASE]["dshare"]) == {1.0}


# --------------------------------------------------------------------------- #
# 5. 상관 산수
# --------------------------------------------------------------------------- #
def test_correlation_from_sufficient_statistics_matches_numpy():
    rng = np.random.default_rng(1)
    x, y = rng.normal(size=300), rng.normal(size=300)
    keys = rng.integers(0, 20, size=300)
    st = RQ.minute_stats(x, y, keys)
    assert RQ.corr_from_stats(st) == pytest.approx(float(np.corrcoef(x, y)[0, 1]))


def test_correlation_is_nan_without_variation():
    st = RQ.minute_stats(np.ones(10), np.arange(10.0), np.zeros(10))
    assert RQ.corr_from_stats(st) != RQ.corr_from_stats(st)


# --------------------------------------------------------------------------- #
# 6. 중단 기준이 코드로 집행된다
# --------------------------------------------------------------------------- #
def _asym(session="regular", **over):
    base = {"session": session, "k": 1, "n_minutes": 500, "corr_lead": 0.02,
            "corr_lag": 0.02, "asymmetry": 0.0, "ci_low": -0.01, "ci_high": 0.01,
            "ci_low_bonferroni": -0.02, "ci_high_bonferroni": 0.02,
            "n_comparisons": 5, "verdict": "symmetric"}
    base.update(over)
    return base


def test_gate_stops_when_lead_and_lag_are_symmetric():
    """**대칭이면 선행이 아니다.** 동시 관계는 거래 대상이 아니다."""
    g = RQ.q1_gate([], [_asym(k=k) for k in RQ.ASYMMETRY_K])
    assert g["proceed_to_q2"] is False
    assert g["by_session"]["regular"]["decision"] == "stop"
    assert "SIMULTANEOUS" in g["by_session"]["regular"]["reason"].upper()


def test_gate_is_not_fooled_by_a_big_symmetric_peak():
    """**이 검사가 코디네이터가 잡은 결함을 고정한다.**

    lag -1/0/+1 이 나란히 크면 구 규칙("선행 셀이 하나라도 0 초과")은 통과시켰고,
    자료가 늘자 답이 뒤집혔다. 비대칭 지표는 공통 성분이 상쇄되므로 0 이어야 한다.
    """
    rows = [_asym(k=k, corr_lead=0.20, corr_lag=0.20, asymmetry=0.0)
            for k in RQ.ASYMMETRY_K]
    g = RQ.q1_gate([], rows)
    assert g["proceed_to_q2"] is False
    assert g["by_session"]["regular"]["max_asymmetry"] == pytest.approx(0.0)


def test_gate_proceeds_only_when_the_lead_side_is_stronger():
    rows = [_asym(k=k) for k in RQ.ASYMMETRY_K]
    rows[1] = _asym(k=2, corr_lead=0.20, corr_lag=0.02, asymmetry=0.18,
                    ci_low=0.10, ci_high=0.26,
                    ci_low_bonferroni=0.05, ci_high_bonferroni=0.31,
                    verdict="lead_stronger")
    g = RQ.q1_gate([], rows)
    assert g["proceed_to_q2"] is True
    assert g["by_session"]["regular"]["max_asymmetry_k"] == 2


def test_gate_uses_the_bonferroni_ci_not_the_raw_one():
    """보정 전 CI 로는 0 을 넘어도 보정 후 넘지 못하면 진행하지 않는다."""
    rows = [_asym(k=k) for k in RQ.ASYMMETRY_K]
    rows[0] = _asym(k=1, asymmetry=0.03, ci_low=0.005, ci_high=0.055,
                    ci_low_bonferroni=-0.004, ci_high_bonferroni=0.064,
                    verdict="symmetric")
    assert RQ.q1_gate([], rows)["proceed_to_q2"] is False


def test_gate_ignores_underpowered_cells():
    rows = [_asym(k=1, asymmetry=0.9, ci_low_bonferroni=0.5,
                  verdict="insufficient")]
    g = RQ.q1_gate([], rows)
    assert g["proceed_to_q2"] is False
    assert g["by_session"]["regular"]["decision"] == "insufficient"


def test_asymmetry_is_zero_on_a_perfectly_symmetric_panel():
    """합성 대칭 자료에서 지표가 0 근처여야 한다 — 지표 자체의 건전성 검사."""
    rng = np.random.default_rng(5)
    rows = []
    for m in range(80):
        for sym in "ABCDEF":
            rows.append((sym, m, 1.0 + 0.001 * rng.normal(),
                         10.0 + rng.normal()))
    p = RQ.build_panel(_candles(rows))
    got = [x for x in RQ.asymmetry_test(p, ks=(1,))
           if x["session"] == "regular" and x["n_minutes"] > 0]
    if got and got[0]["asymmetry"] == got[0]["asymmetry"]:
        assert abs(got[0]["asymmetry"]) < 0.5


def test_economic_significance_reports_bp_against_measured_cost():
    """유의성만으로는 무의미하다 — bp 로 환산해 실측 비용과 나란히 놓는다."""
    rows = []
    for m in range(80):
        for i, sym in enumerate("ABCDEF"):
            rows.append((sym, m, 1.0 + 0.001 * i, 10.0 + i))
    e = RQ.economic_significance(RQ.build_panel(_candles(rows)))
    powered = [x for x in e if x.get("powered")]
    for x in powered:
        assert x["round_trip_cost_bp"] == pytest.approx(
            RQ.REALIZED_ROUND_TRIP * 1e4)
        assert x["net_bp"] == pytest.approx(
            x["top_decile_bp_per_min"] - x["round_trip_cost_bp"])


def test_measured_cost_constant_matches_the_tape_measurement():
    """비용 상수는 §10-R.5 실측(0.46% + 수수료 0.2%)에서 온다."""
    assert RQ.EFFECTIVE_SPREAD_REGULAR == pytest.approx(0.0046)
    assert RQ.REALIZED_ROUND_TRIP == pytest.approx(0.0046 + 0.002)


# --------------------------------------------------------------------------- #
# 7. 러너 전체
# --------------------------------------------------------------------------- #
def _tiny_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db = tmp_path / "rot.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE candles_1m (symbol TEXT, ts_ms INTEGER, open_u INTEGER,"
                 " high_u INTEGER, low_u INTEGER, close_u INTEGER, vol_qu REAL)")
    conn.execute("CREATE TABLE rankings_snap (id INTEGER, snap_ms INTEGER, "
                 "ranking_type TEXT, duration TEXT, rank INTEGER, symbol TEXT, "
                 "last_u INTEGER, vol_qu REAL, amount_u REAL)")
    held = int(pd.Timestamp("2026-07-28T18:00:00").value // 1_000_000) - SS.KST_OFFSET_MS
    rows, rk = [], []
    for m in range(40):
        for i, sym in enumerate("ABCDEF"):
            ts = BASE + m * MIN
            px = int((1.0 + 0.001 * ((m + i) % 5)) * U)
            rows.append((sym, ts, px, px, px, px, 10.0 + i + (m % 3)))
            rk.append((None, ts, S.TOSS_VOLUME, "realtime", i + 1, sym, px,
                       100.0 * (i + 1), 0.0))
        rows.append(("Z", held + m * MIN, U, U, U, U, 5.0))     # 봉인 구간 -> 버려야 한다
    conn.executemany("INSERT INTO candles_1m VALUES (?,?,?,?,?,?,?)", rows)
    conn.executemany("INSERT INTO rankings_snap VALUES (?,?,?,?,?,?,?,?,?)", rk)
    conn.commit()
    conn.close()
    return db


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("rq1")
    db = _tiny_db(tmp)
    out = tmp / "out"
    assert RQ.main(db, out_dir=out) == 0
    return json.loads((out / "rotation_q1.json").read_text(encoding="utf-8")), out


def test_runner_reports_the_holdout_accounting(report):
    rep, _ = report
    assert rep["holdout"]["n_rows_dropped"] == 40      # Z 종목 40분이 봉인 구간
    assert rep["holdout"]["start"] == SS.HOLDOUT_START


def test_runner_rows_match_the_manifest_exactly(report):
    rep, _ = report
    assert rep["cross_correlation"]
    for row in rep["cross_correlation"]:
        assert set(row) == set(RQ.REPORTED_FIELDS), row


def test_runner_withholds_pooled_ci_below_the_cluster_floor(report):
    rep, _ = report
    assert rep["pooled_ci_permitted"] == (len(rep["cycle_dates"]) >= 5)


def test_runner_records_the_gate_decision(report):
    rep, _ = report
    assert "proceed_to_q2" in rep["gate"]
    assert isinstance(rep["gate"]["proceed_to_q2"], bool)


def test_runner_states_the_circularity_guard_in_the_payload(report):
    rep, _ = report
    assert "circular" in rep["signal_note"]

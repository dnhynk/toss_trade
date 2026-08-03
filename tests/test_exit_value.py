"""이탈 값어치 러너 (docs/23 §10-P) — 상한은 상한으로만 쓰이는가, 나머지는 정직한가.

## 이 파일이 지키는 것

1. **상한은 미래를 본다** — 그리고 그 사실이 `LOOK_AHEAD_RULES` 에 **명시 등록**되어
   있어야 한다. 등록부는 상한 하나뿐이며, 관측 가능 규칙이 슬쩍 들어오면 깨진다.
2. **나머지 전부 접두사 불변** — 신호 계열(순위·점유율·호가·테이프)까지 잘라도
   판정이 바뀌지 않아야 한다. 상한만 예외이고, **상한은 실제로 이 시험을 깬다**
   (그것이 상한이 상한인 이유다 — 시험이 위반을 표현할 수 있음을 증명한다).
3. **판정 불가를 값으로 채우지 않는다** — 신호가 없으면 `NaN` + `available=False`
   이며 0 이나 대체값이 아니다(계약: "결측을 값으로 채우지 마라").
4. **`vol_qu` 를 차분하지 않는다** — 롤링 윈도 값이라 시점 간 차분이 무효다(C-2).
   점유율은 **같은 스냅 안의 비율**로만 만든다.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3

import numpy as np
import pandas as pd
import pytest

from tossmon.analysis import shots as S
from tossmon.analysis.measure import design_b as D
from tossmon.analysis.measure import exit_value as V

U = 1_000_000
S12 = 12_000


def series(vals, start=0, step=S12) -> pd.Series:
    idx = [start + i * step for i in range(len(vals))]
    return pd.Series([v * U for v in vals], index=idx, dtype="float64")


def sig(vals, start=0, step=S12) -> pd.Series:
    idx = [start + i * step for i in range(len(vals))]
    return pd.Series([float(v) for v in vals], index=idx, dtype="float64")


#: 미래에 훨씬 높은 봉이 있는 계열 — 사후 고점을 보면 답이 달라진다.
#: 급등(5.00)을 **끝에** 둔 이유: 신호 규칙은 그 전에 팔아야 하므로, 5.00 에 파는
#: 규칙이 있다면 그것은 미래를 본 것이다. 계열이 짧으면 규칙이 지평 끝까지 밀려
#: 우연히 5.00 에 닿을 수 있어 시험이 무의미해진다 — 그래서 12봉(144초)으로 잡았다.
LOOKAHEAD_TRAP = [1.00, 1.02, 1.01, 1.03, 0.99, 0.98,
                  0.97, 0.96, 0.95, 0.94, 5.00, 5.00]

#: 신호가 명확히 꺾이는 문맥. 각 규칙이 **급등 전에** 실제로 발동하도록 맞췄다.
def trap_ctx() -> dict:
    n = len(LOOKAHEAD_TRAP)
    return {
        # 순위: 3 -> 1 로 좋아진 뒤 갱신 없이(둔화) 결국 9 로 밀린다(역전)
        "rank": sig([3, 2, 1, 1, 1, 9, 9, 9, 9, 9, 9, 9][:n]),
        # 점유율: 정점 뒤 반토막
        "share": sig([0.10, 0.20, 0.30, 0.30, 0.05, 0.05,
                      0.05, 0.05, 0.05, 0.05, 0.05, 0.05][:n]),
        "ob": pd.DataFrame(
            {"bid1_u": [1.0] * n,
             "bid1_qu": [100, 100, 100, 10, 10, 10, 10, 10, 10, 10, 10, 10][:n],
             "ask1_u": [1.1] * n,
             "ask1_qu": [100, 100, 100, 900, 900, 900,
                         900, 900, 900, 900, 900, 900][:n],
             "spread_u": [0.01, 0.01, 0.01, 0.40, 0.40, 0.40,
                          0.40, 0.40, 0.40, 0.40, 0.40, 0.40][:n],
             "rel_spread": [0.01, 0.01, 0.01, 0.40, 0.40, 0.40,
                            0.40, 0.40, 0.40, 0.40, 0.40, 0.40][:n]},
            index=[i * S12 for i in range(n)]),
        "tape": sig([500, 500, 500, 10, 10, 10, 10, 10, 10, 10, 10, 10][:n]),
    }


OBSERVABLE = [n for n in V.signal_exit_rules() if n not in V.LOOK_AHEAD_RULES]


# --------------------------------------------------------------------------- #
# 1. 상한은 상한으로만 쓰인다
# --------------------------------------------------------------------------- #
def test_look_ahead_registry_contains_only_the_ceiling():
    """면제 목록이 늘어나면 안 된다 — 관측 가능 규칙이 슬쩍 들어오는 것을 막는다."""
    assert V.LOOK_AHEAD_RULES == ("ceiling_perfect_foresight",)
    fams = V.signal_exit_rules()
    for name in V.LOOK_AHEAD_RULES:
        assert fams[name][0] == "CEILING"


def test_ceiling_names_shout_that_it_is_a_ceiling():
    """이름만 봐도 전략 수익이 아님이 드러나야 한다 (감사 5차 C-1 재발 방지)."""
    for name in V.LOOK_AHEAD_RULES:
        assert "ceiling" in name.lower()
    src = pathlib.Path(V.__file__).read_text(encoding="utf-8")
    assert "UNACHIEVABLE" in src and "NOT a strategy return" in src


def test_ceiling_is_the_best_price_in_the_horizon():
    s = series(LOOKAHEAD_TRAP)
    r = V.ceiling_perfect_foresight(s, 0, float(s.iloc[0]))
    assert r["exit_u"] == pytest.approx(5.00 * U)
    assert r["available"] is True


def test_ceiling_is_never_below_selling_immediately():
    """즉시 청산도 선택지이므로 상한은 0 이상이다."""
    s = series([1.00, 0.90, 0.80])
    r = V.ceiling_perfect_foresight(s, 0, float(s.iloc[0]))
    assert r["exit_u"] >= float(s.iloc[0])


def test_ceiling_breaks_prefix_invariance_on_purpose():
    """**상한은 이 시험을 깨야 한다.** 안 깨지면 그건 상한이 아니다.

    동시에 이 시험이 위반을 **표현할 수 있음**을 증명한다 (H-3 의 교훈).
    """
    full = series(LOOKAHEAD_TRAP)
    a = V.ceiling_perfect_foresight(full, 0, float(full.iloc[0]))
    cut = full[full.index <= 3 * S12]          # 5.00 이 오기 전
    b = V.ceiling_perfect_foresight(cut, 0, float(full.iloc[0]))
    assert b["exit_u"] != pytest.approx(a["exit_u"])


# --------------------------------------------------------------------------- #
# 2. 나머지는 전부 접두사 불변
# --------------------------------------------------------------------------- #
def _truncate_ctx(ctx: dict, ts: int) -> dict:
    out = {}
    for k, v in ctx.items():
        out[k] = v[v.index <= ts] if v is not None and len(v) else v
    return out


@pytest.mark.parametrize("rule", OBSERVABLE)
def test_every_observable_signal_exit_is_prefix_invariant(rule):
    """판정이 t 에 확정됐다면, t 이후의 **가격도 신호도** 더 줘서는 안 바뀐다."""
    fn = V.signal_exit_rules()[rule][1]
    full, ctx = series(LOOKAHEAD_TRAP), trap_ctx()
    entry_u = float(full.iloc[0])
    a = fn(full, 0, entry_u, ctx)
    assert a["available"], f"{rule} did not fire on the fixture - test is vacuous"
    cut_p = full[full.index <= a["exit_ms"]]
    b = fn(cut_p, 0, entry_u, _truncate_ctx(ctx, a["exit_ms"]))
    assert b["exit_u"] == pytest.approx(a["exit_u"]), rule
    assert b["exit_ms"] == a["exit_ms"], rule


@pytest.mark.parametrize("rule", OBSERVABLE)
def test_every_observable_signal_exit_fires_before_the_future_spike(rule):
    """픽스처가 무의미하지 않은지 — 규칙이 미래의 5.00 **전에** 팔아야 한다.

    5.00 에서 파는 규칙이 있다면 그것은 사후 고점을 본 것이다.
    """
    fn = V.signal_exit_rules()[rule][1]
    full, ctx = series(LOOKAHEAD_TRAP), trap_ctx()
    r = fn(full, 0, float(full.iloc[0]), ctx)
    assert r["exit_u"] < 5.00 * U, rule


# --------------------------------------------------------------------------- #
# 3. 판정 불가를 값으로 채우지 않는다
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rule", OBSERVABLE)
def test_missing_signal_is_unavailable_not_zero(rule):
    """신호가 없으면 `NaN` + `available=False`. 0 으로 채우면 평균이 오염된다."""
    fn = V.signal_exit_rules()[rule][1]
    full = series(LOOKAHEAD_TRAP)
    r = fn(full, 0, float(full.iloc[0]), {})
    assert r["available"] is False
    assert r["exit_u"] != r["exit_u"]          # NaN


def test_too_few_signal_observations_is_unavailable():
    full = series(LOOKAHEAD_TRAP)
    thin = {"rank": sig([3, 2])}               # MIN_SIGNAL_OBS 미만
    assert V.exit_rank_stall(full, 0, float(full.iloc[0]), thin)["available"] is False


def test_evaluate_marks_availability_per_rule():
    full = series(LOOKAHEAD_TRAP)
    df = V.evaluate_all({"AAA": full}, {"AAA": trap_ctx()},
                        [{"symbol": "AAA", "signal_ms": -13_000, "entry_idx": 0}])
    assert len(df) == 1
    for name in V.all_rule_names():
        assert f"{name}__available" in df.columns
    assert bool(df.iloc[0]["ceiling_perfect_foresight__available"])
    # 문맥이 없는 종목은 신호 규칙이 전부 판정 불가여야 한다
    df2 = V.evaluate_all({"BBB": full}, {},
                         [{"symbol": "BBB", "signal_ms": -13_000, "entry_idx": 0}])
    for name in OBSERVABLE:
        assert not bool(df2.iloc[0][f"{name}__available"]), name


# --------------------------------------------------------------------------- #
# 4. 단위 계약 C-2 — `vol_qu` 를 차분하지 않는다
# --------------------------------------------------------------------------- #
def test_share_is_a_same_snapshot_ratio_never_a_difference():
    """`vol_qu` 는 롤링 윈도 값이라 시점 간 차분이 무효다 (docs/04 계약 C-2)."""
    src = pathlib.Path(V.__file__).read_text(encoding="utf-8")
    assert "vol_qu" in src
    for forbidden in ("vol_qu'].diff", 'vol_qu"].diff', "vol_qu.diff"):
        assert forbidden not in src
    assert "amount_u" not in src, "amount_u is micro-KRW - do not use it as money here"


def test_share_denominator_is_the_same_snapshot():
    sigdf = pd.DataFrame({
        "snap_ms": [0, 0, S12, S12],
        "ranking_type": [S.TOSS_VOLUME] * 4,
        "symbol": ["AAA", "BBB", "AAA", "BBB"],
        "rank": [1, 2, 2, 1],
        "vol_qu": [30.0, 70.0, 10.0, 90.0]})
    ctx = V.build_contexts(sigdf, pd.DataFrame(), pd.DataFrame())
    assert ctx["AAA"]["share"].loc[0] == pytest.approx(0.30)
    assert ctx["AAA"]["share"].loc[S12] == pytest.approx(0.10)


# --------------------------------------------------------------------------- #
# 5. 회수율·검정력 산수
# --------------------------------------------------------------------------- #
def test_recovery_is_a_ratio_of_means():
    assert V.recovery_of_ceiling(0.005, 0.020) == pytest.approx(0.25)


def test_recovery_is_undefined_when_the_ceiling_is_not_positive():
    assert V.recovery_of_ceiling(0.005, 0.0) != V.recovery_of_ceiling(0.005, 0.0)


def test_days_needed_inverts_the_coverage_rate():
    r = V.days_needed_for_n(5.0, min_n=30)
    assert r["reachable"] and r["days_needed"] == 6


def test_days_needed_reports_unreachable_at_zero_coverage():
    assert V.days_needed_for_n(0.0)["reachable"] is False


# --------------------------------------------------------------------------- #
# 6. 러너 전체 — 합성 DB (라이브·실데이터 불필요)
# --------------------------------------------------------------------------- #
def _tiny_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db = tmp_path / "tiny.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE rankings_snap (snap_ms INTEGER, ranking_type TEXT, "
                 "rank INTEGER, symbol TEXT, last_u INTEGER, vol_qu REAL)")
    conn.execute("CREATE TABLE orderbook_snap (symbol TEXT, snap_ms INTEGER, "
                 "bid1_u INTEGER, bid1_qu REAL, ask1_u INTEGER, ask1_qu REAL, "
                 "spread_u INTEGER)")
    conn.execute("CREATE TABLE trades_snap (symbol TEXT, ts_ms INTEGER, qty_u REAL)")
    base = 1_785_000_000_000
    # 오르다 크게 눌리고 반등 -> 과매도 진입 성립 (반등 봉도 고점 대비 5% 아래여야 한다)
    path = [1.00] * 6 + [1.02] * 4 + [0.90] * 3 + [0.92] * 8 + [0.95] * 6
    rows, ob, tp = [], [], []
    for si, sym in enumerate(("AAA", "BBB", "CCC")):
        for i, px in enumerate(path):
            ts = base + i * 13_000
            rows.append((ts, S.TOSS_VOLUME, si + 1 + (i > 12) * 20, sym,
                         int(px * U), 100.0 - i))
            ob.append((sym, ts, int(px * U), max(1.0, 100.0 - i * 4),
                       int(px * U * 1.01), 10.0 + i * 8, int(px * U * 0.01)))
            tp.append((sym, ts, max(1.0, 400.0 - i * 15)))
    conn.executemany("INSERT INTO rankings_snap VALUES (?,?,?,?,?,?)", rows)
    conn.executemany("INSERT INTO orderbook_snap VALUES (?,?,?,?,?,?,?)", ob)
    conn.executemany("INSERT INTO trades_snap VALUES (?,?,?)", tp)
    conn.commit()
    conn.close()
    return db


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("exitvalue")
    db = _tiny_db(tmp)
    out = tmp / "out"
    assert V.main(db, out_dir=out) == 0
    return json.loads((out / "exit_value.json").read_text(encoding="utf-8")), out


def test_runner_output_keys_match_the_manifest_exactly(report):
    """규칙 레코드의 키 집합 == `REPORTED_FIELDS` (부분집합이 아니라 동일)."""
    rep, _ = report
    assert rep["rules"], "runner produced no rules"
    for row in rep["rules"]:
        assert set(row) == set(V.REPORTED_FIELDS), row.get("rule")


def test_runner_covers_the_twelve_price_rules_and_the_new_families(report):
    rep, _ = report
    names = {r["rule"] for r in rep["rules"]}
    assert set(D.exit_rules()) <= names             # 12종이 같은 표에 있다
    fams = {r["family"] for r in rep["rules"]}
    assert {"price_only", "ranking", "orderbook", "tape", "CEILING"} <= fams


def test_runner_marks_the_ceiling_and_warns_in_the_payload(report):
    rep, _ = report
    ceil = [r for r in rep["rules"] if r["look_ahead"]]
    assert [r["rule"] for r in ceil] == ["ceiling_perfect_foresight"]
    assert "UNACHIEVABLE" in rep["ceiling"]["warning"]


def test_no_observable_rule_beats_the_ceiling(report):
    """상한은 천장이다 — 관측 가능 규칙이 넘으면 어딘가 미래를 본 것이다."""
    rep, _ = report
    ceil = rep["ceiling"]["mean"]
    for r in rep["rules"]:
        if r["look_ahead"] or r["gross_mean"] != r["gross_mean"]:
            continue
        assert r["gross_mean"] <= ceil + 1e-9, r["rule"]


def test_runner_applies_the_cost_scenarios_to_the_ceiling_too(report):
    rep, _ = report
    assert set(rep["ceiling"]["net_by_scenario"]) == set(D.COST_SCENARIOS)
    for k, c in D.COST_SCENARIOS.items():
        assert rep["ceiling"]["net_by_scenario"][k] == pytest.approx(
            rep["ceiling"]["mean"] - c)


def test_runner_withholds_pooled_ci_below_the_day_cluster_floor(report):
    rep, _ = report
    assert rep["pooled_ci_permitted"] == (len(rep["days"]) >= D.MIN_DAY_CLUSTERS)


# --------------------------------------------------------------------------- #
# 6b. 쌍체 키 — **날짜가 다르면 다른 진입이다**
# --------------------------------------------------------------------------- #
def test_entry_keys_are_globally_unique_across_days():
    """`collect_entries` 는 날마다 0 부터 센다 — 이어 붙이면 키가 겹친다.

    겹친 키로 쌍체를 맺으면 **다른 날 진입끼리 짝이 맺히고**, pandas 가 중복 라벨을
    조용히 브로드캐스트해서 **오류도 없이 짝 수만 늘어난다.** 실제로 그렇게 됐다 —
    통제군 짝 수(161)가 정합 성공 건수(160)보다 많아진 것으로 발각됐다.
    """
    raw = [{"symbol": "A", "signal_ms": 0, "entry_idx": 0},
           {"symbol": "B", "signal_ms": 1, "entry_idx": 1}]
    d1 = D.tag_entries(raw, "2026-07-31")
    d2 = D.tag_entries(raw, "2026-08-03")
    keys = [e["entry_idx"] for e in d1 + d2]
    assert len(set(keys)) == len(keys), keys


def test_placebo_inherits_the_entry_key_rather_than_renumbering():
    """위약이 위치 번호로 다시 매기면 날짜 접두사가 사라져 쌍체가 어긋난다."""
    ents = D.tag_entries([{"symbol": "A", "signal_ms": 0, "entry_idx": 0}],
                         "2026-08-03")
    pl = D.placebo_entries(ents, ["A", "B"], seed=1)
    assert pl[0]["entry_idx"] == ents[0]["entry_idx"] == "2026-08-03#0"


def test_paired_difference_does_not_inflate_when_days_are_concatenated():
    """중복 키를 주면 짝 수가 부풀던 자리 — 고유 키에서는 정확히 맞아야 한다."""
    real = pd.DataFrame({"entry_idx": ["d1#0", "d1#1", "d2#0"], "r": [1.0, 2.0, 3.0]})
    plac = pd.DataFrame({"entry_idx": ["d1#0", "d1#1", "d2#0"], "r": [0.0, 0.0, 0.0]})
    d = D.paired_difference(real, plac, "r")
    assert len(d) == 3
    assert sorted(d.tolist()) == [1.0, 2.0, 3.0]


def test_paired_difference_would_inflate_on_colliding_keys():
    """가드가 실제로 잡을 수 있는 결함임을 증명한다 — 중복 키면 짝이 어긋난다."""
    real = pd.DataFrame({"entry_idx": [0, 1, 0], "r": [1.0, 2.0, 3.0]})
    plac = pd.DataFrame({"entry_idx": [0, 1, 0], "r": [0.0, 0.0, 9.0]})
    d = D.paired_difference(real, plac, "r")
    # 위약 0번이 두 날 평균(4.5)으로 뭉개져 실제 두 행에 같은 값이 빠진다
    assert len(d) == 3 and sorted(d.tolist()) != [1.0, 2.0, 3.0]


# --------------------------------------------------------------------------- #
# 7. 통제된 위약 — 상한 우위가 변동성 선택 효과인가
# --------------------------------------------------------------------------- #
def test_realized_volatility_uses_only_the_window_before_the_timestamp():
    """정합 기준이 **사전 관측만**으로 만들어져야 통제가 성립한다."""
    ts = np.array([0, S12, 2 * S12, 3 * S12, 4 * S12])
    px = np.array([1.0, 1.0, 1.0, 1.0, 100.0]) * U
    quiet = V.realized_volatility(ts, px, 3 * S12, window_s=600)
    noisy = V.realized_volatility(ts, px, 4 * S12, window_s=600)
    assert quiet == pytest.approx(0.0, abs=1e-12)
    assert noisy > 0.5                       # 미래 봉이 들어오면 값이 달라진다


def test_realized_volatility_is_nan_when_there_is_too_little_to_measure():
    """관측이 모자라면 **NaN** 이다 — 0 으로 채우면 정합이 조용히 틀어진다."""
    thin = V.realized_volatility(np.array([0, S12]), np.array([1.0, 2.0]) * U,
                                 S12, window_s=600)
    assert thin != thin
    assert V.realized_volatility(None, None, 0, window_s=600) != \
        V.realized_volatility(None, None, 0, window_s=600)


def _vol_universe() -> dict:
    """변동성이 뚜렷이 다른 세 종목. 정합이 실제로 걸러내는지 보기 위한 것."""
    calm = series([1.0, 1.001, 1.0, 1.001] * 8)
    like = series([1.0, 1.05, 1.0, 1.05] * 8)
    wild = series([1.0, 1.60, 1.0, 1.60] * 8)
    return {"REAL": like, "TWIN": like * 1.0, "CALM": calm, "WILD": wild}


def test_volatility_matching_keeps_similar_symbols_and_drops_the_rest():
    uni = _vol_universe()
    ents = [{"symbol": "REAL", "signal_ms": 30 * S12, "entry_idx": 0}]
    match = V.volatility_match_pools(ents, uni, tol=0.2, lookback_s=600)
    pool = match["pools"][0]
    assert "TWIN" in pool                      # 같은 변동성 -> 후보
    assert "CALM" not in pool and "WILD" not in pool


def test_unmatched_entries_are_dropped_and_therefore_countable():
    """정합 대상이 없으면 **짝을 잃는다** — 조용히 줄지 않도록 세어 보고한다."""
    uni = {"REAL": _vol_universe()["REAL"], "CALM": _vol_universe()["CALM"]}
    ents = [{"symbol": "REAL", "signal_ms": 30 * S12, "entry_idx": 0}]
    match = V.volatility_match_pools(ents, uni, tol=0.2, lookback_s=600)
    assert match["pools"][0] == []
    assert V.volatility_matched_placebo(ents, match, seed=1) == []


def test_self_symbol_placebo_keeps_the_symbol_and_moves_the_time():
    """가장 깨끗한 통제군 — 종목 고유 변동성이 완전히 상쇄된다."""
    uni = _vol_universe()
    ents = [{"symbol": "REAL", "signal_ms": 5 * S12, "entry_idx": 0}]
    out = V.self_symbol_placebo(ents, uni, seed=7, horizon_s=60,
                                min_gap_s=120)
    assert len(out) == 1
    assert out[0]["symbol"] == "REAL"
    assert abs(out[0]["signal_ms"] - 5 * S12) >= 120_000


def test_self_symbol_placebo_leaves_room_for_the_horizon():
    uni = _vol_universe()
    ents = [{"symbol": "REAL", "signal_ms": 0, "entry_idx": 0}]
    out = V.self_symbol_placebo(ents, uni, seed=3, horizon_s=60, min_gap_s=0)
    last = int(uni["REAL"].index[-1])
    for e in out:
        assert e["signal_ms"] + (D.ENTRY_DELAY_S + 60) * 1000 <= last


def test_ceiling_arm_reports_both_normalisations():
    uni = _vol_universe()
    arm = V.ceiling_arm(uni, [{"symbol": "REAL", "signal_ms": 0, "entry_idx": 0}],
                        horizon_s=600, lookback_s=600)
    assert set(V.CONTROL_METRICS) <= set(arm.columns)
    assert (arm["ceiling"] >= 0).all()


def test_normalised_ceiling_cancels_a_pure_volatility_difference():
    """**이 검사가 통제의 논리 자체다.**

    같은 모양을 크기만 키운 계열은 상한이 크지만 변동성도 같은 비율로 크다.
    정규화 상한이 거의 같아야 한다 — 아니면 정규화가 제 일을 못 하는 것이다.
    """
    # 진입이 **골**에 떨어지도록(그리고 사전 창에 관측이 남도록) 시각을 뒤로 뺐다.
    # 마루에서 진입하면 상한이 0 이 되어 시험이 무의미해진다.
    small = series([1.02, 1.00] * 80)
    big = series([1.20, 1.00] * 80)
    ent = [{"symbol": "X", "signal_ms": 60 * S12, "entry_idx": 0}]
    a = V.ceiling_arm({"X": small}, ent, horizon_s=600, lookback_s=600)
    b = V.ceiling_arm({"X": big}, ent, horizon_s=600, lookback_s=600)
    assert b["ceiling"].iloc[0] > a["ceiling"].iloc[0] * 3      # 원 상한은 크게 다르다
    ra = a["ceiling_per_pre_rv"].iloc[0]
    rb = b["ceiling_per_pre_rv"].iloc[0]
    assert rb == pytest.approx(ra, rel=0.35)                   # 정규화하면 붙는다


def test_controls_summary_marks_survival_per_metric(report):
    rep, _ = report
    ct = rep["controls"]
    assert set(ct["arms"]) == {"random_symbol", "vol_matched", "self_symbol"}
    for a in ct["arms"].values():
        assert set(a["survives"]) == set(V.CONTROL_METRICS)
    assert isinstance(ct["claim_survives_every_control"], bool)


def test_controls_report_the_match_rate_rather_than_hiding_it(report):
    rep, _ = report
    for m in rep["controls"]["matching"]:
        assert {"day", "entries", "with_vol_match"} <= set(m)
        assert m["with_vol_match"] <= m["entries"]


def test_underpowered_rules_are_flagged_rather_than_judged(report):
    rep, _ = report
    for r in rep["rules"]:
        if not r["powered"]:
            assert r["n"] < V.MIN_N_FOR_VERDICT
            assert "reachable" in r["days_needed_for_n"]

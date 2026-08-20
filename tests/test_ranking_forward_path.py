"""랭킹 사건의 **전방 가격 경로** (`docs/64` · `docs/65`). 이 파일이 지키는 것 열둘.

1. **사건 정의에 가격이 한 번도 들어가지 않는다.** `last_u` 를 통째로 뒤흔들어도
   앵커가 붙는 사건 수가 같아야 한다. 설계 A 가 *"탐지가 상승을 소진한다"* 로 죽은
   자리라(`STRATEGY-VERDICTS` §4.4) 문서가 아니라 테스트가 지켜야 한다.
2. **앵커는 `t0` 를 넘어서만 잡힌다.** `t0` 와 같은 초의 막대에는 `t0` 이전 체결이
   섞여 있다. 그 막대를 앵커로 쓰면 우리가 받기 전 가격으로 재는 것이 된다.
3. **미래를 안 본다.** 지평 뒤 막대를 잘라내고 다시 재도 같은 값이 나와야 한다.
4. **위약은 같은 종목·같은 세션에서만 나오고 자기 측정 구간을 침범하지 않는다.**
   `SELF_GAP_S` 가 지평(300초)보다 짧으면 위약 창이 사건 창과 겹친다.
5. **정합이 실제로 밴드를 좁힌다.** "맞췄다" 는 주장이 아니라 관측이어야 한다.
6. **군집 5 미만이면 CI 를 안 낸다.** 규율이 아니라 코드가 막는지 본다
   (`STRATEGY-VERDICTS` §4.4-B).
7. **홀드아웃 행을 심어도 산출물에 안 닿는다.** 데이터가 우연히 안 걸리는 것과
   코드가 막는 것은 다르다.
8. **라벨 넷이 모든 산출물에 붙고 판정 문구가 없다.** 이 태스크의 지위 자체다.
9. **콘솔이 ASCII 다.** cp949 콘솔에서 비 ASCII 는 `UnicodeEncodeError` 로 죽는다.

`docs/65` 가 더한 셋:

10. **새 추첨기가 옛 추첨기와 같은 추첨이다.** 밴드를 하나 더 켤 수 있게 새로 썼는데
    그 자체가 팔 사이 차이를 만들면 사다리가 사다리가 아니다(`docs/44` §14-1).
    `match_fwd=False` 에서 `draw_stratified` 와 **배열까지** 같아야 한다.
11. **새 정합 키가 미래를 안 쓴다.** `nbar300` 은 직전 300 초다 - 뒤를 잘라내고 다시
    불러도 같은 값이어야 한다. 이 성질이 이 키를 정합 축에 넣을 수 있는 유일한 근거다.
12. **확증 팔을 안 연다.** 2026-08-13 이후 세션이 산출물에 닿으면 `E` 를 확증 데이터에
    맞춰 고르는 길이 열린다(`G2G3-PREREG` §5-1). 규율이 아니라 코드가 막는지 본다.
"""
from __future__ import annotations

import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

from tossmon.analysis import hires_events as HE
from tossmon.analysis.measure import ranking_forward_path as RFP
from tossmon.analysis.measure.vol_matched_placebo import arm_forward_probe

MS = 1000
OPEN_MS = HE.REGULAR_OPEN_S * MS
POLL_MS = 12_000
N_SNAPS = 200

#: 사건이 붙는 스냅. 앞뒤로 `SELF_GAP_S` 를 넘는 후보가 남도록 세션 가운데에 둔다.
EVENT_SNAP = 100

HOLDOUT_DAY = "2026-06-15"
DAYS = ("2026-08-10", "2026-08-11")

#: 이 모듈이 실제로 쓰는 두 랭킹 타입. 다른 타입을 심어도 러너가 안 본다.
TYPES = (("TOP_GAINERS", "1d"), ("TOSS_SECURITIES_TRADING_VOLUME", "realtime"))


def _day0(d: str) -> int:
    return int(pd.Timestamp(d + "T00:00:00Z").timestamp() * 1000)


def _plan(i: int):
    """`AAA` 는 계속 1 위(사건 없음), `BBB` 는 `EVENT_SNAP` 부터 50 위로 들어온다."""
    out = [("AAA", 1, 1_500_000)]
    if i >= EVENT_SNAP:
        out.append(("BBB", 50, 3_000_000))
    return out


def _rank_rows(day: str, *, scramble: bool = False) -> list:
    base = _day0(day) + OPEN_MS
    rows = []
    for i in range(N_SNAPS):
        for sym, rk, last_u in _plan(i):
            for rtype, dur in TYPES:
                px = int(1 + (7919 * (i + rk)) % 90_000_000) if scramble else last_u
                rows.append((base + i * POLL_MS, rtype, dur, rk, sym, px, 1, 1))
    return rows


def _trade_rows(day: str) -> list:
    """`BBB` 는 1 초 격자로 촘촘하게, `AAA` 는 아주 성기게.

    `AAA` 를 성기게 두는 이유: 앵커가 안 잡히는 사건(= `docs/59` 의 98%)이 표에
    실제로 나타나야 깔때기 검사가 공허하지 않다.
    """
    base = _day0(day) + OPEN_MS
    rows = []
    for s in range(0, 2400):
        px = 3_000_000 + (s % 17) * 1_000 + (5_000 if 1200 < s < 1260 else 0)
        rows.append(("BBB", base + s * MS, px, 10))
    for s in range(0, 2400, 900):
        rows.append(("AAA", base + s * MS, 1_500_000, 5))
    return rows


def _make_db(path, *, scramble: bool = False, with_holdout: bool = True):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE rankings_snap (id INTEGER PRIMARY KEY AUTOINCREMENT,
            snap_ms INTEGER NOT NULL, ranking_type TEXT NOT NULL, duration TEXT NOT NULL,
            rank INTEGER NOT NULL, symbol TEXT NOT NULL, last_u INTEGER NOT NULL,
            vol_qu INTEGER NOT NULL, amount_u INTEGER NOT NULL);
        CREATE TABLE trades_snap (symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL,
            price_u INTEGER NOT NULL, qty_u INTEGER NOT NULL,
            PRIMARY KEY (symbol, ts_ms, price_u, qty_u)) WITHOUT ROWID;
        CREATE TABLE tape_gaps (id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL, poll_ms INTEGER NOT NULL, prev_poll_ms INTEGER,
            gap_lo_ms INTEGER NOT NULL, gap_hi_ms INTEGER NOT NULL,
            span_hi_ms INTEGER NOT NULL, n_raw INTEGER NOT NULL, n_stored INTEGER NOT NULL);
        CREATE TABLE symbols (symbol TEXT PRIMARY KEY, name TEXT NOT NULL,
            market TEXT NOT NULL, security_type TEXT NOT NULL, status TEXT NOT NULL,
            list_date TEXT, shares_outstanding_qu INTEGER NOT NULL,
            tier INTEGER NOT NULL DEFAULT 0, is_former_runner INTEGER NOT NULL DEFAULT 0,
            updated_ms INTEGER NOT NULL);
    """)
    rows, trades = [], []
    for d in DAYS:
        rows += _rank_rows(d, scramble=scramble)
        trades += _trade_rows(d)
    if with_holdout:
        # 봉인 구간에 **심는다.** 가드가 없으면 이 행들이 사건이 된다.
        rows += _rank_rows(HOLDOUT_DAY, scramble=scramble)
        trades += _trade_rows(HOLDOUT_DAY)
    conn.executemany("INSERT INTO rankings_snap (snap_ms, ranking_type, duration, rank, "
                     "symbol, last_u, vol_qu, amount_u) VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.executemany("INSERT INTO trades_snap VALUES (?,?,?,?)", trades)
    conn.commit()
    conn.close()
    return path


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    return _make_db(tmp_path_factory.mktemp("rfp") / "t.db")


@pytest.fixture(scope="module")
def report(db, tmp_path_factory):
    out = tmp_path_factory.mktemp("rfp_out")
    assert RFP.main(["prog", str(db), "--out", str(out)]) == 0
    return json.loads((out / "ranking_forward_path.json").read_text(encoding="utf-8")), out


def _cell(rep, rtype="TOSS_SECURITIES_TRADING_VOLUME", kind="E1_new_entry",
          cell="N50", tier="all"):
    for r in rep["cells"]:
        if (r["ranking_type"], r["kind"], r["cell"], r["tier"]) == (
                rtype, kind, cell, tier):
            return r
    raise AssertionError(f"cell not in report: {rtype} {kind} {cell} {tier}")


# --------------------------------------------------------------------------- #
# 1. 사건 정의 - 가격은 한 번도 안 들어간다
# --------------------------------------------------------------------------- #
def test_anchored_event_count_is_invariant_to_price(tmp_path):
    """`last_u` 를 뒤흔들어도 사건도 앵커도 안 움직여야 한다.

    진술이 아니라 관측으로 지킨다 - `docs/59` 의 `test_event_set_is_invariant_to_price`
    와 같은 자리이고, 이 모듈이 그 정의를 **불러 쓰는지**를 확인하는 것이기도 하다.
    """
    plain = _make_db(tmp_path / "plain.db")
    mixed = _make_db(tmp_path / "mixed.db", scramble=True)
    a = RFP.build_report(RFP.run(plain))
    b = RFP.build_report(RFP.run(mixed))
    ka = {(r["ranking_type"], r["kind"], r["cell"]): r["funnel"]["n_anchored"]
          for r in a["cells"]}
    kb = {(r["ranking_type"], r["kind"], r["cell"]): r["funnel"]["n_anchored"]
          for r in b["cells"]}
    assert ka == kb and sum(ka.values()) > 0


# --------------------------------------------------------------------------- #
# 2. 앵커 - `t0` 를 넘어서만
# --------------------------------------------------------------------------- #
def test_anchor_never_uses_a_bar_at_or_before_t0():
    """`t0` 와 같은 밀리초의 막대는 앵커가 될 수 없다. `docs/59` 의 `(t0, t0+W]` 와 같다."""
    ts = np.asarray([1000, 2000, 3000], dtype="int64")
    idx, lag = RFP.anchor_bars(ts, np.asarray([2000], dtype="int64"))
    assert idx[0] == 2 and lag[0] == pytest.approx(1.0)


def test_anchor_is_dropped_when_the_next_bar_is_past_the_wait_cap():
    """상한 밖이면 앵커가 없다 - **길이를 줄이지 않고** -1 로 표시한다."""
    ts = np.asarray([0, 400_000], dtype="int64")
    idx, lag = RFP.anchor_bars(ts, np.asarray([10], dtype="int64"), max_wait_s=300)
    assert idx[0] == -1 and not np.isfinite(lag[0])
    idx2, lag2 = RFP.anchor_bars(ts, np.asarray([10], dtype="int64"), max_wait_s=500)
    assert idx2[0] == 1 and lag2[0] == pytest.approx(399.99)


def test_anchor_wait_cap_equals_the_longest_horizon():
    """상한이 지평보다 짧으면 앵커가 있는데 창이 비는 사건이 생겨 두 표가 어긋난다."""
    assert RFP.ANCHOR_MAX_WAIT_S == max(RFP.PROBE_HORIZONS_S)


# --------------------------------------------------------------------------- #
# 3. 미래를 안 본다
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("h", RFP.PROBE_HORIZONS_S)
def test_forward_path_does_not_look_past_its_horizon(h):
    """지평 뒤 막대를 잘라내고 다시 재도 같은 값이어야 한다 (접두사 불변)."""
    ts = np.arange(0, 900, dtype="int64") * MS
    px = 100.0 + np.arange(ts.size, dtype="float64")      # 계속 오른다 - 자르면 티가 난다
    uni = {"S": {"ts": ts, "px": px}}
    idx = np.asarray([10], dtype="int64")
    full = arm_forward_probe(uni, ["S"], np.zeros(1, "int64"), idx, horizons=(h,))
    keep = ts <= ts[10] + h * MS
    cut = {"S": {"ts": ts[keep], "px": px[keep]}}
    part = arm_forward_probe(cut, ["S"], np.zeros(1, "int64"), idx, horizons=(h,))
    assert full[h]["max_ret"][0] == pytest.approx(part[h]["max_ret"][0])
    assert full[h]["end_ret"][0] == pytest.approx(part[h]["end_ret"][0])


# --------------------------------------------------------------------------- #
# 4. 위약 - 같은 종목·같은 세션·자기 구간 밖
# --------------------------------------------------------------------------- #
def test_self_gap_covers_the_longest_horizon_and_the_lookback():
    """`vol_matched_placebo` 의 180 초는 지평 120 초용이다. 그대로 쓰면 창이 겹친다."""
    assert RFP.SELF_GAP_S >= max(RFP.PROBE_HORIZONS_S) + RFP.VOL_LOOKBACK_S


def test_placebo_draws_stay_in_the_same_symbol_and_outside_the_self_gap(db):
    res = RFP.run(db)
    seen = 0
    for key, box in res["cells"].items():
        for arm, a in box["arms"].items():
            for sess, raw in a["raw"].items():
                real = box["real"][sess]
                assert set(raw["_symbol"]) <= set(real["_symbol"])
                seen += int(raw["_symbol"].size)
    assert seen > 0, "no placebo draws were made - the test would be vacuous"


def test_placebo_bars_never_sit_inside_the_events_own_window(db):
    """추첨된 막대가 사건 앵커의 +-`SELF_GAP_S` 안에 있으면 위약이 사건을 재게 된다."""
    conn = HE.open_ro(db)
    try:
        s = RFP.scan_sessions(conn, HE.holdout_floor_ms(),
                              int(conn.execute("SELECT MAX(snap_ms) "
                                               "FROM rankings_snap").fetchone()[0]))[0]
        bars = RFP.session_second_bars(conn, s["open_ms"], s["close_ms"])
    finally:
        conn.close()
    from tossmon.analysis.measure.density_matched_placebo import (
        add_trade_count, stratified_index)
    from tossmon.analysis.measure.vol_matched_placebo import build_universe
    uni = add_trade_count(build_universe(bars, lookback_s=RFP.VOL_LOOKBACK_S), bars)
    syms = sorted(uni)
    si = syms.index("BBB")
    ts = uni["BBB"]["ts"]
    anchor = np.asarray([1500], dtype="int64")
    uni = RFP.add_forward_depth_proxy(uni)
    d = RFP.draw_banded(uni, syms, stratified_index(uni, syms),
                        np.asarray([si], dtype="int64"), anchor,
                        match_band=True, match_strat=True, match_fwd=True,
                        draws=RFP.MATCH_DRAWS, gap_s=RFP.SELF_GAP_S,
                        rng=np.random.default_rng(RFP.SEED))
    assert d["sym"].size > 0, "nothing was drawn - the test would be vacuous"
    dt = np.abs(ts[d["bar"]] - ts[anchor[0]]) / 1000.0
    assert (dt > RFP.SELF_GAP_S).all()


# --------------------------------------------------------------------------- #
# 5. 정합이 실제로 밴드를 좁힌다
# --------------------------------------------------------------------------- #
def test_matched_arm_actually_narrows_the_volatility_band(db):
    """"맞췄다"는 주장이 아니라 관측이어야 한다 - 밴드 크기가 실제로 줄어야 한다."""
    res = RFP.run(db)
    for key, box in res["cells"].items():
        un = box["arms"].get("placebo_unmatched")
        vm = box["arms"].get("placebo_vol_matched")
        vd = box["arms"].get("placebo_vol_density_matched")
        vf = box["arms"].get("placebo_vol_density_nbar300_matched")
        if not (un and vm and vd and vf):
            continue
        a = RFP.merge_pairing(un["pairing"])
        b = RFP.merge_pairing(vm["pairing"])
        c = RFP.merge_pairing(vd["pairing"])
        e = RFP.merge_pairing(vf["pairing"])
        # 밴드를 더할수록 짝은 줄기만 한다 - 늘어나면 사다리가 사다리가 아니다.
        assert a["n_paired"] >= b["n_paired"] >= c["n_paired"] >= e["n_paired"]
        return
    pytest.fail("no cell carried the full placebo ladder")


def test_the_ladder_changes_exactly_one_band_at_a_time():
    """`docs/44` §14-1 의 규율이 상수에 박혀 있는지. 칸마다 밴드가 **하나씩** 켜진다."""
    got = [(b, s, f) for _a, b, s, f in RFP.PLACEBO_ARMS]
    assert got == [(False, False, False), (True, False, False),
                   (True, True, False), (True, True, True)]
    for prev, cur in zip(got, got[1:]):
        assert sum(int(x) for x in cur) - sum(int(x) for x in prev) == 1


# --------------------------------------------------------------------------- #
# 6. 군집 5 미만이면 CI 를 안 낸다 - **코드가 막는다**
# --------------------------------------------------------------------------- #
def test_pooled_ci_is_withheld_below_five_trading_day_clusters():
    real = {f"d{i}": np.asarray([0.02, 0.03]) for i in range(4)}
    plac = {f"d{i}": np.asarray([0.01, 0.01]) for i in range(4)}
    out = RFP.cluster_bootstrap_diff(real, plac)
    assert out["ci95"] is None and out["n_clusters"] == 4
    assert "4 < 5" in out["ci_withheld"]
    assert out["mean_diff"] is not None, "the point estimate is still reported"


def test_pooled_ci_appears_once_there_are_five_clusters():
    # 다섯 날의 값을 서로 다르게 둔다. 전부 같으면 부트스트랩 분포가 한 점으로
    # 무너져 CI 가 점추정치와 **부동소수점 오차만큼** 어긋난다 - 그건 성질이 아니라
    # 축퇴다.
    real = {f"d{i}": np.asarray([0.02 + 0.01 * i, 0.03 + 0.01 * i]) for i in range(5)}
    plac = {f"d{i}": np.asarray([0.01, 0.01]) for i in range(5)}
    out = RFP.cluster_bootstrap_diff(real, plac)
    assert out["ci95"] is not None and out["ci_withheld"] is None
    assert out["ci95"][0] < out["mean_diff"] < out["ci95"][1]


def test_bonferroni_ci_is_wider_and_uses_the_runners_own_cell_count():
    """`STRATEGY-VERDICTS` §4.4-F 가 뒤집힌 자리 - **보정 분모가 곧 결론이었다.**

    분모를 5 에서 20(실제로 본 칸 수)으로 바로잡자 *"유의하게 음수"* 가 0 을 교차했다.
    그래서 보정 CI 는 항상 넓어야 하고, 분모는 사람이 적는 것이 아니라 러너가 센다.
    """
    real = {f"d{i}": np.asarray([0.02 + 0.01 * i, 0.03 + 0.01 * i]) for i in range(6)}
    plac = {f"d{i}": np.asarray([0.01, 0.01]) for i in range(6)}
    one = RFP.cluster_bootstrap_diff(real, plac, n_comparisons=1)
    many = RFP.cluster_bootstrap_diff(real, plac, n_comparisons=18)
    assert many["ci_bonferroni"][0] < one["ci_bonferroni"][0]
    assert many["ci_bonferroni"][1] > one["ci_bonferroni"][1]
    assert one["ci_bonferroni"] == pytest.approx(one["ci95"], abs=5e-4)
    assert many["n_comparisons"] == 18


def test_the_report_counts_its_own_comparisons(report):
    """분모가 러너가 만든 칸 수와 같아야 한다 - 상수로 박아 두면 §4.4-F 가 재발한다."""
    rep, _out = report
    made = [d for r in rep["cells"] for d in r["diff"].values()]
    assert made, "no differences were produced - the test would be vacuous"
    assert {d["n_comparisons"] for d in made} == {len(made)}


def test_the_bootstrap_resamples_days_not_events():
    """한 날에 사건이 몰려 있으면 사건 단위 CI 는 거짓으로 좁다. 군집이 날이어야 한다.

    한 날만 다른 값을 갖게 해 두면, 날 단위 재추출은 그 날이 뽑히고 안 뽑히고에 따라
    **넓은** CI 를 내야 한다.
    """
    real = {f"d{i}": np.asarray([0.0] * 50) for i in range(5)}
    real["d0"] = np.asarray([1.0] * 50)
    plac = {f"d{i}": np.asarray([0.0] * 50) for i in range(5)}
    out = RFP.cluster_bootstrap_diff(real, plac)
    assert out["ci95"][1] - out["ci95"][0] > 0.3


# --------------------------------------------------------------------------- #
# 7. 홀드아웃
# --------------------------------------------------------------------------- #
def test_holdout_rows_are_planted_but_never_reach_the_report(report):
    rep, _out = report
    assert all(s["session"] > HOLDOUT_DAY for s in rep["sessions"])
    assert rep["holdout"]["window"] == ["2026-05-01", "2026-07-29"]


def test_the_planted_holdout_rows_really_exist_so_the_guard_is_what_removed_them(db):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    lo = _day0(HOLDOUT_DAY)
    n = conn.execute("SELECT COUNT(*) FROM rankings_snap WHERE snap_ms >= ? "
                     "AND snap_ms < ?", (lo, lo + 86_400_000)).fetchone()[0]
    conn.close()
    assert n > 0, "nothing was planted - the guard test above would be vacuous"


# --------------------------------------------------------------------------- #
# 8. 라벨 넷과 판정 금지
# --------------------------------------------------------------------------- #
def test_every_report_carries_the_five_labels(report):
    rep, _out = report
    assert rep["labels"] == list(RFP.LABELS) and len(rep["labels"]) == 5
    assert rep["labels"][0] == "EXPLORATION - NOT A VERDICT"


def test_labels_name_the_poll_conditions_and_the_confirmation_rule(report):
    rep, _out = report
    blob = " ".join(rep["labels"])
    assert "12.4s poll" in blob and "16.1s old" in blob and "29%" in blob
    assert "2026-08-13" in blob
    # 다섯째 라벨: 그 조건이 흔들렸다는 실측을 같은 자리에 적는다. 셋째를 지우지
    # 않는다 - 두 값이 나란히 있어야 어느 쪽을 인용했는지 보인다.
    assert "16.8 / 18.0 / 18.1s" in blob and ":29 and :59" in blob


def test_the_report_never_claims_a_verdict(report):
    rep, _out = report
    blob = json.dumps(rep).lower()
    for bad in RFP.FORBIDDEN_PHRASES:
        assert bad.lower() not in blob, f"the runner wrote a verdict word: {bad!r}"


def test_costs_are_not_subtracted(report):
    """비용 차감은 G-3 이고 사전등록이 필요하다. 이 단계가 하면 안 되는 일이다."""
    rep, _out = report
    assert "NOT subtracted" in rep["design"]["costs"]


def test_the_top_gainers_price_selection_caveat_is_always_attached(report):
    """`TOP_GAINERS` 는 서버가 가격으로 고른 목록이다 - 그 사실이 빠지면 인용이 틀린다."""
    rep, _out = report
    assert "price-ranked list on the SERVER side" in rep["design"]["top_gainers_caveat"]
    assert {"TOP_GAINERS", "TOSS_SECURITIES_TRADING_VOLUME"} == {
        r["ranking_type"] for r in rep["cells"]}


# --------------------------------------------------------------------------- #
# 9. 산출물의 모양
# --------------------------------------------------------------------------- #
def test_metric_manifest_is_exactly_what_the_summary_emits(report):
    """부분집합이 아니라 **동일 집합**이다 - 지표를 늘리고 매니페스트를 잊으면 깨진다."""
    rep, _out = report
    a = _cell(rep)["arms"]["real_all"]
    got = {k for k, v in a.items() if isinstance(v, dict) and "n" in v}
    assert got == set(RFP.REPORTED_METRICS)
    assert list(rep["design"]["metrics"]) == list(RFP.REPORTED_METRICS)


def test_the_funnel_never_grows_as_it_narrows(report):
    """깔때기는 좁아지기만 해야 한다. 늘어나면 어딘가에서 표본이 새로 들어온 것이다."""
    rep, _out = report
    for r in rep["cells"]:
        f = r["funnel"]
        assert f["n_events_regular"] >= f["n_symbol_has_tape"] >= f["n_anchored"]


def test_anchor_lag_is_reported_as_a_distribution_not_a_single_number(report):
    """`docs/44` §3-2 가 배운 것 - 지연은 상수가 아니라 "다음 체결이 안 온다" 이다."""
    rep, _out = report
    lag = _cell(rep)["funnel"]["anchor_lag_s"]
    assert lag["n"] > 0
    assert {"p10", "p50", "p90", "max", "mean"} <= set(lag)


def test_two_scales_are_both_reported(report):
    rep, _out = report
    sc = _cell(rep)["scales"]["real_all"]
    assert sc["event_weighted"] is not None and sc["symbol_uniform"] is not None


def test_symbol_uniform_scale_differs_when_one_symbol_dominates():
    """눈금이 실제로 다른 답을 낼 수 있어야 두 눈금을 내는 의미가 있다."""
    v = np.asarray([1.0, 1.0, 1.0, 1.0, 0.0])
    s = np.asarray(["A", "A", "A", "A", "B"], dtype=object)
    out = RFP.two_scales(v, s)
    assert out["event_weighted"] == pytest.approx(0.8)
    assert out["symbol_uniform"] == pytest.approx(0.5)


def test_pairing_profile_counts_and_profiles_the_lost_side():
    """`docs/44` §14-4 - 세는 것만으로는 부족하다. 잃은 쪽을 프로파일해야 한다."""
    raw = {"nbar60": np.asarray([30.0, 2.0]), "rv60": np.asarray([0.01, np.nan]),
           f"n_bars_{RFP.HEADLINE_H}s": np.asarray([12.0, 0.0])}
    out = RFP.pairing_profile(raw, np.asarray([1.0, 40.0]),
                              np.asarray([True, False]))
    assert out["kept"]["n"] == 1 and out["lost"]["n"] == 1
    assert out["lost"]["nbar60_p50"] == 2.0
    assert out["lost"]["rv60_missing_share"] == 1.0
    assert out["lost"]["forward_no_bar_share"] == 1.0


def test_pairing_census_adds_up(report):
    """짝을 잃은 수는 사유 셋의 합이어야 한다 - 조용히 줄어드는 자리가 없어야 한다."""
    rep, _out = report
    seen = 0
    for r in rep["cells"]:
        for arm in rep["design"]["placebo_arms"]:
            p = r["arms"].get(arm, {}).get("pairing")
            if not p:
                continue
            assert p["n_fires"] == p["n_paired"] + p["n_unpaired"]
            assert p["n_unpaired"] == (p["unpaired_key_missing"]
                                       + p["unpaired_empty_band"]
                                       + p["unpaired_gap_excluded_only"])
            seen += 1
    assert seen > 0


def test_eras_are_labelled_and_never_merged(report):
    """D-21 경계를 넘어 뭉치지 않는다 - 시대가 세션마다 붙어 있어야 한다."""
    rep, _out = report
    assert {s["era"] for s in rep["sessions"]} <= {"d21_before", "d21_after"}
    assert rep["window"]["d21_boundary_utc"] == HE.D21_BOUNDARY_UTC
    for r in rep["cells"]:
        assert set(r["by_era"]) <= {"d21_before", "d21_after"}


def test_era_of_puts_the_boundary_itself_in_the_after_era():
    b = HE.iso_ms(HE.D21_BOUNDARY_UTC)
    assert RFP.era_of(b - 1) == "d21_before" and RFP.era_of(b) == "d21_after"


def test_runner_writes_outside_the_source_tree(report):
    _rep, out = report
    assert (out / "ranking_forward_path.json").exists()
    assert "tossmon" not in str(out)


def test_console_output_is_pure_ascii(report, capsys):
    """cp949 콘솔에서 비 ASCII 는 `UnicodeEncodeError` 로 죽는다 (README 금지 항목)."""
    rep, _out = report
    RFP.print_report(rep)
    text = capsys.readouterr().out
    assert text.strip()
    text.encode("ascii")


def test_session_bars_reproduce_the_repos_canonical_second_bars(db):
    """새 적재기가 `tick_resolution.second_bars` 와 **같은 집계**여야 한다.

    창을 인자로 받으려고 새로 썼을 뿐이고, 값이 달라지면 `docs/44` 의 어떤 수치와도
    나란히 놓을 수 없다. 그래서 겹치는 창에서 **막대·건수·vwap 이 완전히 같은지**
    본다 (라이브 DB 2026-08-14 정규장 MDXH 5,728 막대에서도 차이 0 을 확인했다).
    """
    from tossmon.analysis.measure.tick_resolution import WINDOW_START_MS, second_bars
    day = _day0(DAYS[0]) + OPEN_MS
    assert day >= WINDOW_START_MS, "the reference loader would cut this day off"
    conn = HE.open_ro(db)
    try:
        mine = RFP.session_second_bars(conn, day, day + 2400 * MS)
        ref = second_bars(conn, "BBB")
    finally:
        conn.close()
    ts_m, n_m, _lo, _hi, vw_m = mine["BBB"]
    ts_r, n_r, _lr, _hr, vw_r = ref
    k = (ts_r >= day) & (ts_r < day + 2400 * MS)
    assert np.array_equal(ts_m, ts_r[k]) and np.array_equal(n_m, n_r[k])
    assert np.nanmax(np.abs(vw_m - vw_r[k])) == 0.0


def test_the_db_is_opened_read_only(db):
    """라이브 수집기가 같은 파일에 쓰고 있다. 쓰기로 열면 안 된다."""
    conn = HE.open_ro(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM rankings_snap")
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 10. 새 추첨기는 **같은 추첨**이다 - 팔 사이 차이가 기계에서 나오면 안 된다
# --------------------------------------------------------------------------- #
def _universe_for_draw(db):
    conn = HE.open_ro(db)
    try:
        s = RFP.scan_sessions(conn, HE.holdout_floor_ms(),
                              int(conn.execute("SELECT MAX(snap_ms) "
                                               "FROM rankings_snap").fetchone()[0]))[0]
        bars = RFP.session_second_bars(conn, s["open_ms"], s["close_ms"])
    finally:
        conn.close()
    from tossmon.analysis.measure.density_matched_placebo import (
        add_trade_count, stratified_index)
    from tossmon.analysis.measure.vol_matched_placebo import build_universe
    uni = RFP.add_forward_depth_proxy(
        add_trade_count(build_universe(bars, lookback_s=RFP.VOL_LOOKBACK_S), bars))
    syms = sorted(uni)
    return uni, syms, stratified_index(uni, syms)


@pytest.mark.parametrize("mb,ms", [(False, False), (True, False), (True, True)])
def test_draw_banded_is_the_same_draw_as_draw_stratified_when_the_band_is_off(db, mb, ms):
    """사다리의 규율은 *"팔 사이에 달라지는 것은 밴드 하나뿐"* 이다(`docs/44` §14-1).

    새 팔만 다른 추첨기를 쓰면 팔 사이의 차이가 밴드가 아니라 **기계**에서 나온다.
    그래서 `match_fwd=False` 에서 두 함수가 **배열까지** 같은지 본다 - 통계적으로
    비슷한 것으로는 부족하다.
    """
    from tossmon.analysis.measure.density_matched_placebo import draw_stratified
    uni, syms, index = _universe_for_draw(db)
    si = syms.index("BBB")
    fs = np.full(6, si, dtype="int64")
    fb = np.asarray([300, 700, 1100, 1500, 1900, 2300], dtype="int64")
    kw = dict(match_band=mb, match_strat=ms, draws=RFP.MATCH_DRAWS,
              gap_s=RFP.SELF_GAP_S)
    a = draw_stratified(uni, syms, index, fs, fb,
                        rng=np.random.default_rng(RFP.SEED), **kw)
    b = RFP.draw_banded(uni, syms, index, fs, fb, match_fwd=False,
                        rng=np.random.default_rng(RFP.SEED), **kw)
    assert a["n_paired"] > 0, "nothing was paired - the test would be vacuous"
    assert np.array_equal(a["sym"], b["sym"])
    assert np.array_equal(a["bar"], b["bar"])
    assert np.array_equal(a["slot"], b["slot"])
    assert np.array_equal(a["paired"], b["paired"])
    for k in ("n_fires", "n_paired", "n_unpaired", "unpaired_key_missing",
              "unpaired_empty_band", "unpaired_gap_excluded_only"):
        assert a[k] == b[k], k


def test_the_forward_depth_band_actually_bites(db):
    """"맞췄다" 는 주장이 아니라 관측이어야 한다 - 뽑힌 막대가 밴드 안에 있어야 한다."""
    from tossmon.analysis.measure.density_matched_placebo import DENSITY_TOL, density_band
    uni, syms, index = _universe_for_draw(db)
    si = syms.index("BBB")
    fs = np.full(6, si, dtype="int64")
    fb = np.asarray([300, 700, 1100, 1500, 1900, 2300], dtype="int64")
    d = RFP.draw_banded(uni, syms, index, fs, fb, match_band=True,
                        match_strat=True, match_fwd=True, draws=RFP.MATCH_DRAWS,
                        gap_s=RFP.SELF_GAP_S, rng=np.random.default_rng(RFP.SEED))
    assert d["sym"].size > 0, "nothing was drawn - the test would be vacuous"
    fw = np.asarray(uni["BBB"][RFP.FWD_PROXY_KEY], dtype="float64")
    for slot, bar in zip(d["slot"], d["bar"]):
        lo, hi = density_band(float(fw[fb[int(slot)]]), DENSITY_TOL)
        assert lo <= fw[int(bar)] <= hi


def test_a_wider_ladder_never_pairs_more_than_a_narrower_one(db):
    """밴드를 켜면 후보가 줄기만 한다 - 늘어나면 후보 풀이 팔마다 다른 것이다."""
    uni, syms, index = _universe_for_draw(db)
    si = syms.index("BBB")
    fs = np.full(6, si, dtype="int64")
    fb = np.asarray([300, 700, 1100, 1500, 1900, 2300], dtype="int64")
    sizes = []
    for _a, mb, ms, mf in RFP.PLACEBO_ARMS:
        d = RFP.draw_banded(uni, syms, index, fs, fb, match_band=mb,
                            match_strat=ms, match_fwd=mf, draws=RFP.MATCH_DRAWS,
                            gap_s=RFP.SELF_GAP_S,
                            rng=np.random.default_rng(RFP.SEED))
        sizes.append(d["band_size"].get("p50"))
    got = [s for s in sizes if s is not None]
    assert len(got) == len(sizes)
    assert got == sorted(got, reverse=True)


# --------------------------------------------------------------------------- #
# 11. 새 정합 키는 **미래를 안 쓴다**
# --------------------------------------------------------------------------- #
def test_the_forward_depth_proxy_never_looks_past_its_own_bar():
    """접두사 불변 - 뒤를 잘라내고 다시 불러도 같은 값이어야 한다.

    이 성질이 `nbar300` 을 정합 축에 넣을 수 있게 하는 **유일한** 근거다. 깨지면
    이 팔은 결과와 같은 창의 양으로 정합한 것이 되고, 그게 바로 안 하려던 일이다.
    """
    ts = np.arange(0, 900, 1, dtype="int64") * 1000
    ts = ts[(ts // 1000) % 3 != 1]
    full = RFP.trailing_bar_count(ts, lookback_s=RFP.FWD_PROXY_LOOKBACK_S)
    for cut in (50, 120, 300, 500):
        part = RFP.trailing_bar_count(ts[:cut],
                                      lookback_s=RFP.FWD_PROXY_LOOKBACK_S)
        assert np.array_equal(part, full[:cut])


def test_the_forward_depth_proxy_counts_the_trailing_window_inclusive():
    """자기 막대를 포함한 직전 `lookback` 초. 경계는 `nbar60` 과 같은 `side='left'` 다."""
    ts = np.asarray([0, 1000, 2000, 3000, 300_000, 300_001], dtype="int64")
    got = RFP.trailing_bar_count(ts, lookback_s=300)
    assert got.tolist() == [1, 2, 3, 4, 5, 5]


def test_the_new_key_is_a_match_key_and_the_realised_depth_is_not(report):
    """정합 축에 든 것과 안 든 것을 산출물이 **말로** 구분해 두는지."""
    rep, _out = report
    mk = rep["design"]["match_keys"]
    assert mk["forward_depth_proxy"] == RFP.FWD_PROXY_KEY
    assert RFP.FWD_PROXY_KEY in RFP.BALANCE_KEYS
    fd = rep["design"]["forward_depth"]
    assert "TRAILING" in fd["chosen"]
    assert "REALISED" in fd["rejected"]
    # 실현 전방 깊이는 어느 팔의 밴드도 아니다
    assert f"n_bars_{RFP.HEADLINE_H}s" not in RFP.BALANCE_KEYS


def test_depth_strata_are_a_diagnostic_and_never_carry_a_ci(report):
    """사후 양으로 자른 표에 CI 를 붙이면 그건 추정처럼 읽힌다."""
    rep, _out = report
    seen = 0
    for r in rep["cells"]:
        for _arm, rows in r["depth_strata"].items():
            for b in rows:
                assert b["ci95"] is None
                assert "POST-anchor" in b["no_ci_reason"]
                seen += 1
    assert seen > 0


def test_depth_strata_bins_never_lose_a_pair(report):
    """층으로 자를 때 조용히 사라지는 짝이 없어야 한다."""
    rep, _out = report
    for r in rep["cells"]:
        for arm, rows in r["depth_strata"].items():
            if not rows:
                continue
            a = r["arms"].get(arm + "__real_on_paired", {})
            n = a.get(f"n_bars_{RFP.HEADLINE_H}s", {}).get("n_finite")
            if n is None:
                continue
            assert sum(b["n_real"] for b in rows) == n


# --------------------------------------------------------------------------- #
# 12. **확증 팔을 안 연다** - 규율이 아니라 코드가 막는다
# --------------------------------------------------------------------------- #
def test_the_exploration_arm_is_nine_named_sessions():
    """`G2G3-PREREG` §2-1 의 표본이 상수로 박혀 있는지."""
    assert len(RFP.EXPLORATION_SESSIONS) == 9
    assert RFP.EXPLORATION_SESSIONS[0] == "2026-07-31"
    assert RFP.EXPLORATION_SESSIONS[-1] == "2026-08-12"
    assert all(s < "2026-08-13" for s in RFP.EXPLORATION_SESSIONS)
    assert RFP.CONFIRMATION_FLOOR_UTC.startswith("2026-08-13")


def test_a_session_past_the_floor_is_planted_but_never_reaches_the_report(tmp_path):
    """데이터가 우연히 안 걸리는 것과 **코드가 막는 것**은 다르다.

    확증 팔에 드는 날(08-14)을 통째로 심고, 탐색 모드에서 그 날이 산출물에 안
    닿는지 본다. 같은 DB 를 `--all-sessions` 로 열면 그 날이 나타나야 한다 -
    안 나타나면 이 테스트가 막은 것이 아니라 데이터가 없는 것이다.
    """
    import tossmon.analysis.measure.ranking_forward_path as M
    path = tmp_path / "floor.db"
    conn = sqlite3.connect(path)
    _make_db(path, with_holdout=False)
    conn.close()
    conn = sqlite3.connect(path)
    conn.executemany("INSERT INTO rankings_snap (snap_ms, ranking_type, duration, "
                     "rank, symbol, last_u, vol_qu, amount_u) VALUES (?,?,?,?,?,?,?,?)",
                     _rank_rows("2026-08-14"))
    conn.executemany("INSERT INTO trades_snap VALUES (?,?,?,?)",
                     _trade_rows("2026-08-14"))
    conn.commit()
    conn.close()

    kept = M.run(path)
    assert [s["session"] for s in kept["sessions"]] == list(DAYS)
    assert all(s["session"] in M.EXPLORATION_SESSIONS for s in kept["sessions"])

    opened = M.run(path, exploration_only=False)
    assert "2026-08-14" in [s["session"] for s in opened["sessions"]], (
        "the planted session is missing, so the guard is not what removed it")


def test_the_report_names_the_arm_it_used(report):
    rep, _out = report
    arm = rep["arm"]
    assert arm["name"] == "exploration" and arm["exploration_only"] is True
    assert arm["sessions_used"] and all(s in RFP.EXPLORATION_SESSIONS
                                        for s in arm["sessions_used"])
    assert arm["confirmation_floor_utc"] == RFP.CONFIRMATION_FLOOR_UTC


# --------------------------------------------------------------------------- #
# 13. 표적 층 - 가격은 **사건 뒤에** 자르는 축이다
# --------------------------------------------------------------------------- #
def test_tier_codes_use_the_repos_own_price_band_edge():
    """새 경계를 만들지 않는다 - `hires_events.PRICE_BANDS_U` 의 `p5_10` 하한이다."""
    edge = next(lo for name, lo, _hi in HE.PRICE_BANDS_U if name == "p5_10")
    assert RFP.TIER_SPLIT_U == edge
    got = RFP.tier_codes(np.asarray([0.0, edge - 1, edge, edge + 1, np.nan]))
    assert got.tolist() == ["u5", "u5", "o5", "o5", "unknown"]


def test_every_tier_is_a_subset_of_the_all_tier(report):
    """층은 자르는 축이지 새 표본이 아니다 - 층을 더해도 `all` 을 못 넘는다."""
    rep, _out = report
    by = {}
    for r in rep["cells"]:
        by.setdefault((r["ranking_type"], r["kind"], r["cell"]), {})[r["tier"]] = r
    seen = 0
    for _k, d in by.items():
        if set(d) != set(RFP.TIERS):
            continue
        a, u, o = d["all"]["funnel"], d["u5"]["funnel"], d["o5"]["funnel"]
        assert u["n_events_regular"] + o["n_events_regular"] + a["n_tier_unknown"] == \
            a["n_events_regular"]
        assert u["n_anchored"] + o["n_anchored"] <= a["n_anchored"]
        seen += 1
    assert seen > 0


def test_tiers_carry_only_the_two_strongest_arms(report):
    """층에 붙는 팔은 **돌리기 전에 정한 둘**이다 - 늘리면 분모만 커진다."""
    rep, _out = report
    for r in rep["cells"]:
        if r["tier"] == "all":
            continue
        assert set(r["diff"]) <= set(RFP.TIER_ARMS)

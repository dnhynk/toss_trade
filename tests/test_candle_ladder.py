"""`candle_ladder` ((가′) 사다리, `docs/71`) 가 지키는 것.

사다리에서 조용히 틀릴 수 있는 자리는 **위약 쪽**이다 — 사건은 개정 6 의 자를 그대로 쓰지만
위약은 이 모듈이 처음 만든다. 그래서 얼린 것 넷을 절대 시각·절대 값으로 고정하고 돌연변이로
red 를 확인한다 (`docs/71` §3):

    M1  placebo_taus 가 T_b (내용 시작이 아니라 라벨)      위약이 사건보다 한 봉 뒤에서 시작한다
    M2  자기 간격 `>=`                                    간격 정확히 600 초인 후보가 들어온다
    M3  자기 간격 300 초                                  사건의 측정 구간과 겹치는 이웃이 들어온다
    M4  crv5 밴드 제거                                    변동성 정합이 사라진다
    M5  cvol5 밴드 제거                                   거래량 정합이 사라진다
    M6  키 검사 제거                                      키 없는 분이 위약이 된다
    M7  층이 anchored (first_in_regular 아님)             재진입이 사다리에 들어온다
    M8  앵커 없는 첫 진입을 재기                          선언 밖 모집단이 수치를 만든다

1. **위약의 `tau` 는 봉의 내용 시작이다.** `L0(tau) == T_b` 가 성립해야 그 봉이 위약 자신의
   t0 봉이 되어 사건과 **같은 자**가 된다. 이것이 이 모듈의 핵심 얼림이다.
2. **자기 간격은 엄격 부등호 600 초.** 정확히 600 초인 이웃은 **밖**이다.
3. **밴드 둘이 각각 문다.** 합성 세션에 거래량 감시병(한 분만 vol ×100)과 변동성 감시병
   (한 분만 low 를 내려 `ln(high/low)` 를 키운 것 — **고가는 안 건드린다**)을 심어, 그 분을
   담는 창이 후보에서 빠지는 것을 센다.
4. **층 하나만.** 같은 세션의 재진입(첫 진입 아님)과 앵커 없는 첫 진입은 사다리에 안 들어온다.
   후자는 **세기만** 하고 수익을 만들지 않는다.
5. **통계량은 얼린 그대로다** — 평균 차, 세션 군집 부트스트랩, 본페로니 분모 1, 군집 5 미만이면
   CI 없음. CI 하한은 0 이 아니라 **얼린 편향선 +0.91%p** 와 견준다.
6. **한 시대만.** 08-14(버린 것)·08-26(확증 바닥)을 심어 두고 산출물에 없는 것을 본다.
7. **판정 문구 없음 · ASCII.**
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from tossmon.analysis import hires_events as HE
from tossmon.analysis.measure import candle_ladder as CL
from tossmon.analysis.measure import candle_ruler as CR
from tossmon.analysis.measure import ranking_forward_path as RFP
from tossmon.analysis.measure import ruler_bias as RB

MS = 1000
MIN_MS = 60_000

# --------------------------------------------------------------------------- #
# 0. 얼린 상수 — 사전등록에서 옮겨 적은 것이지 여기서 고른 것이 아니다
# --------------------------------------------------------------------------- #
def test_every_dial_is_a_reused_constant_and_the_bias_line_is_the_frozen_one() -> None:
    """새 눈금을 만들지 않는다 — 전부 개정 3·5 의 상수다."""
    assert CL.SELF_GAP_S == CR.CANDLE_SELF_GAP_S == 600
    assert CL.RV_TOL == CR.CANDLE_RV_TOL == 0.20
    assert CL.VOL_FACTOR == CR.CANDLE_VOL_FACTOR == 1.2
    assert CL.DRAWS == RFP.MATCH_DRAWS == 3
    assert CL.SEED == RFP.SEED == 20260818
    assert CL.N_COMPARISONS == 1
    assert CL.STRATUM == "first_in_regular" and CL.STRATUM in CR.STRATA
    # `docs/70` §4 의 first_in_regular 짝 편향(+0.91%p) = `G2G3-PREREG` 개정 6 [보정 뒤]의 통과선
    assert CL.FIRST_ENTRY_BIAS == pytest.approx(0.0091)
    assert CL.DECLARED == ("TOSS_SECURITIES_TRADING_VOLUME", "realtime",
                           "E1_new_entry", "N10")


# --------------------------------------------------------------------------- #
# 1. 위약의 tau — 이 모듈의 핵심 얼림
# --------------------------------------------------------------------------- #
REG_OPEN = 1_780_320_600_000                  # 2026-06-01T13:30:00Z
LBL = {m: REG_OPEN + m * MIN_MS for m in range(0, 20)}


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_hardcoded_times_are_what_the_comments_say() -> None:
    assert _iso(REG_OPEN) == "2026-06-01T13:30:00Z"
    assert _iso(LBL[6]) == "2026-06-01T13:36:00Z"


def test_placebo_tau_is_the_bar_content_start_so_that_bar_is_its_own_t0_bar() -> None:
    """`tau = T_b - 60s` 이면 `L0(tau) == T_b` — 그 봉이 위약의 t0 봉이 되어 **제외**된다.

    `T_b` 를 그대로 쓰면(M1) `L0` 가 한 봉 뒤로 밀려 위약이 사건과 다른 자를 갖는다.
    """
    ts = np.asarray([LBL[6], LBL[7], LBL[11]], dtype="int64")
    tau = CL.placebo_taus(ts)
    assert tau.tolist() == [LBL[5], LBL[6], LBL[10]]
    assert _iso(tau[0]) == "2026-06-01T13:35:00Z"
    # 핵심 항등식 - 사건과 같은 자
    assert RB.t0_bar_label(tau).tolist() == ts.tolist()


def test_the_placebo_forward_window_is_the_five_bars_after_its_own_bar() -> None:
    """위약 후보 봉 `T_b` = 13:36 이면 전방 경로는 13:37..13:41 — **자기 봉은 밖**이다."""
    rows = [(LBL[m], 1.00, 1.00 + 0.01 * m) for m in (5, 6, 7, 8, 9, 10, 11, 12)]
    cand = RB.ohlc_arrays(np.asarray([r[0] for r in rows], dtype="int64"),
                          np.asarray([r[1] for r in rows]) * 1e6,
                          np.asarray([r[2] for r in rows]) * 1e6)
    tau = CL.placebo_taus(np.asarray([LBL[6]], dtype="int64"))
    f = RB.bar_forward_at(cand, tau)
    assert f["l0_ms"][0] == LBL[6]                      # 자기 봉이 t0 봉
    assert f["entry_label_ms"][0] == LBL[7]             # 진입은 그 다음 봉
    assert f["n_bars"][0] == 5                          # 13:37..13:41
    assert f["max_ret"][0] == pytest.approx(1.11 / 1.00 - 1.0)   # 13:41 봉(고가 1.11)


# --------------------------------------------------------------------------- #
# 2~3. 간격과 밴드 — 순수 함수 위에서 절대 값으로
# --------------------------------------------------------------------------- #
T0 = REG_OPEN + 3600 * MS


def _mask(taus, rv, vol, ev_rv=1.0, ev_vol=100.0, **kw):
    return CL.placebo_candidate_mask(np.asarray(rv, dtype="float64"),
                                     np.asarray(vol, dtype="float64"),
                                     np.asarray(taus, dtype="int64"),
                                     T0, ev_rv, ev_vol, **kw)


def test_the_self_gap_is_a_strict_600s_on_both_sides() -> None:
    """정확히 600 초는 **밖**, 600.001 초는 안. `>=`(M2)·300 초(M3) 가 red 가 되는 자리."""
    taus = [T0 - 600 * MS - MS, T0 - 600 * MS, T0, T0 + 600 * MS, T0 + 600 * MS + MS]
    m = _mask(taus, [1.0] * 5, [100.0] * 5)
    assert m["gap"].tolist() == [True, False, False, False, True]
    assert CL.SELF_GAP_S == 600


def test_the_key_rule_drops_minutes_with_no_volatility_or_no_volume() -> None:
    """키 정의는 개정 5 그대로 `crv5 > 0 ∧ cvol5 > 0` (M6 의 자리)."""
    taus = [T0 + 700 * MS] * 4
    m = _mask(taus, [1.0, 0.0, 1.0, np.nan], [100.0, 100.0, 0.0, 100.0])
    assert m["gap"].all()
    assert m["key"].tolist() == [True, False, False, False]


def test_each_band_bites_at_its_own_edge() -> None:
    """`crv5` ±20%(M4) 와 `cvol5` ×1.2(M5) 가 각각 무는 것을 경계값으로 고정한다."""
    taus = [T0 + 700 * MS] * 5
    # crv5 밴드: [0.8, 1.2]
    m = _mask(taus, [0.79, 0.80, 1.00, 1.20, 1.21], [100.0] * 5)
    assert m["rv"].tolist() == [False, True, True, True, False]
    # cvol5 밴드: [100/1.2, 100*1.2] - 로그 대칭
    m = _mask(taus, [1.0] * 5, [100 / 1.2 - 0.01, 100 / 1.2, 100.0, 120.0, 120.01])
    assert m["all"].tolist() == [False, True, True, True, False]
    # 밴드는 간격·키 **뒤에** 걸린다 - 누적이다
    m = _mask([T0], [1.0], [100.0])
    assert not m["gap"][0] and not m["all"][0]


def test_an_event_whose_own_key_is_undefined_gets_no_candidates() -> None:
    m = _mask([T0 + 700 * MS] * 3, [1.0] * 3, [100.0] * 3, ev_rv=0.0)
    assert m["event_key_ok"] is False
    assert not m["rv"].any() and not m["all"].any()
    assert m["key"].all()          # 후보 자체는 살아 있다 - 못 맞추는 것은 사건 쪽이다


def test_draws_are_seeded_sized_and_taken_from_the_candidates_only() -> None:
    idx = np.asarray([3, 9, 27], dtype="int64")
    a = CL.draw_from(np.random.default_rng(CL.SEED), idx)
    b = CL.draw_from(np.random.default_rng(CL.SEED), idx)
    assert a.tolist() == b.tolist() and a.size == CL.DRAWS == 3
    assert set(a.tolist()) <= set(idx.tolist())
    assert CL.draw_from(np.random.default_rng(CL.SEED), np.zeros(0, "int64")).size == 0


# --------------------------------------------------------------------------- #
# 합성 DB — 손으로 셀 수 있는 세션
# --------------------------------------------------------------------------- #
OPEN_S = HE.REGULAR_OPEN_S
POLL_MS = 12_000
N_SNAPS = 200
EV_SNAP, LEAVE_SNAP, REENTRY_SNAP = 101, 130, 180      # BBB: 첫 진입 / 이탈 / 재진입
CCC_IN, CCC_OUT = 60, 70                               # CCC: 첫 진입인데 체결이 없다
RTYPE, DUR = "TOSS_SECURITIES_TRADING_VOLUME", "realtime"
LOUD_MIN = 30          # 거래량 감시병 - 그 분만 vol x100
WILD_MIN = 45          # 변동성 감시병 - 그 분만 low 를 내린다 (고가는 그대로!)
SPIKE_LABEL_S = 1380   # 사건의 전방 창 안 급등 봉
FLAT_HIGH_U, BASE_U = 3_006_000, 3_000_000
SPIKE_U = 3_300_000

#: 손으로 센 값. t0 = 101*12 = 1212 초 -> L0 = 1260, F = 1320..1560 (5 봉),
#: 진입 = 1320 봉의 시가 3,000,000, 창 안 최고가 = 급등 봉 3,300,000.
EVENT_MAX_RET = SPIKE_U / BASE_U - 1.0                 # +10.0%
PLACEBO_MAX_RET = FLAT_HIGH_U / BASE_U - 1.0           # +0.2% (평평한 구간)
ENTRY_LAG_S = 48.0                                     # 1260 - 1212


def _day0(d: str) -> int:
    return int(pd.Timestamp(d + "T00:00:00Z").timestamp() * 1000)


def _rank_rows(day: str) -> list:
    base = _day0(day) + OPEN_S * MS
    rows = []
    for i in range(N_SNAPS):
        plan = [("AAA", 1, 1_500_000)]
        if EV_SNAP <= i < LEAVE_SNAP or i >= REENTRY_SNAP:
            plan.append(("BBB", 5, BASE_U))
        if CCC_IN <= i < CCC_OUT:
            plan.append(("CCC", 7, 2_000_000))
        for sym, rk, last_u in plan:
            rows.append((base + i * POLL_MS, RTYPE, DUR, rk, sym, last_u, 1, 1))
    for k in range(1, 11):
        rows.append((base - k * POLL_MS, RTYPE, DUR, 1, "AAA", 1_500_000, 1, 1))
    return rows


def _trade_rows(day: str) -> list:
    """**BBB 만** 체결이 있다 — CCC 는 앵커가 안 붙는 첫 진입이 된다."""
    base = _day0(day) + OPEN_S * MS
    return [("BBB", base + s * MS, BASE_U, 10) for s in range(0, 3600)]


def _candle_rows(day: str) -> list:
    base = _day0(day) + OPEN_S * MS
    rows = []
    for sym in ("BBB", "CCC"):
        for m in range(60):
            label = base + (m + 1) * MIN_MS
            o, h, lo, c, v = BASE_U, FLAT_HIGH_U, BASE_U, BASE_U, 1000
            if sym == "BBB":
                if m == LOUD_MIN:
                    v = 100_000                       # 거래량 밴드가 이 분을 담는 창을 버린다
                if m == WILD_MIN:
                    lo = 2_400_000                    # 변동성만 키운다 - 고가는 그대로
                if label == base + SPIKE_LABEL_S * MS:
                    h = SPIKE_U                       # 사건의 전방 창 안
            rows.append((sym, label, o, h, lo, c, v))
    return rows


def _make_db(path, days):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE rankings_snap (id INTEGER PRIMARY KEY AUTOINCREMENT,
            snap_ms INTEGER NOT NULL, ranking_type TEXT NOT NULL, duration TEXT NOT NULL,
            rank INTEGER NOT NULL, symbol TEXT NOT NULL, last_u INTEGER NOT NULL,
            vol_qu INTEGER NOT NULL, amount_u INTEGER NOT NULL);
        CREATE TABLE trades_snap (symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL,
            price_u INTEGER NOT NULL, qty_u INTEGER NOT NULL,
            PRIMARY KEY (symbol, ts_ms, price_u, qty_u)) WITHOUT ROWID;
        CREATE TABLE candles_1m (symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL,
            open_u INTEGER NOT NULL, high_u INTEGER NOT NULL, low_u INTEGER NOT NULL,
            close_u INTEGER NOT NULL, vol_qu INTEGER NOT NULL,
            PRIMARY KEY (symbol, ts_ms)) WITHOUT ROWID;
        CREATE TABLE promotions (id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL, from_tier INTEGER NOT NULL,
            to_tier INTEGER NOT NULL, reason TEXT NOT NULL, score REAL);
        CREATE TABLE orderbook_snap (id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL, snap_ms INTEGER NOT NULL, ts_ms INTEGER,
            bid1_u INTEGER, bid1_qu INTEGER, ask1_u INTEGER, ask1_qu INTEGER,
            depth_json TEXT NOT NULL, spread_u INTEGER, imbalance_signed REAL);
    """)
    for d in days:
        conn.executemany("INSERT INTO rankings_snap (snap_ms, ranking_type, duration, "
                         "rank, symbol, last_u, vol_qu, amount_u) VALUES (?,?,?,?,?,?,?,?)",
                         _rank_rows(d))
        conn.executemany("INSERT INTO trades_snap VALUES (?,?,?,?)", _trade_rows(d))
        conn.executemany("INSERT INTO candles_1m VALUES (?,?,?,?,?,?,?)", _candle_rows(d))
    conn.commit()
    conn.close()
    return path


B_DAYS = ("2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21", "2026-08-24", "2026-08-25")
MIXED = ("2026-08-14",) + B_DAYS + ("2026-08-26",)
TWO_DAYS = ("2026-08-18", "2026-08-19")


@pytest.fixture(scope="module")
def mixed_db(tmp_path_factory):
    return _make_db(tmp_path_factory.mktemp("cl") / "t.db", MIXED)


@pytest.fixture(scope="module")
def thin_db(tmp_path_factory):
    return _make_db(tmp_path_factory.mktemp("cl_thin") / "t.db", TWO_DAYS)


@pytest.fixture(scope="module")
def laddered(mixed_db):
    return CL.ladder(mixed_db, era="B")


@pytest.fixture(scope="module")
def report_b(mixed_db, tmp_path_factory):
    out = tmp_path_factory.mktemp("cl_out")
    assert CL.main(["prog", str(mixed_db), "--era", "B", "--out", str(out),
                    "--name", "b"]) == 0
    return json.loads((out / "b.json").read_text(encoding="utf-8")), out


# --------------------------------------------------------------------------- #
# 4. 층 하나 — 재진입도, 앵커 없는 첫 진입도 안 들어온다
# --------------------------------------------------------------------------- #
def test_only_anchored_first_entries_enter_the_ladder(laddered):
    """M7 의 자리 — 층이 `anchored` 면 재진입 6 건이 같이 들어와 12 가 된다."""
    ev = laddered["events"]
    assert len(ev) == 6                                  # 세션마다 BBB 첫 진입 하나
    assert set(ev.symbol) == {"BBB"}                     # CCC(앵커 없음)는 없다
    assert sorted(ev.session) == list(B_DAYS)
    base = {d: _day0(d) + OPEN_S * MS for d in B_DAYS}
    assert ev.t0_ms.tolist() == [base[d] + EV_SNAP * POLL_MS for d in B_DAYS]
    # 재진입(snap 180)은 어디에도 없다
    reentry = {base[d] + REENTRY_SNAP * POLL_MS for d in B_DAYS}
    assert not set(ev.t0_ms) & reentry


def test_unanchored_first_entries_are_counted_but_never_measured(laddered, report_b):
    """앵커 없는 첫 진입(CCC)은 **세기만** 한다 — 수익은 만들지도 싣지도 않는다 (M8)."""
    u = laddered["unanchored_first"]
    assert u["n"] == 6 and u["with_bar_forward"] == 6    # 봉은 있다. 그래도 안 잰다
    for df in (laddered["events"], laddered["draws"]):
        assert "CCC" not in set(df.symbol)
    rep, out = report_b
    assert rep["funnel"]["unanchored_first"]["n"] == 6
    assert "CCC" not in (out / "b_events.csv").read_text(encoding="utf-8")
    # 세기만 한다는 사실이 산출물에 문장으로 남는다
    assert "counted only" in rep["funnel"]["unanchored_first"]["note"]


# --------------------------------------------------------------------------- #
# 5. 손으로 센 값과 깔때기
# --------------------------------------------------------------------------- #
def test_event_values_are_the_hand_computed_ones(laddered):
    ev = laddered["events"]
    assert ev.bar_n_bars.tolist() == [5] * 6
    assert ev.bar_entry_u.tolist() == pytest.approx([float(BASE_U)] * 6)
    assert ev.bar_entry_lag_s.tolist() == pytest.approx([ENTRY_LAG_S] * 6)
    assert ev.bar_max_ret.tolist() == pytest.approx([EVENT_MAX_RET] * 6)
    assert ev.paired.all() and ev.n_draws.tolist() == [CL.DRAWS] * 6


def test_the_funnel_shows_each_stage_biting(laddered):
    """후보 40 -> 키 39 -> 변동성 밴드 31 -> 거래량 밴드 25. 감시병 둘이 실제로 문다."""
    ev = laddered["events"]
    assert ev.n_cand_gap.tolist() == [40] * 6
    assert ev.n_cand_key.tolist() == [39] * 6            # 창이 빈 첫 분 하나
    assert ev.n_cand_rv.tolist() == [31] * 6             # 변동성 감시병 + 부분 창
    assert ev.n_cand.tolist() == [25] * 6                # 거래량 감시병 6 분
    assert (ev.n_cand < ev.n_cand_rv).all()              # 거래량 밴드가 문다 (M5)
    assert (ev.n_cand_rv < ev.n_cand_key).all()          # 변동성 밴드가 문다 (M4)


def test_placebo_draws_sit_outside_the_gap_inside_the_bands_on_the_same_ruler(laddered):
    dr = laddered["draws"]
    ev = laddered["events"]
    assert len(dr) == 18 == 6 * CL.DRAWS
    # 자기 간격 밖 (엄격 600 초)
    assert (np.abs(dr.tau_ms.to_numpy("int64")
                   - dr.event_t0_ms.to_numpy("int64")) > CL.SELF_GAP_S * MS).all()
    # 밴드 안 - 사건의 키를 기준으로
    e_rv, e_vol = float(ev.crv5.iloc[0]), float(ev.cvol5.iloc[0])
    assert (np.abs(dr.crv5 / e_rv - 1.0) <= CL.RV_TOL + 1e-12).all()
    assert ((dr.cvol5 >= e_vol / CL.VOL_FACTOR - 1e-6)
            & (dr.cvol5 <= e_vol * CL.VOL_FACTOR + 1e-6)).all()
    # 같은 자 - 위약도 전방 5 봉, 평평한 구간이라 값이 하나로 떨어진다
    assert dr.bar_n_bars.tolist() == [5] * 18
    assert dr.bar_max_ret.tolist() == pytest.approx([PLACEBO_MAX_RET] * 18)
    # 위약이 사건의 급등을 먹지 못한다 - 간격이 막는다
    assert (dr.bar_max_ret < EVENT_MAX_RET).all()


# --------------------------------------------------------------------------- #
# 6. 통계량 — 얼린 그대로
# --------------------------------------------------------------------------- #
def test_the_statistic_is_the_frozen_one_and_the_arithmetic_closes(laddered, report_b):
    rep, _ = report_b
    h, s = rep["headline"], laddered["stat"]
    assert s["n_clusters"] == 6 and s["n_real"] == 6 and s["n_placebo"] == 18
    assert h["mean_real"] == pytest.approx(EVENT_MAX_RET)
    assert h["mean_placebo"] == pytest.approx(PLACEBO_MAX_RET)
    assert h["mean_diff"] == pytest.approx(EVENT_MAX_RET - PLACEBO_MAX_RET)
    assert h["median_diff"] == pytest.approx(EVENT_MAX_RET - PLACEBO_MAX_RET)
    # 여섯 세션이 똑같으므로 군집 부트스트랩은 한 점으로 모인다 - CI 기계가 돌았다는 증거
    assert h["ci95"] == pytest.approx([EVENT_MAX_RET - PLACEBO_MAX_RET] * 2)
    assert rep["design"]["n_comparisons"] == 1
    assert h["ci_bonferroni"] == pytest.approx(h["ci95"])


def test_the_ci_is_read_against_the_frozen_bias_line_not_against_zero(report_b):
    rep, _ = report_b
    h = rep["headline"]
    assert rep["design"]["first_entry_bias_line"] == pytest.approx(CL.FIRST_ENTRY_BIAS)
    assert h["ci_low_minus_bias_line"] == pytest.approx(h["ci95"][0] - CL.FIRST_ENTRY_BIAS)
    assert "not against zero" in rep["design"]["no_verdict"]


def test_ci_is_withheld_when_the_clusters_are_too_few(thin_db):
    """군집 5 미만이면 CI 를 내지 않는다 — 기존 규율(`STRATEGY-VERDICTS` §4.4-B) 그대로."""
    res = CL.ladder(thin_db, era="B")
    assert res["stat"]["n_clusters"] == 2 < RFP.MIN_SESSION_CLUSTERS
    assert res["stat"]["ci95"] is None
    assert "no pooled CI" in res["stat"]["ci_withheld"]
    rep = CL.build_report(res)
    assert rep["headline"]["ci95"] is None and rep["headline"]["ci_low_minus_bias_line"] is None


def test_per_session_rows_add_up(report_b, laddered):
    rep, _ = report_b
    per = rep["per_session"]
    assert [s["session"] for s in per] == list(B_DAYS)
    assert sum(s["n_paired"] for s in per) == int(laddered["events"].paired.sum())
    assert sum(s["n_draws"] for s in per) == len(laddered["draws"])
    for s in per:
        assert s["diff_mean"] == pytest.approx(EVENT_MAX_RET - PLACEBO_MAX_RET)


# --------------------------------------------------------------------------- #
# 7. 한 시대만 · 판정 문구 없음 · ASCII
# --------------------------------------------------------------------------- #
def test_era_b_opens_only_its_sessions_and_never_the_confirmation_floor(mixed_db, laddered):
    assert laddered["sessions_used"] == list(B_DAYS)
    assert laddered["until_ms"] < HE.iso_ms(RFP.CONFIRMATION_FLOOR_UTC)
    assert set(laddered["events"].session) == set(B_DAYS)
    with pytest.raises(ValueError):
        CL.ladder(mixed_db, era="AB")
    conn = sqlite3.connect(mixed_db)
    days = {pd.Timestamp(int(r[0]), unit="ms", tz="UTC").strftime("%Y-%m-%d")
            for r in conn.execute("SELECT DISTINCT snap_ms / 86400000 * 86400000 "
                                  "FROM rankings_snap")}
    conn.close()
    assert {"2026-08-14", "2026-08-26"} <= days          # 심어 뒀는데 안 나왔다


def test_no_verdict_words_and_the_labels_are_carried(report_b):
    rep, _ = report_b
    assert rep["labels"] == list(RFP.LABELS) and len(rep["labels"]) == 5
    text = json.dumps(rep).lower()
    for phrase in CL.FORBIDDEN_PHRASES:
        assert phrase not in text
    assert rep["arm"]["confirmation_floor_utc"] == RFP.CONFIRMATION_FLOOR_UTC
    assert rep["design"]["step"].startswith("(3)")


def test_console_is_pure_ascii(report_b, capsys):
    rep, _ = report_b
    CL.print_report(rep)
    out = capsys.readouterr().out
    out.encode("ascii")
    assert "CANDLE LADDER" in out
    assert "decides nothing" in out

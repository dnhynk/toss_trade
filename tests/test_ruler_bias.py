"""`ruler_bias` (`docs/70`) 가 지키는 것.

편향의 원천은 둘이다 - **t0 봉의 처리**와 **진입가**(`G2G3-PREREG` §3-1 개정 6 (A)·(B)). 그 둘을
절대 시각으로 고정하고, 돌연변이가 red 가 되는지를 `docs/70` §3 이 기록한다:

    M1  L0 = floor_min(tau)            t0 봉이 전방 경로에 들어온다 (고가 9.00 감시병이 들어온다)
    M2  lo 를 side='left'              라벨 == L0 인 t0 봉이 들어온다 (같은 감시병)
    M3  진입가 = t0 봉의 종가          t0 봉이 끝나야 아는 값
    M4  진입가 = t0 봉의 시가          t0 이전 가격
    M5  hi 를 side='left'              라벨 == L0 + 300s 인 다섯째 봉을 버린다 (고가 50.0 감시병이 빠진다)
    M6  FWD_MS 를 360s 로              여섯째 봉이 들어온다 (고가 100.0 감시병이 들어온다)

1. **t0 봉은 밖이다.** `tau` = 13:35:23 의 t0 봉은 라벨 13:36(내용 13:35~13:36). 거기 고가 9.00 을
   실어 두어 들어오면 어떤 최댓값도 눈에 띄게 틀린다. 직전 봉(13:35, 고가 1.50)도 밖이다.
2. **진입가 = 전방 창 첫 봉의 시가.** 13:37 봉의 시가 1.10 이지 13:36 봉의 시가 1.02 도 종가 1.30 도 아니다.
3. **분 경계에 정확히 놓인 `tau`.** 13:35:00.000 의 t0 봉도 13:36 이다 - 규칙이 하나다.
4. **창의 오른쪽 끝.** 라벨 `L0 + 300s` 인 봉은 안, `L0 + 360s` 인 봉은 밖.
5. **체결 없는 첫 분.** 창의 첫 분에 봉이 없으면 다음 존재 봉의 시가가 진입가이고 지연이 그만큼 길다.
6. **미래·과거를 안 쓴다.** `L0` 이전 봉을 전부 지우거나 터무니없는 값으로 바꿔도 같은 값이다.
7. **러너는 새로 뽑지 않는다.** 실제 순간은 심은 사건 그 자체(t0·앵커 지연이 손으로 센 값)이고, 위약
   순간은 `ranking_forward_path.run()` 의 추첨이다 - 그 테이프 값을 독립 재계산(`forward_probe`)과 대조한다.
8. **한 시대만 연다.** 08-14(버린 것)·08-26(확증 바닥)을 심어 두고 산출물에 없는 것을 본다.
9. **헤드라인 산술·분해·검열이 닫힌다.** 수치는 표에서 다시 계산한 것과 같고, CI 키가 없고, ASCII 다.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from tossmon.analysis import hires_events as HE
from tossmon.analysis.measure import candle_ruler as CR
from tossmon.analysis.measure import ranking_forward_path as RFP
from tossmon.analysis.measure import ruler_bias as RB
from tossmon.analysis.measure.cross_peak_check import forward_probe

MS = 1000
MIN_MS = 60_000
U = 1_000_000

# --------------------------------------------------------------------------- #
# 절대 시각 - self-check 가 ISO 표기와 일치함을 매 실행 확인한다
# --------------------------------------------------------------------------- #
REG_OPEN = 1_780_320_600_000                 # 2026-06-01T13:30:00Z
TAU_MID = REG_OPEN + 5 * MIN_MS + 23 * MS    # 13:35:23
TAU_EDGE = REG_OPEN + 5 * MIN_MS             # 13:35:00.000
TAU_NEXT = REG_OPEN + 6 * MIN_MS             # 13:36:00.000
TAU_NEXT_MID = REG_OPEN + 6 * MIN_MS + 30 * MS   # 13:36:30
L = {m: REG_OPEN + m * MIN_MS for m in range(0, 15)}   # 라벨 13:30 + m 분


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_hardcoded_constants_are_what_the_comments_say() -> None:
    assert _iso(REG_OPEN) == "2026-06-01T13:30:00Z"
    assert _iso(TAU_MID) == "2026-06-01T13:35:23Z"
    assert _iso(TAU_EDGE) == "2026-06-01T13:35:00Z"
    assert _iso(TAU_NEXT) == "2026-06-01T13:36:00Z"
    assert _iso(L[6]) == "2026-06-01T13:36:00Z" and _iso(L[11]) == "2026-06-01T13:41:00Z"
    assert RB.FWD_MS == 300_000 and RB.FWD_BARS == 5
    assert RB.HEADLINE == "max_ret_300s"


def _bars():
    """라벨 -> (시가, 고가). 13:38 은 체결 없는 분. 감시병 셋: 13:36 고가 9.00 (t0 봉),
    13:42 고가 50.0 (여섯째 분), 13:43 고가 100.0 (일곱째 분)."""
    rows = [
        (L[5], 1.00, 1.50),      # 13:35  내용 13:34~13:35   - 창 앞
        (L[6], 1.02, 9.00),      # 13:36  내용 13:35~13:36   - tau=13:35:23 의 t0 봉. 종가는 아래 CLOSE_1336
        (L[7], 1.10, 1.20),      # 13:37  진입 봉 (tau in [13:35, 13:36))
        # 13:38 없음
        (L[9], 1.05, 1.40),
        (L[10], 1.12, 1.15),
        (L[11], 1.13, 1.16),     # 13:41 = L0 + 300s (L0 = 13:36) - 안
        (L[12], 0.50, 50.0),     # 13:42 - tau in [13:35, 13:36) 에서는 밖, tau in [13:36, 13:37) 에서는 다섯째
        (L[13], 0.60, 100.0),    # 13:43 - 항상 밖
    ]
    ts = np.asarray([r[0] for r in rows], dtype="int64")
    return RB.ohlc_arrays(ts, np.asarray([r[1] for r in rows]) * U,
                          np.asarray([r[2] for r in rows]) * U)


# --------------------------------------------------------------------------- #
# 1~5. 경계
# --------------------------------------------------------------------------- #
def test_t0_bar_label_is_the_minute_after_the_floor() -> None:
    assert RB.t0_bar_label(np.asarray([TAU_MID, TAU_EDGE, TAU_NEXT])).tolist() == [L[6], L[6], L[7]]


def test_the_t0_bar_and_everything_before_it_are_outside_and_the_entry_is_the_next_open() -> None:
    """13:35:23 -> L0 = 13:36; F = 13:37..13:41 (13:38 없음 -> 4 봉). 진입 1.10, 고가 최댓값 1.40."""
    f = RB.bar_forward_at(_bars(), np.asarray([TAU_MID]))
    assert f["n_bars"].tolist() == [4]
    assert f["entry_u"][0] == pytest.approx(1.10 * U)
    assert f["max_ret"][0] == pytest.approx(1.40 / 1.10 - 1.0)
    assert f["entry_lag_s"][0] == pytest.approx(37.0)      # 13:36:00 - 13:35:23
    assert f["t_max_min"][0] == pytest.approx(3.0)         # 13:39 = L0 + 3 분
    assert f["l0_ms"][0] == L[6] and f["entry_label_ms"][0] == L[7]


def test_a_tau_exactly_on_the_minute_has_the_same_t0_bar() -> None:
    """13:35:00.000 의 t0 봉도 13:36 (내용 13:35~13:36) 이다 - 9.00 은 여전히 밖."""
    f = RB.bar_forward_at(_bars(), np.asarray([TAU_EDGE]))
    assert f["n_bars"].tolist() == [4]
    assert f["entry_u"][0] == pytest.approx(1.10 * U)
    assert f["max_ret"][0] == pytest.approx(1.40 / 1.10 - 1.0)
    assert f["entry_lag_s"][0] == pytest.approx(60.0)


def test_the_window_slides_by_whole_bars_and_the_fifth_label_is_inside_the_sixth_is_out() -> None:
    """13:36:00 -> L0 = 13:37; F = 13:38..13:42. 13:38 없음 -> 진입 = 13:39 시가 1.05 (지연 120 초),
    다섯째 봉 13:42 (= L0 + 300s) 의 50.0 은 안, 13:43 의 100.0 은 밖."""
    f = RB.bar_forward_at(_bars(), np.asarray([TAU_NEXT, TAU_NEXT_MID]))
    assert f["n_bars"].tolist() == [4, 4]
    assert f["entry_u"].tolist() == pytest.approx([1.05 * U, 1.05 * U])
    assert f["max_ret"].tolist() == pytest.approx([50.0 / 1.05 - 1.0] * 2)
    assert f["entry_lag_s"].tolist() == pytest.approx([120.0, 90.0])
    assert f["t_max_min"].tolist() == pytest.approx([5.0, 5.0])


def test_no_bar_in_the_window_means_undefined_not_zero() -> None:
    f = RB.bar_forward_at(_bars(), np.asarray([REG_OPEN + 60 * MIN_MS]))
    assert f["n_bars"].tolist() == [0] and np.isnan(f["max_ret"][0]) and np.isnan(f["entry_u"][0])
    empty = RB.ohlc_arrays(np.zeros(0, "int64"), np.zeros(0), np.zeros(0))
    g = RB.bar_forward_at(empty, np.asarray([TAU_MID]))
    assert g["n_bars"].tolist() == [0] and np.isnan(g["max_ret"][0])


# --------------------------------------------------------------------------- #
# 6. 과거를 안 쓴다
# --------------------------------------------------------------------------- #
def test_bars_at_or_before_the_t0_bar_never_enter() -> None:
    full = _bars()
    ref = RB.bar_forward_at(full, np.asarray([TAU_MID]))
    keep = full["ts"] > L[6]
    cut = RB.ohlc_arrays(full["ts"][keep], full["open"][keep], full["high"][keep])
    got = RB.bar_forward_at(cut, np.asarray([TAU_MID]))
    bad = RB.ohlc_arrays(full["ts"], np.where(keep, full["open"], 1e-3),
                         np.where(keep, full["high"], 1e12))
    worse = RB.bar_forward_at(bad, np.asarray([TAU_MID]))
    for f in (got, worse):
        assert f["n_bars"].tolist() == ref["n_bars"].tolist()
        assert f["entry_u"][0] == pytest.approx(ref["entry_u"][0])
        assert f["max_ret"][0] == pytest.approx(ref["max_ret"][0])


def test_tape_on_the_bar_clock_starts_at_the_entry_minute_and_ends_at_l0_plus_300s() -> None:
    """tau = 13:35:23, 진입 봉 라벨 13:37 -> 진입 = 13:36:00 의 첫 초. 창 [13:36:00, 13:41:00].
    13:35:50 의 5.0 (t0 봉 안) 은 밖, 13:41:00.000 의 1.8 은 안(끝 포함), 13:41:01 의 3.0 은 밖."""
    sec = np.arange(L[5], L[12], MS, dtype="int64")
    px = np.ones(sec.size)
    px[sec == L[5] + 50 * MS] = 5.0
    px[sec == L[8] + 10 * MS] = 1.5
    px[sec == L[11]] = 1.8
    px[sec == L[11] + MS] = 3.0
    got = RB.tape_on_bar_clock(sec, px * U, np.asarray([TAU_MID]), np.asarray([L[7]]))
    assert got[0] == pytest.approx(0.8)
    assert np.isnan(RB.tape_on_bar_clock(sec, px * U, np.asarray([TAU_MID]), np.asarray([-1]))[0])


# --------------------------------------------------------------------------- #
# 합성 DB - 손으로 셀 수 있는 세션 (테이프 · 봉 · 원장 · 랭킹)
# --------------------------------------------------------------------------- #
OPEN_MS = HE.REGULAR_OPEN_S * MS
POLL_MS = 12_000
N_SNAPS = 200
EVENT_SNAP = 101          # t0 = 개장 + 1,212 초
LEAVE_SNAP = 130
REENTRY_SNAP = 180        # t0 = 개장 + 2,160 초 - 같은 정규장의 두 번째 진입
PRE_OPEN_SNAPS = 10
RTYPE, DUR = "TOSS_SECURITIES_TRADING_VOLUME", "realtime"
T0_1, T0_2 = EVENT_SNAP * POLL_MS // MS, REENTRY_SNAP * POLL_MS // MS   # 1212, 2160 초
SPIKE_S = 1275            # 진입 봉 안의 테이프 급등 (t0 뒤 63 초)
SPIKE_U = 3_100_000
T0BAR_SENTINEL_U = 9_000_000


def _px(s: int) -> int:
    """초 `s` 의 테이프 가격 - 주기 7 톱니. rv60 은 세션 내내 거의 같아 위약 후보가 넉넉하다."""
    return SPIKE_U if s == SPIKE_S else 3_000_000 + (s % 7) * 1_000


def _day0(d: str) -> int:
    return int(pd.Timestamp(d + "T00:00:00Z").timestamp() * 1000)


def _rank_rows(day: str) -> list:
    base = _day0(day) + OPEN_MS
    rows = []
    for i in range(N_SNAPS):
        plan = [("AAA", 1, 1_500_000)]
        if EVENT_SNAP <= i < LEAVE_SNAP or i >= REENTRY_SNAP:
            plan.append(("BBB", 5, 3_000_000))
        for sym, rk, last_u in plan:
            rows.append((base + i * POLL_MS, RTYPE, DUR, rk, sym, last_u, 1, 1))
    for k in range(1, PRE_OPEN_SNAPS + 1):
        rows.append((base - k * POLL_MS, RTYPE, DUR, 1, "AAA", 1_500_000, 1, 1))
    return rows


def _trade_rows(day: str) -> list:
    base = _day0(day) + OPEN_MS
    return [("BBB", base + s * MS, _px(s), 10) for s in range(0, 3600)]


def _candle_rows(day: str) -> list:
    """분 `m` (내용 [60m, 60m+60), 라벨 60m+60): 시가 = 테이프 첫 초, 고가 3_006_000 (톱니의 꼭대기).
    t0 봉(라벨 1260) 에 고가 9,000,000 감시병 - 테이프에는 없다. 봉 자가 그 봉을 넣으면 눈에 띈다.
    진입 봉(라벨 1320) 의 고가는 테이프 급등 3,100,000 과 같다."""
    base = _day0(day) + OPEN_MS
    rows = []
    for m in range(60):
        label = base + (m + 1) * MIN_MS
        o, h, lo_, c = _px(60 * m), 3_006_000, 3_000_000, _px(60 * m + 59)
        if label == base + 1260 * MS:
            h = T0BAR_SENTINEL_U
        if label == base + 1320 * MS:
            h = SPIKE_U
        rows.append(("BBB", label, o, h, lo_, c, 60))
    return rows


def _ledger_rows(day: str) -> list:
    base = _day0(day) + OPEN_MS
    t0 = base + T0_1 * MS
    return [("BBB", t0 + 24 * MS, 1, 3, "ranking_tier3", 0.3),
            ("BBB", t0 + 324 * MS, 3, 2, "ranking_hold_expired", None),
            ("BBB", base + 4000 * MS, 2, 1, "stale", None)]


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
        conn.executemany("INSERT INTO promotions (symbol, ts_ms, from_tier, to_tier, "
                         "reason, score) VALUES (?,?,?,?,?,?)", _ledger_rows(d))
    conn.commit()
    conn.close()
    return path


B_DAYS = ("2026-08-18", "2026-08-19")
MIXED = ("2026-08-14",) + B_DAYS + ("2026-08-26",)


@pytest.fixture(scope="module")
def mixed_db(tmp_path_factory):
    return _make_db(tmp_path_factory.mktemp("rb") / "t.db", MIXED)


@pytest.fixture(scope="module")
def measured(mixed_db):
    return RB.measure(mixed_db, era="B")


@pytest.fixture(scope="module")
def report_b(mixed_db, tmp_path_factory):
    out = tmp_path_factory.mktemp("rb_out")
    assert RB.main(["prog", str(mixed_db), "--era", "B", "--out", str(out),
                    "--name", "b"]) == 0
    return json.loads((out / "b.json").read_text(encoding="utf-8")), out


# 손으로 센 값 ----------------------------------------------------------------- #
# 사건 1 (t0 = 1212): 봉 - L0 = 1260, F = 1320..1560, 진입 = px(1260) = 3,000,000, 고가 최댓값 = 3,100,000
#                     테이프 - 앵커 = 1213 (지연 1 초), 진입 = px(1213) = 3,002,000, 창 (1213, 1513] 최댓값 3,100,000
# 사건 2 (t0 = 2160): 봉 - L0 = 2220, F = 2280..2520, 진입 = px(2220) = 3,001,000, 고가 3,006,000
#                     테이프 - 앵커 = 2161, 진입 = px(2161) = 3,005,000, 창 최댓값 3,006,000
BAR_1 = SPIKE_U / 3_000_000 - 1.0
TAPE_1 = SPIKE_U / 3_002_000 - 1.0
BAR_2 = 3_006_000 / 3_001_000 - 1.0
TAPE_2 = 3_006_000 / 3_005_000 - 1.0


def test_hand_values_are_consistent_with_the_price_formula() -> None:
    assert (_px(1260), _px(1213), _px(2220), _px(2161)) == (3_000_000, 3_002_000, 3_001_000, 3_005_000)
    assert 1260 <= SPIKE_S < 1320                       # 급등은 진입 봉 안, t0 봉 밖


# --------------------------------------------------------------------------- #
# 7. 실제 순간 = 심은 사건 그 자체
# --------------------------------------------------------------------------- #
def _bbb(df: pd.DataFrame, day: str) -> pd.DataFrame:
    return df[(df.symbol == "BBB") & (df.session == day)].sort_values("tau_ms")


def test_real_moments_are_the_planted_events_with_hand_computed_values(measured):
    real = measured["real"]
    for day in B_DAYS:
        base = _day0(day) + OPEN_MS
        r = _bbb(real, day)
        assert r.tau_ms.tolist() == [base + T0_1 * MS, base + T0_2 * MS]
        assert r.anchor_lag_s.tolist() == pytest.approx([1.0, 1.0])
        assert r.anchor_ms.tolist() == [base + (T0_1 + 1) * MS, base + (T0_2 + 1) * MS]
        assert r.bar_max_ret.tolist() == pytest.approx([BAR_1, BAR_2])
        assert r.tape_max_ret.tolist() == pytest.approx([TAPE_1, TAPE_2])
        assert r.delta.tolist() == pytest.approx([BAR_1 - TAPE_1, BAR_2 - TAPE_2])
        assert r.bar_entry_u.tolist() == pytest.approx([3_000_000.0, 3_001_000.0])
        assert r.bar_entry_lag_s.tolist() == pytest.approx([48.0, 60.0])
        assert r.bar_n_bars.tolist() == [5, 5] and r.tape_n_bars.tolist() == [300.0, 300.0]
        assert r.tape_t_max_s.tolist()[0] == pytest.approx(SPIKE_S - (T0_1 + 1))
        assert r.bar_t_max_min.tolist()[0] == pytest.approx(1.0)
        assert r.tape_applies.all() and r.bar_applies.all() and r.paired.all()
        assert r.first_in_regular.tolist() == [True, False]
        assert r.stratum_first_in_regular.tolist() == [True, False]
        assert r.stratum_no_seat_then_lane.tolist() == [True, False]
        assert r.stratum_anchored.all()
        # 분해: 진입 분의 첫 초 = px(1260) = 봉 시가, 창 최댓값 = 급등 = 봉 고가 -> 가격 출처 몫 0
        assert r.source_component.tolist()[0] == pytest.approx(0.0, abs=1e-12)
        assert r.timing_component.tolist()[0] == pytest.approx(BAR_1 - TAPE_1)


def test_the_t0_bar_sentinel_is_in_the_db_but_not_in_the_number(mixed_db, measured):
    """감시병 9,000,000 이 t0 봉에 **있다** - 안 나온 것은 코드 때문이다."""
    conn = sqlite3.connect(mixed_db)
    base = _day0(B_DAYS[0]) + OPEN_MS
    h = conn.execute("SELECT high_u FROM candles_1m WHERE symbol='BBB' AND ts_ms=?",
                     (base + 1260 * MS,)).fetchone()[0]
    conn.close()
    assert h == T0BAR_SENTINEL_U
    r = _bbb(measured["real"], B_DAYS[0])
    assert r.bar_max_ret.iloc[0] < T0BAR_SENTINEL_U / 3_000_000 - 1.5


# --------------------------------------------------------------------------- #
# 7. 위약 순간 = 러너의 추첨 - 독립 재계산과 대조
# --------------------------------------------------------------------------- #
def test_placebo_moments_are_the_runners_draws_and_their_tape_value_recomputes(measured):
    plac = measured["placebo"]
    assert len(plac) > 0
    sec = np.arange(0, 3600, dtype="int64")
    px = np.asarray([_px(int(s)) for s in sec], dtype="float64")
    cand = RB.ohlc_arrays(np.asarray([(m + 1) * MIN_MS for m in range(60)], dtype="int64"),
                          np.asarray([r[2] for r in _candle_rows(B_DAYS[0])], dtype="float64"),
                          np.asarray([r[3] for r in _candle_rows(B_DAYS[0])], dtype="float64"))
    for day in B_DAYS:
        base = _day0(day) + OPEN_MS
        p = plac[plac.session == day]
        assert set(p.symbol) == {"BBB"}
        rel = (p.tau_ms.to_numpy("int64") - base)
        assert (rel % MS == 0).all() and (rel >= 0).all() and (rel < 3600 * MS).all()
        s_idx = rel // MS
        # 자기 간격 밖 (개정 3 의 360 초) - 추첨 규칙 그대로
        assert (np.abs(p.tau_ms.to_numpy("int64") - p.event_t0_ms.to_numpy("int64")) > 360 * MS).all()
        # 사건마다 3 추첨 이하, 슬롯이 심은 두 사건을 가리킨다
        assert set(p.event_t0_ms - base) <= {T0_1 * MS, T0_2 * MS}
        assert (p.groupby("slot").size() <= RFP.MATCH_DRAWS).all()
        ref = forward_probe(sec * MS, px, s_idx, 300)
        np.testing.assert_allclose(p.tape_max_ret.to_numpy("float64"), ref["max_ret"], rtol=1e-12)
        mine = RB.bar_forward_at(cand, s_idx * MS)
        np.testing.assert_allclose(p.bar_max_ret.to_numpy("float64"), mine["max_ret"], rtol=1e-12,
                                   equal_nan=True)
        both = p.tape_applies & p.bar_applies
        assert both.any()
        np.testing.assert_allclose((p.bar_max_ret - p.tape_max_ret)[both], p.delta[both])


# --------------------------------------------------------------------------- #
# 8. 한 시대만
# --------------------------------------------------------------------------- #
def test_era_b_opens_only_its_sessions_and_never_the_floor(mixed_db, measured):
    assert measured["sessions_used"] == list(B_DAYS)
    assert measured["until_ms"] < HE.iso_ms(RFP.CONFIRMATION_FLOOR_UTC)
    assert set(measured["real"].session) == set(B_DAYS) == set(measured["placebo"].session)
    with pytest.raises(ValueError):
        RB.measure(mixed_db, era="AB")
    conn = sqlite3.connect(mixed_db)
    days = {pd.Timestamp(int(r[0]), unit="ms", tz="UTC").strftime("%Y-%m-%d")
            for r in conn.execute("SELECT DISTINCT snap_ms / 86400000 * 86400000 "
                                  "FROM rankings_snap")}
    conn.close()
    assert {"2026-08-14", "2026-08-26"} <= days


# --------------------------------------------------------------------------- #
# 9. 산술이 닫힌다
# --------------------------------------------------------------------------- #
def test_headline_is_the_median_difference_recomputed_from_the_tables(measured):
    real, plac, h = measured["real"], measured["placebo"], measured["headline"]
    rh = real[real.paired & real.tape_applies & real.bar_applies]
    ph = plac[plac.tape_applies & plac.bar_applies]
    assert h["delta_real"]["n"] == len(rh) == 4 and h["delta_placebo"]["n"] == len(ph)
    assert h["bias_median"] == pytest.approx(float(np.median(rh.delta)) - float(np.median(ph.delta)))
    assert h["bias_mean"] == pytest.approx(float(rh.delta.mean()) - float(ph.delta.mean()))
    du = h["real_minus_placebo_under_each_ruler"]
    assert du["bar"]["mean"] == pytest.approx(float(rh.bar_max_ret.mean() - ph.bar_max_ret.mean()))
    assert h["identity_check_mean"] == pytest.approx(h["bias_mean"])
    pm = ph.groupby(["session", "slot"]).delta.mean()
    j = rh.set_index(["session", "slot"]).delta - pm
    assert h["pair_level"]["n"] == int(j.notna().sum())
    assert h["pair_level"]["median"] == pytest.approx(float(j.dropna().median()))


def test_census_adds_up_and_names_why_the_candle_side_is_missing(measured):
    c = measured["census"]
    r, p = c["real"], c["placebo"]
    assert r["n_anchored"] == 4 and r["both"] == 4 and r["paired_and_both"] == 4
    assert sum(r["bar_why"].values()) == r["n_anchored"]
    assert sum(p["bar_why"].values()) == p["n_draws"] and p["n_draws"] >= p["both"] >= 1
    assert p["bar_why"]["applies"] == p["bar_applies"]


def test_decomposition_closes_and_per_session_and_strata_are_consistent(measured):
    d = measured["decomposition"]
    assert d["closes"]
    per = measured["per_session"]
    assert [s["session"] for s in per] == list(B_DAYS)
    assert sum(s["n_real"] for s in per) == measured["headline"]["delta_real"]["n"]
    st = measured["by_stratum"]
    assert st["anchored"]["n_both"] == 4 and st["first_in_regular"]["n_both"] == 2
    assert st["first_in_regular"]["delta_real"]["median"] == pytest.approx(BAR_1 - TAPE_1)
    assert st["anchored"]["anchor_lag_s_p50"] == pytest.approx(1.0)
    # 사건 1 은 급등이 진입 봉에 있고, 사건 2 는 고가가 다섯 봉 모두 같아 **첫 봉**으로 귀속된다(동률 -> 첫 것)
    hist = measured["speed"]["real_all"]["bar_t_max_min_hist"]
    assert hist["1"] == 4 and sum(hist.values()) == 4


# --------------------------------------------------------------------------- #
# 9. CI 없음 · 사다리 없음 · ASCII · 판정 문구 없음
# --------------------------------------------------------------------------- #
def _walk_keys(obj, acc):
    if isinstance(obj, dict):
        for k, v in obj.items():
            acc.append(str(k))
            _walk_keys(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _walk_keys(v, acc)


def test_no_ci_and_no_ladder_anywhere(report_b):
    rep, out = report_b
    keys: list = []
    _walk_keys(rep, keys)
    assert [k for k in keys if any(f in k.lower() for f in RB.FORBIDDEN_KEYS)] == []
    for name in ("b_real.csv", "b_placebo.csv"):
        cols = pd.read_csv(out / name, nrows=1).columns
        assert not [c for c in cols if any(f in c.lower() for f in RB.FORBIDDEN_KEYS)]
    assert rep["design"]["step"].startswith("(1)")
    assert rep["design"]["round_trip_cost"] == pytest.approx(0.0238)
    assert rep["headline"]["bias_median"] is not None


def test_labels_and_no_verdict_words(report_b):
    rep, _ = report_b
    assert rep["labels"] == list(RFP.LABELS) and len(rep["labels"]) == 5
    text = json.dumps(rep).lower()
    for phrase in RB.FORBIDDEN_PHRASES:
        assert phrase not in text
    assert rep["arm"]["confirmation_floor_utc"] == RFP.CONFIRMATION_FLOOR_UTC
    assert rep["arm"]["sessions_used"] == list(B_DAYS)


def test_console_is_pure_ascii(report_b, capsys):
    rep, _ = report_b
    RB.print_report(rep)
    out = capsys.readouterr().out
    out.encode("ascii")
    assert "No CI, no ladder, no verdict" in out
    assert "DIFFERENTIAL BIAS" in out


def test_strata_come_from_candle_ruler_not_from_here(mixed_db, measured):
    """층 표식은 `candle_ruler.count()` 의 것과 **같다** - 정의가 두 군데 살지 않는다."""
    counted = CR.count(mixed_db, era="B")
    tags = counted["tags"]
    real = measured["real"]
    m = real.merge(tags[["session", "symbol", "t0_ms", "first_in_regular", "pre_match60"]]
                   .rename(columns={"t0_ms": "tau_ms", "first_in_regular": "fir_cr",
                                    "pre_match60": "pre_cr"}),
                   on=["session", "symbol", "tau_ms"], how="left")
    assert m.fir_cr.notna().all()
    assert (m.fir_cr.astype(bool) == m.first_in_regular.astype(bool)).all()
    assert (m.pre_cr.astype(bool) == m.pre_match60.astype(bool)).all()

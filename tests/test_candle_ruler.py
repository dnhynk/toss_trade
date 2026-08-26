"""`candle_ruler` (`docs/69`) 가 지키는 것 열.

1. **봉 라벨 경계.** `ts_ms = T` 는 `[T-60s, T)` 의 **끝** 라벨이다. 기준 시각 `tau` 의 사전
   상태는 라벨 `T <= tau` 인 봉만 쓴다 - `tau` 를 담은 봉(라벨 `floor_min(tau) + 60s`)이
   들어오면 룩어헤드다. **합성기를 쓰지 않고 절대 시각을 하드코딩한다**
   (`test_bar_label_convention.py` 와 같은 이유: 생성기와 소비자가 같은 규약을 쓰면
   규약이 틀려도 전부 초록이다). 한 칸 밀기 돌연변이 셋이 이 파일을 red 로 만드는지를
   `docs/69` §3 가 기록한다.
2. **분 경계에 정확히 놓인 `tau`.** 라벨 `= tau` 인 봉은 내용이 `[tau-60s, tau)` 라 들어간다.
3. **창의 왼쪽 끝.** 라벨이 정확히 `tau - 300s` 인 봉은 밖이다(내용이 창 앞이다).
4. **미래를 안 쓴다.** `tau` 뒤 봉을 전부 지우고 다시 재도 같은 값이다(접두사 불변).
5. **체결 없는 분은 0 이다.** 봉이 3 개면 `cnbar5 = 3` 이고 합은 있는 봉만 더한다.
6. **한 시대만 연다.** 탐색 B 는 자기 세션만, 확증 바닥(08-26)에 심은 날과 버린 날(08-14)은
   DB 에 **있는데** 산출물에 없다 - 데이터가 아니라 코드가 막았다.
7. **앵커 수가 깔때기와 같다.** 같은 DB 에서 `e2_design_funnel` 과 어긋나면 자가 둘이다.
8. **첫 진입은 랭킹 이력으로만 정한다.** 그 정규장에서 두 번째로 든 사건은 첫 진입이 아니다.
9. **위약 풀은 자기 간격 밖·키 있음·밴드 안만 센다.** 값을 손으로 셀 수 있는 합성 세션에서
   `any / any360 / key / rv / rv+vol` 이 각각 정확히 맞아야 한다. 밴드가 실제로 물린다.
10. **수익도 CI 도 어디에도 없고, 콘솔은 ASCII 이며 판정 문구가 없다.** (1) 의 지위 자체다.

그리고 `ranking_forward_path.arm_window` 가 `run()` 이 쓰는 것과 **같은 세션**을 준다 -
팔 규칙이 한 자리에만 살아야 두 러너가 같은 창을 본다.
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
from tossmon.analysis.measure import e2_design_funnel as EF
from tossmon.analysis.measure import ranking_forward_path as RFP

MS = 1000
MIN_MS = 60_000
OPEN_MS = HE.REGULAR_OPEN_S * MS
POLL_MS = 12_000
N_SNAPS = 200
EVENT_SNAP = 101          # t0 = 개장 + 1,212 초 - 분 **가운데**
LEAVE_SNAP = 130
REENTRY_SNAP = 180        # t0 = 개장 + 2,160 초 - 분 **경계**. 같은 정규장의 두 번째 진입
CCC_SNAP = 120            # t0 = 개장 + 1,440 초. 봉이 아예 없는 종목
DDD_SNAP = 140            # 프리마켓에 상위 10 에 있다가 정규장 t0 = 개장 + 1,680 초에 다시 든다
PRE_OPEN_SNAPS = 10       # 개장 **앞** 스냅 수 (같은 UTC 일자)
RTYPE, DUR = "TOSS_SECURITIES_TRADING_VOLUME", "realtime"

# --------------------------------------------------------------------------- #
# 절대 시각 - 아래 self-check 가 ISO 표기와 일치함을 매 실행 확인한다
# --------------------------------------------------------------------------- #
REG_OPEN = 1_780_320_600_000          # 2026-06-01T13:30:00Z
TAU_MID = REG_OPEN + 5 * MIN_MS + 23 * MS   # 2026-06-01T13:35:23Z
TAU_EDGE = REG_OPEN + 5 * MIN_MS            # 2026-06-01T13:35:00Z
TAU_NEXT = REG_OPEN + 6 * MIN_MS            # 2026-06-01T13:36:00Z
L_1330 = REG_OPEN                           # 라벨 13:30:00 - 내용 13:29~13:30
L_1331 = REG_OPEN + 1 * MIN_MS
L_1333 = REG_OPEN + 3 * MIN_MS
L_1335 = REG_OPEN + 5 * MIN_MS              # 내용 13:34~13:35, 13:35:00 에 완결
L_1336 = REG_OPEN + 6 * MIN_MS              # 내용 13:35~13:36 - TAU_MID 를 **담고 있다**
L_1337 = REG_OPEN + 7 * MIN_MS


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_hardcoded_constants_are_what_the_comments_say() -> None:
    assert _iso(REG_OPEN) == "2026-06-01T13:30:00Z"
    assert _iso(TAU_MID) == "2026-06-01T13:35:23Z"
    assert _iso(TAU_EDGE) == "2026-06-01T13:35:00Z"
    assert _iso(TAU_NEXT) == "2026-06-01T13:36:00Z"
    assert _iso(L_1336) == "2026-06-01T13:36:00Z"
    assert CR.CANDLE_LOOKBACK_MS == 300_000


def _bars():
    """라벨 -> (high, low, vol). 13:32·13:34 는 **체결 없는 분**이라 봉이 없다.

    13:36 봉은 룩어헤드 감시병이다 - 거래량 1,000,000 과 큰 범위를 실어 두어, 그 봉이
    사전 상태에 들어오면 어떤 합도 눈에 띄게 틀린다.
    """
    rows = [
        (L_1330, 1.10, 1.00, 7.0),
        (L_1331, 1.10, 1.00, 1.0),
        (L_1333, 1.20, 1.00, 10.0),
        (L_1335, 1.05, 1.00, 100.0),
        (L_1336, 3.00, 1.00, 1_000_000.0),
        (L_1337, 1.10, 1.00, 10_000_000.0),
    ]
    ts = np.asarray([r[0] for r in rows], dtype="int64")
    hi = np.asarray([r[1] for r in rows]) * 1_000_000
    lo = np.asarray([r[2] for r in rows]) * 1_000_000
    vol = np.asarray([r[3] for r in rows])
    return CR.candle_arrays(ts, hi, lo, vol)


def _r2(h, l):
    return np.log(h / l) ** 2


# --------------------------------------------------------------------------- #
# 1~3. 경계
# --------------------------------------------------------------------------- #
def test_pre_state_excludes_the_bar_that_contains_tau() -> None:
    """13:35:23 의 창은 라벨 13:31~13:35 다. 13:36 봉(내용 13:35~13:36)은 `tau` 를 담고
    있으므로 밖이고, 13:30 봉은 창 앞이라 밖이다."""
    k = CR.candle_keys_at(_bars(), np.asarray([TAU_MID]))
    assert k[CR.KEY_NBAR].tolist() == [3]
    assert k[CR.KEY_VOL].tolist() == [111.0]
    exp = np.sqrt(_r2(1.10, 1.00) + _r2(1.20, 1.00) + _r2(1.05, 1.00))
    assert k[CR.KEY_RV][0] == pytest.approx(exp)


def test_a_bar_labelled_exactly_tau_is_inside_and_one_labelled_tau_minus_300s_is_outside():
    """`tau` = 13:35:00.000. 라벨 13:35 의 내용은 13:34~13:35 라 이미 완결 - 들어간다.
    라벨 13:30 (= tau - 300s) 의 내용은 13:29~13:30 라 창 앞 - 나간다."""
    k = CR.candle_keys_at(_bars(), np.asarray([TAU_EDGE]))
    assert k[CR.KEY_NBAR].tolist() == [3]
    assert k[CR.KEY_VOL].tolist() == [111.0]


def test_the_window_slides_by_whole_bars() -> None:
    """`tau` = 13:36:00.000 이면 13:36 봉이 **완결**됐으니 들어오고 13:31 봉이 나간다."""
    k = CR.candle_keys_at(_bars(), np.asarray([TAU_NEXT]))
    assert k[CR.KEY_NBAR].tolist() == [3]
    assert k[CR.KEY_VOL].tolist() == [10.0 + 100.0 + 1_000_000.0]


def test_last_completed_label_is_the_floor_minute() -> None:
    assert CR.last_completed_label(np.asarray([TAU_MID, TAU_EDGE])).tolist() == [L_1335, L_1335]
    assert CR.has_bar_at(_bars(), np.asarray([L_1335, L_1335 - 5])).tolist() == [True, False]


# --------------------------------------------------------------------------- #
# 4~5. 접두사 불변 · 0 리샘플
# --------------------------------------------------------------------------- #
def test_keys_do_not_change_when_every_bar_after_tau_is_deleted() -> None:
    full = _bars()
    keep = full["ts"] <= TAU_MID
    cut = CR.candle_arrays(full["ts"][keep],
                           np.exp(np.sqrt(np.diff(full["cum_r2"])))[keep] * 1_000_000,
                           np.ones(int(keep.sum())) * 1_000_000,
                           np.diff(full["cum_vol"])[keep])
    a = CR.candle_keys_at(full, np.asarray([TAU_MID]))
    b = CR.candle_keys_at(cut, np.asarray([TAU_MID]))
    for key in CR.CANDLE_KEYS:
        assert a[key][0] == pytest.approx(b[key][0])


def test_missing_minutes_count_as_zero_not_as_missing() -> None:
    """13:32·13:34 에 봉이 없어도 키는 정의되고(`cnbar5 = 3`), 합은 있는 봉만 더한다."""
    k = CR.candle_keys_at(_bars(), np.asarray([TAU_MID]))
    assert CR.key_defined(k).tolist() == [True]
    empty = CR.candle_arrays(np.zeros(0, "int64"), np.zeros(0), np.zeros(0), np.zeros(0))
    k0 = CR.candle_keys_at(empty, np.asarray([TAU_MID]))
    assert k0[CR.KEY_NBAR].tolist() == [0] and k0[CR.KEY_VOL].tolist() == [0.0]
    assert CR.key_defined(k0).tolist() == [False]


def test_factor_band_is_log_symmetric_and_none_means_no_band() -> None:
    x = np.asarray([49.0, 50.0, 100.0, 200.0, 201.0])
    assert CR.in_factor_band(x, 100.0, 2.0).tolist() == [False, True, True, True, False]
    assert CR.in_factor_band(x, 100.0, None).all()
    assert CR.sweep_label(0.2, None) == "rv0.2_vfnone" and CR.sweep_label(0.4, 2) == "rv0.4_vf2.0"


def test_a_one_price_bar_adds_zero_range_and_the_band_is_multiplicative() -> None:
    c = CR.candle_arrays(np.asarray([L_1335]), np.asarray([2.0e6]), np.asarray([2.0e6]),
                         np.asarray([5.0]))
    k = CR.candle_keys_at(c, np.asarray([TAU_MID]))
    assert k[CR.KEY_RV][0] == 0.0 and k[CR.KEY_VOL][0] == 5.0
    assert CR.key_defined(k).tolist() == [False]
    x = np.asarray([79.0, 80.0, 100.0, 120.0, 121.0])
    assert CR.in_band(x, 100.0, 0.2).tolist() == [False, True, True, True, False]


# --------------------------------------------------------------------------- #
# 합성 DB - 손으로 셀 수 있는 세션 (봉·테이프·원장·랭킹)
# --------------------------------------------------------------------------- #
def _day0(d: str) -> int:
    return int(pd.Timestamp(d + "T00:00:00Z").timestamp() * 1000)


def _rank_rows(day: str) -> list:
    base = _day0(day) + OPEN_MS
    rows = []
    for i in range(N_SNAPS):
        plan = [("AAA", 1, 1_500_000)]
        if EVENT_SNAP <= i < LEAVE_SNAP or i >= REENTRY_SNAP:
            plan.append(("BBB", 5, 3_000_000))
        if i >= CCC_SNAP:
            plan.append(("CCC", 7, 2_000_000))
        if i >= DDD_SNAP:
            plan.append(("DDD", 3, 4_000_000))
        for sym, rk, last_u in plan:
            rows.append((base + i * POLL_MS, RTYPE, DUR, rk, sym, last_u, 1, 1))
    # 프리마켓: DDD 는 개장 앞 스냅들에서 이미 3 위였다 - 정규장 첫 진입이지만 UTC 일자 첫 진입은 아니다
    for k in range(1, PRE_OPEN_SNAPS + 1):
        rows.append((base - k * POLL_MS, RTYPE, DUR, 1, "AAA", 1_500_000, 1, 1))
        rows.append((base - k * POLL_MS, RTYPE, DUR, 3, "DDD", 4_000_000, 1, 1))
    return rows


#: BBB 의 테이프 = 좌석 시간만. 첫 좌석 [1220, 1500) 는 사건 t0=1212 **뒤**에 시작해
#: (t0 앞 60 초 테이프 0 = 옛 자로 못 재는 모양) 간격 안이고, [1600, 1800) 는 옛 간격(360 초)
#: 밖·새 간격(600 초) 안, [2400, 3600) 는 둘 다 밖이다.
BBB_TAPE = ((1220, 1500), (1600, 1800), (2400, 3600))


def _trade_rows(day: str) -> list:
    base = _day0(day) + OPEN_MS
    rows = []
    for lo, hi in BBB_TAPE:
        for s in range(lo, hi):
            rows.append(("BBB", base + s * MS, 3_000_000 + (s % 7) * 1_000, 10))
    for s in range(1441, 1740):
        rows.append(("CCC", base + s * MS, 2_000_000, 10))
    for s in range(0, 2400, 900):
        rows.append(("AAA", base + s * MS, 1_500_000, 5))
    return rows


def _candle_rows(day: str, *, drop_last_before_event: bool) -> list:
    """BBB 의 봉은 개장 + 1 분부터 60 분 동안 **매 분 같은 봉** - 키가 세션 내내 같다."""
    base = _day0(day) + OPEN_MS
    rows = []
    for m in range(1, 61):
        label = base + m * MIN_MS
        if drop_last_before_event and label == base + 1200 * MS:
            continue                                   # 체결 없는 분
        rows.append(("BBB", label, 3_000_000, 3_100_000, 3_000_000, 3_050_000, 100))
    return rows


def _ledger_rows(day: str, *, lane_at_t0: bool) -> list:
    base = _day0(day) + OPEN_MS
    t0 = base + EVENT_SNAP * POLL_MS
    seat = t0 if lane_at_t0 else t0 + 24 * MS
    ccc = base + CCC_SNAP * POLL_MS
    return [("BBB", seat, 1, 3, "ranking_tier3", 0.3),
            ("BBB", seat + 300 * MS, 3, 2, "ranking_hold_expired", None),
            ("BBB", base + 2400 * MS, 2, 3, "capacity_fill", 0.7),
            ("BBB", base + 3600 * MS, 3, 2, "score_decay", None),
            ("BBB", base + 4000 * MS, 2, 1, "stale", None),       # 날이 tier1 로 끝난다
            ("CCC", ccc + 12 * MS, 1, 3, "ranking_tier3", 0.3)]


def _make_db(path, days, *, lane_at_t0_days=(), drop_days=()):
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
        conn.executemany("INSERT INTO candles_1m VALUES (?,?,?,?,?,?,?)",
                         _candle_rows(d, drop_last_before_event=d in drop_days))
        conn.executemany("INSERT INTO promotions (symbol, ts_ms, from_tier, to_tier, "
                         "reason, score) VALUES (?,?,?,?,?,?)",
                         _ledger_rows(d, lane_at_t0=d in lane_at_t0_days))
    conn.commit()
    conn.close()
    return path


B_DAYS = ("2026-08-18", "2026-08-19")
MIXED = ("2026-08-14",) + B_DAYS + ("2026-08-26",)


@pytest.fixture(scope="module")
def mixed_db(tmp_path_factory):
    return _make_db(tmp_path_factory.mktemp("cr") / "t.db", MIXED,
                    lane_at_t0_days=("2026-08-19",), drop_days=("2026-08-19",))


@pytest.fixture(scope="module")
def counted(mixed_db):
    return CR.count(mixed_db, era="B")


@pytest.fixture(scope="module")
def report_b(mixed_db, tmp_path_factory):
    out = tmp_path_factory.mktemp("cr_out")
    assert CR.main(["prog", str(mixed_db), "--era", "B", "--out", str(out),
                    "--name", "b"]) == 0
    return json.loads((out / "b.json").read_text(encoding="utf-8")), out


def _bbb_first(tags: pd.DataFrame, day: str) -> pd.Series:
    t0 = _day0(day) + OPEN_MS + EVENT_SNAP * POLL_MS
    sub = tags[(tags.symbol == "BBB") & (tags.session == day) & (tags.t0_ms == t0)]
    assert len(sub) == 1
    return sub.iloc[0]


# --------------------------------------------------------------------------- #
# 6. 한 시대만
# --------------------------------------------------------------------------- #
def test_era_b_opens_only_its_sessions_and_never_the_floor(mixed_db, counted):
    assert set(counted["sessions_used"]) == set(B_DAYS)
    assert counted["until_ms"] < HE.iso_ms(RFP.CONFIRMATION_FLOOR_UTC)
    with pytest.raises(ValueError):
        CR.count(mixed_db, era="AB")
    # 대조군: 심은 08-14(버린 것)·08-26(확증)은 DB 에 **있다** - 안 나온 것은 코드 때문이다.
    conn = sqlite3.connect(mixed_db)
    days = {pd.Timestamp(int(r[0]), unit="ms", tz="UTC").strftime("%Y-%m-%d")
            for r in conn.execute("SELECT DISTINCT snap_ms / 86400000 * 86400000 "
                                  "FROM rankings_snap")}
    conn.close()
    assert {"2026-08-14", "2026-08-26"} <= days


def test_arm_window_is_exactly_what_the_runner_opens(mixed_db):
    conn = HE.open_ro(mixed_db)
    try:
        b = RFP.arm_window(conn, exploration_only=True, exploration_era="B")
        c = RFP.arm_window(conn, exploration_only=False, confirmation=True)
        with pytest.raises(ValueError):
            RFP.arm_window(conn, exploration_only=True, confirmation=True)
    finally:
        conn.close()
    run_b = RFP.run(mixed_db, exploration_era="B", primary_cells=(),
                    funnel_only_elsewhere=True)
    assert [s["session"] for s in b["sessions"]] == [s["session"] for s in run_b["sessions"]]
    assert (b["floor_ms"], b["until_ms"]) == (run_b["since_ms"], run_b["until_ms"])
    assert [s["session"] for s in c["sessions"]] == ["2026-08-26"]


# --------------------------------------------------------------------------- #
# 7. 앵커 수가 깔때기와 같다
# --------------------------------------------------------------------------- #
def test_anchored_counts_agree_with_the_funnel(mixed_db, counted):
    ef = EF.run(mixed_db, era="B")
    rt, _d, kind, cell = CR.DECLARED
    theirs = next(r for r in ef["funnel"]
                  if (r["ranking_type"], r["kind"], r["cell"]) == (rt, kind, cell))
    assert counted["n_events"] == theirs["n_events"] > 0
    assert counted["n_anchored"] == theirs["n_anchored"] > 0
    mine_142 = counted["strata"]["no_seat_then_lane"]["n"]
    assert mine_142 == theirs["no_seat_then_lane_within_300s_anchored"] > 0


# --------------------------------------------------------------------------- #
# 8. 첫 진입은 랭킹 이력으로만
# --------------------------------------------------------------------------- #
def test_first_in_regular_flags_only_the_first_top10_entry_of_the_session(counted):
    tags = counted["tags"]
    bbb = tags[(tags.symbol == "BBB") & (tags.session == "2026-08-18")].sort_values("t0_ms")
    assert bbb.first_in_regular.tolist() == [True, False]
    assert bbb.first_in_utc_day.tolist() == [True, False]
    assert bbb.anchored.tolist() == [True, True]
    # 옛 자로는 첫 진입의 사전 상태가 없다 (t0 앞 60 초에 테이프 0) - docs/68 의 142 와 같은 모양
    assert not bool(bbb.pre_match60.iloc[0])
    # DDD 는 프리마켓에 상위 10 이었다: 정규장 기준으로는 첫 진입, UTC 일자 기준으로는 아니다.
    # 창의 왼쪽 끝이 개장이 아니라 일자 시작이면 이 둘이 같아진다 (돌연변이 M7).
    ddd = tags[(tags.symbol == "DDD") & (tags.session == "2026-08-18")]
    assert len(ddd) == 1
    assert bool(ddd.first_in_regular.iloc[0]) and not bool(ddd.first_in_utc_day.iloc[0])


def test_strata_follow_the_ledger_not_the_ranking(counted):
    tags = counted["tags"]
    a = _bbb_first(tags, "2026-08-18")          # 좌석이 t0 + 24 초에 왔다
    b = _bbb_first(tags, "2026-08-19")          # 좌석이 t0 그 스냅에 왔다
    # 원장은 날을 넘어 이어진다(전날 마지막 행이 tier1) - t0 앞 5 분 내내 봉 폴링이 없었다
    assert (a.seat_at_t0, a.seat_after) == ("none", "lane")
    assert a.tier_at_t0 <= 1 and a.tier_floor_prior5 <= 1
    assert (b.seat_at_t0, b.seat_age_s) == ("lane", 0.0)
    masks = CR.strata_masks(tags)
    assert masks["no_seat_then_lane"][a.name] and not masks["no_seat_then_lane"][b.name]
    assert masks["lane_same_snap"][b.name] and not masks["lane_same_snap"][a.name]
    assert masks["first_in_regular"][a.name] and masks["first_in_regular"][b.name]
    assert masks["no_seat_then_lane_and_first"][a.name]
    # 08-19 는 t0 에 이미 차선 좌석(나이 0) - 창 안 최저 티어는 t0 직전 상태(1)다
    assert b.tier_at_t0 == 3 and b.tier_floor_prior5 <= 1


# --------------------------------------------------------------------------- #
# 9. 위약 풀 - 손으로 센 값
# --------------------------------------------------------------------------- #
def test_pool_counts_tape_outside_the_gap_with_a_key_inside_the_band(counted):
    a = _bbb_first(counted["tags"], "2026-08-18")
    # 봉: 매 분 같은 봉이 개장 + 1 분부터 있다 -> t0 = 1,212 초의 창(라벨 960..1200)은 꽉 찼다
    assert (a.has_bar_last, a[CR.KEY_NBAR], a.key_ok, a.coverage) == (True, 5, True, "present")
    assert a[CR.KEY_VOL] == 500.0
    # 간격 600 초: |ts - 1212| > 600 <=> ts > 1812 -> [2400, 3600) = 1,200 초
    # 간격 360 초: ts > 1572 -> [1600, 1800) 200 초가 더 든다
    assert a.pool_any == 1200
    assert a.pool_any_old_gap == 1400
    # 그 1,200 초의 창은 전부 꽉 차 있고 봉이 같으니 키가 같다 -> 전부 밴드 안
    assert a.pool_key == a.pool_rv == a.pool_rv_vol == 1200
    for tol in CR.RV_TOL_SWEEP:
        for fac in CR.VOL_FACTOR_SWEEP:
            assert a["pool_" + CR.sweep_label(tol, fac)] == 1200


def test_the_volume_band_actually_bites(counted):
    """08-19 는 t0 직전 분의 봉을 뺐다: 사건의 `cvol5` 는 400, 후보는 500 -> ±20% 밖.
    `crv5` 는 sqrt(4/5) 배라 밴드 안 - 변동성 밴드만 켜면 짝이 되고 거래량 밴드가 죽인다."""
    b = _bbb_first(counted["tags"], "2026-08-19")
    assert (b.has_bar_last, b[CR.KEY_NBAR], b.key_ok) == (False, 4, True)
    assert b.coverage == "covered_no_trade_last_min"
    assert b[CR.KEY_VOL] == 400.0
    assert b.pool_rv == 1200 and b.pool_rv_vol == 0
    # 폭 넓히기: 500/400 = 1.25 라 f=1.2 는 밖, f=1.5 부터 안. 변동성만이면 전부 안.
    assert b["pool_" + CR.sweep_label(0.2, 1.2)] == 0
    assert b["pool_" + CR.sweep_label(0.2, 1.5)] == 1200
    assert b["pool_" + CR.sweep_label(0.2, None)] == 1200
    s = counted["strata"]["anchored"]["pool"]
    assert s["unpaired_empty_band"] >= 1


def test_a_symbol_without_candles_is_key_missing_and_the_reason_is_named(counted):
    tags = counted["tags"]
    ccc = tags[tags.symbol == "CCC"]
    assert len(ccc) == 2 and ccc.anchored.all()
    assert set(ccc.coverage) == {"no_candle_rows"}
    assert not ccc.key_ok.any() and (ccc.pool_rv_vol == 0).all()
    cov = counted["strata"]["anchored"]["coverage"]
    assert cov["no_candle_rows"] == 2 and sum(cov.values()) == counted["n_anchored"]


def test_stratum_summary_adds_up(counted):
    s = counted["strata"]["anchored"]
    p = s["pool"]
    assert (p["paired_rv_vol"] + p["unpaired_key_missing"]
            + p["unpaired_no_tape_outside_gap"] + p["unpaired_empty_band"]) == s["n"]
    assert sum(s["cnbar5_hist"].values()) == s["n"]
    assert sum(b["n"] for b in s["by_tier_at_t0"].values()) == s["n"]
    assert sum(r["n"] for r in s["per_session"]) == s["n"]
    assert s["pairs_per_session"] == pytest.approx(p["paired_rv_vol"] / len(B_DAYS))


# --------------------------------------------------------------------------- #
# 10. 수익·CI 없음 · ASCII · 판정 문구 없음
# --------------------------------------------------------------------------- #
def _walk_keys(obj, acc):
    if isinstance(obj, dict):
        for k, v in obj.items():
            acc.append(str(k))
            _walk_keys(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _walk_keys(v, acc)


def test_no_forward_return_and_no_ci_anywhere(report_b):
    rep, out = report_b
    keys: list = []
    _walk_keys(rep, keys)
    bad = [k for k in keys if any(f in k.lower() for f in CR.FORBIDDEN_KEYS)]
    assert bad == []
    cols = pd.read_csv(out / "b_events.csv", nrows=1).columns
    assert not [c for c in cols if any(f in c.lower() for f in CR.FORBIDDEN_KEYS)]
    assert rep["design"]["step"].startswith("(1)")


def test_labels_and_no_verdict_words(report_b):
    rep, _ = report_b
    assert rep["labels"] == list(RFP.LABELS) and len(rep["labels"]) == 5
    text = json.dumps(rep).lower()
    for phrase in CR.FORBIDDEN_PHRASES:
        assert phrase not in text
    assert rep["arm"]["confirmation_floor_utc"] == RFP.CONFIRMATION_FLOOR_UTC


def test_console_is_pure_ascii(report_b, capsys):
    rep, _ = report_b
    CR.print_report(rep)
    out = capsys.readouterr().out
    out.encode("ascii")
    assert "No forward return, no CI" in out

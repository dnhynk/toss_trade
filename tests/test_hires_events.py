"""고해상도 랭킹 사건의 **자** (`docs/59`). 이 파일이 지키는 것 여덟.

1. **사건 정의에 가격이 한 번도 들어가지 않는다.** 검증 방식은 진술이 아니라 관측이다 —
   `last_u` 를 통째로 뒤흔든 DB 와 원본 DB 에서 **사건 집합이 같아야** 한다. 설계 A 가
   *"슈팅은 이미 임계만큼 올랐음으로 정의되므로 탐지가 상승을 소진한다"* 로 죽은 자리라
   (`STRATEGY-VERDICTS` §4.4) 이 성질은 문서가 아니라 테스트가 지켜야 한다.
2. **E3 는 E1 의 부분집합이다.** 정의상 그렇고, 아니면 둘 중 하나가 틀렸다.
3. **커버리지 창의 경계**가 말한 대로다 — 사후 `(t0, t0+W]`, 사전 `[t0-W, t0)`.
   `t0` 에 정확히 찍힌 체결이 양쪽에 다 세이거나 양쪽에서 다 빠지면 창이 거짓말한다.
4. **"체결 0" 의 이유가 갈린다.** 종목에 테이프가 아예 없는 것과, 있는데 그 창에
   없는 것은 다른 사실이다. 뭉치면 다음 태스크가 우리 티어 구조를 유동성 부재로 읽는다.
5. **홀드아웃 가드가 실제로 발화한다.** 데이터가 우연히 안 걸리는 것과 코드가 막는 것은
   다르다 — 봉인 구간에 행을 **심어서** 막히는지 본다. 막지 않는 경로도 함께 보여
   *"막은 것이 가드"* 임을 증명한다.
6. **중복 (스냅, 종목) 행을 조용히 브로드캐스트하지 않는다.** §4.4-D 의 pandas 중복
   인덱스 사고와 같은 자리다 — 버린 수를 세어서 내보내야 한다.
7. **부호화 키의 종목 경계**가 실제로 안 새는지. `interval_overlap` 의 누적 최대는
   "앞 종목 값이 더 작다"는 성질에 기대는데, 그 성질이 깨지면 조용히 오탐이 된다.
8. **콘솔이 ASCII 다.** cp949 콘솔에서 비 ASCII 는 `UnicodeEncodeError` 로 죽는다
   (README 금지 항목 — 실제로 supervisor 가 그것 때문에 죽은 적이 있다).
"""
from __future__ import annotations

import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

from tossmon.analysis import hires_events as HE
from tossmon.analysis import session as SS

MS = 1000
NAN = float("nan")

#: 합성 DB 의 세션들. 하나는 **봉인 구간 안**이다 (가드가 발화하는지 보려고 심는다).
HOLDOUT_DAY = "2026-06-15"
DAY_A = "2026-08-03"
DAY_B = "2026-08-04"

#: 정규장 안에서 시작한다 — `session_universe` 가 덮임률을 잴 수 있어야 한다.
OPEN_MS = HE.REGULAR_OPEN_S * MS
POLL_MS = 12_000


def _day0(d: str) -> int:
    return int(pd.Timestamp(d + "T00:00:00Z").timestamp() * 1000)


def _rank_rows(day: str, n_snaps: int, plan) -> list[tuple]:
    """`plan(i)` -> [(symbol, rank, last_u)] 인 스냅 i 의 랭킹 행들."""
    base = _day0(day) + OPEN_MS
    rows = []
    for i in range(n_snaps):
        for sym, rk, last_u in plan(i):
            rows.append((base + i * POLL_MS, "MARKET_TRADING_VOLUME", "realtime",
                         rk, sym, last_u, 1, 1))
    return rows


def _plan(i: int):
    """스냅마다의 상위 목록. 사건이 골고루 나오도록 손으로 짠 대본이다.

    - `AAA` 는 계속 1위 (사건 없음)
    - `BBB` 는 i>=2 에 50위로 들어와 계속 머문다 (E1 N100/N50, E3 는 체류가 길어 성립)
    - `CCC` 는 i==4 에만 들어왔다 나간다 (E1 은 되고 E3 M3 는 안 된다)
    - `DDD` 는 i>=3 에 90위, i>=6 에 20위로 뛴다 (E2 점프 70)
    """
    out = [("AAA", 1, 1_500_000)]
    if i >= 2:
        out.append(("BBB", 50, 3_000_000))
    if i == 4:
        out.append(("CCC", 40, 7_000_000))
    if i >= 3:
        out.append(("DDD", 90 if i < 6 else 20, 25_000_000))
    return out


def _make_db(path, *, scramble_price: bool = False, with_holdout: bool = True):
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
    rows = _rank_rows(DAY_A, 10, _plan) + _rank_rows(DAY_B, 10, _plan)
    if with_holdout:
        # ★ 봉인 구간에 **심는다**. 가드가 없으면 이 행들이 사건이 된다.
        rows += _rank_rows(HOLDOUT_DAY, 10, _plan)
    if scramble_price:
        rows = [r[:5] + (int(1 + (7919 * i) % 90_000_000),) + r[6:]
                for i, r in enumerate(rows)]
    conn.executemany("INSERT INTO rankings_snap "
                     "(snap_ms, ranking_type, duration, rank, symbol, last_u, vol_qu, "
                     "amount_u) VALUES (?,?,?,?,?,?,?,?)", rows)
    # BBB 만 테이프가 있다. 나머지는 "그 세션에 체결 수집이 아예 없는" 종목이다.
    base = _day0(DAY_A) + OPEN_MS
    trades = [("BBB", base + 2 * POLL_MS, 3_000_000, 10),          # t0 정각 (양쪽 제외)
              ("BBB", base + 2 * POLL_MS + 30 * MS, 3_010_000, 10),   # 60초 창 안
              ("BBB", base + 2 * POLL_MS + 200 * MS, 3_020_000, 10),  # 300초 창에만
              ("BBB", base + 2 * POLL_MS - 30 * MS, 2_990_000, 10)]   # 사전 창
    conn.executemany("INSERT INTO trades_snap VALUES (?,?,?,?)", trades)
    conn.execute("INSERT INTO symbols VALUES ('BBB','B','NASDAQ','STOCK','ACTIVE',NULL,"
                 "1,3,0,0)")
    conn.execute("INSERT INTO symbols VALUES ('AAA','A','NYSE','STOCK','ACTIVE',NULL,"
                 "1,1,0,0)")
    conn.commit()
    conn.close()
    return path


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    return _make_db(tmp_path_factory.mktemp("hires") / "t.db")


@pytest.fixture(scope="module")
def report(db, tmp_path_factory):
    out = tmp_path_factory.mktemp("hires_out")
    assert HE.main(["prog", str(db), "--out", str(out)]) == 0
    return json.loads((out / "hires_events.json").read_text(encoding="utf-8")), out


# --------------------------------------------------------------------------- #
# 1. 사건 정의 — 랭킹만으로. 가격은 한 번도 안 들어간다
# --------------------------------------------------------------------------- #
def _rank(rows: list[list[float]]) -> np.ndarray:
    return np.asarray(rows, dtype="float32")


def test_new_entry_fires_on_the_first_snap_the_symbol_is_inside_top_n():
    r = _rank([[NAN], [5.0], [5.0], [NAN], [5.0]])
    rows, cols = HE.new_entry_events(r, 10)
    assert list(rows) == [1, 4] and list(cols) == [0, 0]


def test_new_entry_is_not_fired_by_movement_inside_top_n():
    """이미 안에 있던 종목이 순위만 바꾼 것은 E1 이 아니다 (그건 E2 다)."""
    r = _rank([[9.0], [3.0], [8.0]])
    rows, _c = HE.new_entry_events(r, 10)
    assert rows.size == 0


def test_new_entry_respects_the_top_n_cut():
    """N 을 넘나드는 것도 신규진입이다 — 목록 밖에서 안으로 들어온 순간이다."""
    r = _rank([[30.0], [8.0]])
    assert HE.new_entry_events(r, 10)[0].tolist() == [1]
    assert HE.new_entry_events(r, 50)[0].size == 0


def test_first_row_can_never_be_an_event_because_it_has_no_previous_snap():
    r = _rank([[5.0], [5.0]])
    assert HE.new_entry_events(r, 10)[0].size == 0
    assert HE.rank_jump_events(r, 1)[0].size == 0


def test_rank_jump_needs_both_sides_present_so_an_entry_is_not_a_jump():
    """없다가 들어온 것은 E1 이지 E2 가 아니다 — 시작 순위가 없기 때문이다."""
    r = _rank([[NAN], [1.0]])
    rows, _c, _f = HE.rank_jump_events(r, 5)
    assert rows.size == 0


def test_rank_jump_measures_improvement_not_absolute_distance():
    r = _rank([[90.0], [20.0], [80.0]])          # 70 개선 -> 60 악화
    rows, _c, frm = HE.rank_jump_events(r, 20)
    assert rows.tolist() == [1] and frm.tolist() == [90.0]


def test_dwell_start_requires_m_consecutive_snaps_and_is_a_subset_of_new_entry():
    r = _rank([[NAN], [5.0], [5.0], [NAN], [5.0], [5.0], [5.0]])
    e1_rows = set(HE.new_entry_events(r, 10)[0].tolist())
    d3, _c, _cens = HE.dwell_start_events(r, 10, 3)
    d2, _c2, _cens2 = HE.dwell_start_events(r, 10, 2)
    assert d3.tolist() == [4]                     # 2연속짜리는 탈락
    assert d2.tolist() == [1, 4]
    assert set(d3.tolist()) <= e1_rows and set(d2.tolist()) <= e1_rows


def test_dwell_counts_right_censored_starts_separately():
    """청크 끝에 걸려 **길이를 모르는** 것과 길이가 모자란 것은 다르다."""
    r = _rank([[NAN], [5.0], [5.0]])
    rows, _c, censored = HE.dwell_start_events(r, 10, 3)
    assert rows.size == 0 and censored == 1


def test_forward_run_lengths_counts_downward_not_upward():
    p = np.array([[True], [True], [False], [True]])
    assert HE.forward_run_lengths(p)[:, 0].tolist() == [2, 1, 0, 1]


def test_event_set_is_invariant_to_price(tmp_path):
    """★ **가장 중요한 테스트.** `last_u` 를 통째로 뒤흔들어도 사건이 그대로여야 한다.

    사건 정의에 가격이 새어 들어가면 여기서 깨진다. 설계 A 는 정확히 그 새어 들어감으로
    죽었고, 그 죽음은 데이터를 더 모아도 안 뒤집힌다(정의상).
    """
    plain = _make_db(tmp_path / "plain.db")
    mixed = _make_db(tmp_path / "mixed.db", scramble_price=True)
    a = HE.build_report(HE.run(plain))
    b = HE.build_report(HE.run(mixed))
    key = lambda rep: sorted((c["ranking_type"], c["kind"], c["cell"], c["n"],
                              c["n_zero_60"], c["n_zero_300"]) for c in rep["cells"])
    assert key(a) == key(b), "event counts moved when only last_u changed"
    # 반대 방향: 가격층 표는 **바뀌어야** 한다. 안 바뀌면 위 검사가 공허하다.
    band = lambda rep: sorted((r["price_band"], r["n"]) for r in rep["by_price_band"])
    assert band(a) != band(b)


# --------------------------------------------------------------------------- #
# 2. 테이프 커버리지 — 창 경계와 "0 건의 이유"
# --------------------------------------------------------------------------- #
def _keys(pairs) -> np.ndarray:
    a = np.array([c * HE.TS_STRIDE + t for c, t in pairs], dtype="int64")
    a.sort()
    return a


def test_post_window_excludes_t0_itself_and_includes_the_far_edge():
    """`(t0, t0+W]` — `t0` 정각 체결은 **미래가 아니다**. 넣으면 룩어헤드가 된다."""
    k = _keys([(0, 1_000), (0, 1_001), (0, 61_000), (0, 61_001)])
    t0 = np.array([1_000], dtype="int64")
    got = HE.window_counts(k, np.array([0]), t0, 0, 60 * MS)
    assert got.tolist() == [2]          # 1001 과 61000 만


def test_pre_window_includes_t0_minus_w_and_excludes_t0():
    k = _keys([(0, 1_000), (0, 41_000), (0, 41_001), (0, 101_000)])
    t0 = np.array([101_000], dtype="int64")
    got = HE.window_counts(k, np.array([0]), t0, -60 * MS - 1, -1)
    assert got.tolist() == [2]          # 41000, 41001 (101000 은 t0 자신이라 제외)


def test_window_counts_never_leak_across_symbols():
    """부호화 키가 종목을 진짜로 가르는가 — 안 가르면 이웃 종목의 체결을 센다."""
    k = _keys([(1, 5_000), (1, 6_000), (2, 5_500)])
    t0 = np.array([4_000, 4_000], dtype="int64")
    got = HE.window_counts(k, np.array([1, 2]), t0, 0, 60 * MS)
    assert got.tolist() == [2, 1]


def test_window_counts_matches_a_brute_force_reference():
    """빠른 길과 느린 길이 같은 답을 내는가. 빠른 쪽만 있으면 틀려도 안 보인다."""
    rng = np.random.default_rng(20260813)
    codes = rng.integers(0, 6, 400)
    ts = rng.integers(1_785_000_000_000, 1_785_000_600_000, 400)
    k = _keys(list(zip(codes.tolist(), ts.tolist())))
    q_codes = rng.integers(0, 6, 50)
    q_t0 = rng.integers(1_785_000_000_000, 1_785_000_600_000, 50)
    fast = HE.window_counts(k, q_codes, q_t0, 0, 60 * MS)
    slow = [int(((codes == c) & (ts > t) & (ts <= t + 60 * MS)).sum())
            for c, t in zip(q_codes, q_t0)]
    assert fast.tolist() == slow


def test_interval_overlap_finds_a_gap_that_starts_before_and_ends_inside():
    lo = _keys([(3, 1_000)])
    hi = np.maximum.accumulate(np.array([3 * HE.TS_STRIDE + 50_000], dtype="int64"))
    got = HE.interval_overlap(lo, hi, np.array([3]), np.array([40_000]), 60 * MS)
    assert got.tolist() == [True]


def test_interval_overlap_does_not_borrow_an_earlier_symbols_interval():
    """누적 최대가 종목 경계를 넘으면 **없는 결손을 있다고** 보고한다.

    앞 종목(코드 1)의 결손 끝이 아주 크더라도 코드 2 의 질의에 물들면 안 된다.
    부호화가 `code * STRIDE + ts` 라 앞 종목 값은 구조적으로 더 작다는 것에 기댄다.
    """
    lo = _keys([(1, 1_000), (2, 900_000)])
    hi_raw = np.array([1 * HE.TS_STRIDE + 800_000, 2 * HE.TS_STRIDE + 950_000],
                      dtype="int64")
    hi = np.maximum.accumulate(hi_raw)
    got = HE.interval_overlap(lo, hi, np.array([2, 2]),
                              np.array([10_000, 900_000]), 60 * MS)
    assert got.tolist() == [False, True]


def test_downtime_overlap_flags_a_window_that_touches_a_dead_stretch():
    ivs = [(100_000, 200_000)]
    got = HE.downtime_overlap(ivs, np.array([50_000, 300_000]), 60 * MS)
    assert got.tolist() == [True, False]


def test_zero_trades_is_split_into_no_tape_symbol_and_empty_window(report):
    """★ 이 태스크의 핵심 구분. 뭉치면 우리 티어 구조가 유동성 부재로 읽힌다."""
    rep, _out = report
    cells = {(c["kind"], c["cell"]): c for c in rep["cells"]
             if c["ranking_type"] == "MARKET_TRADING_VOLUME"}
    c = cells[("E1_new_entry", "N100")]
    # 테이프 없는 종목은 반드시 창도 비어 있다 — 반대는 성립하지 않는다.
    assert c["n_no_tape_symbol"] <= c["n_zero_300"] <= c["n"]
    assert c["n_zero_300"] <= c["n_zero_60"]
    # BBB 는 DAY_A 에만 테이프가 있으므로 "테이프 없음"과 "창이 빔"이 **갈린다**.
    assert 0 < c["n_no_tape_symbol"] < c["n"]


def test_the_planted_trades_are_actually_counted(report):
    """0 건이 압도적이라 계측기가 **아무것도 못 세는** 상태여도 표는 똑같아 보인다.

    그래서 심어 둔 체결이 실제로 세이는지 본다 — 그것이 위 표의 신뢰 조건이다.
    """
    rep, _out = report
    tot = sum(c["mean_300"] * c["n"] for c in rep["cells"])
    assert tot > 0, "no trade was counted anywhere - the tape join is dead"


# --------------------------------------------------------------------------- #
# 3. 홀드아웃 — 데이터가 아니라 **코드**가 막아야 한다
# --------------------------------------------------------------------------- #
def test_holdout_floor_is_after_the_sealed_window_and_agrees_with_the_library():
    floor = HE.holdout_floor_ms()
    assert not SS.is_holdout(floor)
    assert SS.is_holdout(floor - HE.MIN_MS)


def test_holdout_rows_are_planted_but_never_reach_the_report(report):
    """★ 가드 발화 시험. 봉인 구간 행을 **심어 두고** 보고서에 안 나오는지 본다."""
    rep, _out = report
    assert HOLDOUT_DAY not in [s["session"] for s in rep["sessions"]]
    assert rep["holdout"]["window"] == [SS.HOLDOUT_START, SS.HOLDOUT_END]


def test_the_planted_holdout_rows_really_exist_so_the_guard_is_what_removed_them(db):
    """가드가 막은 것인지 **데이터가 원래 없던 것인지**를 가른다.

    바닥을 0 으로 낮추면 같은 DB 에서 봉인 날짜가 **나온다**. 이 대조가 없으면 위
    테스트는 "우연히 데이터가 없다"와 구분되지 않는다 — 통과가 아무것도 보증하지 않는다.
    """
    conn = HE.open_ro(db)
    try:
        until = int(conn.execute("SELECT MAX(snap_ms) FROM rankings_snap").fetchone()[0])
        guarded = HE.load_snap_grid(conn, HE.holdout_floor_ms(), until)
        unguarded = HE.load_snap_grid(conn, 0, until)
    finally:
        conn.close()
    dates = lambda g: set(pd.to_datetime(g, unit="ms", utc=True).strftime("%Y-%m-%d"))
    assert HOLDOUT_DAY in dates(unguarded)
    assert HOLDOUT_DAY not in dates(guarded)


def test_holdout_drop_is_reported_as_a_number_not_silently(report):
    rep, _out = report
    assert isinstance(rep["holdout"]["dropped_events"], int)


# --------------------------------------------------------------------------- #
# 4. 조용한 실패의 자리들 — 중복 행 · 세션 라벨 · 경계
# --------------------------------------------------------------------------- #
def test_duplicate_symbol_in_one_snap_is_dropped_and_counted():
    """§4.4-D 의 pandas 중복 인덱스 사고와 같은 자리. **세어서** 내보낸다."""
    df = pd.DataFrame({"snap_ms": [1, 1, 2], "rank": [7, 3, 4],
                       "symbol": ["X", "X", "X"], "last_u": [10, 20, 30]})
    mat = HE.rank_matrix(df)
    assert mat["n_dup_dropped"] == 1
    assert mat["rank"][0, 0] == 3.0            # 좋은 순위를 남긴다
    assert mat["last_u"][0, 0] == 20.0         # 남긴 행의 가격이 따라온다


def test_rank_matrix_is_empty_safe():
    mat = HE.rank_matrix(pd.DataFrame(columns=["snap_ms", "rank", "symbol", "last_u"]))
    assert mat["rank"].shape == (0, 0) and mat["n_dup_dropped"] == 0


def test_utc_day_equals_the_session_cycle_for_this_data():
    """태스크 전제(*UTC 일자 = 세션*)를 **믿지 않고 확인**한다."""
    lo = _day0(DAY_A)
    assert HE.session_label_agrees(lo, lo + HE.DAY_MS)
    assert not HE.session_label_agrees(lo + 3_600_000, lo + HE.DAY_MS + 3_600_000)


def test_session_label_check_is_clean_on_the_synthetic_db(report):
    rep, _out = report
    assert rep["session_label_mismatch"] == []


def test_price_bands_cover_the_target_tiers_and_flag_missing_values():
    got = HE.price_band_codes(np.array([1_000.0, 2_000_000.0, 4_999_999.0,
                                        9_999_999.0, 10_000_000.0, NAN]))
    assert list(got) == ["p0_2", "p2_5", "p2_5", "p5_10", "p10_up", "unknown"]


def test_downtime_intervals_only_fire_past_the_threshold():
    grid = np.array([0, 10_000, 10_000 + HE.DOWNTIME_MIN_MS + 1], dtype="int64")
    assert HE.downtime_intervals(grid) == [(10_000, 10_000 + HE.DOWNTIME_MIN_MS + 1)]


def test_boundary_census_names_its_evidence_for_every_boundary(report):
    """*"경계가 있다"* 는 주장은 근거 문서 없이는 인용할 수 없다."""
    rep, _out = report
    keys = {b["key"] for b in HE.CONFIG_BOUNDARIES}
    assert keys == {r["boundary"] for r in rep["boundaries"]}
    for r in rep["boundaries"]:
        assert r["evidence"].startswith("docs/")
        assert r["rows_before"] >= 0 and r["rows_after"] >= 0


def test_every_ranking_type_gets_its_own_row_so_types_are_never_merged(report):
    """08-04 와 08-07 경계에서 표본이 조용히 섞이지 않으려면 타입이 키여야 한다."""
    rep, _out = report
    for b in HE.CONFIG_BOUNDARIES:
        got = {r["ranking_type"] for r in rep["boundaries"] if r["boundary"] == b["key"]}
        assert got == {t for t, _d in HE.RANKING_SPECS}


# --------------------------------------------------------------------------- #
# 5. 산출물 — 문서 수치는 러너가 만든다
# --------------------------------------------------------------------------- #
def test_cell_records_match_the_manifest_exactly(report):
    """부분집합이 아니라 **동일 집합**이다. 필드를 늘리고 매니페스트를 잊으면 깨진다."""
    rep, _out = report
    assert rep["cells"], "runner produced no cells"
    for row in rep["cells"]:
        assert set(row) == set(HE.REPORTED_FIELDS), f"shape drift in {row.get('cell')}"


def test_manifest_carries_no_forward_return_field():
    """**이번 태스크는 결과를 재지 않는다.** 수익·MFE 필드가 생기면 범위를 넘은 것이다."""
    banned = ("ret", "mfe", "alpha", "pnl", "edge", "peak")
    for f in HE.REPORTED_FIELDS:
        assert not any(b in f.lower() for b in banned), f"{f} looks like an outcome field"


def test_report_carries_the_measurement_conditions_on_every_run(report):
    """*"N 초 창, 표본 X 에서 관측 안 됨"* 을 쓰려면 조건이 산출물에 있어야 한다."""
    rep, _out = report
    c = rep["conditions"]
    assert c["poll_period_s"] == 12.4 and c["poll_5s_deployed"] is False
    assert c["received_ranking_age_median_s"] == 16.1
    assert "snap_ms" in c["t0_definition"] and "16.1" in c["t0_definition"]


def test_report_records_the_grid_so_a_reader_knows_what_was_swept(report):
    rep, _out = report
    assert rep["grid"]["top_n"] == list(HE.TOP_NS)
    assert rep["grid"]["jump_k"] == list(HE.JUMP_KS)
    assert rep["grid"]["dwell_snaps"] == list(HE.DWELL_SNAPS)


def test_tape_gap_census_says_when_the_instrument_starts(report):
    """2026-08-07 이전 세션에 결손 기록이 없는 것은 **결손이 없었다는 뜻이 아니다**."""
    rep, _out = report
    assert "missing instrumentation" in rep["tape_gaps"]["note"]


def test_runner_writes_outside_the_source_tree(report):
    _rep, out = report
    assert "analysis" not in HE.OUT_DIR.resolve().parts
    assert (out / "hires_events.json").exists()


def test_console_output_is_pure_ascii(report, capsys):
    """cp949 콘솔에서 비 ASCII 는 `UnicodeEncodeError` 로 죽는다 (README 금지 항목)."""
    rep, _out = report
    HE.print_report(rep)
    out = capsys.readouterr().out
    out.encode("ascii")            # 비 ASCII 가 있으면 여기서 터진다
    assert "SAMPLE COUNTS ONLY" in out


def test_pct_table_refuses_to_invent_a_median_from_nothing():
    assert HE.pct_table([]) == {"n": 0}
    assert HE.pct_table([1, 2, 3])["p50"] == 2.0


def test_iso_ms_round_trips_the_boundary_constants():
    for b in HE.CONFIG_BOUNDARIES:
        assert HE.ms_iso(HE.iso_ms(b["utc"])) == b["utc"]

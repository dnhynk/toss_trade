"""`e2_design_funnel` (`docs/68`) 이 지키는 것 아홉.

1. **좌석 상태는 원장의 마지막 전이로 정한다.** 승격 행의 시각 자체는 좌석 안이고(수집기가
   같은 스냅에서 승격한다), 강등 뒤는 좌석 밖이며, 사유가 스코어/차선으로 갈린다.
2. **창 경계** - 사전 창은 `[lo, hi)`, 앵커 창은 `(lo, hi]` (러너와 같은 경계).
3. **한 시대만 연다.** 탐색 B 는 자기 세션만, 확증 바닥(08-26)에 심은 날은 안 닿고,
   모르는 시대 이름은 예외다.
4. **앵커 수가 러너와 같다.** 같은 DB 에서 두 모듈의 `n_anchored` 가 어긋나면 자가 둘이다.
5. **가격이 사건에 안 들어간다.** `last_u` 를 뒤흔들어도 깔때기가 같다(층만 달라진다).
6. **좌석 출처가 끝까지 붙는다.** 스코어로 앉힌 날은 `score`, 차선으로 앉힌 날은 `lane`.
7. **전방 수익 CI 가 어디에도 없다.** 짝 진단에는 CI 키 자체가 없다.
8. **라벨 다섯이 붙고 판정 문구가 없다.** 콘솔은 ASCII 다.
9. **랭킹 밖 후보는 세기만 한다** - `first_print`·호가 표가 나오고 수익 열이 없다.
"""
from __future__ import annotations

import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

from tossmon.analysis import hires_events as HE
from tossmon.analysis.measure import e2_design_funnel as EF
from tossmon.analysis.measure import ranking_forward_path as RFP

MS = 1000
OPEN_MS = HE.REGULAR_OPEN_S * MS
POLL_MS = 12_000
N_SNAPS = 200
EVENT_SNAP = 100
TYPES = (("TOSS_SECURITIES_TRADING_VOLUME", "realtime"),
         ("MARKET_TRADING_VOLUME", "realtime"))


def _day0(d: str) -> int:
    return int(pd.Timestamp(d + "T00:00:00Z").timestamp() * 1000)


def _rank_rows(day: str, *, scramble: bool = False) -> list:
    """`AAA` 는 계속 1 위. `BBB` 는 `EVENT_SNAP` 부터 **5 위**로 들어온다 - E1 N10 사건."""
    base = _day0(day) + OPEN_MS
    rows = []
    for i in range(N_SNAPS):
        plan = [("AAA", 1, 1_500_000)]
        if i >= EVENT_SNAP:
            plan.append(("BBB", 5, 3_000_000))
        for sym, rk, last_u in plan:
            for rtype, dur in TYPES:
                px = 50_000_000 if scramble else last_u     # $50: u5 -> o5 로 전부 옮긴다
                rows.append((base + i * POLL_MS, rtype, dur, rk, sym, px, 1, 1))
    return rows


def _trade_rows(day: str) -> list:
    base = _day0(day) + OPEN_MS
    rows = []
    for s in range(0, 2400):
        px = 3_000_000 + (s % 17) * 1_000 + (5_000 if 1200 < s < 1260 else 0)
        rows.append(("BBB", base + s * MS, px, 10))
    for s in range(0, 2400, 900):
        rows.append(("AAA", base + s * MS, 1_500_000, 5))
    return rows


def _ledger_rows(day: str, *, lane: bool) -> list:
    """`BBB` 를 사건(스냅 100 = 개장 +1200 초) **앞**에 스코어로 앉히거나, 사건 **순간**에
    차선으로 앉히고 300 초 뒤 내린다."""
    base = _day0(day) + OPEN_MS
    t0 = base + EVENT_SNAP * POLL_MS
    zzz = ("ZZZ", base + 100 * MS, 1, 2, "first_print", None)   # 테이프 없는 첫 체결 전이
    if lane:
        return [("BBB", t0, 2, 3, "ranking_tier3", 0.3),
                ("BBB", t0 + 300 * MS, 3, 2, "ranking_hold_expired", None), zzz]
    return [("BBB", base + 50 * MS, 2, 3, "capacity_fill", 0.7),
            ("BBB", base + 2000 * MS, 3, 2, "score_decay", None), zzz]


def _make_db(path, days, *, scramble: bool = False, lane_days: tuple = ()):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE rankings_snap (id INTEGER PRIMARY KEY AUTOINCREMENT,
            snap_ms INTEGER NOT NULL, ranking_type TEXT NOT NULL, duration TEXT NOT NULL,
            rank INTEGER NOT NULL, symbol TEXT NOT NULL, last_u INTEGER NOT NULL,
            vol_qu INTEGER NOT NULL, amount_u INTEGER NOT NULL);
        CREATE TABLE trades_snap (symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL,
            price_u INTEGER NOT NULL, qty_u INTEGER NOT NULL,
            PRIMARY KEY (symbol, ts_ms, price_u, qty_u)) WITHOUT ROWID;
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
                         _rank_rows(d, scramble=scramble))
        conn.executemany("INSERT INTO trades_snap VALUES (?,?,?,?)", _trade_rows(d))
        conn.executemany("INSERT INTO promotions (symbol, ts_ms, from_tier, to_tier, "
                         "reason, score) VALUES (?,?,?,?,?,?)",
                         _ledger_rows(d, lane=d in lane_days))
        base = _day0(d) + OPEN_MS
        ob = [("BBB", base + i * 4 * MS, None, 1, 1, 1, 1, "{}", 0, 0.0)
              for i in range(200)]
        ob += [("AAA", base + i * 600 * MS, None, 1, 1, 1, 1, "{}", 0, 0.0)
               for i in range(5)]
        ob += [("CCC", base + i * 4 * MS, None, 1, 1, 1, 1, "{}", 0, 0.0)
               for i in range(150)]
        conn.executemany("INSERT INTO orderbook_snap (symbol, snap_ms, ts_ms, bid1_u, "
                         "bid1_qu, ask1_u, ask1_qu, depth_json, spread_u, imbalance_signed) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?)", ob)
    conn.commit()
    conn.close()
    return path


A_DAYS = ("2026-08-10", "2026-08-11")
B_DAYS = ("2026-08-18", "2026-08-19")
MIXED = A_DAYS + ("2026-08-14",) + B_DAYS + ("2026-08-26",)


@pytest.fixture(scope="module")
def mixed_db(tmp_path_factory):
    return _make_db(tmp_path_factory.mktemp("ef") / "t.db", MIXED, lane_days=("2026-08-19",))


@pytest.fixture(scope="module")
def report_b(mixed_db, tmp_path_factory):
    out = tmp_path_factory.mktemp("ef_out")
    assert EF.main(["prog", str(mixed_db), "--era", "B", "--out", str(out),
                    "--name", "b"]) == 0
    return json.loads((out / "b.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 1. 좌석 상태
# --------------------------------------------------------------------------- #
def test_seat_state_follows_the_last_transition():
    led = {"BBB": {"ts": np.asarray([100, 200, 300, 400], dtype="int64"),
                   "tier": np.asarray([2, 3, 2, 3], dtype="int64"),
                   "reason": np.asarray(["price_activity", "capacity_fill",
                                         "score_decay", "ranking_tier3"], dtype=object)}}
    assert EF.seat_state_at(led, "BBB", 50)["group"] == "none"
    assert EF.seat_state_at(led, "BBB", 150)["group"] == "none"
    s = EF.seat_state_at(led, "BBB", 200)          # 승격 시각 자체는 좌석 안
    assert s["in_tier3"] and s["group"] == "score" and s["since_ms"] == 200
    assert EF.seat_state_at(led, "BBB", 299)["group"] == "score"
    assert EF.seat_state_at(led, "BBB", 300)["group"] == "none"
    s = EF.seat_state_at(led, "BBB", 450)
    assert s["group"] == "lane" and s["reason"] == "ranking_tier3" and s["since_ms"] == 400
    assert EF.seat_state_at(led, "NOPE", 450)["group"] == "none"
    assert EF.tier3_entry_after(led, "BBB", 150, 100) == "capacity_fill"
    assert EF.tier3_entry_after(led, "BBB", 200, 100) is None        # (200, 300] 에 3 없음
    assert EF.tier3_entry_after(led, "BBB", 300, 100) == "ranking_tier3"
    assert EF.seat_group("confirm") == "score" and EF.seat_group(None) == "none"


# --------------------------------------------------------------------------- #
# 2. 창 경계
# --------------------------------------------------------------------------- #
def test_window_edges_match_the_runner():
    ts = np.asarray([1000, 2000, 3000], dtype="int64")
    assert EF.count_between(ts, 1000, 3000, lo_inclusive=True) == 2     # [1000, 3000)
    assert EF.count_between(ts, 1000, 3000, lo_inclusive=False) == 2    # (1000, 3000]
    assert EF.count_between(ts, 3000, 9000, lo_inclusive=False) == 0
    assert EF.count_between(np.zeros(0, dtype="int64"), 0, 9, lo_inclusive=True) == 0


# --------------------------------------------------------------------------- #
# 3. 한 시대만
# --------------------------------------------------------------------------- #
def test_era_b_opens_only_its_sessions_and_never_the_floor(mixed_db):
    res = EF.run(mixed_db, era="B")
    assert set(res["sessions_used"]) == set(B_DAYS)
    assert res["until_ms"] < HE.iso_ms(RFP.CONFIRMATION_FLOOR_UTC)
    a = EF.run(mixed_db, era="A")
    assert set(a["sessions_used"]) == set(A_DAYS)
    with pytest.raises(ValueError):
        EF.run(mixed_db, era="AB")
    # 대조군: 심은 08-14(버린 것)·08-26(확증)은 DB 에 있다 - 안 나온 것은 코드 때문이다.
    opened = {s["session"] for s in RFP.run(mixed_db, exploration_only=False)["sessions"]}
    assert {"2026-08-14", "2026-08-26"} <= opened


# --------------------------------------------------------------------------- #
# 4. 앵커 수가 러너와 같다
# --------------------------------------------------------------------------- #
def test_anchored_counts_agree_with_the_runner(mixed_db):
    ef = EF.run(mixed_db, era="B")
    rfp = RFP.run(mixed_db, exploration_era="B", primary_cells=RFP.REVISION4_LADDER_CELLS,
                  funnel_only_elsewhere=True)
    rt, _d, kind, cell = EF.DECLARED
    mine = next(r for r in ef["funnel"] if (r["ranking_type"], r["kind"], r["cell"])
                == (rt, kind, cell))
    theirs = rfp["cells"][f"{rt}|{kind}|{cell}|all"]
    assert mine["n_events"] == theirs["n_events_regular"] > 0
    assert mine["n_anchored"] == theirs["n_anchored"] > 0
    assert mine["n_has_tape"] == theirs["n_symbol_has_tape"]


# --------------------------------------------------------------------------- #
# 5. 가격이 사건에 안 들어간다
# --------------------------------------------------------------------------- #
def test_funnel_is_invariant_to_price(tmp_path):
    a = EF.run(_make_db(tmp_path / "a.db", B_DAYS), era="B")
    b = EF.run(_make_db(tmp_path / "b.db", B_DAYS, scramble=True), era="B")
    keys = ("n_events", "n_has_tape", "n_pre_match60", "n_anchored",
            "n_anchored_and_pre_match60")
    for ra, rb in zip(a["funnel"], b["funnel"]):
        assert all(ra[k] == rb[k] for k in keys), (ra, rb)
    # 그리고 층 표는 달라져야 앞 검사가 공허하지 않다.
    assert any(ra["n_u5"] != rb["n_u5"] for ra, rb in zip(a["funnel"], b["funnel"]))


# --------------------------------------------------------------------------- #
# 6. 좌석 출처가 끝까지 붙는다
# --------------------------------------------------------------------------- #
def test_seat_provenance_reaches_the_declared_cell(report_b):
    per = {r["session"]: r for r in report_b["declared_per_session"]}
    assert per["2026-08-18"]["anchored_by_seat_at_t0"]["score"] >= 1
    assert per["2026-08-18"]["anchored_by_seat_at_t0"]["lane"] == 0
    assert per["2026-08-19"]["anchored_by_seat_at_t0"]["lane"] >= 1
    assert per["2026-08-19"]["anchored_by_seat_at_t0"]["score"] == 0
    # 러너가 이미 뽑은 짝도 같은 출처로 갈린다 - 세션 표에서 확인한다.
    by = {s["session"]: s["by_seat_n"] for s in report_b["declared_pairs"]["by_session"]}
    assert by["2026-08-18"]["score"] >= 1 and by["2026-08-18"]["lane"] == 0
    assert by["2026-08-19"]["lane"] >= 1 and by["2026-08-19"]["score"] == 0


# --------------------------------------------------------------------------- #
# 7. 전방 수익 CI 가 어디에도 없다
# --------------------------------------------------------------------------- #
def _keys(obj, acc):
    if isinstance(obj, dict):
        for k, v in obj.items():
            acc.add(str(k)); _keys(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _keys(v, acc)
    return acc


def test_no_confidence_interval_anywhere(report_b):
    keys = _keys(report_b, set())
    assert not any(k.startswith("ci") or "bonferroni" in k for k in keys), keys
    assert "no_ci_reason" in report_b["declared_pairs"]["by_seat"]
    for r in report_b["funnel"]:
        assert not any("ret" in k or "mfe" in k for k in r), r


# --------------------------------------------------------------------------- #
# 8. 라벨 · 판정 문구 · ASCII
# --------------------------------------------------------------------------- #
def test_labels_and_no_verdict_words(report_b):
    assert report_b["labels"] == list(RFP.LABELS) and len(report_b["labels"]) == 5
    blob = json.dumps(report_b).lower()
    for bad in EF.FORBIDDEN_PHRASES:
        assert bad.lower() not in blob
    assert report_b["design"]["declared_cell"] == list(EF.DECLARED)
    assert "NOT subtracted" in report_b["design"]["costs"]


def test_console_is_pure_ascii(report_b, capsys):
    EF.print_report(report_b)
    out = capsys.readouterr().out
    assert out and all(ord(ch) < 128 for ch in out)


# --------------------------------------------------------------------------- #
# 9. 랭킹 밖 후보는 세기만 한다
# --------------------------------------------------------------------------- #
def test_non_ranking_candidates_are_counted_not_measured(report_b):
    fp = report_b["first_print"]["total"]
    assert fp["n_events"] == len(B_DAYS)            # 날마다 ZZZ 하나
    assert fp["n_tape_within_300s"] == 0             # ZZZ 는 테이프가 없다
    ob = report_b["orderbook"]["total"]
    assert ob["n_symbols_dense"] == 2 * len(B_DAYS)  # BBB, CCC
    assert ob["n_dense_not_tape"] == len(B_DAYS)     # CCC: 호가는 촘촘한데 테이프 없음
    assert ob["n_dense_and_tape"] == len(B_DAYS)     # BBB
    sn = report_b["declared_pairs"]["sessions_needed"]
    # 합성 세션 둘이 똑같아 짝 분산이 0 이다 - 구조만 본다(반폭 셋, 세션당 짝 수 > 0).
    assert [t["half_width"] for t in sn["targets"]] == list(EF.TARGET_HALF_WIDTHS)
    assert sn["pairs_per_session"] > 0 and "iid" in sn["caveat"]

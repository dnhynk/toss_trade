"""**다음 `E` 의 설계 진단** — `docs/68`. 사건이 잴 수 있는 자리에 있는가를 **세고**, 왜 그런지를
**좌석의 출처**로 가른다. 전방 경로 CI 는 이 모듈이 만들지 않는다.

## 이 모듈의 지위 — **탐색이다. 판정이 아니다. 그리고 대부분은 세기만 한다**

`G2G3-PREREG` §2-1 개정 4 (2026-08-26) 는 탐색 B(08-18~25) 에서 위약 사다리를 붙일 칸을
**하나**(`E1_new_entry N10`, `REVISION4_LADDER_CELLS`)로 데이터 전에 못 박았다. 그래서
이 모듈은 두 가지만 한다:

1. **깔때기 — 후보 칸마다 사건 수·앵커 수만.** 전방 수익은 한 줄도 계산하지 않는다.
   깔때기에 한 단을 더 넣었다 — **사전 상태가 관측되는가**(`t0` 직전 60 초에 초 막대 3 개,
   `rv60` 이 정의되는 최소 조건, `docs/64` §8-1) — 그리고 앵커가 붙은 사건을 **그 종목이
   tier3 좌석을 어떻게 얻었는가**로 가른다: 스코어 경로(`capacity_fill`·`confirm`·
   `precursor`) / D-21 차선(`ranking_tier3`) / 좌석 없음. `trades_snap` 은 tier3 에서만
   채워지므로(`docs/61` §1-2) 이 가름이 곧 *"누가 잴 수 있게 됐나"* 다.
2. **선언한 칸 하나의 진단 — 러너가 이미 뽑은 짝 그대로.** `ranking_forward_path.run()` 을
   같은 씨앗으로 다시 불러 `placebo_vol_density_matched` 팔의 짝을 복원하고(새 추첨 없음,
   `forward_depth_mechanism.pair_table` 과 같은 검산), 짝을 **좌석 출처**와 **세션**으로
   가른다. **CI 를 붙이지 않는다** — 가르는 축이 수집기 내부 상태라 추정량이 아니라
   진단이다(`docs/65` §3 의 `depth_strata` 와 같은 규율).

그리고 랭킹 밖 후보 셋의 **측정 가능성만** 센다: 토스 전용 진입(같은 순간 `MARKET` 상위
100 에 없는 `TOSS` 상위 10 진입), `first_print` 승격(`docs/00` §2-2 의 3 — 사건 정의로
한 번도 안 쟀던 것), 호가 커버리지(`orderbook_snap` 이 tier3 밖에서도 붙는가).

## 시대를 뭉치지 않는다

`run(era=...)` 은 한 시대만 연다 — 탐색 A(D-21 이전 9 세션) 또는 탐색 B(D-21 이후 6 세션).
창·세션 목록·확증 바닥은 전부 `ranking_forward_path` 의 상수를 그대로 쓴다. 두 시대를 여는
인자는 없다.

## 하지 않는 것

- **판정하지 않는다.** 통과/실패를 쓰지 않는다.
- **비용을 차감하지 않는다.** G-3 이고 사전등록이 필요하다.
- **새 사건 정의를 만들지 않는다.** 사건은 `hires_events.chunk_events` 의 격자 그대로다 —
  가격은 여전히 정의에 안 들어간다. 토스 전용 진입은 **격자 사건을 다른 목록의 동시 상태로
  자른 것**이지 새 정의가 아니고, 그 수를 세기만 한다.
- **홀드아웃(2026-05-01~07-29)을 열지 않는다.** `hires_events.holdout_floor_ms` 바닥 +
  `session.drop_holdout`.
- **확증 팔(08-26 이후)을 읽지 않는다.** 시대 천장이 상수다.
- **라이브 워크트리에 쓰지 않는다.** DB 는 `mode=ro`, API 호출 0 건.

실행: `python -m tossmon.analysis.measure.e2_design_funnel [db] --era A|B [--out DIR]
[--name NAME]` -> `out/<name>.json`. **콘솔 ASCII.**
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from tossmon.analysis import hires_events as HE
from tossmon.analysis import session as SS
from tossmon.analysis.measure import ranking_forward_path as RFP

OUT_DIR = RFP.OUT_DIR
SEC_MS = RFP.SEC_MS

#: 깔때기를 세는 후보 칸. **전방 경로는 어느 칸에도 안 붙는다** — 세기만 한다.
#: 첫 줄이 개정 4 가 선언한 칸이다.
CANDIDATE_CELLS = (
    ("TOSS_SECURITIES_TRADING_VOLUME", "realtime", "E1_new_entry", "N10"),
    ("TOSS_SECURITIES_TRADING_VOLUME", "realtime", "E1_new_entry", "N20"),
    ("TOSS_SECURITIES_TRADING_VOLUME", "realtime", "E1_new_entry", "N50"),
    ("TOSS_SECURITIES_TRADING_VOLUME", "realtime", "E2_rank_jump", "K10"),
    ("TOSS_SECURITIES_TRADING_VOLUME", "realtime", "E2_rank_jump", "K10_from11_20"),
    ("MARKET_TRADING_VOLUME", "realtime", "E1_new_entry", "N10"),
    ("MARKET_TRADING_VOLUME", "realtime", "E2_rank_jump", "K10"),
    ("TOP_GAINERS", "1d", "E1_new_entry", "N10"),
    ("TOP_GAINERS", "1d", "E2_rank_jump", "K10"),
)
#: 개정 4 가 선언한 칸 — 짝 진단은 여기에만 붙는다.
DECLARED = ("TOSS_SECURITIES_TRADING_VOLUME", "realtime") + RFP.REVISION4_LADDER_CELLS[0]
#: 짝 진단이 보는 팔. `G2G3-PREREG` §3-1 개정 3 의 사전 상태 정합 팔이다.
DIAG_ARM = "placebo_vol_density_matched"

#: 사전 상태가 **관측되는** 최소 조건 — `rv60` 은 직전 60 초에 초 막대가 3 개 이상 있어야
#: 정의된다(`docs/64` §8-1). 러너의 `key_miss` 와 대조할 수 있게 같은 창을 쓴다.
PRE_MATCH_S = 60
PRE_MATCH_MIN_BARS = 3
#: 사전 테이프 존재 창과 앵커 창. 러너의 `ANCHOR_MAX_WAIT_S` 와 같은 300 초다.
PRE_TAPE_S = 300
FWD_S = RFP.ANCHOR_MAX_WAIT_S

#: tier3 좌석의 출처. `promotions.reason` 은 모든 전이를 남기므로(진입·강등 둘 다)
#: `t` 시점의 상태는 "그 앞 마지막 행의 `to_tier`" 다.
SCORE_REASONS = ("capacity_fill", "confirm", "precursor")
LANE_REASONS = ("ranking_tier3",)
SEAT_GROUPS = ("score", "lane", "none")

#: 토스 전용 진입 판정에 쓰는 `MARKET` 스냅의 최대 나이. 랭킹 폴이 12 초라 30 초면
#: 인접 스냅이다(`hires_events.ADJACENCY_MAX_MS` 와 같은 값).
MARKET_SNAP_MAX_AGE_MS = HE.ADJACENCY_MAX_MS
MARKET_LIST = ("MARKET_TRADING_VOLUME", "realtime")

#: 호가가 **촘촘히** 붙은 종목의 최소 스냅 수. tier3 호가는 4 초 주기라 정규장에 수천
#: 스냅이고, tier2 라운드로빈(600 초)은 40 개를 못 넘는다 — 그 사이를 자른다.
ORDERBOOK_DENSE_MIN = 100

#: 필요 세션 수를 셀 때 쓰는 목표 반폭(비율). 결과를 보기 전에 적었다.
TARGET_HALF_WIDTHS = (0.0025, 0.005, 0.01)

NO_CI = ("diagnostic, not an estimate - split on the COLLECTOR's own seat state, so no CI "
         "is attached on purpose; a CI here would be a new comparison in the family")

FORBIDDEN_PHRASES = RFP.FORBIDDEN_PHRASES


# --------------------------------------------------------------------------- #
# 적재
# --------------------------------------------------------------------------- #
def load_second_ts(conn: sqlite3.Connection, lo_ms: int, hi_ms: int) -> dict:
    """종목별 **정렬된 초 막대 시각**. `[lo_ms, hi_ms)`. 막대 하나짜리 종목도 남긴다.

    `ranking_forward_path.session_second_bars` 와 같은 집계(`GROUP BY symbol, ts_ms`)
    이지만 그쪽은 막대 2 개 미만 종목을 버린다 — 깔때기의 "테이프가 있다" 는 1 건이면
    참이라 여기서는 안 버린다.
    """
    df = pd.read_sql_query(
        "SELECT symbol, ts_ms FROM trades_snap WHERE ts_ms >= ? AND ts_ms < ? "
        "GROUP BY symbol, ts_ms ORDER BY symbol, ts_ms",
        conn, params=(int(lo_ms), int(hi_ms)))
    if df.empty:
        return {}
    return {str(s): sub.ts_ms.to_numpy(dtype="int64")
            for s, sub in df.groupby("symbol", sort=True)}


def load_tier_ledger(conn: sqlite3.Connection, until_ms: int) -> dict:
    """`promotions` 를 종목별 전이 원장으로. `until_ms` **이전** 행만 — 시대 천장이다."""
    df = pd.read_sql_query(
        "SELECT symbol, ts_ms, to_tier, reason FROM promotions WHERE ts_ms < ? "
        "ORDER BY symbol, ts_ms, id", conn, params=(int(until_ms),))
    out = {}
    if df.empty:
        return out
    for s, sub in df.groupby("symbol", sort=True):
        out[str(s)] = {"ts": sub.ts_ms.to_numpy(dtype="int64"),
                       "tier": sub.to_tier.to_numpy(dtype="int64"),
                       "reason": sub.reason.to_numpy(dtype=object)}
    return out


def load_list_members(conn: sqlite3.Connection, rtype: str, duration: str,
                      lo_ms: int, hi_ms: int, top_n: int) -> tuple:
    """한 목록의 (스냅 시각 배열, 스냅별 상위 `top_n` 종목 집합 리스트)."""
    df = HE.load_rankings(conn, rtype, duration, lo_ms, hi_ms)
    if df.empty:
        return np.zeros(0, dtype="int64"), []
    df = df[df["rank"] <= top_n]
    snaps = np.sort(df.snap_ms.unique().astype("int64"))
    groups = df.groupby("snap_ms")["symbol"].apply(set).to_dict()
    return snaps, [groups.get(int(t), set()) for t in snaps]


# --------------------------------------------------------------------------- #
# 좌석 상태
# --------------------------------------------------------------------------- #
def seat_group(reason) -> str:
    if reason in SCORE_REASONS:
        return "score"
    if reason in LANE_REASONS:
        return "lane"
    return "none"


def seat_state_at(ledger: dict, symbol: str, t_ms: int) -> dict:
    """`t_ms` 시점에 그 종목이 tier3 인가, 어느 사유로, 언제부터.

    원장이 **모든** 전이를 남기므로(강등도 `to_tier` 로 적힌다) `t` 직전 마지막 행의
    `to_tier == 3` 이 곧 좌석이다. 행이 없으면 좌석 없음(수집기 시작 상태는 tier0/1).
    """
    led = ledger.get(symbol)
    if led is None:
        return {"in_tier3": False, "reason": None, "since_ms": None, "group": "none"}
    i = int(np.searchsorted(led["ts"], int(t_ms), side="right")) - 1
    if i < 0 or int(led["tier"][i]) != 3:
        return {"in_tier3": False, "reason": None, "since_ms": None, "group": "none"}
    # 좌석의 **시작**은 이 행이 아니라, 3 으로 올라온 마지막 행이다 (3->3 재기록은 없지만
    # 방어적으로 거슬러 올라간다).
    j = i
    while j - 1 >= 0 and int(led["tier"][j - 1]) == 3:
        j -= 1
    return {"in_tier3": True, "reason": str(led["reason"][j]),
            "since_ms": int(led["ts"][j]), "group": seat_group(str(led["reason"][j]))}


def tier3_entry_after(ledger: dict, symbol: str, t_ms: int, within_ms: int):
    """`(t, t+within]` 안에 tier3 로 **올라온** 첫 행의 사유. 없으면 None."""
    led = ledger.get(symbol)
    if led is None:
        return None
    a = int(np.searchsorted(led["ts"], int(t_ms), side="right"))
    b = int(np.searchsorted(led["ts"], int(t_ms) + int(within_ms), side="right"))
    for k in range(a, b):
        if int(led["tier"][k]) == 3:
            return str(led["reason"][k])
    return None


# --------------------------------------------------------------------------- #
# 깔때기 — 사건마다 표식
# --------------------------------------------------------------------------- #
def count_between(ts: np.ndarray, lo_ms: int, hi_ms: int, *, lo_inclusive: bool) -> int:
    """`[lo, hi)` 또는 `(lo, hi]` 안의 막대 수."""
    if ts.size == 0:
        return 0
    if lo_inclusive:      # [lo, hi)
        return int(np.searchsorted(ts, hi_ms, "left") - np.searchsorted(ts, lo_ms, "left"))
    return int(np.searchsorted(ts, hi_ms, "right")     # (lo, hi]
               - np.searchsorted(ts, lo_ms, "right"))


def tag_events(ev: pd.DataFrame, bars: dict, traded: set, ledger: dict,
               market: tuple | None) -> pd.DataFrame:
    """사건 표에 깔때기 표식을 붙인다. 가격은 층(`u5/o5`)에만 쓴다."""
    rows = []
    m_snaps, m_sets = (market if market is not None else (np.zeros(0, dtype="int64"), []))
    for r in ev.itertuples(index=False):
        t0 = int(r.t0_ms)
        ts = bars.get(r.symbol, np.zeros(0, dtype="int64"))
        pre300 = count_between(ts, t0 - PRE_TAPE_S * SEC_MS, t0, lo_inclusive=True)
        pre60 = count_between(ts, t0 - PRE_MATCH_S * SEC_MS, t0, lo_inclusive=True)
        fwd = count_between(ts, t0, t0 + FWD_S * SEC_MS, lo_inclusive=False)
        st = seat_state_at(ledger, r.symbol, t0)
        after = None if st["in_tier3"] else tier3_entry_after(
            ledger, r.symbol, t0, FWD_S * SEC_MS)
        toss_only = None
        if m_snaps.size:
            i = int(np.searchsorted(m_snaps, t0, side="right")) - 1
            if i >= 0 and t0 - int(m_snaps[i]) <= MARKET_SNAP_MAX_AGE_MS:
                toss_only = r.symbol not in m_sets[i]
        rows.append({
            "symbol": r.symbol, "t0_ms": t0,
            "tier": RFP.tier_codes(np.asarray([r.last_u], dtype="float64"))[0],
            "has_tape": r.symbol in traded,
            "pre_tape300": pre300 > 0,
            "pre_match60": pre60 >= PRE_MATCH_MIN_BARS,
            "anchored": fwd > 0,
            "seat_at_t0": st["group"],
            "seat_reason": st["reason"],
            "seat_age_s": (None if st["since_ms"] is None
                           else (t0 - st["since_ms"]) / 1000.0),
            "seat_after": seat_group(after) if after else "none",
            "seat_after_reason": after,
            "toss_only": toss_only,
        })
    return pd.DataFrame(rows)


def funnel_counts(tag: pd.DataFrame) -> dict:
    """한 칸(또는 한 세션)의 깔때기. **비율은 사건 수 분모**로만 낸다."""
    n = int(len(tag))
    if n == 0:
        return {"n_events": 0}
    a = tag.anchored.to_numpy(dtype=bool)
    pm = tag.pre_match60.to_numpy(dtype=bool)
    seat = tag.seat_at_t0.to_numpy(dtype=object)
    out = {
        "n_events": n,
        "n_has_tape": int(tag.has_tape.sum()),
        "n_pre_tape300": int(tag.pre_tape300.sum()),
        "n_pre_match60": int(pm.sum()),
        "n_anchored": int(a.sum()),
        "n_anchored_and_pre_match60": int((a & pm).sum()),
        "share_anchored": float(a.mean()),
        "share_anchored_and_pre_match60": float((a & pm).mean()),
        "seat_at_t0": {g: int((seat == g).sum()) for g in SEAT_GROUPS},
        "anchored_by_seat_at_t0": {g: int((a & (seat == g)).sum()) for g in SEAT_GROUPS},
        "anchored_and_pre_match60_by_seat_at_t0": {
            g: int((a & pm & (seat == g)).sum()) for g in SEAT_GROUPS},
        "no_seat_then_lane_within_300s": int(
            ((seat == "none") & (tag.seat_after.to_numpy(dtype=object) == "lane")).sum()),
        "no_seat_then_lane_within_300s_anchored": int(
            (a & (seat == "none")
             & (tag.seat_after.to_numpy(dtype=object) == "lane")).sum()),
        "n_u5": int((tag.tier.to_numpy(dtype=object) == "u5").sum()),
        "n_u5_anchored": int((a & (tag.tier.to_numpy(dtype=object) == "u5")).sum()),
    }
    if "toss_only" in tag and tag.toss_only.notna().any():
        t = tag.toss_only
        out["toss_only"] = {
            "n_market_snap_present": int(t.notna().sum()),
            "n_toss_only": int((t == True).sum()),          # noqa: E712
            "n_in_both_lists": int((t == False).sum()),     # noqa: E712
            "n_toss_only_anchored": int(((t == True) & tag.anchored).sum()),   # noqa: E712
            "n_toss_only_anchored_and_pre_match60": int(
                ((t == True) & tag.anchored & tag.pre_match60).sum()),         # noqa: E712
        }
    return out


# --------------------------------------------------------------------------- #
# 랭킹 밖 후보 — 측정 가능성만
# --------------------------------------------------------------------------- #
def first_print_census(conn: sqlite3.Connection, open_ms: int, close_ms: int,
                       bars: dict, ledger: dict) -> dict:
    """`first_print` 승격(휴면 -> 첫 체결 전이, `docs/00` §2-2 의 3)이 잴 수 있는 자리에 있나.

    승격 행 자체가 사건이다(수집기가 tier1 스윕에서 본 전이). 그 뒤 300 초 안에 테이프가
    있는가, tier3 로 올라가는가를 센다. 전방 수익은 안 잰다.
    """
    df = pd.read_sql_query(
        "SELECT symbol, ts_ms FROM promotions WHERE reason = 'first_print' "
        "AND ts_ms >= ? AND ts_ms < ? ORDER BY ts_ms", conn,
        params=(int(open_ms), int(close_ms)))
    n = int(len(df))
    if n == 0:
        return {"n_events": 0}
    taped = 0
    seated = 0
    pre = 0
    for r in df.itertuples(index=False):
        ts = bars.get(r.symbol, np.zeros(0, dtype="int64"))
        t = int(r.ts_ms)
        if count_between(ts, t, t + FWD_S * SEC_MS, lo_inclusive=False) > 0:
            taped += 1
        if count_between(ts, t - PRE_MATCH_S * SEC_MS, t, lo_inclusive=True) \
                >= PRE_MATCH_MIN_BARS:
            pre += 1
        if tier3_entry_after(ledger, r.symbol, t, FWD_S * SEC_MS):
            seated += 1
    return {"n_events": n, "n_tape_within_300s": taped,
            "n_tier3_within_300s": seated, "n_pre_match60": pre,
            "n_symbols": int(df.symbol.nunique())}


def orderbook_census(conn: sqlite3.Connection, open_ms: int, close_ms: int,
                     traded: set) -> dict:
    """호가가 tier3 밖에서도 붙는가. 촘촘한 호가 종목 집합과 테이프 종목 집합을 대조한다."""
    df = pd.read_sql_query(
        "SELECT symbol, COUNT(*) AS n FROM orderbook_snap "
        "WHERE snap_ms >= ? AND snap_ms < ? GROUP BY symbol", conn,
        params=(int(open_ms), int(close_ms)))
    if df.empty:
        return {"n_symbols_any": 0}
    dense = set(df.symbol[df.n >= ORDERBOOK_DENSE_MIN])
    return {"n_symbols_any": int(len(df)),
            "n_symbols_dense": int(len(dense)),
            "n_symbols_tape": int(len(traded)),
            "n_dense_and_tape": int(len(dense & traded)),
            "n_dense_not_tape": int(len(dense - traded)),
            "n_tape_not_dense": int(len(traded - dense)),
            "dense_min_snaps": ORDERBOOK_DENSE_MIN,
            "rows": int(df.n.sum())}


# --------------------------------------------------------------------------- #
# 선언한 칸 — 러너가 뽑은 짝 그대로, 좌석 출처와 세션으로 가른다 (CI 없음)
# --------------------------------------------------------------------------- #
def declared_pairs(res: dict, ledger: dict) -> pd.DataFrame:
    """`ranking_forward_path.run()` 결과에서 선언한 칸의 짝을 복원한다. 새 추첨 없음.

    `forward_depth_mechanism.pair_table` 과 같은 검산을 한다 — 원장과 행 수가 어긋나면
    예외로 죽는다. 짝 하나 = 실제 사건 하나 + 그 위약 평균.
    """
    rtype, _dur, kind, cell = DECLARED
    key = f"{rtype}|{kind}|{cell}|all"
    box = res["cells"].get(key)
    if not box or DIAG_ARM not in box["arms"]:
        return pd.DataFrame()
    a = box["arms"][DIAG_ARM]
    sess_names = sorted(a["raw"])
    if len(sess_names) != len(a["pairing"]):
        raise ValueError(f"{key}: pairing ledgers {len(a['pairing'])} vs raw sessions "
                         f"{len(sess_names)} - alignment lost")
    open_of = {s["session"]: s["open_ms"] for s in res["sessions"]}
    rows = []
    for sess, d in zip(sess_names, a["pairing"]):
        rr, pr = a["real_paired"][sess], a["raw"][sess]
        sel = np.flatnonzero(np.asarray(d["paired"], dtype=bool))
        if int(rr["_symbol"].size) != int(sel.size):
            raise ValueError(f"{key} {sess}: real_paired rows != paired fires")
        if int(pr["_symbol"].size) != int(np.asarray(d["sym"]).size):
            raise ValueError(f"{key} {sess}: placebo rows != draws")
        if sel.size == 0:
            continue
        slot = np.asarray(d["slot"], dtype="int64")
        row_of = np.full(int(sel.max()) + 1, -1, dtype="int64")
        row_of[sel] = np.arange(sel.size, dtype="int64")
        prow = row_of[slot]
        if (prow < 0).any():
            raise ValueError(f"{key} {sess}: a draw points at an unpaired fire")
        m = int(sel.size)
        cnt = np.bincount(prow, minlength=m).astype("float64")
        if (cnt == 0).any():
            raise ValueError(f"{key} {sess}: a paired fire has no draws")
        pm = np.bincount(prow, weights=np.asarray(pr[RFP.HEADLINE], "float64"),
                         minlength=m) / cnt
        real = np.asarray(rr[RFP.HEADLINE], "float64")
        t_anchor = open_of[sess] + (np.asarray(rr["t_in_session_s"], "float64")
                                    * SEC_MS).astype("int64")
        for i in range(m):
            st = seat_state_at(ledger, str(rr["_symbol"][i]), int(t_anchor[i]))
            rows.append({"session": sess, "symbol": str(rr["_symbol"][i]),
                         "t_anchor_ms": int(t_anchor[i]),
                         "real": float(real[i]), "placebo": float(pm[i]),
                         "d": float(real[i] - pm[i]),
                         "seat": st["group"], "seat_reason": st["reason"],
                         "seat_age_s": (None if st["since_ms"] is None else
                                        (int(t_anchor[i]) - st["since_ms"]) / 1000.0),
                         "nbar60": float(np.asarray(rr["nbar60"], "float64")[i])})
    return pd.DataFrame(rows)


def _group_stats(df: pd.DataFrame) -> dict:
    d = df.d.to_numpy(dtype="float64")
    ok = np.isfinite(d)
    d = d[ok]
    if d.size == 0:
        return {"n": 0}
    return {"n": int(d.size), "n_symbols": int(df.symbol[ok].nunique()),
            "mean_real": float(df.real[ok].mean()),
            "mean_placebo": float(df.placebo[ok].mean()),
            "mean_diff": float(d.mean()), "p50_diff": float(np.median(d)),
            "share_diff_positive": float((d > 0).mean()),
            "nbar60_p50": float(np.median(df.nbar60[ok])),
            "seat_age_s_p50": (None if df.seat_age_s[ok].isna().all()
                               else float(df.seat_age_s[ok].median()))}


def pairs_by_seat(pairs: pd.DataFrame) -> dict:
    """짝을 좌석 출처로 가른다. **CI 없음.**"""
    if pairs.empty:
        return {"no_ci_reason": NO_CI, "groups": {}}
    return {"no_ci_reason": NO_CI,
            "groups": {g: _group_stats(pairs[pairs.seat == g]) for g in SEAT_GROUPS},
            "all": _group_stats(pairs)}


def pairs_by_session(pairs: pd.DataFrame) -> list:
    """세션별 짝 수와 실제/위약 평균. 러너의 군집 부트스트랩이 재추출하는 바로 그 단위."""
    out = []
    for sess, sub in pairs.groupby("session", sort=True):
        g = _group_stats(sub)
        g["session"] = sess
        g["by_seat_n"] = {k: int((sub.seat == k).sum()) for k in SEAT_GROUPS}
        out.append(g)
    return out


def sessions_needed(pairs: pd.DataFrame, n_sessions: int,
                    widths: tuple = TARGET_HALF_WIDTHS) -> dict:
    """반폭 목표별 **거친** 필요 세션 수. 짝을 독립으로 보는 어림이라 **하한**이다.

    창 겹침(같은 종목의 다른 사건과 300 초 창 공유)이 짝을 비독립으로 만들고 러너의
    군집 부트스트랩은 그것을 세션 단위로만 잡는다 — 실제 필요 수는 이보다 크다.
    """
    d = pairs.d.to_numpy(dtype="float64")
    d = d[np.isfinite(d)]
    if d.size < 2 or n_sessions <= 0:
        return {"n_pairs": int(d.size), "n_sessions": int(n_sessions), "targets": []}
    sd = float(d.std(ddof=1))
    per = d.size / float(n_sessions)
    return {"n_pairs": int(d.size), "n_sessions": int(n_sessions),
            "pairs_per_session": float(per), "pair_sd": sd,
            "targets": [{"half_width": w,
                         "pairs_needed_iid": float((1.96 * sd / w) ** 2),
                         "sessions_needed_iid": float((1.96 * sd / w) ** 2 / per)}
                        for w in widths],
            "caveat": ("iid lower bound; window overlap makes pairs dependent, so "
                       "the true requirement is larger")}


# --------------------------------------------------------------------------- #
# 러너
# --------------------------------------------------------------------------- #
def run(db: Path, *, era: str = "A", progress: bool = False) -> dict:
    """한 시대를 연다. 창·세션은 `ranking_forward_path.run()` 이 같은 시대에 대해 여는
    것과 **같은 상수**로 정한다 — 그 함수를 그대로 불러 세션 목록과 짝을 받는다."""
    if era not in RFP.EXPLORATION_ERAS:
        raise ValueError(f"era must be one of {RFP.EXPLORATION_ERAS}, got {era!r}")
    # 1) 선언한 칸의 사다리 — 러너 그대로 (같은 씨앗, 같은 원장). 세션 목록도 여기서.
    res = RFP.run(db, exploration_era=era, primary_cells=RFP.REVISION4_LADDER_CELLS,
                  funnel_only_elsewhere=True, progress=progress)
    used = [s["session"] for s in res["sessions"]]
    allowed = (RFP.EXPLORATION_SESSIONS if era == "A" else RFP.EXPLORATION_B_SESSIONS)
    bad = sorted(set(used) - set(allowed))
    if bad:
        raise ValueError(f"sessions outside era {era} reached this run: {bad}")
    conn = HE.open_ro(db)
    try:
        ledger = load_tier_ledger(conn, res["until_ms"] + 1)
        cells: dict = {}
        per_session_declared = []
        first_print = []
        orderbook = []
        for s in res["sessions"]:
            lo = s["open_ms"] - PRE_TAPE_S * SEC_MS
            bars = load_second_ts(conn, lo, s["close_ms"])
            traded = {sym for sym, ts in bars.items()
                      if count_between(ts, s["open_ms"], s["close_ms"], lo_inclusive=True)}
            market = load_list_members(conn, *MARKET_LIST, s["open_ms"] - 2 * SEC_MS * 60,
                                       s["close_ms"], 100)
            for rtype, duration, kind, cell in CANDIDATE_CELLS:
                ev = RFP.session_events(conn, rtype, duration, s["day0_ms"],
                                        res["since_ms"], res["until_ms"])
                if ev.empty:
                    continue
                ev = ev[(ev.kind == kind) & (ev.cell == cell)]
                if ev.empty:
                    continue
                tag = tag_events(ev, bars, traded, ledger,
                                 market if (rtype, duration) != MARKET_LIST else None)
                key = f"{rtype}|{kind}|{cell}"
                cells.setdefault(key, []).append(tag)
                if (rtype, duration, kind, cell) == DECLARED:
                    fc = funnel_counts(tag)
                    fc["session"] = s["session"]
                    per_session_declared.append(fc)
                if progress:
                    print(f"  {s['session']} {rtype:<32}{kind:<15}{cell:<14}"
                          f"ev={len(tag):>6,} anchored={int(tag.anchored.sum()):>5,}",
                          flush=True)
            fp = first_print_census(conn, s["open_ms"], s["close_ms"], bars, ledger)
            fp["session"] = s["session"]
            first_print.append(fp)
            ob = orderbook_census(conn, s["open_ms"], s["close_ms"], traded)
            ob["session"] = s["session"]
            orderbook.append(ob)
        pairs = declared_pairs(res, ledger)
    finally:
        conn.close()
    funnel = []
    for key, parts in sorted(cells.items()):
        rtype, kind, cell = key.split("|")
        tag = pd.concat(parts, ignore_index=True)
        rec = {"ranking_type": rtype, "kind": kind, "cell": cell,
               "declared": (rtype, kind, cell) == (DECLARED[0], DECLARED[2], DECLARED[3]),
               **funnel_counts(tag)}
        funnel.append(rec)
    return {"db": str(db), "era": era, "since_ms": res["since_ms"],
            "since_utc": res["since_utc"], "until_ms": res["until_ms"],
            "until_utc": res["until_utc"], "db_max_snap_utc": res["db_max_snap_utc"],
            "sessions": res["sessions"], "sessions_used": used,
            "funnel": funnel, "declared_per_session": per_session_declared,
            "first_print": first_print, "orderbook": orderbook,
            "pairs": pairs, "n_sessions": len(used)}


def build_report(res: dict) -> dict:
    """산출물 하나. **문서에 실리는 수치는 전부 여기를 지나간다.** 전방 수익 CI 는 없다."""
    pairs = res["pairs"]
    fp = res["first_print"]
    ob = res["orderbook"]

    def _sum(items, k):
        return int(sum(int(d.get(k, 0)) for d in items))

    return {
        "labels": list(RFP.LABELS),
        "conditions": HE.MEASUREMENT_CONDITIONS,
        "db": res["db"],
        "window": {"since_utc": res["since_utc"], "until_utc": res["until_utc"],
                   "db_max_snap_utc": res["db_max_snap_utc"],
                   "d21_boundary_utc": HE.D21_BOUNDARY_UTC},
        "arm": {"name": "exploration", "exploration_era": res["era"],
                "sessions_used": res["sessions_used"],
                "sessions_allowed": list(RFP.EXPLORATION_SESSIONS if res["era"] == "A"
                                         else RFP.EXPLORATION_B_SESSIONS),
                "confirmation_floor_utc": RFP.CONFIRMATION_FLOOR_UTC,
                "exploration_b_ceiling_utc": RFP.EXPLORATION_B_CEILING_UTC,
                "discarded_sessions": list(RFP.DISCARDED_SESSIONS),
                "one_era_per_run": ("this runner has no argument that opens two eras; "
                                    "G2G3-PREREG s2-1 revision 4 forbids pooling them")},
        "holdout": {"window": [SS.HOLDOUT_START, SS.HOLDOUT_END]},
        "design": {
            "declared_cell": list(DECLARED),
            "declared_by": ("G2G3-PREREG s2-1 revision 4, commit b06b0af, written before "
                            "any era-B data was read"),
            "candidate_cells_counted_only": [list(c) for c in CANDIDATE_CELLS],
            "no_forward_returns_here": ("this module computes NO forward return for any "
                                        "cell; the declared cell's ladder lives in "
                                        "ranking_forward_path and is only decomposed here"),
            "pre_match_rule": (f"pre-state observable <=> at least {PRE_MATCH_MIN_BARS} "
                               f"second-bars in the {PRE_MATCH_S}s before t0 (rv60 needs "
                               "them, docs/64 s8-1)"),
            "seat_groups": {"score": list(SCORE_REASONS), "lane": list(LANE_REASONS),
                            "none": "no tier3 seat at t0 (trades_snap only fills in tier3, "
                                    "docs/61 s1-2)"},
            "diag_arm": DIAG_ARM,
            "no_ci_reason": NO_CI,
            "costs": "NOT subtracted - that is G-3 and needs a preregistration",
        },
        "funnel": res["funnel"],
        "declared_per_session": res["declared_per_session"],
        "declared_pairs": {
            "by_seat": pairs_by_seat(pairs),
            "by_session": pairs_by_session(pairs),
            "sessions_needed": sessions_needed(pairs, res["n_sessions"]),
        },
        "first_print": {"per_session": fp,
                        "total": {k: _sum(fp, k) for k in
                                  ("n_events", "n_tape_within_300s",
                                   "n_tier3_within_300s", "n_pre_match60")}},
        "orderbook": {"per_session": ob,
                      "total": {k: _sum(ob, k) for k in
                                ("n_symbols_any", "n_symbols_dense", "n_symbols_tape",
                                 "n_dense_and_tape", "n_dense_not_tape",
                                 "n_tape_not_dense")}},
    }


# --------------------------------------------------------------------------- #
# 콘솔 - ASCII 만
# --------------------------------------------------------------------------- #
def _p(v):
    return "-" if v is None else f"{100.0 * float(v):5.1f}%"


def _f(v, nd=4):
    return "-" if v is None or not np.isfinite(float(v)) else f"{float(v):+.{nd}f}"


def print_report(rep: dict) -> None:
    a = rep["arm"]
    print("=" * 78)
    print("E2 DESIGN FUNNEL - counts and seat provenance   (docs/68)")
    print("=" * 78)
    for i, s in enumerate(rep["labels"], 1):
        for j, line in enumerate(RFP._wrap(s, 70)):
            print(f"  [{i}] {line}" if j == 0 else f"      {line}")
    print(f"  arm     : exploration era {a['exploration_era']}  sessions "
          f"{len(a['sessions_used'])} {','.join(a['sessions_used'])}")
    print(f"  window  : {rep['window']['since_utc']} .. {rep['window']['until_utc']}"
          f"  (confirmation floor {a['confirmation_floor_utc']} - not read)")
    print(f"  declared: {rep['design']['declared_cell']}  ({rep['design']['declared_by']})")
    print(f"  rule    : {rep['design']['pre_match_rule']}")
    print("  NO forward return is computed in this module.  The declared cell's ladder")
    print("  is ranking_forward_path's; here its pairs are only split, without a CI.")

    print("\n[1] funnel per candidate cell - events -> tape -> pre-state observable ->")
    print("    anchored -> anchored AND pre-state observable.  'seat' = how the symbol")
    print("    held a tier3 seat at t0: score path / D-21 lane / none.")
    print(f"{'ranking_type':<32}{'cell':<15}{'events':>8}{'tape':>7}{'pre300':>8}"
          f"{'pre60':>7}{'anch':>7}{'anch%':>7}{'a&pre':>7}{'a&pre%':>8}"
          f"{'seat:score':>11}{'lane':>6}{'none':>6}{'lane<300':>9}")
    for r in rep["funnel"]:
        if r["n_events"] == 0:
            continue
        st = r["anchored_by_seat_at_t0"]
        mark = "*" if r["declared"] else " "
        print(f"{mark}{r['ranking_type']:<31}{r['kind'][:2] + ' ' + r['cell']:<15}"
              f"{r['n_events']:>8,}{r['n_has_tape']:>7,}{r['n_pre_tape300']:>8,}"
              f"{r['n_pre_match60']:>7,}{r['n_anchored']:>7,}"
              f"{_p(r['share_anchored']):>7}{r['n_anchored_and_pre_match60']:>7,}"
              f"{_p(r['share_anchored_and_pre_match60']):>8}"
              f"{st['score']:>11,}{st['lane']:>6,}{st['none']:>6,}"
              f"{r['no_seat_then_lane_within_300s_anchored']:>9,}")
    print("    * = the cell revision 4 declared.  lane<300 = no seat at t0 but the D-21")
    print("    lane seated the symbol within 300s (anchored count).  a&pre = anchored")
    print("    AND >=3 bars in the prior 60s = the events the matched ladder can use.")

    print("\n[2] declared cell, per session")
    print(f"{'session':<12}{'events':>8}{'tape':>7}{'pre60':>7}{'anch':>7}{'a&pre':>7}"
          f"{'a&pre:score':>12}{'lane':>6}{'none':>6}{'u5':>6}{'u5anch':>8}")
    for r in rep["declared_per_session"]:
        st = r["anchored_and_pre_match60_by_seat_at_t0"]
        print(f"{r['session']:<12}{r['n_events']:>8,}{r['n_has_tape']:>7,}"
              f"{r['n_pre_match60']:>7,}{r['n_anchored']:>7,}"
              f"{r['n_anchored_and_pre_match60']:>7,}{st['score']:>12,}{st['lane']:>6,}"
              f"{st['none']:>6,}{r['n_u5']:>6,}{r['n_u5_anchored']:>8,}")

    print("\n[3] declared cell - the ladder's own pairs (placebo_vol_density_matched),")
    print("    split by seat provenance at the anchor.  DIAGNOSTIC - no CI.")
    bs = rep["declared_pairs"]["by_seat"]
    print(f"{'seat':<8}{'n':>6}{'n_sym':>7}{'real':>10}{'placebo':>10}{'diff':>10}"
          f"{'p50diff':>10}{'d>0':>7}{'nbar60':>8}{'seat_age':>10}")
    for g in SEAT_GROUPS + ("all",):
        s = bs.get("groups", {}).get(g) if g != "all" else bs.get("all")
        if not s or not s.get("n"):
            print(f"{g:<8}{0:>6}")
            continue
        age = s.get("seat_age_s_p50")
        print(f"{g:<8}{s['n']:>6,}{s['n_symbols']:>7,}{_f(s['mean_real']):>10}"
              f"{_f(s['mean_placebo']):>10}{_f(s['mean_diff']):>10}{_f(s['p50_diff']):>10}"
              f"{_p(s['share_diff_positive']):>7}{s['nbar60_p50']:>8.1f}"
              f"{('-' if age is None else f'{age:.0f}s'):>10}")
    print(f"    {bs.get('no_ci_reason', '')}")

    print("\n[4] declared cell - the same pairs by session")
    print(f"{'session':<12}{'n':>6}{'real':>10}{'placebo':>10}{'diff':>10}{'d>0':>7}"
          f"{'score':>7}{'lane':>6}{'none':>6}")
    for s in rep["declared_pairs"]["by_session"]:
        b = s["by_seat_n"]
        print(f"{s['session']:<12}{s['n']:>6,}{_f(s['mean_real']):>10}"
              f"{_f(s['mean_placebo']):>10}{_f(s['mean_diff']):>10}"
              f"{_p(s['share_diff_positive']):>7}{b['score']:>7}{b['lane']:>6}{b['none']:>6}")
    sn = rep["declared_pairs"]["sessions_needed"]
    if sn.get("targets"):
        print(f"    pairs {sn['n_pairs']} over {sn['n_sessions']} sessions = "
              f"{sn['pairs_per_session']:.1f}/session, pair sd {sn['pair_sd']:.4f}")
        for t in sn["targets"]:
            print(f"    half-width {100 * t['half_width']:.2f}pp -> pairs "
                  f"{t['pairs_needed_iid']:.0f} -> sessions {t['sessions_needed_iid']:.1f}"
                  f"  (iid lower bound)")

    print("\n[5] non-ranking candidates - measurability only")
    fp = rep["first_print"]["total"]
    print(f"    first_print promotions in regular sessions: {fp['n_events']:,}  "
          f"tape within 300s: {fp['n_tape_within_300s']:,}  tier3 within 300s: "
          f"{fp['n_tier3_within_300s']:,}  pre-state observable: {fp['n_pre_match60']:,}")
    ob = rep["orderbook"]["total"]
    print(f"    orderbook: symbols with any snap {ob['n_symbols_any']:,}, dense "
          f"(>= {ORDERBOOK_DENSE_MIN}/session) {ob['n_symbols_dense']:,}, tape symbols "
          f"{ob['n_symbols_tape']:,}, dense&tape {ob['n_dense_and_tape']:,}, "
          f"dense-not-tape {ob['n_dense_not_tape']:,}, tape-not-dense "
          f"{ob['n_tape_not_dense']:,}  (session sums)")
    for r in rep["funnel"]:
        if r.get("toss_only"):
            t = r["toss_only"]
            print(f"    toss-only {r['ranking_type']} {r['kind']} {r['cell']}: market snap "
                  f"present {t['n_market_snap_present']:,}, toss-only "
                  f"{t['n_toss_only']:,} (anchored {t['n_toss_only_anchored']:,}, "
                  f"a&pre {t['n_toss_only_anchored_and_pre_match60']:,}), in both "
                  f"{t['n_in_both_lists']:,}")
    print("\n" + "=" * 78)
    print("This runner decides nothing.  It counts, and it splits pairs the ladder")
    print("already drew.  No forward-return CI was produced here.")
    print("=" * 78)


def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return None if not np.isfinite(obj) else float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    return obj


def main(argv: list) -> int:
    db = HE.DB
    era = "A"
    out_dir = OUT_DIR
    name = None
    args = list(argv[1:])
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--era":
            era = args[i + 1]; i += 2
        elif a == "--out":
            out_dir = Path(args[i + 1]); i += 2
        elif a == "--name":
            name = args[i + 1]; i += 2
        else:
            db = Path(a); i += 1
    name = name or f"e2_design_funnel_era_{era.lower()}"
    print(f"e2 design funnel, era {era} - counting ...", flush=True)
    res = run(db, era=era, progress=True)
    rep = _jsonable(build_report(res))
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{name}.json"
    p.write_text(json.dumps(rep, indent=1), encoding="utf-8")
    print_report(rep)
    print(f"-> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

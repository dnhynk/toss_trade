"""**자를 바꾼다** — 사전 상태를 `candles_1m` 에서 뽑는다 (D-32 = (가), `docs/69`).

## 이 모듈의 지위 — 탐색이다. 판정이 아니다. 그리고 **순서가 전부다**

`G2G3-PREREG` §3-1 개정 3 의 정합 키(`rv60`·`nbar60`·`nbar300`)는 앵커 **이전 테이프**에서
나온다. 테이프는 tier3 좌석에서만 채워지고(`docs/61` §1-2) D-21 차선은 좌석을 사건 **뒤에**
준다 — 그래서 첫 진입은 앵커는 붙는데 사전 상태가 없다(`docs/68` §5). 봉은 다르다: tier2
루프가 폴마다 **최신 200 봉**을 받고(`collector/loops.py` `tier2_symbol_once`,
`CANDLE_PAGE=200`) 승격 직후 `_ensure_history` -> `_backfill_1m` 이 최대 600 봉을 소급해
채운다. 좌석이 사건 뒤에 와도 사건 **앞** 분의 봉이 남는다. **테이프는 소급이 불가능하고
봉은 가능하다** — 그것이 (가) 가 작동하는 기전이고, 이 모듈이 그것을 **센다**.

명세(`specs/w3_candle_ruler.md` §1)가 못 박은 순서:

    (1) 표본을 센다      - `count()`. 사다리도 수익도 CI 도 없다
    (2) 개정 5 를 쓴다   - 사람이 한다 (`G2G3-PREREG` §3-1)
    (3) 사다리를 붙인다  - 개정 5 커밋 뒤에만, 선언한 칸 하나에만

이 파일의 `count()` 는 (1) 이다. **전방 수익을 한 줄도 계산하지 않는다** — 테이프는
"있는가"(앵커·위약 후보의 존재)로만 쓴다.

## 봉 라벨 — 한 칸 밀리면 룩어헤드다

`candles_1m.ts_ms = T` 인 봉은 `[T-60s, T)` 를 담고 시각 `T` 에 **이미 완결**돼 있다
(`session.bar_start_ms`, `docs/36` §1 3 중 검증, `docs/47`). 기준 시각 `tau` 의 사전
상태는 라벨 `T <= tau` 인 봉만 쓴다 — `T > tau` 인 첫 봉은 `tau` 를 **담고 있어서** 그 안에
`tau` 이후 체결이 섞여 있다. 창은 라벨 `(tau - 300s, tau]`, 곧 **`floor_min(tau)` 에서
끝나는 완결 5 분**이다. 이 경계는 `tests/test_candle_ruler.py` 가 절대 시각으로 고정하고
돌연변이(한 칸 밀기)로 red 가 되는지 확인한다(`docs/69` §3).

## 새 키 — `rv60`·`nbar60` 과 같은 축이 **아니다** (정의식)

    B(tau)      = { 봉 b : tau - 300,000 < T_b <= tau }            (T_b = 라벨 ms)
    cnbar5(tau) = |B(tau)|                                       (0..5, 감시 항목)
    cvol5(tau)  = sum_{b in B} vol_qu_b                          (체결 없는 분 = 0)
    crv5(tau)   = sqrt( sum_{b in B} ln(high_b / low_b)^2 )      (체결 없는 분 = 0)

- **체결 없는 분은 봉 행 자체가 없다**(`docs/00` §2-3 함정 5). **0 으로 리샘플한다.**
  좌석 뒤 폴이 200 봉을 소급하므로 사건 뒤 봉이 하나라도 있으면 사건 앞 분의 부재는
  수집 구멍이 아니라 체결 부재다(`docs/69` §2-2) — 결측으로 빼면 활발한 분만 남아
  위약이 활발한 쪽으로 쏠린다.
- 키가 **정의되는** 조건은 옛 자와 같다: `crv5 > 0` 이고 `cvol5 > 0` (`draw_banded` 의
  `key_missing` 규칙). 한 체결짜리 봉은 `high == low` 라 `crv5` 에 0 을 보탠다.
- 1 분봉은 **원주가**다(계약 A5). 봉끼리만 비교하고 일봉과 섞지 않는다.
- 옛 자의 `rv60` 은 초 막대 로그수익률의 표준편차, `nbar60` 은 60 초 안 체결 초의 수다.
  새 키는 5 분 범위 변동성과 5 분 거래량이다 — **같은 축이 아니라 다른 자**다.

## 세는 것

선언한 칸(`docs/68` 개정 4, `TOSS E1_new_entry N10`)의 앵커 붙은 사건마다:

1. `t0` 직전 완결 분(라벨 `floor_min(t0)`)의 봉이 **실제로 있는가**, 없으면 왜 없나
   (`COVERAGE`): 그 세션에 봉이 아예 없다 / `t0` 뒤 봉이 없다(폴이 안 왔다) /
   `t0` 뒤 봉은 있는데 그 분만 없다(체결 없는 분).
2. 있다면 **어디서 왔나**: `t0` 시점 티어(원장). tier <= 1 이면 봉 폴링이 없었으므로
   `t0` 앞 봉은 **소급**으로만 올 수 있다.
3. **위약 쪽**: 같은 종목·같은 정규장의 초 막대(테이프 순간) 중 자기 측정 구간
   밖(`CANDLE_SELF_GAP_S`)이고 봉 키가 정의되며 밴드 안인 후보 수. 위약의 전방 경로는
   여전히 테이프라 후보는 **좌석 시간 안**에만 있다 — 그 풀이 얼마나 좁은지를 센다.

층은 넷을 나란히 낸다: 전부 / `docs/68` 표 [1] 의 **142**(`t0` 에 좌석 없음 -> 차선이
300 초 안에 앉힘) / **정규장 첫 진입**(랭킹만으로 정의: 그 정규장에서 처음 상위 10 에
든 스냅) / 옛 자로 잴 수 있던 250. 첫 진입 층은 좌석 상태가 아니라 랭킹 이력으로 정의해
수집기 내부 상태에 안 기댄다(`docs/68` 이 좌석 가름을 진단으로만 둔 이유).

## 하지 않는 것

- **전방 수익·사다리·CI 를 만들지 않는다.** 그것은 (3) 이고 개정 5 커밋 뒤다.
- **판정하지 않는다.** "(가) 가 작동하는가" 의 판정은 문서(`docs/69`)가 표를 보고 쓴다.
- **새 사건 정의를 만들지 않는다.** 첫 진입 층은 격자 사건을 랭킹 이력으로 자른 것이다.
- **홀드아웃(2026-05-01~07-29)·확증 팔(08-26 이후)을 읽지 않는다.** 창은
  `ranking_forward_path.arm_window` 가 정한다 — 이 모듈에 창 규칙이 따로 없다.
- **라이브 워크트리에 쓰지 않는다.** DB 는 `mode=ro`, API 호출 0 건.

실행: `python -m tossmon.analysis.measure.candle_ruler [db] [--era A|B] [--out DIR]
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
from tossmon.analysis.measure import e2_design_funnel as EF
from tossmon.analysis.measure import ranking_forward_path as RFP
from tossmon.analysis.measure.density_matched_placebo import DENSITY_TOL
from tossmon.analysis.measure.vol_matched_placebo import VOL_MATCH_TOL

OUT_DIR = RFP.OUT_DIR
SEC_MS = RFP.SEC_MS
MIN_MS = SS.MIN_MS

#: 사전 창 = **완결 5 분**. 결과 창(300 초)과 같은 길이 — `docs/65` 가 `nbar300` 을 고른
#: 근거(창 길이 불일치)를 그대로 승계한다. 1 분봉으로 60 초 창은 봉 하나라 자가 못 된다.
CANDLE_LOOKBACK_MIN = 5
CANDLE_LOOKBACK_MS = CANDLE_LOOKBACK_MIN * MIN_MS

KEY_RV = "crv5"
KEY_VOL = "cvol5"
KEY_NBAR = "cnbar5"
CANDLE_KEYS = (KEY_RV, KEY_VOL, KEY_NBAR)

#: 밴드. 변동성은 옛 자와 **같은 ±20%**. 거래량은 양의 꼬리가 긴 연속량이라 **곱셈(로그 대칭)
#: 밴드** `v/f <= x <= f*v` 로 쓰고, `f = 1 + 0.2` 가 ±20% 의 로그 대칭판이다(정수 `nbar60` 의
#: ceil/floor 밴드를 그대로 옮길 수 없다). 이 값이 헤드라인이고, 폭에 따라 풀이 어떻게 변하는지는
#: `VOL_FACTOR_SWEEP` 이 **세기만** 한다 - 사다리는 개정 5 가 고른 폭 하나에만 붙는다.
CANDLE_RV_TOL = VOL_MATCH_TOL
CANDLE_VOL_FACTOR = 1.0 + DENSITY_TOL
#: 풀 크기 민감도(세기만). `None` = 거래량 밴드 없음(변동성만).
VOL_FACTOR_SWEEP = (1.2, 1.5, 2.0, 3.0, None)
RV_TOL_SWEEP = (0.2, 0.4)

#: 위약이 사건의 측정 구간과 겹치지 않는 간격 = 전방 지평 300 + 사전 창 300 = **600 초**.
#: 옛 자는 사전 창이 60 초라 360 초였다(`RFP.SELF_GAP_S`). 창이 길어진 만큼 간격도 길어지고
#: 그 대가는 풀 크기로 나타나므로 옛 간격의 풀도 같이 센다(진단 열).
CANDLE_SELF_GAP_S = RFP.ANCHOR_MAX_WAIT_S + CANDLE_LOOKBACK_MIN * 60
OLD_SELF_GAP_S = RFP.SELF_GAP_S

#: 세는 칸 — 개정 4 가 선언한 그 칸. 층은 이 안에서 자른다.
DECLARED = EF.DECLARED
TOP_N = 10

#: 옛 자의 "사전 상태 관측 가능" 조건. `docs/68` 표 [1] 의 `a&pre` 와 같은 정의.
PRE_MATCH_S = EF.PRE_MATCH_S
PRE_MATCH_MIN_BARS = EF.PRE_MATCH_MIN_BARS
FWD_S = EF.FWD_S

#: 좌석 나이가 이 안이면 사건 스냅 **그 자체**에서 앉힌 것으로 본다(폴 격자 12 초의 아래).
SEAT_AGE_SAME_SNAP_S = 1.0

#: `t0` 직전 완결 분의 봉이 없는 이유. 순서대로 판정한다.
COVERAGE = ("present", "covered_no_trade_last_min", "no_bar_after_t0", "no_candle_rows")

#: 층. 전부 앵커 붙은 사건 위에서 자른다.
STRATA = ("anchored", "no_seat_then_lane", "no_seat_then_lane_and_first", "lane_same_snap",
          "first_in_regular", "first_in_regular_old_unobservable", "old_observable",
          "old_unobservable")

#: `docs/68` §6 (2) 가 (가) 에 미리 걸어 둔 눈금. **결과를 보기 전에 적힌 값이다.**
BAR_PAIRS_PER_SESSION = 20.0
BAR_POOL_MEDIAN = 30

#: 필요 세션 수를 셀 때 쓰는 목표 반폭 — 여기서는 안 쓴다(수익이 없으니 sd 도 없다).
NO_RETURNS = ("this module computes NO forward return, NO ladder and NO CI - it counts "
              "whether the candle pre-state exists and how large the placebo pool is; "
              "the ladder is step (3) and comes only after revision 5 is committed")

FORBIDDEN_PHRASES = RFP.FORBIDDEN_PHRASES
#: 산출물 JSON 에 있어서는 안 되는 키 조각. 테스트가 전체를 훑는다.
FORBIDDEN_KEYS = ("max_ret", "end_ret", "ci95", "bonferroni", "mfe", "mae")


# --------------------------------------------------------------------------- #
# 적재
# --------------------------------------------------------------------------- #
def load_candles(conn: sqlite3.Connection, open_ms: int, close_ms: int) -> dict:
    """정규장 **내용**의 1 분봉을 종목별 배열로. 라벨 `T` 의 내용은 `[T-60s, T)` 이므로
    정규장 `[open, close)` 안에 온전히 드는 봉은 **`open < T <= close`** 다.

    프리마켓 마지막 분(라벨 = `open`)은 뺀다 — 옛 자도 초 막대를 정규장으로 잘랐다
    (`session_second_bars`). 그래서 개장 직후 사건은 사전 창이 짧다: 그것은 `cnbar5` 로
    드러나고 0 으로 리샘플된다.
    """
    df = pd.read_sql_query(
        "SELECT symbol, ts_ms, high_u, low_u, vol_qu FROM candles_1m "
        "WHERE ts_ms > ? AND ts_ms <= ? ORDER BY symbol, ts_ms",
        conn, params=(int(open_ms), int(close_ms)))
    out = {}
    if df.empty:
        return out
    for sym, sub in df.groupby("symbol", sort=True):
        out[str(sym)] = candle_arrays(sub.ts_ms.to_numpy(dtype="int64"),
                                      sub.high_u.to_numpy(dtype="float64"),
                                      sub.low_u.to_numpy(dtype="float64"),
                                      sub.vol_qu.to_numpy(dtype="float64"))
    return out


def candle_arrays(ts: np.ndarray, high: np.ndarray, low: np.ndarray,
                  vol: np.ndarray) -> dict:
    """한 종목의 봉을 **누적합**으로. 창 합은 `searchsorted` 두 번으로 나온다.

    `r2` 는 `ln(high/low)^2`. 값이 없거나 뒤집힌 봉(`high < low`, `<= 0`)은 0 을 보탠다.
    """
    ts = np.asarray(ts, dtype="int64")
    order = np.argsort(ts, kind="stable")
    ts = ts[order]
    high = np.asarray(high, dtype="float64")[order]
    low = np.asarray(low, dtype="float64")[order]
    vol = np.asarray(vol, dtype="float64")[order]
    ok = np.isfinite(high) & np.isfinite(low) & (low > 0) & (high >= low)
    r2 = np.zeros(ts.size, dtype="float64")
    r2[ok] = np.log(high[ok] / low[ok]) ** 2
    v = np.where(np.isfinite(vol) & (vol > 0), vol, 0.0)
    return {"ts": ts,
            "cum_vol": np.concatenate(([0.0], np.cumsum(v))),
            "cum_r2": np.concatenate(([0.0], np.cumsum(r2)))}


def membership_times(snaps: np.ndarray, sets: list) -> dict:
    """종목별 **상위 N 에 든 스냅 시각**(정렬). 첫 진입 층을 자르는 랭킹 이력이다."""
    acc: dict = {}
    for t, members in zip(snaps, sets):
        for sym in members:
            acc.setdefault(str(sym), []).append(int(t))
    return {s: np.asarray(sorted(v), dtype="int64") for s, v in acc.items()}


# --------------------------------------------------------------------------- #
# 봉 키 — 경계가 이 모듈의 전부다
# --------------------------------------------------------------------------- #
def candle_keys_at(cand: dict, tau_ms) -> dict:
    """기준 시각 `tau` 마다 `B(tau) = { T : tau - 300s < T <= tau }` 의 키.

    - `hi` = 라벨 `T <= tau` 인 봉 수 (`side='right'`). **`T > tau` 인 봉은 `tau` 를
      담고 있어서 룩어헤드다** — `side='left'` 로 바꾸거나 `tau` 에 60 초를 더하면 그
      봉이 들어온다. 그 두 돌연변이가 테스트를 red 로 만든다.
    - `lo` = 라벨 `T <= tau - 300s` 인 봉 수 (`side='right'`). 라벨이 정확히
      `tau - 300s` 인 봉의 내용은 `[tau-360s, tau-300s)` 라 창 밖이다.

    `tau` 가 분 경계에 정확히 놓이면(예: `13:35:00.000`) 라벨 `13:35:00` 봉은 내용이
    `[13:34, 13:35)` 라 **들어간다** — `<=` 가 옳고 `<` 는 한 봉을 버리는 과보수다.
    """
    tau = np.atleast_1d(np.asarray(tau_ms, dtype="int64"))
    ts = cand["ts"]
    hi = np.searchsorted(ts, tau, side="right")
    lo = np.searchsorted(ts, tau - CANDLE_LOOKBACK_MS, side="right")
    n = (hi - lo).astype("int64")
    vol = cand["cum_vol"][hi] - cand["cum_vol"][lo]
    r2 = np.maximum(cand["cum_r2"][hi] - cand["cum_r2"][lo], 0.0)
    return {KEY_NBAR: n, KEY_VOL: vol, KEY_RV: np.sqrt(r2)}


def key_defined(keys: dict) -> np.ndarray:
    """옛 자의 `key_missing` 규칙 그대로: 유한하고 0 보다 커야 한다."""
    v = np.asarray(keys[KEY_RV], dtype="float64")
    d = np.asarray(keys[KEY_VOL], dtype="float64")
    return np.isfinite(v) & (v > 0) & np.isfinite(d) & (d > 0)


def last_completed_label(t_ms) -> np.ndarray:
    """`t` 직전에 **완결된** 봉의 라벨 = `floor_min(t)`. 라벨 `floor_min(t)` 의 내용은
    `[floor_min(t) - 60s, floor_min(t))` 이고 `t` 이전에 끝나 있다."""
    t = np.atleast_1d(np.asarray(t_ms, dtype="int64"))
    return (t // MIN_MS) * MIN_MS


def has_bar_at(cand: dict, label_ms) -> np.ndarray:
    lab = np.atleast_1d(np.asarray(label_ms, dtype="int64"))
    ts = cand["ts"]
    return (np.searchsorted(ts, lab, side="right")
            - np.searchsorted(ts, lab, side="left")) > 0


def tier_at(ledger: dict, symbol: str, t_ms: int) -> int:
    """`t` 시점 티어 = 원장에서 `t` 직전 마지막 행의 `to_tier`. 행이 없으면 0.
    `seat_state_at` 과 같은 경계(`side='right'`: 그 시각의 전이는 이미 적용됐다)."""
    led = ledger.get(symbol)
    if led is None:
        return 0
    i = int(np.searchsorted(led["ts"], int(t_ms), side="right")) - 1
    return int(led["tier"][i]) if i >= 0 else 0


def in_band(x: np.ndarray, v: float, tol: float) -> np.ndarray:
    """`v` 의 ±`tol` 밴드(양 끝 포함) - 옛 자의 `rv60` 밴드와 같은 꼴."""
    x = np.asarray(x, dtype="float64")
    return (x >= v * (1.0 - tol)) & (x <= v * (1.0 + tol))


def in_factor_band(x: np.ndarray, v: float, factor) -> np.ndarray:
    """`v/f <= x <= f*v` (로그 대칭). `factor=None` 이면 밴드 없음(전부 참)."""
    x = np.asarray(x, dtype="float64")
    if factor is None:
        return np.ones(x.shape, dtype=bool)
    f = float(factor)
    return (x >= v / f) & (x <= v * f)


def sweep_label(tol: float, factor) -> str:
    return f"rv{tol:.1f}_vf{'none' if factor is None else f'{float(factor):.1f}'}"


def tier_floor_in(ledger: dict, symbol: str, lo_ms: int, hi_ms: int) -> int:
    """`[lo, hi)` 동안의 **최저** 티어 = `lo` 시점 상태와 그 구간 전이들의 최솟값.
    2 미만이면 그 구간 어느 때는 봉 폴링이 없었다 - 그때의 봉은 소급으로만 온다."""
    led = ledger.get(symbol)
    floor = tier_at(ledger, symbol, lo_ms)
    if led is None:
        return floor
    a = int(np.searchsorted(led["ts"], int(lo_ms), side="right"))
    b = int(np.searchsorted(led["ts"], int(hi_ms), side="left"))
    if b > a:
        floor = min(floor, int(led["tier"][a:b].min()))
    return floor


# --------------------------------------------------------------------------- #
# 사건마다 표식
# --------------------------------------------------------------------------- #
def tag_events(ev: pd.DataFrame, cand: dict, secs: dict, ledger: dict, member: dict, *,
               open_ms: int, close_ms: int) -> pd.DataFrame:
    """선언한 칸의 사건 표에 봉 사전 상태·커버리지·좌석·첫 진입·위약 풀을 붙인다.

    가격(`last_u`)은 층(`u5/o5`)에만 쓴다. **전방 수익은 계산하지 않는다.**
    """
    rows = []
    if ev.empty:
        return pd.DataFrame(rows)
    for sym, sub in ev.groupby("symbol", sort=True):
        sym = str(sym)
        t0 = sub.t0_ms.to_numpy(dtype="int64")
        tier = RFP.tier_codes(sub.last_u.to_numpy(dtype="float64"))
        c = cand.get(sym)
        st = secs.get(sym, np.zeros(0, dtype="int64"))
        mt = member.get(sym, np.zeros(0, dtype="int64"))
        if c is not None:
            k = candle_keys_at(c, t0)
            ok = key_defined(k)
            has_last = has_bar_at(c, last_completed_label(t0))
            n_after = (np.searchsorted(c["ts"], close_ms, side="right")
                       - np.searchsorted(c["ts"], t0, side="right"))
            n_rows = int(c["ts"].size)
        else:
            z = np.zeros(t0.size)
            k = {KEY_NBAR: z.astype("int64"), KEY_VOL: z, KEY_RV: z}
            ok = np.zeros(t0.size, dtype=bool)
            has_last = np.zeros(t0.size, dtype=bool)
            n_after = np.zeros(t0.size, dtype="int64")
            n_rows = 0
        # 위약 후보 = 이 종목의 정규장 초 막대. 봉 키는 초 막대 시각 기준으로 **한 번** 잰다.
        if c is not None and st.size:
            ks = candle_keys_at(c, st)
            ok_s = key_defined(ks)
        else:
            ks = None
            ok_s = np.zeros(st.size, dtype=bool)
        for i in range(t0.size):
            t = int(t0[i])
            anchored = EF.count_between(st, t, t + FWD_S * SEC_MS, lo_inclusive=False) > 0
            pre60 = EF.count_between(st, t - PRE_MATCH_S * SEC_MS, t,
                                     lo_inclusive=True) >= PRE_MATCH_MIN_BARS
            seat = EF.seat_state_at(ledger, sym, t)
            after = None if seat["in_tier3"] else EF.tier3_entry_after(
                ledger, sym, t, FWD_S * SEC_MS)
            seat_age = (None if seat["since_ms"] is None
                        else (t - int(seat["since_ms"])) / 1000.0)
            # 첫 진입: 정규장 개장 이후 `t0` 이전 스냅에 상위 N 이력이 없다.
            a = int(np.searchsorted(mt, open_ms, side="left"))
            b = int(np.searchsorted(mt, t, side="left"))
            first_reg = (b - a) == 0
            first_day = b == 0
            if c is None or n_rows == 0:
                cov = "no_candle_rows"
            elif bool(has_last[i]):
                cov = "present"
            elif int(n_after[i]) == 0:
                cov = "no_bar_after_t0"
            else:
                cov = "covered_no_trade_last_min"
            pool = {"pool_any": 0, "pool_any_old_gap": 0, "pool_key": 0,
                    "pool_rv": 0, "pool_rv_vol": 0, "cbars_outside_gap": 0}
            if c is not None:
                # 봉 자체가 세션 안에 몇 개 있나(자기 간격 밖) - 전방 경로까지 봉으로 재는
                # 자(이 과제 밖, docs/69 s9)가 가질 위약 풀의 상한. 세기만 한다.
                pool["cbars_outside_gap"] = int((np.abs(c["ts"] - t)
                                                 > CANDLE_SELF_GAP_S * SEC_MS).sum())
            for tol in RV_TOL_SWEEP:
                for fac in VOL_FACTOR_SWEEP:
                    pool["pool_" + sweep_label(tol, fac)] = 0
            if st.size:
                d = np.abs(st - t)
                gap = d > CANDLE_SELF_GAP_S * SEC_MS
                gap_old = d > OLD_SELF_GAP_S * SEC_MS
                pool["pool_any"] = int(gap.sum())
                pool["pool_any_old_gap"] = int(gap_old.sum())
                pool["pool_key"] = int((gap & ok_s).sum())
                if ks is not None and bool(ok[i]):
                    v, w = float(k[KEY_RV][i]), float(k[KEY_VOL][i])
                    base = gap & ok_s
                    rvb = in_band(ks[KEY_RV], v, CANDLE_RV_TOL)
                    vlb = in_factor_band(ks[KEY_VOL], w, CANDLE_VOL_FACTOR)
                    pool["pool_rv"] = int((base & rvb).sum())
                    pool["pool_rv_vol"] = int((base & rvb & vlb).sum())
                    for tol in RV_TOL_SWEEP:
                        rvt = in_band(ks[KEY_RV], v, tol)
                        for fac in VOL_FACTOR_SWEEP:
                            pool["pool_" + sweep_label(tol, fac)] = int(
                                (base & rvt & in_factor_band(ks[KEY_VOL], w, fac)).sum())
            rows.append({
                "symbol": sym, "t0_ms": t, "tier": tier[i],
                "anchored": bool(anchored), "pre_match60": bool(pre60),
                "seat_at_t0": seat["group"], "seat_reason": seat["reason"],
                "seat_age_s": seat_age,
                "seat_after": EF.seat_group(after) if after else "none",
                "tier_at_t0": tier_at(ledger, sym, t),
                "tier_floor_prior5": tier_floor_in(ledger, sym, t - CANDLE_LOOKBACK_MS, t),
                "first_in_regular": bool(first_reg), "first_in_utc_day": bool(first_day),
                "has_bar_last": bool(has_last[i]), "coverage": cov,
                "n_bars_after_t0": int(n_after[i]), "n_candle_rows_session": n_rows,
                KEY_NBAR: int(k[KEY_NBAR][i]), KEY_VOL: float(k[KEY_VOL][i]),
                KEY_RV: float(k[KEY_RV][i]), "key_ok": bool(ok[i]),
                **pool,
            })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# 층과 요약
# --------------------------------------------------------------------------- #
def strata_masks(tag: pd.DataFrame) -> dict:
    """층 = 앵커 붙은 사건 위의 마스크. 이름은 `STRATA` 순서다."""
    a = tag.anchored.to_numpy(dtype=bool)
    seat = tag.seat_at_t0.to_numpy(dtype=object)
    after = tag.seat_after.to_numpy(dtype=object)
    age = pd.to_numeric(tag.seat_age_s, errors="coerce").to_numpy(dtype="float64")
    first = tag.first_in_regular.to_numpy(dtype=bool)
    pre = tag.pre_match60.to_numpy(dtype=bool)
    lane_same = (seat == "lane") & (np.nan_to_num(age, nan=np.inf) <= SEAT_AGE_SAME_SNAP_S)
    return {
        "anchored": a,
        "no_seat_then_lane": a & (seat == "none") & (after == "lane"),
        "no_seat_then_lane_and_first": a & (seat == "none") & (after == "lane") & first,
        "lane_same_snap": a & lane_same,
        "first_in_regular": a & first,
        "first_in_regular_old_unobservable": a & first & ~pre,
        "old_observable": a & pre,
        "old_unobservable": a & ~pre,
    }


def _hist(values, bins) -> dict:
    v = np.asarray(values)
    return {str(b): int((v == b).sum()) for b in bins}


def _p50(values) -> float | None:
    v = np.asarray(values, dtype="float64")
    v = v[np.isfinite(v)]
    return float(np.median(v)) if v.size else None


def stratum_summary(tag: pd.DataFrame, n_sessions: int) -> dict:
    """한 층의 표. **비율의 분모는 그 층의 사건 수**다."""
    n = int(len(tag))
    if n == 0:
        return {"n": 0}
    has = tag.has_bar_last.to_numpy(dtype=bool)
    ok = tag.key_ok.to_numpy(dtype=bool)
    nb = tag[KEY_NBAR].to_numpy(dtype="int64")
    t0t = tag.tier_at_t0.to_numpy(dtype="int64")
    prv = tag.pool_rv_vol.to_numpy(dtype="int64")
    per = []
    for sess, sub in tag.groupby("session", sort=True):
        per.append({"session": sess, "n": int(len(sub)),
                    "has_bar_last": int(sub.has_bar_last.sum()),
                    "key_ok": int(sub.key_ok.sum()),
                    "paired_rv": int((sub.pool_rv > 0).sum()),
                    "paired_rv_vol": int((sub.pool_rv_vol > 0).sum()),
                    "pool_rv_vol_p50": _p50(sub.pool_rv_vol)})
    by_tier = {}
    for t in sorted(set(int(x) for x in t0t)):
        m = t0t == t
        by_tier[str(t)] = {"n": int(m.sum()), "has_bar_last": int((has & m).sum()),
                           "cnbar5_full": int((m & (nb == CANDLE_LOOKBACK_MIN)).sum()),
                           "key_ok": int((ok & m).sum())}
    tf = tag.tier_floor_prior5.to_numpy(dtype="int64")
    by_floor = {}
    for t in sorted(set(int(x) for x in tf)):
        m = tf == t
        by_floor[str(t)] = {"n": int(m.sum()), "has_bar_last": int((has & m).sum()),
                            "cnbar5_full": int((m & (nb == CANDLE_LOOKBACK_MIN)).sum()),
                            "key_ok": int((ok & m).sum())}
    sweep = {}
    for tol in RV_TOL_SWEEP:
        for fac in VOL_FACTOR_SWEEP:
            col = tag["pool_" + sweep_label(tol, fac)].to_numpy(dtype="int64")
            pr = int((col > 0).sum())
            sweep[sweep_label(tol, fac)] = {
                "rv_tol": tol, "vol_factor": fac, "paired": pr,
                "pairs_per_session": pr / float(n_sessions) if n_sessions else None,
                "pool_p50": _p50(col), "share_ge30": float((col >= BAR_POOL_MEDIAN).mean())}
    seat_first = {}
    for g in EF.SEAT_GROUPS:
        for h in EF.SEAT_GROUPS:
            m = (tag.seat_at_t0.to_numpy(dtype=object) == g) & (
                tag.seat_after.to_numpy(dtype=object) == h)
            if m.any():
                seat_first[f"{g}->{h}"] = {
                    "n": int(m.sum()),
                    "first_in_regular": int((m & tag.first_in_regular.to_numpy(bool)).sum())}
    age = pd.to_numeric(tag.seat_age_s, errors="coerce")
    paired = int((prv > 0).sum())
    return {
        "n": n, "n_symbols": int(tag.symbol.nunique()),
        "has_bar_last": int(has.sum()), "share_has_bar_last": float(has.mean()),
        "cnbar5_hist": _hist(nb, range(CANDLE_LOOKBACK_MIN + 1)),
        "cnbar5_full": int((nb == CANDLE_LOOKBACK_MIN).sum()),
        "key_ok": int(ok.sum()), "share_key_ok": float(ok.mean()),
        "coverage": {c: int((tag.coverage.to_numpy(dtype=object) == c).sum())
                     for c in COVERAGE},
        "by_tier_at_t0": by_tier,
        "by_tier_floor_prior5": by_floor,
        "seat_x_first": seat_first,
        "sweep": sweep,
        "old_ruler_observable": int(tag.pre_match60.sum()),
        "seat_at_t0": {g: int((tag.seat_at_t0 == g).sum()) for g in EF.SEAT_GROUPS},
        "seat_age_s_p50": _p50(age),
        "first_in_regular": int(tag.first_in_regular.sum()),
        "first_in_utc_day": int(tag.first_in_utc_day.sum()),
        "pool": {
            "any_p50": _p50(tag.pool_any), "any_old_gap_p50": _p50(tag.pool_any_old_gap),
            "key_p50": _p50(tag.pool_key), "rv_p50": _p50(tag.pool_rv),
            "rv_vol_p50": _p50(prv),
            "candle_bars_outside_gap_p50": _p50(tag.cbars_outside_gap),
            "rv_vol_p50_where_key_ok": _p50(prv[ok]) if ok.any() else None,
            "share_rv_vol_ge1": float((prv > 0).mean()),
            "share_rv_vol_ge30": float((prv >= BAR_POOL_MEDIAN).mean()),
            "paired_rv": int((tag.pool_rv > 0).sum()),
            "paired_rv_vol": paired,
            "unpaired_key_missing": int((~ok).sum()),
            "unpaired_no_tape_outside_gap": int((ok & (tag.pool_key == 0)).sum()),
            "unpaired_empty_band": int((ok & (tag.pool_key > 0) & (prv == 0)).sum()),
        },
        "pairs_per_session": paired / float(n_sessions) if n_sessions else None,
        "per_session": per,
        "bar_docs68_s6_2": {
            "pairs_per_session_min": BAR_PAIRS_PER_SESSION,
            "pool_median_min": BAR_POOL_MEDIAN,
            "pairs_per_session": paired / float(n_sessions) if n_sessions else None,
            "pool_median": _p50(prv),
            "met": bool(n_sessions and paired / float(n_sessions) >= BAR_PAIRS_PER_SESSION
                        and (_p50(prv) or 0.0) >= BAR_POOL_MEDIAN),
            "note": ("the bar docs/68 s6 (2) wrote down BEFORE any candle data was read: "
                     "pairs/session >= 20 and median placebo candidates >= 30; met/not met "
                     "is a count, not a verdict on returns"),
        },
    }


# --------------------------------------------------------------------------- #
# 러너 (1) — 세기만
# --------------------------------------------------------------------------- #
def count(db: Path, *, era: str = "B", progress: bool = False) -> dict:
    """한 시대의 선언한 칸을 센다. 창·세션은 `ranking_forward_path.arm_window` 가 정한다."""
    if era not in RFP.EXPLORATION_ERAS:
        raise ValueError(f"era must be one of {RFP.EXPLORATION_ERAS}, got {era!r}")
    conn = HE.open_ro(db)
    try:
        arm = RFP.arm_window(conn, exploration_only=True, exploration_era=era)
        sessions = arm["sessions"]
        allowed = (RFP.EXPLORATION_SESSIONS if era == "A" else RFP.EXPLORATION_B_SESSIONS)
        bad = sorted({s["session"] for s in sessions} - set(allowed))
        if bad:
            raise ValueError(f"sessions outside era {era} reached this run: {bad}")
        ledger = EF.load_tier_ledger(conn, arm["until_ms"] + 1)
        rtype, duration, kind, cell = DECLARED
        parts = []
        n_events_total = 0
        for s in sessions:
            ev = RFP.session_events(conn, rtype, duration, s["day0_ms"],
                                    arm["floor_ms"], arm["until_ms"])
            if not ev.empty:
                ev = ev[(ev.kind == kind) & (ev.cell == cell)]
            n_events_total += int(len(ev))
            if ev.empty:
                continue
            cand = load_candles(conn, s["open_ms"], s["close_ms"])
            secs = EF.load_second_ts(conn, s["open_ms"], s["close_ms"])
            snaps, sets = EF.load_list_members(conn, rtype, duration, s["day0_ms"],
                                               s["close_ms"], TOP_N)
            tag = tag_events(ev, cand, secs, ledger, membership_times(snaps, sets),
                             open_ms=s["open_ms"], close_ms=s["close_ms"])
            tag["session"] = s["session"]
            parts.append(tag)
            if progress:
                print(f"  {s['session']} {rtype:<32}{kind} {cell}  ev={len(tag):>5,}"
                      f"  anchored={int(tag.anchored.sum()):>4,}"
                      f"  bar_last={int((tag.anchored & tag.has_bar_last).sum()):>4,}"
                      f"  paired={int((tag.anchored & (tag.pool_rv_vol > 0)).sum()):>4,}",
                      flush=True)
        tag = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    finally:
        conn.close()
    used = [s["session"] for s in sessions]
    strata = {}
    if not tag.empty:
        masks = strata_masks(tag)
        for name in STRATA:
            strata[name] = stratum_summary(tag[masks[name]], len(used))
    return {"db": str(db), "era": era, "since_ms": arm["floor_ms"],
            "since_utc": HE.ms_iso(arm["floor_ms"]), "until_ms": arm["until_ms"],
            "until_utc": HE.ms_iso(arm["until_ms"]),
            "db_max_snap_utc": HE.ms_iso(arm["db_max_ms"]),
            "sessions": sessions, "sessions_used": used, "n_sessions": len(used),
            "n_events": n_events_total, "n_anchored": int(tag.anchored.sum()) if len(tag) else 0,
            "tags": tag, "strata": strata}


def build_report(res: dict) -> dict:
    """산출물 하나. **문서에 실리는 수치는 전부 여기를 지나간다.** 수익·CI 는 없다."""
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
                "window_rule": "ranking_forward_path.arm_window - this module has none"},
        "holdout": {"window": [SS.HOLDOUT_START, SS.HOLDOUT_END]},
        "design": {
            "step": "(1) count the sample - specs/w3_candle_ruler.md s1",
            "declared_cell": list(DECLARED),
            "bar_label": ("candles_1m.ts_ms = T covers [T-60s, T) and is complete at T; "
                          "the pre-state at tau uses labels T <= tau only; the window is "
                          "labels in (tau - 300s, tau] = the 5 completed minutes ending "
                          "at floor_min(tau)"),
            "keys": {
                KEY_NBAR: "number of bars in B(tau), 0..5 - watched, not matched",
                KEY_VOL: "sum of vol_qu over B(tau); a minute with no bar counts 0",
                KEY_RV: "sqrt(sum of ln(high/low)^2 over B(tau)); a minute with no bar "
                        "counts 0; a one-price bar counts 0",
                "defined": f"{KEY_RV} > 0 and {KEY_VOL} > 0 (same rule as the old key_missing)",
                "lookback_min": CANDLE_LOOKBACK_MIN,
                "raw_prices": "1m candles are RAW prices (contract A5); never mixed with 1d",
            },
            "missing_minutes": ("resampled to ZERO, not dropped: after the seat the poll "
                                "pulls the latest 200 bars, so if any bar exists after t0 "
                                "a missing minute before t0 is 'no trade', not 'not "
                                "collected' (coverage classes below make that visible)"),
            "bands": {KEY_RV: f"+-{CANDLE_RV_TOL:.0%} (same as rv60)",
                      KEY_VOL: f"v/{CANDLE_VOL_FACTOR} <= x <= {CANDLE_VOL_FACTOR}*v "
                               f"(log-symmetric +-{DENSITY_TOL:.0%})"},
            "sweep": {"rv_tol": list(RV_TOL_SWEEP),
                      "vol_factor": [f for f in VOL_FACTOR_SWEEP],
                      "what": ("pool size and pairs per band width - COUNTED ONLY; the "
                               "ladder attaches to the ONE width revision 5 fixes")},
            "self_gap_s": CANDLE_SELF_GAP_S, "old_self_gap_s": OLD_SELF_GAP_S,
            "placebo_axis": ("same symbol, same regular session, other moment WITH TAPE - "
                             "the forward path stays tape, so a placebo moment must sit "
                             "inside a tier3 seat; that is what shrinks the pool"),
            "coverage_classes": list(COVERAGE),
            "strata": list(STRATA),
            "first_in_regular": ("ranking-only: no top-10 membership at any regular-"
                                 "session snap before t0; does not use collector state"),
            "no_forward_returns_here": NO_RETURNS,
            "costs": "NOT subtracted - that is G-3 and needs a preregistration",
        },
        "funnel": {"n_events": res["n_events"], "n_anchored": res["n_anchored"],
                   "n_sessions": res["n_sessions"]},
        "strata": res["strata"],
    }


# --------------------------------------------------------------------------- #
# 콘솔 - ASCII 만
# --------------------------------------------------------------------------- #
def _p(v):
    return "-" if v is None else f"{100.0 * float(v):5.1f}%"


def _n(v):
    return "-" if v is None else f"{float(v):.1f}"


def print_report(rep: dict) -> None:
    a = rep["arm"]
    d = rep["design"]
    print("=" * 78)
    print("CANDLE RULER - step (1): count the sample   (docs/69, D-32 = (a))")
    print("=" * 78)
    for i, s in enumerate(rep["labels"], 1):
        for j, line in enumerate(RFP._wrap(s, 70)):
            print(f"  [{i}] {line}" if j == 0 else f"      {line}")
    print(f"  arm     : exploration era {a['exploration_era']}  sessions "
          f"{len(a['sessions_used'])} {','.join(a['sessions_used'])}")
    print(f"  window  : {rep['window']['since_utc']} .. {rep['window']['until_utc']}"
          f"  (confirmation floor {a['confirmation_floor_utc']} - not read)")
    print(f"  cell    : {d['declared_cell']}")
    for k in ("bar_label", "missing_minutes", "placebo_axis", "no_forward_returns_here"):
        for j, line in enumerate(RFP._wrap(d[k], 66)):
            print(f"  {k + ':':<24}{line}" if j == 0 else f"  {'':<24}{line}")
    print(f"  keys    : {KEY_RV} = {d['keys'][KEY_RV]}")
    print(f"            {KEY_VOL} = {d['keys'][KEY_VOL]}")
    print(f"            {KEY_NBAR} = {d['keys'][KEY_NBAR]}")
    print(f"            defined <=> {d['keys']['defined']}")
    print(f"            bands: {KEY_RV} {d['bands'][KEY_RV]}; {KEY_VOL} {d['bands'][KEY_VOL]}")
    print(f"            self-gap {d['self_gap_s']}s (old ruler {d['old_self_gap_s']}s)")
    f = rep["funnel"]
    print(f"  funnel  : events {f['n_events']:,} -> anchored {f['n_anchored']:,} "
          f"over {f['n_sessions']} sessions")

    st = rep["strata"]
    print("\n[1] does the candle pre-state EXIST at t0?  per stratum (anchored events only)")
    print("    bar_last = a bar labelled floor_min(t0) exists (the last completed minute)")
    print("    full5 = all 5 minutes have a bar; key_ok = crv5 > 0 and cvol5 > 0")
    print(f"{'stratum':<36}{'n':>6}{'n_sym':>6}{'bar_last':>9}{'%':>7}{'full5':>7}"
          f"{'key_ok':>8}{'%':>7}{'old_obs':>8}")
    for name in STRATA:
        s = st.get(name, {})
        if not s.get("n"):
            print(f"{name:<36}{0:>6}")
            continue
        print(f"{name:<36}{s['n']:>6,}{s['n_symbols']:>6,}{s['has_bar_last']:>9,}"
              f"{_p(s['share_has_bar_last']):>7}{s['cnbar5_full']:>7,}"
              f"{s['key_ok']:>8,}{_p(s['share_key_ok']):>7}{s['old_ruler_observable']:>8,}")

    print("\n[2] why is the last-minute bar missing?  coverage classes per stratum")
    print(f"{'stratum':<36}" + "".join(f"{c:>27}" for c in COVERAGE))
    for name in STRATA:
        s = st.get(name, {})
        if not s.get("n"):
            continue
        print(f"{name:<36}" + "".join(f"{s['coverage'][c]:>27,}" for c in COVERAGE))
    print("    cnbar5 histogram (bars present in the 5-minute window):")
    for name in STRATA:
        s = st.get(name, {})
        if not s.get("n"):
            continue
        h = s["cnbar5_hist"]
        print(f"    {name:<34}" + "  ".join(f"{k}:{h[k]:>4,}" for k in sorted(h)))

    print("\n[3] where did the bars come from?  lowest tier during [t0-300s, t0) x bar_last")
    print("    floor <= 1 = at some point in the window there was NO candle polling:")
    print("    bars from that window can only be RETROACTIVE (the 200-bar page pulled")
    print("    after the seat / the promotion backfill).  floor >= 2 = live polling was")
    print("    possible (it does not prove the bar came from it).")
    print(f"{'stratum':<36}{'floor':>6}{'n':>6}{'bar_last':>9}{'full5':>7}{'key_ok':>8}")
    for name in STRATA:
        s = st.get(name, {})
        if not s.get("n"):
            continue
        for t, b in s["by_tier_floor_prior5"].items():
            print(f"{name:<36}{t:>6}{b['n']:>6,}{b['has_bar_last']:>9,}"
                  f"{b['cnbar5_full']:>7,}{b['key_ok']:>8,}")
    print("    seat provenance (seat at t0 -> seat within 300s) x ranking-defined first entry:")
    s = st.get("anchored", {})
    for k, b in s.get("seat_x_first", {}).items():
        print(f"    {k:<14} n {b['n']:>5,}   of which first_in_regular {b['first_in_regular']:>4,}")

    print("\n[4] the placebo side - candidates per event (same symbol, same session,")
    print("    tape moment outside the self-gap, candle key defined, inside the band)")
    print("    any = tape moments outside the 600s gap (360s gap in the next column);")
    print("    key = of those, candle key defined; rv = inside the crv5 band;")
    print("    rv+vol = inside both bands = the headline band.  p50 over ALL events in")
    print("    the stratum (0 when the event itself has no key).")
    print(f"{'stratum':<36}{'n':>6}{'any':>7}{'any360':>7}{'key':>7}{'rv':>7}{'rv+vol':>8}"
          f"{'>=1':>7}{'>=30':>7}{'paired':>8}{'/sess':>7}{'cbars':>7}")
    for name in STRATA:
        s = st.get(name, {})
        if not s.get("n"):
            continue
        p = s["pool"]
        print(f"{name:<36}{s['n']:>6,}{_n(p['any_p50']):>7}{_n(p['any_old_gap_p50']):>7}"
              f"{_n(p['key_p50']):>7}{_n(p['rv_p50']):>7}{_n(p['rv_vol_p50']):>8}"
              f"{_p(p['share_rv_vol_ge1']):>7}{_p(p['share_rv_vol_ge30']):>7}"
              f"{p['paired_rv_vol']:>8,}{_n(s['pairs_per_session']):>7}"
              f"{_n(p['candle_bars_outside_gap_p50']):>7}")
    print("    cbars = candle BARS of the symbol in the session outside the gap (p50):")
    print("    the pool ceiling an all-candle ruler would have - counted, not used here")
    print("    unpaired census (headline band): key_missing / no tape outside the gap /")
    print("    empty band - nothing is dropped silently")
    for name in STRATA:
        s = st.get(name, {})
        if not s.get("n"):
            continue
        p = s["pool"]
        print(f"    {name:<34}{p['unpaired_key_missing']:>6,}{p['unpaired_no_tape_outside_gap']:>8,}"
              f"{p['unpaired_empty_band']:>8,}   paired {p['paired_rv_vol']:,} of {s['n']:,}")

    print("\n[4b] band width sweep - COUNTED ONLY (pairs and pool p50 per width).")
    print("     rv tol = +-tol on crv5; vf = factor band on cvol5 (none = no volume band).")
    print("     The ladder will use ONE width, fixed in revision 5 before any return is read.")
    labels = [sweep_label(tol, fac) for tol in RV_TOL_SWEEP for fac in VOL_FACTOR_SWEEP]
    print(f"{'stratum':<36}{'n':>6}" + "".join(f"{lab:>14}" for lab in labels))
    for name in STRATA:
        s = st.get(name, {})
        if not s.get("n"):
            continue
        row = "".join(f"{s['sweep'][lab]['paired']:>7,}/{_n(s['sweep'][lab]['pool_p50']):>6}"
                      for lab in labels)
        print(f"{name:<36}{s['n']:>6,}" + row)
    print("     cell = paired / pool p50")

    print("\n[5] per session - the strata the new ruler is FOR")
    for name in ("no_seat_then_lane", "no_seat_then_lane_and_first", "first_in_regular",
                 "first_in_regular_old_unobservable", "anchored"):
        s = st.get(name, {})
        if not s.get("n"):
            continue
        print(f"-- {name}")
        print(f"   {'session':<12}{'n':>5}{'bar_last':>9}{'key_ok':>7}{'paired':>7}"
              f"{'pool_p50':>9}")
        for r in s["per_session"]:
            print(f"   {r['session']:<12}{r['n']:>5}{r['has_bar_last']:>9}{r['key_ok']:>7}"
                  f"{r['paired_rv_vol']:>7}{_n(r['pool_rv_vol_p50']):>9}")

    print("\n[6] the bar docs/68 s6 (2) set BEFORE any candle data was read:")
    print(f"    pairs/session >= {BAR_PAIRS_PER_SESSION:.0f} and median placebo candidates "
          f">= {BAR_POOL_MEDIAN}.  A count, not a verdict.")
    for name in STRATA:
        s = st.get(name, {})
        if not s.get("n"):
            continue
        b = s["bar_docs68_s6_2"]
        print(f"    {name:<34}pairs/session {_n(b['pairs_per_session']):>6}   pool p50 "
              f"{_n(b['pool_median']):>6}   {'MET' if b['met'] else 'not met'}")
    print("\n" + "=" * 78)
    print("This runner decides nothing.  It counts whether the candle pre-state exists")
    print("and how many placebo candidates each event has.  No forward return, no CI.")
    print("=" * 78)


def main(argv: list) -> int:
    db = HE.DB
    era = "B"
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
    name = name or f"candle_ruler_count_era_{era.lower()}"
    print(f"candle ruler, era {era} - counting (no ladder, no returns, no CI) ...",
          flush=True)
    res = count(db, era=era, progress=True)
    rep = EF._jsonable(build_report(res))
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{name}.json"
    p.write_text(json.dumps(rep, indent=1), encoding="utf-8")
    if len(res["tags"]):
        res["tags"].to_csv(out_dir / f"{name}_events.csv", index=False)
    print_report(rep)
    print(f"-> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""**자 보정** — 봉 자와 테이프 자의 **차등 편향** (D-33 = (가′), `docs/70`).

## 이 모듈의 지위 — 진단이다. 추정량이 아니다. 사다리도 판정도 아니다

D-33 은 위약·사건의 전방 경로를 **봉**으로 재는 자((가′))를 골랐고 그 앞에 **자 보정**을
걸었다(`specs/w3_ruler_bias.md` §1~§2). 결정에 걸리는 양은 *"봉이 높게 읽나"* 가 아니라
**차등 편향**이다 — G-2 의 통계량이 `(실제 − 위약)` 이므로 봉 자가 양쪽을 똑같이 부풀리면
차이는 안 움직인다. 이 모듈은 **두 자가 다 적용되는 같은 순간**에서

    delta          = max_ret_300s(봉) - max_ret_300s(테이프)
    편향(헤드라인)  = median(delta | 실제, 짝 지어진 사건) - median(delta | 위약)

를 낸다. **CI 없음. 사다리 없음. 판정 문구 없음.** 부호·크기·분포만.

## 얼린 것 — `G2G3-PREREG` §3-1 개정 6 [얼린 것] (커밋 `dc65d0f`, 이 파일보다 먼저)

    tau      = 사건이면 t0 (스냅 수신 시각), 위약이면 위약 순간의 시각
    L0(tau)  = floor_min(tau) + 60s              t0 봉의 라벨 - 내용 [floor_min(tau), L0) 가 tau 를 담는다
    F(tau)   = { 봉 b : L0 < T_b <= L0 + 300s }   t0 봉 **다음** 완결 5 봉
    진입가   = F(tau) 의 첫 봉의 open_u             (그 분에 체결이 없으면 F 안의 첫 존재 봉)
    max_ret  = max_{b in F} high_b / 진입가 - 1     진입 봉 자신의 고가 포함

- **(A) t0 봉은 넣지 않는다.** 그 봉의 고가는 t0 이전에 났을 수 있고, 랭킹 진입의 원인인
  급등이 바로 그 봉 안에 있으므로 그 오염은 위약보다 **실제 팔에 크다**. 넣으면
  (실제 − 위약) 이 위로 부풀어 관문이 거짓으로 열린다. 실패 방향은 **진짜 효과를 죽이는
  쪽** — 몇 초짜리 급등이 t0 봉 안에서 끝나면 이 자는 그 뒤의 페이드만 잰다.
- **(B) 진입가는 t0 봉이 끝난 뒤 첫 체결.** tau 이전 가격은 어디에도 안 들어간다. 실패
  방향은 (A) 와 같다 — 진입이 테이프 자의 앵커(t0 뒤 첫 초)보다 0~60 초(+체결 없는 분) 늦다.

이 두 경계는 `tests/test_ruler_bias.py` 가 절대 시각으로 고정하고 돌연변이(t0 봉 포함 /
t0 봉 시가·종가 진입 / 오른쪽 경계 한 칸)로 red 를 확인한다.

## 테이프 자는 다시 구현하지 않는다

개정 3 의 자(앵커 = t0 뒤 첫 초 막대, 진입가 = 그 초의 VWAP, 창 = 앵커 뒤 300 초, 앵커 자신
제외)는 `ranking_forward_path.run()` 의 산출 — 실제 팔의 `max_ret_300s`, 헤드라인 팔
`placebo_vol_density_matched` 의 추첨(씨앗 `RFP.SEED`, 사건당 `MATCH_DRAWS`)과 그
`max_ret_300s` — 을 **그대로** 읽는다. 위약의 순간도 그 추첨이 정한다. 이 모듈이 새로
뽑는 것은 없다. 층(`first_in_regular` 등)은 `candle_ruler.count()` 의 표식을 **그대로**
붙인다 — 층 정의가 두 군데 살면 언젠가 갈라진다.

## 보조 진단 (헤드라인이 아니다)

- **분해**: `delta = (봉 − 봉시계테이프) + (봉시계테이프 − 테이프)`. *봉시계 테이프* 는 진입
  분의 첫 초부터 F 의 끝까지 테이프(초 VWAP)로 잰 것이다. 뒤 항이 **시계**(진입 지연·창 이동·
  진입 초 포함)의 몫, 앞 항이 **가격 출처**(체결 고가 vs 초 VWAP, 시가 vs 첫 초 VWAP)의 몫이다.
- **층별 `delta_real`**: 첫 진입 층은 테이프 자의 앵커 자체가 좌석 뒤라 늦다 — 그 행은 봉 자의
  편향이 아니라 *두 늦은 자의 차이* 다. 앵커 지연을 같이 낸다.
- **세션별 편향**: 부호가 세션마다 흔들리는지. CI 가 아니라 분포다.

## 하지 않는 것

- **CI·부트스트랩·본페로니·사다리·판정** 을 만들지 않는다.
- **홀드아웃(2026-05-01~07-29)·확증 팔(08-26 이후)을 읽지 않는다.** 창은
  `ranking_forward_path.arm_window` 가 정한다 — 이 모듈에 창 규칙이 따로 없다.
- **라이브 API 0 건. DB 는 `mode=ro`. 수집기 무접촉.**

실행: `python -m tossmon.analysis.measure.ruler_bias [db] [--era B] [--out DIR] [--name NAME]`
-> `out/<name>.json` + `<name>_real.csv` + `<name>_placebo.csv`. **콘솔 ASCII.**
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
from tossmon.analysis.measure import candle_ruler as CR
from tossmon.analysis.measure import e2_design_funnel as EF
from tossmon.analysis.measure import ranking_forward_path as RFP
from tossmon.analysis.measure.design_b import CLIP_COSTS
from tossmon.analysis.measure.tick_resolution import pct_table

OUT_DIR = RFP.OUT_DIR
SEC_MS = RFP.SEC_MS
MIN_MS = SS.MIN_MS

#: 전방 경로 = t0 봉 **다음** 완결 5 봉. 300 초 = 테이프 자의 헤드라인 지평(`RFP.HEADLINE_H`).
FWD_BARS = 5
FWD_MS = FWD_BARS * MIN_MS
assert FWD_MS == RFP.HEADLINE_H * SEC_MS

HEADLINE = RFP.HEADLINE
DECLARED = EF.DECLARED
#: 위약 순간을 정하는 팔 = 개정 3 의 헤드라인 팔 (`docs/68` 표 [4] 의 굵은 행).
BIAS_ARM = EF.DIAG_ARM

#: 편향을 견줄 왕복 비용 **추정** — `docs/18` / `STRATEGY-VERDICTS` §4.4 의 $100 클립 2.38%.
#: **호가 스냅샷 추정이고 실체결 0 건이다** (`docs/18` 첫 줄, `docs/68` §10 정정) — "비용
#: 바닥"·"실측" 이 아니다. 여기서 새 숫자를 쓰지 않는다.
ROUND_TRIP_COST = CLIP_COSTS[100]

#: 층. 개정 6 (D) 가 데이터 전에 적은 셋 + 전체. 정의는 `candle_ruler.strata_masks` 그대로.
STRATA = ("anchored", "first_in_regular", "old_observable", "no_seat_then_lane")
STRATA_EXTRA = ("old_unobservable", "lane_same_snap", "no_seat_then_lane_and_first",
                "first_in_regular_old_unobservable")

#: 봉 자가 적용되지 않는 이유. 순서대로 판정한다.
BAR_MISSING = ("applies", "no_bar_in_forward_window", "no_candle_rows")

FORBIDDEN_PHRASES = RFP.FORBIDDEN_PHRASES
#: 산출물 JSON 에 있어서는 안 되는 키 조각 — 이 모듈은 CI 도 사다리도 만들지 않는다.
FORBIDDEN_KEYS = ("ci95", "bonferroni", "bootstrap", "p_value", "verdict")

NO_CI = ("this module computes NO confidence interval, NO bootstrap, NO ladder and NO "
         "verdict - it measures the sign, size and distribution of the differential "
         "ruler bias on a fixed sample (G2G3-PREREG s3-1 revision 6 (D)); the ladder is "
         "step (3) and comes only after revision 6 is completed and committed")


# --------------------------------------------------------------------------- #
# 적재
# --------------------------------------------------------------------------- #
def load_candle_ohlc(conn: sqlite3.Connection, open_ms: int, close_ms: int) -> dict:
    """정규장 **내용**의 1 분봉을 종목별 (라벨, 시가, 고가) 배열로. `candle_ruler.load_candles`
    와 같은 창 — 라벨 `open < T <= close` (내용이 정규장 안에 온전히 드는 봉)."""
    df = pd.read_sql_query(
        "SELECT symbol, ts_ms, open_u, high_u FROM candles_1m "
        "WHERE ts_ms > ? AND ts_ms <= ? ORDER BY symbol, ts_ms",
        conn, params=(int(open_ms), int(close_ms)))
    out = {}
    if df.empty:
        return out
    for sym, sub in df.groupby("symbol", sort=True):
        out[str(sym)] = ohlc_arrays(sub.ts_ms.to_numpy(dtype="int64"),
                                    sub.open_u.to_numpy(dtype="float64"),
                                    sub.high_u.to_numpy(dtype="float64"))
    return out


def ohlc_arrays(ts: np.ndarray, open_u: np.ndarray, high_u: np.ndarray) -> dict:
    ts = np.asarray(ts, dtype="int64")
    order = np.argsort(ts, kind="stable")
    return {"ts": ts[order],
            "open": np.asarray(open_u, dtype="float64")[order],
            "high": np.asarray(high_u, dtype="float64")[order]}


# --------------------------------------------------------------------------- #
# 봉 자 — 경계가 이 모듈의 전부다 (개정 6 (A)·(B)·(C))
# --------------------------------------------------------------------------- #
def t0_bar_label(tau_ms) -> np.ndarray:
    """t0 봉의 라벨 `L0 = floor_min(tau) + 60s`. 내용 `[floor_min(tau), L0)` 가 `tau` 를 담는다.
    `tau` 가 분 경계에 정확히 놓여도(`tau = floor_min(tau)`) 그 봉은 t0 봉이다 — 규칙을 하나로 둔다."""
    return CR.last_completed_label(tau_ms) + MIN_MS


def bar_forward_at(cand: dict, tau_ms) -> dict:
    """기준 시각 `tau` 마다 `F(tau) = { T : L0 < T <= L0 + 300s }` 의 전방 경로.

    - `lo` = 라벨 `T <= L0` 인 봉 수 (`side='right'`) — **라벨 `L0` 인 t0 봉은 밖**이다.
      `side='left'` 로 바꾸거나 `L0` 를 `floor_min(tau)` 로 두면 t0 봉이 들어온다 — 그 두
      돌연변이가 테스트를 red 로 만든다.
    - `hi` = 라벨 `T <= L0 + 300s` 인 봉 수 (`side='right'`) — 라벨이 정확히 `L0 + 300s` 인
      다섯째 봉은 **안**이다.
    - 진입가 = `F` 의 첫 봉의 시가. `max_ret` = `F` 안 고가의 최댓값 / 진입가 − 1.
    - `entry_lag_s` = 진입 봉의 **분 시작**(라벨 − 60s) − `tau`. 첫 분에 봉이 있으면 (0, 60] 초.
    - `t_max_min` = 고가가 난 봉의 순서(1..5, 라벨 기준 분 오프셋). 여럿이면 첫 것.
    """
    tau = np.atleast_1d(np.asarray(tau_ms, dtype="int64"))
    l0 = t0_bar_label(tau)
    ts = cand["ts"]
    lo = np.searchsorted(ts, l0, side="right")
    hi = np.searchsorted(ts, l0 + FWD_MS, side="right")
    n = (hi - lo).astype("int64")
    m = tau.size
    entry = np.full(m, np.nan)
    top = np.full(m, -np.inf)
    top_label = np.full(m, -1, dtype="int64")
    entry_label = np.full(m, -1, dtype="int64")
    has = n > 0
    if has.any() and ts.size:
        first = np.minimum(lo, ts.size - 1)
        entry[has] = cand["open"][first[has]]
        entry_label[has] = ts[first[has]]
        for d in range(FWD_BARS):
            j = lo + d
            ok = has & (j < hi)
            if not ok.any():
                break
            jj = np.minimum(j, ts.size - 1)
            h = np.where(ok, cand["high"][jj], -np.inf)
            better = ok & (h > top)
            top = np.where(better, h, top)
            top_label = np.where(better, ts[jj], top_label)
    max_ret = np.full(m, np.nan)
    good = has & np.isfinite(entry) & (entry > 0)
    max_ret[good] = top[good] / entry[good] - 1.0
    entry_lag = np.full(m, np.nan)
    entry_lag[has] = (entry_label[has] - MIN_MS - tau[has]) / 1000.0
    t_max_min = np.full(m, np.nan)
    t_max_min[good] = (top_label[good] - l0[good]) / MIN_MS
    return {"n_bars": n, "entry_u": entry, "max_ret": max_ret,
            "entry_lag_s": entry_lag, "entry_label_ms": entry_label, "l0_ms": l0,
            "t_max_min": t_max_min}


def tape_on_bar_clock(ts: np.ndarray, px: np.ndarray, tau_ms, entry_label_ms) -> np.ndarray:
    """분해용 **봉시계 테이프 자** — 진입 = 진입 분의 첫 초 막대(`ts >= 라벨 − 60s`),
    창 = 그 초부터 `L0 + 300s` 까지(진입 초 **포함**, 봉 자가 진입 봉의 고가를 포함하는 것의 거울).
    `max_ret` = 창 안 초 VWAP 최댓값 / 진입 초 VWAP − 1. 진입 봉이 없으면 nan."""
    tau = np.atleast_1d(np.asarray(tau_ms, dtype="int64"))
    lab = np.atleast_1d(np.asarray(entry_label_ms, dtype="int64"))
    out = np.full(tau.size, np.nan)
    if ts.size == 0:
        return out
    end = t0_bar_label(tau) + FWD_MS
    for i in range(tau.size):
        if lab[i] < 0:
            continue
        a = int(np.searchsorted(ts, lab[i] - MIN_MS, side="left"))
        b = int(np.searchsorted(ts, end[i], side="right"))
        if b <= a:
            continue
        base = float(px[a])
        if not (np.isfinite(base) and base > 0):
            continue
        out[i] = float(np.max(px[a:b])) / base - 1.0
    return out


# --------------------------------------------------------------------------- #
# 두 자를 같은 순간에 놓는다
# --------------------------------------------------------------------------- #
def _round_ms(open_ms: int, t_s: np.ndarray) -> np.ndarray:
    return int(open_ms) + np.rint(np.asarray(t_s, dtype="float64") * 1000.0).astype("int64")


def _bar_side(cand: dict | None, tau: np.ndarray) -> dict:
    m = int(tau.size)
    if cand is None or cand["ts"].size == 0:
        return {"n_bars": np.zeros(m, "int64"), "entry_u": np.full(m, np.nan),
                "max_ret": np.full(m, np.nan), "entry_lag_s": np.full(m, np.nan),
                "entry_label_ms": np.full(m, -1, "int64"), "l0_ms": t0_bar_label(tau),
                "t_max_min": np.full(m, np.nan), "why": np.full(m, "no_candle_rows", object)}
    f = bar_forward_at(cand, tau)
    f["why"] = np.where(f["n_bars"] > 0, "applies", "no_bar_in_forward_window").astype(object)
    return f


def _moment_rows(sym: str, tau: np.ndarray, tape_raw: dict, sel: np.ndarray,
                 cand: dict | None, secs: dict) -> dict:
    """한 종목의 순간들(`tau`)에 두 자를 놓는다. `tape_raw[sel]` 이 테이프 자의 값이다."""
    f = _bar_side(cand, tau)
    tape = np.asarray(tape_raw[HEADLINE], dtype="float64")[sel]
    tape_n = np.asarray(tape_raw[f"n_bars_{RFP.HEADLINE_H}s"], dtype="float64")[sel]
    tape_tmax = np.asarray(tape_raw[f"t_max_{RFP.HEADLINE_H}s"], dtype="float64")[sel]
    sb = secs.get(sym)
    if sb is not None:
        clock = tape_on_bar_clock(sb[0], sb[4], tau, f["entry_label_ms"])
    else:
        clock = np.full(tau.size, np.nan)
    bar = np.asarray(f["max_ret"], dtype="float64")
    return {
        "symbol": np.full(tau.size, sym, dtype=object), "tau_ms": tau,
        "tape_max_ret": tape, "tape_n_bars": tape_n, "tape_t_max_s": tape_tmax,
        "tape_applies": np.isfinite(tape) & (tape_n >= 1),
        "bar_n_bars": f["n_bars"], "bar_entry_u": f["entry_u"], "bar_max_ret": bar,
        "bar_entry_lag_s": f["entry_lag_s"], "bar_t_max_min": f["t_max_min"],
        "bar_why": f["why"], "bar_applies": np.isfinite(bar),
        "clock_max_ret": clock,
        "delta": bar - tape,
        "timing_component": clock - tape,
        "source_component": bar - clock,
    }


def moments_of_cell(res: dict, conn: sqlite3.Connection, tags: pd.DataFrame,
                    progress: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    """`ranking_forward_path.run()` 의 선언한 칸에서 실제 순간과 위약 순간을 복원하고
    (새 추첨 없음 — `e2_design_funnel.declared_pairs` 와 같은 검산) 봉 자를 얹는다."""
    rtype, _dur, kind, cell = DECLARED
    key = f"{rtype}|{kind}|{cell}|all"
    box = res["cells"].get(key)
    if not box:
        return pd.DataFrame(), pd.DataFrame()
    arm = box["arms"].get(BIAS_ARM)
    sess_names = sorted(box["real"])
    if arm is None or len(sess_names) != len(arm["pairing"]) or len(sess_names) != len(box["lag"]):
        raise ValueError(f"{key}: ledgers misaligned - real {len(sess_names)} sessions, "
                         f"lag {len(box['lag'])}, pairing "
                         f"{None if arm is None else len(arm['pairing'])}")
    meta = {s["session"]: s for s in res["sessions"]}
    real_parts, plac_parts = [], []
    for sess, lag, d in zip(sess_names, box["lag"], arm["pairing"]):
        s = meta[sess]
        rr = box["real"][sess]
        pr = arm["raw"][sess]
        n = int(rr["_symbol"].size)
        if n != int(np.asarray(lag).size) or n != int(np.asarray(d["paired"]).size):
            raise ValueError(f"{key} {sess}: anchored rows {n} vs lag {np.asarray(lag).size} "
                             f"vs paired {np.asarray(d['paired']).size}")
        if int(pr["_symbol"].size) != int(np.asarray(d["sym"]).size):
            raise ValueError(f"{key} {sess}: placebo rows != draws")
        cand = load_candle_ohlc(conn, s["open_ms"], s["close_ms"])
        secs = RFP.session_second_bars(conn, s["open_ms"], s["close_ms"])
        anchor_ms = _round_ms(s["open_ms"], rr["t_in_session_s"])
        t0_ms = anchor_ms - np.rint(np.asarray(lag, dtype="float64") * 1000.0).astype("int64")
        syms = np.asarray(rr["_symbol"], dtype=object)
        paired = np.asarray(d["paired"], dtype=bool)
        for sym in np.unique(syms):
            sel = np.flatnonzero(syms == sym)
            rows = _moment_rows(str(sym), t0_ms[sel], rr, sel, cand.get(str(sym)), secs)
            rows["session"] = np.full(sel.size, sess, dtype=object)
            rows["slot"] = sel
            rows["anchor_ms"] = anchor_ms[sel]
            rows["anchor_lag_s"] = np.asarray(lag, dtype="float64")[sel]
            rows["paired"] = paired[sel]
            real_parts.append(pd.DataFrame(rows))
        tau_p = _round_ms(s["open_ms"], pr["t_in_session_s"])
        psyms = np.asarray(pr["_symbol"], dtype=object)
        slot = np.asarray(d["slot"], dtype="int64")
        for sym in np.unique(psyms):
            sel = np.flatnonzero(psyms == sym)
            rows = _moment_rows(str(sym), tau_p[sel], pr, sel, cand.get(str(sym)), secs)
            rows["session"] = np.full(sel.size, sess, dtype=object)
            rows["slot"] = slot[sel]
            rows["event_t0_ms"] = t0_ms[slot[sel]]
            plac_parts.append(pd.DataFrame(rows))
        if progress:
            print(f"  {sess} anchored={n:>4,} paired={int(paired.sum()):>4,} "
                  f"draws={int(slot.size):>5,}", flush=True)
    real = pd.concat(real_parts, ignore_index=True) if real_parts else pd.DataFrame()
    plac = pd.concat(plac_parts, ignore_index=True) if plac_parts else pd.DataFrame()
    if not real.empty:
        real = attach_strata(real, tags)
    return real, plac


def attach_strata(real: pd.DataFrame, tags: pd.DataFrame) -> pd.DataFrame:
    """`candle_ruler.count()` 의 표식을 (세션, 종목, t0) 로 붙인다. **전부 맞아야 한다** —
    하나라도 못 찾으면 t0 복원이 틀린 것이라 예외로 죽는다."""
    cols = ["session", "symbol", "t0_ms", "anchored", "seat_at_t0", "seat_after", "seat_age_s",
            "first_in_regular", "pre_match60", "key_ok", "tier"]
    t = tags[cols].rename(columns={"t0_ms": "tau_ms"})
    merged = real.merge(t, on=["session", "symbol", "tau_ms"], how="left", validate="one_to_one")
    lost = merged.anchored.isna()
    if lost.any():
        raise ValueError(f"{int(lost.sum())} anchored moments have no candle_ruler tag - "
                         f"t0 reconstruction disagrees with session_events")
    if not merged.anchored.astype(bool).all():
        raise ValueError("a run() anchored event is not anchored in candle_ruler tags")
    masks = CR.strata_masks(merged)
    for name in STRATA + STRATA_EXTRA:
        merged[f"stratum_{name}"] = np.asarray(masks[name], dtype=bool)
    return merged


# --------------------------------------------------------------------------- #
# 요약 — 분포만
# --------------------------------------------------------------------------- #
def dist(values) -> dict:
    a = np.asarray(values, dtype="float64")
    a = a[np.isfinite(a)]
    if a.size == 0:
        return {"n": 0, "median": None, "mean": None, "p25": None, "p75": None,
                "share_positive": None}
    return {"n": int(a.size), "median": float(np.median(a)), "mean": float(a.mean()),
            "p25": float(np.percentile(a, 25)), "p75": float(np.percentile(a, 75)),
            "share_positive": float((a > 0).mean())}


def _diff(a: dict, b: dict, name: str):
    return None if a.get(name) is None or b.get(name) is None else a[name] - b[name]


def census(real: pd.DataFrame, plac: pd.DataFrame) -> dict:
    r_t = real.tape_applies.to_numpy(bool) if len(real) else np.zeros(0, bool)
    r_b = real.bar_applies.to_numpy(bool) if len(real) else np.zeros(0, bool)
    r_p = real.paired.to_numpy(bool) if len(real) else np.zeros(0, bool)
    p_t = plac.tape_applies.to_numpy(bool) if len(plac) else np.zeros(0, bool)
    p_b = plac.bar_applies.to_numpy(bool) if len(plac) else np.zeros(0, bool)
    why_r = real.bar_why.to_numpy(object) if len(real) else np.zeros(0, object)
    why_p = plac.bar_why.to_numpy(object) if len(plac) else np.zeros(0, object)
    return {
        "real": {"n_anchored": int(r_t.size), "tape_applies": int(r_t.sum()),
                 "bar_applies": int(r_b.sum()), "both": int((r_t & r_b).sum()),
                 "paired_headline_arm": int(r_p.sum()), "paired_and_both": int((r_p & r_t & r_b).sum()),
                 "bar_why": {w: int((why_r == w).sum()) for w in BAR_MISSING},
                 "n_symbols_both": int(real.symbol[r_t & r_b].nunique()) if len(real) else 0},
        "placebo": {"n_draws": int(p_t.size), "tape_applies": int(p_t.sum()),
                    "bar_applies": int(p_b.sum()), "both": int((p_t & p_b).sum()),
                    "bar_why": {w: int((why_p == w).sum()) for w in BAR_MISSING},
                    "events_with_a_valid_draw": (int(plac[p_t & p_b].groupby(["session", "slot"]).ngroups)
                                                 if len(plac) else 0)},
    }


def headline(real: pd.DataFrame, plac: pd.DataFrame) -> dict:
    """개정 6 (D) 의 그 한 수: `median(delta | 실제, 짝) − median(delta | 위약)`. 평균판을 나란히
    (G-2 통계량은 평균이다). 짝 단위판은 보조."""
    rh = real[real.paired & real.tape_applies & real.bar_applies] if len(real) else real
    ph = plac[plac.tape_applies & plac.bar_applies] if len(plac) else plac
    d_real = dist(rh.delta) if len(rh) else dist([])
    d_plac = dist(ph.delta) if len(ph) else dist([])
    pair = dist([])
    if len(rh) and len(ph):
        pm = ph.groupby(["session", "slot"]).delta.mean().rename("plac_delta_mean").reset_index()
        j = rh[["session", "slot", "delta"]].merge(pm, on=["session", "slot"], how="inner")
        pair = dist(j.delta - j.plac_delta_mean)
    levels = {}
    for name, df in (("real", rh), ("placebo", ph)):
        levels[name] = {"bar": dist(df.bar_max_ret) if len(df) else dist([]),
                        "tape": dist(df.tape_max_ret) if len(df) else dist([])}
    diff_under = {}
    for ruler in ("bar", "tape"):
        diff_under[ruler] = {k: _diff(levels["real"][ruler], levels["placebo"][ruler], k)
                             for k in ("median", "mean")}
    return {
        "delta_real": d_real, "delta_placebo": d_plac,
        "bias_median": _diff(d_real, d_plac, "median"),
        "bias_mean": _diff(d_real, d_plac, "mean"),
        "pair_level": pair,
        "levels": levels,
        "real_minus_placebo_under_each_ruler": diff_under,
        "identity_check_mean": (None if diff_under["bar"]["mean"] is None
                                or diff_under["tape"]["mean"] is None
                                else diff_under["bar"]["mean"] - diff_under["tape"]["mean"]),
        "what": ("bias_median = median(bar - tape | real, paired, both rulers apply) - "
                 "median(bar - tape | placebo draws of the headline arm, both apply). "
                 "Positive = the candle ruler inflates (real - placebo) = the G-2 pass line "
                 "moves UP by this much; negative = the candle ruler under-reads the effect"),
    }


def by_stratum(real: pd.DataFrame) -> dict:
    out = {}
    if not len(real):
        return out
    both = real.tape_applies & real.bar_applies
    for name in STRATA + STRATA_EXTRA:
        m = both & real[f"stratum_{name}"]
        sub = real[m]
        out[name] = {
            "n_stratum": int(real[f"stratum_{name}"].sum()), "n_both": int(m.sum()),
            "delta_real": dist(sub.delta), "paired_and_both": int(sub.paired.sum()),
            "delta_real_paired": dist(sub.delta[sub.paired]),
            "anchor_lag_s_p50": _p50(sub.anchor_lag_s),
            "bar_entry_lag_s_p50": _p50(sub.bar_entry_lag_s),
            "tape_t_max_le_60s_share": _share_le(sub.tape_t_max_s, 60.0),
            "bar_t_max_in_entry_bar_share": _share_le(sub.bar_t_max_min, 1.0),
            "levels": {"bar": dist(sub.bar_max_ret), "tape": dist(sub.tape_max_ret)},
        }
    return out


def _p50(v):
    a = np.asarray(v, dtype="float64")
    a = a[np.isfinite(a)]
    return float(np.median(a)) if a.size else None


def _share_le(v, x: float):
    a = np.asarray(v, dtype="float64")
    a = a[np.isfinite(a)]
    return float((a <= x).mean()) if a.size else None


def decomposition(real: pd.DataFrame, plac: pd.DataFrame) -> dict:
    """`delta = source + timing`. 항마다 실제·위약·차이(중앙값). 분해가 닫히는지(합 = delta) 도 적는다."""
    rh = real[real.paired & real.tape_applies & real.bar_applies] if len(real) else real
    ph = plac[plac.tape_applies & plac.bar_applies] if len(plac) else plac
    out = {}
    for comp in ("timing_component", "source_component"):
        a = dist(rh[comp]) if len(rh) else dist([])
        b = dist(ph[comp]) if len(ph) else dist([])
        out[comp] = {"real": a, "placebo": b, "differential_median": _diff(a, b, "median"),
                     "differential_mean": _diff(a, b, "mean")}
    closes = True
    for df in (rh, ph):
        if len(df):
            s = (df.timing_component + df.source_component - df.delta).to_numpy(dtype="float64")
            s = s[np.isfinite(s)]
            closes = closes and (s.size == 0 or float(np.max(np.abs(s))) < 1e-12)
    out["closes"] = bool(closes)
    out["clock_defined_share"] = {
        "real": float(np.isfinite(rh.clock_max_ret).mean()) if len(rh) else None,
        "placebo": float(np.isfinite(ph.clock_max_ret).mean()) if len(ph) else None}
    return out


def per_session(real: pd.DataFrame, plac: pd.DataFrame) -> list:
    out = []
    sessions = sorted(set(real.session) | set(plac.session)) if (len(real) or len(plac)) else []
    for sess in sessions:
        rh = real[(real.session == sess) & real.paired & real.tape_applies & real.bar_applies]
        ph = plac[(plac.session == sess) & plac.tape_applies & plac.bar_applies]
        a, b = dist(rh.delta), dist(ph.delta)
        out.append({"session": sess, "n_real": a["n"], "n_placebo": b["n"],
                    "delta_real_median": a["median"], "delta_placebo_median": b["median"],
                    "bias_median": _diff(a, b, "median"), "bias_mean": _diff(a, b, "mean")})
    return out


def speed(real: pd.DataFrame, plac: pd.DataFrame) -> dict:
    """급등이 얼마나 빠른가 — 테이프 자의 고점 도달 시각과 봉 자의 고점 봉. 사용자의 걱정
    (*"몇 초 만에 쏘고 몇 초 만에 박는다"*)을 같은 표본에서 센다."""
    out = {}
    for name, df, m in (("real_paired", real, (real.paired & real.tape_applies & real.bar_applies) if len(real) else None),
                        ("real_all", real, (real.tape_applies & real.bar_applies) if len(real) else None),
                        ("placebo", plac, (plac.tape_applies & plac.bar_applies) if len(plac) else None)):
        sub = df[m] if m is not None else df
        out[name] = {"n": int(len(sub)),
                     "tape_t_max_s": pct_table(sub.tape_t_max_s) if len(sub) else {"n": 0},
                     "tape_t_max_le_60s_share": _share_le(sub.tape_t_max_s, 60.0) if len(sub) else None,
                     "bar_t_max_min_hist": ({str(k): int((sub.bar_t_max_min == k).sum())
                                             for k in range(1, FWD_BARS + 1)} if len(sub) else {}),
                     "bar_entry_lag_s_p50": _p50(sub.bar_entry_lag_s) if len(sub) else None,
                     "anchor_lag_s_p50": _p50(sub.anchor_lag_s) if (len(sub) and "anchor_lag_s" in sub) else None}
    return out


# --------------------------------------------------------------------------- #
# 러너 — 재기만
# --------------------------------------------------------------------------- #
def measure(db: Path, *, era: str = "B", progress: bool = False) -> dict:
    """한 시대의 선언한 칸에서 두 자를 같은 순간에 놓는다. 창·세션은
    `ranking_forward_path.arm_window` 가, 테이프 값과 위약 순간은 `run()` 이, 층은
    `candle_ruler.count()` 가 정한다."""
    if era not in RFP.EXPLORATION_ERAS:
        raise ValueError(f"era must be one of {RFP.EXPLORATION_ERAS}, got {era!r}")
    rtype, _dur, kind, cell = DECLARED
    kc = (kind, cell)
    if progress:
        print(f"  tape ruler: ranking_forward_path.run era {era}, cell {kc} ...", flush=True)
    res = RFP.run(db, exploration_era=era, cell_grid=(kc,), primary_cells=(kc,),
                  funnel_only_elsewhere=True, progress=progress)
    allowed = RFP.EXPLORATION_SESSIONS if era == "A" else RFP.EXPLORATION_B_SESSIONS
    bad = sorted({s["session"] for s in res["sessions"]} - set(allowed))
    if bad:
        raise ValueError(f"sessions outside era {era} reached this run: {bad}")
    if progress:
        print(f"  strata: candle_ruler.count era {era} ...", flush=True)
    counted = CR.count(db, era=era, progress=False)
    if [s["session"] for s in counted["sessions"]] != [s["session"] for s in res["sessions"]]:
        raise ValueError("candle_ruler.count and ranking_forward_path.run opened different sessions")
    conn = HE.open_ro(db)
    try:
        real, plac = moments_of_cell(res, conn, counted["tags"], progress=progress)
    finally:
        conn.close()
    used = [s["session"] for s in res["sessions"]]
    return {"db": str(db), "era": era, "since_ms": res["since_ms"], "since_utc": res["since_utc"],
            "until_ms": res["until_ms"], "until_utc": res["until_utc"],
            "db_max_snap_utc": res["db_max_snap_utc"], "sessions": res["sessions"],
            "sessions_used": used, "n_sessions": len(used), "real": real, "placebo": plac,
            "census": census(real, plac), "headline": headline(real, plac),
            "by_stratum": by_stratum(real), "decomposition": decomposition(real, plac),
            "per_session": per_session(real, plac), "speed": speed(real, plac)}


def build_report(res: dict) -> dict:
    """산출물 하나. **문서에 실리는 수치는 전부 여기를 지나간다.** CI 는 없다."""
    h = res["headline"]
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
            "step": "(1) calibrate the ruler - specs/w3_ruler_bias.md s2; frozen in G2G3-PREREG "
                    "s3-1 revision 6 [frozen] BEFORE this runner existed",
            "declared_cell": list(DECLARED),
            "candle_ruler": {
                "t0_bar": "label L0 = floor_min(tau) + 60s; its content [floor_min(tau), L0) "
                          "contains tau; it is EXCLUDED from the forward path (A)",
                "forward_window": f"labels in (L0, L0 + {FWD_MS // 1000}s] = the {FWD_BARS} "
                                  "completed bars AFTER the t0 bar (C)",
                "entry": "open_u of the FIRST bar inside the forward window (the first trade "
                         "after the t0 minute ends); if that minute has no bar, the first "
                         "existing bar in the window (B)",
                "max_ret": "max(high_u over the window) / entry - 1; the entry bar's own high "
                           "is included (it comes after the open)",
                "lookahead_check": "no price from before tau enters anywhere - neither the "
                                   "entry nor the high",
                "failure_direction": "BOTH freezes fail towards KILLING a real effect: a "
                                     "seconds-scale pump that ends inside the t0 bar is "
                                     "measured as fade only, and the entry is 0-60s later "
                                     "than the tape anchor",
                "raw_prices": "1m candles are RAW prices (contract A5); never mixed with 1d",
            },
            "tape_ruler": "revision 3, NOT re-implemented: ranking_forward_path.run() - anchor "
                          "= first second bar after t0 (<= 300s), entry = that second's VWAP, "
                          "window = (anchor, anchor + 300s], anchor itself excluded",
            "placebo_moments": f"the draws of the headline arm {BIAS_ARM} (seed {RFP.SEED}, "
                               f"{RFP.MATCH_DRAWS} per event) - no new draw here",
            "sample": "moments where BOTH rulers apply: tape n_bars_300s >= 1 and >= 1 bar in "
                      "the candle forward window",
            "headline_statistic": "median(delta | real, paired) - median(delta | placebo)",
            "decomposition": "delta = source_component + timing_component; timing = bar-clock "
                             "tape ruler (entry = first tape second of the entry minute, "
                             "window to L0 + 300s, entry second included) minus tape ruler; "
                             "source = candle ruler minus bar-clock tape ruler. DIAGNOSTIC",
            "round_trip_cost": ROUND_TRIP_COST,
            "no_ci_here": NO_CI,
            "costs": "NOT subtracted - that is G-3 and needs a preregistration",
        },
        "census": res["census"],
        "headline": h,
        "vs_round_trip_cost": {
            "bias_median_over_cost": (None if h["bias_median"] is None
                                      else h["bias_median"] / ROUND_TRIP_COST),
            "bias_mean_over_cost": (None if h["bias_mean"] is None
                                    else h["bias_mean"] / ROUND_TRIP_COST)},
        "by_stratum": res["by_stratum"],
        "decomposition": res["decomposition"],
        "per_session": res["per_session"],
        "speed": res["speed"],
        "funnel": {"n_sessions": res["n_sessions"]},
    }


# --------------------------------------------------------------------------- #
# 콘솔 - ASCII 만
# --------------------------------------------------------------------------- #
def _pp(v):
    """수익률 -> 퍼센트포인트 문자열."""
    return "   -   " if v is None else f"{100.0 * float(v):+7.3f}"


def _p(v):
    return "  -  " if v is None else f"{100.0 * float(v):5.1f}%"


def _n(v):
    return "-" if v is None else f"{float(v):.1f}"


def _dist_row(label: str, d: dict) -> str:
    return (f"{label:<34}{d['n']:>6,}{_pp(d['median']):>9}{_pp(d['mean']):>9}"
            f"{_pp(d['p25']):>9}{_pp(d['p75']):>9}{_p(d['share_positive']):>8}")


def print_report(rep: dict) -> None:
    a = rep["arm"]
    d = rep["design"]
    print("=" * 78)
    print("RULER BIAS - step (1): calibrate before the ladder   (docs/70, D-33 = (a'))")
    print("=" * 78)
    for i, s in enumerate(rep["labels"], 1):
        for j, line in enumerate(RFP._wrap(s, 70)):
            print(f"  [{i}] {line}" if j == 0 else f"      {line}")
    print(f"  arm     : exploration era {a['exploration_era']}  sessions "
          f"{len(a['sessions_used'])} {','.join(a['sessions_used'])}")
    print(f"  window  : {rep['window']['since_utc']} .. {rep['window']['until_utc']}"
          f"  (confirmation floor {a['confirmation_floor_utc']} - not read)")
    print(f"  cell    : {d['declared_cell']}")
    cr = d["candle_ruler"]
    for k in ("t0_bar", "forward_window", "entry", "max_ret", "failure_direction"):
        for j, line in enumerate(RFP._wrap(cr[k], 60)):
            print(f"  {k + ':':<20}{line}" if j == 0 else f"  {'':<20}{line}")
    for k in ("tape_ruler", "placebo_moments", "sample", "headline_statistic", "no_ci_here"):
        for j, line in enumerate(RFP._wrap(d[k], 60)):
            print(f"  {k + ':':<20}{line}" if j == 0 else f"  {'':<20}{line}")

    c = rep["census"]
    print("\n[1] where do both rulers apply?  (the calibration sample)")
    r, p = c["real"], c["placebo"]
    print(f"    real   : anchored {r['n_anchored']:,} -> tape applies {r['tape_applies']:,} | "
          f"candle applies {r['bar_applies']:,} | both {r['both']:,} ({r['n_symbols_both']:,} symbols)")
    print(f"             paired in {BIAS_ARM}: {r['paired_headline_arm']:,} -> paired AND both "
          f"{r['paired_and_both']:,}   <- the headline set")
    print(f"             candle side missing: " + ", ".join(f"{k} {v:,}" for k, v in r["bar_why"].items()))
    print(f"    placebo: draws {p['n_draws']:,} -> tape applies {p['tape_applies']:,} | candle applies "
          f"{p['bar_applies']:,} | both {p['both']:,} (events with >= 1 valid draw "
          f"{p['events_with_a_valid_draw']:,})")
    print(f"             candle side missing: " + ", ".join(f"{k} {v:,}" for k, v in p["bar_why"].items()))

    h = rep["headline"]
    print("\n[2] HEADLINE - delta = max_ret_300s(candle) - max_ret_300s(tape), same moment")
    print("    values in percentage points; median first (revision 6 (D)), mean beside it")
    print(f"{'arm':<34}{'n':>6}{'median':>9}{'mean':>9}{'p25':>9}{'p75':>9}{'>0':>8}")
    print(_dist_row("real (paired, both apply)", h["delta_real"]))
    print(_dist_row("placebo (headline draws, both)", h["delta_placebo"]))
    print(f"    DIFFERENTIAL BIAS  median(real) - median(placebo) = {_pp(h['bias_median'])} pp"
          f"     mean version = {_pp(h['bias_mean'])} pp")
    print(_dist_row("pair level: d_real - mean(d_plac)", h["pair_level"]))
    v = rep["vs_round_trip_cost"]
    print(f"    vs round-trip cost ESTIMATE {100 * d['round_trip_cost']:.2f}% ($100 clip, docs/18, zero real fills): "
          f"median bias / cost = {_n(v['bias_median_over_cost'])}x, mean bias / cost = "
          f"{_n(v['bias_mean_over_cost'])}x")
    print("    sign: positive = the candle ruler inflates (real - placebo); the G-2 pass line")
    print("          would move UP by that much.  negative = the candle ruler under-reads.")

    print("\n[3] levels - max_ret_300s under each ruler on the SAME headline sample")
    print(f"{'arm x ruler':<34}{'n':>6}{'median':>9}{'mean':>9}{'p25':>9}{'p75':>9}{'>0':>8}")
    for arm in ("real", "placebo"):
        for ruler in ("bar", "tape"):
            print(_dist_row(f"{arm} / {'candle' if ruler == 'bar' else 'tape'}",
                            h["levels"][arm][ruler]))
    du = h["real_minus_placebo_under_each_ruler"]
    print(f"    (real - placebo) under the candle ruler: median {_pp(du['bar']['median'])} "
          f"mean {_pp(du['bar']['mean'])} pp;  under the tape ruler: median "
          f"{_pp(du['tape']['median'])} mean {_pp(du['tape']['mean'])} pp")
    print(f"    identity: (real-placebo | candle) - (real-placebo | tape) in means = "
          f"{_pp(h['identity_check_mean'])} pp = the mean bias above")

    print("\n[4] by stratum - delta on real events where both apply (NOT the headline)")
    print("    first_in_regular: the tape anchor itself comes after the seat, i.e. late -")
    print("    that row compares two late rulers, it is not the candle ruler's bias")
    print(f"{'stratum':<34}{'n_str':>6}{'both':>6}{'median':>9}{'mean':>9}{'>0':>8}"
          f"{'pairedn':>8}{'pair_md':>9}{'anc_lag':>8}{'bar_lag':>8}{'tmax<60':>8}")
    for name in STRATA + STRATA_EXTRA:
        s = rep["by_stratum"].get(name)
        if not s:
            continue
        dr = s["delta_real"]
        print(f"{name:<34}{s['n_stratum']:>6,}{s['n_both']:>6,}{_pp(dr['median']):>9}"
              f"{_pp(dr['mean']):>9}{_p(dr['share_positive']):>8}{s['paired_and_both']:>8,}"
              f"{_pp(s['delta_real_paired']['median']):>9}{_n(s['anchor_lag_s_p50']):>8}"
              f"{_n(s['bar_entry_lag_s_p50']):>8}{_p(s['tape_t_max_le_60s_share']):>8}")
    print("    anc_lag = tape anchor lag after t0 (s, p50); bar_lag = candle entry minute start")
    print("    after t0 (s, p50); tmax<60 = share whose tape max came within 60s of the anchor")

    dc = rep["decomposition"]
    print("\n[5] decomposition (diagnostic) - delta = source + timing, medians in pp")
    print("    timing = bar-clock tape ruler - tape ruler (entry 0-60s later, window shifted,")
    print("    entry second included); source = candle ruler - bar-clock tape ruler (trade")
    print("    high vs second VWAP, open vs first-second VWAP)")
    print(f"{'component':<34}{'real n':>7}{'median':>9}{'placebo n':>10}{'median':>9}"
          f"{'diff_md':>9}{'diff_mean':>10}")
    for comp in ("timing_component", "source_component"):
        x = dc[comp]
        print(f"{comp:<34}{x['real']['n']:>7,}{_pp(x['real']['median']):>9}"
              f"{x['placebo']['n']:>10,}{_pp(x['placebo']['median']):>9}"
              f"{_pp(x['differential_median']):>9}{_pp(x['differential_mean']):>10}")
    print(f"    closes (source + timing == delta wherever the clock ruler is defined): "
          f"{'yes' if dc['closes'] else 'NO'}; clock defined share real "
          f"{_p(dc['clock_defined_share']['real'])} placebo {_p(dc['clock_defined_share']['placebo'])}")

    print("\n[6] per session - sign stability (a distribution, not a CI)")
    print(f"{'session':<14}{'n_real':>7}{'n_plac':>7}{'d_real_md':>10}{'d_plac_md':>10}"
          f"{'bias_md':>9}{'bias_mean':>10}")
    for s in rep["per_session"]:
        print(f"{s['session']:<14}{s['n_real']:>7,}{s['n_placebo']:>7,}"
              f"{_pp(s['delta_real_median']):>10}{_pp(s['delta_placebo_median']):>10}"
              f"{_pp(s['bias_median']):>9}{_pp(s['bias_mean']):>10}")

    sp = rep["speed"]
    print("\n[7] how fast is the move?  tape t_max after the anchor; candle t_max bar (1 = entry bar)")
    print(f"{'set':<16}{'n':>6}{'tmax p50 s':>11}{'tmax<=60s':>10}{'entry lag':>10}"
          f"{'anc lag':>8}   candle t_max bar histogram 1..5")
    for name in ("real_paired", "real_all", "placebo"):
        x = sp[name]
        t = x["tape_t_max_s"]
        hist = " ".join(f"{k}:{v:,}" for k, v in x["bar_t_max_min_hist"].items())
        print(f"{name:<16}{x['n']:>6,}{_n(t.get('p50')):>11}{_p(x['tape_t_max_le_60s_share']):>10}"
              f"{_n(x['bar_entry_lag_s_p50']):>10}{_n(x['anchor_lag_s_p50']):>8}   {hist}")
    print("\n" + "=" * 78)
    print("This runner decides nothing.  It measures the differential bias of one ruler")
    print("against another on a fixed sample.  No CI, no ladder, no verdict.")
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
    name = name or f"ruler_bias_era_{era.lower()}"
    print(f"ruler bias, era {era} - calibrating (no ladder, no CI, no verdict) ...", flush=True)
    res = measure(db, era=era, progress=True)
    rep = EF._jsonable(build_report(res))
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{name}.json"
    p.write_text(json.dumps(rep, indent=1), encoding="utf-8")
    if len(res["real"]):
        res["real"].to_csv(out_dir / f"{name}_real.csv", index=False)
    if len(res["placebo"]):
        res["placebo"].to_csv(out_dir / f"{name}_placebo.csv", index=False)
    print_report(rep)
    print(f"-> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

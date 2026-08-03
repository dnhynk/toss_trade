"""**이탈이 얼마나 값어치가 있는가** — 완전예지 상한 대비 회수율 (docs/23 §10-P).

## 왜 이 측정인가

사용자 실전 감각(2026-08-03, D-5 답변)은 두 가지가 섞여 있었다:

- **(나) "진입을 잘 잡으면 더 자주 만났고 크기는 종목 나름"** — 이건 우리 위약 시험과
  **정확히 일치**한다. 진입 규칙은 **도래율**만 움직였고(0.833 vs 무작위 0.40~0.60)
  **크기는 움직이지 않았다.** 데이터와 사람이 독립적으로 같은 답을 냈다.
- **(다) "언제 파느냐가 수익 크기를 갈랐다"** — 이건 **아직 제대로 재지 않았다.**

우리가 시험한 이탈 12종은 **전부 자기 가격만 본다**(고정 시계·다운틱·추적손절·목표가).
결과도 서로 구분되지 않았다(gross +0.37~+0.52%, CI 대부분 겹침). 그런데 사용자는
차트를 **내내 보면서** 팔았다 — 가격만 본 게 아니다. 두 가지 읽기가 가능하다:

1. 이탈은 사실 별 차이를 못 만든다 (사용자 감각이 틀렸다)
2. **우리 12종에 정답이 안 들어 있다** — 가격만 보는 규칙들이라서

이 러너는 **어느 쪽인지 판정**한다. 방법은 하나다: **이탈에 남은 여지의 총량**을 재고,
관측 가능한 규칙들이 그중 얼마를 회수하는지 본다.

## 상한(CEILING)은 전략 수익이 아니다 — 절대로

`ceiling_*` 로 시작하는 모든 것은 **사후 최적 시점 매도**이며 **달성 불가능**하다.
미래를 보고 파는 것이므로 접두사 불변성도 **성립하지 않는다**(그렇게 표시한다).
이것을 전략 수익으로 읽는 것이 정확히 감사 5차 C-1 의 사고였다. 상한의 쓸모는
**단 하나** — 관측 가능한 규칙들이 남긴 격차를 재는 자다:

    격차 = 상한 평균 - 규칙 평균 = **이탈 기술의 최대 값어치**

격차가 작으면 (1)이 맞다: 이탈을 아무리 개선해도 소득이 없다.
격차가 크면 (2)가 맞고, 다음 탐색 방향이 정해진다.

**그리고 상한에도 비용을 뺀다.** 상한조차 시장가 왕복 2.38% 를 못 넘으면 이탈 개선은
무의미하며, 그 사실이 이 측정에서 가장 중요한 결과가 된다.

## 가격 아닌 신호로 파는 이탈 (새 계열)

지금까지 이탈은 자기 가격만 봤다. 이제 다른 것도 본다 — 랭킹(순위 둔화·역전,
유동성 점유율 하락), 티어2 호가(매도 잔량 두꺼워짐, 매수 지지 소멸, 스프레드 급확대),
체결 테이프(체결 강도 소멸). **전부 접두사 불변**이며, 상한 회수율로 12종과 **같은
표에서** 비교한다.

실행: `python -m tossmon.analysis.measure.exit_value [db_path]`
라이브 0콜. DB 는 **읽기 전용**. 콘솔 ASCII.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from tossmon.analysis import shots as S
from tossmon.analysis.measure import design_b as D
from tossmon.analysis.rules import bootstrap_ci_mean

HORIZON_S = 600

#: 신호가 이보다 적게 관측되면 그 진입에서 그 규칙은 **판정 불가**로 센다.
#: 조용히 버리지 않는다 — `__available` 로 세어 표에 n 을 같이 싣는다.
MIN_SIGNAL_OBS = 3

#: 표본이 이만큼은 되어야 규칙 하나를 판정한다(필요 일수 역산의 목표치).
MIN_N_FOR_VERDICT = 30

#: **미래를 보는 규칙의 명시 등록부.** 여기 적힌 것만 접두사 불변성 시험이 면제되며,
#: 테스트가 "면제 목록 = 상한뿐"임을 강제한다. 관측 가능 규칙이 슬쩍 여기 들어오면
#: 테스트가 깨진다.
LOOK_AHEAD_RULES = ("ceiling_perfect_foresight",)

OUT_DIR = D.OUT_DIR

#: docs/23 §10-P 가 싣는 규칙별 필드 전체. 가드가 **동일 집합**으로 대조한다.
REPORTED_FIELDS = (
    "rule", "family", "look_ahead",
    "n", "available_rate",                        # §10-P.2 좌측
    "gross_mean", "gross_ci",
    "recovery_of_ceiling", "gap_to_ceiling",      # §10-P.2 본체
    "placebo_gross_mean", "placebo_recovery_of_ceiling",          # §10-P.3
    "pair_n", "diff_mean", "diff_ci", "diff_ci_bonferroni", "diff_verdict",  # §10-P.3
    "net_by_scenario",                            # §10-P.4
    "powered", "days_needed_for_n",               # §10-P.5
)


# --------------------------------------------------------------------------- #
# 1. 상한 — **사후 최적 매도. 달성 불가.**
# --------------------------------------------------------------------------- #
def ceiling_perfect_foresight(price: pd.Series, entry_ms: int, entry_u: float,
                              ctx: dict | None = None, *,
                              horizon_s: int = HORIZON_S) -> dict:
    """**상한(CEILING) — 미래를 보고 판다. 전략 수익이 아니다.**

    지평 안 **최고 관측가**에 청산한다. 즉시 청산(=진입가)도 선택지이므로 상한은
    항상 0 이상이다. 이것은 어떤 이탈 규칙도 넘을 수 없는 천장이며, **어떤 규칙도
    도달할 수 없다** — 최고점이 어디였는지는 사후에만 안다.

    이 값을 전략 수익으로 읽지 마라. 감사 5차 C-1 이 정확히 그 사고였다.
    """
    w = price[(price.index >= entry_ms) & (price.index <= entry_ms + horizon_s * 1000)]
    if w.empty:
        return {"exit_u": float("nan"), "exit_ms": entry_ms,
                "filled": False, "available": False}
    top = float(w.max())
    return {"exit_u": top, "exit_ms": int(w.idxmax()),
            "filled": True, "available": True}


# --------------------------------------------------------------------------- #
# 2. 랭킹 신호로 파는 이탈 — 사용자 직관 C(순위)·D(유동성 점유율)
# --------------------------------------------------------------------------- #
def _window(sig: pd.Series | None, entry_ms: int, horizon_s: int) -> pd.Series:
    if sig is None or len(sig) == 0:
        return pd.Series(dtype="float64")
    return sig[(sig.index >= entry_ms) & (sig.index <= entry_ms + horizon_s * 1000)]


def _exit_at(price: pd.Series, ts_ms: int) -> float:
    """신호가 뜬 **그 시각의 관측가**로 청산한다(미래를 보지 않는다)."""
    return S.price_at(price, ts_ms)


def _fallback(price: pd.Series, entry_ms: int, horizon_s: int) -> dict:
    """신호가 끝까지 안 뜨면 지평 마지막 관측가. `filled=False` 로 구분해 센다."""
    w = price[(price.index >= entry_ms) & (price.index <= entry_ms + horizon_s * 1000)]
    if w.empty:
        return {"exit_u": float("nan"), "exit_ms": entry_ms,
                "filled": False, "available": False}
    return {"exit_u": float(w.iloc[-1]), "exit_ms": int(w.index[-1]),
            "filled": False, "available": True}


def _unavailable(entry_ms: int) -> dict:
    """신호 자체가 없다 — **판정 불가**. 0 이나 결측 대체값으로 채우지 않는다."""
    return {"exit_u": float("nan"), "exit_ms": entry_ms,
            "filled": False, "available": False}


def exit_rank_stall(price: pd.Series, entry_ms: int, entry_u: float, ctx: dict, *,
                    stall_s: int = 60, horizon_s: int = HORIZON_S) -> dict:
    """**순위 상승이 멈추면 판다** (사용자 직관 C).

    순위는 낮을수록 좋다. 지금까지의 **최고 순위**를 갱신하지 못한 채 `stall_s` 가
    지나면 청산한다. 판정 시점까지의 순위만 보므로 접두사 불변이다.
    """
    w = _window(ctx.get("rank"), entry_ms, horizon_s)
    if len(w) < MIN_SIGNAL_OBS:
        return _unavailable(entry_ms)
    best, best_ts = float(w.iloc[0]), int(w.index[0])
    for ts, r in zip(w.index.tolist()[1:], w.to_numpy()[1:]):
        r, ts = float(r), int(ts)
        if r < best:
            best, best_ts = r, ts
        elif ts - best_ts >= stall_s * 1000:
            px = _exit_at(price, ts)
            if px == px:
                return {"exit_u": px, "exit_ms": ts, "filled": True, "available": True}
    return _fallback(price, entry_ms, horizon_s)


def exit_rank_reverse(price: pd.Series, entry_ms: int, entry_u: float, ctx: dict, *,
                      worsen_by: int = 5, horizon_s: int = HORIZON_S) -> dict:
    """**순위가 뒤집히면 판다** (사용자 직관 C의 강한 형태).

    지금까지의 최고 순위보다 `worsen_by` 계단 이상 밀리면 청산한다.
    """
    w = _window(ctx.get("rank"), entry_ms, horizon_s)
    if len(w) < MIN_SIGNAL_OBS:
        return _unavailable(entry_ms)
    best = float(w.iloc[0])
    for ts, r in zip(w.index.tolist()[1:], w.to_numpy()[1:]):
        r, ts = float(r), int(ts)
        if r >= best + worsen_by:
            px = _exit_at(price, ts)
            if px == px:
                return {"exit_u": px, "exit_ms": ts, "filled": True, "available": True}
        best = min(best, r)
    return _fallback(price, entry_ms, horizon_s)


def exit_share_drop(price: pd.Series, entry_ms: int, entry_u: float, ctx: dict, *,
                    frac: float = 0.5, horizon_s: int = HORIZON_S) -> dict:
    """**유동성 점유율이 반토막 나면 판다** (사용자 직관 D).

    점유율은 **같은 스냅 안에서** `vol_qu / sum(vol_qu)` 로 만든다. 계약 C-2 주의:
    `vol_qu` 는 **롤링 윈도 값이라 시점 간 차분이 무효**다. 그래서 여기서는 차분하지
    않고 **같은 순간의 비율**만 쓴다 — 분모·분자가 같은 시각·같은 단위이므로 비율은
    성립한다. 시간에 따라 비교하는 것은 그 **비율의 수준**이지 누적량의 증분이 아니다.
    """
    w = _window(ctx.get("share"), entry_ms, horizon_s)
    if len(w) < MIN_SIGNAL_OBS:
        return _unavailable(entry_ms)
    peak = float(w.iloc[0])
    for ts, v in zip(w.index.tolist()[1:], w.to_numpy()[1:]):
        v, ts = float(v), int(ts)
        if peak > 0 and v <= frac * peak:
            px = _exit_at(price, ts)
            if px == px:
                return {"exit_u": px, "exit_ms": ts, "filled": True, "available": True}
        peak = max(peak, v)
    return _fallback(price, entry_ms, horizon_s)


# --------------------------------------------------------------------------- #
# 3. 티어2 호가·체결 테이프로 파는 이탈
# --------------------------------------------------------------------------- #
def exit_ask_thicken(price: pd.Series, entry_ms: int, entry_u: float, ctx: dict, *,
                     mult: float = 2.0, horizon_s: int = HORIZON_S) -> dict:
    """**매도 잔량이 두꺼워지면 판다** — 위에서 물량이 쌓이면 못 간다."""
    ob = ctx.get("ob")
    w = _window(None if ob is None else ob["ask1_qu"], entry_ms, horizon_s)
    if len(w) < MIN_SIGNAL_OBS:
        return _unavailable(entry_ms)
    base = float(w.iloc[0])
    if not (base > 0):
        return _unavailable(entry_ms)
    for ts, v in zip(w.index.tolist()[1:], w.to_numpy()[1:]):
        if float(v) >= mult * base:
            px = _exit_at(price, int(ts))
            if px == px:
                return {"exit_u": px, "exit_ms": int(ts), "filled": True,
                        "available": True}
    return _fallback(price, entry_ms, horizon_s)


def exit_bid_vanish(price: pd.Series, entry_ms: int, entry_u: float, ctx: dict, *,
                    frac: float = 0.5, horizon_s: int = HORIZON_S) -> dict:
    """**매수 지지가 사라지면 판다** — 최우선 매수 잔량이 지금까지 최대의 절반 이하."""
    ob = ctx.get("ob")
    w = _window(None if ob is None else ob["bid1_qu"], entry_ms, horizon_s)
    if len(w) < MIN_SIGNAL_OBS:
        return _unavailable(entry_ms)
    peak = float(w.iloc[0])
    for ts, v in zip(w.index.tolist()[1:], w.to_numpy()[1:]):
        v = float(v)
        if peak > 0 and v <= frac * peak:
            px = _exit_at(price, int(ts))
            if px == px:
                return {"exit_u": px, "exit_ms": int(ts), "filled": True,
                        "available": True}
        peak = max(peak, v)
    return _fallback(price, entry_ms, horizon_s)


def exit_spread_blowout(price: pd.Series, entry_ms: int, entry_u: float, ctx: dict, *,
                        mult: float = 2.0, horizon_s: int = HORIZON_S) -> dict:
    """**스프레드가 급확대되면 판다** — 나갈 문이 좁아지는 신호."""
    ob = ctx.get("ob")
    if ob is None or "rel_spread" not in getattr(ob, "columns", []):
        return _unavailable(entry_ms)
    w = _window(ob["rel_spread"], entry_ms, horizon_s)
    w = w[w == w]
    if len(w) < MIN_SIGNAL_OBS:
        return _unavailable(entry_ms)
    base = float(w.iloc[0])
    if not (base > 0):
        return _unavailable(entry_ms)
    for ts, v in zip(w.index.tolist()[1:], w.to_numpy()[1:]):
        if float(v) >= mult * base:
            px = _exit_at(price, int(ts))
            if px == px:
                return {"exit_u": px, "exit_ms": int(ts), "filled": True,
                        "available": True}
    return _fallback(price, entry_ms, horizon_s)


def exit_tape_fade(price: pd.Series, entry_ms: int, entry_u: float, ctx: dict, *,
                   bucket_s: int = 30, frac: float = 0.5,
                   horizon_s: int = HORIZON_S) -> dict:
    """**체결 강도가 정점에서 꺾이면 판다** — 30초 버킷 체결 수량이 최고의 절반 이하.

    버킷은 **닫힌 것만** 본다(진행 중인 버킷을 세면 미래를 보게 된다).
    """
    tape = ctx.get("tape")
    if tape is None or len(tape) == 0:
        return _unavailable(entry_ms)
    end = entry_ms + horizon_s * 1000
    t = tape[(tape.index >= entry_ms) & (tape.index <= end)]
    if len(t) < MIN_SIGNAL_OBS:
        return _unavailable(entry_ms)
    step = bucket_s * 1000
    peak = 0.0
    b0 = entry_ms
    while b0 + step <= end:
        b1 = b0 + step
        vol = float(t[(t.index >= b0) & (t.index < b1)].sum())
        if peak > 0 and vol <= frac * peak:
            px = _exit_at(price, b1)          # 버킷이 **닫힌 뒤**에 판정한다
            if px == px:
                return {"exit_u": px, "exit_ms": int(b1), "filled": True,
                        "available": True}
        peak = max(peak, vol)
        b0 = b1
    return _fallback(price, entry_ms, horizon_s)


def signal_exit_rules() -> dict:
    """가격 아닌 신호로 파는 이탈 + 상한. 계열 이름을 함께 돌려준다."""
    return {
        "ceiling_perfect_foresight": ("CEILING", ceiling_perfect_foresight),
        "rank_stall_60s": ("ranking", exit_rank_stall),
        "rank_reverse_5": ("ranking", exit_rank_reverse),
        "share_drop_50pct": ("ranking", exit_share_drop),
        "ob_ask_thicken_2x": ("orderbook", exit_ask_thicken),
        "ob_bid_vanish_50pct": ("orderbook", exit_bid_vanish),
        "ob_spread_blowout_2x": ("orderbook", exit_spread_blowout),
        "tape_fade_50pct": ("tape", exit_tape_fade),
    }


# --------------------------------------------------------------------------- #
# 4. 신호 자료 적재
# --------------------------------------------------------------------------- #
def load_day_signals(conn, day: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT snap_ms, ranking_type, symbol, rank, vol_qu FROM rankings_snap "
        "WHERE date(snap_ms/1000,'unixepoch')=? AND ranking_type=?",
        conn, params=(day, S.TOSS_VOLUME))


def load_day_orderbook(conn, day: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT symbol, snap_ms, bid1_u, bid1_qu, ask1_u, ask1_qu, spread_u "
        "FROM orderbook_snap WHERE date(snap_ms/1000,'unixepoch')=?",
        conn, params=(day,))


def load_day_tape(conn, day: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT symbol, ts_ms, qty_u FROM trades_snap "
        "WHERE date(ts_ms/1000,'unixepoch')=?", conn, params=(day,))


def build_contexts(sig: pd.DataFrame, ob: pd.DataFrame,
                   tape: pd.DataFrame) -> dict:
    """종목별 신호 묶음. 순위·점유율·호가·테이프를 한 번에 만든다."""
    ctx: dict = {}
    if len(sig):
        s = sig.dropna(subset=["rank"]).copy()
        tot = s.groupby("snap_ms")["vol_qu"].transform("sum")
        s["share"] = np.where(tot > 0, s["vol_qu"] / tot, np.nan)
        for sym, g in s.groupby("symbol"):
            g = g.drop_duplicates("snap_ms").set_index("snap_ms").sort_index()
            ctx.setdefault(sym, {})["rank"] = g["rank"].astype("float64")
            ctx[sym]["share"] = g["share"].astype("float64")
    if len(ob):
        o = ob.copy()
        mid = (pd.to_numeric(o["bid1_u"], errors="coerce")
               + pd.to_numeric(o["ask1_u"], errors="coerce")) / 2.0
        o["rel_spread"] = np.where(mid > 0,
                                   pd.to_numeric(o["spread_u"], errors="coerce") / mid,
                                   np.nan)
        for sym, g in o.groupby("symbol"):
            g = g.drop_duplicates("snap_ms").set_index("snap_ms").sort_index()
            ctx.setdefault(sym, {})["ob"] = g
    if len(tape):
        for sym, g in tape.groupby("symbol"):
            ctx.setdefault(sym, {})["tape"] = (
                g.set_index("ts_ms")["qty_u"].astype("float64").sort_index())
    return ctx


# --------------------------------------------------------------------------- #
# 5. 평가 — 기존 12종 + 상한 + 신호 계열을 **한 표에서**
# --------------------------------------------------------------------------- #
def evaluate_all(series_by_symbol: dict, ctx_by_symbol: dict, entries: list[dict], *,
                 entry_delay_s: int = D.ENTRY_DELAY_S,
                 horizon_s: int = HORIZON_S) -> pd.DataFrame:
    """모든 진입 × 모든 규칙. **미도달·손실·판정불가 전부 계상한다.**"""
    price_rules = D.exit_rules(horizon_s)
    sig_rules = signal_exit_rules()
    rows = []
    for e in entries:
        ser = series_by_symbol.get(e["symbol"])
        if ser is None or ser.empty:
            continue
        fill_ms = int(e["signal_ms"]) + entry_delay_s * 1000
        entry_u = S.price_at(ser, fill_ms)
        if not (entry_u == entry_u and entry_u > 0):
            continue
        ctx = ctx_by_symbol.get(e["symbol"], {})
        rec = {"symbol": e["symbol"], "entry_ms": fill_ms, "entry_u": entry_u,
               "entry_idx": int(e.get("entry_idx", -1))}
        for name, fn in price_rules.items():
            r = fn(ser, fill_ms, entry_u)
            rec[name] = (float(r["exit_u"] / entry_u - 1.0)
                         if r["exit_u"] == r["exit_u"] else float("nan"))
            rec[f"{name}__available"] = bool(r["exit_u"] == r["exit_u"])
        for name, (_fam, fn) in sig_rules.items():
            r = fn(ser, fill_ms, entry_u, ctx, horizon_s=horizon_s)
            ok = bool(r.get("available")) and r["exit_u"] == r["exit_u"]
            rec[name] = float(r["exit_u"] / entry_u - 1.0) if ok else float("nan")
            rec[f"{name}__available"] = ok
        rows.append(rec)
    return pd.DataFrame(rows)


def all_rule_names() -> list[str]:
    return list(D.exit_rules().keys()) + list(signal_exit_rules().keys())


def rule_family(name: str) -> str:
    sig = signal_exit_rules()
    return sig[name][0] if name in sig else "price_only"


# --------------------------------------------------------------------------- #
# 6. 회수율 — 이 측정의 본체
# --------------------------------------------------------------------------- #
def recovery_of_ceiling(rule_mean: float, ceiling_mean: float) -> float:
    """규칙 평균이 **상한 평균의 몇 배**인가.

    진입별 비율의 평균이 아니라 **평균의 비율**이다 — 진입별 상한이 0 에 가까우면
    비율이 폭주하기 때문이다. 상한 평균은 항상 양수이므로 이 비율은 항상 정의된다.
    """
    if not (ceiling_mean == ceiling_mean and ceiling_mean > 0):
        return float("nan")
    return float(rule_mean / ceiling_mean)


def days_needed_for_n(available_per_day: float, *,
                      min_n: int = MIN_N_FOR_VERDICT) -> dict:
    """지금 수집 속도로 판정 표본 `min_n` 을 모으려면 며칠인가.

    효과크기 역산이 아니라 **자료 도달률** 역산이다 — 티어2 호가·테이프처럼 그 규칙을
    **적용할 수조차 없는** 진입이 대부분일 때 필요한 계산이다.
    """
    if not (available_per_day > 0):
        return {"reachable": False, "reason": "no usable entries at current coverage"}
    days = min_n / available_per_day
    return {"reachable": True, "min_n": min_n,
            "available_per_day": float(available_per_day),
            "days_needed": int(np.ceil(days))}


def run(db: Path, *, entry_delay_s: int = D.ENTRY_DELAY_S,
        horizon_s: int = HORIZON_S) -> dict:
    """전 매매일 × (실제 + 위약). 진입 정의는 `design_b` 와 **완전히 같다**."""
    conn = D.ro(db)
    try:
        days = D.load_days(conn)
        real_by_day, plac_by_day, cover = {}, {}, {}
        for day in days:
            rk = D.load_day_rank(conn, day)
            if rk.empty:
                continue
            series = {}
            for sym in rk["symbol"].unique():
                s = S.price_series_multi(rk, sym)
                if len(s) >= 20:
                    series[sym] = s
            ctx = build_contexts(load_day_signals(conn, day),
                                 load_day_orderbook(conn, day),
                                 load_day_tape(conn, day))
            ents = D.collect_entries(rk)
            real_by_day[day] = evaluate_all(series, ctx, ents,
                                            entry_delay_s=entry_delay_s,
                                            horizon_s=horizon_s)
            cover[day] = int(len(real_by_day[day]))
            pl = []
            for seed in D.PLACEBO_SEEDS:
                pe = D.placebo_entries(ents, list(series.keys()), seed)
                d = evaluate_all(series, ctx, pe, entry_delay_s=entry_delay_s,
                                 horizon_s=horizon_s)
                d["seed"] = seed
                pl.append(d)
            plac_by_day[day] = (pd.concat(pl, ignore_index=True) if pl
                                else pd.DataFrame())
    finally:
        conn.close()
    return {"days": days, "real": real_by_day, "placebo": plac_by_day,
            "per_day_counts": cover}


def build_report(res: dict) -> dict:
    """docs/23 §10-P 의 표를 **한 자료구조로** 만든다."""
    days = [d for d in res["days"] if len(res["real"].get(d, []))]
    if not days:
        return {"days": [], "rules": [], "n_real": 0, "pooled_ci_permitted": False}
    allr = pd.concat([res["real"][d] for d in days], ignore_index=True)
    plac = [res["placebo"][d] for d in days if len(res["placebo"].get(d, []))]
    allp = pd.concat(plac, ignore_index=True) if plac else pd.DataFrame()
    n_entries = len(allr)

    ceil_v = pd.to_numeric(allr["ceiling_perfect_foresight"], errors="coerce").dropna()
    ceil_mean = float(ceil_v.mean()) if len(ceil_v) else float("nan")
    ceil_lo, ceil_hi = (bootstrap_ci_mean(ceil_v.tolist()) if len(ceil_v) >= 2
                        else (float("nan"), float("nan")))
    pceil = (pd.to_numeric(allp.get("ceiling_perfect_foresight"), errors="coerce")
             .dropna() if len(allp) else pd.Series(dtype=float))
    pceil_mean = float(pceil.mean()) if len(pceil) else float("nan")

    rules = []
    for name in all_rule_names():
        v = pd.to_numeric(allr.get(name), errors="coerce").dropna()
        avail = (float(allr[f"{name}__available"].mean())
                 if f"{name}__available" in allr.columns else float("nan"))
        lo, hi = (bootstrap_ci_mean(v.tolist()) if len(v) >= 2
                  else (float("nan"), float("nan")))
        mean = float(v.mean()) if len(v) else float("nan")
        pv = (pd.to_numeric(allp.get(name), errors="coerce").dropna()
              if len(allp) else pd.Series(dtype=float))
        pmean = float(pv.mean()) if len(pv) else float("nan")
        # **회수율만으로는 아무것도 주장할 수 없다.** 위약과의 쌍체 차이 CI 가 있어야
        # "이 이탈이 낫다"고 말할 수 있다 — §10-N.8 에서 얻은 교훈 그대로다.
        diff = D.difference_ci(allr, allp, name, n_rules=len(all_rule_names()))
        powered = bool(len(v) >= MIN_N_FOR_VERDICT)
        rules.append({
            "rule": name,
            "family": rule_family(name),
            "look_ahead": name in LOOK_AHEAD_RULES,
            "n": int(len(v)),
            "available_rate": avail,
            "gross_mean": mean,
            "gross_ci": [lo, hi],
            "recovery_of_ceiling": recovery_of_ceiling(mean, ceil_mean),
            "gap_to_ceiling": (float(ceil_mean - mean)
                               if mean == mean else float("nan")),
            "placebo_gross_mean": pmean,
            "placebo_recovery_of_ceiling": recovery_of_ceiling(pmean, pceil_mean),
            "pair_n": diff["n"],
            "diff_mean": diff["mean"],
            "diff_ci": list(diff["ci"]),
            "diff_ci_bonferroni": list(diff["ci_bonferroni"]),
            "diff_verdict": diff["verdict"],
            "net_by_scenario": {k: (mean - c if mean == mean else float("nan"))
                                for k, c in D.COST_SCENARIOS.items()},
            "powered": powered,
            "days_needed_for_n": days_needed_for_n(len(v) / len(days)),
        })
    return {
        "days": days, "n_real": int(n_entries),
        "per_day_counts": res.get("per_day_counts", {}),
        "pooled_ci_permitted": len(days) >= D.MIN_DAY_CLUSTERS,
        "cost_scenarios": dict(D.COST_SCENARIOS),
        "ceiling": {
            "mean": ceil_mean, "ci": [ceil_lo, ceil_hi],
            "placebo_mean": pceil_mean,
            "net_by_scenario": {k: ceil_mean - c
                                for k, c in D.COST_SCENARIOS.items()},
            "warning": ("CEILING is perfect-foresight and UNACHIEVABLE - it is a "
                        "measuring stick for the gap, never a strategy return"),
        },
        "rules": rules,
    }


def main(db: Path, *, out_dir: Path | None = None) -> int:
    res = run(db)
    rep = build_report(res)
    if not rep["days"]:
        print("no entries")
        return 0
    print(f"trading days with entries: {len(rep['days'])} -> {rep['days']}")
    print(f"real entries {rep['n_real']}   per-day {rep['per_day_counts']}")
    print(f"  effective day clusters = {len(rep['days'])} -> pooled CI "
          f"{'PERMITTED' if rep['pooled_ci_permitted'] else 'WITHHELD'}")

    c = rep["ceiling"]
    print("\n=== [docs/23 sec 10-P.1] CEILING - perfect foresight, UNACHIEVABLE")
    print("  !! this is NOT a strategy return. It is the ceiling no rule can reach.")
    print(f"  ceiling gross mean {c['mean']:+.4f}  "
          f"CI [{c['ci'][0]:+.4f},{c['ci'][1]:+.4f}]   placebo ceiling "
          f"{c['placebo_mean']:+.4f}")
    for k, v in c["net_by_scenario"].items():
        verdict = "clears cost" if v > 0 else "BELOW COST"
        print(f"    ceiling net after {k:<20} {v:+.4f}   {verdict}")

    print("\n=== [docs/23 sec 10-P.2] RECOVERY OF THE CEILING (how much each rule gets)")
    print(f"{'rule':<26}{'family':<11}{'n':>5}{'avail':>7}{'gross':>9}"
          f"{'recovery':>10}{'gap':>9}{'plac.rec':>10}")
    for r in rep["rules"]:
        tag = " (LOOK-AHEAD)" if r["look_ahead"] else ""
        print(f"{r['rule']:<26}{r['family']:<11}{r['n']:>5}{r['available_rate']:>7.2f}"
              f"{r['gross_mean']:>9.4f}{r['recovery_of_ceiling']:>10.3f}"
              f"{r['gap_to_ceiling']:>9.4f}"
              f"{r['placebo_recovery_of_ceiling']:>10.3f}{tag}")
    print("  recovery = rule mean / ceiling mean (ratio of means; per-entry ratios "
          "explode when a ceiling is near zero).")
    print("  gap = ceiling mean - rule mean = THE MOST an exit improvement can buy.")
    print("  CAUTION a ceiling is a MAXIMUM, so it grows with volatility alone. A big "
          "gap proves room exists, never that any rule can reach it.")
    print("  CAUTION placebo recovery divides by the PLACEBO ceiling, which is much "
          "smaller - compare absolute means (below), not these ratios, across arms.")

    print("\n=== [docs/23 sec 10-P.3] PAIRED DIFFERENCE vs PLACEBO (real - placebo)")
    print(f"{'rule':<26}{'pair n':>7}{'real':>9}{'placebo':>9}{'diff':>9}"
          f"{'CI low':>9}{'CI high':>9}{'verdict':>15}")
    for r in rep["rules"]:
        print(f"{r['rule']:<26}{r['pair_n']:>7}{r['gross_mean']:>9.4f}"
              f"{r['placebo_gross_mean']:>9.4f}{r['diff_mean']:>9.4f}"
              f"{r['diff_ci'][0]:>9.4f}{r['diff_ci'][1]:>9.4f}{r['diff_verdict']:>15}")
    vc: dict = {}
    for r in rep["rules"]:
        vc[r["diff_verdict"]] = vc.get(r["diff_verdict"], 0) + 1
    print(f"  verdict counts: {vc}")
    print("  NOTE recovery alone claims nothing. Only a difference CI that excludes "
          "zero supports 'this exit is better'.")

    print("\n=== [docs/23 sec 10-P.4] COST SCENARIOS applied to every rule AND ceiling")
    scen = list(D.COST_SCENARIOS)
    print(f"{'rule':<26}" + "".join(f"{k:>20}" for k in scen))
    for r in rep["rules"]:
        print(f"{r['rule']:<26}"
              + "".join(f"{r['net_by_scenario'][k]:>20.4f}" for k in scen))
    print(f"  scenarios: {D.COST_SCENARIOS}")

    print("\n=== [docs/23 sec 10-P.5] POWER - which rules can be judged at all")
    for r in rep["rules"]:
        if r["powered"]:
            continue
        dn = r["days_needed_for_n"]
        tag = (f"{dn['days_needed']} more days at {dn['available_per_day']:.1f}/day"
               if dn.get("reachable") else f"UNREACHABLE ({dn.get('reason')})")
        print(f"  {r['rule']:<26} n={r['n']:<4} INSUFFICIENT (need "
              f"{MIN_N_FOR_VERDICT}) -> {tag}")
    print("  NOTE rules listed here get NO verdict. Tier-2 orderbook and tape only "
          "began accumulating recently.")

    out = (out_dir or OUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "exit_value.json"
    path.write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1
                  else Path(r"C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db")))

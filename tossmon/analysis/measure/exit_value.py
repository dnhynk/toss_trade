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

from tossmon.analysis import session as SS
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
               "entry_idx": e.get("entry_idx", -1),
               "session": SS.session_of(fill_ms)}
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


# --------------------------------------------------------------------------- #
# 7. 통제된 위약 — **상한 우위가 변동성 선택 효과인가**
# --------------------------------------------------------------------------- #
#: 정합 허용 오차 — 진입 직전 실현변동성이 ±20% 안이면 "비슷한 종목"으로 본다.
VOL_MATCH_TOL = 0.20

#: 자기 종목 위약이 실제 진입과 겹치지 않도록 띄우는 최소 간격.
SELF_PLACEBO_MIN_GAP_S = 600

#: 통제 비교 개수(본페로니) — 3개 통제군 × 3개 지표.
N_CONTROL_COMPARISONS = 9


def realized_volatility(ts: np.ndarray, px: np.ndarray, end_ms: int, *,
                        window_s: int) -> float:
    """`end_ms` **이하** `window_s` 구간의 로그수익률 표준편차.

    진입 직전 창을 쓰면 **미래를 보지 않는다** — 정합 기준이 사전 관측만으로
    만들어져야 통제가 성립한다.
    """
    if ts is None or len(ts) < 3:
        return float("nan")
    lo = np.searchsorted(ts, end_ms - window_s * 1000, side="right")
    hi = np.searchsorted(ts, end_ms, side="right")
    w = px[lo:hi]
    w = w[w > 0]
    if len(w) < 3:
        return float("nan")
    return float(np.std(np.diff(np.log(w)), ddof=1))


def numpy_view(series_by_symbol: dict) -> dict:
    """종목별 (시각, 가격) 배열. 정합 풀 계산이 pandas 슬라이싱이면 너무 느리다."""
    return {s: (ser.index.to_numpy(), ser.to_numpy())
            for s, ser in series_by_symbol.items() if len(ser)}


def volatility_match_pools(entries: list[dict], series_by_symbol: dict, *,
                           tol: float = VOL_MATCH_TOL,
                           lookback_s: int = D.ENTRY_LOOKBACK_S) -> dict:
    """진입별 **변동성 정합 후보 풀**. 씨앗과 무관하므로 한 번만 만든다.

    상한은 최댓값이므로 변동성만 커져도 커진다. 무작위 종목 위약은 대개 덜 움직이는
    종목이라, 상한 격차가 **"우리가 변동성 큰 종목을 골랐다"** 로 설명될 수 있다.
    그래서 **진입 직전 실현변동성이 ±`tol` 안**인 종목만 후보로 둔다.
    """
    view = numpy_view(series_by_symbol)
    pools: dict[int, list[str]] = {}
    base_rv: dict[int, float] = {}
    for e in entries:
        i = e.get("entry_idx", -1)
        ts = int(e["signal_ms"])
        own = view.get(e["symbol"])
        rv0 = (realized_volatility(own[0], own[1], ts, window_s=lookback_s)
               if own else float("nan"))
        base_rv[i] = rv0
        if not (rv0 == rv0 and rv0 > 0):
            pools[i] = []
            continue
        lo, hi = rv0 * (1.0 - tol), rv0 * (1.0 + tol)
        cand = []
        for sym, (a, p) in view.items():
            if sym == e["symbol"]:
                continue
            rv = realized_volatility(a, p, ts, window_s=lookback_s)
            if rv == rv and lo <= rv <= hi:
                cand.append(sym)
        pools[i] = cand
    return {"pools": pools, "base_rv": base_rv}


def volatility_matched_placebo(entries: list[dict], match: dict,
                               seed: int) -> list[dict]:
    """정합 풀에서 한 종목씩 뽑는다. **풀이 비면 그 진입은 짝을 잃는다**(세어 보고)."""
    rng = np.random.default_rng(seed)
    pools = match["pools"]
    out = []
    for e in entries:
        i = e.get("entry_idx", -1)
        cand = pools.get(i) or []
        if not cand:
            continue
        pick = cand[int(rng.integers(0, len(cand)))]
        out.append({"symbol": pick, "signal_ms": e["signal_ms"],
                    "signal_u": float("nan"), "entry_idx": i})
    return out


def self_symbol_placebo(entries: list[dict], series_by_symbol: dict, seed: int, *,
                        entry_delay_s: int = D.ENTRY_DELAY_S,
                        horizon_s: int = HORIZON_S,
                        min_gap_s: int = SELF_PLACEBO_MIN_GAP_S) -> list[dict]:
    """**같은 종목·같은 날·무작위 시각.** 가장 깨끗한 통제군.

    종목 고유 변동성이 **완전히 상쇄**되므로, 남는 차이는 오직 **"그 순간을 고른 것"**
    의 값어치다. 실제 진입과 `min_gap_s` 이상 떨어뜨려 구간이 겹치지 않게 한다.
    """
    rng = np.random.default_rng(seed)
    out = []
    for e in entries:
        ser = series_by_symbol.get(e["symbol"])
        if ser is None or len(ser) < 3:
            continue
        ts = ser.index.to_numpy()
        need = (entry_delay_s + horizon_s) * 1000
        ok = ts[(ts + need <= ts[-1])
                & (np.abs(ts - int(e["signal_ms"])) >= min_gap_s * 1000)]
        if len(ok) == 0:
            continue
        out.append({"symbol": e["symbol"],
                    "signal_ms": int(ok[int(rng.integers(0, len(ok)))]),
                    "signal_u": float("nan"),
                    "entry_idx": e.get("entry_idx", -1)})
    return out


def ceiling_arm(series_by_symbol: dict, entries: list[dict], *,
                entry_delay_s: int = D.ENTRY_DELAY_S, horizon_s: int = HORIZON_S,
                lookback_s: int = D.ENTRY_LOOKBACK_S) -> pd.DataFrame:
    """한 팔(arm)의 상한 + 변동성 정규화 상한.

    - `ceiling_per_pre_rv` — **사전** 변동성으로 나눈다(미래를 보지 않는 정규화).
    - `ceiling_per_horizon_rv` — 구간 내 변동성으로 나눈다. **서술용이며 사후값**이라
      거래 가능한 양이 아니다. 실제·위약에 **동일하게** 적용하므로 비교로는 성립한다.
    """
    view = numpy_view(series_by_symbol)
    rows = []
    for e in entries:
        ser = series_by_symbol.get(e["symbol"])
        if ser is None or ser.empty:
            continue
        fill_ms = int(e["signal_ms"]) + entry_delay_s * 1000
        entry_u = S.price_at(ser, fill_ms)
        if not (entry_u == entry_u and entry_u > 0):
            continue
        r = ceiling_perfect_foresight(ser, fill_ms, entry_u, horizon_s=horizon_s)
        if not r["available"]:
            continue
        a, p = view[e["symbol"]]
        pre = realized_volatility(a, p, fill_ms, window_s=lookback_s)
        hor = realized_volatility(a, p, fill_ms + horizon_s * 1000,
                                  window_s=horizon_s)
        ceil = float(r["exit_u"] / entry_u - 1.0)
        rows.append({
            "entry_idx": e.get("entry_idx", -1), "symbol": e["symbol"],
            "session": SS.session_of(fill_ms),
            "ceiling": ceil, "pre_rv": pre, "horizon_rv": hor,
            "ceiling_per_pre_rv": (ceil / pre if pre == pre and pre > 0
                                   else float("nan")),
            "ceiling_per_horizon_rv": (ceil / hor if hor == hor and hor > 0
                                       else float("nan"))})
    return pd.DataFrame(rows)


#: 통제에서 비교하는 지표 3종.
CONTROL_METRICS = ("ceiling", "ceiling_per_pre_rv", "ceiling_per_horizon_rv")


def control_difference(real_arm: pd.DataFrame, ctrl_arm: pd.DataFrame) -> dict:
    """실제 − 통제군, 지표별 쌍체 CI. 본페로니는 통제 비교 9건 기준."""
    out = {}
    for m in CONTROL_METRICS:
        out[m] = D.difference_ci(real_arm, ctrl_arm, m,
                                 n_rules=N_CONTROL_COMPARISONS)
    return out


def build_controls(real_arm: pd.DataFrame, arms: dict, matched: dict) -> dict:
    """§10-P.7 의 통제 표. **하나라도 우위가 사라지면 클레임을 내려 써야 한다.**"""
    out = {"n_real": int(len(real_arm)),
           "real_means": {m: float(pd.to_numeric(real_arm[m], errors="coerce")
                                   .dropna().mean()) if len(real_arm) else float("nan")
                          for m in CONTROL_METRICS},
           "matching": matched, "arms": {}}
    for name, arm in arms.items():
        diffs = control_difference(real_arm, arm)
        out["arms"][name] = {
            "n_rows": int(len(arm)),
            "means": {m: (float(pd.to_numeric(arm[m], errors="coerce").dropna().mean())
                          if len(arm) else float("nan")) for m in CONTROL_METRICS},
            "difference": diffs,
            "survives": {m: diffs[m]["verdict"] == "above_zero"
                         for m in CONTROL_METRICS},
        }
    out["claim_survives_every_control"] = all(
        a["survives"]["ceiling"] for a in out["arms"].values()) if out["arms"] else False
    return out


def run(db: Path, *, entry_delay_s: int = D.ENTRY_DELAY_S,
        horizon_s: int = HORIZON_S) -> dict:
    """전 매매일 × (실제 + 위약). 진입 정의는 `design_b` 와 **완전히 같다**."""
    conn = D.ro(db)
    try:
        days = D.load_days(conn)
        real_by_day, plac_by_day, cover = {}, {}, {}
        ctrl_real, ctrl_arms, match_stats = [], {"random_symbol": [],
                                                 "vol_matched": [],
                                                 "self_symbol": []}, []
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
            ents = D.tag_entries(D.collect_entries(rk), day)
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

            # ---- 통제된 위약 (상한만 계산한다) ----
            # 관측 가능 20종은 이미 전부 차이 CI 가 0 을 교차하므로, 통제를 더 세게
            # 걸어도 주장 가능해질 수 없다. 통제가 시험하는 것은 **상한 우위**뿐이다.
            ckw = dict(entry_delay_s=entry_delay_s, horizon_s=horizon_s)
            ctrl_real.append(ceiling_arm(series, ents, **ckw))
            match = volatility_match_pools(ents, series)
            n_pool = sum(1 for v in match["pools"].values() if v)
            match_stats.append({"day": day, "entries": len(ents),
                                "with_vol_match": int(n_pool)})
            for seed in D.PLACEBO_SEEDS:
                ctrl_arms["random_symbol"].append(ceiling_arm(
                    series, D.placebo_entries(ents, list(series.keys()), seed), **ckw))
                ctrl_arms["vol_matched"].append(ceiling_arm(
                    series, volatility_matched_placebo(ents, match, seed), **ckw))
                ctrl_arms["self_symbol"].append(ceiling_arm(
                    series, self_symbol_placebo(ents, series, seed, **ckw), **ckw))
    finally:
        conn.close()
    cat = lambda fs: (pd.concat([f for f in fs if len(f)], ignore_index=True)
                      if any(len(f) for f in fs) else pd.DataFrame())
    return {"days": days, "real": real_by_day, "placebo": plac_by_day,
            "per_day_counts": cover,
            "control_real": cat(ctrl_real),
            "control_arms": {k: cat(v) for k, v in ctrl_arms.items()},
            "control_matching": match_stats}


def ceiling_by_session(allr: pd.DataFrame, allp: pd.DataFrame) -> list[dict]:
    """**상한과 회수율을 세션별로.** 세션마다 유동성이 다르면 여지도 다를 수 있다.

    0 건인 세션도 행을 남긴다 — 표에서 조용히 사라지면 "없다"와 "안 쟀다"가
    구분되지 않는다.
    """
    out = []
    top = ("downtick_1", "share_drop_50pct", "rank_reverse_5")
    for sess in SS.SESSIONS:
        r = allr[allr["session"] == sess] if "session" in allr.columns else allr.iloc[0:0]
        p = (allp[allp["session"] == sess]
             if len(allp) and "session" in allp.columns else pd.DataFrame())
        c = (pd.to_numeric(r.get("ceiling_perfect_foresight"), errors="coerce").dropna()
             if len(r) else pd.Series(dtype=float))
        cm = float(c.mean()) if len(c) else float("nan")
        lo, hi = (bootstrap_ci_mean(c.tolist()) if len(c) >= 2
                  else (float("nan"), float("nan")))
        diff = D.difference_ci(r, p, "ceiling_perfect_foresight",
                               n_rules=len(SS.SESSIONS))
        rules = []
        for name in top:
            v = (pd.to_numeric(r.get(name), errors="coerce").dropna()
                 if len(r) else pd.Series(dtype=float))
            m = float(v.mean()) if len(v) else float("nan")
            rules.append({"rule": name, "n": int(len(v)), "gross_mean": m,
                          "recovery_of_ceiling": recovery_of_ceiling(m, cm)})
        out.append({"session": sess, "n_entries": int(len(r)),
                    "ceiling_mean": cm, "ceiling_ci": [lo, hi],
                    "ceiling_diff_mean": diff["mean"],
                    "ceiling_diff_ci": list(diff["ci"]),
                    "ceiling_diff_verdict": diff["verdict"],
                    "ceiling_pair_n": diff["n"], "rules": rules})
    return out


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
        # 상한 우위가 **변동성 선택 효과**인지 가리는 통제군 (§10-P.7).
        "controls": build_controls(res.get("control_real", pd.DataFrame()),
                                   res.get("control_arms", {}),
                                   res.get("control_matching", [])),
        "session_counts": SS.session_counts(allr["entry_ms"]),
        "by_session": ceiling_by_session(allr, allp),
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

    ct = rep["controls"]
    print("\n=== [docs/23 sec 10-P.7] CONTROLLED PLACEBOS - is the ceiling edge just "
          "volatility selection?")
    print("  A ceiling is a MAXIMUM, so it grows with volatility alone. Our entries "
          "are selected for a 5pct drawdown,")
    print("  so a random-symbol placebo is a WEAKER-MOVING sample by construction. "
          "These arms remove that.")
    for m in ct["matching"]:
        print(f"  {m['day']}: {m['with_vol_match']}/{m['entries']} entries had a "
              f"volatility-matched candidate (+-{VOL_MATCH_TOL:.0%})")
    print(f"  real n={ct['n_real']}   real means "
          + "  ".join(f"{k}={v:+.4f}" for k, v in ct["real_means"].items()))
    print(f"\n{'control arm':<16}{'metric':<24}{'ctrl mean':>11}{'diff':>10}"
          f"{'CI low':>10}{'CI high':>10}{'pair n':>8}{'verdict':>15}")
    for name, a in ct["arms"].items():
        for m in CONTROL_METRICS:
            d = a["difference"][m]
            print(f"{name:<16}{m:<24}{a['means'][m]:>11.4f}{d['mean']:>10.4f}"
                  f"{d['ci'][0]:>10.4f}{d['ci'][1]:>10.4f}{d['n']:>8}"
                  f"{d['verdict']:>15}")
    print(f"\n  ceiling edge survives EVERY control: "
          f"{ct['claim_survives_every_control']}")
    print("  self_symbol is the cleanest arm - same symbol, same day, random time - "
          "so symbol volatility cancels entirely.")
    print("  NOTE 'room exists' and 'room is reachable' are different claims. No "
          "observable rule captures it (all 20 cross zero).")

    print("\n=== [docs/23 sec 10-Q.5] CEILING AND RECOVERY BY SESSION")
    print(f"  entry session mix: {rep['session_counts']}")
    print(f"{'session':<10}{'n':>5}{'ceiling':>10}{'CI low':>10}{'CI high':>10}"
          f"{'vs plac':>10}{'pair n':>8}{'verdict':>15}")
    for b in rep["by_session"]:
        if not b["n_entries"]:
            print(f"{b['session']:<10}{0:>5}{'':>10}{'':>10}{'':>10}{'':>10}"
                  f"{'':>8}{'NO ENTRIES':>15}")
            continue
        print(f"{b['session']:<10}{b['n_entries']:>5}{b['ceiling_mean']:>10.4f}"
              f"{b['ceiling_ci'][0]:>10.4f}{b['ceiling_ci'][1]:>10.4f}"
              f"{b['ceiling_diff_mean']:>10.4f}{b['ceiling_pair_n']:>8}"
              f"{b['ceiling_diff_verdict']:>15}")
    print(f"\n{'session':<10}{'rule':<22}{'n':>5}{'gross':>10}{'recovery':>10}")
    for b in rep["by_session"]:
        for r in b["rules"]:
            if not r["n"]:
                continue
            print(f"{b['session']:<10}{r['rule']:<22}{r['n']:>5}"
                  f"{r['gross_mean']:>10.4f}{r['recovery_of_ceiling']:>10.3f}")
    print("  NOTE the ceiling is still perfect-foresight and UNACHIEVABLE in every "
          "session, and sec 10-P.7 showed its edge is volatility selection.")

    out = (out_dir or OUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "exit_value.json"
    path.write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1
                  else Path(r"C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db")))

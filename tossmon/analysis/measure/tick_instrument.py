"""**틱 해상도 계측기** (docs/28) — 숫자만 낸다. **판정하지 않는다.**

## 왜 이 계측기가 존재하는가

**우리는 초 단위 현상을 1분봉으로 재고 후보 6개를 닫았다.**

- 사용자 매매는 **1분 미만**, 측정된 슈팅 지속시간 **중앙값 47초**
- 활발한 종목은 **1분봉 한 칸 안에 체결 60~109건**(체결 간격 중앙값 1초 미만)
- 즉 **60개 관측을 숫자 하나로 뭉갠 뒤 "신호 없음"이라고 적었다**

그러므로 앞선 보고들의 정확한 진술은 "성립하지 않는다"가 아니라
**"1분 해상도에서는 관측되지 않는다"** 였다. 이 모듈은 **올바른 해상도의 자를 먼저
만드는 것**이며, **재판정은 여기서 하지 않는다.**

> **이 모듈은 "신호가 있다/없다"를 쓰지 않는다.** 산출물의 모든 수치에는
> **창 길이·판정 방법·오차율·표본 수**가 붙는다. 조건 없는 숫자는 여기서 나가지 않는다.

## 매수/매도 판정과 그 오차 (작업 1)

**틱 규칙**: 직전 체결가보다 높으면 매수 주도, 낮으면 매도 주도, 같으면 **직전 판정 유지**.
호가가 필요 없으므로 **모든 체결**에 적용된다.

**그러나 오차를 모르면 쓸 수 없다.** 호가 폴링은 16초, 체결은 1초 미만이라 호가 하나당
체결이 수십 건 지나간다. 그래서 **체결 직전 `QUOTE_MAX_GAP_S` 안에 호가가 있는 구간만**
골라 호가 기준 판정과 대조하고 **일치율**을 낸다. 일치율은 **상황별로 가른다** —
가격이 움직이는 구간 대 횡보 구간, 스프레드 넓은 종목 대 좁은 종목. 우리 관심은 급등
구간이므로 **거기서의 정확도**가 실제로 쓰이는 숫자다.

## 초 단위 지표 (작업 2)

창은 **5·10·30·60초**. **어느 창에서 값이 유지되는지 자체가 결과다.**

- **체결강도 — 건수가 주(主)**: 사용자 근거 *"주포도 건수로 나눠서 하겠지"*.
  큰 주문은 잘게 쪼개져 나오므로 **건수가 활동을 더 잘 반영**한다. 거래량은 **보조**.
- **매수/매도 비율**: 창별 매수 주도 비율과 **누적 불균형**.
- **흐름 대비 가격 반응**: 흐름 한 단위당 가격이 얼마나 움직이는가.

> ### ⚠ 동시점 반응은 **구조적으로 순환이다**
> 틱 규칙은 **가격 변화로** 매수/매도를 판정한다. 따라서 같은 창의 불균형과 가격 변화는
> **정의상 붙어 있다.** 이 모듈은 동시점 값을 **진단용으로만** 내고
> `mechanically_circular=True` 로 표시하며, **의미 있는 쪽은 다음 창(forward)** 이다.

실행: `python -m tossmon.analysis.measure.tick_instrument [db_path]`
라이브 0콜. DB 는 **읽기 전용**. 콘솔 ASCII.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from tossmon.analysis import session as SS
from tossmon.analysis.measure import design_b as D

#: 초 단위 창. **어느 창에서 값이 유지되는지 자체가 결과다.**
WINDOWS_S = (5, 10, 30, 60)
PRIMARY_WINDOW_S = 10

#: 호가 기준 판정을 시도할 최대 간극. 폴링이 16초라 이 조건을 만족하는 체결은 일부다.
QUOTE_MAX_GAP_S = 2

#: (종목, 사이클)이 이보다 적으면 그 조합은 계측 대상에서 뺀다.
MIN_TRADES_PER_SYMBOL_DAY = 200

#: 판정 대신 **관측 충분성**을 말하기 위한 기준선.
MIN_WINDOWS_FOR_STABLE_READ = 200

#: 흐름 분위 수.
N_FLOW_BUCKETS = 5

OUT_DIR = D.OUT_DIR

#: docs/28 표가 싣는 칸의 필드 전체. 가드가 **동일 집합**으로 대조한다.
REPORTED_FIELDS = (
    "window_s", "session", "n_windows", "n_symbol_days",
    "trades_per_s_median", "trades_per_s_p90",
    "volume_per_s_median", "buy_share_median", "abs_imbalance_median",
    "observed_enough",
)


# --------------------------------------------------------------------------- #
# 1. 매수/매도 판정
# --------------------------------------------------------------------------- #
def tick_classify(price_u: np.ndarray) -> np.ndarray:
    """**틱 규칙** — 직전 체결가보다 높으면 `+1`, 낮으면 `-1`, 같으면 **직전 판정 유지**.

    호가를 쓰지 않으므로 **모든 체결**에 적용된다. 첫 체결과 그 앞의 동일가 구간은
    직전 판정이 없으므로 `0`(미판정)으로 남긴다 — 0 으로 채우지 않고 **미판정으로 센다.**
    """
    p = np.asarray(price_u, dtype="float64")
    out = np.zeros(len(p), dtype="int8")
    prev = 0
    for i in range(1, len(p)):
        if p[i] > p[i - 1]:
            prev = 1
        elif p[i] < p[i - 1]:
            prev = -1
        out[i] = prev
    return out


def quote_classify(price_u, mid_u) -> np.ndarray:
    """**호가 기준 판정** — 중간값보다 위면 매수 주도, 아래면 매도 주도, 같으면 미판정."""
    p = pd.to_numeric(pd.Series(price_u), errors="coerce").to_numpy()
    m = pd.to_numeric(pd.Series(mid_u), errors="coerce").to_numpy()
    out = np.sign(p - m)
    out[~np.isfinite(m) | (m <= 0)] = 0
    return out.astype("int8")


def load_ticks(conn) -> pd.DataFrame:
    """체결 테이프. 홀드아웃을 지나가고 **세션·사이클·수집시기**를 붙인다."""
    tr = pd.read_sql_query(
        "SELECT symbol, ts_ms, price_u, qty_u FROM trades_snap ORDER BY symbol, ts_ms",
        conn)
    got = SS.drop_holdout(tr, "ts_ms")
    d = got["kept"]
    if len(d):
        d = d.copy()
        d["session"] = SS.sessions_of(d["ts_ms"])
        d["cycle_date"] = d["ts_ms"].map(lambda m: SS.session_date(int(m)))
        d["era"] = SS.eras_of(d["ts_ms"])
        d["side_tick"] = (d.groupby("symbol")["price_u"]
                          .transform(lambda s: pd.Series(tick_classify(s.to_numpy()),
                                                         index=s.index)))
    return {"ticks": d, "n_dropped_holdout": got["n_dropped"]}


def load_quotes(conn) -> pd.DataFrame:
    ob = pd.read_sql_query(
        "SELECT symbol, snap_ms, bid1_u, ask1_u FROM orderbook_snap "
        "ORDER BY symbol, snap_ms", conn)
    if ob.empty:
        return ob
    b = pd.to_numeric(ob["bid1_u"], errors="coerce")
    a = pd.to_numeric(ob["ask1_u"], errors="coerce")
    ob["mid_u"] = np.where(a >= b, (a + b) / 2.0, np.nan)
    ob["rel_spread"] = np.where((ob["mid_u"] > 0) & (a >= b),
                                (a - b) / ob["mid_u"], np.nan)
    return ob


def symbol_day_inventory(ticks: pd.DataFrame) -> list[dict]:
    """**표본을 정직하게** — (종목, 사이클)별 체결 수. 부족하면 부족하다고 적는다."""
    if ticks is None or ticks.empty:
        return []
    g = ticks.groupby(["symbol", "cycle_date", "session"]).size().reset_index(
        name="n_trades")
    g["usable"] = g["n_trades"] >= MIN_TRADES_PER_SYMBOL_DAY
    return g.sort_values("n_trades", ascending=False).to_dict("records")


def usable_symbol_days(ticks: pd.DataFrame) -> pd.DataFrame:
    """계측 대상 (종목, 사이클) 만 남긴다."""
    if ticks is None or ticks.empty:
        return ticks
    n = ticks.groupby(["symbol", "cycle_date"])["ts_ms"].transform("size")
    return ticks[n >= MIN_TRADES_PER_SYMBOL_DAY].copy()


def classifier_agreement(ticks: pd.DataFrame, quotes: pd.DataFrame, *,
                         max_gap_s: int = QUOTE_MAX_GAP_S) -> dict:
    """**틱 규칙 대 호가 기준의 일치율**, 그리고 그 조건.

    호가 폴링이 16초라 이 대조가 가능한 체결은 **극히 일부**다. 그 비율을 함께 낸다 —
    일치율만 인용하면 얼마나 좁은 구간의 이야기인지 사라진다.
    """
    if ticks is None or ticks.empty or quotes is None or quotes.empty:
        return {"available": False, "reason": "no ticks or no quotes"}
    books = {s: (g["snap_ms"].to_numpy(), g["mid_u"].to_numpy(),
                 g["rel_spread"].to_numpy())
             for s, g in quotes.groupby("symbol")}
    rows = []
    for sym, g in ticks.groupby("symbol"):
        bk = books.get(sym)
        if bk is None:
            continue
        qts, qmid, qspr = bk
        ts = g["ts_ms"].to_numpy()
        idx = np.searchsorted(qts, ts, side="right") - 1
        ok = (idx >= 0) & (ts - qts[np.clip(idx, 0, None)] <= max_gap_s * 1000) \
             & (ts - qts[np.clip(idx, 0, None)] >= 0)
        if not ok.any():
            continue
        sub = g[ok].copy()
        sub["mid_u"] = qmid[idx[ok]]
        sub["rel_spread"] = qspr[idx[ok]]
        rows.append(sub)
    if not rows:
        return {"available": False, "reason": "no trade had a quote within the gap"}
    d = pd.concat(rows, ignore_index=True)
    d["side_quote"] = quote_classify(d["price_u"], d["mid_u"])
    d = d[(d["side_tick"] != 0) & (d["side_quote"] != 0)]
    if d.empty:
        return {"available": False, "reason": "no trade classified by both methods"}
    d["agree"] = d["side_tick"] == d["side_quote"]
    # 가격이 움직이는 구간 대 횡보 구간 — 직전 10초 체결가 변화로 가른다.
    d = d.sort_values(["symbol", "ts_ms"])
    prev = d.groupby("symbol")["price_u"].shift(1)
    d["moving"] = (d["price_u"] != prev).fillna(False)
    med_spread = float(pd.to_numeric(d["rel_spread"], errors="coerce").median())
    d["wide_spread"] = pd.to_numeric(d["rel_spread"], errors="coerce") > med_spread

    def _cut(sub: pd.DataFrame) -> dict:
        return {"n": int(len(sub)),
                "agreement": float(sub["agree"].mean()) if len(sub) else float("nan")}

    return {
        "available": True, "max_gap_s": max_gap_s,
        "n_trades_total": int(len(ticks)),
        "n_comparable": int(len(d)),
        "comparable_share": float(len(d) / len(ticks)) if len(ticks) else float("nan"),
        "overall": _cut(d),
        "price_moving": _cut(d[d["moving"]]),
        "price_flat": _cut(d[~d["moving"]]),
        "spread_wide": _cut(d[d["wide_spread"]]),
        "spread_narrow": _cut(d[~d["wide_spread"]]),
        "median_rel_spread_at_comparison": med_spread,
        "caveat": ("quote polling is ~16s while trades are sub-second, so this "
                   "agreement describes only the small slice of trades that had a "
                   "fresh quote - it is NOT the accuracy over all trades"),
    }


# --------------------------------------------------------------------------- #
# 2. 초 단위 지표
# --------------------------------------------------------------------------- #
def window_frame(ticks: pd.DataFrame, window_s: int) -> pd.DataFrame:
    """(종목, 사이클, 세션, 수집시기) 안에서 `window_s` 초 버킷으로 접는다.

    **건수가 주 지표**다. 거래량은 보조로만 싣는다.
    """
    if ticks is None or ticks.empty:
        return pd.DataFrame()
    d = ticks.copy()
    d["bucket"] = (d["ts_ms"] // (window_s * 1000)) * (window_s * 1000)
    d["is_buy"] = (d["side_tick"] > 0).astype("int8")
    d["is_sell"] = (d["side_tick"] < 0).astype("int8")
    d["notional"] = pd.to_numeric(d["price_u"], errors="coerce") * \
        pd.to_numeric(d["qty_u"], errors="coerce") / 1e12
    g = d.groupby(["symbol", "cycle_date", "session", "era", "bucket"], sort=True)
    out = g.agg(n_trades=("ts_ms", "size"),
                n_buy=("is_buy", "sum"), n_sell=("is_sell", "sum"),
                volume=("notional", "sum"),
                first_px=("price_u", "first"),
                last_px=("price_u", "last")).reset_index()
    out["window_s"] = window_s
    out["trades_per_s"] = out["n_trades"] / window_s
    out["volume_per_s"] = out["volume"] / window_s
    classified = out["n_buy"] + out["n_sell"]
    out["buy_share"] = np.where(classified > 0, out["n_buy"] / classified, np.nan)
    # **건수 기준 불균형** — 주 지표.
    out["imbalance"] = np.where(classified > 0,
                                (out["n_buy"] - out["n_sell"]) / classified, np.nan)
    out["ret_bp"] = (np.log(out["last_px"] / out["first_px"]) * 1e4).replace(
        [np.inf, -np.inf], np.nan)
    # 다음 창의 수익률 — **순환이 아닌 쪽**.
    key = ["symbol", "cycle_date", "session", "era"]
    nxt_ok = out.groupby(key)["bucket"].shift(-1) == out["bucket"] + window_s * 1000
    out["fwd_ret_bp"] = np.where(nxt_ok, out.groupby(key)["ret_bp"].shift(-1), np.nan)
    return out


def intensity_table(frames: dict) -> list[dict]:
    """창 x 세션의 체결강도·매수비율 요약. **건수가 주 지표임을 표에 남긴다.**"""
    rows = []
    for w, f in frames.items():
        if f is None or f.empty:
            continue
        for sess in SS.SESSIONS:
            d = f[f["session"] == sess]
            if d.empty:
                continue
            rows.append({
                "window_s": w, "session": sess, "n_windows": int(len(d)),
                "n_symbol_days": int(d.groupby(["symbol", "cycle_date"]).ngroups),
                "trades_per_s_median": float(d["trades_per_s"].median()),
                "trades_per_s_p90": float(d["trades_per_s"].quantile(0.90)),
                "volume_per_s_median": float(d["volume_per_s"].median()),
                "buy_share_median": float(
                    pd.to_numeric(d["buy_share"], errors="coerce").dropna().median()),
                "abs_imbalance_median": float(
                    pd.to_numeric(d["imbalance"], errors="coerce").abs().dropna().median()),
                "observed_enough": bool(len(d) >= MIN_WINDOWS_FOR_STABLE_READ)})
    return rows


def window_occupancy(frames: dict) -> list[dict]:
    """**창 하나에 체결이 몇 건 들어오나** — 불균형 비율이 성립하는 창인지 가른다.

    2건짜리 창에서 "매수 비율"은 0, 0.5, 1 세 값뿐이고 불균형은 대부분 +-1 로 포화한다.
    "1분에 60~109건"은 **가장 활발한 종목** 이야기이고 중앙값은 그보다 훨씬 낮다.
    이 표 없이는 아래 불균형 수치를 읽을 수 없다.
    """
    rows = []
    for w, f in frames.items():
        if f is None or f.empty:
            continue
        for sess in SS.SESSIONS:
            d = f[f["session"] == sess]
            if d.empty:
                continue
            n = d["n_trades"]
            rows.append({
                "window_s": w, "session": sess, "n_windows": int(len(d)),
                "trades_per_window_median": float(n.median()),
                "trades_per_window_p90": float(n.quantile(0.90)),
                "share_ge_2": float((n >= 2).mean()),
                "share_ge_5": float((n >= 5).mean()),
                "share_ge_10": float((n >= 10).mean()),
                "imbalance_saturated_share": float(
                    (pd.to_numeric(d["imbalance"], errors="coerce").abs() >= 0.999)
                    .mean())})
    return rows


def flow_price_response(frames: dict, *, n_buckets: int = N_FLOW_BUCKETS) -> list[dict]:
    """**흐름 한 단위당 가격이 얼마나 움직이는가** (사용자가 직접 물은 질문).

    같은 창의 가격 변화는 **틱 규칙이 가격으로 방향을 정하므로 순환**이다. 그래서
    동시점 값은 `mechanically_circular=True` 로 표시하고, **다음 창** 값을 함께 낸다.
    해석(매집이냐 떠넘기기냐)은 **여기서 하지 않는다** — 수치만 낸다.
    """
    rows = []
    for w, f in frames.items():
        if f is None or f.empty:
            continue
        for sess in SS.SESSIONS:
            d = f[(f["session"] == sess)].dropna(subset=["imbalance"])
            if len(d) < MIN_WINDOWS_FOR_STABLE_READ:
                continue
            try:
                q = pd.qcut(d["imbalance"], n_buckets, labels=False,
                            duplicates="drop")
            except (ValueError, IndexError):
                continue
            for b, g in d.assign(q=q).groupby("q"):
                if b != b:
                    continue
                same = pd.to_numeric(g["ret_bp"], errors="coerce").dropna()
                fwd = pd.to_numeric(g["fwd_ret_bp"], errors="coerce").dropna()
                rows.append({
                    "window_s": w, "session": sess, "imbalance_bucket": int(b),
                    "n": int(len(g)),
                    "imbalance_median": float(g["imbalance"].median()),
                    "same_window_ret_bp": (float(same.mean()) if len(same)
                                           else float("nan")),
                    "mechanically_circular": True,
                    "next_window_ret_bp": (float(fwd.mean()) if len(fwd)
                                           else float("nan")),
                    "n_next": int(len(fwd)),
                    "observed_enough": bool(len(fwd) >= MIN_WINDOWS_FOR_STABLE_READ)})
    return rows


def window_stability(response: list[dict], *, session: str = "regular") -> list[dict]:
    """**창을 바꿔도 값이 유지되는가.** 1분봉에서 배운 검사를 그대로 가져온다.

    여기서는 **판정하지 않는다** — 부호가 창마다 달라지는지 **사실만** 싣는다.
    """
    rows = []
    for w in WINDOWS_S:
        d = [r for r in response if r["window_s"] == w and r["session"] == session
             and r["observed_enough"]]
        if not d:
            rows.append({"window_s": w, "n_buckets": 0})
            continue
        top = max(d, key=lambda r: r["imbalance_median"])
        bot = min(d, key=lambda r: r["imbalance_median"])
        rows.append({
            "window_s": w, "n_buckets": len(d),
            "top_bucket_next_ret_bp": top["next_window_ret_bp"],
            "bottom_bucket_next_ret_bp": bot["next_window_ret_bp"],
            "spread_bp": (top["next_window_ret_bp"] - bot["next_window_ret_bp"]
                          if top["next_window_ret_bp"] == top["next_window_ret_bp"]
                          and bot["next_window_ret_bp"] == bot["next_window_ret_bp"]
                          else float("nan"))})
    return rows


def random_time_control(frames: dict, *, seed: int = 20260730) -> list[dict]:
    """**같은 종목·무작위 시각** 대조군. 창 안 불균형 라벨을 종목 내에서 섞는다."""
    rng = np.random.default_rng(seed)
    rows = []
    for w, f in frames.items():
        if f is None or f.empty:
            continue
        d = f[(f["session"] == "regular")].dropna(subset=["imbalance", "fwd_ret_bp"])
        if len(d) < MIN_WINDOWS_FOR_STABLE_READ:
            continue
        shuffled = d.groupby("symbol")["imbalance"].transform(
            lambda s: s.to_numpy()[rng.permutation(len(s))])
        real = float(np.corrcoef(d["imbalance"], d["fwd_ret_bp"])[0, 1])
        plac = float(np.corrcoef(shuffled, d["fwd_ret_bp"])[0, 1])
        rows.append({"window_s": w, "n": int(len(d)),
                     "corr_real": real, "corr_shuffled": plac,
                     "difference": real - plac})
    return rows


def days_needed_for_symbol_days(inventory: list[dict], *,
                                target: int = 30) -> dict:
    """계측 가능한 (종목, 사이클) 조합을 `target` 개 모으려면 며칠인가."""
    usable = [r for r in inventory if r["usable"]]
    cycles = len({r["cycle_date"] for r in inventory}) or 1
    per_day = len(usable) / cycles
    if per_day <= 0:
        return {"reachable": False, "reason": "no usable symbol-days yet"}
    return {"reachable": True, "usable_now": len(usable),
            "cycles_observed": cycles, "usable_per_cycle": per_day,
            "target": target,
            "cycles_needed": int(np.ceil(target / per_day))}


def build_report(conn) -> dict:
    got = load_ticks(conn)
    ticks = got["ticks"]
    quotes = load_quotes(conn)
    inv = symbol_day_inventory(ticks)
    usable = usable_symbol_days(ticks)
    frames = {w: window_frame(usable, w) for w in WINDOWS_S}
    resp = flow_price_response(frames)
    return {
        "measurement_conditions": {
            "windows_s": list(WINDOWS_S),
            "primary_window_s": PRIMARY_WINDOW_S,
            "side_rule": "tick rule (uptick=buy, downtick=sell, equal=carry forward)",
            "quote_max_gap_s": QUOTE_MAX_GAP_S,
            "min_trades_per_symbol_day": MIN_TRADES_PER_SYMBOL_DAY,
            "intensity_primary": "trade COUNT (volume is secondary)",
        },
        "holdout": {"start": SS.HOLDOUT_START, "end": SS.HOLDOUT_END,
                    "n_rows_dropped": got["n_dropped_holdout"]},
        "collector_eras": list(SS.COLLECTOR_ERAS),
        "n_ticks": int(len(ticks)),
        "n_ticks_usable": int(len(usable)),
        "era_counts": (usable["era"].value_counts().to_dict() if len(usable) else {}),
        "unclassified_share": (float((ticks["side_tick"] == 0).mean())
                               if len(ticks) else float("nan")),
        "symbol_day_inventory": inv[:40],
        "n_symbol_days_total": len(inv),
        "n_symbol_days_usable": sum(1 for r in inv if r["usable"]),
        "days_needed": days_needed_for_symbol_days(inv),
        "classifier_agreement": classifier_agreement(ticks, quotes),
        "intensity": intensity_table(frames),
        "window_occupancy": window_occupancy(frames),
        "flow_price_response": resp,
        "window_stability": window_stability(resp),
        "random_time_control": random_time_control(frames),
        "no_verdict_note": ("this module reports measurements only - it does not "
                            "state whether a signal exists; every number here is "
                            "conditional on window length, side rule, agreement rate "
                            "and sample size"),
    }


def main(db: Path, *, out_dir: Path | None = None) -> int:
    conn = D.ro(db)
    try:
        rep = build_report(conn)
    finally:
        conn.close()
    mc = rep["measurement_conditions"]
    print(f"holdout dropped {rep['holdout']['n_rows_dropped']} tick rows")
    print(f"ticks {rep['n_ticks']} -> usable {rep['n_ticks_usable']} "
          f"(symbol-days with >= {mc['min_trades_per_symbol_day']} trades)")
    print(f"  symbol-days: {rep['n_symbol_days_usable']} usable of "
          f"{rep['n_symbol_days_total']}")
    print(f"  collector eras present: {rep['era_counts']} "
          f"(never pooled across a boundary)")
    print(f"  unclassified by tick rule: {rep['unclassified_share']:.1%} "
          f"(counted, not filled)")
    print(f"  {rep['no_verdict_note']}")

    print("\n=== [docs/28 sec 2] SIDE CLASSIFIER AGREEMENT (tick rule vs quote)")
    ag = rep["classifier_agreement"]
    if ag.get("available"):
        print(f"  comparable trades {ag['n_comparable']} of {ag['n_trades_total']} "
              f"({ag['comparable_share']:.2%}) - quote within {ag['max_gap_s']}s")
        for k in ("overall", "price_moving", "price_flat", "spread_wide",
                  "spread_narrow"):
            c = ag[k]
            print(f"    {k:<14} n={c['n']:>7}  agreement {c['agreement']:.3f}")
        print(f"  !! {ag['caveat']}")
    else:
        print(f"  unavailable: {ag.get('reason')}")

    print("\n=== [docs/28 sec 3] SECOND-SCALE INTENSITY (COUNT is primary)")
    print(f"{'win_s':>6}{'session':<9}{'windows':>9}{'sym-days':>10}"
          f"{'trades/s med':>13}{'trades/s p90':>13}{'vol/s med':>11}"
          f"{'buy share':>11}{'|imb| med':>11}{'enough':>8}")
    for r in rep["intensity"]:
        print(f"{r['window_s']:>6}{r['session']:<9}{r['n_windows']:>9}"
              f"{r['n_symbol_days']:>10}{r['trades_per_s_median']:>13.2f}"
              f"{r['trades_per_s_p90']:>13.2f}{r['volume_per_s_median']:>11.1f}"
              f"{r['buy_share_median']:>11.3f}{r['abs_imbalance_median']:>11.3f}"
              f"{str(r['observed_enough']):>8}")

    print("\n=== [docs/28 sec 3b] WINDOW OCCUPANCY - is the ratio even defined here?")
    print(f"{'win_s':>6}{'session':<9}{'trades/win med':>15}{'p90':>7}"
          f"{'>=2':>7}{'>=5':>7}{'>=10':>7}{'|imb|=1':>9}")
    for r in rep["window_occupancy"]:
        print(f"{r['window_s']:>6}{r['session']:<9}"
              f"{r['trades_per_window_median']:>15.1f}"
              f"{r['trades_per_window_p90']:>7.1f}{r['share_ge_2']:>7.2f}"
              f"{r['share_ge_5']:>7.2f}{r['share_ge_10']:>7.2f}"
              f"{r['imbalance_saturated_share']:>9.2f}")
    print("  '|imb|=1' is the share of windows where every classified trade was on one "
          "side - the ratio saturates and carries little information there.")

    print("\n=== [docs/28 sec 4] FLOW vs PRICE RESPONSE (regular session)")
    print("  same-window response is MECHANICALLY CIRCULAR - the tick rule sets side "
          "from price - so read the next-window column")
    print(f"{'win_s':>6}{'bucket':>7}{'n':>8}{'imbalance':>11}"
          f"{'same-win bp':>12}{'next-win bp':>12}{'n next':>8}{'enough':>8}")
    for r in rep["flow_price_response"]:
        if r["session"] != "regular":
            continue
        print(f"{r['window_s']:>6}{r['imbalance_bucket']:>7}{r['n']:>8}"
              f"{r['imbalance_median']:>11.3f}{r['same_window_ret_bp']:>12.2f}"
              f"{r['next_window_ret_bp']:>12.2f}{r['n_next']:>8}"
              f"{str(r['observed_enough']):>8}")

    print("\n=== [docs/28 sec 5] WINDOW STABILITY (fact only - no verdict here)")
    for r in rep["window_stability"]:
        if not r.get("n_buckets"):
            continue
        print(f"  window {r['window_s']:>3}s: top-bucket next {r['top_bucket_next_ret_bp']:+.2f} bp, "
              f"bottom {r['bottom_bucket_next_ret_bp']:+.2f} bp, "
              f"spread {r['spread_bp']:+.2f} bp")

    print("\n=== [docs/28 sec 6] SAME-SYMBOL SHUFFLED CONTROL")
    for r in rep["random_time_control"]:
        print(f"  window {r['window_s']:>3}s: n={r['n']:>7} corr(real) "
              f"{r['corr_real']:+.4f}  corr(shuffled) {r['corr_shuffled']:+.4f}  "
              f"difference {r['difference']:+.4f}")

    print("\n=== [docs/28 sec 7] SAMPLE SUFFICIENCY")
    dn = rep["days_needed"]
    if dn.get("reachable"):
        print(f"  usable symbol-days now {dn['usable_now']} over "
              f"{dn['cycles_observed']} cycles ({dn['usable_per_cycle']:.1f}/cycle)"
              f" -> {dn['cycles_needed']} cycles for {dn['target']}")
    else:
        print(f"  {dn.get('reason')}")

    out = (out_dir or OUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "tick_instrument.json"
    path.write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1
                  else Path(r"C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db")))

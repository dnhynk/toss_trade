"""**체결 테이프로 잰 실측 거래비용** (docs/23 §10-R) — 걸어 내려가기를 대체한다.

## 왜 다시 재는가 — 걸어 내려가기의 근거가 무너졌다

W1 프로브가 두 가지를 확인했고, **둘 다 우리 비용 모델의 토대를 무너뜨린다**:

1. **정규장은 API 가 L1 만 준다.** 깊이 파라미터 6종이 조용히 무시된다(200 OK).
   수집기 결함이 아니라 근본 제약이다. 주간거래의 10레벨은 **토스 대체거래소 장부**로
   보이고 정규장은 **미국 통합시세(NBBO) 재배포**다 — **다른 장부다.**
   따라서 **주간거래에서 잰 깊이 통계를 정규장에 옮겨 쓸 수 없다.**
2. **표시 잔량이 실제 체결 가능량을 크게 과소표시한다.** 프로브에서 AAPL 매도 1주가
   왔다(iceberg·hidden·odd-lot). **호가를 걸어 내려가는 비용 모델은 이 시장에서
   신뢰할 수 없다.**

그래서 `docs/21` 의 클립별 비용(2.38/4.45/4.91/6.44%)과 §10-Q 의 세션별 비용(6.58%)은
**전부 걸어 내려가기 산물**이며 근거가 약하다. 이 러너는 **실제로 체결된 값**으로
다시 잰다 — 버리는 게 아니라 **나란히 놓고 차이를 보여준다.**

## 유효 스프레드 정의 — **2배 인자를 여기에 못 박는다**

    유효 스프레드 = 2 x |P - M| / M

`P` 는 체결가, `M` 은 **체결 직전** L1 중간값이다. 2배인 이유: 한 번 가로지르면
중간값 대비 `|P-M|/M` 을 지불하고, **왕복이면 양쪽에서 두 번** 지불하기 때문이다.
그래서 이 값은 `execution.round_trip_cost(exit_mode="cross")` **에서 수수료를 뺀 것과
같은 자리**의 수치이며, 그렇게 비교해야 한다.

- **지정가로 걸어서 벌면 부호가 반대**가 된다(스프레드를 지불하지 않고 받는다).
  이 러너는 **가로지르는 쪽만** 재므로 결과는 **비관 쪽 상한**이 아니라 **실제로
  지불된 값**이다.
- 이 프로젝트에서 이미 인자 실수(100배 `vol_qu`)가 났으므로 **테스트가 2배를 고정**한다.

## 시각 정합 규율 (W1 경고)

호가 폴링 주기(실측 중앙값 **약 16초**)와 체결 시각 사이에 간극이 있다. 간극이 크면
그 사이의 가격 이동이 **스프레드로 오인**된다. 그래서 **체결 직전 L1 이 `MAX_ALIGN_GAP_S`
안에 있는 체결만** 쓰고, **버린 수를 세어 보고한다.** 간극 민감도도 같이 낸다.

## 편향 — 반드시 같이 읽어야 한다

`trades_snap` 은 **tier3 승격 종목만 조밀하게** 담는다. 즉 표본은 "우리가 감시하기로
결정한 종목"에 치우쳐 있고, **승격되지 않은 종목의 큰 클립 비용은 여전히 미관측**이다.

실행: `python -m tossmon.analysis.measure.tape_cost [db_path]`
라이브 0콜. DB 는 **읽기 전용**. 콘솔 ASCII.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from tossmon.analysis import execution as X
from tossmon.analysis import session as SS
from tossmon.analysis.measure import design_b as D

#: **유효 스프레드의 2배 인자.** 왕복이면 양쪽에서 지불하므로 2다. 테스트가 고정한다.
EFFECTIVE_SPREAD_FACTOR = 2.0

#: 체결 직전 L1 이 이 시간 안에 있어야 쓴다. 폴링 중앙값(약 16초)보다 조금 크게 잡되,
#: 넓히면 가격 이동이 스프레드로 오염되므로 민감도를 함께 낸다.
MAX_ALIGN_GAP_S = 20
GAP_SENSITIVITY_S = (5, 15, 20, 30, 60)

#: 영구 충격을 재는 지평. 체결 뒤 중간값이 어디로 갔는가.
IMPACT_HORIZON_S = 60

#: 노셔널 구간 (USD). 사용자가 "무리 없었다"고 한 $2,000 이 자기 칸을 갖는다.
NOTIONAL_BUCKETS = ((100, 500), (500, 1000), (1000, 2000), (2000, 5000),
                    (5000, float("inf")))

#: 세션별 비용표에서 채울 클립. 각 클립은 자기를 포함하는 구간에서 읽는다.
CLIP_TARGETS = (100, 500, 1000, 2000)

#: 헤드라인 가격 대역 (docs/21·§10-Q 와 같은 잣대).
HEADLINE_BAND = "$2-5"

#: 이보다 적으면 판정하지 않는다.
MIN_N_FOR_VERDICT = 30

#: 종목 내 대조에 쓰려면 그 종목이 두 구간 모두에서 이만큼은 있어야 한다.
MIN_TRADES_PER_SYMBOL_BUCKET = 3

OUT_DIR = D.OUT_DIR

#: docs/23 §10-R 이 싣는 칸의 필드 전체. 가드가 **동일 집합**으로 대조한다.
REPORTED_FIELDS = (
    "session", "bucket", "n", "n_symbols",
    "effective_spread_median", "effective_spread_p25", "effective_spread_p75",
    "impact_median", "pre_trade_vol_median", "powered",
)


def trade_notional_usd(price_u, qty_u):
    """체결 노셔널(USD). 단위 계약 C-2: `price_u` 는 마이크로USD, `qty_u` 는 마이크로주."""
    return (pd.to_numeric(price_u, errors="coerce")
            * pd.to_numeric(qty_u, errors="coerce")) / 1e12


def effective_spread(price_u, mid_u):
    """**유효 스프레드 = 2 x |P - M| / M.**

    2배는 왕복(양쪽 가로지르기) 기준이라는 뜻이다. 이 값은 수수료를 **포함하지 않는다**.
    """
    p = pd.to_numeric(price_u, errors="coerce")
    m = pd.to_numeric(mid_u, errors="coerce")
    out = EFFECTIVE_SPREAD_FACTOR * (p - m).abs() / m
    return out.where(m > 0) if hasattr(out, "where") else out


def trade_direction(price_u, mid_u):
    """중간값 대비 체결가 부호 — `+1` 매수주도, `-1` 매도주도, `0` 중간값 체결."""
    p = pd.to_numeric(price_u, errors="coerce")
    m = pd.to_numeric(mid_u, errors="coerce")
    return np.sign(p - m)


def bucket_of(usd):
    """노셔널 -> 구간 이름. 구간 밖(<$100)은 `None`."""
    for lo, hi in NOTIONAL_BUCKETS:
        if lo <= usd < hi:
            return f"${lo:.0f}-{'inf' if hi == float('inf') else f'{hi:.0f}'}"
    return None


def bucket_for_clip(clip: int) -> str:
    """클립 금액을 **그 금액을 포함하는 구간**으로 읽는다."""
    return bucket_of(float(clip))


def load_l1(conn) -> pd.DataFrame:
    """L1 호가. **정규장에도 온다** — 이게 이 측정의 핵심 이득이다."""
    ob = pd.read_sql_query(
        "SELECT symbol, snap_ms, bid1_u, bid1_qu, ask1_u, ask1_qu "
        "FROM orderbook_snap ORDER BY symbol, snap_ms", conn)
    if ob.empty:
        return ob
    ob["mid_u"] = (pd.to_numeric(ob["bid1_u"], errors="coerce")
                   + pd.to_numeric(ob["ask1_u"], errors="coerce")) / 2.0
    # 크로스/락 호가는 중간값이 의미를 잃는다 — 조용히 섞지 않고 뺀다.
    bad = pd.to_numeric(ob["ask1_u"], errors="coerce") < pd.to_numeric(
        ob["bid1_u"], errors="coerce")
    ob.loc[bad, "mid_u"] = np.nan
    return ob


def load_tape(conn) -> pd.DataFrame:
    tr = pd.read_sql_query(
        "SELECT symbol, ts_ms, price_u, qty_u FROM trades_snap ORDER BY symbol, ts_ms",
        conn)
    if tr.empty:
        return tr
    tr["notional_usd"] = trade_notional_usd(tr["price_u"], tr["qty_u"])
    tr["price_usd"] = pd.to_numeric(tr["price_u"], errors="coerce") / 1e6
    tr["band"] = tr["price_usd"].map(X.price_band)
    tr["session"] = SS.sessions_of(tr["ts_ms"])
    return tr


def align_trades_to_l1(tape: pd.DataFrame, l1: pd.DataFrame, *,
                       max_gap_s: int = MAX_ALIGN_GAP_S,
                       impact_horizon_s: int = IMPACT_HORIZON_S) -> dict:
    """체결마다 **직전** L1 을 붙인다. 간극 초과는 **버리고 센다.**

    직전 호가만 쓴다(체결 이후 호가를 쓰면 미래를 보게 된다). 충격 측정용
    `mid_after` 만 체결 **이후** 호가에서 가져오며, 그것은 사후 관측이 목적이다.
    """
    if tape is None or tape.empty or l1 is None or l1.empty:
        return {"aligned": pd.DataFrame(), "n_trades": int(len(tape) if tape is not None else 0),
                "n_aligned": 0, "n_dropped_no_quote": int(len(tape) if tape is not None else 0),
                "max_gap_s": max_gap_s}
    books = {s: (g["snap_ms"].to_numpy(),
                 g["mid_u"].to_numpy(),
                 pd.to_numeric(g["bid1_qu"], errors="coerce").to_numpy(),
                 pd.to_numeric(g["ask1_qu"], errors="coerce").to_numpy())
             for s, g in l1.groupby("symbol")}
    rows, dropped = [], 0
    for r in tape.itertuples(index=False):
        bk = books.get(r.symbol)
        if bk is None:
            dropped += 1
            continue
        ts, mid, bq, aq = bk
        i = int(np.searchsorted(ts, r.ts_ms, side="right")) - 1
        if i < 0 or not (0 <= r.ts_ms - ts[i] <= max_gap_s * 1000):
            dropped += 1
            continue
        m = mid[i]
        if not (m == m and m > 0):
            dropped += 1
            continue
        j = int(np.searchsorted(ts, r.ts_ms + impact_horizon_s * 1000, side="left"))
        mid_after = (mid[j] if j < len(ts)
                     and ts[j] - (r.ts_ms + impact_horizon_s * 1000) <= max_gap_s * 1000
                     else np.nan)
        rows.append({"symbol": r.symbol, "ts_ms": int(r.ts_ms),
                     "session": r.session, "band": r.band,
                     "notional_usd": float(r.notional_usd),
                     "price_u": float(r.price_u), "mid_u": float(m),
                     "gap_ms": int(r.ts_ms - ts[i]),
                     "mid_after_u": float(mid_after) if mid_after == mid_after else np.nan,
                     "bid1_qu_before": float(bq[i]), "ask1_qu_before": float(aq[i]),
                     "bid1_qu_after": float(bq[i + 1]) if i + 1 < len(ts) else np.nan,
                     "ask1_qu_after": float(aq[i + 1]) if i + 1 < len(ts) else np.nan})
    df = pd.DataFrame(rows)
    if len(df):
        df["eff_spread"] = effective_spread(df["price_u"], df["mid_u"])
        df["direction"] = trade_direction(df["price_u"], df["mid_u"])
        df["impact"] = (df["direction"]
                        * (df["mid_after_u"] - df["mid_u"]) / df["mid_u"])
        df["bucket"] = df["notional_usd"].map(bucket_of)
    return {"aligned": df, "n_trades": int(len(tape)), "n_aligned": int(len(df)),
            "n_dropped_no_quote": int(dropped), "max_gap_s": max_gap_s,
            "aligned_rate": float(len(df) / len(tape)) if len(tape) else float("nan")}


def gap_sensitivity(tape: pd.DataFrame, l1: pd.DataFrame, *,
                    gaps=GAP_SENSITIVITY_S, band: str = HEADLINE_BAND) -> list[dict]:
    """정합 간극을 넓히면 유효 스프레드가 어떻게 움직이는가.

    넓힐수록 표본은 늘지만 **가격 이동이 스프레드로 오염**된다. 값이 간극에 따라
    크게 흔들리면 그 사실 자체가 결과다.
    """
    out = []
    t = tape[tape["band"] == band] if len(tape) else tape
    for g in gaps:
        a = align_trades_to_l1(t, l1, max_gap_s=g)["aligned"]
        v = (pd.to_numeric(a["eff_spread"], errors="coerce").dropna()
             if len(a) else pd.Series(dtype=float))
        out.append({"max_gap_s": g, "n": int(len(v)),
                    "effective_spread_median": float(v.median()) if len(v) else float("nan")})
    return out


def realized_cost_table(aligned: pd.DataFrame, *,
                        band: str | None = HEADLINE_BAND) -> list[dict]:
    """세션 x 노셔널 구간의 **실측** 유효 스프레드·충격."""
    if aligned is None or aligned.empty:
        return []
    d0 = aligned if band is None else aligned[aligned["band"] == band]
    rows = []
    for sess in SS.SESSIONS:
        for lo, hi in NOTIONAL_BUCKETS:
            name = f"${lo:.0f}-{'inf' if hi == float('inf') else f'{hi:.0f}'}"
            d = d0[(d0["session"] == sess) & (d0["bucket"] == name)]
            v = pd.to_numeric(d.get("eff_spread"), errors="coerce").dropna() if len(d) else pd.Series(dtype=float)
            imp = pd.to_numeric(d.get("impact"), errors="coerce").dropna() if len(d) else pd.Series(dtype=float)
            pv = pd.to_numeric(d.get("pre_trade_vol"), errors="coerce").dropna() if len(d) else pd.Series(dtype=float)
            rows.append({
                "session": sess, "bucket": name, "n": int(len(v)),
                "n_symbols": int(d["symbol"].nunique()) if len(d) else 0,
                "effective_spread_median": float(v.median()) if len(v) else float("nan"),
                "effective_spread_p25": float(v.quantile(.25)) if len(v) else float("nan"),
                "effective_spread_p75": float(v.quantile(.75)) if len(v) else float("nan"),
                "impact_median": float(imp.median()) if len(imp) else float("nan"),
                "pre_trade_vol_median": float(pv.median()) if len(pv) else float("nan"),
                "powered": bool(len(v) >= MIN_N_FOR_VERDICT)})
    return rows


def add_pre_trade_volatility(aligned: pd.DataFrame, l1: pd.DataFrame, *,
                             window_s: int = 300) -> pd.DataFrame:
    """체결 **직전** 변동성 — 크기별 비교의 교란을 드러내기 위한 통제 변수.

    큰 체결이 원래 요동치던 순간에 몰려 있으면 "큰 클립이 비싸다"가 아니라
    "요동칠 때 크게 친다"일 수 있다. 상한 사건(§10-P.7)과 같은 함정이므로
    **통제 없이 결론을 쓰지 않는다.**
    """
    if aligned is None or aligned.empty or l1 is None or l1.empty:
        return aligned
    books = {s: (g["snap_ms"].to_numpy(), g["mid_u"].to_numpy())
             for s, g in l1.groupby("symbol")}
    vals = []
    for r in aligned.itertuples(index=False):
        bk = books.get(r.symbol)
        if bk is None:
            vals.append(np.nan)
            continue
        ts, mid = bk
        lo = int(np.searchsorted(ts, r.ts_ms - window_s * 1000, side="left"))
        hi = int(np.searchsorted(ts, r.ts_ms, side="right"))
        w = mid[lo:hi]
        w = w[(w == w) & (w > 0)]
        vals.append(float(np.std(np.diff(np.log(w)), ddof=1)) if len(w) >= 3 else np.nan)
    out = aligned.copy()
    out["pre_trade_vol"] = vals
    return out


def within_symbol_size_contrast(aligned: pd.DataFrame, *,
                                small: str = "$100-500",
                                large: str = "$2000-5000",
                                band: str | None = HEADLINE_BAND) -> dict:
    """**사용자 직감의 직접 검정** — 같은 종목·같은 세션에서 크기만 다른 체결 비교.

    큰 체결이 일어나는 순간이 원래 가격이 움직이는 순간일 수 있으므로(역인과),
    종목과 세션을 고정하고 **크기만** 바꿔 짝을 맺는다. 짝이 맺힌 종목 수를 함께 낸다.
    """
    if aligned is None or aligned.empty:
        return {"available": False, "reason": "no aligned trades"}
    d = aligned if band is None else aligned[aligned["band"] == band]
    out = {"available": True, "small": small, "large": large, "by_session": {}}
    for sess in SS.SESSIONS:
        s0 = d[(d["session"] == sess) & (d["bucket"] == small)]
        s1 = d[(d["session"] == sess) & (d["bucket"] == large)]
        if not len(s0) or not len(s1):
            out["by_session"][sess] = {"n_symbols": 0, "powered": False}
            continue
        a = s0.groupby("symbol").agg(n=("eff_spread", "size"),
                                     sp=("eff_spread", "median"),
                                     vol=("pre_trade_vol", "median"))
        b = s1.groupby("symbol").agg(n=("eff_spread", "size"),
                                     sp=("eff_spread", "median"),
                                     vol=("pre_trade_vol", "median"))
        a = a[a["n"] >= MIN_TRADES_PER_SYMBOL_BUCKET]
        b = b[b["n"] >= MIN_TRADES_PER_SYMBOL_BUCKET]
        common = a.index.intersection(b.index)
        if len(common) == 0:
            out["by_session"][sess] = {"n_symbols": 0, "powered": False}
            continue
        delta = (b.loc[common, "sp"] - a.loc[common, "sp"]).dropna()
        dvol = (b.loc[common, "vol"] - a.loc[common, "vol"]).dropna()
        out["by_session"][sess] = {
            "n_symbols": int(len(common)),
            "median_small": float(a.loc[common, "sp"].median()),
            "median_large": float(b.loc[common, "sp"].median()),
            "median_within_symbol_delta": float(delta.median()) if len(delta) else float("nan"),
            "median_pre_trade_vol_delta": float(dvol.median()) if len(dvol) else float("nan"),
            "powered": bool(len(common) >= 10)}
    return out


def l1_refill(aligned: pd.DataFrame) -> list[dict]:
    """체결 직후 L1 잔량이 얼마나 남아 있나 (다음 스냅 / 직전 스냅).

    작은 클립에는 정적 깊이보다 **재충전 속도**가 결정적이다. 다만 폴링이 약 16초라
    **한 스냅 뒤**밖에 못 본다 — 그보다 빠른 회복은 보이지 않는다. 그렇게 적는다.
    """
    if aligned is None or aligned.empty:
        return []
    d = aligned.copy()
    for side in ("bid", "ask"):
        b = pd.to_numeric(d[f"{side}1_qu_before"], errors="coerce")
        a = pd.to_numeric(d[f"{side}1_qu_after"], errors="coerce")
        d[f"{side}_ratio"] = a.where(b > 0) / b.where(b > 0)
    rows = []
    for sess in SS.SESSIONS:
        s = d[d["session"] == sess]
        if not len(s):
            rows.append({"session": sess, "n": 0})
            continue
        rows.append({
            "session": sess, "n": int(len(s)),
            "bid_qty_ratio_median": float(pd.to_numeric(s["bid_ratio"], errors="coerce").dropna().median()),
            "ask_qty_ratio_median": float(pd.to_numeric(s["ask_ratio"], errors="coerce").dropna().median()),
            "depleted_rate": float((pd.to_numeric(s["ask_ratio"], errors="coerce") < 1.0).mean())})
    return rows


def tape_vs_walk_the_book(realized: list[dict], walk: dict) -> list[dict]:
    """**실측 대 걸어 내려가기를 나란히.** 어느 쪽이 크든 그대로 낸다."""
    out = []
    for clip in CLIP_TARGETS:
        bucket = bucket_for_clip(clip)
        for sess in SS.SESSIONS:
            r = next((x for x in realized
                      if x["session"] == sess and x["bucket"] == bucket), None)
            w = (walk.get(sess, {}).get("by_clip", {}).get(clip, {})
                 if walk else {})
            tape_cost = (r["effective_spread_median"] + D.COMMISSION_ROUND_TRIP
                         if r and r["powered"] else float("nan"))
            out.append({
                "session": sess, "clip": clip, "bucket": bucket,
                "tape_n": (r["n"] if r else 0),
                "tape_round_trip": tape_cost,
                "walk_round_trip": (float(w["median"])
                                    if w and w.get("measurable") else float("nan")),
                "walk_measurable": bool(w.get("measurable")) if w else False})
    return out


def build_report(conn) -> dict:
    """§10-R 의 표를 한 자료구조로. `main()` 과 테스트가 같은 함수를 쓴다."""
    tape, l1 = load_tape(conn), load_l1(conn)
    al = align_trades_to_l1(tape, l1)
    aligned = add_pre_trade_volatility(al["aligned"], l1)
    walk = D.session_clip_costs(D.book_rows(conn))
    realized = realized_cost_table(aligned)
    return {
        "band": HEADLINE_BAND,
        "effective_spread_factor": EFFECTIVE_SPREAD_FACTOR,
        "alignment": {k: v for k, v in al.items() if k != "aligned"},
        "gap_sensitivity": gap_sensitivity(tape, l1),
        "trades_by_session": SS.session_counts(tape["ts_ms"]) if len(tape) else {},
        "realized": realized,
        "size_contrast": within_symbol_size_contrast(aligned),
        "refill": l1_refill(aligned),
        "comparison": tape_vs_walk_the_book(realized, walk),
        "bias": ("trades_snap is dense only for tier3-promoted symbols, so these "
                 "costs describe symbols we already chose to watch; large-clip cost "
                 "for un-promoted symbols remains UNOBSERVED"),
    }


def main(db: Path, *, out_dir: Path | None = None) -> int:
    conn = D.ro(db)
    try:
        rep = build_report(conn)
    finally:
        conn.close()
    al = rep["alignment"]
    print(f"trades {al['n_trades']}  aligned {al['n_aligned']} "
          f"({al.get('aligned_rate', float('nan')):.1%})  "
          f"dropped_no_quote {al['n_dropped_no_quote']}  "
          f"max_gap {al['max_gap_s']}s")
    print(f"  trades by session: {rep['trades_by_session']}")
    print(f"  effective spread = {rep['effective_spread_factor']} x |P - M| / M "
          f"(round-trip convention; commission NOT included)")

    print("\n=== [docs/23 sec 10-R.1] ALIGNMENT GAP SENSITIVITY (band "
          f"{rep['band']})")
    for g in rep["gap_sensitivity"]:
        print(f"  max_gap {g['max_gap_s']:>3}s  n={g['n']:>6}  "
              f"eff_spread median {g['effective_spread_median']:.4f}")
    print("  widening the gap adds sample but lets price drift contaminate the "
          "spread - read the trend, not one cell.")

    print(f"\n=== [docs/23 sec 10-R.2] REALIZED EFFECTIVE SPREAD by session x notional "
          f"(band {rep['band']})")
    print(f"{'session':<9}{'bucket':<14}{'n':>6}{'syms':>6}{'median':>9}{'p25':>9}"
          f"{'p75':>9}{'impact':>9}{'pre-vol':>9}")
    for r in rep["realized"]:
        if not r["n"]:
            continue
        tag = "" if r["powered"] else "  (n<30, no verdict)"
        print(f"{r['session']:<9}{r['bucket']:<14}{r['n']:>6}{r['n_symbols']:>6}"
              f"{r['effective_spread_median']:>9.4f}{r['effective_spread_p25']:>9.4f}"
              f"{r['effective_spread_p75']:>9.4f}{r['impact_median']:>9.4f}"
              f"{r['pre_trade_vol_median']:>9.4f}{tag}")

    print("\n=== [docs/23 sec 10-R.3] SIZE CONTRAST WITHIN SYMBOL - the user's $2,000 question")
    sc = rep["size_contrast"]
    if sc.get("available"):
        print(f"  comparing {sc['large']} against {sc['small']}, same symbol and session")
        for sess, v in sc["by_session"].items():
            if not v["n_symbols"]:
                continue
            tag = "" if v["powered"] else "  (UNDERPOWERED n<10)"
            print(f"  {sess:<9} n_symbols={v['n_symbols']:<4} "
                  f"small={v['median_small']:.4f}  large={v['median_large']:.4f}  "
                  f"delta={v['median_within_symbol_delta']:+.4f}  "
                  f"(pre-trade vol delta {v['median_pre_trade_vol_delta']:+.4f}){tag}")
        print("  pre-trade vol delta > 0 means the large trades also happened in more "
              "volatile moments - do NOT read the spread delta as pure size cost.")

    print("\n=== [docs/23 sec 10-R.4] L1 DEPLETION / REFILL (next snapshot vs prior)")
    for r in rep["refill"]:
        if not r["n"]:
            continue
        print(f"  {r['session']:<9} n={r['n']:>6}  bid_qty_ratio "
              f"{r['bid_qty_ratio_median']:.2f}  ask_qty_ratio "
              f"{r['ask_qty_ratio_median']:.2f}  depleted_rate {r['depleted_rate']:.2f}")
    print("  polling is ~16s, so anything faster than one snapshot is invisible here.")

    print("\n=== [docs/23 sec 10-R.5] TAPE vs WALK-THE-BOOK round trip (commission included "
          "for tape)")
    print(f"{'session':<9}{'clip':>7}{'bucket':<14}{'tape n':>8}{'tape':>10}{'walk':>10}")
    for c in rep["comparison"]:
        if not c["tape_n"] and not c["walk_measurable"]:
            continue
        tape = f"{c['tape_round_trip']:.4f}" if c["tape_round_trip"] == c["tape_round_trip"] else "n/a"
        walk = f"{c['walk_round_trip']:.4f}" if c["walk_round_trip"] == c["walk_round_trip"] else "n/a"
        print(f"{c['session']:<9}{c['clip']:>7}{c['bucket']:<14}{c['tape_n']:>8}"
              f"{tape:>10}{walk:>10}")
    print(f"\n  BIAS: {rep['bias']}")

    out = (out_dir or OUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "tape_cost.json"
    path.write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1
                  else Path(r"C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db")))

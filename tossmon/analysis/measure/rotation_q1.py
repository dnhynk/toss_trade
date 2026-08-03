"""회전 논제 **Q0·Q1** (docs/26, 사용자 승인 설계) — 점유율이 가격을 앞서는가.

## 이 러너가 답하는 것

사용자 논제는 **타이밍이 아니라 선택** 문제다: *"언제 사나"* 가 아니라
***"어느 종목에 있어야 하나"***. 개미 유동성이 그날의 러너들 사이를 옮겨다닌다면,
**점유율 변화가 가격을 앞서야** 거래 대상이 된다.

### Q0. `vol_qu` 롤링 윈도 길이 (관문 아님, 해석 전제)

랭킹의 `vol_qu` 는 **롤링 윈도 값**이라 시점 간 차분이 무효다(계약 C-2). 실제 윈도가
몇 분인지 **캔들 분당 거래량의 누적합과 대조해 역산**한다. 15분이면 랭킹 점유율은
15분짜리 평활 신호라 1분 미만 슈팅의 선행 신호가 될 수 없다.
**결과와 무관하게 Q1 은 진행한다** — 주 측정이 캔들이기 때문이다.

### Q1. ★ 관문 — 점유율이 가격을 앞서는가, 뒤따르는가

- **주 신호는 캔들의 분당 거래량 점유율(수량 기준).** `vol_qu x last_u`(달러 점유율)는
  **가격을 품고 있어** 수익률 예측에 쓰면 순환이다. 그래서 **쓰지 않는다.**
- `dshare(t)` 와 1분 수익률의 교차상관을 **lag -5 ~ +5분**에서 잰다.
  **lag > 0 이 "점유율이 먼저"** 다.
- 대조군: **같은 분(minute) 안에서 종목 라벨 무작위 치환** — 분포와 시각 구조는 그대로
  두고 "어느 종목이었나"만 깬다.
- **세션별로 낸다.** 통합 금지.

### 중단 기준 (보기 전에 못 박음, docs/26 §4)

> 위약 대비 차이 CI 가 **0 을 교차**하거나, 최대 상관이 **lag <= 0**(동시/후행)에
> 있으면 **Q2 이후를 진행하지 않고 종료 보고**한다.

## 홀드아웃 (docs/12 §6)

`candles_1m` 은 **백필이라 봉인 구간(2026-05-01~07-29)을 담고 있다.**
`session.drop_holdout` 을 반드시 지나가며, **버린 행 수를 산출물에 싣는다.**

실행: `python -m tossmon.analysis.measure.rotation_q1 [db_path]`
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

MINUTE_MS = 60_000

#: 교차상관 lag 범위(분). **양수가 "점유율이 먼저"** 다.
LAGS = tuple(range(-5, 6))

#: 한 분에 이보다 적은 종목이 거래되면 점유율이 의미를 잃는다.
MIN_SYMBOLS_PER_MINUTE = 5

#: 관측이 이보다 적은 (세션) 칸은 판정하지 않는다.
MIN_OBS_FOR_VERDICT = 200

#: 위약 씨앗 (종목 라벨 치환).
PLACEBO_SEEDS = D.PLACEBO_SEEDS

#: Q0 윈도 후보(분).
WINDOW_CANDIDATES = tuple(range(1, 31))

#: Q0 역산에 쓸 랭킹 스냅 표본 수(전량은 불필요하게 느리다).
Q0_SAMPLE = 400

BOOTSTRAP_N = 1000
BOOTSTRAP_SEED = 20260730

OUT_DIR = D.OUT_DIR

#: docs/26 결과표가 싣는 필드 전체. 가드가 **동일 집합**으로 대조한다.
REPORTED_FIELDS = (
    "session", "lag_min", "n_obs", "corr_real", "corr_placebo",
    "diff", "ci_low", "ci_high", "verdict",
)


# --------------------------------------------------------------------------- #
# 적재 — 홀드아웃을 반드시 지나간다
# --------------------------------------------------------------------------- #
def load_candles(conn) -> dict:
    """분봉. **봉인 구간을 버리고 버린 수를 돌려준다.**"""
    cd = pd.read_sql_query(
        "SELECT symbol, ts_ms, close_u, vol_qu FROM candles_1m "
        "WHERE close_u > 0 ORDER BY symbol, ts_ms", conn)
    got = SS.drop_holdout(cd, "ts_ms")
    kept = got["kept"]
    if len(kept):
        kept = kept.copy()
        kept["session"] = SS.sessions_of(kept["ts_ms"])
        kept["cycle_date"] = kept["ts_ms"].map(lambda m: SS.session_date(int(m)))
    return {"candles": kept, "n_dropped_holdout": got["n_dropped"],
            "n_kept": got["n_kept"]}


def ranking_universe(conn) -> pd.DataFrame:
    """랭킹은 **"누가 뜨거운가"(모집단 선정)** 에만 쓴다 — 흐름 측정은 캔들이다."""
    return pd.read_sql_query(
        "SELECT DISTINCT symbol, snap_ms, vol_qu FROM rankings_snap "
        "WHERE ranking_type=?", conn, params=(S.TOSS_VOLUME,))


# --------------------------------------------------------------------------- #
# Q0 — 롤링 윈도 길이 역산
# --------------------------------------------------------------------------- #
def rolling_window_estimate(candles: pd.DataFrame, ranks: pd.DataFrame, *,
                            candidates=WINDOW_CANDIDATES,
                            sample: int = Q0_SAMPLE) -> dict:
    """랭킹 `vol_qu` 가 **몇 분치 캔들 거래량**과 맞아떨어지는가.

    후보 윈도 `W` 마다 `랭킹값 / (직전 W분 캔들 합)` 의 중앙값을 보고, **1 에 가장
    가까운** `W` 를 고른다. 캔들 거래량은 구간량이라 합산이 유효하다(계약 C-2).
    """
    if candles is None or candles.empty or ranks is None or ranks.empty:
        return {"available": False, "reason": "no data"}
    rs = ranks.dropna(subset=["vol_qu"])
    rs = rs[rs["vol_qu"] > 0]
    if rs.empty:
        return {"available": False, "reason": "no ranking volume"}
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    if len(rs) > sample:
        rs = rs.iloc[rng.choice(len(rs), size=sample, replace=False)]
    by_sym = {s: (g["ts_ms"].to_numpy(), g["vol_qu"].to_numpy())
              for s, g in candles.groupby("symbol")}
    rows = []
    for r in rs.itertuples(index=False):
        bk = by_sym.get(r.symbol)
        if bk is None:
            continue
        ts, vol = bk
        hi = int(np.searchsorted(ts, r.snap_ms, side="right"))
        if hi <= 0:
            continue
        rec = {"ranking_vol": float(r.vol_qu)}
        for w in candidates:
            lo = int(np.searchsorted(ts, r.snap_ms - w * MINUTE_MS, side="left"))
            tot = float(vol[lo:hi].sum())
            rec[f"w{w}"] = (float(r.vol_qu) / tot) if tot > 0 else np.nan
        rows.append(rec)
    if not rows:
        return {"available": False, "reason": "no symbol overlap"}
    df = pd.DataFrame(rows)
    med = {w: float(pd.to_numeric(df[f"w{w}"], errors="coerce").dropna().median())
           for w in candidates}
    best = min((w for w in candidates if med[w] == med[w]),
               key=lambda w: abs(med[w] - 1.0), default=None)
    return {"available": True, "n": int(len(df)),
            "ratio_by_window": med, "best_window_min": best,
            "ratio_at_best": med.get(best),
            "note": ("ratio = ranking vol_qu / trailing W-minute candle volume; "
                     "W whose ratio is closest to 1 is the implied window")}


# --------------------------------------------------------------------------- #
# Q1 — 점유율 패널
# --------------------------------------------------------------------------- #
def build_panel(candles: pd.DataFrame, universe: set[str] | None = None) -> pd.DataFrame:
    """분당 **수량** 점유율과 1분 수익률의 패널.

    달러 점유율은 **쓰지 않는다** — 가격을 품고 있어 수익률 예측에 쓰면 순환이다.
    """
    if candles is None or candles.empty:
        return pd.DataFrame()
    d = candles if universe is None else candles[candles["symbol"].isin(universe)]
    if d.empty:
        return pd.DataFrame()
    d = d.sort_values(["symbol", "ts_ms"]).copy()
    tot = d.groupby("ts_ms")["vol_qu"].transform("sum")
    cnt = d.groupby("ts_ms")["symbol"].transform("size")
    d["share"] = np.where(tot > 0, d["vol_qu"] / tot, np.nan)
    d = d[cnt >= MIN_SYMBOLS_PER_MINUTE]
    if d.empty:
        return pd.DataFrame()
    g = d.groupby("symbol")
    prev_ts = g["ts_ms"].shift(1)
    d["dshare"] = np.where(prev_ts == d["ts_ms"] - MINUTE_MS,
                           d["share"] - g["share"].shift(1), np.nan)
    d["ret"] = np.where(prev_ts == d["ts_ms"] - MINUTE_MS,
                        d["close_u"] / g["close_u"].shift(1) - 1.0, np.nan)
    return d


def lagged_pairs(panel: pd.DataFrame, lag_min: int) -> pd.DataFrame:
    """`dshare(t)` 와 `ret(t+lag)` 를 짝짓는다. **lag>0 이면 점유율이 먼저**다."""
    if panel is None or panel.empty:
        return pd.DataFrame()
    left = panel[["symbol", "ts_ms", "session", "cycle_date", "dshare"]].dropna()
    right = panel[["symbol", "ts_ms", "ret"]].dropna().copy()
    right["ts_ms"] = right["ts_ms"] - lag_min * MINUTE_MS
    return left.merge(right, on=["symbol", "ts_ms"], how="inner")


def minute_stats(x: np.ndarray, y: np.ndarray, keys: np.ndarray) -> pd.DataFrame:
    """분(minute) 단위 **충분통계**. 부트스트랩이 상관을 매번 다시 안 돌게 한다."""
    df = pd.DataFrame({"k": keys, "x": x, "y": y})
    g = df.groupby("k")
    return pd.DataFrame({"n": g.size(), "sx": g["x"].sum(), "sy": g["y"].sum(),
                         "sxx": (df["x"] * df["x"]).groupby(df["k"]).sum(),
                         "syy": (df["y"] * df["y"]).groupby(df["k"]).sum(),
                         "sxy": (df["x"] * df["y"]).groupby(df["k"]).sum()})


def corr_from_stats(st: pd.DataFrame) -> float:
    """충분통계 합에서 피어슨 상관."""
    n, sx, sy = st["n"].sum(), st["sx"].sum(), st["sy"].sum()
    if n < 3:
        return float("nan")
    cov = st["sxy"].sum() - sx * sy / n
    vx = st["sxx"].sum() - sx * sx / n
    vy = st["syy"].sum() - sy * sy / n
    if not (vx > 0 and vy > 0):
        return float("nan")
    return float(cov / np.sqrt(vx * vy))


def placebo_pairs(pairs: pd.DataFrame, seed: int) -> pd.DataFrame:
    """**같은 분 안에서 종목 라벨만 치환.** 값의 분포와 시각 구조는 보존된다."""
    if pairs is None or pairs.empty:
        return pairs
    rng = np.random.default_rng(seed)
    out = pairs.copy()
    out["dshare"] = (out.groupby("ts_ms")["dshare"]
                     .transform(lambda s: s.to_numpy()[rng.permutation(len(s))]))
    return out


def cross_correlation(panel: pd.DataFrame, *, lags=LAGS,
                      seeds=PLACEBO_SEEDS) -> list[dict]:
    """세션 x lag 의 실제·위약 상관과 **차이의 부트스트랩 CI**.

    부트스트랩은 **분 단위 군집 재표집**이다 — 같은 분의 종목들은 서로 독립이 아니다.
    """
    if panel is None or panel.empty:
        return []
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    rows = []
    for lag in lags:
        pr = lagged_pairs(panel, lag)
        for sess in SS.SESSIONS:
            p = pr[pr["session"] == sess] if len(pr) else pr
            if p is None or len(p) < 3:
                rows.append({"session": sess, "lag_min": lag, "n_obs": int(len(p) if p is not None else 0),
                             "corr_real": float("nan"), "corr_placebo": float("nan"),
                             "diff": float("nan"), "ci_low": float("nan"),
                             "ci_high": float("nan"), "verdict": "insufficient"})
                continue
            real = minute_stats(p["dshare"].to_numpy(), p["ret"].to_numpy(),
                                p["ts_ms"].to_numpy())
            plac = [minute_stats(q["dshare"].to_numpy(), q["ret"].to_numpy(),
                                 q["ts_ms"].to_numpy())
                    for q in (placebo_pairs(p, s) for s in seeds)]
            c_real = corr_from_stats(real)
            c_plac = float(np.nanmean([corr_from_stats(s) for s in plac]))
            powered_cell = len(p) >= MIN_OBS_FOR_VERDICT
            keys = real.index.to_numpy()
            diffs = []
            # 판정 불가 칸은 부트스트랩을 돌리지 않는다 — 어차피 판정하지 않는다.
            for _ in range(BOOTSTRAP_N if powered_cell else 0):
                pick = keys[rng.integers(0, len(keys), size=len(keys))]
                r = corr_from_stats(real.loc[pick])
                # 위약도 **같은 분 재표집**을 쓴다. 분 인덱스가 실제와 동일하므로
                # 교집합을 다시 구할 필요가 없다(그게 병목이었다).
                pl = float(np.nanmean([corr_from_stats(s.loc[pick]) for s in plac[:2]]))
                if r == r and pl == pl:
                    diffs.append(r - pl)
            lo, hi = ((float(np.percentile(diffs, 2.5)),
                       float(np.percentile(diffs, 97.5))) if len(diffs) >= 100
                      else (float("nan"), float("nan")))
            powered = powered_cell
            verdict = ("insufficient" if not powered or lo != lo else
                       "above_zero" if lo > 0 else
                       "below_zero" if hi < 0 else "crosses_zero")
            rows.append({"session": sess, "lag_min": lag, "n_obs": int(len(p)),
                         "corr_real": c_real, "corr_placebo": c_plac,
                         "diff": c_real - c_plac, "ci_low": lo, "ci_high": hi,
                         "verdict": verdict})
    return rows


def q1_gate(rows: list[dict]) -> dict:
    """**사전 등록된 중단 기준을 코드로 집행한다.**

    선행(lag>0)에서 위약 대비 차이 CI 가 0 을 초과하는 칸이 **하나도 없거나**,
    최대 상관이 lag<=0 에 있으면 **Q2 이후로 가지 않는다.**
    """
    out = {"by_session": {}, "proceed_to_q2": False}
    for sess in SS.SESSIONS:
        r = [x for x in rows if x["session"] == sess and x["n_obs"] >= MIN_OBS_FOR_VERDICT]
        if not r:
            out["by_session"][sess] = {"decision": "insufficient", "n_cells": 0}
            continue
        lead = [x for x in r if x["lag_min"] > 0 and x["verdict"] == "above_zero"]
        best = max(r, key=lambda x: abs(x["corr_real"]) if x["corr_real"] == x["corr_real"] else -1)
        out["by_session"][sess] = {
            "decision": "proceed" if lead else "stop",
            "n_cells": len(r),
            "n_leading_cells_above_zero": len(lead),
            "argmax_lag_min": best["lag_min"],
            "argmax_corr": best["corr_real"],
            "argmax_is_leading": bool(best["lag_min"] > 0),
            "reason": ("leading lags beat placebo" if lead else
                       "no leading lag has a difference CI above zero")}
        if lead and best["lag_min"] > 0:
            out["proceed_to_q2"] = True
    return out


def build_report(conn) -> dict:
    cd = load_candles(conn)
    ranks = ranking_universe(conn)
    universe = set(ranks["symbol"]) if len(ranks) else None
    panel = build_panel(cd["candles"], universe)
    rows = cross_correlation(panel)
    cycles = (sorted(panel["cycle_date"].unique().tolist()) if len(panel) else [])
    return {
        "holdout": {"start": SS.HOLDOUT_START, "end": SS.HOLDOUT_END,
                    "n_rows_dropped": cd["n_dropped_holdout"],
                    "n_rows_kept": cd["n_kept"]},
        "q0": rolling_window_estimate(cd["candles"], ranks),
        "cycle_dates": cycles,
        "pooled_ci_permitted": len(cycles) >= D.MIN_DAY_CLUSTERS,
        "universe_symbols": int(len(universe)) if universe else 0,
        "panel_rows": int(len(panel)),
        "session_counts": (SS.session_counts(panel["ts_ms"]) if len(panel) else {}),
        "cross_correlation": rows,
        "gate": q1_gate(rows),
        "signal_note": ("primary signal is QUANTITY share from candles; dollar share "
                        "is deliberately NOT used because it embeds price and would "
                        "make return prediction circular"),
    }


def main(db: Path, *, out_dir: Path | None = None) -> int:
    conn = D.ro(db)
    try:
        rep = build_report(conn)
    finally:
        conn.close()
    h = rep["holdout"]
    print(f"holdout {h['start']}..{h['end']}: dropped {h['n_rows_dropped']} candle rows, "
          f"kept {h['n_rows_kept']}")
    print(f"cycle dates {rep['cycle_dates']} -> pooled CI "
          f"{'PERMITTED' if rep['pooled_ci_permitted'] else 'WITHHELD'} "
          f"(floor {D.MIN_DAY_CLUSTERS})")
    print(f"universe {rep['universe_symbols']} symbols, panel {rep['panel_rows']} rows")
    print(f"  {rep['signal_note']}")

    print("\n=== [docs/26 Q0] IMPLIED ROLLING WINDOW of ranking vol_qu")
    q0 = rep["q0"]
    if q0.get("available"):
        print(f"  best window = {q0['best_window_min']} min "
              f"(ratio {q0['ratio_at_best']:.3f}, n={q0['n']})")
        for w in (1, 2, 3, 5, 10, 15, 20, 30):
            v = q0["ratio_by_window"].get(w)
            if v is not None:
                print(f"    W={w:>2}min  ranking/candle ratio {v:.3f}")
        print(f"  {q0['note']}")
    else:
        print(f"  unavailable: {q0.get('reason')}")

    print("\n=== [docs/26 Q1] CROSS-CORRELATION of dshare(t) with return(t+lag)")
    print("  lag > 0 means SHARE LEADS PRICE - that is the tradable direction")
    print(f"{'session':<9}{'lag':>5}{'n obs':>8}{'real':>9}{'placebo':>9}{'diff':>9}"
          f"{'CI low':>9}{'CI high':>9}{'verdict':>15}")
    for r in rep["cross_correlation"]:
        if r["n_obs"] < MIN_OBS_FOR_VERDICT:
            continue
        print(f"{r['session']:<9}{r['lag_min']:>5}{r['n_obs']:>8}{r['corr_real']:>9.4f}"
              f"{r['corr_placebo']:>9.4f}{r['diff']:>9.4f}{r['ci_low']:>9.4f}"
              f"{r['ci_high']:>9.4f}{r['verdict']:>15}")

    print("\n=== [docs/26 Q1] PREREGISTERED STOP RULE")
    for sess, g in rep["gate"]["by_session"].items():
        if g.get("n_cells", 0) == 0:
            continue
        print(f"  {sess:<9} {g['decision'].upper():<12} argmax lag "
              f"{g['argmax_lag_min']:+d} min (corr {g['argmax_corr']:+.4f}), "
              f"leading cells above zero: {g['n_leading_cells_above_zero']} "
              f"-> {g['reason']}")
    print(f"\n  PROCEED TO Q2: {rep['gate']['proceed_to_q2']}")

    out = (out_dir or OUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "rotation_q1.json"
    path.write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1
                  else Path(r"C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db")))

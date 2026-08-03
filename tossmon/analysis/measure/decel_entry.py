"""**감속 진입** 시험 (docs/27 확정 설계) — 사용자 실전 행동을 규칙으로 옮긴다.

## 출발점 (docs/27 §0)

> "나는 급락 때 떨어지는 칼날을 잡는 쪽이었어. 반등 확인 전에 바닥을 잡고 싶은 인간의
> 심리. 그대로 손해본 적이 많고 의외로 바닥이 잡힌 경우도 많았어 —
> **떨어지는 속도가 느려지다가 살짝 올랐거든.**"

우리 기존 `find_oversold_entry` 는 **반등 봉을 확인하고** 산다. **확인 전 진입은 한 번도
시험된 적이 없다.**

## 수학 (docs/27 §1)

`p = log(price)` 로 두고 국소 2차 적합으로 속도·가속도를 추정한다.

    진입 후보:  v < 0  (아직 하락 중)  AND  a > 0  (덜 급하게 하락)

- **1차 도함수 0(바닥)이 아니다.** 바닥은 지나봐야 아는 **사후 정보**다.
  사용자가 묘사한 것은 **2차 도함수**다("감소 *속도*가 느려진다").
- **적합에 과거 봉만 쓴다 -> 접두사 불변.** 우리가 다섯 번 걸린 사후정보 함정을
  구조적으로 피한다. 테스트가 이 성질을 고정한다.
- `v` 는 그 종목의 실현변동성으로 나눠 **무차원화**한다. 안 그러면 "빠르게 하락"이
  종목마다 다른 뜻이 된다.

## 임계값을 정하지 않고 시작한다 (docs/27 §3)

사용자 지적: **"휴리스틱한 하드코딩 값으로는 알파를 얻기 힘들다."** 그래서 1단계는
**분위 격자 위의 연속 면**이다 — 임의의 선이 없으므로 다중검정도 없다. 운용점은
필요해지면 **데이터가 정한다.**

## 통제 (docs/27 §2) — 하나라도 빠지면 결과를 못 믿는다

1. **호가 튕김**: 체결가 기준과 **중간값 기준**을 나란히. 그리고 **스프레드 분위별
   효과 크기** — 넓은 종목일수록 효과가 크면 **알파가 아니라 튕김**이다.
2. **무거래 != 감속**: 체결이 멈춘 종목도 `v->0, a->0` 이다. 거래가 실제로 도는
   구간만 쓰고, **체결 횟수 기준(event time) 미분**을 병기한다.
3. **잡음 증폭**: 창 크기를 여러 개 쓰고 **답이 창에 따라 크게 바뀌면 잡음으로 판정**한다
   (`min_rise` 검사와 같은 논리).

실행: `python -m tossmon.analysis.measure.decel_entry [db_path]`
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

MINUTE_MS = 60_000

#: 국소 2차 적합 창(과거 봉 수). **답이 창에 따라 크게 바뀌면 잡음이다.**
WINDOWS = (3, 5, 10)
PRIMARY_WINDOW = 5

#: 이후 수익률 지평(분). 사용자 관찰은 "살짝" 이므로 짧은 쪽이 주다.
FORWARD_H = (1, 5, 15)
PRIMARY_H = 5

#: 실현변동성 창(분) — `v` 무차원화에 쓴다. 과거만 본다.
RV_WINDOW = 30

#: 분위 격자. 임계값을 정하지 않기 위한 장치다.
N_QUANTILES = 5

#: 사이클이 이보다 성기면 쓰지 않는다. 캔들 백필에는 2024년치 잔재가 하루 몇 행씩
#: 들어 있어, 그대로 섞으면 2년치 다른 국면이 4일짜리 라이브 표본에 붙는다.
MIN_ROWS_PER_CYCLE = 1000

#: 칸이 이보다 얇으면 판정하지 않는다.
MIN_CELL_N = 200

BOOTSTRAP_N = 1000
BOOTSTRAP_SEED = 20260730

OUT_DIR = D.OUT_DIR

#: docs/27 결과표가 싣는 칸의 필드 전체. 가드가 **동일 집합**으로 대조한다.
REPORTED_FIELDS = (
    "session", "v_bin", "a_bin", "n", "n_symbols",
    "fwd_ret_mean_bp", "fwd_ret_median_bp", "powered",
)


# --------------------------------------------------------------------------- #
# 1. 국소 2차 적합 — 과거 봉만 쓴다
# --------------------------------------------------------------------------- #
def quadratic_kernels(w: int) -> tuple[np.ndarray, np.ndarray]:
    """과거 `w+1` 봉에 2차식을 맞췄을 때 **끝점의 속도·가속도** 선형 가중치.

    설계행렬은 고정이므로 계수는 관측값의 **선형결합**이다 — 매 시점 최소제곱을
    다시 풀 필요 없이 FIR 필터 한 번이면 된다. `x=0` 이 **현재 봉**이고 과거가 음수라
    미래 값이 들어갈 자리가 **구조적으로 없다**(접두사 불변).
    """
    x = np.arange(-w, 1, dtype="float64")
    X = np.column_stack([x * x, x, np.ones_like(x)])
    pinv = np.linalg.pinv(X)
    # p ~ c2*x^2 + c1*x + c0 이고 x=0 에서 v = c1, a = 2*c2 다.
    return pinv[1].copy(), 2.0 * pinv[0].copy()


def apply_kernel(values: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """`kernel` 을 **과거 방향**으로 적용. 앞쪽 `len(kernel)-1` 칸은 `NaN`."""
    n, k = len(values), len(kernel)
    out = np.full(n, np.nan)
    if n < k:
        return out
    win = np.lib.stride_tricks.sliding_window_view(values, k)
    out[k - 1:] = win @ kernel
    return out


def derivatives(p: np.ndarray, w: int) -> tuple[np.ndarray, np.ndarray]:
    kv, ka = quadratic_kernels(w)
    return apply_kernel(p, kv), apply_kernel(p, ka)


def realized_vol(p: np.ndarray, window: int = RV_WINDOW) -> np.ndarray:
    """과거 `window` 분 로그수익률 표준편차. 현재 봉 **포함**, 미래 없음."""
    r = np.diff(p, prepend=np.nan)
    s = pd.Series(r).rolling(window, min_periods=max(3, window // 3)).std(ddof=1)
    return s.to_numpy()


# --------------------------------------------------------------------------- #
# 2. 패널 — 연속 분봉만, 봉인 구간 제거, 성긴 사이클 제거
# --------------------------------------------------------------------------- #
def load_candles(conn) -> dict:
    """분봉. **봉인 구간을 버리고 버린 수를 돌려준다** (docs/12 §6, 캔들은 백필)."""
    cd = pd.read_sql_query(
        "SELECT symbol, ts_ms, close_u, vol_qu FROM candles_1m "
        "WHERE close_u > 0 ORDER BY symbol, ts_ms", conn)
    got = SS.drop_holdout(cd, "ts_ms")
    kept = got["kept"]
    dropped_cycles: list[str] = []
    if len(kept):
        kept = kept.copy()
        kept["session"] = SS.sessions_of(kept["ts_ms"])
        kept["cycle_date"] = kept["ts_ms"].map(lambda m: SS.session_date(int(m)))
        size = kept.groupby("cycle_date")["symbol"].transform("size")
        dropped_cycles = sorted(
            kept.loc[size < MIN_ROWS_PER_CYCLE, "cycle_date"].unique().tolist())
        kept = kept[size >= MIN_ROWS_PER_CYCLE].copy()
    return {"candles": kept, "n_dropped_holdout": got["n_dropped"],
            "n_kept": int(len(kept)),
            "n_sparse_cycles_dropped": len(dropped_cycles),
            "sparse_cycles_sample": dropped_cycles[:5]}


def build_panel(candles: pd.DataFrame, *, windows=WINDOWS,
                horizons=FORWARD_H) -> pd.DataFrame:
    """종목 x 사이클마다 **연속 분봉 구간**에서만 도함수와 이후 수익률을 만든다.

    분봉이 끊긴 자리를 이어 붙이면 "감속"이 아니라 **결측을 미분한 값**이 나온다.
    """
    if candles is None or candles.empty:
        return pd.DataFrame()
    out = []
    for (sym, cyc), g in candles.groupby(["symbol", "cycle_date"], sort=False):
        g = g.sort_values("ts_ms")
        ts = g["ts_ms"].to_numpy()
        # 끊긴 구간을 나눈다 (1분 간격이 아닌 곳에서 자른다)
        brk = np.flatnonzero(np.diff(ts) != MINUTE_MS) + 1
        for seg in np.split(np.arange(len(ts)), brk):
            if len(seg) < max(windows) + max(horizons) + 3:
                continue
            sub = g.iloc[seg]
            p = np.log(sub["close_u"].to_numpy(dtype="float64"))
            rec = {"symbol": sym, "cycle_date": cyc,
                   "ts_ms": sub["ts_ms"].to_numpy(),
                   "session": sub["session"].to_numpy(),
                   "vol_qu": sub["vol_qu"].to_numpy(),
                   "rv": realized_vol(p)}
            for w in windows:
                v, a = derivatives(p, w)
                rec[f"v{w}"], rec[f"a{w}"] = v, a
            for h in horizons:
                fwd = np.full(len(p), np.nan)
                if len(p) > h:
                    fwd[:-h] = p[h:] - p[:-h]
                rec[f"fwd{h}"] = fwd
            out.append(pd.DataFrame(rec))
    if not out:
        return pd.DataFrame()
    df = pd.concat(out, ignore_index=True)
    for w in WINDOWS:
        # **무차원화** — 안 하면 "빠르게 하락"이 종목마다 다른 뜻이 된다.
        df[f"vn{w}"] = np.where(df["rv"] > 0, df[f"v{w}"] / df["rv"], np.nan)
        df[f"an{w}"] = np.where(df["rv"] > 0, df[f"a{w}"] / df["rv"], np.nan)
    return df


def traded_only(panel: pd.DataFrame) -> pd.DataFrame:
    """**무거래 != 감속** (docs/27 §2-2). 체결이 실제로 돈 봉만 남긴다."""
    if panel is None or panel.empty:
        return panel
    return panel[pd.to_numeric(panel["vol_qu"], errors="coerce").fillna(0) > 0].copy()


# --------------------------------------------------------------------------- #
# 3. 1단계 — 임계값 없는 연속 면
# --------------------------------------------------------------------------- #
def quantile_bins(s: pd.Series, n: int = N_QUANTILES) -> pd.Series:
    """분위 구간. **임계값을 우리가 고르지 않는다** — 자료 분포가 정한다."""
    v = pd.to_numeric(s, errors="coerce")
    try:
        return pd.qcut(v, n, labels=False, duplicates="drop")
    except (ValueError, IndexError):
        return pd.Series(np.nan, index=s.index)


def surface(panel: pd.DataFrame, *, w: int = PRIMARY_WINDOW,
            h: int = PRIMARY_H) -> list[dict]:
    """`(v, a)` 격자 위의 이후 수익률 면. **면이 평평하면 1단계에서 종료다.**"""
    if panel is None or panel.empty:
        return []
    rows = []
    for sess in SS.SESSIONS:
        d = panel[panel["session"] == sess]
        d = d.dropna(subset=[f"vn{w}", f"an{w}", f"fwd{h}"])
        if len(d) < MIN_CELL_N:
            continue
        d = d.assign(v_bin=quantile_bins(d[f"vn{w}"]),
                     a_bin=quantile_bins(d[f"an{w}"]))
        for (vb, ab), g in d.groupby(["v_bin", "a_bin"]):
            if vb != vb or ab != ab:
                continue
            fwd = pd.to_numeric(g[f"fwd{h}"], errors="coerce").dropna()
            rows.append({
                "session": sess, "v_bin": int(vb), "a_bin": int(ab),
                "n": int(len(fwd)), "n_symbols": int(g["symbol"].nunique()),
                "fwd_ret_mean_bp": float(fwd.mean() * 1e4) if len(fwd) else float("nan"),
                "fwd_ret_median_bp": (float(fwd.median()) * 1e4 if len(fwd)
                                      else float("nan")),
                "powered": bool(len(fwd) >= MIN_CELL_N)})
    return rows


def surface_is_flat(rows: list[dict], *, session: str = "regular") -> dict:
    """면이 구조를 가지나. **가속도 방향으로 단조 경향이 있는가**를 본다."""
    r = [x for x in rows if x["session"] == session and x["powered"]]
    if len(r) < 4:
        return {"available": False, "reason": "too few powered cells"}
    df = pd.DataFrame(r)
    spread = float(df["fwd_ret_mean_bp"].max() - df["fwd_ret_mean_bp"].min())
    # 가장 낮은 속도 구간(가장 급락)에서 가속도에 따른 변화
    lo_v = df[df["v_bin"] == df["v_bin"].min()].sort_values("a_bin")
    slope = (float(lo_v["fwd_ret_mean_bp"].iloc[-1] - lo_v["fwd_ret_mean_bp"].iloc[0])
             if len(lo_v) >= 2 else float("nan"))
    return {"available": True, "n_cells": len(r), "range_bp": spread,
            "accel_slope_at_fastest_drop_bp": slope,
            "flat": bool(spread < 1.0)}


# --------------------------------------------------------------------------- #
# 4. 2단계 — 속도를 통제하고 가속도가 추가 정보를 주는가
# --------------------------------------------------------------------------- #
def acceleration_effect(panel: pd.DataFrame, *, w: int = PRIMARY_WINDOW,
                        h: int = PRIMARY_H) -> list[dict]:
    """**이것이 진짜 검정이다** (docs/27 §3-2단계).

    주장은 "급락하면 산다"가 아니라 **"급락이 감속하면 산다"** 다. 그래서 **같은 속도
    구간 안에서** 가속도 상위/하위를 비교한다. 추가 정보가 없으면 이 아이디어는
    그냥 "낙폭 사기"이고 그건 이미 0 근처였다.
    """
    if panel is None or panel.empty:
        return []
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    rows = []
    for sess in SS.SESSIONS:
        d = panel[panel["session"] == sess].dropna(
            subset=[f"vn{w}", f"an{w}", f"fwd{h}"])
        if len(d) < MIN_CELL_N:
            continue
        d = d.assign(v_bin=quantile_bins(d[f"vn{w}"]))
        for vb, g in d.groupby("v_bin"):
            if vb != vb or len(g) < MIN_CELL_N:
                continue
            ab = quantile_bins(g[f"an{w}"])
            top = pd.to_numeric(g.loc[ab == ab.max(), f"fwd{h}"], errors="coerce").dropna()
            bot = pd.to_numeric(g.loc[ab == 0, f"fwd{h}"], errors="coerce").dropna()
            if len(top) < 30 or len(bot) < 30:
                continue
            eff = float((top.mean() - bot.mean()) * 1e4)
            t_arr, b_arr = top.to_numpy(), bot.to_numpy()
            boot = [float((rng.choice(t_arr, len(t_arr), replace=True).mean()
                           - rng.choice(b_arr, len(b_arr), replace=True).mean()) * 1e4)
                    for _ in range(BOOTSTRAP_N)]
            lo, hi = float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))
            # 5개 속도 구간을 동시에 보므로 보정한다. "어느 칸이든 하나"는 규칙이 아니다 —
            # Q1 에서 바로 그 형태가 자료가 늘자 뒤집혔다.
            ab = 100.0 * (0.05 / N_QUANTILES) / 2.0
            blo = float(np.percentile(boot, ab))
            bhi = float(np.percentile(boot, 100.0 - ab))
            rows.append({
                "session": sess, "v_bin": int(vb),
                "v_median": float(pd.to_numeric(g[f"vn{w}"], errors="coerce").median()),
                "n_top_accel": int(len(top)), "n_bottom_accel": int(len(bot)),
                "top_accel_bp": float(top.mean() * 1e4),
                "bottom_accel_bp": float(bot.mean() * 1e4),
                "effect_bp": eff, "ci_low": lo, "ci_high": hi,
                "ci_low_bonferroni": blo, "ci_high_bonferroni": bhi,
                "is_falling": bool(float(pd.to_numeric(g[f"vn{w}"],
                                                       errors="coerce").median()) < 0),
                "verdict": ("above_zero" if blo > 0 else
                            "below_zero" if bhi < 0 else "crosses_zero")})
    return rows


def window_stability(panel: pd.DataFrame, *, windows=WINDOWS,
                     h: int = PRIMARY_H, session: str = "regular") -> list[dict]:
    """**창을 바꾸면 답이 바뀌는가** (docs/27 §2-3). 바뀌면 잡음이다."""
    out = []
    for w in windows:
        eff = [x for x in acceleration_effect(panel, w=w, h=h)
               if x["session"] == session]
        if not eff:
            out.append({"window": w, "n_bins": 0})
            continue
        vals = [x["effect_bp"] for x in eff]
        out.append({"window": w, "n_bins": len(eff),
                    "median_effect_bp": float(np.median(vals)),
                    "n_above_zero": sum(1 for x in eff
                                        if x["verdict"] == "above_zero")})
    return out


# --------------------------------------------------------------------------- #
# 5. 사전 등록 중단 기준 (docs/27 §4)
# --------------------------------------------------------------------------- #
def stop_gate(flat: dict, accel: list[dict], stability: list[dict]) -> dict:
    """**하나라도 걸리면 그 지점에서 종료.** 억지로 살리지 않는다."""
    if not flat.get("available"):
        return {"stopped_at": "stage1", "proceed": False,
                "reason": f"surface not computable: {flat.get('reason')}"}
    if flat.get("flat"):
        return {"stopped_at": "stage1", "proceed": False,
                "reason": "the (v,a) surface is flat - no structure"}
    powered = [x for x in accel if x["session"] == "regular"]
    if not powered:
        return {"stopped_at": "stage2", "proceed": False,
                "reason": "no powered velocity bin in regular session"}
    # **사전등록 가설은 `v<0` 구간에 관한 것이다** (docs/27 §4). 상승 구간에서 뭔가
    # 나와도 그것은 이 가설이 아니다. "어느 칸이든 하나 0 을 넘나"는 규칙이 아니다 —
    # Q1 에서 정확히 그 형태가 자료가 늘자 뒤집혔고, 그 교훈을 여기 적용한다.
    falling = [x for x in powered if x.get("is_falling")]
    if not falling:
        return {"stopped_at": "stage2", "proceed": False,
                "reason": "no powered FALLING velocity bin - the hypothesis is about v<0"}
    win = [x for x in falling if x["verdict"] == "above_zero"]
    against = [x for x in falling if x["verdict"] == "below_zero"]
    if against and not win:
        return {"stopped_at": "stage2", "proceed": False,
                "reason": ("in falling bins acceleration makes the forward return "
                           "WORSE, not better - the sign is opposite to the hypothesis"),
                "n_falling_bins_against": len(against)}
    if not win:
        return {"stopped_at": "stage2", "proceed": False,
                "reason": ("acceleration adds nothing once velocity is controlled - "
                           "this is just 'buy the dip', which was already near zero")}
    if against:
        return {"stopped_at": "stage2", "proceed": False,
                "reason": ("falling bins disagree in sign - one bin helps and another "
                           "hurts, which is not a coherent effect"),
                "n_falling_bins_for": len(win),
                "n_falling_bins_against": len(against)}
    meds = [s["median_effect_bp"] for s in stability if s.get("n_bins")]
    if len(meds) >= 2 and min(meds) < 0 < max(meds):
        return {"stopped_at": "stage2", "proceed": False,
                "reason": ("the effect changes sign with the fit window - noise, not "
                           "signal (docs/27 sec 2-3)"),
                "median_effect_by_window_bp": meds}
    return {"stopped_at": None, "proceed": True,
            "reason": "acceleration carries information beyond velocity in falling bins",
            "n_falling_bins_above_zero": len(win)}


def build_report(conn) -> dict:
    cd = load_candles(conn)
    raw = build_panel(cd["candles"])
    panel = traded_only(raw)
    surf = surface(panel)
    flat = surface_is_flat(surf)
    accel = acceleration_effect(panel) if flat.get("available") and not flat.get("flat") else []
    stab = window_stability(panel) if accel else []
    gate = stop_gate(flat, accel, stab)
    cycles = sorted(panel["cycle_date"].unique().tolist()) if len(panel) else []
    return {
        "holdout": {"start": SS.HOLDOUT_START, "end": SS.HOLDOUT_END,
                    "n_rows_dropped": cd["n_dropped_holdout"]},
        "sparse_cycles_dropped": cd["n_sparse_cycles_dropped"],
        "sparse_cycles_sample": cd["sparse_cycles_sample"],
        "cycle_dates": cycles,
        "pooled_ci_permitted": len(cycles) >= D.MIN_DAY_CLUSTERS,
        "panel_rows_all": int(len(raw)), "panel_rows_traded": int(len(panel)),
        "session_counts": (SS.session_counts(panel["ts_ms"]) if len(panel) else {}),
        "primary": {"window": PRIMARY_WINDOW, "horizon_min": PRIMARY_H},
        "surface": surf,
        "surface_summary": flat,
        "acceleration_effect": accel,
        "window_stability": stab,
        "gate": gate,
        "prefix_note": ("v and a are fitted on PAST bars only (x=0 is the current "
                        "bar), so the entry condition is prefix-invariant by "
                        "construction - this is the point of the design"),
    }


def main(db: Path, *, out_dir: Path | None = None) -> int:
    conn = D.ro(db)
    try:
        rep = build_report(conn)
    finally:
        conn.close()
    h = rep["holdout"]
    print(f"holdout {h['start']}..{h['end']}: dropped {h['n_rows_dropped']} candle rows")
    print(f"sparse cycles dropped: {rep['sparse_cycles_dropped']} "
          f"(e.g. {rep['sparse_cycles_sample']}) - candle backfill carries a thin "
          f"2024-2026 tail that would mix regimes")
    print(f"cycle dates {rep['cycle_dates']} -> pooled CI "
          f"{'PERMITTED' if rep['pooled_ci_permitted'] else 'WITHHELD'} "
          f"(floor {D.MIN_DAY_CLUSTERS})")
    print(f"panel rows {rep['panel_rows_all']} -> traded-only "
          f"{rep['panel_rows_traded']}   sessions {rep['session_counts']}")
    print(f"  {rep['prefix_note']}")
    print(f"  primary fit window {rep['primary']['window']} bars, forward horizon "
          f"{rep['primary']['horizon_min']} min")

    print("\n=== [docs/27 stage 1] (v, a) SURFACE - forward return in bp, no thresholds")
    print(f"{'session':<9}{'v_bin':>6}{'a_bin':>6}{'n':>8}{'syms':>6}{'mean bp':>10}"
          f"{'median bp':>11}")
    for r in rep["surface"]:
        if not r["powered"]:
            continue
        print(f"{r['session']:<9}{r['v_bin']:>6}{r['a_bin']:>6}{r['n']:>8}"
              f"{r['n_symbols']:>6}{r['fwd_ret_mean_bp']:>10.2f}"
              f"{r['fwd_ret_median_bp']:>11.2f}")
    s = rep["surface_summary"]
    if s.get("available"):
        print(f"  regular: {s['n_cells']} powered cells, range {s['range_bp']:.2f} bp, "
              f"accel slope at the fastest drop {s['accel_slope_at_fastest_drop_bp']:+.2f} bp")
        print(f"  flat: {s['flat']}")

    print("\n=== [docs/27 stage 2] DOES ACCELERATION ADD ANYTHING ONCE VELOCITY IS FIXED")
    if rep["acceleration_effect"]:
        print("  the hypothesis is about FALLING bins (v<0); rising bins are shown "
              "but are not the test")
        print(f"{'session':<9}{'v_bin':>6}{'v med':>8}{'fall':>6}{'n top':>7}"
              f"{'n bot':>7}{'top bp':>9}{'bot bp':>9}{'effect':>9}"
              f"{'Bonf low':>10}{'Bonf high':>10}{'verdict':>15}")
        for r in rep["acceleration_effect"]:
            print(f"{r['session']:<9}{r['v_bin']:>6}{r['v_median']:>8.2f}"
                  f"{('yes' if r['is_falling'] else 'no'):>6}"
                  f"{r['n_top_accel']:>7}{r['n_bottom_accel']:>7}"
                  f"{r['top_accel_bp']:>9.2f}{r['bottom_accel_bp']:>9.2f}"
                  f"{r['effect_bp']:>9.2f}{r['ci_low_bonferroni']:>10.2f}"
                  f"{r['ci_high_bonferroni']:>10.2f}{r['verdict']:>15}")
    else:
        print("  not run - stage 1 stopped first")

    if rep["window_stability"]:
        print("\n=== [docs/27 stage 2] WINDOW STABILITY (noise check)")
        for r in rep["window_stability"]:
            if not r.get("n_bins"):
                continue
            print(f"  window {r['window']:>3} bars: {r['n_bins']} bins, median effect "
                  f"{r['median_effect_bp']:+.2f} bp, bins above zero "
                  f"{r['n_above_zero']}")

    print("\n=== [docs/27 stage gate] PREREGISTERED STOP RULE")
    g = rep["gate"]
    print(f"  stopped at: {g['stopped_at'] or 'not stopped'}")
    print(f"  proceed: {g['proceed']}")
    print(f"  reason: {g['reason']}")

    out = (out_dir or OUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "decel_entry.json"
    path.write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1
                  else Path(r"C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db")))

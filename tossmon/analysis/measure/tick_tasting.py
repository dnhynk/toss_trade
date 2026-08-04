"""**닫힌 후보 맛보기 재검토** (docs/29) — 예비 조사. **판정하지 않는다.**

## 무엇을 하는 작업인가

사용자: **"시간 뜨니까 한번 맛만 보자. 나중에 데이터 쌓이고는 당연히 해야지."**

**지금 데이터로 확정 판정은 불가능하다는 것을 전제로 시작한다.** 목적은
**"어디를 더 파야 하는지"** 를 정하는 것이고, 가장 값어치 있는 산출물은
**필요 데이터량 역산**이다.

## 왜 이 둘인가 (해상도 때문에 답이 바뀔 개연성이 가장 큰 둘)

### 대상 1 — 회전의 선행/후행
1분 해상도에서 lag -1/0/+1 이 거의 같아 "동시 관계"로 닫혔다.
**그런데 1분 눈금은 5초 선행을 구조적으로 볼 수 없다.** 그래서 초 단위 lag 로 다시 잰다.
지표는 `docs/28` 계측기의 **체결 건수 강도**와 **매수/매도 불균형**이다
(1분봉 거래량 점유율이 아니다). 주 판정 지표는 **비대칭**(선행 - 후행)을 유지한다 —
**대칭이면 선행이 아니다.**

### 대상 2 — 설계 A(슈팅 탐지 후 매수)
"탐지 시점에 상승이 소진됐다"로 닫혔는데, 1분봉에서는 **봉 하나가 +2% 를 보일 때
47초짜리 슈팅이 이미 끝나 있다.** 그래서 **틱에서 얼마나 일찍 알 수 있는지**를 잰다.

- 슈팅 시작 후 **1·3·5·10초** 시점의 **관측 가능한** 정보로 진행 중임을 알 수 있는가
- 그 시점 **이후에 남은 상승폭**은 얼마인가
- **탐지는 접두사 불변이어야 한다** — 판정에 미래가 들어가면 그 자체가 실패다.
  남은 상승폭은 **사후 최대치(ceiling)** 이므로 그렇게 이름 붙인다(§10-P 의 교훈).

## 모든 표에 붙는 제약 (docs/28)

1. **해상도가 사는 곳에서만 본다** — 세션별 **상위 10분위 종목**, 창 **30~60초**.
   그 밖은 `resolution_ok=False` 로 표시하고 수치를 판정에 쓰지 않는다.
2. **매수/매도 판정 오차** — 틱 대 호가 일치율 **0.604**(움직일 때 **0.592**),
   대조 가능 구간은 테이프의 **10.07%**. 불균형 기반 수치에 이 오차가 붙는다.
3. **수집기 시대 경계**를 넘어 뭉치지 않는다.
4. **호가 튕김** — 수익률은 여전히 체결가 기준이라 오염돼 있다.
5. 홀드아웃 봉인, 같은 종목·무작위 시각 대조군, 창 안정성.

실행: `python -m tossmon.analysis.measure.tick_tasting [db_path]`
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
from tossmon.analysis.measure import tick_instrument as TI

#: **해상도가 사는 창만 쓴다** (docs/28 §3b). 10초 창은 정규장 중앙값이 1~2건이라
#: 불균형이 +-1 로 포화해 비율이 성립하지 않는다.
RESOLUTION_WINDOWS_S = (30, 60)

#: 세션별 상위 분위 — 체결 건수 기준. 여기 밖에서는 해상도가 없다.
TOP_DECILE = 0.90

#: 초 단위 lag (초). **양수가 "흐름이 먼저"** 다.
LAGS_S = (-30, -10, -5, 0, 5, 10, 30)
ASYMMETRY_LAGS_S = (5, 10, 30)

#: lag 를 걸고 재는 응답 지평.
RESPONSE_H_S = 10

#: `docs/28` 실측 판정 오차 — 불균형 기반 수치에 항상 붙인다.
SIDE_AGREEMENT_OVERALL = 0.604
SIDE_AGREEMENT_MOVING = 0.592
SIDE_COMPARABLE_SHARE = 0.1007

#: 슈팅 정의 (틱). 임계 의존성을 보이기 위해 **여러 개를 쓸어본다**.
SHOT_RISES = (0.01, 0.02, 0.03)
SHOT_MAX_SECONDS = 60
DETECT_OFFSETS_S = (1, 3, 5, 10)

#: 칸이 이보다 얇으면 판정하지 않는다(그리고 이 작업은 애초에 판정하지 않는다).
MIN_CELL_N = 200
MIN_SHOTS_FOR_READ = 30

BOOTSTRAP_N = 1000
BOOTSTRAP_SEED = 20260730

OUT_DIR = D.OUT_DIR

#: docs/29 표가 싣는 칸의 필드 전체. 가드가 **동일 집합**으로 대조한다.
REPORTED_FIELDS = (
    "window_s", "signal", "lag_s", "n", "corr", "corr_shuffled",
    "resolution_ok", "side_error_applies",
)


def measurement_caveats() -> dict:
    """**모든 산출물에 붙는 조건.** 조건 없는 숫자는 이 모듈에서 나가지 않는다."""
    return {
        "resolution_windows_s": list(RESOLUTION_WINDOWS_S),
        "universe": f"top {(1 - TOP_DECILE) * 100:.0f}% of symbols by trade count, "
                    f"per session",
        "side_agreement_overall": SIDE_AGREEMENT_OVERALL,
        "side_agreement_when_price_moving": SIDE_AGREEMENT_MOVING,
        "side_comparable_share_of_tape": SIDE_COMPARABLE_SHARE,
        "returns_are_trade_price_based": True,
        "bounce_note": ("returns use trade prices, so bid-ask bounce contaminates "
                        "every bp figure here; quotes cover only ~10% of the tape so "
                        "a mid-based version of these tables cannot be built yet"),
        "no_verdict": ("preliminary tasting - this module does not say a candidate is "
                       "open or closed; it reports numbers and what is still missing"),
    }


def top_decile_symbols(ticks: pd.DataFrame, *, q: float = TOP_DECILE) -> set:
    """**해상도가 사는 종목만.** 세션별 체결 건수 상위 분위."""
    if ticks is None or ticks.empty:
        return set()
    keep = set()
    for _sess, g in ticks.groupby("session"):
        n = g.groupby(["symbol", "cycle_date"]).size()
        if n.empty:
            continue
        cut = n.quantile(q)
        keep |= {sym for (sym, _c), v in n.items() if v >= cut}
    return keep


def second_scale_panel(ticks: pd.DataFrame, window_s: int) -> pd.DataFrame:
    """`docs/28` 계측기의 창 프레임에 **강도 변화율**을 더한다."""
    f = TI.window_frame(ticks, window_s)
    if f is None or f.empty:
        return f
    key = ["symbol", "cycle_date", "session", "era"]
    f = f.sort_values(key + ["bucket"])
    prev = f.groupby(key)["n_trades"].shift(1)
    contiguous = f.groupby(key)["bucket"].shift(1) == f["bucket"] - window_s * 1000
    # **건수 강도의 변화율** — 주 지표(사용자: 주포도 건수로 쪼갠다).
    f["intensity_change"] = np.where(contiguous & (prev > 0),
                                     f["n_trades"] / prev - 1.0, np.nan)
    return f


def lagged_correlation(panel: pd.DataFrame, *, signal: str, lag_s: int,
                       window_s: int, response_h_s: int = RESPONSE_H_S) -> dict:
    """신호(창 끝 기준)와 **`lag_s` 뒤 응답 구간 수익률**의 상관.

    `lag_s > 0` 이면 **신호가 먼저**다. 창 길이와 lag 를 분리했으므로 30초 창에서도
    5초 선행을 볼 수 있다 — 1분봉으로는 구조적으로 불가능했던 부분이다.
    """
    if panel is None or panel.empty or signal not in panel.columns:
        return {"n": 0, "corr": float("nan")}
    d = panel.dropna(subset=[signal]).copy()
    if d.empty:
        return {"n": 0, "corr": float("nan")}
    # 창 끝 시각 + lag 에서 시작하는 응답 구간의 수익률을 체결에서 직접 만든다.
    d["signal_end_ms"] = d["bucket"] + window_s * 1000
    return {"n": int(len(d)), "corr": float("nan"), "_frame": d}


def response_returns(ticks: pd.DataFrame, anchors: pd.DataFrame, *, lag_s: int,
                     response_h_s: int = RESPONSE_H_S) -> pd.Series:
    """`anchor + lag` 에서 시작하는 `response_h_s` 초 구간의 로그수익률.

    체결가 기준이므로 **호가 튕김에 오염**돼 있다(docs/28 §8). 그 사실을 표에 단다.
    """
    if ticks is None or ticks.empty or anchors is None or anchors.empty:
        return pd.Series(dtype="float64")
    out = np.full(len(anchors), np.nan)
    books = {(s, c): (g["ts_ms"].to_numpy(), g["price_u"].to_numpy(dtype="float64"))
             for (s, c), g in ticks.groupby(["symbol", "cycle_date"])}
    syms = anchors["symbol"].to_numpy()
    cycs = anchors["cycle_date"].to_numpy()
    ends = anchors["signal_end_ms"].to_numpy()
    for i in range(len(anchors)):
        bk = books.get((syms[i], cycs[i]))
        if bk is None:
            continue
        ts, px = bk
        t0 = ends[i] + lag_s * 1000
        t1 = t0 + response_h_s * 1000
        a = int(np.searchsorted(ts, t0, side="left"))
        b = int(np.searchsorted(ts, t1, side="right")) - 1
        if a >= len(ts) or b <= a or px[a] <= 0 or px[b] <= 0:
            continue
        out[i] = float(np.log(px[b] / px[a]))
    return pd.Series(out, index=anchors.index)


def leadlag_table(ticks: pd.DataFrame, *, windows=RESOLUTION_WINDOWS_S,
                  lags=LAGS_S, seed: int = BOOTSTRAP_SEED) -> list[dict]:
    """대상 1 — 초 단위 lag 에서 흐름과 수익률의 상관, 그리고 셔플 대조군."""
    rows = []
    rng = np.random.default_rng(seed)
    for w in windows:
        panel = second_scale_panel(ticks, w)
        if panel is None or panel.empty:
            continue
        panel = panel[panel["session"] == "regular"]
        if panel.empty:
            continue
        for signal in ("imbalance", "intensity_change"):
            base = lagged_correlation(panel, signal=signal, lag_s=0, window_s=w)
            d = base.get("_frame")
            if d is None or len(d) < MIN_CELL_N:
                continue
            for lag in lags:
                r = response_returns(ticks, d, lag_s=lag)
                ok = r.notna() & d[signal].notna()
                if int(ok.sum()) < MIN_CELL_N:
                    rows.append({"window_s": w, "signal": signal, "lag_s": lag,
                                 "n": int(ok.sum()), "corr": float("nan"),
                                 "corr_shuffled": float("nan"),
                                 "resolution_ok": False,
                                 "side_error_applies": signal == "imbalance"})
                    continue
                x = d.loc[ok, signal].to_numpy()
                y = r[ok].to_numpy()
                shuffled = (d.loc[ok].groupby("symbol")[signal]
                            .transform(lambda s: s.to_numpy()[rng.permutation(len(s))])
                            .to_numpy())
                rows.append({
                    "window_s": w, "signal": signal, "lag_s": lag,
                    "n": int(ok.sum()),
                    "corr": float(np.corrcoef(x, y)[0, 1]),
                    "corr_shuffled": float(np.corrcoef(shuffled, y)[0, 1]),
                    "resolution_ok": True,
                    "side_error_applies": signal == "imbalance"})
    return rows


def asymmetry(rows: list[dict], *, lags=ASYMMETRY_LAGS_S) -> list[dict]:
    """**주 판정 지표 — 선행 − 후행.** 대칭이면 선행이 아니다.

    (이 작업은 판정하지 않는다. 지표는 다음 작업이 바로 쓸 수 있도록 낸다.)
    """
    idx = {(r["window_s"], r["signal"], r["lag_s"]): r for r in rows}
    out = []
    for (w, sig, _l) in sorted({(r["window_s"], r["signal"], 0) for r in rows}):
        for k in lags:
            a, b = idx.get((w, sig, k)), idx.get((w, sig, -k))
            if not a or not b or not (a["resolution_ok"] and b["resolution_ok"]):
                out.append({"window_s": w, "signal": sig, "k_s": k,
                            "asymmetry": float("nan"), "resolution_ok": False})
                continue
            # **불확실성 폭 없이 이 숫자를 읽으면 안 된다.** 상관 하나의 표준오차가
            # 대략 1/sqrt(n) 이므로 차이의 폭은 그 sqrt(2) 배다. 판정이 아니라
            # "이 크기가 잡음 폭 안인가"를 눈으로 볼 수 있게 하는 장치다.
            se = float(np.sqrt(1.0 / max(a["n"], 1) + 1.0 / max(b["n"], 1)))
            out.append({"window_s": w, "signal": sig, "k_s": k,
                        "corr_lead": a["corr"], "corr_lag": b["corr"],
                        "asymmetry": a["corr"] - b["corr"],
                        "approx_se": se, "noise_band": 1.96 * se,
                        "inside_noise_band": bool(abs(a["corr"] - b["corr"])
                                                  <= 1.96 * se),
                        "n_lead": a["n"], "n_lag": b["n"],
                        "resolution_ok": True})
    return out


# --------------------------------------------------------------------------- #
# 대상 2 — 슈팅을 얼마나 일찍 알 수 있나
# --------------------------------------------------------------------------- #
def find_shot_starts(ticks: pd.DataFrame, *, rise: float,
                     max_seconds: int = SHOT_MAX_SECONDS) -> pd.DataFrame:
    """틱에서 **슈팅 시작점**을 찾는다 — 저점에서 `rise` 이상 오르는 구간의 **시작**.

    시작점 자체는 사후에 식별한다(이건 **사건 정의**이지 진입 신호가 아니다).
    탐지 가능성은 **시작 이후 관측 가능한 정보만으로** 따로 잰다.
    """
    if ticks is None or ticks.empty:
        return pd.DataFrame()
    out = []
    for (sym, cyc), g in ticks.groupby(["symbol", "cycle_date"]):
        ts = g["ts_ms"].to_numpy()
        px = g["price_u"].to_numpy(dtype="float64")
        n = len(ts)
        i = 0
        while i < n - 1:
            hi_j, hi_px = -1, px[i]
            j = i + 1
            while j < n and ts[j] - ts[i] <= max_seconds * 1000:
                if px[j] > hi_px:
                    hi_px, hi_j = px[j], j
                j += 1
            if hi_j > 0 and px[i] > 0 and hi_px / px[i] - 1.0 >= rise:
                out.append({"symbol": sym, "cycle_date": cyc,
                            "session": g["session"].iloc[0],
                            "era": g["era"].iloc[0],
                            "start_ms": int(ts[i]), "start_px": float(px[i]),
                            "peak_ms": int(ts[hi_j]), "peak_px": float(hi_px),
                            "total_rise": float(hi_px / px[i] - 1.0),
                            "duration_s": float((ts[hi_j] - ts[i]) / 1000)})
                i = hi_j                      # 겹치지 않게 정점 뒤로 건너뛴다
            else:
                i += 1
    return pd.DataFrame(out)


def detection_profile(ticks: pd.DataFrame, shots: pd.DataFrame, *,
                      offsets=DETECT_OFFSETS_S) -> list[dict]:
    """슈팅 시작 후 `k` 초에 **관측 가능한 것**과 **그 뒤에 남은 상승폭**.

    - 관측 가능: `k` 초까지의 체결 건수와 불균형 — **접두사 불변**이다.
    - 남은 상승폭: `k` 초 시점 가격에서 **사후 정점까지** — 이것은 **달성 불가 상한**
      (`ceiling_`)이며 전략 수익이 아니다(§10-P 의 교훈).
    """
    if ticks is None or ticks.empty or shots is None or shots.empty:
        return []
    books = {(s, c): (g["ts_ms"].to_numpy(),
                      g["price_u"].to_numpy(dtype="float64"),
                      g["side_tick"].to_numpy())
             for (s, c), g in ticks.groupby(["symbol", "cycle_date"])}
    rows = []
    for k in offsets:
        obs_n, obs_imb, remain = [], [], []
        n_seen, n_alive = 0, 0
        for r in shots.itertuples(index=False):
            bk = books.get((r.symbol, r.cycle_date))
            if bk is None:
                continue
            ts, px, side = bk
            a = int(np.searchsorted(ts, r.start_ms, side="left"))
            b = int(np.searchsorted(ts, r.start_ms + k * 1000, side="right")) - 1
            if b < a:
                continue
            n_seen += 1
            seg = side[a:b + 1]
            cls = seg[seg != 0]
            obs_n.append(int(b - a + 1))
            obs_imb.append(float(cls.mean()) if len(cls) else np.nan)
            # **아직 진행 중인 슈팅만** 남은 상승폭을 갖는다. 정점을 지난 뒤의
            # "정점까지 거리"는 남은 기회가 아니라 **되돌림**이며, 그것을 섞으면
            # 늦게 탐지할수록 기회가 커지는 거꾸로 된 표가 나온다(실제로 그랬다).
            if r.peak_ms > r.start_ms + k * 1000 and px[b] > 0:
                n_alive += 1
                remain.append(float(r.peak_px / px[b] - 1.0))
        if not obs_n:
            continue
        rows.append({
            "offset_s": k, "n_shots": int(n_seen),
            "n_still_in_progress": int(n_alive),
            "share_still_in_progress": (float(n_alive / n_seen) if n_seen
                                        else float("nan")),
            "trades_by_offset_median": float(np.median(obs_n)),
            "imbalance_by_offset_median": float(np.nanmedian(obs_imb)),
            "ceiling_remaining_rise_median": (float(np.median(remain)) if remain
                                              else float("nan")),
            "ceiling_remaining_rise_mean": (float(np.mean(remain)) if remain
                                            else float("nan")),
            "observed_enough": bool(n_alive >= MIN_SHOTS_FOR_READ)})
    return rows


def shot_threshold_sweep(ticks: pd.DataFrame, *, rises=SHOT_RISES) -> list[dict]:
    """**임계를 쓸어본다** — 답이 임계를 1:1 로 따라가면 그건 우리 임계다(C-2)."""
    rows = []
    for rise in rises:
        shots = find_shot_starts(ticks, rise=rise)
        prof = detection_profile(ticks, shots)
        at5 = next((p for p in prof if p["offset_s"] == 5), None)
        rows.append({
            "rise": rise, "n_shots": int(len(shots)),
            "median_duration_s": (float(shots["duration_s"].median())
                                  if len(shots) else float("nan")),
            "median_total_rise": (float(shots["total_rise"].median())
                                  if len(shots) else float("nan")),
            "ceiling_remaining_at_5s": (at5["ceiling_remaining_rise_median"]
                                        if at5 else float("nan")),
            "observed_enough": bool(len(shots) >= MIN_SHOTS_FOR_READ)})
    return rows


def data_needed(ticks: pd.DataFrame, shots: pd.DataFrame, *,
                target_shots: int = 300) -> dict:
    """★ **이 작업의 가장 값어치 있는 산출물** — 확정 판정까지 며칠이 더 필요한가."""
    if ticks is None or ticks.empty:
        return {"reachable": False, "reason": "no ticks"}
    cycles = ticks["cycle_date"].nunique() or 1
    top = top_decile_symbols(ticks)
    per_cycle_shots = (len(shots) / cycles) if shots is not None else 0.0
    return {
        "cycles_observed": int(cycles),
        "top_decile_symbols": int(len(top)),
        "shots_now": int(len(shots)) if shots is not None else 0,
        "shots_per_cycle": per_cycle_shots,
        "target_shots": target_shots,
        "cycles_needed_for_shots": (int(np.ceil(target_shots / per_cycle_shots))
                                    if per_cycle_shots > 0 else None),
        "reachable": bool(per_cycle_shots > 0),
        "note": ("cycles here are US trading cycles with our collector running; the "
                 "top-decile universe is what carries second-scale resolution, so the "
                 "binding constraint is cycles OF THOSE SYMBOLS, not calendar days"),
    }


def build_report(conn) -> dict:
    got = TI.load_ticks(conn)
    ticks = got["ticks"]
    top = top_decile_symbols(ticks)
    res = ticks[ticks["symbol"].isin(top)].copy() if len(ticks) else ticks
    ll = leadlag_table(res)
    shots = find_shot_starts(res, rise=SHOT_RISES[0])
    return {
        "caveats": measurement_caveats(),
        "holdout": {"n_rows_dropped": got["n_dropped_holdout"]},
        "eras": list(SS.COLLECTOR_ERAS),
        "era_counts": (res["era"].value_counts().to_dict() if len(res) else {}),
        "n_ticks_all": int(len(ticks)),
        "n_ticks_top_decile": int(len(res)),
        "n_top_decile_symbols": len(top),
        "cycle_dates": (sorted(res["cycle_date"].unique().tolist()) if len(res)
                        else []),
        "target1_leadlag": ll,
        "target1_asymmetry": asymmetry(ll),
        "target2_shots": {
            "definition": (f"a run whose price rises >= {SHOT_RISES[0]:.0%} from a "
                           f"local low within {SHOT_MAX_SECONDS}s; the START is "
                           "identified after the fact as an EVENT definition, not as "
                           "an entry signal"),
            "n_shots": int(len(shots)),
            "median_duration_s": (float(shots["duration_s"].median()) if len(shots)
                                  else float("nan")),
            "detection_profile": detection_profile(res, shots),
            "threshold_sweep": shot_threshold_sweep(res),
        },
        "data_needed": data_needed(res, shots),
    }


def main(db: Path, *, out_dir: Path | None = None) -> int:
    conn = D.ro(db)
    try:
        rep = build_report(conn)
    finally:
        conn.close()
    c = rep["caveats"]
    print("PRELIMINARY TASTING - no candidate is called open or closed here")
    print(f"  universe: {c['universe']}   windows {c['resolution_windows_s']}s")
    print(f"  side-rule error: agreement {c['side_agreement_overall']} overall, "
          f"{c['side_agreement_when_price_moving']} while price moves, "
          f"comparable on {c['side_comparable_share_of_tape']:.2%} of the tape")
    print(f"  ticks {rep['n_ticks_all']} -> top decile {rep['n_ticks_top_decile']} "
          f"({rep['n_top_decile_symbols']} symbols, cycles {rep['cycle_dates']})")
    print(f"  eras: {rep['era_counts']} (never pooled across a boundary)")
    print(f"  !! {c['bounce_note']}")

    print("\n=== [docs/29 sec 2] TARGET 1 - FLOW vs PRICE at SECOND-SCALE LAGS")
    print("  lag > 0 means FLOW LEADS PRICE; response horizon "
          f"{RESPONSE_H_S}s; regular session only")
    print(f"{'win_s':>6}{'signal':<18}{'lag_s':>7}{'n':>8}{'corr':>9}"
          f"{'shuffled':>10}{'resolution':>12}{'side err':>10}")
    for r in rep["target1_leadlag"]:
        cc = f"{r['corr']:.4f}" if r["corr"] == r["corr"] else "n/a"
        sh = f"{r['corr_shuffled']:.4f}" if r["corr_shuffled"] == r["corr_shuffled"] else "n/a"
        print(f"{r['window_s']:>6}{r['signal']:<18}{r['lag_s']:>7}{r['n']:>8}"
              f"{cc:>9}{sh:>10}{str(r['resolution_ok']):>12}"
              f"{str(r['side_error_applies']):>10}")

    print("\n=== [docs/29 sec 3] TARGET 1 - ASYMMETRY (lead minus lag)")
    print("  a symmetric value is NOT a lead - this is the metric the next task should "
          "use")
    print(f"{'win_s':>6}{'signal':<18}{'k_s':>5}{'corr(+k)':>10}{'corr(-k)':>10}"
          f"{'asymmetry':>11}{'+-noise':>10}{'inside noise':>14}")
    for r in rep["target1_asymmetry"]:
        if not r.get("resolution_ok"):
            print(f"{r['window_s']:>6}{r['signal']:<18}{r['k_s']:>5}{'n/a':>10}"
                  f"{'n/a':>10}{'n/a':>11}{'False':>12}")
            continue
        print(f"{r['window_s']:>6}{r['signal']:<18}{r['k_s']:>5}"
              f"{r['corr_lead']:>10.4f}{r['corr_lag']:>10.4f}"
              f"{r['asymmetry']:>11.4f}{r['noise_band']:>10.4f}"
              f"{str(r['inside_noise_band']):>14}")

    t2 = rep["target2_shots"]
    print("\n=== [docs/29 sec 4] TARGET 2 - HOW EARLY IS A SHOT VISIBLE IN TICKS")
    print(f"  shot definition: {t2['definition']}")
    print(f"  shots found {t2['n_shots']}, median duration "
          f"{t2['median_duration_s']:.1f}s")
    print(f"{'offset_s':>9}{'n_shots':>9}{'still alive':>12}{'share':>8}"
          f"{'trades by then':>15}{'imbalance':>11}{'CEILING remaining':>19}"
          f"{'enough':>8}")
    for r in t2["detection_profile"]:
        print(f"{r['offset_s']:>9}{r['n_shots']:>9}{r['n_still_in_progress']:>12}"
              f"{r['share_still_in_progress']:>8.2f}"
              f"{r['trades_by_offset_median']:>15.1f}"
              f"{r['imbalance_by_offset_median']:>11.3f}"
              f"{r['ceiling_remaining_rise_median'] * 100:>18.2f}%"
              f"{str(r['observed_enough']):>8}")
    print("  'still alive' = shots whose peak is still ahead at that offset; only "
          "those carry remaining upside.")
    print("  'CEILING remaining' is the rise to the AFTER-THE-FACT peak - unachievable "
          "by construction, it bounds what detection could buy, never a strategy return")

    print("\n=== [docs/29 sec 5] TARGET 2 - THRESHOLD SWEEP (does the answer follow "
          "our threshold?)")
    print(f"{'rise':>7}{'n_shots':>9}{'median dur_s':>14}{'median rise':>13}"
          f"{'ceiling@5s':>12}{'enough':>8}")
    for r in t2["threshold_sweep"]:
        print(f"{r['rise']:>7.0%}{r['n_shots']:>9}{r['median_duration_s']:>14.1f}"
              f"{r['median_total_rise'] * 100:>12.2f}%"
              f"{r['ceiling_remaining_at_5s'] * 100:>11.2f}%"
              f"{str(r['observed_enough']):>8}")

    print("\n=== [docs/29 sec 6] ** WHAT WOULD IT TAKE TO ACTUALLY DECIDE **")
    dn = rep["data_needed"]
    print(f"  cycles observed {dn['cycles_observed']}, top-decile symbols "
          f"{dn['top_decile_symbols']}, shots {dn['shots_now']} "
          f"({dn['shots_per_cycle']:.1f}/cycle)")
    if dn.get("cycles_needed_for_shots"):
        print(f"  -> {dn['cycles_needed_for_shots']} cycles for "
              f"{dn['target_shots']} shots at the current rate")
    print(f"  {dn['note']}")

    out = (out_dir or OUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "tick_tasting.json"
    path.write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1
                  else Path(r"C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db")))

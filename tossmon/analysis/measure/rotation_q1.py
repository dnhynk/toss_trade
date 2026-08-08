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

#: **비대칭 검정**에서 볼 lag 크기. 주 판정은 `corr(+k) - corr(-k)` 다.
ASYMMETRY_K = (1, 2, 3, 4, 5)

#: 경제적 유의성 환산에 쓰는 **정규장 실측 왕복 비용** (docs/23 §10-R.5):
#: 유효 스프레드 중앙 0.46% + 수수료 0.2%. 통계적 유의성만으로는 아무 의미가 없다.
EFFECTIVE_SPREAD_REGULAR = D.MEASURED_EFFECTIVE_SPREAD_REGULAR
REALIZED_ROUND_TRIP = D.MEASURED_ROUND_TRIP_REGULAR

#: 경제적 환산에서 쓰는 횡단면 분위 수. 매 분 상위 분위를 사는 규칙을 흉내낸다.
N_BUCKETS = 10

BOOTSTRAP_N = 1000
BOOTSTRAP_SEED = 20260730

OUT_DIR = D.OUT_DIR

#: docs/26 결과표가 싣는 필드 전체. 가드가 **동일 집합**으로 대조한다.
REPORTED_FIELDS = (
    "session", "lag_min", "n_obs", "corr_real", "corr_placebo",
    "diff", "ci_low", "ci_high", "verdict",
)

#: 비대칭 검정표가 싣는 필드 전체.
ASYMMETRY_FIELDS = (
    "session", "k", "n_minutes", "corr_lead", "corr_lag", "asymmetry",
    "ci_low", "ci_high", "ci_low_bonferroni", "ci_high_bonferroni",
    "n_comparisons", "verdict",
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
        # 캔들은 **종료 시각 라벨**이라 세션·사이클 소속은 담는 구간으로 판정한다
        # (docs/12 §6.1). 체결 테이프에는 이 보정을 쓰지 않는다 — 그쪽은 진짜 순간이다.
        b = SS.bar_starts_ms(kept["ts_ms"])
        kept["session"] = SS.sessions_of(b)
        kept["cycle_date"] = b.map(lambda m: SS.session_date(int(m)))
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


def asymmetry_test(panel: pd.DataFrame, *, ks=ASYMMETRY_K,
                   seeds=PLACEBO_SEEDS) -> list[dict]:
    """**주 판정 — 선행이 후행보다 센가.** `corr(+k) - corr(-k)` 의 쌍체 부트스트랩 CI.

    ## 왜 "선행 셀이 하나라도 0 을 넘나"를 버렸나

    그 규칙은 셀이 55개(세션 5 x lag 11)라 **다중검정에 무방비**였고, 실제로
    **자료가 늘자 답이 뒤집혔다**(`regular lag +1` 이 0 교차 -> 0 초과).
    사전등록 규칙이 실행 시점에 따라 달라지면 규칙이 아니다.

    ## 왜 이 지표가 옳은가

    관측된 모양은 lag -1 / 0 / +1 이 **거의 같은 크기**이고 +-2 에서 꺼진다 —
    선행이 아니라 **동시 관계**다. `corr(+k) - corr(-k)` 는 그 **동시 성분이 상쇄**되므로
    "돈이 먼저인가 가격이 먼저인가"만 남는다. 대칭이면 0 이고, 그러면 **선행 없음**이다.

    쌍체로 재는 이유: 같은 분(minute) 군집을 **동시에** 재표집해야 두 상관의 공통
    변동이 상쇄된다. 따로 재표집하면 차이의 분산이 부풀려진다.
    """
    if panel is None or panel.empty:
        return []
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    sessions = [s for s in SS.SESSIONS]
    # 본페로니 분모는 **실제로 판정한 칸 수**다 — 미리 세어 둔다.
    judged = []
    cache: dict = {}
    for k in ks:
        pr_p, pr_m = lagged_pairs(panel, k), lagged_pairs(panel, -k)
        for sess in sessions:
            a = pr_p[pr_p["session"] == sess] if len(pr_p) else pr_p
            b = pr_m[pr_m["session"] == sess] if len(pr_m) else pr_m
            if a is None or b is None or len(a) < 3 or len(b) < 3:
                cache[(k, sess)] = None
                continue
            sa = minute_stats(a["dshare"].to_numpy(), a["ret"].to_numpy(),
                              a["ts_ms"].to_numpy())
            sb = minute_stats(b["dshare"].to_numpy(), b["ret"].to_numpy(),
                              b["ts_ms"].to_numpy())
            common = sa.index.intersection(sb.index)
            cache[(k, sess)] = (sa.loc[common], sb.loc[common], len(a), len(b))
            if len(common) >= 2 and min(len(a), len(b)) >= MIN_OBS_FOR_VERDICT:
                judged.append((k, sess))
    m = max(1, len(judged))
    rows = []
    for k in ks:
        for sess in sessions:
            got = cache.get((k, sess))
            if got is None:
                rows.append({"session": sess, "k": k, "n_minutes": 0,
                             "corr_lead": float("nan"), "corr_lag": float("nan"),
                             "asymmetry": float("nan"), "ci_low": float("nan"),
                             "ci_high": float("nan"),
                             "ci_low_bonferroni": float("nan"),
                             "ci_high_bonferroni": float("nan"),
                             "n_comparisons": m, "verdict": "insufficient"})
                continue
            sa, sb, na, nb = got
            c_lead, c_lag = corr_from_stats(sa), corr_from_stats(sb)
            keys = sa.index.to_numpy()
            powered = (k, sess) in judged
            diffs = []
            for _ in range(BOOTSTRAP_N if powered else 0):
                # **같은 분 목록으로 두 상관을 동시에** 다시 계산한다 (쌍체).
                pick = keys[rng.integers(0, len(keys), size=len(keys))]
                x, y = corr_from_stats(sa.loc[pick]), corr_from_stats(sb.loc[pick])
                if x == x and y == y:
                    diffs.append(x - y)
            if len(diffs) >= 100:
                lo, hi = (float(np.percentile(diffs, 2.5)),
                          float(np.percentile(diffs, 97.5)))
                ab = 100.0 * (0.05 / m) / 2.0
                blo, bhi = (float(np.percentile(diffs, ab)),
                            float(np.percentile(diffs, 100.0 - ab)))
            else:
                lo = hi = blo = bhi = float("nan")
            verdict = ("insufficient" if not powered or blo != blo else
                       "lead_stronger" if blo > 0 else
                       "lag_stronger" if bhi < 0 else "symmetric")
            rows.append({"session": sess, "k": k, "n_minutes": int(len(keys)),
                         "corr_lead": c_lead, "corr_lag": c_lag,
                         "asymmetry": (c_lead - c_lag
                                       if c_lead == c_lead and c_lag == c_lag
                                       else float("nan")),
                         "ci_low": lo, "ci_high": hi,
                         "ci_low_bonferroni": blo, "ci_high_bonferroni": bhi,
                         "n_comparisons": m, "verdict": verdict})
    return rows


def economic_significance(panel: pd.DataFrame, *, lag: int = 1,
                          n_buckets: int = N_BUCKETS) -> list[dict]:
    """**통계적 유의성만으로는 무의미하다** — 실제 매매 규칙으로 몇 bp 인가.

    n 이 10만이면 상관 0.02 도 유의해지지만 0.02 는 **분산의 0.04%** 다. 그래서
    신호를 그대로 규칙으로 바꾼다: **매 분 횡단면으로 `dshare` 상위 분위를 사고
    1분 뒤 판다.** 그 평균 수익률(bp)을 **정규장 실측 왕복 비용**과 나란히 놓는다.
    """
    if panel is None or panel.empty:
        return []
    pr = lagged_pairs(panel, lag)
    if pr is None or pr.empty:
        return []
    rows = []
    for sess in SS.SESSIONS:
        d = pr[pr["session"] == sess]
        if len(d) < MIN_OBS_FOR_VERDICT:
            rows.append({"session": sess, "n": int(len(d)), "powered": False})
            continue
        # 매 분 안에서 순위 -> 실제로 그 시점에 할 수 있는 선택만 쓴다.
        rank = d.groupby("ts_ms")["dshare"].rank(pct=True, method="first")
        top = d.loc[rank > 1.0 - 1.0 / n_buckets, "ret"]
        bot = d.loc[rank <= 1.0 / n_buckets, "ret"]
        allr = d["ret"]
        top_bp = float(top.mean() * 1e4) if len(top) else float("nan")
        rows.append({
            "session": sess, "n": int(len(d)), "powered": True,
            "n_top": int(len(top)),
            "top_decile_bp_per_min": top_bp,
            "bottom_decile_bp_per_min": (float(bot.mean() * 1e4) if len(bot)
                                         else float("nan")),
            "spread_bp": (float((top.mean() - bot.mean()) * 1e4)
                          if len(top) and len(bot) else float("nan")),
            "all_bp": float(allr.mean() * 1e4) if len(allr) else float("nan"),
            "round_trip_cost_bp": REALIZED_ROUND_TRIP * 1e4,
            "net_bp": top_bp - REALIZED_ROUND_TRIP * 1e4,
            "variance_explained": None,
        })
    return rows


def q1_gate(rows: list[dict], asym: list[dict] | None = None,
            econ: list[dict] | None = None) -> dict:
    """**사전 등록된 중단 기준을 코드로 집행한다 (비대칭 판정으로 개정).**

    구 규칙("선행 셀이 하나라도 0 을 넘나")은 셀 55개에 다중검정 무방비였고
    **자료가 늘자 답이 뒤집혔다.** 개정 규칙은 **선행이 후행보다 센가**만 본다:
    본페로니 보정 후 `corr(+k) - corr(-k)` 의 CI 가 0 을 초과하는 `k` 가 있어야 한다.
    """
    out = {"by_session": {}, "proceed_to_q2": False,
           "rule": ("asymmetry: corr(+k) - corr(-k) must exceed zero after "
                    "Bonferroni correction; a symmetric peak is NOT a lead")}
    for sess in SS.SESSIONS:
        a = [x for x in (asym or []) if x["session"] == sess
             and x["verdict"] != "insufficient"]
        if not a:
            out["by_session"][sess] = {"decision": "insufficient", "n_k": 0}
            continue
        lead = [x for x in a if x["verdict"] == "lead_stronger"]
        best = max(a, key=lambda x: x["asymmetry"] if x["asymmetry"] == x["asymmetry"]
                   else -9e9)
        e = next((x for x in (econ or []) if x["session"] == sess
                  and x.get("powered")), None)
        out["by_session"][sess] = {
            "decision": "proceed" if lead else "stop",
            "n_k": len(a),
            "n_k_lead_stronger": len(lead),
            "max_asymmetry": best["asymmetry"], "max_asymmetry_k": best["k"],
            "net_bp_at_top_decile": (e.get("net_bp") if e else None),
            "reason": ("lead side is stronger than lag side" if lead else
                       "lead and lag sides are indistinguishable - symmetric "
                       "association is a SIMULTANEOUS relation, not a lead")}
        if lead:
            out["proceed_to_q2"] = True
    return out



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
    asym = asymmetry_test(panel)
    econ = economic_significance(panel)
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
        "session_counts": (SS.session_counts(SS.bar_starts_ms(panel["ts_ms"]))
                           if len(panel) else {}),         # 캔들 축 (docs/12 §6.1)
        "cross_correlation": rows,
        "asymmetry": asym,
        "economic": econ,
        "gate": q1_gate(rows, asym, econ),
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

    print("\n=== [docs/26 Q1b] ASYMMETRY corr(+k) - corr(-k)  [PRIMARY TEST]")
    print("  a symmetric peak at -1/0/+1 is a SIMULTANEOUS relation, not a lead;")
    print("  this statistic cancels that common component. Bonferroni over "
          f"{(rep['asymmetry'][0]['n_comparisons'] if rep['asymmetry'] else 0)} cells.")
    print(f"{'session':<9}{'k':>3}{'minutes':>9}{'corr +k':>9}{'corr -k':>9}"
          f"{'asym':>9}{'CI low':>9}{'CI high':>9}{'Bonf low':>10}{'Bonf high':>10}"
          f"{'verdict':>16}")
    for r in rep["asymmetry"]:
        if r["verdict"] == "insufficient":
            continue
        print(f"{r['session']:<9}{r['k']:>3}{r['n_minutes']:>9}{r['corr_lead']:>9.4f}"
              f"{r['corr_lag']:>9.4f}{r['asymmetry']:>9.4f}{r['ci_low']:>9.4f}"
              f"{r['ci_high']:>9.4f}{r['ci_low_bonferroni']:>10.4f}"
              f"{r['ci_high_bonferroni']:>10.4f}{r['verdict']:>16}")

    print("\n=== [docs/26 Q1c] ECONOMIC SIGNIFICANCE - bp per minute vs measured cost")
    print("  rule: each minute, buy the top decile by dshare, sell one minute later")
    print(f"{'session':<9}{'n':>8}{'top bp':>9}{'bottom bp':>11}{'spread bp':>11}"
          f"{'cost bp':>9}{'net bp':>9}")
    for e in rep["economic"]:
        if not e.get("powered"):
            continue
        print(f"{e['session']:<9}{e['n']:>8}{e['top_decile_bp_per_min']:>9.2f}"
              f"{e['bottom_decile_bp_per_min']:>11.2f}{e['spread_bp']:>11.2f}"
              f"{e['round_trip_cost_bp']:>9.1f}{e['net_bp']:>9.2f}")
    print("  cost = regular effective spread 0.46pct + commission 0.2pct (docs/23 "
          "sec 10-R.5). A correlation of 0.02 explains 0.04pct of variance.")

    print("\n=== [docs/26 Q1] PREREGISTERED STOP RULE")
    print(f"  rule: {rep['gate']['rule']}")
    for sess, g in rep["gate"]["by_session"].items():
        if g.get("n_k", 0) == 0:
            continue
        net = g.get("net_bp_at_top_decile")
        net_s = f", net {net:+.2f} bp/min" if net is not None else ""
        print(f"  {sess:<9} {g['decision'].upper():<12} max asymmetry "
              f"{g['max_asymmetry']:+.4f} at k={g['max_asymmetry_k']}, "
              f"k with lead stronger: {g['n_k_lead_stronger']}/{g['n_k']}{net_s}")
        print(f"             -> {g['reason']}")
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

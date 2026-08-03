"""감사5 재현 하네스 — 설계 B(docs/23 §10.2)의 +2.36% 를 재현하고 공격한다.

docs/23 §10 의 어떤 수치도 커밋된 실행 스크립트가 없다(감사 F-1). 이 파일은 감사용으로
그 측정을 **밖에서 다시 세운다**. 프로덕션 코드가 아니며 `tossmon/` 을 고치지 않는다.

라이브 0콜. 수집 DB 는 `mode=ro` 로만 연다.

실행: python -m tools.audit5_designb [db_path]
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from tossmon.analysis import shots as S

DEFAULT_DB = Path(r"C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db")
VOLUME = "TOSS_SECURITIES_TRADING_VOLUME"
MIN_SNAPS = 20
RNG_SEED = 20260803


def ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)


def boot_ci(v: np.ndarray, *, n: int = 10000, seed: int = RNG_SEED) -> tuple:
    """평균의 부트스트랩 백분위 95% CI. docs/23 의 비대칭 CI 와 형식을 맞춘다."""
    v = np.asarray(v, dtype="float64")
    v = v[np.isfinite(v)]
    if v.size < 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    means = rng.choice(v, size=(n, v.size), replace=True).mean(axis=1)
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def cluster_boot_ci(v: np.ndarray, groups: np.ndarray, *,
                    n: int = 10000, seed: int = RNG_SEED) -> tuple:
    """**군집 부트스트랩** — 표본이 아니라 군집(종목/일)을 통째로 재추출한다.

    같은 종목·같은 날의 진입은 독립이 아니다. 개별 재추출은 CI 를 과도하게 좁힌다.
    """
    v = np.asarray(v, dtype="float64")
    groups = np.asarray(groups)
    ok = np.isfinite(v)
    v, groups = v[ok], groups[ok]
    uniq = np.unique(groups)
    if uniq.size < 2:
        return (float("nan"), float("nan"))
    buckets = [v[groups == g] for g in uniq]
    rng = np.random.default_rng(seed)
    means = np.empty(n)
    for i in range(n):
        pick = rng.integers(0, len(buckets), len(buckets))
        means[i] = np.concatenate([buckets[j] for j in pick]).mean()
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def load(conn, day: str, ranking_type: str = VOLUME) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT snap_ms, ranking_type, rank, symbol, last_u FROM rankings_snap "
        "WHERE ranking_type=? AND date(snap_ms/1000,'unixepoch')=? "
        "ORDER BY symbol, snap_ms", conn, params=(ranking_type, day))


def universe(conn) -> dict:
    return {r[0]: r[1] for r in
            conn.execute("SELECT symbol, shares_outstanding_qu FROM symbols")}


def entry_delayed(series: pd.Series, entry_ms: int, delay_s: int) -> tuple:
    """진입 신호 뒤 `delay_s` 를 두고 **다음 관측가**에 체결됐다고 본다.

    설계 A 는 `detect_ms + delay` 를 적용했다. 설계 B 진입에는 적용되지 않았다 — 그
    비대칭이 감사 대상이다. 신호봉 이후 첫 관측만 쓰므로 미래를 보지 않는다.
    """
    if delay_s <= 0:
        return float(series.loc[entry_ms]), int(entry_ms)
    later = series[series.index >= entry_ms + delay_s * 1000]
    if later.empty:
        return float("nan"), -1
    return float(later.iloc[0]), int(later.index[0])


def observable_shot_exit(series: pd.Series, shots: pd.DataFrame, entry_ms: int,
                         *, n: int = 1, horizon_s: int = 1800,
                         delay_s: int = 0) -> tuple:
    """**관측 가능한** 슈팅 이탈 — 고점이 아니라 `detect_ms(+지연)` 에 판다.

    `shot_exit_from_entry` 는 `peak_u` 에 판다. 고점은 지나야 알 수 있으므로 그것은
    사후값이다. 여기서는 슈팅이 슈팅임을 알게 된 시각의 관측가에 나간다.
    """
    if shots is None or len(shots) == 0:
        return float("nan"), -1
    after = shots[(shots["detect_ms"] > entry_ms)
                  & (shots["detect_ms"] <= entry_ms + horizon_s * 1000)]
    if len(after) < n:
        return float("nan"), -1
    d_ms = int(after["detect_ms"].iloc[n - 1]) + delay_s * 1000
    px = S.price_at(series, d_ms)
    return (float(px), d_ms)


def run(db: Path, *, entry_delay_s: int = 0, exit_mode: str = "peak",
        exit_delay_s: int = 0, horizon_s: int = 1800,
        min_rise: float = S.DEFAULT_MIN_RISE) -> pd.DataFrame:
    """(day, symbol) 별 설계 B 진입 1건씩. 미도래는 버리지 않고 행으로 남긴다."""
    conn = ro(db)
    try:
        shares = universe(conn)
        days = [r[0] for r in conn.execute(
            "SELECT DISTINCT date(snap_ms/1000,'unixepoch') FROM rankings_snap "
            "ORDER BY 1")]
        rows = []
        for day in days:
            rk = load(conn, day)
            if rk.empty:
                continue
            for sym, g in rk.groupby("symbol", sort=False):
                if sym not in shares:          # 유니버스 풀링 = 우리 symbols 테이블
                    continue
                s = S.price_series(g, sym, VOLUME)
                if len(s) < MIN_SNAPS:
                    continue
                e_px, e_ms = S.find_oversold_entry(s, drop=0.05, lookback_s=600)
                if not (e_px == e_px) or e_ms < 0:
                    continue
                fill_px, fill_ms = entry_delayed(s, e_ms, entry_delay_s)
                if not (fill_px == fill_px) or fill_px <= 0:
                    continue
                sh = S.detect_shots(s, symbol=sym, min_rise=min_rise)
                rec = {"day": day, "symbol": sym, "signal_ms": int(e_ms),
                       "signal_u": float(e_px), "entry_ms": int(fill_ms),
                       "entry_u": float(fill_px),
                       # last_u 는 **마이크로달러**, symbol_stratum 은 **USD** 를 받는다.
                       # 이 변환을 빠뜨리면 전 표본이 univ_mcap_out 으로 접힌다(감사 F-5).
                       "stratum": S.symbol_stratum(float(fill_px) / 1e6,
                                                   shares.get(sym))}
                for n in (1, 2):
                    if exit_mode == "peak":
                        r = S.shot_exit_from_entry(s, sh, fill_px, fill_ms, n=n,
                                                   horizon_s=horizon_s)
                        rec[f"ret{n}"] = r["ret"]
                        rec[f"reason{n}"] = r["reason"]
                    else:
                        xp, _xm = observable_shot_exit(
                            s, sh, fill_ms, n=n, horizon_s=horizon_s,
                            delay_s=exit_delay_s)
                        rec[f"ret{n}"] = (float(xp / fill_px - 1.0)
                                          if xp == xp else float("nan"))
                        rec[f"reason{n}"] = ("shot" if xp == xp else "no_shot")
                # 미도래를 **손실로 재계상**하기 위한 지평 종료 시점가
                w = s[(s.index >= fill_ms) & (s.index <= fill_ms + horizon_s * 1000)]
                rec["horizon_u"] = float(w.iloc[-1]) if len(w) else float("nan")
                rec["horizon_ret"] = (float(w.iloc[-1] / fill_px - 1.0)
                                      if len(w) else float("nan"))
                # 진입 신호봉이 슈팅 안에 들어있는가 (순환성 점검)
                if len(sh):
                    inside = sh[(sh["start_ms"] <= fill_ms)
                                & (sh["peak_ms"] >= fill_ms)]
                    rec["entry_inside_shot"] = bool(len(inside))
                    nxt = sh[sh["peak_ms"] > fill_ms]
                    rec["first_shot_start_ms"] = (int(nxt["start_ms"].iloc[0])
                                                  if len(nxt) else -1)
                    rec["first_shot_start_u"] = (float(nxt["start_u"].iloc[0])
                                                 if len(nxt) else float("nan"))
                else:
                    rec["entry_inside_shot"] = False
                    rec["first_shot_start_ms"] = -1
                    rec["first_shot_start_u"] = float("nan")
                rows.append(rec)
        return pd.DataFrame(rows)
    finally:
        conn.close()


def placebo(db: Path, *, seed: int = RNG_SEED, horizon_s: int = 1800
            ) -> pd.DataFrame:
    """**위약 진입** — 과매도 규칙을 버리고 같은 계열에서 무작위 봉에 진입한다.

    진입 규칙이 실제로 기여한다면 위약보다 나아야 한다. 같은 (일, 종목) 집합·같은
    슈팅 정의를 쓰므로 차이는 **진입 시점 선택**에서만 온다.
    """
    conn = ro(db)
    rng = np.random.default_rng(seed)
    try:
        shares = universe(conn)
        days = [r[0] for r in conn.execute(
            "SELECT DISTINCT date(snap_ms/1000,'unixepoch') FROM rankings_snap "
            "ORDER BY 1")]
        rows = []
        for day in days:
            rk = load(conn, day)
            if rk.empty:
                continue
            for sym, g in rk.groupby("symbol", sort=False):
                if sym not in shares:
                    continue
                s = S.price_series(g, sym, VOLUME)
                if len(s) < MIN_SNAPS:
                    continue
                # 실제 진입이 성립한 (일, 종목) 만 대상으로 해 종목 선택 효과를 제거한다
                e_px, e_ms = S.find_oversold_entry(s, drop=0.05, lookback_s=600)
                if not (e_px == e_px) or e_ms < 0:
                    continue
                i = int(rng.integers(0, len(s) - 1))
                f_ms, f_px = int(s.index[i]), float(s.iloc[i])
                sh = S.detect_shots(s, symbol=sym)
                r = S.shot_exit_from_entry(s, sh, f_px, f_ms, n=1,
                                           horizon_s=horizon_s)
                w = s[(s.index >= f_ms) & (s.index <= f_ms + horizon_s * 1000)]
                rows.append({"day": day, "symbol": sym, "entry_ms": f_ms,
                             "entry_u": f_px, "ret1": r["ret"],
                             "reason1": r["reason"],
                             "horizon_ret": (float(w.iloc[-1] / f_px - 1.0)
                                             if len(w) else float("nan"))})
        return pd.DataFrame(rows)
    finally:
        conn.close()


def permutation_placebo(db: Path, *, seed: int = RNG_SEED, horizon_s: int = 1800
                        ) -> pd.DataFrame:
    """**심볼 치환 위약** — 진입 *시각*은 실제 그대로 두고 *종목*만 무작위로 바꾼다.

    무작위 봉 위약은 장중 위치가 달라져 남은 지평까지 같이 흔들린다. 이 위약은
    시각을 고정하므로 남는 차이는 오직 **"그 순간 그 종목을 골랐다"** 뿐이다.
    """
    conn = ro(db)
    rng = np.random.default_rng(seed)
    try:
        shares = universe(conn)
        days = [r[0] for r in conn.execute(
            "SELECT DISTINCT date(snap_ms/1000,'unixepoch') FROM rankings_snap "
            "ORDER BY 1")]
        rows = []
        for day in days:
            rk = load(conn, day)
            if rk.empty:
                continue
            ser, shot = {}, {}
            for sym, g in rk.groupby("symbol", sort=False):
                if sym not in shares:
                    continue
                s = S.price_series(g, sym, VOLUME)
                if len(s) < MIN_SNAPS:
                    continue
                ser[sym] = s
            real = []
            for sym, s in ser.items():
                e_px, e_ms = S.find_oversold_entry(s, drop=0.05, lookback_s=600)
                if e_px == e_px and e_ms >= 0:
                    real.append((sym, int(e_ms)))
            pool = sorted(ser)
            for sym, e_ms in real:
                alts = [x for x in pool if x != sym]
                if not alts:
                    continue
                alt = alts[int(rng.integers(0, len(alts)))]
                s = ser[alt]
                f_px = S.price_at(s, e_ms)
                if not (f_px == f_px) or f_px <= 0:
                    continue
                if alt not in shot:
                    shot[alt] = S.detect_shots(s, symbol=alt)
                r = S.shot_exit_from_entry(s, shot[alt], f_px, e_ms, n=1,
                                           horizon_s=horizon_s)
                w = s[(s.index >= e_ms) & (s.index <= e_ms + horizon_s * 1000)]
                rows.append({"day": day, "symbol": alt, "src_symbol": sym,
                             "entry_ms": e_ms, "entry_u": f_px, "ret1": r["ret"],
                             "reason1": r["reason"],
                             "horizon_ret": (float(w.iloc[-1] / f_px - 1.0)
                                             if len(w) else float("nan"))})
        return pd.DataFrame(rows)
    finally:
        conn.close()


def describe(df: pd.DataFrame, col: str, label: str, *, cluster: str | None = None):
    v = pd.to_numeric(df[col], errors="coerce")
    arr = v.dropna()
    n_entry, n_arr = len(df), len(arr)
    if n_arr == 0:
        print(f"  {label:38} entries {n_entry:>3}  arrivals 0")
        return
    lo, hi = boot_ci(arr.to_numpy())
    line = (f"  {label:38} entries {n_entry:>3}  arrivals {n_arr:>3} "
            f"({n_arr / n_entry:.3f})  med {arr.median() * 100:>+6.2f}%  "
            f"mean {arr.mean() * 100:>+6.2f}%  CI [{lo * 100:>+6.2f}%,{hi * 100:>+6.2f}%]")
    if cluster:
        g = df.loc[arr.index, cluster].to_numpy()
        clo, chi = cluster_boot_ci(arr.to_numpy(), g)
        line += f"  clusterCI [{clo * 100:>+6.2f}%,{chi * 100:>+6.2f}%]"
    print(line)


def main(db: Path = DEFAULT_DB) -> int:
    print("=" * 100)
    print("A. REPRODUCE docs/23 section 10.2 (entry delay 0, exit at shot PEAK)")
    print("=" * 100)
    base = run(db)
    print(f"  total entries: {len(base)}   days: {sorted(base['day'].unique())}")
    print(f"  distinct symbols: {base['symbol'].nunique()}")
    describe(base, "ret1", "first shot (as published)")
    describe(base, "ret2", "second shot (as published)")
    print("\n  by stratum (first shot):")
    for st, g in base.groupby("stratum"):
        describe(g, "ret1", f"  {st}")

    print("\n" + "=" * 100)
    print("B. ATTACK 1 - non-arrivals booked as outcomes, not dropped")
    print("=" * 100)
    b = base.copy()
    b["ret1_filled"] = b["ret1"].fillna(b["horizon_ret"])
    v = pd.to_numeric(b["ret1_filled"], errors="coerce").dropna()
    lo, hi = boot_ci(v.to_numpy())
    print(f"  all entries, non-arrival marked at horizon close: n={len(v)}  "
          f"med {v.median()*100:+.2f}%  mean {v.mean()*100:+.2f}%  "
          f"CI [{lo*100:+.2f}%, {hi*100:+.2f}%]")
    miss = base[base["ret1"].isna()]
    if len(miss):
        mv = pd.to_numeric(miss["horizon_ret"], errors="coerce").dropna()
        print(f"  the {len(miss)} non-arrivals alone: mean {mv.mean()*100:+.2f}%  "
              f"med {mv.median()*100:+.2f}%")

    print("\n" + "=" * 100)
    print("C. ATTACK 2 - entry latency (design A applied 13s; design B applied 0s)")
    print("=" * 100)
    for d in (0, 13, 26, 39):
        describe(run(db, entry_delay_s=d), "ret1", f"entry delay {d:>2}s (peak exit)")

    print("\n" + "=" * 100)
    print("D. ATTACK 3 - observable exit (sell at shot DETECT, not at the peak)")
    print("=" * 100)
    for xd in (0, 13):
        describe(run(db, exit_mode="detect", exit_delay_s=xd), "ret1",
                 f"exit at detect +{xd:>2}s, entry delay 0s")
    describe(run(db, entry_delay_s=13, exit_mode="detect", exit_delay_s=13), "ret1",
             "entry +13s AND exit detect +13s")

    print("\n" + "=" * 100)
    print("E. ATTACK 4 - clustering (same symbol / same day are not independent)")
    print("=" * 100)
    print(f"  entries {len(base)} from {base['symbol'].nunique()} symbols "
          f"x {base['day'].nunique()} days")
    print(f"  entries per day: {base.groupby('day').size().to_dict()}")
    describe(base, "ret1", "first shot, cluster=symbol", cluster="symbol")
    describe(base, "ret1", "first shot, cluster=day", cluster="day")

    print("\n" + "=" * 100)
    print("F. ATTACK - circularity: is entry inside the shot it is measured against?")
    print("=" * 100)
    arr = base[base["ret1"].notna()]
    print(f"  entry bar falls INSIDE a detected shot: "
          f"{int(arr['entry_inside_shot'].sum())}/{len(arr)}")
    near = (pd.to_numeric(arr["entry_u"], errors="coerce")
            <= pd.to_numeric(arr["first_shot_start_u"], errors="coerce") * 1.001)
    print(f"  entry price <= the shot's own low (x1.001): {int(near.sum())}/{len(arr)}")
    print("\n  threshold sensitivity - if the result tracks min_rise it is definitional:")
    for mr in (0.01, 0.02, 0.03, 0.05):
        describe(run(db, min_rise=mr), "ret1", f"min_rise {mr:.2f}")

    print("\n" + "=" * 100)
    print("H. PLACEBO ENTRY - random bar instead of the oversold rule")
    print("=" * 100)
    for sd in (RNG_SEED, RNG_SEED + 1, RNG_SEED + 2):
        p = placebo(db, seed=sd)
        describe(p, "ret1", f"placebo (random entry) seed {sd}")
    print("  -- symbol-permutation placebo (entry TIME held fixed, symbol shuffled) --")
    for sd in (RNG_SEED, RNG_SEED + 1, RNG_SEED + 2):
        describe(permutation_placebo(db, seed=sd), "ret1",
                 f"permuted symbol, seed {sd}")
    print("  real oversold entry, for comparison:")
    describe(base, "ret1", "  real rule")

    print("\n" + "=" * 100)
    print("G. ALL HONEST CORRECTIONS AT ONCE (entry +13s, observable exit,")
    print("   non-arrivals booked at horizon close, per-day split)")
    print("=" * 100)
    comb = run(db, entry_delay_s=13, exit_mode="detect", exit_delay_s=13)
    comb["ret1_filled"] = comb["ret1"].fillna(comb["horizon_ret"])
    v = pd.to_numeric(comb["ret1_filled"], errors="coerce").dropna()
    lo, hi = boot_ci(v.to_numpy())
    print(f"  ALL entries booked: n={len(v)}  med {v.median()*100:+.2f}%  "
          f"mean {v.mean()*100:+.2f}%  CI [{lo*100:+.2f}%, {hi*100:+.2f}%]")
    print(f"  vs cheapest $100 round-trip cost 2.38%  ->  "
          f"net {v.mean()*100 - 2.38:+.2f}%")
    for day, g in comb.groupby("day"):
        gv = pd.to_numeric(g["ret1_filled"], errors="coerce").dropna()
        glo, ghi = boot_ci(gv.to_numpy())
        print(f"    {day}: n={len(gv):>3}  mean {gv.mean()*100:>+6.2f}%  "
              f"CI [{glo*100:>+6.2f}%, {ghi*100:>+6.2f}%]")
    print("\n  same split on the PUBLISHED basis (peak exit, arrivals only):")
    for day, g in base.groupby("day"):
        gv = pd.to_numeric(g["ret1"], errors="coerce").dropna()
        glo, ghi = boot_ci(gv.to_numpy())
        print(f"    {day}: n={len(gv):>3}  mean {gv.mean()*100:>+6.2f}%  "
              f"CI [{glo*100:>+6.2f}%, {ghi*100:>+6.2f}%]")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB))

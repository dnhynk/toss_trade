"""감사5 — 설계 A 사망 판정(탐지 후 남은 상승폭 0.0000)이 **순환적인지** 검토한다.

코디네이터가 독립 재현했지만 재현은 타당성이 아니다. 두 가지를 따로 본다.

  (1) `rise_after_detect` 는 시장 사실인가, `detect_shots` 의 **고점 연장 규칙**이
      만들어낸 값인가. 연장은 `px[k+1] >= px[k]` 인 동안만 계속된다 — 즉 탐지 직후
      한 봉이라도 내리면 고점=탐지봉이 되어 남은 상승폭은 **정의상 정확히 0** 이다.
  (2) 사망 판정의 실질 근거인 `captured` 는 `sell_on_downtick`(첫 하락봉 매도) 하나로만
      쟀다. 그것이 유일한 관측 가능 이탈은 아니다. 다른 관측 가능 이탈에서도 음수인가.

라이브 0콜. DB 는 `mode=ro`.

실행: python -m tools.audit5_designa
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from tossmon.analysis import shots as S
from tools.audit5_designb import (DEFAULT_DB, MIN_SNAPS, VOLUME, boot_ci, load,
                                  ro, universe)


def exit_fixed_hold(series: pd.Series, from_ms: int, bars: int) -> float:
    """`bars` 개 관측 뒤 무조건 매도 — 완전히 관측 가능하다."""
    w = series[series.index >= from_ms]
    if len(w) <= bars:
        return float("nan")
    return float(w.iloc[bars])


def exit_trailing(series: pd.Series, from_ms: int, stop: float,
                  horizon_s: int = 600) -> float:
    """고점 대비 `stop` 만큼 밀리면 매도 — 매 시점 과거만 본다."""
    w = series[(series.index >= from_ms)
               & (series.index <= from_ms + horizon_s * 1000)]
    if w.empty:
        return float("nan")
    hi = float(w.iloc[0])
    for px in w.to_numpy()[1:]:
        px = float(px)
        hi = max(hi, px)
        if px <= hi * (1.0 - stop):
            return px
    return float(w.iloc[-1])


def run(db: Path, *, delay_s: int = 13, target_only: bool = True) -> pd.DataFrame:
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
                if sym not in shares:
                    continue
                s = S.price_series(g, sym, VOLUME)
                if len(s) < MIN_SNAPS:
                    continue
                sh = S.detect_shots(s, symbol=sym)
                if not len(sh):
                    continue
                for _, shot in sh.iterrows():
                    st = S.symbol_stratum(float(shot["detect_u"]) / 1e6,
                                          shares.get(sym))
                    if target_only and st != "target":
                        continue
                    d_ms = int(shot["detect_ms"]) + delay_s * 1000
                    entry = S.price_at(s, d_ms)
                    if not (entry == entry and entry > 0):
                        continue
                    dt, _ = S.sell_on_downtick(s, d_ms)
                    rec = {"day": day, "symbol": sym, "stratum": st,
                           "entry_u": entry,
                           "rise": float(shot["rise"]),
                           "rise_after_detect": float(shot["rise_after_detect"]),
                           "downtick": (float(dt / entry - 1.0)
                                        if dt == dt else float("nan"))}
                    for b in (1, 2, 4, 8, 16):
                        px = exit_fixed_hold(s, d_ms, b)
                        rec[f"hold{b}"] = (float(px / entry - 1.0)
                                           if px == px else float("nan"))
                    for stp, lab in ((0.01, "trail1"), (0.02, "trail2"),
                                     (0.03, "trail3")):
                        px = exit_trailing(s, d_ms, stp)
                        rec[lab] = (float(px / entry - 1.0)
                                    if px == px else float("nan"))
                    rows.append(rec)
        return pd.DataFrame(rows)
    finally:
        conn.close()


def line(df: pd.DataFrame, col: str, label: str) -> None:
    v = pd.to_numeric(df[col], errors="coerce").dropna()
    if v.empty:
        print(f"  {label:34} n=0")
        return
    lo, hi = boot_ci(v.to_numpy())
    print(f"  {label:34} n={len(v):>4}  med {v.median()*100:>+6.2f}%  "
          f"mean {v.mean()*100:>+6.2f}%  CI [{lo*100:>+6.2f}%,{hi*100:>+6.2f}%]  "
          f"pos {float((v > 0).mean()):.3f}")


def main(db: Path = DEFAULT_DB) -> int:
    print("=" * 100)
    print("A. IS rise_after_detect==0 A MARKET FACT OR A DEFINITION?")
    print("=" * 100)
    df = run(db, target_only=True)
    print(f"  target-stratum shots: {len(df)}")
    ra = pd.to_numeric(df["rise_after_detect"], errors="coerce").dropna()
    print(f"  rise_after_detect median {ra.median():.4f}  "
          f"share exactly 0: {float((ra == 0).mean()):.3f}")
    print("\n  detect_shots extends the peak only while px[k+1] >= px[k].")
    print("  So one down-tick right after detection forces peak==detect and")
    print("  rise_after_detect==0 BY CONSTRUCTION. The share above is that share.")
    print("  It measures the stopping rule, not how far the move went.")

    print("\n" + "=" * 100)
    print("B. DESIGN A UNDER OTHER *OBSERVABLE* EXITS (entry = detect +13s)")
    print("=" * 100)
    print("  the published verdict used ONE exit rule (first down-tick):")
    line(df, "downtick", "downtick (as published)")
    print("  other exits, equally observable:")
    for b in (1, 2, 4, 8, 16):
        line(df, f"hold{b}", f"fixed hold {b:>2} bars (~{b*13}s)")
    for lab, s in (("trail1", "1%"), ("trail2", "2%"), ("trail3", "3%")):
        line(df, lab, f"trailing stop {s}")

    print("\n" + "=" * 100)
    print("C. SAME, POOLED UNIVERSE (larger sample)")
    print("=" * 100)
    pool = run(db, target_only=False)
    print(f"  pooled shots: {len(pool)}")
    line(pool, "downtick", "downtick (as published)")
    for b in (1, 2, 4, 8, 16):
        line(pool, f"hold{b}", f"fixed hold {b:>2} bars (~{b*13}s)")
    for lab, s in (("trail1", "1%"), ("trail2", "2%"), ("trail3", "3%")):
        line(pool, lab, f"trailing stop {s}")
    print("\n  NOTE: every number above is GROSS. docs/22 puts the cheapest $100")
    print("  round trip at 2.38%, so a gross figure below +2.38% is still a loss.")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB))

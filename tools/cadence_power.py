"""How many grid slots does a `ranking_cadence` arm actually buy? (docs/62)

`docs/35` measured "29.4% of server grid ticks never reach us" from **10 misses in
34 slots**. That is a proportion on n=34, and nobody wrote down its confidence
interval - so the number has been quoted as if it were exact.

This tool answers three questions with arithmetic, no API calls:

  1. **slots, not calls** - an arm of `dur_s` seconds observes `dur_s / grid_s`
     grid slots regardless of how fast it polls. Halving the gap doubles the
     calls and buys **zero** extra slots.
  2. **how wide is the interval** - Wilson score CI (not Wald: at n=30 Wald is
     badly wrong near the edges, and this is exactly the n we have).
  3. **can two arms be told apart** - the decomposition this measurement exists
     for is "server/cache loss" vs "aliasing loss", and that is a *difference*
     of two proportions. Two intervals of +-16pp cannot resolve a 10pp gap.

Why this matters for the arm design: an arm whose gap is **below** the grid
period samples every slot at least once, so it cannot alias - it measures the
server/cache floor. An arm whose gap is **above** the grid period skips slots by
construction, so it measures floor + aliasing. The difference is the answer.

Live API calls: **0**. Reproduce:
    python -m tools.cadence_power
    python -m tools.cadence_power --arms 1s:1:300 5s:5:300 12s:12:300
"""
from __future__ import annotations

import argparse
import math

#: Server recomputes the volume ranking on a 10s wall-clock grid (docs/35 §5-1:
#: `rankedAt` seconds digit takes six values, milliseconds all `.000`).
GRID_S = 10.0
#: The one miss-rate observation we have, and what it was measured on.
OBSERVED = {"misses": 10, "slots": 34, "arm": "1s", "session": "premarket",
            "source": "docs/35 §5-1"}
#: `open` profile as it stands today (tools/live_probe.py CADENCE_PROFILES).
DEFAULT_ARMS = ("1s:1:300", "12s:12:300")


def wilson(k: int, n: int, z: float = 1.959963985) -> tuple[float, float, float]:
    """Wilson score interval. Returns (p_hat, lo, hi).

    Wald (`p +- z*sqrt(p(1-p)/n)`) is the one people reach for and it is wrong
    here: at n=30 it can put the bound below 0, and its coverage collapses near
    the edges. Wilson stays inside [0,1] and keeps nominal coverage at these n.
    """
    if n <= 0:
        return (float("nan"),) * 3
    p = k / n
    d = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, max(0.0, centre - half), min(1.0, centre + half)


def slots_for(dur_s: float, grid_s: float = GRID_S) -> int:
    """Grid slots an arm covers. **Independent of the poll gap.**"""
    return int(dur_s // grid_s)


def half_width(p: float, n: int, z: float = 1.959963985) -> float:
    """Wilson half-width at the assumed rate - for planning before data exists."""
    _, lo, hi = wilson(int(round(p * n)), n, z)
    return (hi - lo) / 2.0


def slots_needed(p: float, target_half_pp: float, z: float = 1.959963985) -> int:
    """Slots required for a half-width of `target_half_pp` percentage points."""
    target = target_half_pp / 100.0
    n = 1
    while n < 200_000:
        if half_width(p, n, z) <= target:
            return n
        n = n + 1 if n < 200 else int(n * 1.05) + 1
    return -1


def can_separate(p1: float, n1: int, p2: float, n2: int,
                 z: float = 1.959963985) -> tuple[float, float, bool]:
    """Two-proportion difference: (diff, 95% half-width, separated-from-zero?).

    This is the actual question - not "is each arm precise" but "is arm A's miss
    rate distinguishable from arm B's". Uses the independent-samples normal
    approximation on the difference, which is the standard planning calculation.
    """
    diff = p2 - p1
    se = math.sqrt(p1 * (1 - p1) / max(n1, 1) + p2 * (1 - p2) / max(n2, 1))
    return diff, z * se, abs(diff) > z * se


def parse_arm(spec: str) -> tuple[str, float, float]:
    label, gap, dur = spec.split(":")
    return label, float(gap), float(dur)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="grid-slot power for ranking_cadence arms")
    ap.add_argument("--arms", nargs="*", default=list(DEFAULT_ARMS),
                    help="label:gap_s:duration_s (e.g. 5s:5:900)")
    ap.add_argument("--grid-s", type=float, default=GRID_S)
    ap.add_argument("--assume-p", type=float, default=None,
                    help="miss rate to plan against (default: the observed point estimate)")
    args = ap.parse_args(argv)

    p_obs, lo, hi = wilson(OBSERVED["misses"], OBSERVED["slots"])
    p = args.assume_p if args.assume_p is not None else p_obs

    print("# the one observation we have")
    print(f"  {OBSERVED['source']}: {OBSERVED['misses']}/{OBSERVED['slots']} slots missed "
          f"on the {OBSERVED['arm']} arm, {OBSERVED['session']}")
    print(f"  point {p_obs * 100:.1f}%   Wilson 95% CI [{lo * 100:.1f}%, {hi * 100:.1f}%]"
          f"   half-width +-{(hi - lo) / 2 * 100:.1f}pp")
    print(f"  planning against p = {p * 100:.1f}%, grid = {args.grid_s:g}s\n")

    print(f"{'arm':<8} {'gap':>6} {'dur_s':>7} {'calls':>7} {'slots':>6} "
          f"{'95% CI at p':>22} {'+-pp':>6}  aliasing?")
    rows = []
    for spec in args.arms:
        label, gap, dur = parse_arm(spec)
        n = slots_for(dur, args.grid_s)
        calls = int(dur // gap)
        _, alo, ahi = wilson(int(round(p * n)), n)
        # gap < grid  -> every slot gets at least one sample -> cannot alias.
        aliases = "yes (gap > grid)" if gap > args.grid_s else "no (gap <= grid)"
        print(f"{label:<8} {gap:>6g} {dur:>7g} {calls:>7} {n:>6} "
              f"{f'[{alo * 100:.1f}%, {ahi * 100:.1f}%]':>22} "
              f"{(ahi - alo) / 2 * 100:>6.1f}  {aliases}")
        rows.append((label, gap, dur, calls, n, p))

    print("\n# what would it take to narrow the interval (duration, not gap)")
    print(f"{'target +-pp':>12} {'slots':>7} {'duration':>10} "
          f"{'calls @1s':>10} {'calls @5s':>10} {'calls @12s':>11}")
    for tgt in (16, 12, 10, 8, 6, 5):
        n = slots_needed(p, tgt)
        dur = n * args.grid_s
        print(f"{tgt:>12} {n:>7} {f'{dur:g}s':>10} "
              f"{int(dur // 1):>10} {int(dur // 5):>10} {int(dur // 12):>11}")

    print("\n# can two arms be told apart? (the decomposition this measurement is for)")
    print("  rows are: assumed true gap between the two arms' miss rates")
    if len(rows) >= 2:
        a, b = rows[0], rows[-1]
        print(f"  comparing {a[0]} (n={a[4]} slots) vs {b[0]} (n={b[4]} slots)")
        print(f"{'true gap pp':>12} {'95% half-width pp':>19}  separated from 0?")
        for gap_pp in (5, 10, 15, 20, 25, 30):
            d, hw, ok = can_separate(p, a[4], min(1.0, p + gap_pp / 100.0), b[4])
            print(f"{gap_pp:>12} {hw * 100:>19.1f}  {'YES' if ok else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

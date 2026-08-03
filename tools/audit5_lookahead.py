"""감사5 — 룩어헤드 회귀 테스트 14건이 정말 잡는지 **위반을 심어서** 확인한다.

두 부분:
  PART 1  접두사 불변성(prefix invariance) 시연 — 설계 B 이탈이 미래를 읽는지.
  PART 2  변이 시험(mutation testing) — shots.py 에 룩어헤드를 심고 테스트가 죽는지.

PART 2 는 `tossmon/analysis/shots.py` 를 **일시적으로** 고치고 `finally` 에서 항상
원본으로 되돌린다. 실패해도 원본이 남도록 바이트 단위로 복원한다.

라이브 0콜. DB 를 열지 않는다.

실행: python -m tools.audit5_lookahead
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pandas as pd

from tossmon.analysis import shots as S

SHOTS = Path(__file__).resolve().parents[1] / "tossmon" / "analysis" / "shots.py"
TESTS = Path(__file__).resolve().parents[1] / "tests" / "test_shots.py"
S12 = 13_000


def series(px) -> pd.Series:
    """12초 간격 계열. 가격은 마이크로달러."""
    return pd.Series([p * 1e6 for p in px],
                     index=[i * S12 for i in range(len(px))], dtype="float64")


def part1() -> None:
    print("=" * 96)
    print("PART 1 - prefix invariance: does design B's exit read the future?")
    print("=" * 96)
    print("A rule is observable only if its answer never changes when later bars arrive.")
    print("test_oversold_entry_uses_only_past_quotes applies exactly this check to the")
    print("ENTRY. No test applies it to the EXIT. Applying it here:\n")

    px = [1.00, 1.00, 0.90, 0.88, 0.92, 0.94, 1.00, 1.05, 0.95, 0.93]
    full = series(px)
    e_px, e_ms = S.find_oversold_entry(full, drop=0.05)
    print(f"  entry: {e_px/1e6:.2f} at t={e_ms//S12} (oversold rebound)")
    print(f"  full-series price path: {px}\n")
    print("  bars_seen  design_B_exit_ret  reason        <- answer must not move")
    seen = []
    for k in range(3, len(px) + 1):
        s = series(px[:k])
        sh = S.detect_shots(s, min_rise=0.02)
        r = S.shot_exit_from_entry(s, sh, e_px, e_ms, n=1)
        ret = r["ret"]
        seen.append(ret)
        shown = "   nan  " if ret != ret else f"{ret*100:+7.2f}%"
        print(f"    {k:>4}      {shown}         {r['reason']}")
    live = [v for v in seen if v == v]
    if len(set(round(v, 10) for v in live)) > 1:
        print("\n  VIOLATION: the reported exit return CHANGES as future bars arrive.")
        print("  At the moment the shot is first knowable the answer is not yet the one")
        print("  the measurement reports -> the published number is not achievable.")
    else:
        print("\n  no violation detected")

    print("\n  For contrast, the ENTRY under the same check:")
    a = S.find_oversold_entry(series(px), drop=0.05)
    b = S.find_oversold_entry(series(px[:5]), drop=0.05)
    print(f"    full={a}  prefix={b}  ->  {'STABLE' if a == b else 'VIOLATION'}")


#: (이름, 설명, 원본 조각, 변이 조각) — 각 변이는 **진짜 룩어헤드**다.
MUTATIONS = [
    ("M1 entry peeks at the future",
     "find_oversold_entry uses the whole series max instead of the past window",
     "        window = px[(ts <= ts[i]) & (ts >= lo)]",
     "        window = px[(ts >= lo)]"),
    ("M2 shot claims it was knowable at its low",
     "detect_ms is moved back to start_ms - i.e. buy the bottom",
     '                "detect_ms": int(ts[hit]), "detect_u": float(px[hit]),',
     '                "detect_ms": int(lo_ts), "detect_u": float(lo_px),'),
    ("M3 design A enters at the shot low",
     "capturable_shot_return uses the shot low as the fill price",
     "    entry = price_at(series, d_ms)",
     "    entry = float(shot[\"start_u\"])"),
    ("M4 design B exits at the horizon's global max",
     "shot_exit_from_entry sells at the best price in the whole horizon",
     '    return {"ret": float(float(row["peak_u"]) / entry_u - 1.0), "reason": f"shot#{n}",',
     '    _w = series[(series.index >= entry_ms)\n                & (series.index <= entry_ms + horizon_s * 1000)]\n'
     '    return {"ret": float(float(_w.max()) / entry_u - 1.0), "reason": f"shot#{n}",'),
    ("M5 design B exits at the single best price in the WHOLE day",
     "the most blatant look-ahead possible - sell at the global maximum",
     '    if after is None or len(after) < n:\n        return {"ret": _NAN, "reason": "no_shot", "wait_s": _NAN}',
     '    if after is None or len(after) < n:\n        return {"ret": _NAN, "reason": "no_shot", "wait_s": _NAN}\n'
     '    return {"ret": float(float(series.max()) / entry_u - 1.0),\n'
     '            "reason": f"shot#{n}", "wait_s": 0.0}'),
]


def part2() -> int:
    print("\n" + "=" * 96)
    print("PART 2 - mutation testing: plant look-ahead, see if the suite dies")
    print("=" * 96)
    original = SHOTS.read_bytes()
    text = original.decode("utf-8")
    # shots.py 는 CRLF 다. 앵커의 "\n" 을 파일 개행으로 맞추지 않으면 여러 줄 앵커가
    # 조용히 빗나가고 그것이 "변이 없음"으로 오독된다(1차 실행에서 실제로 그랬다).
    nl = "\r\n" if "\r\n" in text else "\n"
    caught = 0
    try:
        for name, desc, old, new in MUTATIONS:
            old, new = old.replace("\n", nl), new.replace("\n", nl)
            print(f"\n  {name}")
            print(f"    {desc}")
            if old not in text:
                print("    SKIPPED - anchor not found (source drifted)")
                continue
            # write_bytes: write_text 는 윈도우에서 개행을 CRLF 로 바꿔 파일을 흔든다.
            SHOTS.write_bytes(text.replace(old, new, 1).encode("utf-8"))
            p = subprocess.run([sys.executable, "-m", "pytest", "-q", str(TESTS)],
                               capture_output=True, text=True)
            tail = [ln for ln in p.stdout.splitlines() if ln.strip()][-1:]
            ok = p.returncode != 0
            caught += int(ok)
            print(f"    suite: {'FAILED -> mutation CAUGHT' if ok else 'PASSED -> mutation SURVIVED'}"
                  f"   ({tail[0].strip() if tail else '?'})")
            if ok:
                names = sorted({ln.split("::")[1].split()[0]
                                for ln in p.stdout.splitlines()
                                if ln.startswith("FAILED") and "::" in ln})
                if names:
                    print(f"    killed by: {', '.join(names)}")
                else:
                    print("    NOTE: non-assertion error (crash), not a real catch:")
                    for ln in p.stdout.splitlines():
                        if "Error" in ln and "tossmon" in ln:
                            print(f"      {ln.strip()[:110]}")
                            break
    finally:
        SHOTS.write_bytes(original)
        assert SHOTS.read_bytes() == original, "restore failed"
        print("\n  [restored tossmon/analysis/shots.py to its original bytes]")
    print(f"\n  caught {caught} / {len(MUTATIONS)} planted look-ahead mutations")
    return 0


def part1b() -> None:
    """왜 M1(진입 룩어헤드)이 살아남는가 — 픽스처가 미래에 둔감하게 짜여 있다."""
    print("\n" + "=" * 96)
    print("PART 1b - why the entry look-ahead test cannot fail")
    print("=" * 96)
    fixture = [1.00, 1.00, 0.90, 0.88, 0.92, 1.50]
    print(f"  test_oversold_entry_uses_only_past_quotes fixture: {fixture}")
    print("  it compares find_oversold_entry(full) vs find_oversold_entry(px[:5]).")
    print("  The only 'future' bar is 1.50 - a RISE. But the rule triggers on a DROP")
    print("  from the lookback high, and 1.50 arrives AFTER the entry bar at index 4.")
    print("  A future peek therefore cannot move the answer in this fixture.\n")
    probe = [1.00, 1.00, 0.90, 0.88, 0.92, 2.00]
    full = S.find_oversold_entry(series(probe), drop=0.05)
    part = S.find_oversold_entry(series(probe[:5]), drop=0.05)
    print(f"  a fixture that WOULD expose it needs the future bar to be a higher HIGH")
    print(f"  arriving before the candidate entry. Current rule, probe {probe}:")
    print(f"    full={full[0]/1e6 if full[0]==full[0] else float('nan'):.2f} "
          f"prefix={part[0]/1e6 if part[0]==part[0] else float('nan'):.2f}")
    print("  -> the test asserts a property the fixture cannot violate.")


def main() -> int:
    part1()
    part1b()
    return part2()


if __name__ == "__main__":
    sys.exit(main())

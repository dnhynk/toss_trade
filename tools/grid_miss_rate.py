"""서버 10 초 격자의 **미수신 슬롯 비율**을 `docs/35` §5-1 의 정의로 다시 잰다.

라이브 호출 **0**. 프로브가 남긴 JSON 만 읽는다.

    .venv/Scripts/python.exe -m tools.grid_miss_rate \
        coordination/daily/2026-08-18_probe_d_output.json

정의 (`docs/35` §5-1 을 그대로 옮긴 것 — 새로 만들지 않았다)
------------------------------------------------------------
프리마켓 원문은 이렇게 셌다: 델타 `15×(+10s)` · `10×(+20s)` · `1×(−10s)` 에서

    관측 슬롯 = 델타가 덮은 격자 구간 수 = Σ(델타/10초)          <- 부호 그대로 = 34
    미수신    = `+20s` 델타 하나가 슬롯 하나를 건너뛴 것          <- 10
    미수신율  = 10 / 34 = 29.4%

일반화하면 (원문의 두 숫자를 그대로 재현한다):

    slots  = Σ_모든델타 (델타 / 10)                     부호 포함
    missed = Σ_양의델타 (델타 / 10 − 1)                 +10 은 0, +20 은 1, +30 은 2

**역행 델타(−10s)는 분모에서 −1 로 세고 분자에서는 안 센다** — 원문이 그렇게 했다.

★ 계기의 결함 하나 (이 도구가 존재하는 이유의 절반)
---------------------------------------------------
`live_probe._analyze_arm` 의 `server_stamp_delta_hist` 는 **첫 전이의 델타를 빠뜨린다**
(`for k in range(1, len(idx))` 라 `idx[0]` 이 짝을 못 얻는다). 그래서 저장된 히스토그램의
델타 수는 늘 `changes − 1` 이다. 2026-08-18 1 초 팔에서 실제로 `changes=12` 인데
히스토그램은 `n=11` 이고, **빠진 것은 `+20.0s`** 였다 — 즉 미수신을 **과소**보고했다.

`raw_polls` 가 있는 팔은 이 도구가 **원본 폴 열에서 다시 세므로** 그 결함을 안 탄다.
`raw_polls` 가 없는 팔(1 초 팔 외)은 히스토그램밖에 없으므로 **빠진 델타 하나를
모든 경우로 넣어 본 구간**을 함께 낸다.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.cadence_power import wilson                      # noqa: E402
from tossmon.api.models import iso_to_ms                     # noqa: E402

GRID_S = 10.0

#: `docs/35` §5-1 이 적은 프리마켓(2026-08-07) 1 초 팔의 델타 히스토그램 **원문 그대로**.
#: 전이 27 회인데 히스토그램은 26 개다 — 위에 적은 첫 델타 누락과 **같은 결함**이다.
PREMARKET_HIST_DOCS35 = {10.0: 15, 20.0: 10, -10.0: 1}
PREMARKET_CHANGES_DOCS35 = 27


def geometric_expectation(deltas: list[float]) -> dict:
    """슬롯이 **독립적으로** q 확률로 빠진다면 델타는 기하분포여야 한다.

    그 경우 `P(+10(k+1)) = q^k (1−q)` 이고, 특히 `+30` 이 상당수 나와야 한다.
    빠지는 자리가 **고정**이면(예: 여섯 자리 중 둘이 늘 비면) 델타는 `+10`/`+20`
    **반반**이고 `+30` 은 **한 번도** 안 나온다. 둘을 가르는 것이 이 함수다.
    """
    pos = [d for d in deltas if d > 0]
    n = len(pos)
    q = miss_rate(deltas)["rate"]
    exp = {10.0: n * (1 - q), 20.0: n * q * (1 - q), 30.0: n * q * q * (1 - q)}
    obs = collections.Counter(pos)
    return {"n_positive": n, "q": q,
            "observed": {k: obs.get(k, 0) for k in (10.0, 20.0, 30.0)},
            "expected_if_independent": {k: round(v, 1) for k, v in exp.items()},
            "expected_if_fixed_positions": {10.0: n / 2, 20.0: n / 2, 30.0: 0}}


def deltas_from_polls(polls: list[dict]) -> list[float]:
    """폴 열에서 **모든** 전이의 서버 스탬프 델타. 첫 전이도 센다."""
    vals = [p.get("rankedAt") for p in polls if not p.get("error")]
    idx = [i for i in range(1, len(vals)) if vals[i] != vals[i - 1]]
    out = []
    for k in idx:
        a, b = vals[k - 1], vals[k]
        if a and b:
            out.append(round((iso_to_ms(b) - iso_to_ms(a)) / 1000.0, 2))
    return out


def miss_rate(deltas: list[float]) -> dict:
    """`docs/35` §5-1 의 분모/분자. 정의를 바꾸지 않는다."""
    slots = sum(d / GRID_S for d in deltas)
    missed = sum(d / GRID_S - 1 for d in deltas if d > 0)
    n_slots, n_missed = int(round(slots)), int(round(missed))
    p, lo, hi = wilson(n_missed, n_slots) if n_slots > 0 else (float("nan"),) * 3
    return {"deltas": len(deltas), "slots": n_slots, "missed": n_missed,
            "rate": p, "lo": lo, "hi": hi,
            "hist": dict(sorted(collections.Counter(deltas).items()))}


def bracket_missing_one(hist: dict[float, int]) -> tuple[dict, dict]:
    """히스토그램만 있는 팔 — 빠진 델타 하나를 모든 종류로 넣어 본 최소/최대."""
    base = [float(k) for k, n in hist.items() for _ in range(n)]
    cands = []
    for extra in sorted({float(k) for k in hist}):
        cands.append(miss_rate(base + [extra]))
    return min(cands, key=lambda r: r["rate"]), max(cands, key=lambda r: r["rate"])


def seconds_occupancy(polls: list[dict]) -> dict:
    """`rankedAt` 초 자리가 몇 개인가 — `docs/62` §6-3 이 미리 적어 둔 분기점."""
    vals = [p.get("rankedAt") for p in polls if not p.get("error")]
    runs = []
    for v in vals:
        if v and (not runs or runs[-1] != v):
            runs.append(v)
    return {"distinct_runs": len(runs),
            "seconds": dict(sorted(collections.Counter(v[17:19] for v in runs).items())),
            "millis": dict(sorted(collections.Counter(v[19:23] for v in runs).items()))}


def _pct(x: float) -> str:
    return "%.1f%%" % (x * 100)


def report(path: Path) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    cad = doc["ranking_cadence"]
    arms = cad["volume_realtime"]["arms"]
    out: dict = {"source": str(path), "profile": cad["profile"], "arms": {}, "decimated": {}}

    print("source  : %s" % path)
    print("profile : %s   measured_at_kst: %s" % (cad["profile"], cad["measured_at_kst"]))
    print("definition: docs/35 5-1  (slots = sum(delta/10), missed = sum(delta/10 - 1) over positive)")
    print("")
    print("=== per arm ===")
    for label, arm in arms.items():
        polls = arm.get("raw_polls")
        if polls:
            r = miss_rate(deltas_from_polls(polls))
            r["source"] = "raw_polls (recounted, first transition included)"
            print("%-4s  RECOUNTED from raw_polls" % label)
            print("      deltas=%-3d slots=%-4d missed=%-3d  rate=%-6s Wilson95 [%s, %s]"
                  % (r["deltas"], r["slots"], r["missed"], _pct(r["rate"]),
                     _pct(r["lo"]), _pct(r["hi"])))
            print("      hist=%s" % r["hist"])
            occ = seconds_occupancy(polls)
            r["seconds_occupancy"] = occ
            print("      rankedAt seconds positions: %s   millis: %s"
                  % (occ["seconds"], occ["millis"]))
        else:
            hist = {float(k): v for k, v in
                    (arm["rankedAt"].get("server_stamp_delta_hist") or {}).items()}
            r = miss_rate([k for k, n in hist.items() for _ in range(n)])
            lo_r, hi_r = bracket_missing_one(hist)
            r["source"] = "stored hist (short by the first transition; bracket shown)"
            r["bracket"] = [lo_r["rate"], hi_r["rate"]]
            print("%-4s  stored hist only (raw_polls kept for the 1s arm only)" % label)
            print("      deltas=%-3d slots=%-4d missed=%-3d  rate=%-6s Wilson95 [%s, %s]"
                  % (r["deltas"], r["slots"], r["missed"], _pct(r["rate"]),
                     _pct(r["lo"]), _pct(r["hi"])))
            print("      hist=%s" % r["hist"])
            print("      + the dropped first delta puts the point estimate in [%s, %s]"
                  % (_pct(lo_r["rate"]), _pct(hi_r["rate"])))
        out["arms"][label] = r
        print("")

    dec = arms.get("1s", {}).get("decimated") or {}
    polls = arms.get("1s", {}).get("raw_polls")
    if polls and dec:
        print("=== same 1s response sequence, decimated (aliasing isolated) ===")
        for name in sorted(dec, key=lambda s: int(s.split("_")[1])):
            k = int(name.split("_")[1])
            r = miss_rate(deltas_from_polls(polls[::k]))
            out["decimated"][name] = r
            print("  %-10s eff_gap=%-4ds deltas=%-3d slots=%-3d missed=%-3d rate=%-6s [%s, %s]"
                  % (name, k, r["deltas"], r["slots"], r["missed"],
                     _pct(r["rate"]), _pct(r["lo"]), _pct(r["hi"])))

    print("")
    print("=== is the skipping random, or are fixed grid positions empty? ===")
    for label, arm in arms.items():
        polls = arm.get("raw_polls")
        if polls:
            dd = deltas_from_polls(polls)
        else:
            hist = {float(k): v for k, v in
                    (arm["rankedAt"].get("server_stamp_delta_hist") or {}).items()}
            dd = [k for k, n in hist.items() for _ in range(n)]
        g = geometric_expectation(dd)
        out["arms"][label]["geometric"] = g
        print("  %-4s observed %s" % (label, g["observed"]))
        print("       if slots were skipped independently : %s"
              % g["expected_if_independent"])
        print("       if fixed positions are always empty : %s"
              % g["expected_if_fixed_positions"])

    print("")
    print("=== premarket (docs/35 5-1, 2026-08-07, 1s arm) for comparison ===")
    pre = [k for k, n in PREMARKET_HIST_DOCS35.items() for _ in range(n)]
    r = miss_rate(pre)
    print("  as printed in docs/35 : deltas=%d slots=%d missed=%d rate=%s Wilson95 [%s, %s]"
          % (r["deltas"], r["slots"], r["missed"], _pct(r["rate"]),
             _pct(r["lo"]), _pct(r["hi"])))
    lo_r, hi_r = bracket_missing_one(PREMARKET_HIST_DOCS35)
    print("  docs/35 records %d changes but only %d deltas - the same dropped first"
          % (PREMARKET_CHANGES_DOCS35, r["deltas"]))
    print("  delta. Restoring it puts the premarket point estimate in [%s, %s]."
          % (_pct(lo_r["rate"]), _pct(hi_r["rate"])))
    out["premarket_docs35"] = {"as_printed": r, "bracket": [lo_r["rate"], hi_r["rate"]]}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("json_path", nargs="?",
                    default="coordination/daily/2026-08-18_probe_d_output.json")
    ap.add_argument("--emit-json", default=None, help="결과를 JSON 으로도 저장")
    a = ap.parse_args(argv)
    res = report(Path(a.json_path))
    if a.emit_json:
        Path(a.emit_json).write_text(json.dumps(res, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

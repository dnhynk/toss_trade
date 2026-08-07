"""테이프 포화 실측 — `docs/43` 의 모든 표를 만드는 재실행 가능한 계산. 소유: W4.

**라이브 API 호출 0.** DB 는 read-only URI + `PRAGMA query_only` (계약 C-6), 로그는 읽기만.
수집 코드에 손대지 않고 **이미 저장된 것에서** 다음 세 가지를 낸다:

1. 폴 주기 P 별 포화 칸 비율 (`--cadence`)
2. 결손의 위치·길이 분포와 상한 기여도 (`--gaps`)
3. 잃은 체결 건수의 **하한** 추정 (`--gaps`, 국소 체결률 × 구멍 길이)

## 이 도구가 말할 수 없는 것 (읽기 전에)

- **저장된 행은 이미 검열된 표본이다.** 상한에 잘린 체결은 애초에 DB 에 없다.
  그러므로 여기 나오는 포화 비율·누락 추정은 **전부 하한**이다.
- **초 안 순서는 없다** (`docs/41` §1). ts 는 초 단위이고 같은 초·같은 가격·같은 수량은
  PK 에서 하나로 접힌다 — 그 접힘도 여기서는 안 보인다(수집기 카운터가 재는 몫이다).
- 칸은 에포크에 정렬된 고정 창이고 실제 폴은 정렬돼 있지 않다. 근사다.

사용:
    python -m tools.tape_saturation_probe --db <path> [--log <path>] [--since-ms N]
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

#: `/trades` 한 응답의 상한 (`api.client.TRADES_MAX`, `collector.loops.TRADES_COUNT`).
CAP = 50
#: 표에 낼 폴 주기 후보 (초). 4s 가 현행이다.
WIDTHS = (1, 2, 3, 4, 6, 8, 12, 16)
#: 구멍 직후 국소 체결률을 재는 창 (ms). 짧으면 표본이 없고 길면 버스트가 희석된다.
RATE_WINDOW_MS = 10_000
#: 이 이상 벌어진 결손은 **연속 폴링 중에 생긴 것이 아니다** — tier3 재진입·재기동이다.
#: 4초 폴이 연속이면 구멍이 폴 주기의 두 배를 넘을 수 없다(실측 90.2% 가 4초 이하).
#: 임계를 쓴 유일한 곳이므로 리포트에 이 값과 그 효과를 항상 같이 찍는다.
CONTINUOUS_GAP_MAX_S = 8.0
#: 로그의 결손 줄. 형식은 `collector.loops._poll_trades` 가 찍는다.
GAP_LINE = re.compile(r"tape gap (\S+): prev_max=(\d+) < this_min=(\d+) \(n=(\d+)\)")
KST = dt.timezone(dt.timedelta(hours=9))


def _ro(db: Path) -> sqlite3.Connection:
    uri = f"file:{quote(db.resolve().as_posix(), safe='/:')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _kst(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, KST).strftime("%Y-%m-%d %H:%M")


def _q(vals: list[int], p: float) -> int:
    return vals[min(len(vals) - 1, int(len(vals) * p))]


def cadence_table(conn: sqlite3.Connection, since_ms: int | None) -> list[str]:
    """폴 주기 P 별 포화 칸 비율. **칸 폭 = 한 응답이 덮는 시간폭**이라는 근사 위에 선다."""
    where, params = ("WHERE ts_ms >= ?", (since_ms,)) if since_ms else ("", ())
    rows = conn.execute(
        f"SELECT symbol, ts_ms FROM trades_snap {where}", params).fetchall()
    if not rows:
        return ["체결 행이 없다."]
    lo = min(r[1] for r in rows)
    hi = max(r[1] for r in rows)
    out = [f"행 {len(rows):,} · 종목 {len({r[0] for r in rows}):,} · "
           f"창 {_kst(lo)} ~ {_kst(hi)} KST", "",
           "| 폴 주기 P | 칸 수 | 포화 칸(≥50행) | 비율 | 그 칸이 가진 행 | 비율 | p50 | p90 | p99 | max |",
           "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for w in WIDTHS:
        buckets: dict[tuple[str, int], int] = defaultdict(int)
        for sym, ts in rows:
            buckets[(sym, ts // (w * 1000))] += 1
        vals = sorted(buckets.values())
        sat = [v for v in vals if v >= CAP]
        out.append(
            f"| {w}s | {len(vals):,} | {len(sat):,} | {100 * len(sat) / len(vals):.2f}% | "
            f"{sum(sat):,} | {100 * sum(sat) / len(rows):.1f}% | "
            f"{_q(vals, .5)} | {_q(vals, .9)} | {_q(vals, .99)} | {vals[-1]} |")
    return out


def _read_gaps(log: Path, since_ms: int | None) -> list[tuple[str, int, int, int]]:
    out = []
    with log.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = GAP_LINE.search(line)
            if not m:
                continue
            sym, prev_max, this_min, n = (m.group(1), int(m.group(2)),
                                          int(m.group(3)), int(m.group(4)))
            if since_ms is not None and this_min < since_ms:
                continue
            out.append((sym, prev_max, this_min, n))
    return out


def gap_table(conn: sqlite3.Connection, log: Path, since_ms: int | None) -> list[str]:
    """결손의 길이 분포 + 상한 기여도 + 누락 체결 **하한** 추정.

    ⚠️ 지금은 로그에서 읽는다. 로그는 32MB×4 로 회전하므로 체결 보존기간보다 짧다 —
    그래서 `tape_gaps` 테이블(스키마 v3)이 생겼다. 테이블이 채워지면 그쪽을 읽어야 한다.
    """
    gaps = _read_gaps(log, since_ms)
    if not gaps:
        return ["결손 줄이 없다."]
    lens = {(s, p, t, n): (t - p) / 1000 for s, p, t, n in gaps}
    short = [k for k, v in lens.items() if v <= CONTINUOUS_GAP_MAX_S]
    capped_short = [k for k in short if k[3] >= CAP]
    out = [f"결손 줄 {len(gaps):,}건",
           f"- 그중 구멍 ≤ {CONTINUOUS_GAP_MAX_S:.0f}s (연속 폴링 중) : "
           f"**{len(short):,}건 ({100 * len(short) / len(gaps):.1f}%)**",
           f"- 그중 n≥{CAP} (상한이 원인) : **{len(capped_short):,}건 "
           f"({100 * len(capped_short) / max(len(short), 1):.1f}%)**", "",
           "| 구멍 길이 | 건수 | 비율 | 길이합 |", "|---|---:|---:|---:|"]
    # 마지막 칸은 열려 있다 — 닫으면 표가 100% 가 안 되고, 그 사실이 조용히 사라진다.
    bins = ((0, 2), (2, 4), (4, 8), (8, 16), (16, 60), (60, 300), (300, 3600),
            (3600, float("inf")))
    for lo, hi in bins:
        sel = [v for v in lens.values() if lo < v <= hi]
        label = f"({lo}, {hi:.0f}] s" if hi != float("inf") else f"> {lo}s"
        out.append(f"| {label} | {len(sel):,} | "
                   f"{100 * len(sel) / len(lens):.1f}% | {sum(sel):,.0f}s |")

    total = 0.0
    for sym, prev_max, this_min, _n in capped_short:
        near = conn.execute(
            "SELECT COUNT(*) FROM trades_snap WHERE symbol=? AND ts_ms>=? AND ts_ms<?",
            (sym, this_min, this_min + RATE_WINDOW_MS)).fetchone()[0]
        total += (near / (RATE_WINDOW_MS / 1000)) * ((this_min - prev_max) / 1000)
    where, params = ("WHERE ts_ms >= ?", (since_ms,)) if since_ms else ("", ())
    stored = conn.execute(
        f"SELECT COUNT(*) FROM trades_snap {where}", params).fetchone()[0]
    out += ["",
            f"구멍 길이 합계 {sum(lens[k] for k in capped_short):,.0f}s · "
            f"구멍 직후 {RATE_WINDOW_MS // 1000}s 국소 체결률로 환산한 **누락 하한** "
            f"≈ {total:,.0f}건",
            f"저장 {stored:,}건 대비 **≥ {100 * total / (stored + total):.1f}%** "
            f"의 테이프가 없다.",
            "",
            "> 하한인 이유: 국소 체결률 자체가 검열된 값이고(그 창도 상한에 물렸을 수 "
            "있다), 접힘으로 사라진 행은 여기 안 들어간다."]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--log", type=Path, help="collector.log (결손 표에 필요)")
    ap.add_argument("--since-ms", type=int,
                    help="설정 경계 이후만 (시대를 뭉치지 않기 위해)")
    ap.add_argument("--cadence", action="store_true")
    ap.add_argument("--gaps", action="store_true")
    args = ap.parse_args(argv)
    both = not (args.cadence or args.gaps)

    conn = _ro(args.db)
    try:
        if args.since_ms:
            print(f"관측 창 시작: {_kst(args.since_ms)} KST ({args.since_ms})\n")
        if both or args.cadence:
            print("## 폴 주기별 포화\n")
            print("\n".join(cadence_table(conn, args.since_ms)), "\n")
        if (both or args.gaps):
            if not args.log:
                print("## 결손\n\n--log 가 없어 건너뛴다.")
            else:
                print("## 결손 — 위치와 길이\n")
                print("\n".join(gap_table(conn, args.log, args.since_ms)))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":                                   # pragma: no cover
    sys.exit(main())

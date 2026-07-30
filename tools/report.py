"""리포트 CLI — 소유: W3. tossmon.analysis.report 를 호출하는 엔트리포인트.

사용 예
    python tools/report.py --db data/tossmon.db --out ops/report.md --days 30
    python tools/report.py --db data/tossmon.db --out ops/report.md \
        --from-ms 1780272000000 --to-ms 1780617000000

`--days` 는 `--to-ms`(기본 = --now-ms) 로부터 역산한다. 시간 인자는 전부 UTC epoch ms
(계약 C-1). "지금"을 코드가 임의로 정하지 않도록 `--now-ms` 를 명시할 수 있게 두었다.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

DAY_MS = 86_400_000


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tools/report.py",
        description="수집 DB(read-only)에서 Phase 1 검증 리포트를 생성한다.")
    p.add_argument("--db", required=True, type=Path, help="SQLite DB 경로 (읽기 전용)")
    p.add_argument("--out", required=True, type=Path, help="출력 마크다운 경로")
    p.add_argument("--from-ms", type=int, default=None, help="분석 시작 (UTC epoch ms)")
    p.add_argument("--to-ms", type=int, default=None, help="분석 종료 (UTC epoch ms)")
    p.add_argument("--days", type=int, default=30,
                   help="--from-ms 미지정 시 --to-ms 에서 역산할 일수 (기본 30)")
    p.add_argument("--now-ms", type=int, default=None,
                   help="--to-ms 미지정 시 사용할 현재 시각 (기본: 시스템 시각)")
    return p


def resolve_range(args: argparse.Namespace) -> tuple[int, int]:
    to_ms = args.to_ms
    if to_ms is None:
        to_ms = args.now_ms if args.now_ms is not None else int(time.time() * 1000)
    from_ms = args.from_ms
    if from_ms is None:
        from_ms = to_ms - max(1, args.days) * DAY_MS
    if from_ms >= to_ms:
        raise SystemExit(f"빈 구간: from_ms({from_ms}) >= to_ms({to_ms})")
    return int(from_ms), int(to_ms)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.db.exists():
        print(f"DB 를 찾을 수 없다: {args.db}", file=sys.stderr)
        return 2
    from_ms, to_ms = resolve_range(args)

    from tossmon.analysis.report import generate_report

    out = generate_report(args.db, args.out, from_ms, to_ms)
    print(f"wrote {out} ({from_ms}..{to_ms})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

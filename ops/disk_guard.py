"""디스크 용량 경보 — 소유: W5.

DB/로그 드라이브 여유 공간을 확인해 임계치 미만이면 경고/위험으로 종료 코드를 낸다.
Task Scheduler에서 독립 주기(예: 10분)로 등록해 쓰거나, healthcheck.py 안에서도 같이 표시된다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .healthcheck import STATUS_CRIT, STATUS_OK, STATUS_WARN, check_disk, worse
from .opsconfig import load_ops_config

_EXIT_CODE = {STATUS_OK: 0, STATUS_WARN: 1, STATUS_CRIT: 2}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_ops_config(args.config)
    disks = check_disk([cfg.db_path, cfg.log_dir, cfg.archive_dir],
                        cfg.disk.warn_free_gb, cfg.disk.critical_free_gb)
    overall = STATUS_OK
    for d in disks:
        overall = worse(overall, d.status)

    if args.json:
        print(json.dumps({"overall_status": overall, "disks": [d.__dict__ for d in disks]},
                          ensure_ascii=False, indent=2))
    else:
        for d in disks:
            print(f"[{d.status:4}] {d.path}: free={d.free_gb:.1f}GB / total={d.total_gb:.1f}GB")
        print(f"overall: {overall}")
        if overall != STATUS_OK:
            print("경고: 디스크 여유 공간이 임계치 미만입니다 — "
                  "tools/report.py(W3)/retention.py(W2) 로 아카이브·정리를 고려하십시오.",
                  file=sys.stderr)

    return _EXIT_CODE[overall]


if __name__ == "__main__":
    raise SystemExit(main())

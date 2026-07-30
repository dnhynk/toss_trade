"""로그 로테이션 — 소유: W5.

`log_dir` 안의 `*.log` 파일을 크기 기준으로 회전(타임스탬프 접미사 부여 + gzip 압축)하고,
`log_retention_days` 보다 오래된 압축 로그를 삭제한다. Windows 작업 스케줄러로 주기 실행
(예: 매일 00:10)하는 것을 전제로, 실행 중인 프로세스가 파일을 계속 쓰고 있어도 안전하도록
"이름 바꾸기 → 새 파일에 이어서 씀"(copy-truncate 아님, rename 방식) 전략을 쓴다.

Windows에서는 다른 프로세스가 파일을 열어 놓았으면 rename이 실패할 수 있다 — 이 경우
`should_rotate`는 True를 반환했더라도 `rotate_file`이 `PermissionError`를 잡아 스킵하고
다음 주기에 재시도한다(데이터 유실 없음, 그냥 회전이 늦어질 뿐).
"""
from __future__ import annotations

import argparse
import gzip
import shutil
import time
from pathlib import Path

from .opsconfig import load_ops_config

DATE_FMT = "%Y%m%d-%H%M%S"


def should_rotate(path: Path, max_bytes: int) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= max_bytes
    except OSError:
        return False


def rotate_file(path: Path, now: float | None = None) -> Path | None:
    """회전 성공 시 새로 만들어진 .gz 경로, 실패(파일 점유 등)하면 None."""
    now = time.time() if now is None else now
    stamp = time.strftime(DATE_FMT, time.localtime(now))
    rotated = path.with_name(f"{path.stem}.{stamp}{path.suffix}")
    try:
        path.rename(rotated)
    except (PermissionError, OSError):
        return None
    gz_path = rotated.with_suffix(rotated.suffix + ".gz")
    with open(rotated, "rb") as src, gzip.open(gz_path, "wb") as dst:
        shutil.copyfileobj(src, dst)
    rotated.unlink()
    # 원본 이름으로 빈 파일을 다시 만들어 둔다 — 로그를 append 모드로 여는 프로세스가
    # 다음 write 때 새 파일을 자동으로 만들어내므로 필수는 아니지만, 파일이 계속
    # 열려 있던 핸들을 가진 프로세스가 그 핸들에 계속 쓰는 것(Windows에서는 흔치 않음)을
    # 대비해 존재를 보장한다.
    path.touch(exist_ok=True)
    return gz_path


def purge_old(log_dir: Path, retention_days: int, now: float | None = None) -> list[Path]:
    now = time.time() if now is None else now
    cutoff = now - retention_days * 86400
    removed = []
    if not log_dir.exists():
        return removed
    for p in log_dir.glob("*.log.*.gz"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed.append(p)
        except OSError:
            continue
    return removed


def run(log_dir: Path, max_bytes: int, retention_days: int) -> dict:
    log_dir.mkdir(parents=True, exist_ok=True)
    rotated: list[str] = []
    skipped: list[str] = []
    for p in sorted(log_dir.glob("*.log")):
        if should_rotate(p, max_bytes):
            out = rotate_file(p)
            (rotated if out else skipped).append(str(p))
    removed = [str(p) for p in purge_old(log_dir, retention_days)]
    return {"rotated": rotated, "rotate_skipped_busy": skipped, "purged": removed}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=None)
    args = ap.parse_args(argv)
    cfg = load_ops_config(args.config)
    result = run(cfg.log_dir, cfg.log_max_bytes, cfg.log_retention_days)
    print(f"rotated: {len(result['rotated'])}, skipped(busy): {len(result['rotate_skipped_busy'])}, "
          f"purged: {len(result['purged'])}")
    for k in ("rotated", "rotate_skipped_busy", "purged"):
        for item in result[k]:
            print(f"  [{k}] {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

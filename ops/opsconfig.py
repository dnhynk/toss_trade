"""Ops 전용 설정 로더 — 소유: W5.

`tossmon/config.py`의 `Config`(운영 파라미터: 티어/폴링/한도)는 W4 소유이며 이 모듈은
그것과 무관하다. 여기서는 감시·복구 스크립트(healthcheck/supervisor/rotate_logs/disk_guard)가
공유하는 **경로·임계값**만 다룬다. 임의로 tossmon/config.py 의 Config 에 키를 추가하지 않기
위한 의도적 분리다.

파일 우선순위: `ops/ops_config.yaml`(있으면) → 없으면 `ops/ops_config.example.yaml`.
운영자는 example 을 복사해 `ops/ops_config.yaml` 을 만든다 (이 파일은 gitignore 대상 아님 —
경로/임계값만 담고 시크릿이 없기 때문. 시크릿은 여전히 `api_keys` 에만 있다).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path("ops/ops_config.yaml")
EXAMPLE_CONFIG_PATH = Path("ops/ops_config.example.yaml")


@dataclass(frozen=True)
class DiskThresholds:
    warn_free_gb: float = 5.0
    critical_free_gb: float = 2.0


@dataclass(frozen=True)
class OpsConfig:
    db_path: Path
    log_dir: Path
    state_dir: Path
    archive_dir: Path
    disk: DiskThresholds
    stale_minutes_warn: int
    stale_minutes_critical: int
    log_retention_days: int
    log_max_bytes: int
    collector_cmd: list[str]
    max_restarts_per_window: int
    restart_window_s: int
    restart_backoff_base_s: float
    restart_backoff_cap_s: float


def _as_path(data: dict, key: str, default: str) -> Path:
    return Path(str(data.get(key, default)))


def load_ops_config(path: Path | str | None = None) -> OpsConfig:
    """YAML 로드. 파일이 없으면 example 로 폴백(부트스트랩 편의). 필수 키 결손은 기본값으로 보정."""
    candidates = [Path(path)] if path is not None else [DEFAULT_CONFIG_PATH, EXAMPLE_CONFIG_PATH]
    data: dict = {}
    for c in candidates:
        if c.exists():
            data = yaml.safe_load(c.read_text(encoding="utf-8")) or {}
            break

    disk = data.get("disk") or {}
    restart = data.get("restart") or {}
    collector_cmd = data.get("collector_cmd")
    if not collector_cmd:
        collector_cmd = ["python", "-m", "tossmon.collector.main"]

    return OpsConfig(
        db_path=_as_path(data, "db_path", "data/tossmon.db"),
        log_dir=_as_path(data, "log_dir", "data/logs"),
        state_dir=_as_path(data, "state_dir", "data/ops_state"),
        archive_dir=_as_path(data, "archive_dir", "data/archive"),
        disk=DiskThresholds(
            warn_free_gb=float(disk.get("warn_free_gb", 5.0)),
            critical_free_gb=float(disk.get("critical_free_gb", 2.0)),
        ),
        stale_minutes_warn=int(data.get("stale_minutes_warn", 5)),
        stale_minutes_critical=int(data.get("stale_minutes_critical", 15)),
        log_retention_days=int(data.get("log_retention_days", 14)),
        log_max_bytes=int(data.get("log_max_bytes", 20_000_000)),
        collector_cmd=list(collector_cmd),
        max_restarts_per_window=int(restart.get("max_restarts_per_window", 5)),
        restart_window_s=int(restart.get("restart_window_s", 600)),
        restart_backoff_base_s=float(restart.get("backoff_base_s", 2.0)),
        restart_backoff_cap_s=float(restart.get("backoff_cap_s", 300.0)),
    )

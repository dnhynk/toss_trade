"""ops/opsconfig.py 테스트 — 소유: W5."""
from __future__ import annotations

from pathlib import Path

from ops.opsconfig import load_ops_config


def test_load_ops_config_falls_back_to_defaults_when_missing(tmp_path):
    cfg = load_ops_config(tmp_path / "does_not_exist.yaml")
    assert cfg.db_path == Path("data/tossmon.db")
    assert cfg.disk.warn_free_gb == 5.0
    assert cfg.collector_cmd == ["python", "-m", "tossmon.collector.main"]


def test_load_ops_config_reads_custom_values(tmp_path):
    p = tmp_path / "ops_config.yaml"
    p.write_text(
        """
db_path: "custom/db.sqlite"
log_dir: "custom/logs"
disk:
  warn_free_gb: 1.5
  critical_free_gb: 0.5
stale_minutes_warn: 1
collector_cmd: ["python", "-m", "fake.entry"]
restart:
  max_restarts_per_window: 9
""",
        encoding="utf-8",
    )
    cfg = load_ops_config(p)
    assert cfg.db_path == Path("custom/db.sqlite")
    assert cfg.log_dir == Path("custom/logs")
    assert cfg.disk.warn_free_gb == 1.5
    assert cfg.disk.critical_free_gb == 0.5
    assert cfg.stale_minutes_warn == 1
    assert cfg.collector_cmd == ["python", "-m", "fake.entry"]
    assert cfg.max_restarts_per_window == 9


def test_load_ops_config_example_file_is_valid():
    cfg = load_ops_config(Path("ops/ops_config.example.yaml"))
    assert cfg.db_path == Path("data/tossmon.db")
    assert cfg.disk.warn_free_gb == 5.0
    assert cfg.max_restarts_per_window == 5

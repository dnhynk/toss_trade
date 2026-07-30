"""ops/disk_guard.py 테스트 — 소유: W5."""
from __future__ import annotations

import json

from ops import disk_guard


def test_main_exits_nonzero_when_over_threshold(tmp_path, capsys, monkeypatch):
    cfg_path = tmp_path / "ops_config.yaml"
    cfg_path.write_text(
        f"""
db_path: "{(tmp_path / 'tossmon.db').as_posix()}"
log_dir: "{(tmp_path / 'logs').as_posix()}"
archive_dir: "{(tmp_path / 'archive').as_posix()}"
disk:
  warn_free_gb: 1000000000
  critical_free_gb: 1000000000
""",
        encoding="utf-8",
    )
    rc = disk_guard.main(["--config", str(cfg_path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert out["overall_status"] == "CRIT"


def test_main_ok_when_thresholds_are_zero(tmp_path, capsys):
    cfg_path = tmp_path / "ops_config.yaml"
    cfg_path.write_text(
        f"""
db_path: "{(tmp_path / 'tossmon.db').as_posix()}"
log_dir: "{(tmp_path / 'logs').as_posix()}"
archive_dir: "{(tmp_path / 'archive').as_posix()}"
disk:
  warn_free_gb: 0
  critical_free_gb: 0
""",
        encoding="utf-8",
    )
    rc = disk_guard.main(["--config", str(cfg_path)])
    assert rc == 0
    assert "overall: OK" in capsys.readouterr().out

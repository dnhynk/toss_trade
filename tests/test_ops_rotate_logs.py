"""ops/rotate_logs.py 테스트 — 소유: W5."""
from __future__ import annotations

import gzip
import os
import time
from pathlib import Path

import pytest

from ops import rotate_logs as rl


def test_should_rotate_by_size(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"x" * 100)
    assert rl.should_rotate(p, max_bytes=50) is True
    assert rl.should_rotate(p, max_bytes=1000) is False


def test_should_rotate_missing_file_is_false(tmp_path):
    assert rl.should_rotate(tmp_path / "nope.log", max_bytes=1) is False


def test_rotate_file_creates_gz_and_resets_original(tmp_path):
    p = tmp_path / "collector.log"
    payload = b"hello world\n" * 100
    p.write_bytes(payload)

    out = rl.rotate_file(p, now=1_700_000_000.0)
    assert out is not None
    assert out.name.endswith(".log.gz")
    with gzip.open(out, "rb") as fh:
        assert fh.read() == payload
    # 원본 이름의 빈 파일이 다시 존재해야 한다 (append 로거가 계속 쓸 수 있도록).
    assert p.exists()
    assert p.stat().st_size == 0


def test_rotate_file_returns_none_when_rename_fails(tmp_path, monkeypatch):
    p = tmp_path / "busy.log"
    p.write_bytes(b"data")

    def _raise_rename(self, target):
        raise PermissionError("simulated lock")

    monkeypatch.setattr(Path, "rename", _raise_rename)
    assert rl.rotate_file(p) is None


def test_purge_old_removes_expired_archives(tmp_path):
    log_dir = tmp_path
    old = log_dir / "a.log.20200101-000000.gz"
    fresh = log_dir / "b.log.20260101-000000.gz"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    now = time.time()
    os.utime(old, (now - 100 * 86400, now - 100 * 86400))
    os.utime(fresh, (now - 1 * 86400, now - 1 * 86400))

    removed = rl.purge_old(log_dir, retention_days=14, now=now)
    assert old in removed
    assert not old.exists()
    assert fresh.exists()


def test_purge_old_missing_dir_is_noop(tmp_path):
    assert rl.purge_old(tmp_path / "nope", retention_days=1) == []


def test_run_rotates_and_purges(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    big = log_dir / "collector.log"
    big.write_bytes(b"x" * 1000)
    small = log_dir / "other.log"
    small.write_bytes(b"y" * 10)

    old_archive = log_dir / "collector.log.20200101-000000.gz"
    old_archive.write_bytes(b"z")
    now = time.time()
    os.utime(old_archive, (now - 100 * 86400, now - 100 * 86400))

    result = rl.run(log_dir, max_bytes=100, retention_days=14)
    assert len(result["rotated"]) == 1
    assert str(old_archive) in result["purged"]
    assert small.exists() and small.stat().st_size == 10  # 임계 미만이라 회전 안 됨

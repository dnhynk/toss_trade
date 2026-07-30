"""ops/supervisor.py 테스트 — 소유: W5."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from ops.opsconfig import DiskThresholds, OpsConfig
from ops.supervisor import RestartBudget, Supervisor, backoff_delay, stop_requested


def test_backoff_delay_zero_on_first_attempt():
    assert backoff_delay(0, base_s=2.0, cap_s=100.0) == 0.0


def test_backoff_delay_exponential_then_capped():
    assert backoff_delay(1, base_s=2.0, cap_s=100.0) == 2.0
    assert backoff_delay(2, base_s=2.0, cap_s=100.0) == 4.0
    assert backoff_delay(3, base_s=2.0, cap_s=100.0) == 8.0
    assert backoff_delay(10, base_s=2.0, cap_s=100.0) == 100.0


def test_restart_budget_under_limit():
    b = RestartBudget(max_restarts=3, window_s=60)
    for t in (0, 10, 20):
        b.record(t)
    assert b.over_limit(20) is False


def test_restart_budget_over_limit():
    b = RestartBudget(max_restarts=2, window_s=60)
    for t in (0, 10, 20, 30):
        b.record(t)
    assert b.over_limit(30) is True


def test_restart_budget_old_events_roll_off():
    b = RestartBudget(max_restarts=1, window_s=10)
    b.record(0)
    b.record(1)
    assert b.over_limit(1) is True
    # 윈도 밖으로 오래된 이벤트가 사라지면 다시 한도 안으로 들어온다.
    b.record(100)
    assert b.over_limit(100) is False


def test_stop_requested(tmp_path):
    stop_file = tmp_path / "STOP"
    assert stop_requested(stop_file) is False
    stop_file.write_text("stop")
    assert stop_requested(stop_file) is True


def _cfg(tmp_path, collector_cmd, max_restarts=5, window_s=600, base=0.01, cap=0.05):
    return OpsConfig(
        db_path=tmp_path / "tossmon.db",
        log_dir=tmp_path / "logs",
        state_dir=tmp_path / "state",
        archive_dir=tmp_path / "archive",
        disk=DiskThresholds(warn_free_gb=0, critical_free_gb=0),
        stale_minutes_warn=5,
        stale_minutes_critical=15,
        log_retention_days=14,
        log_max_bytes=1000,
        collector_cmd=collector_cmd,
        max_restarts_per_window=max_restarts,
        restart_window_s=window_s,
        restart_backoff_base_s=base,
        restart_backoff_cap_s=cap,
    )


def test_supervisor_does_not_restart_on_clean_exit(tmp_path):
    cfg = _cfg(tmp_path, [sys.executable, "-c", "raise SystemExit(0)"])
    sup = Supervisor(cfg, stop_file=tmp_path / "STOP")
    rc = sup.run()
    assert rc == 0


def test_supervisor_stops_immediately_when_stop_file_present(tmp_path, monkeypatch):
    stop_file = tmp_path / "STOP"
    stop_file.write_text("stop")
    cfg = _cfg(tmp_path, [sys.executable, "-c", "raise SystemExit(1)"])
    sup = Supervisor(cfg, stop_file=stop_file)

    def _fail_spawn():
        raise AssertionError("should not spawn when stop file already present")

    monkeypatch.setattr(sup, "_spawn", _fail_spawn)
    rc = sup.run()
    assert rc == 0


def test_supervisor_gives_up_after_restart_budget_exceeded(tmp_path):
    cfg = _cfg(tmp_path, [sys.executable, "-c", "raise SystemExit(1)"], max_restarts=2, window_s=600)
    sup = Supervisor(cfg, stop_file=tmp_path / "STOP")
    rc = sup.run(max_iterations=50)
    assert rc == 1
    # max_restarts=2 -> 최초 실행(재시작 아님) + 재시작 2회까지 허용, 3번째 재시작 시 중단.
    assert len(sup.budget._events) <= 3


def test_supervisor_terminates_running_child_when_stop_file_appears(tmp_path):
    """라이브 리허설 중 발견된 결함의 회귀 테스트: STOP 파일은 자식이 살아있는 동안에도
    감지돼야 한다 (기존엔 `wait()`가 자식 종료까지 무한 대기해 절대 반응하지 않았다)."""
    stop_file = tmp_path / "STOP"
    # 30초 슬립 — STOP 감지 없이는 테스트가 30초 넘게 걸려야 끝난다.
    cfg = _cfg(tmp_path, [sys.executable, "-c", "import time; time.sleep(30)"])
    sup = Supervisor(cfg, stop_file=stop_file, stop_poll_s=0.05)

    result = {}

    def _run():
        result["rc"] = sup.run()

    t = threading.Thread(target=_run, daemon=True)
    started = time.monotonic()
    t.start()
    time.sleep(0.3)  # 자식이 확실히 뜬 뒤에 STOP 을 건다
    stop_file.write_text("stop")
    t.join(timeout=10)

    assert not t.is_alive(), "supervisor did not exit promptly after STOP file appeared"
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"took {elapsed:.1f}s — STOP file was not detected while child ran"
    assert result["rc"] == 0

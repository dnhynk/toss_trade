"""백필 자가 복구 슈퍼바이저 (tools/backfill_supervisor.py) — mock 시나리오 검증. 소유: W4.

라이브 호출 없음 — 러너·프로브·에스컬레이션을 전부 가짜 스크립트로 치환한다
(진행 중인 라이브 백필 런·체크포인트·DB 와 경합 금지, 태스크 불변 규칙 1).

무엇을 지키는가
    * 403 죽음 -> 프로브 대기 -> 성공 즉시 체크포인트 재개 (상한 없음).
    * 외부 킬/일시 오류 -> 백오프 재기동; 진행이 있으면 연속 카운트가 리셋된다.
    * 같은 체크포인트 지문에서 3회 연속 크래시 -> "ESCALATION:" 로그 + 재시도 중단.
      (프로브가 즉시 성공하는 403 크래시 루프도 같은 규칙에 잡힌다 — 무한 tight loop 방지.)
    * STOP 파일/--deadline -> 러너에 --stop-file 로 정상 정지 전달, 체크포인트 플러시 확인.
    * STATUS 정직성: 러너가 죽은 뒤에는 절대 runner=alive 로 찍히지 않는다 (RUNNER-DEAD).
    * exit 0 인데 매니페스트가 없으면 완료로 치지 않는다 (죽은 숫자 미화 금지).
    * 러너(tools/backfill.py) --stop-file: 페이지 경계에서 멈추고 진행분이 체크포인트에
      남으며, 재실행이 스크린 재호출 없이 이어받아 완주한다.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import textwrap
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.test_backfill_runner import (
    FakeBackfillClient,
    daily_bar,
    utc_ms,
    weekdays,
)
from tests.test_collector_helpers import make_config
from tossmon.api.models import Candle

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, filename: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "tools" / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SUP = _load("tossmon_backfill_supervisor", "backfill_supervisor.py")
BF = _load("tossmon_backfill", "backfill.py")

MIN_MS = 60_000


# --------------------------------------------------------------------------- #
# 가짜 스크립트 헬퍼 — 러너는 OUT/runs.txt 로 기동 횟수를 세고, --stop-file 을 파싱한다.
# --------------------------------------------------------------------------- #
def _runner(tmp: Path, body: str) -> Path:
    header = (
        "import json, sys, time\n"
        "from pathlib import Path\n"
        f"OUT = Path({str(tmp)!r})\n"
        "stop = (Path(sys.argv[sys.argv.index('--stop-file') + 1])\n"
        "        if '--stop-file' in sys.argv else None)\n"
        "runs_f = OUT / 'runs.txt'\n"
        "n = (int(runs_f.read_text()) if runs_f.exists() else 0) + 1\n"
        "runs_f.write_text(str(n))\n")
    p = tmp / "fake_runner.py"
    p.write_text(header + textwrap.dedent(body), encoding="utf-8")
    return p


def _plain(tmp: Path, name: str, body: str) -> Path:
    header = ("import json, sys, time\n"
              "from pathlib import Path\n"
              f"OUT = Path({str(tmp)!r})\n")
    p = tmp / name
    p.write_text(header + textwrap.dedent(body), encoding="utf-8")
    return p


def _probe_fail(tmp: Path) -> Path:
    return _plain(tmp, "fake_probe_fail.py", "sys.exit(3)\n")


def _probe_ok(tmp: Path) -> Path:
    return _plain(tmp, "fake_probe_ok.py", "sys.exit(0)\n")


def _escalate(tmp: Path) -> Path:
    return _plain(tmp, "fake_escalate.py",
                  "(OUT / 'escalation.txt').write_text(' | '.join(sys.argv[1:]))\n"
                  "sys.exit(0)\n")


def _argv(tmp: Path, runner: Path, *, probe: Path | None = None,
          extra: list[str] = ()) -> list[str]:
    """빠른 폴링/짧은 백오프의 테스트 인자. 프로브·에스컬레이션은 항상 가짜다
    (기본값이 실 프로브/orca 를 부르는 사고 방지)."""
    argv = ["--out", str(tmp), "--poll-s", "0.03", "--status-interval-s", "0.15",
            "--probe-interval-s", "0.15", "--backoff-s", "0.05,0.08,0.1",
            "--stop-grace-s", "3", "--probe-timeout-s", "10"]
    for a in (sys.executable, str(runner)):
        argv += ["--runner-cmd", a]
    for a in (sys.executable, str(probe or _probe_fail(tmp))):
        argv += ["--probe-cmd", a]
    for a in (sys.executable, str(_escalate(tmp)), "{subject}", "{body}"):
        argv += ["--escalate-cmd", a]
    return argv + list(extra)


def _run_main(argv: list[str], timeout: float = 25.0,
              during: "callable | None" = None) -> int:
    result: dict = {}

    def go():
        result["rc"] = SUP.main(argv)

    t = threading.Thread(target=go, daemon=True)
    t.start()
    if during is not None:
        during(t)
    t.join(timeout)
    assert not t.is_alive(), "supervisor did not finish in time"
    return result["rc"]


def _log(tmp: Path) -> str:
    return (tmp / "supervisor.log").read_text(encoding="ascii")


def _runs(tmp: Path) -> int:
    f = tmp / "runs.txt"
    return int(f.read_text()) if f.exists() else 0


def _wait_for(pred, timeout: float = 8.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached in time")


# --------------------------------------------------------------------------- #
# 정상 완료 + 첫 로그 줄
# --------------------------------------------------------------------------- #
def test_done_path_and_first_log_line_names_stop_file(tmp_path):
    runner = _runner(tmp_path, """
        (OUT / 'backfill_manifest.json').write_text('{}')
        sys.exit(0)
    """)
    rc = _run_main(_argv(tmp_path, runner))
    assert rc == 0
    log = _log(tmp_path)
    first = log.splitlines()[0]
    assert "SUPERVISOR-START" in first and "SUPERVISOR.STOP" in first
    assert "DONE" in log and log.count("RUNNER-START") == 1


def test_exit0_without_manifest_is_not_done(tmp_path):
    runner = _runner(tmp_path, """
        if n == 1:
            sys.exit(0)                      # 매니페스트 없는 exit 0 — 부정직한 종료
        (OUT / 'backfill_manifest.json').write_text('{}')
        sys.exit(0)
    """)
    rc = _run_main(_argv(tmp_path, runner))
    assert rc == 0
    log = _log(tmp_path)
    assert "EXIT-0-WITHOUT-MANIFEST" in log
    assert "BACKOFF" in log and "DONE" in log
    assert _runs(tmp_path) == 2


# --------------------------------------------------------------------------- #
# 403 죽음 -> 프로브 대기 -> 재개
# --------------------------------------------------------------------------- #
def test_403_death_probe_wait_then_resume(tmp_path):
    runner = _runner(tmp_path, """
        if n == 1:
            print('FATAL: Forbidden: 403 ip-not-allowed - aborting whole run')
            sys.exit(3)
        (OUT / 'backfill_manifest.json').write_text('{}')
        sys.exit(0)
    """)
    probe = _plain(tmp_path, "fake_probe_seq.py", """
        pf = OUT / 'probes.txt'
        k = (int(pf.read_text()) if pf.exists() else 0) + 1
        pf.write_text(str(k))
        sys.exit(3 if k <= 2 else 0)         # 두 번 막혔다가 세 번째에 열린다
    """)
    rc = _run_main(_argv(tmp_path, runner, probe=probe), timeout=30)
    assert rc == 0
    log = _log(tmp_path)
    assert "class=ip-blocked" in log
    assert log.count("PROBE forbidden") >= 2
    assert "PROBE ok" in log and "DONE" in log
    assert "ESCALATION:" not in log
    assert _runs(tmp_path) == 2
    assert int((tmp_path / "probes.txt").read_text()) >= 3


# --------------------------------------------------------------------------- #
# 외부 킬 -> 백오프 재기동 (진행이 있으면 연속 카운트 리셋)
# --------------------------------------------------------------------------- #
def test_external_kill_backoff_restart_with_progress(tmp_path):
    runner = _runner(tmp_path, """
        (OUT / 'checkpoint.json').write_text(
            json.dumps({'version': 1, 'screen': {}, 'windows': {}, 'n': n}))
        if n < 3:
            sys.exit(1)                      # 외부 킬/일시 오류 시뮬레이션
        (OUT / 'backfill_manifest.json').write_text('{}')
        sys.exit(0)
    """)
    rc = _run_main(_argv(tmp_path, runner))
    assert rc == 0
    log = _log(tmp_path)
    assert _runs(tmp_path) == 3
    backoffs = [ln for ln in log.splitlines() if "BACKOFF" in ln]
    assert len(backoffs) == 2
    # 체크포인트가 매번 전진했으므로 연속 카운트는 리셋 — 둘 다 strike=1.
    assert all("strike=1" in ln for ln in backoffs)
    assert "DONE" in log and "ESCALATION:" not in log


# --------------------------------------------------------------------------- #
# 같은 지점 3연속 크래시 -> ESCALATION
# --------------------------------------------------------------------------- #
def test_three_same_point_crashes_escalate(tmp_path):
    runner = _runner(tmp_path, "sys.exit(1)\n")     # 체크포인트 불변 — 같은 지점
    rc = _run_main(_argv(tmp_path, runner))
    assert rc == 1
    log = _log(tmp_path)
    assert _runs(tmp_path) == 3                     # 4번째 기동은 없다
    assert "ESCALATION:" in log
    assert "strike=1" in log and "strike=2" in log
    marker = (tmp_path / "escalation.txt").read_text()
    assert "manual intervention" in marker          # subject 치환 확인
    assert "supervisor.log" in marker               # body 에 로그 경로


def test_endpoint_violation_escalates_immediately(tmp_path):
    runner = _runner(tmp_path, """
        print('FATAL: ForbiddenEndpoint: order-family endpoint reached')
        sys.exit(3)
    """)
    rc = _run_main(_argv(tmp_path, runner))
    assert rc == 1
    log = _log(tmp_path)
    assert _runs(tmp_path) == 1                     # 재시도 없음 — 코드 버그
    assert "class=endpoint-violation" in log and "ESCALATION:" in log


def test_forbidden_tight_loop_counts_as_same_point_crash(tmp_path):
    """프로브가 즉시 성공하는 403 죽음(=IP 는 안 막혀 있었다)이 무한 재기동 루프가
    되지 않고 same-point 규칙으로 3회에서 끊긴다."""
    runner = _runner(tmp_path, """
        print('FATAL: Forbidden: 403 something-not-ip - aborting whole run')
        sys.exit(3)
    """)
    rc = _run_main(_argv(tmp_path, runner, probe=_probe_ok(tmp_path)))
    assert rc == 1
    log = _log(tmp_path)
    assert "PROBE-IMMEDIATE-OK" in log
    assert "ESCALATION:" in log
    assert _runs(tmp_path) == 3


# --------------------------------------------------------------------------- #
# STOP 파일 / --deadline 우아한 정지
# --------------------------------------------------------------------------- #
_POLLING_RUNNER = """
    end = time.monotonic() + 20
    while time.monotonic() < end:
        if stop is not None and stop.exists():
            (OUT / 'checkpoint.json').write_text(
                json.dumps({'version': 1, 'screen': {}, 'windows': {}}))
            sys.exit(4)                      # tools/backfill.py 의 stop-file 종료코드
        time.sleep(0.02)
    sys.exit(1)
"""


def test_stop_file_relays_graceful_stop_and_verifies_flush(tmp_path):
    runner = _runner(tmp_path, _POLLING_RUNNER)
    started = time.monotonic()

    def during(_t):
        _wait_for(lambda: (tmp_path / "supervisor.log").exists()
                  and "RUNNER-START" in _log(tmp_path))
        (tmp_path / "SUPERVISOR.STOP").write_text("stop")

    rc = _run_main(_argv(tmp_path, runner), timeout=15, during=during)
    elapsed = time.monotonic() - started
    assert rc == 0
    log = _log(tmp_path)
    assert "STOP-REQUEST source=stop-file" in log
    assert "class=stopped" in log and "STOP-FORCE" not in log
    assert "CHECKPOINT-FLUSH ok=1" in log
    assert "STOP source=stop-file" in log
    assert elapsed < 12, f"graceful stop took {elapsed:.1f}s"


def test_deadline_in_past_stops_before_any_spawn(tmp_path):
    runner = _runner(tmp_path, "sys.exit(1)\n")
    rc = _run_main(_argv(tmp_path, runner, extra=["--deadline", "2000-01-01 00:00"]))
    assert rc == 0
    log = _log(tmp_path)
    assert "STOP source=deadline" in log
    assert "RUNNER-START" not in log and _runs(tmp_path) == 0


def test_deadline_stops_running_child(tmp_path):
    runner = _runner(tmp_path, _POLLING_RUNNER)
    deadline = (datetime.now() + timedelta(seconds=2)).strftime("%Y-%m-%d %H:%M:%S")
    started = time.monotonic()
    rc = _run_main(_argv(tmp_path, runner, extra=["--deadline", deadline]), timeout=15)
    assert rc == 0
    log = _log(tmp_path)
    assert "STOP-REQUEST source=deadline" in log
    assert "CHECKPOINT-FLUSH ok=1" in log
    assert time.monotonic() - started < 12


def test_parse_deadline_formats():
    assert SUP.parse_deadline("2026-08-02 07:30") == datetime(2026, 8, 2, 7, 30)
    assert SUP.parse_deadline("2026-08-02 07:30:15") == datetime(2026, 8, 2, 7, 30, 15)
    with pytest.raises(SystemExit):
        SUP.parse_deadline("tomorrow-ish")


# --------------------------------------------------------------------------- #
# STATUS 정직성 — 죽은 러너는 절대 alive 로 찍히지 않는다
# --------------------------------------------------------------------------- #
def test_status_honesty_dead_runner_is_never_reported_alive(tmp_path):
    runner = _runner(tmp_path, """
        if n == 1:
            time.sleep(0.6)
            print('FATAL: Forbidden: 403 ip-not-allowed - aborting whole run')
            sys.exit(3)
        sys.exit(1)
    """)

    def during(_t):
        time.sleep(1.6)                       # alive STATUS + 사후 RUNNER-DEAD 를 모두 관찰
        (tmp_path / "SUPERVISOR.STOP").write_text("stop")

    rc = _run_main(_argv(tmp_path, runner,
                         extra=["--probe-interval-s", "5"]),   # 프로브 대기 중을 관찰
                   timeout=15, during=during)
    assert rc == 0
    lines = _log(tmp_path).splitlines()
    alive = [i for i, ln in enumerate(lines) if "runner=alive" in ln]
    dead = [i for i, ln in enumerate(lines) if "RUNNER-DEAD" in ln]
    exit_i = next(i for i, ln in enumerate(lines) if "RUNNER-EXIT" in ln)
    assert alive, "no alive STATUS while runner ran"
    assert dead, "no RUNNER-DEAD while waiting"
    assert all(i < exit_i for i in alive), "alive STATUS after runner death (dishonest)"
    assert all("reason=ip-blocked" in lines[i] and "next_retry=" in lines[i]
               for i in dead)


# --------------------------------------------------------------------------- #
# --register: launcher 생성 + schtasks 호출 구성 (실제 등록 없음 — monkeypatch)
# --------------------------------------------------------------------------- #
def test_register_writes_launcher_and_calls_schtasks(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(SUP, "_exec", lambda cmd: (calls.append(cmd) or (0, "ok")))
    monkeypatch.setenv("TOSS_LIVE", "1")
    monkeypatch.setenv("TOSS_BASE_URL", "https://live.example.test")
    rc = SUP.main(["--register", "--out", str(tmp_path), "--from", "2025-09-01"])
    assert rc == 0
    launcher = tmp_path / "run_supervisor.cmd"
    text = launcher.read_text(encoding="ascii")
    assert "backfill_supervisor.py" in text
    assert "--from 2025-09-01" in text
    assert "set TOSS_LIVE=1" in text
    assert "set TOSS_BASE_URL=https://live.example.test" in text
    assert len(calls) == 2
    assert "/Create" in calls[0] and "toss-backfill-supervisor" in calls[0]
    assert str(launcher) in " ".join(calls[0])
    assert "/Run" in calls[1]


def test_register_refuses_without_live_env(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(SUP, "_exec", lambda cmd: (calls.append(cmd) or (0, "ok")))
    monkeypatch.setenv("TOSS_LIVE", "1")
    monkeypatch.delenv("TOSS_BASE_URL", raising=False)
    rc = SUP.main(["--register", "--out", str(tmp_path)])
    assert rc == 2
    assert not calls and not (tmp_path / "run_supervisor.cmd").exists()


# --------------------------------------------------------------------------- #
# 러너(tools/backfill.py) --stop-file — 정지 경계·진행 보존·재개 완주
# --------------------------------------------------------------------------- #
TRAIN = weekdays("2026-01-05", "2026-03-06")
SPIKE = "2026-02-20"


def _mins(symbol: str, date: str, m: int, *, start: int = 0, step: int = 2):
    base = utc_ms(date)
    return [Candle(symbol=symbol, ts_ms=base + (start + i * step) * MIN_MS,
                   open_u=1_000_000, high_u=1_001_000, low_u=999_000,
                   close_u=1_000_000, vol_qu=1_000_000) for i in range(m)]


def _spike_data():
    daily = []
    for d in TRAIN:
        if d == SPIKE:
            daily.append(daily_bar("SPKY", d, close=1_300_000, high=1_310_000,
                                   low=1_290_000, open_=1_295_000))
        else:
            daily.append(daily_bar("SPKY", d, close=1_000_000))
    window_start_day = TRAIN[TRAIN.index(SPIKE) - 25]
    minute = (_mins("SPKY", window_start_day, 1)
              + _mins("SPKY", TRAIN[TRAIN.index(SPIKE) - 1], 400)
              + _mins("SPKY", SPIKE, 210))
    return {"SPKY": daily}, {"SPKY": minute}


def _make_runner(tmp_path, client, out: str, stop: Path | None):
    checkpoint = BF.Checkpoint(tmp_path / out / "checkpoint.json")
    calendar = BF.TradingCalendar.from_json(checkpoint.data.get("calendar"))
    return BF.Runner(cfg=make_config(tmp_path), client=client, store=None,
                     checkpoint=checkpoint, calendar=calendar,
                     out_dir=tmp_path / out, stop_file=stop), checkpoint


def test_runner_stop_file_preempts_before_any_call(tmp_path):
    daily, minute = _spike_data()
    client = FakeBackfillClient(TRAIN, daily, minute)
    stop = tmp_path / "RUNNER.STOP"
    stop.write_text("stop")
    runner, checkpoint = _make_runner(tmp_path, client, "out", stop)
    with pytest.raises(BF.StopRequested):
        asyncio.run(runner.run(["SPKY"]))
    assert client.calls == {"1d": 0, "1m": 0, "calendar": 0}   # 호출 0회에서 멈춤
    assert checkpoint.path.exists()                            # 체크포인트는 저장됨


class _StopAfterFirst1m(FakeBackfillClient):
    def __init__(self, *args, stop_path: Path, **kw):
        super().__init__(*args, **kw)
        self.stop_path = stop_path

    async def get_candles(self, symbol, interval, count=200, before_ms=None,
                          adjusted=True):
        page = await super().get_candles(symbol, interval, count=count,
                                         before_ms=before_ms, adjusted=adjusted)
        if interval == "1m" and self.calls["1m"] == 1:
            self.stop_path.write_text("stop")      # 첫 1m 페이지 직후 정지 요청
        return page


def test_runner_stop_mid_window_persists_progress_and_resumes(tmp_path):
    daily, minute = _spike_data()
    # 기준 런 (정지 없음) — 재개 완주가 봉 수까지 동일해야 한다.
    ref_runner, _ = _make_runner(tmp_path, FakeBackfillClient(TRAIN, daily, minute),
                                 "ref", None)
    ref = asyncio.run(ref_runner.run(["SPKY"]))
    ref_win = ref["symbols"]["SPKY"]["windows"]
    assert len(ref_win) == 1 and ref_win[0]["status"] == "done"

    stop = tmp_path / "RUNNER.STOP"
    client1 = _StopAfterFirst1m(TRAIN, daily, minute, stop_path=stop)
    runner1, ckpt1 = _make_runner(tmp_path, client1, "out", stop)
    with pytest.raises(BF.StopRequested):
        asyncio.run(runner1.run(["SPKY"]))
    saved = BF.Checkpoint(ckpt1.path)                          # 디스크에서 재로드
    wins = list(saved.data["windows"].values())
    assert len(wins) == 1
    assert wins[0]["bars"] > 0 and wins[0]["oldest_ms"] is not None
    assert wins[0]["status"] == "todo"                         # 미완 — 재개 대상

    stop.unlink()
    client2 = FakeBackfillClient(TRAIN, daily, minute)
    runner2, _ = _make_runner(tmp_path, client2, "out", stop)
    manifest = asyncio.run(runner2.run(["SPKY"]))
    win = manifest["symbols"]["SPKY"]["windows"][0]
    assert win["status"] == "done"
    assert win["bars_in_window"] == ref_win[0]["bars_in_window"]   # 유실·중복 없음
    assert client2.calls["1d"] == 0                            # 스크린은 캐시로 스킵
    assert client2.calls["1m"] < client1.calls["1m"] + 10      # 이어받기 (전체 재수집 아님)

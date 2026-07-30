"""프로세스 감시·자동 재시작 — 소유: W5.

collector(W4, `tossmon.collector`)를 자식 프로세스로 띄우고 죽으면 재시작한다.
Windows 작업 스케줄러의 "실패 시 재시작" 옵션과 별개로(그건 태스크 전체가 죽었을 때만
반응하고 폴링 주기가 김), 이 supervisor는 프로세스 내에서 즉시 재시작 여부를 판단해
크래시 후 수 초 안에 재기동한다. 두 메커니즘은 상호 보완적이며 둘 다 등록해도 된다
(`ops/register_task_scheduler.ps1` 참고).

정지 방법: `ops/state/STOP` 파일을 만들면 **실행 중인 자식도** `stop_poll_s`(기본 2초) 안에
감지해 종료 신호를 보낸다(자식이 죽기를 무한정 기다리지 않는다 — 최초 구현은 자식 종료 후에만
STOP을 확인해 "실행 중에는 절대 안 멈추는" 결함이 있었다, 라이브 리허설 중 발견·수정).
콘솔에서는 Ctrl+C(SIGINT)도 동작한다. Windows에서는 `Popen.terminate()`가 `TerminateProcess`로
매핑돼 **강제 종료**다(진짜 SIGTERM 없음) — collector 쪽의 정상 종료 로직(상태 저장·토큰 반납)이
전부 돌지 못할 수 있다. DB는 WAL이라 안전하고(계약 C-6) OS 파일락도 프로세스 종료 시 자동
반납되지만, 마지막 수십 초의 카운터 갱신은 유실될 수 있다 — docs/08 §5·§10 참고.

재시작 폭주 방지: `restart_window_s` 안에 `max_restarts_per_window` 회를 넘겨 죽으면
"고장"으로 간주하고 재시작을 멈춘다 — 무한 재시작이 rate limit/디스크를 반복 두들기는
사고를 막기 위함(계약 C-8 budget.py 의 취지와 동일하게 "초과는 사고"로 취급).
"""
from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .opsconfig import OpsConfig, load_ops_config


def backoff_delay(attempt: int, base_s: float, cap_s: float) -> float:
    """attempt(0부터) 회 연속 실패 후 대기할 시간. 지수 증가 + 상한."""
    if attempt <= 0:
        return 0.0
    return min(base_s * (2 ** (attempt - 1)), cap_s)


@dataclass
class RestartBudget:
    """롤링 윈도 안의 재시작 횟수를 추적해 폭주를 감지한다."""

    max_restarts: int
    window_s: float
    _events: list[float] = field(default_factory=list)

    def record(self, now: float) -> None:
        self._events.append(now)
        cutoff = now - self.window_s
        self._events = [t for t in self._events if t >= cutoff]

    def over_limit(self, now: float) -> bool:
        cutoff = now - self.window_s
        recent = [t for t in self._events if t >= cutoff]
        return len(recent) > self.max_restarts


def stop_requested(stop_file: Path) -> bool:
    return stop_file.exists()


class Supervisor:
    """테스트 용이성을 위해 subprocess 생성/대기를 얇게 래핑한다."""

    def __init__(self, cfg: OpsConfig, stop_file: Path, log_path: Path | None = None,
                 sleep_fn=time.sleep, now_fn=time.monotonic, stop_poll_s: float = 2.0):
        self.cfg = cfg
        self.stop_file = stop_file
        self.log_path = log_path
        self._sleep = sleep_fn
        self._now = now_fn
        self.stop_poll_s = stop_poll_s
        self.budget = RestartBudget(cfg.max_restarts_per_window, cfg.restart_window_s)
        self._child: subprocess.Popen | None = None
        self._should_exit = False

    def _spawn(self) -> subprocess.Popen:
        log_fh = None
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            log_fh = open(self.log_path, "ab", buffering=0)
        return subprocess.Popen(
            self.cfg.collector_cmd,
            stdout=log_fh or subprocess.DEVNULL,
            stderr=subprocess.STDOUT if log_fh else subprocess.DEVNULL,
        )

    def _handle_signal(self, signum, frame) -> None:
        self._should_exit = True
        if self._child is not None and self._child.poll() is None:
            self._child.terminate()

    def _wait_child(self) -> int:
        """자식이 끝나거나 STOP 파일이 나타날 때까지 짧게 폴링한다(무한정 block하지 않음).

        기존 구현은 `self._child.wait()`(무한 대기)를 썼는데, 그러면 자식이 살아있는 동안은
        STOP 파일을 절대 확인하지 못해 "실행 중에는 정지 요청이 통하지 않는" 결함이 있었다
        (라이브 리허설 중 발견 — docs/08 §5 참고).
        """
        assert self._child is not None
        while True:
            try:
                return self._child.wait(timeout=self.stop_poll_s)
            except subprocess.TimeoutExpired:
                if self._should_exit:
                    self._child.terminate()
                    return self._child.wait()
                if stop_requested(self.stop_file):
                    print("[supervisor] stop file detected while running — "
                          "terminating collector", file=sys.stderr)
                    self._child.terminate()
                    return self._child.wait()

    def run(self, max_iterations: int | None = None) -> int:
        """메인 루프. max_iterations는 테스트 전용(무한루프 방지), 실사용은 None."""
        try:
            signal.signal(signal.SIGINT, self._handle_signal)
        except (ValueError, OSError):
            pass  # 메인 스레드가 아니거나 플랫폼 미지원이면 시그널 훅 생략

        attempt = 0
        iterations = 0
        while not self._should_exit:
            if stop_requested(self.stop_file):
                print("[supervisor] stop file detected — exiting", file=sys.stderr)
                return 0

            self._child = self._spawn()
            code = self._wait_child()
            iterations += 1
            now = self._now()

            if stop_requested(self.stop_file):
                print("[supervisor] stop file detected after child exit — exiting",
                      file=sys.stderr)
                return 0

            if self._should_exit:
                return 0
            if code == 0:
                print("[supervisor] collector exited cleanly (0) — not restarting",
                      file=sys.stderr)
                return 0

            self.budget.record(now)
            if self.budget.over_limit(now):
                print(f"[supervisor] restart budget exceeded "
                      f"({self.cfg.max_restarts_per_window}/{self.cfg.restart_window_s}s) — "
                      "giving up, manual intervention required", file=sys.stderr)
                return 1

            attempt += 1
            delay = backoff_delay(attempt, self.cfg.restart_backoff_base_s,
                                   self.cfg.restart_backoff_cap_s)
            print(f"[supervisor] collector exited (code={code}), attempt={attempt}, "
                  f"backoff={delay:.1f}s", file=sys.stderr)
            if delay > 0:
                self._sleep(delay)

            if max_iterations is not None and iterations >= max_iterations:
                return 1
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=None)
    args = ap.parse_args(argv)

    # Windows 콘솔 기본 cp949 로는 이 모듈의 안내 문구(em-dash 등)를 못 찍어 UnicodeEncodeError로
    # 죽는다 — 그러면 STOP 감지 print()가 터지면서 뒤따르는 terminate() 호출 자체가 실행되지 않아
    # 자식(collector)이 고아 프로세스로 남는다(라이브 리허설 중 실제로 겪을 뻔했다 — docs/11 §5).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    cfg = load_ops_config(args.config)
    stop_file = cfg.state_dir / "STOP"
    log_path = cfg.log_dir / "collector.stdout.log"
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    sup = Supervisor(cfg, stop_file, log_path)
    return sup.run()


if __name__ == "__main__":
    raise SystemExit(main())

"""백필 자가 복구 슈퍼바이저 — 소유: W4. (사용자 지시: "중단돼도 개입 없이 빠르게 되살아나게")

왜 존재하는가: 2026-08-01 라이브 백필이 두 번 죽었고(403 ip-not-allowed IP 플랩,
외부 프로세스 킬) 둘 다 사람이 몇 시간 뒤에 발견했다. 둘 다 기계적으로 복구 가능한
유형이었다 — 체크포인트는 멱등이고 IP 는 저절로 돌아왔다. 이 슈퍼바이저는 그 복구를
구조로 만든다:

    러너(tools/backfill.py) 기동 -> 종료 감시 ->
      exit 0 + 신선한 매니페스트          -> DONE, 종료
      403 ip-not-allowed (exit 3)         -> 10분 간격 값싼 프로브(캘린더 1콜), 상한 없음
                                             — 성공하는 순간 체크포인트에서 자동 재개.
                                             매 시간 WAIT-403 로 대기 사실을 로그에 명시.
      기타 크래시(네트워크·5xx·외부 킬)   -> 1/5/15분 백오프 재기동
      같은 체크포인트 지문에서 3회 연속    -> 재시도 중단, 로그에 "ESCALATION:" 줄
      크래시                                 (코디네이터 감시가 grep), 오케스트레이션
                                             escalation 도 시도, exit 1.

정직한 텔레메트리: 30분마다 체크포인트 실측 카운트와 **러너 생존 여부**를 함께 찍는다.
러너가 살아 있으면 `STATUS ... runner=alive pid=N ...`, 죽어 있으면 `RUNNER-DEAD`
(사유·다음 시도 시각 포함). 죽은 숫자를 산 것처럼 찍는 일이 구조적으로 불가능하다 —
카운트는 체크포인트에서 읽지만 생존 판정은 자식 프로세스 핸들에서 직접 한다.

우아한 정지: 첫 로그 줄에 명시된 STOP 파일을 만들면 러너에 `--stop-file` 채널로 정상
정지를 전달하고, 체크포인트가 파싱되는지 확인(CHECKPOINT-FLUSH)한 뒤 exit 0 으로
끝난다. `--deadline "YYYY-MM-DD HH:MM[:SS]"` (로컬 시각)은 같은 일을 정해진 시각에
한다 — 분할 실행·계획된 네트워크 컷 대응.

내구 기동 (PTY 리퍼·콘솔 처닝과 무관하게 생존):
    set TOSS_LIVE=1
    set TOSS_BASE_URL=https://openapi.tossinvest.com
    .venv\\Scripts\\python.exe -u tools\\backfill_supervisor.py --register [러너 인자들]
schtasks(현재 사용자)로 등록 + 즉시 기동한다. 등록 시점에 TOSS_LIVE/TOSS_BASE_URL 이
env 에 없으면 거부한다 (env 누락이 2026-07-31 2시간 사고의 원인이었다).
정지: STOP 파일(우아). 강제: schtasks /End /TN toss-backfill-supervisor.
해제: schtasks /Delete /TN toss-backfill-supervisor /F. 재기동: schtasks /Run /TN ....

검증은 mock 전용이다 — tests/test_backfill_supervisor.py 가 러너·프로브·에스컬레이션을
가짜 스크립트로 치환해 전 시나리오를 돌린다. 라이브 경합 없음.

콘솔·로그 출력은 ASCII 전용 (Windows cp949 안전).
종료코드: 0 완료/정지, 1 에스컬레이션, 2 설정 오류.
--probe-once 모드: 0 성공, 3 forbidden, 1 기타 오류.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
THIS_FILE = Path(__file__).resolve()

DEFAULT_TASK_NAME = "toss-backfill-supervisor"
#: 종료코드 분류 계약 (tools/backfill.py 와 맞물림)
RC_DONE = 0
RC_REFUSED = 2
RC_FORBIDDEN = 3
RC_STOPPED = 4


def _load_backfill():
    """tools/backfill.py 를 모듈로 로드 (tools/ 는 패키지가 아니다 — 테스트와 같은 관례)."""
    import importlib.util

    if "tossmon_backfill" in sys.modules:
        return sys.modules["tossmon_backfill"]
    spec = importlib.util.spec_from_file_location(
        "tossmon_backfill", REPO_ROOT / "tools" / "backfill.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["tossmon_backfill"] = module
    spec.loader.exec_module(module)
    return module


def parse_deadline(raw: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise SystemExit(f'bad --deadline {raw!r} (want "YYYY-MM-DD HH:MM[:SS]", local time)')


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class SupConfig:
    runner_cmd: list[str]
    probe_cmd: list[str]
    escalate_cmd: list[str]
    run_dir: Path
    runner_log: Path
    sup_log: Path
    stop_file: Path
    runner_stop_file: Path
    checkpoint: Path
    manifest: Path
    deadline: datetime | None = None
    status_interval_s: float = 1800.0
    probe_interval_s: float = 600.0
    poll_s: float = 2.0
    backoff_s: tuple[float, ...] = (60.0, 300.0, 900.0)
    stop_grace_s: float = 120.0
    probe_timeout_s: float = 90.0
    max_same_point: int = 3
    escalate_timeout_s: float = 60.0


@dataclass
class Supervisor:
    cfg: SupConfig
    _child: subprocess.Popen | None = None
    _next_status: float = field(default=0.0)
    _log_offset: int = 0

    # ---- 로그 (파일 + 콘솔, ASCII 전용) ---------------------------------
    def log(self, line: str) -> None:
        text = f"{_ts()} {line}"
        data = text.encode("ascii", "backslashreplace") + b"\n"
        self.cfg.sup_log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.cfg.sup_log, "ab") as fh:
            fh.write(data)
        try:
            print(data.decode("ascii"), end="", flush=True)
        except Exception:
            pass                                   # 콘솔이 닫혀도 파일 로그는 남는다

    # ---- 체크포인트 실측 -------------------------------------------------
    def _counts(self) -> str:
        try:
            data = json.loads(self.cfg.checkpoint.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "ckpt=absent"
        wins = list(data.get("windows", {}).values())
        c = Counter(w.get("status") for w in wins)
        return ("screen=%d done=%d partial=%d todo=%d bars=%d calls=%d" % (
            len(data.get("screen", {})), c.get("done", 0), c.get("partial", 0),
            c.get("todo", 0), sum(int(w.get("bars") or 0) for w in wins),
            sum(int(w.get("calls") or 0) for w in wins)))

    def _fingerprint(self) -> str:
        try:
            return hashlib.sha256(self.cfg.checkpoint.read_bytes()).hexdigest()
        except OSError:
            return "absent"

    def _verify_flush(self) -> None:
        """정지 후 체크포인트가 실제로 파싱되는지 — 플러시 확인을 로그로 증명."""
        try:
            json.loads(self.cfg.checkpoint.read_text(encoding="utf-8"))
            self.log(f"CHECKPOINT-FLUSH ok=1 {self._counts()}")
        except (OSError, ValueError) as exc:
            self.log(f"CHECKPOINT-FLUSH ok=0 error={type(exc).__name__}")

    # ---- 정지 판정 -------------------------------------------------------
    def _stop_reason(self) -> str | None:
        if self.cfg.stop_file.exists():
            return "stop-file"
        if self.cfg.deadline is not None and datetime.now() >= self.cfg.deadline:
            return "deadline"
        return None

    # ---- STATUS / RUNNER-DEAD (정직한 텔레메트리) ------------------------
    def _maybe_status(self, alive_pid: int | None, reason: str = "",
                      next_retry: str = "") -> None:
        now = time.monotonic()
        if now < self._next_status:
            return
        self._next_status = now + self.cfg.status_interval_s
        counts = self._counts()
        if alive_pid is not None:
            self.log(f"STATUS runner=alive pid={alive_pid} {counts}")
        else:
            self.log(f"RUNNER-DEAD reason={reason} next_retry={next_retry} {counts}")

    # ---- 자식 러너 -------------------------------------------------------
    def _spawn(self) -> subprocess.Popen:
        self.cfg.runner_log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.cfg.runner_log, "ab") as fh:
            return subprocess.Popen(
                self.cfg.runner_cmd, cwd=str(REPO_ROOT), stdout=fh,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)

    def _stop_child(self, src: str) -> None:
        """정상 정지 전달: 러너 stop 파일 -> 유예 대기 -> (불응 시) 강제 종료."""
        assert self._child is not None
        self.log(f"STOP-REQUEST source={src} relaying via {self.cfg.runner_stop_file}")
        try:
            self.cfg.runner_stop_file.write_text("stop", encoding="ascii")
        except OSError as exc:
            self.log(f"STOP-RELAY-ERROR {type(exc).__name__}: {exc}")
        try:
            code = self._child.wait(timeout=self.cfg.stop_grace_s)
            self.log(f"RUNNER-EXIT code={code} class=stopped")
        except subprocess.TimeoutExpired:
            self.log(f"STOP-FORCE runner ignored stop file for "
                     f"{self.cfg.stop_grace_s:g}s - terminating")
            self._child.terminate()
            code = self._child.wait(timeout=30)
            self.log(f"RUNNER-EXIT code={code} class=stopped-forced")
        self._child = None

    def _run_child_once(self) -> tuple[str, object, float]:
        """1회 기동+감시. ('exit', code, started_wall) 또는 ('stopped', src, started_wall)."""
        try:
            self.cfg.runner_stop_file.unlink()          # 이전 정지 요청 잔재 제거
        except OSError:
            pass
        started_wall = time.time()
        self._log_offset = (self.cfg.runner_log.stat().st_size
                            if self.cfg.runner_log.exists() else 0)
        try:
            self._child = self._spawn()
        except OSError as exc:
            self.log(f"SPAWN-ERROR {type(exc).__name__}: {exc}")
            return ("exit", None, started_wall)          # None code -> crash 분류
        self.log(f"RUNNER-START pid={self._child.pid}")
        while True:
            try:
                code = self._child.wait(timeout=self.cfg.poll_s)
                self._child = None
                return ("exit", code, started_wall)
            except subprocess.TimeoutExpired:
                pass
            src = self._stop_reason()
            if src:
                self._stop_child(src)
                return ("stopped", src, started_wall)
            self._maybe_status(alive_pid=self._child.pid)

    # ---- 종료 분류 -------------------------------------------------------
    def _log_tail_since_spawn(self) -> str:
        """이번 기동 이후 러너 로그만 읽는다 — 이전 런의 FATAL 줄로 오분류하지 않기 위해."""
        try:
            with open(self.cfg.runner_log, "rb") as fh:
                fh.seek(self._log_offset)
                return fh.read(65536).decode("utf-8", "replace")
        except OSError:
            return ""

    def _classify(self, code: int | None, started_wall: float) -> str:
        if code == RC_DONE:
            try:
                fresh = self.cfg.manifest.stat().st_mtime >= started_wall - 5
            except OSError:
                fresh = False
            if fresh:
                return "done"
            self.log("EXIT-0-WITHOUT-MANIFEST treating as crash (dishonest exit)")
            return "crash"
        if code == RC_STOPPED:
            return "stopped-external"                    # 외부 주체가 러너 stop 파일 생성
        if code == RC_REFUSED:
            return "refused"
        if code == RC_FORBIDDEN:
            tail = self._log_tail_since_spawn()
            if "ForbiddenEndpoint" in tail:
                return "endpoint-violation"
            return "ip-blocked"
        return "crash"

    # ---- 프로브 (403 대기) ----------------------------------------------
    def _probe(self) -> str:
        try:
            res = subprocess.run(self.cfg.probe_cmd, cwd=str(REPO_ROOT),
                                 capture_output=True, text=True,
                                 timeout=self.cfg.probe_timeout_s)
        except subprocess.TimeoutExpired:
            return "error(timeout)"
        except OSError as exc:
            return f"error({type(exc).__name__})"
        if res.returncode == 0:
            return "ok"
        if res.returncode == RC_FORBIDDEN:
            return "forbidden"
        return f"error(rc={res.returncode})"

    def _probe_wait(self) -> tuple[str, object]:
        """403 대기 루프. ('ok', attempts) 또는 ('stopped', src). 상한 없음 (사용자가
        포털 등록을 마치면 다음 프로브에서 저절로 살아난다)."""
        iv = self.cfg.probe_interval_s
        self.log(f"WAIT-403 probing every {iv:g}s until IP is allowed again "
                 "(no cap - portal re-registration revives the run automatically)")
        wait_start = time.monotonic()
        next_probe = time.monotonic()
        next_hourly = time.monotonic() + 3600.0
        attempts = 0
        while True:
            src = self._stop_reason()
            if src:
                return ("stopped", src)
            now = time.monotonic()
            if now >= next_probe:
                attempts += 1
                res = self._probe()
                if res == "ok":
                    self.log(f"PROBE ok attempts={attempts} - resuming from checkpoint")
                    return ("ok", attempts)
                next_probe = now + iv
                self.log(f"PROBE {res} attempts={attempts} next_in_s={iv:g}")
            if now >= next_hourly:
                self.log(f"WAIT-403 waited_min={int((now - wait_start) / 60)} "
                         f"still blocked, probing every {iv:g}s")
                next_hourly = now + 3600.0
            nxt = max(0.0, next_probe - time.monotonic())
            self._maybe_status(alive_pid=None, reason="ip-blocked",
                               next_retry=f"probe+{int(nxt)}s")
            time.sleep(min(self.cfg.poll_s, max(0.05, nxt or self.cfg.poll_s)))

    def _sleep_watch(self, delay: float, reason: str) -> str | None:
        """백오프 대기. 정지 요청이 오면 그 사유를 돌려준다 (None = 대기 완료)."""
        end = time.monotonic() + delay
        while time.monotonic() < end:
            src = self._stop_reason()
            if src:
                return src
            self._maybe_status(alive_pid=None, reason=reason,
                               next_retry=f"restart+{int(end - time.monotonic())}s")
            time.sleep(min(self.cfg.poll_s, max(0.05, end - time.monotonic())))
        return None

    # ---- 에스컬레이션 ----------------------------------------------------
    def _escalate_and_exit(self, why: str) -> int:
        subject = "W4 backfill supervisor: manual intervention needed"
        body = (f"{why}; checkpoint {self._counts()}; supervisor log "
                f"{self.cfg.sup_log}; runner log {self.cfg.runner_log}")
        self.log(f"ESCALATION: {why} - retries stopped, exiting 1")
        cmd = [a.replace("{subject}", subject).replace("{body}", body)
               for a in self.cfg.escalate_cmd]
        try:
            res = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True,
                                 text=True, timeout=self.cfg.escalate_timeout_s)
            self.log(f"ESCALATION-SENT rc={res.returncode}")
        except Exception as exc:                       # noqa: BLE001 — best effort:
            # 오케스트레이션 채널이 없어도 ESCALATION: 로그 줄이 1차 채널이다.
            self.log(f"ESCALATION-SEND-FAILED {type(exc).__name__}: {exc}")
        return 1

    # ---- 메인 루프 -------------------------------------------------------
    def run(self) -> int:
        self.log(f"SUPERVISOR-START pid={os.getpid()} stop_file={self.cfg.stop_file} "
                 "(create this file for graceful stop)")
        self.log(f"CONFIG runner_log={self.cfg.runner_log} "
                 f"checkpoint={self.cfg.checkpoint} "
                 f"probe_interval_s={self.cfg.probe_interval_s:g} "
                 f"status_interval_s={self.cfg.status_interval_s:g} "
                 f"backoff_s={','.join('%g' % b for b in self.cfg.backoff_s)} "
                 f"deadline={self.cfg.deadline or 'none'}")
        self._next_status = time.monotonic() + self.cfg.status_interval_s
        strikes = 0
        last_crash_fp: str | None = None
        while True:
            src = self._stop_reason()
            if src:
                self.log(f"STOP source={src} (no runner active) - exiting 0")
                self._verify_flush()
                return 0
            kind, val, started = self._run_child_once()
            if kind == "stopped":
                self._verify_flush()
                self.log(f"STOP source={val} - exiting 0")
                return 0
            code = val
            cls = self._classify(code, started)
            self.log(f"RUNNER-EXIT code={code} class={cls}")
            if cls == "done":
                self.log(f"DONE manifest={self.cfg.manifest} plan exhausted - exiting 0")
                return 0
            if cls == "stopped-external":
                self._verify_flush()
                self.log("STOP source=runner-stop-file(external) - exiting 0")
                return 0
            if cls in ("refused", "endpoint-violation"):
                return self._escalate_and_exit(
                    f"non-retryable runner exit class={cls} code={code}")
            if cls == "ip-blocked":
                res, extra = self._probe_wait()      # extra: attempts 또는 stop 사유
                if res == "stopped":
                    self._verify_flush()
                    self.log(f"STOP source={extra} during 403 wait - exiting 0")
                    return 0
                attempts = int(extra)
                if attempts > 1:
                    # 진짜 차단이었다 — 외부 원인이므로 크래시 연속 카운트를 리셋.
                    strikes = 0
                    last_crash_fp = None
                    continue
                # 프로브가 즉시 성공 = IP 는 막혀 있지 않았다. 403 크래시 루프
                # (예: 미분류 ForbiddenEndpoint)일 수 있으니 same-point 규칙을 적용.
                self.log("PROBE-IMMEDIATE-OK ip was not blocked - counting as crash "
                         "for same-point rule")
                cls = "crash"
            # cls == "crash"
            fp = self._fingerprint()
            if fp == last_crash_fp:
                strikes += 1
            else:
                strikes = 1
                last_crash_fp = fp
            if strikes >= self.cfg.max_same_point:
                return self._escalate_and_exit(
                    f"{strikes} consecutive crashes at same checkpoint fingerprint "
                    f"{fp[:12]} last_code={code}")
            delay = self.cfg.backoff_s[min(strikes - 1, len(self.cfg.backoff_s) - 1)]
            self.log(f"BACKOFF delay_s={delay:g} strike={strikes} fingerprint={fp[:12]}")
            src = self._sleep_watch(delay, reason=f"crash(code={code})")
            if src:
                self._verify_flush()
                self.log(f"STOP source={src} during backoff - exiting 0")
                return 0

    def emergency_stop(self) -> None:
        """KeyboardInterrupt 등 — 자식이 살아 있으면 정상 정지 시도 후 강제 종료."""
        if self._child is not None and self._child.poll() is None:
            try:
                self._stop_child("interrupt")
            except Exception:                           # noqa: BLE001 — 마지막 방어선
                try:
                    self._child.terminate()
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# --probe-once: 값싼 생존 프로브 (캘린더 1콜) — 별도 프로세스로 돌아 리스·이벤트루프를
# 슈퍼바이저 본체와 격리한다. 러너가 죽어 있을 때만 호출되므로 리스 경합이 없다.
# ---------------------------------------------------------------------------
def probe_once(config_path: str) -> int:
    import asyncio

    bf = _load_backfill()
    from tossmon.api.errors import Forbidden, TossApiError  # noqa: E402

    async def go() -> None:
        cfg = bf.load_config(config_path)
        client = bf.build_client(cfg)
        try:
            await client.get_us_calendar()
        finally:
            try:
                client.tokens.release()
            except Exception:                           # noqa: BLE001
                pass
            await client.aclose()

    try:
        asyncio.run(go())
        print("probe ok: calendar reachable")
        return 0
    except Forbidden as exc:
        print(f"probe forbidden: {exc}")
        return 3
    except (TossApiError, RuntimeError, OSError) as exc:
        # RuntimeError: token lease already held (러너 잔존?) — 일시 오류로 취급.
        print(f"probe error: {type(exc).__name__}: {exc}")
        return 1


# ---------------------------------------------------------------------------
# --register: schtasks(현재 사용자) 등록 + 즉시 기동
# ---------------------------------------------------------------------------
def _exec(cmd: list[str]) -> tuple[int, str]:
    """schtasks 호출 래퍼 — 테스트가 monkeypatch 한다."""
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return res.returncode, (res.stdout or "") + (res.stderr or "")


def register(args: argparse.Namespace, run_dir: Path) -> int:
    for var in ("TOSS_LIVE", "TOSS_BASE_URL"):
        if not os.environ.get(var):
            print(f"REFUSED: {var} not set - the generated launcher must embed live "
                  "env (a missing env caused the 2026-07-31 2h outage)")
            return 2
    run_dir.mkdir(parents=True, exist_ok=True)
    sup_args = ["--config", args.config, "--db", args.db, "--out", args.out]
    if args.date_from:
        sup_args += ["--from", args.date_from]
    if args.date_to:
        sup_args += ["--to", args.date_to]
    if args.deadline:
        sup_args += ["--deadline", args.deadline]
    launcher = run_dir / "run_supervisor.cmd"
    console_log = run_dir / "supervisor.console.log"
    launcher.write_text(
        "@echo off\r\n"
        "rem auto-generated by tools/backfill_supervisor.py --register\r\n"
        f"cd /d {REPO_ROOT}\r\n"
        f"set TOSS_LIVE={os.environ['TOSS_LIVE']}\r\n"
        f"set TOSS_BASE_URL={os.environ['TOSS_BASE_URL']}\r\n"
        f"\"{sys.executable}\" -u \"{THIS_FILE}\" "
        + " ".join(f'"{a}"' if " " in a else a for a in sup_args)
        + f" >> \"{console_log}\" 2>&1\r\n",
        encoding="ascii")
    print(f"launcher written: {launcher}")
    rc, out = _exec(["schtasks", "/Create", "/TN", args.task_name,
                     "/TR", f'"{launcher}"', "/SC", "ONCE", "/ST", "00:00", "/F"])
    print(f"schtasks /Create rc={rc}: {out.strip()[:300]}")
    if rc != 0:
        return 2
    rc, out = _exec(["schtasks", "/Run", "/TN", args.task_name])
    print(f"schtasks /Run rc={rc}: {out.strip()[:300]}")
    if rc != 0:
        return 2
    print(f"registered and started. graceful stop: create "
          f"{run_dir / 'SUPERVISOR.STOP'}")
    print(f"force stop: schtasks /End /TN {args.task_name}   "
          f"delete: schtasks /Delete /TN {args.task_name} /F   "
          f"restart: schtasks /Run /TN {args.task_name}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_config(args: argparse.Namespace) -> SupConfig:
    run_dir = Path(args.out)
    runner_stop = run_dir / "RUNNER.STOP"
    if args.runner_cmd:
        runner_cmd = list(args.runner_cmd)
    else:
        runner_cmd = [sys.executable, "-u", str(REPO_ROOT / "tools" / "backfill.py"),
                      "--config", args.config, "--allow-live",
                      "--db", args.db, "--out", args.out]
        if args.date_from:
            runner_cmd += ["--from", args.date_from]
        if args.date_to:
            runner_cmd += ["--to", args.date_to]
    runner_cmd += ["--stop-file", str(runner_stop)]
    probe_cmd = (list(args.probe_cmd) if args.probe_cmd else
                 [sys.executable, str(THIS_FILE), "--probe-once",
                  "--config", args.config])
    escalate_cmd = (list(args.escalate_cmd) if args.escalate_cmd else
                    ["orca", "orchestration", "send", "--type", "escalation",
                     "--subject", "{subject}", "--body", "{body}", "--json"])
    backoff = tuple(float(x) for x in args.backoff_s.split(",") if x.strip())
    return SupConfig(
        runner_cmd=runner_cmd, probe_cmd=probe_cmd, escalate_cmd=escalate_cmd,
        run_dir=run_dir, runner_log=run_dir / "run.log",
        sup_log=run_dir / "supervisor.log",
        stop_file=run_dir / "SUPERVISOR.STOP", runner_stop_file=runner_stop,
        checkpoint=run_dir / "checkpoint.json",
        manifest=run_dir / "backfill_manifest.json",
        deadline=parse_deadline(args.deadline) if args.deadline else None,
        status_interval_s=args.status_interval_s,
        probe_interval_s=args.probe_interval_s, poll_s=args.poll_s,
        backoff_s=backoff or (60.0, 300.0, 900.0), stop_grace_s=args.stop_grace_s,
        probe_timeout_s=args.probe_timeout_s, max_same_point=args.max_same_point)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="self-healing supervisor for tools/backfill.py "
                    "(403 probe-wait resume, backoff restart, honest STATUS, "
                    "STOP file / --deadline graceful stop, schtasks --register)")
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--db", default="data/backfill.db")
    ap.add_argument("--out", default="data/backfill",
                    help="run dir (checkpoint/manifest/run.log/supervisor.log/STOP)")
    ap.add_argument("--from", dest="date_from", default=None)
    ap.add_argument("--to", dest="date_to", default=None)
    ap.add_argument("--deadline", default=None,
                    help='self-stop at local "YYYY-MM-DD HH:MM[:SS]" '
                         "(split runs / planned network cuts)")
    ap.add_argument("--runner-cmd", action="append", default=None, metavar="ARG",
                    help="override runner argv element-by-element (repeat; test "
                         "hook). --stop-file <path> is always appended")
    ap.add_argument("--probe-cmd", action="append", default=None, metavar="ARG",
                    help="override probe argv (repeat; rc 0=ok 3=forbidden)")
    ap.add_argument("--escalate-cmd", action="append", default=None, metavar="ARG",
                    help="override escalation argv; {subject}/{body} are substituted")
    ap.add_argument("--status-interval-s", type=float, default=1800.0)
    ap.add_argument("--probe-interval-s", type=float, default=600.0)
    ap.add_argument("--poll-s", type=float, default=2.0)
    ap.add_argument("--backoff-s", default="60,300,900")
    ap.add_argument("--stop-grace-s", type=float, default=120.0)
    ap.add_argument("--probe-timeout-s", type=float, default=90.0)
    ap.add_argument("--max-same-point", type=int, default=3)
    ap.add_argument("--task-name", default=DEFAULT_TASK_NAME)
    ap.add_argument("--register", action="store_true",
                    help="register + start via schtasks (current user) and exit")
    ap.add_argument("--probe-once", action="store_true",
                    help="one cheap calendar call; rc 0=ok 3=forbidden 1=error")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):        # cp949 콘솔 안전 (ASCII 만 쓰지만)
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    if args.probe_once:
        return probe_once(args.config)
    if args.register:
        return register(args, Path(args.out))

    cfg = build_config(args)
    # 기본(실제) 러너 명령일 때는 env 를 기동 시점에 검증한다 — env 누락은 러너가
    # REFUSED(2) 로 죽고 재시도해도 같으므로, 여기서 즉시 명확하게 실패한다.
    if not args.runner_cmd:
        missing = [v for v in ("TOSS_LIVE", "TOSS_BASE_URL")
                   if not os.environ.get(v)]
        if missing:
            print(f"REFUSED: env missing {','.join(missing)} - live runner would "
                  "refuse to start; fix the launcher env")
            return 2
    sup = Supervisor(cfg)
    try:
        return sup.run()
    except KeyboardInterrupt:
        sup.log("INTERRUPTED (SIGINT) - attempting graceful child stop")
        sup.emergency_stop()
        return 0


if __name__ == "__main__":
    sys.exit(main())

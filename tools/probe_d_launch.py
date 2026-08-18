"""W1 regular-session cadence probe launcher (Task Scheduler entry point).

Fires ``live_probe.py --probe ranking_cadence --cadence-profile d`` -- the "D"
arm set of docs/62 section 8-1 (1s x180, 5s x1200, 12s x1200; 529 calls; cap
550; about 43 minutes).

WHY THIS IS PYTHON AND NOT A .cmd
---------------------------------
The recipe this repo already had (``tools/coord_headless.cmd`` + a schtasks time
trigger) is a **console** process, and on this machine a scheduled console
process is killed by a console control event a few seconds after it starts.
Measured 2026-08-18:

    cmd.exe task, ping loop, heartbeat every 1s
        started 15:10:00.68, last heartbeat 15:10:11.34, LastTaskResult
        -1073741510 (0xC000013A STATUS_CONTROL_C_EXIT) -- dead in 10.7s
    pythonw.exe task, same heartbeat, NO console
        started 15:16:00.53, ran all 200 beats, DONE 15:19:39, result 0

A process with no console cannot receive CTRL_C_EVENT / CTRL_CLOSE_EVENT, so the
launcher runs under ``pythonw.exe`` and starts the probe itself with
``CREATE_NO_WINDOW`` so the child has no console either. The 44 minute live run
has to survive whatever is sending those events.

Started by schtasks ONLY. Do NOT run ``schtasks /run`` from an agent session:
console cleanup propagates Ctrl+C and kills the task (it happened twice in this
repo before today).

Usage:  pythonw.exe tools/probe_d_launch.py <live|smoke|mock>
    live            - the real probe. Live HTTP calls, token reused read-only.
                      Must be spelled out: the default is the harmless one, so
                      an accidental argument-less run cannot fire live calls.
    smoke (default) - launch check only. ZERO HTTP calls.
    mock            - the SAME profile-d probe end to end against a local mock
                      server. ZERO live calls. Proves a ~43 minute unattended
                      run survives, and that profile d (whose ``gain_arms`` is
                      empty -- a path that had never executed) does not crash.

ASCII only, and nothing goes to stdout: under pythonw ``sys.stdout`` is None and
``print`` would raise.
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(r"C:\Users\dongh\orca\workspaces\toss_trade\w1-core-api")
OPS = Path(r"C:\Users\dongh\orca\workspaces\toss_trade\w5-ops")
PY = REPO / ".venv" / "Scripts" / "python.exe"
TOKEN_STATE = OPS / "data" / "token_state.json"
COLLECTOR_LOG = OPS / "data" / "collector.log"
BASE_URL = "https://openapi.tossinvest.com"
MOCK_URL = "http://127.0.0.1:8899"

OUT = REPO / "out"
LOG = OUT / "probe_d_launch.log"

# No console for the child either -- that is the whole point (see module docstring).
CREATE_NO_WINDOW = 0x08000000


def stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def log(msg: str) -> None:
    OUT.mkdir(exist_ok=True)
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write("[probe_d] %s %s\n" % (ts, msg))


def preflight() -> str | None:
    """Every path this run depends on. Returns an error string, or None."""
    for label, path in (("venv python", PY),
                        ("live_probe.py", REPO / "tools" / "live_probe.py"),
                        ("collector token_state.json", TOKEN_STATE),
                        ("collector.log", COLLECTOR_LOG)):
        if not path.exists():
            return "FATAL %s not found at %s" % (label, path)
    return None


def write_window_marker(tag: str) -> Path:
    """docs/62 section 8-3 condition 1 -- mark the probe window in the data.

    The foreign-sender detector must never read this window as evidence of a
    third party: the third-party sender in it is us, and we know the exact send
    count. Without the marker somebody later reads the collector's RANKING
    frn/frnmax here as proof a third sender exists.
    """
    marker = REPO / "data" / ("PROBE_WINDOW_%s.txt" % tag)
    marker.parent.mkdir(exist_ok=True)
    marker.write_text(
        "probe=ranking_cadence profile=d\n"
        "planned_calls=529 call_cap=550 planned_duration_s=2595\n"
        "group=RANKING expected_own_rate_max=1.0/s\n"
        "start_local=%s\n"
        "note=docs/62 8-3: collector RANKING frn/frnmax in this window is this\n"
        "note=probe, not a third-party sender. Do NOT use for docs/55 B/C.\n"
        % dt.datetime.now().isoformat(timespec="seconds"),
        encoding="ascii")
    return marker


def run_child(args: list[str], env: dict) -> int:
    """Run a child with NO console, both streams appended to the launcher log."""
    OUT.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        proc = subprocess.Popen(args, cwd=str(REPO), env=env,
                                stdout=fh, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL,
                                creationflags=CREATE_NO_WINDOW)
        return proc.wait()


def main(argv: list[str]) -> int:
    # Default is "smoke", not "live": firing 529 live calls at the market must
    # require somebody to have typed the word.
    mode = (argv[1] if len(argv) > 1 else "smoke").lower()
    if mode not in ("live", "smoke", "mock"):
        log("FATAL unknown mode %r (expected live|smoke|mock)" % mode)
        return 2
    tag = stamp()
    log("=" * 68)
    log("start mode=%s stamp=%s pid=%d" % (mode, tag, os.getpid()))

    err = preflight()
    if err:
        log(err)
        return 9
    log("preflight OK - python, probe, token_state, collector.log present")

    env = dict(os.environ)
    probe = str(REPO / "tools" / "live_probe.py")

    if mode == "smoke":
        env["TOSS_BASE_URL"] = BASE_URL
        env["TOSS_LIVE"] = "1"
        log("SMOKE - launch check only, ZERO http calls")
        rc = run_child([str(PY), probe, "--list"], env)

    elif mode == "mock":
        env["TOSS_BASE_URL"] = MOCK_URL
        env["TOSS_LIVE"] = "0"
        log("MOCK - profile d against %s, ZERO live calls" % MOCK_URL)
        (OUT / "mock_fixtures").mkdir(exist_ok=True)
        server = subprocess.Popen(
            [str(PY), str(REPO / "tools" / "mock_server.py"), "--port", "8899"],
            cwd=str(REPO), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        log("mock server pid=%d" % server.pid)
        time.sleep(3.0)
        try:
            rc = run_child([
                str(PY), probe,
                "--probe", "ranking_cadence",
                "--cadence-profile", "d",
                "--base-url", MOCK_URL,
                "--collector-log", str(COLLECTOR_LOG),
                "--baseline-min", "180",
                "--state", str(OUT / "mock_token_state.json"),
                "--fixture-dir", str(OUT / "mock_fixtures"),
                "--out", str(OUT / ("probe_d_mock_%s.json" % tag)),
            ], env)
        finally:
            # Kill by handle only. Never by image name -- that would kill the
            # running collector, which is a live process on this machine.
            server.kill()
            log("mock server killed")

    else:
        env["TOSS_BASE_URL"] = BASE_URL
        env["TOSS_LIVE"] = "1"
        marker = write_window_marker(tag)
        log("window marker written: %s" % marker)
        log("LIVE - profile d against %s" % BASE_URL)
        # --reuse-token-state: this API allows ONE valid token per client, so
        # issuing a new one would kill the running collector's token. Read only.
        rc = run_child([
            str(PY), probe,
            "--probe", "ranking_cadence",
            "--cadence-profile", "d",
            "--reuse-token-state", str(TOKEN_STATE),
            "--collector-log", str(COLLECTOR_LOG),
            "--baseline-min", "180",
            "--out", str(OUT / ("probe_d_%s.json" % tag)),
        ], env)
        with marker.open("a", encoding="ascii") as fh:
            fh.write("end_local=%s\n" % dt.datetime.now().isoformat(timespec="seconds"))

    log("exit rc=%s" % rc)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))

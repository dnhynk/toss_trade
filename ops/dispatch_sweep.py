"""디스패치 훑기 — 완료 보고를 믿지 않고 **산출물과 대조한다**. 소유: W5.

배경은 `docs/38_dispatch_sweep.md`.

## 왜 이 도구가 필요한가

2026-08-07 하루에 **정반대 두 사고**가 났다.

| | `worker_done` | 실제 산출물 |
|---|---|---|
| `task_4c7447a2597a` (W5) | **안 옴** (한도 프롬프트에 걸려 멈춤) | **커밋까지 다 돼 있었다** |
| `task_6aa7a20dad66` (W4) | **왔다**, 12분 만에 | **커밋 안 돼 있었다** (워크트리에만) |

**보고와 산출물이 서로를 보증하지 않는다.** 보고가 없다고 일이 안 된 게 아니고,
보고가 왔다고 일이 남은 것도 아니다. 그리고 그날 그것을 안 이유는 사용자가 "잘 되고
있니"라고 물었기 때문이다 — **탐지가 순전히 운이었다.**

그래서 이 도구는 배선 가드(`docs/30` §3)와 같은 모양을 쓴다: **거부를 기본값으로.**
"보고가 왔으니 됐다"를 초록으로 넘기지 않고, 기계가 **커밋을 직접 확인한다**.
`coordination/COORDINATOR-STATE.md` 에 이미 "워커 보고를 믿지 말고 산출물을 직접
확인하라"가 있었지만 **사람 규율로만 남아 있어서 그날 새어나갔다.**

## 무엇을 대조하는가

세 축을 **각각 기계적 사실로** 잰다. 어느 축이든 못 재면 `unknown` 으로 **따로 센다** —
조용히 초록으로 넘기지 않는다(`docs/31` 의 "'결손 0건'이 아니라 '못 봤다'로 읽을 것").

| 축 | 소스 | 사실 |
|---|---|---|
| 하트비트 | `orca orchestration dispatch-show` 의 `last_heartbeat_at` | 마지막 생존 신호 나이 |
| 브랜치 | 워커 워크트리의 `git log` / `git status` | 디스패치 이후 커밋·수정이 있었는가 |
| 보고 매니페스트 | 태스크 `result` 의 `filesModified` | 적어낸 경로가 **실제로 커밋됐는가** |

워크트리 경로는 디스패치 레코드의 `process_incarnation` 에서 나온다 —
`<worktreeId>::<경로>@@<hash>:<uuid>`. 추측이 아니라 런타임이 적어둔 값이다.

## 오분류하지 않기 위해 갈라둔 것들

1. **하트비트 미발신 ≠ 하트비트 정지.** `last_heartbeat_at` 이 `None` 인 것은 죽음이
   아니다. 2026-08-07 의 W3 가 그랬다 — 일은 하고 있는데 하트비트를 한 번도 안 보냈다.
   `never` 로 따로 두고, 브랜치가 움직였으면 `WORKING_SILENT`, 아니면 **`UNKNOWN`**
   이다. `PRESUMED_DEAD` 로 보내지 않는다.
2. **막 디스패치된 워커.** 아직 첫 하트비트 주기가 안 지났으면 `warming` 이다.
   유예 없이 재면 방금 출발한 워커가 전부 빨개진다.
3. **직전 디스패치의 낙오 하트비트.** `last_heartbeat_at < dispatched_at` 이면 그
   하트비트는 이 디스패치의 것일 수 없다 — `never` 로 강등한다(preamble 이 경고하는
   "straggler heartbeat from a previously-failed dispatch cannot mask a hung retry").
4. **커밋과 미커밋 수정.** 둘 다 "살아 있다"의 증거지만 조치가 다르다. 커밋이 있으면
   보고만 유실된 것이고(사람이 게이트 돌리고 닫으면 된다), 미커밋만 있으면 **재디스패치가
   그 작업을 덮는다.** 그래서 `REPORT_LOST` 와 `STALE_UNCOMMITTED` 를 가른다.

## 판정만 한다

이 도구는 **아무 상태도 바꾸지 않는다.** `orca orchestration` 은 읽기 명령
(`task-list`, `dispatch-show`)만 쓰고, git 은 `log`/`status`/`rev-parse` 만 쓴다.
`task-update`·`worker-abandon`·`dispatch` 는 부르지 않는다 — 조치는 사람이 한다.
라이브 API 호출 없음.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

from .opsconfig import load_ops_config

UTC = dt.timezone.utc

# --------------------------------------------------------------------------- #
# 임계값 — 이 파일에서 문턱을 쓰는 곳은 둘뿐이고 둘 다 사유가 있다.
# --------------------------------------------------------------------------- #
#: 하트비트가 이보다 오래되면 `stale`. 근거: 워커 preamble 의 계약이 "5분마다 하트비트"
#: 이므로 **연속 3회 미발신**이다. 1회(5분)로 잡으면 긴 툴 호출 한 번에 멀쩡한 워커가
#: 빨개지고, 30분으로 잡으면 오늘처럼 30분 만에 멈춘 것을 못 잡는다.
DEFAULT_STALE_MIN = 15

#: 완료 보고가 온 태스크의 커밋 대조를 몇 시간 전까지 볼지. 근거: 이 검사가 노리는
#: 사고는 **머지되기 전 같은 날 안에서** 새는 것이다(오늘 W4). 머지가 끝난 뒤에는
#: 워크트리가 리셋·재사용되므로 워크트리 대조는 의미가 없어지고 main 이 기록이다.
DEFAULT_LOOKBACK_H = 24

#: 워크트리 "수정됨" 신호에서 뺄 경로. 근거: 이것들은 워커의 편집이 아니라 도구·수집기의
#: 산출물이라 **모든 워크트리를 항상 살아 있게 보이게 만든다** — 죽은 것을 살았다고 하는
#: 방향의 오류라서 가장 위험하다. 여기서 걸러진 경로는 출력에 노출된다(조용히 안 자른다).
#: 그리고 보고 매니페스트에 적힌 경로는 노이즈에 걸려도 **미커밋으로 센다** — 워커가
#: 스스로 적어낸 경로를 이 필터가 감추면 안 된다(`git_facts` 의 `dirty_set` 참조).
#: 어느 디렉터리 깊이에서든 이 이름의 세그먼트면 노이즈:
NOISE_SEGMENTS: tuple[str, ...] = (
    ".cache", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", "node_modules", ".orca",
)
#: 세그먼트 접미사로 판별하는 것 (`tossmon.egg-info` 처럼 이름이 프로젝트마다 다르다):
NOISE_SUFFIXES: tuple[str, ...] = (".egg-info",)
#: **리포지토리 루트 기준으로만** 노이즈인 것. `data/` 는 수집기 산출물이라 항상 움직이지만
#: `tests/data/` 는 워커가 만드는 픽스처일 수 있어서 세그먼트 일치로 잡으면 안 된다.
NOISE_ROOT_PREFIXES: tuple[str, ...] = ("data/",)

#: `git log` 를 몇 개까지 거슬러 볼지. 디스패치 이후 창에서 이보다 많은 커밋이 나오면
#: 잘렸을 수 있으므로 출력에 `+` 를 붙인다.
GIT_LOG_LIMIT = 200

_INCARNATION = re.compile(r"^[^:]*::(?P<path>.+?)@@[0-9a-fA-F]+:[0-9a-fA-F-]+$")
_SEP = "\x1f"   # git --format 구분자. 파일 경로에 절대 안 들어가는 문자.


# --------------------------------------------------------------------------- #
# 1. 판정 사전 — 코드 하나가 (사람이 할 일, 사람 필요 여부, 못 봤음 여부) 를 정한다.
#    `is_unknown` 은 "이 도구가 판정하지 못했다"는 뜻이지 "이상 없음"이 아니다.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class VerdictKind:
    code: str
    action: str
    needs_human: bool
    is_unknown: bool


VERDICTS: dict[str, VerdictKind] = {v.code: v for v in (
    # --- dispatched (보고가 아직 안 온 것) ---
    VerdictKind("WORKING", "건드리지 마라 — 하트비트 신선", False, False),
    VerdictKind("WORKING_SILENT",
                "건드리지 마라 — 하트비트는 없지만 브랜치가 디스패치 이후 움직였다",
                False, False),
    VerdictKind("REPORT_LOST",
                "일은 됐고 보고가 유실됐다 — 사람이 머지 게이트 돌리고 태스크 닫아라",
                True, False),
    VerdictKind("STALE_UNCOMMITTED",
                "작업 중 죽었고 결과가 미커밋이다 — 재디스패치하면 덮인다. 워크트리 먼저 봐라",
                True, False),
    VerdictKind("PRESUMED_DEAD",
                "도달 못 했거나 죽었다 — 재디스패치 후보",
                True, False),
    # --- 보고가 온 것 (오늘 W4 가 샌 지점) ---
    VerdictKind("DONE_COMMITTED", "없음 — 보고한 경로가 전부 커밋돼 있다", False, False),
    VerdictKind("DONE_UNCOMMITTED",
                "보고는 왔는데 경로가 워크트리에 미커밋이다 — 커밋시키기 전에 닫지 마라",
                True, False),
    VerdictKind("DONE_UNACCOUNTED",
                "보고한 경로에 디스패치 이후 변화가 없다 — 매니페스트가 사실과 다르다",
                True, False),
    VerdictKind("DONE_NO_MANIFEST",
                "보고에 filesModified 가 없어 커밋 여부를 못 잰다 — 손으로 확인하라",
                True, True),
    # --- 보고 없이 닫힌 것 (오늘 W5 가 샌 지점) ---
    VerdictKind("CLOSED_NO_REPORT_COMMITTED",
                "보고 없이 닫혔지만 브랜치는 움직였다 — 산출물은 있다. 게이트만 확인하라",
                True, False),
    VerdictKind("CLOSED_NO_REPORT_UNVERIFIED",
                "보고도 없고 브랜치도 안 움직였는데 닫혀 있다 — 근거 없는 마감이다",
                True, True),
    # --- 못 잰 것 ---
    VerdictKind("UNKNOWN", "판정 불가 — 아래 note 를 읽고 사람이 봐라", True, True),
)}


@dataclass(frozen=True)
class Verdict:
    code: str
    heartbeat_axis: str
    branch_axis: str
    note: str = ""

    @property
    def kind(self) -> VerdictKind:
        return VERDICTS[self.code]


# --------------------------------------------------------------------------- #
# 2. 사실 그릇 — 수집(shell)과 판정(순수 함수)을 갈라두기 위한 것.
#    테스트는 이 그릇을 손으로 채워서 판정만 검증한다.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DispatchFacts:
    """오케스트레이션 런타임이 말하는 것."""
    task_id: str
    title: str = ""
    task_status: str = ""
    dispatch_status: str = ""
    assignee: str = ""
    dispatched_at: dt.datetime | None = None
    last_heartbeat_at: dt.datetime | None = None
    worktree: Path | None = None
    capability_revoked_at: dt.datetime | None = None
    reported: bool = False                      # result.provenance == worker_report
    closed: bool = False                        # task_status 가 종결 상태
    manifest: tuple[str, ...] = ()              # result.filesModified
    manifest_present: bool = False
    probe_error: str | None = None              # dispatch-show 실패 사유


@dataclass(frozen=True)
class GitFacts:
    """워커 워크트리가 말하는 것. `ok=False` 면 이 축은 unknown 이다."""
    ok: bool = True
    error: str | None = None
    branch: str | None = None
    commits_since: int = 0
    commits_truncated: bool = False
    touched_paths: tuple[str, ...] = ()         # 미커밋 + 노이즈 제외
    ignored_noise: tuple[str, ...] = ()         # 노이즈로 걸러낸 경로 (노출용)
    committed_manifest: tuple[str, ...] = ()
    uncommitted_manifest: tuple[str, ...] = ()
    unaccounted_manifest: tuple[str, ...] = ()


@dataclass
class Row:
    facts: DispatchFacts
    git: GitFacts = field(default_factory=GitFacts)
    verdict: Verdict | None = None


# --------------------------------------------------------------------------- #
# 3. 파싱 (순수)
# --------------------------------------------------------------------------- #
def parse_ts(raw: object) -> dt.datetime | None:
    """오케스트레이션 시각을 UTC aware 로. naive 는 UTC 로 읽는다.

    런타임은 두 형태를 섞어 낸다: `2026-08-07 11:16:14`(naive) 와
    `2026-08-07T11:15:24Z`(aware). 둘이 같은 시계임은 실측으로 확인했다 —
    UTC 11:19 시점에 `dispatched_at=11:16:14` 였다(`docs/38` §2).
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip().replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)


def worktree_of(incarnation: object) -> Path | None:
    """`<worktreeId>::<경로>@@<hash>:<uuid>` 에서 워크트리 경로만. 못 읽으면 None."""
    if not isinstance(incarnation, str):
        return None
    m = _INCARNATION.match(incarnation.strip())
    return Path(m.group("path")) if m else None


def report_of(task: dict) -> tuple[bool, tuple[str, ...], bool]:
    """(워커 보고인가, filesModified, 매니페스트 키가 있었는가).

    `provenance` 가 `worker_report` 가 아닌 것(예: `manual_recovery`)은 **사람이 손으로
    닫은 것**이지 워커가 보고한 것이 아니다. 오늘 W5 의 `task_4c7447a2597a` 가
    `result=None` 으로 닫혀 있었다 — 보고는 끝내 안 왔다.
    """
    raw = task.get("result")
    if not isinstance(raw, str) or not raw.strip().startswith("{"):
        return False, (), False
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return False, (), False
    if not isinstance(data, dict) or data.get("provenance") != "worker_report":
        return False, (), False
    files = data.get("filesModified")
    if isinstance(files, list):
        return True, tuple(str(f) for f in files if str(f).strip()), True
    return True, (), False


def norm_path(path: str) -> str:
    """git 이 뱉는 경로를 리포 상대 슬래시 경로로. **문자 클래스 lstrip 을 쓰지 않는다** —
    `lstrip("./")` 는 `.cache/` 를 `cache/` 로 만들어 점으로 시작하는 경로를 전부 망친다."""
    p = path.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def is_noise(path: str) -> bool:
    """워커 편집이 아니라 도구·수집기 산출물인가. 사유는 `NOISE_SEGMENTS` 참조."""
    p = norm_path(path)
    if any(p == pre.rstrip("/") or p.startswith(pre) for pre in NOISE_ROOT_PREFIXES):
        return True
    segs = [s for s in p.split("/") if s]
    return any(s in NOISE_SEGMENTS or s.endswith(NOISE_SUFFIXES) for s in segs)


# --------------------------------------------------------------------------- #
# 4. 수집 — 읽기 명령만. 상태를 바꾸는 orca 하위명령은 이 파일에 없다.
# --------------------------------------------------------------------------- #
def run_read_only(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=str(cwd) if cwd else None, timeout=90)
        return p.returncode, p.stdout or "", p.stderr or ""
    except (OSError, subprocess.SubprocessError) as e:  # noqa: BLE001 — 못 재면 unknown
        return -1, "", f"{type(e).__name__}: {e}"


def orca_json(args: list[str]) -> tuple[dict | None, str | None]:
    """`orca orchestration <args> --json`. (result, error)."""
    rc, out, err = run_read_only(["orca", "orchestration", *args, "--json"])
    if rc != 0 and not out.strip():
        return None, (err.strip() or f"orca exit {rc}")[:300]
    try:
        payload = json.loads(out)
    except ValueError:
        return None, f"orca 출력이 JSON 이 아니다: {out[:200]!r}"
    if not payload.get("ok"):
        return None, json.dumps(payload.get("error", {}), ensure_ascii=False)[:300]
    return payload.get("result") or {}, None


def collect_tasks(run_id: str) -> tuple[list[dict], str | None]:
    result, err = orca_json(["task-list", "--run", run_id, "--brief"])
    if err is not None:
        return [], err
    tasks = result.get("tasks")
    return (list(tasks) if isinstance(tasks, list) else []), None


def collect_dispatch(task_id: str) -> tuple[dict | None, str | None]:
    result, err = orca_json(["dispatch-show", "--task", task_id])
    if err is not None:
        return None, err
    d = result.get("dispatch")
    return (d if isinstance(d, dict) else None), (None if d else "디스패치 레코드 없음")


def facts_for(task: dict) -> DispatchFacts:
    """태스크 하나에 대해 런타임 사실을 모은다(디스패치 레코드 포함)."""
    task_id = str(task.get("id") or "")
    reported, manifest, has_manifest = report_of(task)
    base = DispatchFacts(
        task_id=task_id,
        title=str(task.get("task_title") or task.get("display_name") or "")[:64],
        task_status=str(task.get("status") or ""),
        reported=reported,
        closed=str(task.get("status") or "") in ("completed", "failed", "cancelled"),
        manifest=manifest,
        manifest_present=has_manifest,
    )
    d, err = collect_dispatch(task_id)
    if d is None:
        return replace(base, probe_error=err)
    return replace(
        base,
        dispatch_status=str(d.get("status") or ""),
        assignee=str(d.get("assignee_handle") or ""),
        dispatched_at=parse_ts(d.get("dispatched_at")),
        last_heartbeat_at=parse_ts(d.get("last_heartbeat_at")),
        capability_revoked_at=parse_ts(d.get("capability_revoked_at")),
        worktree=worktree_of(d.get("process_incarnation")),
    )


# --------------------------------------------------------------------------- #
# 5. git 축 — 워커 워크트리에서 읽기만 한다.
# --------------------------------------------------------------------------- #
def git_log_since(worktree: Path, since: dt.datetime) -> tuple[int, bool, set[str], str | None]:
    """(디스패치 이후 커밋 수, 잘렸는가, 그 커밋들이 건드린 경로, 오류)."""
    rc, out, err = run_read_only(
        ["git", "log", f"-n{GIT_LOG_LIMIT}", f"--format=%H{_SEP}%cI", "--name-only", "HEAD"],
        cwd=worktree)
    if rc != 0:
        return 0, False, set(), (err.strip() or f"git log exit {rc}")[:200]
    count, paths, cur_in_window, seen = 0, set(), False, 0
    for line in out.splitlines():
        if _SEP in line:
            seen += 1
            _, _, ts = line.partition(_SEP)
            when = parse_ts(ts)
            cur_in_window = when is not None and when >= since
            if cur_in_window:
                count += 1
        elif line.strip() and cur_in_window:
            paths.add(norm_path(line))
    return count, (seen >= GIT_LOG_LIMIT and count == seen), paths, None


def git_dirty(worktree: Path) -> tuple[tuple[str, ...], tuple[str, ...], str | None]:
    """(워커 편집으로 볼 미커밋 경로, 노이즈로 걸러낸 경로, 오류)."""
    # `-uall` 이어야 한다. 기본(`normal`)은 **새 디렉터리를 `docs/새폴더/` 한 줄로 접는다** —
    # 그러면 보고 매니페스트의 `docs/새폴더/파일.md` 가 어디에도 일치하지 않아 미커밋
    # 산출물이 조용히 통과한다. 이 도구가 존재하는 이유가 바로 그 누락이다.
    rc, out, err = run_read_only(["git", "status", "--porcelain", "--untracked-files=all"],
                                 cwd=worktree)
    if rc != 0:
        return (), (), (err.strip() or f"git status exit {rc}")[:200]
    dirty, noise = [], []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        path = norm_path(line[3:].strip().strip('"').split(" -> ")[-1])   # rename 은 도착지만
        (noise if is_noise(path) else dirty).append(path)
    return tuple(sorted(dirty)), tuple(sorted(noise)), None


def git_facts(worktree: Path | None, since: dt.datetime | None,
              manifest: tuple[str, ...] = ()) -> GitFacts:
    """워크트리 하나에 대한 git 축. 못 읽으면 `ok=False` — '안 움직임'이 아니다."""
    if worktree is None:
        return GitFacts(ok=False, error="워크트리 경로를 못 읽었다(process_incarnation)")
    if not (worktree / ".git").exists() and not worktree.exists():
        return GitFacts(ok=False, error=f"워크트리가 없다: {worktree}")
    if since is None:
        return GitFacts(ok=False, error="dispatched_at 을 못 읽어 기준 시각이 없다")

    rc, out, err = run_read_only(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree)
    if rc != 0:
        return GitFacts(ok=False, error=(err.strip() or "git rev-parse 실패")[:200])
    branch = out.strip() or None

    count, truncated, touched_by_commit, log_err = git_log_since(worktree, since)
    if log_err:
        return GitFacts(ok=False, error=log_err, branch=branch)
    dirty, noise, st_err = git_dirty(worktree)
    if st_err:
        return GitFacts(ok=False, error=st_err, branch=branch)

    committed, uncommitted, unaccounted = [], [], []
    dirty_set = set(dirty) | set(noise)
    for raw in manifest:
        p = norm_path(raw)
        # 디렉터리로 접힌 항목(`docs/새폴더/`)도 그 아래 경로를 미커밋으로 잡는다 —
        # `-uall` 로 대부분 개별 파일이 오지만 서브모듈 등은 여전히 접혀서 온다.
        if p in dirty_set or any(d.endswith("/") and p.startswith(d) for d in dirty_set):
            uncommitted.append(p)
        elif p in touched_by_commit:
            committed.append(p)
        else:
            unaccounted.append(p)

    return GitFacts(
        ok=True, branch=branch, commits_since=count, commits_truncated=truncated,
        touched_paths=dirty, ignored_noise=noise,
        committed_manifest=tuple(committed),
        uncommitted_manifest=tuple(uncommitted),
        unaccounted_manifest=tuple(unaccounted),
    )


# --------------------------------------------------------------------------- #
# 6. 판정 (순수) — 이 프로젝트가 데인 오분류를 여기서 막는다.
# --------------------------------------------------------------------------- #
def heartbeat_axis(facts: DispatchFacts, now: dt.datetime, stale_after: dt.timedelta) -> str:
    """`unknown` | `warming` | `never` | `fresh` | `stale`.

    - `warming`: 디스패치된 지 아직 한 주기도 안 지났다. 방금 출발한 워커를 죽었다고
      하지 않기 위한 유예다.
    - `never`: 하트비트를 **한 번도 안 보냈다**. 정지와 다르다. 그리고 하트비트가
      디스패치보다 이르면 그건 **직전 디스패치의 낙오분**이라 이 디스패치의 증거가
      아니다 — 같은 `never` 로 내린다.
    """
    if facts.dispatched_at is None:
        return "unknown"
    hb = facts.last_heartbeat_at
    if hb is not None and hb < facts.dispatched_at:
        hb = None
    if hb is None:
        return "warming" if now - facts.dispatched_at <= stale_after else "never"
    return "fresh" if now - hb <= stale_after else "stale"


def branch_axis(git: GitFacts) -> str:
    """`unknown` | `moved`(커밋 있음) | `touched`(미커밋 수정만) | `still`."""
    if not git.ok:
        return "unknown"
    if git.commits_since > 0:
        return "moved"
    return "touched" if git.touched_paths else "still"


def is_fenced(facts: DispatchFacts) -> str | None:
    """런타임 자신이 "이 워커는 더 이상 보고할 수 없다"고 적어둔 경우의 사유. 아니면 None.

    실패 유형 3번(디스패치가 워커에 도달조차 못 함)을 하트비트로 재지 않기 위한 것이다.
    08-02 에 `dispatch_capability_invalid` 로 런타임이 보고를 거부한 전례가 있었다 —
    그때 하트비트는 아무 말도 해주지 않는다. **관측한 필드만 쓴다**: 자격 회수 시각은
    완료된 디스패치에서 실제로 채워지는 것을 확인했고(`capability_revoked_at`),
    디스패치 상태는 런타임이 스스로 `dispatched` 라고 부르지 않게 된 것만 본다 —
    어떤 실패인지는 이 도구가 단정하지 않는다.
    """
    if facts.capability_revoked_at is not None:
        return f"자격 회수됨 {facts.capability_revoked_at:%Y-%m-%d %H:%M}Z — 보고 경로가 닫혔다"
    if facts.dispatch_status and facts.dispatch_status != "dispatched":
        return f"런타임이 이 디스패치를 더 이상 dispatched 로 보지 않는다 " \
               f"(status={facts.dispatch_status})"
    return None


def judge_open(facts: DispatchFacts, git: GitFacts,
               now: dt.datetime, stale_after: dt.timedelta) -> Verdict:
    """아직 보고가 안 온 디스패치."""
    hb, br = heartbeat_axis(facts, now, stale_after), branch_axis(git)
    fence = is_fenced(facts)
    if fence is not None:
        # 하트비트가 신선해도 소용없다 — 보고할 자격이 이미 없다.
        if br == "moved":
            return Verdict("REPORT_LOST", hb, br, f"{fence}; 디스패치 이후 커밋 {git.commits_since}건")
        if br == "touched":
            return Verdict("STALE_UNCOMMITTED", hb, br,
                           f"{fence}; 미커밋 {len(git.touched_paths)}건")
        if br == "still":
            return Verdict("PRESUMED_DEAD", hb, br, fence)
        return Verdict("UNKNOWN", hb, br, f"{fence}; git 축도 못 읽었다 ({git.error})")
    if hb == "unknown":
        return Verdict("UNKNOWN", hb, br, facts.probe_error or "dispatched_at 을 못 읽었다")
    if hb in ("fresh", "warming"):
        return Verdict("WORKING", hb, br)
    if hb == "never":
        if br in ("moved", "touched"):
            return Verdict("WORKING_SILENT", hb, br,
                           "하트비트 미발신 — 죽음이 아니다. 브랜치가 증거다")
        return Verdict("UNKNOWN", hb, br,
                       "하트비트를 한 번도 안 보냈고 브랜치도 안 움직였다 — "
                       "죽었는지 조용히 일하는지 이 도구로는 못 가른다"
                       if br == "still" else (git.error or ""))
    # hb == "stale"
    if br == "moved":
        return Verdict("REPORT_LOST", hb, br, f"디스패치 이후 커밋 {git.commits_since}건")
    if br == "touched":
        return Verdict("STALE_UNCOMMITTED", hb, br,
                       f"미커밋 {len(git.touched_paths)}건: "
                       + ", ".join(git.touched_paths[:3]))
    if br == "still":
        return Verdict("PRESUMED_DEAD", hb, br, "커밋도 미커밋 수정도 없다")
    return Verdict("UNKNOWN", hb, br, git.error or "git 축을 못 읽었다")


def judge_closed(facts: DispatchFacts, git: GitFacts,
                 now: dt.datetime, stale_after: dt.timedelta) -> Verdict:
    """이미 닫힌 태스크 — **보고를 믿지 않고 커밋을 확인한다**(오늘 W4 가 샌 지점)."""
    hb, br = heartbeat_axis(facts, now, stale_after), branch_axis(git)
    if not facts.reported:
        if br == "unknown":
            return Verdict("UNKNOWN", hb, br,
                           f"워커 보고 없이 닫혔는데 git 축도 못 읽었다 ({git.error})")
        if br in ("moved", "touched"):
            return Verdict("CLOSED_NO_REPORT_COMMITTED", hb, br,
                           f"worker_done 없음 / 디스패치 이후 커밋 {git.commits_since}건")
        return Verdict("CLOSED_NO_REPORT_UNVERIFIED", hb, br,
                       "worker_done 도 없고 디스패치 이후 커밋도 없다")
    if not facts.manifest_present or not facts.manifest:
        return Verdict("DONE_NO_MANIFEST", hb, br, "보고에 filesModified 가 없다")
    if br == "unknown":
        return Verdict("UNKNOWN", hb, br, f"매니페스트는 있는데 git 축을 못 읽었다 ({git.error})")
    if git.uncommitted_manifest:
        return Verdict("DONE_UNCOMMITTED", hb, br,
                       "미커밋: " + ", ".join(git.uncommitted_manifest[:4]))
    if git.unaccounted_manifest:
        return Verdict("DONE_UNACCOUNTED", hb, br,
                       "변화 없음: " + ", ".join(git.unaccounted_manifest[:4]))
    return Verdict("DONE_COMMITTED", hb, br,
                   f"{len(git.committed_manifest)}개 경로 전부 커밋됨")


def judge(facts: DispatchFacts, git: GitFacts,
          now: dt.datetime, stale_after: dt.timedelta) -> Verdict:
    return (judge_closed if facts.closed else judge_open)(facts, git, now, stale_after)


# --------------------------------------------------------------------------- #
# 7. 훑기 — 어떤 태스크를 볼지 고르고, 축을 채우고, 판정한다.
# --------------------------------------------------------------------------- #
def select_tasks(tasks: list[dict], now: dt.datetime,
                 lookback: dt.timedelta) -> tuple[list[dict], int]:
    """(볼 것, 창 밖이라 건너뛴 닫힌 태스크 수).

    `dispatched` 는 나이와 무관하게 **전부** 본다 — 오래 방치된 것이야말로 이 도구가
    노리는 것이다(08-03 에 13시간 `dispatched` + 하트비트 0 이 있었다).
    닫힌 태스크는 창 안의 것만 본다. 건너뛴 수는 출력에 찍는다 — 조용히 안 자른다.
    """
    picked, skipped = [], 0
    for t in tasks:
        status = str(t.get("status") or "")
        if status == "dispatched":
            picked.append(t)
            continue
        if status not in ("completed", "failed"):
            continue
        stamp = parse_ts(t.get("completed_at")) or parse_ts(t.get("created_at"))
        if stamp is not None and now - stamp <= lookback:
            picked.append(t)
        else:
            skipped += 1
    return picked, skipped


def sweep(run_id: str, *, now: dt.datetime | None = None,
          stale_min: int = DEFAULT_STALE_MIN,
          lookback_h: int = DEFAULT_LOOKBACK_H) -> tuple[list[Row], int, str | None]:
    """(판정된 행, 창 밖 건너뜀 수, 치명 오류). 상태를 바꾸는 호출은 하나도 없다."""
    now = now or dt.datetime.now(UTC)
    stale_after, lookback = dt.timedelta(minutes=stale_min), dt.timedelta(hours=lookback_h)
    tasks, err = collect_tasks(run_id)
    if err is not None:
        return [], 0, err
    picked, skipped = select_tasks(tasks, now, lookback)
    rows = []
    for task in picked:
        facts = facts_for(task)
        git = git_facts(facts.worktree, facts.dispatched_at, facts.manifest)
        rows.append(Row(facts=facts, git=git, verdict=judge(facts, git, now, stale_after)))
    return rows, skipped, None


# --------------------------------------------------------------------------- #
# 8. 출력 — 못 본 것은 `unknown` 으로 **따로 센다**.
# --------------------------------------------------------------------------- #
def summarize(rows: list[Row]) -> dict:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.verdict.code] = counts.get(r.verdict.code, 0) + 1
    return {
        "swept": len(rows),
        "by_verdict": dict(sorted(counts.items())),
        "needs_human": sum(1 for r in rows if r.verdict.kind.needs_human),
        "unknown": sum(1 for r in rows if r.verdict.kind.is_unknown),
        "heartbeat_axis": _axis_counts(r.verdict.heartbeat_axis for r in rows),
        "branch_axis": _axis_counts(r.verdict.branch_axis for r in rows),
    }


def _axis_counts(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


def render(rows: list[Row], skipped: int, summary: dict) -> str:
    lines = ["task            hb        branch    verdict                      who / title",
             "-" * 108]
    for r in sorted(rows, key=lambda x: (not x.verdict.kind.needs_human,
                                         x.verdict.code, x.facts.task_id)):
        f, v = r.facts, r.verdict
        lines.append(f"{f.task_id:<15} {v.heartbeat_axis:<9} {v.branch_axis:<9} "
                     f"{v.code:<28} {(r.git.branch or '?'):<16} {f.title}")
        if v.note:
            lines.append(f"{'':<15} └ {v.note}")
        if v.kind.needs_human:
            lines.append(f"{'':<15}   → {v.kind.action}")
        if r.git.ignored_noise:
            lines.append(f"{'':<15}   (노이즈로 제외한 미커밋 {len(r.git.ignored_noise)}건: "
                         f"{', '.join(r.git.ignored_noise[:3])})")
        if r.git.commits_truncated:
            lines.append(f"{'':<15}   (커밋 {GIT_LOG_LIMIT}개에서 잘렸을 수 있다)")
    lines += [
        "-" * 108,
        f"훑음 {summary['swept']}건 | 사람 필요 {summary['needs_human']}건 | "
        f"판정 불가(unknown) {summary['unknown']}건 | 창 밖이라 건너뜀 {skipped}건",
        f"하트비트 축: {summary['heartbeat_axis']}",
        f"브랜치 축:   {summary['branch_axis']}",
        f"판정 분포:   {summary['by_verdict']}",
        "unknown 은 '이상 없음'이 아니라 '못 봤다'다. 조치는 사람이 한다 — "
        "이 도구는 아무 상태도 바꾸지 않는다.",
    ]
    return "\n".join(lines)


def as_json(rows: list[Row], skipped: int, summary: dict) -> str:
    payload = {
        "summary": {**summary, "skipped_out_of_window": skipped},
        "rows": [{
            "task_id": r.facts.task_id,
            "title": r.facts.title,
            "task_status": r.facts.task_status,
            "dispatch_status": r.facts.dispatch_status,
            "assignee": r.facts.assignee,
            "worktree": str(r.facts.worktree) if r.facts.worktree else None,
            "branch": r.git.branch,
            "dispatched_at": r.facts.dispatched_at.isoformat() if r.facts.dispatched_at else None,
            "last_heartbeat_at": (r.facts.last_heartbeat_at.isoformat()
                                  if r.facts.last_heartbeat_at else None),
            "heartbeat_axis": r.verdict.heartbeat_axis,
            "branch_axis": r.verdict.branch_axis,
            "commits_since_dispatch": r.git.commits_since,
            "uncommitted": list(r.git.touched_paths),
            "manifest": list(r.facts.manifest),
            "manifest_uncommitted": list(r.git.uncommitted_manifest),
            "manifest_unaccounted": list(r.git.unaccounted_manifest),
            "verdict": r.verdict.code,
            "note": r.verdict.note,
            "action": r.verdict.kind.action,
            "needs_human": r.verdict.kind.needs_human,
            "unknown": r.verdict.kind.is_unknown,
            "git_error": r.git.error,
        } for r in rows],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def exit_code(summary: dict) -> int:
    """0 = 사람 손 필요 없음 / 1 = 조치 필요 / 2 = 조치 필요는 없지만 못 잰 것이 있다."""
    actionable = summary["needs_human"] - summary["unknown"]
    if actionable > 0:
        return 1
    return 2 if summary["unknown"] > 0 else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="dispatched 태스크를 훑어 하트비트·브랜치·보고 매니페스트를 대조한다 "
                    "(읽기 전용, 상태 변경 없음)")
    ap.add_argument("--run", default=None,
                    help="오케스트레이션 Run id. 생략 시 ops_config 의 orchestration_run_id")
    ap.add_argument("--stale-min", type=int, default=DEFAULT_STALE_MIN,
                    help=f"하트비트를 stale 로 볼 분 (기본 {DEFAULT_STALE_MIN} = 5분 계약 3회 미발신)")
    ap.add_argument("--lookback-hours", type=int, default=DEFAULT_LOOKBACK_H,
                    help=f"닫힌 태스크의 커밋 대조 창 (기본 {DEFAULT_LOOKBACK_H}h)")
    ap.add_argument("--json", action="store_true", help="기계 판독용 JSON")
    args = ap.parse_args(argv)

    run_id = args.run or load_ops_config().orchestration_run_id
    if not run_id:
        print("Run id 가 없다. --run 을 주거나 ops_config.yaml 의 orchestration_run_id 를 채워라.",
              file=sys.stderr)
        return 3

    rows, skipped, err = sweep(run_id, stale_min=args.stale_min,
                               lookback_h=args.lookback_hours)
    if err is not None:
        print(f"태스크 목록을 못 읽었다 — 훑지 못했다(이상 없음이 아니다): {err}", file=sys.stderr)
        return 3
    summary = summarize(rows)
    print(as_json(rows, skipped, summary) if args.json else render(rows, skipped, summary))
    return exit_code(summary)


if __name__ == "__main__":
    raise SystemExit(main())

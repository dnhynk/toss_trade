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

여섯 축을 **각각 기계적 사실로** 잰다. 어느 축이든 못 재면 `unknown` 으로 **따로 센다** —
조용히 초록으로 넘기지 않는다(`docs/31` 의 "'결손 0건'이 아니라 '못 봤다'로 읽을 것").

| 축 | 소스 | 사실 |
|---|---|---|
| 하트비트 | `orca orchestration dispatch-show` 의 `last_heartbeat_at` | 마지막 생존 신호 나이 |
| 브랜치 | 워커 워크트리의 `git log` / `git status` | 디스패치 이후 커밋·수정이 있었는가 |
| 보고 매니페스트 | 태스크 `result` 의 `filesModified` | 적어낸 경로가 **실제로 커밋됐는가** |
| **경과** | `dispatched_at` 과 현재 시각 | 디스패치된 지 얼마나 됐는가 (§ 08-08 추가) |
| **머지** | `git log HEAD --not main` | 그 커밋들이 **이미 main 에 들어갔는가** |
| **소유** | 같은 워크트리의 살아 있는 디스패치 + 파일 mtime | **지금 그 경로를 쓰고 있는 태스크가 따로 있는가** |

워크트리 경로는 디스패치 레코드의 `process_incarnation` 에서 나온다 —
`<worktreeId>::<경로>@@<hash>:<uuid>`. 추측이 아니라 런타임이 적어둔 값이다.

## 등급 — "사람 필요"가 상시 1건이면 진짜 1건이 안 보인다

`docs/34` 의 파일 등급 계약(`ALERT_`/`PLANNED_`/`NOTE_`/`TRADEOFF_`)이 지키려던 것은
**"`ALERT_` 파일이 있으면 진짜 문제"** 다. 이 도구 자신은 그 규율을 안 받고 있었다 —
08-08 아침에 "사람 필요"가 상시 1~2건이었고 그 전부가 조치할 것이 없는 건이었다.
그래서 판정마다 등급을 붙인다. 아침에 **한 줄**만 보면 갈린다.

| 등급 | 뜻 | 아침에 할 일 |
|---|---|---|
| `ALERT` | 무엇을 할지 정해져 있는 사고 | 그 조치를 해라 |
| `CHECK` | **이 도구가 못 쟀다** (= 옛 `unknown`) | 사람이 봐라 |
| `NOTE` | 기계로 확인했고 사람이 할 일이 없다 | 참고만 |
| `-` | 아무것도 아니다 | 무시 |

종료 코드는 `ALERT` 로만 1이 된다. `NOTE` 는 절대 1을 만들지 않는다 — 그게 이 등급의 요지다.

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
5. **갓 출발한 워커와 5시간 막힌 워커.** 둘 다 하트비트 미발신 + 브랜치 정지다. 경과
   시간이 없으면 **글자가 똑같다** — 08-08 아침에 W3 가 과제문을 붙인 채 5시간을 서 있었고
   사용자가 먼저 발견했다. 경과가 정상 소요 범위를 넘으면 `NO_SIGNAL_STALLED`(ALERT),
   범위 안이면 `UNKNOWN`(CHECK)이다.
6. **이미 main 에 들어간 산출물.** 보고 없이 닫혔어도 그 커밋이 전부 main 에 있으면
   **사람이 할 일이 없다.** `CLOSED_NO_REPORT_MERGED` 로 내리고 조용히 한다.
7. **살아 있는 다음 태스크가 그 경로의 주인인 경우.** 끝난 태스크의 워크트리에서 미커밋이
   보여도, 그 파일을 **지금 다른 디스패치가 쓰고 있으면** 옛 태스크의 유실이 아니다.
   `DONE_UNCOMMITTED_LIVE` 로 내린다. 단 **못 재면 소유권을 주지 않는다** — 조용해지는
   쪽으로 기울면 진짜 미커밋이 사라진다.

## 판정만 한다

이 도구는 **아무 상태도 바꾸지 않는다.** `orca orchestration` 은 읽기 명령
(`task-list`, `dispatch-show`)만 쓰고, git 은 `log`/`status`/`rev-parse` 만 쓴다.
`task-update`·`worker-abandon`·`dispatch` 는 부르지 않는다 — 조치는 사람이 한다.
라이브 API 호출 없음. 파일시스템도 `stat()` 만 읽는다(소유 축의 mtime).
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
# 임계값 — 이 파일에서 문턱을 쓰는 곳은 셋뿐이고 셋 다 사유가 있다.
# --------------------------------------------------------------------------- #
#: 하트비트가 이보다 오래되면 `stale`. 근거: 워커 preamble 의 계약이 "5분마다 하트비트"
#: 이므로 **연속 3회 미발신**이다. 1회(5분)로 잡으면 긴 툴 호출 한 번에 멀쩡한 워커가
#: 빨개지고, 30분으로 잡으면 오늘처럼 30분 만에 멈춘 것을 못 잡는다.
DEFAULT_STALE_MIN = 15

#: 디스패치된 지 이보다 오래됐으면 `overdue`. **근거는 추측이 아니라 이 Run 의 실측이다** —
#: `run_92948a1f80a5` 의 완료된 디스패치 94건(장시간 라이브 수집·백필 감시 5건과 08-07 의
#: 멈춤 2건 제외)의 `dispatched_at → completed_at`:
#:
#:     n=94  최소 3.8분  중앙 18.8분  p90 46.3분  p95 56.8분  **최대 72.7분**
#:
#: 즉 **정상 디스패치가 73분을 넘은 적이 한 번도 없다.** 90분은 그 최대값의 1.24배이고,
#: 08-08 의 멈춤(5.05시간)과는 3.4배 떨어져 있다. 60분으로 잡으면 실측 3건(61·62·73분)이
#: 빨개지고, 4시간으로 잡으면 오늘의 5시간을 겨우 1시간 앞두고 잡는다.
DEFAULT_OVERDUE_MIN = 90

#: 머지 축이 "이미 들어갔다"를 판정할 기준 ref. 앞의 것부터 시도하고 **하나도 못 찾으면
#: `unknown`** 이다 — 못 찾은 것을 "안 들어갔다"로도 "들어갔다"로도 읽지 않는다.
MAIN_REF_CANDIDATES: tuple[str, ...] = ("main", "origin/main")

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
# 1. 판정 사전 — 코드 하나가 (사람이 할 일, 등급) 을 정한다.
#    `CHECK` 는 "이 도구가 판정하지 못했다"는 뜻이지 "이상 없음"이 아니다.
# --------------------------------------------------------------------------- #
#: 등급 4종. `docs/34` 의 파일 등급 계약과 같은 목적이다 — **ALERT 가 있으면 진짜 문제**.
GRADE_ALERT = "ALERT"   # 조치가 정해져 있다
GRADE_CHECK = "CHECK"   # 이 도구가 못 쟀다 — 사람이 봐야 하지만 조치가 정해져 있지 않다
GRADE_NOTE = "NOTE"     # 기계로 확인했고 사람이 할 일이 없다
GRADE_NONE = "-"        # 아무것도 아니다
GRADE_ORDER = (GRADE_ALERT, GRADE_CHECK, GRADE_NOTE, GRADE_NONE)


@dataclass(frozen=True)
class VerdictKind:
    code: str
    action: str
    grade: str

    @property
    def needs_human(self) -> bool:
        """사람의 눈에 올라가는가. `NOTE` 는 표에는 찍히지만 여기 안 든다."""
        return self.grade in (GRADE_ALERT, GRADE_CHECK)

    @property
    def is_unknown(self) -> bool:
        """**`CHECK` 와 같은 말이다.** 못 잰 것은 정의상 사람에게 올라간다 —
        둘을 따로 두면 "unknown 인데 조용한" 조합을 만들 수 있어서 하나로 묶었다."""
        return self.grade == GRADE_CHECK


VERDICTS: dict[str, VerdictKind] = {v.code: v for v in (
    # --- dispatched (보고가 아직 안 온 것) ---
    VerdictKind("WORKING", "건드리지 마라 — 하트비트 신선", GRADE_NONE),
    VerdictKind("WORKING_SILENT",
                "건드리지 마라 — 하트비트는 없지만 브랜치가 디스패치 이후 움직였다",
                GRADE_NONE),
    VerdictKind("REPORT_LOST",
                "일은 됐고 보고가 유실됐다 — 사람이 머지 게이트 돌리고 태스크 닫아라",
                GRADE_ALERT),
    VerdictKind("STALE_UNCOMMITTED",
                "작업 중 죽었고 결과가 미커밋이다 — 재디스패치하면 덮인다. 워크트리 먼저 봐라",
                GRADE_ALERT),
    VerdictKind("PRESUMED_DEAD",
                "도달 못 했거나 죽었다 — 재디스패치 후보",
                GRADE_ALERT),
    VerdictKind("NO_SIGNAL_STALLED",
                "하트비트도 산출물도 없이 정상 소요시간을 넘겼다 — 터미널을 직접 봐라 "
                "(과제문이 입력창에 붙은 채 서 있는 모양이다)",
                GRADE_ALERT),
    # --- 보고가 온 것 (오늘 W4 가 샌 지점) ---
    VerdictKind("DONE_COMMITTED", "없음 — 보고한 경로가 전부 커밋돼 있다", GRADE_NONE),
    VerdictKind("DONE_UNCOMMITTED",
                "보고는 왔는데 경로가 워크트리에 미커밋이다 — 커밋시키기 전에 닫지 마라",
                GRADE_ALERT),
    VerdictKind("DONE_UNCOMMITTED_LIVE",
                "없음 — 미커밋이지만 그 경로는 지금 살아 있는 디스패치가 쓰고 있다",
                GRADE_NOTE),
    VerdictKind("DONE_UNACCOUNTED",
                "보고한 경로에 디스패치 이후 변화가 없다 — 매니페스트가 사실과 다르다",
                GRADE_ALERT),
    VerdictKind("DONE_NO_MANIFEST",
                "보고에 filesModified 가 없어 커밋 여부를 못 잰다 — 손으로 확인하라",
                GRADE_CHECK),
    # --- 보고 없이 닫힌 것 (오늘 W5 가 샌 지점) ---
    VerdictKind("CLOSED_NO_REPORT_COMMITTED",
                "보고 없이 닫혔지만 브랜치는 움직였다 — 산출물은 있다. 게이트만 확인하라",
                GRADE_ALERT),
    VerdictKind("CLOSED_NO_REPORT_MERGED",
                "없음 — 보고는 없었지만 그 커밋이 이미 main 에 들어가 있다",
                GRADE_NOTE),
    VerdictKind("CLOSED_NO_REPORT_UNVERIFIED",
                "보고도 없고 브랜치도 안 움직였는데 닫혀 있다 — 근거 없는 마감이다",
                GRADE_CHECK),
    # --- 못 잰 것 ---
    VerdictKind("UNKNOWN", "판정 불가 — 아래 note 를 읽고 사람이 봐라", GRADE_CHECK),
)}


@dataclass(frozen=True)
class Verdict:
    code: str
    heartbeat_axis: str
    branch_axis: str
    note: str = ""
    age_axis: str = "unknown"
    merge_axis: str = "unknown"
    owner_axis: str = "none"

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
    completed_at: dt.datetime | None = None     # 머지 축의 창 상한. 열린 디스패치면 None
    last_heartbeat_at: dt.datetime | None = None
    worktree: Path | None = None
    capability_revoked_at: dt.datetime | None = None
    reported: bool = False                      # result.provenance == worker_report
    closed: bool = False                        # task_status 가 종결 상태
    manifest: tuple[str, ...] = ()              # result.filesModified
    manifest_present: bool = False
    probe_error: str | None = None              # dispatch-show 실패 사유


@dataclass(frozen=True)
class LiveOwner:
    """같은 워크트리에서 **지금 돌고 있는** 디스패치. 소유 축의 입력이다."""
    task_id: str
    dispatched_at: dt.datetime


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
    #: 머지 축. `main_ref=None` 이면 못 쟀다 — "안 들어갔다"가 아니다.
    main_ref: str | None = None
    #: 머지 축이 실제로 센 커밋 수 — **창 상한(`completed_at`)까지** 적용한 값이다.
    #: `None` 이면 상한이 없었다는 뜻이고 그때는 `commits_since` 와 같다.
    merge_window_commits: int | None = None
    unmerged_since: int | None = None           # 그 중 main 에 없는 것
    #: 소유 축. 살아 있는 다음 디스패치가 쓰고 있다고 판정한 매니페스트 경로.
    live_owner: LiveOwner | None = None
    live_owned_manifest: tuple[str, ...] = ()


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
        completed_at=parse_ts(d.get("completed_at")),
        last_heartbeat_at=parse_ts(d.get("last_heartbeat_at")),
        capability_revoked_at=parse_ts(d.get("capability_revoked_at")),
        worktree=worktree_of(d.get("process_incarnation")),
    )


# --------------------------------------------------------------------------- #
# 5. git 축 — 워커 워크트리에서 읽기만 한다.
# --------------------------------------------------------------------------- #
def git_log_since(worktree: Path, since: dt.datetime, not_ref: str | None = None,
                  until: dt.datetime | None = None) -> tuple[int, bool, set[str], str | None]:
    """(창 안 커밋 수, 잘렸는가, 그 커밋들이 건드린 경로, 오류).

    `not_ref` 를 주면 **그 ref 에 이미 들어간 커밋을 뺀다**(`git log HEAD --not main`).
    같은 창을 두 번 세어 빼면 "그 창의 커밋 중 아직 main 에 없는 것" 이 나온다.
    가장 최근 커밋 하나만 `merge-base --is-ancestor` 로 물어보는 방법도 있지만, `git log`
    는 커밋 날짜 순이라 머지가 섞이면 "최근 것이 조상이면 나머지도 조상"이 성립하지 않는다.

    `until` 은 **창 상한**이다. 워크트리는 태스크마다가 아니라 워커마다라(§7) 상한이 없으면
    **뒤에 돈 태스크의 커밋이 앞 태스크의 창에 들어온다.** 커밋 직후 실측으로 확인했다:
    `task_4c7447a2597a` 의 창에서 상한을 빼면 미머지 1건(= 방금 내가 만든 다른 태스크의
    커밋)이 잡혀 상시 경보가 되살아났고, 상한을 넣으면 창 안 8건이 전부 main 이었다.
    """
    extra = ["--not", not_ref] if not_ref else []
    rc, out, err = run_read_only(
        ["git", "log", f"-n{GIT_LOG_LIMIT}", f"--format=%H{_SEP}%cI", "--name-only", "HEAD",
         *extra],
        cwd=worktree)
    if rc != 0:
        return 0, False, set(), (err.strip() or f"git log exit {rc}")[:200]
    count, paths, cur_in_window, seen = 0, set(), False, 0
    for line in out.splitlines():
        if _SEP in line:
            seen += 1
            _, _, ts = line.partition(_SEP)
            when = parse_ts(ts)
            cur_in_window = (when is not None and when >= since
                             and (until is None or when <= until))
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


def git_main_ref(worktree: Path) -> str | None:
    """머지 축의 기준 ref. 후보를 순서대로 물어보고 **하나도 없으면 None**(= 못 쟀다)."""
    for ref in MAIN_REF_CANDIDATES:
        rc, out, _ = run_read_only(["git", "rev-parse", "--verify", "--quiet", ref], cwd=worktree)
        if rc == 0 and out.strip():
            return ref
    return None


def owned_by_live(worktree: Path, paths: tuple[str, ...],
                  owner: LiveOwner | None) -> tuple[str, ...]:
    """살아 있는 다음 디스패치가 **지금 쓰고 있는** 경로만 골라낸다.

    판별은 파일 mtime 이다: 그 디스패치가 출발한 뒤에 수정된 파일이면 그 디스패치의 것이다.
    08-08 아침의 `docs/44` 가 정확히 그랬다 — 2분 전 수정, 224줄 추가.

    **못 재면 소유권을 주지 않는다.** 이 축은 판정을 조용하게 만드는 방향이라, 근거가
    없을 때 기울면 진짜 미커밋이 사라진다(`docs/30` §3 의 거부 기본값).
    """
    if owner is None:
        return ()
    owned = []
    for p in paths:
        try:
            mtime = dt.datetime.fromtimestamp((worktree / p).stat().st_mtime, UTC)
        except (OSError, ValueError, OverflowError):
            continue                    # 못 쟀다 → 소유권 없음
        if mtime >= owner.dispatched_at:
            owned.append(p)
    return tuple(owned)


def git_facts(worktree: Path | None, since: dt.datetime | None,
              manifest: tuple[str, ...] = (), owner: LiveOwner | None = None,
              until: dt.datetime | None = None) -> GitFacts:
    """워크트리 하나에 대한 git 축. 못 읽으면 `ok=False` — '안 움직임'이 아니다.

    `until`(= 태스크의 `completed_at`)은 **머지 축에만** 건다. 브랜치 축(`commits_since`)에
    걸지 않는 이유는, 코디네이터가 태스크를 닫은 **뒤에** 워커의 잔여물을 커밋해 주는 일이
    실제로 있었기 때문이다(08-07 W4, `ba9a570`). 거기까지 상한으로 잘라내면 그 태스크가
    `DONE_UNACCOUNTED` 로 되살아난다 — 시끄러워지는 방향의 새 오탐이다.
    """
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

    # 머지 축 — **창 상한까지 적용한** 커밋 중 아직 main 에 없는 것.
    # ref 를 못 찾거나 어느 한쪽을 못 세면 None 이다(= 못 쟀다. "안 들어갔다"가 아니다).
    main_ref = git_main_ref(worktree)
    window_commits: int | None = None
    unmerged: int | None = None
    if main_ref is not None:
        if until is None:
            window_commits, window_trunc = count, truncated
        else:
            window_commits, window_trunc, _, w_err = git_log_since(worktree, since, None, until)
            if w_err is not None:
                window_commits = None
        if window_commits and not window_trunc:
            u_count, u_trunc, _, m_err = git_log_since(worktree, since, main_ref, until)
            if m_err is None and not u_trunc:
                unmerged = u_count

    committed, uncommitted, unaccounted, live_owned = [], [], [], []
    dirty_set = set(dirty) | set(noise)

    def is_dirty(p: str) -> bool:
        # 디렉터리로 접힌 항목(`docs/새폴더/`)도 그 아래 경로를 미커밋으로 잡는다 —
        # `-uall` 로 대부분 개별 파일이 오지만 서브모듈 등은 여전히 접혀서 온다.
        return p in dirty_set or any(d.endswith("/") and p.startswith(d) for d in dirty_set)

    dirty_manifest = tuple(p for p in map(norm_path, manifest) if is_dirty(p))
    owned_set = set(owned_by_live(worktree, dirty_manifest, owner))
    for raw in manifest:
        p = norm_path(raw)
        if p in owned_set:
            live_owned.append(p)        # 미커밋이지만 지금 다른 디스패치가 쓰고 있다
        elif is_dirty(p):
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
        main_ref=main_ref, merge_window_commits=window_commits, unmerged_since=unmerged,
        live_owner=owner, live_owned_manifest=tuple(live_owned),
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
    """`unknown` | `moved`(커밋 있음) | `touched`(미커밋 수정만) | `still`.

    소유 축은 여기 안 들어간다 — 이 축은 "워크트리가 살아 있는가"이고, 살아 있는 다음
    디스패치의 편집도 워크트리가 살아 있다는 사실 자체는 맞기 때문이다. 소유는 **매니페스트
    대조**에서만 쓴다(그쪽이 08-08 아침에 실제로 샌 지점이다).
    """
    if not git.ok:
        return "unknown"
    if git.commits_since > 0:
        return "moved"
    return "touched" if git.touched_paths else "still"


def age_axis(facts: DispatchFacts, now: dt.datetime,
             stale_after: dt.timedelta, overdue_after: dt.timedelta) -> str:
    """`unknown` | `warming` | `normal` | `overdue`.

    08-08 아침에 없어서 5시간을 놓친 축이다. 하트비트 미발신 + 브랜치 정지는 **갓 출발한
    워커와 5시간 막힌 워커가 같은 글자**인데, 경과가 그 둘을 가른다.
    `warming` 문턱은 하트비트 축과 같은 값을 쓴다 — 두 축이 같은 "아직 이르다"를 뜻하는데
    문턱이 다르면 표에서 두 축이 어긋난 채 찍힌다.
    """
    if facts.dispatched_at is None:
        return "unknown"
    elapsed = now - facts.dispatched_at
    if elapsed <= stale_after:
        return "warming"
    return "normal" if elapsed < overdue_after else "overdue"


def merge_window(git: GitFacts) -> int:
    """머지 축이 실제로 센 커밋 수. 창 상한이 없었으면 `commits_since` 와 같다."""
    return git.commits_since if git.merge_window_commits is None else git.merge_window_commits


def merge_axis(git: GitFacts) -> str:
    """`unknown`(기준 ref 를 못 찾음) | `none`(잴 커밋 없음) | `merged` | `unmerged`."""
    if not git.ok or git.main_ref is None:
        return "unknown"
    if merge_window(git) == 0:
        return "none"
    if git.unmerged_since is None:
        return "unknown"
    return "merged" if git.unmerged_since == 0 else "unmerged"


def owner_axis(git: GitFacts) -> str:
    """`none`(살아 있는 다음 디스패치 없음) | `live`(있지만 이 경로는 아님) | `held`."""
    if git.live_owner is None:
        return "none"
    return "held" if git.live_owned_manifest else "live"


def elapsed_label(facts: DispatchFacts, now: dt.datetime) -> str:
    """표에 찍을 경과 시간. 문턱이 아니라 **사람이 읽는 숫자**다 — 15분과 5시간을 눈으로 가른다."""
    if facts.dispatched_at is None:
        return "?"
    m = (now - facts.dispatched_at).total_seconds() / 60
    return f"{m:.0f}m" if m < 90 else f"{m / 60:.1f}h"


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


def judge_open(facts: DispatchFacts, git: GitFacts, now: dt.datetime,
               stale_after: dt.timedelta, overdue_after: dt.timedelta) -> Verdict:
    """아직 보고가 안 온 디스패치."""
    hb, br = heartbeat_axis(facts, now, stale_after), branch_axis(git)
    ax = dict(age_axis=age_axis(facts, now, stale_after, overdue_after),
              merge_axis=merge_axis(git), owner_axis=owner_axis(git))
    fence = is_fenced(facts)
    if fence is not None:
        # 하트비트가 신선해도 소용없다 — 보고할 자격이 이미 없다.
        if br == "moved":
            return Verdict("REPORT_LOST", hb, br,
                           f"{fence}; 디스패치 이후 커밋 {git.commits_since}건"
                           + _merged_note(git), **ax)
        if br == "touched":
            return Verdict("STALE_UNCOMMITTED", hb, br,
                           f"{fence}; 미커밋 {len(git.touched_paths)}건", **ax)
        if br == "still":
            return Verdict("PRESUMED_DEAD", hb, br, fence, **ax)
        return Verdict("UNKNOWN", hb, br, f"{fence}; git 축도 못 읽었다 ({git.error})", **ax)
    if hb == "unknown":
        return Verdict("UNKNOWN", hb, br, facts.probe_error or "dispatched_at 을 못 읽었다", **ax)
    if hb in ("fresh", "warming"):
        return Verdict("WORKING", hb, br, "", **ax)
    if hb == "never":
        if br in ("moved", "touched"):
            return Verdict("WORKING_SILENT", hb, br,
                           "하트비트 미발신 — 죽음이 아니다. 브랜치가 증거다", **ax)
        if br == "still":
            # ★ 경과 축이 여기서 갈린다. 08-08 아침에 이 두 줄이 같은 `UNKNOWN` 이었고,
            #    그래서 5시간 막힌 W3 가 갓 출발한 워커와 구분되지 않았다.
            if ax["age_axis"] == "overdue":
                return Verdict("NO_SIGNAL_STALLED", hb, br,
                               f"디스패치 {elapsed_label(facts, now)} 경과 — 하트비트 0회, "
                               f"커밋 0건, 미커밋 0건. 실측상 정상 디스패치는 73분을 넘은 적이 없다",
                               **ax)
            return Verdict("UNKNOWN", hb, br,
                           f"하트비트를 한 번도 안 보냈고 브랜치도 안 움직였다 "
                           f"({elapsed_label(facts, now)} 경과 — 아직 정상 소요 범위 안이다). "
                           "죽었는지 조용히 일하는지 이 도구로는 못 가른다", **ax)
        return Verdict("UNKNOWN", hb, br, git.error or "", **ax)
    # hb == "stale"
    if br == "moved":
        return Verdict("REPORT_LOST", hb, br,
                       f"디스패치 이후 커밋 {git.commits_since}건" + _merged_note(git), **ax)
    if br == "touched":
        return Verdict("STALE_UNCOMMITTED", hb, br,
                       f"미커밋 {len(git.touched_paths)}건: "
                       + ", ".join(git.touched_paths[:3]), **ax)
    if br == "still":
        return Verdict("PRESUMED_DEAD", hb, br, "커밋도 미커밋 수정도 없다", **ax)
    return Verdict("UNKNOWN", hb, br, git.error or "git 축을 못 읽었다", **ax)


def _merged_note(git: GitFacts) -> str:
    """머지 축을 조치에 붙인다 — 이미 main 이면 게이트가 아니라 '닫기'만 남는다."""
    if merge_axis(git) == "merged":
        return f"; 그 커밋은 이미 {git.main_ref} 에 있다 (게이트는 끝났다, 닫기만 남았다)"
    return ""


def judge_closed(facts: DispatchFacts, git: GitFacts, now: dt.datetime,
                 stale_after: dt.timedelta, overdue_after: dt.timedelta) -> Verdict:
    """이미 닫힌 태스크 — **보고를 믿지 않고 커밋을 확인한다**(오늘 W4 가 샌 지점)."""
    hb, br = heartbeat_axis(facts, now, stale_after), branch_axis(git)
    ax = dict(age_axis=age_axis(facts, now, stale_after, overdue_after),
              merge_axis=merge_axis(git), owner_axis=owner_axis(git))
    if not facts.reported:
        if br == "unknown":
            return Verdict("UNKNOWN", hb, br,
                           f"워커 보고 없이 닫혔는데 git 축도 못 읽었다 ({git.error})", **ax)
        if br in ("moved", "touched"):
            # ★ 머지 축. 산출물이 이미 main 에 있으면 사람이 할 일이 없다 — 08-08 아침에
            #    이 한 줄이 없어서 `task_4c7447a2597a` 가 상시 경보로 남아 있었다.
            if ax["merge_axis"] == "merged":
                return Verdict("CLOSED_NO_REPORT_MERGED", hb, br,
                               f"이 태스크의 창(디스패치~종료) 안 커밋 {merge_window(git)}건이 "
                               f"전부 {git.main_ref} 에 들어가 있다 — 조치 없음", **ax)
            return Verdict("CLOSED_NO_REPORT_COMMITTED", hb, br,
                           f"worker_done 없음 / 디스패치 이후 커밋 {git.commits_since}건"
                           + (f" (창 안 {merge_window(git)}건 중 {git.unmerged_since}건이 "
                              f"아직 {git.main_ref} 밖)"
                              if ax["merge_axis"] == "unmerged" else ""), **ax)
        return Verdict("CLOSED_NO_REPORT_UNVERIFIED", hb, br,
                       "worker_done 도 없고 디스패치 이후 커밋도 없다", **ax)
    if not facts.manifest_present or not facts.manifest:
        return Verdict("DONE_NO_MANIFEST", hb, br, "보고에 filesModified 가 없다", **ax)
    if br == "unknown":
        return Verdict("UNKNOWN", hb, br,
                       f"매니페스트는 있는데 git 축을 못 읽었다 ({git.error})", **ax)
    if git.uncommitted_manifest:
        return Verdict("DONE_UNCOMMITTED", hb, br,
                       "미커밋: " + ", ".join(git.uncommitted_manifest[:4]), **ax)
    # ★ 소유 축. 미커밋이 남았지만 그 경로를 **지금 다른 디스패치가 쓰고 있다면** 옛 태스크의
    #    유실이 아니다 — 08-08 아침의 `docs/44` 가 그랬다(2분 전 수정, 224줄 추가).
    if git.live_owned_manifest:
        owner = git.live_owner
        return Verdict("DONE_UNCOMMITTED_LIVE", hb, br,
                       f"미커밋 {len(git.live_owned_manifest)}건은 살아 있는 "
                       f"{owner.task_id if owner else '?'} 가 쓰고 있다: "
                       + ", ".join(git.live_owned_manifest[:4]), **ax)
    if git.unaccounted_manifest:
        return Verdict("DONE_UNACCOUNTED", hb, br,
                       "변화 없음: " + ", ".join(git.unaccounted_manifest[:4]), **ax)
    return Verdict("DONE_COMMITTED", hb, br,
                   f"{len(git.committed_manifest)}개 경로 전부 커밋됨", **ax)


def judge(facts: DispatchFacts, git: GitFacts, now: dt.datetime, stale_after: dt.timedelta,
          overdue_after: dt.timedelta = dt.timedelta(minutes=DEFAULT_OVERDUE_MIN)) -> Verdict:
    return (judge_closed if facts.closed else judge_open)(
        facts, git, now, stale_after, overdue_after)


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


def live_owners(all_facts: list[DispatchFacts]) -> dict[Path, LiveOwner]:
    """워크트리 → 지금 그 워크트리에서 돌고 있는 디스패치.

    **워크트리는 태스크마다가 아니라 워커마다다.** 그래서 끝난 태스크의 워크트리에
    다음 태스크가 이미 들어와 있을 수 있고, 08-08 아침에 그것이 옛 태스크의 미커밋으로
    읽혔다. 같은 워크트리에 여럿이면 **가장 최근 것**이 주인이다.
    """
    out: dict[Path, LiveOwner] = {}
    for f in all_facts:
        if f.closed or f.dispatch_status != "dispatched":
            continue
        if f.worktree is None or f.dispatched_at is None:
            continue
        cur = out.get(f.worktree)
        if cur is None or f.dispatched_at > cur.dispatched_at:
            out[f.worktree] = LiveOwner(f.task_id, f.dispatched_at)
    return out


def owner_for(facts: DispatchFacts, owners: dict[Path, LiveOwner]) -> LiveOwner | None:
    """이 태스크의 워크트리를 **자기보다 나중에** 점유한 살아 있는 디스패치. 자기 자신은 뺀다."""
    if facts.worktree is None or facts.dispatched_at is None:
        return None
    owner = owners.get(facts.worktree)
    if owner is None or owner.task_id == facts.task_id:
        return None
    return owner if owner.dispatched_at > facts.dispatched_at else None


def sweep(run_id: str, *, now: dt.datetime | None = None,
          stale_min: int = DEFAULT_STALE_MIN,
          lookback_h: int = DEFAULT_LOOKBACK_H,
          overdue_min: int = DEFAULT_OVERDUE_MIN) -> tuple[list[Row], int, str | None]:
    """(판정된 행, 창 밖 건너뜀 수, 치명 오류). 상태를 바꾸는 호출은 하나도 없다."""
    now = now or dt.datetime.now(UTC)
    stale_after, lookback = dt.timedelta(minutes=stale_min), dt.timedelta(hours=lookback_h)
    overdue_after = dt.timedelta(minutes=overdue_min)
    tasks, err = collect_tasks(run_id)
    if err is not None:
        return [], 0, err
    picked, skipped = select_tasks(tasks, now, lookback)
    # 소유 축은 **행 하나로는 못 잰다** — 다른 태스크의 디스패치 레코드가 필요하다.
    # 그래서 런타임 사실을 전부 모은 뒤에 git 축으로 넘어간다.
    all_facts = [facts_for(task) for task in picked]
    owners = live_owners(all_facts)
    rows = []
    for facts in all_facts:
        git = git_facts(facts.worktree, facts.dispatched_at, facts.manifest,
                        owner_for(facts, owners), facts.completed_at)
        rows.append(Row(facts=facts, git=git,
                        verdict=judge(facts, git, now, stale_after, overdue_after)))
    return rows, skipped, None


# --------------------------------------------------------------------------- #
# 8. 출력 — 못 본 것은 `unknown` 으로 **따로 센다**.
# --------------------------------------------------------------------------- #
def summarize(rows: list[Row]) -> dict:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.verdict.code] = counts.get(r.verdict.code, 0) + 1
    by_grade = {g: sum(1 for r in rows if r.verdict.kind.grade == g) for g in GRADE_ORDER}
    return {
        "swept": len(rows),
        "by_verdict": dict(sorted(counts.items())),
        "by_grade": by_grade,
        "alert": by_grade[GRADE_ALERT],
        "needs_human": sum(1 for r in rows if r.verdict.kind.needs_human),
        "unknown": sum(1 for r in rows if r.verdict.kind.is_unknown),
        "heartbeat_axis": _axis_counts(r.verdict.heartbeat_axis for r in rows),
        "branch_axis": _axis_counts(r.verdict.branch_axis for r in rows),
        "age_axis": _axis_counts(r.verdict.age_axis for r in rows),
        "merge_axis": _axis_counts(r.verdict.merge_axis for r in rows),
        "owner_axis": _axis_counts(r.verdict.owner_axis for r in rows),
    }


def _axis_counts(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


def render(rows: list[Row], skipped: int, summary: dict, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(UTC)
    lines = ["task            grade  age     hb        branch    verdict                      "
             "who / title",
             "-" * 116]
    for r in sorted(rows, key=lambda x: (GRADE_ORDER.index(x.verdict.kind.grade),
                                         x.verdict.code, x.facts.task_id)):
        f, v = r.facts, r.verdict
        lines.append(f"{f.task_id:<15} {v.kind.grade:<6} {elapsed_label(f, now):<7} "
                     f"{v.heartbeat_axis:<9} {v.branch_axis:<9} "
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
    g = summary["by_grade"]
    lines += [
        "-" * 116,
        f"훑음 {summary['swept']}건 | ★ 조치(ALERT) {g[GRADE_ALERT]}건 | "
        f"확인(CHECK, 못 잼) {g[GRADE_CHECK]}건 | 참고(NOTE) {g[GRADE_NOTE]}건 | "
        f"창 밖이라 건너뜀 {skipped}건",
        f"하트비트 축: {summary['heartbeat_axis']}",
        f"브랜치 축:   {summary['branch_axis']}",
        f"경과 축:     {summary['age_axis']}",
        f"머지 축:     {summary['merge_axis']}",
        f"소유 축:     {summary['owner_axis']}",
        f"판정 분포:   {summary['by_verdict']}",
        "조치(ALERT) 0건이면 손댈 것이 없다. 확인(CHECK)은 '이상 없음'이 아니라 '못 봤다'다 — "
        "참고(NOTE)는 기계로 확인해 조치가 없는 것이다.",
        "조치는 전부 사람이 한다 — 이 도구는 아무 상태도 바꾸지 않는다.",
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
            "age_axis": r.verdict.age_axis,
            "merge_axis": r.verdict.merge_axis,
            "owner_axis": r.verdict.owner_axis,
            "main_ref": r.git.main_ref,
            "merge_window_commits": merge_window(r.git),
            "unmerged_since_dispatch": r.git.unmerged_since,
            "live_owner_task": r.git.live_owner.task_id if r.git.live_owner else None,
            "manifest_live_owned": list(r.git.live_owned_manifest),
            "commits_since_dispatch": r.git.commits_since,
            "uncommitted": list(r.git.touched_paths),
            "manifest": list(r.facts.manifest),
            "manifest_uncommitted": list(r.git.uncommitted_manifest),
            "manifest_unaccounted": list(r.git.unaccounted_manifest),
            "verdict": r.verdict.code,
            "grade": r.verdict.kind.grade,
            "note": r.verdict.note,
            "action": r.verdict.kind.action,
            "needs_human": r.verdict.kind.needs_human,
            "unknown": r.verdict.kind.is_unknown,
            "git_error": r.git.error,
        } for r in rows],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def exit_code(summary: dict) -> int:
    """0 = 손댈 것 없음 / 1 = 조치(ALERT) 있음 / 2 = 조치는 없지만 **못 잰 것**(CHECK)이 있다.

    **`NOTE` 는 절대 1을 만들지 않는다.** 그것이 이 등급의 요지다 — 08-08 아침처럼 조치가
    없는 건이 매일 종료 코드 1을 만들면 진짜 1이 왔을 때 아무도 안 본다.
    """
    if summary["by_grade"][GRADE_ALERT] > 0:
        return 1
    return 2 if summary["by_grade"][GRADE_CHECK] > 0 else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="dispatched 태스크를 훑어 하트비트·브랜치·보고 매니페스트·경과·머지·소유를 "
                    "대조한다 (읽기 전용, 상태 변경 없음)")
    ap.add_argument("--run", default=None,
                    help="오케스트레이션 Run id. 생략 시 ops_config 의 orchestration_run_id")
    ap.add_argument("--stale-min", type=int, default=DEFAULT_STALE_MIN,
                    help=f"하트비트를 stale 로 볼 분 (기본 {DEFAULT_STALE_MIN} = 5분 계약 3회 미발신)")
    ap.add_argument("--overdue-min", type=int, default=DEFAULT_OVERDUE_MIN,
                    help=f"디스패치 경과를 overdue 로 볼 분 "
                         f"(기본 {DEFAULT_OVERDUE_MIN} = 실측 최장 정상 디스패치 72.7분의 1.24배)")
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
                               lookback_h=args.lookback_hours,
                               overdue_min=args.overdue_min)
    if err is not None:
        print(f"태스크 목록을 못 읽었다 — 훑지 못했다(이상 없음이 아니다): {err}", file=sys.stderr)
        return 3
    summary = summarize(rows)
    print(as_json(rows, skipped, summary) if args.json else render(rows, skipped, summary))
    return exit_code(summary)


if __name__ == "__main__":
    raise SystemExit(main())

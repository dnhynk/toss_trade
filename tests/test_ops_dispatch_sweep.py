"""`ops/dispatch_sweep.py` 검증 — 소유: W5.

## 이 스위트의 성공 기준은 "잡는다"가 아니라 "**멀쩡한 것을 안 잡는다**"

죽은 디스패치를 잡는 도구는 만들기 쉽다 — 전부 죽었다고 하면 100% 재현율이다.
그래서 이 스위트의 절반은 **대조군**이다: 멀쩡히 일하는 워커를 죽었다고 부르면
코디네이터는 살아 있는 워커를 재디스패치해 그 워크트리를 덮는다. 오분류의 대가가
탐지 실패보다 크다.

대조군 4종 (`test_control_*`):

| 상황 | 왜 살아 있다고 봐야 하는가 |
|---|---|
| 하트비트 신선 | 계약대로 보내고 있다 |
| 하트비트 없지만 브랜치가 방금 움직임 | 오늘 W3 — 미발신은 죽음이 아니다 |
| 방금 디스패치돼 아직 첫 주기 전 | 유예 없이 재면 출발한 워커가 전부 빨개진다 |
| 보고 왔고 매니페스트 전부 커밋됨 | 실제로 끝난 것 |

## 고치기 전 실패를 재현한다 (`test_before_the_tool_*`)

"고쳤다"를 주장하려면 고치기 전 상태가 어떻게 안 보였는지를 보여야 한다.
이 도구가 없을 때 코디네이터가 가진 신호는 **태스크 상태 하나**였다:
`completed` 면 됐고 `dispatched` 면 도는 중. 그 한 축으로는 오늘의 두 사고가
**둘 다 초록**이다. 같은 픽스처를 세 축으로 대조하면 둘 다 빨개진다.

## 사보타주 (`test_sabotage_*`)

판정 로직을 "전부 살아있음"으로 못 박으면 몇 개가 빨개지는지, 그리고 그때
**대조군이 여전히 초록인지**를 확인한다. 대조군까지 같이 빨개지는 사보타주는
"가드가 작동한다"의 증거가 아니라 "테스트가 아무거나 잡는다"의 증거다.

라이브 API 호출 없음. `orca` 상태 변경 명령 없음(그 자체를 `test_no_mutating_*` 이 강제).
git 은 `tmp_path` 안의 일회용 리포지토리에만 쓴다 — 실제 워크트리는 안 건드린다.
"""
from __future__ import annotations

import ast
import datetime as dt
import json
import pathlib
import subprocess

import pytest

from ops import dispatch_sweep as DS

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 7, 11, 20, tzinfo=UTC)
STALE = dt.timedelta(minutes=DS.DEFAULT_STALE_MIN)


def mins(n: int) -> dt.datetime:
    """NOW 기준 n분 전."""
    return NOW - dt.timedelta(minutes=n)


def facts(**kw) -> DS.DispatchFacts:
    # 기본 디스패치 시각은 충분히 과거로 둔다 — 하트비트가 디스패치보다 이르면 도구가
    # 그것을 **직전 디스패치의 낙오분**으로 보고 `never` 로 내리기 때문에, 그 규칙을
    # 검증하지 않는 케이스에서까지 섞여 들어오면 무엇을 재는지 흐려진다.
    base = dict(task_id="task_x", dispatched_at=mins(600), dispatch_status="dispatched")
    base.update(kw)
    return DS.DispatchFacts(**base)


def git(**kw) -> DS.GitFacts:
    return DS.GitFacts(**kw)


def verdict(f: DS.DispatchFacts, g: DS.GitFacts) -> DS.Verdict:
    return DS.judge(f, g, NOW, STALE)


# --------------------------------------------------------------------------- #
# 1. 오늘의 두 실제 사례 — 픽스처는 실측 레코드에서 왔다
#    (task-list / dispatch-show 를 2026-08-07 11:19Z 에 읽은 값)
# --------------------------------------------------------------------------- #
#: W5 `task_4c7447a2597a` — 클로드 사용 한도 프롬프트에 걸려 `worker_done` 이 안 왔다.
#: 그런데 커밋은 6건 다 돼 있었다. 태스크는 사람이 손으로 `completed` 로 닫았고
#: `result` 는 `None` 이었다(= worker_report provenance 없음).
CASE_REPORT_LOST = (
    facts(task_id="task_4c7447a2597a", title="W5: 결번된 아침 리포트 따라잡기",
          task_status="completed", closed=True, reported=False,
          dispatched_at=dt.datetime(2026, 8, 7, 0, 37, 45, tzinfo=UTC),
          last_heartbeat_at=dt.datetime(2026, 8, 7, 1, 5, tzinfo=UTC)),
    git(branch="w5-ops", commits_since=6),
)

#: W4 `task_6aa7a20dad66` — `worker_done` 이 12분 만에 왔고 `filesModified` 까지 적혀
#: 있었는데, 그 파일은 워크트리에 **미커밋 376줄**로 남아 있었다.
CASE_DONE_UNCOMMITTED = (
    facts(task_id="task_6aa7a20dad66", title="W4: 조용히 늘고 있는 두 숫자",
          task_status="completed", closed=True, reported=True,
          manifest=("docs/37_quiet_counters.md",), manifest_present=True,
          dispatched_at=dt.datetime(2026, 8, 7, 10, 45, 44, tzinfo=UTC),
          last_heartbeat_at=dt.datetime(2026, 8, 7, 10, 58, 24, tzinfo=UTC)),
    git(branch="w4-collector", commits_since=0,
        touched_paths=("docs/37_quiet_counters.md",),
        uncommitted_manifest=("docs/37_quiet_counters.md",)),
)


def test_todays_case_report_never_arrived_but_the_work_was_committed():
    v = verdict(*CASE_REPORT_LOST)
    assert v.code == "CLOSED_NO_REPORT_COMMITTED"
    assert v.kind.needs_human, "보고가 유실된 채 닫힌 태스크는 사람이 게이트를 돌려야 한다"


def test_todays_case_report_arrived_but_nothing_was_committed():
    v = verdict(*CASE_DONE_UNCOMMITTED)
    assert v.code == "DONE_UNCOMMITTED"
    assert v.kind.needs_human
    assert "docs/37_quiet_counters.md" in v.note


def test_before_the_tool_task_status_alone_calls_both_of_todays_failures_green():
    """도구가 없을 때 코디네이터가 가진 유일한 축(태스크 상태)으로는 둘 다 안 보인다."""
    def old_view(f: DS.DispatchFacts) -> str:      # 2026-08-07 이전의 실제 판정 방식
        return "ok" if f.task_status == "completed" else "running"

    assert old_view(CASE_REPORT_LOST[0]) == "ok"
    assert old_view(CASE_DONE_UNCOMMITTED[0]) == "ok"
    # 같은 픽스처를 세 축으로 대조하면 둘 다 사람을 부른다.
    assert verdict(*CASE_REPORT_LOST).kind.needs_human
    assert verdict(*CASE_DONE_UNCOMMITTED).kind.needs_human


def test_before_the_tool_a_fresh_worker_looks_identical_to_a_dead_one():
    """`dispatched` 하나로는 살아 있는 워커와 죽은 워커가 같은 글자다 — 축이 갈라야 한다."""
    alive = facts(dispatch_status="dispatched", last_heartbeat_at=mins(2))
    dead = facts(dispatch_status="dispatched", last_heartbeat_at=mins(90))
    assert alive.task_status == dead.task_status == ""      # 런타임이 주는 글자는 같다
    assert verdict(alive, git(branch="b")).code == "WORKING"
    assert verdict(dead, git(branch="b")).code == "PRESUMED_DEAD"


# --------------------------------------------------------------------------- #
# 2. ★ 대조군 — 멀쩡한 워커를 죽었다고 하면 안 된다 (이 스위트의 성공 기준)
# --------------------------------------------------------------------------- #
CONTROLS: dict[str, tuple[DS.DispatchFacts, DS.GitFacts]] = {
    "하트비트 신선": (facts(last_heartbeat_at=mins(3)), git(branch="b")),
    "하트비트 신선 + 브랜치 정지": (facts(last_heartbeat_at=mins(1)), git(branch="b")),
    "하트비트 미발신 + 브랜치 커밋": (facts(last_heartbeat_at=None), git(branch="b", commits_since=2)),
    "하트비트 미발신 + 워크트리 수정": (facts(last_heartbeat_at=None),
                                git(branch="b", touched_paths=("ops/x.py",))),
    "방금 디스패치 (첫 주기 전)": (facts(dispatched_at=mins(4), last_heartbeat_at=None), git(branch="b")),
    "보고 왔고 전부 커밋됨": (facts(closed=True, task_status="completed", reported=True,
                            manifest=("ops/a.py",), manifest_present=True,
                            last_heartbeat_at=mins(40)),
                       git(branch="b", commits_since=1, committed_manifest=("ops/a.py",))),
}


@pytest.mark.parametrize("name", list(CONTROLS))
def test_control_a_healthy_worker_is_never_called_dead(name):
    f, g = CONTROLS[name]
    v = verdict(f, g)
    assert not v.kind.needs_human, (
        f"대조군 '{name}' 이 사람을 불렀다 (verdict={v.code}). 멀쩡한 워커를 잡으면 "
        "코디네이터가 살아 있는 워커를 재디스패치해 워크트리를 덮는다.")
    assert v.code in ("WORKING", "WORKING_SILENT", "DONE_COMMITTED"), v.code


def test_control_never_sent_heartbeat_is_not_death():
    """오늘의 W3 — 일은 하고 있는데 하트비트를 한 번도 안 보냈다. 미발신 != 정지."""
    silent = facts(last_heartbeat_at=None)
    assert DS.heartbeat_axis(silent, NOW, STALE) == "never"
    assert DS.heartbeat_axis(facts(last_heartbeat_at=mins(90)), NOW, STALE) == "stale"
    # 미발신 + 브랜치 정지는 죽음이 아니라 **판정 불가**다.
    v = verdict(silent, git(branch="b"))
    assert v.code == "UNKNOWN" and v.kind.is_unknown
    assert v.code != "PRESUMED_DEAD"


def test_control_a_straggler_heartbeat_from_an_older_dispatch_does_not_mask_a_hung_retry():
    """디스패치보다 이른 하트비트는 이 디스패치의 증거가 아니다 — 신선으로 세면 안 된다."""
    f = facts(dispatched_at=mins(60), last_heartbeat_at=mins(80))   # 디스패치 이전
    assert DS.heartbeat_axis(f, NOW, STALE) == "never"
    assert verdict(f, git(branch="b")).code != "WORKING"


# --------------------------------------------------------------------------- #
# 3. 진짜 죽은 디스패치는 잡혀야 한다
# --------------------------------------------------------------------------- #
def test_a_truly_dead_dispatch_is_caught():
    """하트비트 정지 + 브랜치 안 움직임 = 재디스패치 후보."""
    v = verdict(facts(last_heartbeat_at=mins(90)), git(branch="b"))
    assert v.code == "PRESUMED_DEAD" and v.kind.needs_human


def test_stale_with_commits_is_a_lost_report_not_a_dead_worker():
    v = verdict(facts(last_heartbeat_at=mins(90)), git(branch="b", commits_since=3))
    assert v.code == "REPORT_LOST"
    assert "재디스패치" not in v.kind.action, "일이 끝난 것을 다시 시키면 안 된다"


def test_stale_with_uncommitted_work_warns_against_redispatch():
    """재디스패치가 워크트리를 덮는 경우 — `PRESUMED_DEAD` 와 갈라야 조치가 갈린다."""
    v = verdict(facts(last_heartbeat_at=mins(90)),
                git(branch="b", touched_paths=("tossmon/x.py", "tests/y.py")))
    assert v.code == "STALE_UNCOMMITTED" and v.kind.needs_human
    assert "덮" in v.kind.action


def test_a_fenced_dispatch_is_caught_even_when_the_heartbeat_is_fresh():
    """실패 유형 3 — 런타임이 보고를 거부한 경우(08-02 `dispatch_capability_invalid`).

    하트비트는 살아 있다고 말하지만 보고 경로가 이미 닫혀 있으므로 영원히 안 온다.
    """
    revoked = facts(last_heartbeat_at=mins(1), capability_revoked_at=mins(30))
    assert DS.heartbeat_axis(revoked, NOW, STALE) == "fresh"      # 하트비트는 초록
    assert verdict(revoked, git(branch="b")).code == "PRESUMED_DEAD"
    assert verdict(revoked, git(branch="b", commits_since=1)).code == "REPORT_LOST"


def test_a_dispatch_the_runtime_no_longer_calls_dispatched_is_surfaced():
    f = facts(last_heartbeat_at=mins(1), dispatch_status="failed")
    v = verdict(f, git(branch="b"))
    assert v.code == "PRESUMED_DEAD" and "failed" in v.note


# --------------------------------------------------------------------------- #
# 4. 못 본 것을 "이상 없음"으로 말하지 않는다
# --------------------------------------------------------------------------- #
def test_an_unreadable_git_axis_is_unknown_not_still():
    """워크트리를 못 읽은 것을 '안 움직임'으로 읽으면 멀쩡한 워커가 죽었다고 찍힌다."""
    broken = DS.GitFacts(ok=False, error="워크트리가 없다")
    assert DS.branch_axis(broken) == "unknown"
    v = verdict(facts(last_heartbeat_at=mins(90)), broken)
    assert v.code == "UNKNOWN" and v.kind.is_unknown
    assert v.code != "PRESUMED_DEAD"


def test_an_unreadable_dispatch_record_is_unknown():
    f = DS.DispatchFacts(task_id="t", dispatched_at=None, probe_error="디스패치 레코드 없음")
    v = verdict(f, git())
    assert v.code == "UNKNOWN" and v.kind.is_unknown and "레코드" in v.note


def test_a_report_without_a_manifest_cannot_be_verified_and_says_so():
    """`filesModified` 가 없으면 커밋 여부를 못 잰다 — 초록이 아니라 unknown 이다."""
    v = verdict(facts(closed=True, task_status="completed", reported=True,
                      manifest=(), manifest_present=False, last_heartbeat_at=mins(40)),
                git(branch="b", commits_since=1))
    assert v.code == "DONE_NO_MANIFEST"
    assert v.kind.is_unknown and v.kind.needs_human


def test_a_task_closed_with_no_report_and_no_commits_is_unverified():
    v = verdict(facts(closed=True, task_status="completed", reported=False,
                      last_heartbeat_at=mins(90)), git(branch="b"))
    assert v.code == "CLOSED_NO_REPORT_UNVERIFIED"
    assert v.kind.is_unknown and v.kind.needs_human


def test_a_manifest_path_with_no_change_at_all_is_not_green():
    """보고는 왔고 미커밋도 아닌데 디스패치 이후 그 경로가 커밋된 적도 없는 경우."""
    v = verdict(facts(closed=True, task_status="completed", reported=True,
                      manifest=("ops/ghost.py",), manifest_present=True,
                      last_heartbeat_at=mins(40)),
                git(branch="b", commits_since=1, unaccounted_manifest=("ops/ghost.py",)))
    assert v.code == "DONE_UNACCOUNTED" and v.kind.needs_human


def test_summary_counts_unknown_separately_from_clean():
    rows = [DS.Row(facts=f, git=g, verdict=verdict(f, g)) for f, g in (
        CONTROLS["하트비트 신선"],
        CASE_DONE_UNCOMMITTED,
        (facts(last_heartbeat_at=None), git(branch="b")),          # UNKNOWN
    )]
    s = DS.summarize(rows)
    assert s == {**s, "swept": 3, "needs_human": 2, "unknown": 1}
    assert s["by_verdict"]["UNKNOWN"] == 1
    assert DS.exit_code(s) == 1                                    # 조치 필요가 있으니 1
    only_unknown = DS.summarize([rows[2]])
    assert DS.exit_code(only_unknown) == 2, "못 잰 것만 있으면 0(초록)이 아니라 2다"
    assert DS.exit_code(DS.summarize([rows[0]])) == 0


# --------------------------------------------------------------------------- #
# 5. 사보타주 — 판정을 "전부 살아있음"으로 못 박으면 무엇이 무너지는가
# --------------------------------------------------------------------------- #
SABOTAGE_CASES = {
    "진짜 죽음": (facts(last_heartbeat_at=mins(90)), git(branch="b"), "PRESUMED_DEAD"),
    "오늘 W5 (보고 유실)": (*CASE_REPORT_LOST, "CLOSED_NO_REPORT_COMMITTED"),
    "오늘 W4 (미커밋)": (*CASE_DONE_UNCOMMITTED, "DONE_UNCOMMITTED"),
    "미커밋 채로 죽음": (facts(last_heartbeat_at=mins(90)),
                   git(branch="b", touched_paths=("a.py",)), "STALE_UNCOMMITTED"),
}


def test_sabotage_pinning_every_verdict_to_working_turns_four_cases_green():
    """도구가 없던 상태의 재현 — '전부 살아있음'이면 잡히던 것이 전부 사라진다."""
    def always_alive(f, g, now, stale):
        return DS.Verdict("WORKING", "fresh", "moved")

    lost = [name for name, (f, g, expected) in SABOTAGE_CASES.items()
            if verdict(f, g).code == expected
            and not always_alive(f, g, NOW, STALE).kind.needs_human]
    assert len(lost) == 4, f"사보타주로 놓치는 사례가 4건이어야 한다: {lost}"


def test_sabotage_the_control_group_stays_green_under_the_sabotage():
    """대조군까지 같이 빨개지는 사보타주는 가드의 증거가 아니다 — 아무거나 잡는다는 증거다."""
    for name, (f, g) in CONTROLS.items():
        assert not verdict(f, g).kind.needs_human, name       # 사보타주 전에도 초록
    # 사보타주("전부 살아있음")를 걸어도 대조군의 판정은 바뀌지 않는다 — 원래 살아있음이다.


def test_sabotage_treating_never_sent_heartbeat_as_death_reddens_a_control():
    """미발신을 정지로 뭉개는 사보타주는 대조군(오늘의 W3)을 잡아먹는다."""
    f, g = CONTROLS["하트비트 미발신 + 브랜치 커밋"]
    assert not verdict(f, g).kind.needs_human

    def sabotaged_axis(fa, now, stale):                    # never 를 stale 로 뭉갠다
        axis = DS.heartbeat_axis(fa, now, stale)
        return "stale" if axis == "never" else axis

    hb = sabotaged_axis(f, NOW, STALE)
    assert hb == "stale" and DS.branch_axis(g) == "moved"
    # 그러면 이 대조군은 REPORT_LOST 가 되어 사람을 부른다 = 오분류.
    assert DS.VERDICTS["REPORT_LOST"].needs_human


def test_sabotage_treating_unreadable_worktree_as_still_reddens_a_control():
    """git 축을 못 읽은 것을 '안 움직임'으로 뭉개면 조용한 워커가 죽었다고 찍힌다."""
    f = facts(last_heartbeat_at=mins(90))
    unreadable = DS.GitFacts(ok=False, error="워크트리가 없다")
    assert verdict(f, unreadable).code == "UNKNOWN"
    sabotaged = DS.GitFacts(ok=True, commits_since=0)      # 못 읽은 것을 '안 움직임'으로
    assert verdict(f, sabotaged).code == "PRESUMED_DEAD"


# --------------------------------------------------------------------------- #
# 6. git 축 — 진짜 리포지토리에 대고 잰다 (tmp_path 안 일회용, 실제 워크트리 무접촉)
# --------------------------------------------------------------------------- #
def _git(repo: pathlib.Path, *args: str, when: str | None = None) -> str:
    """`when` 은 **커미터 날짜**를 박는다. `--date` 는 저자 날짜만 바꾸고 도구가 읽는 것은
    `%cI`(커미터) 라서, `--date` 로만 심으면 심은 과거 커밋이 전부 '방금'으로 읽힌다 —
    이 픽스처를 처음 썼을 때 실제로 그렇게 새서 테스트가 2건을 1건으로 못 셌다."""
    import os
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
    if when:
        env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"] = when
    p = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                       env={**os.environ, **env}, encoding="utf-8", errors="replace")
    assert p.returncode == 0, p.stderr
    return p.stdout


SEED_TIME = "2026-08-01T00:00:00+00:00"


@pytest.fixture()
def repo(tmp_path: pathlib.Path) -> pathlib.Path:
    r = tmp_path / "wt"
    r.mkdir()
    _git(r, "init", "-q", "-b", "w9-test")
    (r / "seed.txt").write_text("seed", encoding="utf-8")
    _git(r, "add", "seed.txt")
    _git(r, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed", when=SEED_TIME)
    return r


def _commit(repo: pathlib.Path, path: str, when: str) -> None:
    f = repo / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x", encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", f"add {path}", when=when)


def test_git_axis_sees_a_commit_made_after_dispatch(repo):
    since = dt.datetime(2026, 8, 7, 0, 0, tzinfo=UTC)
    _commit(repo, "docs/new.md", "2026-08-07T05:00:00+00:00")
    g = DS.git_facts(repo, since, ("docs/new.md",))
    assert g.ok and g.branch == "w9-test"
    assert g.commits_since == 1
    assert g.committed_manifest == ("docs/new.md",)
    assert g.uncommitted_manifest == () and g.unaccounted_manifest == ()


def test_git_axis_ignores_commits_made_before_dispatch(repo):
    """디스패치 이전 커밋을 '움직였다'로 세면 죽은 워커가 살아 있어 보인다."""
    _commit(repo, "docs/old.md", "2026-08-05T05:00:00+00:00")
    g = DS.git_facts(repo, dt.datetime(2026, 8, 7, 0, 0, tzinfo=UTC))
    assert g.ok and g.commits_since == 0
    assert DS.branch_axis(g) == "still"


def test_git_axis_reproduces_todays_w4_leak(repo):
    """보고는 왔는데 파일이 워크트리에만 있는 상태 — 실제 08-07 W4."""
    (repo / "docs").mkdir()
    (repo / "docs" / "37_quiet_counters.md").write_text("376 lines\n" * 376, encoding="utf-8")
    g = DS.git_facts(repo, dt.datetime(2026, 8, 7, 0, 0, tzinfo=UTC),
                     ("docs/37_quiet_counters.md",))
    assert g.ok and g.commits_since == 0
    assert g.uncommitted_manifest == ("docs/37_quiet_counters.md",)
    f = facts(closed=True, task_status="completed", reported=True,
              manifest=("docs/37_quiet_counters.md",), manifest_present=True,
              last_heartbeat_at=mins(40))
    assert verdict(f, g).code == "DONE_UNCOMMITTED"


def test_git_axis_reproduces_todays_w5_case(repo):
    """보고는 없는데 커밋은 다 돼 있는 상태 — 실제 08-07 W5."""
    since = dt.datetime(2026, 8, 7, 0, 37, tzinfo=UTC)
    for i in range(6):
        _commit(repo, f"ops/f{i}.py", f"2026-08-07T0{i + 1}:00:00+00:00")
    g = DS.git_facts(repo, since)
    assert g.ok and g.commits_since == 6 and g.touched_paths == ()
    f = facts(closed=True, task_status="completed", reported=False,
              last_heartbeat_at=dt.datetime(2026, 8, 7, 1, 5, tzinfo=UTC))
    assert verdict(f, g).code == "CLOSED_NO_REPORT_COMMITTED"


def test_git_axis_does_not_count_tool_noise_as_worker_activity(repo):
    """`.cache/` 만 더러운 워크트리를 '살아 있다'로 읽으면 죽은 것이 전부 살아 보인다."""
    (repo / ".cache").mkdir()
    (repo / ".cache" / "x.json").write_text("{}", encoding="utf-8")
    (repo / "data").mkdir()
    (repo / "data" / "tossmon.db").write_text("db", encoding="utf-8")
    g = DS.git_facts(repo, dt.datetime(2026, 8, 7, 0, 0, tzinfo=UTC))
    assert g.touched_paths == (), g.touched_paths
    assert g.ignored_noise, "걸러낸 경로는 출력에 노출돼야 한다 — 조용히 자르지 않는다"
    assert DS.branch_axis(g) == "still"


def test_a_manifest_path_under_a_noise_directory_is_still_checked(repo):
    """워커가 스스로 적어낸 경로는 노이즈 필터가 감추면 안 된다."""
    (repo / "data").mkdir()
    (repo / "data" / "report.txt").write_text("x", encoding="utf-8")
    g = DS.git_facts(repo, dt.datetime(2026, 8, 7, 0, 0, tzinfo=UTC), ("data/report.txt",))
    assert g.uncommitted_manifest == ("data/report.txt",)


def test_git_axis_on_a_missing_worktree_is_unknown(tmp_path):
    g = DS.git_facts(tmp_path / "nope", dt.datetime(2026, 8, 7, tzinfo=UTC))
    assert not g.ok and DS.branch_axis(g) == "unknown"


def test_git_axis_without_a_dispatch_time_is_unknown(repo):
    """기준 시각이 없으면 '이후'를 못 잰다 — 0 커밋으로 읽으면 안 된다."""
    g = DS.git_facts(repo, None)
    assert not g.ok and DS.branch_axis(g) == "unknown"


# --------------------------------------------------------------------------- #
# 7. 파싱 — 워크트리 경로·시각·보고 매니페스트
# --------------------------------------------------------------------------- #
def test_worktree_is_parsed_from_the_runtime_incarnation_string():
    real = ("12e59c9d-6eff-4602-8e62-802907e489b4::"
            "C:/Users/dongh/orca/workspaces/toss_trade/w4-collector@@41c50d3c:"
            "4f08c390-7df4-44df-bb24-e37ea47364be")
    assert DS.worktree_of(real) == pathlib.Path("C:/Users/dongh/orca/workspaces/toss_trade/w4-collector")


@pytest.mark.parametrize("bad", [None, "", "garbage", "a::b", 12, "x::C:/p@@zz"])
def test_an_unparseable_incarnation_is_none_not_a_guess(bad):
    assert DS.worktree_of(bad) is None


def test_naive_and_aware_timestamps_land_on_the_same_clock():
    """런타임은 두 형태를 섞어 낸다. naive 를 로컬시로 읽으면 9시간이 틀어진다."""
    assert DS.parse_ts("2026-08-07 11:16:14") == dt.datetime(2026, 8, 7, 11, 16, 14, tzinfo=UTC)
    assert DS.parse_ts("2026-08-07T11:15:24Z") == dt.datetime(2026, 8, 7, 11, 15, 24, tzinfo=UTC)
    assert DS.parse_ts(None) is None and DS.parse_ts("nonsense") is None


def test_only_a_worker_report_counts_as_a_report():
    worker = json.dumps({"provenance": "worker_report", "filesModified": ["a.py", "b.py"]})
    assert DS.report_of({"result": worker}) == (True, ("a.py", "b.py"), True)
    manual = json.dumps({"provenance": "manual_recovery", "reason": "PTY lost"})
    assert DS.report_of({"result": manual}) == (False, (), False)
    assert DS.report_of({"result": None}) == (False, (), False)
    assert DS.report_of({"result": "{not json"}) == (False, (), False)


def test_a_report_with_an_empty_manifest_is_distinguished_from_a_missing_one():
    empty = json.dumps({"provenance": "worker_report", "filesModified": []})
    assert DS.report_of({"result": empty}) == (True, (), True)
    absent = json.dumps({"provenance": "worker_report"})
    assert DS.report_of({"result": absent}) == (True, (), False)


@pytest.mark.parametrize("path,noisy", [
    (".cache/x.json", True), ("data/tossmon.db", True), ("__pycache__/a.pyc", True),
    ("tossmon.egg-info/PKG-INFO", True), ("ops/.venv/lib/x.py", True),
    ("ops/dispatch_sweep.py", False), ("tests/data/fixture.json", False),
    ("docs/38_dispatch_sweep.md", False), ("data_loader.py", False),
])
def test_noise_filter_only_removes_tool_output(path, noisy):
    """`tests/data/` 는 워커가 만드는 픽스처다 — 세그먼트 일치로 잡으면 산출물이 사라진다."""
    assert DS.is_noise(path) is noisy


def test_norm_path_does_not_eat_a_leading_dot():
    """`lstrip('./')` 는 문자 클래스라 `.cache/` 를 `cache/` 로 만든다 — 실제로 낸 버그."""
    assert DS.norm_path(".cache/x") == ".cache/x"
    assert DS.norm_path("./ops/a.py") == "ops/a.py"
    assert DS.norm_path("ops\\a.py") == "ops/a.py"


# --------------------------------------------------------------------------- #
# 8. 훑을 대상 고르기 — 조용히 자르지 않는다
# --------------------------------------------------------------------------- #
def test_every_dispatched_task_is_swept_regardless_of_age():
    """13시간 방치된 `dispatched` 가 이 도구가 노리는 것이다(08-03) — 창으로 자르면 안 된다."""
    tasks = [{"id": "old", "status": "dispatched", "created_at": "2026-08-01 00:00:00"},
             {"id": "new", "status": "dispatched", "created_at": "2026-08-07 11:00:00"}]
    picked, skipped = DS.select_tasks(tasks, NOW, dt.timedelta(hours=1))
    assert {t["id"] for t in picked} == {"old", "new"} and skipped == 0


def test_closed_tasks_outside_the_window_are_counted_not_hidden():
    tasks = [{"id": "recent", "status": "completed", "completed_at": "2026-08-07 10:00:00"},
             {"id": "ancient", "status": "completed", "completed_at": "2026-07-31 04:00:00"}]
    picked, skipped = DS.select_tasks(tasks, NOW, dt.timedelta(hours=24))
    assert [t["id"] for t in picked] == ["recent"]
    assert skipped == 1, "건너뛴 수는 출력에 찍힌다 — '이상 없음'으로 읽히면 안 된다"


def test_a_ready_task_is_not_swept_as_a_live_dispatch():
    picked, skipped = DS.select_tasks([{"id": "r", "status": "ready"}], NOW,
                                      dt.timedelta(hours=24))
    assert picked == [] and skipped == 0


# --------------------------------------------------------------------------- #
# 9. 출력 — 사람이 읽는 표에 unknown 과 조치가 빠지지 않는가
# --------------------------------------------------------------------------- #
def _rows() -> list[DS.Row]:
    picked = [CASE_REPORT_LOST, CASE_DONE_UNCOMMITTED,
              CONTROLS["하트비트 신선"], (facts(last_heartbeat_at=None), git(branch="b"))]
    return [DS.Row(facts=f, git=g, verdict=verdict(f, g)) for f, g in picked]


def test_the_table_names_the_unknown_count_and_the_action():
    rows = _rows()
    text = DS.render(rows, skipped=88, summary=DS.summarize(rows))
    assert "판정 불가(unknown) 1건" in text
    assert "건너뜀 88건" in text
    assert "이상 없음" in text and "못 봤다" in text
    assert "게이트" in text, "조치가 안 적히면 표는 읽히고 잊힌다"


def test_the_json_carries_every_axis_for_each_row():
    rows = _rows()
    payload = json.loads(DS.as_json(rows, 88, DS.summarize(rows)))
    assert payload["summary"]["skipped_out_of_window"] == 88
    for row in payload["rows"]:
        for key in ("heartbeat_axis", "branch_axis", "verdict", "needs_human", "unknown",
                    "commits_since_dispatch", "manifest_uncommitted", "action"):
            assert key in row, key


def test_every_verdict_code_has_an_action():
    for code, kind in DS.VERDICTS.items():
        assert kind.action.strip(), code
        assert kind.needs_human or not kind.is_unknown, (
            f"{code}: unknown 인데 사람을 안 부르면 조용히 초록으로 넘어간다")


def test_every_verdict_the_judge_can_return_is_registered():
    """판정 함수가 사전에 없는 코드를 내면 `.kind` 가 KeyError 로 터진다 — 미리 잡는다."""
    src = pathlib.Path(DS.__file__).read_text(encoding="utf-8")
    emitted = {n.args[0].value for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "Verdict" and n.args
               and isinstance(n.args[0], ast.Constant)}
    assert emitted and emitted <= set(DS.VERDICTS), emitted - set(DS.VERDICTS)


# --------------------------------------------------------------------------- #
# 10. 경계 — 이 도구는 아무 상태도 바꾸지 않는다
# --------------------------------------------------------------------------- #
#: 이 도구가 실행해도 되는 것 전부. **허용 목록이 아니라 전수 요구다** — 아래 가드는
#: 모듈이 실제로 조립하는 모든 커맨드를 AST 로 **발견해서** 이 집합에 들어오길 요구한다
#: (docs/30 §3 의 거부 기본값). 문자열 검색으로 하면 `result.get("dispatch")` 같은
#: 무관한 리터럴에 걸려 가드가 소음이 되고, 진짜 위반은 변수 조립으로 빠져나간다.
READ_ONLY_COMMANDS = {
    ("orca", "orchestration", "task-list"),
    ("orca", "orchestration", "dispatch-show"),
    # `orca_json` 자신 — 하위명령이 `*args` 라 여기서는 접두만 보인다. 이것을 허용해도
    # 구멍이 아닌 이유는 **모든 `orca_json` 호출부가 같은 스캔에 걸리기 때문**이다
    # (`test_the_read_only_guard_can_actually_fail` 이 그것을 증명한다).
    ("orca", "orchestration"),
    ("git", "log"), ("git", "status"), ("git", "rev-parse"),
}


def _shelled_commands(src: str) -> set[tuple[str, ...]]:
    """`run_read_only`/`orca_json` 에 넘기는 리스트 리터럴에서 앞쪽 상수 토큰만 뽑는다."""
    found: set[tuple[str, ...]] = set()
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("run_read_only", "orca_json") and node.args):
            continue
        arg = node.args[0]
        assert isinstance(arg, ast.List), (
            f"{node.func.id} 에 리스트 리터럴이 아닌 것이 넘어간다 — 가드가 못 본다")
        head: list[str] = []
        for el in arg.elts:
            if isinstance(el, ast.Constant) and isinstance(el.value, str):
                head.append(el.value)
            else:
                break               # `*args` 나 변수가 나오면 거기까지가 고정 접두다
        if node.func.id == "orca_json":
            head = ["orca", "orchestration", *head]
        found.add(tuple(head[:3] if head[0] == "orca" else head[:2]))
    return found


def test_every_command_this_tool_can_run_is_read_only():
    """판정만 하고 조치는 사람이 한다 — 실행 가능한 커맨드를 전수로 못 박는다."""
    used = _shelled_commands(pathlib.Path(DS.__file__).read_text(encoding="utf-8"))
    assert used, "커맨드를 하나도 못 찾았다 — 가드가 아무것도 안 지키고 있다"
    assert used <= READ_ONLY_COMMANDS, f"읽기 전용이 아닌 커맨드: {sorted(used - READ_ONLY_COMMANDS)}"


def test_the_read_only_guard_can_actually_fail():
    """실패할 수 있음을 증명하지 못한 가드는 가드가 아니다."""
    bad = 'def f():\n    run_read_only(["git", "commit", "-m", "x"])\n'
    assert _shelled_commands(bad) == {("git", "commit")}
    assert not _shelled_commands(bad) <= READ_ONLY_COMMANDS
    worse = 'def f():\n    orca_json(["task-update", "--status", "completed"])\n'
    assert not _shelled_commands(worse) <= READ_ONLY_COMMANDS


def test_no_live_api_call():
    src = pathlib.Path(DS.__file__).read_text(encoding="utf-8")
    for banned in ("requests", "httpx", "urllib", "socket", "aiohttp"):
        assert f"import {banned}" not in src, banned


# --------------------------------------------------------------------------- #
# 11. 배선 가드 — 거부 기본값 (docs/30 §3). 만들었는데 안 불리면 없는 것과 같다.
# --------------------------------------------------------------------------- #
#: 도달 불가능해도 되는 공개 함수와 **사유**. 허용 목록이 아니다.
NOT_WIRED = {
    "main": "entry point itself - reachability is measured from here",
    "judge_open": "judge() 가 dict 가 아닌 조건 분기로 고른다 - AST 호출 그래프에 안 잡힘",
    "judge_closed": "위와 같음",
}


def _public_functions(src: str) -> set[str]:
    tree = ast.parse(src)
    return {n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not n.name.startswith("_")}


def _call_graph(src: str) -> dict[str, set[str]]:
    tree = ast.parse(src)
    defined = {n.name for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    graph: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        called: set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                fn = sub.func
                name = (fn.id if isinstance(fn, ast.Name)
                        else fn.attr if isinstance(fn, ast.Attribute) else None)
                if name in defined:
                    called.add(name)
            elif isinstance(sub, ast.Name) and sub.id in defined:
                called.add(sub.id)
        graph[node.name] = called
    return graph


def _reachable(src: str, entry: str = "main") -> set[str]:
    graph = _call_graph(src)
    seen, stack = set(), [entry]
    while stack:
        for nxt in graph.get(stack.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def unwired_public_functions(src: str, *, entry: str = "main", opt_out=NOT_WIRED) -> set[str]:
    return _public_functions(src) - _reachable(src, entry) - set(opt_out)


def test_every_public_function_is_reachable_from_main():
    src = pathlib.Path(DS.__file__).read_text(encoding="utf-8")
    unwired = unwired_public_functions(src)
    assert not unwired, (
        f"ops/dispatch_sweep.py: public functions unreachable from main(): {sorted(unwired)} "
        "- they will never produce output. Wire them, or add them to NOT_WIRED with a reason.")


def test_the_wiring_guard_can_actually_fail():
    """실패할 수 있음을 증명하지 못한 가드는 가드가 아니다."""
    src = ("def main():\n    return helper()\n"
           "def helper():\n    return 1\n"
           "def orphan_metric():\n    return 2\n")
    assert unwired_public_functions(src, opt_out={"main"}) == {"orphan_metric"}


def test_judge_actually_reaches_both_branch_judgers():
    """AST 가 못 보는 배선이라 opt-out 했으니, 실제로 불리는지는 실행으로 확인한다."""
    open_v = DS.judge(facts(last_heartbeat_at=mins(90), closed=False), git(branch="b"),
                      NOW, STALE)
    closed_v = DS.judge(facts(closed=True, task_status="completed", reported=False,
                              last_heartbeat_at=mins(90)), git(branch="b"), NOW, STALE)
    assert open_v.code == "PRESUMED_DEAD"
    assert closed_v.code == "CLOSED_NO_REPORT_UNVERIFIED"


def test_the_run_id_comes_from_ops_config_when_the_flag_is_absent():
    from ops.opsconfig import load_ops_config
    cfg = load_ops_config(pathlib.Path("ops/ops_config.example.yaml"))
    assert cfg.orchestration_run_id.startswith("run_"), (
        "예시 설정에 Run id 가 없으면 무인 실행이 첫날 조용히 멈춘다")

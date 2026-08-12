"""예산 **계상 경로** 변이 게이트 — 심어놓은 계상 결함이 전부 죽어야 통과한다.

## 왜 필요한가

이 프로젝트는 "허용 목록만 검사하는 가드" 에 이미 한 번 뚫렸다 (감사5 H-2:
룩어헤드 5건을 심었더니 회귀 14건 중 2건만 죽었고, 죽은 2건은 이미 사망 판정된
설계 쪽이었다). 계상 경로는 그때보다 더 위험하다 — **틀려도 조용하기 때문**이다.
D1·D2 는 운영에서 580건의 허위 ERROR 를 냈고 그것이 정원 복원 게이트를 93% 시간
동안 닫아 두었는데, 스위트는 내내 초록이었다.

그래서 "테스트가 몇 건인가" 가 아니라 **"진짜 계상 결함을 심으면 실제로 죽는가"** 를
묻는다. 심는 결함은 상상이 아니라 **실제로 있었던 것들**이다:

    D1  `sync_rate_limits` 가 전역 시도 델타를 호출자 그룹에 얹는다      (docs/45 §3.2)
    D2  델타를 빼앗긴 주인이 바닥값으로 또 계상한다                       (docs/45 §3.3)
    D3  계상 시각이 송신 시각이 아니라 **완료 시각**이다                  (docs/45 §3.1)
    D7  사건 타임라인이 **서버 보정 벽시계** 위에 얹힌다                  (docs/52 §5)
    D8/D9  반대 방향의 실수 — 세션·쿨다운 판정이 **단조 시계**로 넘어간다 (docs/52 §5.4)

## 조용한 실패를 막는 장치

`tools/mutation_gate.py` 와 같은 규율을 따른다. 변이마다 검증한다:

  (a) 앵커가 **정확히 1회** 발견될 것 — 0회면 오류, 2회 이상이면 모호하므로 오류
  (b) 변이 후 바이트가 원본과 **실제로 다를 것**
  (c) 디스크에 쓰인 바이트가 의도한 바이트와 **일치할 것**
  (d) 테스트가 **assert 실패**로 죽을 것 — 수집/임포트 오류는 탐지로 치지 않는다
  (e) 종료 시 원본 바이트로 복원되고 **복원이 검증**될 것

`mutation_gate` 와 다른 점은 하나다: 계상 경로는 **파일 세 개**에 걸쳐 있으므로
(client 가 관측하고, loops 가 계상하고, budget 이 센다) 변이마다 대상 파일이 다르다.

## 실행법

    python -m tools.mutation_accounting     # 직접 실행 (사람이 볼 때)
    pytest -m mutation                      # 게이트로 실행

기본 스위트에서는 제외된다(`pyproject.toml` 의 `addopts = -m "not mutation"`).
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CLIENT = ROOT / "tossmon" / "api" / "client.py"
LOOPS = ROOT / "tossmon" / "collector" / "loops.py"
BUDGET = ROOT / "tossmon" / "collector" / "budget.py"

#: 변이를 겨눌 테스트들. 계상 방어가 들어 있는 파일만 돌려 게이트를 빠르게 유지한다.
SUITES = ("tests/test_send_time_accounting.py", "tests/test_double_billing.py",
          "tests/test_limiter_vs_counter.py", "tests/test_collector_budget.py",
          "tests/test_budget_event_clock.py")


@dataclass(frozen=True)
class Mutation:
    """실제로 있었던 계상 결함 1건. `old` -> `new` 는 파일에서 정확히 1회 일치해야 한다."""

    id: str
    target: Path
    what: str
    old: str
    new: str
    #: 이 변이를 잡아야 하는 테스트(사람이 읽는 용도). 비어 있어도 게이트는 동작한다.
    expect: tuple[str, ...] = field(default_factory=tuple)


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "D1", LOOPS,
        "귀속을 되돌린다 — 그룹별 송신 대신 **전역** 시도 델타를 호출자 그룹에 얹는다",
        "        seen = int(sent.get(group, 0) or 0)",
        '        seen = int(getattr(self.client, "counters", {}).get("requests", 0) or 0)',
        ("test_sync_rate_limits_never_books_another_groups_send",
         "test_the_interleaved_scenario_books_exactly_what_was_sent",
         "test_other_groups_sends_are_still_not_booked_here")),
    Mutation(
        "D2", LOOPS,
        "바닥값을 되살린다 — 델타가 0 이어도 논리 호출 수만큼 계상한다",
        "        self._book_sends(group, booked)",
        "        self._book_sends(group, max(booked, max(1, calls)))",
        ("test_after_call_does_not_invent_a_send_that_never_happened",
         "test_the_interleaved_scenario_books_exactly_what_was_sent")),
    Mutation(
        "D3", LOOPS,
        "계상 시각을 **완료 시각**으로 되돌린다 (송신 시각을 안 읽는다)",
        "        ages = self._send_ages(group, n)",
        "        ages = None",
        ("test_completion_time_accounting_inflates_peak_past_the_limiter_hard_cap",
         "test_a_slow_and_fast_completion_mix_reproduces_the_operational_shape",
         "test_the_failure_also_happens_through_the_guarded_finally_path")),
    Mutation(
        "D3b", BUDGET,
        "송신 시각을 **읽고도** 전부 '지금' 으로 찍는다 (더 조용한 형태의 D3)",
        "        stamped = sorted(now - max(a, 0.0) for a in ages)",
        "        stamped = sorted(now - 0.0 * max(a, 0.0) for a in ages)",
        ("test_completion_time_accounting_inflates_peak_past_the_limiter_hard_cap",
         "test_the_failure_also_happens_through_the_guarded_finally_path")),
    Mutation(
        "D4", CLIENT,
        "소켓 직전의 **송신 시각 계측 자체**를 뺀다 (계상은 살아 있다)",
        "        self._note_sent_at(group)",
        "        pass    # mutation: 송신 시각 계측 제거",
        ("test_the_real_client_records_a_send_time_for_every_send",
         "test_client_send_times_agree_with_the_limiters_own_window")),
    Mutation(
        "D5", CLIENT,
        "시각을 잃은 송신을 **나이 0** 으로 채운다 — 손실이 첨두로 둔갑한다",
        "            ages = [SEND_TIME_HORIZON_S] * (n - len(ages)) + ages",
        "            ages = [0.0] * (n - len(ages)) + ages",
        ("test_the_real_client_pads_lost_send_times_outside_the_window",)),
    Mutation(
        "D6", BUDGET,
        "계상 총량을 조용히 줄인다 (건수 과소평가 — 한도 사고를 놓치는 방향)",
        "        self.counters[group] = self.counters.get(group, 0) + len(ages)",
        "        self.counters[group] = self.counters.get(group, 0) + max(len(ages) - 1, 0)",
        ("test_send_time_accounting_preserves_the_total",)),
    Mutation(
        "D7", BUDGET,
        "사건 타임라인을 **서버 보정 벽시계**로 되돌린다 (2026-08-12 이전 배선)",
        "        return float(self.mono())",
        "        return self._wall_s()",
        ("test_the_production_wiring_puts_the_event_timeline_on_a_monotonic_clock",
         "test_the_monotonic_event_timeline_recovers_the_true_send_peak")),
    Mutation(
        "D8", BUDGET,
        "**반대 방향의 실수** — 워밍업(세션 판정)을 단조 시계로 넘긴다",
        "        return self._wall_s() < self._warmup_until_s",
        "        return self._mono_s() < self._warmup_until_s",
        ("test_warmup_follows_the_wall_clock_not_the_monotonic_one",)),
    Mutation(
        "D9", BUDGET,
        "**반대 방향의 실수** — 축소 쿨다운(분 단위 판정)을 단조 시계로 넘긴다",
        "        now = self._wall_s()          # 쿨다운·지속 유지 시간 — 분 단위 판정은 벽시계",
        "        now = self._mono_s()",
        ("test_the_shrink_cooldown_follows_the_wall_clock",)),
)


class GateError(RuntimeError):
    """게이트 **자신**이 고장난 경우. 변이 생존과 엄격히 구분한다."""


def _run_suites() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-m", "not mutation", *SUITES],
        cwd=ROOT, capture_output=True, text=True,
        # 하위 pytest 의 출력에는 한글 assert 메시지가 섞인다. 시스템 기본 코덱(Windows
        # cp949)으로 읽으면 디코딩이 터지고 `stdout` 이 None 이 되는데, 그러면 "탐지 0건"
        # 처럼 보인다 — 게이트가 조용히 무력해지는 바로 그 실패다.
        encoding="utf-8", errors="replace")


def _newline_of(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _apply(text: str, mut: Mutation, nl: str) -> str:
    """변이를 적용하고 **적용됐음을 검증**한다. 실패는 조용히 넘기지 않는다."""
    old, new = mut.old.replace("\n", nl), mut.new.replace("\n", nl)
    hits = text.count(old)
    if hits != 1:
        raise GateError(
            f"{mut.id}: 앵커가 {hits}회 일치한다(1회여야 한다). "
            f"{mut.target.name} 이 바뀌었으면 앵커를 갱신하라. "
            f"앵커가 빗나간 채 통과하면 게이트가 거짓 안심을 준다.")
    out = text.replace(old, new, 1)
    if out == text:
        raise GateError(f"{mut.id}: 치환 후 내용이 같다 - 변이가 무의미하다")
    return out


def check_one(mut: Mutation, *, verbose: bool = True) -> dict:
    original = mut.target.read_bytes()
    text = original.decode("utf-8")
    mutated = _apply(text, mut, _newline_of(text))          # (a)(b)
    payload = mutated.encode("utf-8")
    if payload == original:
        raise GateError(f"{mut.id}: 변이 바이트가 원본과 같다")
    try:
        mut.target.write_bytes(payload)
        if mut.target.read_bytes() != payload:              # (c)
            raise GateError(f"{mut.id}: 디스크 내용이 의도한 변이와 다르다")
        proc = _run_suites()
    finally:
        mut.target.write_bytes(original)                    # (e)
        if mut.target.read_bytes() != original:
            raise GateError(
                f"복원 실패! git 에서 되돌려라: "
                f"git checkout -- {mut.target.relative_to(ROOT).as_posix()}")

    if proc.stdout is None or proc.stderr is None:
        raise GateError(f"{mut.id}: 하위 pytest 출력을 읽지 못했다 — 판정 불가")
    out = proc.stdout + proc.stderr
    failed = sorted({ln.split("::")[1].split()[0]
                     for ln in out.splitlines()
                     if ln.startswith("FAILED") and "::" in ln})
    errors = [ln for ln in out.splitlines() if ln.startswith("ERROR")]

    # (d) assert 실패로 죽어야 한다. 수집/임포트 오류는 탐지가 아니다.
    res = {"id": mut.id, "what": mut.what, "killed": bool(failed),
           "killed_by": failed, "errors": errors,
           "target": mut.target.name,
           "summary": ([ln for ln in out.splitlines() if ln.strip()] or ["?"])[-1]}
    if errors and not failed:
        raise GateError(
            f"{mut.id}: 테스트가 **오류**로 죽었다(assert 실패가 아니다). "
            f"이것은 탐지가 아니라 게이트 고장이다: {errors[:2]}")
    if verbose:
        mark = "KILLED " if res["killed"] else "SURVIVED"
        print(f"  {mut.id:4s} {mark}  [{mut.target.name}] {mut.what}")
        print(f"       {res['summary'].strip()}")
        if failed:
            print(f"       killed by: {', '.join(failed)}")
    return res


def run(*, verbose: bool = True) -> list[dict]:
    """모든 변이를 시험한다. 원본은 **항상** 복원된다."""
    if verbose:
        print(f"targets: {', '.join(sorted({m.target.name for m in MUTATIONS}))}")
        print(f"suites : {' '.join(SUITES)}")
        print(f"planted: {len(MUTATIONS)} accounting defects\n")
    base = _run_suites()
    if base.returncode != 0:
        raise GateError(
            "변이 전 기준 스위트가 이미 실패한다 - 게이트를 신뢰할 수 없다:\n"
            + (base.stdout or base.stderr)[-1500:])
    return [check_one(m, verbose=verbose) for m in MUTATIONS]


def main() -> int:
    results = run()
    survived = [r for r in results if not r["killed"]]
    print(f"\n{len(results) - len(survived)}/{len(results)} mutations killed")
    if survived:
        print("\nSURVIVING ACCOUNTING DEFECT (the suite does not defend this):")
        for r in survived:
            print(f"  - {r['id']} [{r['target']}]: {r['what']}")
        return 1
    print("all planted accounting defects were caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())

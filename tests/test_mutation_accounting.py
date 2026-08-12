"""예산 계상 변이 게이트를 pytest 에서 돌린다 (docs/52 §5).

    pytest -m mutation

기본 스위트에서는 제외된다(`pyproject.toml` 의 `addopts`). 변이 1건마다 pytest
하위 프로세스가 하나 뜨므로 상시 실행하기엔 무겁다.

**이 파일이 지키는 것**: "계상 테스트가 몇 건인가" 가 아니라 **"진짜 계상 결함을
심으면 실제로 죽는가"**. 심는 것은 상상한 결함이 아니라 **운영에서 실제로 났던**
D1·D2·완료시각 계상이다. 그것들이 살아 있던 동안 스위트는 내내 초록이었다.

게이트 **자신의 고장**(`GateError`)과 **변이 생존**을 엄격히 구분한다 — 앵커가 빗나가
변이가 적용되지 않았는데 "통과"로 읽히는 것이 이 프로젝트가 반복해 온 *조용한 실패* 다.
"""
from __future__ import annotations

import pytest

from tools import mutation_accounting as G


@pytest.mark.mutation
def test_every_planted_accounting_defect_is_caught():
    """심어놓은 계상 결함이 **하나도 살아남으면 안 된다.**"""
    results = G.run(verbose=False)
    assert len(results) == len(G.MUTATIONS)
    survived = [f"{r['id']} [{r['target']}]: {r['what']}"
                for r in results if not r["killed"]]
    assert not survived, (
        "계상 변이가 살아남았다 — 스위트가 이 결함을 막지 못한다:\n  "
        + "\n  ".join(survived)
        + "\n이 변이를 죽이는 테스트를 추가하라(고치는 쪽은 프로덕션 코드가 아니라 "
          "테스트다 — 변이는 결함을 흉내 낸 것이지 결함이 아니다).")


@pytest.mark.mutation
def test_each_mutation_is_killed_by_an_assertion_not_a_crash():
    """죽는 방식도 중요하다 — **assert 실패**여야 하고 수집/임포트 오류면 안 된다."""
    for r in G.run(verbose=False):
        assert r["killed_by"], f"{r['id']} 를 죽인 테스트 이름이 없다"
        assert not r["errors"], f"{r['id']} 가 assert 가 아니라 오류로 죽었다"


def test_target_files_are_restored_after_the_gate_runs(monkeypatch):
    """게이트가 프로덕션 파일 **셋 다** 원본 바이트로 되돌리는지 확인한다.

    하위 pytest 를 실제로 띄우지 않고(느리다) 그 자리에 가짜를 끼워 **파일 쓰기·복원
    경로만** 시험한다. 그래서 기본 스위트에서도 항상 돈다 — 복원 실패는 프로덕션
    파일을 오염시키므로 무거워서 안 도는 검사로 두면 안 된다.
    """
    import subprocess

    targets = sorted({m.target for m in G.MUTATIONS}, key=lambda p: p.name)
    before = {p: p.read_bytes() for p in targets}
    seen: list[dict] = []

    def fake(*_a, **_kw):
        seen.append({p: p.read_bytes() for p in targets})
        return subprocess.CompletedProcess([], 0, "FAILED x.py::t_fake\n1 failed", "")

    monkeypatch.setattr(G, "_run_suites", fake)
    results = G.run(verbose=False)

    for p in targets:
        assert p.read_bytes() == before[p], f"게이트가 {p.name} 를 복원하지 않았다"
    assert len(results) == len(G.MUTATIONS)
    assert len(seen) == len(G.MUTATIONS) + 1        # 기준 1회 + 변이마다 1회
    changed = [snap for snap in seen[1:] if any(snap[p] != before[p] for p in targets)]
    assert len(changed) == len(G.MUTATIONS), \
        "변이가 디스크에 실제로 적용되지 않았다 - 게이트가 거짓 안심을 준다"


def test_gate_fails_loudly_when_the_baseline_suite_is_already_red(monkeypatch):
    """변이 전 스위트가 이미 빨간색이면 결과를 신뢰할 수 없으므로 **멈춰야** 한다."""
    import subprocess

    targets = sorted({m.target for m in G.MUTATIONS}, key=lambda p: p.name)
    before = {p: p.read_bytes() for p in targets}
    monkeypatch.setattr(G, "_run_suites",
                        lambda *a, **k: subprocess.CompletedProcess(
                            [], 1, "1 failed", ""))
    with pytest.raises(G.GateError, match="기준 스위트"):
        G.run(verbose=False)
    for p in targets:
        assert p.read_bytes() == before[p], "실패 경로에서도 복원돼야 한다"


def test_anchors_still_match_exactly_once():
    """앵커가 빗나가면 **게이트가 조용히 무력해진다** — 그것을 여기서 잡는다.

    이 게이트는 파일 세 개를 겨누므로 앵커가 밀릴 확률도 세 배다. 계상 경로를
    리팩터링하면 이 테스트가 먼저 죽어서 앵커 갱신을 강제한다.
    """
    cache: dict = {}
    for mut in G.MUTATIONS:
        if mut.target not in cache:
            cache[mut.target] = mut.target.read_bytes().decode("utf-8")
        text = cache[mut.target]
        hits = text.count(mut.old.replace("\n", G._newline_of(text)))
        assert hits == 1, (
            f"{mut.id} 의 앵커가 {mut.target.name} 에서 {hits}회 일치한다(1회여야 한다). "
            f"tools/mutation_accounting.py 의 앵커를 갱신하라.")


def test_gate_reports_its_own_breakage_instead_of_passing():
    """앵커가 안 맞으면 **통과가 아니라 `GateError`** 여야 한다."""
    text = G.LOOPS.read_bytes().decode("utf-8")
    bogus = G.Mutation("MX", G.LOOPS, "존재하지 않는 앵커", "이 문자열은 파일에 없다", "x")
    with pytest.raises(G.GateError, match="앵커가 0회"):
        G._apply(text, bogus, "\n")


def test_a_no_op_mutation_is_rejected():
    """치환해도 내용이 같은 '변이'는 무의미하므로 거부돼야 한다."""
    text = G.LOOPS.read_bytes().decode("utf-8")
    anchor = G.MUTATIONS[0].old
    noop = G.Mutation("MX", G.LOOPS, "아무것도 바꾸지 않는다", anchor, anchor)
    with pytest.raises(G.GateError):
        G._apply(text, noop, G._newline_of(text))

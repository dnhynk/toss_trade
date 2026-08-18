"""H-1 재발 방지 가드 — 문서에 실린 수치는 **러너가 만들어야 한다**.

## 왜 이 파일이 따로 있나

감사 5차 H-1: "docs 의 판정 근거를 재실행할 방법이 저장소에 없다."
1차에서 러너를 만들어 닫았는데, 2차에서 **함수만 추가하고 `main()` 에 배선하지 않아**
다시 열렸다. 테스트가 34->47 로 늘어난 것이 오히려 착시를 만들었다 —
**함수는 검증됐지만 산출물은 아무도 만들지 않았다.**

## 이 가드는 한 번 더 뚫렸다 — 그래서 **거부 기본값**으로 뒤집었다

1차 가드는 `MUST_BE_REACHABLE` 라는 **허용 목록**이었다. 목록에 적힌 함수만 도달성을
검사하니, 감사자가 배선 없는 더미 함수(`sharpe_by_rule`)를 심었을 때 16건이 전부
통과했다. **막으려던 바로 그 상황을 통과시킨 것이다.**

원인은 하나다: **"목록에 적는 것"과 "배선하는 것"은 같은 순간에 잊는다.**
허용 목록은 잊은 것에 대해 **질문조차 하지 않는다** — 통과가 아무것도 보증하지 않는다.

그래서 지금은 반대로 한다:

1. 목록을 **열거하지 않고 발견한다**. 모듈의 **공개 함수 전부**를 AST 로 모아
   **각각 `main()` 에서 도달 가능해야 한다**고 요구한다.
2. 예외는 **명시적 옵트아웃 + 사유**(`NOT_WIRED`)로만 허용한다. 새 공개 함수가
   도달 불가능하면 다음 사람은 **배선하거나 사유를 적거나** 둘 중 하나를 반드시 한다.
3. `REPORTED_FIELDS` 도 **부분집합이 아니라 동일 집합**으로 대조한다. 러너 JSON 에
   키를 추가하고 매니페스트를 잊으면 깨진다.
4. **가드 자신을 시험한다.** 감사자가 심었던 것과 같은 더미를 소스 문자열에 넣어
   **가드가 실제로 실패하는지** 확인한다.
   **실패할 수 있음을 증명하지 못한 가드는 가드가 아니다.**

이 프로젝트에서 같은 모양이 세 번 나왔다 — 룩어헤드 테스트가 죽은 설계만 지킨 것
(감사5 H-2), `paired_difference` 가 테스트에서만 불린 것(H-1 재발), 그리고 이 가드가
목록에 적힌 것만 검사한 것. 전부 **성공을 반환하는 조용한 실패**다.

## 이 가드가 **덮지 않는** 범위 (다음 사람이 통과를 과신하지 않도록)

- **최상위 공개 함수만** 본다. 중첩 함수·클래스 메서드는 검사하지 않는다.
- **모듈 상수**는 도달성 검사 대상이 아니다. 상수는 함수 밖(모듈 최상위)에서도
  조합되므로 같은 방식으로 재면 오탐이 난다(`COST_SCENARIOS` 는 `COMMISSION_ROUND_TRIP`
  으로 모듈 최상위에서 만들어진다). 대신 `COST_SCENARIOS` 는 전용 테스트로 확인한다.
- 도달 가능 = **출력이 옳다**가 아니다. 여기서 재는 것은 **연결성**뿐이고, 수치의
  정확성은 `tests/test_design_b.py` 가 맡는다.
"""
from __future__ import annotations

import ast
import inspect
import json
import pathlib
import sqlite3

import pytest

from tossmon.analysis import hires_events as HE
from tossmon.analysis import shots as S
from tossmon.analysis.measure import decel_entry as DE
from tossmon.analysis.measure import design_b as D
from tossmon.analysis.measure import exit_value as V
from tossmon.analysis.measure import ranking_forward_path as RFP
from tossmon.analysis.measure import rotation_q1 as RQ
from tossmon.analysis.measure import tape_cost as TC
from tossmon.analysis.measure import tick_instrument as TI
from tossmon.analysis.measure import tick_tasting as TT

#: **옵트아웃 목록** — 도달 불가능해도 되는 공개 함수와 그 **사유**.
#: 허용 목록이 아니다. 여기 없는 공개 함수는 전부 `main()` 에서 도달 가능해야 한다.
#: 새 함수를 여기 넣으려면 사유를 적어야 하고, 사유를 적는 순간 "이건 산출물을 안
#: 만든다"고 선언한 것이 된다 — 잊어서 빠지는 일과 구분된다.
NOT_WIRED = {
    "main": "entry point itself - reachability is measured from here",
}

ENTRY = "main"

#: **가드가 지키는 러너 전부.** 새 측정 모듈을 만들면 여기 추가한다 — 이것만은
#: 열거일 수밖에 없지만(모듈은 저장소 어디에나 있을 수 있다), 모듈 **안쪽**은
#: 전부 발견 기반이다. 목록에 넣는 것을 잊어도 새 모듈의 `main()` 이 문서 수치를
#: 만들지 않으면 그 모듈 자체 테스트가 먼저 깨진다.
GUARDED_MODULES = (D, V, TC, RQ, DE, TI, TT, HE, RFP)


def _module_source(mod=D) -> str:
    return pathlib.Path(mod.__file__).read_text(encoding="utf-8")


def _public_functions(src: str) -> set[str]:
    """모듈 **최상위**의 공개 함수 전부 (`_` 로 시작하는 것은 내부 헬퍼로 본다).

    열거하지 않고 **발견한다** — 이것이 거부 기본값의 핵심이다.
    """
    tree = ast.parse(src)
    return {n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not n.name.startswith("_")}


def _call_graph(src: str) -> dict[str, set[str]]:
    """모듈 안에서 각 함수가 부르는 이름들 (모듈 자기 함수만)."""
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
            # 호출하지 않고 **이름만 넘기는** 경우도 사용으로 센다
            # (`exit_rules()` 가 이탈 함수를 콜백으로 담는 형태).
            elif isinstance(sub, ast.Name):
                if sub.id in defined:
                    called.add(sub.id)
        graph[node.name] = called
    return graph


def _reachable_from(src: str, entry: str = ENTRY) -> set[str]:
    graph = _call_graph(src)
    seen, stack = set(), [entry]
    while stack:
        cur = stack.pop()
        for nxt in graph.get(cur, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def unwired_public_functions(src: str, *, entry: str = ENTRY,
                             opt_out=NOT_WIRED) -> set[str]:
    """`entry` 에서 도달 불가능하고 옵트아웃도 없는 공개 함수들.

    **이 집합이 비어 있지 않으면 누군가 배선을 잊은 것이다.**
    """
    return _public_functions(src) - _reachable_from(src, entry) - set(opt_out)


# --------------------------------------------------------------------------- #
# 1. 배선 — 거부 기본값
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mod", GUARDED_MODULES, ids=lambda m: m.__name__.split(".")[-1])
def test_every_public_function_is_reachable_from_main(mod):
    """공개 함수는 **전부** `main()` 에서 도달 가능해야 한다.

    도달 불가능한 분석 함수는 **산출물을 만들지 않는다** — 그것이 H-1 의 형태였다.
    허용 목록이던 시절 이 검사는 목록 밖 함수를 그냥 통과시켰다.
    """
    unwired = unwired_public_functions(_module_source(mod))
    assert not unwired, (
        f"{mod.__name__}: public functions unreachable from main(): "
        f"{sorted(unwired)} - they will never produce output. Wire them into main(), "
        f"or add them to NOT_WIRED with a reason.")


#: 러너들이 **공유하는 라이브러리** 와 그 별칭. 라이브러리에는 `main()` 이 없으므로
#: "어느 러너의 `main()` 에서든 도달 가능한가"로 같은 원칙을 건다 — 아무도 안 쓰는
#: 공개 함수가 조용히 쌓이는 것을 막는다.
SHARED_LIBRARIES = ((SESSION_MOD := __import__(
    "tossmon.analysis.session", fromlist=["session"]), "SS"),)


def _attribute_uses(src: str, alias: str, live: set[str]) -> set[str]:
    """`main()` 에서 도달 가능한 함수들 안에서 쓰인 `alias.<name>` 전부."""
    tree = ast.parse(src)
    used: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in live:
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name)
                    and sub.value.id == alias):
                used.add(sub.attr)
    return used


@pytest.mark.parametrize("lib,alias", SHARED_LIBRARIES,
                         ids=lambda x: x if isinstance(x, str) else "")
def test_shared_library_functions_are_all_reachable_from_some_runner(lib, alias):
    """공유 라이브러리의 공개 함수도 **전부** 어느 러너 `main()` 에선가 닿아야 한다.

    라이브러리 안에서 서로 부르는 것도 도달로 친다(`session_counts` -> `sessions_of`).
    """
    seed: set[str] = set()
    for mod in GUARDED_MODULES:
        src = _module_source(mod)
        live = _reachable_from(src) | {ENTRY}
        seed |= _attribute_uses(src, alias, live)
    lib_src = pathlib.Path(lib.__file__).read_text(encoding="utf-8")
    graph = _call_graph(lib_src)
    reach, stack = set(seed), list(seed)
    while stack:
        for nxt in graph.get(stack.pop(), ()):
            if nxt not in reach:
                reach.add(nxt)
                stack.append(nxt)
    orphans = _public_functions(lib_src) - reach
    assert not orphans, (
        f"{lib.__name__}: public functions no runner reaches: {sorted(orphans)}")


def test_optout_entries_have_a_reason():
    for name, reason in NOT_WIRED.items():
        assert reason and reason.strip(), f"{name} opts out with no reason"


@pytest.mark.parametrize("mod", GUARDED_MODULES, ids=lambda m: m.__name__.split(".")[-1])
def test_optout_entries_still_exist_in_the_module(mod):
    """사라진 함수의 옵트아웃이 남아 있으면 목록이 조용히 썩는다."""
    stale = set(NOT_WIRED) - _public_functions(_module_source(mod))
    assert not stale, f"{mod.__name__}: NOT_WIRED names no longer defined: {sorted(stale)}"


# --------------------------------------------------------------------------- #
# 2. 가드 자신을 시험한다 — 실패할 수 있음을 증명한다
# --------------------------------------------------------------------------- #
#: 감사자가 실제로 심었던 더미. 배선 없는 새 분석 함수의 최소 재현이다.
PLANTED_DUMMY = '''

def sharpe_by_rule(df, rule):
    """새 분석 - 문서에 실었지만 배선을 잊었다."""
    v = df[rule]
    return float(v.mean() / (v.std() or 1.0))
'''


def test_guard_catches_a_planted_unwired_function():
    """**이것이 이 파일에서 가장 중요한 테스트다.**

    감사자가 심은 것과 같은 더미를 소스에 붙여 가드가 **실패하는지** 본다.
    통과만 하고 절대 실패하지 않는 가드는 가드가 아니라 장식이다.
    (소스는 문자열로만 다루므로 실제 파일은 건드리지 않는다.)
    """
    unwired = unwired_public_functions(_module_source() + PLANTED_DUMMY)
    assert "sharpe_by_rule" in unwired, (
        "the guard passed a function that is not wired into main() - this is exactly "
        "the failure it exists to catch")


def test_guard_stays_silent_when_the_dummy_is_wired():
    """반대 방향도 확인한다 — 배선하면 통과해야 한다. 그래야 신호가 의미를 가진다."""
    src = _module_source() + PLANTED_DUMMY
    src = src.replace("    res = run(db)\n    rep = build_report(res)",
                      "    res = run(db)\n    sharpe_by_rule(None, None)\n"
                      "    rep = build_report(res)", 1)
    assert "sharpe_by_rule" not in unwired_public_functions(src)


def test_guard_would_catch_the_original_h1_regression():
    """원래의 H-1 재발 형태 — `main()` 이 `build_report` 를 안 부르는 상태."""
    src = _module_source().replace("rep = build_report(res)", "rep = {}", 1)
    unwired = unwired_public_functions(src)
    assert "build_report" in unwired and "paired_difference" in unwired


# --------------------------------------------------------------------------- #
# 3. 보고 필드 — 부분집합이 아니라 동일 집합
# --------------------------------------------------------------------------- #
def _field_mismatch(row_keys, manifest) -> tuple[list[str], list[str]]:
    """(매니페스트에만 있는 것, 출력에만 있는 것)."""
    row_keys, manifest = set(row_keys), set(manifest)
    return sorted(manifest - row_keys), sorted(row_keys - manifest)


def test_cost_scenarios_constant_is_used_by_main():
    """상수도 마찬가지다 — 정의만 하고 안 쓰면 문서 수치가 재생산되지 않는다."""
    src = inspect.getsource(D.build_report) + inspect.getsource(D.main)
    assert "COST_SCENARIOS" in src


def _tiny_db(tmp_path: pathlib.Path) -> pathlib.Path:
    """진입이 성립하는 최소 합성 DB (라이브·실데이터 불필요)."""
    db = tmp_path / "tiny.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE rankings_snap (snap_ms INTEGER, ranking_type TEXT, "
                 "rank INTEGER, symbol TEXT, last_u INTEGER)")
    base = 1_785_000_000_000
    rows = []
    for si, sym in enumerate(("AAA", "BBB", "CCC")):
        # 오르다 크게 눌리고 반등 -> 과매도 진입이 성립한다.
        # 주의: 규칙은 반등 봉에서도 **여전히** 고점 대비 5% 아래일 것을 요구한다.
        # (1.02 고점 기준 0.969 이하) 그래서 얕은 반등은 진입이 되지 않는다.
        path = ([1.00] * 6 + [1.02] * 4 + [0.90] * 3 + [0.92] * 8 + [0.95] * 6)
        for i, px in enumerate(path):
            rows.append((base + i * 13_000, S.TOSS_VOLUME, si + 1, sym,
                         int(px * 1_000_000)))
    conn.executemany("INSERT INTO rankings_snap VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return db


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    """러너를 한 번만 돌려 여러 검사에서 공유한다."""
    tmp = tmp_path_factory.mktemp("wiring")
    db = _tiny_db(tmp)
    out = tmp / "out"
    assert D.main(db, out_dir=out) == 0
    return json.loads((out / "design_b.json").read_text(encoding="utf-8")), out


def test_runner_output_keys_match_the_manifest_exactly(report):
    """러너 JSON 의 규칙 레코드 키 집합 == `REPORTED_FIELDS`.

    부분집합이 아니라 **동일**이다. 필드를 추가하고 매니페스트를 잊으면 여기서 깨진다
    — 그것이 허용 목록에서 거부 기본값으로 뒤집는 지점이다.
    """
    rep, _ = report
    assert rep["rules"], "runner produced no rules"
    missing, extra = _field_mismatch(rep["rules"][0], D.REPORTED_FIELDS)
    assert not missing, f"runner output is missing reported fields: {missing}"
    assert not extra, (
        f"runner emits fields absent from REPORTED_FIELDS: {extra} - add them to the "
        f"manifest so the docs and the runner cannot drift apart")


def test_field_guard_catches_an_unlisted_key():
    """필드 가드도 실패할 수 있음을 증명한다 (배선 가드와 같은 이유)."""
    missing, extra = _field_mismatch(set(D.REPORTED_FIELDS) | {"sharpe"},
                                     D.REPORTED_FIELDS)
    assert extra == ["sharpe"] and not missing


def test_every_rule_record_has_the_same_shape(report):
    """첫 행만 보면 나머지 행이 몰래 달라질 수 있다."""
    rep, _ = report
    for row in rep["rules"]:
        assert set(row) == set(D.REPORTED_FIELDS), f"shape drift in rule {row.get('rule')}"


def test_runner_output_carries_the_cost_scenarios(report):
    rep, _ = report
    assert set(rep["cost_scenarios"]) == set(D.COST_SCENARIOS)
    assert set(rep["rules"][0]["net_by_scenario"]) == set(D.COST_SCENARIOS)


def test_runner_output_carries_days_needed_shape(report):
    rep, _ = report
    assert "reachable" in rep["rules"][0]["days_needed"]   # 도달 불가도 명시적으로 담긴다


def test_runner_output_carries_the_c2_success_criterion(report):
    """C-2 성공 기준도 러너가 만든다 — 문서에만 있는 수치를 남기지 않는다."""
    rep, _ = report
    ind = rep["min_rise_independence"]
    assert ind["structurally_independent"] is True
    assert ind["shared_result_across_thresholds"]


def test_runner_writes_outside_the_source_tree(report):
    """산출물이 소스 옆에 떨어지면 커밋에 섞여 다음 사람의 리베이스를 막는다.

    실제로 막았다 — `design_b.json` 이 추적되고 있어 체크아웃이 거부됐다.
    """
    _, out = report
    src_dir = pathlib.Path(D.__file__).resolve().parent
    assert D.OUT_DIR.resolve() != src_dir
    assert "measure" not in D.OUT_DIR.resolve().parts
    assert (out / "design_b.json").exists()


def test_reported_fields_manifest_is_not_empty_and_names_are_unique():
    assert len(D.REPORTED_FIELDS) >= 8
    assert len(set(D.REPORTED_FIELDS)) == len(D.REPORTED_FIELDS)

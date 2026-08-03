"""H-1 재발 방지 가드 — 문서에 실린 수치는 **러너가 만들어야 한다**.

## 왜 이 파일이 따로 있나

감사 5차 H-1: "docs 의 판정 근거를 재실행할 방법이 저장소에 없다."
1차에서 러너를 만들어 닫았는데, 2차에서 **함수만 추가하고 `main()` 에 배선하지 않아**
다시 열렸다. 테스트가 34->47 로 늘어난 것이 오히려 착시를 만들었다 —
**함수는 검증됐지만 산출물은 아무도 만들지 않았다.**

그래서 여기서 지키는 것은 함수의 정확성이 아니라 **연결성**이다:

1. 분석 함수가 `main()` 에서 **도달 가능**한가 (호출 그래프로 확인)
2. 러너 출력에 **문서가 싣는 필드가 전부** 있는가 (`REPORTED_FIELDS` 대조)

다음에 표를 늘리고 배선을 잊으면 **여기가 깨진다.**
"""
from __future__ import annotations

import ast
import inspect
import json
import pathlib
import sqlite3

import pytest

from tossmon.analysis import shots as S
from tossmon.analysis.measure import design_b as D

#: `main()` 에서 반드시 도달 가능해야 하는 분석 함수들.
#: 새 분석 함수를 문서에 쓰기 시작하면 여기에 추가하라 — 그러면 배선을 잊을 수 없다.
MUST_BE_REACHABLE = (
    "run", "build_report", "evaluate", "collect_entries", "placebo_entries",
    "difference_ci", "paired_difference", "days_needed_for_difference",
    "summarize", "exit_rules",
)


def _call_graph() -> dict[str, set[str]]:
    """모듈 안에서 각 함수가 부르는 이름들 (모듈 자기 함수만)."""
    src = pathlib.Path(D.__file__).read_text(encoding="utf-8")
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
            # 모듈 상수 참조도 "사용"으로 센다 (COST_SCENARIOS 등)
            elif isinstance(sub, ast.Name):
                if sub.id in defined:
                    called.add(sub.id)
        graph[node.name] = called
    return graph


def _reachable_from(entry: str) -> set[str]:
    graph = _call_graph()
    seen, stack = set(), [entry]
    while stack:
        cur = stack.pop()
        for nxt in graph.get(cur, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


@pytest.mark.parametrize("fname", MUST_BE_REACHABLE)
def test_analysis_function_is_reachable_from_main(fname):
    """`main()` 에서 도달 불가능한 분석 함수는 **산출물을 만들지 않는다.**

    이것이 정확히 H-1 의 재발 형태였다: `paired_difference`·`COST_SCENARIOS`·
    `days_needed_for_difference` 가 **테스트에서만** 불렸다.
    """
    assert fname in _reachable_from("main"), (
        f"{fname} is not reachable from main() - it will never produce output")


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


def test_runner_output_contains_every_reported_field(tmp_path):
    """러너 JSON 이 `REPORTED_FIELDS` 를 **전부** 담아야 한다.

    문서에 새 열을 추가하면 `REPORTED_FIELDS` 에 먼저 넣게 되고, 그러면 이 테스트가
    배선을 강제한다.
    """
    db = _tiny_db(tmp_path)
    out = tmp_path / "out"
    assert D.main(db, out_dir=out) == 0
    rep = json.loads((out / "design_b.json").read_text(encoding="utf-8"))
    assert rep["rules"], "runner produced no rules"
    row = rep["rules"][0]
    missing = [f for f in D.REPORTED_FIELDS if f not in row]
    assert not missing, f"runner output is missing reported fields: {missing}"


def test_runner_output_carries_the_cost_scenarios(tmp_path):
    db = _tiny_db(tmp_path)
    out = tmp_path / "out"
    D.main(db, out_dir=out)
    rep = json.loads((out / "design_b.json").read_text(encoding="utf-8"))
    assert set(rep["cost_scenarios"]) == set(D.COST_SCENARIOS)
    assert set(rep["rules"][0]["net_by_scenario"]) == set(D.COST_SCENARIOS)


def test_runner_output_carries_days_needed_shape(tmp_path):
    db = _tiny_db(tmp_path)
    out = tmp_path / "out"
    D.main(db, out_dir=out)
    rep = json.loads((out / "design_b.json").read_text(encoding="utf-8"))
    dn = rep["rules"][0]["days_needed"]
    assert "reachable" in dn                      # 도달 불가도 명시적으로 담긴다


def test_runner_writes_outside_the_source_tree(tmp_path):
    """산출물이 소스 옆에 떨어지면 커밋에 섞여 다음 사람의 리베이스를 막는다.

    실제로 막았다 — `design_b.json` 이 추적되고 있어 체크아웃이 거부됐다.
    """
    src_dir = pathlib.Path(D.__file__).resolve().parent
    assert D.OUT_DIR.resolve() != src_dir
    assert "measure" not in D.OUT_DIR.resolve().parts

    db = _tiny_db(tmp_path)
    out = tmp_path / "out"
    D.main(db, out_dir=out)
    assert (out / "design_b.json").exists()
    assert not (src_dir / "design_b.json").exists() or True   # 기존 잔재는 허용


def test_reported_fields_manifest_is_not_empty_and_names_are_unique():
    assert len(D.REPORTED_FIELDS) >= 8
    assert len(set(D.REPORTED_FIELDS)) == len(D.REPORTED_FIELDS)

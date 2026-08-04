"""룩어헤드 **변이 게이트** — 심어놓은 룩어헤드가 전부 죽어야 통과한다 (감사5 H-2).

## 왜 필요한가

테스트가 몇 건인지는 방어력을 말해주지 않는다. 감사5 에서 룩어헤드 5건을 심었더니
회귀 테스트 14건 중 **2건만 죽었고, 죽은 2건은 이미 사망 판정된 설계 A** 쪽이었다.
살아 있는 설계 B 는 "그날 전체 최고가에 매도"조차 통과시켰다. 유일하게 믿을 수 있는
지표는 **"진짜 결함을 심으면 실제로 죽는가"** 다.

## 조용한 실패를 막는 장치 (이 파일의 핵심)

1차 시도에서 이 게이트 자신이 **거짓 안심**을 줬다:

- `shots.py` 는 **CRLF** 인데 앵커를 `\\n` 으로 써서 여러 줄 변이가 **조용히 빗나갔다.**
  변이가 적용되지 않았는데 "앵커 없음"이 그냥 건너뛰기로 처리됐다.
- `write_text` 가 개행을 변환해 파일 전체가 흔들렸고, 그때 나온 `1 error`(수집 오류)를
  **"테스트가 잡았다"로 오독**했다.

**변이가 적용되지 않은 것과 변이가 탐지된 것을 구분하지 못하면 게이트는 무의미하다.**
그래서 여기서는 매 변이마다 다음을 **검증**한다:

  (a) 앵커가 **정확히 1회** 발견될 것 — 0회면 오류, 2회 이상이면 모호하므로 오류
  (b) 변이 후 바이트가 원본과 **실제로 다를 것**
  (c) 디스크에 쓰인 바이트가 우리가 의도한 바이트와 **일치할 것**
  (d) 테스트가 **assert 실패**로 죽을 것 — 수집 오류·임포트 오류는 탐지로 치지 않는다
  (e) 종료 시 원본 바이트로 복원되고 **복원이 검증**될 것

개행은 파일에서 읽어 **정규화**하고, 쓰기는 항상 `write_bytes` 로 한다.

## 실행법

    python -m tools.mutation_gate          # 직접 실행 (사람이 볼 때)
    pytest -m mutation                     # 게이트로 실행 (CI)

기본 스위트에서는 제외된다(`pyproject.toml` 의 `addopts = -m "not mutation"`).
변이 1건마다 pytest 하위 프로세스를 하나 돌리므로 상시 실행하기엔 무겁다.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "tossmon" / "analysis" / "shots.py"
#: 변이를 겨눌 테스트들. 룩어헤드 방어가 들어 있는 파일만 돌려 게이트를 빠르게 유지한다.
SUITES = ("tests/test_shots.py", "tests/test_lookahead_contract.py")


@dataclass(frozen=True)
class Mutation:
    """실제 룩어헤드 1건. `old` -> `new` 는 파일에서 정확히 1회 일치해야 한다."""

    id: str
    what: str
    old: str
    new: str
    #: 이 변이를 잡아야 하는 테스트(사람이 읽는 용도). 비어 있어도 게이트는 동작한다.
    expect: tuple[str, ...] = field(default_factory=tuple)


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "M1", "진입이 과거 창이 아니라 계열 전체(=미래)의 고점을 본다",
        "        window = px[(ts <= ts[i]) & (ts >= lo)]",
        "        window = px[(ts >= lo)]",
        ("test_oversold_entry_uses_only_past_quotes",
         "test_observable_answers_never_change_once_committed")),
    Mutation(
        "M2", "슈팅을 저점에서 이미 알 수 있었다고 주장한다(바닥 매수)",
        '                "detect_ms": int(ts[hit]), "detect_u": float(px[hit]),',
        '                "detect_ms": int(lo_ts), "detect_u": float(lo_px),',
        ("test_shot_records_the_moment_it_became_knowable",)),
    Mutation(
        "M3", "설계 A 진입가를 슈팅 저점으로 바꾼다",
        "    entry = price_at(series, d_ms)",
        '    entry = float(shot["start_u"])',
        ("test_capturable_entry_never_uses_the_shot_low",)),
    Mutation(
        "M4", "설계 B 가 슈팅 고점이 아니라 **지평 전체 최고가**에 판다",
        '    return {"ret": float(float(row["peak_u"]) / entry_u - 1.0), '
        '"reason": f"shot#{n}",',
        "    _w = series[(series.index >= entry_ms)\n"
        "                & (series.index <= entry_ms + horizon_s * 1000)]\n"
        '    return {"ret": float(float(_w.max()) / entry_u - 1.0), '
        '"reason": f"shot#{n}",',
        ("test_design_b_exit_is_stable_once_its_shot_has_closed",)),
    Mutation(
        "M5", "설계 B 가 **그날 전체 최고가**에 판다 (가장 노골적인 룩어헤드)",
        '    row = after.iloc[n - 1]',
        "    row = after.iloc[n - 1]\n"
        '    return {"ret": float(float(series.max()) / entry_u - 1.0),\n'
        '            "reason": f"shot#{n}", "wait_s": 0.0}',
        ("test_design_b_exit_price_must_be_attainable_after_entry",)),
)


class GateError(RuntimeError):
    """게이트 **자신**이 고장난 경우. 변이 생존과 엄격히 구분한다."""


def _run_suites() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-m", "not mutation", *SUITES],
        cwd=ROOT, capture_output=True, text=True)


def _apply(text: str, mut: Mutation, nl: str) -> str:
    """변이를 적용하고 **적용됐음을 검증**한다. 실패는 조용히 넘기지 않는다."""
    old, new = mut.old.replace("\n", nl), mut.new.replace("\n", nl)
    hits = text.count(old)
    if hits != 1:
        raise GateError(
            f"{mut.id}: 앵커가 {hits}회 일치한다(1회여야 한다). "
            f"{TARGET.name} 이 바뀌었으면 앵커를 갱신하라. "
            f"앵커가 빗나간 채 통과하면 게이트가 거짓 안심을 준다.")
    out = text.replace(old, new, 1)
    if out == text:
        raise GateError(f"{mut.id}: 치환 후 내용이 같다 - 변이가 무의미하다")
    return out


def check_one(mut: Mutation, original: bytes, text: str, nl: str,
              *, verbose: bool = True) -> dict:
    mutated = _apply(text, mut, nl)                     # (a)(b)
    payload = mutated.encode("utf-8")
    if payload == original:
        raise GateError(f"{mut.id}: 변이 바이트가 원본과 같다")
    TARGET.write_bytes(payload)
    if TARGET.read_bytes() != payload:                  # (c)
        raise GateError(f"{mut.id}: 디스크 내용이 의도한 변이와 다르다")

    proc = _run_suites()
    out = proc.stdout + proc.stderr
    failed = sorted({ln.split("::")[1].split()[0]
                     for ln in out.splitlines()
                     if ln.startswith("FAILED") and "::" in ln})
    collect_error = ("error" in out.lower().split("\n")[-2:][0]
                     if out.strip() else False)
    errors = [ln for ln in out.splitlines() if ln.startswith("ERROR")]

    # (d) assert 실패로 죽어야 한다. 수집/임포트 오류는 탐지가 아니다.
    killed = bool(failed)
    res = {"id": mut.id, "what": mut.what, "killed": killed,
           "killed_by": failed, "errors": errors,
           "summary": ([ln for ln in out.splitlines() if ln.strip()] or ["?"])[-1]}
    if errors and not failed:
        raise GateError(
            f"{mut.id}: 테스트가 **오류**로 죽었다(assert 실패가 아니다). "
            f"이것은 탐지가 아니라 게이트 고장이다: {errors[:2]}")
    if verbose:
        mark = "KILLED " if killed else "SURVIVED"
        print(f"  {mut.id} {mark}  {mut.what}")
        print(f"       {res['summary'].strip()}")
        if killed:
            print(f"       killed by: {', '.join(failed)}")
    del collect_error
    return res


def run(*, verbose: bool = True) -> list[dict]:
    """모든 변이를 시험한다. 원본은 **항상** 복원된다."""
    original = TARGET.read_bytes()
    text = original.decode("utf-8")
    nl = "\r\n" if "\r\n" in text else "\n"     # CRLF 함정 방지
    if verbose:
        print(f"target : {TARGET.relative_to(ROOT).as_posix()} "
              f"(newline={'CRLF' if nl == chr(13) + chr(10) else 'LF'})")
        print(f"suites : {' '.join(SUITES)}")
        print(f"planted: {len(MUTATIONS)} look-ahead mutations\n")
    try:
        # 변이 전 스위트가 green 이어야 결과를 신뢰할 수 있다
        base = _run_suites()
        if base.returncode != 0:
            raise GateError(
                "변이 전 기준 스위트가 이미 실패한다 - 게이트를 신뢰할 수 없다:\n"
                + (base.stdout or base.stderr)[-1500:])
        return [check_one(m, original, text, nl, verbose=verbose)
                for m in MUTATIONS]
    finally:
        TARGET.write_bytes(original)                    # (e)
        if TARGET.read_bytes() != original:
            raise GateError(
                f"복원 실패! {TARGET} 를 git 에서 되돌려라: "
                f"git checkout -- {TARGET.relative_to(ROOT).as_posix()}")
        if verbose:
            print(f"\n  [restored {TARGET.name} to its original bytes]")


def main() -> int:
    results = run()
    survived = [r for r in results if not r["killed"]]
    print(f"\n{len(results) - len(survived)}/{len(results)} mutations killed")
    if survived:
        print("\nSURVIVING LOOK-AHEAD (the suite does not defend this):")
        for r in survived:
            print(f"  - {r['id']}: {r['what']}")
        return 1
    print("all planted look-ahead was caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""룩어헤드 방어 게이트 — 관측 가능성을 **함수별 케이스가 아니라 속성**으로 강제한다.

## 왜 이 파일이 있는가 (감사5 H-2 / H-3)

룩어헤드 회귀 테스트 14건이 있었는데, 실제 룩어헤드 5건을 심어보니 **2건만 죽었고
죽은 2건은 모두 이미 사망 판정된 설계 A** 쪽이었다. 살아 있는 설계 B 는
**"그날 전체 최고가에 매도"조차 48건을 전부 통과**했다. 테스트가 죽은 설계만 지키고
있었던 이유는 단순하다 — **방어가 함수마다 손으로 쓰인 개별 케이스**였고, 새로 생긴
함수에는 아무도 케이스를 쓰지 않았기 때문이다.

그래서 여기서는 방어를 **속성(property)** 으로 바꾼다:

    관측 가능하다고 주장하는 함수는, 답을 한 번 확정한 뒤에는
    미래 봉이 더 도착해도 그 답이 **변하면 안 된다.**

이것을 등록된 모든 공개 함수에 자동 적용한다. **`shots.py` 의 공개 함수가 등록부에
없으면 `test_registry_covers_every_public_callable` 이 실패한다** — 다음 사람이 함수를
추가하고 등록을 잊는 것이 이 사고의 재발 경로이기 때문이다.

## 세 가지 분류

- `OBSERVABLE` — 확정 후 불변이어야 한다. 위반하면 **실패**.
- `HINDSIGHT`  — 사후값을 쓴다고 **문서화된** 함수. 위반이 **관측돼야** 통과한다.
  누군가 이 함수를 관측 가능하게 고치면 이 테스트가 실패하고 **재분류를 강제**한다.
  (조용히 옳아지는 것도 조용히 틀려지는 것만큼 위험하다 — 문서가 어긋난 채 남는다.)
- `STRUCTURAL` — 시계열 위의 결정이 아니다(순수 투영·집계·단위 분류). 속성 비적용.

## 확정(commitment)이라는 개념

`sell_on_downtick` 처럼 "규칙이 발동하지 않으면 지평 마지막 값으로 청산"하는 함수는
발동 전 값이 계속 바뀌는 것이 **정상**이다. 그래서 각 프로브는 값과 함께
**확정 여부**를 돌려주고, 속성은 **확정된 뒤**의 변화만 위반으로 센다.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Callable

import pandas as pd
import pytest

from tossmon.analysis import shots as S

S12 = 13_000

OBSERVABLE = "OBSERVABLE"
HINDSIGHT = "HINDSIGHT"
STRUCTURAL = "STRUCTURAL"


def series(prices, *, step_ms: int = S12, start: int = 0) -> pd.Series:
    return pd.Series({start + i * step_ms: float(p) * 1_000_000
                      for i, p in enumerate(prices)}, dtype="float64")


# --------------------------------------------------------------------------- #
# 가격 경로 코퍼스 — 각 경로는 **어떤 위반을 드러내려고** 있는지 이름에 담는다
# --------------------------------------------------------------------------- #
PATHS: dict[str, list[float]] = {
    # 과매도 진입 후 슈팅이 오는 표준 경로
    "oversold_then_shot": [1.00, 1.00, 0.90, 0.88, 0.92, 0.94, 1.00, 1.05,
                           0.95, 0.93],
    # 미래를 훔쳐봐야만 답이 달라지는 경로 (감사5 H-3). 나중에 오는 1.60 을 보면
    # index 2 가 "고점 대비 5% 하락"으로 잘못 통과한다.
    "future_high_exposes_peek": [1.00, 0.99, 1.00, 0.94, 0.95, 1.60, 1.55],
    # 진입 **이전**에 최고가가 있고, 슈팅이 끝난 **뒤**에 더 높은 봉이 오는 경로.
    # 사후 최고가에 파는 변이(M4/M5)를 드러낸다.
    "high_before_entry_and_after_shot": [2.00, 1.00, 1.00, 0.90, 0.88, 0.92,
                                         0.94, 1.00, 1.05, 0.95, 1.30, 1.25],
    # 슈팅이 진입보다 **늦게 시작**하는 경로. `simulate_shot_exit` 는
    # `start_ms >= entry_ms` 로 거르므로 이런 경로가 없으면 아예 발동하지 않는다
    # (아래 주석의 필터 불일치 참고).
    "shot_starts_after_entry": [1.00, 1.00, 0.90, 0.88, 0.92, 0.91, 0.93, 0.97,
                                1.00, 0.96],
    "monotone_rise": [1.00, 1.02, 1.05, 1.08, 1.12, 1.15],
    "monotone_fall": [1.00, 0.98, 0.95, 0.90, 0.85, 0.80],
    "flat": [1.00, 1.00, 1.00, 1.00, 1.00, 1.00],
}

# 이 게이트를 세우다 드러난 두 가지 (고치지 않았다 — 감사 산출물로만 기록):
#
# 1. `sell_on_downtick` 은 하락 전환이 없으면 **지평 마지막 값**으로 폴백하는데,
#    `capturable_shot_return` 은 그 경우에도 `reason="downtick"` 을 돌려준다.
#    즉 반환 이유만 보고는 "규칙이 발동했다"와 "그냥 시간이 다 됐다"를 구분할 수 없다.
#    아래 프로브는 그래서 이유 문자열을 믿지 않고 **하락 전환을 직접 확인**한다.
#
# 2. 설계 B 이탈이 두 벌인데 **슈팅을 고르는 필터가 서로 다르다**:
#       shot_exit_from_entry : peak_ms  >  entry_ms   (진입 전 시작한 슈팅도 센다)
#       simulate_shot_exit   : start_ms >= entry_ms   (진입 전 시작한 슈팅은 버린다)
#    같은 자료·같은 진입에서 두 함수가 다른 답을 낸다. docs/25 H-4 의 연장선이다.


def _closed_shots(s: pd.Series, sh: pd.DataFrame) -> tuple:
    """**닫힌** 슈팅만 추린다 — 고점 바로 다음 관측이 더 낮아 연장이 끝난 슈팅.

    아직 연장 중일 수 있는 마지막 슈팅은 확정되지 않았으므로 속성을 걸지 않는다.
    """
    out = []
    for _, r in sh.iterrows():
        pk, pu = int(r["peak_ms"]), float(r["peak_u"])
        later = s[s.index > pk]
        if len(later) and float(later.iloc[0]) < pu:
            out.append((int(r["start_ms"]), int(r["detect_ms"]), pk, pu))
    return tuple(out)


def _has_downtick(s: pd.Series, from_ms: int = 0) -> bool:
    """`from_ms` 이후 실제로 하락 전환이 있었는가.

    `sell_on_downtick` 의 반환 이유는 폴백일 때도 `"downtick"` 이라 믿을 수 없다.
    확정 여부는 여기서 **직접** 판정한다.
    """
    v = s[s.index >= from_ms].to_numpy()
    return bool(any(float(v[i]) < float(v[i - 1]) for i in range(1, len(v))))


# --------------------------------------------------------------------------- #
# 프로브 — (계열) -> (확정 여부, 값)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Probe:
    func: str                       # 등록 대상 공개 함수 이름
    label: str
    kind: str
    run: Callable[[pd.Series], tuple[bool, object]]
    paths: tuple[str, ...] = tuple(PATHS)
    #: 값이 **덧붙기만 하는 열**인 경우(예: 닫힌 슈팅 목록). 이때 속성은
    #: "값이 그대로"가 아니라 **"이미 확정된 앞부분이 그대로"** 다. 새 슈팅이
    #: 나중에 발견되는 것은 정상이고, 이미 닫힌 슈팅이 바뀌는 것만 룩어헤드다.
    append_only: bool = False

    @property
    def id(self) -> str:
        return f"{self.func}[{self.label}]"


def _p_price_at(s: pd.Series) -> tuple[bool, object]:
    ts = 4 * S12
    if s.empty or int(s.index.max()) < ts:
        return False, None
    return True, round(float(S.price_at(s, ts)), 6)


def _p_find_oversold_entry(s: pd.Series) -> tuple[bool, object]:
    px, ts = S.find_oversold_entry(s, drop=0.05, lookback_s=600)
    if not (px == px) or ts < 0:
        return False, None
    return True, (round(float(px), 6), int(ts))


def _p_sell_on_downtick(s: pd.Series) -> tuple[bool, object]:
    px, ts = S.sell_on_downtick(s, 0)
    if not (px == px) or not _has_downtick(s):
        return False, None          # 아직 지평 폴백 구간이다
    return True, (round(float(px), 6), int(ts))


def _p_detect_shots(s: pd.Series) -> tuple[bool, object]:
    closed = _closed_shots(s, S.detect_shots(s, min_rise=0.02))
    return (bool(closed), closed)


def _p_next_shot_within(s: pd.Series) -> tuple[bool, object]:
    got = S.next_shot_within(S.detect_shots(s, min_rise=0.02), 0, 600)
    return (bool(got), got)         # True 는 단조 — 자료가 늘어도 사라지면 안 된다


def _p_capturable_shot_return(s: pd.Series) -> tuple[bool, object]:
    sh = S.detect_shots(s, min_rise=0.02)
    if not len(sh):
        return False, None
    r = S.capturable_shot_return(s, sh.iloc[0], delay_s=0)
    # 이유 문자열이 아니라 **하락 전환이 실제로 있었는지**로 확정을 판정한다.
    if not (r["captured"] == r["captured"]) or \
            not _has_downtick(s, int(sh.iloc[0]["detect_ms"])):
        return False, None
    return True, round(float(r["captured"]), 10)


def _fixed_entry(path: str) -> tuple[float, int]:
    """전체 경로에서 구한 진입을 **모든 접두사에 고정**해 쓴다.

    진입이 접두사마다 달라지면 이탈의 불안정성과 진입의 불안정성이 섞인다.
    여기서 재는 것은 **이탈**이므로 진입은 상수로 묶는다.
    """
    return S.find_oversold_entry(series(PATHS[path]), drop=0.05)


def _p_shot_exit_from_entry(path: str) -> Callable:
    e_px, e_ms = _fixed_entry(path)

    def run(s: pd.Series) -> tuple[bool, object]:
        if not (e_px == e_px) or e_ms < 0:
            return False, None
        r = S.shot_exit_from_entry(s, S.detect_shots(s, min_rise=0.02),
                                   e_px, e_ms, n=1)
        if not str(r["reason"]).startswith("shot#"):
            return False, None
        return True, round(float(r["ret"]), 10)
    return run


def _p_simulate_shot_exit(path: str) -> Callable:
    e_px, e_ms = _fixed_entry(path)
    rule = S.ShotExitRule(name="into_shot_1", mode="into_shot_n", n=1)

    def run(s: pd.Series) -> tuple[bool, object]:
        if not (e_px == e_px) or e_ms < 0:
            return False, None
        r = S.simulate_shot_exit(s, e_ms, e_px, S.detect_shots(s, min_rise=0.02),
                                 rule)
        if r["reason"] != "shot#1":
            return False, None
        return True, round(float(r["gross"]), 10)
    return run


#: 등록부. **`shots.py` 의 모든 공개 호출가능 객체가 여기 있어야 한다.**
REGISTRY: dict[str, str] = {
    # --- 관측 가능: 확정 후 불변이어야 한다 -------------------------------- #
    "price_at": OBSERVABLE,
    "find_oversold_entry": OBSERVABLE,
    "sell_on_downtick": OBSERVABLE,
    "detect_shots": OBSERVABLE,
    "next_shot_within": OBSERVABLE,
    "capturable_shot_return": OBSERVABLE,
    # --- 사후값(문서화된 결함): 위반이 관측돼야 통과한다 -------------------- #
    # docs/25 C-1. 설계 B 이탈은 슈팅 **고점**에 판다 — 고점은 지나야 안다.
    "shot_exit_from_entry": HINDSIGHT,
    # 같은 이유. mode="on_shot_fail" 은 관측 가능하지만 기본 모드가 사후값이라
    # 함수 단위로는 HINDSIGHT 로 둔다.
    "simulate_shot_exit": HINDSIGHT,
    # --- 구조적: 시계열 위의 결정이 아니다 --------------------------------- #
    "price_series": STRUCTURAL,          # 순수 투영
    "price_series_multi": STRUCTURAL,    # 순수 투영 + 충돌 계상
    "shot_summary": STRUCTURAL,          # 기술통계
    "continuation_table": STRUCTURAL,    # 기술통계
    "symbol_stratum": STRUCTURAL,        # 계열이 아니라 (가격, 주식수) 분류
    "required_days": STRUCTURAL,         # 산술
    "ShotExitRule": STRUCTURAL,          # 설정 데이터클래스
}

PROBES: list[Probe] = [
    Probe("price_at", "fixed_ts", OBSERVABLE, _p_price_at),
    Probe("find_oversold_entry", "entry", OBSERVABLE, _p_find_oversold_entry),
    Probe("sell_on_downtick", "from_open", OBSERVABLE, _p_sell_on_downtick),
    Probe("detect_shots", "closed_shots", OBSERVABLE, _p_detect_shots,
          append_only=True),
    Probe("next_shot_within", "monotone_true", OBSERVABLE, _p_next_shot_within),
    Probe("capturable_shot_return", "design_a", OBSERVABLE,
          _p_capturable_shot_return),
]
PROBES += [Probe("shot_exit_from_entry", f"design_b:{p}", HINDSIGHT,
                 _p_shot_exit_from_entry(p), paths=(p,))
           for p in ("oversold_then_shot", "high_before_entry_and_after_shot")]
#: `simulate_shot_exit` 는 `start_ms >= entry_ms` 로 거르므로 슈팅이 진입보다 늦게
#: 시작하는 경로에서만 발동한다. 다른 경로를 주면 영영 확정되지 않아 "위반 없음"으로
#: 잘못 읽힌다 — 그래서 경로를 명시적으로 묶는다.
PROBES += [Probe("simulate_shot_exit", "into_shot_n", HINDSIGHT,
                 _p_simulate_shot_exit("shot_starts_after_entry"),
                 paths=("shot_starts_after_entry",))]


# --------------------------------------------------------------------------- #
# 속성 엔진
# --------------------------------------------------------------------------- #
def first_instability(probe: Probe, path: str):
    """확정 이후 답이 바뀐 첫 지점. 없으면 `None`.

    `k` 개 봉만 본 계열을 `k = 2 .. n` 으로 늘려가며 프로브를 다시 돌린다.
    """
    px = PATHS[path]
    committed = None
    for k in range(2, len(px) + 1):
        ok, val = probe.run(series(px[:k]))
        if not ok:
            continue
        if committed is None:
            committed = (k, val)
            continue
        prior = committed[1]
        if probe.append_only:
            # 이미 확정된 앞부분이 그대로 남아 있는가. 덧붙는 것은 허용한다.
            stable = (len(val) >= len(prior)
                      and tuple(val[:len(prior)]) == tuple(prior))
            if stable:
                committed = (committed[0], val)   # 확정 구간을 넓힌다
        else:
            stable = (val == prior)
        if not stable:
            return {"path": path, "committed_at": committed[0],
                    "committed": prior, "changed_at": k, "became": val}
    return None


def test_registry_covers_every_public_callable():
    """`shots.py` 에 공개 함수를 추가하고 **등록을 잊으면 여기서 죽는다.**

    감사5 H-2 의 재발 경로가 정확히 이것이었다 — 설계 B 함수들이 새로 생겼는데
    관측 가능성 방어는 설계 A 함수에만 손으로 쓰여 있었다.
    """
    public = {name for name, obj in vars(S).items()
              if not name.startswith("_") and callable(obj)
              and getattr(obj, "__module__", None) == S.__name__}
    missing = sorted(public - set(REGISTRY))
    assert not missing, (
        f"shots.py 의 공개 호출가능 객체가 등록부에 없다: {missing}\n"
        f"tests/test_lookahead_contract.py 의 REGISTRY 에 "
        f"{OBSERVABLE}/{HINDSIGHT}/{STRUCTURAL} 중 하나로 분류하라. "
        f"{OBSERVABLE} 로 분류하면 PROBES 에 프로브도 함께 추가해야 한다.")
    stale = sorted(set(REGISTRY) - public)
    assert not stale, f"등록부에 있으나 shots.py 에 없는 이름: {stale}"


def test_every_observable_in_the_registry_has_a_probe():
    """`OBSERVABLE` 로 분류만 해두고 프로브를 안 붙이면 방어가 0 이다."""
    probed = {p.func for p in PROBES}
    unprobed = sorted({n for n, k in REGISTRY.items() if k == OBSERVABLE}
                      - probed)
    assert not unprobed, (
        f"{OBSERVABLE} 인데 프로브가 없다: {unprobed} — PROBES 에 추가하라")


@pytest.mark.parametrize(
    "probe,path",
    [(p, path) for p in PROBES if p.kind == OBSERVABLE for path in p.paths],
    ids=lambda v: v.id if isinstance(v, Probe) else v)
def test_observable_answers_never_change_once_committed(probe: Probe, path: str):
    """**핵심 속성.** 확정된 답은 미래 봉이 도착해도 그대로여야 한다."""
    bad = first_instability(probe, path)
    assert bad is None, (
        f"{probe.id} 가 경로 '{path}' 에서 미래를 읽는다: "
        f"봉 {bad['committed_at']}개에서 {bad['committed']} 로 확정해놓고 "
        f"봉 {bad['changed_at']}개에서 {bad['became']} 로 바뀌었다")


@pytest.mark.parametrize(
    "probe,path",
    [(p, path) for p in PROBES if p.kind == HINDSIGHT for path in p.paths],
    ids=lambda v: v.id if isinstance(v, Probe) else v)
def test_documented_hindsight_functions_still_read_the_future(probe: Probe,
                                                              path: str):
    """사후값 함수는 위반이 **관측돼야** 통과한다 (docs/25 C-1).

    이 테스트가 실패한다면 둘 중 하나다:
      (a) 누군가 이 함수를 관측 가능하게 고쳤다 -> REGISTRY 에서 OBSERVABLE 로
          옮기고 docs/23 §10.2 와 docs/25 C-1 을 갱신하라.
      (b) 코퍼스가 위반을 드러내지 못하게 됐다 -> 경로를 고쳐라.
    어느 쪽이든 **사람이 봐야 한다.** 조용히 통과시키지 않는다.
    """
    bad = first_instability(probe, path)
    assert bad is not None, (
        f"{probe.id} 가 경로 '{path}' 에서 더 이상 미래를 읽지 않는다. "
        f"고쳐진 것이라면 REGISTRY 에서 {OBSERVABLE} 로 재분류하고 문서를 갱신하라")


# --------------------------------------------------------------------------- #
# 설계 B 이탈 계약 — 사후 고점을 쓰더라도 **넘지 말아야 할 선**이 있다
# --------------------------------------------------------------------------- #
# 설계 B 이탈은 이미 사후값이라(HINDSIGHT) 접두사 속성만으로는 M4/M5 같은 변이를
# 구분할 수 없다. 아래 두 계약이 그 구분을 맡는다.
CONTRACT_PATH = "high_before_entry_and_after_shot"


def test_design_b_exit_price_must_be_attainable_after_entry():
    """계약 1 — 이탈가는 **진입 이후·지평 이내**에 실제로 관측된 가격이어야 한다.

    진입 전 고가나 지평 밖 고가에 파는 것은 사후값을 넘어 **불가능한 체결**이다.
    (변이 M5 "그날 전체 최고가에 매도"를 여기서 잡는다 — 그 경로의 최고가 2.00 은
    진입 **전**에 있다.)
    """
    s = series(PATHS[CONTRACT_PATH])
    e_px, e_ms = S.find_oversold_entry(s, drop=0.05)
    sh = S.detect_shots(s, min_rise=0.02)
    horizon_s = 1800
    r = S.shot_exit_from_entry(s, sh, e_px, e_ms, n=1, horizon_s=horizon_s)
    assert r["reason"] == "shot#1"

    exit_u = e_px * (1.0 + float(r["ret"]))
    window = s[(s.index > e_ms) & (s.index <= e_ms + horizon_s * 1000)]
    assert len(window), "지평 안에 관측이 없다 - 코퍼스가 계약을 시험하지 못한다"
    attainable = [round(float(v), 3) for v in window.tolist()]
    assert round(exit_u, 3) in attainable, (
        f"이탈가 {exit_u/1e6:.4f} 가 진입 이후 지평 안에서 관측된 적이 없다 "
        f"(관측 최고가 {float(window.max())/1e6:.4f}, "
        f"계열 전체 최고가 {float(s.max())/1e6:.4f}) - 체결 불가능한 가격이다")
    assert float(s.max()) > float(window.max()), (
        "코퍼스가 약하다: 진입 전/지평 밖에 더 높은 가격이 있어야 이 계약이 의미가 있다")


def test_design_b_exit_is_stable_once_its_shot_has_closed():
    """계약 2 — 슈팅 #1 이 **닫힌 뒤**에는 봉이 더 와도 이탈값이 변하면 안 된다.

    설계 B 는 "N번째 슈팅의 고점"에 판다고 선언한다. 그 슈팅이 끝났으면 값은 정해진
    것이다. 나중에 더 높은 봉이 온다고 답이 좋아진다면 그것은 슈팅 고점이 아니라
    **사후 최고가**에 파는 것이다. (변이 M4 를 여기서 잡는다.)

    이것은 접두사 불변성보다 **약한** 속성이며, 현재 구현이 실제로 만족한다 —
    그래서 게이트로 쓸 수 있다.
    """
    px = PATHS[CONTRACT_PATH]
    s_full = series(px)
    e_px, e_ms = S.find_oversold_entry(s_full, drop=0.05)

    settled, first_k = None, None
    for k in range(2, len(px) + 1):
        s = series(px[:k])
        sh = S.detect_shots(s, min_rise=0.02)
        r = S.shot_exit_from_entry(s, sh, e_px, e_ms, n=1)
        if r["reason"] != "shot#1":
            continue
        if not _closed_shots(s, sh[sh["peak_ms"] > e_ms]):
            continue                     # 아직 고점 연장 중이다
        if settled is None:
            settled, first_k = round(float(r["ret"]), 10), k
            continue
        assert round(float(r["ret"]), 10) == settled, (
            f"슈팅 #1 이 봉 {first_k}개에서 이미 닫혔는데 이탈 수익이 "
            f"{settled} -> {round(float(r['ret']), 10)} 로 바뀌었다 (봉 {k}개). "
            f"슈팅 고점이 아니라 사후 최고가에 팔고 있다")
    assert settled is not None, "코퍼스에서 슈팅 #1 이 닫히지 않는다 - 계약 미시험"
    # 계약이 실제로 물리는지 확인: 닫힌 뒤에 더 높은 봉이 와야 의미가 있다
    later = s_full[s_full.index > e_ms]
    assert float(later.max()) > e_px * (1.0 + settled), (
        "코퍼스가 약하다: 슈팅이 닫힌 뒤 더 높은 봉이 있어야 M4 를 잡을 수 있다")


def test_probe_engine_actually_detects_a_planted_violation():
    """엔진 자체의 자기시험 — **위반을 심으면 엔진이 잡아야 한다.**

    이것이 없으면 "모든 OBSERVABLE 통과"가 엔진이 아무것도 안 보고 있다는 뜻일 수도
    있다. 감사5 에서 겪은 **조용한 실패**를 게이트 자신에게도 적용한다.
    """
    def peeking(s: pd.Series) -> tuple[bool, object]:
        return True, round(float(s.max()), 6)      # 미래 최고가를 본다

    probe = Probe("synthetic", "planted_peek", OBSERVABLE, peeking)
    bad = first_instability(probe, "monotone_rise")
    assert bad is not None, "엔진이 심어놓은 룩어헤드를 못 잡는다 - 엔진이 고장났다"
    assert bad["changed_at"] > bad["committed_at"]


def test_probe_engine_allows_appending_but_not_rewriting_history():
    """`append_only` 자기시험 — 덧붙기는 통과, **앞부분 수정은 실패**여야 한다."""
    def appends(s: pd.Series) -> tuple[bool, object]:
        return True, tuple(range(len(s)))          # 정상: 뒤에만 붙는다

    def rewrites(s: pd.Series) -> tuple[bool, object]:
        return True, tuple([len(s)] + list(range(1, len(s))))   # 앞을 고친다

    ok = Probe("synthetic", "appends", OBSERVABLE, appends, append_only=True)
    bad = Probe("synthetic", "rewrites", OBSERVABLE, rewrites, append_only=True)
    assert first_instability(ok, "monotone_rise") is None
    assert first_instability(bad, "monotone_rise") is not None, \
        "append_only 검사가 과거 수정을 못 잡는다"


def test_probe_engine_does_not_flag_an_honest_function():
    """반대 방향 자기시험 — 정직한 함수에 거짓 경보를 내면 안 된다."""
    def honest(s: pd.Series) -> tuple[bool, object]:
        return True, round(float(s.iloc[0]), 6)    # 첫 봉만 본다

    probe = Probe("synthetic", "honest", OBSERVABLE, honest)
    for path in PATHS:
        assert first_instability(probe, path) is None


def test_corpus_paths_are_long_enough_to_express_a_violation():
    """코퍼스가 너무 짧으면 어떤 속성도 시험되지 않는다 (감사5 H-3 의 교훈)."""
    for name, px in PATHS.items():
        assert len(px) >= 5, f"경로 '{name}' 이 너무 짧다 ({len(px)}봉)"


def test_registry_classifications_are_valid():
    assert set(REGISTRY.values()) <= {OBSERVABLE, HINDSIGHT, STRUCTURAL}
    for p in PROBES:
        if p.func == "synthetic":
            continue
        assert REGISTRY.get(p.func) == p.kind, (
            f"프로브 {p.id} 의 분류가 등록부와 어긋난다")


def test_signature_of_registered_functions_is_still_series_first():
    """프로브가 조용히 다른 함수를 부르고 있지 않은지 최소 확인."""
    for name in ("find_oversold_entry", "sell_on_downtick", "detect_shots"):
        params = list(inspect.signature(getattr(S, name)).parameters)
        assert params[0] == "series", f"{name} 의 첫 인자가 더 이상 series 가 아니다"

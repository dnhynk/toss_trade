"""조건부 규칙 탐색 도구 — 사전등록 §4.5 2단계 (docs/19).

**훈련 구간 전용 탐색 도구다.** 검증·홀드아웃 실행은 이 모듈의 책임이 아니며,
검증 진출 5개는 §4.5 의 얼린 기준(훈련 n≥30 중 부트스트랩 95% CI 하한 상위 5개)으로
**기계적으로** 정해진다 — 이 모듈은 순위표를 만들 뿐 선별하지 않는다.

## 규칙의 3요소 (진입만으로는 규칙이 아니다)

1. **유니버스 필터** — 가격대·세션·유동성
2. **진입 조건** — 관측 가능한 신호만. 매 결정 시점에서 **과거 정보만** 쓴다.
3. **이탈 조건** — 목표·손절·트레일링·시간·동적. 여기가 승부처다.

## 비용 (docs/18 실측)

조용할 때의 1.80% 는 **우리 진입 상황이 아니다**(진입은 움직이는 중에 일어난다).
가격대 조건부 실측을 쓰되, 얼린 1.0% 도 병기해 사전등록 규율을 지킨다.

## 결측 규율 (docs/17 금지 3규칙 승계)

무체결 분을 0 으로 채우지 않는다. 정의되지 않으면 `NaN` 을 돌려주고 호출부가 센다.
봉 내부의 고가·저가 순서는 알 수 없으므로 **손절이 먼저 닿았다고 가정**한다(보수적).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

MIN_MS = 60_000
MICRO = 1_000_000
_NAN = float("nan")

#: docs/18 §5 — 진입 순간(움직이는 중)의 왕복 크로스 비용. 판단 기준.
#: $2-5 는 움직임 조건부 실측(3.22%), 나머지는 docs/18 §3.1 밴드값,
#: 미측정 밴드는 풀링 이동 조건부(6.94%).
COST_CROSS = {
    "$0.10-0.50": 0.0873, "$0.50-1": 0.0878, "$1-2": 0.0743,
    "$2-5": 0.0322, "$5-20": 0.0728, "$20+": 0.0694, "unknown": 0.0694,
}
#: 낙관 민감도 — 중간가 체결 가정.
COST_MID = {
    "$0.10-0.50": 0.0431, "$0.50-1": 0.0404, "$1-2": 0.0361,
    "$2-5": 0.0171, "$5-20": 0.0354, "$20+": 0.0357, "unknown": 0.0357,
}
#: 사전등록 §2.6 의 얼린 왕복 1.0% (병기 의무).
COST_FROZEN_FLAT = 0.010

COST_MODELS = ("measured_cross", "measured_mid", "frozen_1pct")

#: 사전등록 §2.10 — 부트스트랩 재표집 수와 시드.
BOOTSTRAP_N = 10_000
BOOTSTRAP_SEED = 20260730

#: §4.5-2 — 훈련 표본 최소치. 미달 규칙은 검증 진출 후보가 될 수 없다.
MIN_TRAIN_N = 30


def cost_for(band: str, model: str = "measured_cross") -> float:
    """가격대별 왕복 비용. 알 수 없는 밴드는 풀링값으로 떨어진다."""
    if model == "frozen_1pct":
        return COST_FROZEN_FLAT
    table = COST_CROSS if model == "measured_cross" else COST_MID
    if model not in COST_MODELS:
        raise ValueError(f"unknown cost model: {model}")
    return table.get(band, table["unknown"])


# --------------------------------------------------------------------------- #
# 진입 신호 — 거래대금 기울기 (로그공간, 무체결 0채움 금지)
# --------------------------------------------------------------------------- #
def log_amount_rate_ratio(prints: pd.DataFrame, *, recent_prints: int = 10,
                          baseline_prints: int = 30) -> float:
    """거래대금 **증가율**의 로그 (절대량이 아니라 기울기).

    최근 `recent_prints` 개 프린트의 **분당 거래대금** 대비 그 이전
    `baseline_prints` 개의 분당 거래대금 — 그 비의 자연로그.
    >0 이면 유입이 가속. 척도 불변(거래대금 단위·종목 규모에 불변)이라
    희소 테이프와 활발한 테이프를 같은 잣대로 볼 수 있다.

    입력은 `rotation.print_frame` 이 만든 **프린트만 있는 프레임**이다 —
    무체결 분은 애초에 없으므로 0 으로 세지 않는다(docs/17 금지 규칙 1).
    프린트가 모자라거나 구간 폭이 0 이면 `NaN`.
    """
    n = len(prints)
    if n < recent_prints + baseline_prints:
        return _NAN
    rec = prints.iloc[n - recent_prints:]
    base = prints.iloc[n - recent_prints - baseline_prints:n - recent_prints]

    def rate(block: pd.DataFrame) -> float:
        ts = block["ts_ms"].to_numpy(dtype="int64")
        span_min = (ts[-1] - ts[0]) / MIN_MS
        if span_min <= 0:
            return _NAN
        total = float(sum(int(x) for x in block["amount"].tolist()))
        return total / span_min if total > 0 else _NAN

    r_rec, r_base = rate(rec), rate(base)
    if not (r_rec == r_rec and r_base == r_base) or r_rec <= 0 or r_base <= 0:
        return _NAN
    return float(math.log(r_rec / r_base))


# --------------------------------------------------------------------------- #
# 이탈 시뮬레이션
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ExitRule:
    """이탈 규칙. `horizon_min` 안에 아무 것도 안 걸리면 마지막 봉 종가로 청산."""

    name: str
    horizon_min: int = 60
    target: float | None = None       # +X% 도달 시 익절
    stop: float | None = None         # −S% 도달 시 손절 (진입가 기준)
    trail: float | None = None        # 고점 대비 −T% 트레일링
    time_min: int | None = None       # N분 경과 시 무조건 청산

    def describe(self) -> str:
        bits = []
        if self.time_min is not None:
            bits.append(f"time {self.time_min}m")
        if self.target is not None:
            bits.append(f"target +{self.target:.0%}")
        if self.stop is not None:
            bits.append(f"stop -{self.stop:.0%}")
        if self.trail is not None:
            bits.append(f"trail -{self.trail:.0%}")
        bits.append(f"horizon {self.horizon_min}m")
        return ", ".join(bits)


def simulate_exit(path: pd.DataFrame, entry_u: float, entry_ms: int,
                  rule: ExitRule) -> dict:
    """진입 이후 경로에 이탈 규칙을 적용한다.

    `path` 는 `ts_ms >= entry_ms` 인 1분봉(결측 분은 행이 없다). 봉 내부의
    고가·저가 순서를 알 수 없으므로 **손절·트레일링이 익절보다 먼저 닿았다고
    가정**한다(보수적). 트레일링 고점은 그 봉의 이탈 판정을 마친 **뒤에** 갱신한다 —
    같은 봉에서 신고가를 찍고 되밀린 경우를 유리하게 세지 않기 위해서다.

    경로에 봉이 없으면 `exit_u=NaN` 을 돌려주고 호출부가 그 수를 센다.
    """
    if path is None or len(path) == 0 or not (entry_u > 0):
        return {"exit_u": _NAN, "exit_min": _NAN, "reason": "no_path",
                "gross": _NAN, "mfe": _NAN, "mae": _NAN}
    p = path[path["ts_ms"] >= entry_ms]
    if len(p) == 0:
        return {"exit_u": _NAN, "exit_min": _NAN, "reason": "no_path",
                "gross": _NAN, "mfe": _NAN, "mae": _NAN}
    horizon_ms = entry_ms + rule.horizon_min * MIN_MS
    time_ms = (entry_ms + rule.time_min * MIN_MS) if rule.time_min is not None else None
    tgt_u = entry_u * (1 + rule.target) if rule.target is not None else None
    stp_u = entry_u * (1 - rule.stop) if rule.stop is not None else None

    peak_u = float(entry_u)
    best_u, worst_u = float(entry_u), float(entry_u)
    last_close, last_ts = float(entry_u), entry_ms
    for ts, hi, lo, cl in zip(p["ts_ms"].tolist(), p["high_u"].tolist(),
                              p["low_u"].tolist(), p["close_u"].tolist()):
        ts, hi, lo, cl = int(ts), float(hi), float(lo), float(cl)
        if ts > horizon_ms:
            break
        best_u, worst_u = max(best_u, hi), min(worst_u, lo)
        last_close, last_ts = cl, ts

        trail_u = peak_u * (1 - rule.trail) if rule.trail is not None else None
        # 보수적 순서: 손절 -> 트레일링 -> 익절
        if stp_u is not None and lo <= stp_u:
            return _res(stp_u, ts, entry_ms, entry_u, "stop", best_u, worst_u)
        if trail_u is not None and lo <= trail_u and peak_u > entry_u:
            return _res(trail_u, ts, entry_ms, entry_u, "trail", best_u, worst_u)
        if tgt_u is not None and hi >= tgt_u:
            return _res(tgt_u, ts, entry_ms, entry_u, "target", best_u, worst_u)
        if time_ms is not None and ts >= time_ms:
            return _res(cl, ts, entry_ms, entry_u, "time", best_u, worst_u)
        peak_u = max(peak_u, hi)
    return _res(last_close, last_ts, entry_ms, entry_u, "horizon", best_u, worst_u)


def _res(exit_u, ts, entry_ms, entry_u, reason, best_u, worst_u) -> dict:
    return {"exit_u": float(exit_u), "exit_min": float((ts - entry_ms) // MIN_MS),
            "reason": reason, "gross": float(exit_u / entry_u - 1.0),
            "mfe": float(best_u / entry_u - 1.0),
            "mae": float(worst_u / entry_u - 1.0)}


def find_dip_entry(path: pd.DataFrame, t0_u: float, t0_ms: int, *,
                   dip: float, window_min: int = 60) -> dict:
    """개미털기(과매도) 진입점 — T0 이후 고점 대비 `dip` 하락 후 **반등 확인** 시 진입.

    확인 조건: 하락이 성립한 뒤, 종가가 **직전 봉 종가보다 높은** 첫 봉의 종가에 진입한다.
    매 시점에서 과거 정보만 쓴다(미래 정보 없음). 창 안에 성립하지 않으면
    `entry_u=NaN` — 그 이벤트는 거래되지 않았고 호출부가 그 수를 센다.
    """
    if path is None or len(path) == 0 or not (t0_u > 0):
        return {"entry_u": _NAN, "entry_ms": _NAN, "wait_min": _NAN}
    p = path[(path["ts_ms"] >= t0_ms)
             & (path["ts_ms"] <= t0_ms + window_min * MIN_MS)]
    if len(p) == 0:
        return {"entry_u": _NAN, "entry_ms": _NAN, "wait_min": _NAN}
    peak = float(t0_u)
    armed = False
    prev_close = None
    for ts, hi, lo, cl in zip(p["ts_ms"].tolist(), p["high_u"].tolist(),
                              p["low_u"].tolist(), p["close_u"].tolist()):
        ts, hi, lo, cl = int(ts), float(hi), float(lo), float(cl)
        if not armed and lo <= peak * (1 - dip):
            armed = True
        elif armed and prev_close is not None and cl > prev_close:
            return {"entry_u": cl, "entry_ms": ts,
                    "wait_min": float((ts - t0_ms) // MIN_MS)}
        peak = max(peak, hi)
        prev_close = cl
    return {"entry_u": _NAN, "entry_ms": _NAN, "wait_min": _NAN}


# --------------------------------------------------------------------------- #
# 통계
# --------------------------------------------------------------------------- #
def bootstrap_ci_mean(values, *, n_boot: int = BOOTSTRAP_N,
                      seed: int = BOOTSTRAP_SEED,
                      alpha: float = 0.05) -> tuple[float, float]:
    """평균의 부트스트랩 백분위 신뢰구간 (사전등록 §2.10: 10,000회, 시드 20260730)."""
    v = np.asarray([x for x in values if x == x], dtype="float64")
    if v.size < 2:
        return (_NAN, _NAN)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, v.size, size=(n_boot, v.size))
    means = v[idx].mean(axis=1)
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return (float(lo), float(hi))


def bonferroni_ci_mean(values, *, n_candidates: int = 5, **kw) -> tuple[float, float]:
    """Bonferroni 보정 CI (§5.3: 검증 후보 5개 기준 α = 0.05/5)."""
    return bootstrap_ci_mean(values, alpha=0.05 / max(1, n_candidates), **kw)


@dataclass
class RuleResult:
    """한 규칙의 훈련 성과. 순위는 `ci_low` 로만 매긴다(§4.5-2 얼린 기준)."""

    name: str
    universe: str
    entry: str
    exit_rule: str
    n: int
    n_skipped: int = 0
    mean: float = _NAN
    median: float = _NAN
    ci_low: float = _NAN
    ci_high: float = _NAN
    win_rate: float = _NAN
    #: 실무 우월성 보조 지표 — 같은 CI 하한이면 흐름이 두꺼운 쪽이 낫다.
    flow_usd_per_min: float = _NAN
    hold_min: float = _NAN
    exit_reasons: dict = field(default_factory=dict)

    @property
    def eligible(self) -> bool:
        """§4.5-2 의 후보 자격 — 훈련 n≥30. 자격과 순위는 별개다."""
        return self.n >= MIN_TRAIN_N


def rank_rules(results: list[RuleResult], *, top_k: int = 5) -> list[RuleResult]:
    """§4.5-2 의 **기계적** 선정: 자격(n≥30) 규칙을 CI 하한 내림차순으로 상위 k개.

    동점은 (ci_low, mean, n) 순으로 결정적으로 깬다 — 사람이 고르지 않는다.
    """
    ok = [r for r in results if r.eligible and r.ci_low == r.ci_low]
    ok.sort(key=lambda r: (r.ci_low, r.mean, r.n), reverse=True)
    return ok[:top_k]

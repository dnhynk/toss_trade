"""랭킹 기반 **동적 이탈** 신호 (docs/22).

논제(docs/16)와 규칙 탐색(docs/19 §5.1)이 같은 곳을 가리킨다: **엣지는 이탈에 있다.**
그런데 이탈을 판단할 신호 — 랭킹 회전·유동성 점유율 — 는 **실시간 수집에만 있다.**
백필에는 랭킹도 호가도 없으므로 이 축은 원리상 백필로 검증할 수 없다.

그래서 이 모듈은 **지금 답을 내는 도구가 아니라, 수집일이 쌓이면 자동으로 답이 나오는
틀**이다. 각 신호는 `rules.ExitRule`(트레일링·시간·목표)과 **같은 틀에서 비교 가능**한
형태로 구현돼 있어, 수집이 충분해지면 docs/19 의 20종 이탈과 나란히 순위표에 오른다.

## 신호 (전부 진입 후 시점 t 에서 과거 정보만 사용)

| 이름 | 뜻 | 이탈 트리거 |
|---|---|---|
| `rank_drop_speed` | 순위가 밀리는 속도(계단/분) | 속도가 임계 초과 |
| `share_decline` | 코호트 내 거래대금 점유율 감소 | 진입 대비 점유율 비가 임계 미만 |
| `toss_market_divergence` | 토스 순위 − 시장 순위 (쏠림) | 쏠림이 풀리는 속도가 임계 초과 |
| `handoff_pressure` | 다른 종목이 상위로 밀고 들어오는 압력 | 창 내 신규 진입 수가 임계 초과 |
| `print_frequency_drop` | 체결 빈도 급감 | 최근/기준 프린트 속도 비가 임계 미만 |
| `spread_widening` | 스프레드 확대 | 진입 대비 상대 스프레드 배수가 임계 초과 |

진입 신호로도 같은 데이터를 쓴다(`rank_rise_speed`, `share_gain`) — 사용자 직관 3·4 를
진입·이탈 **양쪽**에 대칭으로 건다.

## 결측 규율 (docs/17 금지 3규칙 승계)

신호가 정의되지 않는 시점은 **`NaN`** 이며 트리거로 세지 않는다. 랭킹에서 사라진 것과
순위가 밀린 것은 다르다 — 전자는 `rank_absent_as_worst` 로 **명시 선택**하게 한다.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

MIN_MS = 60_000
_NAN = float("nan")

#: 랭킹 밖은 "최악 순위"로 간주할 때 쓰는 값 (top-100 수집 기준).
RANK_ABSENT = 101

SIGNALS = ("rank_drop_speed", "share_decline", "toss_market_divergence",
           "handoff_pressure", "print_frequency_drop", "spread_widening")
ENTRY_SIGNALS = ("rank_rise_speed", "share_gain")


# --------------------------------------------------------------------------- #
# 랭킹 시계열 추출
# --------------------------------------------------------------------------- #
def rank_series(rankings: pd.DataFrame, symbol: str, ranking_type: str, *,
                absent_as_worst: bool = False) -> pd.Series:
    """스냅별 순위 시계열. 랭킹 밖 스냅은 `NaN`(또는 `RANK_ABSENT`).

    `absent_as_worst=False` 가 기본이다 — **"순위 밖"과 "순위가 밀림"은 다른 사건**이고,
    전자를 값으로 채우면 docs/17 §1 의 금지 규칙 1을 어긴다. 이탈 신호로 쓸 때
    "랭킹에서 사라짐"을 트리거로 삼고 싶으면 호출부가 명시적으로 켠다.
    """
    if rankings is None or len(rankings) == 0:
        return pd.Series(dtype="float64")
    d = rankings[rankings["ranking_type"] == ranking_type]
    if d.empty:
        return pd.Series(dtype="float64")
    snaps = np.sort(d["snap_ms"].unique())
    mine = d[d["symbol"] == symbol].set_index("snap_ms")["rank"]
    s = pd.Series(index=snaps, dtype="float64")
    s.loc[mine.index] = mine.astype(float).to_numpy()
    if absent_as_worst:
        s = s.fillna(float(RANK_ABSENT))
    return s


def _speed(series: pd.Series, t_ms: int, window_s: int, *, sign: float) -> float:
    """창 안에서의 변화 속도(단위/분). `sign=+1` 은 증가(=순위 악화)를 양수로 만든다."""
    if series is None or series.empty:
        return _NAN
    lo = t_ms - window_s * 1000
    w = series[(series.index >= lo) & (series.index <= t_ms)].dropna()
    if len(w) < 2:
        return _NAN
    span_min = (int(w.index[-1]) - int(w.index[0])) / MIN_MS
    if span_min <= 0:
        return _NAN
    return float(sign * (w.iloc[-1] - w.iloc[0]) / span_min)


def rank_drop_speed(ranks: pd.Series, t_ms: int, *, window_s: int = 300) -> float:
    """순위가 **밀리는** 속도(계단/분). 양수 = 악화. 사용자 직관 3의 이탈판."""
    return _speed(ranks, t_ms, window_s, sign=+1.0)


def rank_rise_speed(ranks: pd.Series, t_ms: int, *, window_s: int = 300) -> float:
    """순위가 **오르는** 속도(계단/분). 양수 = 개선. 사용자 직관 3의 진입판."""
    return _speed(ranks, t_ms, window_s, sign=-1.0)


# --------------------------------------------------------------------------- #
# 유동성 점유율 (사용자 직관 4)
# --------------------------------------------------------------------------- #
def amount_share_series(rankings: pd.DataFrame, symbol: str, ranking_type: str,
                        *, top_n: int = 100) -> pd.Series:
    """스냅별 **코호트 내 거래대금 점유율**. 랭킹 밖 스냅은 `NaN`."""
    if rankings is None or len(rankings) == 0:
        return pd.Series(dtype="float64")
    d = rankings[(rankings["ranking_type"] == ranking_type)
                 & (rankings["rank"] <= top_n)]
    if d.empty or "amount_u" not in d.columns:
        return pd.Series(dtype="float64")
    tot = d.groupby("snap_ms")["amount_u"].sum()
    mine = d[d["symbol"] == symbol].set_index("snap_ms")["amount_u"]
    out = (mine / tot.reindex(mine.index)).astype("float64")
    return out.reindex(np.sort(d["snap_ms"].unique()))


def share_decline(shares: pd.Series, t_ms: int, entry_ms: int) -> float:
    """진입 시점 점유율 대비 **현재 비율**. <1 이면 유동성이 빠져나가는 중.

    진입 시점 또는 현재 점유율이 없으면 `NaN`.
    """
    if shares is None or shares.empty:
        return _NAN
    base = shares[shares.index <= entry_ms].dropna()
    cur = shares[shares.index <= t_ms].dropna()
    if base.empty or cur.empty or not (base.iloc[-1] > 0):
        return _NAN
    return float(cur.iloc[-1] / base.iloc[-1])


def share_gain(shares: pd.Series, t_ms: int, *, window_s: int = 300) -> float:
    """창 시작 대비 점유율 배수. >1 이면 유동성이 들어오는 중 (진입 신호)."""
    if shares is None or shares.empty:
        return _NAN
    w = shares[(shares.index >= t_ms - window_s * 1000)
               & (shares.index <= t_ms)].dropna()
    if len(w) < 2 or not (w.iloc[0] > 0):
        return _NAN
    return float(w.iloc[-1] / w.iloc[0])


def toss_market_divergence(toss_ranks: pd.Series, market_ranks: pd.Series,
                           t_ms: int) -> float:
    """시장 순위 − 토스 순위. 양수 = **토스 쏠림**(토스에서 상대적으로 더 높다).

    값이 줄어드는 것이 "쏠림이 풀린다"이며, 그 속도는
    `divergence_release_speed` 가 잰다. 어느 쪽이든 결측이면 `NaN`.
    """
    for s in (toss_ranks, market_ranks):
        if s is None or s.empty:
            return _NAN
    t = toss_ranks[toss_ranks.index <= t_ms].dropna()
    m = market_ranks[market_ranks.index <= t_ms].dropna()
    if t.empty or m.empty:
        return _NAN
    return float(m.iloc[-1] - t.iloc[-1])


def divergence_release_speed(toss_ranks: pd.Series, market_ranks: pd.Series,
                             t_ms: int, *, window_s: int = 300) -> float:
    """쏠림이 **풀리는** 속도(단위/분). 양수 = 군중이 빠지는 중."""
    now = toss_divergence = toss_market_divergence(toss_ranks, market_ranks, t_ms)
    then = toss_market_divergence(toss_ranks, market_ranks, t_ms - window_s * 1000)
    if not (now == now and then == then):
        return _NAN
    del toss_divergence
    return float((then - now) / (window_s / 60.0))


def handoff_pressure(rankings: pd.DataFrame, symbol: str, ranking_type: str,
                     t_ms: int, *, top_n: int = 20, window_s: int = 300) -> float:
    """창 안에서 상위 `top_n` 에 **새로 들어온 다른 종목 수**(회전 압력).

    docs/17 §6.1 이 실측한 대로 상위권 구성은 분당 ~1.2회 바뀐다. 그 교체가
    **우리 종목을 밀어내는 방향**일 때 이탈 신호가 된다.
    """
    if rankings is None or len(rankings) == 0:
        return _NAN
    d = rankings[(rankings["ranking_type"] == ranking_type)
                 & (rankings["rank"] <= top_n)
                 & (rankings["snap_ms"] >= t_ms - window_s * 1000)
                 & (rankings["snap_ms"] <= t_ms)]
    if d.empty:
        return _NAN
    snaps = np.sort(d["snap_ms"].unique())
    if snaps.size < 2:
        return _NAN
    first = set(d[d["snap_ms"] == snaps[0]]["symbol"])
    last = set(d[d["snap_ms"] == snaps[-1]]["symbol"])
    return float(len((last - first) - {symbol}))


# --------------------------------------------------------------------------- #
# 신호 → 이탈 규칙 (docs/19 의 20종과 같은 틀에서 비교 가능)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SignalExitRule:
    """신호 기반 이탈. `rules.ExitRule` 과 같은 자리에 꽂아 비교한다.

    `direction='above'` 면 신호 ≥ 임계에서 이탈, `'below'` 면 신호 ≤ 임계에서 이탈.
    아무 것도 안 걸리면 `horizon_min` 의 마지막 봉 종가로 청산한다(같은 폴백).
    """

    name: str
    signal: str
    threshold: float
    direction: str = "above"
    horizon_min: int = 60
    #: 안전장치 — 신호가 끝내 정의되지 않으면 이 트레일링으로 빠진다(없으면 지평 청산).
    fallback_trail: float | None = None

    def describe(self) -> str:
        arrow = ">=" if self.direction == "above" else "<="
        fb = f", fallback trail -{self.fallback_trail:.0%}" \
            if self.fallback_trail is not None else ""
        return f"{self.signal} {arrow} {self.threshold:g}, horizon {self.horizon_min}m{fb}"


def simulate_signal_exit(path: pd.DataFrame, entry_u: float, entry_ms: int,
                         signal: pd.Series, rule: SignalExitRule) -> dict:
    """경로 위에서 신호 기반 이탈을 시뮬레이션한다.

    `signal` 은 시각(ms) 색인의 신호 시계열이며, 각 봉에서 **그 봉 시각 이하의 마지막
    관측치**만 본다(미래 정보 없음). 신호가 `NaN` 인 봉은 트리거로 세지 않는다.
    반환 형식은 `rules.simulate_exit` 과 동일해 같은 집계에 들어간다.
    """
    from tossmon.analysis.rules import ExitRule, simulate_exit, _res

    if path is None or len(path) == 0 or not (entry_u > 0):
        return {"exit_u": _NAN, "exit_min": _NAN, "reason": "no_path",
                "gross": _NAN, "mfe": _NAN, "mae": _NAN, "signal_defined": False}
    p = path[(path["ts_ms"] >= entry_ms)
             & (path["ts_ms"] <= entry_ms + rule.horizon_min * MIN_MS)]
    if len(p) == 0:
        return {"exit_u": _NAN, "exit_min": _NAN, "reason": "no_path",
                "gross": _NAN, "mfe": _NAN, "mae": _NAN, "signal_defined": False}
    sig = signal.dropna() if signal is not None else pd.Series(dtype="float64")
    best_u, worst_u = float(entry_u), float(entry_u)
    ever_defined = False
    for ts, hi, lo, cl in zip(p["ts_ms"].tolist(), p["high_u"].tolist(),
                              p["low_u"].tolist(), p["close_u"].tolist()):
        ts, hi, lo, cl = int(ts), float(hi), float(lo), float(cl)
        best_u, worst_u = max(best_u, hi), min(worst_u, lo)
        prior = sig[sig.index <= ts]
        if not prior.empty:
            ever_defined = True
            v = float(prior.iloc[-1])
            fired = (v >= rule.threshold) if rule.direction == "above" \
                else (v <= rule.threshold)
            if fired:
                out = _res(cl, ts, entry_ms, entry_u, f"signal:{rule.signal}",
                           best_u, worst_u)
                out["signal_defined"] = True
                return out
    if not ever_defined and rule.fallback_trail is not None:
        out = simulate_exit(path, entry_u, entry_ms,
                            ExitRule(f"{rule.name}_fallback",
                                     horizon_min=rule.horizon_min,
                                     trail=rule.fallback_trail))
        out["reason"] = f"fallback_trail({out['reason']})"
        out["signal_defined"] = False
        return out
    last = p.iloc[-1]
    out = _res(float(last["close_u"]), int(last["ts_ms"]), entry_ms, entry_u,
               "horizon", best_u, worst_u)
    out["signal_defined"] = ever_defined
    return out


# --------------------------------------------------------------------------- #
# 필요 표본 추정 — "며칠 더 모아야 하나"
# --------------------------------------------------------------------------- #
def required_days(observed_sd: float, target_ci_halfwidth: float,
                  events_per_day: float, *, min_n: int = 30,
                  z: float = 1.96) -> dict:
    """목표 정밀도에 필요한 **수집일 수**를 역산한다.

    n = (z·sd / halfwidth)^2 이 통계적 요구량이고, §4.5-2 의 최소 표본 `min_n` 과
    비교해 큰 쪽을 택한다. `events_per_day` 는 실측 자격 이벤트 수/일.

    수치가 아니라 **의사결정용 추정**이다 — 사용자가 "언제 결과를 기대할 수 있는지"
    알아야 하기 때문에 낸다.
    """
    if not (observed_sd > 0 and target_ci_halfwidth > 0 and events_per_day > 0):
        return {"n_needed": None, "days_needed": None, "reason": "invalid inputs"}
    n_stat = (z * observed_sd / target_ci_halfwidth) ** 2
    n = max(float(min_n), n_stat)
    return {"n_needed": int(np.ceil(n)), "n_statistical": int(np.ceil(n_stat)),
            "min_n_floor": min_n, "events_per_day": events_per_day,
            "days_needed": int(np.ceil(n / events_per_day))}

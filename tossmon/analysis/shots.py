"""연속 슈팅(shooting) 구조 — 1분 미만 임펄스의 열 (docs/23).

## 왜 이 모듈이 필요한가

지금까지의 분석은 **"T0 → 단일 고점 → 붕괴"라는 하나의 아크**를 가정했다.
사용자의 실전 관찰은 실제 구조가 **1분 미만 임펄스의 연속**이라고 말한다 —
슈팅이 오고, 끝나고, 또 오고, 어느 순간 마지막이 된다.

그렇다면 docs/18·19 가 "필요 이탈 정밀도(피크 대비 −3.66%)가 1분 해상도보다 미세하다"고
결론 낸 것은 **해상도 부족이 아니라 모델이 틀린 것**일 수 있다. 1분봉에 하나의 봉으로
뭉개진 것이 실은 여러 번의 슈팅이었다면, "고점"은 예측 대상이 아니라 **슈팅의 열이
끝났는지를 판정하는 문제**가 된다.

## 관측 수단 — 12초 가격 계열

1분봉으로는 원리상 안 보인다. `rankings_snap` 이 **약 13초 주기**로 `last_u`(현재가)를
남기므로 이것을 12초급 가격 계열로 쓴다. 랭킹 상위에 있는 동안만 관측되므로
**빠진 구간은 결측**이며, 결측을 건너뛰고 잇지 않는다(docs/17 금지 규칙 1).

## `amount_u` 에 대한 경고 (실측으로 확인)

`rankings_snap.amount_u` 는 **`vol_qu × last_u` 에서 완전히 유도되는 값**이며
비례상수가 **1.44e9** 다(p10 1.43e9 / p90 1.46e9, 분산 1.02배).
`1.44e9 = 1e6 × 1440` 이고 1440 은 USD/KRW 환율대다 — 즉 **`amount_u` 는 원화(마이크로원)
표기**로 보인다. `docs/04` 계약은 "KRW 가 등장할 일은 없다(US 전용)"고 못박고 있으므로
이는 **계약과 어긋나는 필드**다.

또한 `amount_u`·`vol_qu` 는 **누적이 아니다**(연속 차분의 5~6% 가 음수). 롤링 창
집계로 보이며 창 길이는 확인되지 않았다. 따라서 **차분을 "구간 체결대금"으로 쓰면 안 된다.**
이 모듈은 슈팅을 **가격으로만** 정의하고, 활동량은 상대 지표로만 참고한다.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

_NAN = float("nan")

#: 슈팅 최소 상승폭 (저점 대비). 12초급에서 잡음과 구분되는 하한.
DEFAULT_MIN_RISE = 0.02
#: 슈팅이 완성돼야 하는 최대 시간 — 사용자 관찰("1분도 안 된다").
DEFAULT_MAX_SPAN_S = 60
#: 스냅 간격이 이보다 크면 계열이 끊긴 것으로 보고 **잇지 않는다**.
DEFAULT_MAX_GAP_S = 40
#: 두 슈팅을 별개로 셀 최소 간격.
DEFAULT_MIN_SEPARATION_S = 24

SHOT_COLUMNS = ("symbol", "ordinal", "start_ms", "peak_ms", "end_ms",
                "start_u", "peak_u", "rise", "span_s", "gap_prev_s")


def price_series(rankings: pd.DataFrame, symbol: str, ranking_type: str) -> pd.Series:
    """12초급 가격 계열(`last_u`). 랭킹 밖 구간은 **행이 없다**(결측을 채우지 않는다)."""
    if rankings is None or len(rankings) == 0:
        return pd.Series(dtype="float64")
    d = rankings[(rankings["ranking_type"] == ranking_type)
                 & (rankings["symbol"] == symbol)]
    d = d[pd.to_numeric(d["last_u"], errors="coerce") > 0]
    if d.empty:
        return pd.Series(dtype="float64")
    s = (d.drop_duplicates("snap_ms").set_index("snap_ms")["last_u"]
         .astype("float64").sort_index())
    return s


def _segments(series: pd.Series, max_gap_s: int):
    """연속 관측 구간으로 자른다 — 간격이 크면 **잇지 않고 끊는다**."""
    if series.empty:
        return []
    ts = series.index.to_numpy(dtype="int64")
    brk = np.nonzero(np.diff(ts) > max_gap_s * 1000)[0]
    bounds = [0, *(brk + 1), len(ts)]
    return [series.iloc[bounds[i]:bounds[i + 1]] for i in range(len(bounds) - 1)
            if bounds[i + 1] - bounds[i] >= 2]


def detect_shots(series: pd.Series, *, symbol: str = "",
                 min_rise: float = DEFAULT_MIN_RISE,
                 max_span_s: int = DEFAULT_MAX_SPAN_S,
                 max_gap_s: int = DEFAULT_MAX_GAP_S,
                 min_separation_s: int = DEFAULT_MIN_SEPARATION_S) -> pd.DataFrame:
    """**슈팅** = 한 저점에서 `max_span_s` 안에 `min_rise` 이상 오른 임펄스.

    알고리즘(전방 1회 주사, 미래 정보 없음):
    각 시점을 잠재 시작점으로 두고, `max_span_s` 안에서 그 시점 대비 상승폭이
    `min_rise` 를 처음 넘는 순간을 찾는다. 넘으면 그 구간의 **최고가까지**를 한 슈팅으로
    확정하고, 다음 탐색은 그 최고가 이후 `min_separation_s` 부터 재개한다.

    결측 구간을 건너뛰어 이어붙이지 않는다 — `max_gap_s` 를 넘는 간격은 계열을 끊는다.
    슈팅이 없으면 빈 프레임(컬럼 구조는 유지).
    """
    rows: list[dict] = []
    prev_end_ms: int | None = None
    for seg in _segments(series, max_gap_s):
        ts = seg.index.to_numpy(dtype="int64")
        px = seg.to_numpy(dtype="float64")
        i, n = 0, len(ts)
        while i < n - 1:
            lo_px, lo_ts = px[i], ts[i]
            j = i + 1
            hit = -1
            while j < n and (ts[j] - lo_ts) <= max_span_s * 1000:
                if px[j] >= lo_px * (1.0 + min_rise):
                    hit = j
                    break
                if px[j] < lo_px:                 # 더 낮은 저점 -> 시작점을 옮긴다
                    break
                j += 1
            if hit < 0:
                i += 1
                continue
            # 상승이 멈출 때까지 최고가를 연장한다
            k = hit
            while k + 1 < n and px[k + 1] >= px[k] and \
                    (ts[k + 1] - lo_ts) <= max_span_s * 1000:
                k += 1
            rows.append({
                "symbol": symbol, "ordinal": len(rows) + 1,
                "start_ms": int(lo_ts), "peak_ms": int(ts[k]), "end_ms": int(ts[k]),
                "start_u": float(lo_px), "peak_u": float(px[k]),
                "rise": float(px[k] / lo_px - 1.0),
                "span_s": float((ts[k] - lo_ts) / 1000.0),
                "gap_prev_s": (_NAN if prev_end_ms is None
                               else float((lo_ts - prev_end_ms) / 1000.0)),
            })
            prev_end_ms = int(ts[k])
            nxt = np.searchsorted(ts, ts[k] + min_separation_s * 1000)
            i = max(k + 1, int(nxt))
    return pd.DataFrame(rows, columns=list(SHOT_COLUMNS))


def shot_summary(shots: pd.DataFrame) -> dict:
    """한 심볼·하루의 슈팅 열 요약. 슈팅이 없으면 `n_shots=0` 을 그대로 돌려준다."""
    if shots is None or len(shots) == 0:
        return {"n_shots": 0, "total_rise": _NAN, "median_rise": _NAN,
                "median_gap_s": _NAN, "median_span_s": _NAN, "last_rise": _NAN}
    gaps = pd.to_numeric(shots["gap_prev_s"], errors="coerce").dropna()
    return {"n_shots": int(len(shots)),
            "total_rise": float((1 + shots["rise"]).prod() - 1),
            "median_rise": float(shots["rise"].median()),
            "median_gap_s": (float(gaps.median()) if len(gaps) else _NAN),
            "median_span_s": float(shots["span_s"].median()),
            "last_rise": float(shots["rise"].iloc[-1])}


def next_shot_within(shots: pd.DataFrame, after_ms: int, within_s: int) -> bool:
    """`after_ms` 이후 `within_s` 안에 **다음 슈팅이 오는가** — 구조 판정의 핵심 질문."""
    if shots is None or len(shots) == 0:
        return False
    nxt = shots[shots["start_ms"] > after_ms]
    if nxt.empty:
        return False
    return bool(int(nxt["start_ms"].iloc[0]) - after_ms <= within_s * 1000)


def continuation_table(shots: pd.DataFrame, *, within_s: int = 120) -> pd.DataFrame:
    """N번째 슈팅 뒤에 또 슈팅이 올 **조건부 확률**과 그때의 크기.

    "다음 슈팅이 또 오는가"를 서수별로 본다 — 마지막 슈팅이 구분되는지의 1차 증거.
    """
    if shots is None or len(shots) == 0:
        return pd.DataFrame(columns=["ordinal", "n", "p_next", "median_rise",
                                     "median_next_rise"])
    rows = []
    for o, g in shots.groupby("ordinal"):
        nxt = shots[shots["ordinal"] == o + 1]
        rows.append({
            "ordinal": int(o), "n": int(len(g)),
            "p_next": float(len(nxt) / len(g)) if len(g) else _NAN,
            "median_rise": float(g["rise"].median()),
            "median_next_rise": (float(nxt["rise"].median()) if len(nxt) else _NAN),
        })
    del within_s
    return pd.DataFrame(rows).sort_values("ordinal").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# 새 이탈 계열 — **슈팅에 실어 판다** (강세 매도)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ShotExitRule:
    """슈팅 기반 이탈. `rules.ExitRule` 과 같은 자리에서 비교된다.

    - `mode="into_shot_n"`: **N번째 슈팅의 고점에서** 판다(강세에 매도).
      사용자 관찰의 "슈팅 때 이탈"을 그대로 규칙화한 것이다.
    - `mode="on_shot_fail"`: 슈팅이 끝난 뒤 `fail_after_s` 안에 다음 슈팅이 **없으면** 판다.
      "다음 슈팅이 또 오는가"에 대한 구조 판정이며, 약세를 보고 도망치는 것이 아니라
      **열이 끝났다는 증거**로 나간다.
    """

    name: str
    mode: str = "into_shot_n"
    n: int = 1
    fail_after_s: int = 120
    horizon_min: int = 60

    def describe(self) -> str:
        if self.mode == "into_shot_n":
            return f"sell into shot #{self.n}, horizon {self.horizon_min}m"
        return f"sell if no next shot within {self.fail_after_s}s, horizon {self.horizon_min}m"


def simulate_shot_exit(series: pd.Series, entry_ms: int, entry_u: float,
                       shots: pd.DataFrame, rule: ShotExitRule) -> dict:
    """슈팅 기반 이탈을 12초 계열 위에서 시뮬레이션한다.

    반환 형식은 `rules.simulate_exit` 과 동일해 같은 순위표에 들어간다.
    진입 이후의 슈팅만 본다(미래 정보 없음). 규칙이 발동하지 못하면 지평 마지막
    관측가로 청산하고 `reason="horizon"`.
    """
    from tossmon.analysis.rules import _res

    if series is None or series.empty or not (entry_u > 0):
        return {"exit_u": _NAN, "exit_min": _NAN, "reason": "no_path",
                "gross": _NAN, "mfe": _NAN, "mae": _NAN, "n_shots_after": 0}
    hi_ms = entry_ms + rule.horizon_min * 60_000
    win = series[(series.index >= entry_ms) & (series.index <= hi_ms)]
    if win.empty:
        return {"exit_u": _NAN, "exit_min": _NAN, "reason": "no_path",
                "gross": _NAN, "mfe": _NAN, "mae": _NAN, "n_shots_after": 0}
    best, worst = float(win.max()), float(win.min())
    after = (shots[(shots["start_ms"] >= entry_ms) & (shots["peak_ms"] <= hi_ms)]
             if shots is not None and len(shots) else pd.DataFrame(columns=SHOT_COLUMNS))
    out_n = int(len(after))

    if rule.mode == "into_shot_n":
        if out_n >= rule.n:
            row = after.iloc[rule.n - 1]
            return {**_res(float(row["peak_u"]), int(row["peak_ms"]), entry_ms,
                           entry_u, f"shot#{rule.n}", best, worst),
                    "n_shots_after": out_n}
    elif rule.mode == "on_shot_fail":
        for i in range(out_n):
            pk = int(after["peak_ms"].iloc[i])
            if not next_shot_within(after, pk, rule.fail_after_s):
                idx = win.index[win.index >= pk + rule.fail_after_s * 1000]
                if len(idx):
                    t = int(idx[0])
                    return {**_res(float(win.loc[t]), t, entry_ms, entry_u,
                                   "shot_fail", best, worst),
                            "n_shots_after": out_n}
                break
    else:
        raise ValueError("mode must be 'into_shot_n' or 'on_shot_fail'")

    t = int(win.index[-1])
    return {**_res(float(win.iloc[-1]), t, entry_ms, entry_u, "horizon", best, worst),
            "n_shots_after": out_n}


def required_days(shots_per_day: float, *, min_n: int = 30) -> dict:
    """판정에 필요한 수집일 — 슈팅 관측 빈도 기준의 단순 역산."""
    if not (shots_per_day > 0):
        return {"days_needed": None, "reason": "no shots observed"}
    return {"min_n": min_n, "shots_per_day": shots_per_day,
            "days_needed": int(np.ceil(min_n / shots_per_day))}

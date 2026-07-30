"""베이스라인 지표 — 계약 C-7. 소유: W3. 정의식은 docs/07_analysis_spec.md 에 기록.

핵심 원칙
---------
1. **시간대 보정**: RVOL 의 분모는 "세션 내 분 위치별" 기대 거래량이다. 절대 시각이 아니라
   `(세션명, 세션 시작 이후 경과분)` 을 키로 쓰므로 서머타임·조기폐장·세션 길이 차이에
   자동 대응한다.
2. **정수 오버플로 방지**: `tp_u * vol_qu` 는 봉당 ~1e17 이라 390봉 누적이 int64 상한
   (9.22e18)을 넘는다. numpy/pandas 정수 누적은 조용히 오버플로하므로 가격×수량 누적은
   반드시 Python 임의정밀도 int 로 계산한다.
3. **결측 처리**: 체결이 없는 분은 API 가 봉을 주지 않는다 → 거래량 0 으로 간주한다.
   단, (날짜, 세션) 전체가 비어 있으면(수집 중단) 그 세션은 평균에서 **제외**한다.
   그러지 않으면 분모가 작아져 RVOL 이 구조적으로 부풀려진다.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from itertools import accumulate

import pandas as pd

from ..api.models import SessionWindow, UsMarketDay

MIN_MS = 60_000
SESSION_NAMES = ("day", "pre", "regular", "after")
DEFAULT_WINDOW_DAYS = 20


# --------------------------------------------------------------------------- #
# 공통 유틸
# --------------------------------------------------------------------------- #
def session_windows(md: UsMarketDay) -> list[tuple[str, SessionWindow]]:
    """시간순 (세션명, 윈도우). None 세션(미제공)은 제외."""
    return [(n, getattr(md, n)) for n in SESSION_NAMES if getattr(md, n) is not None]


def locate_session(md: UsMarketDay, ts_ms: int) -> tuple[str, SessionWindow] | None:
    for name, win in session_windows(md):
        if win.start_ms <= ts_ms < win.end_ms:
            return name, win
    return None


def _require_single_symbol(df: pd.DataFrame) -> None:
    if "symbol" in df.columns and not df.empty and df["symbol"].nunique(dropna=True) > 1:
        raise ValueError("단일 심볼 DataFrame 만 허용 (심볼별로 나눠 호출)")


def true_range_u(df: pd.DataFrame) -> pd.Series:
    """TR_i = max(H-L, |H - C_{i-1}|, |L - C_{i-1}|). 첫 봉은 H-L.

    입력 행 순서(ts_ms 오름차순)를 그대로 신뢰한다.
    """
    high, low, close = df["high_u"], df["low_u"], df["close_u"]
    prev = close.shift(1)
    hl = (high - low).abs()
    tr = pd.concat([hl, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    if len(tr):
        tr.iloc[0] = float(hl.iloc[0])
    return tr.astype("int64")


def atr_u(df: pd.DataFrame, n: int = DEFAULT_WINDOW_DAYS) -> int | None:
    """ATR(n) = TR 의 단순이동평균(SMA). Wilder 평활이 아님 (docs/07 §2.2)."""
    if df is None or df.empty:
        return None
    tr = true_range_u(df.sort_values("ts_ms"))
    tail = tr.iloc[-n:]
    if tail.empty:
        return None
    return int(sum(int(x) for x in tail) // len(tail))


# --------------------------------------------------------------------------- #
# C-7: compute_daily_baseline
# --------------------------------------------------------------------------- #
def compute_daily_baseline(df_1d: pd.DataFrame,
                           window_days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """일봉 20일 롤링 베이스라인.

    반환 키
        n_days        사용된 일봉 수 (0 이면 나머지는 None/NaN)
        window_days   요청 윈도우
        adv20_qu      최근 window_days 평균 거래량 (마이크로주, int)
        atr20_u       ATR(window_days) (마이크로달러, int)
        atr20_pct     atr20_u / 마지막 종가
        close_last_u  마지막 종가 (int)
        vol_mean_qu   거래량 표본평균 / vol_std_qu 표본표준편차(ddof=1)
        logvol_mean   ln(거래량) 표본평균 / logvol_std 표본표준편차(ddof=1)
        ret_std       일간 로그수익률 표준편차

    거래량 z-score 의 **기본 정의는 로그 공간**(`daily_volume_z`). 원공간 z 는 우편향
    때문에 임계값 3~4 가 사실상 도달 불가/과민해진다 (docs/07 §2.3).
    """
    empty = {"n_days": 0, "window_days": window_days, "adv20_qu": None, "atr20_u": None,
             "atr20_pct": float("nan"), "close_last_u": None,
             "vol_mean_qu": float("nan"), "vol_std_qu": float("nan"),
             "logvol_mean": float("nan"), "logvol_std": float("nan"),
             "ret_std": float("nan")}
    if df_1d is None or df_1d.empty:
        return empty
    _require_single_symbol(df_1d)

    df = df_1d.sort_values("ts_ms")
    tail = df.iloc[-window_days:]
    vols = [int(v) for v in tail["vol_qu"].tolist() if int(v) > 0]
    closes = [int(c) for c in df["close_u"].tolist()]

    out = dict(empty)
    out["n_days"] = int(len(tail))
    out["close_last_u"] = closes[-1]

    if vols:
        n = len(vols)
        mean = sum(vols) / n
        out["adv20_qu"] = int(sum(vols) // n)
        out["vol_mean_qu"] = mean
        out["vol_std_qu"] = (math.sqrt(sum((v - mean) ** 2 for v in vols) / (n - 1))
                             if n > 1 else float("nan"))
        lv = [math.log(v) for v in vols]
        lmean = sum(lv) / n
        out["logvol_mean"] = lmean
        out["logvol_std"] = (math.sqrt(sum((x - lmean) ** 2 for x in lv) / (n - 1))
                             if n > 1 else float("nan"))

    atr = atr_u(df, window_days)
    if atr is not None:
        out["atr20_u"] = atr
        out["atr20_pct"] = atr / closes[-1] if closes[-1] else float("nan")

    if len(closes) > 2:
        rets = [math.log(b / a) for a, b in zip(closes[:-1], closes[1:]) if a > 0 and b > 0]
        tr = rets[-window_days:]
        if len(tr) > 1:
            m = sum(tr) / len(tr)
            out["ret_std"] = math.sqrt(sum((r - m) ** 2 for r in tr) / (len(tr) - 1))
    return out


def daily_volume_z(baseline: dict, vol_qu: int, log: bool = True) -> float:
    """당일 거래량의 20일 롤링 z-score. 기본 로그 공간 (docs/07 §2.3)."""
    if vol_qu is None or int(vol_qu) <= 0:
        return float("nan")
    if log:
        mu, sd, x = baseline.get("logvol_mean"), baseline.get("logvol_std"), \
            math.log(int(vol_qu))
    else:
        mu, sd, x = baseline.get("vol_mean_qu"), baseline.get("vol_std_qu"), float(vol_qu)
    if mu is None or sd is None or mu != mu or sd != sd or not sd > 0:
        return float("nan")
    return (x - mu) / sd


# --------------------------------------------------------------------------- #
# C-7: minute_of_session_volume_curve
# --------------------------------------------------------------------------- #
CURVE_SESSION_KEY = "sessions"
CURVE_CUM_KEY = "cum_curve"
CURVE_DAYS_KEY = "n_days_by_key"


def minute_of_session_volume_curve(df_1m: pd.DataFrame,
                                   calendar: list[UsMarketDay], *,
                                   exclude_dates: list[str] | tuple[str, ...] | None = None
                                   ) -> pd.Series:
    """세션 내 분 위치별 **평균 봉거래량** 곡선 (시간대 보정 RVOL 의 분모).

    index: MultiIndex (session, minute_of_session), value: 평균 vol_qu (float).
    index 는 날짜와 무관하므로 **다른 날에 그대로 재사용**할 수 있다.

    계약 C-7 의 `rvol()` 시그니처가 calendar 를 받지 않으므로, 세션 판정에 필요한 정보는
    curve 가 `Series.attrs` 로 운반한다:
        attrs["sessions"]      [(start_ms, end_ms, session, date)] 시간순
        attrs["cum_curve"]     같은 index 의 **누적** 평균거래량 (세션 시작~해당 분)
        attrs["n_days_by_key"] 각 (session, minute) 에 기여한 세션 수

    `exclude_dates` (A1 §2 확장): 그 날짜들은 **평균 계산에서 제외**하되 세션 윈도우는
    attrs 에 그대로 등록한다. 급등 당일을 분모에 넣으면 RVOL 이 1 쪽으로 축소되는
    자기오염이 생기므로, 평가 대상 날짜는 반드시 제외해야 한다 (docs/07 §2.4).

    결측 규칙: 봉이 없는 분은 거래량 0. 단 (날짜, 세션) 전체가 비면 그 세션은 제외.
    """
    _require_single_symbol(df_1m)
    skip = set(exclude_dates or ())
    by_ts: dict[int, int] = {}
    if df_1m is not None and not df_1m.empty:
        by_ts = {int(t): int(v)
                 for t, v in zip(df_1m["ts_ms"].tolist(), df_1m["vol_qu"].tolist())}

    bar_sum: dict[tuple[str, int], int] = {}
    cum_sum: dict[tuple[str, int], int] = {}
    day_cnt: dict[tuple[str, int], int] = {}
    sessions: list[tuple[int, int, str, str]] = []

    for md in calendar:
        for name, win in session_windows(md):
            sessions.append((win.start_ms, win.end_ms, name, md.date))
            if md.date in skip:
                continue                      # 세션 윈도우만 등록, 평균에서는 제외
            n = int((win.end_ms - win.start_ms) // MIN_MS)
            minute_vols = [by_ts.get(win.start_ms + m * MIN_MS, 0) for m in range(n)]
            if not any(minute_vols):
                continue                      # 수집 중단 세션은 평균에서 제외
            running = 0
            for m, v in enumerate(minute_vols):
                running += v
                key = (name, m)
                bar_sum[key] = bar_sum.get(key, 0) + v
                cum_sum[key] = cum_sum.get(key, 0) + running
                day_cnt[key] = day_cnt.get(key, 0) + 1

    sessions.sort()
    keys = sorted(day_cnt)
    idx = pd.MultiIndex.from_tuples(keys or [(None, None)], names=["session", "minute"])
    if not keys:
        idx = idx[:0]
    curve = pd.Series([bar_sum[k] / day_cnt[k] for k in keys], index=idx,
                      dtype="float64", name="mean_vol_qu")
    cum = pd.Series([cum_sum[k] / day_cnt[k] for k in keys], index=idx,
                    dtype="float64", name="mean_cum_vol_qu")
    curve.attrs[CURVE_SESSION_KEY] = sessions
    curve.attrs[CURVE_CUM_KEY] = cum
    curve.attrs[CURVE_DAYS_KEY] = day_cnt
    return curve


def _session_table(curve: pd.Series,
                   calendar: list[UsMarketDay] | None) -> list[tuple[int, int, str, str]]:
    """세션 위치 판정용 표. `calendar` 가 주어지면 그것을 우선한다.

    곡선(분모)은 **이력일**로 만들고 적용은 **오늘**에 하는 것이 정상 운용이다.
    이때 오늘의 세션 윈도우는 curve.attrs 에 없으므로 calendar 로 넘긴다 (A1 §2 확장).
    """
    if calendar:
        return sorted((w.start_ms, w.end_ms, n, md.date)
                      for md in calendar for n, w in session_windows(md))
    return curve.attrs.get(CURVE_SESSION_KEY, [])


def curve_locate(curve: pd.Series, ts_ms: int, *,
                 calendar: list[UsMarketDay] | None = None
                 ) -> tuple[str, int, int, int] | None:
    """`ts_ms` 가 속한 세션 판정 → (session, minute, start_ms, end_ms)."""
    for start_ms, end_ms, name, _date in _session_table(curve, calendar):
        if start_ms <= ts_ms < end_ms:
            return name, int((ts_ms - start_ms) // MIN_MS), int(start_ms), int(end_ms)
    return None


# --------------------------------------------------------------------------- #
# C-7: rvol
# --------------------------------------------------------------------------- #
def rvol(df_1m: pd.DataFrame, curve: pd.Series, ts_ms: int, *,
         calendar: list[UsMarketDay] | None = None) -> float:
    """시간대 보정 **누적** 상대거래량.

        RVOL(t) = (세션 시작~t 누적거래량) / (같은 (세션, 분위치)의 평균 누적거래량)

    docs/02 §4.1 의 "RVOL 1.5x 주목 / 3~5x 스캐너" 임계값은 장중 누적 기준 지표이므로
    누적 정의를 기본으로 삼는다. 단일 분봉 기준은 `rvol_bar`.

    `ts_ms` 가 어떤 세션에도 속하지 않거나 기대값이 0/미관측이면 NaN.
    `calendar` 를 주면 세션 판정에 그것을 쓴다 (곡선은 이력일, 적용은 당일인 정상 운용).
    """
    loc = curve_locate(curve, ts_ms, calendar=calendar)
    if loc is None:
        return float("nan")
    name, minute, start_ms, _end = loc
    cum: pd.Series | None = curve.attrs.get(CURVE_CUM_KEY)
    if cum is None or (name, minute) not in cum.index:
        return float("nan")
    expected = float(cum.loc[(name, minute)])
    if not expected > 0:
        return float("nan")
    sub = df_1m[(df_1m["ts_ms"] >= start_ms) & (df_1m["ts_ms"] <= ts_ms)]
    actual = float(sum(int(v) for v in sub["vol_qu"].tolist()))
    return actual / expected


def rvol_bar(df_1m: pd.DataFrame, curve: pd.Series, ts_ms: int, *,
             calendar: list[UsMarketDay] | None = None) -> float:
    """단일 분봉 기준 시간대 보정 상대거래량 (봉이 없으면 거래량 0 → 0.0)."""
    loc = curve_locate(curve, ts_ms, calendar=calendar)
    if loc is None:
        return float("nan")
    name, minute, _start, _end = loc
    if (name, minute) not in curve.index:
        return float("nan")
    expected = float(curve.loc[(name, minute)])
    if not expected > 0:
        return float("nan")
    row = df_1m[df_1m["ts_ms"] == ts_ms]
    actual = float(int(row["vol_qu"].iloc[0])) if not row.empty else 0.0
    return actual / expected


def rvol_series(df_1m: pd.DataFrame, curve: pd.Series, *,
                calendar: list[UsMarketDay] | None = None) -> pd.Series:
    """모든 봉의 누적 RVOL 을 한 번에 계산 (봉마다 `rvol()` 호출하면 O(n²)).

    index: ts_ms (int64), value: RVOL (float). 세션 밖 봉은 NaN.
    1024일치 백필(수십만 봉)에도 O(n) 이어야 하므로 dict 조회 기반 단일 패스로 구현한다.
    """
    empty = pd.Series([], index=pd.Index([], dtype="int64", name="ts_ms"),
                      dtype="float64", name="rvol")
    if df_1m is None or df_1m.empty:
        return empty
    cum: pd.Series | None = curve.attrs.get(CURVE_CUM_KEY)
    cum_map = ({(str(s), int(m)): float(v) for (s, m), v in cum.items()}
               if cum is not None else {})
    sessions = _session_table(curve, calendar)
    df = df_1m.sort_values("ts_ms")
    ts_list = [int(t) for t in df["ts_ms"].tolist()]
    vol_list = [int(v) for v in df["vol_qu"].tolist()]

    starts = [s[0] for s in sessions]
    out: list[float] = []
    running = 0
    cur: tuple[int, int, str, str] | None = None
    for t, v in zip(ts_list, vol_list):
        if cur is None or not (cur[0] <= t < cur[1]):
            # sessions 는 start 오름차순 → 이진탐색 (1024일 백필에서도 O(n log n))
            j = bisect_right(starts, t) - 1
            cand = sessions[j] if j >= 0 else None
            cur = cand if (cand is not None and cand[0] <= t < cand[1]) else None
            running = 0
        if cur is None:
            out.append(float("nan"))
            continue
        running += v
        exp = cum_map.get((cur[2], int((t - cur[0]) // MIN_MS)), 0.0)
        out.append(running / exp if exp > 0 else float("nan"))
    return pd.Series(out, index=pd.Index(ts_list, dtype="int64", name="ts_ms"),
                     dtype="float64", name="rvol")


# --------------------------------------------------------------------------- #
# C-7: session_vwap_u
# --------------------------------------------------------------------------- #
def session_vwap_u(df_1m: pd.DataFrame, session: SessionWindow) -> pd.Series:
    """세션별 VWAP (마이크로달러) 시계열. 세션 시작에서 리셋.

        tp_u   = (H + L + C) // 3
        VWAP_t = Σ(tp_u · vol_qu) // Σ(vol_qu)      (세션 시작 ~ t)

    누적은 Python 임의정밀도 int (int64 오버플로 방지). 누적 거래량이 0 인 선두 구간은
    해당 봉의 tp_u 를 사용한다. index: ts_ms(int64), dtype int64.
    """
    empty = pd.Series([], index=pd.Index([], dtype="int64", name="ts_ms"),
                      dtype="int64", name="vwap_u")
    if df_1m is None or df_1m.empty:
        return empty
    sub = df_1m[(df_1m["ts_ms"] >= session.start_ms)
                & (df_1m["ts_ms"] < session.end_ms)].sort_values("ts_ms")
    if sub.empty:
        return empty
    tps = [int(x) for x in ((sub["high_u"] + sub["low_u"] + sub["close_u"]) // 3).tolist()]
    vols = [int(v) for v in sub["vol_qu"].tolist()]
    num = list(accumulate(t * v for t, v in zip(tps, vols)))
    den = list(accumulate(vols))
    vals = [(n // d) if d > 0 else t for n, d, t in zip(num, den, tps)]
    return pd.Series(vals, index=pd.Index([int(t) for t in sub["ts_ms"].tolist()],
                                          dtype="int64", name="ts_ms"),
                     dtype="int64", name="vwap_u")


def session_vwap_map(df_1m: pd.DataFrame, calendar: list[UsMarketDay],
                     sessions: tuple[str, ...] = ("regular",)) -> pd.Series:
    """지정 세션들의 VWAP 을 이어붙인 시계열 (세션마다 리셋). index: ts_ms."""
    parts: list[pd.Series] = []
    for md in calendar:
        for name, win in session_windows(md):
            if name in sessions:
                s = session_vwap_u(df_1m, win)
                if not s.empty:
                    parts.append(s)
    if not parts:
        return pd.Series([], index=pd.Index([], dtype="int64", name="ts_ms"),
                         dtype="int64", name="vwap_u")
    return pd.concat(parts).sort_index()

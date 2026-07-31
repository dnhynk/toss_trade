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


#: 각 세션 시작이 **그 매매일의 00:00 ET** 로부터 떨어진 분수 (docs/01 §5 세션 규약).
#: day 세션만 전일 20:00 ET 시작이라 음수다. 조기폐장은 종료만 당겨지므로 시작 오프셋은
#: 반일장에서도 그대로다.
_ET_MIDNIGHT_OFFSET_MIN = {"day": -240, "pre": 240, "regular": 570, "after": 960}
DAY_MS = 86_400_000


def et_midnight_ms(md: UsMarketDay) -> int | None:
    """그 매매일 date 의 **00:00 ET** (= 일봉 ts 규약, docs/06 §2-2).

    캘린더가 준 세션 시작에서 역산한다 — ET 오프셋을 코드에 박지 않기 위함이며(계약 C-1),
    서머타임 전환도 캘린더가 이미 반영한 값이라 자동으로 따라온다.
    세션이 하나도 없는 날(휴장)은 None.
    """
    for name in ("regular", "pre", "after", "day"):
        w = getattr(md, name)
        if w is not None:
            return int(w.start_ms - _ET_MIDNIGHT_OFFSET_MIN[name] * MIN_MS)
    return None


def daily_bar_date_table(calendar: list[UsMarketDay]) -> list[tuple[int, str]]:
    """[(00:00 ET ms, 매매일 date)] 오름차순 — 일봉 ts → 매매일 date 매핑용."""
    out = [(m, md.date) for md, m in ((md, et_midnight_ms(md)) for md in (calendar or []))
           if m is not None]
    return sorted(out)


def daily_bar_date(ts_ms: int, table: list[tuple[int, str]]) -> str | None:
    """일봉 ts 가 속한 **매매일 date**. 매핑 불가면 None.

    **ms 스팬(세션 구간)으로 버킷팅하면 안 된다** — 일봉 ts 는 00:00 ET 라 day 세션이
    없는 매매일에서는 어느 세션 스팬에도 들어가지 않아 조용히 버려진다
    (3차 감사 F-1/F-2(b)). 날짜 단위로 매핑하는 것이 유일하게 옳다.
    """
    if not table:
        return None
    starts = [t[0] for t in table]
    j = bisect_right(starts, int(ts_ms)) - 1
    if j < 0:
        return None
    start, date = table[j]
    return date if int(ts_ms) - start < DAY_MS else None


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
                           window_days: int = DEFAULT_WINDOW_DAYS, *,
                           as_of_ms: int | None = None,
                           as_of_date: str | None = None,
                           calendar: list[UsMarketDay] | None = None) -> dict:
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

    **as-of 앵커 (사전등록 §2.2, 2026-07-31 보강)**: `as_of_ms`(또는 `as_of_date`+`calendar`)
    를 주면 **그 시각보다 엄격히 과거의 일봉만** 후보가 된다. 곡선(§2.4b)과 같은 원칙이며,
    호출자 재량에 맡기지 않고 **함수가 강제**한다. 앵커 없이 부르면 입력 프레임의 마지막
    20행을 쓰므로 D 이후 일봉이 섞인 프레임에서 조용히 미래를 본다 —
    반환 dict 의 `as_of_applied=False` 가 그 사실을 드러낸다. **주 분석은
    `prereg_daily_baseline()` 로 부른다.**
    """
    table = daily_bar_date_table(calendar or [])
    # 앵커 모드: date(안전) > ms(구식·불안전) > 없음
    if as_of_date is None and as_of_ms is not None and table:
        # raw ms 를 받았어도 캘린더가 있으면 date 로 승격한다 (3차 감사 F-1 권고)
        for start_ms, _end_ms, date in _market_day_spans(calendar or []):
            if start_ms <= int(as_of_ms) < _end_ms:
                as_of_date = date
                break
    if as_of_date is not None and table:
        mode = "date"
    elif as_of_ms is not None:
        mode = "ms_unanchored_by_date"
    else:
        mode = None
    empty = {"n_days": 0, "window_days": window_days, "adv20_qu": None, "atr20_u": None,
             "atr20_pct": float("nan"), "close_last_u": None,
             "vol_mean_qu": float("nan"), "vol_std_qu": float("nan"),
             "logvol_mean": float("nan"), "logvol_std": float("nan"),
             "ret_std": float("nan"), "as_of_ms": as_of_ms, "as_of_date": as_of_date,
             "as_of_mode": mode,
             "as_of_applied": (as_of_date is not None or as_of_ms is not None)}
    if df_1d is None or df_1d.empty:
        return empty
    _require_single_symbol(df_1d)

    df = df_1d.sort_values("ts_ms")
    if mode == "date":
        # **매매일 date 단위**로 거른다 (3차 감사 F-1). ms 부등호는 day 세션이 없는
        # 매매일에서 평가일 자기 일봉(명백한 미래)을 통과시킨다.
        bar_dates = [daily_bar_date(int(t), table) for t in df["ts_ms"].tolist()]
        keep = [(d is not None and d < as_of_date) for d in bar_dates]
        df = df[pd.Series(keep, index=df.index)]
    elif as_of_ms is not None:
        df = df[df["ts_ms"] < int(as_of_ms)]      # 캘린더 없음 → 구식 ms 비교(불안전)
    if df.empty:
        return empty
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
CURVE_LENGTHS_KEY = "session_lengths"
CURVE_DROPPED_KEY = "dropped_below_min_days"


def session_len_min(win: SessionWindow) -> int:
    """세션 길이(분). 조기폐장일은 정상일보다 짧다."""
    return int((win.end_ms - win.start_ms) // MIN_MS)


def _window_dates(calendar: list[UsMarketDay], window_days: int,
                  as_of_date: str | None, skip: set[str]) -> set[str]:
    """분모에 넣을 매매일 date 집합 (사전등록 §2.2).

    `as_of_date` 가 주어지면 **그 날짜보다 엄격히 이전**만 후보가 되고, 그중 최근
    `window_days` 개를 쓴다. 휴장일(세션 없음)과 `exclude_dates` 는 후보에서 빠진다.
    """
    dates = sorted({md.date for md in calendar
                    if session_windows(md) and md.date not in skip})
    if as_of_date is not None:
        dates = [d for d in dates if d < as_of_date]
    if window_days is not None and window_days > 0:
        dates = dates[-window_days:]
    return set(dates)


#: 사전등록 §2.2 가 얼린 곡선 파라미터. 주 분석은 반드시 이 값으로 부른다
#: (`prereg_volume_curve()` 가 둘을 한 번에 적용한다).
PREREG_WINDOW_DAYS = 20
PREREG_MIN_DAYS = 10


def minute_of_session_volume_curve(df_1m: pd.DataFrame,
                                   calendar: list[UsMarketDay], *,
                                   exclude_dates: list[str] | tuple[str, ...] | None = None,
                                   min_days: int = 1,
                                   window_days: int = PREREG_WINDOW_DAYS,
                                   as_of_date: str | None = None) -> pd.Series:
    """세션 내 분 위치별 **평균 봉거래량** 곡선 (시간대 보정 RVOL 의 분모).

    index: MultiIndex **(session, session_len_min, minute_of_session)**,
    value: 평균 vol_qu (float). index 는 날짜와 무관하므로 같은 길이의 다른 날에 그대로
    재사용할 수 있다.

    **세션 길이가 키에 들어간다 (감사 M-4 수정).** 조기폐장(반일장, 정규장 210분)과
    정상일(390분)은 같은 `minute` 값이 시장에서 전혀 다른 국면을 뜻한다 — 반일장의
    minute 209 는 **종가 경매**이고 정상일의 minute 209 는 한산한 장중이다. 길이를 키에서
    빼면 종가 스파이크가 정상일 분모를 20~100배로 부풀려 그 분의 `rvol_bar` 를 0.04 로
    죽이고, 반대로 반일장 종가를 허위 버스트로 만든다 (docs/07 §2.4a).

    계약 C-7 의 `rvol()` 시그니처가 calendar 를 받지 않으므로, 세션 판정에 필요한 정보는
    curve 가 `Series.attrs` 로 운반한다:
        attrs["sessions"]      [(start_ms, end_ms, session, date)] 시간순
        attrs["cum_curve"]     같은 index 의 **누적** 평균거래량 (세션 시작~해당 분)
        attrs["n_days_by_key"] 각 (session, len, minute) 에 기여한 세션 수
        attrs["session_lengths"]        {session: {길이분: 관측 세션 수}} — 반일장이
                                        분리됐다는 사실과 그 건수를 드러낸다
        attrs["dropped_below_min_days"] min_days 미달로 버려진 (session, len) 목록

    `exclude_dates` (A1 §2 확장): 그 날짜들은 **평균 계산에서 제외**하되 세션 윈도우는
    attrs 에 그대로 등록한다. 급등 당일을 분모에 넣으면 RVOL 이 1 쪽으로 축소되는
    자기오염이 생기므로, 평가 대상 날짜는 반드시 제외해야 한다 (docs/07 §2.4).

    `min_days`: 관측 세션 수가 이 값 미만인 (session, len) 버킷은 **통째로 버린다**(NaN).
    기본 1 은 기존 동작 유지값이고, **사전등록 §2.2 의 주 분석은 10 을 요구한다**.
    반일장처럼 드문 길이는 여기서 걸러져 RVOL 미가용 → `rvol_gated=False` 로 흘러간다
    (docs/07 §2.4a).

    `window_days` / `as_of_date` (사전등록 §2.2): 평가일 D(`as_of_date`)에 대해
    **엄격히 과거** 최근 `window_days` 매매일만 분모에 넣는다. 자기오염 금지를 옵션이 아니라
    **기간 분리로 구조적으로 강제**하는 장치다 (docs/07 §2.4b).
    `as_of_date=None` 이면 캘린더의 마지막 `window_days` 매매일을 쓴다(앵커 없음).
    세션 윈도우 등록(attrs["sessions"])은 창 밖 날짜도 유지하므로, 창 밖 날의 봉도
    위치 판정은 되고 분모만 창 안에서 온다.

    결측 규칙: 봉이 없는 분은 거래량 0. 단 (날짜, 세션) 전체가 비면 그 세션은 제외.
    """
    _require_single_symbol(df_1m)
    skip = set(exclude_dates or ())
    in_window = _window_dates(calendar, window_days, as_of_date, skip)
    by_ts: dict[int, int] = {}
    if df_1m is not None and not df_1m.empty:
        by_ts = {int(t): int(v)
                 for t, v in zip(df_1m["ts_ms"].tolist(), df_1m["vol_qu"].tolist())}

    bar_sum: dict[tuple[str, int, int], int] = {}
    cum_sum: dict[tuple[str, int, int], int] = {}
    day_cnt: dict[tuple[str, int, int], int] = {}
    sessions: list[tuple[int, int, str, str]] = []
    lengths: dict[str, dict[int, int]] = {}

    for md in calendar:
        for name, win in session_windows(md):
            sessions.append((win.start_ms, win.end_ms, name, md.date))
            if md.date not in in_window:
                continue                      # 세션 윈도우만 등록, 평균에서는 제외
            n = session_len_min(win)
            minute_vols = [by_ts.get(win.start_ms + m * MIN_MS, 0) for m in range(n)]
            if not any(minute_vols):
                continue                      # 수집 중단 세션은 평균에서 제외
            lengths.setdefault(name, {})
            lengths[name][n] = lengths[name].get(n, 0) + 1
            running = 0
            for m, v in enumerate(minute_vols):
                running += v
                key = (name, n, m)
                bar_sum[key] = bar_sum.get(key, 0) + v
                cum_sum[key] = cum_sum.get(key, 0) + running
                day_cnt[key] = day_cnt.get(key, 0) + 1

    # 관측이 부족한 (세션, 길이) 버킷은 통째로 버린다 — 반일장처럼 드문 길이가
    # 자기 자신만으로 분모가 되어 RVOL≡1 이 되는 것을 막는다.
    dropped = sorted({(s, ln) for (s, ln, _m), c in day_cnt.items() if c < min_days})
    keys = sorted(k for k, c in day_cnt.items() if c >= min_days)

    sessions.sort()
    idx = pd.MultiIndex.from_tuples(keys or [(None, None, None)],
                                    names=["session", "session_len_min", "minute"])
    if not keys:
        idx = idx[:0]
    curve = pd.Series([bar_sum[k] / day_cnt[k] for k in keys], index=idx,
                      dtype="float64", name="mean_vol_qu")
    cum = pd.Series([cum_sum[k] / day_cnt[k] for k in keys], index=idx,
                    dtype="float64", name="mean_cum_vol_qu")
    curve.attrs[CURVE_SESSION_KEY] = sessions
    curve.attrs[CURVE_CUM_KEY] = cum
    curve.attrs[CURVE_DAYS_KEY] = day_cnt
    curve.attrs[CURVE_LENGTHS_KEY] = lengths
    curve.attrs[CURVE_DROPPED_KEY] = dropped
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


# --------------------------------------------------------------------------- #
# 사전등록 §7-e — 분할일 식별 (1분봉 원주가 vs 일봉 수정주가)
# --------------------------------------------------------------------------- #
#: 분할 판정 임계 — **얼린 값**(사전등록 §7-e, 2026-07-31 확정). 근거: 최소 실재 분할비
#: 2:1(점프 2.0배)과 비분할 노이즈(배당 조정 ≲1.1배) 사이의 중간값. 바꾸지 말 것.
SPLIT_RATIO_THRESHOLD = 1.5


def _market_day_spans(calendar: list[UsMarketDay]) -> list[tuple[int, int, str]]:
    out = [(w[0][1].start_ms, w[-1][1].end_ms, md.date)
           for md, w in ((md, session_windows(md)) for md in (calendar or [])) if w]
    return sorted(out)


def split_ratio_series(df_1m: pd.DataFrame, df_1d: pd.DataFrame,
                       calendar: list[UsMarketDay]) -> pd.Series:
    """매매일별 조정계수 `r` (사전등록 §7-e).

        r(d) = 일봉 **수정** 종가(d) / 같은 매매일 1분봉 **마지막** 종가(원주가)

    분할이 없는 구간에서 r 은 누적 조정계수라 (배당 조정 수준의 미세 변동을 빼면) 상수이고,
    분할일에 분할비만큼 점프한다.

    index: 매매일 `date`(문자열, 오름차순), value: r (float).
    두 계열 중 하나라도 그 매매일에 없으면 그 날은 **행이 없다**(계산 불가).
    """
    empty = pd.Series([], index=pd.Index([], dtype="object", name="date"),
                      dtype="float64", name="split_ratio")
    if df_1m is None or len(df_1m) == 0 or df_1d is None or len(df_1d) == 0:
        return empty
    _require_single_symbol(df_1m)
    _require_single_symbol(df_1d)

    spans = _market_day_spans(calendar)
    if not spans:
        return empty
    starts = [s[0] for s in spans]

    def _bucket(ts: int) -> str | None:
        j = bisect_right(starts, ts) - 1
        return spans[j][2] if (j >= 0 and spans[j][0] <= ts < spans[j][1]) else None

    # 매매일별 1분봉 마지막 종가(원주가)
    last_1m: dict[str, tuple[int, int]] = {}      # date -> (ts, close)
    for ts, close in zip(df_1m["ts_ms"].tolist(), df_1m["close_u"].tolist()):
        d = _bucket(int(ts))
        if d is None:
            continue
        prev = last_1m.get(d)
        if prev is None or int(ts) > prev[0]:
            last_1m[d] = (int(ts), int(close))

    # 매매일별 일봉(수정주가) 종가 — **날짜 단위로 매핑**한다.
    # 일봉 ts 는 00:00 ET 라 세션 ms 스팬으로 버킷팅하면 day 세션 없는 매매일에서
    # 조용히 버려진다 (3차 감사 F-2 트리거 (b)).
    date_table = daily_bar_date_table(calendar)
    daily: dict[str, tuple[int, int]] = {}
    for ts, close in zip(df_1d["ts_ms"].tolist(), df_1d["close_u"].tolist()):
        d = daily_bar_date(int(ts), date_table)
        if d is None:
            continue
        prev = daily.get(d)
        if prev is None or int(ts) >= prev[0]:
            daily[d] = (int(ts), int(close))

    dates = sorted(set(last_1m) & set(daily))
    vals = [daily[d][1] / last_1m[d][1] for d in dates if last_1m[d][1] > 0]
    idx = [d for d in dates if last_1m[d][1] > 0]
    return pd.Series(vals, index=pd.Index(idx, dtype="object", name="date"),
                     dtype="float64", name="split_ratio")


def detect_split_dates(df_1m: pd.DataFrame, df_1d: pd.DataFrame,
                       calendar: list[UsMarketDay], *,
                       threshold: float = SPLIT_RATIO_THRESHOLD) -> set[str]:
    """분할 매매일 집합 (사전등록 §7-e).

    `r` 이 **직전(계산 가능한) 매매일 대비 `threshold` 배 이상 또는 1/`threshold` 이하**로
    급변한 매매일을 분할일로 등록한다.

    경계조건
      * **첫 매매일은 분할일이 아니다** — 비교할 직전 매매일이 없어 r 변화를 정의할 수 없다.
      * r 을 계산할 수 없는 날(1분봉 또는 일봉 결측)은 건너뛰고, 다음 계산 가능한 날은
        **마지막으로 관측된 r** 과 비교한다. 결측 구간을 사이에 두고 분할이 일어나도
        놓치지 않기 위함이다.
      * 새 수집은 필요 없다 — 이미 보유한 1분봉(원주가)·일봉(수정주가)만 쓴다.
    """
    return split_scan_report(df_1m, df_1d, calendar,
                             threshold=threshold)["split_dates"]


def split_scan_report(df_1m: pd.DataFrame, df_1d: pd.DataFrame,
                      calendar: list[UsMarketDay], *,
                      threshold: float = SPLIT_RATIO_THRESHOLD) -> dict:
    """분할 스캔 결과 + §7-e 보고 의무 항목.

    반환 키
        split_dates          분할 매매일 date 집합
        n_r_uncomputable     r 을 계산할 수 없었던 매매일 수 (§7-e 보고 의무)
        r_uncomputable_dates 그 날짜 목록 (정렬)
        n_widened            결측 구간을 넘어 관측돼 **보수적으로 확대 등록**된 날 수
        n_observed_days      r 이 계산된 매매일 수

    **보수적 확대 등록 (3차 감사 F-2)**: 점프가 결측 구간을 넘어 관측되면
    `(마지막 관측 매매일, 관측 매매일]` 구간의 **모든 매매일**을 분할일로 등록한다.
    구간 내 어느 날이 진짜 분할 유효일인지는 원리상 알 수 없고, 관측일만 등록하면
    **정작 보호가 필요한 날(진짜 분할일)이 무방비로 남아** 가짜 ±N00% 갭 이벤트가
    카탈로그에 그대로 들어간다. 놓치는 것보다 넓게 잡는 쪽이 안전하다 —
    넓힌 날은 `split_excluded`(scope=`symbol_day`) 카운트로 크기가 드러난다.
    """
    r = split_ratio_series(df_1m, df_1d, calendar)
    observed = [str(d) for d in r.index]
    all_days = [md.date for md in (calendar or []) if session_windows(md)]
    uncomputable = sorted(set(all_days) - set(observed))

    order = {d: i for i, d in enumerate(sorted(all_days))}
    out: set[str] = set()
    widened = 0
    prev_date: str | None = None
    prev_val: float | None = None
    lo = 1.0 / threshold
    for d, v in r.items():
        d = str(d)
        if not (v == v and v > 0):
            continue
        if prev_val is not None:
            jump = v / prev_val
            if jump >= threshold or jump <= lo:
                # (마지막 관측일, 관측일] 의 모든 매매일 — 결측이 없으면 관측일 1개뿐이다
                i0 = order.get(prev_date, -1)
                i1 = order.get(d, -1)
                span = [x for x in sorted(all_days) if i0 < order[x] <= i1] or [d]
                out.update(span)
                widened += max(0, len(span) - 1)
        prev_date, prev_val = d, v
    return {"split_dates": out, "n_r_uncomputable": len(uncomputable),
            "r_uncomputable_dates": uncomputable, "n_widened": widened,
            "n_observed_days": len(observed)}


def detect_split_dates_by_symbol(df_1m: pd.DataFrame, df_1d: pd.DataFrame,
                                 calendar: list[UsMarketDay], *,
                                 threshold: float = SPLIT_RATIO_THRESHOLD
                                 ) -> dict[str, set[str]]:
    """다심볼 편의 래퍼 — `{symbol: {분할 매매일 date}}`."""
    if df_1m is None or len(df_1m) == 0 or "symbol" not in df_1m.columns:
        return {}
    out: dict[str, set[str]] = {}
    daily_has_symbol = df_1d is not None and len(df_1d) and "symbol" in df_1d.columns
    for sym, g in df_1m.groupby("symbol"):
        d1 = (df_1d[df_1d["symbol"] == sym] if daily_has_symbol else df_1d)
        out[str(sym)] = detect_split_dates(g, d1, calendar, threshold=threshold)
    return out


def prereg_daily_baseline(df_1d: pd.DataFrame, as_of_date: str,
                          calendar: list[UsMarketDay]) -> dict:
    """사전등록 §2.2 일봉 베이스라인 — 주 분석 진입점.

    평가일 `as_of_date` 기준 **엄격히 과거 20 매매일**만 쓴다. 서명이 (date, calendar) 인
    이유는 3차 감사 F-1 권고다 — raw ms 를 받으면 호출자가 `t0_ms` 를 넘기는 실수를 막을
    수 없고, 그러면 **모든 날에서** 평가일 자기 일봉이 통과한다.
    """
    if not calendar:
        raise ValueError("prereg_daily_baseline 은 calendar 가 필요하다 "
                         "(일봉 ts 를 매매일 date 로 매핑해야 한다 — 3차 감사 F-1)")
    return compute_daily_baseline(df_1d, DEFAULT_WINDOW_DAYS,
                                  as_of_date=as_of_date, calendar=calendar)


def prereg_volume_curve(df_1m: pd.DataFrame, calendar: list[UsMarketDay],
                        as_of_date: str) -> pd.Series:
    """사전등록 §2.2 를 **한 번에** 적용한 곡선 — 주 분석은 이것만 쓴다.

    평가일 `as_of_date` 기준 엄격히 과거 20 매매일, (세션, 길이) 버킷당 관측 10일 이상.
    두 값을 호출자가 따로 넘기다 잊는 사고를 막으려고 하나로 묶었다.
    """
    return minute_of_session_volume_curve(
        df_1m, calendar, window_days=PREREG_WINDOW_DAYS, min_days=PREREG_MIN_DAYS,
        as_of_date=as_of_date)


def curve_locate(curve: pd.Series, ts_ms: int, *,
                 calendar: list[UsMarketDay] | None = None
                 ) -> tuple[str, int, int, int] | None:
    """`ts_ms` 가 속한 세션 판정 → (session, minute, start_ms, end_ms)."""
    for start_ms, end_ms, name, _date in _session_table(curve, calendar):
        if start_ms <= ts_ms < end_ms:
            return name, int((ts_ms - start_ms) // MIN_MS), int(start_ms), int(end_ms)
    return None


def curve_key(curve: pd.Series, ts_ms: int, *,
              calendar: list[UsMarketDay] | None = None
              ) -> tuple[str, int, int] | None:
    """`ts_ms` → 곡선 색인 키 `(session, session_len_min, minute)` (M-4 이후).

    세션 길이가 키에 포함되므로 조기폐장일은 정상일 곡선을 **참조하지 않는다**.
    해당 길이의 버킷이 곡선에 없으면(관측 부족 등) 호출자는 NaN 을 받는다.
    """
    loc = curve_locate(curve, ts_ms, calendar=calendar)
    if loc is None:
        return None
    name, minute, start_ms, end_ms = loc
    return (name, int((end_ms - start_ms) // MIN_MS), minute)


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
    _name, _minute, start_ms, _end = loc
    key = curve_key(curve, ts_ms, calendar=calendar)
    cum: pd.Series | None = curve.attrs.get(CURVE_CUM_KEY)
    if cum is None or key is None or key not in cum.index:
        return float("nan")
    expected = float(cum.loc[key])
    if not expected > 0:
        return float("nan")
    sub = df_1m[(df_1m["ts_ms"] >= start_ms) & (df_1m["ts_ms"] <= ts_ms)]
    actual = float(sum(int(v) for v in sub["vol_qu"].tolist()))
    return actual / expected


def rvol_bar(df_1m: pd.DataFrame, curve: pd.Series, ts_ms: int, *,
             calendar: list[UsMarketDay] | None = None) -> float:
    """단일 분봉 기준 시간대 보정 상대거래량 (봉이 없으면 거래량 0 → 0.0)."""
    key = curve_key(curve, ts_ms, calendar=calendar)
    if key is None or key not in curve.index:
        return float("nan")
    expected = float(curve.loc[key])
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
    cum_map = ({(str(s), int(ln), int(m)): float(v) for (s, ln, m), v in cum.items()}
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
        # 키에 세션 길이를 포함한다 — 조기폐장일이 정상일 분모를 쓰지 않도록 (M-4)
        exp = cum_map.get((cur[2], int((cur[1] - cur[0]) // MIN_MS),
                           int((t - cur[0]) // MIN_MS)), 0.0)
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

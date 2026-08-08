"""전조 피처 추출 — 계약 C-7 + C-7 개정 A2. 소유: W3.

**룩어헤드 금지가 이 모듈의 존재 이유다.**
`t0_ms` 이후 데이터는 한 바이트도 섞이면 안 된다. 함수가 입력을 직접 잘라내고
(`cut_frame`), 이를 증명하는 테스트(`test_analysis_features.py`·
`test_cutoff_amendment_a2.py`)가 있다.

컷오프 (A2, 2026-08-09 — **봉과 스냅이 갈렸다. 한 플래그로 밀지 마라**)
    캔들(구간·종료 라벨) : `ts_ms   <= t0_ms`  ← `include_t0` 로 모드 선택, **기본 `<=`**
    랭킹(순간·도착 지연)  : `snap_ms <  t0_ms`  ← **항상 엄격. 플래그 대상이 아니다.**

    **왜 캔들이 `<=` 인가.** 예전 근거는 *"T0 봉의 종가·거래량은 그 분이 끝난 뒤에만
    관측되므로 T0 시점 판단에 쓸 수 없다"* 였다. 이건 **시작 시각 라벨 전제**라 성립하지
    않는다 — `candles_1m.ts_ms` 는 **종료 시각 라벨**이고(사전등록 §6.1, docs/36 §1)
    라벨 `t0` 인 봉은 `[t0−60초, t0)` 를 담아 **`t0` 에 이미 완결**돼 있다. 즉 T0 봉은
    실시간 검출기에만이 아니라 **누구에게나 t0 에 관측 가능**하다. `<` 는 완전히 관측된
    1분을 버려 `cutoff_lag_min ≥ 1` 이라는 구조적 60초 사각을 강제했다.

    **왜 랭킹은 `<` 인가.** 랭킹은 구간이 아니라 **순간**이고, W1 실측으로 도착 시
    **중앙 16.1초 늙어 있다**(docs/35, 서버 10초 격자). `snap_ms = t0` 인 스냅은 **t0 에
    우리 손에 없다** — 여기를 `<=` 로 넓히면 그것은 과보수 회수가 아니라 **진짜 룩어헤드**다.
    그래서 아래에서 랭킹 컷은 `include_t0` 를 **받지 않고** `False` 를 박아 넘긴다.

    **`cutoff_mode` 표기 (사전등록 §1 P1 개정 상자).** 반환 피처 `include_t0` 가 곧
    모드 태그다 — `1.0` = `obs_le`(개정 후), `0.0` = `strict_lt`(개정 전). 개정 전
    시행을 인용할 때 `strict_lt` 를 병기하기 위해 **엄격 모드는 지우지 않고 남긴다.**
    두 모드 값을 같은 표·분포·CI 에 섞으면 그 집계는 무효다.

반환값은 항상 `dict[str, float]` (bool 은 0.0/1.0, 미가용은 NaN). 키 집합은 입력 가용성과
무관하게 **항상 동일**하다 — 하류(`detector.precursor_score`)가 키 존재를 가정할 수 있어야
하기 때문이다. `feature_names()` 가 그 목록이다.
"""
from __future__ import annotations

import math
import warnings
from bisect import bisect_right

import pandas as pd

from ..api.models import SessionWindow, UsMarketDay
from .baselines import (MIN_MS, curve_key, curve_locate, daily_volume_z,
                        rvol_series, session_vwap_u, session_windows, true_range_u)
from .session import bar_start_ms

DEFAULT_WINDOWS_MIN = (5, 15, 30, 60)
#: `vol_z_{w}` 비교 기준 구간 길이(분) — 윈도우 직전 구간
VOL_Z_BASELINE_MIN = 240
VOL_Z_MIN_SAMPLES = 30
#: 누적 RVOL 최초 돌파 리드타임을 재는 임계값 (검증질문 1)
RVOL_CROSS_THRESHOLDS = (2.0, 3.0, 5.0)
#: 봉 단위 로그거래량 z 의 "이상" 임계 (세션 경계를 넘는 리드타임 측정용)
BAR_VOL_Z_SURGE = 3.0
#: 이력 이벤트 프록시 판정 (일중 저가→고가 상승률)
PRIOR_EVENT_RANGE_MIN = 0.15
PRIOR_EVENT_LOOKBACK_DAYS = 20
#: 세션 판정 불가 시 "당일" 대체 구간 (정규장 길이)
FALLBACK_SESSION_MIN = 390

TOSS_RANK_TYPE = "TOSS_SECURITIES_TRADING_AMOUNT"
MARKET_RANK_TYPE = "MARKET_TRADING_AMOUNT"

_NAN = float("nan")


# --------------------------------------------------------------------------- #
# 컷오프
# --------------------------------------------------------------------------- #
def cut_frame(df: pd.DataFrame, t0_ms: int, ts_col: str = "ts_ms", *,
              include_t0: bool = True) -> pd.DataFrame:
    """A2 컷오프 적용. 이 모듈의 모든 입력은 반드시 여기를 통과한다.

    기본은 **관측가능 컷오프**(`obs_le`, `<=`) — 종료 라벨이라 라벨 `t0_ms` 인 봉은
    `t0_ms` 에 완결돼 있다. `include_t0=False`(`strict_lt`, `<`)는 **1봉 과보수**이며
    개정 전 수치를 재현·병기하기 위해 남겨 둔 모드다 (모듈 docstring 참조).

    **랭킹(`ts_col="snap_ms"`)에 이 함수를 쓸 때는 `include_t0=False` 를 박아 넘겨라.**
    A2 §2 는 랭킹의 `<` 를 플래그 없는 불변식으로 정했다 — 유일한 호출부는
    `extract_precursor_features` 이고 거기서 그렇게 한다.
    """
    if df is None or len(df) == 0:
        return df if df is not None else pd.DataFrame()
    mask = (df[ts_col] <= t0_ms) if include_t0 else (df[ts_col] < t0_ms)
    return df[mask]


# --------------------------------------------------------------------------- #
# 작은 통계 유틸 (전부 컷오프 이후 데이터에만 적용)
# --------------------------------------------------------------------------- #
def _slope(xs: list[float], ys: list[float]) -> float:
    """단순 OLS 기울기 (분당 변화량). 표본 2개 미만이면 NaN."""
    n = len(xs)
    if n < 2:
        return _NAN
    mx, my = sum(xs) / n, sum(ys) / n
    var = sum((x - mx) ** 2 for x in xs)
    if var <= 0:
        return _NAN
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var


def _mean_std(vals: list[float]) -> tuple[float, float]:
    n = len(vals)
    if n < 2:
        return (_NAN, _NAN)
    m = sum(vals) / n
    return m, math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))


def _minute_volumes(pre: pd.DataFrame, t_from: int, t_to: int) -> list[float]:
    """**내용 구간** [t_from, t_to) 의 분 단위 거래량. 봉이 없는 분은 0 (미체결).

    `t_from`/`t_to` 는 **거래가 일어난 시각**의 구간이지 라벨 구간이 아니다.
    슬롯 i = `[t_from + i·60초, t_from + (i+1)·60초)` 의 내용을 담은 봉은 **그 슬롯
    끝에 라벨된 봉**이므로(종료 라벨, docs/12 §6.1), 라벨로는 `(t_from, t_to]` 를 모은다.
    """
    if t_to <= t_from or pre.empty:
        return []
    have = {int(t): int(v) for t, v in zip(pre["ts_ms"].tolist(), pre["vol_qu"].tolist())
            if t_from < int(t) <= t_to}
    n = (t_to - t_from) // MIN_MS
    return [float(have.get(t_from + (i + 1) * MIN_MS, 0)) for i in range(n)]


DAY_MS = 86_400_000


def market_day_spans(calendar) -> list[tuple[int, int, str]]:
    """캘린더 → 시간순 `(매매일 시작 ms, 종료 ms, date)` 목록. 휴장일은 빠진다."""
    out: list[tuple[int, int, str]] = []
    for md in (calendar or []):
        wins = session_windows(md)
        if wins:
            out.append((int(wins[0][1].start_ms), int(wins[-1][1].end_ms), md.date))
    return sorted(out)


def assign_market_days(ts_list: list[int],
                       spans: list[tuple[int, int, str]]) -> list[str | None]:
    """각 **캔들 라벨** ts 를 매매일 `date` 로 매핑. 캘린더 밖이면 None. (O(n log d))

    **UTC 날짜로 묶으면 안 된다** — 겨울(EST)에는 애프터장이 UTC 자정을 넘어 하나의
    매매일이 두 UTC 날짜로 쪼개진다 (감사 M-3, docs/07 §3.1a).
    소속은 라벨이 아니라 **봉이 담는 구간**으로 판정한다 (docs/12 §6.1).
    """
    if not spans:
        return [None] * len(ts_list)
    starts = [s[0] for s in spans]
    out: list[str | None] = []
    for t in ts_list:
        b = bar_start_ms(t)
        j = bisect_right(starts, b) - 1
        out.append(spans[j][2] if (j >= 0 and spans[j][0] <= b < spans[j][1]) else None)
    return out


def _locate_day_start(cutoff: int, calendar) -> int:
    """cutoff 가 속한 **매매일** 시작 시각.

    캘린더가 있으면 `UsMarketDay` 기준. 없으면 UTC 날짜 경계로 폴백하는데,
    **이 폴백은 겨울(EST)에 틀린다** — 매매일이 UTC 자정을 넘기 때문이다(M-3).
    호출부는 캘린더를 주는 것이 원칙이며, 폴백 사용 사실은
    `day_grouping_calendar` 피처로 드러난다.
    """
    b = bar_start_ms(cutoff)                       # 소속은 봉이 담는 구간으로 (§6.1)
    for start_ms, end_ms, _date in market_day_spans(calendar):
        if start_ms <= b < end_ms:
            return start_ms
    return (b // DAY_MS) * DAY_MS


def _first_bar_vol_z_cross(pre: pd.DataFrame, day_lo: int, t_hi: int,
                           thr: float) -> int | None:
    """매매일 내에서 **봉 단위** 로그거래량 z 가 `thr` 을 처음 넘은 봉의 ts_ms(라벨).

    누적 RVOL 과 달리 세션 상대량이 아니라 자기 이력(직전 240분) 대비이므로
    세션을 넘어 비교해도 정의가 일관된다 (프리마켓 전조 → 정규장 T0 를 잡을 수 있다).
    `t_hi` 는 **내용 구간**의 끝(= 마지막 봉의 라벨)이다.
    """
    lo = day_lo - VOL_Z_BASELINE_MIN * MIN_MS
    vals = _minute_volumes(pre, lo, t_hi)
    n_base = VOL_Z_BASELINE_MIN
    if len(vals) <= n_base:
        return None
    logs = [math.log1p(v) for v in vals]
    s = sum(logs[:n_base])
    s2 = sum(x * x for x in logs[:n_base])
    for i in range(n_base, len(logs)):
        mean = s / n_base
        var = max(0.0, s2 / n_base - mean * mean) * n_base / (n_base - 1)
        sd = math.sqrt(var)
        if sd > 0 and (logs[i] - mean) / sd >= thr:
            return lo + (i + 1) * MIN_MS       # 슬롯 i 를 담은 봉의 **라벨**
        s += logs[i] - logs[i - n_base]
        s2 += logs[i] * logs[i] - logs[i - n_base] * logs[i - n_base]
    return None


def _locate_session_start(cutoff: int, curve, calendar) -> int | None:
    """cutoff 가 속한 세션의 시작 시각. curve → calendar 순으로 시도."""
    if curve is not None and not curve.empty:
        loc = curve_locate(curve, cutoff, calendar=calendar)
        if loc is not None:
            return loc[2]
    b = bar_start_ms(cutoff)                       # 소속은 봉이 담는 구간으로 (§6.1)
    for md in (calendar or []):
        for name in ("day", "pre", "regular", "after"):
            w = getattr(md, name)
            if w is not None and w.start_ms <= b < w.end_ms:
                return w.start_ms
    return None


# --------------------------------------------------------------------------- #
# C-7: extract_precursor_features
# --------------------------------------------------------------------------- #
def extract_precursor_features(df_1m: pd.DataFrame, rankings: pd.DataFrame,
                               t0_ms: int,
                               windows_min: tuple[int, ...] = DEFAULT_WINDOWS_MIN, *,
                               include_t0: bool = True,
                               symbol: str | None = None,
                               curve: pd.Series | None = None,
                               calendar: list[UsMarketDay] | None = None,
                               baseline: dict | None = None,
                               shares_outstanding_qu: int | None = None,
                               prior_events: pd.DataFrame | None = None,
                               prev_close_u: int | None = None,
                               split_dates: set[str] | None = None,
                               toss_type: str = TOSS_RANK_TYPE,
                               market_type: str = MARKET_RANK_TYPE) -> dict[str, float]:
    """T0 이전 구간만으로 전조 피처를 만든다. 반환 키는 `feature_names()` 와 동일.

    위치인자는 계약 C-7 그대로. 키워드 전용 인자는 A1 §2 확장:
        include_t0             A2. **캔들 컷오프 모드에만 작용한다** — 기본 True
                               (`obs_le`, `ts_ms <= t0_ms`). False 는 개정 전 재현용
                               (`strict_lt`). **랭킹은 이 값과 무관하게 언제나
                               `snap_ms < t0_ms` 엄격이다** (A2 §2)
        symbol                 랭킹 필터. None 이면 df_1m 의 symbol 컬럼에서 추론
        curve                  `minute_of_session_volume_curve()` — 시간대 보정 RVOL 분모
        calendar               세션 판정용 (곡선은 이력일, 적용은 당일인 정상 운용)
        baseline               `compute_daily_baseline()` — 일봉 z·ATR% 참조
        shares_outstanding_qu  진행분 플로트 로테이션 분모
        prior_events           이력 이벤트(t0_ms 컬럼). 없으면 df_1m 이전 날들로 프록시 계산
        prev_close_u           직전 매매일 **정규장 마지막 1분봉 종가(원주가)**.
                               `gap_from_prev_close` 전용이며 없으면 그 피처는 NaN —
                               일봉(수정주가) 대체는 금지다 (사전등록 §2.3)
        split_dates            분할 매매일 date 집합 (사전등록 §7-e). 컷오프가 분할일에
                               속하면 `gap_from_prev_close` 를 NaN 으로 둔다 — 원주가
                               계열에서 분할 전일 종가와 비교하면 가짜 갭이 나온다
        toss_type/market_type  토스 쏠림도에 쓸 랭킹 type 2종
    """
    if split_dates is not None and calendar is None:
        # 3차 감사 F-3 계열: calendar 없이는 컷오프의 매매일을 알 수 없어 무음 스킵된다.
        raise ValueError("split_dates 를 쓰려면 calendar 가 필요하다 (3차 감사 F-3)")
    pre = cut_frame(df_1m, t0_ms, include_t0=include_t0).sort_values("ts_ms")
    # A2 §2: 랭킹은 **캔들 플래그를 따라가지 않는다.** 봉은 구간이라 라벨 t0 봉이 t0 에
    # 완결되지만, 스냅은 순간이고 도착 시 중앙 16.1초 늙어 있다(docs/35) — `snap_ms = t0`
    # 인 스냅은 t0 에 손에 없다. 여기에 `include_t0` 를 흘리면 진짜 룩어헤드가 된다.
    rk = cut_frame(rankings, t0_ms, "snap_ms", include_t0=False)

    if symbol is None and df_1m is not None and len(df_1m) and "symbol" in df_1m.columns:
        uniq = df_1m["symbol"].dropna().unique()
        symbol = str(uniq[0]) if len(uniq) == 1 else None

    feats: dict[str, float] = {k: _NAN for k in feature_names(windows_min)}
    feats["t0_ms"] = float(t0_ms)
    feats["include_t0"] = 1.0 if include_t0 else 0.0
    feats["n_bars_pre"] = float(len(pre))
    if pre.empty:
        return feats

    cutoff = int(pre["ts_ms"].to_numpy()[-1])          # 실제로 관측된 마지막 봉
    close_cut = int(pre["close_u"].to_numpy()[-1])
    feats["cutoff_ms"] = float(cutoff)
    feats["cutoff_lag_min"] = float((t0_ms - cutoff) // MIN_MS)
    feats["close_cut_u"] = float(close_cut)
    sess_start = _locate_session_start(cutoff, curve, calendar)

    _volume_features(feats, pre, cutoff, windows_min, curve, calendar, baseline,
                     sess_start)
    # 사전등록 §7-e: 분할 매매일이면 전일 종가 비교 자체가 무의미하다.
    cutoff_day = None
    cutoff_b = bar_start_ms(cutoff)                # 소속은 봉이 담는 구간으로 (§6.1)
    for _lo, _hi, _d in market_day_spans(calendar):
        if _lo <= cutoff_b < _hi:
            cutoff_day = _d
            break
    if split_dates and cutoff_day is not None and cutoff_day in split_dates:
        prev_close_u = None
    _price_features(feats, pre, cutoff, close_cut, windows_min, baseline, sess_start,
                    prev_close_u)
    _print_activity_features(feats, pre, cutoff, windows_min, sess_start)
    _toss_concentration_features(feats, rk, symbol, cutoff, toss_type, market_type)
    _history_features(feats, pre, cutoff, prior_events, shares_outstanding_qu,
                      sess_start, calendar)
    return feats


def feature_names(windows_min: tuple[int, ...] = DEFAULT_WINDOWS_MIN) -> list[str]:
    """가용성과 무관하게 항상 반환되는 피처 키 목록 (하류 계약)."""
    names = ["t0_ms", "cutoff_ms", "cutoff_lag_min", "include_t0", "n_bars_pre",
             "close_cut_u",
             # 거래량
             "vol_slope_30", "vol_bar_z_max_60", "rvol_at_cutoff", "daily_vol_z",
             "atr20_pct", "vol_surge_lead_min",
             # 가격 궤적
             "coil_score", "nr_ratio", "dist_from_vwap", "dist_from_hod",
             "up_bar_ratio_30", "new_high_count_30", "gap_from_prev_close",
             # 체결 활동 (A2 §3 no_print / staleness / first_print 프록시)
             "minutes_since_last_print", "session_print_age_min",
             "session_first_print_lead_min", "dormant_ratio_prior_day",
             # 토스 쏠림도
             "toss_share", "toss_share_max", "toss_share_slope_30", "toss_in_ranking",
             "toss_rank_best", "market_rank_best", "minutes_since_toss_entry",
             "ranking_snaps_pre",
             # 이력
             "prior_event_count_20d", "days_since_prior_event", "former_runner",
             "hist_days_available", "float_rotation_pre",
             # 진단: 매매일 그룹화가 캘린더 기준이었나(1.0) UTC 날짜 폴백이었나(0.0) — M-3
             "day_grouping_calendar"]
    for w in windows_min:
        names += [f"vol_z_{w}", f"vol_ratio_{w}", f"rvol_curve_{w}", f"vol_sum_{w}_qu",
                  f"ret_{w}", f"atr_pct_{w}", f"range_pct_{w}", f"no_print_ratio_{w}"]
    names += [f"rvol_first_cross_{thr:g}_lead_min" for thr in RVOL_CROSS_THRESHOLDS]
    return names


# --------------------------------------------------------------------------- #
# 거래량 피처
# --------------------------------------------------------------------------- #
def _expected_window_vol(curve, calendar, t_from: int, t_to: int) -> float:
    """곡선 기준 **내용 구간** [t_from, t_to) 기대 거래량 합. 곡선에 없는 분은 건너뛴다."""
    if curve is None or curve.empty:
        return _NAN
    total, seen = 0.0, 0
    # 슬롯 [ts−60초, ts) 를 담은 봉의 라벨은 `ts` 다 — `curve_key` 는 라벨을 받는다 (§6.1)
    for ts in range(t_from + MIN_MS, t_to + MIN_MS, MIN_MS):
        key = curve_key(curve, ts, calendar=calendar)   # (session, len, minute) — M-4
        if key is not None and key in curve.index:
            total += float(curve.loc[key])
            seen += 1
    return total if seen else _NAN


def _volume_features(feats: dict, pre: pd.DataFrame, cutoff: int,
                     windows_min: tuple[int, ...], curve, calendar, baseline,
                     sess_start: int | None) -> None:
    # 창은 **내용 구간**으로 잡는다. cutoff 봉의 내용은 `[cutoff−60초, cutoff)` 이므로
    # 그 봉까지 포함하는 창의 끝은 `cutoff` 다 (종료 라벨, docs/12 §6.1).
    t_hi = cutoff
    for w in windows_min:
        t_lo = t_hi - w * MIN_MS
        vols = _minute_volumes(pre, t_lo, t_hi)
        if not vols:
            continue
        feats[f"vol_sum_{w}_qu"] = float(sum(vols))

        # 로그 공간 z-score: 직전 VOL_Z_BASELINE_MIN 분과 비교 (자기완결적 정의)
        base = _minute_volumes(pre, t_lo - VOL_Z_BASELINE_MIN * MIN_MS, t_lo)
        if len(base) >= VOL_Z_MIN_SAMPLES:
            bm, bs = _mean_std([math.log1p(v) for v in base])
            wm = sum(math.log1p(v) for v in vols) / len(vols)
            if bs == bs and bs > 0:
                feats[f"vol_z_{w}"] = (wm - bm) / bs
            bmean = sum(base) / len(base)
            if bmean > 0:
                feats[f"vol_ratio_{w}"] = (sum(vols) / len(vols)) / bmean

        exp = _expected_window_vol(curve, calendar, t_lo, t_hi)
        if exp == exp and exp > 0:
            feats[f"rvol_curve_{w}"] = float(sum(vols)) / exp

    vols30 = _minute_volumes(pre, t_hi - 30 * MIN_MS, t_hi)
    if len(vols30) >= 10:
        feats["vol_slope_30"] = _slope([float(i) for i in range(len(vols30))],
                                       [math.log1p(v) for v in vols30])

    v60 = _minute_volumes(pre, t_hi - 60 * MIN_MS, t_hi)
    base60 = _minute_volumes(pre, t_hi - (60 + VOL_Z_BASELINE_MIN) * MIN_MS,
                             t_hi - 60 * MIN_MS)
    if v60 and len(base60) >= VOL_Z_MIN_SAMPLES:
        bm, bs = _mean_std([math.log1p(v) for v in base60])
        if bs == bs and bs > 0:
            feats["vol_bar_z_max_60"] = max((math.log1p(v) - bm) / bs for v in v60)

    if baseline:
        atrp = baseline.get("atr20_pct", _NAN)
        feats["atr20_pct"] = float(atrp) if atrp is not None else _NAN
        lo = sess_start if sess_start is not None else t_hi - FALLBACK_SESSION_MIN * MIN_MS
        day_vol = sum(_minute_volumes(pre, lo, t_hi))
        if day_vol > 0:
            feats["daily_vol_z"] = daily_volume_z(baseline, int(day_vol))

    # 누적 RVOL 과 임계 최초 돌파 리드타임 (검증질문 1의 입력).
    # **T0 가 속한 세션 안에서만** 잰다 — 누적 RVOL 은 세션 상대량이라 세션을 넘어
    # 비교하면 정의가 깨지고, 얇은 세션(day/after)의 작은 분모가 허위 돌파를 만든다
    # (docs/07 §2.5). 세션 경계를 넘는 리드타임은 vol_surge_lead_min 으로 본다.
    if curve is not None and not curve.empty:
        rv = rvol_series(pre, curve, calendar=calendar).dropna()
        if sess_start is not None:
            # 라벨 `sess_start` 인 봉은 내용이 **직전** 세션의 마지막 분이다 (§6.1)
            rv = rv[rv.index > sess_start]
        if not rv.empty:
            feats["rvol_at_cutoff"] = float(rv.iloc[-1])
            for thr in RVOL_CROSS_THRESHOLDS:
                hit = rv[rv >= thr]
                if not hit.empty:
                    feats[f"rvol_first_cross_{thr:g}_lead_min"] = float(
                        (feats["t0_ms"] - int(hit.index[0])) // MIN_MS)

    day_lo = _locate_day_start(cutoff, calendar)
    hit_ms = _first_bar_vol_z_cross(pre, day_lo, t_hi, BAR_VOL_Z_SURGE)
    if hit_ms is not None:
        feats["vol_surge_lead_min"] = float((feats["t0_ms"] - hit_ms) // MIN_MS)


# --------------------------------------------------------------------------- #
# 가격 궤적 피처
# --------------------------------------------------------------------------- #
def _range_pct(sub: pd.DataFrame, ref_u: int) -> float:
    if sub.empty or ref_u <= 0:
        return _NAN
    return (int(sub["high_u"].max()) - int(sub["low_u"].min())) / ref_u


def _atr_pct(sub: pd.DataFrame, ref_u: int) -> float:
    if len(sub) < 2 or ref_u <= 0:
        return _NAN
    tr = true_range_u(sub)
    return (sum(int(x) for x in tr) / len(tr)) / ref_u


def _price_features(feats: dict, pre: pd.DataFrame, cutoff: int, close_cut: int,
                    windows_min: tuple[int, ...], baseline,
                    sess_start: int | None,
                    prev_close_u: int | None = None) -> None:
    for w in windows_min:
        sub = pre[pre["ts_ms"] > cutoff - w * MIN_MS]
        if sub.empty:
            continue
        feats[f"ret_{w}"] = close_cut / int(sub["close_u"].to_numpy()[0]) - 1.0
        feats[f"atr_pct_{w}"] = _atr_pct(sub, close_cut)
        feats[f"range_pct_{w}"] = _range_pct(sub, close_cut)

    # coil = 직전 구간 대비 최근 구간의 변동성 수축. 겹치지 않는 두 구간을 비교.
    recent = pre[pre["ts_ms"] > cutoff - 15 * MIN_MS]
    prior = pre[(pre["ts_ms"] > cutoff - 60 * MIN_MS)
                & (pre["ts_ms"] <= cutoff - 15 * MIN_MS)]
    a_recent, a_prior = _atr_pct(recent, close_cut), _atr_pct(prior, close_cut)
    if a_recent == a_recent and a_prior == a_prior and a_recent > 0 and a_prior > 0:
        feats["coil_score"] = math.log(a_prior / a_recent)
    r15 = _range_pct(recent, close_cut)
    r60 = _range_pct(pre[pre["ts_ms"] > cutoff - 60 * MIN_MS], close_cut)
    if r15 == r15 and r60 == r60 and r60 > 0:
        feats["nr_ratio"] = r15 / r60

    lo = sess_start if sess_start is not None else cutoff - FALLBACK_SESSION_MIN * MIN_MS
    # 라벨 `lo` 인 봉은 내용이 세션 시작 **전**이라 세션 밖이다 (docs/12 §6.1)
    sess = pre[pre["ts_ms"] > lo]
    if sess.empty:
        return
    vw = session_vwap_u(sess, SessionWindow(start_ms=lo, end_ms=cutoff))
    if not vw.empty and int(vw.iloc[-1]) > 0:
        feats["dist_from_vwap"] = close_cut / int(vw.iloc[-1]) - 1.0
    hod = int(sess["high_u"].max())
    if hod > 0:
        feats["dist_from_hod"] = close_cut / hod - 1.0

    s30 = sess[sess["ts_ms"] > cutoff - 30 * MIN_MS]
    if not s30.empty:
        feats["up_bar_ratio_30"] = float(int((s30["close_u"] > s30["open_u"]).sum())
                                         / len(s30))
    # 최근 30분 내 세션 신고가 갱신 횟수 (돌파 압력)
    running_hi, cnt = 0, 0
    for ts, h in zip(sess["ts_ms"].tolist(), sess["high_u"].tolist()):
        h = int(h)
        if h > running_hi:
            running_hi = h
            if int(ts) > cutoff - 30 * MIN_MS:
                cnt += 1
    feats["new_high_count_30"] = float(cnt)

    # 사전등록 §2.3: 1분봉(원주가)과 일봉(수정주가)의 **가격 수준 직접 비교 금지**.
    # 전일 종가는 반드시 직전 매매일 정규장 마지막 1분봉 종가(원주가)를 명시로 받는다.
    # baseline["close_last_u"] 는 수정주가라 여기에 쓰면 분할 종목에서 가짜 갭이 만들어진다.
    if prev_close_u is not None and int(prev_close_u) > 0:
        feats["gap_from_prev_close"] = close_cut / int(prev_close_u) - 1.0


# --------------------------------------------------------------------------- #
# 체결 활동 피처 (A2 §3: no_print / staleness_s / first_print 의 1분봉 프록시)
# --------------------------------------------------------------------------- #
def _print_activity_features(feats: dict, pre: pd.DataFrame, cutoff: int,
                             windows_min: tuple[int, ...],
                             sess_start: int | None) -> None:
    """`/prices.timestamp` 파생상태의 캔들 기반 프록시.

    실시간 경로(W4)는 `/prices` 의 no_print/staleness_s/first_print 를 직접 쓰고,
    과거 백필 경로(연구)는 **봉의 부재**로 같은 상태를 복원한다. 해상도가 1분이고
    봉이 있어도 체결이 1건뿐일 수 있다는 한계가 있다 (docs/07 §5.3).
    """
    t_hi = cutoff                              # 내용 구간의 끝 (§6.1, `_volume_features` 동일)
    for w in windows_min:
        vols = _minute_volumes(pre, t_hi - w * MIN_MS, t_hi)
        if vols:
            feats[f"no_print_ratio_{w}"] = sum(1 for v in vols if v <= 0) / len(vols)

    ts = [int(t) for t in pre["ts_ms"].tolist()]
    if len(ts) >= 2:
        feats["minutes_since_last_print"] = float((ts[-1] - ts[-2]) // MIN_MS - 1)
    elif ts:
        feats["minutes_since_last_print"] = 0.0

    if sess_start is None:
        return
    in_sess = [t for t in ts if t > sess_start]     # 라벨 sess_start 봉은 직전 세션 (§6.1)
    if in_sess:
        feats["session_print_age_min"] = float((cutoff - in_sess[0]) // MIN_MS)
        # first_print 프록시: 세션 시작 후 첫 체결까지 걸린 시간 (작을수록 활발).
        # 첫 분부터 체결이 있으면 0 이어야 하므로 **봉이 담는 구간의 시작**으로 잰다.
        feats["session_first_print_lead_min"] = float(
            (bar_start_ms(in_sess[0]) - sess_start) // MIN_MS)
    prior = _minute_volumes(pre, sess_start - 24 * 60 * MIN_MS, sess_start)
    if prior:
        feats["dormant_ratio_prior_day"] = sum(1 for v in prior if v <= 0) / len(prior)


# --------------------------------------------------------------------------- #
# 토스 쏠림도 (docs/01 §3.2 — TOSS/MARKET 거래대금 비율의 레벨·기울기)
# --------------------------------------------------------------------------- #
def _toss_concentration_features(feats: dict, rk: pd.DataFrame, symbol: str | None,
                                 cutoff: int, toss_type: str, market_type: str) -> None:
    feats["ranking_snaps_pre"] = 0.0
    feats["toss_in_ranking"] = 0.0
    if rk is None or len(rk) == 0 or symbol is None:
        return
    mine = rk[rk["symbol"] == symbol] if "symbol" in rk.columns else rk
    if mine.empty:
        return
    feats["ranking_snaps_pre"] = float(len(mine))

    has_type = "ranking_type" in mine.columns
    toss = mine[mine["ranking_type"] == toss_type] if has_type else mine
    mkt = mine[mine["ranking_type"] == market_type] if has_type else mine.iloc[0:0]

    if not toss.empty:
        feats["toss_in_ranking"] = 1.0
        feats["toss_rank_best"] = float(int(toss["rank"].min()))
        feats["minutes_since_toss_entry"] = float(
            (cutoff - int(toss["snap_ms"].min())) // MIN_MS)
    if not mkt.empty:
        feats["market_rank_best"] = float(int(mkt["rank"].min()))
    if toss.empty or mkt.empty:
        return

    # 같은 스냅샷의 TOSS/MARKET 거래대금 비율. 실데이터에서 1 을 넘을 수 있으므로
    # 클리핑하지 않는다 (docs/07 §5.2).
    t_map = {int(s): int(a) for s, a in zip(toss["snap_ms"].tolist(),
                                            toss["amount_u"].tolist())}
    m_map = {int(s): int(a) for s, a in zip(mkt["snap_ms"].tolist(),
                                            mkt["amount_u"].tolist())}
    shares = [(s, t_map[s] / m_map[s]) for s in sorted(set(t_map) & set(m_map))
              if m_map[s] > 0]
    if not shares:
        return
    feats["toss_share"] = shares[-1][1]
    feats["toss_share_max"] = max(v for _s, v in shares)
    recent = [(s, v) for s, v in shares if s > cutoff - 30 * MIN_MS]
    if len(recent) >= 2:
        feats["toss_share_slope_30"] = _slope(
            [(s - recent[0][0]) / MIN_MS for s, _v in recent], [v for _s, v in recent])


# --------------------------------------------------------------------------- #
# 이력 피처
# --------------------------------------------------------------------------- #
def _history_features(feats: dict, pre: pd.DataFrame, cutoff: int,
                      prior_events: pd.DataFrame | None,
                      shares_outstanding_qu: int | None,
                      sess_start: int | None,
                      calendar=None) -> None:
    """이력 피처. **매매일 단위로 묶는다 — UTC 날짜가 아니다** (감사 M-3).

    겨울(EST)에는 애프터장이 UTC 자정을 넘으므로 UTC 날짜로 묶으면 하나의 매매일이
    둘로 쪼개져 `hist_days_available` 가 부풀고 former-runner 프록시가 **이중 계산**된다.
    former runner 는 유니버스 선정의 핵심 축이라(docs/02 §2.4) 그대로 두면 러너 이력이
    조작된 채 분석에 들어간다.

    캘린더가 없거나 봉을 다 덮지 못하면 UTC 날짜로 폴백하되, `day_grouping_calendar=0.0`
    으로 **결과에 드러내고** 경고를 낸다 — 조용히 틀리는 것이 최악이다.
    """
    ts = [int(t) for t in pre["ts_ms"].tolist()]
    spans = market_day_spans(calendar)
    day_of = assign_market_days(ts, spans) if (ts and spans) else [None] * len(ts)
    covered = bool(ts) and all(d is not None for d in day_of)
    feats["day_grouping_calendar"] = 1.0 if covered else 0.0

    if ts and not covered:
        warnings.warn(
            "features: 매매일 그룹화가 UTC 날짜 폴백으로 내려갔다 "
            "(캘린더 미제공 또는 구간 미포함). 겨울(EST)에는 매매일이 UTC 자정을 넘어 "
            "hist_days_available·former-runner 프록시가 이중 계산된다 — "
            "calendar= 를 넘겨라 (감사 M-3, docs/07 §3.1a).",
            RuntimeWarning, stacklevel=3)

    if ts:
        keys = day_of if covered else [str(bar_start_ms(t) // DAY_MS) for t in ts]
        feats["hist_days_available"] = float(len(set(keys)))

    if prior_events is not None and len(prior_events):
        pe = prior_events[prior_events["t0_ms"] < cutoff]
        feats["prior_event_count_20d"] = float(
            len(pe[pe["t0_ms"] >= cutoff - PRIOR_EVENT_LOOKBACK_DAYS * DAY_MS]))
        if not pe.empty:
            feats["days_since_prior_event"] = (cutoff - int(pe["t0_ms"].max())) / DAY_MS
    elif ts:
        # 프록시: 컷오프 **이전 매매일들** 중 일중 저가→고가 상승률이 임계 이상이었던 날 수
        lo_ms = cutoff - PRIOR_EVENT_LOOKBACK_DAYS * DAY_MS
        if covered:
            cut_b = bar_start_ms(cutoff)           # 소속은 봉이 담는 구간으로 (§6.1)
            cur = next((d for (s, e, d) in spans if s <= cut_b < e), None)
            buckets: dict[str, list[int]] = {}
            for t, d in zip(ts, day_of):
                if d is not None and d != cur and lo_ms <= t < cutoff:
                    buckets.setdefault(d, []).append(t)
        else:
            cur = str(bar_start_ms(cutoff) // DAY_MS)
            buckets = {}
            for t in ts:
                d = str(bar_start_ms(t) // DAY_MS)
                if d != cur and lo_ms <= t < cutoff:
                    buckets.setdefault(d, []).append(t)

        cnt, last_ms = 0, None
        for _d, day_ts in sorted(buckets.items()):
            sub = pre[(pre["ts_ms"] >= min(day_ts)) & (pre["ts_ms"] <= max(day_ts))]
            if sub.empty:
                continue
            lo_u, hi_u = int(sub["low_u"].min()), int(sub["high_u"].max())
            if lo_u > 0 and hi_u / lo_u - 1.0 >= PRIOR_EVENT_RANGE_MIN:
                cnt += 1
                last_ms = int(sub["ts_ms"].to_numpy()[-1])
        feats["prior_event_count_20d"] = float(cnt)
        if last_ms is not None:
            feats["days_since_prior_event"] = (cutoff - last_ms) / DAY_MS

    c = feats.get("prior_event_count_20d", _NAN)
    if c == c:
        feats["former_runner"] = 1.0 if c > 0 else 0.0

    if shares_outstanding_qu:
        lo = sess_start if sess_start is not None \
            else cutoff - FALLBACK_SESSION_MIN * MIN_MS
        vol = sum(int(v) for t, v in zip(pre["ts_ms"].tolist(), pre["vol_qu"].tolist())
                  if int(t) > lo)          # 라벨 `lo` 봉은 세션 시작 전 내용 (§6.1)
        feats["float_rotation_pre"] = vol / int(shares_outstanding_qu)

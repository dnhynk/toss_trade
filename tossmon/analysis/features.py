"""전조 피처 추출 — 계약 C-7 + C-7 개정 A1. 소유: W3.

**룩어헤드 금지가 이 모듈의 존재 이유다.**
`t0_ms` 이후 데이터는 한 바이트도 섞이면 안 된다. 함수가 입력을 직접 잘라내고
(`cut_frame`), 이를 증명하는 테스트(`test_analysis_features.py`)가 있다.

컷오프 (A1 §1)
    include_t0=False (기본, 연구용) : `ts_ms <  t0_ms` — T0 봉 자체도 제외.
        근거: T0 봉의 종가·거래량은 그 분이 끝난 뒤에만 관측되므로 T0 시점 판단에 쓸 수 없다.
    include_t0=True  (W4 실시간)    : `ts_ms <= t0_ms` — T0 봉 종료 시점에 판정하므로 합법.
    랭킹도 동일 규칙(`snap_ms`)을 적용한다.

반환값은 항상 `dict[str, float]` (bool 은 0.0/1.0, 미가용은 NaN). 키 집합은 입력 가용성과
무관하게 **항상 동일**하다 — 하류(`detector.precursor_score`)가 키 존재를 가정할 수 있어야
하기 때문이다. `feature_names()` 가 그 목록이다.
"""
from __future__ import annotations

import math

import pandas as pd

from ..api.models import SessionWindow, UsMarketDay
from .baselines import (MIN_MS, curve_locate, daily_volume_z, rvol_series,
                        session_vwap_u, true_range_u)

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
              include_t0: bool = False) -> pd.DataFrame:
    """A1 §1 컷오프 적용. 이 모듈의 모든 입력은 반드시 여기를 통과한다."""
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
    """[t_from, t_to) 의 **분 단위** 거래량. 봉이 없는 분은 0 (미체결)."""
    if t_to <= t_from or pre.empty:
        return []
    have = {int(t): int(v) for t, v in zip(pre["ts_ms"].tolist(), pre["vol_qu"].tolist())
            if t_from <= int(t) < t_to}
    n = (t_to - t_from) // MIN_MS
    return [float(have.get(t_from + i * MIN_MS, 0)) for i in range(n)]


def _locate_day_start(cutoff: int, calendar) -> int:
    """cutoff 가 속한 **매매일** 시작 시각. calendar 없으면 UTC 날짜 경계

    (토스 4세션은 UTC 00:00~22:00 에 들어가므로 UTC 날짜 = 매매일 — docs/07 §3.1).
    """
    for md in (calendar or []):
        wins = [getattr(md, n) for n in ("day", "pre", "regular", "after")]
        wins = [w for w in wins if w is not None]
        if wins and wins[0].start_ms <= cutoff < wins[-1].end_ms:
            return int(wins[0].start_ms)
    return (cutoff // 86_400_000) * 86_400_000


def _first_bar_vol_z_cross(pre: pd.DataFrame, day_lo: int, t_hi: int,
                           thr: float) -> int | None:
    """매매일 내에서 **봉 단위** 로그거래량 z 가 `thr` 을 처음 넘은 봉의 ts_ms.

    누적 RVOL 과 달리 세션 상대량이 아니라 자기 이력(직전 240분) 대비이므로
    세션을 넘어 비교해도 정의가 일관된다 (프리마켓 전조 → 정규장 T0 를 잡을 수 있다).
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
            return lo + i * MIN_MS
        s += logs[i] - logs[i - n_base]
        s2 += logs[i] * logs[i] - logs[i - n_base] * logs[i - n_base]
    return None


def _locate_session_start(cutoff: int, curve, calendar) -> int | None:
    """cutoff 가 속한 세션의 시작 시각. curve → calendar 순으로 시도."""
    if curve is not None and not curve.empty:
        loc = curve_locate(curve, cutoff, calendar=calendar)
        if loc is not None:
            return loc[2]
    for md in (calendar or []):
        for name in ("day", "pre", "regular", "after"):
            w = getattr(md, name)
            if w is not None and w.start_ms <= cutoff < w.end_ms:
                return w.start_ms
    return None


# --------------------------------------------------------------------------- #
# C-7: extract_precursor_features
# --------------------------------------------------------------------------- #
def extract_precursor_features(df_1m: pd.DataFrame, rankings: pd.DataFrame,
                               t0_ms: int,
                               windows_min: tuple[int, ...] = DEFAULT_WINDOWS_MIN, *,
                               include_t0: bool = False,
                               symbol: str | None = None,
                               curve: pd.Series | None = None,
                               calendar: list[UsMarketDay] | None = None,
                               baseline: dict | None = None,
                               shares_outstanding_qu: int | None = None,
                               prior_events: pd.DataFrame | None = None,
                               toss_type: str = TOSS_RANK_TYPE,
                               market_type: str = MARKET_RANK_TYPE) -> dict[str, float]:
    """T0 이전 구간만으로 전조 피처를 만든다. 반환 키는 `feature_names()` 와 동일.

    위치인자는 계약 C-7 그대로. 키워드 전용 인자는 A1 §2 확장:
        include_t0             A1 §1. 기본 False(엄격). W4 실시간 검출기는 True
        symbol                 랭킹 필터. None 이면 df_1m 의 symbol 컬럼에서 추론
        curve                  `minute_of_session_volume_curve()` — 시간대 보정 RVOL 분모
        calendar               세션 판정용 (곡선은 이력일, 적용은 당일인 정상 운용)
        baseline               `compute_daily_baseline()` — 일봉 z·ATR% 참조
        shares_outstanding_qu  진행분 플로트 로테이션 분모
        prior_events           이력 이벤트(t0_ms 컬럼). 없으면 df_1m 이전 날들로 프록시 계산
        toss_type/market_type  토스 쏠림도에 쓸 랭킹 type 2종
    """
    pre = cut_frame(df_1m, t0_ms, include_t0=include_t0).sort_values("ts_ms")
    rk = cut_frame(rankings, t0_ms, "snap_ms", include_t0=include_t0)

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
    _price_features(feats, pre, cutoff, close_cut, windows_min, baseline, sess_start)
    _print_activity_features(feats, pre, cutoff, windows_min, sess_start)
    _toss_concentration_features(feats, rk, symbol, cutoff, toss_type, market_type)
    _history_features(feats, pre, cutoff, prior_events, shares_outstanding_qu, sess_start)
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
             "hist_days_available", "float_rotation_pre"]
    for w in windows_min:
        names += [f"vol_z_{w}", f"vol_ratio_{w}", f"rvol_curve_{w}", f"vol_sum_{w}_qu",
                  f"ret_{w}", f"atr_pct_{w}", f"range_pct_{w}", f"no_print_ratio_{w}"]
    names += [f"rvol_first_cross_{thr:g}_lead_min" for thr in RVOL_CROSS_THRESHOLDS]
    return names


# --------------------------------------------------------------------------- #
# 거래량 피처
# --------------------------------------------------------------------------- #
def _expected_window_vol(curve, calendar, t_from: int, t_to: int) -> float:
    """곡선 기준 [t_from, t_to) 기대 거래량 합. 곡선에 없는 분은 건너뛴다."""
    if curve is None or curve.empty:
        return _NAN
    total, seen = 0.0, 0
    for ts in range(t_from, t_to, MIN_MS):
        loc = curve_locate(curve, ts, calendar=calendar)
        if loc is None:
            continue
        key = (loc[0], loc[1])
        if key in curve.index:
            total += float(curve.loc[key])
            seen += 1
    return total if seen else _NAN


def _volume_features(feats: dict, pre: pd.DataFrame, cutoff: int,
                     windows_min: tuple[int, ...], curve, calendar, baseline,
                     sess_start: int | None) -> None:
    t_hi = cutoff + MIN_MS                     # cutoff 봉 포함
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
            rv = rv[rv.index >= sess_start]
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
                    sess_start: int | None) -> None:
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
    sess = pre[pre["ts_ms"] >= lo]
    if sess.empty:
        return
    vw = session_vwap_u(sess, SessionWindow(start_ms=lo, end_ms=cutoff + MIN_MS))
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

    if baseline and baseline.get("close_last_u"):
        prev_close = int(baseline["close_last_u"])
        if prev_close > 0:
            feats["gap_from_prev_close"] = close_cut / prev_close - 1.0


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
    t_hi = cutoff + MIN_MS
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
    in_sess = [t for t in ts if t >= sess_start]
    if in_sess:
        feats["session_print_age_min"] = float((cutoff - in_sess[0]) // MIN_MS)
        # first_print 프록시: 세션 시작 후 첫 체결까지 걸린 시간 (작을수록 활발)
        feats["session_first_print_lead_min"] = float((in_sess[0] - sess_start) // MIN_MS)
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
                      sess_start: int | None) -> None:
    day_ms = 86_400_000
    ts = [int(t) for t in pre["ts_ms"].tolist()]
    if ts:
        feats["hist_days_available"] = float(len({t // day_ms for t in ts}))

    if prior_events is not None and len(prior_events):
        pe = prior_events[prior_events["t0_ms"] < cutoff]
        feats["prior_event_count_20d"] = float(
            len(pe[pe["t0_ms"] >= cutoff - PRIOR_EVENT_LOOKBACK_DAYS * day_ms]))
        if not pe.empty:
            feats["days_since_prior_event"] = (cutoff - int(pe["t0_ms"].max())) / day_ms
    elif ts:
        # 프록시: 이전 UTC 날짜들 중 일중 저가→고가 상승률이 임계 이상이었던 날 수
        cur_day = cutoff // day_ms
        lo_day = (cutoff - PRIOR_EVENT_LOOKBACK_DAYS * day_ms) // day_ms
        cnt, last_ms = 0, None
        for d in sorted({t // day_ms for t in ts if lo_day <= t // day_ms < cur_day}):
            sub = pre[(pre["ts_ms"] >= d * day_ms) & (pre["ts_ms"] < (d + 1) * day_ms)]
            if sub.empty:
                continue
            lo_u, hi_u = int(sub["low_u"].min()), int(sub["high_u"].max())
            if lo_u > 0 and hi_u / lo_u - 1.0 >= PRIOR_EVENT_RANGE_MIN:
                cnt += 1
                last_ms = int(sub["ts_ms"].to_numpy()[-1])
        feats["prior_event_count_20d"] = float(cnt)
        if last_ms is not None:
            feats["days_since_prior_event"] = (cutoff - last_ms) / day_ms

    c = feats.get("prior_event_count_20d", _NAN)
    if c == c:
        feats["former_runner"] = 1.0 if c > 0 else 0.0

    if shares_outstanding_qu:
        lo = sess_start if sess_start is not None \
            else cutoff - FALLBACK_SESSION_MIN * MIN_MS
        vol = sum(int(v) for t, v in zip(pre["ts_ms"].tolist(), pre["vol_qu"].tolist())
                  if int(t) >= lo)
        feats["float_rotation_pre"] = vol / int(shares_outstanding_qu)

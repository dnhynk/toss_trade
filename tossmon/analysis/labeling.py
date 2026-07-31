"""이벤트 라벨링 — 계약 C-7 + **C-7 개정 A1**. 소유: W3. 라벨 필드 정의: docs/03 §3.

이벤트 정의 (docs/03 §3, 파라미터화)
    T0 = 아래 가격 조건 최초 충족 1분봉 AND 세션 누적 RVOL ≥ rvol_min
      (a) `window_min` 분 내 `+ret_min` : close_t / min(close in [t-window, t]) - 1 ≥ ret_min
      (b) 당일 `+day_ret_min`           : close_t / prev_close - 1 ≥ day_ret_min
    kind = 'win' | 'day' | 'both' (어느 조건이 충족됐는지 — A1 §3)

매매일 경계
    calendar 가 주어지면 `UsMarketDay` 의 첫 세션 시작 ~ 마지막 세션 종료.
    없으면 **UTC 날짜**로 묶는데, 이 폴백은 **여름(EDT)에만 맞다** — 겨울(EST)에는
    애프터장이 UTC 자정을 넘어 하나의 매매일이 두 UTC 날짜로 쪼개진다
    (감사 M-3, docs/07 §3.1a). **캘린더를 넘기는 것이 원칙이다.**

룩어헤드 주의
    이 모듈은 **사후 라벨링용**이므로 라벨은 T0 이후 데이터를 당연히 쓴다.
    전조 피처(T0 이전만)는 `features.py` 를 쓸 것.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ..api.models import SessionWindow, UsMarketDay
from .baselines import MIN_MS, session_vwap_u, session_windows

#: `shape` 분류 임계 (docs/07 §4.2)
SHAPE_INSTANT_FRACTION = 0.5    # T0 봉 자체 수익률이 ret_min 의 이 비율 이상이면 즉발형
SHAPE_COIL_LOOKBACK_MIN = 60    # coil 판정 관찰 구간
SHAPE_COIL_SKIP_MIN = 5         # T0 직전 몇 분을 관찰에서 제외할지
SHAPE_COIL_RANGE_MAX = 0.05     # 관찰 구간 (high-low)/close 상한
SHAPE_RAMP_DRIFT_MIN = 0.05     # 이 이상 사전 상승이면 ramp

#: `outcome` 분류 임계 (docs/07 §4.3)
OUTCOME_DUMP_DRAWDOWN = 0.20    # 피크 대비 -20%
OUTCOME_DUMP_WINDOW_MIN = 30    # 위 낙폭이 이 시간 내 발생하면 dump
OUTCOME_FADE_GIVEBACK = 0.5     # T0→피크 상승분의 이 비율 이상 반납하면 fade

#: LULD 홀트 프록시 — 정규장 내 이 길이 이상의 캔들 공백
HALT_GAP_MIN_DEFAULT = 5

EVENT_COLUMNS = [
    # 계약 C-7 명시 7컬럼 (순서 고정)
    "t0_ms", "kind", "peak_ms", "peak_ret", "ret_30m", "ret_close", "session",
    # A1 §4 추가 컬럼
    "symbol", "hod_ms", "hod_ret", "retrace_30m", "retrace_close", "duration_min",
    "time_to_peak_min", "vwap_close_rel", "closed_below_vwap", "float_rotation",
    "ranking_first_entry_ms", "ranking_lead_lag_min", "next_day_gap", "t0_min_from_open",
    "rvol_at_t0", "halt_gap_count", "shape", "outcome",
    # A1 §6 의무 컬럼
    "rvol_gated",
]


@dataclass(frozen=True)
class EventParams:
    window_min: int = 30
    ret_min: float = 0.15
    day_ret_min: float = 0.30
    rvol_min: float = 3.0


# --------------------------------------------------------------------------- #
# 내부 유틸
# --------------------------------------------------------------------------- #
def _day_spans(df_1m: pd.DataFrame,
               calendar: list[UsMarketDay] | None
               ) -> list[tuple[UsMarketDay | None, int, int]]:
    """(UsMarketDay|None, 매매일 시작 ms, 매매일 종료 ms) 시간순 목록."""
    if calendar:
        out = []
        for md in calendar:
            wins = session_windows(md)
            if wins:
                out.append((md, wins[0][1].start_ms, wins[-1][1].end_ms))
        return sorted(out, key=lambda x: x[1])
    day_ms = 86_400_000
    days = sorted({int(t) // day_ms for t in df_1m["ts_ms"].tolist()})
    return [(None, d * day_ms, (d + 1) * day_ms) for d in days]


def _rolling_min_close(sub: pd.DataFrame, window_min: int) -> list[int]:
    """시간 기준 트레일링 윈도우 `[t-window, t]` 의 최소 종가.

    봉 공백(홀트/미체결)이 있어도 **봉 개수가 아니라 시간**으로 자른다.
    pandas 시간 rolling(`closed="both"`)을 쓴다 — tests/synth 의 좌측 포인터 나이브
    구현과 방식이 달라 교차 검증이 된다.
    """
    idx = pd.to_datetime(sub["ts_ms"].to_numpy(), unit="ms", utc=True)
    s = pd.Series(sub["close_u"].to_numpy(), index=idx)
    return [int(x) for x in s.rolling(f"{window_min}min", closed="both").min().tolist()]


def _last_close_before(df: pd.DataFrame, ts_ms: int) -> int | None:
    sub = df[df["ts_ms"] < ts_ms]
    return int(sub["close_u"].to_numpy()[-1]) if not sub.empty else None


def _prev_day_close_u(sdf: pd.DataFrame,
                      prev_span: tuple[UsMarketDay | None, int, int],
                      prev_bounds: tuple[int, int]) -> int | None:
    """직전 매매일의 전일 종가 (사전등록 §2.3).

    정규장 마지막 1분봉 종가를 우선하고, 정규장 봉이 하나도 없으면 그 매매일의 마지막
    1분봉 종가를 쓴다. 봉이 아예 없으면 None — 호출자는 **당일 조건 판정을 건너뛴다**
    (당일 첫 시가로 대체하지 않는다).
    """
    pmd, _pfrom, _pto = prev_span
    plo, phi = prev_bounds
    if plo >= phi:
        return None
    prev_day = sdf.iloc[plo:phi]
    if pmd is not None and pmd.regular is not None:
        reg = prev_day[(prev_day["ts_ms"] >= pmd.regular.start_ms)
                       & (prev_day["ts_ms"] < pmd.regular.end_ms)]
        if not reg.empty:
            return int(reg["close_u"].to_numpy()[-1])
    return int(prev_day["close_u"].to_numpy()[-1])


def _regular_frame(df: pd.DataFrame, md: UsMarketDay | None) -> pd.DataFrame:
    """정규장 봉만. calendar 가 없으면 입력 그대로."""
    if md is None or md.regular is None:
        return df
    return df[(df["ts_ms"] >= md.regular.start_ms) & (df["ts_ms"] < md.regular.end_ms)]


def _count_halt_gaps(df: pd.DataFrame, md: UsMarketDay | None, min_gap_min: int) -> int:
    """정규장 내 `min_gap_min` 분 이상 캔들 공백 개수 (LULD 홀트 프록시)."""
    reg = _regular_frame(df, md)
    ts = [int(t) for t in reg["ts_ms"].tolist()]
    return sum(1 for a, b in zip(ts[:-1], ts[1:]) if (b - a) // MIN_MS - 1 >= min_gap_min)


def _classify_shape(day: pd.DataFrame, t0_ms: int, t0_open_u: int, t0_close_u: int,
                    ret_min: float) -> str:
    """instant(즉발) / ramp(사전 상승) / coil(저변동 수축 후 폭발) / mixed."""
    if t0_open_u > 0 and (t0_close_u / t0_open_u - 1.0) >= SHAPE_INSTANT_FRACTION * ret_min:
        return "instant"
    pre = day[(day["ts_ms"] >= t0_ms - SHAPE_COIL_LOOKBACK_MIN * MIN_MS)
              & (day["ts_ms"] < t0_ms - SHAPE_COIL_SKIP_MIN * MIN_MS)]
    if len(pre) < 10:
        return "mixed"
    first_c = int(pre["close_u"].to_numpy()[0])
    last_c = int(pre["close_u"].to_numpy()[-1])
    rng = (int(pre["high_u"].max()) - int(pre["low_u"].min())) / last_c if last_c else 9e9
    drift = (last_c / first_c - 1.0) if first_c else 0.0
    if drift >= SHAPE_RAMP_DRIFT_MIN:
        return "ramp"
    return "coil" if rng <= SHAPE_COIL_RANGE_MAX else "mixed"


def _classify_outcome(day: pd.DataFrame, peak_ms: int, peak_u: int,
                      peak_ret: float, ret_close: float) -> str:
    """dump(피크 후 급락) / fade(상승분 대부분 반납) / hold."""
    win = day[(day["ts_ms"] >= peak_ms)
              & (day["ts_ms"] <= peak_ms + OUTCOME_DUMP_WINDOW_MIN * MIN_MS)]
    if not win.empty and peak_u > 0:
        if int(win["low_u"].min()) / peak_u - 1.0 <= -OUTCOME_DUMP_DRAWDOWN:
            return "dump"
    if peak_ret > 0 and ret_close < OUTCOME_FADE_GIVEBACK * peak_ret:
        return "fade"
    return "hold"


def _ranking_first_entry(rankings: pd.DataFrame | None, symbol: str | None,
                         t_from_ms: int, t_to_ms: int,
                         ranking_type: str | None) -> int | None:
    """매매일 구간 내 해당 심볼의 랭킹 최초 진입 시각."""
    if rankings is None or rankings.empty or symbol is None:
        return None
    r = rankings
    if "symbol" in r.columns:
        r = r[r["symbol"] == symbol]
    if ranking_type is not None and "ranking_type" in r.columns:
        r = r[r["ranking_type"] == ranking_type]
    r = r[(r["snap_ms"] >= t_from_ms) & (r["snap_ms"] < t_to_ms)]
    return int(r["snap_ms"].min()) if not r.empty else None


# --------------------------------------------------------------------------- #
# C-7: detect_events
# --------------------------------------------------------------------------- #
def detect_events(df_1m: pd.DataFrame, params: EventParams, *,
                  calendar: list[UsMarketDay] | None = None,
                  rvol_series: pd.Series | None = None,
                  prev_close_u: int | dict[str, int] | None = None,
                  shares_outstanding_qu: int | dict[str, int] | None = None,
                  rankings: pd.DataFrame | None = None,
                  ranking_type: str | None = None,
                  max_per_day: int = 1,
                  halt_gap_min: int = HALT_GAP_MIN_DEFAULT,
                  split_dates: set[str] | dict[str, set[str]] | None = None
                  ) -> pd.DataFrame:
    """급등 이벤트 검출 + 라벨 전부. 반환 컬럼은 `EVENT_COLUMNS`.

    위치인자 시그니처는 계약 C-7 그대로. 키워드 전용 인자는 A1 §2 확장:
        calendar               세션·매매일 판정. 없으면 session='unknown' + UTC 날짜 그룹화
        rvol_series            `baselines.rvol_series()` 결과(index=ts_ms). 없으면 A1 §6 대로
                               가격 조건만 적용하고 `rvol_gated=False` 로 명시
        prev_close_u           전일 종가(심볼별 dict 허용). 없으면 df 내 이전 봉의 마지막 종가,
                               그것도 없으면 당일 첫 봉 시가 (docs/07 §3.3 편향)
        shares_outstanding_qu  플로트 로테이션 분모(심볼별 dict 허용)
        rankings               랭킹 스냅샷 (snap_ms, symbol[, ranking_type])
        ranking_type           랭킹 필터. None 이면 전달된 전체에서 최초 진입
        max_per_day            매매일당 최대 이벤트 수 (기본 1 = 최초 트리거만)
        halt_gap_min           홀트 프록시로 볼 캔들 공백 길이(분)
        split_dates            분할 매매일 date 집합(심볼별 dict 허용) — 사전등록 §7-e.
                               해당 매매일은 **당일 조건(+30%) 판정에서 제외**한다
                               (윈도우 조건만, `kind='win'`). 원주가 계열에서 분할 전일
                               종가 대비 가짜 ±N00% 갭이 그대로 판정되는 것을 막는다.
                               `baselines.detect_split_dates()` 로 만든다.
                               **미지정 시 결과는 주 분석에 쓸 수 없다** —
                               `events.attrs["split_dates_applied"]=False` 로 표시된다

    `df_1m` 에 `symbol` 컬럼이 있으면 심볼별로 독립 처리한다.
    """
    if split_dates is not None and calendar is None:
        # 3차 감사 F-3: calendar 없이는 매매일 date 를 알 수 없어 split_dates 가 조용히
        # 무위가 되고, provenance 만 "적용됨"으로 남아 거짓 안심을 준다.
        raise ValueError("split_dates 를 쓰려면 calendar 가 필요하다 "
                         "(매매일 date 매핑 없이는 무위 — 3차 감사 F-3)")
    if df_1m is None or df_1m.empty:
        return pd.DataFrame(columns=EVENT_COLUMNS)

    df = df_1m.sort_values("ts_ms")
    groups = ([(str(s), g) for s, g in df.groupby("symbol", sort=True)]
              if "symbol" in df.columns else [(None, df)])
    spans = _day_spans(df, calendar)
    # RVOL 맵은 심볼·날짜 루프 밖에서 딱 한 번 (1024일 백필에서 재구성하면 O(n·days))
    rv_map = ({int(k): float(v) for k, v in rvol_series.items()}
              if (rvol_series is not None and not rvol_series.empty) else None)

    n_split_excluded = 0
    per_day_prev = (isinstance(prev_close_u, dict)
                    and any(isinstance(k, tuple) for k in prev_close_u))
    rows: list[dict] = []
    for symbol, sdf in groups:
        prev_close = (None if per_day_prev
                      else (prev_close_u.get(symbol) if isinstance(prev_close_u, dict)
                            else prev_close_u))
        sym_splits = (split_dates.get(symbol, set()) if isinstance(split_dates, dict)
                      else (split_dates or set()))
        shares = (shares_outstanding_qu.get(symbol)
                  if isinstance(shares_outstanding_qu, dict) else shares_outstanding_qu)
        # 매매일 슬라이싱을 O(log n) 으로 (봉×날짜 이중 스캔 방지)
        sts = sdf["ts_ms"].to_numpy()
        bounds = [(int(sts.searchsorted(a, side="left")),
                   int(sts.searchsorted(b, side="left"))) for _md, a, b in spans]
        # 3차 감사 F-4: 스칼라(또는 심볼별) 전일 종가는 **하루치 프레임에만** 유효하다.
        # 다일 프레임에 주면 그 값이 모든 매매일의 기준가로 쓰여 당일 조건이 전부 오염된다.
        if prev_close is not None and sum(1 for lo, hi in bounds if lo < hi) > 1:
            raise ValueError(
                "다일 프레임에는 스칼라/심볼별 prev_close_u 를 쓸 수 없다 — "
                "{(symbol, date): value} 로 매매일별로 주거나 생략해 대체 사슬에 맡겨라 "
                "(3차 감사 F-4)")
        for i, (md, t_from, t_to) in enumerate(spans):
            lo, hi = bounds[i]
            if lo >= hi:
                continue
            day = sdf.iloc[lo:hi]
            # 사전등록 §2.3 전일 종가 대체 사슬 — **당일 첫 시가 대체는 금지**다.
            #   ① 인자로 받은 prev_close_u
            #   ② 직전 매매일의 **정규장** 마지막 1분봉 종가 (원주가)
            #   ③ 직전 매매일의 마지막 1분봉 종가 (정규장 봉이 없을 때)
            #   ④ 그래도 없으면 base_prev=None → **당일 조건(+30%) 판정 자체를 건너뛴다**
            base_prev = prev_close
            if per_day_prev and md is not None:
                base_prev = prev_close_u.get((symbol, md.date))
            if base_prev is None and i > 0:
                base_prev = _prev_day_close_u(sdf, spans[i - 1], bounds[i - 1])
            # 사전등록 §7-e: 분할 매매일은 당일 조건 판정에서 제외한다.
            if md is not None and md.date in sym_splits:
                base_prev = None
                n_split_excluded += 1
            nxt_day = None
            if i + 1 < len(spans):
                nlo, nhi = bounds[i + 1]
                if nlo < nhi:
                    nxt_day = (spans[i + 1][0], sdf.iloc[nlo:nhi])
            rows.extend(_label_day(
                symbol=symbol, day=day, md=md, t_from=t_from, t_to=t_to,
                next_day=nxt_day, params=params,
                prev_close_u=None if base_prev is None else int(base_prev),
                rv_map=rv_map, shares_outstanding_qu=shares, rankings=rankings,
                ranking_type=ranking_type, max_per_day=max_per_day,
                halt_gap_min=halt_gap_min))

    out = pd.DataFrame(rows, columns=EVENT_COLUMNS)
    if not out.empty:
        out = out.sort_values(["t0_ms", "symbol"]).reset_index(drop=True)
    # 주 분석 강제용 표식 (사전등록 §7-e) — 사유별 카운트가 이 값을 읽는다.
    out.attrs["split_dates_applied"] = split_dates is not None
    out.attrs["split_excluded"] = n_split_excluded
    return out


def _label_day(*, symbol, day, md, t_from, t_to, next_day, params, prev_close_u,
               rv_map, shares_outstanding_qu, rankings, ranking_type,
               max_per_day, halt_gap_min) -> list[dict]:
    ts = [int(t) for t in day["ts_ms"].tolist()]
    close = [int(c) for c in day["close_u"].tolist()]
    open_ = [int(o) for o in day["open_u"].tolist()]
    win_min_close = _rolling_min_close(day, params.window_min)

    gated = rv_map is not None
    rv = rv_map or {}

    out: list[dict] = []
    for i, t in enumerate(ts):
        if len(out) >= max_per_day:
            break
        base = win_min_close[i]
        win_ret = (close[i] / base - 1.0) if base > 0 else 0.0
        win_ok = win_ret >= params.ret_min
        # 전일 종가가 없으면 당일 조건은 **판정하지 않는다** (사전등록 §2.3).
        # 당일 첫 시가로 대체하면 갭업이 구조적으로 과소평가된다.
        day_ok = False
        if prev_close_u is not None and prev_close_u > 0:
            day_ok = (close[i] / prev_close_u - 1.0) >= params.day_ret_min
        if not (win_ok or day_ok):
            continue
        rvol_at = rv.get(t, float("nan")) if gated else float("nan")
        if gated and not (rvol_at == rvol_at and rvol_at >= params.rvol_min):
            continue
        out.append(_build_label(
            symbol=symbol, day=day, md=md, t_from=t_from, t_to=t_to,
            next_day=next_day, params=params, i=i, ts=ts, close=close, open_=open_,
            prev_close_u=prev_close_u,
            kind=("both" if (win_ok and day_ok) else ("win" if win_ok else "day")),
            rvol_at=rvol_at, rvol_gated=bool(gated),
            shares_outstanding_qu=shares_outstanding_qu, rankings=rankings,
            ranking_type=ranking_type, halt_gap_min=halt_gap_min))
    return out


def _build_label(*, symbol, day, md, t_from, t_to, next_day, params, i, ts, close,
                 open_, prev_close_u, kind, rvol_at, rvol_gated, shares_outstanding_qu,
                 rankings, ranking_type, halt_gap_min) -> dict:
    t0_ms = ts[i]
    c0 = close[i]

    # 피크 = T0 이후 당일 고가 / HOD = 매매일 전체 고가
    post = day[day["ts_ms"] >= t0_ms]
    ph = post["high_u"].to_numpy()
    pk = int(ph.argmax())
    peak_ms = int(post["ts_ms"].to_numpy()[pk])
    peak_u = int(ph[pk])
    dh = day["high_u"].to_numpy()
    hk = int(dh.argmax())
    hod_ms = int(day["ts_ms"].to_numpy()[hk])
    hod_u = int(dh[hk])

    # 종가 기준: 정규장 마지막 봉 (calendar 없으면 매매일 마지막 봉)
    reg = _regular_frame(day, md)
    ref = reg if not reg.empty else day
    close_ref_u = int(ref["close_u"].to_numpy()[-1])

    w30 = day[(day["ts_ms"] > t0_ms) & (day["ts_ms"] <= t0_ms + 30 * MIN_MS)]
    ret_30m = (int(w30["close_u"].to_numpy()[-1]) / c0 - 1.0) if not w30.empty \
        else float("nan")
    ret_close = close_ref_u / c0 - 1.0
    peak_ret = peak_u / c0 - 1.0
    hod_ret = ((hod_u / prev_close_u - 1.0)
               if (prev_close_u is not None and prev_close_u > 0) else float("nan"))

    ap = day[(day["ts_ms"] > peak_ms) & (day["ts_ms"] <= peak_ms + 30 * MIN_MS)]
    retrace_30m = (int(ap["close_u"].to_numpy()[-1]) / peak_u - 1.0) if not ap.empty \
        else float("nan")
    retrace_close = close_ref_u / peak_u - 1.0

    # 지속시간: T0 종가를 다시 하회하기까지. 끝까지 유지되면 NaN (우측 절단)
    gb = day[(day["ts_ms"] > t0_ms) & (day["close_u"] <= c0)]
    duration_min = (float((int(gb["ts_ms"].to_numpy()[0]) - t0_ms) // MIN_MS)
                    if not gb.empty else float("nan"))

    session = "unknown"
    t0_min_from_open = float("nan")
    if md is not None:
        for name, win in session_windows(md):
            if win.start_ms <= t0_ms < win.end_ms:
                session = name
                break
        if md.regular is not None:
            t0_min_from_open = float((t0_ms - md.regular.start_ms) // MIN_MS)

    # VWAP 대비 종가: 정규장 VWAP. calendar 가 없거나 **정규장 체결이 아예 없는 종목**
    # (데이마켓만 거래되는 초저유동성)이면 매매일 전체 VWAP 로 대체한다 — close_ref_u 의
    # 대체 규칙과 일치시켜야 두 값을 나눈 비율이 의미를 갖는다.
    vw = pd.Series(dtype="int64")
    if md is not None and md.regular is not None:
        vw = session_vwap_u(day, md.regular)
    if vw.empty:
        vw = session_vwap_u(day, SessionWindow(start_ms=t_from, end_ms=t_to))
    vwap_close_rel = float("nan")
    closed_below_vwap = None
    if not vw.empty:
        vwap_last = int(vw.iloc[-1])
        if vwap_last > 0:
            vwap_close_rel = close_ref_u / vwap_last - 1.0
            closed_below_vwap = bool(close_ref_u < vwap_last)

    float_rotation = float("nan")
    if shares_outstanding_qu:
        float_rotation = (sum(int(v) for v in day["vol_qu"].tolist())
                          / int(shares_outstanding_qu))

    entry_ms = _ranking_first_entry(rankings, symbol, t_from, t_to, ranking_type)
    lead_lag = float((entry_ms - t0_ms) // MIN_MS) if entry_ms is not None else float("nan")

    next_day_gap = float("nan")
    if next_day is not None:
        nmd, nxt = next_day
        if nmd is not None and nmd.regular is not None:
            nreg = nxt[(nxt["ts_ms"] >= nmd.regular.start_ms)
                       & (nxt["ts_ms"] < nmd.regular.end_ms)]
            nxt = nreg if not nreg.empty else nxt
        if not nxt.empty and close_ref_u > 0:
            next_day_gap = int(nxt["open_u"].to_numpy()[0]) / close_ref_u - 1.0

    return {
        "t0_ms": t0_ms, "kind": kind, "peak_ms": peak_ms, "peak_ret": peak_ret,
        "ret_30m": ret_30m, "ret_close": ret_close, "session": session,
        "symbol": symbol, "hod_ms": hod_ms, "hod_ret": hod_ret,
        "retrace_30m": retrace_30m, "retrace_close": retrace_close,
        "duration_min": duration_min,
        "time_to_peak_min": float((peak_ms - t0_ms) // MIN_MS),
        "vwap_close_rel": vwap_close_rel, "closed_below_vwap": closed_below_vwap,
        "float_rotation": float_rotation, "ranking_first_entry_ms": entry_ms,
        "ranking_lead_lag_min": lead_lag, "next_day_gap": next_day_gap,
        "t0_min_from_open": t0_min_from_open, "rvol_at_t0": rvol_at,
        "halt_gap_count": _count_halt_gaps(day, md, halt_gap_min),
        "shape": _classify_shape(day, t0_ms, open_[i], c0, params.ret_min),
        "outcome": _classify_outcome(day, peak_ms, peak_u, peak_ret, ret_close),
        "rvol_gated": bool(rvol_gated),
    }

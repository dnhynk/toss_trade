"""평가 — 계약 C-7. 소유: W3. docs/03 §3 검증 질문 6개와 1:1 대응.

    q1  거래량 이상은 가격 급등보다 몇 분 선행하는가? 임계값별 정밀도/재현율
    q2  토스 랭킹 진입은 가격 대비 선행인가 후행인가? (Barber 2022)
    q3  데이마켓 급등이 정규장에서 지속되는가 소멸하는가?
    q4  피크→-20% 도달 시간 분포, 홀트 개입 빈도
    q5  전조 스코어 조건부 기대수익 (왕복 비용 차감 후 양수인가)
    q6  시간대 효과 (개장 15분 / 10:00 ET 전후 / 마감 전)

RVOL 게이트 규약 (계약 A1 §6 의무)
    `rvol_gated=False` 이벤트(= RVOL 곡선 없이 가격 조건만으로 잡힌 것)는 **기본적으로
    집계에서 제외**한다. 정밀도/재현율 통계를 조용히 오염시키는 것을 막기 위함이며, 모든
    반환 DataFrame 은 `gate_policy` / `n_ungated_excluded` 컬럼으로 그 사실을 드러낸다.
    `gate="separate"` 를 주면 제외하지 않고 전부 집계하되 제외 대상 수를 함께 보고한다.

비용 규약 (계약 A2 §5)
    `cost_roundtrip=0.01`(1%) 은 왕복 수수료 0.2% + 환전 스프레드 + 저유동성 슬리피지를
    포함한 **보수적 총비용**이다. 수수료만의 값(0.002)과 혼동하지 않도록 q5 는 내역을
    분해한 컬럼(`cost_commission`/`cost_fx`/`cost_slippage`)을 함께 반환한다.
"""
from __future__ import annotations

import pandas as pd

from .baselines import MIN_MS
from .features import RVOL_CROSS_THRESHOLDS

#: q6 시간대 버킷 — (라벨, 개장 후 경과분 하한, 상한). docs/02 §2.4·§4.1 근거.
TOD_BUCKETS = [
    ("pre_or_day", -100_000, 0),     # 개장 전 (데이마켓·프리마켓)
    ("open_0_15", 0, 15),            # 밴드 2배 구간, HOD 46.6% 형성
    ("open_15_30", 15, 30),          # ~10:00 ET (HOD 63% 이 시점까지 형성)
    ("mid_30_180", 30, 180),
    ("late_180_330", 180, 330),
    ("close_330_390", 330, 390),
    ("after_hours", 390, 100_000),
]

#: q5 기본 청산 정책 — 라벨만으로 계산 가능한 것들
EXIT_POLICIES = ("t0_to_30m", "t0_to_close", "half_peak")

#: docs/02 §2.4 SmallCapLab 실측 기저율 (우리 데이터로 재검증할 대상)
KNOWN_BASE_RATES = {
    "fade_rate": 0.715,
    "break_20pct_from_hod": 0.50,
    "close_below_vwap": 0.73,
    "hod_before_1000et": 0.63,
    "hod_within_15min": 0.466,
}

_NAN = float("nan")


# --------------------------------------------------------------------------- #
# 공통
# --------------------------------------------------------------------------- #
def _gated_mask(events: pd.DataFrame) -> pd.Series:
    return events["rvol_gated"].fillna(False).astype(bool)


def _gate(events: pd.DataFrame, gate: str) -> tuple[pd.DataFrame, int]:
    """A1 §6: 게이트 미적용 이벤트 처리. 반환 (사용할 events, 제외 대상 수)."""
    if events is None or len(events) == 0 or "rvol_gated" not in events.columns:
        return (events if events is not None else pd.DataFrame()), 0
    ok = _gated_mask(events)
    n_ungated = int((~ok).sum())
    return (events[ok] if gate == "exclude" else events), n_ungated


def _stamp(df: pd.DataFrame, gate: str, n_excluded: int) -> pd.DataFrame:
    """모든 반환 DataFrame 에 게이트 정책을 명시 (A1 §6)."""
    out = df.copy()
    out["gate_policy"] = gate
    out["n_ungated_excluded"] = n_excluded
    return out


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    if df is None or col not in df.columns:
        return pd.Series(dtype="float64")
    return pd.to_numeric(df[col], errors="coerce")


def _dist(series: pd.Series, prefix: str = "") -> dict[str, float]:
    s = pd.to_numeric(series, errors="coerce").dropna()
    keys = ["n", "mean", "median", "p10", "p25", "p75", "p90"]
    if s.empty:
        out = {f"{prefix}{k}": _NAN for k in keys}
        out[f"{prefix}n"] = 0.0
        return out
    return {f"{prefix}n": float(len(s)), f"{prefix}mean": float(s.mean()),
            f"{prefix}median": float(s.median()), f"{prefix}p10": float(s.quantile(0.10)),
            f"{prefix}p25": float(s.quantile(0.25)),
            f"{prefix}p75": float(s.quantile(0.75)),
            f"{prefix}p90": float(s.quantile(0.90))}


def _share(series: pd.Series, predicate) -> float:
    s = pd.Series(series).dropna()
    return float(predicate(s).mean()) if len(s) else _NAN


def _bool_share(df: pd.DataFrame, col: str) -> float:
    if col not in df.columns:
        return _NAN
    s = df[col].dropna()
    return float(s.astype(bool).mean()) if len(s) else _NAN


def join_events_features(events: pd.DataFrame, feats: pd.DataFrame) -> pd.DataFrame:
    """(symbol, t0_ms) 로 이벤트와 피처를 결합. 공통 키가 없으면 t0_ms 만 사용."""
    if events is None or len(events) == 0:
        return pd.DataFrame()
    if feats is None or len(feats) == 0:
        return events.copy()
    keys = ["symbol", "t0_ms"] if ("symbol" in events.columns
                                   and "symbol" in feats.columns) else ["t0_ms"]
    dup = [c for c in feats.columns if c in events.columns and c not in keys]
    return events.merge(feats.drop(columns=dup), on=keys, how="left")


# --------------------------------------------------------------------------- #
# q1 — 거래량 이상의 리드타임 + 임계값별 정밀도/재현율
# --------------------------------------------------------------------------- #
def q1_volume_leadtime(events: pd.DataFrame, feats: pd.DataFrame, *,
                       gate: str = "exclude") -> pd.DataFrame:
    """거래량 이상의 가격 대비 리드타임 분포와 임계값별 정밀도/재현율.

    리드타임 지표
        vol_surge_lead_min               봉 단위 로그거래량 z≥3 최초 시각의 T0 대비 리드(분)
        rvol_first_cross_{thr}_lead_min  세션 누적 RVOL 이 thr 을 처음 넘은 시각의 리드(분)

    정밀도/재현율은 **음성 표본이 있어야** 계산된다. `feats` 에 `is_event`(0/1) 컬럼이 있으면
    계산하고, 없으면 NaN + `note='no_controls'` 로 표시한다 (합성데이터 평가에서는 noise
    시나리오가 음성 표본이 된다). 문헌 대비 해석: 리드타임 중앙값이 0 근처거나 음수 검출률이
    낮으면 "전조 탐지"보다 "시작 후 수 분 내 확인-진입"이 현실적이라는 La Morgia 결론과 정합.
    """
    ev, n_ex = _gate(events, gate)
    joined = join_events_features(ev, feats)
    n_total = float(len(joined))
    rows: list[dict] = []

    metrics = ["vol_surge_lead_min"] + [f"rvol_first_cross_{t:g}_lead_min"
                                        for t in RVOL_CROSS_THRESHOLDS]
    for m in metrics:
        d = _dist(_num(joined, m), "lead_")
        rows.append({"metric": m, "n_events": n_total, "detected": d["lead_n"],
                     "detect_rate": (d["lead_n"] / n_total) if n_total else _NAN,
                     "precision": _NAN, "recall": _NAN, "f1": _NAN,
                     "tp": _NAN, "fp": _NAN, "fn": _NAN, **d, "note": ""})

    has_controls = (feats is not None and len(feats)
                    and "is_event" in feats.columns
                    and "rvol_at_cutoff" in feats.columns)
    for thr in RVOL_CROSS_THRESHOLDS:
        row = {"metric": f"precision_recall@rvol_at_cutoff>={thr:g}",
               "n_events": n_total, "detected": _NAN, "detect_rate": _NAN,
               "precision": _NAN, "recall": _NAN, "f1": _NAN,
               "tp": _NAN, "fp": _NAN, "fn": _NAN, "note": "no_controls"}
        if has_controls:
            c = feats.dropna(subset=["rvol_at_cutoff"])
            pred = _num(c, "rvol_at_cutoff") >= thr
            pos = _num(c, "is_event") > 0.5
            tp, fp, fn = int((pred & pos).sum()), int((pred & ~pos).sum()), \
                int((~pred & pos).sum())
            prec = tp / (tp + fp) if (tp + fp) else _NAN
            rec = tp / (tp + fn) if (tp + fn) else _NAN
            f1 = (2 * prec * rec / (prec + rec)
                  if (prec == prec and rec == rec and (prec + rec) > 0) else _NAN)
            row.update({"precision": prec, "recall": rec, "f1": f1, "tp": float(tp),
                        "fp": float(fp), "fn": float(fn), "note": ""})
        rows.append(row)

    return _stamp(pd.DataFrame(rows), gate, n_ex)


# --------------------------------------------------------------------------- #
# q2 — 토스 랭킹 진입의 리드/래그
# --------------------------------------------------------------------------- #
def q2_ranking_lead_lag(events: pd.DataFrame, rankings: pd.DataFrame, *,
                        gate: str = "exclude",
                        window_min: int = 24 * 60) -> pd.DataFrame:
    """랭킹 최초 진입 시각의 T0 대비 리드/래그 분포 (음수 = 선행).

    **랭킹은 과거 조회가 불가능하다** (계약 A2 §4 — 실시간 수집 기간에만 존재). 따라서
    `rankings` 가 비어 있는 것은 오류가 아니라 정상 경로다: 예외를 던지지 않고
    `available=False` 한 줄을 반환한다.

    판정(`verdict`): 래그 비율 > 60% → 'lag' (어텐션 피크 = 청산 카운트다운, Barber 2022),
    리드 비율 > 60% → 'lead', 그 사이는 'mixed'.
    """
    ev, n_ex = _gate(events, gate)
    base_cols = {"ranking_type": "(none)", "available": False,
                 "n_events": float(len(ev)), "n_entered": 0.0, "entry_rate": _NAN,
                 "lead_share": _NAN, "lag_share": _NAN, "verdict": "unavailable"}
    if rankings is None or len(rankings) == 0:
        row = {**base_cols, **_dist(pd.Series(dtype="float64"), "leadlag_"),
               "note": "랭킹 스냅샷 없음 — 과거 조회 불가(A2 §4). 실시간 수집 기간 필요."}
        return _stamp(pd.DataFrame([row]), gate, n_ex)

    types = (sorted(rankings["ranking_type"].dropna().unique().tolist())
             if "ranking_type" in rankings.columns else ["(all)"])
    has_sym = "symbol" in rankings.columns and "symbol" in ev.columns
    rows: list[dict] = []
    for rtype in types:
        sub = (rankings[rankings["ranking_type"] == rtype]
               if "ranking_type" in rankings.columns else rankings)
        by_sym = ({str(s): g for s, g in sub.groupby("symbol")} if has_sym else {})
        leads: list[float] = []
        for _i, e in ev.iterrows():
            t0 = int(e["t0_ms"])
            r = by_sym.get(str(e.get("symbol")), None) if has_sym else sub
            if r is None or r.empty:
                continue
            w = r[(r["snap_ms"] >= t0 - window_min * MIN_MS)
                  & (r["snap_ms"] <= t0 + window_min * MIN_MS)]
            if not w.empty:
                leads.append(float((int(w["snap_ms"].min()) - t0) // MIN_MS))
        s = pd.Series(leads, dtype="float64")
        n_ev = len(ev)
        lead_share = float((s < 0).mean()) if len(s) else _NAN
        lag_share = float((s > 0).mean()) if len(s) else _NAN
        verdict = "mixed"
        if lag_share == lag_share and lag_share > 0.6:
            verdict = "lag"
        elif lead_share == lead_share and lead_share > 0.6:
            verdict = "lead"
        rows.append({"ranking_type": str(rtype), "available": True,
                     "n_events": float(n_ev), "n_entered": float(len(s)),
                     "entry_rate": (len(s) / n_ev) if n_ev else _NAN,
                     "lead_share": lead_share, "lag_share": lag_share,
                     "verdict": verdict, **_dist(s, "leadlag_"), "note": ""})
    return _stamp(pd.DataFrame(rows), gate, n_ex)


# --------------------------------------------------------------------------- #
# q3 — 데이마켓 급등의 정규장 지속성
# --------------------------------------------------------------------------- #
def q3_daymarket_persistence(events: pd.DataFrame, *,
                             gate: str = "exclude") -> pd.DataFrame:
    """세션별 지속/소멸. 데이마켓(한국 낮) 급등이 정규장까지 살아남는지 판별.

    `ret_close` 는 T0 종가 → **같은 매매일 정규장 마지막 종가** 수익률이므로, 데이마켓 T0
    이벤트에서는 그 자체가 "정규장까지의 지속성"을 측정한다 (매매일이 day→pre→regular→after
    순서로 구성되기 때문 — docs/07 §3.1).
    """
    ev, n_ex = _gate(events, gate)
    groups: list[tuple[str, pd.DataFrame]] = [("all", ev)]
    if len(ev) and "session" in ev.columns:
        groups += [(str(s), g) for s, g in ev.groupby("session", dropna=False)]

    rows: list[dict] = []
    for name, g in groups:
        if g is None or len(g) == 0:
            rows.append({"session": name, "n": 0.0,
                         **_dist(pd.Series(dtype="float64"), "retclose_")})
            continue
        rows.append({
            "session": name, "n": float(len(g)),
            "persist_rate": _share(_num(g, "ret_close"), lambda s: s > 0),
            "median_ret_close": float(_num(g, "ret_close").median()),
            "median_peak_ret": float(_num(g, "peak_ret").median()),
            "median_retrace_close": float(_num(g, "retrace_close").median()),
            "median_time_to_peak_min": float(_num(g, "time_to_peak_min").median()),
            "closed_below_vwap_share": _bool_share(g, "closed_below_vwap"),
            "fade_share": _share(g.get("outcome"), lambda s: s == "fade")
            if "outcome" in g.columns else _NAN,
            "dump_share": _share(g.get("outcome"), lambda s: s == "dump")
            if "outcome" in g.columns else _NAN,
            **_dist(_num(g, "ret_close"), "retclose_"),
        })
    return _stamp(pd.DataFrame(rows), gate, n_ex)


# --------------------------------------------------------------------------- #
# q4 — 덤프 속도
# --------------------------------------------------------------------------- #
def q4_dump_speed(events: pd.DataFrame, df_1m: pd.DataFrame, *,
                  gate: str = "exclude",
                  drawdowns: tuple[float, ...] = (0.20, 0.50),
                  horizon_min: int = 390) -> pd.DataFrame:
    """피크에서 각 낙폭까지 걸린 시간 분포 + 홀트(캔들 공백) 개입 빈도.

    낙폭 도달은 `low_u <= peak_u * (1 - dd)` 최초 봉으로 판정한다. 미도달 이벤트는
    우측 절단(censored)이므로 분포에서 제외하고 `reach_rate` 로 따로 보고한다 —
    미도달을 0 이나 최대값으로 대체하면 중앙값이 심하게 왜곡된다 (docs/07 §6.4).

    `horizon_min` (기본 390 = 정규장 1세션): 피크 이후 이 시간까지만 본다. 제한이 없으면
    다음 매매일의 하락까지 "덤프 속도"로 집계돼 중앙값이 수백 분으로 왜곡된다.
    """
    ev, n_ex = _gate(events, gate)
    empty_rows = [{"drawdown": dd, "metric": f"peak_to_-{int(dd * 100)}pct", "n": 0.0,
                   "reached": 0.0, "reach_rate": _NAN,
                   **_dist(pd.Series(dtype="float64"), "minutes_")}
                  for dd in drawdowns]
    if len(ev) == 0 or df_1m is None or len(df_1m) == 0:
        return _stamp(pd.DataFrame(empty_rows), gate, n_ex)

    has_sym = "symbol" in df_1m.columns and "symbol" in ev.columns
    per_symbol = ({str(s): g.sort_values("ts_ms") for s, g in df_1m.groupby("symbol")}
                  if has_sym else {})

    times: dict[float, list[float]] = {dd: [] for dd in drawdowns}
    n_valid = 0
    for _i, e in ev.iterrows():
        if pd.isna(e.get("peak_ms")):
            continue
        d = per_symbol.get(str(e.get("symbol"))) if has_sym else df_1m
        if d is None or len(d) == 0:
            continue
        peak_ms = int(e["peak_ms"])
        after = d[(d["ts_ms"] >= peak_ms)
                  & (d["ts_ms"] <= peak_ms + horizon_min * MIN_MS)]
        if after.empty:
            continue
        n_valid += 1
        peak_u = int(after["high_u"].to_numpy()[0])
        for dd in drawdowns:
            hit = after[after["low_u"] <= int(peak_u * (1.0 - dd))]
            if not hit.empty:
                times[dd].append(float((int(hit["ts_ms"].to_numpy()[0]) - peak_ms)
                                       // MIN_MS))

    rows: list[dict] = []
    for dd in drawdowns:
        s = pd.Series(times[dd], dtype="float64")
        rows.append({"drawdown": dd, "metric": f"peak_to_-{int(dd * 100)}pct",
                     "n": float(n_valid), "reached": float(len(s)),
                     "reach_rate": (len(s) / n_valid) if n_valid else _NAN,
                     "horizon_min": float(horizon_min), **_dist(s, "minutes_")})
    halt = _num(ev, "halt_gap_count").dropna()
    rows.append({"drawdown": _NAN, "metric": "halt_gaps", "n": float(len(ev)),
                 "reached": _NAN, "reach_rate": _NAN,
                 "halt_any_share": float((halt > 0).mean()) if len(halt) else _NAN,
                 "halt_mean": float(halt.mean()) if len(halt) else _NAN,
                 **_dist(halt, "minutes_")})
    return _stamp(pd.DataFrame(rows), gate, n_ex)


# --------------------------------------------------------------------------- #
# q5 — 전조 스코어 조건부 기대수익
# --------------------------------------------------------------------------- #
def q5_expectancy(events: pd.DataFrame, feats: pd.DataFrame,
                  cost_roundtrip: float = 0.01, *,
                  gate: str = "exclude",
                  score_col: str | None = None,
                  n_buckets: int = 4,
                  cost_commission: float = 0.002,
                  cost_fx: float = 0.003) -> pd.DataFrame:
    """전조 스코어 버킷 × 청산정책별 기대수익 분포 (왕복 비용 차감).

    비용 분해 (계약 A2 §5): `cost_commission` 왕복 수수료(US 0.1%/체결 → 0.2%),
    `cost_fx` 환전 스프레드, 나머지가 `cost_slippage`. 세 값의 합이 `cost_roundtrip` 이다.
    기본 1% 는 **보수적 총비용**이며 수수료만의 0.2% 와 다르다.

    청산정책
        t0_to_30m    T0 종가 진입 → 30분 후 종가 청산 (`ret_30m`)
        t0_to_close  T0 종가 진입 → 정규장 종가 청산 (`ret_close`)
        half_peak    피크 상승분의 절반 포착 (`0.5 * peak_ret`) — **실행 가능성 상한선**이며
                     달성 가정이 아니다 (docs/07 §6.5)
    """
    ev, n_ex = _gate(events, gate)
    joined = join_events_features(ev, feats)
    slip = cost_roundtrip - cost_commission - cost_fx
    cost_cols = {"cost_roundtrip": cost_roundtrip, "cost_commission": cost_commission,
                 "cost_fx": cost_fx, "cost_slippage": max(0.0, slip)}
    note = "" if slip >= 0 else "cost_commission+cost_fx > cost_roundtrip"

    if score_col is None:
        for cand in ("score", "precursor_score", "rvol_at_cutoff", "rvol_at_t0"):
            if cand in joined.columns and joined[cand].notna().any():
                score_col = cand
                break

    if len(joined) == 0:
        rows = [{"bucket": "all", "policy": p, "n": 0.0, "score_col": score_col or "",
                 **cost_cols, "note": note} for p in EXIT_POLICIES]
        return _stamp(pd.DataFrame(rows), gate, n_ex)

    joined = joined.copy()
    joined["_gross_t0_to_30m"] = _num(joined, "ret_30m")
    joined["_gross_t0_to_close"] = _num(joined, "ret_close")
    joined["_gross_half_peak"] = _num(joined, "peak_ret") * 0.5

    buckets: list[tuple[str, pd.DataFrame]] = [("all", joined)]
    if score_col and int(joined[score_col].notna().sum()) >= n_buckets:
        try:
            q = pd.qcut(joined[score_col], n_buckets, duplicates="drop")
            buckets += [(f"{score_col}({iv.left:.3g},{iv.right:.3g}]", g)
                        for iv, g in joined.groupby(q, observed=True)]
        except ValueError:
            pass

    rows = []
    for bname, g in buckets:
        for policy in EXIT_POLICIES:
            gross = g[f"_gross_{policy}"].dropna()
            net = gross - cost_roundtrip
            rows.append({
                "bucket": bname, "policy": policy, "score_col": score_col or "",
                "n": float(len(gross)),
                "mean_gross": float(gross.mean()) if len(gross) else _NAN,
                "median_gross": float(gross.median()) if len(gross) else _NAN,
                "win_rate_gross": float((gross > 0).mean()) if len(gross) else _NAN,
                "mean_net": float(net.mean()) if len(net) else _NAN,
                "median_net": float(net.median()) if len(net) else _NAN,
                "win_rate_net": float((net > 0).mean()) if len(net) else _NAN,
                "p25_net": float(net.quantile(0.25)) if len(net) else _NAN,
                "p75_net": float(net.quantile(0.75)) if len(net) else _NAN,
                "positive_expectancy": bool(net.mean() > 0) if len(net) else False,
                **cost_cols, "note": note,
            })
    return _stamp(pd.DataFrame(rows), gate, n_ex)


# --------------------------------------------------------------------------- #
# q6 — 시간대 효과
# --------------------------------------------------------------------------- #
def _hod_min_from_open(ev: pd.DataFrame) -> pd.Series:
    """정규장 개장 시각을 역산해 HOD 의 개장 후 경과분을 구한다.

    `t0_min_from_open` = (t0 - 정규장개장)/1분 이므로 개장 시각 = t0 - that*60000.
    """
    mfo = _num(ev, "t0_min_from_open")
    open_ms = _num(ev, "t0_ms") - mfo * MIN_MS
    return (_num(ev, "hod_ms") - open_ms) / MIN_MS


def q6_time_of_day(events: pd.DataFrame, *, gate: str = "exclude") -> pd.DataFrame:
    """개장 15분 / 10:00 ET 전후 / 마감 전의 신호 성능 차이.

    버킷 기준은 `t0_min_from_open`(정규장 개장 후 경과분, 음수 = 개장 전).
    `hod_within_15min_share` 는 docs/02 §2.4 의 "HOD 46.6% 가 개장 15분 내" 재검증용.
    """
    ev, n_ex = _gate(events, gate)
    if len(ev) == 0 or "t0_min_from_open" not in ev.columns:
        return _stamp(pd.DataFrame([{"bucket": b, "min_from_open_lo": float(lo),
                                     "min_from_open_hi": float(hi), "n": 0.0}
                                    for b, lo, hi in TOD_BUCKETS]), gate, n_ex)

    mfo = _num(ev, "t0_min_from_open")
    hod_mfo = _hod_min_from_open(ev)
    rows: list[dict] = []
    for bname, lo, hi in TOD_BUCKETS:
        sel = (mfo >= lo) & (mfo < hi)
        g = ev[sel.fillna(False)]
        row = {"bucket": bname, "min_from_open_lo": float(lo),
               "min_from_open_hi": float(hi), "n": float(len(g))}
        if len(g):
            hm = hod_mfo[sel.fillna(False)]
            row.update({
                "median_peak_ret": float(_num(g, "peak_ret").median()),
                "median_ret_30m": float(_num(g, "ret_30m").median()),
                "median_ret_close": float(_num(g, "ret_close").median()),
                "median_time_to_peak_min": float(_num(g, "time_to_peak_min").median()),
                "hod_within_15min_share": _share(hm, lambda s: (s >= 0) & (s <= 15)),
                "hod_before_1000et_share": _share(hm, lambda s: s <= 30),
                "closed_below_vwap_share": _bool_share(g, "closed_below_vwap"),
                "fade_share": _share(g.get("outcome"), lambda s: s == "fade")
                if "outcome" in g.columns else _NAN,
                "dump_share": _share(g.get("outcome"), lambda s: s == "dump")
                if "outcome" in g.columns else _NAN,
            })
        rows.append(row)
    return _stamp(pd.DataFrame(rows), gate, n_ex)


# --------------------------------------------------------------------------- #
# 기저율 재검증 + 종합
# --------------------------------------------------------------------------- #
def base_rate_comparison(events: pd.DataFrame, *,
                         gate: str = "exclude") -> pd.DataFrame:
    """docs/02 §2.4 SmallCapLab 실측 기저율과 우리 데이터의 대조표."""
    ev, n_ex = _gate(events, gate)
    rows: list[dict] = []

    def add(name: str, observed: float) -> None:
        known = KNOWN_BASE_RATES[name]
        rows.append({"base_rate": name, "known": known, "observed": observed,
                     "n": float(len(ev)),
                     "delta": (observed - known) if observed == observed else _NAN})

    if len(ev) == 0:
        for k in KNOWN_BASE_RATES:
            add(k, _NAN)
        return _stamp(pd.DataFrame(rows), gate, n_ex)

    add("fade_rate", _share(ev.get("outcome"), lambda s: s.isin(["fade", "dump"]))
        if "outcome" in ev.columns else _NAN)
    add("break_20pct_from_hod", _share(_num(ev, "retrace_close"), lambda s: s <= -0.20))
    add("close_below_vwap", _bool_share(ev, "closed_below_vwap"))
    hm = _hod_min_from_open(ev)
    add("hod_before_1000et", _share(hm, lambda s: s <= 30))
    add("hod_within_15min", _share(hm, lambda s: (s >= 0) & (s <= 15)))
    return _stamp(pd.DataFrame(rows), gate, n_ex)


def run_all(events: pd.DataFrame, feats: pd.DataFrame, rankings: pd.DataFrame,
            df_1m: pd.DataFrame, *, gate: str = "exclude",
            cost_roundtrip: float = 0.01) -> dict[str, pd.DataFrame]:
    """검증 질문 6개 + 기저율 대조를 한 번에. 리포트 입력."""
    return {
        "q1_volume_leadtime": q1_volume_leadtime(events, feats, gate=gate),
        "q2_ranking_lead_lag": q2_ranking_lead_lag(events, rankings, gate=gate),
        "q3_daymarket_persistence": q3_daymarket_persistence(events, gate=gate),
        "q4_dump_speed": q4_dump_speed(events, df_1m, gate=gate),
        "q5_expectancy": q5_expectancy(events, feats, cost_roundtrip, gate=gate),
        "q6_time_of_day": q6_time_of_day(events, gate=gate),
        "base_rates": base_rate_comparison(events, gate=gate),
    }

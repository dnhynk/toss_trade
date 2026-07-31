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

#: **판정·선택에 쓸 수 있는 정책** (사전등록 §2.6).
#: `half_peak` 은 피크의 절반을 항상 잡는다는 비현실적 가정이라 승률이 구조적으로 1.0 이
#: 된다 — **실행 가능성 상한선 보고 전용**이며 §4.5 의 어떤 판정·선택에도 쓰지 않는다.
#: 하류에서 정책을 고를 때는 EXIT_POLICIES 가 아니라 이 튜플을 참조하라.
DECISION_POLICIES = ("t0_to_30m", "t0_to_close")
#: 보고 전용(판정 금지) 정책
REPORT_ONLY_POLICIES = ("half_peak",)

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


def _bucket_of(mfo: pd.Series) -> pd.Series:
    """경과분 → 버킷 라벨 (어디에도 안 들어가면 NaN)."""
    out = pd.Series([None] * len(mfo), index=mfo.index, dtype="object")
    for bname, lo, hi in TOD_BUCKETS:
        sel = (mfo >= lo) & (mfo < hi)
        out[sel.fillna(False)] = bname
    return out


def q6_time_of_day(events: pd.DataFrame, *, gate: str = "exclude",
                   sensitivity_min: int = 1) -> pd.DataFrame:
    """개장 15분 / 10:00 ET 전후 / 마감 전의 신호 성능 차이.

    버킷 기준은 `t0_min_from_open`(정규장 개장 후 경과분, 음수 = 개장 전).
    `hod_within_15min_share` 는 docs/02 §2.4 의 "HOD 46.6% 가 개장 15분 내" 재검증용.

    **±1분 감도 의무 병기 (사전등록 §7-f)**: 봉 타임스탬프가 봉의 시작인지 끝인지 아직
    미확정이라 버킷 경계가 1분 흔들릴 수 있다. 그래서 `t0_min_from_open` 을 ±`sensitivity_min`
    만큼 민 경우의 버킷 인원(`n_minus`/`n_plus`)과 소속이 바뀌는 이벤트 수
    (`n_boundary_sensitive`), 그리고 `boundary_sensitive` 플래그를 함께 낸다.
    **경계 이동으로 결론이 뒤집히는 버킷은 판정 불가로 다룬다.**
    """
    ev, n_ex = _gate(events, gate)
    if len(ev) == 0 or "t0_min_from_open" not in ev.columns:
        return _stamp(pd.DataFrame([{"bucket": b, "min_from_open_lo": float(lo),
                                     "min_from_open_hi": float(hi), "n": 0.0,
                                     "n_minus": 0.0, "n_plus": 0.0,
                                     "n_boundary_sensitive": 0.0,
                                     "boundary_sensitive": False}
                                    for b, lo, hi in TOD_BUCKETS]), gate, n_ex)

    mfo = _num(ev, "t0_min_from_open")
    base_bucket = _bucket_of(mfo)
    minus_bucket = _bucket_of(mfo - sensitivity_min)
    plus_bucket = _bucket_of(mfo + sensitivity_min)
    moved = (base_bucket != minus_bucket) | (base_bucket != plus_bucket)
    hod_mfo = _hod_min_from_open(ev)
    rows: list[dict] = []
    for bname, lo, hi in TOD_BUCKETS:
        sel = (mfo >= lo) & (mfo < hi)
        g = ev[sel.fillna(False)]
        n_sensitive = int((moved & sel.fillna(False)).sum())
        row = {"bucket": bname, "min_from_open_lo": float(lo),
               "min_from_open_hi": float(hi), "n": float(len(g)),
               # 사전등록 §7-f — ±1분 경계 감도 의무 병기
               "n_minus": float((minus_bucket == bname).sum()),
               "n_plus": float((plus_bucket == bname).sum()),
               "n_boundary_sensitive": float(n_sensitive),
               "boundary_sensitive": bool(n_sensitive)}
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


# --------------------------------------------------------------------------- #
# 사전등록 §2.7 — 분석 표본 유니버스 필터
# --------------------------------------------------------------------------- #
MICRO = 1_000_000
#: 사전등록 §2.7 확정값 (마이크로달러)
SAMPLE_PRICE_MIN_U = 100_000                 # $0.10
SAMPLE_PRICE_MAX_U = 20_000_000              # $20.00
SAMPLE_MCAP_MIN_U = 10_000_000 * MICRO       # $10M
SAMPLE_MCAP_MAX_U = 300_000_000 * MICRO      # $300M
COMMON_SECURITY_TYPES = ("STOCK", "FOREIGN_STOCK")

#: 제외 사유 코드 (리포트 카운트 키). 순서 = 판정 우선순위.
EXCLUSION_REASONS = (
    "not_common_stock",        # 보통주 아님 / ETF·ETN
    "not_active",              # status != ACTIVE
    "meta_missing",            # symbols 메타에 없음
    "t0_price_unavailable",    # T0 봉 종가(원주가)를 못 구함 → 검증 불가
    "price_out_of_range",      # 종가 ∉ [$0.10, $20]
    "mcap_out_of_range",       # 시총 ∉ [$10M, $300M]
    "half_day_length_sample",  # 반일장 — (세션,길이) 표본 부족으로 RVOL 미가용
)

#: 시총 경계 ±10% 감도 밴드 (사전등록 §2.7). **판정은 확정 경계로만** 하고 밴드는
#: 보고 전용이다 — 경계를 움직이는 근거로 쓸 수 없다.
MCAP_BAND_FRAC = 0.10
MCAP_BAND_ROWS = ("mcap_band_low_kept", "mcap_band_low_excluded",
                  "mcap_band_high_kept", "mcap_band_high_excluded")


def _t0_close_map(events: pd.DataFrame, df_1m: pd.DataFrame | None) -> dict:
    """(symbol, t0_ms) → T0 봉 종가(원주가). df_1m 이 없으면 빈 맵."""
    if df_1m is None or len(df_1m) == 0:
        return {}
    has_sym = "symbol" in df_1m.columns
    cols = ["ts_ms", "close_u"] + (["symbol"] if has_sym else [])
    sub = df_1m[cols]
    if has_sym:
        return {(str(s), int(t)): int(c)
                for s, t, c in zip(sub["symbol"], sub["ts_ms"], sub["close_u"])}
    return {(None, int(t)): int(c) for t, c in zip(sub["ts_ms"], sub["close_u"])}


def _dropped_length_keys(curve) -> set:
    if curve is None:
        return set()
    try:
        return {tuple(k) for k in curve.attrs.get("dropped_below_min_days", [])}
    except (AttributeError, TypeError):
        return set()


def apply_sample_filter(events: pd.DataFrame, meta: pd.DataFrame, *,
                        df_1m: pd.DataFrame | None = None,
                        curve=None,
                        calendar=None,
                        price_min_u: int = SAMPLE_PRICE_MIN_U,
                        price_max_u: int = SAMPLE_PRICE_MAX_U,
                        mcap_min_u: int = SAMPLE_MCAP_MIN_U,
                        mcap_max_u: int = SAMPLE_MCAP_MAX_U,
                        split_excluded: int | None = None,
                        split_dates_applied: bool | None = None,
                        r_uncomputable: int | None = None
                        ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """사전등록 §2.7 표본 필터. 반환 `(kept, reasons_df)`.

    T0 **시점 기준**으로 판정한다:
        증권 종류  보통주(STOCK/FOREIGN_STOCK, `is_common`) · status=ACTIVE (ETF/ETN 제외)
        명목 가격  T0 봉 종가(**원주가**) ∈ [$0.10, $20.00]
        시총       `sharesOutstanding × T0 종가(원주가)` ∈ [$10M, $300M]

    추가로 **반일장(세션 길이 표본 부족)** 을 별도 사유로 센다 — 곡선에서
    `dropped_below_min_days` 로 버려진 (세션, 길이) 버킷에 T0 가 속하면 RVOL 이 미가용이라
    주 분석에 들어갈 수 없다 (docs/07 §2.4a).

    `reasons_df` 컬럼: reason, n, share. 제외 0건인 사유도 행으로 남긴다 —
    "그 사유가 0이었다"와 "그 사유를 검사하지 않았다"를 구분하기 위해서다.

    한계(사전등록 §2.7 명문): `sharesOutstanding` 은 현재 스냅샷이라 과거 시점 시총은
    근사치이고, float 이 아니라 발행주식수다. 둘 다 그대로 보고한다.
    """
    n_total = 0 if events is None else len(events)
    counts = dict.fromkeys(EXCLUSION_REASONS, 0)
    if n_total == 0:
        rows = [{"reason": r, "n": 0, "share": _NAN, "scope": "event"}
                for r in EXCLUSION_REASONS]
        # 이벤트가 0건이어도 분할 제외 건수·출처 표식은 남아야 한다 (§7-e).
        if split_excluded is not None:
            rows.append({"reason": "split_excluded", "n": int(split_excluded),
                         "share": _NAN, "scope": "symbol_day"})
        if split_dates_applied is False:
            rows.append({"reason": "(split_dates_not_applied)", "n": _NAN,
                         "share": _NAN, "scope": "provenance",
                         "note": "split_dates 미적용 — 이 검출 결과는 주 분석에 쓸 수 없다"
                                 " (사전등록 §7-e)"})
        reasons = pd.DataFrame(rows)
        reasons["n_total"] = 0
        reasons["n_kept"] = 0
        return (events if events is not None else pd.DataFrame()), reasons

    meta_by_symbol: dict = {}
    if meta is not None and len(meta) and "symbol" in meta.columns:
        meta_by_symbol = {str(r["symbol"]): r for _i, r in meta.iterrows()}
    closes = _t0_close_map(events, df_1m)
    dropped = _dropped_length_keys(curve)

    keep_mask: list[bool] = []
    mcap_of: list[int | None] = []
    for _i, e in events.iterrows():
        mcaps: list[int] = []
        sym = str(e.get("symbol")) if pd.notna(e.get("symbol")) else None
        t0 = int(e["t0_ms"])
        reason = None

        m = meta_by_symbol.get(sym)
        if m is None:
            reason = "meta_missing"
        else:
            sec = str(m.get("security_type", "")).strip().upper()
            is_common = m.get("is_common", True)
            if pd.isna(is_common):
                is_common = True
            if (not bool(is_common)) or sec not in COMMON_SECURITY_TYPES:
                reason = "not_common_stock"
            elif str(m.get("status", "")).strip().upper() != "ACTIVE":
                reason = "not_active"

        close_u = closes.get((sym, t0), closes.get((None, t0)))
        if reason is None:
            if close_u is None:
                reason = "t0_price_unavailable"
            elif not (price_min_u <= close_u <= price_max_u):
                reason = "price_out_of_range"
            else:
                shares = m.get("shares_outstanding_qu") if m is not None else None
                if shares is None or pd.isna(shares) or int(shares) <= 0:
                    reason = "meta_missing"
                else:
                    mcap_u = (close_u * int(shares)) // MICRO
                    mcaps.append(mcap_u)
                    if not (mcap_min_u <= mcap_u <= mcap_max_u):
                        reason = "mcap_out_of_range"

        if reason is None and dropped:
            from .baselines import curve_key
            key = curve_key(curve, t0, calendar=calendar) if curve is not None else None
            if key is not None and (key[0], key[1]) in dropped:
                reason = "half_day_length_sample"

        if reason is not None:
            counts[reason] += 1
        keep_mask.append(reason is None)
        mcap_of.append(mcaps[0] if mcaps else None)

    kept = events[pd.Series(keep_mask, index=events.index)]

    # 시총 경계 ±10% 밴드 (사전등록 §2.7) — 걸린 쪽/통과한 쪽 **양쪽 모두** 보고.
    lo_band = (int(mcap_min_u * (1 - MCAP_BAND_FRAC)), int(mcap_min_u * (1 + MCAP_BAND_FRAC)))
    hi_band = (int(mcap_max_u * (1 - MCAP_BAND_FRAC)), int(mcap_max_u * (1 + MCAP_BAND_FRAC)))
    band = dict.fromkeys(MCAP_BAND_ROWS, 0)
    for mc, keep in zip(mcap_of, keep_mask):
        if mc is None:
            continue
        if lo_band[0] <= mc <= lo_band[1]:
            band["mcap_band_low_kept" if keep else "mcap_band_low_excluded"] += 1
        if hi_band[0] <= mc <= hi_band[1]:
            band["mcap_band_high_kept" if keep else "mcap_band_high_excluded"] += 1

    # 3차 감사 F-5: 곡선이 없으면 반일장 검사를 **하지 않은 것**이지 0건이 아니다.
    half_day_checked = curve is not None
    rows = []
    for r in EXCLUSION_REASONS:
        if r == "half_day_length_sample" and not half_day_checked:
            rows.append({"reason": r, "n": _NAN, "share": _NAN, "scope": "event",
                         "note": "curve 미제공 — 검사하지 않음(0건 아님)"})
        else:
            rows.append({"reason": r, "n": counts[r], "share": counts[r] / n_total,
                         "scope": "event"})
    if not half_day_checked:
        rows.append({"reason": "(half_day_check_not_run)", "n": _NAN, "share": _NAN,
                     "scope": "provenance",
                     "note": "curve 를 넘기지 않아 반일장(세션 길이 표본 부족) 사유를"
                             " 판정하지 못했다 (3차 감사 F-5)"})
    if r_uncomputable is not None:
        rows.append({"reason": "r_uncomputable", "n": int(r_uncomputable),
                     "share": _NAN, "scope": "symbol_day",
                     "note": "분할 신호 r 을 계산할 수 없었던 (심볼, 매매일) 수"
                             " (사전등록 §7-e 보고 의무)"})
    rows += [{"reason": r, "n": band[r], "share": band[r] / n_total,
              "scope": "mcap_band_sensitivity"} for r in MCAP_BAND_ROWS]
    # 사전등록 §7-e: 분할일 당일조건 제외 건수는 **(심볼,매매일) 단위**라 이벤트 제외와
    # 합산하면 안 된다 — scope 로 구분한다.
    if split_excluded is not None:
        rows.append({"reason": "split_excluded", "n": int(split_excluded),
                     "share": _NAN, "scope": "symbol_day"})
    if split_dates_applied is False:
        rows.append({"reason": "(split_dates_not_applied)", "n": _NAN, "share": _NAN,
                     "scope": "provenance",
                     "note": "split_dates 미적용 — 이 검출 결과는 주 분석에 쓸 수 없다"
                             " (사전등록 §7-e)"})
    reasons = pd.DataFrame(rows)
    reasons["n_total"] = n_total
    reasons["n_kept"] = int(len(kept))
    return kept, reasons


def run_all(events: pd.DataFrame, feats: pd.DataFrame, rankings: pd.DataFrame,
            df_1m: pd.DataFrame, *, gate: str = "exclude",
            cost_roundtrip: float = 0.01,
            meta: pd.DataFrame | None = None,
            curve=None, calendar=None,
            r_uncomputable: int | None = None) -> dict[str, pd.DataFrame]:
    """검증 질문 6개 + 기저율 대조를 한 번에. 리포트 입력.

    `meta`(W2 `Reader.symbols()`)를 주면 사전등록 §2.7 표본 필터를 적용하고 q1~q6 를
    **걸러진 표본**으로 계산한다. 제외 내역은 `sample_filter` 섹션으로 나간다.
    주지 않으면 필터를 적용하지 않고 그 사실을 섹션에 남긴다 — 필터를 안 돌린 것과
    제외가 0건인 것은 리포트에서 반드시 구분돼야 한다.
    """
    # 사전등록 §7-e: split_dates 적용 여부는 events.attrs 가 운반한다.
    attrs = getattr(events, "attrs", {}) or {}
    split_applied = attrs.get("split_dates_applied")
    split_n = attrs.get("split_excluded")
    if meta is not None:
        events, reasons = apply_sample_filter(events, meta, df_1m=df_1m, curve=curve,
                                              calendar=calendar,
                                              split_excluded=split_n,
                                              split_dates_applied=split_applied,
                                              r_uncomputable=r_uncomputable)
    else:
        reasons = pd.DataFrame([{
            "reason": "(filter_not_applied)", "n": _NAN, "share": _NAN,
            "n_total": 0 if events is None else len(events),
            "n_kept": 0 if events is None else len(events),
            "note": "meta 미제공 — 사전등록 §2.7 표본 필터를 적용하지 않았다",
            "scope": "provenance",
        }])
        if split_applied is False:
            reasons = pd.concat([reasons, pd.DataFrame([{
                "reason": "(split_dates_not_applied)", "n": _NAN, "share": _NAN,
                "scope": "provenance",
                "note": "split_dates 미적용 — 이 검출 결과는 주 분석에 쓸 수 없다"
                        " (사전등록 §7-e)"}])], ignore_index=True)
    out = {
        "q1_volume_leadtime": q1_volume_leadtime(events, feats, gate=gate),
        "q2_ranking_lead_lag": q2_ranking_lead_lag(events, rankings, gate=gate),
        "q3_daymarket_persistence": q3_daymarket_persistence(events, gate=gate),
        "q4_dump_speed": q4_dump_speed(events, df_1m, gate=gate),
        "q5_expectancy": q5_expectancy(events, feats, cost_roundtrip, gate=gate),
        "q6_time_of_day": q6_time_of_day(events, gate=gate),
        "base_rates": base_rate_comparison(events, gate=gate),
        "sample_filter": reasons,
    }
    return out

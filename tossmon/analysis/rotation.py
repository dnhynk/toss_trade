"""회전 인지형 전조 측정식 (rotation-aware precursor measures) — docs/17.

이 모듈은 사전등록(docs/12)의 얼린 측정식 `vol_surge_lead_min` 을 **대체하지 않는다.**
대체하려면 §9 개정이 필요하며, 이 모듈은 그 개정 제안의 근거를 만드는 **측정 도구**다.
가설 검정이 아니므로 §4.5 2단계 조건부 규칙 탐색과 무관하고 검증 깔때기를 소모하지 않는다.

논제(docs/16): 개미 유동성은 그날의 러너들 **사이를 옮겨다닌다.** 그래서 묻는 질문이
"이 종목이 곧 급등하나"(단일 종목 절대 예측)가 아니라 **"유동성이 지금 어디로
옮겨가고 있나"**(러너 집합 안에서의 상대 회전)로 바뀐다.

## 설계 금지 규칙 (구 측정식의 실패에서 도출 — docs/17 §1)

`vol_surge_lead_min` 은 **무체결 분을 거래량 0 으로 채워** 240분 기준선의 분산을 냈다.
희소 테이프에서는 기준선이 거의 전부 0 이라 분산이 0 에 수렴하고, **아무 체결이나
z≥3 을 발화**시킨다. 실측 결과 검출률이 체결률 분위별로 0.876 → 0.542 → 0.487 → 0.280,
즉 **데이터 밀도의 역지표**였고 검출의 51%가 측정 창 가장자리에 몰렸다.

이 모듈의 전 측정식은 다음 3규칙을 강제한다:

1. **결측을 값으로 채우지 않는다.** 봉이 없는 분은 통계에서 제외하며, 0 으로 세지 않는다.
   `PRINT` 는 `vol_qu > 0` 인 봉만이다.
2. **시계(clock-time)가 아니라 사건 시간(event-time)** — 프린트 발생 순서 위에서
   통계를 낸다. 프린트 간격의 **비(ratio)** 는 척도 불변이라 밀도에 둔감하다.
3. **정의되지 않으면 `NaN`** 을 돌려주고 호출부가 그 수를 셀 수 있게 한다.
   "값이 나온 것"과 "신호가 있는 것"을 절대 섞지 않는다.

추가로 횡단면 측정식은 **같은 분에 관측된 동료 종목들 사이의 백분위**로 정의한다 —
종목 자신의 시계열 기준선을 쓰지 않으므로 구 측정식의 실패 양식이 원리상 재현되지 않는다.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

MIN_MS = 60_000
_NAN = float("nan")

#: 프린트 = 거래량이 있는 1분봉. 무체결 분은 **행 자체가 없다**(0 으로 채우지 않는다).
PRINT_MIN_VOL_QU = 1

#: **단위 규약** (감사 4차 B10). `amount_u12` = `close_u × vol_qu`
#: = 마이크로달러 × 마이크로주 = **USD × 1e12**. 나누지 않은 원시 곱이다.
#: `execution.level_notional_u` 는 `// MICRO` 를 해서 **마이크로달러**를 만든다 —
#: 두 모듈의 단위가 다르므로 컬럼 이름에 단위를 박아 혼동을 원천 차단한다.
#: 이 모듈 안에서는 `share`(비율)로만 쓰여 단위가 약분되지만, 프레임이 밖으로 나가면
#: 그 보호가 사라진다(3차 감사의 `vol_qu/10000` 과 같은 계열의 잠재 결함).
AMOUNT_PER_USD = 10 ** 12

#: 사건 시간 통계의 기본 블록 크기 (프린트 개수 단위 — 분 단위가 아니다).
DEFAULT_RECENT_PRINTS = 5
DEFAULT_BASELINE_PRINTS = 20

#: 횡단면 기본 파라미터.
DEFAULT_WINDOW_MIN = 30
DEFAULT_MIN_PRINTS = 3
DEFAULT_MIN_COHORT = 10

#: 회전 검출 임계 — 동료 대비 상위 백분위.
DEFAULT_ROTATION_THRESHOLD = 0.90

ROTATION_COLUMNS = ("symbol", "n_prints", "n_prints_prev", "amount_u12",
                    "amount_u12_prev",
                    "share", "share_prev", "share_delta", "share_delta_pct",
                    "rank_now", "rank_prev", "rank_delta", "rank_delta_pct")


# --------------------------------------------------------------------------- #
# 프린트(사건) 추출 — 결측은 결측으로
# --------------------------------------------------------------------------- #
def print_frame(df_1m: pd.DataFrame, *, t_from: int | None = None,
                t_to: int | None = None) -> pd.DataFrame:
    """거래량이 있는 봉만 시간순으로. **무체결 분은 채우지 않는다** (금지 규칙 1).

    `t_to` 는 **배타적**이다 — 엄격 컷오프(`ts_ms < t0_ms`, A1 §1)를 그대로 따른다.

    **주의 (docs/47 §5)**: `ts_ms` 는 **종료 시각 라벨**이라(사전등록 §6.1) 라벨 `t_to`
    인 봉의 내용도 `t_to` **이전**이다. 즉 이 배타적 컷은 누출 방지가 아니라
    **1봉(60초) 과보수**다. `t_from` 쪽도 같은 이유로 `[t_from−60초, t_from)` 을 담은
    봉 하나를 더 들인다. **산술은 사전등록 §2.1 예측 1a 문언에 묶여 있어 그대로 뒀다 —
    변경은 §9 개정 후 별도 태스크다.** (창/경계 판정인 `_window_stats` 는 이번에 고쳤다.)
    """
    if df_1m is None or len(df_1m) == 0 or "ts_ms" not in df_1m.columns:
        return pd.DataFrame(columns=["ts_ms", "vol_qu", "close_u", "amount_u12"])
    d = df_1m
    if t_from is not None:
        d = d[d["ts_ms"] >= t_from]
    if t_to is not None:
        d = d[d["ts_ms"] < t_to]
    vol = pd.to_numeric(d.get("vol_qu"), errors="coerce")
    d = d[vol.reindex(d.index).fillna(0) >= PRINT_MIN_VOL_QU]
    if len(d) == 0:
        return pd.DataFrame(columns=["ts_ms", "vol_qu", "close_u", "amount_u12"])
    out = d[["ts_ms", "vol_qu", "close_u"]].copy().sort_values("ts_ms")
    out["amount_u12"] = [int(c) * int(v) for c, v in
                     zip(out["close_u"].tolist(), out["vol_qu"].tolist())]
    return out.reset_index(drop=True)


def amount_usd(amount_u12) -> float:
    """`amount_u12`(USD × 1e12) → 달러. 단위를 밖으로 내보낼 때 반드시 통과시킨다."""
    try:
        return float(amount_u12) / AMOUNT_PER_USD
    except (TypeError, ValueError):
        return _NAN


def inter_print_gaps_min(ts_ms) -> np.ndarray:
    """연속한 프린트 사이의 간격(분). 프린트가 2개 미만이면 빈 배열."""
    ts = np.asarray(list(ts_ms), dtype="int64")
    if ts.size < 2:
        return np.empty(0, dtype="float64")
    return np.diff(ts) / MIN_MS


def _blocks(prints: pd.DataFrame, recent: int, baseline: int):
    """(최근 블록, 기준 블록). 프린트가 모자라면 (None, None)."""
    n = len(prints)
    if n < recent + baseline:
        return None, None
    return prints.iloc[n - recent:], prints.iloc[n - recent - baseline:n - recent]


# --------------------------------------------------------------------------- #
# 사건 시간 측정식 — 척도 불변(밀도에 둔감)
# --------------------------------------------------------------------------- #
def print_intensity_ratio(prints: pd.DataFrame, *,
                          recent_prints: int = DEFAULT_RECENT_PRINTS,
                          baseline_prints: int = DEFAULT_BASELINE_PRINTS) -> float:
    """PIR — 프린트가 **빨라지고 있는가**. 기준 블록 간격 중앙값 / 최근 블록 간격 중앙값.

    >1 이면 체결이 촘촘해지는 중(유동성 유입). 모든 간격에 상수를 곱해도 값이 변하지
    않으므로(척도 불변) 희소 종목과 활발한 종목을 같은 잣대로 비교할 수 있다.
    프린트가 `recent+baseline` 개 미만이거나 최근 간격 중앙값이 0 이면 `NaN`.
    """
    rec, base = _blocks(prints, recent_prints, baseline_prints)
    if rec is None:
        return _NAN
    g_rec = inter_print_gaps_min(rec["ts_ms"])
    g_base = inter_print_gaps_min(base["ts_ms"])
    if g_rec.size == 0 or g_base.size == 0:
        return _NAN
    m_rec, m_base = float(np.median(g_rec)), float(np.median(g_base))
    if not (m_rec > 0) or not math.isfinite(m_base):
        return _NAN
    return m_base / m_rec


def print_size_ratio(prints: pd.DataFrame, *,
                     recent_prints: int = DEFAULT_RECENT_PRINTS,
                     baseline_prints: int = DEFAULT_BASELINE_PRINTS) -> float:
    """PSR — 프린트 **한 건당 크기**가 커지고 있는가. 최근/기준 거래량 중앙값 비.

    척도 불변(거래량 단위를 바꿔도 불변). 기준 중앙값이 0 이면 `NaN` — 0 으로 나누지
    않고 결측을 돌려준다(금지 규칙 3).
    """
    rec, base = _blocks(prints, recent_prints, baseline_prints)
    if rec is None:
        return _NAN
    v_rec = float(np.median(pd.to_numeric(rec["vol_qu"], errors="coerce").dropna()))
    v_base = float(np.median(pd.to_numeric(base["vol_qu"], errors="coerce").dropna()))
    if not (v_base > 0):
        return _NAN
    return v_rec / v_base


def dormancy_wake_ratio(prints: pd.DataFrame, *,
                        baseline_prints: int = DEFAULT_BASELINE_PRINTS) -> float:
    """DWR — **휴면 각성**. 직전 침묵 구간이 평소 간격 대비 몇 배였나.

    마지막 간격 / 그 이전 `baseline_prints` 개 간격의 중앙값. 큰 값 = 오래 잠들어
    있다가 방금 깨어남. 희소 테이프에서도 정의되는 통계다 — 무체결을 0 으로 세지 않고
    **간격 자체를 관측값으로** 쓰기 때문이다.
    """
    if len(prints) < baseline_prints + 2:
        return _NAN
    gaps = inter_print_gaps_min(prints["ts_ms"])
    if gaps.size < baseline_prints + 1:
        return _NAN
    last = float(gaps[-1])
    base = float(np.median(gaps[-1 - baseline_prints:-1]))
    if not (base > 0):
        return _NAN
    return last / base


# --------------------------------------------------------------------------- #
# 횡단면 회전 측정식 — 동료 대비 백분위
# --------------------------------------------------------------------------- #
def _window_stats(day_bars: pd.DataFrame, lo: int, hi: int) -> pd.DataFrame:
    """**내용 구간** [lo, hi) 의 심볼별 (프린트 수, 거래대금). 무체결 분은 세지 않는다.

    `ts_ms` 는 종료 시각 라벨이라 내용 `[lo, hi)` 는 라벨 `(lo, hi]` 다 (docs/12 §6.1).
    """
    d = day_bars[(day_bars["ts_ms"] > lo) & (day_bars["ts_ms"] <= hi)]
    vol = pd.to_numeric(d["vol_qu"], errors="coerce").fillna(0)
    d = d[vol.reindex(d.index) >= PRINT_MIN_VOL_QU]
    if len(d) == 0:
        return pd.DataFrame(columns=["symbol", "n_prints", "amount_u12"])
    amt = [int(c) * int(v) for c, v in
           zip(d["close_u"].tolist(), d["vol_qu"].tolist())]
    tmp = pd.DataFrame({"symbol": d["symbol"].tolist(), "amount_u12": amt})
    g = tmp.groupby("symbol", sort=False).agg(n_prints=("amount_u12", "size"),
                                              amount_u12=("amount_u12", "sum"))
    return g.reset_index()


def rotation_scores(day_bars: pd.DataFrame, t_ms: int, *,
                    window_min: int = DEFAULT_WINDOW_MIN,
                    min_prints: int = DEFAULT_MIN_PRINTS,
                    min_cohort: int = DEFAULT_MIN_COHORT) -> pd.DataFrame:
    """시각 `t_ms` 에서 **동료 집합(cohort) 안의 회전 점수**.

    - 코호트 = 두 창 `[t-2W, t-W)`·`[t-W, t)` **양쪽 모두** 에서 프린트가
      `min_prints` 개 이상인 심볼. 한쪽만 관측된 심볼은 회전을 잴 수 없으므로
      코호트에서 빠지고, 호출부는 그 수를 셀 수 있다(금지 규칙 3).
      *한쪽만 관측된 심볼의 "거래대금 0"을 값으로 쓰지 않는다* — 그것이 구 측정식이
      저지른 바로 그 실수다(휴면 각성은 `dormancy_wake_ratio` 가 따로 잰다).
    - `share` = 코호트 내 거래대금 점유율. `share_delta` = 최근 창 − 직전 창.
    - `share_delta_pct` / `rank_delta_pct` = **코호트 내 백분위**(0~1).
      종목 자신의 시계열 기준선을 쓰지 않는 것이 핵심이다.

    `t_ms` 는 배타적(엄격 컷오프). 코호트가 `min_cohort` 미만이면 빈 프레임.
    """
    if day_bars is None or len(day_bars) == 0:
        return pd.DataFrame(columns=list(ROTATION_COLUMNS))
    w = window_min * MIN_MS
    now = _window_stats(day_bars, t_ms - w, t_ms)
    prev = _window_stats(day_bars, t_ms - 2 * w, t_ms - w)
    if now.empty or prev.empty:
        return pd.DataFrame(columns=list(ROTATION_COLUMNS))

    m = now.merge(prev, on="symbol", how="inner", suffixes=("", "_prev"))
    m = m[(m["n_prints"] >= min_prints) & (m["n_prints_prev"] >= min_prints)]
    if len(m) < min_cohort:
        return pd.DataFrame(columns=list(ROTATION_COLUMNS))

    tot_now = float(sum(int(x) for x in m["amount_u12"].tolist()))
    tot_prev = float(sum(int(x) for x in m["amount_u12_prev"].tolist()))
    if not (tot_now > 0 and tot_prev > 0):
        return pd.DataFrame(columns=list(ROTATION_COLUMNS))

    m = m.copy()
    m["share"] = [int(x) / tot_now for x in m["amount_u12"].tolist()]
    m["share_prev"] = [int(x) / tot_prev for x in m["amount_u12_prev"].tolist()]
    m["share_delta"] = m["share"] - m["share_prev"]
    # 순위는 거래대금 큰 쪽이 1위. 개선(상승)이 양수가 되도록 prev - now.
    m["rank_now"] = m["amount_u12"].rank(ascending=False, method="average")
    m["rank_prev"] = m["amount_u12_prev"].rank(ascending=False, method="average")
    m["rank_delta"] = m["rank_prev"] - m["rank_now"]
    m["share_delta_pct"] = m["share_delta"].rank(pct=True, method="average")
    m["rank_delta_pct"] = m["rank_delta"].rank(pct=True, method="average")
    return m[list(ROTATION_COLUMNS)].reset_index(drop=True)


def rotation_score_at(day_bars: pd.DataFrame, symbol: str, t_ms: int, *,
                      metric: str = "rank_delta_pct", **kw) -> float:
    """한 심볼의 회전 백분위. 코호트에 없으면 `NaN`."""
    sc = rotation_scores(day_bars, t_ms, **kw)
    if sc.empty:
        return _NAN
    hit = sc[sc["symbol"] == symbol]
    if hit.empty:
        return _NAN
    return float(hit[metric].to_numpy()[0])


def rotation_score_series(day_bars: pd.DataFrame, symbol: str, t0_ms: int, *,
                          scan_min: int, step_min: int = 5,
                          metric: str = "rank_delta_pct", **kw) -> pd.Series:
    """`[t0-scan, t0)` 을 `step_min` 간격으로 훑은 회전 백분위 시계열(엄격 컷오프)."""
    out: dict[int, float] = {}
    t = t0_ms - scan_min * MIN_MS
    while t < t0_ms:
        out[t] = rotation_score_at(day_bars, symbol, t, metric=metric, **kw)
        t += step_min * MIN_MS
    return pd.Series(out, dtype="float64")


def first_cross_lead_min(series: pd.Series, t0_ms: int, *,
                         threshold: float = DEFAULT_ROTATION_THRESHOLD) -> float:
    """임계를 **처음** 넘은 시각의 리드타임(분). 한 번도 못 넘으면 `NaN`."""
    s = series.dropna()
    if s.empty:
        return _NAN
    hit = s[s >= threshold]
    if hit.empty:
        return _NAN
    return float((t0_ms - int(hit.index[0])) // MIN_MS)


# --------------------------------------------------------------------------- #
# 실시간 전용 축 — 랭킹 회전 / 호가
# --------------------------------------------------------------------------- #
def ranking_top_changes(rankings: pd.DataFrame, *, top_n: int = 20,
                        ranking_type: str | None = None) -> pd.DataFrame:
    """상위 `top_n` **구성 변화**(진입/이탈)를 스냅 단위로. 이것이 회전의 직접 관측이다.

    입력은 `rankings_snap` 스키마(`snap_ms`, `ranking_type`, `rank`, `symbol`).
    반환: `snap_ms`, `symbol`, `event`(`enter`/`exit`), `rank`.
    """
    cols = ["snap_ms", "symbol", "event", "rank"]
    if rankings is None or len(rankings) == 0:
        return pd.DataFrame(columns=cols)
    d = rankings
    if ranking_type is not None:
        d = d[d["ranking_type"] == ranking_type]
    d = d[d["rank"] <= top_n]
    if d.empty:
        return pd.DataFrame(columns=cols)
    rows: list[dict] = []
    prev: set[str] = set()
    first = True
    for snap, g in d.groupby("snap_ms", sort=True):
        cur = set(g["symbol"])
        if not first:
            ranks = dict(zip(g["symbol"], g["rank"]))
            for s in cur - prev:
                rows.append({"snap_ms": int(snap), "symbol": s, "event": "enter",
                             "rank": float(ranks.get(s, _NAN))})
            for s in prev - cur:
                rows.append({"snap_ms": int(snap), "symbol": s, "event": "exit",
                             "rank": _NAN})
        prev, first = cur, False
    return pd.DataFrame(rows, columns=cols)


def rotation_handoffs(changes: pd.DataFrame, *, within_s: int = 120) -> pd.DataFrame:
    """이탈(A)과 진입(B)이 `within_s` 안에 짝지어진 **핸드오프**.

    논제("유동성이 A 에서 B 로 옮겨간다")의 가장 직접적인 관측 단위다.
    """
    cols = ["exit_symbol", "enter_symbol", "exit_ms", "enter_ms", "lag_s"]
    if changes is None or len(changes) == 0:
        return pd.DataFrame(columns=cols)
    ex = changes[changes["event"] == "exit"].sort_values("snap_ms")
    en = changes[changes["event"] == "enter"].sort_values("snap_ms")
    if ex.empty or en.empty:
        return pd.DataFrame(columns=cols)
    rows = []
    en_ms = en["snap_ms"].to_numpy()
    en_sym = en["symbol"].tolist()
    for _i, r in ex.iterrows():
        t = int(r["snap_ms"])
        lo, hi = np.searchsorted(en_ms, t), np.searchsorted(en_ms, t + within_s * 1000)
        for j in range(lo, hi):
            if en_sym[j] == r["symbol"]:
                continue                       # 같은 종목의 재진입은 회전이 아니다
            rows.append({"exit_symbol": r["symbol"], "enter_symbol": en_sym[j],
                         "exit_ms": t, "enter_ms": int(en_ms[j]),
                         "lag_s": (int(en_ms[j]) - t) / 1000.0})
    return pd.DataFrame(rows, columns=cols)


def _relative_spread_series(d: pd.DataFrame) -> pd.Series:
    """호가 프레임 → 상대 스프레드 시계열. **크로스 호가(ask < bid)는 `NaN`** (감사 4차 B11).

    `execution.relative_spread` 와 **같은 조건**을 쓴다 — 한 리포에서 한 모듈은 막고
    다른 모듈은 음수를 중앙값에 섞는 상태가 결함이었다. 락 호가(ask == bid)는 스프레드
    0 으로 유효하다.
    """
    from tossmon.analysis.execution import relative_spread
    return pd.Series(
        [relative_spread(b, a) for b, a in zip(d["bid1_u"], d["ask1_u"])],
        index=d.index, dtype="float64")


def crossed_book_count(d: pd.DataFrame) -> int:
    """크로스 호가 스냅 수 — 버리는 것을 **세어서** 보고하기 위한 것(금지 규칙 3)."""
    if d is None or len(d) == 0:
        return 0
    n = 0
    for b, a in zip(d["bid1_u"], d["ask1_u"]):
        # NaN 은 파이썬에서 truthy 라 `if b and a` 로는 걸러지지 않는다 (실데이터에서 발현).
        try:
            bi, ai = int(b), int(a)
        except (TypeError, ValueError):
            continue
        if bi > 0 and ai > 0 and ai < bi:
            n += 1
    return n


def spread_compression_event_time(orderbook: pd.DataFrame, symbol: str, t_ms: int, *,
                                  recent_snaps: int = 5,
                                  baseline_snaps: int = 20) -> float:
    """호가 스프레드 압축을 **스냅 개수**(사건 시간) 블록으로 잰다 — 금지 규칙 2.

    실측(2026-07-31): 호가 스냅은 심볼당 중앙값 65건·구간 폭 20분뿐이라 **시계 기준
    30분×2 창은 원리상 채워지지 않는다**(파일럿에서 143건 중 0건 산출). 승격된 동안에만
    수집되기 때문이다. 그래서 clock-time 이 아니라 **직전 N개 스냅**으로 블록을 잡는다.

    기준 블록 상대스프레드 중앙값 / 최근 블록 중앙값. >1 이면 압축(유동성 유입).
    `t_ms` 는 배타적. 스냅이 모자라면 `NaN`.
    """
    if orderbook is None or len(orderbook) == 0:
        return _NAN
    d = orderbook[(orderbook["symbol"] == symbol)
                  & (orderbook["snap_ms"] < t_ms)].sort_values("snap_ms")
    if len(d) < recent_snaps + baseline_snaps:
        return _NAN
    rel = _relative_spread_series(d)          # B11: 크로스 호가는 NaN (execution 과 동일)
    n = len(rel)
    rec = rel.iloc[n - recent_snaps:].dropna()
    base = rel.iloc[n - recent_snaps - baseline_snaps:n - recent_snaps].dropna()
    if rec.empty or base.empty:
        return _NAN
    m_rec = float(np.median(rec))
    if not (m_rec > 0):
        return _NAN
    return float(np.median(base)) / m_rec


def spread_compression(orderbook: pd.DataFrame, symbol: str, t_ms: int, *,
                       window_min: int = 30, min_snaps: int = 5) -> float:
    """호가 1레벨 **상대 스프레드**가 좁아지는 중인가 — 기준 중앙값 / 최근 중앙값.

    >1 이면 압축(유동성 유입). 스냅이 `min_snaps` 미만인 창은 `NaN`.
    백필에는 호가가 없으므로 **실시간 수집분 전용**이다.
    """
    if orderbook is None or len(orderbook) == 0:
        return _NAN
    w = window_min * 60_000
    d = orderbook[(orderbook["symbol"] == symbol)
                  & (orderbook["snap_ms"] >= t_ms - 2 * w)
                  & (orderbook["snap_ms"] < t_ms)]
    if len(d) < 2 * min_snaps:
        return _NAN
    rel = _relative_spread_series(d)          # B11: 크로스 호가는 NaN (execution 과 동일)
    rec = rel[d["snap_ms"] >= t_ms - w].dropna()
    base = rel[d["snap_ms"] < t_ms - w].dropna()
    if len(rec) < min_snaps or len(base) < min_snaps:
        return _NAN
    m_rec = float(np.median(rec))
    if not (m_rec > 0):
        return _NAN
    return float(np.median(base)) / m_rec


# --------------------------------------------------------------------------- #
# 검증 도구 — 밀도 무상관 / 창 의존성
# --------------------------------------------------------------------------- #
def _spearman(a: pd.Series, b: pd.Series) -> float:
    """순위 상관. `scipy` 없이 (순위 변환 후 피어슨) 으로 계산한다."""
    if len(a) < 2 or a.nunique() < 2 or b.nunique() < 2:
        return _NAN          # 금지 규칙 3 — 정의 불가를 "통과에 유리한 0" 으로 채우지 않는다
    ra = a.rank(method="average").to_numpy(dtype="float64", copy=True)
    rb = b.rank(method="average").to_numpy(dtype="float64", copy=True)
    ra = ra - ra.mean()
    rb = rb - rb.mean()
    denom = math.sqrt(float((ra * ra).sum()) * float((rb * rb).sum()))
    if not (denom > 0):
        return _NAN
    return float((ra * rb).sum() / denom)


def density_independence(detected, fill, *, n_quantiles: int = 4) -> dict:
    """검출 여부가 데이터 밀도와 무상관인지 검사 — **폐기됨(감사 4차 A6)**.

    이 검정은 완전한 무상관을 탈락시키고 U자 의존을 통과시킨다. 보존은 docs/17 v1
    수치의 재현을 위해서일 뿐이며, **새 판정에는 `density_independence_v2` 를 쓴다.**

    구 측정식은 체결률 분위별 검출률이 0.876→0.280 으로 단조 감소했다. 새 측정식은
    그런 단조 패턴이 없어야 하며, 없으면 `passed=True`.

    합격 기준(얼림): (a) Spearman |rho| < 0.20 **그리고** (b) 분위 검출률이 단조
    (증가 또는 감소) 배열이 아닐 것 — 둘 다 충족해야 통과.
    """
    d = pd.Series(list(detected)).astype(float)
    f = pd.Series(list(fill)).astype(float)
    ok = d.notna() & f.notna()
    d, f = d[ok], f[ok]
    if len(d) < n_quantiles * 2:
        return {"n": int(len(d)), "passed": False, "reason": "sample too small",
                "quantile_rates": [], "spearman_rho": _NAN}
    rho = _spearman(f, d)
    q = pd.qcut(f, n_quantiles, duplicates="drop")
    grp = d.groupby(q, observed=True)
    rates = [float(x) for x in grp.mean().tolist()]
    counts = [int(x) for x in grp.size().tolist()]
    mono_up = all(rates[i] <= rates[i + 1] for i in range(len(rates) - 1))
    mono_dn = all(rates[i] >= rates[i + 1] for i in range(len(rates) - 1))
    monotone = bool(mono_up or mono_dn)
    passed = bool(abs(rho) < 0.20 and not monotone)
    return {"n": int(len(d)), "quantile_rates": rates, "quantile_counts": counts,
            "spearman_rho": rho, "monotone": monotone, "passed": passed,
            "reason": ("ok" if passed else
                       ("monotone in density" if monotone else "|rho| >= 0.20"))}


def density_independence_v2(detected, fill, *, n_bins: int = 5, n_perm: int = 2000,
                            seed: int = 20260730, max_rate_ratio: float = 2.0,
                            alpha: float = 0.05) -> dict:
    """밀도 무상관 검정 **재설계** (감사 4차 A6).

    구 `density_independence` 는 네 가지로 깨져 있었다:
    (a) 완전한 무상관(전 분위 동일)이 `monotone=True` 로 **탈락**했고,
    (b) 검출률이 9배 출렁이는 U자 의존이 ρ=0 이라 **통과**했으며,
    (c) "비단조" 조건은 4분위에서 우연히 91.7% 가 통과해 **검정력이 없고**,
    (d) 코호트 필터가 희소 표본을 미리 잘라 ρ 를 0 쪽으로 끌어당겼다(범위 제한).

    새 설계는 **순열 검정**이다. 통계량은 분위별 검출률의 **분산**(단조·U자·아무 형태의
    의존에 모두 반응한다). 귀무가설은 "검출 여부가 밀도와 독립"이고, `fill` 라벨을
    섞어 통계량의 귀무분포를 만든다.

    - 완전 무상관 → 통계량이 귀무분포 한가운데 → **통과**(구 검정의 (a) 해소).
    - U자 의존 → 분산이 크다 → **탈락**((b) 해소).
    - 검정력은 순열 분포가 직접 준다((c) 해소).
    - (d) 범위 제한은 검정으로 못 고친다 — `fill` 의 실제 범위를 함께 보고해
      **호출부가 판단**하도록 한다(`fill_range`, `fill_iqr`).

    합격: 순열 p > `alpha` **그리고** 분위 검출률의 max/min 비 ≤ `max_rate_ratio`.
    두 번째 조건은 표본이 작아 p 가 커지는 경우에 대한 안전장치다.
    """
    d = pd.Series(list(detected)).astype(float)
    f = pd.Series(list(fill)).astype(float)
    ok = d.notna() & f.notna()
    d, f = d[ok].reset_index(drop=True), f[ok].reset_index(drop=True)
    if len(d) < n_bins * 4 or d.nunique() < 2:
        return {"n": int(len(d)), "passed": False, "reason": "sample too small",
                "p_value": _NAN, "rate_ratio": _NAN, "bin_rates": [],
                "spearman_rho": _NAN}
    bins = pd.qcut(f, n_bins, duplicates="drop", labels=False)
    n_eff = int(pd.Series(bins).nunique())
    if n_eff < 2:
        return {"n": int(len(d)), "passed": False, "reason": "fill has no spread",
                "p_value": _NAN, "rate_ratio": _NAN, "bin_rates": [],
                "spearman_rho": _NAN}

    def stat(labels) -> float:
        return float(pd.Series(d.to_numpy()).groupby(labels).mean().var(ddof=0))

    observed = stat(bins)
    rng = np.random.default_rng(seed)
    arr = bins.to_numpy()
    null = np.empty(n_perm, dtype="float64")
    for i in range(n_perm):
        null[i] = stat(pd.Series(rng.permutation(arr)))
    # +1 보정: 관측을 귀무표본에 포함해 p 가 0 이 되지 않게 한다
    p = float((np.sum(null >= observed) + 1) / (n_perm + 1))
    rates = [float(x) for x in pd.Series(d.to_numpy()).groupby(arr).mean().tolist()]
    counts = [int(x) for x in pd.Series(d.to_numpy()).groupby(arr).size().tolist()]
    lo, hi = min(rates), max(rates)
    ratio = (hi / lo) if lo > 0 else float("inf")
    passed = bool(p > alpha and ratio <= max_rate_ratio)
    return {"n": int(len(d)), "n_bins": n_eff, "bin_rates": rates,
            "bin_counts": counts, "observed_stat": observed, "p_value": p,
            "rate_ratio": ratio, "spearman_rho": _spearman(f, d),
            "fill_range": [float(f.min()), float(f.max())],
            "fill_iqr": [float(f.quantile(0.25)), float(f.quantile(0.75))],
            "passed": passed,
            "reason": ("ok" if passed else
                       (f"permutation p={p:.4f} <= {alpha}" if p <= alpha
                        else f"rate ratio {ratio:.2f} > {max_rate_ratio}"))}


def window_edge_mass(leads, scan_min: int, *, edge_frac: float = 0.10) -> float:
    """리드타임이 **측정 창 가장자리**에 몰린 비율. 크면 창이 신호보다 짧다는 뜻이다.

    구 측정식은 이 값이 0.51 이었다(60분 창에서 55~60분에 51%).
    """
    s = pd.Series(list(leads)).astype(float).dropna()
    if s.empty:
        return _NAN
    edge = scan_min * (1.0 - edge_frac)
    return float((s >= edge).mean())

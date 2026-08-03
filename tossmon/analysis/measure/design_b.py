"""설계 B 정직 측정 러너 (docs/23 §10 전면 개정, 감사 5차 C-1~C-4·H-1 대응).

## 왜 다시 짰나

구 §10 은 "과매도 진입 → **슈팅 고점** 매도"를 쟀다. 그런데 슈팅은 **"저점 대비
`min_rise` 이상 올랐다"로 정의**되므로, 이탈 목표를 그 슈팅의 고점으로 잡으면
"진입 → 슈팅 고점"은 **정의상 `min_rise` 쯤**이 나온다. 감사 5차가 임계를 1/2/3/5% 로
쓸어 답이 +1.71/+2.53/+3.24/+5.18% 로 **1:1 로 따라오는 것**을 보였다. 시장이 아니라
우리 임계를 측정하고 있었던 것이다.

## 이 러너의 규칙 (전부 강제)

1. **이탈이 슈팅 정의를 참조하지 않는다.** 고정 시계·다운틱·추적 손절·지정가 목표·
   지평 보유만 쓴다. `shots.detect_shots` 를 **호출하지 않는다.**
2. **모든 진입을 계상한다.** "슈팅이 도래한 건"만 고르면 다시 임계의 함수가 된다.
   손실·미도달 전부 포함한다.
3. **위약을 상시 병기한다.** 진입 시각은 고정하고 종목만 무작위 치환한 대조군을
   씨앗 여러 개로 돌려 **(실제 − 위약)** 을 주 지표로 삼는다.
4. **날 군집을 정면으로 다룬다.** 날짜별로 따로 보고하고, 유효 거래일이
   `MIN_DAY_CLUSTERS` 미만이면 **통합 CI 를 내지 않는다.**
5. **비용을 항상 차감한다.** 헤드라인은 순수익이다(docs/21 클립별 실측).
6. **진입 체결 지연을 적용한다**(감사 M-2 — 설계 A 에만 걸던 잣대를 대칭으로).

## 성공 기준

이탈 규칙을 고정한 채 `min_rise` 를 1/2/3/5% 로 쓸어도 **평균 수익이 움직이지 않아야
한다.** 이 러너는 이탈이 `shots` 를 전혀 호출하지 않으므로 **구조적으로 독립**이며 —
비슷한 정도가 아니라 값이 완전히 동일하다 — `min_rise_independence()` 가 그 사실을
소스 수준에서 확인한다. 테스트도 `detect_shots`·`peak_u` 가 실행 코드에 없음을 강제한다.

실행: `python -m tossmon.analysis.measure.design_b [db_path]`
라이브 0콜. DB 는 **읽기 전용**. 콘솔 ASCII.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from tossmon.analysis import execution as X
from tossmon.analysis import session as SS
from tossmon.analysis import shots as S
from tossmon.analysis.rules import bootstrap_ci_mean

#: docs/21 클립별 실측 왕복 비용. 헤드라인은 이걸 뺀 순수익이다.
CLIP_COSTS = {100: 0.0238, 500: 0.0445, 1000: 0.0491, 2000: 0.0644}

#: 진입 규칙 (슈팅 정의와 무관) — 최근 `lookback_s` 고점 대비 `drop` 하락 후 반등 확인.
ENTRY_DROP = 0.05
ENTRY_LOOKBACK_S = 600
#: 감사 M-2 — 설계 A 와 같은 잣대. 신호봉에 즉시 체결된다고 보지 않는다.
ENTRY_DELAY_S = 13

#: 날 군집이 이보다 적으면 통합 CI 를 내지 않는다 (감사 C-3).
MIN_DAY_CLUSTERS = 5

#: 위약 씨앗 (종목 치환). 5개 이상.
PLACEBO_SEEDS = (20260730, 20260731, 20260801, 20260802, 20260803)

TOSS_TYPES = (S.TOSS_VOLUME, S.TOSS_AMOUNT)

#: 산출물은 소스 옆이 아니라 저장소 루트의 `out/` 에 쓴다(`.gitignore` 대상).
#: 소스 옆에 두면 커밋에 섞여 다음 사람의 리베이스를 막는다 — 실제로 막았다.
OUT_DIR = Path(__file__).resolve().parents[3] / "out"

#: **docs/23 가 싣는 수치의 목록.** 러너 출력(JSON)은 이 필드를 전부 포함해야 하며
#: 테스트가 그것을 강제한다. 문서에 표를 추가하면 **여기에 먼저 추가**해야 하고,
#: 그러면 배선을 잊을 수 없다 — 감사 5차 H-1(문서 수치를 재실행할 방법이 없음)의
#: 재발 방지 장치다. 1차에서 닫았다가 2차에서 다시 열렸으므로 이번엔 코드로 막는다.
#: 러너가 규칙별로 내보내는 **필드 전체**. 가드는 이것을 **부분집합이 아니라 동일
#: 집합**으로 대조한다 — 필드를 추가하고 여기 적기를 잊으면 테스트가 깨진다.
#: (허용 목록이면 "잊은 것"은 검사되지 않는다. 감사 지적 참조.)
REPORTED_FIELDS = (
    "rule",              # 공통
    "n", "fill_rate",                                    # §10-N.9 좌측 열
    "gross_mean", "gross_ci", "net_by_scenario",        # §10-N.9
    "pair_n", "diff_mean", "diff_ci", "diff_ci_bonferroni", "diff_verdict",  # §10-N.8
    "days_needed",                                       # §10-N.10
)


# --------------------------------------------------------------------------- #
# 이탈 규칙 — 전부 관측 가능하고, 슈팅 정의를 참조하지 않는다
# --------------------------------------------------------------------------- #
def exit_after_seconds(series: pd.Series, entry_ms: int, seconds: int) -> dict:
    """`entry_ms + seconds` **이하**의 마지막 관측가에 청산."""
    w = series[(series.index >= entry_ms)
               & (series.index <= entry_ms + seconds * 1000)]
    if w.empty:
        return {"exit_u": float("nan"), "exit_ms": entry_ms, "filled": False}
    return {"exit_u": float(w.iloc[-1]), "exit_ms": int(w.index[-1]), "filled": True}


def exit_on_downticks(series: pd.Series, entry_ms: int, *, n: int = 1,
                      horizon_s: int = 600) -> dict:
    """직전 관측가보다 낮은 봉이 `n` 번 **연속**되면 청산. 없으면 지평 마지막 관측가."""
    w = series[(series.index >= entry_ms)
               & (series.index <= entry_ms + horizon_s * 1000)]
    if w.empty:
        return {"exit_u": float("nan"), "exit_ms": entry_ms, "filled": False}
    prev = float(w.iloc[0])
    run = 0
    for ts, px in zip(w.index.tolist()[1:], w.to_numpy()[1:]):
        px = float(px)
        run = run + 1 if px < prev else 0
        prev = px
        if run >= n:
            return {"exit_u": px, "exit_ms": int(ts), "filled": True}
    return {"exit_u": float(w.iloc[-1]), "exit_ms": int(w.index[-1]), "filled": False}


def exit_trailing(series: pd.Series, entry_ms: int, *, trail: float,
                  horizon_s: int = 600) -> dict:
    """진행형 추적 손절 — 지금까지 **관측된** 고점 대비 `trail` 하락 시 청산.

    고점은 그 봉의 판정을 마친 **뒤에** 갱신한다(`rules.simulate_exit` 과 같은 보수 규약).
    사후 고점을 쓰지 않으므로 접두사 불변이다.
    """
    w = series[(series.index >= entry_ms)
               & (series.index <= entry_ms + horizon_s * 1000)]
    if w.empty:
        return {"exit_u": float("nan"), "exit_ms": entry_ms, "filled": False}
    peak = float(w.iloc[0])
    for ts, px in zip(w.index.tolist()[1:], w.to_numpy()[1:]):
        px = float(px)
        if px <= peak * (1.0 - trail):
            return {"exit_u": px, "exit_ms": int(ts), "filled": True}
        peak = max(peak, px)
    return {"exit_u": float(w.iloc[-1]), "exit_ms": int(w.index[-1]), "filled": False}


def exit_target(series: pd.Series, entry_ms: int, entry_u: float, *, target: float,
                horizon_s: int = 600) -> dict:
    """+`target` 지정가. 도달하면 **지정가에** 체결, 아니면 지평 마지막 관측가.

    목표가는 임계가 아니라 **우리가 고르는 주문**이므로 정당하다. 다만 체결률
    (`filled`)을 반드시 함께 보고한다 — 미체결이 많으면 평균이 낙관된다.
    """
    lim = entry_u * (1.0 + target)
    w = series[(series.index >= entry_ms)
               & (series.index <= entry_ms + horizon_s * 1000)]
    if w.empty:
        return {"exit_u": float("nan"), "exit_ms": entry_ms, "filled": False}
    hit = w[w >= lim]
    if len(hit):
        return {"exit_u": float(lim), "exit_ms": int(hit.index[0]), "filled": True}
    return {"exit_u": float(w.iloc[-1]), "exit_ms": int(w.index[-1]), "filled": False}


def exit_rules(horizon_s: int = 600) -> dict:
    """이탈 규칙 표. **어느 것도 `shots` 를 참조하지 않는다.**"""
    return {
        "time_30s": lambda s, e, u: exit_after_seconds(s, e, 30),
        "time_60s": lambda s, e, u: exit_after_seconds(s, e, 60),
        "time_120s": lambda s, e, u: exit_after_seconds(s, e, 120),
        "time_300s": lambda s, e, u: exit_after_seconds(s, e, 300),
        "downtick_1": lambda s, e, u: exit_on_downticks(s, e, n=1,
                                                        horizon_s=horizon_s),
        "downtick_2": lambda s, e, u: exit_on_downticks(s, e, n=2,
                                                        horizon_s=horizon_s),
        "trail_0.5pct": lambda s, e, u: exit_trailing(s, e, trail=0.005,
                                                      horizon_s=horizon_s),
        "trail_1.0pct": lambda s, e, u: exit_trailing(s, e, trail=0.010,
                                                      horizon_s=horizon_s),
        "target_1pct": lambda s, e, u: exit_target(s, e, u, target=0.01,
                                                   horizon_s=horizon_s),
        "target_2pct": lambda s, e, u: exit_target(s, e, u, target=0.02,
                                                   horizon_s=horizon_s),
        "target_3pct": lambda s, e, u: exit_target(s, e, u, target=0.03,
                                                   horizon_s=horizon_s),
        "hold_horizon": lambda s, e, u: exit_after_seconds(s, e, horizon_s),
    }


# --------------------------------------------------------------------------- #
# 진입 수집 + 평가
# --------------------------------------------------------------------------- #
def collect_entries(day_rank: pd.DataFrame, *, min_snaps: int = 20,
                    drop: float = ENTRY_DROP,
                    lookback_s: int = ENTRY_LOOKBACK_S) -> list[dict]:
    """그날 전 종목의 과매도 진입 신호. **슈팅 정의를 쓰지 않는다.**"""
    out = []
    for sym in day_rank["symbol"].unique():
        ser = S.price_series_multi(day_rank, sym)
        if len(ser) < min_snaps:
            continue
        px, ms = S.find_oversold_entry(ser, drop=drop, lookback_s=lookback_s)
        if px == px:
            out.append({"symbol": sym, "signal_ms": int(ms), "signal_u": float(px),
                    "entry_idx": len(out)})
    return out


def tag_entries(entries: list[dict], day: str) -> list[dict]:
    """진입 키에 **날짜 접두사**를 박아 전역 고유로 만든다.

    `collect_entries` 는 날마다 0 부터 센다. 여러 날을 이어 붙인 뒤 `entry_idx` 로
    쌍체를 맺으면 **다른 날 진입끼리 짝이 맺힌다** — 라벨이 중복되면 pandas 가
    조용히 브로드캐스트해서 오류도 나지 않고 짝 수만 늘어난다. 실제로 그렇게 됐고,
    통제군 짝 수가 정합 성공 건수보다 많아진 것으로 발각됐다.
    """
    return [dict(e, entry_idx=f"{day}#{int(e.get('entry_idx', i))}")
            for i, e in enumerate(entries)]


def evaluate(series_by_symbol: dict, entries: list[dict], *,
             entry_delay_s: int = ENTRY_DELAY_S, horizon_s: int = 600) -> pd.DataFrame:
    """모든 진입을 모든 이탈 규칙으로 평가한다. **미도달·손실 전부 계상.**"""
    rules = exit_rules(horizon_s)
    rows = []
    for e in entries:
        ser = series_by_symbol.get(e["symbol"])
        if ser is None or ser.empty:
            continue
        fill_ms = int(e["signal_ms"]) + entry_delay_s * 1000
        entry_u = S.price_at(ser, fill_ms)
        if not (entry_u == entry_u and entry_u > 0):
            continue                      # 체결가 없음 -> 거래 성립 안 함
        rec = {"symbol": e["symbol"], "entry_ms": fill_ms, "entry_u": entry_u,
               # 쌍체 비교의 키. 실제와 위약이 **같은 진입 시각**을 공유하므로
               # 이 인덱스로 짝지어야 한다(씨앗을 독립 표본으로 세면 안 된다).
               # 쌍체 키. **날짜 접두사가 붙은 전역 고유 키**여야 한다 —
               # 날마다 0 부터 다시 세면 다른 날 진입끼리 짝이 맺힌다.
               "entry_idx": e.get("entry_idx", -1),
               # **세션은 필터가 아니라 차원이다.** 경계에 걸치면 진입 시각 기준.
               "session": SS.session_of(fill_ms)}
        for name, fn in rules.items():
            r = fn(ser, fill_ms, entry_u)
            rec[name] = (float(r["exit_u"] / entry_u - 1.0)
                         if r["exit_u"] == r["exit_u"] else float("nan"))
            rec[f"{name}__filled"] = bool(r["filled"])
        rows.append(rec)
    return pd.DataFrame(rows)


def placebo_entries(entries: list[dict], symbols: list[str], seed: int) -> list[dict]:
    """**진입 시각은 고정하고 종목만 무작위 치환** (감사 C-2 의 위약 설계).

    시각 분포·개수를 그대로 두므로, 실제 규칙이 만들어내는 우위가 있다면
    위약보다 나아야 한다. 신호 가격은 새 종목의 그 시각 가격으로 다시 잡는다.
    """
    rng = np.random.default_rng(seed)
    pick = rng.choice(np.asarray(symbols, dtype=object), size=len(entries),
                      replace=True)
    # 진입의 **원래 키를 그대로 물려준다.** 위치 번호로 다시 매기면 날짜 접두사가
    # 사라져 쌍체가 어긋난다.
    return [{"symbol": str(pick[i]), "signal_ms": e["signal_ms"],
             "signal_u": float("nan"), "entry_idx": e.get("entry_idx", i)}
            for i, e in enumerate(entries)]


# --------------------------------------------------------------------------- #
# 집계
# --------------------------------------------------------------------------- #
def summarize(df: pd.DataFrame, rule: str, *, clip: int = 100) -> dict:
    """한 규칙의 요약. **비용 차감 순수익이 헤드라인.**"""
    v = pd.to_numeric(df.get(rule), errors="coerce").dropna() if len(df) else pd.Series(dtype=float)
    cost = CLIP_COSTS[clip]
    if len(v) < 2:
        return {"n": int(len(v)), "gross_mean": float("nan"),
                "net_mean": float("nan"), "ci": (float("nan"), float("nan")),
                "fill_rate": float("nan")}
    lo, hi = bootstrap_ci_mean(v.tolist())
    fill = (df[f"{rule}__filled"].mean() if f"{rule}__filled" in df.columns
            else float("nan"))
    return {"n": int(len(v)), "gross_mean": float(v.mean()),
            "gross_median": float(v.median()),
            "net_mean": float(v.mean() - cost), "ci": (lo - cost, hi - cost),
            "fill_rate": float(fill)}


#: 비용 시나리오 3종 (감사 후속 지시).
#: (i) 시장가성 — docs/21 티어2 실측 왕복(호가창을 가로지른다).
#: (ii) 지정가 — docs/06 §7 수수료 왕복 0.2% 만. **상한 시나리오**(미체결·역선택 0 가정).
#: (iii) 절충 — 한 다리만 지정가: 수수료 + 편도 스프레드.
#:     편도 = $2~5 밴드 움직임 조건부 상대 스프레드 2.56%(docs/18 §3.1)의 절반.
COMMISSION_ROUND_TRIP = 0.002
ONE_LEG_SPREAD = 0.0128
COST_SCENARIOS = {
    "market_2.38pct": 0.0238,
    "limit_only_0.2pct": COMMISSION_ROUND_TRIP,
    "hybrid_one_leg": COMMISSION_ROUND_TRIP + ONE_LEG_SPREAD,
}


def paired_difference(real: pd.DataFrame, placebo: pd.DataFrame,
                      rule: str) -> pd.Series:
    """진입 단위 **쌍체** 차이 (실제 − 위약평균).

    위약은 씨앗마다 다른 종목을 뽑으므로, 먼저 **진입별로 씨앗 평균**을 낸 뒤
    실제와 짝짓는다. 씨앗을 독립 표본으로 세면 안 된다 — 씨앗은 5개뿐이고 **같은
    진입 시각 집합을 공유**하므로 유효 자유도를 부풀린다.
    """
    if real is None or len(real) == 0 or placebo is None or len(placebo) == 0:
        return pd.Series(dtype="float64")
    if rule not in real.columns or rule not in placebo.columns:
        return pd.Series(dtype="float64")
    r = real.set_index("entry_idx")[rule]
    p = placebo.groupby("entry_idx")[rule].mean()      # 씨앗 평균 먼저
    common = r.index.intersection(p.index)
    return (r.loc[common] - p.loc[common]).dropna()


def difference_ci(real: pd.DataFrame, placebo: pd.DataFrame, rule: str, *,
                  n_rules: int = 12) -> dict:
    """쌍체 차이의 부트스트랩 CI. **본페로니 보정본을 병기**한다(12규칙 다중검정).

    `verdict` 는 세 갈래로만 말한다: `above_zero` / `crosses_zero` / `below_zero`.
    """
    d = paired_difference(real, placebo, rule)
    if len(d) < 2:
        return {"n": int(len(d)), "mean": float("nan"),
                "ci": (float("nan"), float("nan")),
                "ci_bonferroni": (float("nan"), float("nan")),
                "verdict": "insufficient"}
    lo, hi = bootstrap_ci_mean(d.tolist())
    blo, bhi = bootstrap_ci_mean(d.tolist(), alpha=0.05 / max(1, n_rules))
    verdict = ("above_zero" if lo > 0 else
               "below_zero" if hi < 0 else "crosses_zero")
    return {"n": int(len(d)), "mean": float(d.mean()), "sd": float(d.std(ddof=1)),
            "ci": (lo, hi), "ci_bonferroni": (blo, bhi), "verdict": verdict}


def days_needed_for_difference(diff_mean: float, diff_sd: float,
                               entries_per_day: float, *,
                               z: float = 1.96) -> dict:
    """차이 CI 가 0 을 배제하려면 거래일이 몇 개 필요한가 (현재 효과크기·분산 기준).

    `mean > z*sd/sqrt(n)` 을 풀어 `n > (z*sd/mean)^2`. 군집 하한
    `MIN_DAY_CLUSTERS` 도 함께 건다. 효과가 0 이하면 **도달 불가**를 그대로 돌려준다.
    """
    if not (diff_sd > 0 and entries_per_day > 0):
        return {"reachable": False, "reason": "invalid inputs"}
    if not (diff_mean > 0):
        return {"reachable": False, "reason": "point estimate is not positive",
                "diff_mean": diff_mean}
    n = (z * diff_sd / diff_mean) ** 2
    days = max(float(MIN_DAY_CLUSTERS), n / entries_per_day)
    return {"reachable": True, "n_entries_needed": int(np.ceil(n)),
            "entries_per_day": entries_per_day,
            "days_needed": int(np.ceil(days)),
            "floor_applied": bool(n / entries_per_day < MIN_DAY_CLUSTERS)}


# --------------------------------------------------------------------------- #
# 세션별 비용 모델 (사용자 지적 2026-08-03) — 단일 2.38% 를 대체한다
# --------------------------------------------------------------------------- #
SESSION_CLIPS = (100, 500, 1000, 2000)

#: 호가 단계가 이 비율 미만이면 **클립 비용을 낼 수 없다** — 최우선 호가만으로는
#: 큰 클립이 어디까지 먹고 들어가는지 알 수 없기 때문이다.
MULTI_LEVEL_MIN_RATE = 0.5


def book_rows(conn) -> pd.DataFrame:
    """호가 스냅을 **한 번만** 파싱해 세션·대역·비용을 붙인다."""
    # 티어2 호가가 없는 DB 도 정상 입력이다 — 비용 모델이 **비어 나올 뿐** 터지지 않는다.
    try:
        ob = pd.read_sql_query(
            "SELECT symbol, snap_ms, bid1_u, bid1_qu, ask1_u, ask1_qu, depth_json "
            "FROM orderbook_snap", conn)
    except Exception:
        return pd.DataFrame()
    rows = []
    for r in ob.itertuples(index=False):
        bids, asks = X.parse_depth(r.depth_json)
        mid = X.mid_u(r.bid1_u, r.ask1_u)
        rec = {"symbol": r.symbol, "snap_ms": int(r.snap_ms),
               "session": SS.session_of(int(r.snap_ms)),
               "n_bid_lv": len(bids), "n_ask_lv": len(asks),
               "rel_spread": X.relative_spread(r.bid1_u, r.ask1_u),
               "mid_usd": (mid / X.MICRO if mid == mid else float("nan")),
               "tob_min_usd": X.top_of_book_usd(r.bid1_u, r.bid1_qu,
                                                r.ask1_u, r.ask1_qu)["min_usd"]}
        rec["band"] = X.price_band(rec["mid_usd"])
        for clip in SESSION_CLIPS:
            rt = (X.round_trip_cost(bids, asks, clip, exit_mode="cross")
                  if bids and asks else {"total": float("nan"),
                                         "entry_exhausted": True})
            rec[f"rt_{clip}"] = rt["total"]
            # **호가를 다 먹고도 못 채운 건 비용을 지어내지 않는다** — 따로 센다.
            rec[f"exhausted_{clip}"] = bool(rt["entry_exhausted"])
        rows.append(rec)
    return pd.DataFrame(rows)


def depth_completeness(books: pd.DataFrame) -> dict:
    """**자료 완전성 먼저.** 세션마다 호가 단계가 몇 개나 저장돼 있나.

    실측 결과 **다단계 호가는 주간(`day`) 세션에서만 저장된다** — 나머지 세션은
    최우선 호가 1단뿐이다. 이건 유동성이 아니라 **수집 형태**의 문제일 수 있으므로,
    세션 간 클립 비용을 비교하기 전에 **반드시 먼저 읽어야 하는 표**다.
    """
    if books is None or books.empty:
        return {}
    g = books.groupby("session")
    return {s: {"rows": int(len(d)),
                "median_ask_levels": float(d["n_ask_lv"].median()),
                "multi_level_rate": float((d["n_ask_lv"] > 1).mean())}
            for s, d in g}


def session_clip_costs(books: pd.DataFrame, *, band: str | None = None) -> dict:
    """세션 x 클립 왕복 비용(중앙값). **낼 수 없는 칸은 `measurable=False`.**

    `$100` 은 최우선 호가만으로도 대개 채워지므로 **모든 세션에서 측정 가능**하다
    (채우지 못한 스냅은 제외하고 그 비율을 함께 낸다). 그보다 큰 클립은 다단계
    호가가 있어야 하므로 **`day` 외에는 낼 수 없다** — 지어내지 않고 그렇게 적는다.
    """
    if books is None or books.empty:
        return {}
    d0 = books if band is None else books[books["band"] == band]
    out = {}
    for s, d in d0.groupby("session"):
        multi = float((d["n_ask_lv"] > 1).mean())
        per_clip = {}
        for clip in SESSION_CLIPS:
            ok = d[~d[f"exhausted_{clip}"]]
            v = pd.to_numeric(ok[f"rt_{clip}"], errors="coerce").dropna()
            # 최우선 호가만 있는 세션에서 $100 초과 클립은 **측정 불가**다.
            measurable = bool(len(v) >= 30 and
                              (clip == min(SESSION_CLIPS)
                               or multi >= MULTI_LEVEL_MIN_RATE))
            per_clip[clip] = {
                "measurable": measurable,
                "median": float(v.median()) if measurable else float("nan"),
                "n": int(len(v)),
                "excluded_exhausted_rate": float(d[f"exhausted_{clip}"].mean()),
                "reason": ("" if measurable else
                           "top-of-book only - clip depth unknown"
                           if multi < MULTI_LEVEL_MIN_RATE else "n < 30"),
            }
        out[s] = {"n_snaps": int(len(d)), "n_symbols": int(d["symbol"].nunique()),
                  "multi_level_rate": multi,
                  "median_rel_spread": float(pd.to_numeric(
                      d["rel_spread"], errors="coerce").dropna().median()),
                  "median_tob_usd": float(pd.to_numeric(
                      d["tob_min_usd"], errors="coerce").dropna().median()),
                  "by_clip": per_clip}
    return out


def within_symbol_cost_contrast(books: pd.DataFrame, *,
                                reference: str = "regular") -> dict:
    """**종목 내 대조** — 세션마다 종목 구성이 다르므로 이것이 정본이다.

    티어2 승격이 동적이라 세션별 단순 중앙값은 **종목 구성 차이**를 비용 차이로
    오독하게 만든다. 그래서 `reference` 세션과 **같은 종목**을 가진 짝만 남겨
    종목별 차이의 중앙값을 낸다.
    """
    if books is None or books.empty:
        return {}
    piv = (books.groupby(["symbol", "session"])["rel_spread"]
           .median().unstack())
    if reference not in getattr(piv, "columns", []):
        return {"reference": reference, "available": False,
                "reason": f"no {reference} snapshots"}
    out = {"reference": reference, "available": True, "pairs": {}}
    for s in piv.columns:
        if s == reference:
            continue
        both = piv.dropna(subset=[reference, s])
        out["pairs"][s] = {
            "n_symbols": int(len(both)),
            "median_reference": (float(both[reference].median())
                                 if len(both) else float("nan")),
            "median_other": float(both[s].median()) if len(both) else float("nan"),
            "median_within_symbol_delta": (float((both[s] - both[reference]).median())
                                           if len(both) else float("nan")),
            "powered": bool(len(both) >= 10),
        }
    return out


def session_cost_model(conn, *, band: str = "$2-5") -> dict:
    """세션별 비용 모델 전체. `CLIP_COSTS` 의 단일 값을 대체하는 자리다."""
    books = book_rows(conn)
    return {"band": band,
            "depth_completeness": depth_completeness(books),
            "all_bands": session_clip_costs(books),
            "in_band": session_clip_costs(books, band=band),
            "within_symbol": within_symbol_cost_contrast(books),
            "note": ("multi-level depth is stored only in the day session, so "
                     "clip costs above $100 are not computable elsewhere")}


def session_round_trip(cost_model: dict, session: str, *, clip: int = 100) -> float:
    """그 세션의 왕복 비용. 낼 수 없으면 **NaN** 이고, 단일 값으로 대체하지 않는다."""
    row = (cost_model.get("all_bands", {}).get(session, {})
           .get("by_clip", {}).get(clip))
    if not row or not row.get("measurable"):
        return float("nan")
    return float(row["median"])


def load_days(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT date(snap_ms/1000,'unixepoch') FROM rankings_snap ORDER BY 1")]


def load_day_rank(conn, day: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT snap_ms, ranking_type, symbol, last_u FROM rankings_snap "
        "WHERE date(snap_ms/1000,'unixepoch')=? AND ranking_type IN (?,?)",
        conn, params=(day, *TOSS_TYPES))


def ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)


def run(db: Path, *, entry_delay_s: int = ENTRY_DELAY_S,
        horizon_s: int = 600) -> dict:
    """전 매매일에 대해 실제 + 위약을 산출한다."""
    conn = ro(db)
    try:
        days = load_days(conn)
        real_by_day, plac_by_day = {}, {}
        for day in days:
            rk = load_day_rank(conn, day)
            if rk.empty:
                continue
            series = {}
            for sym in rk["symbol"].unique():
                s = S.price_series_multi(rk, sym)
                if len(s) >= 20:
                    series[sym] = s
            ents = tag_entries(collect_entries(rk), day)
            real_by_day[day] = evaluate(series, ents, entry_delay_s=entry_delay_s,
                                        horizon_s=horizon_s)
            pl = []
            for seed in PLACEBO_SEEDS:
                pe = placebo_entries(ents, list(series.keys()), seed)
                d = evaluate(series, pe, entry_delay_s=entry_delay_s,
                             horizon_s=horizon_s)
                d["seed"] = seed
                pl.append(d)
            plac_by_day[day] = (pd.concat(pl, ignore_index=True) if pl
                                else pd.DataFrame())
        cost_model = session_cost_model(conn)
    finally:
        conn.close()
    return {"days": days, "real": real_by_day, "placebo": plac_by_day,
            "cost_model": cost_model}


def min_rise_independence(db: Path | None = None, thresholds=(0.01, 0.02, 0.03, 0.05),
                          res: dict | None = None) -> dict:
    """**성공 기준 점검** — 답이 슈팅 임계 `min_rise` 에 의존하지 않음을 보인다.

    구 §10 은 이탈 목표가 슈팅 고점이라 답이 임계를 1:1 로 따라갔다
    (감사 5차 C-2: 1/2/3/5% -> +1.71/+2.53/+3.24/+5.18%).

    이 러너는 이탈이 `shots` 를 **전혀 호출하지 않으므로** 결과가 임계와 **구조적으로**
    독립이다 — 값이 비슷한 정도가 아니라 **완전히 동일**하다. 4번 돌려 같은 수를 얻는
    것은 계산 낭비이므로, 대신 (a) 실행 코드에 슈팅 의존이 없음을 확인하고
    (b) 한 번의 실행 결과를 임계별로 그대로 보고한다. 이것이 정직한 형태다.
    """
    import ast
    code = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(code) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(code) if isinstance(n, ast.Attribute)}
    depends = bool({"detect_shots", "min_rise"} & (names | attrs))
    # 이미 계산된 실행 결과가 있으면 재사용한다. `main()` 이 이 함수를 부르는데
    # 여기서 `run` 을 다시 돌리면 전체 분석이 두 번 돌아간다.
    if res is None:
        res = run(db)
    allr = (pd.concat([d for d in res["real"].values() if len(d)], ignore_index=True)
            if res["real"] else pd.DataFrame())
    base = summarize(allr, "downtick_1")
    return {"structurally_independent": not depends,
            "shared_result_across_thresholds": {
                str(t): {"gross_mean": base["gross_mean"],
                         "net_mean": base["net_mean"]} for t in thresholds},
            "note": ("exits never reference the shot definition, so every threshold "
                     "yields the identical number by construction")}


def session_breakdown(allr: pd.DataFrame, allp: pd.DataFrame,
                      cost_model: dict) -> list[dict]:
    """**세션별로 전부 다시 낸다** — 진입 수·총수익·위약 대비 차이·그 세션의 실측 비용.

    세션은 필터가 아니라 차원이므로 0 건인 세션도 행을 남긴다. 날 군집은 세션을
    가르면 더 얇아지므로(정규장은 사이클 하나뿐) CI 는 **전부 잠정**이다.
    """
    out = []
    for sess in SS.SESSIONS:
        r = allr[allr["session"] == sess] if "session" in allr.columns else allr.iloc[0:0]
        p = (allp[allp["session"] == sess]
             if len(allp) and "session" in allp.columns else pd.DataFrame())
        cost = session_round_trip(cost_model, sess)
        row = {"session": sess, "n_entries": int(len(r)),
               "measured_round_trip": cost,
               "cycle_dates": sorted({SS.session_date(int(m))
                                      for m in r.get("entry_ms", [])}),
               "rules": []}
        for rule in exit_rules().keys():
            v = pd.to_numeric(r.get(rule), errors="coerce").dropna() if len(r) else pd.Series(dtype=float)
            lo, hi = (bootstrap_ci_mean(v.tolist()) if len(v) >= 2
                      else (_nan(), _nan()))
            diff = difference_ci(r, p, rule, n_rules=len(exit_rules()))
            mean = float(v.mean()) if len(v) else _nan()
            row["rules"].append({
                "rule": rule, "n": int(len(v)), "gross_mean": mean,
                "gross_ci": [lo, hi],
                # **그 세션에서 실제로 잰 비용**을 뺀다. 못 잰 세션은 NaN 그대로 둔다.
                "net_at_session_cost": (mean - cost
                                        if mean == mean and cost == cost else _nan()),
                "diff_mean": diff["mean"], "diff_ci": list(diff["ci"]),
                "diff_verdict": diff["verdict"], "pair_n": diff["n"]})
        out.append(row)
    return out


def build_report(res: dict) -> dict:
    """docs/23 §10-N.8·9·10 의 세 표를 **한 자료구조로** 만든다.

    `main()` 과 테스트가 같은 함수를 쓰므로, 문서에 실리는 수치는 반드시 여기를 지난다.
    """
    days = [d for d in res["days"] if len(res["real"].get(d, []))]
    if not days:
        return {"days": [], "rules": [], "n_real": 0, "pooled_ci_permitted": False}
    allr = pd.concat([res["real"][d] for d in days], ignore_index=True)
    plac = [res["placebo"][d] for d in days if len(res["placebo"].get(d, []))]
    allp = pd.concat(plac, ignore_index=True) if plac else pd.DataFrame()
    epd = len(allr) / len(days)
    rules = []
    for rule in exit_rules().keys():
        g = pd.to_numeric(allr.get(rule), errors="coerce").dropna()
        lo, hi = bootstrap_ci_mean(g.tolist()) if len(g) >= 2 else (_nan(), _nan())
        # `summarize` 가 n·총수익·체결률의 **정본 계산부**다. 여기서 쓰지 않으면
        # 테스트만 부르는 죽은 코드가 되고, 그것이 H-1 의 재발 형태였다.
        base = summarize(allr, rule)
        diff = difference_ci(allr, allp, rule, n_rules=len(exit_rules()))
        dn = days_needed_for_difference(diff.get("mean", _nan()),
                                        diff.get("sd", _nan()), epd)
        rules.append({
            "rule": rule,
            "n": base["n"],
            "fill_rate": base["fill_rate"],
            "gross_mean": base["gross_mean"],
            "gross_ci": [lo, hi],
            "net_by_scenario": {k: (float(g.mean()) - c if len(g) else _nan())
                                for k, c in COST_SCENARIOS.items()},
            "pair_n": diff["n"],
            "diff_mean": diff["mean"],
            "diff_ci": list(diff["ci"]),
            "diff_ci_bonferroni": list(diff["ci_bonferroni"]),
            "diff_verdict": diff["verdict"],
            "days_needed": dn,
        })
    cost_model = res.get("cost_model", {})
    return {"days": days, "n_real": int(len(allr)), "entries_per_day": epd,
            # 사용자 지적(2026-08-03): 세션은 1급 차원이다.
            "session_counts": SS.session_counts(allr["entry_ms"]),
            "cost_model": cost_model,
            "by_session": session_breakdown(allr, allp, cost_model),
            "cost_scenarios": dict(COST_SCENARIOS),
            # C-2 성공 기준 점검도 러너가 만든다 — 문서에 싣는 수치가 여기를 지나야 한다.
            "min_rise_independence": min_rise_independence(res=res),
            "pooled_ci_permitted": len(days) >= MIN_DAY_CLUSTERS,
            "per_day_counts": {d: int(len(res["real"][d])) for d in days},
            "rules": rules}


def _nan() -> float:
    return float("nan")


def main(db: Path, *, out_dir: Path | None = None) -> int:
    res = run(db)
    rep = build_report(res)
    if not rep["days"]:
        print("no entries")
        return 0
    print(f"trading days with entries: {len(rep['days'])} -> {rep['days']}")
    print(f"real entries {rep['n_real']}   entries/day {rep['entries_per_day']:.1f}")

    print(f"\n=== [docs/23 sec 10-N.5] PER-DAY entry counts "
          f"(cluster check, need >= {MIN_DAY_CLUSTERS})")
    for d, n in rep["per_day_counts"].items():
        print(f"  {d}: {n}")
    print(f"  effective day clusters = {len(rep['days'])} -> pooled CI "
          f"{'PERMITTED' if rep['pooled_ci_permitted'] else 'WITHHELD'}")

    # ---- docs/23 §10-N.8 ----
    print("\n=== [docs/23 sec 10-N.8] PAIRED DIFFERENCE (real - placebo)")
    print(f"{'rule':<16}{'pair n':>7}{'diff':>9}{'CI low':>9}{'CI high':>9}"
          f"{'verdict':>15}{'Bonf low':>10}{'Bonf high':>10}")
    for r in rep["rules"]:
        print(f"{r['rule']:<16}{r['pair_n']:>7}{r['diff_mean']:>9.4f}"
              f"{r['diff_ci'][0]:>9.4f}{r['diff_ci'][1]:>9.4f}{r['diff_verdict']:>15}"
              f"{r['diff_ci_bonferroni'][0]:>10.4f}{r['diff_ci_bonferroni'][1]:>10.4f}")
    verdicts: dict = {}
    for r in rep["rules"]:
        verdicts[r["diff_verdict"]] = verdicts.get(r["diff_verdict"], 0) + 1
    print(f"  verdict counts: {verdicts}")

    # ---- docs/23 §10-N.9 ----
    print("\n=== [docs/23 sec 10-N.9] COST SCENARIOS (net mean by exit rule)")
    scen = list(COST_SCENARIOS)
    print(f"{'rule':<16}{'fill':>7}{'gross':>9}{'gross CI':>21}"
          + "".join(f"{k:>20}" for k in scen))
    for r in rep["rules"]:
        ci = f"[{r['gross_ci'][0]:+.4f},{r['gross_ci'][1]:+.4f}]"
        print(f"{r['rule']:<16}{r['fill_rate']:>7.3f}{r['gross_mean']:>9.4f}{ci:>21}"
              + "".join(f"{r['net_by_scenario'][k]:>20.4f}" for k in scen))
    print(f"  scenarios: {COST_SCENARIOS}")
    print("  NOTE limit_only is an UPPER BOUND (assumes zero non-fill and zero "
          "adverse selection); real limit fill quality is unmeasured.")

    # ---- docs/23 §10-N.10 ----
    print("\n=== [docs/23 sec 10-N.10] DAYS NEEDED for the difference CI to exclude 0")
    for r in rep["rules"]:
        dn = r["days_needed"]
        tag = (f"{dn['days_needed']} days (n={dn['n_entries_needed']})"
               if dn.get("reachable") else f"UNREACHABLE ({dn.get('reason')})")
        print(f"  {r['rule']:<16} diff {r['diff_mean']:+.4f} -> {tag}")
    print("  NOTE conditional on the point estimate being the true effect. Where the "
          "difference CI crosses zero, no number of days excludes zero.")

    # ---- C-2 성공 기준 (이탈이 슈팅 임계에 의존하지 않는가) ----
    ind = rep["min_rise_independence"]
    print("\n=== [docs/23 sec 10-N.2] MIN_RISE INDEPENDENCE (C-2 success criterion)")
    print(f"  structurally independent of the shot threshold: "
          f"{ind['structurally_independent']}")
    print(f"  {ind['note']}")
    for t, v in ind["shared_result_across_thresholds"].items():
        print(f"    min_rise={t}: gross {v['gross_mean']:+.4f}  net {v['net_mean']:+.4f}")

    # ---- 세션 (사용자 지적 2026-08-03) ----
    cm = rep["cost_model"]
    print("\n=== [docs/23 sec 10-Q.1] SESSION MIX of the entries (KST boundaries)")
    print(f"  {rep['session_counts']}")
    print("  boundary rule: an entry is assigned by its ENTRY time; an exit that "
          "crosses into the next session does not move it.")

    print("\n=== [docs/23 sec 10-Q.2] DEPTH COMPLETENESS - read this BEFORE any cost table")
    print(f"{'session':<10}{'rows':>8}{'median ask levels':>20}{'multi-level rate':>19}")
    for s, d in cm.get("depth_completeness", {}).items():
        print(f"{s:<10}{d['rows']:>8}{d['median_ask_levels']:>20.1f}"
              f"{d['multi_level_rate']:>19.3f}")
    print("  !! multi-level depth is stored ONLY in the day session. Elsewhere we have "
          "top-of-book only,")
    print("  !! so clip costs above the smallest clip are NOT computable there. This is "
          "a DATA property, not a liquidity finding.")

    print("\n=== [docs/23 sec 10-Q.3] SESSION x CLIP round-trip cost (median, "
          "cross-and-cross)")
    print(f"{'session':<10}{'snaps':>7}{'syms':>6}{'spread':>9}{'ToB $':>9}"
          + "".join(f"{'$'+str(c):>13}" for c in SESSION_CLIPS))
    for s, d in cm.get("all_bands", {}).items():
        cells = []
        for c in SESSION_CLIPS:
            r = d["by_clip"][c]
            cells.append(f"{r['median']:>13.4f}" if r["measurable"] else f"{'n/a':>13}")
        print(f"{s:<10}{d['n_snaps']:>7}{d['n_symbols']:>6}"
              f"{d['median_rel_spread']:>9.4f}{d['median_tob_usd']:>9.0f}"
              + "".join(cells))
    print("  n/a = not computable (top-of-book only). We do not invent a number for it.")

    ws = cm.get("within_symbol", {})
    print("\n=== [docs/23 sec 10-Q.3b] WITHIN-SYMBOL contrast vs regular (spread)")
    if ws.get("available"):
        for s, d in ws["pairs"].items():
            tag = "" if d["powered"] else "  (UNDERPOWERED n<10)"
            print(f"  regular vs {s:<8} n_symbols={d['n_symbols']:<4} "
                  f"regular={d['median_reference']:.4f}  {s}={d['median_other']:.4f}  "
                  f"within-symbol delta={d['median_within_symbol_delta']:+.4f}{tag}")
        print("  session symbol mix differs (tier-2 promotion is dynamic), so the "
              "pooled table above is confounded; THIS is the canonical comparison.")
    else:
        print(f"  unavailable: {ws.get('reason')}")

    print("\n=== [docs/23 sec 10-Q.4] PER-SESSION result (downtick_1, at that "
          "session's measured cost)")
    print(f"{'session':<10}{'n':>5}{'cost':>9}{'gross':>9}{'net':>9}{'diff':>9}"
          f"{'CI low':>9}{'CI high':>9}{'verdict':>15}")
    for b in rep["by_session"]:
        r = next((x for x in b["rules"] if x["rule"] == "downtick_1"), None)
        if r is None or not b["n_entries"]:
            print(f"{b['session']:<10}{b['n_entries']:>5}{'':>9}{'':>9}{'':>9}"
                  f"{'':>9}{'':>9}{'':>9}{'NO ENTRIES':>15}")
            continue
        print(f"{b['session']:<10}{b['n_entries']:>5}{b['measured_round_trip']:>9.4f}"
              f"{r['gross_mean']:>9.4f}{r['net_at_session_cost']:>9.4f}"
              f"{r['diff_mean']:>9.4f}{r['diff_ci'][0]:>9.4f}{r['diff_ci'][1]:>9.4f}"
              f"{r['diff_verdict']:>15}")
    print("  NOTE splitting by session thins the day clusters further - every "
          "per-session CI is provisional.")

    out = (out_dir or OUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "design_b.json"
    path.write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1
                  else Path(r"C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db")))

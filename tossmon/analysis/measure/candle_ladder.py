"""**(가′) 사다리** — 첫 진입 층 하나에, 두 팔 다 봉 자로 (`G2G3-PREREG` §3-1 개정 6 [보정 뒤], `docs/71`).

## 지위 — 탐색 B 위의 사다리다. 판정이 아니다

개정 6 [보정 뒤] (커밋 `8c31733`, **이 파일보다 먼저**)가 데이터 전에 얼린 것을 그대로 옮긴다:

- **칸 하나**: 선언 칸(`TOSS E1_new_entry N10`) × `anchored ∧ first_in_regular`
  (`candle_ruler.strata_masks` 그대로). 다른 층·다른 칸·다른 폭 없음.
  앵커 없는 첫 진입은 **세기만** 하고 재지 않는다.
- **두 팔 다 개정 6 의 봉 자**: `ruler_bias.bar_forward_at` — t0 봉 제외, 진입가 = F 첫 봉 시가.
- **위약 = 같은 종목·같은 정규장의 봉 분.** 후보의 `tau` = 그 봉의 내용 시작(`T_b − 60s`) —
  그 봉이 위약의 t0 봉이 되어 사건과 같은 규칙으로 제외된다. 자기 간격 `|tau − t0| > 600s`
  (`candle_ruler.CANDLE_SELF_GAP_S`), 정합 = 개정 5 의 봉 키 밴드 그대로(`crv5` ±20% ∧
  `cvol5` ×1.2, 키 정의 필수), **사건당 3 추첨 · 씨앗 20260818** — 전부 기존 상수의 재사용이고
  이 모듈이 새로 만드는 눈금은 없다.
- **통계량**: (실제 − 위약) `max_ret_300s(봉)` 의 평균 차, 세션 군집 부트스트랩
  (`ranking_forward_path.cluster_bootstrap_diff` 그대로, 군집 < 5 면 CI 없음), 중앙값 차 병기,
  **본페로니 분모 1**. 읽는 법: CI 하한을 0 이 아니라 **+0.91%p**(`FIRST_ENTRY_BIAS` — `docs/70`
  §4 의 그 층 짝 편향, 짝 18)와 견준다. **판정 문구는 없다** — 탐색 B 위의 수다.
- **깔때기를 같이 싣는다**: 후보 분 → 키 → 밴드 → 짝, 세션별 짝 수, 위약 분의 사전 상태 분포.
  짝이 서지 않으면 그것이 결과다.

실제 팔의 관례는 개정 3 의 사다리와 같다: 실제 값은 **짝 지어진**(추첨 ≥ 1) 사건의 값이고
위약 값은 추첨 전부다. 유한하지 않은 값은 CI 계산이 떨군다(기존 함수 그대로).

## 하지 않는 것

- **홀드아웃(2026-05-01~07-29)·확증 팔(08-26 이후)을 읽지 않는다** — 창은
  `ranking_forward_path.arm_window` 가 정한다(`candle_ruler.count` 를 그대로 지나간다).
- **라이브 API 0 건. DB 는 `mode=ro`. 수집기 무접촉. 테이프를 아예 읽지 않는다.**
- 판정 문구·다른 층·다른 폭·새 눈금을 만들지 않는다.

실행: `python -m tossmon.analysis.measure.candle_ladder [db] [--era B] [--out DIR] [--name NAME]`
-> `out/<name>.json` + `<name>_events.csv` + `<name>_draws.csv`. **콘솔 ASCII.**
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from tossmon.analysis import hires_events as HE
from tossmon.analysis import session as SS
from tossmon.analysis.measure import candle_ruler as CR
from tossmon.analysis.measure import e2_design_funnel as EF
from tossmon.analysis.measure import ranking_forward_path as RFP
from tossmon.analysis.measure import ruler_bias as RB

OUT_DIR = RFP.OUT_DIR
SEC_MS = RFP.SEC_MS
MIN_MS = SS.MIN_MS

DECLARED = EF.DECLARED
#: 사다리가 붙는 유일한 층 (개정 6 [보정 뒤]). 마스크는 `candle_ruler.strata_masks` 그대로.
STRATUM = "first_in_regular"

#: 전부 기존 상수의 재사용 — 이 모듈이 새로 만드는 눈금은 없다.
SELF_GAP_S = CR.CANDLE_SELF_GAP_S          # 600 = 지평 300 + 사전 창 300 (개정 5)
RV_TOL = CR.CANDLE_RV_TOL                  # crv5 ±20%
VOL_FACTOR = CR.CANDLE_VOL_FACTOR          # cvol5 ×1.2 (로그 대칭 ±20%)
DRAWS = RFP.MATCH_DRAWS                    # 사건당 3
SEED = RFP.SEED                            # 20260818
N_COMPARISONS = 1                          # 칸 하나·밴드 하나·층 하나 — 개정 6 이 그 하나다

#: ★ 이 사다리의 CI 하한을 견줄 선 — `docs/70` §4: first_in_regular 짝 18 의 짝 단위
#: 차등 편향 **평균 +0.91%p** (`G2G3-PREREG` §3-1 개정 6 [보정 뒤] "통과선 조정").
#: 계산하지 않는다 — 사전등록에서 옮겨 적은 얼린 수다.
FIRST_ENTRY_BIAS = 0.0091

FORBIDDEN_PHRASES = RFP.FORBIDDEN_PHRASES

NO_VERDICT = ("exploration era B only - this ladder produces a distribution and a CI on a "
              "pre-declared single stratum with both arms on the candle ruler; it renders "
              "NO verdict. The CI lower bound is compared against the frozen differential "
              "ruler bias line (+0.91pp, G2G3-PREREG s3-1 revision 6 [after], docs/70 s4), "
              "not against zero")


# --------------------------------------------------------------------------- #
# 위약 후보 — 순수 함수. 경계가 이 모듈의 전부다
# --------------------------------------------------------------------------- #
def placebo_candidate_mask(key_rv: np.ndarray, key_vol: np.ndarray, tau_ms: np.ndarray,
                           t0_ms: int, ev_rv: float, ev_vol: float, *,
                           gap_s: int = SELF_GAP_S, rv_tol: float = RV_TOL,
                           vol_factor: float = VOL_FACTOR) -> dict:
    """후보 `tau` 마다 (간격 → 키 → `crv5` 밴드 → `cvol5` 밴드) 를 누적으로 판정한다.

    - `gap`  : `|tau − t0| > gap_s` — 자기 사건의 측정 구간(전방 300 + 사전 300)과 겹치지 않는다.
      `>=` 로 바꾸거나 300 초로 줄이는 돌연변이가 테스트를 red 로 만든다.
    - `key`  : `crv5 > 0 ∧ cvol5 > 0` (개정 5 의 키 정의 그대로).
    - `rv`   : `crv5 ∈ [ev·0.8, ev·1.2]`.
    - `vol`  : `cvol5 ∈ [ev/1.2, ev·1.2]` — 이 밴드가 무는 것을 테스트가 심어 확인한다.
    """
    tau = np.asarray(tau_ms, dtype="int64")
    rv = np.asarray(key_rv, dtype="float64")
    vol = np.asarray(key_vol, dtype="float64")
    gap = np.abs(tau - int(t0_ms)) > int(gap_s) * SEC_MS
    key = gap & np.isfinite(rv) & (rv > 0) & np.isfinite(vol) & (vol > 0)
    ok_ev = (np.isfinite(ev_rv) and ev_rv > 0 and np.isfinite(ev_vol) and ev_vol > 0)
    if ok_ev:
        band_rv = key & CR.in_band(rv, float(ev_rv), rv_tol)
        band_all = band_rv & CR.in_factor_band(vol, float(ev_vol), vol_factor)
    else:
        band_rv = np.zeros(tau.size, dtype=bool)
        band_all = band_rv
    return {"gap": gap, "key": key, "rv": band_rv, "all": band_all,
            "event_key_ok": bool(ok_ev)}


def placebo_taus(ts: np.ndarray) -> np.ndarray:
    """위약 후보의 `tau` = 그 봉의 **내용 시작** `T_b - 60s`.

    개정 6 [보정 뒤]가 얼린 것: 그렇게 두면 **그 봉이 위약 자신의 t0 봉**이 되고, 사건과
    똑같이 `F(tau) = (L0, L0+300s]` 로 제외된다 (`L0(T_b - 60s) = T_b`). `T_b` 를 그대로
    `tau` 로 쓰면 위약은 사건보다 한 봉 **뒤**에서 시작해 두 팔이 다른 자를 갖는다 —
    그 돌연변이가 테스트를 red 로 만든다.
    """
    return np.asarray(ts, dtype="int64") - MIN_MS


def draw_from(rng: np.random.Generator, idx: np.ndarray, draws: int = DRAWS) -> np.ndarray:
    """후보 인덱스에서 `draws` 개를 **복원 추출** — 개정 3 의 추첨(`draw_banded`)과 같은 의미론."""
    idx = np.asarray(idx, dtype="int64")
    if idx.size == 0:
        return np.zeros(0, dtype="int64")
    return idx[rng.integers(0, idx.size, size=int(draws))]


# --------------------------------------------------------------------------- #
# 사다리 — 재기만. 층 하나, 팔 둘 다 봉 자
# --------------------------------------------------------------------------- #
def ladder(db: Path, *, era: str = "B", progress: bool = False) -> dict:
    """한 시대의 선언한 칸에서 `anchored ∧ first_in_regular` 사건에 봉 자 사다리를 태운다.

    창·세션·사건·층 표식은 `candle_ruler.count` 가(그 안의 `arm_window` 가) 정한다 —
    이 모듈에 창 규칙이 따로 없다. 사건의 봉 키(`crv5`·`cvol5`)도 그 표식의 값을 그대로 쓴다.
    """
    counted = CR.count(db, era=era, progress=progress)
    tags = counted["tags"]
    conn = HE.open_ro(db)
    ev_parts, draw_parts = [], []
    n_unanchored_first = 0
    n_unanchored_first_with_bar = 0
    try:
        for s in counted["sessions"]:
            sess = s["session"]
            t = tags[tags.session == sess] if len(tags) else tags
            if not len(t):
                continue
            masks = CR.strata_masks(t)
            first = t[masks[STRATUM]]
            un = t[t.first_in_regular.to_numpy(bool) & ~t.anchored.to_numpy(bool)]
            cand_fwd = RB.load_candle_ohlc(conn, s["open_ms"], s["close_ms"])
            cand_keys = CR.load_candles(conn, s["open_ms"], s["close_ms"])
            n_unanchored_first += int(len(un))
            for _, row in un.iterrows():
                c = cand_fwd.get(str(row.symbol))
                if c is not None:
                    f = RB.bar_forward_at(c, np.asarray([int(row.t0_ms)]))
                    # 세기만 한다 - 수익은 읽지도 싣지도 않는다 (개정 6 [보정 뒤]).
                    n_unanchored_first_with_bar += int(f["n_bars"][0] > 0)
            rng = np.random.default_rng(SEED)
            for _, row in first.iterrows():
                sym = str(row.symbol)
                t0 = int(row.t0_ms)
                c = cand_fwd.get(sym)
                ck = cand_keys.get(sym)
                fe = (RB.bar_forward_at(c, np.asarray([t0])) if c is not None else None)
                ev = {"session": sess, "symbol": sym, "t0_ms": t0,
                      "crv5": float(row.crv5), "cvol5": float(row.cvol5),
                      "key_ok": bool(row.key_ok),
                      "bar_n_bars": int(fe["n_bars"][0]) if fe is not None else 0,
                      "bar_max_ret": float(fe["max_ret"][0]) if fe is not None else np.nan,
                      "bar_entry_u": float(fe["entry_u"][0]) if fe is not None else np.nan,
                      "bar_entry_lag_s": float(fe["entry_lag_s"][0]) if fe is not None else np.nan,
                      "n_cand_gap": 0, "n_cand_key": 0, "n_cand_rv": 0, "n_cand": 0,
                      "n_draws": 0, "paired": False}
                if c is not None and ck is not None and c["ts"].size:
                    tau_all = placebo_taus(c["ts"])     # 봉의 내용 시작 = 위약의 tau
                    keys = CR.candle_keys_at(ck, tau_all)
                    m = placebo_candidate_mask(keys[CR.KEY_RV], keys[CR.KEY_VOL], tau_all,
                                               t0, float(row.crv5), float(row.cvol5))
                    ev["n_cand_gap"] = int(m["gap"].sum())
                    ev["n_cand_key"] = int(m["key"].sum())
                    ev["n_cand_rv"] = int(m["rv"].sum())
                    ev["n_cand"] = int(m["all"].sum())
                    picks = draw_from(rng, np.flatnonzero(m["all"]))
                    if picks.size:
                        tau_p = tau_all[picks]
                        fp = RB.bar_forward_at(c, tau_p)
                        ev["n_draws"] = int(picks.size)
                        ev["paired"] = True
                        draw_parts.append(pd.DataFrame({
                            "session": sess, "symbol": sym, "event_t0_ms": t0,
                            "tau_ms": tau_p,
                            "crv5": np.asarray(keys[CR.KEY_RV], "float64")[picks],
                            "cvol5": np.asarray(keys[CR.KEY_VOL], "float64")[picks],
                            "bar_n_bars": fp["n_bars"], "bar_max_ret": fp["max_ret"],
                            "bar_entry_u": fp["entry_u"], "bar_entry_lag_s": fp["entry_lag_s"],
                        }))
                ev_parts.append(ev)
            if progress:
                np_ = sum(1 for e in ev_parts if e["session"] == sess and e["paired"])
                print(f"  {sess} first_anchored={len(first):>3,} paired={np_:>3,} "
                      f"unanchored_first={len(un):>2,}", flush=True)
    finally:
        conn.close()
    events = pd.DataFrame(ev_parts)
    draws = pd.concat(draw_parts, ignore_index=True) if draw_parts else pd.DataFrame()
    real = {}
    plac = {}
    if len(events):
        for sess, sub in events[events.paired].groupby("session"):
            real[sess] = sub.bar_max_ret.to_numpy(dtype="float64")
    if len(draws):
        for sess, sub in draws.groupby("session"):
            plac[sess] = sub.bar_max_ret.to_numpy(dtype="float64")
    stat = RFP.cluster_bootstrap_diff(real, plac, n_comparisons=N_COMPARISONS)
    return {"db": str(db), "era": era, "since_ms": counted["since_ms"],
            "since_utc": counted["since_utc"], "until_ms": counted["until_ms"],
            "until_utc": counted["until_utc"], "db_max_snap_utc": counted["db_max_snap_utc"],
            "sessions": counted["sessions"], "sessions_used": counted["sessions_used"],
            "n_sessions": counted["n_sessions"], "events": events, "draws": draws,
            "stat": stat,
            "unanchored_first": {"n": n_unanchored_first,
                                 "with_bar_forward": n_unanchored_first_with_bar,
                                 "note": "counted only - no return was computed or kept "
                                         "(revision 6 [after]); widening the population "
                                         "beyond anchored is a future decision"}}


def _dist(v) -> dict:
    a = np.asarray(v, dtype="float64")
    a = a[np.isfinite(a)]
    if a.size == 0:
        return {"n": 0, "median": None, "mean": None, "p25": None, "p75": None}
    return {"n": int(a.size), "median": float(np.median(a)), "mean": float(a.mean()),
            "p25": float(np.percentile(a, 25)), "p75": float(np.percentile(a, 75))}


def build_report(res: dict) -> dict:
    """산출물 하나. 문서에 실리는 수치는 전부 여기를 지나간다."""
    ev, dr, stat = res["events"], res["draws"], res["stat"]
    paired = ev[ev.paired] if len(ev) else ev
    per_session = []
    for s in res["sessions_used"]:
        e = ev[ev.session == s] if len(ev) else ev
        p = e[e.paired] if len(e) else e
        d = dr[dr.session == s] if len(dr) else dr
        rv = p.bar_max_ret.to_numpy("float64") if len(p) else np.zeros(0)
        dv = d.bar_max_ret.to_numpy("float64") if len(d) else np.zeros(0)
        rv, dv = rv[np.isfinite(rv)], dv[np.isfinite(dv)]
        per_session.append({
            "session": s, "n_first": int(len(e)), "n_paired": int(len(p)),
            "n_draws": int(len(d)),
            "real_mean": float(rv.mean()) if rv.size else None,
            "placebo_mean": float(dv.mean()) if dv.size else None,
            "diff_mean": (float(rv.mean() - dv.mean()) if rv.size and dv.size else None)})
    rmed = _dist(paired.bar_max_ret if len(paired) else [])
    pmed = _dist(dr.bar_max_ret if len(dr) else [])
    median_diff = (None if rmed["median"] is None or pmed["median"] is None
                   else rmed["median"] - pmed["median"])
    ci_low = stat["ci95"][0] if stat.get("ci95") else None
    return {
        "labels": list(RFP.LABELS),
        "conditions": HE.MEASUREMENT_CONDITIONS,
        "db": res["db"],
        "window": {"since_utc": res["since_utc"], "until_utc": res["until_utc"],
                   "db_max_snap_utc": res["db_max_snap_utc"]},
        "arm": {"name": "exploration", "exploration_era": res["era"],
                "sessions_used": res["sessions_used"],
                "confirmation_floor_utc": RFP.CONFIRMATION_FLOOR_UTC,
                "exploration_b_ceiling_utc": RFP.EXPLORATION_B_CEILING_UTC,
                "discarded_sessions": list(RFP.DISCARDED_SESSIONS),
                "window_rule": "candle_ruler.count -> ranking_forward_path.arm_window - "
                               "this module has no window rule of its own"},
        "holdout": {"window": [SS.HOLDOUT_START, SS.HOLDOUT_END]},
        "design": {
            "step": "(3) the ladder - only AFTER revision 6 [after] was committed "
                    "(G2G3-PREREG s3-1, commit 8c31733); one cell, one stratum, one band",
            "declared_cell": list(DECLARED),
            "stratum": STRATUM,
            "both_arms_ruler": "revision 6 candle ruler (ruler_bias.bar_forward_at): t0 bar "
                               "excluded, entry = open of the first bar of F(tau)",
            "placebo": (f"bar minutes of the same symbol and regular session; tau = bar "
                        f"content start (T_b - 60s) so that bar is the placebo's own t0 bar; "
                        f"self gap > {SELF_GAP_S}s; keys at tau via candle_keys_at; bands "
                        f"crv5 +/-{int(RV_TOL * 100)}% AND cvol5 x{VOL_FACTOR}; "
                        f"{DRAWS} draws per event, seed {SEED} - all reused constants"),
            "statistic": "mean(real, paired) - mean(placebo draws), trading-day cluster "
                         "bootstrap (cluster_bootstrap_diff, no CI under 5 clusters), "
                         "median difference alongside",
            "n_comparisons": N_COMPARISONS,
            "first_entry_bias_line": FIRST_ENTRY_BIAS,
            "no_verdict": NO_VERDICT,
        },
        "funnel": {
            "n_first_anchored": int(len(ev)),
            "n_bar_forward": int((ev.bar_n_bars > 0).sum()) if len(ev) else 0,
            "candidates_per_event": {k: _dist(ev[c]) for k, c in
                                     (("outside_gap", "n_cand_gap"), ("key", "n_cand_key"),
                                      ("rv_band", "n_cand_rv"), ("rv_and_vol_band", "n_cand"))}
            if len(ev) else {},
            "n_paired": int(ev.paired.sum()) if len(ev) else 0,
            "n_draws": int(len(dr)),
            "unanchored_first": res["unanchored_first"],
        },
        "headline": {
            "real": rmed, "placebo": pmed,
            "mean_real": stat["mean_real"], "mean_placebo": stat["mean_placebo"],
            "mean_diff": stat["mean_diff"], "median_diff": median_diff,
            "ci95": stat.get("ci95"), "ci_bonferroni": stat.get("ci_bonferroni"),
            "n_clusters": stat["n_clusters"], "n_real": stat["n_real"],
            "n_placebo": stat["n_placebo"], "ci_withheld": stat.get("ci_withheld"),
            "ci_low_minus_bias_line": (None if ci_low is None
                                       else ci_low - FIRST_ENTRY_BIAS),
        },
        "per_session": per_session,
        "placebo_pre_state": {
            "event_crv5": _dist(ev.crv5 if len(ev) else []),
            "event_cvol5": _dist(ev.cvol5 if len(ev) else []),
            "draw_crv5": _dist(dr.crv5 if len(dr) else []),
            "draw_cvol5": _dist(dr.cvol5 if len(dr) else []),
        },
    }


# --------------------------------------------------------------------------- #
# 콘솔 - ASCII 만
# --------------------------------------------------------------------------- #
def _pp(v):
    return "   -   " if v is None else f"{100.0 * float(v):+7.3f}"


def _n(v):
    return "-" if v is None else f"{float(v):,.1f}"


def print_report(rep: dict) -> None:
    print("=" * 78)
    print("CANDLE LADDER - step (3): one pre-declared stratum, both arms on the candle")
    print("ruler   (G2G3-PREREG s3-1 revision 6 [after], docs/70 s11)")
    print("=" * 78)
    for i, s in enumerate(rep["labels"], 1):
        for j, line in enumerate(RFP._wrap(s, 70)):
            print(f"  [{i}] {line}" if j == 0 else f"      {line}")
    a, d = rep["arm"], rep["design"]
    print(f"  arm     : exploration era {a['exploration_era']}  sessions "
          f"{len(a['sessions_used'])} {','.join(a['sessions_used'])}")
    print(f"  window  : {rep['window']['since_utc']} .. {rep['window']['until_utc']}"
          f"  (confirmation floor {a['confirmation_floor_utc']} - not read)")
    print(f"  cell    : {d['declared_cell']}  x  stratum {d['stratum']}")
    for k in ("both_arms_ruler", "placebo", "statistic", "no_verdict"):
        for j, line in enumerate(RFP._wrap(d[k], 58)):
            print(f"  {k + ':':<20}{line}" if j == 0 else f"  {'':<20}{line}")

    f = rep["funnel"]
    print("\n[1] funnel - first_in_regular (anchored) events and their placebo pool")
    print(f"    events {f['n_first_anchored']:,} -> bar forward path {f['n_bar_forward']:,} "
          f"-> paired {f['n_paired']:,} -> draws {f['n_draws']:,}")
    if f.get("candidates_per_event"):
        print(f"    {'candidates per event':<26}{'median':>8}{'p25':>7}{'p75':>7}")
        for k, dd in f["candidates_per_event"].items():
            print(f"    {k:<26}{_n(dd['median']):>8}{_n(dd['p25']):>7}{_n(dd['p75']):>7}")
    u = f["unanchored_first"]
    print(f"    unanchored first entries: {u['n']:,} ({u['with_bar_forward']:,} with a bar "
          f"forward path) - COUNTED ONLY, no return computed")

    print("\n[2] per session")
    print(f"{'session':<14}{'n_first':>8}{'paired':>7}{'draws':>7}{'real_mean':>10}"
          f"{'plac_mean':>10}{'diff':>9}")
    for s in rep["per_session"]:
        print(f"{s['session']:<14}{s['n_first']:>8,}{s['n_paired']:>7,}{s['n_draws']:>7,}"
              f"{_pp(s['real_mean']):>10}{_pp(s['placebo_mean']):>10}{_pp(s['diff_mean']):>9}")

    h = rep["headline"]
    print("\n[3] the one number - max_ret_300s(candle), real paired vs placebo draws, in pp")
    print(f"    real   n={h['n_real']:>4,}  mean {_pp(h['mean_real'])}  median {_pp(h['real']['median'])}")
    print(f"    placebo n={h['n_placebo']:>4,}  mean {_pp(h['mean_placebo'])}  median {_pp(h['placebo']['median'])}")
    print(f"    mean diff {_pp(h['mean_diff'])} pp   median diff {_pp(h['median_diff'])} pp")
    if h["ci95"]:
        print(f"    ci95 [{_pp(h['ci95'][0])}, {_pp(h['ci95'][1])}] pp  "
              f"(bonferroni x{rep['design']['n_comparisons']}: "
              f"[{_pp(h['ci_bonferroni'][0])}, {_pp(h['ci_bonferroni'][1])}])")
        print(f"    ci95 lower bound - frozen bias line "
              f"{_pp(rep['design']['first_entry_bias_line'])} pp = "
              f"{_pp(h['ci_low_minus_bias_line'])} pp   (exploration - no verdict)")
    else:
        print(f"    CI: withheld - {h['ci_withheld']}")

    p = rep["placebo_pre_state"]
    print("\n[4] pre-state balance - the draws stand where the events stand?")
    print(f"    {'key':<10}{'events md':>12}{'draws md':>12}{'events p25/p75':>22}{'draws p25/p75':>22}")
    for k in ("crv5", "cvol5"):
        e, dd = p[f"event_{k}"], p[f"draw_{k}"]
        fmt = (lambda x: "-" if x is None else f"{x:,.4g}")
        print(f"    {k:<10}{fmt(e['median']):>12}{fmt(dd['median']):>12}"
              f"{fmt(e['p25']) + '/' + fmt(e['p75']):>22}{fmt(dd['p25']) + '/' + fmt(dd['p75']):>22}")
    print("\n" + "=" * 78)
    print("This ladder measures ONE pre-declared stratum with both arms on the candle")
    print("ruler.  Its CI is read against the frozen bias line, and it decides nothing.")
    print("=" * 78)


def main(argv: list) -> int:
    db = HE.DB
    era = "B"
    out_dir = OUT_DIR
    name = None
    args = list(argv[1:])
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--era":
            era = args[i + 1]; i += 2
        elif a == "--out":
            out_dir = Path(args[i + 1]); i += 2
        elif a == "--name":
            name = args[i + 1]; i += 2
        else:
            db = Path(a); i += 1
    name = name or f"candle_ladder_era_{era.lower()}"
    print(f"candle ladder, era {era} - one stratum, both arms on the candle ruler ...",
          flush=True)
    res = ladder(db, era=era, progress=True)
    rep = EF._jsonable(build_report(res))
    text = json.dumps(rep).lower()
    for phrase in FORBIDDEN_PHRASES:
        if phrase in text:
            raise ValueError(f"forbidden phrase in report: {phrase!r}")
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{name}.json"
    p.write_text(json.dumps(rep, indent=1), encoding="utf-8")
    if len(res["events"]):
        res["events"].to_csv(out_dir / f"{name}_events.csv", index=False)
    if len(res["draws"]):
        res["draws"].to_csv(out_dir / f"{name}_draws.csv", index=False)
    print_report(rep)
    print(f"-> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

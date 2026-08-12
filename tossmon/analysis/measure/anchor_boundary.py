"""**따름정리가 기대는 앵커를 경계에서 잰다** — `vol_surge_lead_min` 의 매매일 앵커.

`docs/48` §11-5d 가 §4-0 따름정리를 폐기가 아니라 **성립 조건 명시**로 정정했다:

> **조건**: 이벤트마다, `obs_le` 의 컷오프(= `t0`)와 `strict_lt` 의 컷오프에 대해
> **앵커 함수가 같은 값을 돌려줄 것.** 앵커 단위는 지표마다 다르다 —
> `rvol_first_cross_*` 는 **세션**, `vol_surge_lead_min` 은 **매매일**.

세션 쪽은 `cutoff_tautology` 가 이미 쟀다(`mode_changed_session`, `docs/49` §5-1).
**매매일 쪽은 아무도 재지 않았다** — `docs/48` §11-5c 가 *"W3 은 이쪽을 재지 않았다 —
미실측이다"* 라고 적어 두었고 §11-7 요구 #1 이 그것이다. 이 모듈이 그 하나를 잰다.

> ★ **인용 정정 (2026-08-12).** 위 두 줄은 **이 모듈이 돌기 전(2026-08-09)의 상태**이며,
> 지금 그대로 읽으면 안 된다.
> ① **이 모듈이 그것을 쟀다** — 결과는 `docs/51_anchor_boundary` §2·§3·§5
> (같은 60건, 매매일 앵커가 갈린 건 2/60, 검출 항등 어긋남 0, 리드 값 어긋남 0/42).
> ② 위에 **축어 인용한 `docs/48` §11-5c 의 문장이 바뀌었다** — W7 이 `d4d3e20` 으로
> 그 문장에 **취소선을 긋고** *"재지 않은 것이 아니라 2026-08-09 에 쟀다"* 를 병기했다.
> 인용만 떼어 읽으면 원문과 어긋난다.
> 원문을 지우지 않은 것은 **이 모듈이 왜 쓰였는지**가 그 두 줄에 들어 있기 때문이다.
> 지목: `docs/48` §12-4 #2 · `docs/54` §3.

## 왜 매매일 앵커가 따름정리를 깰 수 있는가

`vol_surge_lead_min` 의 스캔 창은 `_first_bar_vol_z_cross(pre, day_lo, t_hi, 3.0)` 이고
`day_lo = _locate_day_start(cutoff, calendar)`, 스캔 시작은 `lo = day_lo − 240분` 이다.
**창의 시작이 컷오프의 함수**이므로:

- **두 모드의 `day_lo` 가 같으면**: `strict_lt` 의 창은 `obs_le` 창의 **접두(prefix)** 다.
  전진 스캔의 첫 교차는 접두 안에서 이미 결정되고, 봉이 없는 분은 거래량 0 → `log1p(0)=0`
  이라 z ≥ 3 을 만들 수 없으므로, 꼬리에서 새로 잡히는 것은 **T0 봉뿐(리드 0)** 이다.
  → 따름정리 성립.
- **`day_lo` 가 다르면**: 두 창이 접두 관계가 아니다. 창의 시작과 **베이스라인 240분까지
  통째로 이동**하므로 검출 여부만이 아니라 **리드 값 자체가 달라질 수 있다** —
  세션 앵커(`rvol_first_cross`)보다 파괴력이 크다.

## 무엇을 재고, 무엇을 재지 않는가

**잰다**: 두 모드의 매매일 앵커가 같은 값인가, 다른 이벤트가 있으면 그 이벤트에서
따름정리(검출 항등 **그리고 리드 값 동일**)가 실제로 깨지는가.

**반증 관측**: 깨진 이벤트인데 **매매일 앵커가 같으면** 매매일 앵커 가설은 틀렸다.
그때는 다른 설명(세션 앵커·베이스라인 표본 부족·데이터 공백)을 찾아야 한다.
그래서 이 모듈은 깨진 건마다 `mode_changed_day`·`mode_changed_session`·
`strict_warmup_short`·`cutoff_gap_min` 을 **함께** 기록한다 — 대안 설명을 지우기 위해서다.

**재지 않는다 — 판정하지 않는다.** 사전등록 §4 의 어떤 판정도 다시 내리지 않고
등록 시행을 재실행하지 않는다. 여기서 나오는 검출률 비슷한 수를 §4 판정에 인용하면
그 결론은 무효다 — **표본이 등록 표본이 아니다**(§2.7·§2.8 미통과).

## 표본은 `cutoff_tautology` 와 **같은 60건**이다

W7 이 *"새 시행을 요구하지 않는다 — 계측기에 `_locate_day_start` 비교를 더하면 같은
60건에서 잰다"* 고 지정했다. 그래서 캘린더 역산·심볼 선정·봉 적재를 새로 쓰지 않고
`cutoff_tautology` 의 것을 **그대로 import** 한다. 같은 DB·같은 상한·같은 게이트 →
이벤트 집합이 구성상 동일하다. 캘린더 역산에 관한 유보도 그 모듈 docstring 그대로다.

실행: `python -m tossmon.analysis.measure.anchor_boundary [db_path] [n_symbols]`
→ `out/anchor_boundary.json`. **라이브 콜 0. DB 는 `mode=ro`. 홀드아웃 열람 없음.**
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

from tossmon.analysis import baselines as B
from tossmon.analysis import features as F
from tossmon.analysis import labeling as L
from tossmon.api.models import UsMarketDay

from .cutoff_tautology import (DB, MIN_MS, OUT_DIR, TRAIN_END_MS, calendar_fit,
                               calendar_from_daily, load_bars, pick_symbols, ro)

def _fin(v) -> bool:
    """NaN 이 아니면 True. (`float('nan') != float('nan')`)"""
    return v == v


def _day_start_is_from_calendar(day_lo: int, calendar: list[UsMarketDay]) -> bool:
    """`_locate_day_start` 가 캘린더에서 찾았는가, UTC 자정 폴백인가.

    폴백이면 매매일 경계가 겨울(EST)에 틀린다(M-3). 앵커가 갈린 건이 실은 폴백 탓일
    가능성을 지우기 위해 함께 기록한다.
    """
    return any(start == day_lo for start, _end, _d in F.market_day_spans(calendar))


# --------------------------------------------------------------------------- #
# 관측
# --------------------------------------------------------------------------- #
def measure_symbol(df: pd.DataFrame, calendar: list[UsMarketDay]) -> dict:
    """한 심볼: 게이트를 돌리고, 이벤트마다 두 모드의 **매매일 앵커**를 나란히 놓는다."""
    curve = B.minute_of_session_volume_curve(df, calendar)
    if curve is None or curve.empty:
        return {"events": 0, "note": "curve empty"}
    rv_full = B.rvol_series(df, curve, calendar=calendar)

    ev = L.detect_events(df, L.EventParams(), calendar=calendar, rvol_series=rv_full,
                         max_per_day=1)
    if ev.empty:
        return {"events": 0}

    recs = []
    for _i, e in ev.iterrows():
        t0 = int(e["t0_ms"])
        obs = F.extract_precursor_features(df, None, t0, curve=curve, calendar=calendar,
                                           symbol=str(e["symbol"]), include_t0=True)
        strict = F.extract_precursor_features(df, None, t0, curve=curve,
                                              calendar=calendar,
                                              symbol=str(e["symbol"]), include_t0=False)
        cut_obs, cut_str = int(obs["cutoff_ms"]), int(strict["cutoff_ms"])
        # ★ 이 모듈이 더하는 것: `vol_surge_lead_min` 이 기대는 **매매일** 앵커.
        day_obs = F._locate_day_start(cut_obs, calendar)
        day_str = F._locate_day_start(cut_str, calendar)
        # 대안 설명을 지우기 위한 동반 관측 (세션 앵커는 vol_surge 가 쓰지 않는다)
        ss_obs = F._locate_session_start(cut_obs, curve, calendar)
        ss_str = F._locate_session_start(cut_str, curve, calendar)
        rec = {
            "symbol": str(e["symbol"]),
            "t0_ms": t0,
            "cutoff_obs": cut_obs,
            "cutoff_strict": cut_str,
            # 공백이 있으면 strict 컷오프가 t0−1분보다 더 앞이다 — 꼬리가 1봉이 아니다
            "cutoff_gap_min": (t0 - cut_str) // MIN_MS,
            "day_lo_obs": day_obs,
            "day_lo_strict": day_str,
            "mode_changed_day": day_obs != day_str,
            "day_lo_obs_from_calendar": _day_start_is_from_calendar(day_obs, calendar),
            "day_lo_strict_from_calendar": _day_start_is_from_calendar(day_str, calendar),
            "sess_start_obs": ss_obs,
            "sess_start_strict": ss_str,
            "mode_changed_session": ss_obs != ss_str,
            # 스캔 가능한 분 수. `_first_bar_vol_z_cross` 는 `lo = day_lo − 240분` 부터
            # 창을 잡고 앞 240분을 워밍업으로 소비하므로, 실제로 z 를 재는 구간은
            # `(day_lo, t_hi]` 다. 이 값이 0 이하면 검출이 **원리상 불가능**하다 —
            # 그때 obs 에서만 잡히는 것은 앵커 이동이 아니라 창이 빈 탓이다.
            "scan_min_obs": (cut_obs - day_obs) // MIN_MS,
            "scan_min_strict": (cut_str - day_str) // MIN_MS,
            "strict_warmup_short": cut_str <= day_str,
            "vol_surge_obs": float(obs["vol_surge_lead_min"]),
            "vol_surge_strict": float(strict["vol_surge_lead_min"]),
            # 새 검출은 리드 0 에서만 생긴다(§4-0) — 그런데 리드 0 이 **하나도** 안 생기면
            # 두 설명이 갈린다: (가) T0 봉이 z ≥ 3 을 안 넘었다 (나) obs 창이 T0 봉을
            # 애초에 안 봤다(= 코드 결함). 둘을 가르는 관측이 이것이다.
            # `day_lo = t0 − 1분` 으로 부르면 스캔이 **T0 슬롯 하나**만 돌고, 그때의
            # 베이스라인은 실제 스캔이 그 슬롯에서 쓰는 직전 240분과 **같은 창**이다.
            "t0_bar_crosses_z3": F._first_bar_vol_z_cross(
                F.cut_frame(df, t0), t0 - MIN_MS, t0, F.BAR_VOL_Z_SURGE) is not None,
            "n_bars_pre_obs": float(obs["n_bars_pre"]),
            "n_bars_pre_strict": float(strict["n_bars_pre"]),
        }
        # 부수 관측: 세션 앵커 지표의 **리드 값** 동일성. `cutoff_tautology` 는 검출
        # 여부만 셌고 따름정리의 후반부("리드 값까지 동일")는 아무도 세지 않았다.
        for thr in F.RVOL_CROSS_THRESHOLDS:
            k = f"rvol_first_cross_{thr:g}_lead_min"
            rec[f"cross_obs_{thr:g}"] = float(obs[k])
            rec[f"cross_strict_{thr:g}"] = float(strict[k])
        recs.append(rec)
    return {"events": len(recs), "rows": recs}


# --------------------------------------------------------------------------- #
# 집계
# --------------------------------------------------------------------------- #
def recovery_block(rows: list[dict], obs_key: str, strict_key: str,
                   anchor_key: str) -> dict:
    """한 지표의 따름정리 성립 여부 + 반증 관측.

    따름정리는 두 개의 주장이다. **둘 다 센다.**
      (1) 검출 항등: {`obs_le` 리드 ≥ 1} ≡ {`strict_lt` 검출}
      (2) 리드 값 동일: 그 교집합에서 두 리드가 같은 수

    `anchor_key` 는 이 지표가 기대는 앵커가 모드에 따라 바뀌었는지의 플래그 이름이다
    (`mode_changed_day` / `mode_changed_session`). 깨진 건이 전부 그 앵커가 바뀐
    건이면 앵커 가설이 지지되고, **하나라도 앵커가 같은데 깨졌으면 가설은 틀렸다.**
    """
    n = len(rows)
    det_o = sum(1 for r in rows if _fin(r[obs_key]))
    det_s = sum(1 for r in rows if _fin(r[strict_key]))
    lead0 = sum(1 for r in rows if _fin(r[obs_key]) and r[obs_key] == 0.0)
    ge1 = sum(1 for r in rows if _fin(r[obs_key]) and r[obs_key] >= 1.0)

    # (1) 검출 항등의 반례 — 이벤트별 **대칭차**라 양방향을 다 센다.
    broken_det = [r for r in rows
                  if (_fin(r[obs_key]) and r[obs_key] >= 1.0) != _fin(r[strict_key])]
    # (2) 리드 값의 반례 — 양쪽 다 있는데 값이 다른 건.
    both = [r for r in rows
            if _fin(r[obs_key]) and r[obs_key] >= 1.0 and _fin(r[strict_key])]
    broken_lead = [r for r in both if r[obs_key] != r[strict_key]]

    def _diag(bs: list[dict]) -> dict:
        return {
            "n": len(bs),
            "all_anchor_changed": all(r[anchor_key] for r in bs) if bs else None,
            "same_anchor_n": sum(1 for r in bs if not r[anchor_key]),
            # 대안 설명들 — 깨진 건에서 이것들이 켜져 있으면 앵커만으로 설명한 것이 아니다
            "also_changed_session_n": sum(1 for r in bs if r["mode_changed_session"]),
            "warmup_short_n": sum(1 for r in bs if r["strict_warmup_short"]),
            "gap_gt_1min_n": sum(1 for r in bs if r["cutoff_gap_min"] > 1),
            "t0_ms": [r["t0_ms"] for r in bs],
        }

    return {
        "detect_obs_le": det_o, "detect_rate_obs_le": round(det_o / n, 6) if n else None,
        "detect_strict_lt": det_s,
        "detect_rate_strict_lt": round(det_s / n, 6) if n else None,
        "lead_zero_obs_le": lead0,
        "lead_ge1_obs_le": ge1,
        "corollary_holds": (not broken_det) and (not broken_lead),
        "detect_identity_broken": _diag(broken_det),
        "lead_value_broken": _diag(broken_lead),
        "both_detected_n": len(both),
        "lead_equal_n": len(both) - len(broken_lead),
    }


def summarise(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"events": 0}
    changed_day = [r for r in rows if r["mode_changed_day"]]
    out = {
        "events": n,
        # ★ 요구 #1 의 앞쪽 절반: 매매일 앵커가 두 모드에서 같은 값을 돌려주는가
        "mode_changed_day_n": len(changed_day),
        "mode_changed_day_t0_ms": [r["t0_ms"] for r in changed_day],
        "mode_changed_session_n": sum(1 for r in rows if r["mode_changed_session"]),
        "day_lo_fallback_n": sum(1 for r in rows
                                 if not (r["day_lo_obs_from_calendar"]
                                         and r["day_lo_strict_from_calendar"])),
        "cutoff_gap_gt_1min_n": sum(1 for r in rows if r["cutoff_gap_min"] > 1),
        "strict_warmup_short_n": sum(1 for r in rows if r["strict_warmup_short"]),
        "cutoff_obs_is_t0_n": sum(1 for r in rows if r["cutoff_obs"] == r["t0_ms"]),
        # 리드 0 이 안 생긴 이유를 가르는 관측 (모듈 docstring 참조)
        "t0_bar_crosses_z3_n": sum(1 for r in rows if r["t0_bar_crosses_z3"]),
        # ★ 요구 #1 의 뒤쪽 절반: 그래서 따름정리가 이 지표에서 성립하는가
        "vol_surge_lead_min": recovery_block(rows, "vol_surge_obs", "vol_surge_strict",
                                             "mode_changed_day"),
        # 부수: 세션 앵커 지표의 리드 값 동일성 (검출 여부는 cutoff_tautology 소관)
        "rvol_first_cross": {
            f"thr_{thr:g}": recovery_block(rows, f"cross_obs_{thr:g}",
                                           f"cross_strict_{thr:g}",
                                           "mode_changed_session")
            for thr in F.RVOL_CROSS_THRESHOLDS
        },
    }
    return out


# --------------------------------------------------------------------------- #
def main(argv: list[str]) -> int:
    db = Path(argv[1]) if len(argv) > 1 else DB
    n_sym = int(argv[2]) if len(argv) > 2 else 12
    conn = ro(db)
    try:
        calendar = calendar_from_daily(conn, upto_ms=TRAIN_END_MS)
        syms = pick_symbols(conn, n_sym)
        print(f"calendar days={len(calendar)}  symbols={len(syms)}")
        all_rows: list[dict] = []
        fit = None
        per_symbol = {}
        for k, sym in enumerate(syms, 1):
            df = load_bars(conn, sym)
            if fit is None:
                fit = calendar_fit(df, calendar)
                print(f"calendar_fit (first symbol): {fit}")
            r = measure_symbol(df, calendar)
            per_symbol[sym] = r.get("events", 0)
            all_rows.extend(r.get("rows", []))
            print(f"[{k}/{len(syms)}] {sym:8s} bars={len(df):7d} events={r.get('events')}"
                  f"  running_total={len(all_rows)}")
    finally:
        conn.close()

    res = {"db": str(db), "train_end_ms": TRAIN_END_MS, "symbols": syms,
           "per_symbol_events": per_symbol, "calendar_fit": fit,
           "summary": summarise(all_rows), "rows": all_rows}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p = OUT_DIR / "anchor_boundary.json"
    p.write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps(res["summary"], indent=1))
    print(f"-> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

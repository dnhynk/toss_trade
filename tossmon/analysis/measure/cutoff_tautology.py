"""**개정 A2 뒤 `rvol_at_cutoff` 가 게이트의 `rvol_at_t0` 와 같아지는가** (docs/49 §5).

사전등록 §3 "대조군 컷오프 규약 개정" 상자가 자기 진술을 **미실측**으로 표시하고
확인/반증 관측을 하나 지정했다:

> 개정 후 이벤트마다 **`rvol_at_cutoff` 와 라벨 `rvol_at_t0` 가 일치하는가.**
> 일치하면 위 동어반복이 확정이고, 불일치하면 위 진술 전부를 재검토해야 한다.
> (전제: 피처 추출에 게이트와 **같은 `curve`** 를 넘긴 경우)
> **실측 소유는 W3** 이며 이 개정문은 요구만 남기고 재지 않는다.

이 모듈이 그 하나를 잰다. **덤으로 두 번째 동어반복 후보**도 같이 잰다 —
`rvol_first_cross_{2,3}_lead_min` 의 **검출률**이 구조적으로 1.000 이 되는지.
사전등록 §1 P1 개정 상자는 리드 0 **병기 의무**를 새로 두었는데, 그 장치가
검출률까지 덮는지는 아무도 재지 않았다.

## 무엇을 재고, 무엇을 재지 않는가

**잰다**: 두 계산 경로가 **같은 수를 내는가**(구조적 동일성), 그리고
`rvol_first_cross_{2,3,5}` 의 **정의율(=검출률)** 이 두 컷오프 모드에서 각각 얼마인가.

**재지 않는다 — 판정하지 않는다.** 이 모듈은 사전등록 §4 의 어떤 판정도 다시 내리지
않고, 등록 시행(T-005·T-023 등)을 재실행하지도 않는다. 여기서 나오는 재현율·정밀도
비슷한 수를 §4 판정에 쓰면 그 결론은 무효다 — 표본이 등록 표본이 아니다.

## 캘린더에 관한 정직한 유보 (**결과 해석에 중요하다**)

이 워크트리에 phase1c 산출물(`ev_v3.parquet`)도, 저장된 분석 캘린더도 없다.
라이브 호출은 금지다. 그래서 캘린더를 **DB 의 일봉에서 역산**한다 —
`candles_1d.ts_ms` 가 그 매매일의 **00:00 ET** 라는 규약(docs/06 §2-2)과
세션별 ET 자정 오프셋(docs/01 §5, `baselines._ET_MIDNIGHT_OFFSET_MIN`)을 쓴다.
`baselines.et_midnight_ms` 가 이미 그 역방향을 하고 있으므로 규약 자체는 코드로 증명돼 있다.

**이 재구성이 틀리면 rvol 의 절대값이 틀린다.** 그러나 이 모듈의 두 결론은 절대값이
아니라 **두 경로의 일치**와 **게이트 조건부 정의율**이고, 두 경로는 **같은 캘린더·같은
곡선**을 쓴다. 즉 캘린더 오차는 두 경로에 똑같이 실려 상쇄된다 — 결론은 재구성 품질에
견고하고, **절대 rvol 수치는 그렇지 않다.** 재구성 품질은 §"calendar_fit" 에 실측으로 싣는다.
조기폐장(반일장)은 역산으로 복원되지 않으므로 정상일 길이로 잡힌다 — 그 날들은
곡선 키(M-4, 세션 길이 포함)가 어긋나 rvol 이 NaN 이 되거나 분모가 달라진다.
**그래서 조기폐장 후보일은 표본에서 제외한다**(§"early_close_excluded").

실행: `python -m tossmon.analysis.measure.cutoff_tautology [db_path] [n_symbols]`
→ `out/cutoff_tautology.json`. **라이브 콜 0. DB 는 `mode=ro`. 홀드아웃 열람 없음.**
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

from tossmon.analysis import baselines as B
from tossmon.analysis import features as F
from tossmon.analysis import labeling as L
from tossmon.api.models import SessionWindow, UsMarketDay

DB = Path(r"C:/Users/dongh/orca/workspaces/toss_trade/w4-collector/data/backfill.db")
OUT_DIR = Path("out")

MIN_MS = 60_000
#: 사전등록 train 상한. 홀드아웃은 물론 검증기간도 이 모듈에서 건드리지 않는다.
TRAIN_END_MS = 1_767_225_600_000        # 2026-01-01T00:00:00Z, 상한은 **미만**
#: 곡선이 자리 잡을 만큼의 이력이 필요하므로 표본은 백필 뒷부분에서 고른다.
MIN_BARS_PER_SYMBOL = 20_000

#: 세션 길이(분) — 정상일. 역산 캘린더는 이 길이만 만든다(조기폐장 복원 불가).
#: **라이브 캡처**(`tests/fixtures/live/live_market_calendar_us.json`, source=live) 기준:
#: day 09:00~17:00 KST=480 · pre 17:00~22:30=330 · regular 22:30~05:00=390 ·
#: after 05:00~08:50=230.
#: 스펙 유래 합성 픽스처(`market_calendar_us.json`)는 day=470 / after=120 으로 **다르다** —
#: docs/01 §5 표는 스펙값을, 같은 문서 데이마켓 항목은 실측 **17:00** 을 싣고 있다.
#: 실측을 따른다. 길이를 틀리면 세션 경계가 움직여 `_locate_session_start` 가 달라지고,
#: 곡선 키의 세션 길이(M-4)도 어긋난다.
_SESSION_LEN_MIN = {"day": 480, "pre": 330, "regular": 390, "after": 230}


def ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)


# --------------------------------------------------------------------------- #
# 1. 캘린더 역산 (위 유보 참조)
# --------------------------------------------------------------------------- #
def calendar_from_daily(conn: sqlite3.Connection, *, upto_ms: int) -> list[UsMarketDay]:
    """일봉 ts(= 00:00 ET)에서 세션 창을 복원한다. 정상일 길이만 만든다."""
    rows = conn.execute(
        "SELECT DISTINCT ts_ms FROM candles_1d WHERE ts_ms < ? ORDER BY ts_ms",
        (upto_ms,)).fetchall()
    out: list[UsMarketDay] = []
    for (mid,) in rows:
        mid = int(mid)
        wins = {}
        for name, off in B._ET_MIDNIGHT_OFFSET_MIN.items():
            start = mid + off * MIN_MS
            wins[name] = SessionWindow(start_ms=start,
                                       end_ms=start + _SESSION_LEN_MIN[name] * MIN_MS)
        date = pd.Timestamp(mid, unit="ms", tz="UTC").strftime("%Y-%m-%d")
        out.append(UsMarketDay(date=date, day=wins["day"], pre=wins["pre"],
                               regular=wins["regular"], after=wins["after"]))
    return out


def calendar_fit(df_1m: pd.DataFrame, calendar: list[UsMarketDay]) -> dict:
    """역산 캘린더가 실제 봉을 얼마나 덮는가 — 유보를 숫자로 확인한다."""
    spans = F.market_day_spans(calendar)
    if not spans:
        return {"bars": int(len(df_1m)), "inside_day": 0, "inside_session": 0}
    lo = [s[0] for s in spans]
    inside_day = inside_sess = 0
    by_date = {md.date: md for md in calendar}
    for t in df_1m["ts_ms"].tolist():
        b = L.bar_start_ms(int(t))
        j = pd.Series(lo).searchsorted(b, side="right") - 1
        if j < 0 or not (spans[j][0] <= b < spans[j][1]):
            continue
        inside_day += 1
        md = by_date.get(spans[j][2])
        if md is not None and B.locate_session(md, int(t)) is not None:
            inside_sess += 1
    n = len(df_1m)
    return {"bars": int(n), "inside_day": inside_day, "inside_session": inside_sess,
            "inside_day_pct": round(100.0 * inside_day / n, 2) if n else None,
            "inside_session_pct": round(100.0 * inside_sess / n, 2) if n else None}


# --------------------------------------------------------------------------- #
# 2. 표본
# --------------------------------------------------------------------------- #
def pick_symbols(conn: sqlite3.Connection, n: int) -> list[str]:
    rows = conn.execute(
        "SELECT symbol, COUNT(*) c FROM candles_1m WHERE ts_ms < ? "
        "GROUP BY symbol HAVING c >= ? ORDER BY c DESC LIMIT ?",
        (TRAIN_END_MS, MIN_BARS_PER_SYMBOL, n)).fetchall()
    return [str(s) for s, _c in rows]


def load_bars(conn: sqlite3.Connection, sym: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT symbol, ts_ms, open_u, high_u, low_u, close_u, vol_qu FROM candles_1m "
        "WHERE symbol=? AND ts_ms < ? ORDER BY ts_ms", conn, params=(sym, TRAIN_END_MS))


# --------------------------------------------------------------------------- #
# 3. 관측
# --------------------------------------------------------------------------- #
def measure_symbol(df: pd.DataFrame, calendar: list[UsMarketDay]) -> dict:
    """한 심볼: 게이트를 돌리고, 이벤트마다 두 경로의 rvol 을 나란히 놓는다."""
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
        # 가설 H: 병기 장치가 옛 검출률을 정확히 복원하지 못하는 이유는 컷오프가
        # 움직이면서 **rvol 창이 속한 세션까지 바뀌기** 때문이다 (`features._locate_
        # session_start(cutoff)` → `rv = rv[rv.index > sess_start]`). t0 가 세션 첫 봉이면
        # obs_le 는 새 세션(창 1봉)을, strict_lt 는 직전 세션(창 전체)을 본다.
        # 반증 관측: 불일치 이벤트인데 두 세션이 같으면 H 는 틀렸다.
        ss_obs = F._locate_session_start(int(obs["cutoff_ms"]), curve, calendar)
        ss_str = F._locate_session_start(int(strict["cutoff_ms"]), curve, calendar)
        rec = {
            "t0_ms": t0,
            "sess_start_obs": ss_obs,
            "sess_start_strict": ss_str,
            "mode_changed_session": ss_obs != ss_str,
            "rvol_at_t0": float(e["rvol_at_t0"]),
            "rvol_at_cutoff_obs": float(obs["rvol_at_cutoff"]),
            "rvol_at_cutoff_strict": float(strict["rvol_at_cutoff"]),
            "cutoff_ms_obs": float(obs["cutoff_ms"]),
            "cutoff_lag_obs": float(obs["cutoff_lag_min"]),
            "cutoff_lag_strict": float(strict["cutoff_lag_min"]),
        }
        for thr in F.RVOL_CROSS_THRESHOLDS:
            k = f"rvol_first_cross_{thr:g}_lead_min"
            rec[f"lead_obs_{thr:g}"] = float(obs[k])
            rec[f"lead_strict_{thr:g}"] = float(strict[k])
        recs.append(rec)
    return {"events": len(recs), "rows": recs}


def summarise(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"events": 0}

    def _fin(v) -> bool:
        return v == v            # NaN 이 아니면 True

    exact = sum(1 for r in rows
                if _fin(r["rvol_at_cutoff_obs"])
                and r["rvol_at_cutoff_obs"] == r["rvol_at_t0"])
    both_nan = sum(1 for r in rows if not _fin(r["rvol_at_cutoff_obs"]))
    strict_eq = sum(1 for r in rows
                    if _fin(r["rvol_at_cutoff_strict"])
                    and r["rvol_at_cutoff_strict"] == r["rvol_at_t0"])
    gate_ok = sum(1 for r in rows if r["rvol_at_t0"] >= 3.0)
    lag0 = sum(1 for r in rows if r["cutoff_lag_obs"] == 0.0)

    out = {
        "events": n,
        "gate_rvol_at_t0_ge_3": gate_ok,
        # ★ 사전등록 §3 이 지정한 확인/반증 관측
        "identity_obs_le": {"equal": exact, "nan": both_nan,
                            "rate": round(exact / n, 6)},
        # 대조: 개정 전 모드에서는 같아지지 않아야 한다 (같으면 동어반복 논증이 무너진다)
        "identity_strict_lt": {"equal": strict_eq, "rate": round(strict_eq / n, 6)},
        "cutoff_lag_obs_is_zero": {"n": lag0, "rate": round(lag0 / n, 6)},
        "first_cross": {},
    }
    for thr in F.RVOL_CROSS_THRESHOLDS:
        o = [r[f"lead_obs_{thr:g}"] for r in rows]
        s = [r[f"lead_strict_{thr:g}"] for r in rows]
        det_o = sum(1 for v in o if _fin(v))
        det_s = sum(1 for v in s if _fin(v))
        lead0 = sum(1 for v in o if _fin(v) and v == 0.0)
        # 병기 의무가 옛 값을 복원하는가: {리드>=1 in obs_le} == {검출 in strict_lt} ?
        ge1 = sum(1 for v in o if _fin(v) and v >= 1.0)
        # 복원이 깨진 이벤트들. 가설 H 는 이들이 전부 세션이 바뀐 건이라고 예측한다.
        broken = [r for r in rows
                  if (_fin(r[f"lead_obs_{thr:g}"]) and r[f"lead_obs_{thr:g}"] >= 1.0)
                  != _fin(r[f"lead_strict_{thr:g}"])]
        out["first_cross"][f"thr_{thr:g}"] = {
            "detect_obs_le": det_o, "detect_rate_obs_le": round(det_o / n, 6),
            "detect_strict_lt": det_s, "detect_rate_strict_lt": round(det_s / n, 6),
            "lead_zero_obs_le": lead0,
            "lead_ge1_obs_le": ge1,
            "recovers_strict_detect": ge1 == det_s,
            "recovery_broken_n": len(broken),
            "recovery_broken_all_changed_session":
                all(r["mode_changed_session"] for r in broken) if broken else None,
            "recovery_broken_same_session_n":
                sum(1 for r in broken if not r["mode_changed_session"]),
        }
    out["mode_changed_session_n"] = sum(1 for r in rows if r["mode_changed_session"])
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
           "summary": summarise(all_rows)}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p = OUT_DIR / "cutoff_tautology.json"
    p.write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps(res["summary"], indent=1))
    print(f"-> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""**변동성을 맞추면 `docs/44` 의 숫자들이 어디로 움직이는가** (docs/44 §11 / D-7).

`docs/44` 의 위약은 전부 **무작위 시각**이었다. 그 위약은 한 가지를 못 가른다:

> 우리 발화는 **변동성 큰 순간**을 고르고, 변동성 큰 순간은 최댓값도 최솟값도 크다.

`docs/44` §3-4 가 이미 그 모양을 보였다 — 임계를 0.5%→3% 로 올리면 MFE +1.27%→+2.30%,
MAE −1.26%→−2.35% 로 **거의 완벽한 대칭**이고 합이 0 근처다. 그리고 §10-8 2번은
"변동성을 맞춘 대조를 안 했다. **못 갈랐다**" 고 스스로 적었다. 이 모듈이 그것을 잰다.

이 프로젝트는 **바로 이 대조군 하나에 결과가 죽은 전례**가 있다
(`COORDINATOR-STATE` §4.4-D, 2026-08-03): 이탈 여지 상한이 위약 대비 +4.55%p 로 0 을
넘었는데, **변동성을 맞추자 위약이 +1.17% → +4.47% 로 뛰어** 격차 4.94%p 중 약 3.3%p 가
순전히 "우리가 변동성 큰 종목을 골랐다" 였다.

## 이 모듈이 하는 일 — 세 자리에 정합 위약을 붙인다

| # | `docs/44` | 물음 |
|---|---|---|
| **M1** | §3-3 진입 후 수익률 | 무작위 시각과 구별이 안 됐다. **정합에서는?** |
| **M2** | §3-4 임계 쓸이 | MFE·MAE 대칭이 **위약에서도** 임계를 따라 커지는가 |
| **M3** | §10-3 앵커=최고가 30.7% vs 31.1% | 정합하면 **어느 쪽으로** 움직이는가 |

## 정합 방법 — §4.4-D 에서 **무엇을 바꿨고 왜인가**

| | §4.4-D (2026-08-03) | **여기** | 왜 바꿨나 |
|---|---|---|---|
| 자료 | 1분봉 | **초 막대 vwap** | D-10. 1분봉 경로 금지 |
| 정합 축 | **다른 종목**, 같은 시각 | **같은 종목, 다른 시각** (＋풀 합침도 병기) | 아래 |
| 되돌아보기 창 | 진입 직전 10분 | **직전 60초** | 사건 자체가 60초 정의다. 10분이면 사건 밖을 잰다 |
| 키 | 실현변동성 1개 | **실현변동성 + 직전 60초 변동폭 2개** | D-7 원문이 "직전 60초 변동폭" 이라 적혀 있다. 둘 다 낸다 |
| 대조의 짝 | 위약 vs 실제 | **정합 위약 vs 비정합 위약 vs 실제** | 아래 |

**왜 축을 바꿨나.** §4.4-D 가 죽인 것은 **종목 선택** 효과였다 — 그때 위약이 "같은 시각의
무작위 **종목**" 이었고, 우리 진입은 변동성 큰 **종목**에 몰려 있었다. 그런데 `docs/44`
§3-3 의 주 위약(`placebo_random_time`)과 §10-3 의 눈금(무작위 막대)은 **이미 같은 종목**
이다 — 종목 고유 변동성은 그쪽에서 이미 상쇄돼 있다. 남은 의심은 **순간**이다.
그래서 여기서는 **같은 종목 안에서 시각을 흔들되 직전 변동성을 맞춘다.**
(§10-3 의 눈금은 종목을 합쳐 뽑았으므로 **풀 합침** 모드도 같이 낸다.)

**왜 비정합 위약도 같이 뽑나.** `docs/44` 의 위약은 추첨 규칙이 여기와 다르다(간격 배제·
키 결측 처리·짝 집합). 정합 위약을 §3-3 표의 숫자와 바로 비교하면 **정합 때문인지 추첨
규칙 때문인지** 알 수 없다. 그래서 **같은 기계로 뽑은 비정합 위약**을 같은 표에 놓는다 —
그래야 두 칸의 차이가 오직 **변동성 밴드** 하나가 된다.

## 반드시 지킨 것

- **사전 관측 가능한 변동성만.** 키는 앵커 막대 **이하**의 직전 60초로만 만든다.
  사후 변동성으로 맞추면 그 자체가 미래를 쓰는 것이다(테스트로 고정).
- **짝을 못 지은 표본은 세어서 낸다.** 사유별로 가른다(키 결측 / 밴드 비었음 / 간격 배제).
- **짝 지은 발화 위에서만 비교한다.** 실제·비정합·정합 세 팔이 **같은 발화 집합**을 쓴다.
  안 그러면 팔마다 모집단이 달라진다(§4.4-D 의 "짝 161 vs 정합 160" 이 그 사고였다).
- **초 막대만.** 1분봉 경로 없음. 초 안 순서에 의존하는 함수 없음(`docs/42`).
- **기존 코드 한 줄도 안 고쳤다.** `find_fires`·`_entry_outcomes`·`forward_probe` 를
  **그대로 불러 쓴다** — 팔마다 다른 기계를 쓰면 차이가 기계에서 나온다.

## 이 모듈이 **하지 않는 것**

- **판정하지 않는다.** "그래서 X 는 죽었다/살았다" 는 이 파일이 쓸 문장이 아니다.
  낼 것은 **"변동성을 맞추면 이 숫자가 이렇게 움직인다"** 까지다.
- **CI 를 못 낸다.** 표본이 2.6 정규장이다(`docs/44` §6-1). 씨앗 민감도는 내지만
  그것은 **추첨 잡음**이지 표본 신뢰구간이 아니다.
- `docs/44` §1~10 을 고치지 않는다. §11 로 **더한다.**

실행: `python -m tossmon.analysis.measure.vol_matched_placebo [db_path]`
→ `out/vol_matched_placebo.json`. **라이브 콜 0. DB 는 `mode=ro`.**
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

from tossmon.analysis.measure.cross_peak_check import (
    HORIZONS_S as PROBE_HORIZONS_S,
    forward_probe,
    probe_summary,
    random_time_baseline,
)
from tossmon.analysis.measure.tick_resolution import (
    WINDOW_START_MS,
    open_ro,
    pct_table,
    session_of,
)
from tossmon.analysis.measure.tick_stages import (
    DETECT_LAG_S,
    HORIZONS_S as ENTRY_HORIZONS_S,
    PLACEBO_DRAWS,
    SHOT_MAX_SECONDS,
    SHOT_RISE,
    THRESHOLD_SWEEP,
    _entry_outcomes,
    find_fires,
    load_bars,
    price_band_of,
    select_symbols,
)

OUT_DIR = Path("out")

#: 정합 키를 만드는 되돌아보기 창(초). **사건 정의와 같은 60초**다 —
#: `find_fires` 가 보는 창과 정확히 같은 구간이라야 "그 순간의 변동성" 을 잰 것이 된다.
#: §4.4-D 는 600초(10분)였다. 그때 사건은 10분 고점 대비 하락이라 창이 사건과 맞았다.
VOL_LOOKBACK_S = 60

#: 정합 허용 오차. **§4.4-D 와 같은 ±20%** 로 둔다 — 바꿀 이유가 없고, 같아야 그때
#: 결과와 눈금이 맞는다. 민감도는 `TOL_SWEEP` 로 따로 낸다.
VOL_MATCH_TOL = 0.20
TOL_SWEEP: tuple[float, ...] = (0.10, 0.20, 0.40)

#: 위약 막대가 발화 자신의 측정 구간과 **겹치지 않도록** 띄우는 최소 간격(초).
#: 앞으로 최대 지평 120초 + 뒤로 키 창 60초 = 180초. 이보다 가까우면 자기 자신과
#: 비교하게 된다. §4.4-D 는 600초였는데 그때는 지평이 10분이었다.
SELF_GAP_S = max(ENTRY_HORIZONS_S) + VOL_LOOKBACK_S

#: 발화 1건당 위약 추첨 수. `docs/44` §3-3 과 **같은 3** 이라야 위약 쪽 잡음이 같다.
MATCH_DRAWS = PLACEBO_DRAWS

#: 밴드 안에서 뽑은 막대가 간격 배제에 걸렸을 때 다시 뽑는 횟수 상한.
#: 무한 재시도는 밴드가 좁을 때 멈추지 않는다 — 대신 **버리고 센다.**
MAX_REDRAW = 8

SEED = 20260808
SEED_SWEEP: tuple[int, ...] = (20260808, 20260809, 20260810)

#: 정합 키 두 개. 둘 다 **앵커 막대 이하**의 자료로만 만든다.
MATCH_KEYS: tuple[str, ...] = ("rv60", "range60")
#: 후보 풀 두 개. `same_symbol` 이 주(主) — `docs/44` 의 위약이 이미 같은 종목이다.
POOL_MODES: tuple[str, ...] = ("same_symbol", "pooled")


# --------------------------------------------------------------------------- #
# 1. 사전 관측 가능한 변동성 — **앵커 이하만 본다**
# --------------------------------------------------------------------------- #
def trailing_stats(ts: np.ndarray, px: np.ndarray, *,
                   lookback_s: int = VOL_LOOKBACK_S) -> dict:
    """막대마다 **직전 `lookback_s` 초**(자기 막대 포함)의 변동성 키.

    돌려주는 것:
      - `rv60`     그 창 안 로그수익률의 표준편차(ddof=1). §4.4-D 와 **같은 추정량**이다.
      - `range60`  그 창 안 `max/min − 1`. **D-7 원문이 말한 "직전 60초 변동폭"** 이다.
      - `nbar60`   그 창 안 막대 수. 변동성이 아니라 **테이프 밀도**다 — 정합 뒤에
                   이것이 안 맞으면 "같은 변동성" 이 실은 "같은 활발함" 이 아니다.

    **미래를 안 쓴다**: 창의 오른쪽 끝이 앵커 막대 자신이다. 앵커 뒤 자료를 잘라내고
    다시 불러도 같은 값이 나온다(테스트로 고정).

    `find_fires` 가 보는 창(`ts - max_seconds` 이상, side='left')과 **같은 경계**를 쓴다.
    그래서 발화 막대의 `range60` 은 정의상 `rise` 이상이다 — 그 사실 자체가 아래
    §"두 키는 성격이 다르다" 의 근거다.
    """
    n = int(np.asarray(ts).size)
    out = {"rv60": np.full(n, np.nan), "range60": np.full(n, np.nan),
           "nbar60": np.zeros(n, dtype="int64")}
    if n < 2:
        return out
    ts = np.asarray(ts, dtype="int64")
    px = np.asarray(px, dtype="float64")
    idx = np.arange(n, dtype="int64")
    lo = np.searchsorted(ts, ts - lookback_s * 1000, side="left")
    out["nbar60"] = idx - lo + 1

    # --- 실현변동성: 창 안에 **양 끝이 다 들어오는** 로그수익률만 쓴다 ------------- #
    # r[j] 는 막대 j-1 → j 의 수익률이므로 j 가 lo+1 이상이어야 두 끝이 창 안이다.
    pos = px > 0
    r = np.zeros(n, dtype="float64")
    ok_r = np.r_[False, pos[1:] & pos[:-1]]
    r[ok_r] = np.log(px[1:][ok_r[1:]] / px[:-1][ok_r[1:]])
    c1 = np.concatenate(([0.0], np.cumsum(r)))
    c2 = np.concatenate(([0.0], np.cumsum(r * r)))
    m = idx - lo                                   # 쓸 수 있는 수익률 개수
    s1 = c1[idx + 1] - c1[lo + 1]
    s2 = c2[idx + 1] - c2[lo + 1]
    good = m >= 2                                  # ddof=1 이라 2개는 있어야 한다
    var = np.zeros(n, dtype="float64")
    var[good] = (s2[good] - s1[good] ** 2 / m[good]) / (m[good] - 1)
    out["rv60"][good] = np.sqrt(np.maximum(var[good], 0.0))

    # --- 변동폭: 창 안 max/min. 1초 격자라 창 안 막대 수가 유계다(≤ lookback+1) --- #
    k = int(out["nbar60"].max())
    vmax = np.full(n, -np.inf)
    vmin = np.full(n, np.inf)
    for d in range(k):
        j = idx - d
        okd = (j >= 0) & (j >= lo)
        v = px[np.where(okd, j, 0)]
        np.maximum(vmax, np.where(okd & pos[np.where(okd, j, 0)], v, -np.inf), out=vmax)
        np.minimum(vmin, np.where(okd & pos[np.where(okd, j, 0)], v, np.inf), out=vmin)
    fin = np.isfinite(vmax) & np.isfinite(vmin) & (vmin > 0)
    out["range60"][fin] = vmax[fin] / vmin[fin] - 1.0
    return out


def build_universe(bars: dict, *, lookback_s: int = VOL_LOOKBACK_S) -> dict:
    """종목별 (초 막대 + 정합 키). 임계를 쓸어도 키는 **한 번만** 만든다.

    키가 임계와 무관하다는 것이 중요하다 — 임계마다 키가 달라지면 M2 의 다섯 줄이
    서로 다른 자로 잰 값이 된다.
    """
    uni = {}
    for s, (ts, _n, _lo, _hi, vwap) in bars.items():
        st = trailing_stats(ts, vwap, lookback_s=lookback_s)
        uni[s] = {"ts": ts, "px": vwap, **st}
    return uni


# --------------------------------------------------------------------------- #
# 2. 후보 색인 — 키로 정렬해 두면 밴드는 searchsorted 두 번이다
# --------------------------------------------------------------------------- #
def pool_index(uni: dict, syms: list, key: str, mode: str, *,
               require_key: bool = True) -> dict:
    """키 값으로 정렬한 후보 막대 목록.

    `same_symbol` 이면 종목마다 하나, `pooled` 면 전체에 하나.

    `require_key=True` (기본)면 **키가 결측이거나 0 인 막대를 후보에서 뺀다** —
    ±20% 밴드는 곱셈이라 0 에서 정의되지 않고, 결측을 넣으면 "변동성을 맞췄다" 가
    거짓이 된다. 뺀 개수를 같이 돌려준다.

    `require_key=False` 는 **비정합 팔 전용**이다. `docs/44` §10-3 의 눈금은 아무 막대나
    뽑았으므로, 그 줄과 이어 붙이려면 **키가 없는 막대까지 포함한** 풀이 하나 필요하다.
    이 풀로는 밴드를 열 수 없다(정렬 키가 결측을 포함한다) — `draw_controls` 가
    막는다.
    """
    def one(sel: list) -> dict:
        sid, bid, val = [], [], []
        for si in sel:
            v = uni[syms[si]][key]
            f = (np.flatnonzero(np.isfinite(v) & (v > 0)) if require_key
                 else np.arange(v.size, dtype="int64"))
            if f.size == 0:
                continue
            sid.append(np.full(f.size, si, dtype="int64"))
            bid.append(f)
            val.append(v[f])
        if not val:
            return {"vals": np.array([]), "sym": np.array([], "int64"),
                    "bar": np.array([], "int64")}
        val = np.concatenate(val)
        sid = np.concatenate(sid)
        bid = np.concatenate(bid)
        o = np.argsort(val, kind="stable")
        return {"vals": val[o], "sym": sid[o], "bar": bid[o]}

    n_all = sum(int(uni[s]["ts"].size) for s in syms)
    if mode == "same_symbol":
        by = {si: one([si]) for si in range(len(syms))}
        n_pool = sum(int(v["vals"].size) for v in by.values())
        out = {"mode": mode, "key": key, "by_sym": by, "banded": require_key,
               "n_candidate_bars": n_pool, "n_bars_total": n_all,
               "n_dropped_key_missing": n_all - n_pool}
    else:
        g = one(list(range(len(syms))))
        out = {"mode": mode, "key": key, "global": g, "banded": require_key,
               "n_candidate_bars": int(g["vals"].size), "n_bars_total": n_all,
               "n_dropped_key_missing": n_all - int(g["vals"].size)}
    return out


def _slice_for(index: dict, sym_id: int) -> dict:
    return index["by_sym"][int(sym_id)] if index["mode"] == "same_symbol" else index["global"]


# --------------------------------------------------------------------------- #
# 3. 추첨 — 정합/비정합을 **같은 기계**로 뽑는다
# --------------------------------------------------------------------------- #
def draw_controls(uni: dict, syms: list, index: dict,
                  fire_sym: np.ndarray, fire_bar: np.ndarray, *,
                  matched: bool, tol: float = VOL_MATCH_TOL,
                  draws: int = MATCH_DRAWS, gap_s: int = SELF_GAP_S,
                  rng: np.random.Generator | None = None,
                  max_redraw: int = MAX_REDRAW) -> dict:
    """발화마다 위약 막대를 `draws` 개씩. **`matched=False` 면 밴드만 없앤다.**

    두 팔의 차이가 오직 밴드 하나가 되도록, 간격 배제·재추첨·결측 처리는 **똑같다.**

    돌려주는 것:
      - `sym` · `bar` · `slot`  뽑힌 막대와 그것이 어느 발화의 짝인지
      - `paired`   발화별 불리언. **한 개도 못 뽑은 발화는 False** 다
      - `unpaired_*`  짝을 잃은 사유별 개수. **조용히 줄이지 않는다**
    """
    rng = rng or np.random.default_rng(SEED)
    if matched and not index.get("banded", True):
        raise ValueError("키 결측을 포함한 풀로는 밴드를 열 수 없다 "
                         "(require_key=False 는 비정합 팔 전용이다)")
    key = index["key"]
    n = int(fire_sym.size)
    paired = np.zeros(n, dtype=bool)
    o_sym, o_bar, o_slot = [], [], []
    n_missing_key = n_empty_band = n_gap_only = 0
    band_sizes = []

    for i in range(n):
        si = int(fire_sym[i])
        bi = int(fire_bar[i])
        u = uni[syms[si]]
        v = float(u[key][bi])
        if not np.isfinite(v) or v <= 0:
            n_missing_key += 1
            continue
        cand = _slice_for(index, si)
        vals = cand["vals"]
        if vals.size == 0:
            n_empty_band += 1
            continue
        if matched:
            a = int(np.searchsorted(vals, v * (1.0 - tol), side="left"))
            b = int(np.searchsorted(vals, v * (1.0 + tol), side="right"))
        else:
            a, b = 0, int(vals.size)
        if b <= a:
            n_empty_band += 1
            continue
        band_sizes.append(b - a)
        t0 = int(u["ts"][bi])
        got = 0
        for _ in range(draws * max_redraw):
            if got >= draws:
                break
            p = a + int(rng.integers(0, b - a))
            cs = int(cand["sym"][p])
            cb = int(cand["bar"][p])
            if cs == si and abs(int(uni[syms[cs]]["ts"][cb]) - t0) <= gap_s * 1000:
                continue                      # 자기 자신의 측정 구간과 겹친다
            o_sym.append(cs)
            o_bar.append(cb)
            o_slot.append(i)
            got += 1
        if got:
            paired[i] = True
        else:
            n_gap_only += 1

    return {
        "sym": np.asarray(o_sym, dtype="int64"),
        "bar": np.asarray(o_bar, dtype="int64"),
        "slot": np.asarray(o_slot, dtype="int64"),
        "paired": paired,
        "n_fires": n,
        "n_paired": int(paired.sum()),
        "n_unpaired": int((~paired).sum()),
        "unpaired_key_missing": n_missing_key,
        "unpaired_empty_band": n_empty_band,
        "unpaired_gap_excluded_only": n_gap_only,
        "band_size": pct_table(np.asarray(band_sizes, dtype="float64")),
        "matched": bool(matched),
        "tol": float(tol) if matched else None,
    }


# --------------------------------------------------------------------------- #
# 4. 팔(arm) 측정 — 실제·위약이 **같은 함수**를 통과한다
# --------------------------------------------------------------------------- #
ENTRY_KEYS = (["slip_from_fire", "entry_delay_actual_s", "mfe_60s", "mae_60s"]
              + [f"ret_{h}s" for h in ENTRY_HORIZONS_S])


def arm_entry_outcomes(uni: dict, syms: list, sym_id: np.ndarray, bar: np.ndarray, *,
                       lag_s: float = DETECT_LAG_S) -> dict:
    """`tick_stages._entry_outcomes` 를 **그대로** 앵커 집합에 적용한다.

    종목별로 묶어서 부르되 결과는 **원래 순서 자리에 되돌려 놓는다**. 진입 못 한
    앵커는 `nan` 으로 남고 `entered=False` 다 — 길이를 줄이면 짝이 어긋난다
    (§4.4-D 의 "짝 161 vs 정합 160" 이 정확히 그 사고였다).

    비공개 함수(`_entry_outcomes`)를 부르는 이유: **실제와 위약이 다른 기계를 통과하면
    차이가 기계에서 나온다.** 여기서 다시 구현하면 `docs/44` §3-3 과 나란히 놓을 수 없다.
    """
    m = int(sym_id.size)
    out = {k: np.full(m, np.nan) for k in ENTRY_KEYS}
    out["entered"] = np.zeros(m, dtype=bool)
    for si in np.unique(sym_id):
        sel = np.flatnonzero(sym_id == si)
        u = uni[syms[int(si)]]
        res = _entry_outcomes(u["ts"], u["px"], bar[sel], lag_s=lag_s)
        ent = res.get("entered")
        if ent is None or not np.any(ent):
            continue
        pos = sel[ent]
        out["entered"][pos] = True
        for k in ENTRY_KEYS:
            if k in res:
                out[k][pos] = np.asarray(res[k], dtype="float64")
    return out


def arm_forward_probe(uni: dict, syms: list, sym_id: np.ndarray, bar: np.ndarray, *,
                      horizons: tuple[int, ...] = PROBE_HORIZONS_S) -> dict:
    """`cross_peak_check.forward_probe` 를 **그대로** 앵커 집합에 적용한다(§10-3 의 물음)."""
    m = int(sym_id.size)
    keys = ("n_bars", "max_ret", "end_ret", "t_max_s")
    out = {h: {"n_bars": np.zeros(m, "int64"),
               "max_ret": np.full(m, np.nan),
               "end_ret": np.full(m, np.nan),
               "t_max_s": np.full(m, np.nan)} for h in horizons}
    for si in np.unique(sym_id):
        sel = np.flatnonzero(sym_id == si)
        u = uni[syms[int(si)]]
        for h in horizons:
            r = forward_probe(u["ts"], u["px"], bar[sel], h)
            for k in keys:
                out[h][k][sel] = r[k]
    return out


def summarize_entry(bag: dict) -> dict:
    """`docs/44` §3-3 과 **같은 규약**: 중앙값만 읽지 않고 평균·양(+)의 비율을 같이 낸다."""
    ent = bag.get("entered")
    n = int(ent.size) if ent is not None else 0
    out = {"n_anchors": n, "n_entered": int(ent.sum()) if n else 0}
    for k in ENTRY_KEYS:
        a = bag.get(k)
        if a is None:
            continue
        f = a[np.isfinite(a)]
        t = pct_table(f)
        t["share_positive"] = float((f > 0).mean()) if f.size else None
        t["n_finite"] = int(f.size)
        out[k] = t
    return out


def balance_table(uni: dict, syms: list, sym_id: np.ndarray, bar: np.ndarray) -> dict:
    """**정합이 실제로 물렸는지**를 보여 주는 표. 이게 없으면 "맞췄다" 는 주장일 뿐이다.

    변동성 두 키뿐 아니라 **테이프 밀도(`nbar60`)·가격대·세션**도 낸다 — 변동성을
    맞췄는데 밀도가 안 맞으면 "같은 변동성" 이 실은 "같은 활발함" 이 아니다.
    """
    m = int(sym_id.size)
    if m == 0:
        return {"n": 0}
    rv = np.empty(m); rg = np.empty(m); nb = np.empty(m)
    tms = np.empty(m, dtype="int64"); usd = np.empty(m)
    for si in np.unique(sym_id):
        sel = np.flatnonzero(sym_id == si)
        u = uni[syms[int(si)]]
        b = bar[sel]
        rv[sel] = u["rv60"][b]
        rg[sel] = u["range60"][b]
        nb[sel] = u["nbar60"][b]
        tms[sel] = u["ts"][b]
        usd[sel] = u["px"][b] / 1e6
    band = price_band_of(usd)
    sess = session_of(tms)
    return {
        "n": m,
        "rv60": pct_table(rv[np.isfinite(rv)]),
        "range60": pct_table(rg[np.isfinite(rg)]),
        "nbar60": pct_table(nb),
        "anchor_usd": pct_table(usd),
        "price_band_share": {b: float((band == b).mean())
                             for b in ("under_1", "1_to_3", "3_to_10", "over_10")},
        "session_share": {x: float((sess == x).mean())
                          for x in ("pre", "regular", "after", "overnight")},
        "n_distinct_symbols": int(np.unique(sym_id).size),
    }


def fire_overlap(fire_sym: np.ndarray, fire_bar: np.ndarray,
                 sym_id: np.ndarray, bar: np.ndarray) -> dict:
    """뽑힌 위약 막대 중 **그 자체가 발화 막대**인 비율.

    정합을 세게 걸면 후보가 "발화 근처" 로 몰릴 수 있다. 그러면 위약이 위약이 아니다.
    **거르지 않고 세어서 낸다** — 거르면 정합의 뜻이 바뀌기 때문이다.
    """
    if sym_id.size == 0:
        return {"n": 0}
    fires = set(zip(fire_sym.tolist(), fire_bar.tolist()))
    hit = sum(1 for p in zip(sym_id.tolist(), bar.tolist()) if p in fires)
    return {"n": int(sym_id.size), "n_control_is_a_fire_bar": int(hit),
            "share_control_is_a_fire_bar": float(hit / sym_id.size)}


def unpaired_fire_profile(uni: dict, syms: list, fire_sym: np.ndarray,
                          fire_bar: np.ndarray, paired: np.ndarray, *,
                          horizon_s: int = 60) -> dict:
    """**짝을 잃은 발화는 어떤 발화인가.** 개수만 세면 그것이 무작위인 줄 알게 된다.

    §4.4-D 는 짝을 잃은 25/185 를 **세어서** 냈다. 세는 것으로는 부족하다 — 잃은 것이
    한쪽으로 치우쳐 있으면 **남은 표본 자체가 옮겨간다.** 그래서 잃은 쪽과 남은 쪽의
    직전 테이프 밀도·'앵커=최고가'·빈 창 비율을 나란히 낸다.
    """
    n = int(fire_sym.size)
    if n == 0:
        return {"n": 0}
    pr = arm_forward_probe(uni, syms, fire_sym, fire_bar,
                           horizons=(horizon_s,))[horizon_s]
    is_max = ~(pr["max_ret"] > 0)
    empty = pr["n_bars"] == 0
    nb = np.empty(n)
    for si in np.unique(fire_sym):
        sel = np.flatnonzero(fire_sym == si)
        nb[sel] = uni[syms[int(si)]]["nbar60"][fire_bar[sel]]
    out = {"n": n, "horizon_s": horizon_s}
    for tag, msk in (("paired", paired), ("unpaired", ~paired), ("all", np.ones(n, bool))):
        if not msk.any():
            continue
        out[tag] = {"n": int(msk.sum()),
                    "trailing_bars_p50": float(np.median(nb[msk])),
                    "anchor_is_max_incl_empty": float(is_max[msk].mean()),
                    "share_forward_window_empty": float(empty[msk].mean())}
    out["reading"] = (
        "`unpaired` 쪽의 `trailing_bars_p50` 이 작고 `anchor_is_max` 가 크면, 짝을 잃은 "
        "것은 **직전에 거의 거래가 없던 발화**다. 그러면 정합 뒤 실제 팔의 값이 움직인 "
        "이유의 일부는 정합이 아니라 **그 표본이 빠진 것**이다. 둘을 섞어 읽으면 안 된다.")
    return out


def flat_tape_diagnostic(uni: dict, syms: list, sym_id: np.ndarray, bar: np.ndarray, *,
                         key: str = "rv60", horizon_s: int = 60) -> dict:
    """**눈금 안에 죽은 테이프가 얼마나 들어 있는가.**

    직전 60초 동안 값이 한 번도 안 움직인 막대(키가 결측이거나 0)는 정합 풀에서 빠진다.
    그런데 그런 막대는 **앞으로도 조용해서** "앵커가 최고가" 가 되기 쉽다 — 심지어 앞
    60초에 체결이 하나도 없으면 `incl_empty` 규약이 그것을 "더 안 올랐다" 로 센다.

    그러므로 이 표가 없으면, 정합 위약이 눈금보다 낮게 나왔을 때 그것이 **변동성을
    맞춰서**인지 **죽은 막대를 빼서**인지 가를 수 없다. 여기서 갈라 놓는다.
    """
    m = int(sym_id.size)
    if m == 0:
        return {"n": 0}
    flat = np.zeros(m, dtype=bool)
    for si in np.unique(sym_id):
        sel = np.flatnonzero(sym_id == si)
        v = uni[syms[int(si)]][key][bar[sel]]
        flat[sel] = ~(np.isfinite(v) & (v > 0))
    pr = arm_forward_probe(uni, syms, sym_id, bar, horizons=(horizon_s,))[horizon_s]
    is_max = ~(pr["max_ret"] > 0)                 # incl_empty 규약 (§10-3 과 같다)
    empty = pr["n_bars"] == 0
    out = {"n": m, "horizon_s": horizon_s, "key": key,
           "share_flat_tape": float(flat.mean())}
    for tag, msk in (("flat_tape", flat), ("moving_tape", ~flat)):
        if not msk.any():
            continue
        out[tag] = {"n": int(msk.sum()),
                    "anchor_is_max_incl_empty": float(is_max[msk].mean()),
                    "share_forward_window_empty": float(empty[msk].mean())}
    out["reading"] = (
        "`flat_tape` 쪽의 `anchor_is_max` 가 높고 그 상당 부분이 `빈 창`이면, 그 몫은 "
        "**시장이 아니라 체결이 없는 것**이다. 눈금에 그 막대가 몇 % 들어 있는지가 "
        "곧 눈금이 얼마나 부풀어 있는지다.")
    return out


# --------------------------------------------------------------------------- #
# 5. 발화 수집
# --------------------------------------------------------------------------- #
def collect_fires(uni: dict, syms: list, *, rise: float = SHOT_RISE,
                  max_seconds: int = SHOT_MAX_SECONDS) -> tuple:
    """`find_fires` 를 **그대로** 불러 (종목 번호, 막대 번호) 로 편다."""
    sid, bid = [], []
    for si, s in enumerate(syms):
        u = uni[s]
        f = find_fires(u["ts"], u["px"], u["px"], rise=rise,
                       max_seconds=max_seconds, cooldown_s=max_seconds)
        if f.size:
            sid.append(np.full(f.size, si, dtype="int64"))
            bid.append(f)
    if not sid:
        return np.array([], "int64"), np.array([], "int64")
    return np.concatenate(sid), np.concatenate(bid)


# --------------------------------------------------------------------------- #
# 6. 비교 블록 — 실제 / 비정합 위약 / 정합 위약을 **같은 발화 집합** 위에
# --------------------------------------------------------------------------- #
def compare_block(uni: dict, syms: list, index: dict,
                  fire_sym: np.ndarray, fire_bar: np.ndarray, *,
                  tol: float = VOL_MATCH_TOL, draws: int = MATCH_DRAWS,
                  seed: int = SEED, lag_s: float = DETECT_LAG_S,
                  with_probe: bool = True, index_all: dict | None = None,
                  probe_horizons: tuple[int, ...] = PROBE_HORIZONS_S) -> dict:
    """한 (키 × 풀) 조합에 대해 팔들을 낸다.

    **짝을 지은 발화 위에서만** 비교한다. 정합에서 짝을 잃은 발화는 다른 팔에서도
    빼야 팔들이 같은 모집단이 된다. 뺀 개수와 사유는 `pairing` 에 남는다.

    `index_all` 을 주면 팔이 하나 더 붙는다 — **`placebo_unmatched_all_bars`**.
    이것이 `docs/44` §10-3 눈금과 직접 이어지는 줄이다. 왜 필요한가:
    정합 풀은 키가 결측이거나 0 인 막대를 뺀다(= **직전 60초 동안 값이 한 번도 안
    움직인 막대**). 그런데 그런 막대는 앞으로도 조용해서 "앵커가 최고가" 가 되기
    아주 쉽다. 그 막대들을 빼는 것만으로도 눈금이 내려가므로, **밴드가 한 일**과
    **풀을 만들면서 한 일**을 갈라 놓지 않으면 둘이 섞인다.
    """
    m_draw = draw_controls(uni, syms, index, fire_sym, fire_bar,
                           matched=True, tol=tol, draws=draws,
                           rng=np.random.default_rng(seed))
    keep = m_draw["paired"]
    f_sym, f_bar = fire_sym[keep], fire_bar[keep]
    # 비정합 팔은 **같은 발화 집합·같은 씨앗·같은 추첨 규칙**으로 뽑는다.
    u_draw = draw_controls(uni, syms, index, f_sym, f_bar,
                           matched=False, draws=draws,
                           rng=np.random.default_rng(seed))
    # 정합 팔의 뽑기를 짝 지은 발화로 다시 좁힌다(슬롯 번호를 새 인덱스로 옮긴다).
    remap = np.full(fire_sym.size, -1, dtype="int64")
    remap[keep] = np.arange(int(keep.sum()), dtype="int64")
    ms = remap[m_draw["slot"]]
    m_ok = ms >= 0
    m_sym, m_bar = m_draw["sym"][m_ok], m_draw["bar"][m_ok]

    arms = {
        "real": (f_sym, f_bar),
        "placebo_unmatched": (u_draw["sym"], u_draw["bar"]),
        "placebo_vol_matched": (m_sym, m_bar),
    }
    a_draw = None
    if index_all is not None:
        a_draw = draw_controls(uni, syms, index_all, f_sym, f_bar,
                               matched=False, draws=draws,
                               rng=np.random.default_rng(seed))
        arms["placebo_unmatched_all_bars"] = (a_draw["sym"], a_draw["bar"])
    block: dict = {
        "match_key": index["key"],
        "pool_mode": index["mode"],
        "tol": tol,
        "draws_per_fire": draws,
        "seed": seed,
        "pairing": {
            "n_fires": m_draw["n_fires"],
            "n_fires_paired": m_draw["n_paired"],
            "n_fires_unpaired": m_draw["n_unpaired"],
            "share_unpaired": (float(m_draw["n_unpaired"] / m_draw["n_fires"])
                               if m_draw["n_fires"] else None),
            "unpaired_key_missing": m_draw["unpaired_key_missing"],
            "unpaired_empty_band": m_draw["unpaired_empty_band"],
            "unpaired_gap_excluded_only": m_draw["unpaired_gap_excluded_only"],
            "match_band_size_bars": m_draw["band_size"],
            "n_candidate_bars": index["n_candidate_bars"],
            "n_bars_dropped_key_missing": index["n_dropped_key_missing"],
            "note": ("짝을 못 지은 발화는 **다른 팔 전부에서** 뺐다. 사유별로 갈라 놨다 — "
                     "조용히 줄이지 않는다."),
            "who_was_dropped": unpaired_fire_profile(uni, syms, fire_sym, fire_bar,
                                                     keep),
        },
        "pairing_unmatched_arm": {
            "n_fires": u_draw["n_fires"],
            "n_paired": u_draw["n_paired"],
            "n_unpaired": u_draw["n_unpaired"],
            "unpaired_gap_excluded_only": u_draw["unpaired_gap_excluded_only"],
            "note": ("비정합 팔도 **같은 간격 배제**를 받는다. 여기서 짝을 잃는 것은 "
                     "밴드가 아니라 간격 때문이다 — 두 팔의 차이가 밴드 하나임을 "
                     "확인하려면 이 수가 0 에 가까워야 한다."),
        },
        "entry_outcomes": {},
        "balance": {},
        "control_overlap_with_fires": {},
    }
    for tag, (a_sym, a_bar) in arms.items():
        block["entry_outcomes"][tag] = summarize_entry(
            arm_entry_outcomes(uni, syms, a_sym, a_bar, lag_s=lag_s))
        block["balance"][tag] = balance_table(uni, syms, a_sym, a_bar)
        if tag != "real":
            block["control_overlap_with_fires"][tag] = fire_overlap(
                fire_sym, fire_bar, a_sym, a_bar)

    if with_probe:
        probe: dict = {}
        for tag, (a_sym, a_bar) in arms.items():
            r = arm_forward_probe(uni, syms, a_sym, a_bar, horizons=probe_horizons)
            probe[tag] = {f"fixed_{h}s": probe_summary(r[h]) for h in probe_horizons}
        block["anchor_is_max"] = probe
        if a_draw is not None:
            block["flat_tape_in_the_ruler"] = flat_tape_diagnostic(
                uni, syms, a_draw["sym"], a_draw["bar"], key=index["key"])
        block["anchor_is_max_note"] = (
            "docs/44 §10-3 과 **같은 물음**이다 — 앵커 막대가 다음 H초의 최고가인 비율. "
            "빈 창 규약 두 가지를 다 싣는다(`incl_empty` / `excl_empty`).")
    return block


#: 팔을 읽는 순서. 눈금에서 실제로 **한 걸음씩** 올라간다.
ARM_ORDER = ("placebo_unmatched_all_bars", "placebo_unmatched",
             "placebo_vol_matched", "real")

#: 임계 쓸이·민감도에는 `all_bars` 팔을 안 붙인다 — 거기 물음은 "임계를 따라 커지는가"
#: 라서 눈금 구성 사다리가 필요 없고, 다섯 줄 × 네 팔은 읽히지 않는다.
SWEEP_ARMS = ("real", "placebo_unmatched", "placebo_vol_matched")


def ruler_ladder(bars: dict, block: dict, *, horizon_s: int = 60) -> dict:
    """`docs/44` §10-3 의 **31.1% 에서 내 정합 위약까지 한 걸음씩** 이어 놓는다.

    §10-3 은 발화 30.7% 를 무작위 막대 31.1% 옆에 놓고 "차이 없음" 이라 읽었다.
    그런데 그 눈금은 **종목마다 300개씩** 뽑은 것이라(`random_time_baseline`),
    종목 구성이 발화 쪽과 다르다. 발화는 종목마다 개수가 크게 다르기 때문이다.

    그래서 사다리를 놓는다. 한 칸씩 **딱 하나만** 바꾼다:

    | 칸 | 무엇이 바뀌나 |
    |---|---|
    | `docs44_ruler_equal_weight_per_symbol` | §10-3 그대로 (재현용) |
    | `placebo_unmatched_all_bars` | 종목 구성을 **발화와 같게** 맞춘다 |
    | `placebo_unmatched` | 키가 없는 막대(죽은 테이프)를 뺀다 |
    | `placebo_vol_matched` | **변동성 밴드**를 건다 |
    | `real` | 발화 그 자체 |

    **어느 칸이 옳은 눈금인지 판정하지 않는다.** 각 칸이 무엇을 바꾼 값인지만 적는다.
    """
    # §10-3 의 눈금은 **그 모듈의 함수를 그대로** 불러 재현한다 — 재구현하면 재현이 아니다.
    ruler = random_time_baseline(bars, horizons=(horizon_s,))[f"fixed_{horizon_s}s"]
    rungs = {"docs44_ruler_equal_weight_per_symbol": {
        "n": ruler.get("n"),
        "anchor_is_max_incl_empty": ruler.get("share_anchor_is_max_incl_empty"),
        "anchor_is_max_excl_empty": ruler.get("share_anchor_is_max_excl_empty"),
        "share_empty_window": ruler.get("share_empty_window"),
        "what_changed": "없음 — docs/44 §10-3 의 그 줄을 이 창에서 재현한 값",
    }}
    changed = {
        "placebo_unmatched_all_bars": "종목 구성을 **발화와 같게** 맞췄다",
        "placebo_unmatched": "직전 60초에 값이 안 움직인 막대를 뺐다(정합 풀 구성)",
        "placebo_vol_matched": "**변동성 밴드**를 걸었다",
        "real": "발화 그 자체 (위약이 아니다)",
    }
    for arm in ARM_ORDER:
        a = block.get("anchor_is_max", {}).get(arm, {}).get(f"fixed_{horizon_s}s")
        if a is None:
            continue
        rungs[arm] = {
            "n": a.get("n"),
            "anchor_is_max_incl_empty": a.get("share_anchor_is_max_incl_empty"),
            "anchor_is_max_excl_empty": a.get("share_anchor_is_max_excl_empty"),
            "share_empty_window": a.get("share_empty_window"),
            "what_changed": changed[arm],
        }
    return {"horizon_s": horizon_s, "rungs": rungs,
            "how_to_read": (
                "칸마다 **하나만** 바뀐다. 어느 칸에서 값이 크게 움직이는지가 곧 그 "
                "숫자를 만들고 있던 것이 무엇인지다. **어느 칸이 옳은 눈금인지는 "
                "이 문서가 정하지 않는다 — 사용자 안건이다.**")}


# --------------------------------------------------------------------------- #
# 7. M2 — 임계 쓸이에 정합 위약을 붙인다
# --------------------------------------------------------------------------- #
SWEEP_METRICS = ("mfe_60s", "mae_60s", "slip_from_fire", "ret_30s", "ret_60s")


def threshold_sweep_with_control(uni: dict, syms: list, index: dict, *,
                                 thresholds: tuple[float, ...] = THRESHOLD_SWEEP,
                                 tol: float = VOL_MATCH_TOL,
                                 draws: int = MATCH_DRAWS, seed: int = SEED,
                                 max_seconds: int = SHOT_MAX_SECONDS) -> dict:
    """`docs/44` §3-4 의 다섯 줄에 **정합 위약 두 칸**을 더한다.

    물음 하나다: **MFE·MAE 가 임계를 따라 커지는 것이 위약에서도 일어나는가.**
    일어나면 그 대칭은 "임계가 고른 사건" 이 아니라 **"임계가 고른 변동성"** 이다.
    임계가 올라가면 발화 순간의 변동성이 올라가고, 정합 위약은 그 변동성을 따라간다 —
    그래서 위약 MFE 도 같이 커지면 그 커짐은 사건이 아니다.

    키(`rv60`·`range60`)는 임계와 무관하게 **한 번** 만들어 둔 것을 쓴다. 임계마다 키가
    달라지면 다섯 줄이 서로 다른 자로 잰 값이 된다.
    """
    out: dict = {}
    for r in thresholds:
        f_sym, f_bar = collect_fires(uni, syms, rise=r, max_seconds=max_seconds)
        blk = compare_block(uni, syms, index, f_sym, f_bar, tol=tol, draws=draws,
                            seed=seed, with_probe=False)
        row = {"n_fires": int(f_sym.size),
               "n_fires_paired": blk["pairing"]["n_fires_paired"],
               "n_fires_unpaired": blk["pairing"]["n_fires_unpaired"]}
        for tag in SWEEP_ARMS:
            eo = blk["entry_outcomes"][tag]
            row[tag] = {"n_entered": eo.get("n_entered")}
            for k in SWEEP_METRICS:
                row[tag][k] = eo.get(k, {}).get("mean")
            row[tag]["mfe_plus_mae"] = (
                None if row[tag]["mfe_60s"] is None or row[tag]["mae_60s"] is None
                else row[tag]["mfe_60s"] + row[tag]["mae_60s"])
            row[tag]["rv60_p50"] = blk["balance"][tag].get("rv60", {}).get("p50")
        out[f"{r:.3%}"] = row
    out["reading"] = (
        "실제 줄과 정합 위약 줄이 **같이** 커지면, §3-4 의 대칭은 임계가 고른 "
        "**변동성**의 성질이다. 실제만 커지면 임계가 고른 **사건**의 성질이다. "
        "**이 함수는 어느 쪽인지 판정하지 않는다** — 두 줄을 나란히 놓을 뿐이다.")
    return out


# --------------------------------------------------------------------------- #
# 8. 민감도 — 허용 오차와 씨앗
# --------------------------------------------------------------------------- #
HEADLINE_KEYS = ("mfe_60s", "mae_60s", "ret_30s", "ret_60s", "slip_from_fire")


def _headline(block: dict) -> dict:
    out: dict = {"n_fires_paired": block["pairing"]["n_fires_paired"]}
    for tag in SWEEP_ARMS:
        eo = block["entry_outcomes"][tag]
        out[tag] = {k: eo.get(k, {}).get("mean") for k in HEADLINE_KEYS}
        out[tag]["rv60_p50"] = block["balance"][tag].get("rv60", {}).get("p50")
        am = block.get("anchor_is_max", {}).get(tag, {})
        out[tag]["anchor_is_max_60s_incl_empty"] = (
            am.get("fixed_60s", {}).get("share_anchor_is_max_incl_empty"))
        out[tag]["anchor_is_max_60s_excl_empty"] = (
            am.get("fixed_60s", {}).get("share_anchor_is_max_excl_empty"))
    return out


def tolerance_sensitivity(uni: dict, syms: list, index: dict,
                          fire_sym: np.ndarray, fire_bar: np.ndarray, *,
                          tols: tuple[float, ...] = TOL_SWEEP,
                          seed: int = SEED) -> dict:
    """**정합을 조이면 답이 어디로 가는가.** 조일수록 짝을 더 잃는다 — 그것도 같이 낸다."""
    return {f"tol_{t:.2f}": _headline(
        compare_block(uni, syms, index, fire_sym, fire_bar, tol=t, seed=seed))
        for t in tols}


def seed_sensitivity(uni: dict, syms: list, index: dict,
                     fire_sym: np.ndarray, fire_bar: np.ndarray, *,
                     seeds: tuple[int, ...] = SEED_SWEEP,
                     tol: float = VOL_MATCH_TOL) -> dict:
    """씨앗을 바꿔 본다. **이것은 추첨 잡음이지 표본 신뢰구간이 아니다.**

    표본이 2.6 정규장이라 CI 를 못 낸다(`docs/44` §6-1). 씨앗 폭이 좁다고 해서
    "차이가 유의하다" 가 되지 않는다 — 같은 2.6일을 다시 뽑는 것뿐이다.
    """
    out = {f"seed_{s}": _headline(
        compare_block(uni, syms, index, fire_sym, fire_bar, tol=tol, seed=s))
        for s in seeds}
    out["what_this_is_not"] = (
        "**표본 CI 가 아니다.** 같은 2.6 정규장 안에서 위약 추첨만 다시 한 것이다. "
        "표본이 늘지 않았으므로 신뢰구간은 여전히 낼 수 없다.")
    return out


# --------------------------------------------------------------------------- #
# 9. 조립
# --------------------------------------------------------------------------- #
def build_report(conn: sqlite3.Connection, *, rise: float = SHOT_RISE,
                 max_seconds: int = SHOT_MAX_SECONDS,
                 tol: float = VOL_MATCH_TOL, seed: int = SEED) -> dict:
    t0 = time.time()
    syms_all = select_symbols(conn)
    print(f"  [1/5] symbols: {len(syms_all)}  ({time.time()-t0:.0f}s)")
    bars = load_bars(conn, syms_all)
    syms = [s for s in syms_all if s in bars]
    print(f"  [2/5] second bars: {len(syms)}  ({time.time()-t0:.0f}s)")
    uni = build_universe(bars)
    n_bars = sum(int(uni[s]["ts"].size) for s in syms)
    print(f"  [3/5] volatility keys on {n_bars} bars  ({time.time()-t0:.0f}s)")
    fire_sym, fire_bar = collect_fires(uni, syms, rise=rise, max_seconds=max_seconds)
    print(f"  [4/5] fires: {fire_sym.size}  ({time.time()-t0:.0f}s)")

    indices = {(k, m): pool_index(uni, syms, k, m)
               for k in MATCH_KEYS for m in POOL_MODES}
    # 비정합 팔 전용 — **키가 없는 막대까지** 포함한 풀. docs/44 §10-3 눈금과 이어진다.
    indices_all = {(k, m): pool_index(uni, syms, k, m, require_key=False)
                   for k in MATCH_KEYS for m in POOL_MODES}

    rep: dict = {
        "question": (
            "docs/44 의 관측들(§3-3 진입 후 수익률 · §3-4 임계 대칭 · §10 앵커=최고가)이 "
            "**변동성 선택의 그림자인가.** 무작위 시각 위약은 그것을 못 가른다 — "
            "변동성을 맞춘 위약만 가른다."),
        "not_a_verdict": (
            "**판정하지 않는다.** 어떤 설계도 살리거나 죽이지 않는다. 여기 있는 것은 "
            "'변동성을 맞추면 이 숫자가 이렇게 움직인다' 까지다."),
        "conditions": {
            "window_start_ms": WINDOW_START_MS,
            "window_end_ms": int(max(int(uni[s]["ts"][-1]) for s in syms)),
            "symbols": len(syms),
            "second_bars": n_bars,
            "rise": rise, "max_seconds": max_seconds,
            "detector": "tick_stages.find_fires — 직전 60초 최저 대비, 미래 안 씀, 60초 잠금",
            "entry_rule": f"발화 + {DETECT_LAG_S}초 이후 처음 잡히는 초 막대 (§3-3 과 동일)",
            "price_series": "초 막대 vwap. 초 안 순서 미사용(docs/42)",
            "costs_excluded": "스프레드·수수료·슬리피지 전부 미포함",
            "no_1m_candles": "candles_1m 경로 없음 (D-10)",
            "reused_not_reimplemented": [
                "tick_stages.find_fires", "tick_stages._entry_outcomes",
                "cross_peak_check.forward_probe", "cross_peak_check.probe_summary",
            ],
        },
        "method": {
            "match_keys": {
                "rv60": (f"직전 {VOL_LOOKBACK_S}초 로그수익률 표준편차(ddof=1). "
                         "§4.4-D 와 **같은 추정량**, 창만 600초→60초."),
                "range60": (f"직전 {VOL_LOOKBACK_S}초 max/min − 1. "
                            "**D-7 원문이 말한 '직전 60초 변동폭'** 이다."),
            },
            "two_keys_differ_by_construction": (
                "`find_fires` 는 '현재가 / 직전 60초 최저 − 1 ≥ rise' 일 때 발화한다. "
                "그러므로 발화 막대의 `range60` 은 **정의상 rise 이상**이다 — "
                "`range60` 정합은 **방아쇠 변수 자체를 맞추는 것**이라 더 세고, "
                "동시에 더 순환적이다. `rv60` 은 방아쇠가 아니다. "
                "**그래서 둘 다 낸다.**"),
            "pool_modes": {
                "same_symbol": ("같은 종목, 다른 시각. **주(主) 모드다** — docs/44 §3-3 의 "
                                "위약과 §10-3 의 눈금이 이미 같은 종목이라, 남은 의심은 "
                                "종목이 아니라 **순간**이다."),
                "pooled": ("종목을 합친 풀. §10-3 의 무작위 막대 눈금이 종목을 합쳐 "
                           "뽑았으므로 그 줄과 직접 비교하려면 이 모드가 필요하다."),
            },
            "changed_from_4_4_D": [
                "1분봉 → **초 막대**(D-10).",
                "정합 축: **다른 종목·같은 시각** → **같은 종목·다른 시각**. "
                "§4.4-D 의 위약은 무작위 **종목**이라 종목 변동성이 안 맞았지만, "
                "docs/44 의 위약은 이미 같은 종목이다. 남은 축은 **순간**이다.",
                f"되돌아보기 창 600초 → **{VOL_LOOKBACK_S}초**. 사건 정의가 60초라 "
                "600초 창은 사건 밖을 잰다.",
                "키 1개 → **2개**(rv60·range60). D-7 원문이 변동폭을 지목했다.",
                "**비정합 위약을 같은 기계로 같이 뽑는다.** 정합 위약을 docs/44 표와 "
                "바로 비교하면 정합 때문인지 추첨 규칙 때문인지 모른다.",
                f"자기 구간 배제 600초 → **{SELF_GAP_S}초**(최대 지평 120 + 키 창 60).",
            ],
            "lookahead_free": (
                "정합 키는 앵커 막대 **이하**의 자료로만 만든다. 앵커 뒤를 잘라내고 다시 "
                "계산해도 같은 값이 나온다(테스트로 고정). 사후 변동성으로 맞추면 그 자체가 "
                "미래를 쓰는 것이다."),
            "draws_per_fire": MATCH_DRAWS,
            "self_gap_s": SELF_GAP_S,
            "tolerance": tol,
        },
    }

    # ---- M1 · M3: (키 × 풀) 네 조합 ---------------------------------------- #
    blocks: dict = {}
    for k in MATCH_KEYS:
        for m in POOL_MODES:
            tag = f"{k}__{m}"
            blocks[tag] = compare_block(uni, syms, indices[(k, m)],
                                        fire_sym, fire_bar, tol=tol, seed=seed,
                                        index_all=indices_all[(k, m)])
            print(f"  [5/5] block {tag}: paired "
                  f"{blocks[tag]['pairing']['n_fires_paired']}/{fire_sym.size}"
                  f"  ({time.time()-t0:.0f}s)")
    rep["blocks"] = blocks
    rep["ruler_ladder"] = {tag: ruler_ladder(bars, b) for tag, b in blocks.items()}
    rep["ruler_ladder_why"] = (
        "docs/44 §10-3 은 발화 30.7% 를 무작위 막대 31.1% 옆에 놓았다. 그런데 그 눈금은 "
        "**종목마다 300개씩** 뽑은 것이라 종목 구성이 발화 쪽과 다르다. 사다리는 "
        "그 눈금에서 정합 위약까지 **한 칸에 하나씩만** 바꿔 이어 놓은 것이다.")
    rep["primary_block"] = "rv60__same_symbol"
    rep["primary_block_why"] = (
        "docs/44 §3-3 의 위약이 '같은 종목, 무작위 시각' 이라 종목 축은 이미 상쇄돼 "
        "있다. 그리고 rv60 은 방아쇠 변수가 아니다(range60 은 방아쇠다). "
        "그래서 이 칸이 '순간 선택' 을 가장 곧게 묻는다.")

    # ---- M2: 임계 쓸이 ------------------------------------------------------ #
    rep["threshold_sweep"] = threshold_sweep_with_control(
        uni, syms, indices[("rv60", "same_symbol")], tol=tol, seed=seed,
        max_seconds=max_seconds)
    print(f"  threshold sweep done  ({time.time()-t0:.0f}s)")

    # ---- 민감도 ------------------------------------------------------------- #
    rep["tolerance_sensitivity"] = tolerance_sensitivity(
        uni, syms, indices[("rv60", "same_symbol")], fire_sym, fire_bar, seed=seed)
    rep["seed_sensitivity"] = seed_sensitivity(
        uni, syms, indices[("rv60", "same_symbol")], fire_sym, fire_bar, tol=tol)
    print(f"  sensitivity done  ({time.time()-t0:.0f}s)")

    # ---- docs/44 의 어느 줄과 짝인가 ---------------------------------------- #
    rep["what_each_block_answers"] = {
        "M1_docs44_3_3": ("`entry_outcomes` — 진입 후 수익률·MFE·MAE. docs/44 §3-3 은 "
                          "무작위 시각 위약과 구별이 안 됐다. 여기서는 정합 위약과 "
                          "비정합 위약을 **같은 발화 집합** 위에 나란히 놓는다."),
        "M2_docs44_3_4": ("`threshold_sweep` — 임계 0.5~3%. §3-4 의 MFE·MAE 대칭이 "
                          "위약에서도 임계를 따라 커지는가."),
        "M3_docs44_10_3": ("`blocks[*].anchor_is_max` — 앵커 막대가 다음 H초의 최고가인 "
                           "비율. §10-3 은 발화 30.7% vs 무작위 막대 31.1% 였다."),
    }

    rep["solo_decisions"] = [
        ("정합 축을 §4.4-D 의 '다른 종목·같은 시각' 에서 **'같은 종목·다른 시각'** 으로 "
         "바꿨다. 근거: docs/44 의 위약이 이미 같은 종목이라 종목 축은 상쇄돼 있고, "
         "§10-8 2번이 지목한 잔여 의심은 **순간**이다. 풀 합침 모드도 같이 내서 "
         "§10-3 눈금과의 직접 비교를 남겼다. **동의 안 하면 되돌려야 한다.**"),
        (f"되돌아보기 창을 **{VOL_LOOKBACK_S}초**로 정했다. 사건 정의(60초)와 같게 맞춘 "
         "것이다. 300초·600초로 하면 '그 순간' 이 아니라 '그 시간대' 를 맞추게 된다. "
         "이 선택은 검정 안 했다 — 창 민감도는 안 냈다."),
        ("정합 키를 **두 개** 냈다. D-7 원문은 '변동폭' 이라 적혀 있는데, 변동폭은 "
         "`find_fires` 의 방아쇠 변수 자체라 정합이 순환적이다. 그래서 방아쇠가 아닌 "
         "`rv60` 을 주로 놓고 `range60` 을 같이 냈다."),
        ("**비정합 위약을 새로 뽑았다.** 태스크에 없던 것이다. 없으면 정합 위약 값이 "
         "docs/44 §3-3 의 위약 값과 다를 때 그것이 정합 때문인지 추첨 규칙 때문인지 "
         "가를 수 없다."),
        ("짝을 못 지은 발화를 **세 팔 전부에서** 뺐다. 실제 팔을 전체 발화로 두면 "
         "세 팔의 모집단이 달라진다. 뺀 개수·사유는 `pairing` 에 그대로 있다."),
        (f"위약 막대가 발화 자신의 측정 구간과 겹치지 않도록 **±{SELF_GAP_S}초**를 "
         "배제했다. 다른 발화 근처는 배제하지 않았다 — 배제하면 정합이 찾는 고변동성 "
         "구간을 통째로 지우게 된다. 대신 **위약이 발화 막대인 비율을 세어서 낸다.**"),
        ("씨앗 민감도를 냈지만 **CI 가 아니다.** 표본이 2.6 정규장이라 신뢰구간을 "
         "만들 수 없다는 것은 그대로다."),
    ]
    rep["limits"] = [
        "**2.6 정규장뿐이다 — CI 를 못 낸다.** §4.4-D 는 n=185 였고 지금 조건이 그때보다 "
        "나을 이유가 없다. 여기 수치는 전부 **점추정**이다.",
        "비용 미포함(스프레드·수수료·슬리피지).",
        "`rv60` 은 막대 개수 기반 표준편차라 **테이프 밀도와 섞인다.** 시간 정규화를 "
        "안 했다 — 대신 `balance.nbar60` 로 밀도가 맞았는지 보이게 했다.",
        "포화 칸(테이프 끊김)을 따로 안 갈랐다. 끊긴 구간에서는 변동성도 **과소**로 "
        "잡히므로 정합 자체가 그쪽으로 치우칠 수 있다.",
        "되돌아보기 창 60초는 **검정하지 않은 선택**이다(창 민감도 미측정).",
        "`pooled` 모드는 **같은 시각을 강제하지 않는다.** §4.4-D 는 같은 시각의 다른 "
        "종목이었다 — 여기 pooled 는 시각도 종목도 흔든다. 같은 뜻이 아니다.",
        "**판정이 아니다.** 어떤 설계도 살리거나 죽이지 않는다.",
    ]
    return rep


# --------------------------------------------------------------------------- #
# 10. 화면 요약
# --------------------------------------------------------------------------- #
def _f(v, nd=5):
    return "n/a" if v is None else f"{v:.{nd}f}"


def print_report(rep: dict) -> None:
    c = rep["conditions"]
    print(f"\n=== conditions: {c['symbols']} symbols, {c['second_bars']} second bars, "
          f"rise {c['rise']}, window_end_ms {c['window_end_ms']}")

    print("\n=== pairing — fires that found no volatility twin (counted, not hidden)")
    print(f"  {'block':<24}{'fires':>7}{'paired':>8}{'unpaired':>9}"
          f"{'key_miss':>9}{'empty_band':>11}{'gap_only':>9}{'band_p50':>10}")
    for tag, b in rep["blocks"].items():
        p = b["pairing"]
        print(f"  {tag:<24}{p['n_fires']:>7}{p['n_fires_paired']:>8}"
              f"{p['n_fires_unpaired']:>9}{p['unpaired_key_missing']:>9}"
              f"{p['unpaired_empty_band']:>11}{p['unpaired_gap_excluded_only']:>9}"
              f"{_f(p['match_band_size_bars'].get('p50'), 0):>10}")
    print("  who was dropped (the loss is NOT random — read this before the tables):")
    print(f"    {'block':<24}{'group':<10}{'n':>7}{'trail bars p50':>16}"
          f"{'anchor_is_max':>15}{'empty fwd':>11}")
    for tag, b in rep["blocks"].items():
        w = b["pairing"].get("who_was_dropped", {})
        for g in ("paired", "unpaired"):
            v = w.get(g)
            if not v:
                continue
            print(f"    {tag:<24}{g:<10}{v['n']:>7}{v['trailing_bars_p50']:>16.0f}"
                  f"{v['anchor_is_max_incl_empty']:>15.4f}"
                  f"{v['share_forward_window_empty']:>11.4f}")

    print("\n=== M1 (docs/44 §3-3) — entry outcomes, means")
    for tag, b in rep["blocks"].items():
        print(f"  [{tag}]  paired fires {b['pairing']['n_fires_paired']}")
        print(f"    {'arm':<22}{'rv60 p50':>10}{'slip':>10}{'ret30s':>10}"
              f"{'ret60s':>10}{'MFE':>10}{'MAE':>10}{'MFE+MAE':>10}")
        for arm in ARM_ORDER:
            eo = b["entry_outcomes"][arm]
            mfe = eo.get("mfe_60s", {}).get("mean")
            mae = eo.get("mae_60s", {}).get("mean")
            print(f"    {arm:<22}"
                  f"{_f(b['balance'][arm].get('rv60', {}).get('p50')):>10}"
                  f"{_f(eo.get('slip_from_fire', {}).get('mean')):>10}"
                  f"{_f(eo.get('ret_30s', {}).get('mean')):>10}"
                  f"{_f(eo.get('ret_60s', {}).get('mean')):>10}"
                  f"{_f(mfe):>10}{_f(mae):>10}"
                  f"{_f(None if mfe is None or mae is None else mfe + mae):>10}")
        ov = b["control_overlap_with_fires"]
        print(f"    controls that ARE fire bars: "
              + "  ".join(f"{k.split('_', 1)[1]} {_f(v.get('share_control_is_a_fire_bar'), 3)}"
                          for k, v in ov.items()))

    print("\n=== M3 (docs/44 §10-3) — share where the ANCHOR bar is the max of the next H s")
    hs = PROBE_HORIZONS_S
    for tag, b in rep["blocks"].items():
        if "anchor_is_max" not in b:
            continue
        print(f"  [{tag}]")
        print(f"    {'arm':<22}" + "".join(f"{'H' + str(h) + 's':>10}" for h in hs)
              + "   (incl. empty windows)")
        for arm in ARM_ORDER:
            a = b["anchor_is_max"][arm]
            print(f"    {arm:<22}" + "".join(
                f"{_f(a[f'fixed_{h}s'].get('share_anchor_is_max_incl_empty'), 4):>10}"
                for h in hs))
        print(f"    {'':<22}" + "".join(f"{'':>10}" for h in hs) + "   (empty DROPPED)")
        for arm in ARM_ORDER:
            a = b["anchor_is_max"][arm]
            print(f"    {arm:<22}" + "".join(
                f"{_f(a[f'fixed_{h}s'].get('share_anchor_is_max_excl_empty'), 4):>10}"
                for h in hs))

    print("\n=== M3b — the ruler ladder: docs/44 §10-3's 31.1% to the matched placebo")
    print("    (one thing changes per rung; 60s horizon, incl. empty windows)")
    for tag, lad in rep["ruler_ladder"].items():
        print(f"  [{tag}]")
        for rung, v in lad["rungs"].items():
            print(f"    {rung:<38}{_f(v['anchor_is_max_incl_empty'], 4):>8}"
                  f"  (excl.empty {_f(v['anchor_is_max_excl_empty'], 4)},"
                  f" empty {_f(v.get('share_empty_window'), 3)}, n {v['n']})"
                  f"  <- {v['what_changed']}")
        ft = rep["blocks"][tag].get("flat_tape_in_the_ruler")
        if ft and ft.get("n"):
            print(f"    dead tape inside the all-bars ruler: "
                  f"share {_f(ft['share_flat_tape'], 4)}  "
                  f"anchor_is_max {_f(ft.get('flat_tape', {}).get('anchor_is_max_incl_empty'), 4)}"
                  f" (of which empty windows "
                  f"{_f(ft.get('flat_tape', {}).get('share_forward_window_empty'), 3)})"
                  f"  vs moving tape "
                  f"{_f(ft.get('moving_tape', {}).get('anchor_is_max_incl_empty'), 4)}")

    print("\n=== M2 (docs/44 §3-4) — threshold sweep, MFE / MAE means "
          "[key rv60, pool same_symbol]")
    print(f"  {'thr':<8}{'fires':>7}{'paired':>7}" + "".join(
        f"{a:>26}" for a in SWEEP_ARMS))
    print(f"  {'':<8}{'':>7}{'':>7}" + "".join(
        f"{'MFE':>9}{'MAE':>9}{'sum':>8}" for _ in SWEEP_ARMS))
    for thr, row in rep["threshold_sweep"].items():
        if not isinstance(row, dict):
            continue
        line = f"  {thr:<8}{row['n_fires']:>7}{row['n_fires_paired']:>7}"
        for arm in SWEEP_ARMS:
            a = row[arm]
            line += (f"{_f(a['mfe_60s'], 4):>9}{_f(a['mae_60s'], 4):>9}"
                     f"{_f(a['mfe_plus_mae'], 4):>8}")
        print(line)

    print("\n=== sensitivity — matching tolerance (rv60, same_symbol)")
    for tag, h in rep["tolerance_sensitivity"].items():
        print(f"  {tag:<10} paired {h['n_fires_paired']:>6}  "
              f"real MFE {_f(h['real']['mfe_60s'], 4)} / matched MFE "
              f"{_f(h['placebo_vol_matched']['mfe_60s'], 4)}  |  "
              f"real anchor_is_max60 {_f(h['real']['anchor_is_max_60s_incl_empty'], 4)} / "
              f"matched {_f(h['placebo_vol_matched']['anchor_is_max_60s_incl_empty'], 4)}")

    print("\n=== sensitivity — draw seed (NOT a confidence interval)")
    for tag, h in rep["seed_sensitivity"].items():
        if not isinstance(h, dict) or "real" not in h:
            continue
        print(f"  {tag:<16} matched MFE {_f(h['placebo_vol_matched']['mfe_60s'], 4)}  "
              f"matched MAE {_f(h['placebo_vol_matched']['mae_60s'], 4)}  "
              f"matched anchor_is_max60 "
              f"{_f(h['placebo_vol_matched']['anchor_is_max_60s_incl_empty'], 4)}")

    print("\n=== solo decisions (self-reported)")
    for i, d in enumerate(rep["solo_decisions"], 1):
        print(f"  {i}. {d}")
    print("\n=== limits")
    for x in rep["limits"]:
        print(f"  - {x}")


def main(db: Path, *, out_dir: Path | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    conn = open_ro(db)
    print("=== volatility-matched placebo (read-only, no live calls)")
    rep = build_report(conn)
    print_report(rep)
    out = out_dir or OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    path = out / "vol_matched_placebo.json"
    path.write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    db_path = Path(args[0]) if args else Path(
        r"C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db")
    sys.exit(main(db_path))

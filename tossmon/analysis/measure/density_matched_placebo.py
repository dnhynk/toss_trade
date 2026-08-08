"""**테이프 밀도 정합 위약 (D-13) — §11-4 의 MFE 격차를 밀도까지 맞춰 다시 낸다** (`docs/44` §14).

`docs/44` §11-3 이 스스로 적은 구멍을 메우는 것이다:

> **그런데 테이프 밀도는 안 맞았다.** 실제 발화의 직전 60초 체결이 중앙 **26건**인데
> 위약은 **35건**이다. `rv60` 은 막대 개수 기반 표준편차라 **성긴 테이프에서 부풀어
> 오른다** — 즉 "같은 `rv60`"이 "같은 활발함"이 아니다. **못 갈랐다.**

그래서 §11-4 의 *"MFE 는 정합을 걸수록 위약 쪽이 올라간다"* (실제 +1.66% vs 정합 위약
**+1.89%**)가 **밀도 탓일 수 있다.** 위약이 **더 활발한 순간**에서 뽑혔기 때문이다.

## 밀도를 무엇으로 맞추는가 — **`nbar60`(직전 60초 초 막대 수)**. 근거 셋

1. **§11-3 이 진단한 바로 그 통계량이다.** 26 vs 35 가 이 값이다. 진단된 값을 그대로
   맞추는 것이 가장 곧다 — 다른 대리지표를 쓰면 "진단한 것과 맞춘 것이 다르다"가 된다.
2. **`rv60` 이 부풀어 오르는 기계적 원인이 이것이다.** `rv60` 은 창 안 로그수익률
   `nbar60 − 1` 개의 표준편차다. 막대가 적으면 표본이 적어 추정이 흔들리고, 성긴 초에는
   가격이 크게 건너뛴다. **밀도가 곧 `rv60` 의 분모다.**
3. **체결 건수는 검열돼 있다.** `/trades` 는 4초 칸당 **50건 상한**이 물리고, 그 상한은
   **바쁠 때** 물린다(`docs/41` §4: 칸 수로는 2.14%지만 테이프 분량으로는 **27.3%**).
   즉 체결 건수로 맞추면 **바쁜 순간에서 검열값끼리 맞추는 것**이 된다.
   그래서 체결 건수(`ntrade60`)는 **정합 키가 아니라 균형표의 감시 항목**으로 낸다 —
   막대 수를 맞췄을 때 체결 건수도 따라 맞는지 그대로 싣는다.

**허용 오차는 `rv60` 과 같은 ±20%** 로 둔다. 새 자유변수를 만들지 않기 위해서다.
`nbar60` 은 정수라 밴드도 정수로 닫는다(`ceil(0.8d)` ~ `floor(1.2d)`) — 안쪽으로
닫으므로 ±20% 보다 **약간 더 엄격**하다. 민감도는 `TOL_SWEEP` 로 따로 낸다.

## 사다리 — **한 칸에 하나씩만** 바꾼다

§11-6 이 쓴 규율 그대로다. 네 팔 모두 **같은 후보 풀**(`rv60` 이 있는 막대)에서 뽑고
**같은 간격 배제·재추첨**을 받는다. 달라지는 것은 **밴드 하나뿐**이다.

| 팔 | `rv60` 밴드 | `nbar60` 밴드 | 무엇을 묻나 |
|---|---|---|---|
| `placebo_unmatched` | — | — | §11 의 비정합 팔 (재현) |
| `placebo_vol_matched` | ±20% | — | §11 의 정합 팔 (재현) |
| **`placebo_density_matched`** | — | ±20% | **밀도만 맞추면 어디로 가는가** |
| **`placebo_vol_density_matched`** | ±20% | ±20% | **밀도가 변동성 위에 무엇을 더하는가** |

앞의 두 팔은 `vol_matched_placebo.draw_controls` 를 **그대로 불러** 뽑는다 —
그래야 §11-4 의 숫자가 **재현**되고, 새 두 팔과의 차이가 밴드 때문이라고 말할 수 있다.

## 눈금 (§13 을 반영한다)

§12-8 (A) 는 §11-2~§11-6 을 **"이미 발화 가중"** 으로 분류했다. 위약 팔이 **발화당 3개**씩
뽑히므로 위약의 종목 구성이 발화와 같기 때문이다. **그 분류가 맞는지 여기서 확인하고**,
§13 의 접는 기계(`quantity_two_scales`)로 **두 눈금을 같이 낸다** — 눈금이 암묵이 아니라
표에 적히도록.

## 이 모듈이 하지 않는 것

- **판정하지 않는다.** "그래서 설계 X 가 살았다/죽었다"는 이 모듈이 쓸 문장이 아니다.
- **기존 코드를 한 줄도 안 고친다.** `trailing_stats` · `build_universe` · `pool_index` ·
  `draw_controls` · `collect_fires` · `arm_entry_outcomes` · `summarize_entry` ·
  `balance_table` · `fire_overlap` · `unpaired_fire_profile` · `find_fires` ·
  `_entry_outcomes` 를 그대로 불러 쓴다.
- **1분봉 경로를 안 쓴다**(D-10 은 다음 태스크다).
- **`pooled` 모드를 안 쓴다.** §11-4 의 표가 `rv60`·**같은 종목**이고, 종목별 눈금을
  접으려면 위약의 종목이 발화의 종목이어야 한다.

실행: `python -m tossmon.analysis.measure.density_matched_placebo [db_path]`
→ `out/density_matched_placebo.json`. **라이브 콜 0. DB 는 `mode=ro`.**
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

from tossmon.analysis.measure.scale_convention import SEED_SWEEP
from tossmon.analysis.measure.scale_mixed_recount import quantity_two_scales
from tossmon.analysis.measure.tick_resolution import (
    WINDOW_START_MS,
    open_ro,
    pct_table,
)
from tossmon.analysis.measure.tick_stages import (
    DETECT_LAG_S,
    SHOT_MAX_SECONDS,
    SHOT_RISE,
    TRADES_COUNT_CAP,
    load_bars,
    select_symbols,
)
from tossmon.analysis.measure.vol_matched_placebo import (
    MATCH_DRAWS,
    MAX_REDRAW,
    SEED,
    SELF_GAP_S,
    TOL_SWEEP,
    VOL_LOOKBACK_S,
    VOL_MATCH_TOL,
    arm_entry_outcomes,
    balance_table,
    build_universe,
    collect_fires,
    draw_controls,
    fire_overlap,
    pool_index,
    summarize_entry,
    unpaired_fire_profile,
)

OUT_DIR = Path("out")

#: 밀도 키. **§11-3 이 진단한 그 통계량**이다 — 직전 60초 안 초 막대 수.
DENSITY_KEY = "nbar60"
#: 변동성 밴드 키. §11-4 의 표가 이 키다(방아쇠가 아닌 쪽).
BAND_KEY = "rv60"

#: 밀도 허용 오차. **`rv60` 과 같은 ±20%** — 새 자유변수를 만들지 않는다.
DENSITY_TOL = VOL_MATCH_TOL

#: 이 태스크의 헤드라인 지표. §11-4 가 "위약이 더 낫다"고 읽은 그 칸이다.
HEADLINE_METRIC = "mfe_60s"
#: §11-4 표에 실린 칸 전부. MFE 만 보고 나머지를 숨기지 않는다.
REPORT_METRICS = ("slip_from_fire", "ret_30s", "ret_60s", "mfe_60s", "mae_60s")

#: 사다리 읽는 순서. **한 칸에 하나씩** 밴드가 더해진다.
ARM_ORDER = ("placebo_unmatched", "placebo_vol_matched",
             "placebo_density_matched", "placebo_vol_density_matched", "real")


# --------------------------------------------------------------------------- #
# 1. 밀도 키 — 막대 수는 이미 있고, 체결 건수는 감시용으로 더 만든다
# --------------------------------------------------------------------------- #
def trailing_trade_count(ts: np.ndarray, n: np.ndarray, *,
                         lookback_s: int = VOL_LOOKBACK_S) -> np.ndarray:
    """막대마다 **직전 `lookback_s` 초**(자기 막대 포함)의 **체결 건수 합**.

    **정합 키가 아니다 — 균형표의 감시 항목이다.** `/trades` 의 4초 칸당 50건 상한이
    바쁜 순간에 물리므로(`docs/41` §4) 이 값은 위쪽이 검열돼 있다. 검열된 값끼리
    맞추면 "같은 활발함"이 아니라 "같이 상한에 걸림"을 맞추게 된다.

    `trailing_stats` 와 **같은 창 경계**(`side='left'`)를 쓴다 — 두 밀도 지표가 다른
    구간을 재면 나란히 놓을 수 없다.
    """
    ts = np.asarray(ts, dtype="int64")
    n = np.asarray(n, dtype="int64")
    if ts.size == 0:
        return np.zeros(0, dtype="int64")
    lo = np.searchsorted(ts, ts - lookback_s * 1000, side="left")
    c = np.concatenate(([0], np.cumsum(n)))
    return (c[np.arange(ts.size) + 1] - c[lo]).astype("int64")


def add_trade_count(uni: dict, bars: dict, *,
                    lookback_s: int = VOL_LOOKBACK_S) -> dict:
    """`build_universe` 결과에 `ntrade60` 을 **더한 새 dict** 를 만든다.

    원본을 고치지 않는다 — `vol_matched_placebo` 쪽 함수들이 같은 `uni` 를 보고 있다.
    배열은 참조로 공유하므로 비용은 dict 하나뿐이다.
    """
    out = {}
    for s, u in uni.items():
        ts, n = bars[s][0], bars[s][1]
        out[s] = {**u, "ntrade60": trailing_trade_count(ts, n, lookback_s=lookback_s)}
    return out


def density_band(d: float, tol: float = DENSITY_TOL) -> tuple[int, int]:
    """정수 밀도의 ±`tol` 밴드. **안쪽으로 닫는다.**

    `nbar60` 은 정수라 밴드도 정수로 닫아야 한다. `ceil`/`floor` 로 닫으면 밴드가
    ±`tol` 보다 **약간 더 엄격**해진다 — 느슨한 쪽으로 반올림해 "맞췄다"를 부풀리는
    것보다 이쪽이 안전하다. `d` 가 작으면 밴드가 **정확히 일치**로 좁아진다
    (`d=2` → [2, 2]). 그것도 그대로 둔다 — 성긴 발화에서 짝이 줄어드는 것은
    **감춰야 할 것이 아니라 세어서 낼 것**이다(§14 의 짝 프로파일).
    """
    lo = int(np.ceil(d * (1.0 - tol)))
    hi = int(np.floor(d * (1.0 + tol)))
    return max(lo, 1), max(hi, 1)


# --------------------------------------------------------------------------- #
# 2. 층화 색인 — 밀도로 칸을 나누고, 칸 안에서 변동성으로 정렬
# --------------------------------------------------------------------------- #
def stratified_index(uni: dict, syms: list, *, band_key: str = BAND_KEY,
                     strat_key: str = DENSITY_KEY) -> dict:
    """종목마다 `{밀도값: 변동성으로 정렬된 후보}`.

    2 차원 밴드를 sorted-array 두 번으로 못 여니 **밀도로 층을 나눈다.** `nbar60` 은
    1~`lookback+1` 의 정수라 층이 유계다 — 그래서 이 표현이 성립한다.

    후보 자격은 `pool_index(require_key=True)` 와 **똑같다**(`rv60` 이 유한하고 0 이 아님).
    사다리의 네 팔이 **같은 풀**에서 뽑아야 칸 사이의 차이가 밴드 하나가 된다.
    """
    by_sym: dict = {}
    n_all = 0
    n_pool = 0
    for si, s in enumerate(syms):
        u = uni[s]
        v = np.asarray(u[band_key], dtype="float64")
        d = np.asarray(u[strat_key], dtype="float64")
        n_all += int(v.size)
        f = np.flatnonzero(np.isfinite(v) & (v > 0) & np.isfinite(d) & (d > 0))
        buckets: dict = {}
        if f.size:
            dv = d[f].astype("int64")
            order = np.argsort(dv, kind="stable")
            f_s, dv_s = f[order], dv[order]
            edges = np.flatnonzero(np.r_[True, dv_s[1:] != dv_s[:-1]])
            for a, b in zip(edges, np.r_[edges[1:], dv_s.size]):
                m = f_s[a:b]
                vals = v[m]
                o = np.argsort(vals, kind="stable")
                buckets[int(dv_s[a])] = {
                    "vals": vals[o],
                    "sym": np.full(m.size, si, dtype="int64"),
                    "bar": m[o],
                }
            n_pool += int(f.size)
        by_sym[si] = buckets
    return {"band_key": band_key, "strat_key": strat_key, "by_sym": by_sym,
            "n_candidate_bars": n_pool, "n_bars_total": n_all,
            "n_dropped_key_missing": n_all - n_pool}


def draw_stratified(uni: dict, syms: list, index: dict,
                    fire_sym: np.ndarray, fire_bar: np.ndarray, *,
                    match_band: bool, match_strat: bool,
                    tol: float = VOL_MATCH_TOL, strat_tol: float = DENSITY_TOL,
                    draws: int = MATCH_DRAWS, gap_s: int = SELF_GAP_S,
                    rng: np.random.Generator | None = None,
                    max_redraw: int = MAX_REDRAW) -> dict:
    """발화마다 위약 막대를 `draws` 개씩. **밴드 두 개를 따로 켜고 끈다.**

    `draw_controls` 와 **같은 규약**이다 — 간격 배제, 재추첨 상한, 사유별 짝 잃음
    집계가 전부 같다. 다른 것은 후보 범위가 층 여럿에 걸친다는 것뿐이고, 층을 이어
    붙인 뒤 **균등하게** 뽑으므로 한 층짜리(= `draw_controls`)와 같은 추첨이다.
    """
    rng = rng or np.random.default_rng(SEED)
    bk, sk = index["band_key"], index["strat_key"]
    n = int(fire_sym.size)
    paired = np.zeros(n, dtype=bool)
    o_sym, o_bar, o_slot = [], [], []
    n_missing_key = n_empty_band = n_gap_only = 0
    band_sizes = []

    for i in range(n):
        si = int(fire_sym[i])
        bi = int(fire_bar[i])
        u = uni[syms[si]]
        v = float(u[bk][bi])
        d = float(u[sk][bi])
        if not np.isfinite(v) or v <= 0 or not np.isfinite(d) or d <= 0:
            n_missing_key += 1
            continue
        buckets = index["by_sym"].get(si, {})
        if not buckets:
            n_empty_band += 1
            continue
        if match_strat:
            lo_d, hi_d = density_band(d, strat_tol)
            keys = [k for k in buckets if lo_d <= k <= hi_d]
        else:
            keys = list(buckets)
        segs = []
        total = 0
        for k in keys:
            vals = buckets[k]["vals"]
            if match_band:
                a = int(np.searchsorted(vals, v * (1.0 - tol), side="left"))
                b = int(np.searchsorted(vals, v * (1.0 + tol), side="right"))
            else:
                a, b = 0, int(vals.size)
            if b > a:
                segs.append((k, a, b))
                total += b - a
        if total == 0:
            n_empty_band += 1
            continue
        band_sizes.append(total)
        offs = np.cumsum([s[2] - s[1] for s in segs])
        t0 = int(u["ts"][bi])
        got = 0
        for _ in range(draws * max_redraw):
            if got >= draws:
                break
            p = int(rng.integers(0, total))
            j = int(np.searchsorted(offs, p, side="right"))
            k, a, _b = segs[j]
            local = a + p - (int(offs[j - 1]) if j else 0)
            cs = int(buckets[k]["sym"][local])
            cb = int(buckets[k]["bar"][local])
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
        "match_band": bool(match_band), "match_strat": bool(match_strat),
        "tol": float(tol) if match_band else None,
        "strat_tol": float(strat_tol) if match_strat else None,
    }


# --------------------------------------------------------------------------- #
# 3. 밀도 균형 — **맞췄다는 주장이 아니라 표로**
# --------------------------------------------------------------------------- #
def density_balance(uni: dict, syms: list, sym_id: np.ndarray,
                    bar: np.ndarray) -> dict:
    """`balance_table` 이 안 내는 것 하나: **체결 건수**(검열된 감시 항목).

    `balance_table` 은 `nbar60` 까지만 낸다. 막대 수를 맞췄을 때 체결 건수도 따라
    맞는지는 **다른 물음**이고, 안 맞으면 그 사실을 적어야 한다.
    """
    m = int(sym_id.size)
    if m == 0:
        return {"n": 0}
    nt = np.empty(m, dtype="float64")
    nb = np.empty(m, dtype="float64")
    for si in np.unique(sym_id):
        sel = np.flatnonzero(sym_id == si)
        u = uni[syms[int(si)]]
        nt[sel] = u["ntrade60"][bar[sel]]
        nb[sel] = u["nbar60"][bar[sel]]
    return {
        "n": m,
        "ntrade60": pct_table(nt),
        "nbar60": pct_table(nb),
        "trades_per_bar_p50": float(np.median(nt / np.maximum(nb, 1.0))),
        "censoring_note": (
            f"체결 건수는 4초 칸당 {TRADES_COUNT_CAP}건 상한에 검열돼 있다"
            "(`docs/41` §4). 위쪽이 눌린 값이라 **정합 키로 쓰지 않았다** — "
            "여기 있는 것은 막대 수를 맞췄을 때 이 값이 따라 맞는지 보는 감시다."),
    }


# --------------------------------------------------------------------------- #
# 4. 눈금 — §13 의 접는 기계를 그대로 쓴다
# --------------------------------------------------------------------------- #
def metric_two_scales(uni: dict, syms: list, sym_id: np.ndarray,
                      values: np.ndarray) -> dict:
    """한 지표를 **두 눈금으로**. `scale_mixed_recount.quantity_two_scales` 를 그대로 쓴다.

    §12-8 (A) 는 §11 의 팔들을 "이미 발화 가중"으로 분류했다. 위약이 **발화당 3개**씩
    뽑히므로 종목 구성이 발화와 같기 때문이다. 그 분류가 맞으면 `fire_weighted` 칸이
    §11-4 가 실은 값과 **같은 수**여야 한다 — 그것을 재현 검사로 확인한다.
    """
    return quantity_two_scales(sym_id, np.asarray(values, dtype="float64"), len(syms))


def arm_two_scale_table(uni: dict, syms: list, sym_id: np.ndarray,
                        outcomes: dict) -> dict:
    """§11-4 표의 칸 전부를 두 눈금으로."""
    return {k: metric_two_scales(uni, syms, sym_id, outcomes[k])
            for k in REPORT_METRICS if k in outcomes}


def contamination_diagnostic(uni: dict, syms: list,
                             fire_sym: np.ndarray, fire_bar: np.ndarray,
                             a_sym: np.ndarray, a_bar: np.ndarray,
                             outcomes: dict, *,
                             metric: str = HEADLINE_METRIC) -> dict:
    """뽑힌 위약 막대 중 **그 자체가 발화 막대**인 것을 빼면 값이 어디로 가는가.

    §11-8 7번은 그 비율을 **세기만** 했다(2~5%). 밴드를 둘 다 걸면 후보가 발화 쪽으로
    더 몰리므로, 그 비율이 오르면 **위약이 실제 쪽으로 끌려가 격차가 줄어든다** —
    즉 이 오염은 이 절의 결론과 **같은 방향**으로 작용한다. 그래서 세는 것으로는
    부족하고, **빼 보고 얼마나 움직이는지**를 같이 내야 한다.

    **이것은 새 팔이 아니다.** 이미 뽑은 표본에 사후 필터를 걸어 본 진단이다 —
    거르고 다시 뽑으면 정합의 뜻이 바뀐다(§11-8 7번).
    """
    if a_sym.size == 0:
        return {"n": 0}
    fires = set(zip(fire_sym.tolist(), fire_bar.tolist()))
    is_fire = np.fromiter(((int(s), int(b)) in fires
                           for s, b in zip(a_sym.tolist(), a_bar.tolist())),
                          dtype=bool, count=a_sym.size)
    v = np.asarray(outcomes[metric], dtype="float64")
    full = metric_two_scales(uni, syms, a_sym, v)["fire_weighted"]["mean"]
    kept = (metric_two_scales(uni, syms, a_sym[~is_fire], v[~is_fire])
            ["fire_weighted"]["mean"]) if (~is_fire).any() else None
    dropped = (metric_two_scales(uni, syms, a_sym[is_fire], v[is_fire])
               ["fire_weighted"]["mean"]) if is_fire.any() else None
    return {
        "n": int(a_sym.size), "metric": metric,
        "n_control_is_a_fire_bar": int(is_fire.sum()),
        "share_control_is_a_fire_bar": float(is_fire.mean()),
        "mean_all_draws": full,
        "mean_excluding_fire_bars": kept,
        "mean_of_the_fire_bar_draws_only": dropped,
        "move_pp": None if None in (full, kept) else (kept - full) * 100.0,
        "reading": ("`move_pp` 가 음수면 발화 막대를 빼자 위약이 **내려간다** — 그 오염이 "
                    "위약을 위로 밀고 있었다는 뜻이고, 그러면 격차는 **더 벌어진다.** "
                    "양수면 반대다. 어느 쪽이든 크기가 격차보다 작아야 결론을 읽을 수 있다."),
    }


# --------------------------------------------------------------------------- #
# 5. 사다리 블록 — 네 팔 + 실제를 **같은 발화 집합** 위에
# --------------------------------------------------------------------------- #
def density_ladder(uni: dict, syms: list, index_flat: dict, index_strat: dict,
                   fire_sym: np.ndarray, fire_bar: np.ndarray, *,
                   tol: float = VOL_MATCH_TOL, strat_tol: float = DENSITY_TOL,
                   draws: int = MATCH_DRAWS, seed: int = SEED,
                   lag_s: float = DETECT_LAG_S) -> dict:
    """**가장 엄격한 팔에서 짝 지은 발화** 위에서만 다섯 팔을 비교한다.

    §11 이 정한 규율 그대로다(§11-8 6번): 짝을 못 지은 발화를 다른 팔에서도 빼야
    팔들이 같은 모집단이 된다. 여기서는 **밴드 둘을 다 건 팔**이 가장 엄격하므로
    그 팔의 짝으로 모집단을 정한다. **뺀 쪽이 어떤 발화였는지는 아래에 낸다.**
    """
    # ---- 각 팔이 **전체 발화 위에서** 얼마나 짝을 짓는가 (밴드별 비용) --------- #
    cost: dict = {}
    probe_draws = {
        "placebo_unmatched": lambda: draw_controls(
            uni, syms, index_flat, fire_sym, fire_bar, matched=False,
            draws=draws, rng=np.random.default_rng(seed)),
        "placebo_vol_matched": lambda: draw_controls(
            uni, syms, index_flat, fire_sym, fire_bar, matched=True, tol=tol,
            draws=draws, rng=np.random.default_rng(seed)),
        "placebo_density_matched": lambda: draw_stratified(
            uni, syms, index_strat, fire_sym, fire_bar, match_band=False,
            match_strat=True, strat_tol=strat_tol, draws=draws,
            rng=np.random.default_rng(seed)),
        "placebo_vol_density_matched": lambda: draw_stratified(
            uni, syms, index_strat, fire_sym, fire_bar, match_band=True,
            match_strat=True, tol=tol, strat_tol=strat_tol, draws=draws,
            rng=np.random.default_rng(seed)),
    }
    full: dict = {}
    for tag, fn in probe_draws.items():
        d = fn()
        full[tag] = d
        cost[tag] = {
            "n_fires": d["n_fires"], "n_paired": d["n_paired"],
            "n_unpaired": d["n_unpaired"],
            "share_unpaired": (float(d["n_unpaired"] / d["n_fires"])
                               if d["n_fires"] else None),
            "unpaired_key_missing": d["unpaired_key_missing"],
            "unpaired_empty_band": d["unpaired_empty_band"],
            "unpaired_gap_excluded_only": d["unpaired_gap_excluded_only"],
            "band_size_bars": d["band_size"],
        }

    strict = full["placebo_vol_density_matched"]
    keep = strict["paired"]
    f_sym, f_bar = fire_sym[keep], fire_bar[keep]

    # ---- 공통 모집단 위에서 팔을 다시 뽑는다 -------------------------------- #
    arms: dict = {"real": (f_sym, f_bar)}
    redrawn: dict = {}
    redrawn["placebo_unmatched"] = draw_controls(
        uni, syms, index_flat, f_sym, f_bar, matched=False, draws=draws,
        rng=np.random.default_rng(seed))
    redrawn["placebo_vol_matched"] = draw_controls(
        uni, syms, index_flat, f_sym, f_bar, matched=True, tol=tol, draws=draws,
        rng=np.random.default_rng(seed))
    redrawn["placebo_density_matched"] = draw_stratified(
        uni, syms, index_strat, f_sym, f_bar, match_band=False, match_strat=True,
        strat_tol=strat_tol, draws=draws, rng=np.random.default_rng(seed))
    redrawn["placebo_vol_density_matched"] = draw_stratified(
        uni, syms, index_strat, f_sym, f_bar, match_band=True, match_strat=True,
        tol=tol, strat_tol=strat_tol, draws=draws, rng=np.random.default_rng(seed))
    for tag, d in redrawn.items():
        arms[tag] = (d["sym"], d["bar"])

    block: dict = {
        "band_key": index_strat["band_key"], "strat_key": index_strat["strat_key"],
        "pool_mode": "same_symbol", "tol": tol, "strat_tol": strat_tol,
        "draws_per_fire": draws, "seed": seed,
        "population": {
            "n_fires_all": int(fire_sym.size),
            "n_fires_kept": int(keep.sum()),
            "kept_by": "placebo_vol_density_matched (가장 엄격한 팔)",
            "note": ("§11-8 6번과 같은 규율 — 짝을 못 지은 발화는 **다른 팔 전부에서** "
                     "뺐다. 팔마다 모집단이 다르면 표를 가로로 못 읽는다."),
            "who_was_dropped": unpaired_fire_profile(uni, syms, fire_sym, fire_bar,
                                                     keep),
        },
        "pairing_cost_by_band": cost,
        "pairing_cost_reading": (
            "**전체 발화 위에서** 각 팔이 혼자 얼마나 짝을 잃는지다. 밴드를 더할 때마다 "
            "얼마를 잃는지가 여기 보인다 — 공통 모집단(위 `population`)은 그중 가장 "
            "엄격한 팔이 정한다."),
        "entry_outcomes": {}, "balance": {}, "density_balance": {},
        "control_overlap_with_fires": {}, "contamination": {}, "two_scales": {},
    }
    for tag, (a_sym, a_bar) in arms.items():
        out = arm_entry_outcomes(uni, syms, a_sym, a_bar, lag_s=lag_s)
        block["entry_outcomes"][tag] = summarize_entry(out)
        block["balance"][tag] = balance_table(uni, syms, a_sym, a_bar)
        block["density_balance"][tag] = density_balance(uni, syms, a_sym, a_bar)
        block["two_scales"][tag] = arm_two_scale_table(uni, syms, a_sym, out)
        if tag != "real":
            block["control_overlap_with_fires"][tag] = fire_overlap(
                f_sym, f_bar, a_sym, a_bar)
            block["contamination"][tag] = contamination_diagnostic(
                uni, syms, f_sym, f_bar, a_sym, a_bar, out)
    return block


def gap_ladder(block: dict, *, metric: str = HEADLINE_METRIC) -> dict:
    """`실제 − 위약` 을 **팔마다, 눈금마다**. §11-4 가 읽은 그 격차다."""
    real = block["two_scales"]["real"].get(metric, {})
    out: dict = {"metric": metric}
    for tag in ARM_ORDER:
        if tag == "real" or tag not in block["two_scales"]:
            continue
        arm = block["two_scales"][tag].get(metric, {})
        row: dict = {}
        for scale in ("fire_weighted", "symbol_uniform"):
            r = real.get(scale, {}).get("mean")
            p = arm.get(scale, {}).get("mean")
            row[scale] = {
                "real": r, "placebo": p,
                "real_minus_placebo_pp": None if None in (r, p) else (r - p) * 100.0,
            }
        out[tag] = row
    out["how_to_read"] = (
        "음수면 **위약이 더 높다**(§11-4 가 읽은 방향). 사다리를 따라 내려가면서 "
        "그 값이 0 쪽으로 오는지, 지나쳐 양수가 되는지, 그대로인지만 본다. "
        "**판정하지 않는다.**")
    return out


# --------------------------------------------------------------------------- #
# 6. 민감도 — 씨앗과 허용 오차
# --------------------------------------------------------------------------- #
def _headline_row(block: dict, metric: str) -> dict:
    return {tag: block["two_scales"][tag].get(metric, {}).get(
        "fire_weighted", {}).get("mean")
        for tag in ARM_ORDER if tag in block["two_scales"]}


def seed_sensitivity(uni: dict, syms: list, index_flat: dict, index_strat: dict,
                     fire_sym: np.ndarray, fire_bar: np.ndarray, *,
                     seeds: tuple[int, ...] = SEED_SWEEP,
                     tol: float = VOL_MATCH_TOL,
                     strat_tol: float = DENSITY_TOL,
                     metric: str = HEADLINE_METRIC) -> dict:
    """**추첨 잡음의 폭.** 실제 팔은 추첨이 없지만 **모집단이 씨앗마다 달라진다** —
    가장 엄격한 팔의 짝이 씨앗에 안 흔들려도 간격 배제 재추첨이 흔들리기 때문이다.
    그래서 실제 팔도 같이 싣는다. **표본 CI 가 아니다.**"""
    per_seed: dict = {}
    kept: dict = {}
    for s in seeds:
        b = density_ladder(uni, syms, index_flat, index_strat, fire_sym, fire_bar,
                           tol=tol, strat_tol=strat_tol, seed=s)
        row = _headline_row(b, metric)
        # **격차 자체의 산포**가 필요하다. 팔마다 따로 재면 두 산포를 어떻게 합칠지
        # 또 정해야 하는데, 격차는 씨앗마다 한 값이라 그냥 재면 된다.
        real = row.get("real")
        for tag in list(row):
            if tag != "real" and row[tag] is not None and real is not None:
                row[f"gap_vs_{tag}_pp"] = (real - row[tag]) * 100.0
        per_seed[str(s)] = row
        kept[str(s)] = b["population"]["n_fires_kept"]
    spread: dict = {}
    for field in next(iter(per_seed.values())):
        vals = [v[field] for v in per_seed.values() if v.get(field) is not None]
        if vals:
            unit = 1.0 if field.endswith("_pp") else 100.0
            spread[field] = {"min": min(vals), "max": max(vals),
                             "spread_pp": (max(vals) - min(vals)) * unit,
                             "mean": float(np.mean(vals))}
    return {"seeds": list(seeds), "metric": metric, "per_seed": per_seed,
            "n_fires_kept_per_seed": kept, "spread": spread,
            "not_a_ci": "추첨 잡음이지 표본 CI 가 아니다. 표본은 여전히 2.6 정규장이다."}


def tolerance_sensitivity(uni: dict, syms: list, index_flat: dict,
                          index_strat: dict, fire_sym: np.ndarray,
                          fire_bar: np.ndarray, *,
                          tols: tuple[float, ...] = TOL_SWEEP,
                          seed: int = SEED,
                          metric: str = HEADLINE_METRIC) -> dict:
    """밀도 밴드를 좁히고 넓히면 어디로 가는가. **변동성 밴드는 ±20% 로 고정**한다 —
    둘을 같이 흔들면 어느 쪽이 움직였는지 또 못 가른다."""
    out: dict = {}
    for t in tols:
        b = density_ladder(uni, syms, index_flat, index_strat, fire_sym, fire_bar,
                           tol=VOL_MATCH_TOL, strat_tol=t, seed=seed)
        out[f"{t:.2f}"] = {**_headline_row(b, metric),
                           "n_fires_kept": b["population"]["n_fires_kept"],
                           "density_p50_matched": b["density_balance"][
                               "placebo_vol_density_matched"]["nbar60"].get("p50"),
                           "density_p50_real": b["density_balance"]["real"][
                               "nbar60"].get("p50")}
    out["note"] = ("변동성 밴드는 ±20% 고정. 움직이는 것은 밀도 밴드 하나다.")
    return out


# --------------------------------------------------------------------------- #
# 7. 재현 검사 — §11-4 의 두 팔이 그대로 나오는가
# --------------------------------------------------------------------------- #
def reproduction_check(uni: dict, syms: list, index_flat: dict,
                       fire_sym: np.ndarray, fire_bar: np.ndarray, *,
                       tol: float = VOL_MATCH_TOL, draws: int = MATCH_DRAWS,
                       seed: int = SEED, metric: str = HEADLINE_METRIC) -> dict:
    """**§11-4 를 그 모집단에서 그대로 재현**하고, 발화 가중이 그 값과 같은지 본다.

    두 가지를 한 번에 지킨다:
      1. `draw_controls` 를 §11 과 **같은 인자**로 부르면 §11-4 의 팔이 나온다
         (모집단은 §11 처럼 **정합 팔의 짝**으로 정한다).
      2. §13 의 `fire_weighted` 접기가 그 팔의 **이어 붙인 평균과 같은 수**다 —
         §12-8 (A) 의 "§11 은 이미 발화 가중" 분류가 맞다는 증거다.
    """
    m = draw_controls(uni, syms, index_flat, fire_sym, fire_bar, matched=True,
                      tol=tol, draws=draws, rng=np.random.default_rng(seed))
    keep = m["paired"]
    f_sym, f_bar = fire_sym[keep], fire_bar[keep]
    remap = np.full(fire_sym.size, -1, dtype="int64")
    remap[keep] = np.arange(int(keep.sum()), dtype="int64")
    ok = remap[m["slot"]] >= 0
    out: dict = {"n_fires_paired_section11_rule": int(keep.sum())}
    for tag, (a_sym, a_bar) in (("real", (f_sym, f_bar)),
                                ("placebo_vol_matched",
                                 (m["sym"][ok], m["bar"][ok]))):
        o = arm_entry_outcomes(uni, syms, a_sym, a_bar)
        pooled = summarize_entry(o)[metric]["mean"]
        folded = metric_two_scales(uni, syms, a_sym, o[metric])["fire_weighted"]["mean"]
        out[tag] = {
            "pooled_mean_as_section11": pooled,
            "fire_weighted_fold": folded,
            "same": (pooled is not None and folded is not None
                     and abs(pooled - folded) < 1e-12),
        }
    out["what_this_guards"] = (
        "**§11 의 두 팔이 §11 의 규칙으로 그대로 나와야 한다**(모집단 = 정합 팔의 짝). "
        "그리고 발화 가중 접기가 이어 붙인 평균과 **같은 수**여야 한다 — §12-8 (A) 의 "
        "분류가 맞다는 증거다. 창이 §11 때보다 늘어 소수점은 안 맞는다. "
        "**§14 의 표는 이보다 좁은 모집단**(밴드 둘 다 건 팔의 짝) 위에 있다.")
    return out


# --------------------------------------------------------------------------- #
# 8. 조립
# --------------------------------------------------------------------------- #
def build_report(conn: sqlite3.Connection, *, rise: float = SHOT_RISE,
                 max_seconds: int = SHOT_MAX_SECONDS,
                 tol: float = VOL_MATCH_TOL, strat_tol: float = DENSITY_TOL,
                 seed: int = SEED) -> dict:
    t0 = time.time()
    syms_all = select_symbols(conn)
    bars = load_bars(conn, syms_all)
    syms = [s for s in syms_all if s in bars]
    print(f"  [1/6] second bars: {len(syms)} symbols  ({time.time()-t0:.0f}s)")
    uni0 = build_universe(bars)
    uni = add_trade_count(uni0, bars)
    n_bars = sum(int(uni[s]["ts"].size) for s in syms)
    print(f"  [2/6] keys on {n_bars} bars (+ntrade60)  ({time.time()-t0:.0f}s)")
    fire_sym, fire_bar = collect_fires(uni, syms, rise=rise, max_seconds=max_seconds)
    print(f"  [3/6] fires: {fire_sym.size}  ({time.time()-t0:.0f}s)")

    index_flat = pool_index(uni, syms, BAND_KEY, "same_symbol")
    index_strat = stratified_index(uni, syms, band_key=BAND_KEY,
                                   strat_key=DENSITY_KEY)
    print(f"  [4/6] pools: flat {index_flat['n_candidate_bars']} / strat "
          f"{index_strat['n_candidate_bars']}  ({time.time()-t0:.0f}s)")

    repro = reproduction_check(uni, syms, index_flat, fire_sym, fire_bar,
                               tol=tol, seed=seed)
    block = density_ladder(uni, syms, index_flat, index_strat, fire_sym, fire_bar,
                           tol=tol, strat_tol=strat_tol, seed=seed)
    print(f"  [5/6] ladder: kept {block['population']['n_fires_kept']}"
          f"/{fire_sym.size}  ({time.time()-t0:.0f}s)")
    sweep = seed_sensitivity(uni, syms, index_flat, index_strat, fire_sym, fire_bar,
                             tol=tol, strat_tol=strat_tol)
    tolsens = tolerance_sensitivity(uni, syms, index_flat, index_strat,
                                    fire_sym, fire_bar, seed=seed)
    print(f"  [6/6] sensitivity done  ({time.time()-t0:.0f}s)")

    return {
        "question": (
            "docs/44 §11-4 의 'MFE 는 정합을 걸수록 위약 쪽이 올라간다'(실제 +1.66% vs "
            "정합 위약 +1.89%)가 **테이프 밀도 탓인가.** §11-3 이 실제 26건 vs 위약 "
            "35건으로 진단했다. **밀도까지 맞춘 위약으로 다시 낸다.**"),
        "not_a_verdict": (
            "**판정하지 않는다.** 어떤 설계도 살리거나 죽이지 않는다. 여기 있는 것은 "
            "'밀도를 맞추면 이 숫자가 이렇게 움직인다' 까지다."),
        "conditions": {
            "window_start_ms": WINDOW_START_MS,
            "window_end_ms": int(max(int(uni[s]["ts"][-1]) for s in syms)),
            "symbols": len(syms), "second_bars": n_bars,
            "rise": rise, "max_seconds": max_seconds,
            "n_fires_all": int(fire_sym.size),
            "band_key": BAND_KEY, "strat_key": DENSITY_KEY,
            "tol_vol": tol, "tol_density": strat_tol,
            "density_band_rule": (
                "정수 밴드를 **안쪽으로** 닫는다: ceil(0.8d) ~ floor(1.2d). "
                "d 가 작으면 정확히 일치로 좁아진다(d=2 → [2,2])."),
            "pool_mode": "same_symbol (§11-4 의 표와 같다)",
            "draws_per_fire": MATCH_DRAWS, "self_gap_s": SELF_GAP_S,
            "seed": seed, "seeds_swept": list(SEED_SWEEP),
            "detector": "tick_stages.find_fires — 미래 안 씀, 60초 잠금",
            "entry_rule": f"발화 + {DETECT_LAG_S}초 이후 처음 잡히는 초 막대 (§3-3 과 동일)",
            "scale": (
                "§13 의 접는 기계(`quantity_two_scales`)로 **두 눈금을 같이 낸다.** "
                "§12-8 (A) 는 §11 의 팔들을 '이미 발화 가중'으로 분류했고 "
                "`reproduction_check` 가 그 분류를 확인한다 — 즉 **1단계가 §11-4 의 "
                "숫자를 옮기지 않는다.** 옮기지 않는다는 것 자체를 표에 적는다."),
            "costs_excluded": "스프레드·수수료·슬리피지 전부 미포함",
            "no_1m_candles": "candles_1m 경로 없음 (D-10)",
            "reused_not_reimplemented": [
                "vol_matched_placebo.trailing_stats / build_universe / pool_index / "
                "draw_controls / collect_fires / arm_entry_outcomes / summarize_entry / "
                "balance_table / fire_overlap / unpaired_fire_profile",
                "scale_mixed_recount.quantity_two_scales",
                "tick_stages.find_fires / _entry_outcomes",
            ],
        },
        "why_this_density_key": {
            "chosen": DENSITY_KEY,
            "reasons": [
                "§11-3 이 진단한 바로 그 통계량이다(실제 26 vs 위약 35).",
                "`rv60` 은 창 안 로그수익률 `nbar60 − 1` 개의 표준편차다 — "
                "**밀도가 곧 그 추정량의 분모**라서 성긴 테이프에서 부푼다.",
                f"체결 건수는 4초 칸당 {TRADES_COUNT_CAP}건 상한에 검열돼 있고 그 상한은 "
                "**바쁠 때** 물린다(`docs/41` §4, 테이프 분량의 27.3%). 검열된 값으로 "
                "맞추면 '같이 상한에 걸림'을 맞추게 된다.",
            ],
            "not_chosen": {
                "ntrade60": ("정합 키로 안 썼다. 대신 **균형표의 감시 항목**으로 낸다 — "
                             "막대 수를 맞췄을 때 체결 건수도 따라 맞는지 그대로 싣는다."),
            },
        },
        "reproduction_check": repro,
        "ladder": block,
        "gap": {m: gap_ladder(block, metric=m) for m in REPORT_METRICS},
        "seed_sensitivity": sweep,
        "tolerance_sensitivity": tolsens,
        "not_claimed": [
            "**판정하지 않는다.** 어떤 설계도 살리거나 죽이지 않는다.",
            "CI 가 없다. 표본은 여전히 2.6 정규장이고 씨앗 산포는 추첨 잡음일 뿐이다.",
            "**포화 칸을 안 갈랐다.** 상한이 물린 칸에서는 막대 수 자체가 눌릴 수 있어 "
            "밀도 정합이 그쪽으로 치우칠 수 있다(§10-8 3번).",
            "**되돌아보기 창 60초는 검정하지 않은 선택**이다(§11-7 3번 그대로).",
            "**`range60` 키로는 다시 안 냈다.** §11-4 의 헤드라인 표가 `rv60` 이고, "
            "`range60` 은 방아쇠 변수 자체라 순환적이다(§11-1).",
            "**`pooled` 모드로는 다시 안 냈다.** 종목별로 접으려면 위약의 종목이 "
            "발화의 종목이어야 한다.",
            "비용 미포함.",
        ],
    }


# --------------------------------------------------------------------------- #
# 9. 화면 요약 (ASCII — JSON 쪽에 한국어 본문이 있다)
# --------------------------------------------------------------------------- #
def _f(v, nd=5):
    return "        -" if v is None else f"{v:>9.{nd}f}"


def print_report(rep: dict) -> None:
    c = rep["conditions"]
    print(f"\n=== conditions: {c['symbols']} symbols / {c['second_bars']} second bars, "
          f"end_ms {c['window_end_ms']}")
    print(f"    fires {c['n_fires_all']}, band {c['band_key']} +-{c['tol_vol']}, "
          f"strat {c['strat_key']} +-{c['tol_density']}, draws/fire "
          f"{c['draws_per_fire']}, seed {c['seed']}")

    print("\n--- reproduction (section 11 arms must come out under section 11's rule)")
    r = rep["reproduction_check"]
    print(f"  n_fires paired by the section-11 rule: "
          f"{r['n_fires_paired_section11_rule']}")
    for tag in ("real", "placebo_vol_matched"):
        v = r[tag]
        print(f"  {tag:<26} pooled {_f(v['pooled_mean_as_section11'], 6)}  "
              f"fire-weighted fold {_f(v['fire_weighted_fold'], 6)}  "
              f"same={v['same']}")

    b = rep["ladder"]
    p = b["population"]
    print(f"\n--- population: {p['n_fires_kept']}/{p['n_fires_all']} fires "
          f"(kept by {p['kept_by']})")
    wd = p["who_was_dropped"]
    for tag in ("all", "paired", "unpaired"):
        if tag in wd:
            x = wd[tag]
            print(f"    {tag:<9} n {x['n']:>5}  trailing bars p50 "
                  f"{x['trailing_bars_p50']:>5.1f}  anchor_is_max "
                  f"{x['anchor_is_max_incl_empty']:.4f}  fwd window empty "
                  f"{x['share_forward_window_empty']:.4f}")

    print("\n--- what each band costs, ON ALL FIRES (each arm alone)")
    print(f"  {'arm':<30}{'paired':>8}{'unpaired':>10}{'key_miss':>10}"
          f"{'empty_band':>12}{'gap_only':>10}{'band p50':>10}")
    for tag, v in b["pairing_cost_by_band"].items():
        print(f"  {tag:<30}{v['n_paired']:>8}{v['n_unpaired']:>10}"
              f"{v['unpaired_key_missing']:>10}{v['unpaired_empty_band']:>12}"
              f"{v['unpaired_gap_excluded_only']:>10}"
              f"{v['band_size_bars'].get('p50', 0):>10.0f}")

    print("\n--- BALANCE: did the density actually match?")
    print(f"  {'arm':<30}{'rv60 p50':>11}{'nbar60 p50':>12}{'ntrade60 p50':>14}"
          f"{'trades/bar':>12}")
    for tag in ARM_ORDER:
        if tag not in b["balance"]:
            continue
        bal, den = b["balance"][tag], b["density_balance"][tag]
        print(f"  {tag:<30}{bal['rv60'].get('p50', 0):>11.5f}"
              f"{bal['nbar60'].get('p50', 0):>12.1f}"
              f"{den['ntrade60'].get('p50', 0):>14.1f}"
              f"{den['trades_per_bar_p50']:>12.2f}")

    print("\n--- THE LADDER: section 11-4's table, one band added per rung "
          "(fire-weighted, means)")
    print(f"  {'arm':<30}{'slip':>10}{'ret30s':>10}{'ret60s':>10}"
          f"{'MFE':>10}{'MAE':>10}{'MFE+MAE':>10}")
    for tag in ARM_ORDER:
        if tag not in b["two_scales"]:
            continue
        t = b["two_scales"][tag]
        def g(k):
            return t.get(k, {}).get("fire_weighted", {}).get("mean")
        mfe, mae = g("mfe_60s"), g("mae_60s")
        tot = None if None in (mfe, mae) else mfe + mae
        print(f"  {tag:<30}{_f(g('slip_from_fire')):>10}{_f(g('ret_30s')):>10}"
              f"{_f(g('ret_60s')):>10}{_f(mfe):>10}{_f(mae):>10}{_f(tot):>10}")

    print("\n--- same ladder on the SYMBOL-UNIFORM scale (the other ruler)")
    print(f"  {'arm':<30}{'MFE':>10}{'MAE':>10}{'ret30s':>10}")
    for tag in ARM_ORDER:
        if tag not in b["two_scales"]:
            continue
        t = b["two_scales"][tag]
        def gu(k):
            return t.get(k, {}).get("symbol_uniform", {}).get("mean")
        print(f"  {tag:<30}{_f(gu('mfe_60s')):>10}{_f(gu('mae_60s')):>10}"
              f"{_f(gu('ret_30s')):>10}")

    print("\n--- THE GAP: real minus placebo (negative = placebo higher)")
    for metric in ("mfe_60s", "ret_30s"):
        g = rep["gap"][metric]
        print(f"  [{metric}]")
        for tag in ARM_ORDER:
            if tag == "real" or tag not in g:
                continue
            row = g[tag]
            fw = row["fire_weighted"]["real_minus_placebo_pp"]
            su = row["symbol_uniform"]["real_minus_placebo_pp"]
            print(f"    vs {tag:<30} fire-wtd {fw:+8.3f}pp   "
                  f"sym-unif {su:+8.3f}pp")

    print("\n--- contamination: placebo bars that ARE fire bars, and what dropping "
          "them does")
    print(f"  {'arm':<30}{'share':>9}{'mean all':>11}{'mean excl':>11}{'move':>10}")
    for tag in ARM_ORDER:
        v = b["contamination"].get(tag)
        if not v or not v.get("n"):
            continue
        print(f"  {tag:<30}{v['share_control_is_a_fire_bar']:>9.4f}"
              f"{_f(v['mean_all_draws'], 6):>11}"
              f"{_f(v['mean_excluding_fire_bars'], 6):>11}"
              f"{v['move_pp']:>+9.3f}pp")

    print("\n--- seed sensitivity of the headline AND of the gap "
          "(NOT a confidence interval)")
    for field, s in rep["seed_sensitivity"]["spread"].items():
        if field.endswith("_pp"):
            print(f"  {field:<40} min {s['min']:+8.3f}pp  max {s['max']:+8.3f}pp  "
                  f"spread {s['spread_pp']:.3f}pp  mean {s['mean']:+8.3f}pp")
        else:
            print(f"  {field:<40} min {_f(s['min'], 6)}  max {_f(s['max'], 6)}  "
                  f"spread {s['spread_pp']:.3f}pp")
    print("  fires kept per seed: " + "  ".join(
        f"{k} {v}" for k, v in rep["seed_sensitivity"]["n_fires_kept_per_seed"].items()))

    print("\n--- density tolerance sensitivity (vol band fixed at +-20%)")
    for t, v in rep["tolerance_sensitivity"].items():
        if not isinstance(v, dict):
            continue
        print(f"  tol {t}  kept {v['n_fires_kept']:>5}  "
              f"real MFE {_f(v.get('real'), 6)}  "
              f"vol+density MFE {_f(v.get('placebo_vol_density_matched'), 6)}  "
              f"nbar60 p50 real {v['density_p50_real']:>5.1f} / "
              f"matched {v['density_p50_matched']:>5.1f}")

    print("\n=== not claimed (Korean text in the JSON)")
    print("  - no verdict; no design is kept or killed here")
    print("  - no CI; sample is still 2.6 regular sessions; seed spread is draw noise")
    print("  - saturated cells NOT separated - the cap can flatten bar counts too")
    print("  - range60 key and pooled mode NOT re-run (see JSON)")


def main(db: Path, *, out_dir: Path | None = None) -> int:
    out_dir = out_dir or OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    print("=== density-matched placebo D-13 (read-only, no live calls)")
    conn = open_ro(db)
    try:
        rep = build_report(conn)
    finally:
        conn.close()
    (out_dir / "density_matched_placebo.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print_report(rep)
    print(f"\nwrote {out_dir / 'density_matched_placebo.json'}")
    return 0


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/tossmon.db")
    raise SystemExit(main(path))

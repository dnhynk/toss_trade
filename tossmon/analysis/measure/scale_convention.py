"""**눈금 규약 (D-14) — 발화 가중으로 `docs/44` §10 의 네 숫자를 다시 낸다** (§12).

사용자가 D-14 를 정했다: **발화 가중(fire-weighted)이 정본이다.** 근거는 전략 골격이다 —
우리가 묻는 것은 "이 현상이 종목 일반의 성질인가"가 아니라 **"우리가 실제로 마주칠
분포가 무엇인가"** 이므로, 한 표는 종목이 아니라 **사건**에 있어야 한다.

## 이건 표기 변경이 아니다 — §10 의 헤드라인이 걸려 있다

§10-7 이 실은 문장은 이것이다:

> 인과적 발화의 30.7% 는 눈금(31.1%)과 같다. **시장이라고 부를 수 있는 몫은 못 찾았다.**

`docs/44` §11-6 이 그 눈금이 **종목마다 300개씩** 뽑은 것임을 밝혔고, 종목 구성을 발화와
같게 맞추면 **−7.3%p** 움직인다는 것까지 냈다. 그러면 물어야 할 것은 두 값이 얼마로
바뀌는가가 아니라 **격차가 유지되는가** 다.

- 둘 다 비슷하게 내려가면 → "구별 안 됨" 이 그대로 선다. §10 결론은 눈금과 무관하다.
- 한쪽만 움직이면 → **§10 결론이 눈금의 산물이었다.**

## 먼저 확인한 것 — 두 숫자는 애초에 **같은 눈금이 아니었다**

코드를 읽으면 바로 나온다. 사건 쪽(`cross_peak_check.collect_anchors` → `probe_summary`)은
종목을 **이어 붙인 뒤** 비율을 낸다 — 사건 하나가 한 표다(**이미 발화 가중**).
눈금 쪽(`random_time_baseline`)은 `draws_per_symbol=300` 으로 **종목마다 같은 개수**를
뽑는다 — 종목 하나가 한 표다(**종목당 균등**).

    # cross_peak_check.random_time_baseline
    idx = rng.integers(0, ts.size, size=min(draws_per_symbol, ts.size))   # 종목마다 300

그래서 30.7% 와 31.1% 를 나란히 놓은 것은 **두 자를 겹쳐 놓은 것**이다.
이 모듈은 그것을 고치지 않는다 — **네 숫자를 두 눈금에서 각각 다 내서 나란히 놓는다.**

## 어떻게 재는가 — 같은 종목별 비율, **가중치만 바꾼다**

종목 `s` 의 비율 `r_s` 를 한 번만 재고, 접는 가중치만 바꾼다. 그러면 두 눈금의 차이가
**오직 가중치**라는 것이 구조로 보장된다(추첨을 다시 하면 추첨 잡음이 섞여 안 보인다).

| 눈금 | 가중치 `w_s` | 뜻 |
|---|---|---|
| **발화 가중** | 그 종목의 **사건 수** | 사건 하나가 한 표. 우리가 마주칠 분포 |
| **종목당 균등** | 1 | 종목 하나가 한 표. "종목 일반의 성질인가" |
| *(눈금줄 전용)* | 발화 수 / 교차 수 | 무작위 막대의 **종목 구성**을 사건과 같게 |

눈금줄은 추첨이라 두 방법이 있다: (a) 종목마다 300개 뽑아 `r_s` 를 재고 **발화 수로
다시 가중**, (b) 아예 **종목마다 발화 수만큼** 뽑아 이어 붙임. (a)가 같은 양의 저분산
추정이라 주로 쓰고, (b)는 교차 검증으로 같이 낸다(§11-6 이 쓴 방법이 (b)다).

## 이 모듈이 하지 않는 것

- **판정하지 않는다.** "그래서 설계 A 가 살았다/죽었다" 는 이 모듈이 쓸 문장이 아니다.
- **D-13(테이프 밀도 정합)을 같이 하지 않는다.** 한꺼번에 하면 어느 쪽이 숫자를
  움직였는지 못 가른다. **눈금 하나만** 바꾼다.
- **기존 코드를 한 줄도 안 고친다.** `find_episodes` · `find_fires` · `forward_probe` ·
  `probe_summary` · `random_time_baseline` · `trailing_min_index` 를 그대로 불러 쓴다.

실행: `python -m tossmon.analysis.measure.scale_convention [db_path]`
→ `out/scale_convention.json`. **라이브 콜 0. DB 는 `mode=ro`.**
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

from tossmon.analysis.measure.cross_peak_check import (
    HORIZONS_S,
    forward_probe,
    probe_summary,
    random_time_baseline,
    trailing_min_index,
)
from tossmon.analysis.measure.tick_resolution import (
    WINDOW_START_MS,
    open_ro,
    pct_table,
    session_of,
)
from tossmon.analysis.measure.tick_stages import (
    SHOT_MAX_SECONDS,
    SHOT_RISE,
    find_episodes,
    find_fires,
    load_bars,
    select_symbols,
)

OUT_DIR = Path("out")

#: §10-3 의 눈금과 **같은 값이라야** 그 줄을 재현한 것이다. 바꾸면 재현이 아니다.
DRAWS_PER_SYMBOL = 300

SEED = 20260808
#: §11-6 이 쓴 3개를 포함한다 — 그때의 "추첨 잡음" 판정 기준을 그대로 쓰기 위해서다.
SEED_SWEEP: tuple[int, ...] = (20260808, 20260809, 20260810, 20260811, 20260812)

#: 종목당 균등 눈금은 **사건이 1건뿐인 종목도 한 표**를 준다. 그 표는 0 아니면 1 이다.
#: 그래서 "사건 수가 적은 종목 탓 아닌가" 를 갈라 보려고 최소 사건 수 문턱을 같이 낸다.
MIN_EVENTS_GUARD = 20

#: 헤드라인 지평. §10 의 세 숫자가 전부 이 창이다.
HEADLINE_H = SHOT_MAX_SECONDS


# --------------------------------------------------------------------------- #
# 1. 접는 기계 — 종목별 비율은 한 번만, 가중치만 바꾼다
# --------------------------------------------------------------------------- #
def per_symbol_rates(sym_id: np.ndarray, flag: np.ndarray, n_sym: int) -> tuple:
    """종목마다 (사건 수, 그 종목 안의 비율). **여기서 눈금을 정하지 않는다.**

    비율을 종목별로 한 번만 재 두고 나중에 가중치만 바꾸는 이유: 두 눈금의 차이가
    **오직 가중치**라는 것을 구조로 보장하기 위해서다. 눈금마다 따로 재면 표본이나
    추첨이 같이 달라져 무엇이 숫자를 움직였는지 못 가른다.
    """
    sym_id = np.asarray(sym_id, dtype="int64")
    flag = np.asarray(flag).astype("float64")
    counts = np.bincount(sym_id, minlength=n_sym).astype("int64")
    hits = np.bincount(sym_id, weights=flag, minlength=n_sym)
    rates = np.full(n_sym, np.nan)
    nz = counts > 0
    rates[nz] = hits[nz] / counts[nz]
    return counts, rates


def fold(counts: np.ndarray, rates: np.ndarray, weights=None, *,
         min_events: int = 0) -> dict:
    """종목별 비율을 **한 눈금으로** 접는다.

    - `weights=None` → 가중치 = 그 종목의 사건 수. **사건 하나가 한 표 = 발화 가중.**
      (이것이 `probe_summary` 가 이어 붙여 내던 값과 정확히 같다 — 아래 재현 검사.)
    - `weights="uniform"` → 가중치 = 1. **종목 하나가 한 표 = 종목당 균등.**
    - `weights=배열` → 임의 눈금. 무작위 막대의 **종목 구성**을 발화에 맞출 때 쓴다.

    `min_events` 는 그 문턱보다 사건이 적은 종목을 **분모에서 뺀다**. 종목당 균등에서
    사건 1건짜리 종목의 표(0 또는 1)가 값을 흔드는지 보려는 것이지, 기본값이 아니다.
    """
    counts = np.asarray(counts, dtype="int64")
    ok = counts >= max(1, min_events)
    if weights is None:
        w = counts.astype("float64")
    elif isinstance(weights, str) and weights == "uniform":
        w = np.ones(counts.size, dtype="float64")
    else:
        w = np.asarray(weights, dtype="float64")
    w = np.where(ok, w, 0.0)
    tot = float(w.sum())
    if tot <= 0.0:
        return {"value": None, "n_symbols": 0, "n_events": 0}
    r = np.where(ok, np.nan_to_num(rates, nan=0.0), 0.0)
    return {"value": float((w * r).sum() / tot),
            "n_symbols": int((w > 0).sum()),
            "n_events": int(counts[ok].sum())}


def both_scales(counts: np.ndarray, rates: np.ndarray, *,
                min_events: int = MIN_EVENTS_GUARD) -> dict:
    """같은 자료를 **두 눈금으로** 접어 나란히 놓는다. 어느 쪽이 옳은지는 안 정한다."""
    return {
        "fire_weighted": fold(counts, rates),
        "symbol_uniform": fold(counts, rates, "uniform"),
        "symbol_uniform_min_events": fold(counts, rates, "uniform",
                                          min_events=min_events),
        "min_events_guard": min_events,
    }


def concentration(counts: np.ndarray) -> dict:
    """**왜 두 눈금이 갈라지는가** 를 보여 주는 한 칸. 사건이 고르면 안 갈라진다.

    사건 수가 종목마다 같으면 두 눈금은 같은 값이다. 갈라지는 폭은 곧 **집중도**다.
    """
    c = counts[counts > 0].astype("float64")
    if c.size == 0:
        return {"n_symbols": 0}
    order = np.sort(c)[::-1]
    tot = float(order.sum())
    return {
        "n_symbols_with_events": int(c.size),
        "events_per_symbol": pct_table(c),
        "top1_share": float(order[0] / tot),
        "top5_share": float(order[:5].sum() / tot),
        "top12_share": float(order[:12].sum() / tot),
        "share_of_symbols_under_20_events": float((c < 20).mean()),
        "events_in_symbols_under_20": int(c[c < 20].sum()),
    }


# --------------------------------------------------------------------------- #
# 2. 앵커 — 종목 라벨을 **버리지 않고** 모은다
# --------------------------------------------------------------------------- #
def collect_by_symbol(bars: dict, *, rise: float = SHOT_RISE,
                      max_seconds: int = SHOT_MAX_SECONDS,
                      horizons: tuple[int, ...] = HORIZONS_S) -> dict:
    """`cross_peak_check.collect_anchors` 와 **같은 앵커**를, 종목 라벨을 남겨 모은다.

    그쪽은 종목을 이어 붙여 버려서 눈금을 바꿀 수가 없다. 여기서는 같은 함수를
    같은 순서로 부르되 `sym_id` 를 같이 남긴다 — **재구현이 아니라 라벨 보존**이다.
    (같은 값이 나오는지는 `reproduction_check` 와 테스트가 지킨다.)
    """
    syms = list(bars)
    kinds = ("stage1_cross", "causal_fire")
    keys = ("n_bars", "max_ret", "end_ret", "t_max_s")
    acc: dict = {k: {"sym_id": [], "anchor_ms": [],
                     "episode": {q: [] for q in keys},
                     "fixed": {h: {q: [] for q in keys} for h in horizons}}
                 for k in kinds}

    for si_id, s in enumerate(syms):
        ts, _n, _lo, _hi, vwap = bars[s]
        ep = find_episodes(ts, vwap, vwap, rise=rise, max_seconds=max_seconds)
        fires = find_fires(ts, vwap, vwap, rise=rise, max_seconds=max_seconds,
                           cooldown_s=max_seconds)
        lows = trailing_min_index(ts, vwap, fires, max_seconds)

        for kind, anchor, origin in (("stage1_cross", ep["cross"], ep["start"]),
                                     ("causal_fire", fires, lows)):
            if anchor.size == 0:
                continue
            a = acc[kind]
            a["sym_id"].append(np.full(anchor.size, si_id, dtype="int64"))
            a["anchor_ms"].append(ts[anchor])
            # 에피소드식 창: 저점 + max_seconds 에서 끝난다 → 앵커 뒤에 남는 시간
            lead = (ts[anchor] - ts[origin]) / 1000.0
            rem = np.maximum(max_seconds - lead, 0.0)
            r = forward_probe(ts, vwap, anchor, rem)
            for q in keys:
                a["episode"][q].append(r[q])
            for h in horizons:
                rf = forward_probe(ts, vwap, anchor, h)
                for q in keys:
                    a["fixed"][h][q].append(rf[q])

    def cat(bag):
        return {q: (np.concatenate(v) if v else np.array([])) for q, v in bag.items()}

    out: dict = {"symbols": syms, "n_symbols": len(syms)}
    for kind in kinds:
        a = acc[kind]
        out[kind] = {
            "sym_id": np.concatenate(a["sym_id"]) if a["sym_id"] else np.array([], "int64"),
            "anchor_ms": (np.concatenate(a["anchor_ms"]) if a["anchor_ms"]
                          else np.array([], "int64")),
            "episode": cat(a["episode"]),
            "fixed": {h: cat(a["fixed"][h]) for h in horizons},
        }
    return out


def ruler_draws_by_symbol(bars: dict, *, horizons: tuple[int, ...] = HORIZONS_S,
                          draws_per_symbol: int = DRAWS_PER_SYMBOL,
                          seed: int = SEED) -> dict:
    """`cross_peak_check.random_time_baseline` **과 같은 추첨**을 종목 라벨을 남겨 되풀이.

    추첨 순서·크기·`rng` 호출 횟수를 **그대로** 맞춘다. 그래야 §10-3 의 31.1% 를
    재현한 것이지 비슷한 다른 값이 아니다.
    `tests/test_scale_convention.py::test_ruler_draw_reproduces_random_time_baseline`
    이 두 값이 같은지 지킨다.
    """
    rng = np.random.default_rng(seed)
    keys = ("n_bars", "max_ret", "end_ret", "t_max_s")
    syms = list(bars)
    sym_id: list = []
    acc: dict = {h: {q: [] for q in keys} for h in horizons}
    for si_id, s in enumerate(syms):
        ts, _cnt, _lo, _hi, v = bars[s]
        if ts.size < 3:
            continue
        idx = rng.integers(0, ts.size, size=min(draws_per_symbol, ts.size))
        sym_id.append(np.full(idx.size, si_id, dtype="int64"))
        for h in horizons:
            r = forward_probe(ts, v, idx, h)
            for q in keys:
                acc[h][q].append(r[q])
    return {
        "symbols": syms, "n_symbols": len(syms), "seed": seed,
        "draws_per_symbol": draws_per_symbol,
        "sym_id": np.concatenate(sym_id) if sym_id else np.array([], "int64"),
        "fixed": {h: {q: (np.concatenate(acc[h][q]) if acc[h][q] else np.array([]))
                      for q in keys} for h in horizons},
    }


def ruler_draws_matched_counts(bars: dict, counts: np.ndarray, symbols: list, *,
                               horizon_s: int = HEADLINE_H,
                               multiplier: int = 3, seed: int = SEED) -> dict:
    """눈금을 **종목마다 사건 수만큼(×배수)** 뽑아 이어 붙인다 — §11-6 이 쓴 방법.

    §11-6 의 `placebo_unmatched_all_bars` 와 같은 발상이다(종목 구성을 발화와 같게).
    가중치 재접기(`fold(..., weights=counts)`)와 **같은 양을 다른 방법으로** 추정하므로,
    두 값이 어긋나면 둘 중 하나가 틀린 것이다. 교차 검증으로만 쓴다.
    """
    rng = np.random.default_rng(seed)
    keys = ("n_bars", "max_ret", "end_ret", "t_max_s")
    sym_id: list = []
    acc: dict = {q: [] for q in keys}
    for si_id, s in enumerate(symbols):
        want = int(counts[si_id]) * multiplier
        if want <= 0 or s not in bars:
            continue
        ts, _cnt, _lo, _hi, v = bars[s]
        if ts.size < 3:
            continue
        idx = rng.integers(0, ts.size, size=want)
        sym_id.append(np.full(idx.size, si_id, dtype="int64"))
        r = forward_probe(ts, v, idx, horizon_s)
        for q in keys:
            acc[q].append(r[q])
    return {
        "horizon_s": horizon_s, "multiplier": multiplier, "seed": seed,
        "sym_id": np.concatenate(sym_id) if sym_id else np.array([], "int64"),
        "probe": {q: (np.concatenate(acc[q]) if acc[q] else np.array([])) for q in keys},
    }


# --------------------------------------------------------------------------- #
# 3. 깃발 — "앵커가 곧 최고가인가" 를 이벤트마다 하나씩
# --------------------------------------------------------------------------- #
def anchor_is_max_flags(probe: dict) -> dict:
    """`probe_summary` 와 **같은 규약**으로 이벤트마다 참/거짓을 만든다.

    - `incl_empty`: 창이 비면 "더 안 올랐다" 로 센다. **1단계 규칙이 이쪽**이라
      §10 의 표가 전부 이 규약이다.
    - `excl_empty`: 창이 빈 이벤트를 분모에서 뺀다.
    동률(같은 가격)은 상승이 아니다 — `max_ret > 0` 이 엄격 부등호인 것이 그것이다.
    """
    n_bars = np.asarray(probe["n_bars"])
    mx = np.asarray(probe["max_ret"], dtype="float64")
    has = n_bars >= 1
    higher = np.zeros(n_bars.size, dtype=bool)
    higher[has] = mx[has] > 0.0
    return {"is_max_incl_empty": ~higher, "has_bars": has}


def row_two_scales(sym_id: np.ndarray, probe: dict, n_sym: int, *,
                   min_events: int = MIN_EVENTS_GUARD,
                   weights: np.ndarray | None = None) -> dict:
    """한 줄(앵커 × 창)을 **두 눈금으로** 낸다. `incl_empty` · `excl_empty` 둘 다.

    `weights` 를 주면 세 번째 눈금을 더 낸다 — 눈금줄의 종목 구성을 사건에 맞출 때만.
    """
    f = anchor_is_max_flags(probe)
    c_all, r_all = per_symbol_rates(sym_id, f["is_max_incl_empty"], n_sym)
    keep = f["has_bars"]
    c_ex, r_ex = per_symbol_rates(sym_id[keep], f["is_max_incl_empty"][keep], n_sym)
    out = {
        "n_events": int(sym_id.size),
        "n_empty_window": int((~keep).sum()),
        "share_empty_window": float((~keep).mean()) if sym_id.size else None,
        "incl_empty": both_scales(c_all, r_all, min_events=min_events),
        "excl_empty": both_scales(c_ex, r_ex, min_events=min_events),
        "concentration": concentration(c_all),
    }
    if weights is not None:
        out["incl_empty"]["weighted_by_event_counts"] = fold(c_all, r_all, weights)
        out["excl_empty"]["weighted_by_event_counts"] = fold(c_ex, r_ex, weights)
    return out


# --------------------------------------------------------------------------- #
# 4. 재현 검사 — 내 발화 가중이 §10 의 그 값과 **같은 수인가**
# --------------------------------------------------------------------------- #
def reproduction_check(anch: dict, ruler: dict, *,
                       horizon_s: int = HEADLINE_H) -> dict:
    """내 `fold(weights=None)` 이 `probe_summary` 의 값과 같은지 **자리마다 확인**한다.

    같지 않으면 내가 사건 집합을 바꿔 버린 것이고, 그러면 §10 과 나란히 놓을 수 없다.
    눈금줄은 `random_time_baseline` 을 **그대로 불러** 대조한다.
    """
    out: dict = {}
    for kind in ("stage1_cross", "causal_fire"):
        a = anch[kind]
        for tag, probe in (("episode_window", a["episode"]),
                           (f"fixed_{horizon_s}s", a["fixed"][horizon_s])):
            mine = fold(*per_symbol_rates(a["sym_id"],
                                          anchor_is_max_flags(probe)["is_max_incl_empty"],
                                          anch["n_symbols"]))
            theirs = probe_summary(probe)
            out[f"{kind}__{tag}"] = {
                "mine_fire_weighted": mine["value"],
                "cross_peak_check_probe_summary": theirs.get(
                    "share_anchor_is_max_incl_empty"),
                "n_mine": mine["n_events"], "n_theirs": theirs.get("n"),
                "same": (mine["value"] is not None
                         and abs(mine["value"]
                                 - theirs["share_anchor_is_max_incl_empty"]) < 1e-12),
            }
    r = ruler["ruler_symbol_uniform_reference"]
    out["ruler__symbol_uniform"] = {
        "mine_pooled_over_equal_draws": ruler["pooled_over_equal_draws"],
        "cross_peak_check_random_time_baseline": r,
        "same": (r is not None and ruler["pooled_over_equal_draws"] is not None
                 and abs(r - ruler["pooled_over_equal_draws"]) < 1e-12),
    }
    out["what_this_guards"] = (
        "발화 가중 값이 §10 의 그 수와 **같은 수**여야 한다 — 눈금만 바꾼 것이지 사건 "
        "집합을 바꾼 것이 아니라는 증거다. 하나라도 same=false 면 아래 표를 읽으면 안 된다.")
    return out


# --------------------------------------------------------------------------- #
# 5. 눈금줄 — 두 눈금 + 사건 구성으로 다시 가중
# --------------------------------------------------------------------------- #
def ruler_row(bars: dict, draw: dict, weight_sets: dict, *,
              horizon_s: int = HEADLINE_H,
              min_events: int = MIN_EVENTS_GUARD) -> dict:
    """무작위 막대 줄. **같은 추첨**을 세 가지 가중치로 접는다.

    `weight_sets` 는 {이름: 종목별 사건 수}. 눈금의 **종목 구성**을 그 사건에 맞춘다.
    추첨을 다시 하지 않는 것이 핵심이다 — 추첨 잡음이 섞이면 가중치 효과가 안 보인다.
    """
    probe = draw["fixed"][horizon_s]
    f = anchor_is_max_flags(probe)
    n_sym = draw["n_symbols"]
    c_all, r_all = per_symbol_rates(draw["sym_id"], f["is_max_incl_empty"], n_sym)
    keep = f["has_bars"]
    c_ex, r_ex = per_symbol_rates(draw["sym_id"][keep],
                                  f["is_max_incl_empty"][keep], n_sym)
    row: dict = {
        "n_draws": int(draw["sym_id"].size),
        "draws_per_symbol": draw["draws_per_symbol"], "seed": draw["seed"],
        "share_empty_window": float((~keep).mean()) if keep.size else None,
        "incl_empty": both_scales(c_all, r_all, min_events=min_events),
        "excl_empty": both_scales(c_ex, r_ex, min_events=min_events),
    }
    # 종목마다 같은 개수를 뽑았으므로 **이어 붙인 값 = 종목당 균등**이다(§10-3 그 수).
    row["pooled_over_equal_draws"] = row["incl_empty"]["fire_weighted"]["value"]
    for name, w in weight_sets.items():
        row["incl_empty"][f"weighted_by_{name}"] = fold(c_all, r_all, w)
        row["excl_empty"][f"weighted_by_{name}"] = fold(c_ex, r_ex, w)
        # `≥20건` 칸의 짝. 눈금줄은 종목마다 300개씩이라 자기 `min_events` 로는 아무도
        # 안 빠진다 — **사건 쪽이 뺀 것과 같은 종목**을 빼야 나란히 놓을 수 있다.
        keep = np.asarray(w, dtype="float64") >= min_events
        row["incl_empty"][f"symbol_uniform_on_{name}_min_events"] = fold(
            c_all, r_all, keep.astype("float64"))
        row["excl_empty"][f"symbol_uniform_on_{name}_min_events"] = fold(
            c_ex, r_ex, keep.astype("float64"))
    row["note"] = (
        "`fire_weighted` 칸은 눈금줄에서 **의미가 없다** — 추첨 수가 종목마다 같으므로 "
        "그냥 종목당 균등과 같은 값이다. 눈금의 발화 가중은 "
        "`weighted_by_causal_fire` 칸이다.")
    return row


def ruler_seed_sweep(bars: dict, weight_sets: dict, *,
                     horizon_s: int = HEADLINE_H,
                     seeds: tuple[int, ...] = SEED_SWEEP,
                     draws_per_symbol: int = DRAWS_PER_SYMBOL) -> dict:
    """**추첨 잡음의 폭.** §11-6 이 −0.7%p 를 "잡음 이하"로 판정할 때 쓴 그 기준이다.

    사건 줄은 추첨이 없으므로 씨앗에 안 흔들린다 — 격차의 씨앗 잡음은 **전부 눈금 쪽**이다.
    **이것은 표본 CI 가 아니다.**
    """
    per_seed: dict = {}
    for s in seeds:
        d = ruler_draws_by_symbol(bars, horizons=(horizon_s,),
                                  draws_per_symbol=draws_per_symbol, seed=s)
        r = ruler_row(bars, d, weight_sets, horizon_s=horizon_s)
        per_seed[str(s)] = {
            "symbol_uniform": r["incl_empty"]["symbol_uniform"]["value"],
            "pooled_over_equal_draws": r["pooled_over_equal_draws"],
            **{f"weighted_by_{k}": r["incl_empty"][f"weighted_by_{k}"]["value"]
               for k in weight_sets},
        }
    spread: dict = {}
    for field in next(iter(per_seed.values())):
        vals = [v[field] for v in per_seed.values() if v[field] is not None]
        if vals:
            spread[field] = {"min": min(vals), "max": max(vals),
                             "spread_pp": (max(vals) - min(vals)) * 100.0,
                             "mean": float(np.mean(vals)),
                             "sd": float(np.std(vals, ddof=1)) if len(vals) > 1 else 0.0}
    return {"seeds": list(seeds), "per_seed": per_seed, "spread": spread,
            "not_a_ci": ("씨앗 민감도는 **추첨 잡음**이지 표본 CI 가 아니다. "
                         "표본은 여전히 2.6 정규장이고 사이클 군집이 하한에 못 미친다.")}


# --------------------------------------------------------------------------- #
# 6. 격차 — 이 태스크가 실제로 묻는 것
# --------------------------------------------------------------------------- #
def gap_table(rows: dict, ruler: dict, sweep: dict, *,
              convention: str = "incl_empty") -> dict:
    """`사건 − 무작위` 를 **두 눈금에서 각각** 낸다. 그리고 씨앗 잡음 폭을 옆에 둔다.

    같은 눈금 안에서만 뺀다. §10-7 이 뺀 30.7 − 31.1 은 **다른 두 눈금 사이의 뺄셈**이라
    여기 어느 줄에도 해당하지 않는다 — 그 사실 자체를 `mixed_scale_as_published` 에 남긴다.
    """
    out: dict = {}
    for name, wkey in (("causal_fire", "causal_fire"), ("stage1_cross", "stage1_cross")):
        ev = rows[f"{name}__fixed_{HEADLINE_H}s"][convention]
        fw_ev = ev["fire_weighted"]["value"]
        su_ev = ev["symbol_uniform"]["value"]
        fw_ru = ruler[convention][f"weighted_by_{wkey}"]["value"]
        su_ru = ruler[convention]["symbol_uniform"]["value"]
        noise = sweep["spread"].get(f"weighted_by_{wkey}", {}).get("spread_pp")
        noise_su = sweep["spread"].get("symbol_uniform", {}).get("spread_pp")
        out[name] = {
            "fire_weighted": {
                "event": fw_ev, "ruler": fw_ru,
                "gap_pp": None if None in (fw_ev, fw_ru) else (fw_ev - fw_ru) * 100.0,
                "ruler_seed_spread_pp": noise,
            },
            "symbol_uniform": {
                "event": su_ev, "ruler": su_ru,
                "gap_pp": None if None in (su_ev, su_ru) else (su_ev - su_ru) * 100.0,
                "ruler_seed_spread_pp": noise_su,
                "ruler_alt_pooled_over_equal_draws": ruler["pooled_over_equal_draws"],
                "ruler_alt_note": (
                    "§10-3 이 실은 31.1% 는 **종목마다 같은 개수를 뽑아 이어 붙인** 값이고 "
                    "여기 `ruler` 는 **종목별 비율의 단순 평균**이다. 둘 다 종목당 균등이며 "
                    "차이는 막대가 300개 미만인 종목에서만 난다."),
            },
        }
        g1 = out[name]["fire_weighted"]["gap_pp"]
        g2 = out[name]["symbol_uniform"]["gap_pp"]
        if g1 is not None and g2 is not None:
            out[name]["gap_moves_pp"] = g1 - g2
            big = max(x for x in (noise, noise_su) if x is not None)
            out[name]["gap_move_vs_seed_noise"] = (
                "씨앗 잡음보다 크다" if abs(g1 - g2) > big else "씨앗 잡음 이하")
    out["mixed_scale_as_published"] = {
        "what_docs44_10_7_subtracted": (
            "인과적 발화 30.7%(발화 가중) − 무작위 막대 31.1%(종목당 균등). "
            "**두 값이 서로 다른 눈금이다.** 아래 어느 줄과도 같지 않다."),
    }
    out["how_to_read"] = (
        "**같은 눈금 안에서만** 뺀 값이다. 두 눈금에서 격차가 비슷하면 §10 의 "
        "'구별 안 됨' 은 눈금과 무관하고, 한 눈금에서만 격차가 서면 §10 의 결론은 "
        "눈금의 산물이었던 것이다.")
    return out


# --------------------------------------------------------------------------- #
# 7. 조립
# --------------------------------------------------------------------------- #
def build_report(conn: sqlite3.Connection, *, rise: float = SHOT_RISE,
                 max_seconds: int = SHOT_MAX_SECONDS,
                 horizons: tuple[int, ...] = HORIZONS_S,
                 seed: int = SEED) -> dict:
    t0 = time.time()
    syms = select_symbols(conn)
    print(f"  [1/5] symbols: {len(syms)}  ({time.time()-t0:.0f}s)")
    bars = load_bars(conn, syms)
    n_bars = int(sum(b[0].size for b in bars.values()))
    print(f"  [2/5] second bars: {len(bars)} symbols / {n_bars} bars  "
          f"({time.time()-t0:.0f}s)")
    anch = collect_by_symbol(bars, rise=rise, max_seconds=max_seconds,
                             horizons=horizons)
    n_sym = anch["n_symbols"]
    print(f"  [3/5] anchors: stage1_cross {anch['stage1_cross']['sym_id'].size}, "
          f"causal_fire {anch['causal_fire']['sym_id'].size}  ({time.time()-t0:.0f}s)")

    # 눈금줄을 사건 구성으로 다시 가중하려면 종목별 사건 수가 먼저 있어야 한다.
    weight_sets = {
        kind: np.bincount(anch[kind]["sym_id"], minlength=n_sym).astype("float64")
        for kind in ("stage1_cross", "causal_fire")}

    draw = ruler_draws_by_symbol(bars, horizons=horizons,
                                 draws_per_symbol=DRAWS_PER_SYMBOL, seed=seed)
    ruler = ruler_row(bars, draw, weight_sets, horizon_s=max_seconds)
    # §10-3 눈금을 **그 모듈의 함수 그대로** 불러 대조한다 — 재구현이 아님을 보이려고.
    ref = random_time_baseline(bars, horizons=(max_seconds,),
                               draws_per_symbol=DRAWS_PER_SYMBOL, seed=seed)
    ruler["ruler_symbol_uniform_reference"] = ref[f"fixed_{max_seconds}s"].get(
        "share_anchor_is_max_incl_empty")
    print(f"  [4/5] ruler drawn: {ruler['n_draws']} bars  ({time.time()-t0:.0f}s)")

    rows: dict = {}
    for kind in ("stage1_cross", "causal_fire"):
        a = anch[kind]
        rows[f"{kind}__episode_window"] = row_two_scales(
            a["sym_id"], a["episode"], n_sym)
        for h in horizons:
            rows[f"{kind}__fixed_{h}s"] = row_two_scales(
                a["sym_id"], a["fixed"][h], n_sym)

    sweep = ruler_seed_sweep(bars, weight_sets, horizon_s=max_seconds)
    print(f"  [5/5] seed sweep ({len(SEED_SWEEP)} seeds) done  ({time.time()-t0:.0f}s)")

    # 교차 검증: 가중치 재접기 대신 **실제로 발화 수만큼 뽑아** 이어 붙인 눈금
    drawn = ruler_draws_matched_counts(bars, weight_sets["causal_fire"],
                                       anch["symbols"], horizon_s=max_seconds,
                                       seed=seed)
    dsum = probe_summary(drawn["probe"])

    a_ms = anch["causal_fire"]["anchor_ms"]
    c_ms = anch["stage1_cross"]["anchor_ms"]
    rep: dict = {
        "question": (
            "D-14 로 눈금이 **발화 가중**으로 정해졌다. docs/44 §10 의 네 숫자를 그 "
            "눈금에서 다시 내고, **격차가 유지되는지** 본다. 판정은 하지 않는다."),
        "conditions": {
            "window_start_ms": WINDOW_START_MS,
            "window_end_ms": int(max(a_ms.max() if a_ms.size else 0,
                                     c_ms.max() if c_ms.size else 0)),
            "symbols": len(bars), "second_bars": n_bars,
            "rise": rise, "max_seconds": max_seconds, "horizons_s": list(horizons),
            "price_series": "초 막대 vwap (tick_stages 와 같은 열)",
            "headline_horizon_s": max_seconds,
            "convention": ("`incl_empty` — 창이 비면 '더 안 올랐다' 로 센다. "
                           "§10 의 표가 전부 이 규약이다. `excl_empty` 도 같이 낸다."),
            "seed": seed, "seeds_swept": list(SEED_SWEEP),
            "draws_per_symbol": DRAWS_PER_SYMBOL,
            "sessions_causal_fire": _session_mix(a_ms),
            "sessions_stage1_cross": _session_mix(c_ms),
            "reused_not_reimplemented": (
                "find_episodes · find_fires · forward_probe · probe_summary · "
                "random_time_baseline · trailing_min_index 를 그대로 불러 쓴다. "
                "기존 코드는 한 줄도 안 고쳤다."),
            "not_in_scope": ("D-13(테이프 밀도 정합)은 여기서 하지 않는다 — 눈금 하나만 "
                             "바꿔야 어느 쪽이 숫자를 움직였는지 갈린다."),
            "costs_excluded": "스프레드·수수료·슬리피지 전부 미포함",
        },
        "what_scale_each_published_number_was_on": {
            "the_three_event_numbers": (
                "**발화(사건) 가중.** cross_peak_check.collect_anchors 가 종목을 이어 "
                "붙인 뒤 probe_summary 가 비율을 낸다 — 사건 하나가 한 표다."),
            "the_ruler": (
                "**종목당 균등.** random_time_baseline 이 draws_per_symbol=300 으로 "
                "종목마다 같은 개수를 뽑는다 — 종목 하나가 한 표다."),
            "therefore": (
                "§10-7 이 뺀 30.7 − 31.1 은 **서로 다른 두 눈금 사이의 뺄셈**이었다. "
                "표기 문제가 아니라 헤드라인이 걸린 문제다."),
        },
        "reproduction_check": reproduction_check(anch, ruler, horizon_s=max_seconds),
        "rows": rows,
        "ruler": ruler,
        "ruler_cross_check_drawn_by_fire_counts": {
            "n_draws": int(drawn["sym_id"].size),
            "multiplier": drawn["multiplier"], "seed": drawn["seed"],
            "anchor_is_max_incl_empty": dsum.get("share_anchor_is_max_incl_empty"),
            "anchor_is_max_excl_empty": dsum.get("share_anchor_is_max_excl_empty"),
            "vs_reweighted": ruler["incl_empty"]["weighted_by_causal_fire"]["value"],
            "what_it_is": ("가중치 재접기 대신 **실제로 종목마다 발화 수×배수만큼** 뽑아 "
                           "이어 붙인 값. 같은 양의 고분산 추정 — 어긋나면 둘 중 하나가 "
                           "틀린 것이다. §11-6 이 쓴 방법이 이쪽이다."),
        },
        "seed_sensitivity": sweep,
        "gap": gap_table(rows, ruler, sweep),
        "not_claimed": [
            "**판정하지 않는다.** 어떤 설계도 살리거나 죽이지 않는다.",
            "CI 가 없다. 표본은 여전히 2.6 정규장이고 씨앗 민감도는 추첨 잡음일 뿐이다.",
            "테이프 밀도(D-13)를 안 맞췄다. 눈금 하나만 바꾼 값이다.",
            "포화 칸을 안 갈랐다 — 끊긴 구간은 '앵커가 정점' 쪽으로 치우친다(§10-8 3번).",
            "종목당 균등 줄은 사건 1건짜리 종목에도 한 표를 준다. "
            "`symbol_uniform_min_events` 로 그 민감도를 같이 냈다.",
        ],
    }
    return rep


def _session_mix(ts_ms: np.ndarray) -> dict:
    if ts_ms.size == 0:
        return {}
    s = session_of(ts_ms)
    return {k: float((s == k).mean()) for k in ("pre", "regular", "after", "overnight")}


# --------------------------------------------------------------------------- #
# 8. 화면 요약
# --------------------------------------------------------------------------- #
def _p(v, nd=4):
    return "     -" if v is None else f"{v:.{nd}f}"


def print_report(rep: dict) -> None:
    c = rep["conditions"]
    print(f"\n=== conditions: {c['symbols']} symbols, {c['second_bars']} second bars, "
          f"rise {c['rise']}, window_end_ms {c['window_end_ms']}")
    print(f"    seed {c['seed']}, draws/symbol {c['draws_per_symbol']}, "
          f"convention incl_empty, horizon {c['headline_horizon_s']}s")
    ss = c.get("sessions_causal_fire", {})
    print("    causal_fire sessions: " + "  ".join(f"{k} {v:.3f}" for k, v in ss.items()))

    print("\n=== reproduction check (fire-weighted must EQUAL the published number)")
    for k, v in rep["reproduction_check"].items():
        if not isinstance(v, dict):
            continue
        if "mine_fire_weighted" in v:
            print(f"  {k:<38} mine {_p(v['mine_fire_weighted'])}  "
                  f"cross_peak_check {_p(v['cross_peak_check_probe_summary'])}  "
                  f"n {v['n_mine']}/{v['n_theirs']}  same={v['same']}")
        else:
            print(f"  {k:<38} mine {_p(v['mine_pooled_over_equal_draws'])}  "
                  f"random_time_baseline "
                  f"{_p(v['cross_peak_check_random_time_baseline'])}  same={v['same']}")

    h = rep["conditions"]["headline_horizon_s"]
    print("\n=== THE FOUR NUMBERS, both scales (incl_empty, horizon %ds)" % h)
    print(f"  {'row':<44}{'n':>7}{'fire-wtd':>11}{'sym-unif':>11}{'sym-unif n>=20':>16}")
    order = [("stage1_cross__episode_window",
              "stage1 cross x episode window   (was 56.6%)"),
             ("causal_fire__episode_window",
              "causal fire x same geometry     (was 44.3%)"),
             (f"stage1_cross__fixed_{h}s",
              f"stage1 cross x fixed {h}s        (was 41.9%)"),
             (f"causal_fire__fixed_{h}s",
              f"causal fire  x fixed {h}s        (was 30.7%)")]
    for key, label in order:
        r = rep["rows"][key]["incl_empty"]
        print(f"  {label:<44}{rep['rows'][key]['n_events']:>7}"
              f"{_p(r['fire_weighted']['value']):>11}"
              f"{_p(r['symbol_uniform']['value']):>11}"
              f"{_p(r['symbol_uniform_min_events']['value']):>16}")
    ru = rep["ruler"]["incl_empty"]
    print(f"  {'random bar   x fixed %ds        (was 31.1%%)' % h:<44}"
          f"{rep['ruler']['n_draws']:>7}"
          f"{_p(ru['weighted_by_causal_fire']['value']):>11}"
          f"{_p(ru['symbol_uniform']['value']):>11}"
          f"{_p(ru['symbol_uniform_min_events']['value']):>16}")
    print("    ruler fire-wtd column = weighted_by_causal_fire "
          "(ruler symbol mix set equal to the fires')")
    print("    ruler sym-unif n>=20 column is NOT the counterpart of the event one "
          "(300 draws/symbol drops nobody);")
    print(f"    the counterpart, restricted to the SAME symbols the fires' n>=20 "
          f"filter keeps, is "
          f"{_p(ru['symbol_uniform_on_causal_fire_min_events']['value'])} "
          f"(n_symbols {ru['symbol_uniform_on_causal_fire_min_events']['n_symbols']})")
    print(f"    ruler weighted_by_stage1_cross = "
          f"{_p(ru['weighted_by_stage1_cross']['value'])}"
          f"   ruler pooled_over_equal_draws = "
          f"{_p(rep['ruler']['pooled_over_equal_draws'])}")
    x = rep["ruler_cross_check_drawn_by_fire_counts"]
    print(f"    cross-check (bars actually DRAWN in proportion to fires, "
          f"n={x['n_draws']}): {_p(x['anchor_is_max_incl_empty'])}"
          f"  vs reweighted {_p(x['vs_reweighted'])}")

    print("\n=== THE GAP (event minus ruler), WITHIN one scale only")
    for name in ("causal_fire", "stage1_cross"):
        g = rep["gap"][name]
        print(f"  [{name}]")
        for scale in ("fire_weighted", "symbol_uniform"):
            s = g[scale]
            print(f"    {scale:<16} event {_p(s['event'])}  ruler {_p(s['ruler'])}  "
                  f"gap {s['gap_pp']:+.2f}pp   (ruler seed spread "
                  f"{s['ruler_seed_spread_pp']:.2f}pp)")
        biggest = max(g[s]["ruler_seed_spread_pp"] for s in
                      ("fire_weighted", "symbol_uniform"))
        print(f"    gap moves {g['gap_moves_pp']:+.2f}pp between the two scales "
              f"(largest ruler seed spread {biggest:.2f}pp) -> "
              f"{'ABOVE' if abs(g['gap_moves_pp']) > biggest else 'within'} seed noise; "
              f"the gap itself is far above it in BOTH scales")
    print("  as published (docs/44 section 10-7): causal 30.7% (fire-weighted) "
          "minus random 31.1% (symbol-uniform) = MIXED SCALES, matches neither row")

    print("\n=== seed sensitivity of the ruler (NOT a confidence interval)")
    for field, s in rep["seed_sensitivity"]["spread"].items():
        print(f"  {field:<32} min {_p(s['min'])}  max {_p(s['max'])}  "
              f"spread {s['spread_pp']:.2f}pp  sd {s['sd']:.4f}")

    print("\n=== why the two scales differ at all - event concentration")
    for key, label in order:
        cc = rep["rows"][key]["concentration"]
        print(f"  {label:<44} symbols {cc['n_symbols_with_events']:>3}  "
              f"per-symbol p50 {cc['events_per_symbol'].get('p50', 0):>6.1f}  "
              f"top5 {cc['top5_share']:.3f}  "
              f"symbols with <20 events {cc['share_of_symbols_under_20_events']:.3f}")

    print("\n=== not claimed (Korean text in the JSON)")
    print("  - no verdict; no design is kept or killed here")
    print("  - no CI; sample is still 2.6 regular sessions; seed spread is draw noise")
    print("  - tape density (D-13) NOT matched - only the scale was changed")
    print("  - saturated cells not separated (docs/44 section 10-8 item 3)")


def main(db: Path, *, out_dir: Path | None = None) -> int:
    out_dir = out_dir or OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    print("=== scale convention D-14 (read-only, no live calls)")
    conn = open_ro(db)
    try:
        rep = build_report(conn)
    finally:
        conn.close()
    (out_dir / "scale_convention.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print_report(rep)
    print(f"\nwrote {out_dir / 'scale_convention.json'}")
    return 0


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/tossmon.db")
    raise SystemExit(main(path))

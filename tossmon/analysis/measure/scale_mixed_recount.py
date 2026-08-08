"""**눈금 섞인 대조 둘을 정본 눈금으로 — D-15 · D-16** (`docs/44` §13).

`docs/44` §12-8 (C) 가 눈금이 섞인 대조 **다섯**을 찾았다. 이 모듈은 그중 **하중을
받는 둘**만 다시 낸다. 나머지 셋(4번·5번·1번)은 목록만 유지한다 — 1번은 §12 가 이미
냈고, 4번은 문서가 스스로 "약한 대조"라 적었고, 5번은 재계산이 아니라 표기다.

| # | 어디 | 무엇이 섞였나 | 그 위에 선 문장 |
|---|---|---|---|
| **D-15** | `44` §10-5 | 무작위 막대 60초 최대 상승 **+1.43%** (`random_time_baseline`, `draws_per_symbol=300` → **종목당 균등**) vs 인과적 발화 **+1.38%** (이어 붙임 → **발화 가중**) | **§10-8 1번** — "탐지 후에 먹을 게 있다고 말하지 않는다" |
| **D-16** | `44` §4-2 | 기저율 **1.6% / 2.4%** (`coverage_base_rate`, `per_symbol=200` → **종목당 균등**) vs 슈팅 순간 **4.2%** (넓은 스트림 슈팅 이어 붙임 → **사건 가중**) | **"1.75배"** — "선정이 헛돌고 있는 것은 아니다" |

**두 건 다 §10-3 과 똑같은 구조다**: 사건 쪽은 이어 붙였고 대조 쪽은 종목마다 같은
개수를 뽑았다. D-14 로 **발화(사건) 가중이 정본**으로 정해졌으므로, 여기서는 두 대조를
**한 눈금 안에서** 다시 뺀다.

## 재는 법 — §12 와 **같은 기계**를 쓴다

종목 `s` 의 값을 한 번만 재고 접는 가중치만 바꾼다. `scale_convention.per_symbol_rates`
· `fold` 를 **그대로 불러 쓴다** — 눈금 기계를 여기서 다시 짜면 §12 와 나란히 놓을 수
없다. 달라지는 것은 **접는 대상**뿐이다.

| | §12 (D-14) | **여기 (D-15 · D-16)** |
|---|---|---|
| 접는 대상 | 비율 (앵커가 최고가인가) | D-15 는 **양**(창 안 최대 상승률), D-16 은 비율 (그때 tier3 였나) |

**양을 접을 때 주의할 것 하나**: 비율은 종목별 평균을 접으면 그만이지만, 백분위수는
그렇지 않다. 그래서 백분위수는 **사건마다 가중치 `w_s / n_s` 를 주고 가중 분위수**로
낸다(`weighted_quantile`). 발화 가중이면 가중치가 전부 1 이라 그냥 분위수와 같다.

## 이 모듈이 하지 않는 것

- **판정하지 않는다.** "그래서 §10-8 1번이 죽었다 / 설계 A 가 살았다"는 이 모듈이 쓸
  문장이 아니다. 방향과 크기, 그리고 씨앗 산포만 낸다.
- **D-13(테이프 밀도 정합)을 같이 하지 않는다.** 한 번에 하나만 움직인다.
- **기존 코드를 한 줄도 안 고친다.** `collect_by_symbol` · `ruler_draws_by_symbol` ·
  `fold` · `per_symbol_rates` · `random_time_baseline` · `probe_summary` ·
  `coverage_base_rate` · `wide_episodes` · `watched_at` 을 그대로 불러 쓴다.
- **1분봉 경로를 안 쓴다.** 초 막대와 랭킹 스냅뿐이다.

실행: `python -m tossmon.analysis.measure.scale_mixed_recount [db_path]`
→ `out/scale_mixed_recount.json`. **라이브 콜 0. DB 는 `mode=ro`.**
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

from tossmon.analysis.measure.cross_peak_check import (
    probe_summary,
    random_time_baseline,
)
from tossmon.analysis.measure.scale_convention import (
    DRAWS_PER_SYMBOL,
    HEADLINE_H,
    SEED,
    SEED_SWEEP,
    collect_by_symbol,
    concentration,
    fold,
    per_symbol_rates,
    ruler_draws_by_symbol,
)
from tossmon.analysis.measure.tick_resolution import (
    WINDOW_START_MS,
    open_ro,
    session_of,
)
from tossmon.analysis.measure.tick_stages import (
    SHOT_MAX_SECONDS,
    SHOT_RISE,
    load_bars,
    load_ranking_series,
    load_tier3_watch,
    coverage_base_rate,
    select_symbols,
    watched_at,
    wide_episodes,
)

OUT_DIR = Path("out")

#: `tick_stages.coverage_base_rate` 의 기본값. **바꾸면 §4-2 의 재현이 아니다.**
BASE_RATE_PER_SYMBOL = 200

#: 백분위수를 눈금별로 낼 때 쓰는 자리. 평균만으로는 두꺼운 꼬리를 못 읽는다.
QUANTILES: tuple[float, ...] = (0.5, 0.9)


# --------------------------------------------------------------------------- #
# 1. 눈금 위에서 **양**을 접는다 — §12 는 비율이었다
# --------------------------------------------------------------------------- #
def event_weights(sym_id: np.ndarray, counts: np.ndarray,
                  symbol_weights) -> np.ndarray:
    """종목 가중치 `W_s` 를 **사건 가중치 `W_s / n_s`** 로 편다.

    가중 분위수를 내려면 종목별 요약이 아니라 사건마다 가중치가 있어야 한다.
    `symbol_weights = counts` 면 사건 가중치가 전부 1 이 되어 **그냥 분위수**와 같다 —
    즉 발화 가중은 이어 붙인 값과 자동으로 일치한다.
    """
    sid = np.asarray(sym_id, dtype="int64")
    c = np.asarray(counts, dtype="float64")[sid]
    w = np.asarray(symbol_weights, dtype="float64")[sid]
    return np.where(c > 0.0, w / np.where(c > 0.0, c, 1.0), 0.0)


def weighted_quantile(values: np.ndarray, weights: np.ndarray,
                      qs: tuple[float, ...] = QUANTILES) -> dict:
    """가중 분위수 (역누적분포, 보간 없음).

    보간을 안 하는 이유: 가중치가 붙으면 보간의 뜻이 흐려진다. 대신 **가중치가 전부
    같을 때는 `np.percentile` 의 보간값과 자리 하나 안쪽에서 일치**한다. 그래서 이
    값으로 §10-5 의 `p50` 을 재현했다고 주장하지 않는다 — **재현 검사는 평균으로 한다.**
    """
    v = np.asarray(values, dtype="float64")
    w = np.asarray(weights, dtype="float64")
    keep = np.isfinite(v) & (w > 0.0)
    if not keep.any():
        return {}
    v, w = v[keep], w[keep]
    order = np.argsort(v, kind="mergesort")
    v, w = v[order], w[order]
    cw = np.cumsum(w)
    tot = float(cw[-1])
    out = {}
    for q in qs:
        i = int(np.searchsorted(cw, q * tot, side="left"))
        out[f"p{int(round(q * 100)):02d}"] = float(v[min(i, v.size - 1)])
    return out


def quantity_one_scale(sym_id: np.ndarray, values: np.ndarray, n_sym: int,
                       symbol_weights=None) -> dict:
    """한 눈금에서 **평균 + 가중 분위수**. 유한하지 않은 값(빈 창)은 먼저 뺀다.

    `symbol_weights` 규약은 `fold` 와 같다: `None` 이면 사건 수(발화 가중),
    `"uniform"` 이면 1(종목당 균등), 배열이면 임의 눈금.
    """
    v = np.asarray(values, dtype="float64")
    sid = np.asarray(sym_id, dtype="int64")
    keep = np.isfinite(v)
    counts, means = per_symbol_rates(sid[keep], v[keep], n_sym)
    folded = fold(counts, means, symbol_weights)
    if symbol_weights is None:
        w_sym = counts.astype("float64")
    elif isinstance(symbol_weights, str) and symbol_weights == "uniform":
        w_sym = np.ones(n_sym, dtype="float64")
    else:
        w_sym = np.asarray(symbol_weights, dtype="float64")
    q = weighted_quantile(v[keep], event_weights(sid[keep], counts, w_sym))
    return {"mean": folded["value"], "n_symbols": folded["n_symbols"],
            "n_events": folded["n_events"], **q}


def quantity_two_scales(sym_id: np.ndarray, values: np.ndarray, n_sym: int, *,
                        weight_sets: dict | None = None) -> dict:
    """같은 자료를 **두 눈금으로** (+ 필요하면 외부 가중치로 더) 접어 나란히 놓는다."""
    v = np.asarray(values, dtype="float64")
    keep = np.isfinite(v)
    out = {
        "n_events": int(v.size),
        "n_used": int(keep.sum()),
        "n_empty_window": int((~keep).sum()),
        "share_empty_window": float((~keep).mean()) if v.size else None,
        "fire_weighted": quantity_one_scale(sym_id, v, n_sym),
        "symbol_uniform": quantity_one_scale(sym_id, v, n_sym, "uniform"),
    }
    for name, w in (weight_sets or {}).items():
        out[f"weighted_by_{name}"] = quantity_one_scale(sym_id, v, n_sym, w)
    return out


# --------------------------------------------------------------------------- #
# 2. D-15 — §10-5 의 무작위 막대 +1.43%
# --------------------------------------------------------------------------- #
def d15_event_rows(anch: dict, *, horizon_s: int = HEADLINE_H) -> dict:
    """사건 줄(인과적 발화 · 1단계 교차)의 창 안 최대 상승 / 그냥 수익률, 두 눈금."""
    n_sym = anch["n_symbols"]
    out: dict = {}
    for kind in ("causal_fire", "stage1_cross"):
        a = anch[kind]
        probe = a["fixed"][horizon_s]
        out[kind] = {
            field: quantity_two_scales(a["sym_id"], probe[field], n_sym)
            for field in ("max_ret", "end_ret")
        }
    return out


def d15_ruler_row(draw: dict, weight_sets: dict, *,
                  horizon_s: int = HEADLINE_H) -> dict:
    """무작위 막대 줄. **같은 추첨**을 네 가중치로 접는다 — 추첨을 다시 하지 않는다.

    추첨을 눈금마다 다시 하면 추첨 잡음이 섞여 **무엇이 숫자를 움직였는지** 못 가른다.
    이건 §12-2 가 정한 규율 그대로다.
    """
    probe = draw["fixed"][horizon_s]
    n_sym = draw["n_symbols"]
    out: dict = {"n_draws": int(draw["sym_id"].size),
                 "draws_per_symbol": draw["draws_per_symbol"], "seed": draw["seed"]}
    for field in ("max_ret", "end_ret"):
        row = quantity_two_scales(draw["sym_id"], probe[field], n_sym,
                                  weight_sets=weight_sets)
        # 종목마다 같은 개수를 뽑았으므로 이어 붙인 값 = §10-5 가 실은 그 수다.
        row["pooled_over_equal_draws"] = row["fire_weighted"]["mean"]
        row["note"] = (
            "`fire_weighted` 칸은 눈금줄에서 **의미가 없다** — 추첨 수가 종목마다 "
            "같으므로 이어 붙인 값(= §10-5 가 실은 값)과 사실상 같다. 눈금의 발화 "
            "가중은 `weighted_by_causal_fire` 칸이다.")
        out[field] = row
    return out


def d15_comparison(events: dict, ruler: dict, sweep: dict, *,
                   field: str = "max_ret") -> dict:
    """`사건 − 무작위`. **같은 눈금 안에서만 뺀다.**

    §10-5 가 나란히 놓은 +1.38% 와 +1.43% 는 **서로 다른 두 눈금**이었다. 그 사실
    자체를 `mixed_scale_as_published` 에 남긴다 — 아래 어느 줄과도 같지 않다.
    """
    out: dict = {}
    for kind in ("causal_fire", "stage1_cross"):
        ev = events[kind][field]
        row: dict = {}
        for scale, ruler_key in (("fire_weighted", f"weighted_by_{kind}"),
                                 ("symbol_uniform", "symbol_uniform")):
            e = ev[scale]["mean"]
            r = ruler[field][ruler_key]["mean"]
            row[scale] = {
                "event_mean": e, "ruler_mean": r,
                "event_minus_ruler_pp": None if None in (e, r) else (e - r) * 100.0,
                "ruler_seed_spread_pp": sweep["spread"].get(
                    f"{field}__{ruler_key}", {}).get("spread_pp"),
                "event_p50": ev[scale].get("p50"),
                "ruler_p50": ruler[field][ruler_key].get("p50"),
            }
        g1 = row["fire_weighted"]["event_minus_ruler_pp"]
        g2 = row["symbol_uniform"]["event_minus_ruler_pp"]
        if g1 is not None and g2 is not None:
            row["gap_moves_pp"] = g1 - g2
            noises = [x for x in (row["fire_weighted"]["ruler_seed_spread_pp"],
                                  row["symbol_uniform"]["ruler_seed_spread_pp"])
                      if x is not None]
            if noises:
                # 두 판정은 **다른 물음**이다: 격차 자체가 잡음 밖인가 /
                # 눈금을 바꿨을 때의 이동이 잡음 밖인가. 섞어 읽으면 안 된다.
                row["gap_vs_seed_noise"] = ("씨앗 잡음보다 크다"
                                            if abs(g1) > max(noises)
                                            else "씨앗 잡음 이하")
                row["move_vs_seed_noise"] = ("씨앗 잡음보다 크다"
                                             if abs(g1 - g2) > max(noises)
                                             else "씨앗 잡음 이하")
        out[kind] = row
    out["mixed_scale_as_published"] = (
        "§10-5 는 인과적 발화 +1.38%(발화 가중) 와 무작위 막대 +1.43%(종목당 균등) 를 "
        "나란히 놓았다. **두 값이 서로 다른 눈금이다.** 위 어느 줄과도 같지 않다.")
    out["how_to_read"] = (
        "부호가 뒤집히는지, 그리고 그 크기가 눈금줄 씨앗 산포보다 큰지 둘만 본다. "
        "**판정하지 않는다.**")
    return out


def d15_seed_sweep(bars: dict, weight_sets: dict, *,
                   horizon_s: int = HEADLINE_H,
                   seeds: tuple[int, ...] = SEED_SWEEP,
                   draws_per_symbol: int = DRAWS_PER_SYMBOL) -> dict:
    """**눈금줄의 추첨 잡음.** 사건 줄은 추첨이 없어 씨앗에 안 흔들린다.

    §12-5 가 ±1.18%p 를 판정 기준으로 쓴 그 방식과 같다. **표본 CI 가 아니다.**
    """
    per_seed: dict = {}
    for s in seeds:
        d = ruler_draws_by_symbol(bars, horizons=(horizon_s,),
                                  draws_per_symbol=draws_per_symbol, seed=s)
        row = d15_ruler_row(d, weight_sets, horizon_s=horizon_s)
        rec: dict = {}
        for field in ("max_ret", "end_ret"):
            rec[f"{field}__symbol_uniform"] = row[field]["symbol_uniform"]["mean"]
            rec[f"{field}__pooled_over_equal_draws"] = row[field][
                "pooled_over_equal_draws"]
            for name in weight_sets:
                rec[f"{field}__weighted_by_{name}"] = row[field][
                    f"weighted_by_{name}"]["mean"]
        per_seed[str(s)] = rec
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
                         "표본은 여전히 2.6 정규장이다.")}


def d15_reproduction(anch: dict, draw: dict, bars: dict, *,
                     horizon_s: int = HEADLINE_H, seed: int = SEED) -> dict:
    """**옛 값이 옛 눈금에서 그대로 나오는가.** 하나라도 어긋나면 아래 표를 읽으면 안 된다.

    사건 줄은 `probe_summary` 를, 눈금줄은 `random_time_baseline` 을 **그대로 불러**
    대조한다. 재현 대상은 **평균**이다 — 가중 분위수는 보간 규약이 달라 자리가 어긋날
    수 있고, §10-5 의 헤드라인은 평균이다.
    """
    out: dict = {}
    n_sym = anch["n_symbols"]
    for kind in ("causal_fire", "stage1_cross"):
        a = anch[kind]
        probe = a["fixed"][horizon_s]
        theirs = probe_summary(probe)
        for field in ("max_ret", "end_ret"):
            mine = quantity_one_scale(a["sym_id"], probe[field], n_sym)
            ref = theirs.get(field, {}).get("mean")
            out[f"{kind}__{field}"] = {
                "mine_fire_weighted": mine["mean"],
                "cross_peak_check_probe_summary": ref,
                "n_mine": mine["n_events"], "n_theirs": theirs.get(field, {}).get("n"),
                "same": (ref is not None and mine["mean"] is not None
                         and abs(mine["mean"] - ref) < 1e-12),
            }
    ref_row = random_time_baseline(bars, horizons=(horizon_s,),
                                   draws_per_symbol=draw["draws_per_symbol"],
                                   seed=seed)[f"fixed_{horizon_s}s"]
    mine_row = d15_ruler_row(draw, {}, horizon_s=horizon_s)
    for field in ("max_ret", "end_ret"):
        r = ref_row.get(field, {}).get("mean")
        m = mine_row[field]["pooled_over_equal_draws"]
        out[f"random_bar__{field}"] = {
            "mine_pooled_over_equal_draws": m,
            "cross_peak_check_random_time_baseline": r,
            "n_mine": mine_row[field]["n_used"], "n_theirs": ref_row.get(field, {}).get("n"),
            "same": (r is not None and m is not None and abs(m - r) < 1e-12),
        }
    out["what_this_guards"] = (
        "**옛 눈금에서 옛 값이 그대로 나와야 한다.** 눈금만 바꾼 것이지 사건 집합이나 "
        "추첨을 바꾼 것이 아니라는 증거다. 창이 그때보다 늘어 §10-5 의 소수점과는 "
        "안 맞는다 — 맞아야 하는 것은 **이 실행 안에서 두 값이 같은가**다.")
    return out


# --------------------------------------------------------------------------- #
# 3. D-16 — §4-2 의 커버리지 기저율
# --------------------------------------------------------------------------- #
def wide_shot_coverage_by_symbol(watch: dict, rk: dict, eps: dict) -> dict:
    """넓은 스트림 슈팅마다 "그때 tier3 였나" 를 **종목 라벨을 남겨** 모은다.

    `tick_stages.stage3_coverage` 가 하는 것과 **같은 식**이다 (그쪽은 종목을 이어
    붙여 버려 눈금을 못 바꾼다):

        wide_flag.append(watched_at(watch, s, rt[si]))
        in_tier3_share = float(np.concatenate(wide_flag).mean())

    추첨이 없으므로 씨앗과 무관하다 — 이 줄의 값은 씨앗을 바꿔도 안 움직인다.
    """
    syms = list(rk)
    at = {s: i for i, s in enumerate(syms)}
    sym_id, flag, ms = [], [], []
    for s, ep in eps.items():
        rt, _rp = rk[s]
        si = ep["start"]
        if si.size == 0:
            continue
        sym_id.append(np.full(si.size, at[s], dtype="int64"))
        flag.append(watched_at(watch, s, rt[si]))
        ms.append(rt[si])
    return {
        "symbols": syms, "n_symbols": len(syms),
        "sym_id": np.concatenate(sym_id) if sym_id else np.array([], "int64"),
        "in_tier3": np.concatenate(flag) if flag else np.array([], dtype=bool),
        "shot_ms": np.concatenate(ms) if ms else np.array([], "int64"),
    }


def base_rate_draws_by_symbol(watch: dict, rk: dict, *,
                              per_symbol: int = BASE_RATE_PER_SYMBOL,
                              seed: int = SEED) -> dict:
    """`tick_stages.coverage_base_rate` **와 같은 추첨**을 종목 라벨을 남겨 되풀이한다.

    순회 순서·건너뛰기 조건(`rt.size < 3`)·추첨 크기(`min(per_symbol, rt.size)`)를
    **그대로** 맞춘다. 그래야 §4-2 의 1.6% / 2.4% 를 재현한 것이지 비슷한 다른 값이
    아니다. `tests/test_scale_mixed_recount.py` 가 두 값이 같은지 지킨다.
    """
    rng = np.random.default_rng(seed)
    syms = list(rk)
    at = {s: i for i, s in enumerate(syms)}
    sym_id, flag = [], []
    for s, (rt, _rp) in rk.items():
        if rt.size < 3:
            continue
        idx = rng.integers(0, rt.size, size=min(per_symbol, rt.size))
        sym_id.append(np.full(idx.size, at[s], dtype="int64"))
        flag.append(watched_at(watch, s, rt[idx]))
    return {
        "symbols": syms, "n_symbols": len(syms), "seed": seed,
        "per_symbol": per_symbol,
        "sym_id": np.concatenate(sym_id) if sym_id else np.array([], "int64"),
        "in_tier3": np.concatenate(flag) if flag else np.array([], dtype=bool),
    }


def d16_rows(shots: dict, draw: dict, eps: dict) -> dict:
    """슈팅 줄과 기저율 줄을 **두 눈금(+ 슈팅 구성 가중)으로** 낸다.

    ★ 눈금줄에서 갈리는 지점 하나를 미리 적어 둔다: 기저율을 **슈팅 수로 가중**하면
    슈팅이 0 건인 종목의 가중치가 0 이 되므로, `all_symbols` 와 `shot_symbols_only`
    구분이 **자동으로 사라진다.** 즉 정본 눈금에서 기저율은 **하나뿐**이다.
    """
    n_sym = shots["n_symbols"]
    shot_w = np.bincount(shots["sym_id"], minlength=n_sym).astype("float64")
    shot_syms = np.zeros(n_sym, dtype="float64")
    at = {s: i for i, s in enumerate(shots["symbols"])}
    for s in eps:
        if s in at:
            shot_syms[at[s]] = 1.0

    c_shot, r_shot = per_symbol_rates(shots["sym_id"], shots["in_tier3"], n_sym)
    c_draw, r_draw = per_symbol_rates(draw["sym_id"], draw["in_tier3"], n_sym)

    return {
        "shot_side": {
            "n_shots": int(shots["sym_id"].size),
            "n_symbols_with_shots": int((shot_w > 0).sum()),
            "shot_weighted": fold(c_shot, r_shot),
            "symbol_uniform": fold(c_shot, r_shot, "uniform"),
            "concentration": concentration(c_shot),
            "what_scale_docs44_published": (
                "**사건(슈팅) 가중.** stage3_coverage 가 종목을 이어 붙인 뒤 mean() 을 "
                "낸다 — 슈팅 하나가 한 표다. §4-2 의 4.2% 가 이 칸이다."),
        },
        "base_rate": {
            "n_draws": int(draw["sym_id"].size),
            "per_symbol": draw["per_symbol"], "seed": draw["seed"],
            "pooled_all_symbols": fold(c_draw, r_draw),
            "pooled_shot_symbols_only": fold(c_draw, r_draw,
                                             np.where(shot_syms > 0,
                                                      c_draw.astype("float64"), 0.0)),
            "symbol_uniform_all_symbols": fold(c_draw, r_draw, "uniform"),
            "symbol_uniform_shot_symbols_only": fold(c_draw, r_draw, shot_syms),
            "shot_weighted": fold(c_draw, r_draw, shot_w),
            "what_scale_docs44_published": (
                "**종목당 균등에 가깝다.** coverage_base_rate 가 종목마다 "
                f"min({draw['per_symbol']}, 관측수) 개를 뽑아 이어 붙인다. 관측이 "
                f"{draw['per_symbol']}개보다 적은 종목은 표가 적으므로 **정확히** 종목당 "
                "균등은 아니다 — 그래서 `pooled_*`(§4-2 가 실은 값)과 "
                "`symbol_uniform_*`(진짜 종목당 균등)을 둘 다 낸다."),
            "collapse_note": (
                "`shot_weighted` 는 슈팅 0 건인 종목에 가중치 0 을 준다. 그래서 정본 "
                "눈금에서는 `all_symbols` 와 `shot_symbols_only` 가 **같은 값**이다 — "
                "§4-2 의 1.6% / 2.4% 두 줄이 하나로 합쳐진다."),
        },
    }


def d16_ratio(rows: dict, sweep: dict) -> dict:
    """`슈팅 순간 ÷ 기저율`. **같은 눈금 안에서만 나눈다.** (§4-2 는 배수로 읽었다.)"""
    shot = rows["shot_side"]
    base = rows["base_rate"]
    out: dict = {}
    for scale, shot_key, base_key, noise_key in (
            ("shot_weighted", "shot_weighted", "shot_weighted", "shot_weighted"),
            ("symbol_uniform", "symbol_uniform", "symbol_uniform_shot_symbols_only",
             "symbol_uniform_shot_symbols_only")):
        s = shot[shot_key]["value"]
        b = base[base_key]["value"]
        out[scale] = {
            "shot_moment": s, "base_rate": b,
            "ratio": None if not b else s / b,
            "difference_pp": None if None in (s, b) else (s - b) * 100.0,
            "base_rate_seed_spread_pp": sweep["spread"].get(noise_key, {}).get(
                "spread_pp"),
        }
    out["mixed_scale_as_published"] = (
        "§4-2 는 슈팅 순간 4.2%(사건 가중) ÷ 기저율 2.4%(종목당 균등) = **1.75배** 로 "
        "읽었다. **두 값이 서로 다른 눈금이다.** 위 어느 줄과도 같지 않다.")
    out["how_to_read"] = (
        "배수가 1 을 넘는지, 그리고 그 움직임이 기저율의 씨앗 산포보다 큰지 둘만 본다. "
        "**판정하지 않는다** — '선정이 헛돈다/안 돈다'는 이 모듈이 쓸 문장이 아니다.")
    return out


def d16_seed_sweep(watch: dict, rk: dict, shots: dict, eps: dict, *,
                   seeds: tuple[int, ...] = SEED_SWEEP,
                   per_symbol: int = BASE_RATE_PER_SYMBOL) -> dict:
    """기저율 줄의 추첨 잡음. 슈팅 줄은 추첨이 없어 씨앗에 안 흔들린다."""
    per_seed: dict = {}
    for s in seeds:
        d = base_rate_draws_by_symbol(watch, rk, per_symbol=per_symbol, seed=s)
        b = d16_rows(shots, d, eps)["base_rate"]
        per_seed[str(s)] = {k: b[k]["value"] for k in
                            ("pooled_all_symbols", "pooled_shot_symbols_only",
                             "symbol_uniform_all_symbols",
                             "symbol_uniform_shot_symbols_only", "shot_weighted")}
    spread: dict = {}
    for field in next(iter(per_seed.values())):
        vals = [v[field] for v in per_seed.values() if v[field] is not None]
        if vals:
            spread[field] = {"min": min(vals), "max": max(vals),
                             "spread_pp": (max(vals) - min(vals)) * 100.0,
                             "mean": float(np.mean(vals)),
                             "sd": float(np.std(vals, ddof=1)) if len(vals) > 1 else 0.0}
    return {"seeds": list(seeds), "per_seed": per_seed, "spread": spread,
            "not_a_ci": "추첨 잡음이지 표본 CI 가 아니다."}


def d16_reproduction(watch: dict, rk: dict, eps: dict, draw: dict, shots: dict, *,
                     per_symbol: int = BASE_RATE_PER_SYMBOL, seed: int = SEED) -> dict:
    """**옛 값이 옛 눈금에서 그대로 나오는가.**

    기저율은 `coverage_base_rate` 를 **그대로 불러** 대조한다(추첨이 있어 순서가 어긋나면
    값이 달라진다 — 진짜 검사다). 슈팅 줄은 추첨이 없어 `stage3_coverage` 의 식
    `np.concatenate(wide_flag).mean()` 을 그 자리에서 다시 세워 대조한다 —
    `stage3_coverage` 자체는 400종목의 초 막대를 다시 읽어(`confirm_wide_shots`) 여기서
    부르기엔 비싸다. **부르지 않았다는 사실을 숨기지 않는다.**
    """
    n_sym = shots["n_symbols"]
    c_draw, r_draw = per_symbol_rates(draw["sym_id"], draw["in_tier3"], n_sym)
    shot_syms = np.zeros(n_sym, dtype="float64")
    at = {s: i for i, s in enumerate(shots["symbols"])}
    for s in eps:
        if s in at:
            shot_syms[at[s]] = 1.0
    ref = coverage_base_rate(watch, rk, eps, seed=seed, per_symbol=per_symbol)

    mine_all = fold(c_draw, r_draw)
    mine_shot = fold(c_draw, r_draw,
                     np.where(shot_syms > 0, c_draw.astype("float64"), 0.0))
    c_shot, r_shot = per_symbol_rates(shots["sym_id"], shots["in_tier3"], n_sym)
    pooled_shot = (float(np.asarray(shots["in_tier3"]).mean())
                   if shots["in_tier3"].size else None)
    mine_shot_side = fold(c_shot, r_shot)

    def cmp(mine, theirs):
        return (mine is not None and theirs is not None
                and abs(mine - theirs) < 1e-12)

    return {
        "base_rate__all_symbols": {
            "mine_pooled": mine_all["value"],
            "tick_stages_coverage_base_rate": ref["all_symbols"]["in_tier3_share"],
            "n_mine": mine_all["n_events"], "n_theirs": ref["all_symbols"]["n"],
            "same": cmp(mine_all["value"], ref["all_symbols"]["in_tier3_share"]),
        },
        "base_rate__shot_symbols_only": {
            "mine_pooled": mine_shot["value"],
            "tick_stages_coverage_base_rate": ref["shot_symbols_only"]["in_tier3_share"],
            # `fold` 의 `n_events` 는 **가중치가 아니라 min_events 로 걸린 것**만 센다.
            # 여기는 가중치로 종목을 뺐으므로 분모를 직접 센다.
            "n_mine": int(c_draw[shot_syms > 0].sum()),
            "n_theirs": ref["shot_symbols_only"]["n"],
            "same": cmp(mine_shot["value"],
                        ref["shot_symbols_only"]["in_tier3_share"]),
        },
        "shot_side__pooled": {
            "mine_shot_weighted_fold": mine_shot_side["value"],
            "stage3_coverage_expression": pooled_shot,
            "n_mine": mine_shot_side["n_events"], "n_theirs": int(shots["in_tier3"].size),
            "same": cmp(mine_shot_side["value"], pooled_shot),
            "caveat": ("`stage3_coverage` 를 직접 부르지 않고 그 식을 다시 세워 "
                       "대조했다 — 그 함수는 confirm_wide_shots 로 400종목의 초 막대를 "
                       "다시 읽는다. 추첨이 없어 재현 위험이 없는 줄이다."),
        },
        "what_this_guards": (
            "**옛 눈금에서 옛 값이 그대로 나와야 한다.** 창이 §4-2 때보다 늘어 그 표의 "
            "소수점과는 안 맞는다 — 맞아야 하는 것은 **이 실행 안에서 두 값이 같은가**다."),
    }


# --------------------------------------------------------------------------- #
# 4. 조립
# --------------------------------------------------------------------------- #
def _session_mix(ts_ms: np.ndarray) -> dict:
    if ts_ms.size == 0:
        return {}
    s = session_of(ts_ms)
    return {k: float((s == k).mean()) for k in ("pre", "regular", "after", "overnight")}


def build_report(conn: sqlite3.Connection, *, rise: float = SHOT_RISE,
                 max_seconds: int = SHOT_MAX_SECONDS, seed: int = SEED) -> dict:
    t0 = time.time()
    # ---- D-15: 초 막대 위 -------------------------------------------------- #
    syms = select_symbols(conn)
    bars = load_bars(conn, syms)
    n_bars = int(sum(b[0].size for b in bars.values()))
    print(f"  [1/6] second bars: {len(bars)} symbols / {n_bars} bars  "
          f"({time.time()-t0:.0f}s)")
    anch = collect_by_symbol(bars, rise=rise, max_seconds=max_seconds,
                             horizons=(max_seconds,))
    n_sym = anch["n_symbols"]
    weight_sets = {
        kind: np.bincount(anch[kind]["sym_id"], minlength=n_sym).astype("float64")
        for kind in ("causal_fire", "stage1_cross")}
    print(f"  [2/6] anchors: causal_fire {anch['causal_fire']['sym_id'].size}, "
          f"stage1_cross {anch['stage1_cross']['sym_id'].size}  "
          f"({time.time()-t0:.0f}s)")

    draw = ruler_draws_by_symbol(bars, horizons=(max_seconds,),
                                 draws_per_symbol=DRAWS_PER_SYMBOL, seed=seed)
    events = d15_event_rows(anch, horizon_s=max_seconds)
    ruler = d15_ruler_row(draw, weight_sets, horizon_s=max_seconds)
    sweep15 = d15_seed_sweep(bars, weight_sets, horizon_s=max_seconds)
    print(f"  [3/6] D-15 ruler + {len(SEED_SWEEP)} seeds  ({time.time()-t0:.0f}s)")

    # ---- D-16: 랭킹 가격 스트림 위 ------------------------------------------ #
    watch = load_tier3_watch(conn)
    rk = load_ranking_series(conn)
    print(f"  [4/6] ranking series: {len(rk)} symbols, tier3 watch "
          f"{len(watch)} symbols  ({time.time()-t0:.0f}s)")
    eps = wide_episodes(rk, rise=rise, max_seconds=max_seconds)
    shots = wide_shot_coverage_by_symbol(watch, rk, eps)
    print(f"  [5/6] wide shots: {shots['sym_id'].size} in {len(eps)} symbols  "
          f"({time.time()-t0:.0f}s)")
    bdraw = base_rate_draws_by_symbol(watch, rk, per_symbol=BASE_RATE_PER_SYMBOL,
                                      seed=seed)
    rows16 = d16_rows(shots, bdraw, eps)
    sweep16 = d16_seed_sweep(watch, rk, shots, eps)
    print(f"  [6/6] D-16 base rate + {len(SEED_SWEEP)} seeds  "
          f"({time.time()-t0:.0f}s)")

    a_ms = anch["causal_fire"]["anchor_ms"]
    rk_end = max((int(t[-1]) for t, _p in rk.values() if t.size), default=0)
    bar_end = max((int(b[0][-1]) for b in bars.values() if b[0].size), default=0)

    return {
        "question": (
            "docs/44 §12-8 (C) 가 찾은 눈금 섞인 대조 다섯 중 **하중을 받는 둘**"
            "(D-15 · D-16)을 D-14 정본 눈금(발화/사건 가중)에서 다시 낸다. "
            "**판정은 하지 않는다.**"),
        "conditions": {
            "window_start_ms": WINDOW_START_MS,
            "window_end_ms_second_bars": bar_end,
            "window_end_ms_ranking": rk_end,
            "symbols_second_bars": len(bars), "second_bars": n_bars,
            "symbols_ranking": len(rk), "symbols_tier3_watch": len(watch),
            "rise": rise, "max_seconds": max_seconds,
            "price_series_d15": "초 막대 vwap (tick_stages 와 같은 열)",
            "price_series_d16": "랭킹 last_u (두 실시간 목록의 합집합)",
            "convention_d15": (
                "빈 창(창 안에 체결이 하나도 없음)은 **분모에서 뺀다** — "
                "`probe_summary` 의 `pct_table(mx[has])` 규약이 이쪽이고 §10-5 의 표가 "
                "전부 그 값이다. 뺀 건수를 같이 싣는다."),
            "seed": seed, "seeds_swept": list(SEED_SWEEP),
            "draws_per_symbol_d15": DRAWS_PER_SYMBOL,
            "draws_per_symbol_d16": BASE_RATE_PER_SYMBOL,
            "sessions_causal_fire": _session_mix(a_ms),
            "sessions_wide_shots": _session_mix(shots["shot_ms"]),
            "reused_not_reimplemented": (
                "collect_by_symbol · ruler_draws_by_symbol · fold · per_symbol_rates · "
                "probe_summary · random_time_baseline · coverage_base_rate · "
                "wide_episodes · watched_at 을 그대로 불러 쓴다. "
                "**기존 코드는 한 줄도 안 고쳤다.**"),
            "not_in_scope": (
                "D-13(테이프 밀도 정합)은 여기서 하지 않는다 — 한 번에 하나만 움직인다. "
                "§12-8 (C) 의 1·4·5번도 다시 내지 않는다(1번은 §12 가 이미 냈고, "
                "4번은 문서가 스스로 약한 대조라 적었고, 5번은 표기다)."),
            "costs_excluded": "스프레드·수수료·슬리피지 전부 미포함",
        },
        "d15": {
            "what_was_mixed": (
                "§10-5 의 무작위 막대 60초 최대 상승 **+1.43%** 는 "
                "`random_time_baseline(draws_per_symbol=300)` — **종목당 균등**이고, "
                "나란히 놓인 인과적 발화 **+1.38%** 는 종목을 이어 붙인 값 — "
                "**발화 가중**이다. §10-8 1번이 그 뺄셈 위에 서 있다."),
            "reproduction": d15_reproduction(anch, draw, bars,
                                             horizon_s=max_seconds, seed=seed),
            "events": events, "ruler": ruler,
            "comparison_max_ret": d15_comparison(events, ruler, sweep15,
                                                 field="max_ret"),
            "comparison_end_ret": d15_comparison(events, ruler, sweep15,
                                                 field="end_ret"),
            "seed_sensitivity": sweep15,
        },
        "d16": {
            "what_was_mixed": (
                "§4-2 의 기저율 **1.6% / 2.4%** 는 "
                f"`coverage_base_rate(per_symbol={BASE_RATE_PER_SYMBOL})` — 종목마다 "
                "같은 개수를 뽑은 값이고, 나란히 놓인 슈팅 순간 **4.2%** 는 넓은 스트림 "
                "슈팅을 이어 붙인 값 — **사건 가중**이다. \"1.75배\"가 그 나눗셈이다."),
            "reproduction": d16_reproduction(watch, rk, eps, bdraw, shots,
                                             per_symbol=BASE_RATE_PER_SYMBOL,
                                             seed=seed),
            "rows": rows16,
            "ratio": d16_ratio(rows16, sweep16),
            "seed_sensitivity": sweep16,
        },
        "still_mixed_not_recomputed": [
            "§12-8 (C) 1번 — §10-3 · §10-4 · §10-7. **§12 가 이미 냈다.**",
            "§12-8 (C) 4번 — §3-3 의 무작위 **종목** 위약 열. 문서가 이미 n=263 "
            "'약한 대조'로 표시했다. 목록만 유지한다.",
            "§12-8 (C) 5번 — §11-6 사다리 첫 칸. 이미 '종목당 균등'이라 표기돼 있다. "
            "재계산이 아니라 §12 값으로 갱신하면 되는 자리다.",
        ],
        "not_claimed": [
            "**판정하지 않는다.** 어떤 설계도 살리거나 죽이지 않는다.",
            "CI 가 없다. 표본은 여전히 2.6 정규장이고 씨앗 산포는 추첨 잡음일 뿐이다.",
            "테이프 밀도(D-13)를 안 맞췄다. 눈금 하나만 바꾼 값이다.",
            "포화 칸을 안 갈랐다 — 끊긴 구간은 '앵커가 정점' 쪽으로 치우친다(§10-8 3번).",
            "D-16 의 넓은 스트림 슈팅은 12.2초 격자로 본 것이라 체결 쪽 슈팅과 "
            "**같은 사건이 아니다**(§4-3). 두 수를 직접 비교하면 안 된다.",
            "D-16 의 tier3 소속은 **호가 폴 밀도 대리지표**다(±30초에 3폴 이상). "
            "그 판정 자체는 이 태스크에서 안 흔들었다.",
        ],
    }


# --------------------------------------------------------------------------- #
# 5. 화면 요약 (ASCII — JSON 쪽에 한국어 본문이 있다)
# --------------------------------------------------------------------------- #
def _p(v, nd=4):
    return "     -" if v is None else f"{v:.{nd}f}"


def print_report(rep: dict) -> None:
    c = rep["conditions"]
    print(f"\n=== conditions: D-15 {c['symbols_second_bars']} symbols / "
          f"{c['second_bars']} second bars, end_ms {c['window_end_ms_second_bars']}")
    print(f"                D-16 {c['symbols_ranking']} ranking symbols, "
          f"end_ms {c['window_end_ms_ranking']}")
    print(f"    seed {c['seed']}, draws/symbol D-15 {c['draws_per_symbol_d15']} / "
          f"D-16 {c['draws_per_symbol_d16']}, rise {c['rise']}, "
          f"horizon {c['max_seconds']}s")
    ss = c.get("sessions_causal_fire", {})
    print("    causal_fire sessions: " + "  ".join(f"{k} {v:.3f}" for k, v in ss.items()))
    sw = c.get("sessions_wide_shots", {})
    print("    wide shot  sessions: " + "  ".join(f"{k} {v:.3f}" for k, v in sw.items()))

    # ---- D-15 ----------------------------------------------------------- #
    print("\n" + "=" * 78)
    print("=== D-15  docs/44 section 10-5: random bar 60s max rise (+1.43%)")
    print("=" * 78)
    print("\n--- reproduction (old value must come out on the OLD scale)")
    for k, v in rep["d15"]["reproduction"].items():
        if not isinstance(v, dict):
            continue
        mine = v.get("mine_fire_weighted", v.get("mine_pooled_over_equal_draws"))
        theirs = v.get("cross_peak_check_probe_summary",
                       v.get("cross_peak_check_random_time_baseline"))
        print(f"  {k:<30} mine {_p(mine, 6)}  cross_peak_check {_p(theirs, 6)}  "
              f"n {v['n_mine']}/{v['n_theirs']}  same={v['same']}")

    print("\n--- 60s MAX RISE, both scales (empty windows dropped, as published)")
    print(f"  {'row':<34}{'n_used':>8}{'fire-wtd':>11}{'sym-unif':>11}"
          f"{'fw p50':>10}{'su p50':>10}")
    for kind, label in (("causal_fire", "causal fire   (was +1.38%)"),
                        ("stage1_cross", "stage1 cross  (was +0.83%)")):
        r = rep["d15"]["events"][kind]["max_ret"]
        print(f"  {label:<34}{r['n_used']:>8}"
              f"{_p(r['fire_weighted']['mean'], 6):>11}"
              f"{_p(r['symbol_uniform']['mean'], 6):>11}"
              f"{_p(r['fire_weighted'].get('p50'), 6):>10}"
              f"{_p(r['symbol_uniform'].get('p50'), 6):>10}")
    ru = rep["d15"]["ruler"]["max_ret"]
    print(f"  {'random bar    (was +1.43%)':<34}{ru['n_used']:>8}"
          f"{_p(ru['weighted_by_causal_fire']['mean'], 6):>11}"
          f"{_p(ru['symbol_uniform']['mean'], 6):>11}"
          f"{_p(ru['weighted_by_causal_fire'].get('p50'), 6):>10}"
          f"{_p(ru['symbol_uniform'].get('p50'), 6):>10}")
    print("    ruler fire-wtd column = weighted_by_causal_fire "
          "(ruler symbol mix set equal to the fires')")
    print(f"    ruler weighted_by_stage1_cross = "
          f"{_p(ru['weighted_by_stage1_cross']['mean'], 6)}"
          f"   ruler pooled_over_equal_draws = "
          f"{_p(ru['pooled_over_equal_draws'], 6)}")
    print(f"    empty-window share: causal_fire "
          f"{_p(rep['d15']['events']['causal_fire']['max_ret']['share_empty_window'])}"
          f"  random bar {_p(ru['share_empty_window'])}")

    print("\n--- THE COMPARISON (event minus ruler), WITHIN one scale only")
    for kind in ("causal_fire", "stage1_cross"):
        g = rep["d15"]["comparison_max_ret"][kind]
        print(f"  [{kind}]  60s max rise")
        for scale in ("fire_weighted", "symbol_uniform"):
            s = g[scale]
            sp = s["ruler_seed_spread_pp"]
            tail = f"   (ruler seed spread {sp:.3f}pp)" if sp is not None else ""
            print(f"    {scale:<16} event {_p(s['event_mean'], 6)}  "
                  f"ruler {_p(s['ruler_mean'], 6)}  "
                  f"diff {s['event_minus_ruler_pp']:+.3f}pp{tail}")
        print(f"    the diff itself is {g.get('gap_vs_seed_noise', 'n/a')} "
              f"vs seed noise; changing scale moves it {g['gap_moves_pp']:+.3f}pp "
              f"({g.get('move_vs_seed_noise', 'n/a')})")
    print("  as published (section 10-5): causal +1.38% (fire-weighted) vs "
          "random +1.43% (symbol-uniform) = MIXED SCALES")

    print("\n--- plain 60s return (end_ret), same two scales")
    for kind in ("causal_fire", "stage1_cross"):
        g = rep["d15"]["comparison_end_ret"][kind]
        for scale in ("fire_weighted", "symbol_uniform"):
            s = g[scale]
            print(f"  {kind:<14}{scale:<16} event {_p(s['event_mean'], 6)}  "
                  f"ruler {_p(s['ruler_mean'], 6)}  "
                  f"diff {s['event_minus_ruler_pp']:+.3f}pp")

    print("\n--- ruler seed sensitivity (NOT a confidence interval)")
    for field, s in rep["d15"]["seed_sensitivity"]["spread"].items():
        print(f"  {field:<42} min {_p(s['min'], 6)}  max {_p(s['max'], 6)}  "
              f"spread {s['spread_pp']:.3f}pp")

    # ---- D-16 ----------------------------------------------------------- #
    print("\n" + "=" * 78)
    print("=== D-16  docs/44 section 4-2: coverage base rate (1.6% / 2.4%) "
          "and the 1.75x")
    print("=" * 78)
    print("\n--- reproduction (old value must come out on the OLD scale)")
    for k, v in rep["d16"]["reproduction"].items():
        if not isinstance(v, dict):
            continue
        mine = v.get("mine_pooled", v.get("mine_shot_weighted_fold"))
        theirs = v.get("tick_stages_coverage_base_rate",
                       v.get("stage3_coverage_expression"))
        print(f"  {k:<32} mine {_p(mine, 6)}  original {_p(theirs, 6)}  "
              f"n {v['n_mine']}/{v['n_theirs']}  same={v['same']}")

    rows = rep["d16"]["rows"]
    sh, ba = rows["shot_side"], rows["base_rate"]
    print(f"\n--- THE TWO SIDES, both scales   (shots {sh['n_shots']} in "
          f"{sh['n_symbols_with_shots']} symbols; base-rate draws {ba['n_draws']})")
    print(f"  {'row':<42}{'shot-wtd':>11}{'sym-unif':>11}")
    print(f"  {'shot moment in tier3   (was 4.2%)':<42}"
          f"{_p(sh['shot_weighted']['value']):>11}"
          f"{_p(sh['symbol_uniform']['value']):>11}")
    print(f"  {'base rate, shot symbols only (was 2.4%)':<42}"
          f"{_p(ba['shot_weighted']['value']):>11}"
          f"{_p(ba['symbol_uniform_shot_symbols_only']['value']):>11}")
    print(f"  {'base rate, all symbols       (was 1.6%)':<42}"
          f"{_p(ba['shot_weighted']['value']):>11}"
          f"{_p(ba['symbol_uniform_all_symbols']['value']):>11}")
    print("    on the shot-weighted scale the two base-rate rows COLLAPSE into one "
          "(0-shot symbols get weight 0)")
    print(f"    what section 4-2 published: pooled_all_symbols "
          f"{_p(ba['pooled_all_symbols']['value'])}  "
          f"pooled_shot_symbols_only {_p(ba['pooled_shot_symbols_only']['value'])}")

    print("\n--- THE RATIO (shot moment / base rate), WITHIN one scale only")
    for scale in ("shot_weighted", "symbol_uniform"):
        r = rep["d16"]["ratio"][scale]
        sp = r["base_rate_seed_spread_pp"]
        print(f"  {scale:<16} shot {_p(r['shot_moment'])}  base {_p(r['base_rate'])}  "
              f"ratio {r['ratio']:.3f}x  diff {r['difference_pp']:+.3f}pp"
              + (f"   (base seed spread {sp:.3f}pp)" if sp is not None else ""))
    print("  as published (section 4-2): 4.2% (event-weighted) / 2.4% "
          "(symbol-uniform) = 1.75x = MIXED SCALES")

    print("\n--- base-rate seed sensitivity (NOT a confidence interval)")
    for field, s in rep["d16"]["seed_sensitivity"]["spread"].items():
        print(f"  {field:<38} min {_p(s['min'], 6)}  max {_p(s['max'], 6)}  "
              f"spread {s['spread_pp']:.3f}pp")

    print("\n=== not claimed (Korean text in the JSON)")
    print("  - no verdict; no design is kept or killed here")
    print("  - no CI; sample is still 2.6 regular sessions; seed spread is draw noise")
    print("  - tape density (D-13) NOT matched - only the scale was changed")
    print("  - three of the five mixed-scale comparisons were NOT recomputed "
          "(list kept, see JSON)")


def main(db: Path, *, out_dir: Path | None = None) -> int:
    out_dir = out_dir or OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    print("=== scale-mixed recount D-15 / D-16 (read-only, no live calls)")
    conn = open_ro(db)
    try:
        rep = build_report(conn)
    finally:
        conn.close()
    (out_dir / "scale_mixed_recount.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print_report(rep)
    print(f"\nwrote {out_dir / 'scale_mixed_recount.json'}")
    return 0


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/tossmon.db")
    raise SystemExit(main(path))

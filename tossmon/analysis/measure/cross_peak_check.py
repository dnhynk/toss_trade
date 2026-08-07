"""**"교차가 곧 정점" 57.6% 는 시장인가 우리 경계인가** (docs/44 §10).

`docs/44` §2-3 의 헤드라인은 이것이다:

> 슈팅의 **57.6%** 는 "+1% 를 처음 넘는 순간"이 곧 정점이다.
> → 해상도 문제가 아니라 **임계 정의의 문제**였다.

이 모듈은 **그 문장 하나만** 다시 잰다. 의심하는 지점은 분모가 아니라 **창**이다.

## 왜 의심하는가 — 규칙을 그대로 읽어 보면

`tick_stages.find_episodes` 의 정점은 이렇게 정해진다:

    fwd = mat[1:]                 # 시작 막대에서 앞으로 max_seconds 안의 막대들
    best_d = fwd.argmax(axis=0)   # 그 안의 **최고가 막대**
    p = i + best_d                # ← 이것이 '정점' 이고 에피소드는 여기서 끝난다

즉 **창이 시작(=사후적 저점)에 붙어 있다.** 교차는 그 창 안 어딘가에서 일어나므로,
교차 뒤에 남는 관측 시간은 60초가 아니라 **60 − (교차까지 걸린 시간)** 이다.
`docs/44` §2-3 이 실은 그 값을 이미 싣고 있다 — 교차 p50 20초, p75 46초, p90 58초.
**상위 10%는 교차 뒤에 2초밖에 안 본다.** 그 2초 안에 더 높은 막대가 없으면
"교차가 곧 정점"이 된다. 이건 시장이 아니라 **자의 길이**다.

## 그래서 무엇을 하는가 — 앵커와 창을 **따로** 흔든다

두 축을 2×2 로 갈라야 "탐지기 탓" 과 "경계 탓" 이 분리된다.

| 앵커 \\ 창 | 에피소드식(저점 + 60초에서 끝) | **고정 시계**(앵커 + H초) |
|---|---|---|
| 1단계 교차 (사후적 저점 기준) | 57.6% 를 재현해야 한다 | ? |
| 인과적 발화 `find_fires` | ? | ? |

**고정 시계 쪽은 에피소드 끝을 아예 안 쓴다.** 에피소드가 언제 끝나든 답이 나온다 —
그게 "시장" 과 "우리 경계" 를 가르는 유일한 방법이다.

## 이 모듈이 하지 않는 것

- **판정하지 않는다.** 어떤 설계도 살리거나 죽이지 않는다.
- **기존 코드를 고치지 않는다.** `tick_stages.find_episodes` · `find_fires` 를
  **그대로 불러 쓴다** — 사건 집합이 달라지면 57.6% 와 나란히 놓을 수 없다.
- `docs/44` 의 다른 두 결과(임계 대칭·커버리지 1.75배)는 건드리지 않는다.

실행: `python -m tossmon.analysis.measure.cross_peak_check [db_path]`
→ `out/cross_peak_check.json`. **라이브 콜 0. DB 는 `mode=ro`.**
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

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
    price_band_of,
    select_symbols,
)

OUT_DIR = Path("out")

#: 고정 시계 창(초). 슈팅 지속 p50 이 32초라 그 앞뒤를 덮는다.
#: **60초를 포함하는 이유**: 에피소드식 창의 최대 길이와 같아야 "창이 짧아서" 인지
#: "앵커가 달라서" 인지가 갈린다.
HORIZONS_S: tuple[int, ...] = (5, 15, 30, 60)

#: 교차 뒤 **남은 관측 시간**을 이 칸으로 갈라 본다. 순환이 있다면 남은 시간이 짧을수록
#: "교차=정점" 비율이 1 에 붙는다 — 그 모양 자체가 증거다.
REMAINING_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("le_2s", 0.0, 2.0),
    ("2_to_10s", 2.0, 10.0),
    ("10_to_30s", 10.0, 30.0),
    ("30_to_50s", 30.0, 50.0),
    ("gt_50s", 50.0, float("inf")),
)


# --------------------------------------------------------------------------- #
# 핵심 — 앵커 막대에서 앞으로 H초. **에피소드 끝을 참조하지 않는다.**
# --------------------------------------------------------------------------- #
def forward_probe(ts: np.ndarray, px: np.ndarray, idx: np.ndarray,
                  horizon_s) -> dict:
    """앵커 막대 `idx` **다음** 막대부터 `horizon_s` 초까지를 본다.

    `horizon_s` 는 스칼라(고정 시계) 또는 앵커마다 다른 배열(에피소드식 창)이다.
    한 함수로 둘 다 받는 이유: 두 창을 **같은 기계**로 통과시켜야 차이가 창에서만
    나온다는 것을 보장할 수 있다.

    돌려주는 것 (앵커마다 한 값씩):
      - `n_bars`   창 안 막대 수. **0 이면 모르는 것이다** — "앵커가 정점" 이 아니다.
      - `max_ret`  창 안 최고가 / 앵커가 − 1. 앵커 자신은 빼고 잰다.
      - `end_ret`  창 안 **마지막** 막대 / 앵커가 − 1 (그냥 수익률).
      - `t_max_s`  최고가까지 걸린 시간(초).

    앵커 자신을 빼는 이유: "교차한 그 막대가 정점인가" 라는 물음은 **그 뒤에 더 높은
    막대가 있는가** 와 같은 물음이다. 자신을 넣으면 `max_ret >= 0` 이 자동으로 성립해
    물음이 사라진다.
    """
    idx = np.asarray(idx, dtype="int64")
    m = idx.size
    empty = {"n_bars": np.zeros(m, "int64"),
             "max_ret": np.full(m, np.nan), "end_ret": np.full(m, np.nan),
             "t_max_s": np.full(m, np.nan)}
    if m == 0 or ts.size == 0:
        return empty
    h = np.broadcast_to(np.asarray(horizon_s, dtype="float64"), (m,)).astype("float64")
    base_t = ts[idx].astype("int64")
    base_p = px[idx].astype("float64")

    # 1초 격자라 창 안 막대 수가 유계다 — 그래서 행렬로 펴도 안전하다.
    lim = base_t + np.maximum(h, 0.0) * 1000.0
    end = np.searchsorted(ts, lim.astype("int64"), side="right")
    k = int(np.max(end - idx)) if m else 1
    if k <= 1:
        return empty
    d = np.arange(1, k, dtype="int64")
    j = idx[None, :] + d[:, None]
    ok = j < ts.size
    jj = np.where(ok, j, 0)
    # 창 밖은 inf 로 밀어 낸다 — 아래 `inside` 가 prefix 마스크가 되도록.
    dt = np.where(ok, (ts[jj] - base_t[None, :]) / 1000.0, np.inf)
    inside = dt <= h[None, :]
    n_bars = inside.sum(axis=0).astype("int64")

    vals = np.where(inside, px[jj], -np.inf)
    top = vals.max(axis=0)
    top_d = vals.argmax(axis=0) + 1
    has = n_bars >= 1
    out = {"n_bars": n_bars,
           "max_ret": np.full(m, np.nan), "end_ret": np.full(m, np.nan),
           "t_max_s": np.full(m, np.nan)}
    if not has.any():
        return out
    out["max_ret"][has] = top[has] / base_p[has] - 1.0
    out["t_max_s"][has] = (ts[idx[has] + top_d[has]] - base_t[has]) / 1000.0
    # `inside` 가 prefix 마스크이므로 창 안 마지막 막대는 idx + n_bars 다.
    out["end_ret"][has] = px[idx[has] + n_bars[has]] / base_p[has] - 1.0
    return out


def trailing_min_index(ts: np.ndarray, px: np.ndarray, idx: np.ndarray,
                       lookback_s: int) -> np.ndarray:
    """앵커 막대에서 **뒤로** `lookback_s` 안의 최저가 막대. 동률이면 **가장 이른 것**.

    `find_fires` 가 기준으로 삼는 그 저점이다(값만 쓰고 위치는 안 돌려주므로 여기서
    다시 잡는다). 이 위치가 있어야 **1단계와 같은 기하**의 창을 발화 위에 얹을 수 있다:
    1단계 창은 저점 + 60초에서 끝나므로, 발화 쪽도 저점 + 60초에서 끝나야 대조가 된다.

    **동률에서 가장 이른 것을 고르는 이유**: 1단계의 시작점도 (앞에서부터 훑으므로)
    이른 쪽으로 치우친다. 이른 저점 → 창이 더 일찍 닫힘 → 발화 뒤 남는 시간이 **더 짧다**.
    즉 이 선택은 "발화도 정점이다" 쪽에 **유리한(보수적인)** 선택이다.
    """
    idx = np.asarray(idx, dtype="int64")
    if idx.size == 0 or ts.size == 0:
        return idx.copy()
    lo = np.searchsorted(ts, ts[idx] - lookback_s * 1000, side="left")
    k = int(np.max(idx - lo + 1))
    d = np.arange(k, dtype="int64")
    j = idx[None, :] - d[:, None]
    ok = (j >= 0) & (j >= lo[None, :])
    vals = np.where(ok, px[np.where(ok, j, 0)], np.inf)
    mn = vals.min(axis=0)
    is_min = vals <= mn[None, :]
    d_earliest = (k - 1) - np.argmax(is_min[::-1], axis=0)
    return idx - d_earliest


# --------------------------------------------------------------------------- #
# 요약 — 동률과 빈 창을 **숨기지 않는다**
# --------------------------------------------------------------------------- #
def probe_summary(res: dict) -> dict:
    """`forward_probe` 결과를 읽을 수 있는 표로.

    두 규약을 **둘 다** 낸다. 빈 창(창 안에 체결이 하나도 없다)을 어떻게 세느냐로
    답이 몇 %p 씩 움직이기 때문이다.
      - `..._incl_empty`  빈 창을 "더 안 올랐다" 로 센다. **1단계 규칙이 이쪽이다** —
        에피소드 정점은 창 안에서만 찾으므로 창이 비면 교차가 곧 정점이 된다.
      - `..._excl_empty`  빈 창을 분모에서 뺀다. **모르는 것을 모른다고 두는 쪽.**

    동률도 가른다: 뒤 막대가 앵커와 **같은 가격**이면 `argmax` 가 앞을 고르므로
    1단계는 그것을 "교차=정점" 으로 센다. 그래서 여기서도 **엄격히 더 높은가**를 쓴다.
    """
    n = int(res["n_bars"].size)
    if n == 0:
        return {"n": 0}
    has = res["n_bars"] >= 1
    mx = res["max_ret"]
    higher = np.zeros(n, dtype=bool)
    higher[has] = mx[has] > 0.0
    out = {
        "n": n,
        "n_empty_window": int((~has).sum()),
        "share_empty_window": float((~has).mean()),
        "share_anchor_is_max_incl_empty": float((~higher).mean()),
        "share_higher_later_incl_empty": float(higher.mean()),
    }
    if has.any():
        out["share_anchor_is_max_excl_empty"] = float((~higher[has]).mean())
        out["share_higher_later_excl_empty"] = float(higher[has].mean())
        out["max_ret"] = pct_table(mx[has])
        out["end_ret"] = pct_table(res["end_ret"][has])
        out["t_max_s"] = pct_table(res["t_max_s"][has])
        pos = mx[has] > 0
        if pos.any():
            out["max_ret_when_higher"] = pct_table(mx[has][pos])
        out["end_ret_share_positive"] = float((res["end_ret"][has] > 0).mean())
        out["end_ret_share_zero"] = float((res["end_ret"][has] == 0).mean())
    return out


def _bucket_of(v: np.ndarray) -> np.ndarray:
    out = np.full(np.shape(v), "gt_50s", dtype=object)
    for name, lo, hi in REMAINING_BUCKETS:
        out[(v >= lo) & (v < hi)] = name
    return out


# --------------------------------------------------------------------------- #
# 수집 — 두 앵커를 같은 막대 위에서 뽑는다
# --------------------------------------------------------------------------- #
def collect_anchors(bars: dict, *, rise: float = SHOT_RISE,
                    max_seconds: int = SHOT_MAX_SECONDS,
                    horizons: tuple[int, ...] = HORIZONS_S) -> dict:
    """종목별로 **1단계 교차**와 **인과적 발화**를 뽑고 두 창을 다 통과시킨다.

    두 앵커는 `tick_stages` 의 함수를 **그대로** 부른다. 여기서 다시 구현하면
    사건 수가 미세하게 달라져 57.6% 와 나란히 놓을 수 없다.
    """
    acc: dict = {}
    for kind in ("stage1_cross", "causal_fire"):
        acc[kind] = {
            "episode_window": {k: [] for k in ("n_bars", "max_ret", "end_ret", "t_max_s")},
            "fixed": {h: {k: [] for k in ("n_bars", "max_ret", "end_ret", "t_max_s")}
                      for h in horizons},
            "anchor_ms": [], "anchor_usd": [], "lead_s": [], "remaining_s": [],
            "n": 0,
        }
    ep_extra = {"duration_s": [], "total_rise": [], "cross_is_peak": []}

    for s, (ts, _n, _lo, _hi, vwap) in bars.items():
        ep = find_episodes(ts, vwap, vwap, rise=rise, max_seconds=max_seconds)
        si, ci, pi = ep["start"], ep["cross"], ep["peak"]
        fires = find_fires(ts, vwap, vwap, rise=rise, max_seconds=max_seconds,
                           cooldown_s=max_seconds)
        lows = trailing_min_index(ts, vwap, fires, max_seconds)

        for kind, anchor, origin in (("stage1_cross", ci, si),
                                     ("causal_fire", fires, lows)):
            if anchor.size == 0:
                continue
            lead = (ts[anchor] - ts[origin]) / 1000.0      # 저점 → 앵커 (초)
            rem = np.maximum(max_seconds - lead, 0.0)      # 에피소드식 창에 남은 시간
            a = acc[kind]
            a["n"] += int(anchor.size)
            a["anchor_ms"].append(ts[anchor])
            a["anchor_usd"].append(vwap[anchor] / 1e6)
            a["lead_s"].append(lead)
            a["remaining_s"].append(rem)
            r = forward_probe(ts, vwap, anchor, rem)
            for k in a["episode_window"]:
                a["episode_window"][k].append(r[k])
            for h in horizons:
                rf = forward_probe(ts, vwap, anchor, h)
                for k in a["fixed"][h]:
                    a["fixed"][h][k].append(rf[k])

        if si.size:
            ep_extra["duration_s"].append((ts[pi] - ts[si]) / 1000.0)
            ep_extra["total_rise"].append(vwap[pi] / vwap[si] - 1.0)
            ep_extra["cross_is_peak"].append(ci == pi)

    def cat(bag):
        return {k: (np.concatenate(v) if v else np.array([])) for k, v in bag.items()}

    out = {}
    for kind, a in acc.items():
        out[kind] = {
            "n": a["n"],
            "episode_window": cat(a["episode_window"]),
            "fixed": {h: cat(a["fixed"][h]) for h in horizons},
            "anchor_ms": np.concatenate(a["anchor_ms"]) if a["anchor_ms"] else np.array([]),
            "anchor_usd": (np.concatenate(a["anchor_usd"]) if a["anchor_usd"]
                           else np.array([])),
            "lead_s": np.concatenate(a["lead_s"]) if a["lead_s"] else np.array([]),
            "remaining_s": (np.concatenate(a["remaining_s"]) if a["remaining_s"]
                            else np.array([])),
        }
    out["stage1_episode_extra"] = cat(ep_extra)
    return out


# --------------------------------------------------------------------------- #
# 3번 물음 — 에피소드 종료 규칙이 순환을 만드는가
# --------------------------------------------------------------------------- #
def boundary_audit(anch: dict, *, max_seconds: int = SHOT_MAX_SECONDS,
                   horizons: tuple[int, ...] = HORIZONS_S) -> dict:
    """**1단계 에피소드는 무엇 때문에 끝나는가**, 그리고 그것이 답을 만드는가.

    규칙을 인용하고, 그 규칙이 만드는 두 가지를 센다:
      1. **천장에 눌린 몫** — 지속시간이 정확히 `max_seconds` 인 에피소드.
      2. **교차 뒤 남은 관측 시간별 "교차=정점" 비율.** 남은 시간이 짧을수록 1 에
         붙으면, 그 비율은 시장이 아니라 창의 길이를 재고 있는 것이다.
      3. **뒤집힘** — 에피소드식 창에서는 "교차=정점" 이었는데 고정 60초 창에서는
         더 높은 막대가 나온 건수. **그 건수가 곧 경계가 만들어 낸 몫이다.**
    """
    c = anch["stage1_cross"]
    ex = anch["stage1_episode_extra"]
    rem = c["remaining_s"]
    lead = c["lead_s"]
    dur = ex["duration_s"]
    is_peak_ep = ~(c["episode_window"]["max_ret"] > 0)     # nan(빈 창) → True
    n = int(is_peak_ep.size)

    by_rem = {}
    if n:
        b = _bucket_of(rem)
        for name, _lo, _hi in REMAINING_BUCKETS:
            m = b == name
            if not m.any():
                continue
            by_rem[name] = {
                "n": int(m.sum()), "share_of_all": float(m.mean()),
                "cross_is_peak_share": float(is_peak_ep[m].mean()),
            }

    flips = {}
    for h in horizons:
        mx = anch["stage1_cross"]["fixed"][h]["max_ret"]
        higher = np.zeros(n, dtype=bool)
        ok = np.isfinite(mx)
        higher[ok] = mx[ok] > 0
        flip = is_peak_ep & higher
        flips[f"fixed_{h}s"] = {
            "n_flipped": int(flip.sum()),
            "share_of_all_shots": float(flip.mean()) if n else None,
            "share_of_cross_is_peak": (float(flip.sum() / is_peak_ep.sum())
                                       if is_peak_ep.any() else None),
        }

    return {
        "the_rule_verbatim": (
            "tick_stages.find_episodes: `best_d = fwd.argmax(axis=0) + 1` / "
            "`p = i + int(best_d[i])` — 정점은 **시작 막대에서 앞으로 max_seconds 안의 "
            "최고가 막대**이고 에피소드는 거기서 끝난다. 그 다음 `i = p` 로 재시작한다."),
        "what_ends_an_episode": (
            "가격이 안 오르면 끝나는 것이 **아니다**. 창(시작 + max_seconds) 안의 "
            "argmax 에서 끝난다. 그래서 종료 시각은 **가격의 성질이 아니라 창의 길이**가 "
            "정한다 — 창이 닫히면 그 뒤에 더 높은 막대가 있어도 에피소드는 이미 끝나 있다."),
        "why_this_is_circular_for_the_57_6_number": (
            "창이 **시작(사후적 저점)** 에 붙어 있어, 교차 뒤에 남는 관측 시간은 "
            "60초가 아니라 60 − (교차까지 걸린 시간)이다. 교차 p90 이 58초이므로 "
            "상위 10%는 교차 뒤 **2초**만 본다. 그 2초에 더 높은 막대가 없으면 "
            "'교차 = 정점' 이 된다."),
        "time_to_cross_s": pct_table(lead),
        "remaining_window_after_cross_s": pct_table(rem),
        "remaining_window_shares": {
            "le_1s": float((rem <= 1).mean()) if n else None,
            "le_2s": float((rem <= 2).mean()) if n else None,
            "le_5s": float((rem <= 5).mean()) if n else None,
            "le_10s": float((rem <= 10).mean()) if n else None,
        },
        "duration_at_ceiling": {
            "n": int(dur.size),
            "share_exactly_max_seconds": (float((dur >= max_seconds).mean())
                                          if dur.size else None),
            "share_ge_59s": float((dur >= 59).mean()) if dur.size else None,
            "share_ge_55s": float((dur >= 55).mean()) if dur.size else None,
            "note": ("지속시간이 정확히 max_seconds 라는 것은 **창이 닫혀서** 끝났다는 "
                     "뜻이다 — 그 에피소드의 진짜 정점은 창 밖일 수 있다."),
        },
        "cross_is_peak_by_remaining_window": by_rem,
        "cross_is_peak_by_remaining_reading": (
            "남은 시간이 짧은 칸에서 비율이 1 에 붙고 긴 칸에서 떨어지면, 57.6% 는 "
            "**창의 길이 분포**를 재고 있는 것이다. 평평하면 시장의 성질이다."),
        "flipped_by_fixed_window": flips,
        "flipped_reading": (
            "에피소드식 창에서는 '교차 = 정점' 이었는데 같은 앵커에서 고정 창을 열면 "
            "더 높은 막대가 나온 건수. **경계가 만들어 낸 몫이 이만큼이다.**"),
    }


def random_time_baseline(bars: dict, *, horizons: tuple[int, ...] = HORIZONS_S,
                         draws_per_symbol: int = 300, seed: int = 20260808) -> dict:
    """**아무 막대나 찍으면 그 막대가 다음 H초의 최고가일 확률.** 고정 창 칸의 눈금이다.

    이게 없으면 고정 창의 30.7% 를 읽을 수 없다. 무작위 막대도 30% 면 앵커는 아무것도
    고르지 못한 것이고, 무작위가 15% 인데 앵커가 30% 면 앵커는 **정말로 고점을 고르고
    있다** — 크기는 배로 줄지만 방향은 남는다.

    **판정이 아니다.** 진입 후 수익률의 위약은 `docs/44` §3-3 이 이미 냈다(무작위 시각과
    구별 안 됨). 여기 것은 "이 막대가 고점인가" 라는 **다른 물음**의 눈금일 뿐이다.
    """
    rng = np.random.default_rng(seed)
    keys = ("n_bars", "max_ret", "end_ret", "t_max_s")
    acc: dict = {h: [] for h in horizons}
    n = 0
    for _s, (ts, _cnt, _lo, _hi, v) in bars.items():
        if ts.size < 3:
            continue
        idx = rng.integers(0, ts.size, size=min(draws_per_symbol, ts.size))
        n += int(idx.size)
        for h in horizons:
            acc[h].append(forward_probe(ts, v, idx, h))
    out: dict = {"n_draws": n, "draws_per_symbol": draws_per_symbol, "seed": seed,
                 "method": "종목마다 초 막대를 균등 추첨해 같은 고정 창을 통과시킨다"}
    for h in horizons:
        out[f"fixed_{h}s"] = (
            probe_summary({k: np.concatenate([a[k] for a in acc[h]]) for k in keys})
            if acc[h] else {"n": 0})
    return out


def sensitivity_vs_coordinator(bars: dict, ordered_syms: list, *,
                               rise: float = SHOT_RISE,
                               max_seconds: int = SHOT_MAX_SECONDS,
                               horizons: tuple[int, ...] = (30, 60),
                               top_n: int = 12) -> dict:
    """코디네이터 대조점(고정 30초에서 **85%**, 12종목)과 우리 값의 차이가 어디서 오는가.

    아는 차이는 넷이고, 그중 **둘은 여기서 직접 흔들 수 있다**:
      - 종목 폭 (그쪽 12 vs 우리 66) → 상위 12종목으로 좁혀 본다
      - 발화 후 60초 잠금 (그쪽에는 없다) → 잠금을 풀어 본다
    나머지 둘(가격열 vwap vs 단순 평균, 슈팅 필터 유무)은 여기서 못 흔든다 — 적어 둔다.
    """
    keys = ("n_bars", "max_ret", "end_ret", "t_max_s")
    top = [s for s in ordered_syms[:top_n] if s in bars]
    out: dict = {}
    for tag, syms, cooldown in (
            ("all_symbols_cooldown_60s", list(bars), max_seconds),
            (f"top{top_n}_by_trades_cooldown_60s", top, max_seconds),
            ("all_symbols_no_cooldown", list(bars), 0),
            (f"top{top_n}_by_trades_no_cooldown", top, 0)):
        acc: dict = {h: [] for h in horizons}
        n = 0
        for s in syms:
            ts, _n, _lo, _hi, v = bars[s]
            fires = find_fires(ts, v, v, rise=rise, max_seconds=max_seconds,
                               cooldown_s=cooldown)
            if fires.size == 0:
                continue
            n += int(fires.size)
            for h in horizons:
                acc[h].append(forward_probe(ts, v, fires, h))
        row = {"n_symbols": len(syms), "n_fires": n}
        for h in horizons:
            row[f"fixed_{h}s"] = (
                probe_summary({k: np.concatenate([a[k] for a in acc[h]]) for k in keys})
                if acc[h] else {"n": 0})
        out[tag] = row
    out["cannot_shake_here"] = [
        "가격열: 우리 vwap vs 그쪽 초당 단순 평균",
        "그쪽은 슈팅 필터가 없다 — 그냥 오르는 구간도 포함",
    ]
    return out


# --------------------------------------------------------------------------- #
# 조립
# --------------------------------------------------------------------------- #
def build_report(conn: sqlite3.Connection, *, rise: float = SHOT_RISE,
                 max_seconds: int = SHOT_MAX_SECONDS,
                 horizons: tuple[int, ...] = HORIZONS_S) -> dict:
    t0 = time.time()
    syms = select_symbols(conn)
    print(f"  [1/3] symbols: {len(syms)}  ({time.time()-t0:.0f}s)")
    bars = load_bars(conn, syms)
    print(f"  [2/3] second bars: {len(bars)}  ({time.time()-t0:.0f}s)")
    anch = collect_anchors(bars, rise=rise, max_seconds=max_seconds,
                           horizons=horizons)
    print(f"  [3/3] anchors: stage1_cross {anch['stage1_cross']['n']}, "
          f"causal_fire {anch['causal_fire']['n']}  ({time.time()-t0:.0f}s)")

    ex = anch["stage1_episode_extra"]
    rep: dict = {
        "question": ("docs/44 §2-3 의 57.6% 가 시장의 성질인가, 에피소드 경계가 "
                     "만들어 낸 값인가. **이 문서는 그것 하나만 잰다.**"),
        "conditions": {
            "window_start_ms": WINDOW_START_MS,
            "rise": rise, "max_seconds": max_seconds,
            "price_series": "초 막대 vwap (tick_stages 와 같은 열)",
            "symbols": len(bars),
            "anchors_are_reused_not_reimplemented": (
                "tick_stages.find_episodes / find_fires 를 그대로 부른다 — 사건 집합이 "
                "달라지면 57.6% 와 나란히 놓을 수 없다"),
            "horizons_s": list(horizons),
            "tie_rule": ("**엄격히 더 높은 막대**가 있어야 '교차가 정점이 아니다' 로 센다. "
                         "1단계 argmax 가 동률에서 앞을 고르는 것과 같은 규약이다."),
            "costs_excluded": "스프레드·수수료·슬리피지 전부 미포함",
        },
        "stage1_episode_reference": {
            "n_shots": int(ex["cross_is_peak"].size),
            "cross_is_peak_share_published_rule": (
                float(ex["cross_is_peak"].mean()) if ex["cross_is_peak"].size else None),
            "duration_s": pct_table(ex["duration_s"]),
            "total_rise": pct_table(ex["total_rise"]),
            "note": "docs/44 §2-3 의 57.6% 를 이 실행에서 재현한 값이다.",
        },
    }

    # ---- 2×2: 앵커 × 창 ---------------------------------------------------- #
    grid: dict = {}
    for kind in ("stage1_cross", "causal_fire"):
        a = anch[kind]
        grid[kind] = {
            "n": a["n"],
            "lead_from_low_s": pct_table(a["lead_s"]),
            "episode_style_window": probe_summary(a["episode_window"]),
            "fixed_window": {f"{h}s": probe_summary(a["fixed"][h]) for h in horizons},
        }
    rep["anchor_by_window_grid"] = grid
    rep["grid_reading"] = (
        "행(앵커)을 따라 움직이면 **탐지기** 탓이고, 열(창)을 따라 움직이면 "
        "**우리 경계** 탓이다. 둘 다 움직이면 둘 다다.")
    rep["random_time_baseline"] = random_time_baseline(bars, horizons=horizons)
    rep["random_time_baseline_reading"] = (
        "고정 창 칸의 **눈금**이다. 무작위 막대의 '이 막대가 최고가' 비율과 앵커의 값을 "
        "비교해야 앵커가 실제로 고점을 고르는지 알 수 있다. **이것은 수익률 위약이 "
        "아니다** — 진입 후 수익률 위약은 docs/44 §3-3 이 이미 냈다.")

    # ---- 세 숫자 나란히 ----------------------------------------------------- #
    # 고정 창의 대표값은 **에피소드식 창의 최대 길이와 같은 것**을 쓴다 — 그래야
    # 두 칸의 차이가 "창이 짧아서" 가 아니라 "창이 어디에 붙어 있어서" 가 된다.
    hmax = max_seconds if max_seconds in horizons else horizons[-1]
    s1e = grid["stage1_cross"]["episode_style_window"]
    cfe = grid["causal_fire"]["episode_style_window"]
    s1f = grid["stage1_cross"]["fixed_window"][f"{hmax}s"]
    cff = grid["causal_fire"]["fixed_window"][f"{hmax}s"]
    rep["three_numbers"] = {
        "stage1_retrospective_episode_rule": {
            "n": s1e.get("n"),
            "cross_is_peak_share": s1e.get("share_anchor_is_max_incl_empty"),
            "what_it_is": "docs/44 §2-3 이 실은 값. 앵커=사후적 저점 기준 교차, 창=에피소드",
        },
        "causal_detector_same_geometry": {
            "n": cfe.get("n"),
            "cross_is_peak_share": cfe.get("share_anchor_is_max_incl_empty"),
            "what_it_is": ("앵커=find_fires 발화(미래 안 씀), 창=저점+60초 (1단계와 같은 기하). "
                           "1단계와 **탐지기만** 다르다"),
        },
        "fixed_clock_window": {
            "stage1_cross_n": s1f.get("n"),
            "stage1_cross_share": s1f.get("share_anchor_is_max_incl_empty"),
            "causal_fire_n": cff.get("n"),
            "causal_fire_share": cff.get("share_anchor_is_max_incl_empty"),
            "what_it_is": (f"앵커는 그대로, 창만 **고정 {hmax}초**. "
                           "에피소드 끝을 아예 안 쓴다"),
        },
    }

    # ---- 코디네이터 대조점(고정 30초, '더 올랐나') ---------------------------- #
    h30 = 30 if 30 in horizons else horizons[-1]
    rep["coordinator_control_comparison"] = {
        "their_method": ("초막대=그 초 체결가 평균, 사건=직전 60초 최저×1.01 을 처음 넘은 초, "
                         "고정 30초 안에 더 올랐는가. 결과 85% (n=1,332, 12종목)"),
        "our_nearest_equivalent": {
            "anchor": "find_fires (같은 규칙: 직전 60초 최저 × 1.01, 상승 에지)",
            "horizon_s": h30,
            "n": grid["causal_fire"]["fixed_window"][f"{h30}s"].get("n"),
            "share_higher_later_incl_empty":
                grid["causal_fire"]["fixed_window"][f"{h30}s"].get(
                    "share_higher_later_incl_empty"),
            "share_higher_later_excl_empty":
                grid["causal_fire"]["fixed_window"][f"{h30}s"].get(
                    "share_higher_later_excl_empty"),
        },
        "known_differences": [
            f"종목: 우리 {len(bars)} vs 그쪽 12 (체결 상위)",
            "가격열: 우리 vwap vs 그쪽 단순 평균",
            "발화 후 60초 잠금이 우리 쪽에만 있다",
            "그쪽은 슈팅 필터가 없다 — 그냥 오르는 구간도 포함",
        ],
        "sensitivity": sensitivity_vs_coordinator(
            bars, syms, rise=rise, max_seconds=max_seconds),
    }

    # ---- 3번 물음 ----------------------------------------------------------- #
    rep["boundary_audit"] = boundary_audit(anch, max_seconds=max_seconds,
                                           horizons=horizons)

    # ---- 갈라 보기: 가격대 · 세션 (호가 눈금 효과가 섞였는지) ------------------- #
    splits: dict = {}
    for kind in ("stage1_cross", "causal_fire"):
        a = anch[kind]
        if a["n"] == 0:
            continue
        is_peak_ep = ~(a["episode_window"]["max_ret"] > 0)
        mx60 = a["fixed"][hmax]["max_ret"]
        higher60 = np.zeros(a["n"], dtype=bool)
        ok = np.isfinite(mx60)
        higher60[ok] = mx60[ok] > 0
        band = price_band_of(a["anchor_usd"])
        sess = session_of(a["anchor_ms"])
        splits[kind] = {
            "by_price_band": {
                str(b): {"n": int((band == b).sum()),
                         "episode_rule_is_peak": float(is_peak_ep[band == b].mean()),
                         "fixed_60s_is_peak": float((~higher60[band == b]).mean())}
                for b in ("under_1", "1_to_3", "3_to_10", "over_10")
                if (band == b).any()},
            "by_session": {
                str(x): {"n": int((sess == x).sum()),
                         "episode_rule_is_peak": float(is_peak_ep[sess == x].mean()),
                         "fixed_60s_is_peak": float((~higher60[sess == x]).mean())}
                for x in ("pre", "regular", "after", "overnight") if (sess == x).any()},
        }
    rep["splits"] = splits
    rep["limits"] = [
        "2.6 정규장뿐이다 — CI 없음, 방향만.",
        "초 미만은 원리상 못 본다(docs/41 §2-1).",
        "포화 칸(테이프 끊김)을 여기서 따로 안 갈랐다 — 끊긴 구간에서는 "
        "'더 높은 막대' 가 우리 기록에 없어 **'앵커가 정점' 쪽으로 치우친다.**",
        "고정 창의 빈 창(체결 없음)은 두 규약으로 다 실었다 — 하나만 읽으면 안 된다.",
        "이 문서는 **판정하지 않는다.** 어떤 설계도 살리거나 죽이지 않는다.",
    ]
    return rep


def _f(v, nd=4):
    return "n/a" if v is None else f"{v:.{nd}f}"


def main(db: Path, *, out_dir: Path | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    conn = open_ro(db)
    print("=== cross-vs-peak check (read-only, no live calls)")
    rep = build_report(conn)

    ref = rep["stage1_episode_reference"]
    print(f"\n=== stage-1 reference: {ref['n_shots']} shots, "
          f"cross_is_peak (published rule) = "
          f"{_f(ref['cross_is_peak_share_published_rule'], 4)}")

    print("\n=== 2x2: ANCHOR x WINDOW  (share where the anchor bar IS the max)")
    hs = rep["conditions"]["horizons_s"]
    print(f"  {'anchor':<16}{'n':>7}{'episode':>10}" +
          "".join(f"{'fix' + str(h) + 's':>10}" for h in hs))
    for kind in ("stage1_cross", "causal_fire"):
        g = rep["anchor_by_window_grid"][kind]
        row = f"  {kind:<16}{g['n']:>7}" \
              f"{_f(g['episode_style_window'].get('share_anchor_is_max_incl_empty')):>10}"
        for h in hs:
            row += (f"{_f(g['fixed_window'][f'{h}s'].get('share_anchor_is_max_incl_empty')):>10}")
        print(row)
    print("  (empty windows counted as 'anchor is max' — the stage-1 convention)")
    print("  same rows, empty windows DROPPED instead (fixed windows only):")
    for kind in ("stage1_cross", "causal_fire"):
        g = rep["anchor_by_window_grid"][kind]
        row = f"  {kind:<16}{'':>7}{'':>10}"
        for h in hs:
            row += f"{_f(g['fixed_window'][f'{h}s'].get('share_anchor_is_max_excl_empty')):>10}"
        print(row)

    rb = rep["random_time_baseline"]
    print(f"  {'BASELINE random bar':<16}{rb['n_draws']:>7}{'':>10}" +
          "".join(f"{_f(rb[f'fixed_{h}s'].get('share_anchor_is_max_incl_empty')):>10}"
                  for h in hs))
    print("  ^ a random bar being the max of the next H seconds — the ruler for the "
          "fixed columns")

    print("\n=== fixed-clock: how much is left after the anchor (means)")
    for kind in ("stage1_cross", "causal_fire"):
        g = rep["anchor_by_window_grid"][kind]
        print(f"  {kind}")
        for h in hs:
            v = g["fixed_window"][f"{h}s"]
            print(f"    {h:>3}s  n {v.get('n'):>6}  empty {_f(v.get('share_empty_window'),3)}"
                  f"  max_ret mean {_f(v.get('max_ret',{}).get('mean'),5)}"
                  f"  p50 {_f(v.get('max_ret',{}).get('p50'),5)}"
                  f"  |  end_ret mean {_f(v.get('end_ret',{}).get('mean'),5)}"
                  f"  >0 {_f(v.get('end_ret_share_positive'),3)}")

    ba = rep["boundary_audit"]
    print("\n=== boundary audit — what ends a stage-1 episode")
    print(f"  time to cross s: p50 {_f(ba['time_to_cross_s'].get('p50'),0)} "
          f"p75 {_f(ba['time_to_cross_s'].get('p75'),0)} "
          f"p90 {_f(ba['time_to_cross_s'].get('p90'),0)}")
    print(f"  remaining window after cross s: p10 "
          f"{_f(ba['remaining_window_after_cross_s'].get('p10'),0)} "
          f"p25 {_f(ba['remaining_window_after_cross_s'].get('p25'),0)} "
          f"p50 {_f(ba['remaining_window_after_cross_s'].get('p50'),0)}")
    rs = ba["remaining_window_shares"]
    print(f"  share with <=2s left: {_f(rs['le_2s'],3)}   <=5s: {_f(rs['le_5s'],3)}"
          f"   <=10s: {_f(rs['le_10s'],3)}")
    dc = ba["duration_at_ceiling"]
    print(f"  duration pinned at the {60}s ceiling: "
          f"{_f(dc['share_exactly_max_seconds'],3)}  (>=59s {_f(dc['share_ge_59s'],3)})")
    print("  cross_is_peak by remaining window:")
    for k, v in ba["cross_is_peak_by_remaining_window"].items():
        print(f"    {k:<12} n {v['n']:>6} ({v['share_of_all']:.1%})  "
              f"cross_is_peak {_f(v['cross_is_peak_share'],3)}")
    print("  flipped by a fixed window (was 'peak' under the episode rule):")
    for k, v in ba["flipped_by_fixed_window"].items():
        print(f"    {k:<12} flipped {v['n_flipped']:>6}  "
              f"= {_f(v['share_of_cross_is_peak'],3)} of the cross_is_peak set")

    cc = rep["coordinator_control_comparison"]["our_nearest_equivalent"]
    print(f"\n=== coordinator control (fixed {cc['horizon_s']}s, 'did it go higher')")
    print(f"  ours: n {cc['n']}  higher incl.empty "
          f"{_f(cc['share_higher_later_incl_empty'],3)}  excl.empty "
          f"{_f(cc['share_higher_later_excl_empty'],3)}   (theirs: 0.85, n=1332)")
    print("  sensitivity — which of the known differences moves it:")
    sens = rep["coordinator_control_comparison"]["sensitivity"]
    print(f"    {'variant':<34}{'syms':>6}{'fires':>7}{'h30 incl':>10}{'h30 excl':>10}"
          f"{'h60 excl':>10}")
    for tag, v in sens.items():
        if not isinstance(v, dict):
            continue
        print(f"    {tag:<34}{v['n_symbols']:>6}{v['n_fires']:>7}"
              f"{_f(v['fixed_30s'].get('share_higher_later_incl_empty'),3):>10}"
              f"{_f(v['fixed_30s'].get('share_higher_later_excl_empty'),3):>10}"
              f"{_f(v['fixed_60s'].get('share_higher_later_excl_empty'),3):>10}")

    out = out_dir or OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    path = out / "cross_peak_check.json"
    path.write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    db = Path(args[0]) if args else Path(
        r"C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db")
    sys.exit(main(db))

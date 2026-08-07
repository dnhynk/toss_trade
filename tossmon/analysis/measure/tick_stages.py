"""**1~3단계 — 초 막대 위의 슈팅 해부 · 탐지 후 잔여 · 감시 폭** (docs/44).

0단계(`tick_resolution`, docs/41)가 자를 쟀다. 이 모듈은 그 자 위에서 **현상을 본다.**

## 이 모듈이 서 있는 전제 (전부 0단계 실측)

- **초 막대만 쓴다.** `ts_ms` 가 전부 `.000` 이고 초 안의 순서는 저장 순서(가격 오름차순)라
  복구 불가다(docs/41 §2-1, docs/42). 그래서 `second_bars` 로만 내려간다.
- **탐지 지연 2.56초**를 진입에 넣는다(docs/41 §3-3). "이론상 탐지 시점" 은 쓰지 않는다.
- **포화 칸을 표시하고 센다.** 테이프의 27.3%가 50건 상한이 물린 칸에 있고, 상한은
  **바쁠 때** 물린다(docs/41 §4). 조용히 섞으면 슈팅 구간이 체계적으로 왜곡된다.
- `tick_classify` · `window_frame` · `find_shot_starts` 는 **오염된 경로다**(docs/42 §5).
  여기서는 쓰지 않고, 고치지도 않는다 — **초 막대 구현으로 우회한다.**

## 여기서 판정하지 않는다

**닫힌 후보를 살리지도 죽이지도 않는다.** 표본은 **2.6 정규장**뿐이고(docs/41 §1-2)
사이클 군집 하한(5)에 못 미친다. 이 문서가 낼 수 있는 것은
**"초 막대·2.6 정규장 조건에서 이렇게 보인다"** 까지다.

실행: `python -m tossmon.analysis.measure.tick_stages [db_path]` → `out/tick_stages.json`.
**라이브 콜 0. DB 는 `mode=ro`.**
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

from tossmon.analysis.measure.tick_resolution import (
    SESSION_BANDS,
    TRADES_COUNT_CAP,
    WINDOW_START_MS,
    open_ro,
    pct_table,
    second_bars,
    session_of,
)

# --------------------------------------------------------------------------- #
# 측정 조건 — 모든 표에 이것이 붙는다
# --------------------------------------------------------------------------- #

#: 탐지 지연(체결 스트림) — docs/41 §3-3. 폴 대기 p50 2.06초 + 파이프라인 하한 0.50초.
#: **1초 격자 위에서는 사실상 +3초로 올림된다** (진입 가능한 첫 막대가 그것뿐이다).
DETECT_LAG_S = 2.56
#: 최악 지연 — 폴 대기 4.11초 + 0.50초. 격자 위에서는 +5초.
DETECT_LAG_WORST_S = 4.61

#: 슈팅 정의의 기본값. `docs/29`·docs/41 §6 과 **같은 규칙**이라야 비교가 된다.
SHOT_RISE = 0.01
SHOT_MAX_SECONDS = 60

#: ★ 임계 쓸이. 답이 임계를 1:1 로 따라가면 그건 시장이 아니라 **우리 정의**다(감사5 min_rise).
THRESHOLD_SWEEP = (0.005, 0.01, 0.015, 0.02, 0.03)

#: 진입 후 성과를 보는 지평(초). 슈팅 p50 이 32초라 그 앞뒤를 덮는다.
HORIZONS_S = (5, 10, 20, 30, 60, 120)

#: 지평 가격을 읽을 때 허용하는 최대 묵힘(초). 이보다 오래 체결이 없으면 **모른다**로 둔다.
STALE_CAP_S = 30

#: 가격대 구분(USD). $1 미만이 체결의 19.4%고 거기서는 1% 가 호가 한두 칸이다(docs/41 §4-3).
PRICE_BANDS: tuple[tuple[str, float, float], ...] = (
    ("under_1", 0.0, 1.0),
    ("1_to_3", 1.0, 3.0),
    ("3_to_10", 3.0, 10.0),
    ("over_10", 10.0, float("inf")),
)

#: 1~2단계 종목 선정. docs/41 §6 과 같은 값이라야 그 표와 같은 모집단이다.
MIN_TRADES_PER_SYMBOL = 500

#: tier3 소속 판정 — 이 반폭(초) 안의 호가 폴 수가 `TIER3_MIN_POLLS` 이상이면 tier3.
#: 실측 근거: (종목, 60초 칸) 별 호가 행수가 **1 근처(tier2, 600초 주기)와 14~15(tier3,
#: 4초 주기)로 갈린다.** 중간값은 진입·이탈 전이 구간이다.
TIER3_HALF_WINDOW_S = 30
TIER3_MIN_POLLS = 3

#: 위약 대조군 추첨 수 (실제 슈팅 1건당). 늘리면 위약 쪽 잡음만 준다.
PLACEBO_DRAWS = 3
#: 무작위 **종목** 위약에서 "그때 그 종목도 거래 중이었나" 를 판정하는 허용 간격(초).
#: 이보다 멀면 버린다 — 안 버리면 몇 시간 뒤 막대로 진입해 위약의 뜻이 바뀐다.
PLACEBO_NEAR_S = 60
SEED = 20260808

#: 랭킹 가격 스트림 — 두 실시간 목록의 **합집합**을 쓴다. 목록마다 폴 시각이 달라
#: 합치면 종목당 관측이 촘촘해진다.
RANKING_REALTIME_TYPES = ("TOSS_SECURITIES_TRADING_VOLUME", "MARKET_TRADING_VOLUME")

OUT_DIR = Path("out")


# --------------------------------------------------------------------------- #
# 공통 — 창 최대치와 구간 최대 (초 막대는 1초 격자라 창 안 막대 수가 유계다)
# --------------------------------------------------------------------------- #
def forward_window(ts: np.ndarray, max_seconds: int) -> tuple[np.ndarray, int]:
    """각 막대 `i` 에서 앞으로 `max_seconds` 안에 들어오는 막대의 **끝 인덱스(배타)**.

    초 막대는 1초 격자라 창 안 막대 수가 `max_seconds + 1` 을 넘을 수 없다. 이 유계성이
    아래 행렬 표현을 가능하게 한다 — 창이 무한정 길어질 걱정이 없다.
    """
    end = np.searchsorted(ts, ts + max_seconds * 1000, side="right")
    k = int((end - np.arange(ts.size)).max()) if ts.size else 1
    return end, max(k, 1)


def window_matrix(ts: np.ndarray, px: np.ndarray, end: np.ndarray, k: int) -> np.ndarray:
    """`M[d, i]` = 막대 `i+d` 의 가격 (단, 창 밖이면 `-inf`).

    창 안 최대·그 위치를 **벡터로** 구하려고 만든다. 임계를 쓸어도 이 행렬은 그대로
    재사용된다 — 다시 만드는 것은 임계에 딸린 비교뿐이다.
    """
    n = ts.size
    m = np.full((k, n), -np.inf, dtype="float64")
    idx = np.arange(n)
    for d in range(k):
        j = idx + d
        ok = (j < n) & (j < end)
        m[d, ok] = px[j[ok]]
    return m


# --------------------------------------------------------------------------- #
# 1단계 — 슈팅 에피소드 (docs/29 와 같은 규칙, 초 막대 위)
# --------------------------------------------------------------------------- #
def find_episodes(ts: np.ndarray, anchor: np.ndarray, peak: np.ndarray, *,
                  rise: float, max_seconds: int,
                  mat: np.ndarray | None = None,
                  end: np.ndarray | None = None) -> dict:
    """저점에서 `max_seconds` 안에 `rise` 이상 오르는 **에피소드**를 집는다.

    규칙은 `docs/29` `find_shot_starts` 와 같다(정의를 바꾸면 옛 값과 비교가 안 된다).
    입력만 다르다 — 체결 행이 아니라 **초 막대**다.

    **이것은 현상 기술용이지 실시간 탐지기가 아니다.** 시작점이 "지나고 보니 저점" 이기
    때문이다. 실시간으로 발화 가능한 쪽은 `find_fires` 다 — 2단계는 그쪽을 쓴다.

    돌려주는 것: 에피소드마다 (시작·교차·정점) 막대 인덱스. **교차**는 처음으로
    `rise` 를 넘은 막대이며 0단계에는 없던 항목이다 — 2단계의 탐지 시점이 거기다.
    """
    n = ts.size
    if n < 2:
        return {"start": np.array([], "int64"), "cross": np.array([], "int64"),
                "peak": np.array([], "int64")}
    if end is None or mat is None:
        end, k = forward_window(ts, max_seconds)
        mat = window_matrix(ts, peak, end, k)
    fwd = mat[1:]                                   # 자기 자신은 제외한다
    best = fwd.max(axis=0)
    best_d = fwd.argmax(axis=0) + 1
    target = anchor * (1.0 + rise)
    hit = (anchor > 0) & (best >= target)

    starts, crosses, peaks = [], [], []
    i = 0
    while i < n - 1:
        if not hit[i]:
            i += 1
            continue
        p = i + int(best_d[i])
        # 교차 = 정점까지 가는 길에 처음으로 임계를 넘는 막대
        seg = mat[1:p - i + 1, i]
        over = np.flatnonzero(seg >= target[i])
        c = i + 1 + int(over[0]) if over.size else p
        starts.append(i); crosses.append(c); peaks.append(p)
        i = p                                        # 정점에서 다시 시작 (겹침 방지)
    return {"start": np.asarray(starts, "int64"),
            "cross": np.asarray(crosses, "int64"),
            "peak": np.asarray(peaks, "int64")}


def find_fires(ts: np.ndarray, anchor: np.ndarray, peak: np.ndarray, *,
               rise: float, max_seconds: int, cooldown_s: int) -> np.ndarray:
    """**실시간으로 발화 가능한** 탐지 — 되돌아보는 저점을 쓰지 않는다.

    각 막대 `j` 에서 **직전 `max_seconds` 안의 최저가**를 기준으로 삼고, 현재가가 거기서
    `rise` 이상이면 발화한다. 미래를 안 쓰므로 그대로 구현 가능하다.

    `find_episodes` 와 왜 다른가: 에피소드의 시작점은 **지나고 나서야** 저점인 줄 안다.
    "탐지 후 남은 상승폭" 을 물으려면 탐지가 실제로 가능한 시점이어야 하므로 2단계는
    이쪽을 쓴다. 두 값이 다르면 그 차이 자체가 결과다.

    되울림 방지: **상승 에지에서만** 발화하고, 발화 후 `cooldown_s` 동안 잠근다.
    """
    n = ts.size
    if n < 2:
        return np.array([], "int64")
    start = np.searchsorted(ts, ts - max_seconds * 1000, side="left")
    k = int((np.arange(n) - start + 1).max())
    # 뒤를 보는 최소 — 앞을 보는 최대와 같은 행렬 요령을 부호만 바꿔 쓴다
    idx = np.arange(n)
    m = np.full((k, n), np.inf, dtype="float64")
    for d in range(k):
        j = idx - d
        ok = (j >= 0) & (j >= start)
        m[d, ok] = anchor[j[ok]]
    trail_min = m.min(axis=0)
    live = (trail_min > 0) & (peak / np.maximum(trail_min, 1e-12) - 1.0 >= rise)
    edge = live & ~np.r_[False, live[:-1]]           # 상승 에지에서만
    fires = []
    last = -np.inf
    for j in np.flatnonzero(edge):
        if ts[j] - last < cooldown_s * 1000:
            continue
        fires.append(int(j))
        last = ts[j]
    return np.asarray(fires, "int64")


# --------------------------------------------------------------------------- #
# 보조 자료 — 포화 칸, tier3 소속, 초 막대 캐시
# --------------------------------------------------------------------------- #
def load_saturation(conn: sqlite3.Connection, *, bucket_s: float = 4.0) -> dict:
    """**50건 상한이 물린 칸** — (종목 → 포화한 4초 칸 번호 집합).

    docs/41 §4: 칸 수로는 2.14% 지만 **테이프 분량으로는 27.3%** 다. 그리고 상한은
    바쁠 때 물리므로 **슈팅 구간에 집중**된다. 그래서 이 모듈의 모든 2단계 표는
    포화 포함·제외를 **둘 다** 낸다.
    """
    b = int(bucket_s * 1000)
    out: dict[str, set] = {}
    for sym, bucket in conn.execute(
            "SELECT symbol, ts_ms / ? FROM trades_snap WHERE ts_ms >= ? "
            "GROUP BY symbol, ts_ms / ? HAVING COUNT(*) >= ?",
            (b, WINDOW_START_MS, b, TRADES_COUNT_CAP)):
        out.setdefault(sym, set()).add(int(bucket))
    return out


def load_tier3_watch(conn: sqlite3.Connection) -> dict:
    """(종목 → 호가 폴 시각 배열). **tier3 소속의 대리지표**다.

    왜 호가인가: `/trades` 와 `/orderbook` 은 **같은 tier3 집합**에 돈다. 그런데 체결은
    "그 순간 거래가 있었나" 에 좌우되고 호가는 **거래와 무관하게 4초마다** 찍힌다.
    즉 호가 쪽이 "우리가 보고 있었나" 를 더 곧게 답한다.

    실측 근거(이 창): (종목, 60초 칸) 별 호가 행수가 **1(=tier2 느린 레인)** 과
    **14~15(=tier3 4초 레인)** 로 갈린다. 그래서 ±30초에 3폴 이상이면 tier3 로 읽는다.
    """
    out: dict[str, list] = {}
    for sym, snap in conn.execute(
            "SELECT symbol, snap_ms FROM orderbook_snap WHERE snap_ms >= ? "
            "ORDER BY symbol, snap_ms", (WINDOW_START_MS,)):
        out.setdefault(sym, []).append(snap)
    return {s: np.asarray(v, dtype="int64") for s, v in out.items()}


def watched_at(watch: dict, sym: str, t_ms: np.ndarray, *,
               half_s: int = TIER3_HALF_WINDOW_S,
               min_polls: int = TIER3_MIN_POLLS) -> np.ndarray:
    """그 시각에 **그 종목을 tier3 로 보고 있었는가.** 벡터로 답한다."""
    w = watch.get(sym)
    t = np.atleast_1d(np.asarray(t_ms, dtype="int64"))
    if w is None or w.size == 0:
        return np.zeros(t.shape, dtype=bool)
    lo = np.searchsorted(w, t - half_s * 1000, side="left")
    hi = np.searchsorted(w, t + half_s * 1000, side="right")
    return (hi - lo) >= min_polls


def price_band_of(px_usd: np.ndarray) -> np.ndarray:
    out = np.full(np.shape(px_usd), "over_10", dtype=object)
    for name, lo, hi in PRICE_BANDS:
        out[(px_usd >= lo) & (px_usd < hi)] = name
    return out


def load_bars(conn: sqlite3.Connection, symbols) -> dict:
    """종목별 초 막대 캐시. 임계를 쓸어도 DB 는 한 번만 읽는다."""
    bars = {}
    for s in symbols:
        b = second_bars(conn, s)
        if b is not None and b[0].size >= 2:
            bars[s] = b
    return bars


def select_symbols(conn: sqlite3.Connection, *,
                   min_trades: int = MIN_TRADES_PER_SYMBOL) -> list:
    return [s for s, in conn.execute(
        "SELECT symbol FROM trades_snap WHERE ts_ms >= ? GROUP BY symbol "
        "HAVING COUNT(*) >= ? ORDER BY COUNT(*) DESC", (WINDOW_START_MS, min_trades))]


def px_at(ts: np.ndarray, px: np.ndarray, when_ms: np.ndarray, *,
          stale_cap_s: int = STALE_CAP_S) -> np.ndarray:
    """`when_ms` **이하**의 마지막 막대 가격. 너무 묵었으면 `nan` — 모르는 것은 모른다고 둔다.

    "이하" 인 이유: 그 시점에 우리가 실제로 알고 있던 마지막 값이 그것이기 때문이다.
    앞의 막대를 끌어오면 미래를 쓰게 된다.
    """
    w = np.atleast_1d(np.asarray(when_ms, dtype="int64"))
    i = np.searchsorted(ts, w, side="right") - 1
    out = np.full(w.shape, np.nan)
    ok = i >= 0
    out[ok] = px[i[ok]]
    stale = np.zeros(w.shape, dtype=bool)
    stale[ok] = (w[ok] - ts[i[ok]]) > stale_cap_s * 1000
    out[stale] = np.nan
    return out


# --------------------------------------------------------------------------- #
# 1단계 — 슈팅 해부
# --------------------------------------------------------------------------- #
def stage1_anatomy(conn: sqlite3.Connection, bars: dict, sat: dict, watch: dict, *,
                   rise: float = SHOT_RISE,
                   max_seconds: int = SHOT_MAX_SECONDS) -> dict:
    """**지속시간 · 상승폭 · 도래 간격**을 초 막대 위에서 처음부터 낸다.

    세션별·가격대별로 가른다. **중앙값 하나로 뭉개지 않는다** — 분포 전체를 싣는다.
    """
    dur, tot_rise, t_cross, rise_cross = [], [], [], []
    cross_to_peak, cross_is_peak, rise_frac = [], [], []
    start_ms, anchor_usd, saturated, syms = [], [], [], []
    gaps_clean, gaps_raw = [], []
    watched_s_total = 0.0

    for s, (ts, n, lo, hi, vwap) in bars.items():
        end, k = forward_window(ts, max_seconds)
        mat = window_matrix(ts, vwap, end, k)
        ep = find_episodes(ts, vwap, vwap, rise=rise, max_seconds=max_seconds,
                           mat=mat, end=end)
        si, ci, pi = ep["start"], ep["cross"], ep["peak"]
        if si.size == 0:
            continue
        dur.append((ts[pi] - ts[si]) / 1000.0)
        tot_rise.append(vwap[pi] / vwap[si] - 1.0)
        t_cross.append((ts[ci] - ts[si]) / 1000.0)
        rise_cross.append(vwap[ci] / vwap[si] - 1.0)
        # ★ 임계를 넘은 뒤에 **아직 남아 있는 몫** — 2단계의 상한이다.
        # 종목별로 재서 모은다. 중앙값끼리 나누면 안 된다(종목마다 분포가 달라서다).
        cross_to_peak.append((ts[pi] - ts[ci]) / 1000.0)
        cross_is_peak.append(ci == pi)
        _rt = vwap[pi] / vwap[si] - 1.0
        _rc = vwap[ci] / vwap[si] - 1.0
        _ok = _rt > 0
        rise_frac.append(_rc[_ok] / _rt[_ok])
        start_ms.append(ts[si])
        anchor_usd.append(vwap[si] / 1e6)
        syms.extend([s] * si.size)

        capped = sat.get(s, set())
        if capped:
            flag = np.array([any(b in capped for b in
                                 range(int(ts[a] // 4000), int(ts[b] // 4000) + 1))
                             for a, b in zip(si, pi)])
        else:
            flag = np.zeros(si.size, dtype=bool)
        saturated.append(flag)

        # 도래 간격 — 같은 종목 안 연속 슈팅 시작의 차. **회전으로 끊긴 구간은 갈라낸다.**
        if si.size >= 2:
            g = np.diff(ts[si]) / 1000.0
            gaps_raw.append(g)
            w = watch.get(s)
            if w is not None and w.size:
                keep = []
                for a, b in zip(ts[si][:-1], ts[si][1:]):
                    seg = w[(w >= a) & (w <= b)]
                    # 사이 내내 4초 레인이 살아 있었으면 tier3 를 안 떠난 것이다
                    keep.append(bool(seg.size >= 2 and
                                     np.diff(np.r_[a, seg, b]).max() <= 60_000))
                gaps_clean.append(g[np.asarray(keep, dtype=bool)])
        w = watch.get(s)
        if w is not None and w.size >= 2:
            d = np.diff(w)
            watched_s_total += float(d[d <= 60_000].sum()) / 1000.0

    if not dur:
        return {"n_shots": 0}
    dur = np.concatenate(dur); tot_rise = np.concatenate(tot_rise)
    t_cross = np.concatenate(t_cross); rise_cross = np.concatenate(rise_cross)
    start_ms = np.concatenate(start_ms); anchor_usd = np.concatenate(anchor_usd)
    saturated = np.concatenate(saturated)
    sess = session_of(start_ms)
    band = price_band_of(anchor_usd)
    graw = np.concatenate(gaps_raw) if gaps_raw else np.array([])
    gcln = np.concatenate(gaps_clean) if gaps_clean else np.array([])

    def split(key_arr, keys):
        return {k: {"n": int((key_arr == k).sum()),
                    "duration_s": pct_table(dur[key_arr == k]),
                    "total_rise": pct_table(tot_rise[key_arr == k]),
                    "time_to_cross_s": pct_table(t_cross[key_arr == k])}
                for k in keys}

    return {
        "conditions": {
            "rise": rise, "max_seconds": max_seconds,
            "price_series": "초 막대 vwap (초 안 순서는 쓰지 않는다)",
            "symbols": len(bars), "min_trades_per_symbol": MIN_TRADES_PER_SYMBOL,
            "definition": "docs/29 find_shot_starts 와 같은 규칙, 입력만 초 막대",
            "resolution_s": 1.0,
        },
        "n_shots": int(dur.size),
        "duration_s": pct_table(dur),
        "total_rise": pct_table(tot_rise),
        "time_to_cross_s": pct_table(t_cross),
        "rise_at_cross": pct_table(rise_cross),
        "after_cross": {
            "cross_to_peak_s": pct_table(np.concatenate(cross_to_peak)),
            "cross_is_peak_share": float(np.concatenate(cross_is_peak).mean()),
            "rise_fraction_done_at_cross": pct_table(np.concatenate(rise_frac)),
            "reading": ("임계를 처음 넘은 막대가 **그대로 정점인** 비율이 "
                        "`cross_is_peak_share` 다. 그 경우 탐지 시점에 남은 상승은 "
                        "0 이다 — 지연을 0 으로 만들어도 그렇다. 이건 시장의 성질이 "
                        "아니라 **'+1% 를 넘으면 슈팅'이라는 정의의 성질**이다."),
        },
        "duration_shares": {f"le_{t}s": float((dur <= t).mean())
                            for t in (1, 2, 4, 8, 16, 30, 45, 59)},
        "by_session": split(sess, ("pre", "regular", "after", "overnight")),
        "by_price_band": split(band, tuple(b[0] for b in PRICE_BANDS)),
        "saturation": {
            "shots_touching_capped_bucket": int(saturated.sum()),
            "share": float(saturated.mean()),
            "duration_s_saturated": pct_table(dur[saturated]),
            "duration_s_clean": pct_table(dur[~saturated]),
            "total_rise_saturated": pct_table(tot_rise[saturated]),
            "total_rise_clean": pct_table(tot_rise[~saturated]),
            "note": ("포화 칸을 지나는 슈팅은 테이프가 끊긴 구간을 포함한다 — "
                     "그 구간의 고가·저가가 우리 기록에 없으므로 지속시간·상승폭 모두 "
                     "**과소**로 잡힐 수 있다."),
        },
        "inter_arrival_s": {
            "raw": pct_table(graw),
            "continuous_watch_only": pct_table(gcln),
            "dropped_by_rotation": int(graw.size - gcln.size),
            "note": ("같은 종목 안 연속 슈팅 시작의 차. **raw 는 tier3 회전으로 부풀려진다** "
                     "— 종목이 정원에서 빠졌다 돌아오면 그 사이가 통째로 간격이 된다. "
                     "`continuous_watch_only` 는 사이 내내 4초 레인이 살아 있던 간격만."),
        },
        "arrival_rate": {
            "watched_symbol_hours": round(watched_s_total / 3600.0, 1),
            "shots_per_watched_symbol_hour": (round(dur.size / (watched_s_total / 3600.0), 2)
                                              if watched_s_total > 0 else None),
            "note": "감시 시간은 호가 4초 레인이 살아 있던 시간의 합(종목·시간).",
        },
    }


# --------------------------------------------------------------------------- #
# 2단계 — ★ 탐지 후 남은 상승폭
# --------------------------------------------------------------------------- #
def _entry_outcomes(ts: np.ndarray, vwap: np.ndarray, fire_i: np.ndarray, *,
                    lag_s: float, horizons=HORIZONS_S) -> dict:
    """발화 → **지연을 먹고** 진입 → 각 지평의 성과. 미래를 쓰지 않는다."""
    fire_ms = ts[fire_i]
    want = fire_ms + int(round(lag_s * 1000))
    ei = np.searchsorted(ts, want, side="left")      # 지연 이후 **처음 잡히는** 막대
    ok = ei < ts.size
    # `entered` 를 돌려주는 이유: 진입 못 한 발화가 있으면 결과 배열이 발화 배열보다
    # 짧아진다. 포화 여부 같은 발화측 표식과 짝을 맞추려면 이 마스크가 있어야 한다.
    out = {"n": int(fire_i.size), "n_entered": int(ok.sum()), "entered": ok}
    if not ok.any():
        return out
    ei = ei[ok]; fi = fire_i[ok]
    entry_px = vwap[ei]
    out["entry_delay_actual_s"] = (ts[ei] - fire_ms[ok]) / 1000.0
    out["slip_from_fire"] = entry_px / vwap[fi] - 1.0     # 지연 동안 이미 달아난 몫
    for h in horizons:
        p = px_at(ts, vwap, ts[ei] + h * 1000)
        out[f"ret_{h}s"] = p / entry_px - 1.0
    # 진입 후 60초 안의 최대·최소 — 무엇이 **가능했는지**의 상한/하한
    end, k = forward_window(ts, 60)
    fwd_hi = window_matrix(ts, vwap, end, k)[:, ei].max(axis=0)
    fwd_lo = (-window_matrix(ts, -vwap, end, k)[:, ei]).min(axis=0)
    out["mfe_60s"] = fwd_hi / entry_px - 1.0
    out["mae_60s"] = fwd_lo / entry_px - 1.0
    return out


def stage2_remaining(conn: sqlite3.Connection, bars: dict, sat: dict, *,
                     rise: float = SHOT_RISE,
                     max_seconds: int = SHOT_MAX_SECONDS,
                     lag_s: float = DETECT_LAG_S,
                     rng: np.random.Generator | None = None,
                     with_placebo: bool = True) -> dict:
    """**탐지가 실제로 가능한 시점 이후에 무엇이 남는가.**

    1분봉에서는 정확히 0.0000% 였다. 그때는 슈팅을 6초로 알고 있었고 실제는 32초다.
    지연 2.56초는 32초의 8%다 — **남아 있을 수 있다.** 여기서 그것을 잰다.

    탐지는 `find_fires`(되돌아보지 않는 쪽)로 한다. 진입은 발화 + `lag_s` 이후
    **처음 잡히는 막대**다. 1초 격자라 2.56초는 사실상 +3초가 된다.

    위약 둘을 같은 기계로 통과시킨다:
      - **무작위 시각** — 같은 종목, 아무 막대. "이 종목이 그냥 오르고 있었나" 를 가른다.
      - **무작위 종목** — 같은 시각, 그때 감시 중이던 다른 종목. "그 순간 시장 전체가
        움직였나" 를 가른다.
    """
    rng = rng or np.random.default_rng(SEED)
    real: dict[str, list] = {}
    pl_time: dict[str, list] = {}
    pl_sym: dict[str, list] = {}
    fire_ms_all, fire_sym, fire_sat = [], [], []
    n_fires = 0
    n_pl_sym_dropped = 0

    def stash(bag, res, keys):
        for kk in keys:
            if kk in res:
                bag.setdefault(kk, []).append(np.atleast_1d(res[kk]))

    keys = (["slip_from_fire", "mfe_60s", "mae_60s", "entry_delay_actual_s"]
            + [f"ret_{h}s" for h in HORIZONS_S])
    syms = list(bars)

    for s in syms:
        ts, _n, lo, hi, vwap = bars[s]
        fires = find_fires(ts, vwap, vwap, rise=rise, max_seconds=max_seconds,
                           cooldown_s=max_seconds)
        if fires.size == 0:
            continue
        n_fires += int(fires.size)
        res = _entry_outcomes(ts, vwap, fires, lag_s=lag_s)
        stash(real, res, keys)
        entered = res["entered"]
        fire_ms_all.append(ts[fires])
        fire_sym.extend([s] * fires.size)
        capped = sat.get(s, set())
        flag = (np.array([int(t // 4000) in capped for t in ts[fires]])
                if capped else np.zeros(fires.size, dtype=bool))
        fire_sat.append(flag[entered])                # 결과 배열과 같은 길이로 맞춘다
        if not with_placebo:
            continue
        # 위약 1 — 같은 종목, 무작위 시각
        draw = rng.integers(0, ts.size, size=fires.size * PLACEBO_DRAWS)
        stash(pl_time, _entry_outcomes(ts, vwap, draw, lag_s=lag_s), keys)

    if with_placebo and fire_ms_all:
        # 위약 2 — **같은 시각**, 그때 실제로 거래되고 있던 **다른** 종목.
        # 그 시각 근처에 막대가 없는 종목은 버린다. 안 버리면 몇 시간 뒤 막대로 진입하게
        # 되어 "같은 순간의 다른 종목" 이 아니라 그냥 무작위 시각 위약이 되어 버린다.
        all_ms = np.concatenate(fire_ms_all)
        all_sym = np.asarray(fire_sym, dtype=object)
        for _ in range(PLACEBO_DRAWS):
            pick = rng.integers(0, len(syms), size=all_ms.size)
            for k, s in enumerate(syms):
                sel = (pick == k) & (all_sym != s)
                if not sel.any():
                    continue
                ts, _n, lo, hi, vwap = bars[s]
                want = all_ms[sel]
                j = np.searchsorted(ts, want, side="left")
                good = j < ts.size
                good[good] &= (ts[j[good]] - want[good]) <= PLACEBO_NEAR_S * 1000
                n_pl_sym_dropped += int((~good).sum())
                if not good.any():
                    continue
                stash(pl_sym, _entry_outcomes(ts, vwap, j[good], lag_s=lag_s), keys)

    def summarize(bag):
        """중앙값만으로는 못 읽는다 — **1초 격자 위에서 수익률 중앙값은 0으로 눌린다**
        (가격이 이산이고 30초 안에 몇 칸 안 움직인다). 그래서 평균과 **양(+)의 비율**을
        같이 낸다. 셋을 나란히 놓아야 0 이 '움직임 없음'인지 '반반'인지 갈린다."""
        if not bag:
            return {"n": 0}
        out = {"n": int(np.concatenate(bag[keys[0]]).size)}
        for kk, v in bag.items():
            a = np.concatenate(v)
            out[kk] = pct_table(a)
            f = a[np.isfinite(a)]
            out[kk]["share_positive"] = float((f > 0).mean()) if f.size else None
            out[kk]["share_zero"] = float((f == 0).mean()) if f.size else None
        return out

    sat_flag = np.concatenate(fire_sat) if fire_sat else np.array([], dtype=bool)
    result = {
        "conditions": {
            "rise": rise, "max_seconds": max_seconds,
            "detector": ("find_fires — 직전 60초 최저가 대비. **미래를 쓰지 않는다.** "
                         "상승 에지 발화 + 60초 잠금."),
            "detect_lag_s": lag_s,
            "entry_rule": (f"발화 + {lag_s}초 **이후 처음 잡히는 초 막대**. "
                           "1초 격자라 실제 진입 지연은 3초 근처가 된다."),
            "price_series": "초 막대 vwap. **호가 스프레드·체결 비용은 넣지 않았다.**",
            "symbols": len(bars),
        },
        "n_fires": n_fires,
        "real": summarize(real),
    }
    if with_placebo:
        result["placebo_random_time"] = summarize(pl_time)
        result["placebo_random_symbol"] = summarize(pl_sym)
        result["placebo_definitions"] = {
            "random_time": ("같은 종목, 무작위 막대. '이 종목이 그냥 오르고 있었나' 를 가른다."),
            "random_symbol": (f"**같은 시각**, 그때 {PLACEBO_NEAR_S}초 안에 거래가 있던 "
                              f"다른 종목. '그 순간 시장 전체가 움직였나' 를 가른다."),
            "random_symbol_draws_dropped": n_pl_sym_dropped,
            "random_symbol_drop_reason": ("추첨된 종목이 그 시각에 거래 중이 아니었다 — "
                                          "버리지 않으면 무작위 시각 위약으로 변질된다"),
        }
    if sat_flag.size and sat_flag.any() and real:
        sub = {}
        for tag, m in (("saturated", sat_flag), ("clean", ~sat_flag)):
            if not m.any():
                continue
            sub[tag] = {"n": int(m.sum())}
            for kk in keys:
                if kk in real:
                    sub[tag][kk] = pct_table(np.concatenate(real[kk])[m])
        result["by_saturation"] = sub
    if real and "entry_delay_actual_s" in real:
        # ★ 지연 2.56초보다 더 센 제약: **다음 체결이 아예 안 온다.**
        # 발화 직후 테이프가 조용해지면 진입 자체가 몇십 초 뒤로 밀린다. 그 표본을
        # 섞어 두면 "탐지 후 잔여" 가 아니라 "한참 뒤 잔여" 를 재게 된다.
        delay = np.concatenate(real["entry_delay_actual_s"])
        sub = {}
        for tag, m in (("prompt_le_5s", delay <= 5.0),
                       ("late_gt_5s", delay > 5.0)):
            if not m.any():
                continue
            sub[tag] = {"n": int(m.sum()), "share": float(m.mean())}
            for kk in keys:
                if kk in real:
                    sub[tag][kk] = pct_table(np.concatenate(real[kk])[m])
        result["by_entry_promptness"] = sub
        result["by_entry_promptness_note"] = (
            "발화 후 5초 안에 진입 가능했던 것과 아닌 것을 가른다. 늦은 쪽은 "
            "**탐지 지연 때문이 아니라 체결이 없어서** 늦은 것이다 — 다른 현상이다.")
        result["by_saturation_note"] = (
            "발화 순간의 4초 칸이 50건 상한에 물렸는가로 가른다. 포화 칸에서는 "
            "테이프가 끊겨 **진입 후 가격이 실제보다 평평하게** 보일 수 있다.")
    return result


def stage2_threshold_sweep(conn: sqlite3.Connection, bars: dict, sat: dict, *,
                           rng: np.random.Generator | None = None) -> dict:
    """★ **답이 임계를 따라가는가.** 따라가면 그건 시장이 아니라 우리 정의다."""
    rng = rng or np.random.default_rng(SEED)
    out = {}
    for r in THRESHOLD_SWEEP:
        res = stage2_remaining(conn, bars, sat, rise=r, rng=rng, with_placebo=False)
        real = res.get("real", {})
        out[f"{r:.3%}"] = {
            "n_fires": res.get("n_fires", 0),
            "n_entered": real.get("n", 0),
            **{f"{k}_{stat}": real.get(k, {}).get(stat)
               for k in ["slip_from_fire", "mfe_60s", "mae_60s"]
                        + [f"ret_{h}s" for h in HORIZONS_S]
               for stat in ("p50", "mean", "share_positive")},
        }
    return out


# --------------------------------------------------------------------------- #
# 3단계 — 그 순간 우리가 보고 있었는가 / 어느 스트림으로 몇 %를 볼 수 있었는가
# --------------------------------------------------------------------------- #
def load_ranking_series(conn: sqlite3.Connection) -> dict:
    """랭킹 **가격 필드**의 종목별 시계열. 두 실시간 목록의 **합집합**.

    docs/41 §7-1 실측: 순위열은 16.1초 늙었지만 **가격 필드는 약 1초**다
    (일치율 0.579, 위약 0.013, n=17,338). 즉 우리는 이미 **넓고 꽤 신선한** 가격
    스트림을 받고 있으면서 안 쓰고 있었다. 이 함수가 그것을 꺼낸다.
    """
    q = ("SELECT symbol, snap_ms, last_u FROM rankings_snap "
         "WHERE snap_ms >= ? AND ranking_type IN (?, ?) ORDER BY symbol, snap_ms")
    rows = conn.execute(q, (WINDOW_START_MS, *RANKING_REALTIME_TYPES)).fetchall()
    out = {}
    cur, cts, cpx = None, [], []
    def flush():
        if cur is None or len(cts) < 2:
            return
        t = np.asarray(cts, dtype="int64"); p = np.asarray(cpx, dtype="float64")
        t, idx = np.unique(t, return_index=True)      # 같은 ms 는 하나로
        out[cur] = (t, p[idx])
    for sym, snap, last in rows:
        if sym != cur:
            flush(); cur, cts, cpx = sym, [], []
        cts.append(snap); cpx.append(last)
    flush()
    return out


def ranking_stream_shape(conn: sqlite3.Connection, rk: dict) -> dict:
    """랭킹 스트림 자체의 성질 — **커버리지 · 갱신 간격 · 무엇을 못 보는가.**"""
    concurrent = np.asarray([r[0] for r in conn.execute(
        "SELECT COUNT(DISTINCT symbol) FROM rankings_snap WHERE snap_ms >= ? "
        "AND duration = 'realtime' GROUP BY snap_ms / 15000", (WINDOW_START_MS,))],
        dtype="int64")
    gaps = [np.diff(t) / 1000.0 for t, _ in rk.values() if t.size > 1]
    g = np.concatenate(gaps) if gaps else np.array([])
    # 두 목록에 **동시에** 있는 종목은 실효 간격이 절반이 된다 — 폴 시각이 어긋나 있어서다.
    both = {s for s, in conn.execute(
        "SELECT symbol FROM rankings_snap WHERE snap_ms >= ? AND duration = 'realtime' "
        "GROUP BY symbol HAVING COUNT(DISTINCT ranking_type) = 2", (WINDOW_START_MS,))}
    g_both = [np.diff(t) / 1000.0 for s, (t, _p) in rk.items()
              if s in both and t.size > 1]
    g_one = [np.diff(t) / 1000.0 for s, (t, _p) in rk.items()
             if s not in both and t.size > 1]
    # ★ 체결량을 복원할 수 있는가 — vol_qu 가 누적이면 차분이 구간 거래량이 된다
    vol_dirs = []
    for s, in conn.execute(
            "SELECT symbol FROM trades_snap WHERE ts_ms >= ? GROUP BY symbol "
            "ORDER BY COUNT(*) DESC LIMIT 20", (WINDOW_START_MS,)):
        v = np.asarray([r[0] for r in conn.execute(
            "SELECT vol_qu FROM rankings_snap WHERE symbol = ? AND snap_ms >= ? "
            "AND ranking_type = ? ORDER BY snap_ms",
            (s, WINDOW_START_MS, RANKING_REALTIME_TYPES[0]))], dtype="int64")
        if v.size > 1:
            vol_dirs.append(np.diff(v))
    vd = np.concatenate(vol_dirs) if vol_dirs else np.array([])
    return {
        "concurrent_symbols_per_15s": pct_table(concurrent),
        "concurrent_note": ("두 실시간 목록은 각각 100종이지만 **겹친다** — 합집합은 "
                            "200 이 아니다. 이 값이 실제 동시 커버리지다."),
        "symbols_in_window": len(rk),
        "update_gap_s": pct_table(g),
        "update_gap_share_le_15s": float((g <= 15).mean()) if g.size else None,
        "update_gap_in_both_lists_s": pct_table(
            np.concatenate(g_both) if g_both else []),
        "update_gap_in_one_list_s": pct_table(
            np.concatenate(g_one) if g_one else []),
        "symbols_in_both_lists": len(both),
        "cadence_note": ("두 목록의 폴 시각이 어긋나 있어, **양쪽에 다 오르는 종목은 "
                         "실효 간격이 절반**이 된다. 슈팅을 볼 해상도가 종목마다 다르다는 뜻."),
        "price_field_age_s": 1.0,
        "price_field_age_source": "docs/41 §7-1 (이 DB 안에서 실측, 위약 대조 통과)",
        "order_field_age_s": 16.1,
        "order_field_age_source": "docs/35 §5-2 (W1 실측, 프리마켓 1회) — 여기서 재현 못 함",
        "cannot_see": {
            "trade_count": "체결 건수 없음 — 랭킹 행에는 가격·순위·거래량 필드만 있다",
            "direction": "매수/매도 구분 없음 — 호가도 없어 방향을 못 만든다",
            "interval_volume": {
                "recoverable": False,
                "vol_qu_diff_negative_share": (float((vd < 0).mean())
                                               if vd.size else None),
                "n_diffs": int(vd.size),
                "why": ("`vol_qu` 가 **누적이 아니다** — 차분의 상당수가 음수다. "
                        "회전하는 창(rolling window) 값이라 차분해도 구간 거래량이 "
                        "안 나온다. 즉 랭킹으로는 **거래량 급증을 직접 못 만든다.**"),
            },
        },
    }


def wide_episodes(rk: dict, *, rise: float, max_seconds: int) -> dict:
    """넓은 스트림(랭킹 가격)의 슈팅을 **한 번만** 찾아 두고 돌려 쓴다.

    3단계·위약·테이프 확인이 모두 같은 목록을 봐야 서로 대조가 된다 —
    각자 다시 찾으면 미세한 차이가 대조를 흐린다.
    """
    out = {}
    for s, (rt, rp) in rk.items():
        if rt.size < 3:
            continue
        ep = find_episodes(rt, rp, rp, rise=rise, max_seconds=max_seconds)
        if ep["start"].size:
            out[s] = ep
    return out


def wide_stream_placebo(rk: dict, *, rise: float, max_seconds: int,
                        seed: int = SEED) -> dict:
    """**넓은 스트림의 슈팅이 진짜인가** — 시간 구조를 부순 대조군.

    랭킹 `last_u` 는 묵은 값이 튈 수 있어, 두 값 사이를 오가기만 해도 가짜 +1% 가 생긴다.
    그래서 종목 안에서 **가격 순서를 무작위로 섞고** 같은 규칙을 돌린다. 시간 구조가
    만든 사건이면 섞으면 사라져야 한다. 안 사라지면 우리가 세는 것은 잡음이다.

    **한 번만 섞어 기준열·정점열에 같이 넣는다.** 따로 섞으면 서로 무관한 두 열을
    비교하게 되어 대조군이 아니라 다른 실험이 된다.
    """
    rng = np.random.default_rng(seed)
    n = 0
    for _s, (rt, rp) in rk.items():
        if rt.size < 3:
            continue
        shuffled = rng.permutation(rp)          # 한 번만 섞는다
        ep = find_episodes(rt, shuffled, shuffled,
                           rise=rise, max_seconds=max_seconds)
        n += int(ep["start"].size)
    return {"n_shots_shuffled": n,
            "method": ("종목 안에서 가격 순서만 무작위로 섞고 같은 규칙을 돌린다 "
                       "(시각은 그대로, 기준열·정점열은 같은 섞기)")}


def coverage_base_rate(watch: dict, rk: dict, eps: dict, *,
                       seed: int = SEED, per_symbol: int = 200) -> dict:
    """★ **커버리지 질문의 진짜 대조군** — 기저율.

    "슈팅의 96%가 tier3 밖" 이라는 말만으로는 아무것도 안 나온다. tier3 는 동시에 10종목
    뿐이고 랭킹은 ~158종목을 덮으므로, **아무 순간이나 찍어도** 대부분은 tier3 밖이다.

    물어야 할 것은 이것이다: **슈팅 순간이 무작위 순간보다 tier3 안일 확률이 높은가.**
      - 높다 → 우리 tier3 선정이 움직이는 종목을 실제로 골라내고 있다(탐지 문제).
      - 같거나 낮다 → 선정이 슈팅과 무관하다(커버리지 문제).

    두 가지 기저율을 낸다. `all_symbols` 는 랭킹에 보인 모든 종목·시각 위의 균등 추첨이고,
    `shot_symbols_only` 는 **슈팅을 낸 적 있는 종목으로 한정**한다 — 종목 선택 효과를
    빼고 **시점**만 묻기 위해서다. 뒤쪽이 더 날카로운 대조다.
    """
    rng = np.random.default_rng(seed)
    hits = {"all_symbols": [0, 0], "shot_symbols_only": [0, 0]}
    for s, (rt, _rp) in rk.items():
        if rt.size < 3:
            continue
        idx = rng.integers(0, rt.size, size=min(per_symbol, rt.size))
        w = watched_at(watch, s, rt[idx])
        hits["all_symbols"][0] += int(w.sum())
        hits["all_symbols"][1] += int(w.size)
        if s in eps:
            hits["shot_symbols_only"][0] += int(w.sum())
            hits["shot_symbols_only"][1] += int(w.size)
    return {
        k: {"n": v[1], "in_tier3": v[0],
            "in_tier3_share": (v[0] / v[1]) if v[1] else None}
        for k, v in hits.items()
    } | {"method": (f"종목마다 랭킹 관측 시각을 최대 {per_symbol}개 균등 추첨해 "
                    "그때 tier3 였는지 센다")}


def confirm_wide_shots(conn: sqlite3.Connection, watch: dict, rk: dict,
                       eps: dict, *, rise: float, max_symbols: int = 400) -> dict:
    """**넓은 스트림이 본 슈팅을 테이프가 확인해 주는가.**

    tier3 안에 있던 넓은 스트림 슈팅만 볼 수 있다 — 그때만 체결 자료가 있다. 그 구간의
    초 막대 고가/저가가 같은 상승을 보여 주면 넓은 스트림을 믿을 수 있다는 뜻이다.
    **이 확인율이 낮으면 아래 '커버리지 문제' 수치는 잡음으로 부풀려진 것이다.**
    """
    per_sym: dict[str, list] = {}
    for s, ep in eps.items():
        rt, rp = rk[s]
        si, pi = ep["start"], ep["peak"]
        seen = watched_at(watch, s, rt[si])
        if seen.any():
            per_sym[s] = [rt[si][seen], rt[pi][seen], rp[si][seen], rp[pi][seen]]
    order = sorted(per_sym, key=lambda k: -len(per_sym[k][0]))[:max_symbols]
    conf, checked, tape_rise = 0, 0, []
    for s in order:
        b = second_bars(conn, s)
        if b is None or b[0].size < 2:
            continue
        ts, _n, lo, hi, _v = b
        t0, t1, _p0, _p1 = per_sym[s]
        for a, z in zip(t0, t1):
            i = np.searchsorted(ts, a - 2000, side="left")
            j = np.searchsorted(ts, z + 2000, side="right")
            if j - i < 2:
                continue
            checked += 1
            r = float(hi[i:j].max() / lo[i:j].min() - 1.0)
            tape_rise.append(r)
            if r >= rise:
                conf += 1
    return {
        "n_checked": checked,
        "n_confirmed": conf,
        "confirm_rate": (conf / checked) if checked else None,
        "tape_rise_in_window": pct_table(tape_rise),
        "symbols_checked": len(order),
        "rule": (f"넓은 스트림 슈팅 구간 [시작-2초, 정점+2초] 안에서 초 막대 "
                 f"hi/lo 폭이 {rise:.0%} 이상이면 확인된 것으로 센다"),
        "scope": ("tier3 안에 있던 넓은 스트림 슈팅만 확인 가능하다 — 밖은 체결 자료가 "
                  "애초에 없다. 즉 이 확인율은 **감시 중이던 부분집합**에서 잰 값이다."),
    }


def stage3_coverage(conn: sqlite3.Connection, bars: dict, watch: dict, rk: dict,
                    eps: dict, *, rise: float = SHOT_RISE,
                    max_seconds: int = SHOT_MAX_SECONDS) -> dict:
    """**탐지 문제인가 커버리지 문제인가.**

    체결 스트림에서 찾은 슈팅에 "그때 tier3 였나" 를 묻는 것은 **순환이다** — `/trades`
    가 tier3 에만 도니 답은 정의상 예다. 그래서 질문을 뒤집는다:

      **넓은 스트림(랭킹 가격)에서 슈팅을 찾고, 그중 몇 %가 tier3 안이었나.**

    tier3 밖에서 일어난 슈팅의 몫이 곧 **커버리지 문제의 크기**다.
    """
    # (a) 좁고 빠른 스트림에서 찾은 슈팅 — tier3 소속은 순환이므로 대신 **체류**를 본다
    tenure_before, tenure_after, rk_points = [], [], []
    trade_shot_ms: dict[str, np.ndarray] = {}
    for s, (ts, _n, lo, hi, vwap) in bars.items():
        ep = find_episodes(ts, vwap, vwap, rise=rise, max_seconds=max_seconds)
        si, pi = ep["start"], ep["peak"]
        if si.size == 0:
            continue
        trade_shot_ms[s] = ts[si]
        w = watch.get(s)
        if w is not None and w.size:
            for a in ts[si]:
                seg = w[(w >= a - 600_000) & (w <= a)]
                tenure_before.append((a - seg.min()) / 1000.0 if seg.size else 0.0)
                seg2 = w[(w >= a) & (w <= a + 600_000)]
                tenure_after.append((seg2.max() - a) / 1000.0 if seg2.size else 0.0)
        # 랭킹 점이 슈팅 안에 몇 개 찍히는가 — **형태가 보이는가**
        if s in rk:
            rt = rk[s][0]
            for a, b in zip(ts[si], ts[pi]):
                rk_points.append(int(((rt >= a) & (rt <= b)).sum()))

    # (b) ★ 넓은 스트림에서 찾은 슈팅 — 여기가 커버리지 질문의 본체
    wide_n = 0
    wide_dur, wide_rise, wide_ms, wide_flag = [], [], [], []
    for s, ep in eps.items():
        rt, rp = rk[s]
        si, pi = ep["start"], ep["peak"]
        wide_n += int(si.size)
        wide_dur.append((rt[pi] - rt[si]) / 1000.0)
        wide_rise.append(rp[pi] / rp[si] - 1.0)
        wide_ms.append(rt[si])
        # 종목 단위로 한 번에 묻는다 — 슈팅마다 묻지 않는다
        wide_flag.append(watched_at(watch, s, rt[si]))

    wms = np.concatenate(wide_ms) if wide_ms else np.array([], "int64")
    wsess = session_of(wms) if wms.size else np.array([], dtype=object)
    wwatch_flags = (np.concatenate(wide_flag) if wide_flag
                    else np.array([], dtype=bool))

    rkp = np.asarray(rk_points) if rk_points else np.array([])
    return {
        "conditions": {"rise": rise, "max_seconds": max_seconds,
                       "tier3_proxy": (f"호가 폴이 ±{TIER3_HALF_WINDOW_S}초에 "
                                       f"{TIER3_MIN_POLLS}회 이상")},
        "narrow_stream": {
            "what": "체결 초 막대에서 찾은 슈팅 (tier3 10종목)",
            "circularity": ("이 슈팅들이 tier3 였는지 묻는 것은 순환이다 — `/trades` 는 "
                            "tier3 에만 돈다. 그래서 소속 대신 **체류 시간**을 본다."),
            "n_shots": sum(v.size for v in trade_shot_ms.values()),
            "tier3_tenure_before_shot_s": pct_table(tenure_before),
            "tier3_tenure_after_shot_s": pct_table(tenure_after),
            "tenure_lookback_cap_s": 600,
            "tenure_before_at_cap_share": (
                float((np.asarray(tenure_before) >= 599.0).mean())
                if tenure_before else None),
            "tenure_reading": ("시작 직전 체류가 짧으면 **슈팅이라서 정원에 넣은 것**이지 "
                               "정원에 있어서 본 것이 아니다 — 그 경우 이 표본은 "
                               "'이미 움직인 뒤' 로 치우친다. 조회 창이 600초라 "
                               "그 값에 눌린 몫을 따로 싣는다."),
        },
        "ranking_points_inside_shot": {
            "n_shots": int(rkp.size),
            "distribution": {**{f"{k}": int((rkp == k).sum()) for k in range(0, 5)},
                             "5_or_more": int((rkp >= 5).sum())},
            "share_ge_2": float((rkp >= 2).mean()) if rkp.size else None,
            "share_0": float((rkp == 0).mean()) if rkp.size else None,
            "mean": float(rkp.mean()) if rkp.size else None,
            "median": float(np.median(rkp)) if rkp.size else None,
            "reading": ("12.4초 간격으로 32초짜리를 보면 2~3점이다. 시작·끝은 찍히지만 "
                        "**형태(가속·감속)는 안 보인다.** 0점인 슈팅은 그 종목이 그때 "
                        "랭킹 100위 밖이었다는 뜻이다."),
        },
        "wide_stream": {
            "what": "랭킹 가격 필드에서 같은 규칙으로 찾은 슈팅 (동시 ~158종목)",
            "n_shots": wide_n,
            "duration_s": pct_table(np.concatenate(wide_dur) if wide_dur else []),
            "total_rise": pct_table(np.concatenate(wide_rise) if wide_rise else []),
            "in_tier3_at_shot": int(wwatch_flags.sum()) if wwatch_flags.size else 0,
            "in_tier3_share": (float(wwatch_flags.mean())
                               if wwatch_flags.size else None),
            "outside_tier3_share": (float(1.0 - wwatch_flags.mean())
                                    if wwatch_flags.size else None),
            "by_session": {n: {"n": int((wsess == n).sum()),
                               "in_tier3_share": (float(wwatch_flags[wsess == n].mean())
                                                  if (wsess == n).any() else None)}
                           for n in ("pre", "regular", "after", "overnight")}
                          if wms.size else {},
            "reading": ("tier3 밖 비율이 곧 **커버리지 문제의 크기**다. 그 슈팅들은 "
                        "탐지기가 나빠서가 아니라 **보고 있지 않아서** 놓친 것이다. "
                        "**단, 아래 두 대조를 통과한 뒤에만 그렇게 읽어야 한다.**"),
            "caveat": ("랭킹 표본은 12.4초 간격이라 같은 규칙이라도 **더 굵게** 본다 — "
                       "짧은 슈팅은 통째로 빠지고, 지속시간은 격자에 눌린다. "
                       "체결 쪽 수와 직접 비교하면 안 된다."),
            "base_rate": coverage_base_rate(watch, rk, eps),
            "base_rate_reading": ("슈팅 순간의 tier3 비율을 **이 기저율과 비교**해야 한다. "
                                  "비슷하면 우리 정원 선정이 슈팅과 무관하다는 뜻이고, "
                                  "그것이 곧 커버리지 문제다."),
            "placebo_shuffled": wide_stream_placebo(rk, rise=rise,
                                                    max_seconds=max_seconds),
            "placebo_shuffled_verdict": (
                "**이 대조군은 쓸모가 없었다.** 가격열을 섞으면 실제보다 훨씬 들쭉날쭉해져 "
                "60초 안 +1% 가 오히려 더 자주 생긴다 — 섞은 쪽이 실제보다 많이 나온다. "
                "즉 '섞으면 사라진다' 는 기대가 성립하지 않는 종류의 귀무가설이다. "
                "넓은 스트림을 믿을 근거는 아래 `tape_confirmation` 쪽이다. "
                "숨기지 않고 실패한 대조로 남긴다."),
            "tape_confirmation": confirm_wide_shots(conn, watch, rk, eps, rise=rise),
        },
    }


# --------------------------------------------------------------------------- #
# 조립
# --------------------------------------------------------------------------- #
def build_report(conn: sqlite3.Connection) -> dict:
    t0 = time.time()
    rng = np.random.default_rng(SEED)
    syms = select_symbols(conn)
    print(f"  [1/6] symbols selected: {len(syms)}  ({time.time()-t0:.0f}s)")
    bars = load_bars(conn, syms)
    print(f"  [2/6] second bars loaded: {len(bars)}  ({time.time()-t0:.0f}s)")
    sat = load_saturation(conn)
    watch = load_tier3_watch(conn)
    print(f"  [3/6] saturation + tier3 watch loaded  ({time.time()-t0:.0f}s)")

    s1 = stage1_anatomy(conn, bars, sat, watch)
    print(f"  [4/6] stage 1 done: {s1.get('n_shots')} shots  ({time.time()-t0:.0f}s)")
    s2 = stage2_remaining(conn, bars, sat, rng=rng)
    s2w = stage2_remaining(conn, bars, sat, lag_s=DETECT_LAG_WORST_S,
                           rng=rng, with_placebo=False)
    sweep = stage2_threshold_sweep(conn, bars, sat, rng=rng)
    print(f"  [5/6] stage 2 done: {s2.get('n_fires')} fires  ({time.time()-t0:.0f}s)")

    rk = load_ranking_series(conn)
    print(f"  [6/6] ranking series loaded: {len(rk)} symbols  ({time.time()-t0:.0f}s)")
    eps = wide_episodes(rk, rise=SHOT_RISE, max_seconds=SHOT_MAX_SECONDS)
    print(f"        wide episodes found on {len(eps)} symbols  "
          f"({time.time()-t0:.0f}s)")
    s3 = stage3_coverage(conn, bars, watch, rk, eps)
    print(f"        stage 3 coverage done  ({time.time()-t0:.0f}s)")
    shape = ranking_stream_shape(conn, rk)
    print(f"        ranking stream shape done  ({time.time()-t0:.0f}s)")

    end_ms = conn.execute("SELECT MAX(ts_ms) FROM trades_snap").fetchone()[0]
    return {
        "measurement_conditions": {
            "window_start_ms": WINDOW_START_MS,
            "window_end_ms": int(end_ms) if end_ms else None,
            "window_note": ("08-04 04:00 ET 부터. 이전은 체결 종목 폭이 10배 좁아 "
                            "같은 표에 섞지 않는다."),
            "session_bands_et": [{"name": n, "from_s": lo, "to_s": hi}
                                 for n, lo, hi in SESSION_BANDS],
            "session_source": ("`session_date` 를 쓰지 않았다 — 고정 ET 벽시계. "
                               "애프터 종료는 이미 20:00 ET(=09:00 KST)로 잡혀 있어 "
                               "대기 중인 08:50→09:00 수정의 영향을 받지 않는다."),
            "regular_sessions_available": 2.6,
            "regular_sessions_note": ("사이클 군집 하한(5) 미만이다. **통합 CI 를 낼 수 "
                                      "없고, 여기 있는 것은 전부 방향뿐이다.**"),
            "tier3_max_config": 10,
            "streams_used": ["trades_snap", "orderbook_snap", "rankings_snap"],
            "streams_avoided": ["candles_1m (D-10 라벨 미해결)", "session_date",
                                "tick_classify/window_frame/find_shot_starts (docs/42)"],
            "detect_lag_s": DETECT_LAG_S,
            "detect_lag_worst_s": DETECT_LAG_WORST_S,
            "costs_excluded": "스프레드·수수료·체결 슬리피지는 **넣지 않았다**",
        },
        "stage1_shot_anatomy": s1,
        "stage2_remaining_after_detection": s2,
        "stage2_worst_case_lag": {"lag_s": DETECT_LAG_WORST_S,
                                  "real": s2w.get("real", {})},
        "stage2_threshold_sweep": sweep,
        "stage3_coverage": s3,
        "ranking_stream": shape,
    }


def _f(v, nd=4):
    return "n/a" if v is None else f"{v:.{nd}f}"


def main(db: Path, *, out_dir: Path | None = None) -> int:
    # 콘솔 기본 인코딩이 cp949 라 한글 주석·em dash 가 그대로 죽는다. 리포트 본문은
    # 어차피 UTF-8 JSON 으로 나가므로 화면 쪽만 맞춰 준다.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    conn = open_ro(db)
    print("=== building tick_stages report (read-only, no live calls)")
    rep = build_report(conn)

    s1 = rep["stage1_shot_anatomy"]
    print("\n=== [docs/44 stage 1] SHOT ANATOMY (second bars, 08-04+)")
    print(f"  shots {s1['n_shots']} over {s1['conditions']['symbols']} symbols")
    d = s1["duration_s"]
    print(f"  duration s: p10 {_f(d.get('p10'),0)} p25 {_f(d.get('p25'),0)} "
          f"p50 {_f(d.get('p50'),0)} p75 {_f(d.get('p75'),0)} p90 {_f(d.get('p90'),0)}")
    r = s1["total_rise"]
    print(f"  total rise: p10 {_f(r.get('p10'))} p50 {_f(r.get('p50'))} "
          f"p90 {_f(r.get('p90'))} max {_f(r.get('max'))}")
    tc = s1["time_to_cross_s"]
    print(f"  time to +{s1['conditions']['rise']:.0%} cross: p50 {_f(tc.get('p50'),0)}s "
          f"p90 {_f(tc.get('p90'),0)}s  (detection can only happen here or later)")
    ac = s1["after_cross"]
    print(f"  ** the crossing bar IS the peak bar in {ac['cross_is_peak_share']:.1%} "
          f"of shots -- nothing is left even at zero lag")
    print(f"     cross->peak seconds: p50 {_f(ac['cross_to_peak_s'].get('p50'),0)} "
          f"p75 {_f(ac['cross_to_peak_s'].get('p75'),0)} "
          f"p90 {_f(ac['cross_to_peak_s'].get('p90'),0)}  |  rise already done at cross: "
          f"p25 {_f(ac['rise_fraction_done_at_cross'].get('p25'),3)} "
          f"p50 {_f(ac['rise_fraction_done_at_cross'].get('p50'),3)}")
    for k, v in s1["by_session"].items():
        print(f"    {k:<10} n {v['n']:>5}  dur p50 {_f(v['duration_s'].get('p50'),0)}s "
              f"rise p50 {_f(v['total_rise'].get('p50'))}")
    for k, v in s1["by_price_band"].items():
        print(f"    {k:<10} n {v['n']:>5}  dur p50 {_f(v['duration_s'].get('p50'),0)}s "
              f"rise p50 {_f(v['total_rise'].get('p50'))}")
    sa = s1["saturation"]
    print(f"  touching a capped bucket: {sa['shots_touching_capped_bucket']} "
          f"({sa['share']:.1%})  dur p50 sat {_f(sa['duration_s_saturated'].get('p50'),0)}s "
          f"vs clean {_f(sa['duration_s_clean'].get('p50'),0)}s")
    ia = s1["inter_arrival_s"]
    print(f"  inter-arrival s: raw p50 {_f(ia['raw'].get('p50'),0)} "
          f"(n={ia['raw'].get('n')})  continuous-watch p50 "
          f"{_f(ia['continuous_watch_only'].get('p50'),0)} "
          f"(n={ia['continuous_watch_only'].get('n')})")
    ar = s1["arrival_rate"]
    print(f"  arrival rate: {ar['shots_per_watched_symbol_hour']} shots per watched "
          f"symbol-hour over {ar['watched_symbol_hours']} symbol-hours")

    s2 = rep["stage2_remaining_after_detection"]
    print("\n=== [docs/44 stage 2] WHAT IS LEFT AFTER DETECTION (+2.56s)")
    print(f"  detector: {s2['conditions']['detector']}")
    print(f"  fires {s2['n_fires']}, entered {s2['real'].get('n')}")
    print(f"  actual entry delay p50 "
          f"{_f(s2['real'].get('entry_delay_actual_s',{}).get('p50'),1)}s "
          f"(1s grid rounds 2.56s up)")
    print("  p50 is pinned to 0 by the 1s grid + discrete prices — read mean and")
    print("  share>0 alongside it, never the median alone.")
    print(f"  {'':<16}{'real p50':>10}{'real mean':>11}{'real >0':>9}"
          f"{'plcT mean':>11}{'plcT >0':>9}{'plcS mean':>11}{'plcS >0':>9}")
    for kk in ["slip_from_fire"] + [f"ret_{h}s" for h in HORIZONS_S] + \
              ["mfe_60s", "mae_60s"]:
        r = s2["real"].get(kk, {})
        t_ = s2.get("placebo_random_time", {}).get(kk, {})
        y = s2.get("placebo_random_symbol", {}).get(kk, {})
        print(f"  {kk:<16}{_f(r.get('p50')):>10}{_f(r.get('mean'),5):>11}"
              f"{_f(r.get('share_positive'),3):>9}{_f(t_.get('mean'),5):>11}"
              f"{_f(t_.get('share_positive'),3):>9}{_f(y.get('mean'),5):>11}"
              f"{_f(y.get('share_positive'),3):>9}")
    if "by_entry_promptness" in s2:
        print("  by entry promptness (the tape often goes SILENT right after a fire):")
        for tag, v in s2["by_entry_promptness"].items():
            print(f"    {tag:<12} n {v['n']:>5} ({v['share']:.1%})  "
                  f"ret_30s mean {_f(v.get('ret_30s',{}).get('mean'),5)}  "
                  f"mfe_60s mean {_f(v.get('mfe_60s',{}).get('mean'),5)}  "
                  f"mae_60s mean {_f(v.get('mae_60s',{}).get('mean'),5)}")
    if "by_saturation" in s2:
        print("  by saturation (ret_30s p50):")
        for tag, v in s2["by_saturation"].items():
            print(f"    {tag:<10} n {v['n']:>5}  "
                  f"ret_30s {_f(v.get('ret_30s',{}).get('p50'))}  "
                  f"mfe_60s {_f(v.get('mfe_60s',{}).get('p50'))}")
    w = rep["stage2_worst_case_lag"]["real"]
    print(f"  worst-case lag {DETECT_LAG_WORST_S}s: ret_30s p50 "
          f"{_f(w.get('ret_30s',{}).get('p50'))}  mfe_60s p50 "
          f"{_f(w.get('mfe_60s',{}).get('p50'))}")

    print("\n  --- does the answer follow our threshold? (min_rise sweep; means)")
    print(f"  {'thr':<9}{'fires':>7}{'slip':>10}{'ret_10s':>10}{'ret_30s':>10}"
          f"{'ret_60s':>10}{'mfe_60s':>10}{'mae_60s':>10}")
    for th, v in rep["stage2_threshold_sweep"].items():
        print(f"  {th:<9}{v['n_fires']:>7}{_f(v['slip_from_fire_mean'],5):>10}"
              f"{_f(v['ret_10s_mean'],5):>10}{_f(v['ret_30s_mean'],5):>10}"
              f"{_f(v['ret_60s_mean'],5):>10}{_f(v['mfe_60s_mean'],5):>10}"
              f"{_f(v['mae_60s_mean'],5):>10}")

    s3 = rep["stage3_coverage"]
    rs = rep["ranking_stream"]
    print("\n=== [docs/44 stage 3] WERE WE EVEN LOOKING")
    n = s3["narrow_stream"]
    print(f"  narrow (trades, tier3): {n['n_shots']} shots — asking 'were they tier3' "
          f"is circular")
    print(f"    tier3 tenure BEFORE shot: p10 "
          f"{_f(n['tier3_tenure_before_shot_s'].get('p10'),0)}s "
          f"p50 {_f(n['tier3_tenure_before_shot_s'].get('p50'),0)}s")
    rp = s3["ranking_points_inside_shot"]
    print(f"  ranking points landing inside a shot: mean {_f(rp['mean'],2)}  "
          f"share 0 pts {_f(rp['share_0'],3)}  share >=2 pts {_f(rp['share_ge_2'],3)}")
    print(f"    distribution {rp['distribution']}")
    wd = s3["wide_stream"]
    print(f"  wide (ranking price field): {wd['n_shots']} shots  "
          f"dur p50 {_f(wd['duration_s'].get('p50'),0)}s  "
          f"rise p50 {_f(wd['total_rise'].get('p50'))}")
    pl = wd["placebo_shuffled"]
    tc = wd["tape_confirmation"]
    br = wd["base_rate"]
    print(f"    CONTROL tape confirmation (in-tier3 subset): "
          f"{tc['n_confirmed']}/{tc['n_checked']} = {_f(tc['confirm_rate'],3)} "
          f"<- wide detector is trustworthy where we can check it")
    print(f"    CONTROL shuffled placebo: {pl['n_shots_shuffled']} vs "
          f"{wd['n_shots']} real -- USELESS NULL, see verdict in json")
    print(f"    IN tier3 at shot time: {_f(wd['in_tier3_share'],3)} "
          f"-> OUTSIDE tier3: {_f(wd['outside_tier3_share'],3)}")
    print(f"    BASE RATE in_tier3 at a random ranking moment: "
          f"all {_f(br['all_symbols']['in_tier3_share'],3)}  "
          f"shot-capable symbols only "
          f"{_f(br['shot_symbols_only']['in_tier3_share'],3)}")
    print(f"    -> compare the two lines above: that comparison, not the raw 96%, "
          f"is the coverage answer")
    for k, v in wd["by_session"].items():
        print(f"      {k:<10} n {v['n']:>6}  in_tier3 {_f(v['in_tier3_share'],3)}")

    print("\n=== [docs/44] RANKING PRICE STREAM — the candidate nobody costed")
    print(f"  concurrent symbols per 15s: p10 "
          f"{_f(rs['concurrent_symbols_per_15s'].get('p10'),0)} "
          f"p50 {_f(rs['concurrent_symbols_per_15s'].get('p50'),0)} "
          f"p90 {_f(rs['concurrent_symbols_per_15s'].get('p90'),0)}  "
          f"(NOT 200 — the two lists overlap)")
    print(f"  update gap s: p50 {_f(rs['update_gap_s'].get('p50'),1)}  "
          f"share <=15s {_f(rs['update_gap_share_le_15s'],3)}")
    print(f"    in BOTH lists ({rs['symbols_in_both_lists']} symbols): gap p50 "
          f"{_f(rs['update_gap_in_both_lists_s'].get('p50'),1)}s  |  in ONE list: "
          f"{_f(rs['update_gap_in_one_list_s'].get('p50'),1)}s")
    cv = rs["cannot_see"]["interval_volume"]
    print(f"  interval volume recoverable: {cv['recoverable']} — "
          f"vol_qu diffs negative {_f(cv['vol_qu_diff_negative_share'],3)} "
          f"(n={cv['n_diffs']}) => rolling window, not cumulative")

    out = out_dir or OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    path = out / "tick_stages.json"
    path.write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    db = Path(args[0]) if args else Path(
        r"C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db")
    sys.exit(main(db))

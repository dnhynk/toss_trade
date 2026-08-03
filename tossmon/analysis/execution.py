"""집행 현실성 측정식 — 스프레드·호가 잔량·실효 왕복 비용 (docs/18).

docs/16 의 비용 전제(정규장 왕복 ~1.7%)를 **실측으로 교체**하기 위한 도구다.
가설 검정이 아니라 측정 도구이므로 검증 깔때기와 무관하다.

## 단위 (docs/04 §계약)

- 가격 `price_u` = **마이크로달러** (1 USD = 1_000_000)
- 수량 `qty_u` = **마이크로주** (1주 = 1_000_000)
- 금액 = `price_u * qty_u / 1_000_000` (마이크로달러)

정수 누적은 Python `int` 로 한다 — 가격×수량은 int64 를 넘길 수 있다(구 VWAP 결함).

## 설계 규칙 (docs/17 §1 의 금지 규칙 승계)

- 호가가 없거나 유효하지 않으면 **`NaN` 을 돌려주고 그 수를 센다.** 0 으로 채우지 않는다.
- 시계 창이 채워지지 않는 데이터(호가는 심볼당 ~20분만 존재)에서는 **사건 시간**
  (스냅 개수) 블록을 쓴다.
"""
from __future__ import annotations

import json
import math

import pandas as pd

MICRO = 1_000_000
_NAN = float("nan")

#: 한 번에 걷어낼 수 있는 최대 호가 단계 (그 이상은 "소진"으로 본다).
DEFAULT_MAX_LEVELS = 10

#: docs/16 §2 — 회당 물량 $500~2,000 급.
DEFAULT_NOTIONALS_USD = (500, 1000, 2000, 5000)

#: docs/16 비용 전제 중 회전 비용에 해당하는 부분 (수수료 왕복).
COMMISSION_ROUND_TRIP = 0.002


def parse_depth(depth_json: str | None) -> tuple[list[tuple[int, int]],
                                                 list[tuple[int, int]]]:
    """`depth_json` → (bids, asks). bids 는 높은 가격 우선, asks 는 낮은 가격 우선.

    파싱 불가·빈 값이면 빈 리스트 두 개. 예외를 삼키지 않고 빈 결과로 드러낸다.
    """
    if not depth_json:
        return [], []
    try:
        d = json.loads(depth_json)
    except (ValueError, TypeError):
        return [], []
    def side(key: str) -> list[tuple[int, int]]:
        out = []
        for lv in (d.get(key) or []):
            try:
                p, q = int(lv["price_u"]), int(lv["qty_u"])
            except (KeyError, TypeError, ValueError):
                continue
            if p > 0 and q > 0:
                out.append((p, q))
        return out
    bids = sorted(side("bids"), key=lambda x: -x[0])
    asks = sorted(side("asks"), key=lambda x: x[0])
    return bids, asks


def relative_spread(bid1_u, ask1_u) -> float:
    """(ask − bid) / mid. 한쪽이라도 없으면 `NaN`."""
    try:
        b, a = int(bid1_u), int(ask1_u)
    except (TypeError, ValueError):
        return _NAN
    if b <= 0 or a <= 0 or a < b:
        return _NAN
    mid = (a + b) / 2.0
    return (a - b) / mid if mid > 0 else _NAN


def mid_u(bid1_u, ask1_u) -> float:
    try:
        b, a = int(bid1_u), int(ask1_u)
    except (TypeError, ValueError):
        return _NAN
    return (a + b) / 2.0 if (b > 0 and a > 0) else _NAN


def level_notional_u(price_u: int, qty_u: int) -> int:
    """한 단계의 금액(마이크로달러). Python int 누적."""
    return int(price_u) * int(qty_u) // MICRO


def walk_book(levels: list[tuple[int, int]], notional_usd: float, *,
              max_levels: int = DEFAULT_MAX_LEVELS) -> dict:
    """`notional_usd` 만큼 호가를 걷어낼 때의 평균 체결가·소요 단계.

    반환 `avg_px_u` 는 체결 금액 가중 평균가. 주문을 다 채우지 못하면
    `exhausted=True` 이고 `filled_usd` 가 실제 체결 가능액이다 —
    **못 채운 부분을 임의 가격으로 채우지 않는다**(결측을 값으로 만들지 않기).
    """
    target_u = int(round(float(notional_usd) * MICRO))
    if not levels or target_u <= 0:
        return {"avg_px_u": _NAN, "filled_usd": 0.0, "levels_used": 0,
                "exhausted": True, "shares": 0.0}
    spent_u = 0            # 마이크로달러
    shares_qu = 0          # 마이크로주
    used = 0
    for price_u, qty_u in levels[:max_levels]:
        used += 1
        avail_u = level_notional_u(price_u, qty_u)
        take_u = min(avail_u, target_u - spent_u)
        if take_u <= 0:
            used -= 1
            break
        # 금액 take_u 를 이 가격에 사면 몇 주인가
        take_qu = take_u * MICRO // int(price_u)
        spent_u += take_u
        shares_qu += take_qu
        if spent_u >= target_u:
            break
    if shares_qu <= 0:
        return {"avg_px_u": _NAN, "filled_usd": 0.0, "levels_used": used,
                "exhausted": True, "shares": 0.0}
    avg_px_u = spent_u * MICRO / shares_qu
    return {"avg_px_u": float(avg_px_u), "filled_usd": spent_u / MICRO,
            "levels_used": used, "exhausted": spent_u < target_u,
            "shares": shares_qu / MICRO}


def cross_cost(levels: list[tuple[int, int]], mid: float, notional_usd: float, *,
               side: str, max_levels: int = DEFAULT_MAX_LEVELS) -> dict:
    """중간가 대비 편도 비용(비율). `side='buy'` 는 asks, `'sell'` 은 bids 를 먹는다.

    미체결(호가 소진)이면 `cost` 는 **체결된 부분에 대해서만** 계산하고
    `exhausted=True` 로 표시한다 — 체결 못 한 물량의 비용을 지어내지 않는다.
    """
    if side not in ("buy", "sell"):          # 결측보다 먼저 잡는다 (오타를 NaN 으로 숨기지 않기)
        raise ValueError("side must be 'buy' or 'sell'")
    w = walk_book(levels, notional_usd, max_levels=max_levels)
    if not (mid == mid) or mid <= 0 or not (w["avg_px_u"] == w["avg_px_u"]):
        return {**w, "cost": _NAN}
    cost = ((w["avg_px_u"] - mid) / mid) if side == "buy" else ((mid - w["avg_px_u"]) / mid)
    return {**w, "cost": float(cost)}


def round_trip_cost(bids, asks, notional_usd: float, *, exit_mode: str = "cross",
                    commission: float = COMMISSION_ROUND_TRIP,
                    max_levels: int = DEFAULT_MAX_LEVELS) -> dict:
    """왕복 실효 비용(비율). 진입은 항상 **마켓터블**(ask 를 걷는다).

    `exit_mode`:
      - `"cross"`   — 이탈도 걷는다(bid 를 먹는다). 최악이자 즉시성 보장.
      - `"passive"` — 이탈은 최우선 매도호가에 걸어 수요에 체결된다고 본다
        (스프레드를 **번다**). 서지 중 수요가 붙는 상황의 낙관 시나리오.
      - `"mid"`     — 중간가 체결 가정. 두 시나리오의 중간 참고선.

    반환 `total` = 진입비용 + 이탈비용 + 수수료. 어느 한쪽이라도 산출 불가면 `NaN`.
    """
    m = None
    if bids and asks and asks[0][0] >= bids[0][0]:
        # B1(감사 4차): 크로스/락 호가(ask < bid)는 `relative_spread` 가 이미 NaN 으로
        # 막는 조건이다. 같은 모듈에서 이 함수만 통과시키면 음의 진입비용이 중앙값
        # 집계에 섞여 비용을 낙관 쪽으로 민다.
        m = (bids[0][0] + asks[0][0]) / 2.0
    if m is None or m <= 0:
        # 키 구성은 정상 경로와 동일하게 유지한다 — 호출부가 분기하지 않도록.
        return {"entry": _NAN, "exit": _NAN, "total": _NAN, "entry_levels": 0,
                "entry_exhausted": True, "exit_exhausted": True,
                "entry_filled_usd": 0.0, "exit_size_aware": exit_mode == "cross"}
    ent = cross_cost(asks, m, notional_usd, side="buy", max_levels=max_levels)
    if exit_mode == "cross":
        ex = cross_cost(bids, m, notional_usd, side="sell", max_levels=max_levels)
        exit_cost, exit_exh = ex["cost"], ex["exhausted"]
    elif exit_mode == "passive":
        # 최우선 매도호가에 걸어 체결 -> 중간가 대비 +half spread 를 번다
        exit_cost = -((asks[0][0] - m) / m)
        exit_exh = False
    elif exit_mode == "mid":
        exit_cost, exit_exh = 0.0, False
    else:
        raise ValueError("exit_mode must be 'cross', 'passive' or 'mid'")
    if not (ent["cost"] == ent["cost"]) or not (exit_cost == exit_cost):
        total = _NAN
    else:
        total = ent["cost"] + exit_cost + commission
    return {"entry": ent["cost"], "exit": exit_cost, "total": total,
            "entry_levels": ent["levels_used"], "entry_exhausted": ent["exhausted"],
            "exit_exhausted": exit_exh, "entry_filled_usd": ent["filled_usd"],
            # B2(감사 4차): passive/mid 이탈은 반대편 호가를 걷지 않으므로 **크기를
            # 반영하지 않는다**. 호출부가 이 사실을 알 수 있게 드러낸다.
            "exit_size_aware": exit_mode == "cross"}


def size_cost_curve(books, notionals=DEFAULT_NOTIONALS_USD, *,
                    require_unexhausted: bool = True,
                    max_levels: int = DEFAULT_MAX_LEVELS) -> "pd.DataFrame":
    """주문 크기 → 비용 곡선 (감사 4차 A3 대비 — **데이터가 쌓이면 자동 산출**).

    A3 의 지적: 호가가 소진되면 체결분에만 비용을 매기므로 **주문을 20배 키워도 비용이
    변하지 않는다**. 즉 현재 표본(스냅의 95.9% 가 1레벨)에서는 크기 효과가 원리상
    측정 불가다. 이 함수는 그 한계를 **숨기지 않고 분리**한다:

    - `require_unexhausted=True` 면 **가장 큰 주문까지 소진되지 않는 스냅**만 쓴다.
      그 표본에서만 크기-비용 곡선이 의미를 갖는다.
    - `n_usable` 이 0 이면 "측정 불가"를 그대로 돌려준다 — 상수 곡선을 지어내지 않는다.

    W5 의 tier-2 호가 수집이 쌓여 다단계 호가가 들어오면 `n_usable` 이 올라가고
    이 곡선이 처음으로 의미를 갖는다. 그 전까지 결론을 내면 안 된다.

    `books` 는 `depth_json`(또는 `bids`/`asks`) 을 가진 행들의 iterable.
    """
    import pandas as pd  # 지역 임포트 — 모듈 상단 의존성을 늘리지 않는다

    parsed = []
    for r in books:
        dj = r.get("depth_json") if isinstance(r, dict) else getattr(r, "depth_json", None)
        b, a = parse_depth(dj)
        if b and a:
            parsed.append((b, a))
    rows = []
    biggest = max(notionals) if notionals else 0
    for n in notionals:
        costs, used = [], 0
        for b, a in parsed:
            if require_unexhausted:
                probe = walk_book(a, biggest, max_levels=max_levels)
                if probe["exhausted"]:
                    continue
            rt = round_trip_cost(b, a, n, max_levels=max_levels)
            if rt["total"] == rt["total"]:
                costs.append(rt["total"])
                used += 1
        rows.append({"notional_usd": n, "n_usable": used,
                     "n_books": len(parsed),
                     "median_round_trip": (float(pd.Series(costs).median())
                                           if costs else _NAN),
                     "measurable": bool(used > 0)})
    return pd.DataFrame(rows)


def breakeven_pct(round_trip_total: float) -> float:
    """왕복 비용 `c` 를 덮는 데 필요한 총 상승률 = `c / (1 - c)` (감사 4차 C1).

    구현이 항등함수였다 — 비용률과 필요 상승률을 같은 값으로 봤다. 진입가 대비 c 를
    쓰고 나면 남은 원금이 `1-c` 이므로 실제로는 그만큼 더 올라야 한다.
    3.22% 에서 +0.11%p, 6.94% 에서 +0.52%p, 8.73% 에서 +0.84%p 차이가 난다.
    """
    c = round_trip_total
    if not (c == c) or c >= 1.0:
        return _NAN
    return float(c / (1.0 - c))


def top_of_book_usd(bid1_u, bid1_qu, ask1_u, ask1_qu) -> dict:
    """최우선 호가 양측의 금액($). 집행 가능 규모의 1차 상한."""
    def n(p, q):
        try:
            p, q = int(p), int(q)
        except (TypeError, ValueError):
            return _NAN
        return level_notional_u(p, q) / MICRO if (p > 0 and q > 0) else _NAN
    b, a = n(bid1_u, bid1_qu), n(ask1_u, ask1_qu)
    both = [x for x in (b, a) if x == x]
    return {"bid_usd": b, "ask_usd": a,
            "min_usd": (min(both) if len(both) == 2 else _NAN)}


def depth_to_usd(levels: list[tuple[int, int]], *,
                 max_levels: int = DEFAULT_MAX_LEVELS) -> float:
    """상위 `max_levels` 단계의 누적 금액($)."""
    if not levels:
        return _NAN
    return sum(level_notional_u(p, q) for p, q in levels[:max_levels]) / MICRO


def price_band(mid_price_usd: float) -> str:
    """가격대 라벨 — 유니버스 필터 제안의 축."""
    if not (mid_price_usd == mid_price_usd) or mid_price_usd <= 0:
        return "unknown"
    for hi, label in ((0.5, "$0.10-0.50"), (1.0, "$0.50-1"), (2.0, "$1-2"),
                      (5.0, "$2-5"), (20.0, "$5-20")):
        if mid_price_usd < hi:
            return label
    return "$20+"


PRICE_BAND_ORDER = ("$0.10-0.50", "$0.50-1", "$1-2", "$2-5", "$5-20", "$20+",
                    "unknown")


def summarize(series: pd.Series) -> dict:
    """중앙값 중심 요약. 전부 결측이면 n=0 을 그대로 보고한다."""
    s = pd.to_numeric(pd.Series(series), errors="coerce").dropna()
    if s.empty:
        return {"n": 0, "median": _NAN, "p25": _NAN, "p75": _NAN, "mean": _NAN}
    return {"n": int(len(s)), "median": float(s.median()),
            "p25": float(s.quantile(0.25)), "p75": float(s.quantile(0.75)),
            "mean": float(s.mean())}

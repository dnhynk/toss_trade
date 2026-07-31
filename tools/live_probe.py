"""라이브 실측 프로브 — 소유: W1. 라이브 리스 보유 시에만 실행.

최소 호출 수로 docs/06_live_facts.md 의 미확인 항목을 실측한다 (목록: ORCHESTRATION_PROMPT §4-W1-4).
각 항목은 "확인됨/미확인 + 근거 응답 스니펫(마스킹)" 으로 기록. 추측 금지.

    # mock 드라이런 (기본값 — 안전한 쪽)
    TOSS_BASE_URL=http://127.0.0.1:8899 python tools/live_probe.py --probe all

    # 라이브 실측 (리스 보유자만)
    TOSS_LIVE=1 TOSS_BASE_URL=https://... python tools/live_probe.py --live --keys api_keys
    python tools/live_probe.py --list

**기본값은 안전한 쪽이다** (감사 B-5). 실토큰 발급은 `--live` 플래그 + `TOSS_LIVE=1` 이
둘 다 있을 때만 일어나고, 그 둘 없이 실서버를 대상으로 하면 실행 자체를 거부한다.
base URL 은 기본값이 없다 (계약 C-9) — `--base-url` 또는 `TOSS_BASE_URL` 필수.

`force429` 는 **의도적으로 라이브 429 를 유발**하므로 `--probe all` 에 포함되지 않는다.
`--probe force429` 로 명시해야만 실행된다. 같은 자격증명으로 컬렉터가 돌고 있으면
서버 측 같은 버킷의 페널티를 공유하므로, 컬렉터 가동 중에는 실행하지 말 것.

결과는 stdout(JSON) + `--out` 경로에 쓰고, 응답 스냅샷은 tests/fixtures/live/live_*.json 으로
마스킹해 저장한다.

호출 예산: `--probe all` 기준 약 70회 (그룹별 한도의 70% 이내로 자동 조절됨).
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tossmon.api.client import TossClient                    # noqa: E402
from tossmon.api.errors import (                             # noqa: E402
    Forbidden,
    RateLimited,
    SchemaMismatch,
    TossApiError,
    TransientHTTP,
)
from tossmon.api.client import GuardedTransport              # noqa: E402
from tossmon.api.endpoints import check_allowed              # noqa: E402
from tossmon.api.limiter import GroupRateLimiter             # noqa: E402
from tossmon.api.models import iso_to_ms, ms_to_iso, ms_to_iso_et, ms_to_iso_kst  # noqa: E402
from tossmon.api.tokens import TokenManager, lease_dir       # noqa: E402

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "live"

# base URL 은 **기본값이 없다** (계약 C-9). `--base-url` 또는 `TOSS_BASE_URL` 로만 받는다.
# 이전의 `LIVE_BASE_URL = "https://" + "openapi..."` 는 기본값이자 grep 회피였다 (감사 M-7).
#
# 아래 상수는 그것과 용도가 다르다 — **오직 판별용**이며 요청 URL 을 만드는 데 쓰지 않는다.
# base_url 이 실서버를 가리키는지 검사해
#   (a) 라이브 플래그 없이 실서버를 때리는 실행을 거부하고
#   (b) 캡처 픽스처를 source="live" 로 표기할지 정한다.
# 이 상수를 없애면 "실서버인지 모르는 채로 때리는" 상태가 되어 오히려 더 위험하다.
LIVE_HOST_MARKER = "openapi.tossinvest.com"

LIMITS = {"AUTH": 5, "STOCK": 5, "MARKET_DATA": 10, "MARKET_DATA_CHART": 5,
          "RANKING": 5, "MARKET_INFO": 3}

# 유동성이 확실한 대형주(시세 진행 관측용) + 소형주(전략 대상 실측용)
LIQUID = "AAPL"
SMALL = ["SNTI", "BTAI", "CRKN"]

DAY_MS = 86_400_000

# 폴링형 프로브의 기본 파라미터. --fast 로 줄여 mock 드라이런에 쓴다.
POLL = {"quote_polls": 6, "quote_gap_s": 12.0, "rank_polls": 4, "rank_gap_s": 20.0}

# 마스킹 대상 필드 (계약 C-11 §5)
MASK_FIELDS = {"accountNo", "accountSeq", "access_token", "requestId", "isinCode"}
MASK_HEADERS = {"authorization", "set-cookie", "x-amz-cf-id", "x-request-id"}

# save_fixture 가 source 를 판정하기 위한 플래그 (run() 에서 설정).
_IS_LIVE_TARGET = [False]


def default_state_path() -> Path:
    """토큰 상태파일 기본 경로 — 리스와 같은 리포 밖 절대경로 (감사 A-1).

    상대경로 기본값(`data/token_state.json`)은 CWD 종속이라 워크트리마다 다른 파일을
    가리켰다. 리스는 이제 자격증명에서 유도되므로 상태파일이 갈라져도 토큰 살해로는
    이어지지 않지만, 갈라질 이유 자체가 없다.
    """
    return lease_dir() / "token_state.json"


# ------------------------------------------------------------------ 유틸


def now_ms() -> int:
    return int(time.time() * 1000)


def _masked_value(v):
    """타입 보존 마스킹 — 스키마가 깨지면 픽스처로 못 쓴다."""
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return 0
    if isinstance(v, float):
        return 0.0
    return "***MASKED***"


def mask(obj):
    """응답에서 계좌/토큰/추적 식별자를 마스킹 (타입은 유지)."""
    if isinstance(obj, dict):
        return {k: (_masked_value(v) if k in MASK_FIELDS else mask(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask(x) for x in obj]
    return obj


def mask_headers(headers: dict) -> dict:
    out = {}
    for k, v in headers.items():
        if k.lower() in MASK_HEADERS:
            out[k] = "***MASKED***"
        elif k.lower().startswith(("x-ratelimit", "retry-after", "content-type", "date")):
            out[k] = v
    return out


def save_fixture(name: str, endpoint: str, case: str, status: int, body,
                 headers: dict | None = None, note: str = "", match: dict | None = None) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    fx = {
        "endpoint": endpoint,
        "case": case,
        "status": status,
        # mock 드라이런 결과가 라이브 실측으로 둔갑하지 않도록 base_url 로 판정한다.
        "source": "live" if _IS_LIVE_TARGET[0] else "synthetic",
        "captured_at_ms": now_ms(),
        "note": note,
    }
    if match:
        fx["match"] = match
    if headers:
        fx["headers"] = mask_headers(headers)
    fx["body"] = mask(body)
    (FIXTURE_DIR / name).write_text(
        json.dumps(fx, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def snippet(obj, limit: int = 700) -> str:
    """docs/06 근거용 스니펫 (마스킹 후 잘라내기)."""
    return json.dumps(mask(obj), ensure_ascii=False)[:limit]


def trunc_result(body: dict, n: int = 3) -> dict:
    """근거 스니펫용으로 result 배열을 앞 n개만 남긴다."""
    b = copy.deepcopy(body)
    r = b.get("result")
    if isinstance(r, list):
        b["result"] = r[:n]
        b["_truncated_from"] = len(r)
    elif isinstance(r, dict):
        for key in ("candles", "rankings", "bids", "asks"):
            if isinstance(r.get(key), list):
                r[f"_{key}_len"] = len(r[key])
                r[key] = r[key][:n]
    return b


# ---------------------------------------------------------------- 프로브


async def probe_calendar(c: TossClient) -> dict:
    """`/market-calendar/US` 실제 응답 + 현재 세션 판정."""
    body = await c._request("GET", "/api/v1/market-calendar/US")
    save_fixture("live_market_calendar_us.json", "GET /api/v1/market-calendar/US",
                 "default", 200, body, c.last_headers, "라이브 캡처")
    cal = await c.get_us_calendar()
    t = now_ms()
    session = "closed"
    today = cal["today"]
    for name in ("day", "pre", "regular", "after"):
        w = getattr(today, name)
        if w and w.start_ms <= t < w.end_ms:
            session = name
    return {
        "verdict": "확인됨",
        "session_now": session,
        "now_kst": ms_to_iso_kst(t),
        "now_et": ms_to_iso_et(t),
        "today_date": today.date,
        "sessions_today": {
            n: (None if getattr(today, n) is None else
                [ms_to_iso_kst(getattr(today, n).start_ms), ms_to_iso_kst(getattr(today, n).end_ms)])
            for n in ("day", "pre", "regular", "after")
        },
        "keys": sorted(cal),
        "evidence": snippet(trunc_result(body)),
    }


async def probe_quote_realtime(c: TossClient, polls: int | None = None,
                               gap_s: float | None = None) -> dict:
    """미국 시세 실시간 여부/지연 — 같은 심볼을 시간차 폴링해 timestamp 진행을 본다."""
    polls = POLL["quote_polls"] if polls is None else polls
    gap_s = POLL["quote_gap_s"] if gap_s is None else gap_s
    symbols = [LIQUID] + SMALL
    obs: list[dict] = []
    first_body = None
    for i in range(polls):
        wall = now_ms()
        body = await c._request("GET", "/api/v1/prices",
                                params={"symbols": ",".join(symbols)})
        if first_body is None:
            first_body = body
        row = {"wall_ms": wall}
        for item in body.get("result", []):
            ts = item.get("timestamp")
            row[item["symbol"]] = {
                "ts": ts,
                "lag_s": None if ts is None else round((wall - iso_to_ms(ts)) / 1000, 1),
                "last": item.get("lastPrice"),
            }
        obs.append(row)
        if i < polls - 1:
            await asyncio.sleep(gap_s)

    save_fixture("live_prices_us.json", "GET /api/v1/prices", "default", 200,
                 first_body, c.last_headers,
                 "라이브 캡처 — mock 의 심볼 카탈로그로도 쓰인다")

    per_symbol = {}
    for sym in symbols:
        tss = [o[sym]["ts"] for o in obs if sym in o and o[sym]["ts"]]
        lags = [o[sym]["lag_s"] for o in obs if sym in o and o[sym]["lag_s"] is not None]
        lasts = [o[sym]["last"] for o in obs if sym in o]
        uniq_ts = sorted(set(tss))
        per_symbol[sym] = {
            "distinct_timestamps": len(uniq_ts),
            "timestamp_advanced": len(uniq_ts) > 1,
            "price_changed": len(set(lasts)) > 1,
            "lag_s_min": min(lags) if lags else None,
            "lag_s_max": max(lags) if lags else None,
            "lag_s_median": sorted(lags)[len(lags) // 2] if lags else None,
            "sample_ts": uniq_ts[-3:],
        }
    liquid = per_symbol.get(LIQUID, {})
    med = liquid.get("lag_s_median")
    if med is None:
        verdict, note = "미확인", "timestamp 가 전부 null (해당 세션에 체결 없음)"
    elif med > 600:
        verdict, note = "확인됨", f"지연 시세로 관측됨 (중앙 지연 {med}s ≈ {med/60:.1f}분)"
    elif liquid.get("timestamp_advanced"):
        verdict, note = "확인됨", f"실시간으로 관측됨 (중앙 지연 {med}s, 폴링마다 timestamp 진행)"
    else:
        verdict, note = "부분확인", f"중앙 지연 {med}s 이나 관측 구간에서 timestamp 진행 없음"
    return {
        "verdict": verdict, "note": note,
        "polls": polls, "gap_s": gap_s,
        "per_symbol": per_symbol,
        "evidence": snippet(trunc_result(first_body)),
    }


async def _candles_at(c: TossClient, symbol: str, interval: str, before_ms: int | None,
                      count: int = 1) -> tuple[list, dict]:
    params = {"symbol": symbol, "interval": interval, "count": count, "adjusted": "true"}
    if before_ms is not None:
        params["before"] = ms_to_iso(before_ms)
    body = await c._request("GET", "/api/v1/candles", params=params)
    return (body.get("result") or {}).get("candles") or [], body


async def probe_candle_retention(c: TossClient) -> dict:
    """`/candles 1m` 과거 보관 기간 — `before` 역방향 지수탐색 + 이분탐색 (Phase 1 핵심 변수)."""
    calls = 0
    newest, newest_body = await _candles_at(c, LIQUID, "1m", None, count=200)
    calls += 1
    if not newest:
        return {"verdict": "미확인", "note": "최신 1m 페이지가 비어 있음", "calls": calls}
    newest_ts = max(iso_to_ms(x["timestamp"]) for x in newest)
    oldest_in_page = min(iso_to_ms(x["timestamp"]) for x in newest)
    next_before = (newest_body.get("result") or {}).get("nextBefore")

    save_fixture("live_candles_1m.json", "GET /api/v1/candles", "default", 200,
                 newest_body, c.last_headers,
                 f"{LIQUID} 1m x{len(newest)} 라이브 캡처", match={"interval": "1m"})

    # `before` 가 UTC 오프셋을 받아주는지 확인 (우리는 ms→UTC ISO 로 직렬화한다)
    utc_probe, _ = await _candles_at(c, LIQUID, "1m", newest_ts - 3_600_000, count=2)
    calls += 1
    utc_offset_ok = bool(utc_probe)

    # before 경계가 inclusive 인지 (스펙은 inclusive 라고 명시 — 실측으로 확인)
    edge, _ = await _candles_at(c, LIQUID, "1m", newest_ts, count=1)
    calls += 1
    before_inclusive = bool(edge) and iso_to_ms(edge[0]["timestamp"]) == newest_ts

    # 지수 탐색: 며칠 전까지 봉이 나오는가
    hits: dict[int, str] = {}
    misses: list[int] = []
    lo_days, hi_days = 0, None
    for days in (1, 2, 4, 8, 16, 32, 64, 128, 256, 512):
        got, _ = await _candles_at(c, LIQUID, "1m", newest_ts - days * DAY_MS, count=1)
        calls += 1
        if got:
            hits[days] = got[0]["timestamp"]
            lo_days = days
        else:
            misses.append(days)
            hi_days = days
            break

    # 이분 탐색으로 경계 좁히기 (하루 단위)
    if hi_days is not None:
        lo, hi = lo_days, hi_days
        while hi - lo > 1:
            mid = (lo + hi) // 2
            got, _ = await _candles_at(c, LIQUID, "1m", newest_ts - mid * DAY_MS, count=1)
            calls += 1
            if got:
                hits[mid] = got[0]["timestamp"]
                lo = mid
            else:
                misses.append(mid)
                hi = mid
        boundary = {"last_day_with_data": lo, "first_day_without_data": hi}
        verdict = "확인됨"
        note = f"1m 봉은 약 {lo}일 전까지 조회됨 ({lo+1}일 전부터 빈 응답)"
    else:
        boundary = {"last_day_with_data": lo_days, "first_day_without_data": None}
        verdict = "확인됨"
        note = f"512일 전까지도 1m 봉이 반환됨 (상한 미발견, 최소 {lo_days}일 보관)"

    oldest_seen_ms = None
    if hits:
        oldest_seen_ms = min(iso_to_ms(v) for v in hits.values())

    return {
        "verdict": verdict, "note": note, "calls": calls,
        "symbol": LIQUID,
        "newest_page_bars": len(newest),
        "newest_bar_kst": ms_to_iso_kst(newest_ts),
        "page_span_minutes": round((newest_ts - oldest_in_page) / 60000, 1),
        "nextBefore_raw": next_before,
        "before_param_utc_offset_accepted": utc_offset_ok,
        "before_is_inclusive": before_inclusive,
        "retention_probe_days_hit": sorted(hits),
        "retention_probe_days_miss": sorted(misses),
        "boundary_days": boundary,
        "oldest_bar_seen_kst": None if oldest_seen_ms is None else ms_to_iso_kst(oldest_seen_ms),
        "evidence": snippet(trunc_result(newest_body)),
    }


async def probe_candle_1d(c: TossClient) -> dict:
    """1일봉 보관 기간(베이스라인용 200봉 확보 가능 여부)."""
    bars, body = await _candles_at(c, LIQUID, "1d", None, count=200)
    if not bars:
        return {"verdict": "미확인", "note": "1d 응답 비어 있음"}
    ts = sorted(iso_to_ms(x["timestamp"]) for x in bars)
    save_fixture("live_candles_1d.json", "GET /api/v1/candles", "daily", 200, body,
                 c.last_headers, f"{LIQUID} 1d x{len(bars)} 라이브 캡처",
                 match={"interval": "1d"})
    return {
        "verdict": "확인됨",
        "bars": len(bars),
        "oldest_kst": ms_to_iso_kst(ts[0]),
        "newest_kst": ms_to_iso_kst(ts[-1]),
        "span_days": round((ts[-1] - ts[0]) / DAY_MS, 1),
        "nextBefore_raw": (body.get("result") or {}).get("nextBefore"),
        "evidence": snippet(trunc_result(body)),
    }


async def probe_orderbook(c: TossClient) -> dict:
    """미국 `/orderbook` 레벨 수 + 스프레드."""
    out = {}
    saved = False
    for sym in [LIQUID] + SMALL[:2]:
        try:
            body = await c._request("GET", "/api/v1/orderbook", params={"symbol": sym})
        except TossApiError as exc:
            out[sym] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        result = body.get("result") or {}
        bids, asks = result.get("bids") or [], result.get("asks") or []
        if not saved and (bids or asks):
            save_fixture("live_orderbook_us.json", "GET /api/v1/orderbook", "default", 200,
                         body, c.last_headers, f"{sym} 라이브 캡처")
            saved = True
        entry = {
            "bid_levels": len(bids), "ask_levels": len(asks),
            "timestamp": result.get("timestamp"),
            "bids_price_order": _order_of([b.get("price") for b in bids]),
            "asks_price_order": _order_of([a.get("price") for a in asks]),
        }
        if bids and asks:
            best_bid = max(Decimal(str(b["price"])) for b in bids)
            best_ask = min(Decimal(str(a["price"])) for a in asks)
            entry["best_bid"] = str(best_bid)
            entry["best_ask"] = str(best_ask)
            entry["spread"] = str(best_ask - best_bid)
        out[sym] = entry
    levels = [v["bid_levels"] for v in out.values() if isinstance(v.get("bid_levels"), int)]
    if not levels:
        verdict, note = "미확인", "모든 심볼에서 호가 조회 실패"
    elif max(levels) == 0:
        verdict, note = "부분확인", "응답은 오지만 이 세션에서 호가 배열이 비어 있음"
    else:
        verdict = "확인됨"
        note = (f"미국 호가 레벨 수 = {sorted(set(levels))} (관측 심볼 {len(levels)}개)")
    return {"verdict": verdict, "note": note, "per_symbol": out}


def _order_of(prices: list) -> str:
    if len(prices) < 2:
        return "n/a"
    vals = [Decimal(str(p)) for p in prices]
    if all(vals[i] > vals[i + 1] for i in range(len(vals) - 1)):
        return "descending"
    if all(vals[i] < vals[i + 1] for i in range(len(vals) - 1)):
        return "ascending"
    return "unsorted"


async def probe_trades(c: TossClient) -> dict:
    """`/trades` 실제 반환 건수·지연."""
    out = {}
    saved = False
    for sym in [LIQUID] + SMALL[:2]:
        wall = now_ms()
        try:
            body = await c._request("GET", "/api/v1/trades",
                                    params={"symbol": sym, "count": 50})
        except TossApiError as exc:
            out[sym] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        rows = body.get("result") or []
        if not saved and rows:
            save_fixture("live_trades_us.json", "GET /api/v1/trades", "default", 200,
                         body, c.last_headers, f"{sym} 라이브 캡처")
            saved = True
        tss = [iso_to_ms(r["timestamp"]) for r in rows if r.get("timestamp")]
        out[sym] = {
            "requested": 50, "returned": len(rows),
            "newest_lag_s": round((wall - max(tss)) / 1000, 1) if tss else None,
            "span_s": round((max(tss) - min(tss)) / 1000, 1) if len(tss) > 1 else None,
            "order": ("descending" if len(tss) > 1 and tss[0] > tss[-1]
                      else "ascending" if len(tss) > 1 else "n/a"),
            "sub_second_ts": any("." in (r.get("timestamp") or "") for r in rows[:5]),
        }
    counts = [v["returned"] for v in out.values() if isinstance(v.get("returned"), int)]
    if not counts or max(counts) == 0:
        verdict, note = "부분확인", "이 세션에서 체결 내역이 비어 있음 (엔드포인트는 200 응답)"
    else:
        verdict, note = "확인됨", f"count=50 요청 시 실제 반환 {sorted(set(counts))}건"
    return {"verdict": verdict, "note": note, "per_symbol": out}


async def probe_rankings(c: TossClient, polls: int | None = None,
                         gap_s: float | None = None) -> dict:
    """TOP_GAINERS realtime 400 여부 + 거래대금/거래량 realtime 갱신 주기."""
    polls = POLL["rank_polls"] if polls is None else polls
    gap_s = POLL["rank_gap_s"] if gap_s is None else gap_s
    findings: dict = {}

    # 1) TOP_GAINERS / TOP_LOSERS realtime → 400 여부
    for rtype in ("TOP_GAINERS", "TOP_LOSERS"):
        try:
            body = await c._request("GET", "/api/v1/rankings", params={
                "type": rtype, "marketCountry": "US", "duration": "realtime", "count": 10})
            findings[f"{rtype}_realtime"] = {
                "verdict": "확인됨", "http": c.last_status,
                "note": "400 이 아니라 정상 응답 — docs/01 §3.2 전제와 다름!",
                "evidence": snippet(trunc_result(body)),
            }
            save_fixture(f"live_rankings_{rtype.lower()}_realtime.json",
                         "GET /api/v1/rankings", f"{rtype.lower()}_realtime", 200, body,
                         c.last_headers, "realtime 이 허용됨(!)",
                         match={"type": rtype, "duration": "realtime"})
        except SchemaMismatch as exc:
            findings[f"{rtype}_realtime"] = {
                "verdict": "확인됨", "http": c.last_status,
                "note": f"거부됨 — {exc.detail}",
                "evidence": exc.detail,
            }
            save_fixture(f"live_rankings_{rtype.lower()}_realtime_400.json",
                         "GET /api/v1/rankings", f"{rtype.lower()}_realtime_400",
                         c.last_status or 400,
                         {"error": {"requestId": "***MASKED***",
                                    "code": "unsupported-ranking-duration",
                                    "message": exc.detail}},
                         c.last_headers, "realtime 미지원 확인",
                         match={"type": rtype, "duration": "realtime"})

    # 2) realtime 랭킹 갱신 주기 — rankedAt 이 언제 바뀌는지
    for rtype in ("MARKET_TRADING_AMOUNT", "TOSS_SECURITIES_TRADING_AMOUNT"):
        seen: list[tuple[int, str | None, str]] = []
        first_body = None
        for i in range(polls):
            wall = now_ms()
            body = await c._request("GET", "/api/v1/rankings", params={
                "type": rtype, "marketCountry": "US", "duration": "realtime", "count": 100})
            if first_body is None:
                first_body = body
            result = body.get("result") or {}
            rows = result.get("rankings") or []
            seen.append((wall, result.get("rankedAt"),
                         ",".join(r["symbol"] for r in rows[:5])))
            if i < polls - 1:
                await asyncio.sleep(gap_s)
        if first_body is not None:
            save_fixture(f"live_rankings_{rtype.lower()}.json", "GET /api/v1/rankings",
                         "default" if rtype == "MARKET_TRADING_AMOUNT" else rtype.lower(),
                         200, first_body, c.last_headers, "라이브 캡처",
                         match={"type": rtype})
        stamps = [s[1] for s in seen]
        uniq = [s for i, s in enumerate(stamps) if i == 0 or s != stamps[i - 1]]
        deltas = []
        for i in range(1, len(seen)):
            if seen[i][1] and seen[i - 1][1] and seen[i][1] != seen[i - 1][1]:
                deltas.append(round((iso_to_ms(seen[i][1]) - iso_to_ms(seen[i - 1][1])) / 1000, 1))
        rows0 = (first_body.get("result") or {}).get("rankings") or []
        findings[rtype] = {
            "verdict": "확인됨" if stamps and stamps[0] else "부분확인",
            "rows_returned": len(rows0),
            "polls": polls, "gap_s": gap_s,
            "rankedAt_sequence": stamps,
            "distinct_rankedAt": len(set(x for x in stamps if x)),
            "rankedAt_deltas_s": deltas,
            "top5_changed": len(set(s[2] for s in seen)) > 1,
            "rankedAt_lag_s": (round((seen[0][0] - iso_to_ms(stamps[0])) / 1000, 1)
                               if stamps and stamps[0] else None),
            "note": (f"{polls}회 폴링({gap_s}s 간격) 중 rankedAt 이 "
                     f"{len(set(x for x in stamps if x))}개 값으로 관측됨"),
            "evidence": snippet(trunc_result(first_body)),
        }
    return findings


async def probe_stocks(c: TossClient) -> dict:
    """`/stocks` 미국 종목 메타 — sharesOutstanding 등."""
    body = await c._request("GET", "/api/v1/stocks",
                            params={"symbols": ",".join([LIQUID] + SMALL)})
    save_fixture("live_stocks_us.json", "GET /api/v1/stocks", "default", 200, body,
                 c.last_headers, "라이브 캡처 (isinCode 마스킹)")
    rows = body.get("result") or []
    return {
        "verdict": "확인됨",
        "requested": len([LIQUID] + SMALL), "returned": len(rows),
        "missing": sorted(set([LIQUID] + SMALL) - {r.get("symbol") for r in rows}),
        "fields": sorted(rows[0]) if rows else [],
        "evidence": snippet(trunc_result(body)),
    }


async def probe_warnings(c: TossClient) -> dict:
    """미국 종목의 `/stocks/{symbol}/warnings` 실효성."""
    out = {}
    for sym in [LIQUID, SMALL[0]]:
        try:
            body = await c._request("GET", f"/api/v1/stocks/{sym}/warnings")
            out[sym] = {"http": c.last_status, "count": len(body.get("result") or []),
                        "body": snippet(body, 300)}
            save_fixture("live_warnings_us.json", "GET /api/v1/stocks/{symbol}/warnings",
                         "default", 200, body, c.last_headers, f"{sym} 라이브 캡처")
        except TossApiError as exc:
            out[sym] = {"http": c.last_status, "error": f"{type(exc).__name__}: {exc}"}
    empties = [v.get("count") for v in out.values() if "count" in v]
    return {
        "verdict": "확인됨" if empties else "미확인",
        "note": ("미국 종목은 빈 배열 — KRX 전용으로 확인" if empties and max(empties) == 0
                 else "일부 미국 종목에 유의사항 존재"),
        "per_symbol": out,
    }


async def probe_price_limits(c: TossClient) -> dict:
    """미국 종목 상/하한가 (null 예상)."""
    body = await c._request("GET", "/api/v1/price-limits", params={"symbol": LIQUID})
    save_fixture("live_price_limits_us.json", "GET /api/v1/price-limits", "default", 200,
                 body, c.last_headers, f"{LIQUID} 라이브 캡처")
    r = body.get("result") or {}
    return {
        "verdict": "확인됨",
        "upper": r.get("upperLimitPrice"), "lower": r.get("lowerLimitPrice"),
        "timestamp": r.get("timestamp"),
        "evidence": snippet(body),
    }


async def probe_fx(c: TossClient) -> dict:
    """`/exchange-rate` 실제 스프레드 관측 (rate vs midRate)."""
    body = await c._request("GET", "/api/v1/exchange-rate",
                            params={"baseCurrency": "USD", "quoteCurrency": "KRW"})
    save_fixture("live_exchange_rate.json", "GET /api/v1/exchange-rate", "default", 200,
                 body, c.last_headers, "라이브 캡처")
    r = body.get("result") or {}
    out = {"verdict": "확인됨", "evidence": snippet(body)}
    try:
        rate, mid = Decimal(str(r["rate"])), Decimal(str(r["midRate"]))
        out.update({
            "rate": str(rate), "midRate": str(mid),
            "basisPoint": str(r.get("basisPoint")),
            "spread_krw": str(rate - mid),
            "spread_pct": str(((rate - mid) / mid * 100).quantize(Decimal("0.0001"))),
            "valid_window": [r.get("validFrom"), r.get("validUntil")],
        })
    except (KeyError, ArithmeticError) as exc:
        out["verdict"] = "부분확인"
        out["note"] = f"필드 파싱 실패: {exc}"
    return out


async def probe_commissions(c: TossClient) -> dict:
    """`/commissions` 실제 수수료율 (accountSeq 필요)."""
    acc = await c._request("GET", "/api/v1/accounts")
    save_fixture("live_accounts.json", "GET /api/v1/accounts", "default", 200, acc,
                 c.last_headers, "accountNo/accountSeq 마스킹됨")
    rows = acc.get("result") or []
    if not rows:
        return {"verdict": "미확인", "note": "계좌 목록이 비어 있음 — /commissions 조회 불가"}
    seq = rows[0].get("accountSeq")
    body = await c._request("GET", "/api/v1/commissions",
                            headers={"X-Tossinvest-Account": str(seq)})
    save_fixture("live_commissions.json", "GET /api/v1/commissions", "default", 200,
                 body, c.last_headers, "라이브 캡처")
    per_market = {r.get("marketCountry"): r.get("commissionRate")
                  for r in (body.get("result") or [])}
    us = per_market.get("US")
    note = "수수료율 단위 미상"
    if us is not None:
        d = Decimal(str(us))
        note = (f"US 수수료율 원값 {us} — 퍼센트로 해석 시 편도 {d}% "
                f"(왕복 {d * 2}%), 비율로 해석 시 편도 {d * 100}%. 단위는 문서 미명시")
    return {"verdict": "확인됨", "per_market": per_market, "note": note,
            "evidence": snippet(body)}


async def probe_ratelimit_headers(c: TossClient) -> dict:
    """rate limit 헤더 실제 포맷."""
    await c._request("GET", "/api/v1/prices", params={"symbols": LIQUID})
    md = {k: v for k, v in c.last_headers.items() if k.lower().startswith("x-ratelimit")}
    await c._request("GET", "/api/v1/exchange-rate",
                     params={"baseCurrency": "USD", "quoteCurrency": "KRW"})
    mi = {k: v for k, v in c.last_headers.items() if k.lower().startswith("x-ratelimit")}
    return {
        "verdict": "확인됨" if md else "미확인",
        "MARKET_DATA_headers": md,
        "MARKET_INFO_headers": mi,
        "header_names_exact": sorted(md) or sorted(mi),
        "all_response_headers_sample": sorted(mask_headers(c.last_headers)),
        "note": ("정상 응답에도 X-RateLimit-* 가 실려 옴" if md
                 else "정상 응답에 X-RateLimit-* 없음 — limiter 자기보정 불가"),
    }


async def probe_force_429(c: TossClient, burst: int = 12) -> dict:
    """429 재현 + 그때의 헤더값. MARKET_INFO(3 req/s) 를 limiter 우회로 버스트.

    ⚠️ **의도적으로 라이브 429 를 유발한다.** 같은 시각 컬렉터가 돌고 있으면 같은 자격증명
    = 서버 측 같은 버킷이라 그 페널티를 공유한다. 그래서 `--probe all` 에 포함되지 않고
    `--probe force429` 로 **명시했을 때만** 실행된다 (감사 B-5).
    """
    import httpx

    token = await c.tokens.get()
    got: dict = {"attempts": burst, "429_at": None}
    # limiter 는 의도적으로 우회하지만 **allowlist 는 우회하지 않는다** (감사 B-5).
    # 리포 안에 "이중 차단 우회 예제" 를 남기지 않기 위해 명시적으로 관문을 통과시킨다.
    check_allowed("GET", "/api/v1/exchange-rate")
    async with httpx.AsyncClient(base_url=c.base_url, timeout=10.0,
                                 transport=GuardedTransport()) as raw:
        for i in range(burst):
            resp = await raw.get("/api/v1/exchange-rate",
                                 params={"baseCurrency": "USD", "quoteCurrency": "KRW"},
                                 headers={"Authorization": f"Bearer {token}"})
            if resp.status_code == 429:
                got["429_at"] = i + 1
                got["status"] = 429
                got["headers"] = mask_headers(dict(resp.headers))
                got["retry_after_raw"] = resp.headers.get("Retry-After")
                try:
                    got["body"] = mask(resp.json())
                except ValueError:
                    got["body"] = resp.text[:300]
                save_fixture("live_429_exchange_rate.json", "GET /api/v1/exchange-rate",
                             "rate_limited", 429, got["body"], dict(resp.headers),
                             f"MARKET_INFO 그룹 {i+1}번째 연속 호출에서 429 재현")
                break
    if got["429_at"] is None:
        got["verdict"] = "미확인"
        got["note"] = f"{burst}회 연속 호출로 429 재현 실패 (버스트 허용치가 더 큼)"
    else:
        got["verdict"] = "확인됨"
        got["note"] = (f"MARKET_INFO(공시 3 req/s) 연속 호출 {got['429_at']}회째에 429. "
                       f"Retry-After={got.get('retry_after_raw')}")
    # 페널티 반영 — 이후 프로브가 곧바로 다시 때리지 않도록
    if got.get("retry_after_raw"):
        try:
            c.limiter.on_429("MARKET_INFO", float(got["retry_after_raw"]))
        except ValueError:
            c.limiter.on_429("MARKET_INFO", 2.0)
    await asyncio.sleep(2.0)
    return got


async def probe_empty_symbol(c: TossClient) -> dict:
    """존재하지 않는 심볼 — 에러 형태 확인 (스키마 검증 경로 픽스처)."""
    out = {}
    try:
        body = await c._request("GET", "/api/v1/prices", params={"symbols": "ZZZZNOPE"})
        out = {"http": c.last_status, "verdict": "확인됨",
               "note": "미존재 심볼도 200 + 빈/누락 결과", "evidence": snippet(body)}
        save_fixture("live_prices_unknown_symbol.json", "GET /api/v1/prices",
                     "unknown_symbol", 200, body, c.last_headers,
                     "미존재 심볼 요청 결과")
    except TossApiError as exc:
        out = {"http": c.last_status, "verdict": "확인됨",
               "note": f"미존재 심볼은 {type(exc).__name__}: {exc}"}
    return out


async def probe_candle_deep(c: TossClient) -> dict:
    """1m 보관 상한 심층 확인 + 아주 오래된 구간이 진짜 '1분봉'인지 검증."""
    newest, _ = await _candles_at(c, LIQUID, "1m", None, count=1)
    if not newest:
        return {"verdict": "미확인", "note": "최신 1m 봉 없음"}
    anchor = iso_to_ms(newest[0]["timestamp"])

    hits, misses = {}, []
    for days in (512, 1024, 2048, 4096):
        got, _ = await _candles_at(c, LIQUID, "1m", anchor - days * DAY_MS, count=1)
        if got:
            hits[days] = got[0]["timestamp"]
        else:
            misses.append(days)
            break

    deepest = max(hits) if hits else None
    spacing = None
    sample = []
    if deepest is not None:
        bars, _ = await _candles_at(c, LIQUID, "1m", anchor - deepest * DAY_MS, count=5)
        ts = sorted(iso_to_ms(b["timestamp"]) for b in bars)
        sample = [b["timestamp"] for b in bars]
        if len(ts) > 1:
            gaps = [(ts[i + 1] - ts[i]) // 1000 for i in range(len(ts) - 1)]
            spacing = {"gaps_s": gaps, "is_1m_grid": all(g % 60 == 0 for g in gaps),
                       "all_exactly_60s": all(g == 60 for g in gaps)}

    # 소형주도 같은 기간만큼 1m 봉이 남아 있는지 (유니버스 전체 백필 가능성)
    small_hits = {}
    for days in (30, 180, 365):
        got, _ = await _candles_at(c, SMALL[0], "1m", anchor - days * DAY_MS, count=1)
        if got:
            small_hits[days] = got[0]["timestamp"]

    return {
        "verdict": "확인됨" if hits else "미확인",
        "anchor_kst": ms_to_iso_kst(anchor),
        "deep_days_hit": sorted(hits), "deep_days_miss": sorted(misses),
        "deepest_bar_ts": hits.get(deepest) if deepest else None,
        "deepest_bar_age_days": deepest,
        "spacing_at_deepest": spacing,
        "sample_at_deepest": sample,
        f"smallcap_{SMALL[0]}_1m_hits_days": sorted(small_hits),
        "smallcap_samples": small_hits,
        "note": (f"{LIQUID} 1m 봉이 {deepest}일 전까지 존재" if deepest else "심층 조회 실패"),
    }


async def probe_orderbook_depth(c: TossClient) -> dict:
    """호가 레벨 수가 '항상 1' 인지 유동성 최상위 종목들로 재확인 (설계 전제 검증)."""
    liquid = ["QQQ", "SOXL", "MSFT", "TSLA", "NVDA"]
    per = {}
    for sym in liquid:
        try:
            body = await c._request("GET", "/api/v1/orderbook", params={"symbol": sym})
        except TossApiError as exc:
            per[sym] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        r = body.get("result") or {}
        per[sym] = {"bid_levels": len(r.get("bids") or []),
                    "ask_levels": len(r.get("asks") or []),
                    "timestamp": r.get("timestamp"),
                    "raw": snippet(r, 300)}
    levels = [v["bid_levels"] for v in per.values() if isinstance(v.get("bid_levels"), int)]
    maxlv = max(levels) if levels else 0
    return {
        "verdict": "확인됨" if levels else "미확인",
        "max_levels_observed": maxlv,
        "distinct_level_counts": sorted(set(levels)),
        "note": ("미국 호가는 최우선 1레벨만 제공됨 — 호가창 깊이 기반 지표 불가"
                 if maxlv <= 1 else f"최대 {maxlv} 레벨 관측"),
        "session": "probe 실행 시점 세션은 calendar 프로브 결과 참조",
        "per_symbol": per,
    }


PROBES = {
    "calendar": probe_calendar,
    "candle_deep": probe_candle_deep,
    "orderbook_depth": probe_orderbook_depth,
    "quote_realtime": probe_quote_realtime,
    "candle_retention": probe_candle_retention,
    "candle_1d": probe_candle_1d,
    "orderbook": probe_orderbook,
    "trades": probe_trades,
    "rankings": probe_rankings,
    "stocks": probe_stocks,
    "warnings": probe_warnings,
    "price_limits": probe_price_limits,
    "fx": probe_fx,
    "commissions": probe_commissions,
    "ratelimit": probe_ratelimit_headers,
    "unknown_symbol": probe_empty_symbol,
    "force429": probe_force_429,
}

# `--probe all` 에서 제외되는 프로브 — 명시적으로 이름을 적어야만 실행된다 (감사 B-5).
# force429 는 라이브 429 를 **의도적으로** 유발하고, 같은 자격증명을 쓰는 컬렉터가 돌고 있으면
# 서버 측 같은 버킷의 페널티를 공유한다. 기본 실행에 섞여 있을 물건이 아니다.
OPT_IN_ONLY = frozenset({"force429"})
ALL_PROBES = [n for n in PROBES if n not in OPT_IN_ONLY]


# ------------------------------------------------------------------ main


async def run(args) -> int:
    names = (list(ALL_PROBES) if args.probe == "all"
             else [n.strip() for n in args.probe.split(",") if n.strip()])
    unknown = [n for n in names if n not in PROBES]
    if unknown:
        print(f"unknown probes: {unknown}. available: {sorted(PROBES)}", file=sys.stderr)
        return 2

    global FIXTURE_DIR
    if args.fixture_dir:
        FIXTURE_DIR = Path(args.fixture_dir)
    _IS_LIVE_TARGET[0] = LIVE_HOST_MARKER in args.base_url

    os.environ["TOSS_BASE_URL"] = args.base_url
    tokens = TokenManager(Path(args.keys), Path(args.state), live=args.live)
    limiter = GroupRateLimiter(LIMITS, usage_ratio=args.usage_ratio)
    client = TossClient(args.base_url, tokens, limiter, timeout_s=args.timeout_s)

    results: dict = {
        "meta": {
            "started_ms": now_ms(),
            "started_kst": ms_to_iso_kst(now_ms()),
            "started_et": ms_to_iso_et(now_ms()),
            "base_url": args.base_url,
            "probes": names,
            "usage_ratio": args.usage_ratio,
        }
    }
    try:
        for name in names:
            t0 = time.monotonic()
            before = client.counters["requests"]
            print(f"[probe] {name} ...", file=sys.stderr, flush=True)
            try:
                results[name] = await PROBES[name](client)
            except (TossApiError, RuntimeError) as exc:
                results[name] = {"verdict": "미확인",
                                 "error": f"{type(exc).__name__}: {exc}"}
            results[name]["_calls"] = client.counters["requests"] - before
            results[name]["_elapsed_s"] = round(time.monotonic() - t0, 1)
            print(f"[probe] {name} done in {results[name]['_elapsed_s']}s "
                  f"({results[name]['_calls']} calls)", file=sys.stderr, flush=True)
    finally:
        results["meta"]["counters"] = dict(client.counters)
        results["meta"]["finished_kst"] = ms_to_iso_kst(now_ms())
        await client.aclose()
        tokens.release()

    payload = json.dumps(results, ensure_ascii=False, indent=2)
    for stream in (sys.stdout, sys.stderr):   # Windows 콘솔 기본 cp949 대응
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
        print(f"[probe] wrote {args.out}", file=sys.stderr)
    print(payload)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Toss Open API 라이브 실측 프로브 (W1 전용)")
    ap.add_argument("--probe", default="all",
                    help="쉼표구분 프로브 이름 또는 all")
    ap.add_argument("--list", action="store_true", help="프로브 목록 출력 후 종료")
    ap.add_argument("--keys", default="api_keys", help="client_id/secret 파일 경로")
    # 상대경로 기본값은 CWD 종속이라 워크트리마다 다른 파일을 가리킨다 (감사 A-1).
    # 리스 자체는 이제 자격증명 유도 경로에 잡히지만, 상태파일도 절대경로로 고정한다.
    ap.add_argument("--state", default=str(default_state_path()),
                    help="토큰 상태파일 (절대경로 권장)")
    # 기본값 없음 — 계약 C-9. TOSS_BASE_URL 이 없으면 실행을 거부한다 (감사 M-7).
    ap.add_argument("--base-url", default=os.environ.get("TOSS_BASE_URL"))
    ap.add_argument("--out", default=None, help="결과 JSON 저장 경로")
    ap.add_argument("--fixture-dir", default=None,
                    help="픽스처 저장 경로 (기본 tests/fixtures/live). 드라이런은 임시 경로를 쓸 것")
    ap.add_argument("--timeout-s", type=float, default=15.0)
    ap.add_argument("--usage-ratio", type=float, default=0.7)
    # 기본값은 **안전한 쪽**이다 (감사 B-5 / 리스 사고 방지). live 는 --live 와 TOSS_LIVE=1 이
    # 모두 있을 때만 True 가 된다. 이전에는 TokenManager(live=True) 가 하드코딩이라
    # 리스 보유자가 컬렉터를 돌리는 중에 프로브를 띄우면 토큰을 죽였다.
    ap.add_argument("--live", action="store_true",
                    help="라이브 호출 확인 플래그. TOSS_LIVE=1 과 함께 있어야 실행됨")
    ap.add_argument("--fast", action="store_true",
                    help="폴링 간격 축소 (mock 드라이런 전용 — 라이브 결론에 쓰지 말 것)")
    args = ap.parse_args(argv)

    if args.fast:
        POLL.update(quote_polls=3, quote_gap_s=1.0, rank_polls=2, rank_gap_s=1.0)

    if args.list:
        for n, fn in PROBES.items():
            opt = "  [--probe 로 명시해야 실행]" if n in OPT_IN_ONLY else ""
            print(f"{n:18} {(fn.__doc__ or '').splitlines()[0]}{opt}")
        return 0

    if not args.base_url:
        print("refusing to run: --base-url 또는 TOSS_BASE_URL 이 필요합니다 "
              "(계약 C-9: base URL 에 기본값 없음).", file=sys.stderr)
        return 3

    targets_live_host = LIVE_HOST_MARKER in args.base_url
    env_live = os.environ.get("TOSS_LIVE") == "1"

    # 라이브 발급은 --live 와 TOSS_LIVE=1 이 **둘 다** 있을 때만. 기본은 안전한 쪽이다.
    args.live = bool(args.live and env_live)

    if targets_live_host and not args.live:
        print("refusing to run: 실서버를 대상으로 하면서 라이브 모드가 아닙니다.\n"
              "라이브 실측은 --live 플래그와 TOSS_LIVE=1 이 모두 필요하고, 라이브 리스 보유\n"
              "워커만 실행할 수 있습니다 (계약 C-11 §2·§3).", file=sys.stderr)
        return 3
    if args.live and not targets_live_host:
        # mock 을 상대로 실발급 경로를 태우는 것은 드라이런에서 정상 — 경고만 남긴다.
        print(f"[probe] note: live=True 이지만 대상이 실서버가 아닙니다 ({args.base_url}) "
              "— 드라이런으로 간주합니다.", file=sys.stderr)
    if not args.live:
        print(f"[probe] mock 모드 (live=False, 대상 {args.base_url}). "
              "실토큰 발급을 하지 않습니다.", file=sys.stderr)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())

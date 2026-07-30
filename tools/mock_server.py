"""픽스처 기반 mock 서버 — 소유: W1. 표준 라이브러리만 사용 (계약 C-10).

tests/fixtures/live/*.json 을 서빙. --port 8899.
--inject 429|latency|schema-drift 오류 주입 모드 지원.
다른 모든 워커의 개발 기반이므로 최우선 완성 대상.

사용법
------
    python tools/mock_server.py --port 8899
    python tools/mock_server.py --port 8899 --inject 429
    python tools/mock_server.py --port 8899 --inject latency --inject-latency-s 12
    python tools/mock_server.py --port 8899 --inject schema-drift
    python tools/mock_server.py --port 8899 --strict          # 미수록 심볼은 합성하지 않음

워커 설정:
    TOSS_BASE_URL=http://127.0.0.1:8899   TOSS_LIVE=0

오류 주입은 요청 헤더로도 켤 수 있다 (서버 재시작 없이 케이스별 테스트):
    X-Mock-Inject: 429 | latency | schema-drift | 500 | 401 | 403
    X-Mock-Case:   <fixture case 이름>       # 특정 픽스처 케이스 강제 선택

픽스처 포맷 (tests/fixtures/live/*.json)
--------------------------------------
    {
      "endpoint": "GET /api/v1/prices",   # canonical path ({symbol} 템플릿 사용)
      "case": "us_ok",                    # 케이스 이름. "default" 는 기본 응답
      "status": 200,
      "source": "live" | "synthetic",
      "captured_at_ms": 1785400000000,
      "match": {"symbols": "AAPL"},       # (선택) 쿼리 파라미터 부분 일치 조건
      "headers": {...},                   # (선택) 응답 헤더 추가/덮어쓰기
      "body": {...}                       # 실제 응답 본문
    }

동일 (method, path) 에 여러 케이스가 있으면 `match` 를 만족하는 것 중 조건이 가장 많은 것을,
없으면 case=="default" → status==200 인 첫 케이스 순으로 고른다.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "live"

# 실서버와 동일한 rate limit 그룹/한도 (overview.md "Rate Limits").
GROUP_LIMITS: dict[str, int] = {
    "AUTH": 5, "ACCOUNT": 1, "ASSET": 5, "STOCK": 5, "MARKET_INFO": 3,
    "MARKET_DATA": 10, "MARKET_DATA_CHART": 5, "RANKING": 5, "ORDER_INFO": 6,
}
GROUP_OF: dict[str, str] = {
    "/oauth2/token": "AUTH",
    "/api/v1/prices": "MARKET_DATA",
    "/api/v1/candles": "MARKET_DATA_CHART",
    "/api/v1/trades": "MARKET_DATA",
    "/api/v1/orderbook": "MARKET_DATA",
    "/api/v1/price-limits": "MARKET_DATA",
    "/api/v1/rankings": "RANKING",
    "/api/v1/stocks": "STOCK",
    "/api/v1/stocks/{symbol}/warnings": "STOCK",
    "/api/v1/market-calendar/KR": "MARKET_INFO",
    "/api/v1/market-calendar/US": "MARKET_INFO",
    "/api/v1/exchange-rate": "MARKET_INFO",
    "/api/v1/accounts": "ACCOUNT",
    "/api/v1/commissions": "ORDER_INFO",
}
# 템플릿 경로 (실제 경로 → canonical 변환용)
TEMPLATES = tuple(p for p in GROUP_OF if "{" in p)

BATCH_MAX = 200
MOCK_TOKEN = "mock-access-token"
EXPIRED_TOKEN = "expired"   # 이 토큰을 보내면 401 (AuthExpired 경로 테스트용)


def canonical(path: str) -> str:
    p = urlsplit(path).path
    if len(p) > 1:
        p = p.rstrip("/")
    for tmpl in TEMPLATES:
        parts = re.split(r"\{[^/}]+\}", tmpl)
        rx = re.compile("^" + "[^/]+".join(re.escape(x) for x in parts) + "$")
        if rx.match(p):
            return tmpl
    return p


# ---------------------------------------------------------------- fixtures


class FixtureStore:
    def __init__(self, fixture_dir: Path, strict: bool = False) -> None:
        self.dir = fixture_dir
        self.strict = strict
        self.by_route: dict[tuple[str, str], list[dict]] = {}
        self.prices_by_symbol: dict[str, dict] = {}
        self.stocks_by_symbol: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        self.by_route.clear()
        self.prices_by_symbol.clear()
        self.stocks_by_symbol.clear()
        if not self.dir.is_dir():
            return
        for fp in sorted(self.dir.glob("*.json")):
            try:
                fx = json.loads(fp.read_text(encoding="utf-8"))
            except ValueError as exc:
                print(f"[mock] skip {fp.name}: bad json ({exc})", file=sys.stderr)
                continue
            ep = fx.get("endpoint")
            if not isinstance(ep, str) or " " not in ep:
                print(f"[mock] skip {fp.name}: missing/invalid 'endpoint'", file=sys.stderr)
                continue
            method, _, path = ep.partition(" ")
            fx.setdefault("case", fp.stem)
            fx["_file"] = fp.name
            self.by_route.setdefault((method.upper(), canonical(path)), []).append(fx)
            self._index_batch(method.upper(), canonical(path), fx)

    def _index_batch(self, method: str, path: str, fx: dict) -> None:
        """배치 엔드포인트(/prices, /stocks)의 심볼별 항목을 색인 — 임의 심볼 조회 대응."""
        if method != "GET" or int(fx.get("status", 200)) != 200:
            return
        target = {"/api/v1/prices": self.prices_by_symbol,
                  "/api/v1/stocks": self.stocks_by_symbol}.get(path)
        if target is None:
            return
        result = (fx.get("body") or {}).get("result")
        if isinstance(result, list):
            for item in result:
                if isinstance(item, dict) and isinstance(item.get("symbol"), str):
                    target.setdefault(item["symbol"].upper(), item)

    def pick(self, method: str, path: str, query: dict[str, list[str]],
             case: str | None) -> dict | None:
        candidates = self.by_route.get((method.upper(), canonical(path)))
        if not candidates:
            return None
        if case:
            for fx in candidates:
                if fx.get("case") == case:
                    return fx
            return None
        best, best_score = None, -1
        for fx in candidates:
            match = fx.get("match") or {}
            if not isinstance(match, dict):
                match = {}
            if any(str(query.get(k, [""])[0]) != str(v) for k, v in match.items()):
                continue
            score = len(match) * 10
            if fx.get("case") == "default":
                score += 5
            # 라이브 캡처본이 스펙 기반 합성본을 항상 이긴다 (파일명 정렬에 의존하지 않도록).
            if fx.get("source") == "live":
                score += 2
            if int(fx.get("status", 200)) == 200:
                score += 1
            if score > best_score:
                best, best_score = fx, score
        return best


# ------------------------------------------------------- symbol synthesis


def _seed(symbol: str) -> int:
    return int(hashlib.sha256(symbol.upper().encode()).hexdigest()[:12], 16)


def synth_price(symbol: str, now_ms: int) -> dict:
    """미수록 심볼용 결정적 합성 현재가 (소형주 가격대 $0.10~$25)."""
    cents = 10 + _seed(symbol) % 249_900          # 10 ~ 249,909 cents
    return {
        "symbol": symbol,
        "timestamp": _iso_kst(now_ms - (_seed(symbol) % 5_000)),
        "lastPrice": f"{cents / 100:.2f}",
        "currency": "USD",
    }


def synth_stock(symbol: str) -> dict:
    s = _seed(symbol)
    markets = ("NASDAQ", "NYSE", "AMEX", "US_ETC")
    return {
        "symbol": symbol,
        "name": f"MOCK {symbol}",
        "englishName": f"MOCK {symbol} INC",
        "isinCode": f"US{s % 10**10:010d}",
        "market": markets[s % len(markets)],
        "securityType": "STOCK",
        "isCommonShare": True,
        "status": "ACTIVE",
        "currency": "USD",
        "listDate": "2015-01-05",
        "delistDate": None,
        "sharesOutstanding": str(3_000_000 + s % 400_000_000),
        "leverageFactor": None,
        "koreanMarketDetail": None,
    }


def _iso_kst(ts_ms: int) -> str:
    """KST(+09:00) ISO 8601 — 실서버 응답과 동일한 표기."""
    secs, ms = divmod(int(ts_ms), 1000)
    lt = time.gmtime(secs + 9 * 3600)
    return time.strftime("%Y-%m-%dT%H:%M:%S", lt) + f".{ms:03d}+09:00"


# --------------------------------------------------------------- injection


def apply_schema_drift(body):
    """스키마 드리프트 주입: 필수 필드 제거 + 타입 변경 + 필드 rename."""
    body = copy.deepcopy(body)
    result = body.get("result") if isinstance(body, dict) else None

    def drift(item):
        if not isinstance(item, dict):
            return item
        # 1) 가격 필드를 문자열 decimal → float 로 (계약 C-2 위반 유발)
        for key in ("lastPrice", "closePrice", "price", "rate"):
            if key in item:
                try:
                    item[key] = float(item[key])
                except (TypeError, ValueError):
                    item[key] = None
        # 2) 필수 timestamp 제거
        item.pop("timestamp", None)
        # 3) symbol → ticker 로 rename
        if "symbol" in item:
            item["ticker"] = item.pop("symbol")
        return item

    if isinstance(result, list):
        body["result"] = [drift(x) for x in result]
    elif isinstance(result, dict):
        for key in ("candles", "rankings", "bids", "asks"):
            if isinstance(result.get(key), list):
                result[key] = [drift(x) for x in result[key]]
        drift(result)
    return body


def error_body(code: str, message: str, data=None) -> dict:
    err = {"requestId": "01MOCKREQUESTID0000000", "code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"error": err}


# ----------------------------------------------------------------- handler


class MockHandler(BaseHTTPRequestHandler):
    server_version = "tossmon-mock/1.0"
    protocol_version = "HTTP/1.1"

    # 서버 인스턴스에서 주입되는 설정
    store: FixtureStore
    inject: str | None
    inject_every: int
    inject_latency_s: float
    enforce_limits: bool
    verbose: bool

    # ---- plumbing ----

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if self.verbose:
            sys.stderr.write("[mock] %s %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body, extra_headers: dict | None = None) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Request-Id", "01MOCKREQUESTID0000000")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(raw)

    def _limit_headers(self, path: str, exhausted: bool = False) -> dict:
        group = GROUP_OF.get(canonical(path), "MARKET_DATA")
        limit = GROUP_LIMITS.get(group, 5)
        srv = self.server
        with srv.lock:                                   # type: ignore[attr-defined]
            bucket = srv.buckets.setdefault(group, [float(limit), time.monotonic()])  # type: ignore[attr-defined]
            now = time.monotonic()
            bucket[0] = min(float(limit), bucket[0] + (now - bucket[1]) * limit)
            bucket[1] = now
            if exhausted:
                bucket[0] = 0.0
            elif bucket[0] >= 1.0:
                bucket[0] -= 1.0
            remaining = int(bucket[0])
        return {
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": "0" if exhausted else str(remaining),
            "X-RateLimit-Reset": "1",
        }

    def _effective_inject(self) -> str | None:
        forced = self.headers.get("X-Mock-Inject")
        if forced:
            return forced.strip().lower()
        if not self.inject:
            return None
        srv = self.server
        with srv.lock:                                   # type: ignore[attr-defined]
            srv.counter += 1                             # type: ignore[attr-defined]
            n = srv.counter                              # type: ignore[attr-defined]
        return self.inject if n % max(self.inject_every, 1) == 0 else None

    # ---- routing ----

    def do_POST(self) -> None:  # noqa: N802
        path = canonical(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if path != "/oauth2/token":
            # 실서버와 동일: 미지원 경로는 404 edge-blocked
            self._send(404, error_body("edge-blocked", "요청한 API 경로를 지원하지 않습니다."))
            return
        form = parse_qs(raw.decode("utf-8", "replace"))
        if form.get("grant_type", [""])[0] != "client_credentials":
            self._send(400, {"error": "unsupported_grant_type",
                             "error_description": "Only client_credentials grant type is supported."})
            return
        if not form.get("client_id", [""])[0] or not form.get("client_secret", [""])[0]:
            self._send(400, {"error": "invalid_request",
                             "error_description": "Required parameter is missing."})
            return
        self._send(200, {"access_token": MOCK_TOKEN, "token_type": "Bearer",
                         "expires_in": 86400},
                   self._limit_headers("/oauth2/token"))

    def do_GET(self) -> None:  # noqa: N802
        split = urlsplit(self.path)
        path = canonical(split.path)
        query = parse_qs(split.query, keep_blank_values=True)

        if path == "/__mock__/health":
            self._send(200, {"ok": True, "fixtures": sorted(
                f"{m} {p}" for m, p in self.store.by_route)})
            return

        inject = self._effective_inject()
        if inject == "latency":
            time.sleep(self.inject_latency_s)
        if inject == "429":
            self._send(429, error_body("rate-limit-exceeded", "Rate limit 초당 요청 수를 초과했습니다."),
                       {**self._limit_headers(path, exhausted=True), "Retry-After": "2"})
            return
        if inject == "500":
            self._send(500, error_body("internal-error", "서버 일시 장애."),
                       self._limit_headers(path))
            return

        # 인증 (실서버와 동일한 코드로 응답)
        auth = self.headers.get("Authorization", "")
        if inject == "401" or auth.removeprefix("Bearer ").strip() == EXPIRED_TOKEN:
            self._send(401, error_body("expired-token", "액세스 토큰이 만료되었습니다."),
                       self._limit_headers(path))
            return
        if inject == "403":
            self._send(403, error_body("forbidden", "요청에 필요한 권한이 부족합니다."),
                       self._limit_headers(path))
            return
        if not auth.startswith("Bearer ") or not auth.removeprefix("Bearer ").strip():
            self._send(401, error_body("edge-blocked", "Authorization 헤더가 전달되지 않았습니다."),
                       self._limit_headers(path))
            return

        if path not in GROUP_OF:
            self._send(404, error_body("edge-blocked", "요청한 API 경로를 지원하지 않습니다."))
            return

        if self.enforce_limits:
            hdrs = self._limit_headers(path)
            if hdrs["X-RateLimit-Remaining"] == "0":
                self._send(429, error_body("rate-limit-exceeded", "Rate limit 초당 요청 수를 초과했습니다."),
                           {**hdrs, "Retry-After": "1"})
                return
        else:
            hdrs = self._limit_headers(path)

        status, body = self._resolve(path, query)
        if inject == "schema-drift" and status == 200:
            body = apply_schema_drift(body)
            hdrs = {**hdrs, "X-Mock-Drift": "1"}
        self._send(status, body, hdrs)

    # ---- fixture resolution ----

    def _resolve(self, path: str, query: dict[str, list[str]]) -> tuple[int, dict]:
        case = (self.headers.get("X-Mock-Case") or "").strip() or None

        if path in ("/api/v1/prices", "/api/v1/stocks"):
            batched = self._batch(path, query, case)
            if batched is not None:
                return batched

        fx = self.store.pick("GET", path, query, case)
        if fx is None:
            if case:
                return 404, error_body("mock-case-not-found", f"no fixture case {case!r} for {path}")
            return 404, error_body("mock-fixture-missing",
                                   f"no fixture for GET {path} — capture one into tests/fixtures/live/")
        return int(fx.get("status", 200)), copy.deepcopy(fx.get("body"))

    def _batch(self, path: str, query: dict[str, list[str]],
               case: str | None) -> tuple[int, dict] | None:
        """/prices·/stocks: 요청 심볼 목록대로 응답을 조립한다."""
        if case:
            return None
        raw = query.get("symbols", [""])[0]
        symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
        if not symbols:
            return 400, error_body("invalid-request", "요청이 올바르지 않습니다.",
                                   {"field": "symbols", "constraint": {"min": 1, "max": BATCH_MAX}})
        if len(symbols) > BATCH_MAX:
            return 400, error_body("invalid-request", "요청이 올바르지 않습니다.",
                                   {"field": "symbols", "constraint": {"min": 1, "max": BATCH_MAX}})
        now_ms = int(time.time() * 1000)
        index = (self.store.prices_by_symbol if path == "/api/v1/prices"
                 else self.store.stocks_by_symbol)
        out = []
        for sym in symbols:
            item = index.get(sym)
            if item is not None:
                item = copy.deepcopy(item)
                if path == "/api/v1/prices":
                    # 픽스처 캡처 시각이 아니라 "지금"으로 갱신해 폴링 루프가 진행을 관측하게 한다.
                    item["timestamp"] = _iso_kst(now_ms)
            elif self.store.strict:
                continue
            else:
                item = (synth_price(sym, now_ms) if path == "/api/v1/prices"
                        else synth_stock(sym))
            out.append(item)
        return 200, {"result": out}


# -------------------------------------------------------------------- main


def build_server(port: int, args) -> ThreadingHTTPServer:
    store = FixtureStore(FIXTURE_DIR, strict=args.strict)

    handler = type("BoundMockHandler", (MockHandler,), {
        "store": store,
        "inject": args.inject,
        "inject_every": args.inject_every,
        "inject_latency_s": args.inject_latency_s,
        "enforce_limits": args.enforce_limits,
        "verbose": args.verbose,
    })
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    httpd.lock = threading.Lock()          # type: ignore[attr-defined]
    httpd.counter = 0                      # type: ignore[attr-defined]
    httpd.buckets = {}                     # type: ignore[attr-defined]
    httpd.store = store                    # type: ignore[attr-defined]
    return httpd


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="tossmon fixture-backed mock server (stdlib only)")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--inject", choices=["429", "latency", "schema-drift", "500", "401", "403"],
                    default=None, help="오류 주입 모드")
    ap.add_argument("--inject-every", type=int, default=1,
                    help="N 번째 요청마다 주입 (기본 1 = 매 요청)")
    ap.add_argument("--inject-latency-s", type=float, default=12.0,
                    help="--inject latency 시 지연 초 (기본 12 = 클라이언트 기본 타임아웃 초과)")
    ap.add_argument("--enforce-limits", action="store_true",
                    help="rate limit 을 실제로 강제 (초과 시 429)")
    ap.add_argument("--strict", action="store_true",
                    help="픽스처에 없는 심볼을 합성하지 않음")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    httpd = build_server(args.port, args)
    n = len(httpd.store.by_route)                       # type: ignore[attr-defined]
    print(f"[mock] listening on http://127.0.0.1:{args.port}  "
          f"routes={n} fixtures={FIXTURE_DIR}"
          + (f"  inject={args.inject} every={args.inject_every}" if args.inject else ""),
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

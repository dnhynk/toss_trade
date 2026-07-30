"""엔드포인트 allowlist — 계약 C-4·C-11.

이 목록 밖의 (method, path) 는 TossClient._request 관문에서 ForbiddenEndpoint.
주문/조건주문/계좌변경 계열은 GET 포함 어떤 형태로도 추가 금지.
"""
from __future__ import annotations

ALLOWLIST: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/oauth2/token"),
    ("GET", "/api/v1/prices"),
    ("GET", "/api/v1/candles"),
    ("GET", "/api/v1/trades"),
    ("GET", "/api/v1/orderbook"),
    ("GET", "/api/v1/price-limits"),
    ("GET", "/api/v1/rankings"),
    ("GET", "/api/v1/stocks"),
    ("GET", "/api/v1/stocks/{symbol}/warnings"),
    ("GET", "/api/v1/market-calendar/KR"),
    ("GET", "/api/v1/market-calendar/US"),
    ("GET", "/api/v1/exchange-rate"),
    ("GET", "/api/v1/accounts"),
    ("GET", "/api/v1/commissions"),
})

# rate limit 그룹 매핑 (docs/01 §2)
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

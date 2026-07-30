"""엔드포인트 allowlist — 계약 C-4·C-11.

이 목록 밖의 (method, path) 는 TossClient._request 관문에서 ForbiddenEndpoint.
주문/조건주문/계좌변경 계열은 GET 포함 어떤 형태로도 추가 금지.
"""
from __future__ import annotations

import re

from .errors import ForbiddenEndpoint

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

# 공시 한도 (req/s). config 의 limits 에 없는 그룹만 이 값으로 보강한다 (docs/01 §2 + overview.md).
# ACCOUNT(1/s)·ORDER_INFO(6/s) 는 config.example.yaml 에 없으므로 여기가 유일한 출처.
SPEC_LIMITS: dict[str, float] = {
    "AUTH": 5.0,
    "ACCOUNT": 1.0,
    "ASSET": 5.0,
    "STOCK": 5.0,
    "MARKET_INFO": 3.0,
    "MARKET_DATA": 10.0,
    "MARKET_DATA_CHART": 5.0,
    "RANKING": 5.0,
    "ORDER_INFO": 6.0,
}

# 미지 그룹에 대한 보수적 기본값 (req/s).
DEFAULT_LIMIT: float = 1.0

# 템플릿 경로({symbol} 등)를 실제 경로와 대조하기 위한 정규식.
def _tmpl_regex(tmpl: str) -> re.Pattern[str]:
    parts = re.split(r"\{[^/}]+\}", tmpl)
    return re.compile("^" + "[^/]+".join(re.escape(p) for p in parts) + "$")


_TEMPLATED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (tmpl, _tmpl_regex(tmpl)) for _m, tmpl in sorted(ALLOWLIST) if "{" in tmpl
)


def canonical_path(path: str) -> str:
    """실제 경로 → allowlist 상의 템플릿 경로. 매칭 실패 시 입력을 그대로 반환."""
    p = path.split("?", 1)[0]
    if not p.startswith("/"):
        p = "/" + p
    if len(p) > 1:
        p = p.rstrip("/")
    for tmpl, rx in _TEMPLATED:
        if rx.match(p):
            return tmpl
    return p


def is_allowed(method: str, path: str) -> bool:
    return (method.upper(), canonical_path(path)) in ALLOWLIST


def check_allowed(method: str, path: str) -> str:
    """allowlist 관문. 위반 시 ForbiddenEndpoint. 통과 시 정규화된 템플릿 경로 반환.

    TossClient._request 와 TokenManager 의 토큰 발급이 모두 이 함수를 통과한다 (단일 출처).
    """
    m = method.upper()
    canon = canonical_path(path)
    if (m, canon) not in ALLOWLIST:
        raise ForbiddenEndpoint(f"endpoint not allowlisted: {m} {canon}")
    return canon


def group_of(path: str) -> str:
    return GROUP_OF.get(canonical_path(path), "UNKNOWN")

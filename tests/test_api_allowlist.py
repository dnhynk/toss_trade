"""GET-only 하드 차단 테스트 — 계약 C-4·C-11.

거래(주문/조건주문/계좌변경) 계열은 GET 을 포함해 어떤 메서드로도 나가면 안 된다.
차단은 (1) _request 관문 (2) 전송 레벨 _GuardedTransport 두 겹이어야 한다.
"""
from __future__ import annotations

import pytest

from tests.test_api_support import client, mock_server  # noqa: F401
from tossmon.api import endpoints
from tossmon.api.errors import ForbiddenEndpoint

# 절대 나가면 안 되는 경로 (계약 C-11 §1)
FORBIDDEN_PATHS = [
    "/api/v1/orders",
    "/api/v1/orders/abc123",
    "/api/v1/orders/abc123/cancel",
    "/api/v1/orders/abc123/modify",
    "/api/v1/conditional-orders",
    "/api/v1/conditional-orders/xyz",
    "/api/v1/conditional-orders/xyz/modify",
    "/api/v1/holdings",
    "/api/v1/buying-power",
    "/api/v1/sellable-quantity",
]
ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def test_allowlist_contains_no_trading_endpoint():
    """allowlist 에 거래 계열 경로가 없어야 한다.

    경로 세그먼트 단위로 본다 — `/api/v1/orderbook` 은 시세 조회라 정상이고,
    `/api/v1/orders` 계열만 금지 대상이다.
    """
    banned_segments = {"orders", "conditional-orders", "holdings",
                       "buying-power", "sellable-quantity"}
    for _method, path in endpoints.ALLOWLIST:
        segments = set(path.strip("/").split("/"))
        leaked = segments & banned_segments
        assert not leaked, f"거래 계열이 allowlist 에 있다: {path} ({leaked})"


@pytest.mark.parametrize("path", FORBIDDEN_PATHS)
@pytest.mark.parametrize("method", ALL_METHODS)
def test_check_allowed_blocks_trading(method, path):
    with pytest.raises(ForbiddenEndpoint):
        endpoints.check_allowed(method, path)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_read_endpoints_are_get_only(method):
    """읽기 엔드포인트라도 GET 이외의 메서드는 차단된다."""
    with pytest.raises(ForbiddenEndpoint):
        endpoints.check_allowed(method, "/api/v1/prices")


def test_only_token_endpoint_allows_post():
    posts = {p for m, p in endpoints.ALLOWLIST if m == "POST"}
    assert posts == {"/oauth2/token"}


def test_templated_path_canonicalisation():
    assert endpoints.canonical_path("/api/v1/stocks/AAPL/warnings") == \
        "/api/v1/stocks/{symbol}/warnings"
    assert endpoints.canonical_path("/api/v1/stocks/BRK.B/warnings") == \
        "/api/v1/stocks/{symbol}/warnings"
    assert endpoints.check_allowed("GET", "/api/v1/stocks/AAPL/warnings")
    # 한 칸 더 깊은 경로는 템플릿에 매칭되면 안 된다
    with pytest.raises(ForbiddenEndpoint):
        endpoints.check_allowed("GET", "/api/v1/stocks/AAPL/warnings/extra")


def test_query_string_and_trailing_slash_do_not_bypass():
    with pytest.raises(ForbiddenEndpoint):
        endpoints.check_allowed("POST", "/api/v1/orders?dry=1")
    with pytest.raises(ForbiddenEndpoint):
        endpoints.check_allowed("POST", "/api/v1/orders/")
    assert endpoints.check_allowed("GET", "/api/v1/prices?symbols=AAPL") == "/api/v1/prices"


@pytest.mark.parametrize("path", FORBIDDEN_PATHS[:4])
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
async def test_request_gate_blocks_trading(client, method, path):  # noqa: F811
    """관문 1겹: TossClient._request."""
    with pytest.raises(ForbiddenEndpoint):
        await client._request(method, path)
    assert client.counters["requests"] == 0, "차단된 요청이 전송 카운터를 올렸다"


@pytest.mark.parametrize("path", ["/api/v1/orders", "/api/v1/holdings"])
async def test_transport_gate_blocks_when_request_gate_is_bypassed(client, path):  # noqa: F811
    """관문 2겹: _request 를 우회해 httpx 로 직접 쏴도 전송 레벨에서 막힌다."""
    with pytest.raises(ForbiddenEndpoint):
        await client._http.get(path)
    with pytest.raises(ForbiddenEndpoint):
        await client._http.post(path, json={"side": "BUY"})


async def test_allowed_read_still_works(client):  # noqa: F811
    prices = await client.get_prices(["AAPL"])
    assert prices and prices[0].symbol == "AAPL"

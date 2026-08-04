"""레이트리밋 **계약** 회귀 테스트 — 2026-08-04 실측 확정분 (docs/06 §9-3~§9-6).

이 파일이 지키는 것은 두 가지다.

1. **429 계측이 엉뚱한 응답을 찍지 않는다.**
   `client.last_headers` 는 "마지막 응답" 이라 429 뒤에 200 이 하나만 지나가도 덮어써진다.
   실제로 2026-08-04 운영 로그에 `HTTP-429-DETAIL ... status=200 remaining=9` 가 남았고,
   그 잘못된 근거 위에서 "한도의 1/5 만 쓰는데 왜 429 인가" 를 판별할 수 없었다.
   여기서는 **429 다음에 200 을 연달아 주고**, 기록된 것이 429 쪽인지 검증한다.

2. **서버의 고정 1초 창을 넘기지 않는다.**
   서버 창은 벽시계 초에 정렬된 고정 1초다(실측). 연속 시간 토큰버킷만으로는 경계에서
   2×limit 이 통과한다 — 공시 3 req/s 그룹에 6회를 0.561초 안에 통과시킨 것이 실측이다.
   여기서는 **어떤 1초 구간에도 공시 한도를 넘는 호출이 들어가지 않음**을 검증한다.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from tossmon.api import client as client_mod
from tossmon.api.client import TossClient, _retry_after
from tossmon.api.errors import RateLimited
from tossmon.api.limiter import (
    WINDOW_HORIZON_S,
    GroupRateLimiter,
)
from tossmon.api.tokens import TokenManager

LIMITS = {"AUTH": 5, "STOCK": 5, "MARKET_DATA": 10,
          "MARKET_DATA_CHART": 5, "RANKING": 5, "MARKET_INFO": 3}

PRICES_OK = {"result": [{"symbol": "AAPL", "lastPrice": "1.00", "timestamp": None}]}
RL_BODY = {"error": {"requestId": "x", "code": "rate-limit-exceeded",
                     "message": "요청 한도를 초과했습니다."}}


def _scripted_client(tmp_path, responses, usage_ratio: float = 0.7) -> TossClient:
    """정해진 순서대로 응답을 돌려주는 client. 전송 계층만 갈아끼운다."""
    tokens = TokenManager(keys_path=tmp_path / "api_keys",
                          state_path=tmp_path / "token_state.json", live=False)
    c = TossClient("http://stub", tokens, GroupRateLimiter(LIMITS, usage_ratio))
    seq = list(responses)
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        status, headers, body = seq[min(len(sent) - 1, len(seq) - 1)]
        return httpx.Response(status, headers=headers, json=body)

    c._http = httpx.AsyncClient(base_url="http://stub",
                                transport=httpx.MockTransport(handler))
    c.sent_requests = sent          # type: ignore[attr-defined]
    return c


# --------------------------------------------------------------- 429 계측


async def test_last_429_records_the_429_response_not_the_following_200(
        tmp_path, monkeypatch):
    """429 → 200 순서로 줘도 429 기록이 200 으로 덮어써지면 안 된다.

    이것이 2026-08-04 오독의 정확한 재현이다: 그때 남은 기록은 `status=200,
    remaining=9` 였고 그것은 **429 뒤에 성공한 재시도**의 헤더였다.
    """
    monkeypatch.setattr(client_mod, "DEFAULT_RETRY_AFTER_S", 0.0)
    c = _scripted_client(tmp_path, [
        (429, {"x-ratelimit-limit": "5", "x-ratelimit-remaining": "0",
               "x-ratelimit-reset": "1", "retry-after": "0",
               "x-request-id": "req-429"}, RL_BODY),
        (200, {"x-ratelimit-limit": "10", "x-ratelimit-remaining": "9",
               "x-ratelimit-reset": "1", "x-request-id": "req-200"}, PRICES_OK),
    ])
    try:
        out = await c.get_prices(["AAPL"])
        assert len(out) == 1                       # 재시도가 성공했다

        # 함정: 마지막 응답 기준 진단은 200 을 가리킨다 (예전 계측이 읽던 값).
        assert c.last_status == 200
        assert c.last_headers["x-ratelimit-remaining"] == "9"

        # 진짜 429 기록은 그대로 남아 있어야 한다.
        rec = c.last_429
        assert rec is not None, "429 를 받았는데 기록이 없다"
        assert rec["status"] == 429
        assert rec["headers"]["x-ratelimit-remaining"] == "0"
        assert rec["headers"]["x-ratelimit-limit"] == "5"
        assert rec["headers"]["x-request-id"] == "req-429"
        assert "rate-limit-exceeded" in rec["error_code"]
        assert rec["retry_after_present"] is True
        assert rec["group"] == "MARKET_DATA"
        assert rec["path"] == "/api/v1/prices"
        assert c.counters["http_429"] == 1
    finally:
        await c.aclose()


async def test_429_evidence_rides_on_the_exception(tmp_path, monkeypatch):
    """재시도까지 실패해 예외로 나가는 경로에서도 근거가 함께 올라와야 한다."""
    monkeypatch.setattr(client_mod, "DEFAULT_RETRY_AFTER_S", 0.0)
    c = _scripted_client(tmp_path, [
        (429, {"x-ratelimit-limit": "3", "x-ratelimit-remaining": "0",
               "retry-after": "0"}, RL_BODY),
    ])
    try:
        with pytest.raises(RateLimited) as ei:
            await c.get_prices(["AAPL"])
        assert ei.value.evidence["status"] == 429
        assert "rate-limit-exceeded" in ei.value.evidence["error_code"]
    finally:
        await c.aclose()


async def test_missing_retry_after_is_counted_not_silently_defaulted(
        tmp_path, monkeypatch):
    """Retry-After 가 **없는** 429 가 실측된다 (2026-08-04). 조용히 넘어가면 안 된다."""
    monkeypatch.setattr(client_mod, "DEFAULT_RETRY_AFTER_S", 0.0)
    c = _scripted_client(tmp_path, [
        (429, {"x-ratelimit-limit": "5", "x-ratelimit-remaining": "4"}, RL_BODY),
        (200, {"x-ratelimit-limit": "10", "x-ratelimit-remaining": "9"}, PRICES_OK),
    ])
    try:
        await c.get_prices(["AAPL"])
        assert c.counters["http_429_no_retry_after"] == 1
        assert c.last_429["retry_after_present"] is False
    finally:
        await c.aclose()


def test_absent_retry_after_defaults_to_one_full_server_window():
    """헤더가 없으면 1.0초 — 고정 1초 창에서는 어느 위상에서든 다음 창으로 넘어간다."""
    assert _retry_after({}) == pytest.approx(1.0)
    assert _retry_after({"retry-after": "2"}) == pytest.approx(2.0)
    assert _retry_after({"Retry-After": " 3 "}) == pytest.approx(3.0)
    # HTTP-date 형식(스펙상 가능, 실측 미관측)도 해석하되 상한을 넘지 않는다.
    assert 0.0 <= _retry_after({"retry-after": "Tue, 04 Aug 2026 02:30:13 GMT"}) \
        <= client_mod.MAX_RETRY_AFTER_S


async def test_429_below_our_own_send_rate_is_flagged(tmp_path, monkeypatch):
    """그 서버 초에 우리가 한도만큼 쏘지 않았는데 429 면 원인이 밖에 있다는 뜻이다.

    2026-08-04 운영 429 가 정확히 이 모양이었다 (limit=5 인데 우리 CHART 사용률은 0.5/s).
    카운터로 드러나야 W4 가 "우리 예산 문제" 와 "다른 발신자/다른 한도" 를 구분할 수 있다.
    """
    monkeypatch.setattr(client_mod, "DEFAULT_RETRY_AFTER_S", 0.0)
    date = "Tue, 04 Aug 2026 02:30:13 GMT"
    c = _scripted_client(tmp_path, [
        (429, {"x-ratelimit-limit": "5", "x-ratelimit-remaining": "4",
               "date": date}, RL_BODY),
        (200, {"x-ratelimit-limit": "5", "x-ratelimit-remaining": "4",
               "date": date}, PRICES_OK),
    ])
    try:
        await c.get_prices(["AAPL"])
        rec = c.last_429
        assert rec["own_requests_in_that_server_second"] == 1
        assert rec["limit_header"] == 5
        assert rec["under_own_limit"] is True
        assert c.counters["http_429_under_own_limit"] == 1
    finally:
        await c.aclose()


async def test_recent_429s_keeps_the_first_shot(tmp_path, monkeypatch):
    """사고가 여러 발이면 첫 발이 가장 중요하다 — 하나만 들고 있으면 그게 밀려난다."""
    monkeypatch.setattr(client_mod, "DEFAULT_RETRY_AFTER_S", 0.0)
    c = _scripted_client(tmp_path, [
        (429, {"x-ratelimit-limit": "5", "x-ratelimit-remaining": "0",
               "retry-after": "0", "x-request-id": "first"}, RL_BODY),
        (429, {"x-ratelimit-limit": "5", "x-ratelimit-remaining": "0",
               "retry-after": "0", "x-request-id": "second"}, RL_BODY),
    ])
    try:
        with pytest.raises(RateLimited):
            await c.get_prices(["AAPL"])
        ids = [r["headers"].get("x-request-id") for r in c.recent_429s]
        assert ids == ["first", "second"]
    finally:
        await c.aclose()


# ----------------------------------------------------- 고정 1초 창 하드캡


def _max_in_any_window(stamps: list[float], window_s: float) -> int:
    """어떤 window_s 구간에도 들어간 최대 호출 수."""
    worst = 0
    for i, start in enumerate(stamps):
        n = sum(1 for t in stamps[i:] if t < start + window_s)
        worst = max(worst, n)
    return worst


async def test_never_exceeds_declared_limit_in_any_one_second_window():
    """어떤 1초 구간에도 공시 한도를 넘지 않는다 (고정창 경계 2배 통과 방지).

    usage_ratio=1.0 은 옛 구조에서 최악이었다: capacity(1.0) + rate(3) = 4 가
    한 초에 통과할 수 있었고, 그게 서버의 고정 창 하나에 몰리면 곧바로 429다.
    """
    lim = GroupRateLimiter({"G": 3.0}, usage_ratio=1.0)
    stamps: list[float] = []
    t_end = time.monotonic() + 2.6
    while time.monotonic() < t_end:
        await lim.acquire("G")
        stamps.append(time.monotonic())
    assert len(stamps) >= 5, "표본이 너무 적어 판정할 수 없다"
    worst = _max_in_any_window(stamps, 1.0)
    assert worst <= 3, f"1초 구간에 {worst}회 — 공시 한도 3 초과"


async def test_burst_after_idle_respects_the_window_cap():
    """유휴 뒤 한꺼번에 몰려도 캡을 넘지 않는다 (여러 루프가 같은 순간에 깨는 경우)."""
    lim = GroupRateLimiter({"G": 3.0}, usage_ratio=1.0)
    await asyncio.sleep(1.3)                       # 버킷을 채운 뒤
    stamps: list[float] = []

    async def one():
        await lim.acquire("G")
        stamps.append(time.monotonic())

    await asyncio.gather(*(one() for _ in range(6)))
    assert _max_in_any_window(stamps, 1.0) <= 3


async def test_window_cap_is_the_server_limit_not_the_budget():
    """하드캡은 공시 한도 그대로다 — usage_ratio 를 곱해 작은 그룹을 손해보게 하지 않는다."""
    lim = GroupRateLimiter({"MARKET_INFO": 3.0}, usage_ratio=0.7)
    snap = lim.snapshot("MARKET_INFO")
    assert snap["window_cap"] == 3.0
    assert snap["rate"] == pytest.approx(2.1)      # 예산은 버킷이 맡는다
    assert snap["window_horizon_s"] == pytest.approx(WINDOW_HORIZON_S)


def test_sustained_rate_reports_the_binding_constraint():
    """W4 예산 모델이 읽을 값. 기본 설정에서는 버킷이, 사용률을 올리면 하드캡이 병목."""
    low = GroupRateLimiter({"MARKET_DATA": 10.0}, usage_ratio=0.7)
    assert low.sustained_rate("MARKET_DATA") == pytest.approx(7.0)

    high = GroupRateLimiter({"MARKET_DATA": 10.0}, usage_ratio=1.0)
    # 하드캡(10 / 1.15s) 이 병목이 된다 — 버킷 rate 10 이 그대로 나오면 안 된다.
    assert high.sustained_rate("MARKET_DATA") == pytest.approx(10.0 / WINDOW_HORIZON_S)
    assert high.sustained_rate("MARKET_DATA") < 10.0


def test_small_limit_groups_no_longer_overshoot_by_capacity_floor():
    """limit ≤ 3 그룹의 옛 결함: capacity 하한 1.0 때문에 1초 통과량이 한도를 넘었다.

    MARKET_INFO 는 1.0 + 2.1 = 3.1 > 3, ACCOUNT 는 1.0 + 0.7 = 1.7 > 1 이었다.
    하드캡이 그 위를 덮는다.
    """
    lim = GroupRateLimiter({"MARKET_INFO": 3.0}, usage_ratio=0.7)
    for group, cap in (("MARKET_INFO", 3.0), ("ACCOUNT", 1.0)):
        snap = lim.snapshot(group)
        assert snap["capacity"] + snap["rate"] > cap, "옛 결함 전제가 바뀌었다"
        assert snap["window_cap"] == cap


def test_429_remaining_header_is_not_trusted():
    """429 의 `remaining` 은 경계에서 다음 창의 값을 가리킬 수 있다 — 잔량으로 쓰면 안 된다."""
    lim = GroupRateLimiter({"G": 10.0}, usage_ratio=1.0)
    lim.on_429("G", 0.5)
    assert lim.snapshot("G")["tokens"] == 0.0

    # 429 응답이 "아직 4개 남았다" 고 말해도 토큰이 되살아나면 안 된다.
    lim.update_from_headers("G", {"X-RateLimit-Limit": "10",
                                  "X-RateLimit-Remaining": "4"}, status=429)
    assert lim.snapshot("G")["tokens"] == 0.0
    assert lim.snapshot("G")["blocked_for_s"] > 0.0


def test_200_remaining_header_still_clamps_down():
    """반대로 정상 응답의 잔량은 계속 우리 추정의 상한으로 쓴다 (기존 계약 유지)."""
    lim = GroupRateLimiter({"G": 10.0}, usage_ratio=1.0)
    lim._bucket("G").tokens = 9.0
    lim.update_from_headers("G", {"X-RateLimit-Limit": "10",
                                  "X-RateLimit-Remaining": "2"}, status=200)
    assert lim.snapshot("G")["tokens"] == pytest.approx(2.0)

"""GroupRateLimiter 테스트 — 계약 C-4 (토큰버킷·헤더 자기보정·429 백오프)."""
from __future__ import annotations

import asyncio
import time

import pytest

from tossmon.api.limiter import MAX_BACKOFF, GroupRateLimiter

LIMITS = {"AUTH": 5, "STOCK": 5, "MARKET_DATA": 10,
          "MARKET_DATA_CHART": 5, "RANKING": 5, "MARKET_INFO": 3}


def test_effective_rate_is_declared_limit_times_usage_ratio():
    lim = GroupRateLimiter(LIMITS, usage_ratio=0.7)
    assert lim.snapshot("MARKET_DATA")["rate"] == pytest.approx(7.0)
    assert lim.snapshot("MARKET_INFO")["rate"] == pytest.approx(2.1)


def test_unknown_group_falls_back_to_spec_limit_not_unlimited():
    """config 에 없는 그룹(ACCOUNT=1/s, ORDER_INFO=6/s)도 무제한이 되면 안 된다."""
    lim = GroupRateLimiter(LIMITS, usage_ratio=0.7)
    assert lim.snapshot("ACCOUNT")["rate"] == pytest.approx(0.7)
    assert lim.snapshot("ORDER_INFO")["rate"] == pytest.approx(4.2)
    assert lim.snapshot("TOTALLY_UNKNOWN")["rate"] == pytest.approx(0.7)


async def test_acquire_throttles_to_configured_rate():
    """버킷을 비운 뒤의 획득 속도가 실효 rate 를 넘지 않아야 한다."""
    lim = GroupRateLimiter({"G": 10.0}, usage_ratio=0.7)      # 실효 7 req/s
    for _ in range(7):                                        # 초기 버스트 소진
        await lim.acquire("G")
    t0 = time.monotonic()
    for _ in range(7):
        await lim.acquire("G")
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.85, f"7req 를 {elapsed:.2f}s 에 통과 — 7 req/s 상한 초과"


async def test_concurrent_acquires_do_not_exceed_rate():
    lim = GroupRateLimiter({"G": 4.0}, usage_ratio=0.5)       # 실효 2 req/s, capacity 2
    for _ in range(2):
        await lim.acquire("G")
    t0 = time.monotonic()
    await asyncio.gather(*(lim.acquire("G") for _ in range(4)))
    elapsed = time.monotonic() - t0
    assert elapsed >= 1.8, f"동시 4req 가 {elapsed:.2f}s — 직렬화되지 않았다"


async def test_on_429_blocks_for_retry_after_and_backs_off():
    lim = GroupRateLimiter({"G": 10.0})
    lim.on_429("G", 0.4)
    snap = lim.snapshot("G")
    assert snap["tokens"] == 0.0
    assert snap["backoff"] == pytest.approx(2.0)
    assert snap["blocked_for_s"] >= 0.4

    t0 = time.monotonic()
    await lim.acquire("G")
    assert time.monotonic() - t0 >= 0.4, "Retry-After 를 기다리지 않았다"


def test_repeated_429_backoff_is_exponential_and_capped():
    lim = GroupRateLimiter({"G": 10.0})
    seen = []
    for _ in range(6):
        lim.on_429("G", 0.0)
        seen.append(lim.snapshot("G")["backoff"])
    assert seen[:3] == [2.0, 4.0, 8.0]
    assert max(seen) <= MAX_BACKOFF
    assert lim.snapshot("G")["effective_rate"] < lim.snapshot("G")["rate"]


def test_update_from_headers_adopts_server_limit():
    """서버가 공시 한도를 바꾸면 헤더값을 채택한다 (자기보정)."""
    lim = GroupRateLimiter({"MARKET_DATA": 10.0}, usage_ratio=0.7)
    assert lim.snapshot("MARKET_DATA")["rate"] == pytest.approx(7.0)
    lim.update_from_headers("MARKET_DATA", {
        "X-RateLimit-Limit": "4", "X-RateLimit-Remaining": "3", "X-RateLimit-Reset": "1"})
    assert lim.snapshot("MARKET_DATA")["rate"] == pytest.approx(2.8)
    assert lim.limits["MARKET_DATA"] == 4.0


def test_update_from_headers_is_case_insensitive():
    lim = GroupRateLimiter({"G": 10.0}, usage_ratio=1.0)
    lim.update_from_headers("G", {"x-ratelimit-limit": "2", "x-ratelimit-remaining": "2"})
    assert lim.snapshot("G")["rate"] == pytest.approx(2.0)


def test_update_from_headers_only_lowers_tokens():
    """서버 잔량은 상한으로만 쓴다 — 다른 프로세스가 같은 client 를 쓸 수 있으므로."""
    lim = GroupRateLimiter({"G": 10.0}, usage_ratio=1.0)
    lim.update_from_headers("G", {"X-RateLimit-Limit": "10", "X-RateLimit-Remaining": "2"})
    assert lim.snapshot("G")["tokens"] == pytest.approx(2.0)
    lim.update_from_headers("G", {"X-RateLimit-Limit": "10", "X-RateLimit-Remaining": "9"})
    assert lim.snapshot("G")["tokens"] <= 2.0 + 1e-6, "헤더로 토큰이 늘어났다"


async def test_remaining_zero_blocks_until_reset():
    lim = GroupRateLimiter({"G": 10.0}, usage_ratio=1.0)
    lim.update_from_headers("G", {"X-RateLimit-Limit": "10",
                                  "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0.5"})
    assert lim.snapshot("G")["blocked_for_s"] >= 0.4
    t0 = time.monotonic()
    await lim.acquire("G")
    assert time.monotonic() - t0 >= 0.4


def test_missing_or_garbage_headers_are_ignored():
    lim = GroupRateLimiter({"G": 10.0}, usage_ratio=0.7)
    before = lim.snapshot("G")["rate"]
    lim.update_from_headers("G", {})
    lim.update_from_headers("G", {"X-RateLimit-Limit": "", "X-RateLimit-Remaining": "n/a"})
    lim.update_from_headers("G", {"X-RateLimit-Limit": "abc"})
    assert lim.snapshot("G")["rate"] == pytest.approx(before)


def test_successful_responses_recover_backoff():
    lim = GroupRateLimiter({"G": 10.0})
    lim.on_429("G", 0.0)
    lim.on_429("G", 0.0)
    assert lim.snapshot("G")["backoff"] == pytest.approx(4.0)
    for _ in range(20):
        lim.update_from_headers("G", {"X-RateLimit-Limit": "10", "X-RateLimit-Remaining": "9"})
    assert lim.snapshot("G")["backoff"] == pytest.approx(1.0)

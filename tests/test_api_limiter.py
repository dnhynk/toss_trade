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


async def test_update_from_headers_only_lowers_tokens():
    """서버 잔량은 상한으로만 쓴다 — 다른 프로세스가 같은 client 를 쓸 수 있으므로."""
    lim = GroupRateLimiter({"G": 10.0}, usage_ratio=1.0)
    # 버킷은 빈 상태로 시작하므로(감사 B-1) 먼저 채워야 "내려가는지"를 볼 수 있다.
    # 버킷은 lazy 생성이라 sleep 전에 만들어 두어야 경과 시간이 잡힌다.
    bucket = lim._bucket("G")
    await asyncio.sleep(0.4)
    bucket.refill(time.monotonic())
    filled = lim.snapshot("G")["tokens"]
    assert filled > 0.5, "테스트 전제: 버킷이 어느 정도 차 있어야 한다"

    lim.update_from_headers("G", {"X-RateLimit-Limit": "10", "X-RateLimit-Remaining": "0.2"})
    lowered = lim.snapshot("G")["tokens"]
    assert lowered < filled, "서버 잔량이 우리 추정보다 작은데 내려가지 않았다"

    lim.update_from_headers("G", {"X-RateLimit-Limit": "10", "X-RateLimit-Remaining": "9"})
    assert lim.snapshot("G")["tokens"] <= lowered + 1e-6, "헤더로 토큰이 늘어났다"


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


def test_backoff_does_not_recover_on_success_count(monkeypatch):
    """감사 H-4 회귀: 성공 응답 **건수**로는 429 감속이 풀리면 안 된다.

    수정 전에는 성공 5건이면 backoff 2.0 → 1.0 이라, 초당 7콜 하는 그룹에서
    429 감속의 실효 수명이 약 1초였다. 지수 백오프가 사실상 장식이었다.
    """
    lim = GroupRateLimiter({"G": 10.0})
    lim.on_429("G", 0.0)
    lim.on_429("G", 0.0)
    assert lim.snapshot("G")["backoff"] == pytest.approx(4.0)

    for _ in range(200):
        lim.update_from_headers("G", {"X-RateLimit-Limit": "10", "X-RateLimit-Remaining": "9"})
    assert lim.snapshot("G")["backoff"] == pytest.approx(4.0), \
        "성공 응답 건수만으로 감속이 풀렸다 (감사 H-4 재발)"


def test_backoff_recovers_on_elapsed_time(monkeypatch):
    """회복은 경과 시간 기준이어야 한다."""
    from tossmon.api import limiter as limiter_mod

    lim = GroupRateLimiter({"G": 10.0})
    lim.on_429("G", 0.0)
    lim.on_429("G", 0.0)
    assert lim.snapshot("G")["backoff"] == pytest.approx(4.0)

    b = lim._bucket("G")
    # 회복 간격 2스텝만큼 시간이 흐른 것으로 만든다.
    b.last_recover -= limiter_mod.RECOVER_INTERVAL_S * 2 + 1
    lim.update_from_headers("G", {"X-RateLimit-Limit": "10", "X-RateLimit-Remaining": "9"})
    assert lim.snapshot("G")["backoff"] == pytest.approx(4.0 * 0.8 * 0.8)

    # 충분히 오래 지나면 1.0 으로 완전 회복
    b.last_recover -= limiter_mod.RECOVER_INTERVAL_S * 50
    lim.update_from_headers("G", {"X-RateLimit-Limit": "10", "X-RateLimit-Remaining": "9"})
    assert lim.snapshot("G")["backoff"] == pytest.approx(1.0)

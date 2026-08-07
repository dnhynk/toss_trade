"""리미터의 1초 창 계약 vs `over_limit_1s` 계측 — 어느 쪽이 틀렸나 (docs/45).

운영 로그에 `budget: MARKET_DATA 1초에 11~14회 — 공시 한도 10 초과` ERROR 가 580건
남았다. 그런데 **같은 순간 서버는 MARKET_DATA 에 429 를 주지 않았다** (docs/37 §3.5:
429 250건 중 250건이 `own_within_limit=True`, 벌은 전부 CHART 가 받았다).

후보는 셋이었다:

  (가) 리미터가 진짜로 1초 창을 못 지킨다 — 하드캡에 구멍이 있다
  (나) 계측이 과장한다 — `over_limit_1s` 가 세는 것이 실제 송신이 아니다
  (다) 창 기준이 다르다 — 우리는 보낸 시각, 서버는 도착 시각

이 파일이 고정하는 것:

  1. 하드캡은 **어떤 1초 구간에서도** 공시 한도를 넘기지 않는다 — 한도 10 규모에서도.
     (기존 `test_api_ratelimit_contract.py` 는 한도 3 에서만 고정하고 있었다.)
  2. 429 재시도도 리미터를 **다시** 통과한다 — 하드캡을 우회하는 경로가 아니다.
  3. 그런데 예산 계측은 송신과 1:1 이 아니었다. `_guarded` 의 `finally` 가 부르는
     `sync_rate_limits()` 가 **전역 시도 델타를 클램프 없이** 자기 그룹에 얹고,
     그 사이 남의 그룹은 `after_call` 의 바닥값으로 **또** 계상했다. 같은 호출이
     두 번 계상된다 → `peak_1s` 가 실제 송신보다 커진다.

즉 1·2 가 (가)를 반증하고, 3 이 (나)를 재현했다.

**2026-08-08 (W4, docs/46)**: 3 의 원인 두 개(D1·D2)를 고쳤다. 계상 근거가 `client` 의
**그룹별 송신 카운터**로 바뀌어 남의 송신이 얹히지도, 같은 송신이 두 번 세지지도 않는다.
그래서 아래 3번 테스트는 이제 **고쳐졌음을 지키는 쪽**이다 — 시나리오는 그대로 두고
기대값만 뒤집었다. 계상 쪽 계약은 `tests/test_double_billing.py` 가 전수로 고정한다.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx
import pytest

from tossmon.api import client as client_mod
from tossmon.api.client import TossClient
from tossmon.api.limiter import GroupRateLimiter
from tossmon.api.tokens import TokenManager
from tossmon.collector.budget import (GROUP_CHART, GROUP_MARKET_DATA,
                                      GROUP_RANKING, BudgetGuard)
from tossmon.collector.loops import CollectorContext
from tossmon.collector.notifier import Notifier
from tossmon.store import Store

from .test_collector_helpers import FrozenClock, calendar_dict, make_config, simple_day

LIMITS = {"AUTH": 5, "STOCK": 5, "MARKET_DATA": 10,
          "MARKET_DATA_CHART": 5, "RANKING": 5, "MARKET_INFO": 3}

DAY0 = 1753833600000     # 2026-07-30 00:00:00 UTC (helpers 와 같은 기준)
MIN_MS = 60_000


def _max_in_any_window(stamps: list[float], window_s: float) -> int:
    """어떤 `window_s` 구간에도 들어간 최대 호출 수.

    `BudgetGuard.peak_1s` 와 **같은 반개구간 규약**(t_right - t_left < window)을 쓴다 —
    다른 규약으로 재면 판정이 계측과 어긋나서 비교 자체가 무의미해진다.
    """
    worst = 0
    for i, start in enumerate(stamps):
        n = sum(1 for t in stamps[i:] if t < start + window_s)
        worst = max(worst, n)
    return worst


# --------------------------------------------------------------------------- #
# 1. 하드캡은 MARKET_DATA 규모(한도 10)에서도 1초 창을 지킨다  → (가) 반증
# --------------------------------------------------------------------------- #
async def test_hard_cap_holds_at_market_data_scale_under_saturating_concurrency():
    """한도 10 그룹에 동시 요청을 몰아넣어도 어떤 1초 구간에도 10 을 넘지 않는다.

    `usage_ratio=1.2` 로 **버킷을 일부러 과잉 공급**해 병목에서 빼둔다. 그래야 남는
    제약이 하드캡뿐이라 하드캡만 시험하게 된다 — 기본값(0.85)에서는 버킷이 먼저
    걸려서 하드캡이 발화조차 안 하고, 그러면 "통과"가 아무것도 증명하지 못한다.
    """
    lim = GroupRateLimiter({"MARKET_DATA": 10.0}, usage_ratio=1.2)
    stamps: list[float] = []

    async def one() -> None:
        await lim.acquire("MARKET_DATA")
        stamps.append(time.monotonic())

    await asyncio.gather(*(one() for _ in range(22)))
    stamps.sort()

    worst = _max_in_any_window(stamps, 1.0)
    assert worst <= 10, f"1초 구간에 {worst}회 — 공시 한도 10 초과 (하드캡에 구멍)"
    # 캡이 실제로 발화했는지 확인 — 22건이 순식간에 끝났다면 시험한 것이 없다.
    assert stamps[-1] - stamps[0] >= 1.0, "캡이 걸리지 않았다 — 시나리오가 무효"


def test_hard_cap_bounds_every_one_second_window_by_construction():
    """`WINDOW_HORIZON_S`(1.15s) 캡이 **1.0초 창**을 덮는다는 것을 직접 확인한다.

    증명: 어떤 1.0초 구간 I 의 마지막 송신을 t 라 하면, 송신 시점 t 에서 캡은
    (t-1.15, t] 안의 송신을 cap 개 이하로 보장한다. t ∈ I 이므로 I 의 좌단
    a ≥ t-1.0 > t-1.15 이고, 따라서 I ⊆ (t-1.15, t] — I 안의 송신도 cap 개 이하다.

    아래는 그 결론을 캡 로직 자체로 재현한다 (버킷을 비병목으로 두고 포화 주입).
    """
    from tossmon.api.limiter import _Bucket

    for cap in (1, 3, 5, 10):
        b = _Bucket(rate=1e9, window_cap=cap)      # 버킷은 병목에서 제외
        now = 0.0
        sends: list[float] = []
        for _ in range(cap * 6):
            wait = b.window_wait(now)
            if wait > 0.0:
                now += wait
            b.note_sent(now)
            sends.append(now)
        worst = _max_in_any_window(sends, 1.0)
        assert worst <= cap, f"cap={cap}: 1초 구간에 {worst}회"


# --------------------------------------------------------------------------- #
# 2. 재시도도 리미터를 다시 통과한다 → 하드캡 우회 경로가 아니다  → (가) 반증
# --------------------------------------------------------------------------- #
async def test_retry_after_429_reacquires_the_limiter(tmp_path, monkeypatch):
    """429 재시도가 `acquire()` 를 **다시** 밟는지 — 안 밟으면 캡 밖의 송신이 된다.

    `docs/30` §2 가 "재시도 계상이 허위 첨두를 만든다" 를 고쳤다고 적었지만, 그것은
    **계상** 쪽 수정이었다. 여기서 확인하는 것은 **송신** 쪽이다: 재시도가 캡을
    우회하면 계측과 무관하게 리미터가 진짜로 깨진다.
    """
    monkeypatch.setattr(client_mod, "DEFAULT_RETRY_AFTER_S", 0.0)

    acquired: list[str] = []
    lim = GroupRateLimiter(LIMITS, usage_ratio=0.7)
    real_acquire = lim.acquire

    async def counting_acquire(group: str) -> None:
        acquired.append(group)
        await real_acquire(group)

    lim.acquire = counting_acquire            # type: ignore[method-assign]

    tokens = TokenManager(keys_path=tmp_path / "api_keys",
                          state_path=tmp_path / "token_state.json", live=False)
    c = TossClient("http://stub", tokens, lim)
    sent: list[httpx.Request] = []
    seq = [
        (429, {"x-ratelimit-limit": "10", "retry-after": "0"},
         {"error": {"requestId": "x", "code": "rate-limit-exceeded", "message": "m"}}),
        (200, {"x-ratelimit-limit": "10", "x-ratelimit-remaining": "9"},
         {"result": [{"symbol": "AAPL", "lastPrice": "1.00", "timestamp": None}]}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        status, headers, body = seq[min(len(sent) - 1, len(seq) - 1)]
        return httpx.Response(status, headers=headers, json=body)

    c._http = httpx.AsyncClient(base_url="http://stub",
                                transport=httpx.MockTransport(handler))
    try:
        out = await c.get_prices(["AAPL"])
        assert len(out) == 1                                  # 재시도가 성공했다
        assert len(sent) == 2, "429 재시도가 실제로 일어나지 않았다 — 시나리오 무효"
        assert acquired == ["MARKET_DATA", "MARKET_DATA"], (
            f"송신 2건에 acquire 가 {len(acquired)}건 — 재시도가 하드캡을 우회한다")
        assert c.counters["requests"] == 2
    finally:
        await c.aclose()


# --------------------------------------------------------------------------- #
# 3. 계측은 송신과 1:1 이 아니다 — 같은 호출이 두 그룹에 계상된다  → (나) 재현
# --------------------------------------------------------------------------- #
class _CountingClient:
    """HTTP 시도 수만 세는 최소 client (전송 없음).

    `counters["requests"]` 는 `TossClient._send` 에서 **소켓으로 나가기 직전** 오르는
    전역 카운터다. 그래서 "보냈지만 아직 완료되지 않은" 상태를 이 수로 표현할 수 있다.
    """

    def __init__(self) -> None:
        self.counters = {"requests": 0, "http_429": 0, "retries": 0}
        # 2026-08-08 (docs/46): 실제 `_send` 는 전역 `requests` 와 **그룹별** 송신 수를
        # 같은 자리에서 올린다. 그룹별 쪽이 예산 계상의 유일한 귀속 근거다.
        self.sent_by_group: dict[str, int] = {}
        self.last_headers: dict[str, str] = {}
        self.last_status: int | None = None
        self.last_429: dict | None = None

    def send(self, group: str, n: int = 1) -> None:
        self.counters["requests"] += n
        self.sent_by_group[group] = self.sent_by_group.get(group, 0) + n

    async def get_us_calendar(self, date=None):
        return calendar_dict([simple_day("2026-07-30", DAY0)], 0)


def _build_ctx(tmp_path):
    cfg = make_config(tmp_path)
    store = Store(cfg.store.db_path)
    day = simple_day("2026-07-30", DAY0)
    clock = FrozenClock(day.regular.start_ms + MIN_MS)
    client = _CountingClient()
    ctx = CollectorContext.create(client, store, cfg, notifier=Notifier(console=False),
                                  clock=clock, symbols=())
    ctx.scheduler.calendar = calendar_dict([day], 0)
    ctx.scheduler.fetched_ms = clock.now_ms()
    ctx.session = "regular"
    return ctx, client


def test_sync_rate_limits_books_other_groups_calls_into_market_data(tmp_path):
    """`_guarded` 의 `finally` 가 **남의 그룹 송신**을 MARKET_DATA 로 계상하지 않는다.

    재현하는 실제 순서 (단일 이벤트루프, `loops.py` 그대로):

      1. tier3 가 MARKET_DATA 1건을 보내고 완료 → `after_call(MARKET_DATA)`
      2. 그 코루틴이 DB 를 쓰는 **동안** tier2(CHART) 가 3건을 소켓으로 내보낸다.
         아직 응답 전이라 CHART 의 `after_call` 은 안 돌았다.
      3. tier3 의 `_guarded` 가 끝나며 `finally: sync_rate_limits(MARKET_DATA)`
      4. 뒤늦게 CHART 의 `after_call` 3건이 돈다.

    **2026-08-08 (W4, docs/46): 이 테스트는 원래 결함을 재현하는 쪽이었다.**
    당시 값은 MD 4 / CHART 3 / 합 7 — 실제 송신 4건에 7건 계상이었다. 3 에서 전역 시도
    델타를 클램프 없이 MD 에 얹었고(D1), 4 에서 진짜 주인이 `max(booked, calls)`
    바닥값으로 또 셌다(D2). 지금은 계상 근거가 **그룹별 송신 카운터**뿐이라
    같은 순서에서 MD 1 / CHART 3 / 합 4 가 나온다. 시나리오는 그대로 두고
    기대값만 뒤집어, 결함이 되살아나면 여기서 잡히게 한다.
    """
    ctx, client = _build_ctx(tmp_path)
    budget = ctx.budget

    md_before = budget.counters.get(GROUP_MARKET_DATA, 0)
    chart_before = budget.counters.get(GROUP_CHART, 0)

    client.send(GROUP_MARKET_DATA, 1)                # 1. MARKET_DATA 가 보냈다
    ctx.after_call(GROUP_MARKET_DATA)                #    완료 → 계상

    client.send(GROUP_CHART, 3)                      # 2. CHART 3건이 나갔다 (완료 전)
    ctx.sync_rate_limits(GROUP_MARKET_DATA)          # 3. _guarded finally

    for _ in range(3):                               # 4. 뒤늦은 CHART 완료
        ctx.after_call(GROUP_CHART)

    md_booked = budget.counters.get(GROUP_MARKET_DATA, 0) - md_before
    chart_booked = budget.counters.get(GROUP_CHART, 0) - chart_before

    assert client.counters["requests"] == 4, "실제 송신은 4건이다"
    assert md_booked == 1, (
        f"MARKET_DATA 는 1건만 보냈는데 {md_booked}건 계상 — "
        "sync_rate_limits 가 남의 그룹 송신을 가져갔다 (D1)")
    assert chart_booked == 3, f"CHART 는 3건 보냈는데 {chart_booked}건 계상"
    assert md_booked + chart_booked == client.counters["requests"], (
        f"총 계상 {md_booked + chart_booked} != 실제 송신 "
        f"{client.counters['requests']} — 같은 호출이 두 번 계상된다")


def test_peak_1s_can_exceed_the_limit_although_the_limiter_never_did(tmp_path):
    """송신은 한도를 지켰는데 `over_limit_1s` 가 발화한다 — 계측이 만든 초과.

    송신 시각열은 **진짜 리미터**가 만든다(하드캡 통과). 그 위에 위 이중 계상만
    얹으면 `peak_1s` 가 11 이 된다 — 서버가 본 적 없는 초과다.
    """
    from tossmon.api.limiter import _Bucket

    # (a) 리미터가 실제로 허용한 MARKET_DATA 송신 시각열 (한도 10, 포화 주입).
    b = _Bucket(rate=1e9, window_cap=10)
    now = 0.0
    sends: list[float] = []
    for _ in range(10):
        wait = b.window_wait(now)
        if wait > 0.0:
            now += wait
        b.note_sent(now)
        sends.append(now)
    assert _max_in_any_window(sends, 1.0) <= 10, "전제 위반: 송신이 이미 한도를 넘었다"

    # (b) 같은 창에서 CHART 가 1건 보냈고, 그것이 MARKET_DATA 로 잘못 계상된다.
    class _Clock:
        t = 0.0

        def now_ms(self) -> int:
            return int(self.t * 1000)

    clock = _Clock()
    guard = BudgetGuard({"MARKET_DATA": 10, "MARKET_DATA_CHART": 5, "RANKING": 5},
                        usage_ratio=0.85, clock=clock)
    for t in sends:
        clock.t = t
        guard.on_request(GROUP_MARKET_DATA)
    clock.t = sends[-1]                      # 같은 1초 창 안에서 오귀속이 일어난다
    guard.on_request(GROUP_MARKET_DATA)      # ← 실제로는 CHART 의 송신

    peak = guard.peak_1s(GROUP_MARKET_DATA)
    assert peak == 11, f"peak_1s={peak} — 재현 실패"
    assert guard.over_limit_1s(GROUP_MARKET_DATA) is True, (
        "송신은 10 을 안 넘었는데 계측은 초과라고 말해야 재현이다")


# --------------------------------------------------------------------------- #
# 0단계. `HTTP-429-DETAIL` 이 429 원문을 찍는다 (그래야 (다)를 가를 수 있다)
# --------------------------------------------------------------------------- #
class _Recorder(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.msgs: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.msgs.append(record.getMessage())


def test_http_429_detail_logs_the_429_response_not_the_following_200(tmp_path):
    """429 뒤 재시도가 200 으로 성공해도 진단 줄은 **429 쪽**을 찍어야 한다.

    2026-08-04 운영 로그의 오독을 그대로 재현한다: 그때 남은 줄은
    `HTTP-429-DETAIL group=MARKET_DATA status=200 ... remaining: 9` 였고, 그것은
    429 가 아니라 **뒤따른 성공 재시도**의 헤더였다. 그 때문에 429 응답의 진짜
    `x-ratelimit-*` 를 한 번도 못 봤고, "서버가 그룹 한도를 공유하는가"(가설 (다))를
    확정도 기각도 못 했다 (docs/32 §3.6, docs/37 §3.6).

    이 테스트는 수정 전 코드에서 **빨갛다** — 옛 배선은 `client.last_headers` 를
    읽으므로 `status=200`, `remaining=9` 가 찍힌다.
    """
    ctx, client = _build_ctx(tmp_path)
    rec = _Recorder()
    ctx.notifier.log.addHandler(rec)
    try:
        # CHART 가 429 를 맞았고, 그 뒤 재시도가 200 으로 성공한 직후의 상태.
        client.counters["http_429"] = 1
        client.last_status = 200
        client.last_headers = {"x-ratelimit-limit": "10", "x-ratelimit-remaining": "9"}
        client.last_429 = {
            "group": GROUP_CHART,
            "status": 429,
            "headers": {"x-ratelimit-limit": "5", "x-ratelimit-remaining": "0",
                        "retry-after": "1", "x-request-id": "req-429"},
            "error_code": "rate-limit-exceeded",
            "retry_after_s": 1.0,
            "retry_after_present": True,
            "own_requests_in_that_server_second": 2,
            "limit_header": 5,
            "under_own_limit": True,
            "path": "/api/v1/candles",
        }
        # 호출자는 MARKET_DATA 인데 429 를 맞은 것은 CHART 다 — 귀속을 추정하면 틀린다.
        ctx.sync_rate_limits(GROUP_MARKET_DATA)

        lines = [m for m in rec.msgs if "HTTP-429-DETAIL" in m]
        assert len(lines) == 1, f"진단 줄이 {len(lines)}개"
        msg = lines[0]

        assert "status=429" in msg, f"429 가 아닌 응답을 찍었다: {msg}"
        assert f"group={GROUP_CHART}" in msg, f"429 를 맞은 그룹이 아니다: {msg}"
        assert "'x-ratelimit-remaining': '0'" in msg, f"429 원문 헤더가 없다: {msg}"
        assert "'9'" not in msg, f"뒤따른 200 의 헤더가 섞였다: {msg}"
        # (다)를 가르는 필드들 — 지금까지 로그에 한 번도 없던 것들.
        assert "under_own_limit=True" in msg
        assert "own_in_server_s=2" in msg
        assert "err=rate-limit-exceeded" in msg
        assert "limit_hdr=5" in msg
    finally:
        ctx.notifier.log.removeHandler(rec)

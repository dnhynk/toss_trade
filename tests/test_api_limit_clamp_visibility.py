"""헤더 한도 클램프의 **가시성** — docs/56.

배경: `update_from_headers` 는 서버 헤더를 공시 한도 천장으로 자른다(감사 B-2). 규칙 자체는
옳다 — 헤더 의미가 초당→분당으로 바뀌기만 해도 rate 가 60배 뛰기 때문이다. 그런데 서버가
2026-08-11 12:51 부터 `/api/v1/candles` 에 20/s 를 주는 동안 우리는 5/s 로 돌았고,
**그 사실이 이틀 반 동안 어느 줄에도 안 남았다.** 카운터는 있었지만 아무도 읽지 않았다.

여기서 고정하는 것은 세 가지다:
1. 위로 자른 것(보고 대상)과 아래로 따라간 것(정상)을 **다른 카운터**로 센다.
2. 그 값이 **수집기 텔레메트리 한 줄**에 실린다.
3. 상태가 바뀔 때만 로그 한 줄 — 매 응답마다가 아니다.

그리고 무엇보다: **송신률은 한 건도 달라지지 않는다.** 이건 가시성 태스크다.
"""
from __future__ import annotations

import logging
import time

import pytest

from tests.test_collector_helpers import MIN_MS, FrozenClock, make_config, simple_day
from tossmon.api.limiter import GroupRateLimiter
from tossmon.collector.loops import CollectorContext
from tossmon.collector.notifier import Notifier
from tossmon.store.writer import Store

CHART = "MARKET_DATA_CHART"          # /api/v1/candles — 실제로 잘리고 있는 그룹
DAY0 = 1753_000_000_000


def hdr(limit: str, remaining: str | None = None) -> dict[str, str]:
    return {"X-RateLimit-Limit": limit, "X-RateLimit-Remaining": remaining or limit}


# ======================================================= 1. 방향을 가른 계상


def test_upward_clamp_is_counted_per_group_with_the_size_it_was_cut_from():
    """서버가 천장보다 **높게** 준 것 — 보고 대상. 그룹과 크기가 같이 남아야 한다."""
    lim = GroupRateLimiter({CHART: 5.0}, usage_ratio=0.85)
    for _ in range(3):
        lim.update_from_headers(CHART, hdr("20"), status=200)

    assert lim.counters["limit_header_clamped"] == 3
    assert lim.counters["limit_header_lowered"] == 0, "위로 자른 것이 하향으로 새어 들어갔다"

    st = lim.clamped[CHART]
    assert st["count"] == 3
    assert st["active"] is True
    # "5 로 잘렸다" 와 "20 이 5 로 잘렸다" 는 다른 사실이다 — 둘 다 남아야 한다.
    assert st["header_max"] == pytest.approx(20.0)
    assert st["ceiling"] == pytest.approx(5.0)


def test_normal_downward_header_is_not_reported_as_a_clamp():
    """★ 대조군. 서버가 천장보다 **낮게** 주는 정상 경로에서 보고 대상은 0 이어야 한다.

    이게 무너지면 평상시 정상 동작이 매 응답마다 경보가 된다.
    """
    lim = GroupRateLimiter({CHART: 5.0}, usage_ratio=0.85)
    fired: list[tuple[str, dict]] = []
    lim.on_clamp_change = lambda g, st: fired.append((g, st))

    for _ in range(10):
        lim.update_from_headers(CHART, hdr("3"), status=200)

    assert lim.counters["limit_header_clamped"] == 0, "정상 하향이 보고 대상으로 계상됐다"
    assert lim.counters["limit_header_lowered"] == 10
    assert lim.clamp_report()["limit_header_clamped_groups"] == "-"
    assert fired == [], "정상 하향에서 로그 줄이 나갔다 — 정상 동작이 경보가 된다"
    # 내려가는 방향은 계속 채택돼야 한다 (규칙을 바꾸지 않았다는 확인).
    assert lim.snapshot(CHART)["rate"] == pytest.approx(3.0 * 0.85)


def test_header_equal_to_the_ceiling_is_neither_direction():
    """천장과 같은 값은 자른 것도 내려간 것도 아니다 — 평시 대부분이 여기다."""
    lim = GroupRateLimiter({CHART: 5.0}, usage_ratio=0.85)
    for _ in range(5):
        lim.update_from_headers(CHART, hdr("5"), status=200)
    assert lim.counters["limit_header_clamped"] == 0
    assert lim.counters["limit_header_lowered"] == 0


def test_groups_are_counted_separately():
    """어느 그룹이 잘리는지 모르면 쓸모가 없다."""
    lim = GroupRateLimiter({CHART: 5.0, "MARKET_DATA": 10.0}, usage_ratio=0.85)
    lim.update_from_headers(CHART, hdr("20"), status=200)
    lim.update_from_headers("MARKET_DATA", hdr("4"), status=200)     # 정상 하향

    assert lim.clamped[CHART]["count"] == 1
    assert lim.clamped["MARKET_DATA"]["count"] == 0
    assert lim.clamp_report()["limit_header_clamped_groups"] == f"{CHART}:20>5x1"


# ================================================ 2. 상태 전이에서만 한 줄


def test_clamp_line_fires_on_state_change_only_not_on_every_response():
    """매초 찍으면 그건 로그가 아니라 소음이다 (티어 전이 978/1000줄의 교훈)."""
    lim = GroupRateLimiter({CHART: 5.0}, usage_ratio=0.85)
    fired: list[tuple[str, bool]] = []
    lim.on_clamp_change = lambda g, st: fired.append((g, bool(st["active"])))

    for _ in range(20):                       # 20개 응답이 같은 20/s 헤더를 달고 온다
        lim.update_from_headers(CHART, hdr("20"), status=200)
    assert fired == [(CHART, True)], f"응답마다 줄이 나갔다: {len(fired)}줄"

    for _ in range(5):                        # 서버가 다시 천장 아래로 내려온다
        lim.update_from_headers(CHART, hdr("3"), status=200)
    assert fired == [(CHART, True), (CHART, False)], "멈춘 것이 안 남았다"

    lim.update_from_headers(CHART, hdr("20"), status=200)   # 다시 시작
    assert fired[-1] == (CHART, True)
    assert len(fired) == 3


def test_first_observation_below_the_ceiling_is_silent():
    """기동 직후 정상 상태에서 줄이 나가면 안 된다 (거짓 경보 방지)."""
    lim = GroupRateLimiter({CHART: 5.0}, usage_ratio=0.85)
    fired: list = []
    lim.on_clamp_change = lambda g, st: fired.append(g)
    lim.update_from_headers(CHART, hdr("5"), status=200)
    lim.update_from_headers(CHART, hdr("3"), status=200)
    assert fired == []


def test_a_broken_clamp_logger_cannot_break_the_request_path():
    """관측 코드가 요청을 죽이면 안 된다. 다만 삼킨 사실까지 조용해지지는 않는다."""
    lim = GroupRateLimiter({CHART: 5.0}, usage_ratio=0.85)

    def boom(group: str, st: dict) -> None:
        raise RuntimeError("sink down")

    lim.on_clamp_change = boom
    lim.update_from_headers(CHART, hdr("20"), status=200)      # 예외가 새어 나오면 실패

    assert lim.counters["clamp_log_failures"] == 1
    assert lim.counters["limit_header_clamped"] == 1, "계상까지 함께 죽었다"
    assert lim.limits[CHART] == pytest.approx(5.0), "한도 적용이 콜백 실패에 끌려갔다"


# ============================================ 3. 송신률 불변 (이 태스크의 조건)


def test_clamp_visibility_does_not_move_the_rate_by_one_call():
    """관측을 붙였다고 나가는 속도가 달라지면 실패다.

    금값(golden)으로 못박는다: 천장 5, usage_ratio 0.85 → rate 4.25, window_cap 5,
    sustained_rate min(4.25, 5/1.15=4.348) = 4.25. 서버가 20 을 줘도 전부 그대로다.
    """
    lim = GroupRateLimiter({CHART: 5.0}, usage_ratio=0.85)
    before = lim.snapshot(CHART)

    for _ in range(50):
        lim.update_from_headers(CHART, hdr("20"), status=200)
    after = lim.snapshot(CHART)

    assert before["rate"] == pytest.approx(4.25)
    assert after["rate"] == pytest.approx(4.25), "헤더 20 이 rate 로 새어 들어왔다"
    assert after["capacity"] == pytest.approx(before["capacity"])
    assert after["window_cap"] == pytest.approx(5.0)
    assert after["sustained_rate"] == pytest.approx(4.25)
    assert lim.limits[CHART] == pytest.approx(5.0), "오염된 한도가 기록으로 남았다"


async def test_a_clamped_group_still_sends_at_the_ceiling_rate():
    """숫자만이 아니라 **실제로 나가는 속도**도 그대로여야 한다."""
    lim = GroupRateLimiter({CHART: 5.0}, usage_ratio=1.0)          # 실효 5 req/s
    for _ in range(5):                                             # 초기 버스트 소진
        await lim.acquire(CHART)

    lim.update_from_headers(CHART, hdr("20"), status=200)          # 서버가 20 을 준다
    assert lim.counters["limit_header_clamped"] == 1                # 관측은 됐고

    t0 = time.monotonic()
    for _ in range(5):
        await lim.acquire(CHART)
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.8, f"5req 가 {elapsed:.2f}s — 헤더 20/s 가 송신률로 새어 들어왔다"


# ================================================== 4. 텔레메트리에 실린다


class _LimiterClient:
    """실제 limiter 만 물고 있는 최소 클라이언트 (전송하지 않는다)."""

    def __init__(self, limiter: GroupRateLimiter) -> None:
        self.limiter = limiter
        self.counters = {"http_429": 0, "requests": 0, "retries": 0,
                         "precision_rounded": 0}
        self.last_headers: dict[str, str] = {}

    async def get_prices(self, symbols):
        return []


def _build_ctx(tmp_path, limiter: GroupRateLimiter):
    cfg = make_config(tmp_path)
    store = Store(cfg.store.db_path)
    day = simple_day("2026-07-30", DAY0)
    notifier = Notifier(console=False)
    ctx = CollectorContext.create(_LimiterClient(limiter), store, cfg, notifier=notifier,
                                  clock=FrozenClock(day.regular.start_ms + MIN_MS))
    ctx.session = "regular"
    return ctx


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def test_clamp_lands_on_the_collector_telemetry_line(tmp_path):
    """카운터가 텔레메트리 줄에 **실제로 나와야** 한다 — 없으면 이번과 똑같이 아무도 모른다."""
    lim = GroupRateLimiter({CHART: 5.0}, usage_ratio=0.85)
    ctx = _build_ctx(tmp_path, lim)
    try:
        for _ in range(7):
            lim.update_from_headers(CHART, hdr("20"), status=200)

        tel = ctx.telemetry()
        assert tel["limit_header_clamped"] == 7
        assert tel["limit_header_clamped_groups"] == f"{CHART}:20>5x7"
        assert tel["limit_header_lowered"] == 0

        # 줄은 `k=v` 를 공백으로 잇는다 — 값에 공백이 있으면 파서가 깨진다.
        rendered = " ".join(f"{k}={v}" for k, v in tel.items())
        assert f"limit_header_clamped_groups={CHART}:20>5x7" in rendered
        assert " " not in str(tel["limit_header_clamped_groups"])
    finally:
        ctx.store.close()


def test_clamp_state_change_reaches_the_collector_log(tmp_path):
    """전이 한 줄이 사람이 보는 채널(collector.log)까지 도달해야 한다.

    limiter 가 `logging` 을 직접 쓰면 안 되는 이유이기도 하다 — 컬렉터는 root 핸들러를
    설정하지 않으므로 `tossmon.api.*` 로 찍은 줄은 그 파일에 안 남는다.
    """
    lim = GroupRateLimiter({CHART: 5.0}, usage_ratio=0.85)
    ctx = _build_ctx(tmp_path, lim)
    cap = _Capture()
    ctx.notifier.log.addHandler(cap)
    try:
        for _ in range(4):
            lim.update_from_headers(CHART, hdr("20"), status=200)
        starts = [ln for ln in cap.lines if ln.startswith("LIMIT-CLAMP start")]
        assert len(starts) == 1, f"전이 줄이 {len(starts)}개 (응답은 4개)"
        assert f"group={CHART}" in starts[0]
        assert "server=20.0" in starts[0] and "ceiling=5.0" in starts[0]

        for _ in range(3):
            lim.update_from_headers(CHART, hdr("3"), status=200)
        ends = [ln for ln in cap.lines if ln.startswith("LIMIT-CLAMP end")]
        assert len(ends) == 1 and f"group={CHART}" in ends[0]
    finally:
        ctx.notifier.log.removeHandler(cap)
        ctx.store.close()


def test_telemetry_says_so_when_the_limiter_cannot_be_read(tmp_path):
    """관측치가 조용히 **사라지는** 것이 이 태스크의 원인이다 — 없으면 없다고 적는다."""
    from tossmon.collector.loops import _limiter_clamp

    class _NoLimiter:
        counters: dict[str, int] = {}

    out = _limiter_clamp(_NoLimiter())
    assert out["limit_header_clamped"] == -1
    assert out["limit_header_clamped_groups"] == "n/a"

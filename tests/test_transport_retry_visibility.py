"""전송 계층 재시도의 **가시성** — docs/63.

배경: 2026-08-14 정규장 개장 뒤 첫 70분에 `md_peak_1s`·`chart_peak_1s`·`rank_peak_1s`
가 **셋 다 동시에 0** 인 텔레메트리 창이 두 번 났다. 세 그룹은 리미터 버킷도 락도
따로이므로(`limiter.py:190,313`) 그룹 하나가 굶는 것으로는 셋이 같이 0 이 되지 않는다.
그룹과 무관하게 전부를 세울 수 있는 코드 경로는 둘뿐이고,

  * `_request` 의 TransientHTTP 재시도 (`client.py:285-292`, 최대 3회 + 백오프 0.5/1/2초)
  * 토큰 재발급 (`tokens.py:170-182`) — `_alock` 을 **쥔 채** 15초 타임아웃 POST 를 한다

**둘 다 세는 카운터가 텔레메트리에 한 번도 나온 적이 없었다.** `client.counters` 에만
있고 상태파일에도 없다. 그래서 그 두 창이 "재시도였나 아니었나" 를 사후에 못 갈랐다.

이 파일이 고정하는 것은 둘이다:
1. 세 카운터가 **수집기 텔레메트리 한 줄에 실린다.**
2. 셋은 **프로세스 수명**이므로 `counter_scope` 가 그렇게 선언한다 — 안 그러면
   `retries=0` 이 "재시도가 없었다" 로 읽힌다. 실제로는 "이 프로세스가 뜬 뒤로 없었다"
   이고, 그 오독이 docs/56 §9 다.

**송신률은 한 건도 달라지지 않는다. 이건 가시성 태스크다.**
"""
from __future__ import annotations

import logging

from tests.test_collector_helpers import MIN_MS, FrozenClock, make_config, simple_day
from tossmon.collector.loops import PROC_SCOPED_COUNTERS, CollectorContext
from tossmon.collector.notifier import Notifier
from tossmon.store.writer import Store

DAY0 = 1753_000_000_000

#: `_request` 가 재시도할 때 올리는 이름들 (`client.py`). 여기 적힌 것이 전부 나가야 한다.
RETRY_COUNTERS = ("retries", "http_5xx", "auth_refresh")


class _CountersClient:
    """`client.counters` 만 흉내내는 더블. 송신은 하지 않는다."""

    def __init__(self, **counters: int) -> None:
        self.counters = dict(counters)


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _build_ctx(tmp_path, client) -> CollectorContext:
    cfg = make_config(tmp_path)
    store = Store(cfg.store.db_path)
    day = simple_day("2026-07-30", DAY0)
    ctx = CollectorContext.create(client, store, cfg, notifier=Notifier(console=False),
                                  clock=FrozenClock(day.regular.start_ms + MIN_MS))
    ctx.session = "regular"
    return ctx


def test_transport_retry_counters_reach_the_telemetry_dict(tmp_path):
    """세 값이 텔레메트리에 **실제로 나와야** 한다 — 없으면 08-14 와 똑같이 못 가른다."""
    ctx = _build_ctx(tmp_path, _CountersClient(retries=7, http_5xx=4, auth_refresh=2))
    try:
        data = ctx.telemetry()
        assert data["retries"] == 7
        assert data["http_5xx"] == 4
        assert data["auth_refresh"] == 2
    finally:
        ctx.store.close()


def test_the_counters_land_on_the_emitted_line_not_just_the_dict(tmp_path):
    """딕셔너리에만 있고 줄에 안 실리면 운영에서는 없는 것과 같다."""
    ctx = _build_ctx(tmp_path, _CountersClient(retries=3, http_5xx=3, auth_refresh=0))
    cap = _Capture()
    logger = logging.getLogger("tossmon.collector")
    logger.addHandler(cap)
    prev = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        ctx.report_telemetry(force=True)
        line = next(x for x in cap.lines if x.startswith("telemetry "))
        for name in RETRY_COUNTERS:
            assert f"{name}=" in line, f"`{name}` 가 텔레메트리 줄에 없다: {line[:400]}"
    finally:
        logger.removeHandler(cap)
        logger.setLevel(prev)
        ctx.store.close()


def test_a_missing_client_counter_reads_as_zero_not_as_a_crash(tmp_path):
    """대조군. 옛 client 더블처럼 카운터가 없어도 텔레메트리는 죽지 않는다."""
    ctx = _build_ctx(tmp_path, _CountersClient())
    try:
        data = ctx.telemetry()
        for name in RETRY_COUNTERS:
            assert data[name] == 0
    finally:
        ctx.store.close()


def test_retry_counters_are_declared_process_scoped():
    """★ `retries=0` 이 "재시도 없었다" 인지 "방금 떴다" 인지 줄만 보고 갈릴 수 있어야 한다.

    셋은 `client.counters` 에 있고 `TossClient` 는 기동마다 새로 만들어지므로
    (`__main__.py`) 상태파일로 복원되지 않는다 — 정의상 프로세스 수명이다.
    `counter_scope` 가 이걸 설치 수명이라고 말하면 0 의 뜻이 뒤집힌다 (docs/56 §9).
    """
    for name in RETRY_COUNTERS:
        assert name in PROC_SCOPED_COUNTERS, (
            f"`{name}` 는 재시작으로 0 이 되는데 `counter_scope` 는 설치 수명이라고 "
            f"말한다 — 그러면 0 이 두 가지 뜻을 갖는다")


def test_the_declaration_string_actually_carries_them(tmp_path):
    """선언 목록이 아니라 **줄에 찍히는 문자열**을 본다 — 둘이 갈린 적이 있다."""
    ctx = _build_ctx(tmp_path, _CountersClient(retries=1, http_5xx=1, auth_refresh=1))
    try:
        declared = str(ctx.telemetry()["counter_scope"]).split(";")[0]
        declared = declared[len("proc:"):].split(",")
        for name in RETRY_COUNTERS:
            assert name in declared, f"선언 문자열에 `{name}` 이 없다: {declared}"
    finally:
        ctx.store.close()

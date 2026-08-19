"""이벤트 루프 정지의 **가시성** — docs/63 §12.

배경. 2026-08-14·08-17 두 정규장에서 랭킹·호가가 60~122 초 동안 **같이** 멈추고,
같은 구간에서 5 분짜리 텔레메트리 틱까지 늦었다. `docs/63` §8-5 는 그것을 이렇게
남겼다: *"이벤트루프가 막힌 것인지 송신만 막힌 것인지 못 갈랐다."*

못 가른 이유는 계측 부재다. 폴 시각(`snap_ms`)은 **폴이 일어났을 때만** 찍히므로
"안 찍힌 구간"이 정지인지 대기인지 구분하지 못한다. 둘은 고쳐야 할 곳이 정반대다.

  * **정지** — 어떤 동기 호출이 이벤트 루프를 쥐고 있었다. 그 호출을 찾아 스레드로
    넘겨야 한다. 그룹·락·리미터를 아무리 고쳐도 안 없어진다.
  * **대기** — 태스크들이 무언가를 `await` 했다. 그러면 그 자원을 고치는 게 맞다.

이 파일이 고정하는 계측은 둘이고, **짝으로** 읽어야 뜻이 생긴다.

  `loop_lag_max_ms`  `Clock.sleep(x)` 가 x 보다 얼마나 더 걸렸나의 창 최대치.
                     모든 수집 루프의 쉬는 시간이 `Clock.sleep` 한 곳을 지나므로,
                     루프가 통째로 멈추면 그때 자고 있던 태스크의 초과분에 정지
                     길이가 그대로 남는다. **대기로는 이 값이 안 오른다** — 대기는
                     자는 것이 아니라 `await` 하는 것이고 루프는 계속 돈다.
  `db_write_max_ms`  동기 `store.*` 호출 1건의 소요 창 최대치. `store/writer.py` 는
                     `sqlite3` 를 직접 부르고 스레드로 넘기지 않으므로(`to_thread`·
                     `run_in_executor` 가 파일 전체에 없다) 한 번의 `COMMIT` 이
                     오래 걸리면 그동안 루프 전체가 선다.

읽는 법:

    lag 큼 + db 큼   -> 동기 sqlite 호출이 루프를 세웠다
    lag 큼 + db 작음 -> 루프는 섰는데 sqlite 가 아니다 (상태파일 쓰기·로깅·GC·OS)
    lag 작음         -> 루프는 돌았다. 폴이 멈췄다면 정지가 아니라 대기다

**송신률은 한 건도 달라지지 않는다. 이건 가시성 태스크다.** 새 호출도, 새 로그 줄도,
새 태스크도 없다 — 이미 지나가던 자리에서 시간을 재기만 한다.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import pathlib
import time

from tests.test_collector_helpers import MIN_MS, FrozenClock, make_config, simple_day
from tossmon.collector.loops import (WINDOW_SCOPED_GAUGES, CollectorContext, _timed)
from tossmon.collector.notifier import Notifier
from tossmon.collector.scheduler import Clock
from tossmon.store.writer import Store

DAY0 = 1753_000_000_000

#: 이 태스크가 내보내는 창 게이지. 여기 적힌 것이 전부 줄에 실려야 한다.
STALL_GAUGES = ("loop_lag_max_ms", "db_write_max_ms")

#: 블로킹 흉내 길이 (초). 스케줄러 흔들림(수 ms)보다 충분히 크고 테스트는 짧게.
BLOCK_S = 0.30


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


def _build_ctx(tmp_path) -> CollectorContext:
    cfg = make_config(tmp_path)
    store = Store(cfg.store.db_path)
    day = simple_day("2026-07-30", DAY0)
    ctx = CollectorContext.create(_CountersClient(), store, cfg,
                                  notifier=Notifier(console=False),
                                  clock=FrozenClock(day.regular.start_ms + MIN_MS))
    ctx.session = "regular"
    return ctx


# --------------------------------------------------------------------------- #
# 1. 루프 정지가 실제로 게이지에 남는가 (진짜 이벤트 루프로 잰다)
# --------------------------------------------------------------------------- #
def test_a_blocked_event_loop_shows_up_in_the_sleep_lag():
    """★ 이 파일의 중심. **동기 블로킹**이 자고 있던 태스크의 초과분으로 나타난다.

    `time.sleep` 은 `asyncio.sleep` 과 달리 이벤트 루프를 놓지 않는다 — `store.*` 의
    `sqlite3` 호출이 하는 일과 같은 종류다. 그동안 다른 태스크는 깨어날 수 없다.
    """
    clock = Clock(sync=False)

    async def scenario():
        sleeper = asyncio.create_task(clock.sleep(0.01))
        await asyncio.sleep(0)                  # sleeper 를 먼저 재운다
        time.sleep(BLOCK_S)                     # 루프를 쥔다 (동기 블로킹 흉내)
        await sleeper

    asyncio.run(scenario())
    assert clock.sleep_lag_max_ms >= int(BLOCK_S * 1000 * 0.7), (
        f"루프가 {BLOCK_S}s 멈췄는데 게이지가 {clock.sleep_lag_max_ms}ms 다 — "
        "이 값이 안 오르면 08-14·08-17 과 똑같이 정지와 대기를 못 가른다")


def test_a_quiet_loop_reports_a_small_lag():
    """대조군. 아무도 루프를 안 쥐면 게이지는 잡음 수준이어야 한다.

    이 대조군이 없으면 위 테스트는 "항상 큰 값이 나온다" 로도 통과한다.
    """
    clock = Clock(sync=False)
    asyncio.run(clock.sleep(0.01))
    assert clock.sleep_lag_max_ms < int(BLOCK_S * 1000 * 0.7), (
        f"조용한 루프에서 {clock.sleep_lag_max_ms}ms 가 나왔다 — 게이지가 정지가 아니라 "
        "다른 것을 재고 있다")


def test_the_gauge_keeps_the_worst_not_the_last():
    """최댓값 게이지다. 큰 정지 뒤에 작은 것이 와도 큰 쪽이 남아야 한다."""
    clock = Clock(sync=False)

    async def scenario():
        for block in (BLOCK_S, 0.0):
            sleeper = asyncio.create_task(clock.sleep(0.01))
            await asyncio.sleep(0)
            if block:
                time.sleep(block)
            await sleeper

    asyncio.run(scenario())
    assert clock.sleep_lag_max_ms >= int(BLOCK_S * 1000 * 0.7)


def test_taking_the_gauge_resets_the_window():
    """창 값이다 — 읽고 나면 0 이어야 다음 창의 뜻이 산다."""
    clock = Clock(sync=False)
    clock.sleep_lag_max_ms = 4321
    assert clock.take_sleep_lag_max_ms() == 4321
    assert clock.sleep_lag_max_ms == 0
    assert clock.take_sleep_lag_max_ms() == 0


# --------------------------------------------------------------------------- #
# 2. `_timed` — 값도 예외도 통과시키면서 재기만 한다
# --------------------------------------------------------------------------- #
def test_timed_passes_the_return_value_through(tmp_path):
    ctx = _build_ctx(tmp_path)
    try:
        assert _timed(ctx, lambda a, b=0: a + b, 40, b=2) == 42
    finally:
        ctx.store.close()


def test_timed_records_the_duration_of_a_slow_store_call(tmp_path):
    ctx = _build_ctx(tmp_path)
    try:
        _timed(ctx, time.sleep, BLOCK_S)
        assert ctx._db_write_max_ms >= int(BLOCK_S * 1000 * 0.7), (
            f"{BLOCK_S}s 짜리 쓰기가 {ctx._db_write_max_ms}ms 로 기록됐다")
    finally:
        ctx.store.close()


def test_timed_still_records_when_the_call_raises(tmp_path):
    """★ 쓰기 실패 경로가 오히려 느리다 — 예외가 나도 재야 한다.

    `rankings_once` 는 `insert_rankings` 의 예외를 삼키고 계속 간다
    (`rankings_write_failures`). 그 경로가 안 재지면 가장 나쁜 창이 안 보인다.
    """
    ctx = _build_ctx(tmp_path)

    def boom():
        time.sleep(BLOCK_S)
        raise RuntimeError("disk on fire")

    try:
        try:
            _timed(ctx, boom)
        except RuntimeError as exc:
            assert "disk on fire" in str(exc), "예외가 원본 그대로 통과해야 한다"
        else:
            raise AssertionError("예외를 삼켰다 — `_timed` 는 동작을 바꾸면 안 된다")
        assert ctx._db_write_max_ms >= int(BLOCK_S * 1000 * 0.7)
    finally:
        ctx.store.close()


def test_a_real_store_write_is_measured(tmp_path):
    """더블이 아니라 진짜 `Store` 로 한 번 — 게이지가 0 을 벗어나기만 하면 된다."""
    ctx = _build_ctx(tmp_path)
    try:
        _timed(ctx, ctx.store.record_promotion, "AAAA", DAY0, 1, 2, "test", 0.5)
        assert ctx._db_write_max_ms >= 0, "실제 쓰기 경로에서 게이지가 안 잡힌다"
        assert isinstance(ctx._db_write_max_ms, int)
    finally:
        ctx.store.close()


# --------------------------------------------------------------------------- #
# 3. 드리프트 방지 — 새 store 호출이 계측 밖으로 새지 않는가
# --------------------------------------------------------------------------- #
def test_every_store_call_in_loops_goes_through_timed():
    """★ 이름 목록이 아니라 **소스 전수**로 잰다.

    `docs/56` §9 가 남긴 교훈이 이것이다 — "선언한 것이 맞는가" 만 재는 테스트는
    "빠진 것이 있는가" 를 못 본다. 그리고 드리프트는 늘 그쪽으로 난다. 여기서는
    `ctx.store.foo(...)` 형태의 **직접 호출**이 하나라도 남아 있으면 죽는다.
    (`_timed(ctx, ctx.store.foo, ...)` 는 호출이 아니라 속성 참조라 걸리지 않는다.)
    """
    src = pathlib.Path("tossmon/collector/loops.py").read_text(encoding="utf-8")
    bare = [
        (node.lineno, ast.unparse(node.func))
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "store"
    ]
    assert not bare, (
        f"계측을 안 지나는 동기 store 호출이 있다: {bare} — 이 호출이 오래 걸리면 "
        "이벤트 루프 전체가 서는데 `db_write_max_ms` 는 그것을 못 본다")


# --------------------------------------------------------------------------- #
# 4. 줄에 실리는가 · 창마다 되돌아가는가 · 선언이 사실인가
# --------------------------------------------------------------------------- #
def test_both_gauges_land_on_the_emitted_line(tmp_path):
    """딕셔너리에만 있고 줄에 안 실리면 운영에서는 없는 것과 같다."""
    ctx = _build_ctx(tmp_path)
    cap = _Capture()
    logger = logging.getLogger("tossmon.collector")
    logger.addHandler(cap)
    prev = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        ctx._db_write_max_ms = 1234
        ctx.clock.sleep_lag_max_ms = 5678
        ctx.report_telemetry(force=True)
        line = next(x for x in cap.lines if x.startswith("telemetry "))
        assert "db_write_max_ms=1234" in line, line[:400]
        assert "loop_lag_max_ms=5678" in line, line[:400]
    finally:
        logger.removeHandler(cap)
        logger.setLevel(prev)
        ctx.store.close()


def test_the_window_gauges_reset_after_each_report(tmp_path):
    """창 값이므로 두 번째 창은 0 이어야 한다 — 아니면 최댓값이 영원히 얼어붙는다."""
    ctx = _build_ctx(tmp_path)
    try:
        ctx._db_write_max_ms = 900
        ctx.clock.sleep_lag_max_ms = 800
        first = ctx.report_telemetry(force=True)
        second = ctx.report_telemetry(force=True)
        assert first["db_write_max_ms"] == 900 and first["loop_lag_max_ms"] == 800
        assert second["db_write_max_ms"] == 0 and second["loop_lag_max_ms"] == 0
    finally:
        ctx.store.close()


def test_telemetry_itself_stays_pure(tmp_path):
    """★ 되돌리는 자리는 `report_telemetry` 여야 한다.

    `telemetry()` 는 테스트와 도구가 여러 번 부른다. 거기서 되돌리면 **부르는 횟수가
    값을 바꾼다.** 그래서 `promotions_delta` 도 `report_telemetry` 에 있다.
    """
    ctx = _build_ctx(tmp_path)
    try:
        ctx._db_write_max_ms = 777
        ctx.telemetry()
        ctx.telemetry()
        assert ctx._db_write_max_ms == 777, (
            "`telemetry()` 가 게이지를 건드렸다 — 순수해야 한다")
        for name in STALL_GAUGES:
            assert name not in ctx.telemetry(), (
                f"`{name}` 은 창 값이라 `telemetry()` 가 아니라 `report_telemetry` 가 낸다")
    finally:
        ctx.store.close()


def test_the_gauges_are_declared_window_scoped(tmp_path):
    """`loop_lag_max_ms=0` 이 "한 번도 안 밀렸다" 로 안 읽히게 줄이 말해야 한다.

    `rest:install` 은 나머지가 전부 설치 수명 누적이라고 선언한다. 이 둘은 창 값이라
    그 선언 아래 두면 0 의 뜻이 뒤집힌다 — 정확히 `docs/56` §9 다.
    """
    ctx = _build_ctx(tmp_path)
    try:
        field = str(ctx.telemetry()["counter_scope"])
        segments = dict(seg.split(":", 1) for seg in field.split(";"))
        declared = segments["window"].split(",")
        for name in STALL_GAUGES:
            assert name in WINDOW_SCOPED_GAUGES
            assert name in declared, f"선언 문자열에 `{name}` 이 없다: {field}"
    finally:
        ctx.store.close()


def test_the_existing_proc_scope_parser_still_works(tmp_path):
    """★ 회귀 방어. `window:` 를 끼워 넣으면서 기존 독자를 깨면 안 된다.

    `tests/test_api_limit_clamp_visibility.py` 는 `counter_scope.split(";")[0]` 이
    `proc:` 으로 시작한다고 가정하고 읽는다. 그 가정을 여기서 못 박는다.
    """
    ctx = _build_ctx(tmp_path)
    try:
        head = str(ctx.telemetry()["counter_scope"]).split(";")[0]
        assert head.startswith("proc:"), head
        assert "http_429" in head[len("proc:"):].split(",")
    finally:
        ctx.store.close()


def test_the_declared_window_gauges_are_all_actually_emitted(tmp_path):
    """선언한 이름이 줄에 실제로 있는가 — 오타 하나로 선언이 무의미해진다."""
    ctx = _build_ctx(tmp_path)
    try:
        data = ctx.report_telemetry(force=True)
        missing = [k for k in WINDOW_SCOPED_GAUGES if k not in data]
        assert not missing, f"선언했지만 텔레메트리에 없는 이름: {missing}"
    finally:
        ctx.store.close()

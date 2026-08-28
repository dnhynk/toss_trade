"""수집 루프 단위 — 버퍼·회계·상태복원, 그리고 W1 이 실측한 함정 6종의 대응 검증."""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from tests.test_collector_helpers import (MIN_MS, FrozenClock, ReplayClient,
                                          calendar_dict, make_config, simple_day)
from tossmon.api import models
from tossmon.api.models import (Candle, CandlePage, Price, RankingPage,
                                RankingRow, Trade)
from tossmon.collector import loops
from tossmon.collector.detector import EventEmission
from tossmon.collector.loops import (CollectorContext, RankingBuffer, SymbolBuffer,
                                     candles_frame, tape_stats)
from tossmon.collector.notifier import Notifier
from tossmon.store.writer import Store

DAY0 = 1_785_000_000_000
HOUR_MS = 3_600_000


def bar(ts_ms, close_u=1_000_000, vol_qu=5_000_000, symbol="AAA"):
    return Candle(symbol=symbol, ts_ms=ts_ms, open_u=close_u, high_u=close_u + 1000,
                  low_u=close_u - 1000, close_u=close_u, vol_qu=vol_qu)


# --------------------------------------------------------------------------- #
# 버퍼
# --------------------------------------------------------------------------- #
def test_symbol_buffer_counts_only_new_bars():
    """함정3 대응의 절반: 경계 봉이 다시 와도 **카운팅이 틀어지지 않아야** 한다."""
    buf = SymbolBuffer("AAA")
    page1 = [bar(DAY0 + i * MIN_MS) for i in range(5)]
    assert buf.upsert(page1) == 5
    overlap = [bar(DAY0 + 4 * MIN_MS), bar(DAY0 + 5 * MIN_MS)]
    assert buf.upsert(overlap) == 1                     # 겹친 1개는 새 봉이 아니다
    assert len(buf) == 6
    assert buf.last_ts() == DAY0 + 5 * MIN_MS


def test_symbol_buffer_is_bounded_and_prunable():
    buf = SymbolBuffer("AAA", max_bars=10)
    buf.upsert([bar(DAY0 + i * MIN_MS) for i in range(50)])
    assert len(buf) == 10                                # 링버퍼 상한
    assert buf.last_ts() == DAY0 + 49 * MIN_MS           # 최신 것이 남는다
    assert buf.prune_before(DAY0 + 45 * MIN_MS) == 5
    assert len(buf) == 5


def test_symbol_buffer_frame_dtypes_are_int64():
    buf = SymbolBuffer("AAA")
    buf.upsert([bar(DAY0)])
    df = buf.frame()
    assert list(df.columns) == ["symbol", "ts_ms", "open_u", "high_u", "low_u",
                                "close_u", "vol_qu"]
    assert all(str(df[c].dtype) == "int64" for c in df.columns if c != "symbol")
    assert candles_frame([]).empty


def _page(symbol, rank, amount_u, rtype="TOSS_SECURITIES_TRADING_VOLUME"):
    return RankingPage(ranking_type=rtype, duration="realtime", ranked_at_ms=None,
                       rows=[RankingRow(rank=rank, symbol=symbol, last_u=1_000_000,
                                        base_u=1_000_000, change_rate=0.1,
                                        vol_qu=10, amount_u=amount_u)])


def test_ranking_buffer_keeps_watched_and_top_symbols_only():
    """전 종목을 들고 있으면 메모리가 선형으로 는다 — 관심 심볼과 상위권만 남긴다."""
    rb = RankingBuffer()
    rb.add(DAY0, _page("WATCHED", 55, 100), keep={"WATCHED"})
    rb.add(DAY0, _page("TOPPER", 3, 100), keep={"WATCHED"})
    rb.add(DAY0, _page("NOBODY", 90, 100), keep={"WATCHED"})
    assert set(rb.rows) == {"WATCHED", "TOPPER"}
    assert len(rb.frame("WATCHED")) == 1
    assert rb.frame("NOBODY").empty


def test_ranking_buffer_prunes_by_time_and_membership():
    rb = RankingBuffer(keep_ms=60_000)
    rb.add(DAY0, _page("AAA", 1, 100), keep={"AAA"})
    rb.add(DAY0 + 120_000, _page("BBB", 1, 100), keep={"BBB"})
    rb.prune(DAY0 + 120_000, keep={"AAA", "BBB"})
    assert "AAA" not in rb.rows                          # 오래된 것 제거
    assert "BBB" in rb.rows
    rb.prune(DAY0 + 120_000, keep={"AAA"})
    assert rb.rows == {}                                 # 관심 밖도 제거


# --------------------------------------------------------------------------- #
# int64 오버플로 회귀 (W3 인수인계 §1)
# --------------------------------------------------------------------------- #
def test_tape_notional_does_not_overflow_int64():
    """price_u × qty_u 누적은 int64 상한을 넘는다 — Python int 로만 합산해야 한다.

    pandas/numpy 정수 누적이었다면 **조용히 랩어라운드**해 값이 쓰레기가 된다.
    """
    price_u = 3_500_000                                   # $3.50
    qty_u = 1_000_000_000_000                             # 1,000,000 주
    trades = [Trade(symbol="AAA", ts_ms=DAY0 + i, price_u=price_u, qty_u=qty_u)
              for i in range(5)]
    raw_product_sum = 5 * price_u * qty_u
    assert raw_product_sum > 2 ** 63 - 1                  # int64 로는 못 담는다

    stats = tape_stats(trades)
    assert stats["notional_u"] == raw_product_sum // 1_000_000
    assert stats["notional_u"] == 17_500_000_000_000      # $17.5M (마이크로달러)
    assert stats["qty_qu"] == 5 * qty_u
    assert stats["min_ts_ms"] == DAY0 and stats["max_ts_ms"] == DAY0 + 4


def test_tape_stats_on_empty_tape():
    assert tape_stats([])["n"] == 0


# --------------------------------------------------------------------------- #
# 컨텍스트 / 상태 복원
# --------------------------------------------------------------------------- #
class StubClient:
    """tier1 스윕 검증용 최소 클라이언트."""

    def __init__(self, known: dict[str, Price | None]):
        self.known = known
        self.counters = {"http_429": 0, "requests": 0, "retries": 0}
        # 실제 `TossClient` 와 같은 자리 — 예산 계상의 귀속 근거 (docs/46).
        self.sent_by_group: dict[str, int] = {}
        self.last_headers: dict[str, str] = {}
        self.requested: list[list[str]] = []

    def sent(self, group: str, n: int = 1, *, retries: int = 0) -> None:
        """`TossClient._send` 가 소켓 직전에 하는 계상을 그대로 흉내낸다.

        전역 `requests` 와 **그룹별** `sent_by_group` 이 같은 자리에서 오른다.
        더블이 그룹별 쪽을 빠뜨리면 예산이 이 호출을 못 본다.
        """
        self.counters["requests"] += n
        self.counters["retries"] = self.counters.get("retries", 0) + retries
        self.sent_by_group[group] = self.sent_by_group.get(group, 0) + n

    async def get_prices(self, symbols):
        self.requested.append(list(symbols))
        self.sent("MARKET_DATA")
        # 함정1: 모르는 심볼은 404 가 아니라 응답에서 조용히 빠진다.
        return [p for p in (self.known.get(s) for s in symbols) if p is not None]

    async def get_us_calendar(self, date=None):
        return calendar_dict([simple_day("2026-07-30", DAY0)], 0)


def build_ctx(tmp_path, client, *, now_ms=None, symbols=(), **cfg_sections):
    cfg = make_config(tmp_path, **cfg_sections)
    store = Store(cfg.store.db_path)
    day = simple_day("2026-07-30", DAY0)
    clock = FrozenClock(now_ms if now_ms is not None else day.regular.start_ms + MIN_MS)
    # 예산의 **사건 타임라인**은 이제 단조 시계다 (docs/52 §5). `FrozenClock` 은
    # `sync=False` 라 서버 오프셋이 없고 `advance` 로만 움직이므로, 이 자리의
    # 결정론적 단조 시계로 쓴다 — 테스트가 `clock.advance` 로 사건을 벌리는 전제를
    # 그대로 유지한다. 두 시계를 갈라 놓은 것 자체는 `test_budget_event_clock.py` 가 본다.
    ctx = CollectorContext.create(client, store, cfg, notifier=Notifier(console=False),
                                  clock=clock, symbols=symbols,
                                  mono=lambda: clock.local_now_ms() / 1000.0)
    ctx.scheduler.calendar = calendar_dict([day], 0)
    ctx.scheduler.fetched_ms = clock.now_ms()
    ctx.session = "regular"
    return ctx, day


def test_state_round_trip_restores_the_last_collection_point(tmp_path):
    client = StubClient({})
    ctx, _day = build_ctx(tmp_path, client, symbols=("AAA", "BBB"))
    ctx.tiers.on_new_data("AAA", 0.9, ctx.clock.now_ms())
    ctx.flush_changes()
    ctx.buffer("AAA").upsert([bar(DAY0 + 5 * MIN_MS)])
    ctx.last_trade_ms["AAA"] = DAY0 + 999
    assert ctx.save_state(force=True) is True
    ctx.store.close()

    # 재시작
    client2 = StubClient({})
    ctx2, _ = build_ctx(tmp_path, client2)
    assert set(ctx2.watchlist) == {"AAA", "BBB"}
    assert ctx2.tiers.tier_of("AAA") == 2
    assert ctx2.last_trade_ms["AAA"] == DAY0 + 999
    assert ctx2.resume_point_ms("AAA") == DAY0 + 5 * MIN_MS
    assert ctx2.counters.get("resumes") == 1
    # 이어받기는 승격 이력이 아니다 — promotions 에 가짜 행이 생기면 안 된다.
    assert ctx2.tiers.drain_changes() == []
    ctx2.store.close()


def test_corrupt_state_file_starts_fresh_instead_of_crashing(tmp_path):
    (tmp_path / "collector_state.json").write_text("{not json", encoding="utf-8")
    ctx, _ = build_ctx(tmp_path, StubClient({}))
    assert ctx.watchlist == []
    ctx.store.close()


def test_promotions_are_recorded_to_the_store(tmp_path):
    ctx, _ = build_ctx(tmp_path, StubClient({}))
    ctx.tiers.force("SNTI", 2, "first_print", 1.0, ctx.clock.now_ms())
    assert ctx.flush_changes() == 1
    rows = ctx.store._conn.execute(
        "SELECT symbol, from_tier, to_tier, reason FROM promotions").fetchall()
    assert rows == [("SNTI", 1, 2, "first_print")]
    ctx.store.close()


# --------------------------------------------------------------------------- #
# 함정1 — 미존재 심볼은 200 + 조용한 누락
# --------------------------------------------------------------------------- #
def test_missing_symbols_are_detected_and_eventually_dropped(tmp_path):
    now = DAY0 + 900 * MIN_MS
    known = {"AAPL": Price(symbol="AAPL", ts_ms=now - 1000, last_u=338_748_800)}
    ctx, _ = build_ctx(tmp_path, StubClient(known), now_ms=now,
                       symbols=("AAPL", "GHOST"))
    for _ in range(loops.MISSING_STREAK_DROP):
        asyncio.run(loops.tier1_sweep_once(ctx))
        ctx.clock.advance(45)

    assert ctx.counters["prices_missing"] >= loops.MISSING_STREAK_DROP
    assert "GHOST" not in ctx.watchlist                  # 연속 누락 → 워치리스트에서 제거
    assert "AAPL" in ctx.watchlist
    ctx.store.close()


def test_first_print_promotes_immediately(tmp_path):
    """A2 §3: null → 값 전이는 '휴면 동전주가 깨어나는 순간' — 승격 1순위."""
    now = DAY0 + 900 * MIN_MS
    known = {"SNTI": Price(symbol="SNTI", ts_ms=None, last_u=347_000)}
    ctx, _ = build_ctx(tmp_path, StubClient(known), now_ms=now, symbols=("SNTI",))

    asyncio.run(loops.tier1_sweep_once(ctx))
    assert ctx.tiers.tier_of("SNTI") == 1               # 첫 관측은 전이가 아니다

    known["SNTI"] = Price(symbol="SNTI", ts_ms=now + 1000, last_u=350_000)
    ctx.clock.advance(45)
    asyncio.run(loops.tier1_sweep_once(ctx))
    assert ctx.tiers.tier_of("SNTI") == 2
    reasons = [r[0] for r in ctx.store._conn.execute(
        "SELECT reason FROM promotions").fetchall()]
    assert "first_print" in reasons
    ctx.store.close()


# --------------------------------------------------------------------------- #
# 함정3 / 함정4 — 페이지네이션 경계, 진행형 일봉
# --------------------------------------------------------------------------- #
class PagingClient:
    """`before` 가 inclusive 인 실서버 동작을 재현한다 (함정3)."""

    def __init__(self, bars, daily=()):
        self.bars = sorted(bars, key=lambda c: c.ts_ms)
        self.daily = list(daily)
        self.counters = {"http_429": 0, "requests": 0}
        self.last_headers: dict[str, str] = {}
        self.requests: list[int | None] = []
        self.candle_calls: list[tuple[str, bool]] = []

    async def get_candles(self, symbol, interval, count=200, before_ms=None,
                          adjusted=True):
        # 기본값을 일부러 True 로 둔다 — 호출부가 1m 에 adjusted 를 빠뜨리면 여기 True 가
        # 기록되고 A5 회귀 테스트가 잡는다.
        self.candle_calls.append((interval, adjusted))
        if interval == "1d":
            return CandlePage(candles=self.daily, next_before_ms=None)
        self.requests.append(before_ms)
        rows = self.bars
        if before_ms is not None:
            rows = [c for c in rows if c.ts_ms <= before_ms]     # inclusive!
        page = rows[-count:]
        if not page:
            return CandlePage(candles=[], next_before_ms=None)
        oldest = page[0].ts_ms
        more = any(c.ts_ms < oldest for c in rows)
        return CandlePage(candles=page, next_before_ms=oldest if more else None)

    async def get_us_calendar(self, date=None):
        return calendar_dict([simple_day("2026-07-30", DAY0)], 0)


def test_backfill_shifts_before_to_avoid_the_duplicate_boundary_bar(tmp_path):
    bars = [bar(DAY0 + i * MIN_MS) for i in range(12)]
    client = PagingClient(bars)
    ctx, _ = build_ctx(tmp_path, client)
    monkey_page = 4
    original = loops.CANDLE_PAGE
    loops.CANDLE_PAGE = monkey_page
    try:
        total = asyncio.run(loops._backfill_1m(ctx, "AAA", pages=3))
    finally:
        loops.CANDLE_PAGE = original

    assert total == 12                                   # 중복 없이 정확히 12개
    assert len(ctx.buffer("AAA")) == 12
    # nextBefore 를 그대로 넘겼다면 경계 봉이 다시 왔을 것 — 1ms 당겨 요청했는지 확인
    assert client.requests[0] is None
    assert client.requests[1] == DAY0 + 8 * MIN_MS - 1
    assert client.requests[2] == DAY0 + 4 * MIN_MS - 1
    ctx.store.close()


def test_backfill_stops_at_the_resume_point(tmp_path):
    bars = [bar(DAY0 + i * MIN_MS) for i in range(12)]
    client = PagingClient(bars)
    ctx, _ = build_ctx(tmp_path, client)
    original = loops.CANDLE_PAGE
    loops.CANDLE_PAGE = 4
    try:
        asyncio.run(loops._backfill_1m(ctx, "AAA", pages=5,
                                       stop_at_ms=DAY0 + 8 * MIN_MS))
    finally:
        loops.CANDLE_PAGE = original
    assert len(client.requests) == 1                     # 첫 페이지에서 이미 도달
    ctx.store.close()


def test_daily_baseline_excludes_the_progressing_today_bar(tmp_path):
    """함정4: 당일 일봉은 장중에도 존재하며 진행형이라 완성봉이 아니다."""
    day = simple_day("2026-07-30", DAY0)
    et_midnight = day.regular.start_ms - int(9.5 * HOUR_MS)
    daily = [bar(et_midnight - i * 24 * HOUR_MS, close_u=2_000_000 + i,
                 vol_qu=56_090_840_000_000) for i in range(5, 0, -1)]
    today_bar = bar(et_midnight, close_u=9_999_999, vol_qu=54_209_000_000)  # 진행형
    client = PagingClient([], daily=daily + [today_bar])
    ctx, _ = build_ctx(tmp_path, client, now_ms=day.regular.start_ms + 30 * MIN_MS)

    asyncio.run(loops._refresh_baseline(ctx, "AAA", ctx.clock.now_ms()))
    assert ctx.counters["daily_today_bar_dropped"] == 1
    assert ctx.baselines["AAA"]["n_days"] == 5
    # prev_close 는 **수정주가 일봉에서 뽑지 않는다** (§2.3 혼합 금지 수정). 이 테스트는
    # 1분봉 버퍼가 없으므로 원주가 전일종가를 구할 수 없어 prev_close 는 미설정이어야 한다
    # — 특히 진행형 일봉 종가(9,999,999)로 채워지면 안 된다.
    assert "AAA" not in ctx.prev_close
    # 저장은 받은 그대로 (당일 봉도 DB 에는 들어간다 — 자르는 것은 베이스라인 계산뿐)
    assert ctx.store._conn.execute("SELECT COUNT(*) FROM candles_1d").fetchone()[0] == 6
    ctx.store.close()


def test_prev_close_uses_raw_1m_not_adjusted_daily(tmp_path):
    """항목 5 회귀: 당일 조건 분모(prev_close)는 전일 **정규장 마지막 1분봉(원주가)**.

    수정 전에는 일봉(수정주가) 종가를 썼다 — 분할 종목에서 전일 대비 가짜 갭 -> day
    트리거 라이브 오탐(RECT). 원주가 1분봉끼리 비교해야 한다.
    """
    day1 = simple_day("2026-07-29", DAY0 - 1440 * MIN_MS)
    day2 = simple_day("2026-07-30", DAY0)
    now = day2.regular.start_ms + 30 * MIN_MS
    # 수정주가 일봉 종가(2,000,00x)와 **다른** 원주가 1분봉 종가(1,930,000)를 둔다.
    et_midnight = day2.regular.start_ms - int(9.5 * HOUR_MS)
    daily = [bar(et_midnight - i * 24 * HOUR_MS, close_u=2_000_000 + i,
                 vol_qu=56_090_840_000_000) for i in range(5, 0, -1)]
    client = PagingClient([], daily=daily)
    ctx, _ = build_ctx(tmp_path, client, now_ms=now)
    ctx.scheduler.calendar = {"previous": day1, "today": day2}
    ctx.history_days = [day1]
    # 전일 정규장 1분봉 (원주가): 마지막 봉 종가 = 1,930,000
    buf = ctx.buffer("AAA")
    buf.upsert([bar(day1.regular.start_ms + i * MIN_MS, close_u=1_900_000 + i * 1000,
                    symbol="AAA") for i in range(31)])       # 마지막 = 1,900,000+30,000
    try:
        asyncio.run(loops._refresh_baseline(ctx, "AAA", now))
        assert ctx.prev_close["AAA"] == 1_930_000            # 원주가 전일 정규장 마지막 봉
        assert ctx.prev_close["AAA"] not in {2_000_001, 2_000_005}   # 수정주가 일봉 아님
    finally:
        ctx.store.close()


# --------------------------------------------------------------------------- #
# 이벤트 알림 등급 — alert 는 "사람이 반드시 봐야 하는 것"만
# --------------------------------------------------------------------------- #
def _emission(t0_ms, *, is_new=True, session="regular", symbol="ABCD"):
    record = {"symbol": symbol, "t0_ms": t0_ms, "kind": "win", "session": session,
              "meta_json": "{}"}
    return EventEmission(record=record, t0_ms=t0_ms, is_new=is_new, session=session)


def test_fresh_first_detection_alerts(tmp_path):
    ctx, day = build_ctx(tmp_path, StubClient({}),
                         now_ms=day_regular_start() + 30 * MIN_MS)
    try:
        level = ctx.announce_event(_emission(ctx.clock.now_ms() - 2 * MIN_MS))
        assert level == "alert"
        assert ctx.notifier.counters["alert"] == 1
        assert ctx.counters["event_alerts"] == 1
    finally:
        ctx.store.close()


def test_label_update_is_info_not_alert(tmp_path):
    """같은 이벤트의 반복 알림이 채널을 오염시키던 문제 — 갱신은 정보일 뿐이다."""
    ctx, _ = build_ctx(tmp_path, StubClient({}),
                       now_ms=day_regular_start() + 30 * MIN_MS)
    try:
        t0 = ctx.clock.now_ms() - MIN_MS
        assert ctx.announce_event(_emission(t0)) == "alert"
        assert ctx.announce_event(_emission(t0, is_new=False)) == "info"
        assert ctx.announce_event(_emission(t0, is_new=False)) == "info"
        assert ctx.notifier.counters["alert"] == 1          # 최초 1회뿐
        assert ctx.counters["event_infos"] == 2
    finally:
        ctx.store.close()


def test_past_session_event_is_info(tmp_path):
    """실측: 프리마켓 수집 중에 session=day 인 과거 이벤트가 ERROR 로 올라왔다."""
    ctx, day = build_ctx(tmp_path, StubClient({}),
                         now_ms=day_regular_start() + 10 * MIN_MS)
    try:
        past_t0 = day.day.start_ms + 60 * MIN_MS          # 데이마켓 시각
        assert past_t0 < ctx.current_session_start_ms()
        assert ctx.announce_event(_emission(past_t0, session="day")) == "info"
        assert ctx.notifier.counters["alert"] == 0
    finally:
        ctx.store.close()


def test_stale_event_inside_the_session_is_info(tmp_path):
    """같은 세션이라도 검출 지연이 크면 지금 행동할 대상이 아니다."""
    ctx, _ = build_ctx(tmp_path, StubClient({}),
                       now_ms=day_regular_start() + 200 * MIN_MS)
    try:
        stale = ctx.clock.now_ms() - (loops.EVENT_ALERT_MAX_LAG_MIN + 5) * MIN_MS
        assert ctx.announce_event(_emission(stale)) == "info"
        fresh = ctx.clock.now_ms() - (loops.EVENT_ALERT_MAX_LAG_MIN - 5) * MIN_MS
        assert ctx.announce_event(_emission(fresh)) == "alert"
    finally:
        ctx.store.close()


def test_restart_does_not_storm_alerts(tmp_path):
    """재기동 시 억제 집합은 비지만(=전부 is_new) 과거 이벤트가 alert 로 쏟아지면 안 된다.

    23:30~00:30 계획 정지·재기동에서 실제로 걸리는 시나리오다.
    """
    ctx, day = build_ctx(tmp_path, StubClient({}),
                         now_ms=day_regular_start() + 240 * MIN_MS)
    try:
        # 오늘 있었던 이벤트 9건 — 과거 세션 3건 + 같은 세션의 오래된 것 6건
        for i in range(3):
            ctx.announce_event(_emission(day.day.start_ms + i * MIN_MS, session="day"))
        for i in range(6):
            ctx.announce_event(_emission(day.regular.start_ms + i * MIN_MS))
        assert ctx.notifier.counters["alert"] == 0        # 폭주 없음
        assert ctx.counters["event_infos"] == 9

        # 지금 막 일어난 것만 알린다
        ctx.announce_event(_emission(ctx.clock.now_ms() - MIN_MS))
        assert ctx.notifier.counters["alert"] == 1
    finally:
        ctx.store.close()


def test_alerts_survive_a_missing_calendar(tmp_path):
    """캘린더가 없으면 세션 판정을 못 한다 — 신선도만으로 판단하고 죽지 않는다."""
    ctx, _ = build_ctx(tmp_path, StubClient({}),
                       now_ms=day_regular_start() + 30 * MIN_MS)
    try:
        ctx.scheduler.calendar = {}
        assert ctx.current_session_start_ms() is None
        assert ctx.announce_event(_emission(ctx.clock.now_ms() - MIN_MS)) == "alert"
    finally:
        ctx.store.close()


def test_demotion_keeps_event_history_but_drops_heavy_state(tmp_path):
    """라이브 40% 중복의 근본 원인 회귀.

    승격 → stale 강등 → 재승격 churn 은 흔하다. 강등에서 검출 이력을 지우면 재승격 직후
    같은 이벤트를 전부 다시 기록한다. 무거운 상태(버퍼·곡선)만 버려야 한다.
    """
    ctx, _ = build_ctx(tmp_path, StubClient({}))
    try:
        ctx.buffer("ABCD").upsert([bar(DAY0)])
        ctx.curves["ABCD"] = (0, None)
        ctx.detector.seed_suppression("ABCD", 111)

        ctx.drop_symbol_state("ABCD")

        assert "ABCD" not in ctx.buffers                  # 메모리는 회수하고
        assert "ABCD" not in ctx.curves
        assert ctx.detector.emitted_count() == 1          # 이력은 남긴다
    finally:
        ctx.store.close()


def day_regular_start() -> int:
    return simple_day("2026-07-30", DAY0).regular.start_ms


# --------------------------------------------------------------------------- #
# 텔레메트리 — 정밀도 반올림 지표 (계약 A4, W1 인수인계)
# --------------------------------------------------------------------------- #
def test_telemetry_carries_precision_metrics(tmp_path):
    """무인 실행에서 로그가 유일한 관측 창이다 — 반올림 지표가 거기 실려야 한다."""
    models.reset_precision_stats()
    client = StubClient({})
    client.counters["precision_rounded"] = 3
    ctx, _ = build_ctx(tmp_path, client, symbols=("AAA",))
    try:
        models.dec_to_u("0.123456789")               # 9자리 → 반올림 발생
        data = ctx.report_telemetry(force=True)
        assert data["precision_rounded"] == 3        # client 카운터
        assert data["precision_parsed"] >= 1         # models 전역 통계
        assert data["precision_max_digits"] == 9
        assert data["precision_rounded_pct"] > 0
        assert data["session"] == "regular"
    finally:
        models.reset_precision_stats()
        ctx.store.close()


def test_telemetry_is_rate_limited(tmp_path):
    ctx, _ = build_ctx(tmp_path, StubClient({}))
    try:
        assert ctx.report_telemetry(force=True) is not None
        assert ctx.report_telemetry() is None                    # 간격 미달
        ctx.clock.advance(loops.TELEMETRY_EVERY_S + 1)
        assert ctx.report_telemetry() is not None
    finally:
        ctx.store.close()


def test_growing_max_digits_raises_an_early_warning(tmp_path):
    """max_digits 신고점 = API 응답 형식 변화 의심 신호 (W1 인수인계).

    첫 관측은 기준선(warn), 그 뒤의 상승은 경보(alert) 다 — '갑자기 커지는 것' 이 신호다.
    """
    models.reset_precision_stats()
    ctx, _ = build_ctx(tmp_path, StubClient({}))
    try:
        models.dec_to_u("0.1234567")                             # 7자리 → 기준선
        ctx.report_telemetry(force=True)
        assert ctx.notifier.counters["warn"] >= 1
        alerts_before = ctx.notifier.counters["alert"]

        ctx.clock.advance(loops.TELEMETRY_EVERY_S + 1)
        ctx.report_telemetry(force=True)                          # 변화 없음 → 조용
        assert ctx.notifier.counters["alert"] == alerts_before

        models.dec_to_u("0.12345678901")                          # 11자리 → 신고점
        ctx.clock.advance(loops.TELEMETRY_EVERY_S + 1)
        ctx.report_telemetry(force=True)
        assert ctx.notifier.counters["alert"] == alerts_before + 1
        assert ctx.counters["precision_drift"] == 2
    finally:
        models.reset_precision_stats()
        ctx.store.close()


def test_session_change_forces_a_telemetry_line(tmp_path):
    ctx, day = build_ctx(tmp_path, StubClient({}))
    try:
        ctx.session = "closed"
        ctx.scheduler.session = "closed"
        before = ctx.notifier.counters["info"]
        asyncio.run(loops.run_session_watch(ctx, cycles=1))
        assert ctx.session == "regular"
        assert ctx.notifier.counters["info"] > before + 1          # 세션 로그 + 텔레메트리
    finally:
        ctx.store.close()


# --------------------------------------------------------------------------- #
# 계약 C-2 개정 A5 — 1분봉 원주가 / 일봉 수정주가
# --------------------------------------------------------------------------- #
def test_candle_adjusted_table_matches_contract_a5():
    assert loops.CANDLE_ADJUSTED == {"1m": False, "1d": True}
    assert loops.candle_adjusted("1m") is False       # 원주가 — 명목 가격대 보존
    assert loops.candle_adjusted("1d") is True        # 수정주가 — 분할 가로지르는 연속성


def test_unknown_interval_fails_loudly_instead_of_guessing():
    """새 interval 을 조용히 한쪽으로 떨어뜨리면 규약이 소리 없이 깨진다."""
    with pytest.raises(ValueError, match="A5"):
        loops.candle_adjusted("5m")


def test_every_candle_call_passes_the_contracted_adjustment(tmp_path):
    """tier2 증분·백필·일봉 3개 경로 전부 검증.

    수정주가는 그 시점의 명목 가격을 지운다 — CRKN 2025-03-05 은 실제로 $1.99 였는데
    수정주가로는 $0.10 로 보인다(W5 라이브 실측). 동전주 가격대가 논지인 이상
    1분봉을 수정주가로 받으면 과거 종목을 통째로 오분류한다.
    """
    day = simple_day("2026-07-30", DAY0)
    et_midnight = day.regular.start_ms - int(9.5 * HOUR_MS)
    daily = [bar(et_midnight - i * 24 * HOUR_MS, close_u=2_000_000 + i)
             for i in range(5, 0, -1)]
    client = PagingClient([bar(DAY0 + i * MIN_MS) for i in range(5)], daily=daily)
    ctx, _ = build_ctx(tmp_path, client, now_ms=day.regular.start_ms + 30 * MIN_MS)
    try:
        asyncio.run(loops.tier2_symbol_once(ctx, "AAA"))   # 백필 + 일봉 + 증분 한 번에

        intervals = [c[0] for c in client.candle_calls]
        assert "1m" in intervals and "1d" in intervals     # 세 경로가 다 돌았다
        assert len(client.candle_calls) >= 3
        for interval, adjusted in client.candle_calls:
            assert adjusted is loops.CANDLE_ADJUSTED[interval], (interval, adjusted)
        assert {a for i, a in client.candle_calls if i == "1m"} == {False}
        assert {a for i, a in client.candle_calls if i == "1d"} == {True}
    finally:
        ctx.store.close()


# --------------------------------------------------------------------------- #
# 세션 전환 시 티어 재구성
# --------------------------------------------------------------------------- #
def test_session_change_rescales_tier_capacity(tmp_path):
    ctx, _ = build_ctx(tmp_path, StubClient({}))
    caps_regular = loops.reconfigure_tiers(ctx, "regular")
    uni = ctx.cfg.universe                               # 설정값을 복제하지 않는다
    assert caps_regular == {"tier2_max": uni.tier2_max, "tier3_max": uni.tier3_max}

    for i in range(5):                                   # tier3 를 5개 채운다
        sym = f"S{i}"
        ctx.tiers.on_new_data(sym, 0.9, 0)
        ctx.tiers.on_new_data(sym, 0.9, 200_000)
    ctx.flush_changes()
    assert len(ctx.tiers.members(3)) == 5

    caps_day = loops.reconfigure_tiers(ctx, "day")        # 얇은 세션 → 감시 축소
    scale = loops.SESSION_TIER_SCALE
    assert caps_day["tier3_max"] == int(uni.tier3_max * scale["day"])
    assert caps_day["tier3_max"] < uni.tier3_max          # 실제로 줄어야 의미가 있다
    caps_after = loops.reconfigure_tiers(ctx, "after")
    assert caps_after["tier3_max"] == int(uni.tier3_max * scale["after"])
    assert len(ctx.tiers.members(3)) <= caps_after["tier3_max"]
    ctx.store.close()


def test_budget_shrink_demotes_and_is_recorded(tmp_path):
    """429 는 사고다 — 정원을 줄이고, 정원을 넘는 만큼 최약체부터 안전 강등한다."""
    ctx, _ = build_ctx(tmp_path, StubClient({}), universe={"tier3_max": 6})
    for i in range(6):
        sym = f"S{i}"
        ctx.tiers.on_new_data(sym, 0.9 - i * 0.01, 0)
        ctx.tiers.on_new_data(sym, 0.9 - i * 0.01, 200_000)
    ctx.flush_changes()
    assert len(ctx.tiers.members(3)) == 6

    ctx.budget.on_429("MARKET_DATA")                      # 사고 발생
    orders = ctx.apply_budget()
    assert orders and "MARKET_DATA" in orders
    assert len(ctx.tiers.members(3)) < 6
    demoted = ctx.store._conn.execute(
        "SELECT COUNT(*) FROM promotions WHERE reason='budget_shrink'").fetchone()[0]
    assert demoted >= 1
    ctx.store.close()


def test_forbidden_stops_every_loop(tmp_path):
    """계약 C-5: Forbidden 은 치명 — 수집을 멈추고 경보한다."""
    from tossmon.api.errors import Forbidden

    class Boom(StubClient):
        async def get_prices(self, symbols):
            raise Forbidden("IP not registered")

    ctx, _ = build_ctx(tmp_path, Boom({}), symbols=("AAA",))
    asyncio.run(loops.run_tier1_price_sweep(ctx.client, ctx.store, ctx.cfg, ctx=ctx,
                                            cycles=3))
    assert not ctx.running()
    assert ctx.notifier.counters["alert"] >= 1
    ctx.store.close()


def test_forbidden_endpoint_stops_every_loop_with_an_alert(tmp_path):
    """계약 A6 회귀: allowlist 위반(ForbiddenEndpoint)은 TossApiError 를 상속하지 않는다.

    광역 catch-all 이 warn 한 줄로 삼켜 수집이 계속되지도(조용한 위반),
    미처리 예외로 루프가 조용히 죽지도 않아야 한다 — 주문 계열 엔드포인트 도달은
    Phase 2 의 마지막 방어선이므로 **경보와 함께 수집 전체가 선다.**
    """
    from tossmon.api.errors import ForbiddenEndpoint

    class Breach(StubClient):
        async def get_prices(self, symbols):
            self.counters["requests"] += 1
            raise ForbiddenEndpoint("GET /api/v1/orders — not in allowlist")

    client = Breach({})
    ctx, _ = build_ctx(tmp_path, client, symbols=("AAA",))
    asyncio.run(loops.run_tier1_price_sweep(ctx.client, ctx.store, ctx.cfg, ctx=ctx,
                                            cycles=5))
    assert not ctx.running()                              # 계속 돌지 않는다
    assert ctx.notifier.counters["alert"] >= 1            # 조용히 죽지도 않는다
    assert ctx.counters.get("forbidden_endpoint") == 1
    assert ctx.counters.get("loop_errors", 0) == 0        # catch-all 로 삼켜지지 않았다
    assert client.counters["requests"] == 1               # 위반 후 재호출 없음
    ctx.store.close()


def test_schema_mismatch_skips_without_killing_the_loop(tmp_path):
    from tossmon.api.errors import SchemaMismatch

    class Drifted(StubClient):
        async def get_prices(self, symbols):
            raise SchemaMismatch("prices: missing field 'lastPrice'")

    ctx, _ = build_ctx(tmp_path, Drifted({}), symbols=("AAA",))
    asyncio.run(loops.run_tier1_price_sweep(ctx.client, ctx.store, ctx.cfg, ctx=ctx,
                                            cycles=3))
    assert ctx.running()                                  # 살아 있다
    assert ctx.counters["schema_mismatch"] == 3
    ctx.store.close()


# --------------------------------------------------------------------------- #
# 5일 무인 하드닝 — 인증 실패 별도 노출 + 삼켜지던 사각 승격 (항목 1·2)
# --------------------------------------------------------------------------- #
def test_auth_expired_is_counted_as_auth_failure_not_api_error(tmp_path):
    """항목 1: 인증 실패가 api_errors 에 뭉개지지 않고 auth_failures 로 드러난다.

    2026-08-01 사고: 재발급 실패가 api_errors=0 인 채 조용히 지나갔다. AuthExpired 는
    TossApiError 하위라 광역 핸들러에 잡히면 api_errors 가 되므로 먼저 잡아 분리한다.
    """
    from tossmon.api.errors import AuthExpired

    class AuthDown(StubClient):
        async def get_prices(self, symbols):
            self.counters["requests"] += 1
            raise AuthExpired("token issuance rejected (401)")

    ctx, _ = build_ctx(tmp_path, AuthDown({}), symbols=("AAA",))
    asyncio.run(loops.run_tier1_price_sweep(ctx.client, ctx.store, ctx.cfg, ctx=ctx,
                                            cycles=3))
    assert ctx.running()                                  # 인증 실패로 죽지는 않는다
    assert ctx.counters["auth_failures"] == 3             # 별도 카운터로 계상
    assert ctx.counters.get("api_errors", 0) == 0         # api_errors 로 뭉개지지 않음
    assert ctx.counters.get("loop_errors", 0) == 0
    assert ctx.notifier.counters["warn"] >= 3             # AUTH-FAILURE 로그 남김
    assert ctx.telemetry()["auth_failures"] == 3          # 워치독이 5분마다 읽는다
    ctx.store.close()


def test_token_issuance_runtimeerror_is_classified_as_auth_failure(tmp_path):
    """항목 1·2: env 부재/리스 충돌은 RuntimeError 로 온다 — 이것도 auth_failures 로 승격.

    바로 이 경로가 2026-08-01 사고의 실제 형태(env 부재로 재발급 RuntimeError)다.
    """
    class NoEnv(StubClient):
        async def get_prices(self, symbols):
            raise RuntimeError("TOSS_BASE_URL is not set (계약 C-9)")

    ctx, _ = build_ctx(tmp_path, NoEnv({}), symbols=("AAA",))
    asyncio.run(loops.run_tier1_price_sweep(ctx.client, ctx.store, ctx.cfg, ctx=ctx,
                                            cycles=2))
    assert ctx.counters["auth_failures"] == 2             # loop_errors 가 아니라 auth_failures
    assert ctx.counters.get("loop_errors", 0) == 0
    ctx.store.close()


def test_generic_runtimeerror_stays_loop_error(tmp_path):
    """분류 정확성: 토큰과 무관한 RuntimeError 는 여전히 loop_errors (오분류 금지)."""
    class Bug(StubClient):
        async def get_prices(self, symbols):
            raise RuntimeError("index out of range in some parser")

    ctx, _ = build_ctx(tmp_path, Bug({}), symbols=("AAA",))
    asyncio.run(loops.run_tier1_price_sweep(ctx.client, ctx.store, ctx.cfg, ctx=ctx,
                                            cycles=2))
    assert ctx.counters["loop_errors"] == 2
    assert ctx.counters.get("auth_failures", 0) == 0
    ctx.store.close()


def test_telemetry_exposes_blindspot_counters_for_the_watchdog(tmp_path):
    """항목 2·4: 삼켜지던 사각과 데이터 건강도가 텔레메트리에 실린다 (워치독 계약)."""
    ctx, _ = build_ctx(tmp_path, StubClient({}), symbols=("AAA",))
    try:
        data = ctx.telemetry()
        for key in ("auth_failures", "loop_errors", "schema_mismatch",
                    "event_write_failures", "promotion_write_failures",
                    "rankings_write_failures", "ranking_snap_age_s",
                    "prices_missing", "fetch_success_pct", "candles_1m"):
            assert key in data, key
        assert data["ranking_snap_age_s"] == -1           # 스냅 없으면 -1
        assert data["fetch_success_pct"] == 100.0         # 요청 없으면 100
    finally:
        ctx.store.close()


def test_ranking_snap_age_grows_when_the_ranking_loop_stalls(tmp_path):
    """항목 4: 랭킹 스냅 간격 이상 — 마지막 스냅 이후 경과가 커지면 워치독이 잡는다."""
    ctx, _ = build_ctx(tmp_path, StubClient({}))
    try:
        ctx.rankings.last_snap_ms = ctx.clock.now_ms()
        assert ctx.telemetry()["ranking_snap_age_s"] == 0
        ctx.clock.advance(600)                            # 10분 경과, 새 스냅 없음
        assert ctx.telemetry()["ranking_snap_age_s"] == 600
    finally:
        ctx.store.close()


def test_fetch_success_pct_reflects_silent_omission(tmp_path):
    """항목 4: 워치 종목 대비 조회 성공률 — 조용한 누락(함정1)이 비율로 드러난다."""
    now = DAY0 + 900 * MIN_MS
    known = {"AAA": Price(symbol="AAA", ts_ms=now - 1000, last_u=1_000_000),
             "BBB": Price(symbol="BBB", ts_ms=now - 1000, last_u=1_000_000)}
    ctx, _ = build_ctx(tmp_path, StubClient(known), now_ms=now,
                       symbols=("AAA", "BBB", "GHOST1", "GHOST2"))
    try:
        asyncio.run(loops.tier1_sweep_once(ctx))
        assert ctx.counters["prices_seen"] == 2
        assert ctx.counters["prices_missing"] == 2
        assert ctx.telemetry()["fetch_success_pct"] == 50.0
    finally:
        ctx.store.close()


def test_loops_idle_while_the_market_is_closed(tmp_path):
    client = StubClient({})
    ctx, day = build_ctx(tmp_path, client, now_ms=day_closed(),
                         symbols=("AAA",))
    ctx.session = "closed"
    asyncio.run(loops.run_tier1_price_sweep(ctx.client, ctx.store, ctx.cfg, ctx=ctx,
                                            cycles=3))
    assert client.requested == []                         # 예산을 태우지 않는다
    ctx.store.close()


def day_closed() -> int:
    day = simple_day("2026-07-30", DAY0)
    return day.after.end_ms + MIN_MS


# --------------------------------------------------------------------------- #
# 핫픽스 2026-08-01 — 애프터 세션 랭킹 int64 오버플로 (110분 전량 유실 사고)
# --------------------------------------------------------------------------- #
class RankingClient(StubClient):
    """rtype 별로 페이지를 돌려주는 랭킹 스텁."""

    def __init__(self, rows_by_any):
        super().__init__({})
        self.rows = rows_by_any

    async def get_rankings(self, rtype, duration="realtime", market="US", count=100):
        self.counters["requests"] += 1
        return RankingPage(ranking_type=rtype, duration=duration, ranked_at_ms=None,
                           rows=list(self.rows))


def _rrow(rank, symbol, *, amount_u=1_000_000_000, vol_qu=10, last_u=1_000_000):
    return RankingRow(rank=rank, symbol=symbol, last_u=last_u, base_u=last_u,
                      change_rate=0.1, vol_qu=vol_qu, amount_u=amount_u)


def test_ranking_int64_overflow_is_clamped_per_row_not_fatal(tmp_path):
    """라이브 사고 재현: 한 행의 amount_u 가 SQLite INTEGER 상한을 넘는다.

    수정 전에는 executemany 단일 트랜잭션이 통째로 죽어 **폴 전체가 0건 저장**됐고
    (경고는 api_errors=0 인 채 unexpected OverflowError 로만 남았다),
    수정 후에는 해당 필드만 클램프되고 나머지 행·나머지 페이지가 전부 저장된다.
    """
    huge = 2 ** 63 + 7
    client = RankingClient([_rrow(1, "OVRF", amount_u=huge),
                            _rrow(2, "OKAY")])
    ctx, _ = build_ctx(tmp_path, client)
    warns: list[str] = []
    original_warn = ctx.notifier.warn
    ctx.notifier.warn = lambda msg: (warns.append(msg), original_warn(msg))[1]
    try:
        asyncio.run(loops.rankings_once(ctx))

        rows = ctx.store._conn.execute(
            "SELECT symbol, amount_u FROM rankings_snap WHERE "
            "ranking_type='TOSS_SECURITIES_TRADING_VOLUME' ORDER BY rank").fetchall()
        assert [s for s, _a in rows] == ["OVRF", "OKAY"]      # 행 단위 — 배치 생존
        assert rows[0][1] == loops.SQLITE_INT_MAX             # 클램프 값 자체가 표식
        assert rows[1][1] == 1_000_000_000                    # 정상 행은 원값 그대로
        n_types = len(loops.RANKING_TYPES)
        assert ctx.counters["rankings_clamped"] == n_types    # rtype 수 × 1 필드
        assert ctx.counters.get("rankings_write_failures", 0) == 0
        assert ctx.counters.get("loop_errors", 0) == 0        # 더는 unexpected 로 새지 않는다
        clamp_warns = [w for w in warns if "clamp" in w]
        assert clamp_warns and "OVRF" in clamp_warns[0]       # 심볼 명시
        assert "amount_u" in clamp_warns[0]                   # 필드 명시
        assert str(huge) in clamp_warns[0]                    # 원값 명시
        # 텔레메트리로 드러난다 (docs/11 §11-1 사각 봉합)
        data = ctx.telemetry()
        assert data["rankings_clamped"] == n_types
        assert data["rankings_write_failures"] == 0
    finally:
        ctx.store.close()


def test_ranking_clamp_survives_realistic_micro_unit_overflow(tmp_path):
    """항목 3: 실데이터 형태(마이크로 단위 대형 정수)로 클램프 분기를 조인다.

    라이브에서 오버플로한 것은 마이크로 단위(값×1e6) 필드다. 여러 필드가 한 행에서
    동시에 넘치고, 여러 행에 흩어져도 **행 단위로** 클램프되고 배치가 살아남아야 한다.
    경계값(2^63-1)은 통과, 그 바로 위(2^63)는 클램프.
    """
    over = 2 ** 63                                              # 정확히 상한 바로 위
    at_max = loops.SQLITE_INT_MAX                              # 경계 = 2^63-1, 통과
    # 대형주 애프터장 실측형: 거래대금 amount_u 와 거래량 vol_qu 가 동시에 마이크로
    # 오버플로 (예: $9.3e12 상당 마이크로 = 9.3e18, 200억주 마이크로 = 2e19).
    client = RankingClient([
        _rrow(1, "MEGA", amount_u=9_300_000_000 * 1_000_000_000,   # ~9.3e18 > 상한
              vol_qu=20_000_000_000 * 1_000_000_000),             # ~2e19 > 상한
        _rrow(2, "EDGE", amount_u=at_max, vol_qu=at_max),         # 경계 — 통과
        _rrow(3, "TINY", amount_u=1_500_000, vol_qu=42),          # 정상
    ])
    ctx, _ = build_ctx(tmp_path, client)
    warns: list[str] = []
    ow = ctx.notifier.warn
    ctx.notifier.warn = lambda m: (warns.append(m), ow(m))[1]
    try:
        asyncio.run(loops.rankings_once(ctx))
        got = ctx.store._conn.execute(
            "SELECT symbol, amount_u, vol_qu FROM rankings_snap WHERE "
            "ranking_type='TOSS_SECURITIES_TRADING_VOLUME' ORDER BY rank").fetchall()
        assert [g[0] for g in got] == ["MEGA", "EDGE", "TINY"]     # 3행 전부 저장
        assert got[0][1] == at_max and got[0][2] == at_max        # MEGA 두 필드 클램프
        assert got[1][1] == at_max and got[1][2] == at_max        # EDGE 경계는 원값(통과)
        assert got[2][1] == 1_500_000 and got[2][2] == 42         # TINY 원값
        # MEGA 한 행에 2필드 × rtype 수, EDGE 는 경계라 클램프 아님
        assert ctx.counters["rankings_clamped"] == 2 * len(loops.RANKING_TYPES)
        assert ctx.counters.get("rankings_write_failures", 0) == 0
        mega = [w for w in warns if "MEGA" in w]
        assert mega and "amount_u" in mega[0] and "vol_qu" in mega[0]
    finally:
        ctx.store.close()


def test_ranking_store_failure_is_counted_and_does_not_kill_the_poll(tmp_path):
    """저장 실패는 카운터·경고로 드러나고, 버퍼·트리거 등 폴의 나머지는 계속된다."""
    client = RankingClient([_rrow(1, "AAA")])
    ctx, _ = build_ctx(tmp_path, client, symbols=("AAA",))

    def boom(_snap_ms, _page):
        raise RuntimeError("disk says no")

    ctx.store.insert_rankings = boom
    try:
        asyncio.run(loops.rankings_once(ctx))
        assert ctx.counters["rankings_write_failures"] == len(loops.RANKING_TYPES)
        assert ctx.counters.get("loop_errors", 0) == 0
        assert len(ctx.rankings.frame("AAA")) > 0             # 버퍼는 살아 있다
        assert ctx.telemetry()["rankings_write_failures"] == len(loops.RANKING_TYPES)
    finally:
        ctx.store.close()


# --------------------------------------------------------------------------- #
# 감사 F-2 — 유니버스 게이트: 랭킹 출현 대형주가 워치리스트에 오르면 안 된다
# --------------------------------------------------------------------------- #
from tossmon.api.models import StockMeta  # noqa: E402  (테스트 하단 그룹 전용)


def meta(symbol, shares, *, security_type="STOCK", common=True, status="ACTIVE"):
    return StockMeta(symbol=symbol, name=f"T {symbol}", market="NASDAQ",
                     security_type=security_type, is_common=common, status=status,
                     list_date="2015-01-05",
                     shares_outstanding_qu=shares * 1_000_000)      # 주 → 마이크로주


class MetaClient(StubClient):
    """유니버스 판정용 `/stocks` 를 갖춘 스텁."""

    def __init__(self, known=None, stocks=None):
        super().__init__(known or {})
        self.stocks = stocks or {}
        self.stock_requests: list[list[str]] = []

    async def get_stocks(self, symbols):
        self.stock_requests.append(list(symbols))
        self.counters["requests"] += 1
        return [self.stocks[s] for s in symbols if s in self.stocks]


def toss_page(*rows):
    return RankingPage(ranking_type="TOSS_SECURITIES_TRADING_VOLUME",
                       duration="realtime", ranked_at_ms=None,
                       rows=[RankingRow(rank=r, symbol=s, last_u=last_u,
                                        base_u=last_u, change_rate=0.1,
                                        vol_qu=10, amount_u=1_000_000_000)
                             for r, s, last_u in rows])


def test_large_caps_from_rankings_are_rejected_and_small_caps_pass(tmp_path):
    """감사 F-2 회귀: NOK·AMD 류가 랭킹으로 정상 등록되던 결함.

    가격($0.10~$20)·시총($10M~$300M) 미달 심볼은 워치리스트에도, tier2 에도 못 오른다.
    걸러진 심볼은 로그로 드러난다 — 조용히 거르면 사후 추적이 불가능하다.
    """
    client = MetaClient(stocks={
        "AMD": meta("AMD", 1_600_000_000),               # $170 → 가격 초과
        "NOK": meta("NOK", 5_500_000_000),               # $3.5 이지만 시총 $19B → 초과
        "SNTI": meta("SNTI", 40_000_000),                # $0.35 × 40M주 = $14M → 통과
    })
    ctx, _ = build_ctx(tmp_path, client)
    try:
        page = toss_page((1, "AMD", 170_000_000), (2, "NOK", 3_500_000),
                         (3, "SNTI", 350_000))
        snap = ctx.clock.now_ms()
        asyncio.run(loops._resolve_universe(
            ctx, [(r.symbol, r.last_u) for r in page.rows], snap))
        loops._ranking_triggers(ctx, page, snap)

        assert "SNTI" in ctx.watchlist
        assert "AMD" not in ctx.watchlist and "NOK" not in ctx.watchlist
        assert ctx.tiers.tier_of("SNTI") == 2            # 통과분만 승격 트리거
        assert ctx.tiers.tier_of("AMD") == 1
        assert ctx.counters["universe_rejected"] == 2
        infos = ctx.notifier.counters["info"]
        assert infos >= 2                                 # 거부 로그가 남았다
        assert ctx.universe_status == {"AMD": False, "NOK": False, "SNTI": True}
        assert ctx.shares_out["SNTI"] == 40_000_000 * 1_000_000
        # 판정은 심볼당 1회 — 다음 스냅샷에서는 /stocks 를 다시 부르지 않는다
        calls_before = len(client.stock_requests)
        asyncio.run(loops._resolve_universe(
            ctx, [(r.symbol, r.last_u) for r in page.rows], snap + 12_000))
        assert len(client.stock_requests) == calls_before
        # 텔레메트리 게이지: 워치리스트 안에 미통과 심볼이 없다
        assert ctx.telemetry()["watch_outside_universe"] == 0
    finally:
        ctx.store.close()


def test_resumed_large_cap_watchlist_is_cleaned_with_a_warning(tmp_path):
    """구 상태파일에서 복원된 대형주는 판정 즉시 경고와 함께 내린다 (조용한 잠식 금지)."""
    client = MetaClient(stocks={"META": meta("META", 2_200_000_000)})
    ctx, _ = build_ctx(tmp_path, client)
    try:
        ctx.watchlist.append("META")                     # 구 상태 복원분을 재현
        snap = ctx.clock.now_ms()
        asyncio.run(loops._resolve_universe(ctx, [("META", 532_600_000)], snap))
        assert "META" not in ctx.watchlist
        assert ctx.notifier.counters["warn"] >= 1
    finally:
        ctx.store.close()


def test_pinned_cli_symbols_bypass_the_universe_gate(tmp_path):
    """운영자가 CLI 로 명시한 심볼은 게이트를 기다리지 않는다 (기존 운용 방식 보존)."""
    ctx, _ = build_ctx(tmp_path, StubClient({}), symbols=("AAPL",))
    try:
        assert "AAPL" in ctx.watchlist
        assert ctx.universe_status["AAPL"] is True
        assert "AAPL" in ctx.pinned
    finally:
        ctx.store.close()


def test_watchlist_seeds_from_the_symbols_table(tmp_path):
    """감사 F-2: collector 가 `symbols` 테이블(build_universe 결과)을 실제로 소비한다."""
    cfg = make_config(tmp_path)
    store = Store(cfg.store.db_path)
    store.upsert_symbols([meta("BTAI", 19_000_000), meta("CRKN", 31_000_000)], tier=1)
    store.upsert_symbols([meta("VTVT", 97_000_000)], tier=0)
    store.close()

    ctx, _ = build_ctx(tmp_path, StubClient({}))
    try:
        assert {"BTAI", "CRKN"} <= set(ctx.watchlist)     # tier1 은 워치리스트 시드
        assert "VTVT" not in ctx.watchlist                # tier0 은 통과 캐시만
        assert ctx.universe_status["VTVT"] is True
        assert ctx.watch("VTVT") is True                  # 랭킹 진입 시 즉시 등록 가능
        assert ctx.shares_out["BTAI"] == 19_000_000 * 1_000_000
    finally:
        ctx.store.close()


def test_ranking_symbols_without_meta_are_not_watched(tmp_path):
    """`/stocks` 가 조용히 누락한 심볼(함정1)은 통과로 치지 않는다."""
    client = MetaClient(stocks={})                        # 메타 없음
    ctx, _ = build_ctx(tmp_path, client)
    try:
        snap = ctx.clock.now_ms()
        asyncio.run(loops._resolve_universe(ctx, [("GHOST", 1_000_000)], snap))
        assert ctx.universe_status["GHOST"] is False
        assert ctx.watch("GHOST") is False
        assert "GHOST" not in ctx.watchlist
    finally:
        ctx.store.close()


def test_ranking_promotion_cannot_evict_scored_members_when_full(tmp_path):
    """감사 H-7 회귀: 랭킹 유래 점수(0.50~0.77)가 실제 스코어(0.3~0.6)를 축출하던 결함.

    랭킹 승격은 tier2 **진입**만 허용한다 — 정원이 찼으면 기존 멤버를 밀어내지 못한다.
    """
    client = MetaClient(stocks={"NEWP": meta("NEWP", 40_000_000)})
    ctx, _ = build_ctx(tmp_path, client, universe={"tier2_max": 2, "tier3_max": 2})
    try:
        now = ctx.clock.now_ms()
        for sym in ("BTAI", "CRKN"):                      # 실제 스코어로 정원을 채운다
            ctx.tiers.on_new_data(sym, 0.40, now)
        ctx.flush_changes()
        assert sorted(ctx.tiers.at_least(2)) == ["BTAI", "CRKN"]

        page = toss_page((1, "NEWP", 350_000))            # 통과 심볼의 랭킹 1위 진입
        asyncio.run(loops._resolve_universe(ctx, [("NEWP", 350_000)], now))
        loops._ranking_triggers(ctx, page, now)

        assert "NEWP" in ctx.watchlist                    # 보긴 본다 (tier1 스윕 대상)
        assert ctx.tiers.tier_of("NEWP") == 1             # 그러나 축출은 없다
        assert sorted(ctx.tiers.at_least(2)) == ["BTAI", "CRKN"]
    finally:
        ctx.store.close()


# --------------------------------------------------------------------------- #
# 감사 F-3 — 실시간 재판정은 현재 매매일로 제한된다
# --------------------------------------------------------------------------- #
def two_day_calendar():
    day1 = simple_day("2026-07-30", DAY0)
    day2 = simple_day("2026-07-31", DAY0 + 1440 * MIN_MS)
    return day1, day2


def spike_bars(base_ms, symbol="ABCD"):
    """30분 창 +15% 를 확실히 넘는 급등 하루 (가격 조건만으로 이벤트가 되는 데이터)."""
    bars = []
    for i in range(120):
        close = 1_000_000 if i < 60 else 1_600_000       # 60분째 +60% 점프
        bars.append(bar(base_ms + i * MIN_MS, close_u=close, symbol=symbol))
    return bars


def test_previous_day_events_are_not_rejudged(tmp_path):
    """감사 F-3 회귀: D 일 이벤트가 D+1 재스캔에서 다시 판정되면 안 된다.

    D+1 의 곡선에는 D 일 폭등 거래량(그 t0 기준 미래)이 들어가 rvol 라벨이 17.6→3.4 로
    무너지고, UPSERT 가 깨끗한 기록을 덮어쓴다. 검출 자체를 당일로 제한하면 사라진다.
    """
    day1, day2 = two_day_calendar()
    now = day2.regular.start_ms + 30 * MIN_MS
    ctx, _ = build_ctx(tmp_path, StubClient({}), now_ms=now)
    try:
        ctx.scheduler.calendar = {"previous": day1, "today": day2}
        ctx.history_days = [day1]
        # 승격 직후처럼 곡선이 아직 없다 (감사 C-1 의 정상 경로) — 가격 조건만으로 판정된다.
        ctx.curves["ABCD"] = (now, None)
        # 버퍼: D 일의 급등 + D+1 의 평탄한 봉 (실제 tier2 버퍼가 이틀을 든 상황)
        buf = ctx.buffer("ABCD")
        buf.upsert(spike_bars(day1.regular.start_ms))
        buf.upsert([bar(day2.regular.start_ms + i * MIN_MS, close_u=1_600_000,
                        symbol="ABCD") for i in range(25)])

        loops._detect(ctx, "ABCD")

        rows = ctx.store._conn.execute("SELECT symbol, t0_ms FROM events").fetchall()
        day2_start = day2.day.start_ms
        assert all(t0 >= day2_start for _s, t0 in rows), \
            f"전일 이벤트가 재판정·기록됐다: {rows}"
    finally:
        ctx.store.close()


def test_detection_still_fires_for_todays_event(tmp_path):
    """당일 제한이 당일 이벤트까지 죽이면 안 된다 — 검출 경로 자체의 생존 확인."""
    day1, day2 = two_day_calendar()
    now = day2.regular.start_ms + 119 * MIN_MS
    ctx, _ = build_ctx(tmp_path, StubClient({}), now_ms=now)
    try:
        ctx.scheduler.calendar = {"previous": day1, "today": day2}
        ctx.buffer("ABCD").upsert(spike_bars(day2.regular.start_ms))
        loops._detect(ctx, "ABCD")
        rows = ctx.store._conn.execute("SELECT t0_ms FROM events").fetchall()
        assert rows and all(t0 >= day2.day.start_ms for (t0,) in rows)
    finally:
        ctx.store.close()


# --------------------------------------------------------------------------- #
# 감사 C-1 — 곡선 실패(None)가 1시간 고착되면 RVOL 게이트가 조용히 꺼진다
# --------------------------------------------------------------------------- #
def test_curve_failure_is_retried_quickly_not_cached_for_an_hour(tmp_path):
    day1, day2 = two_day_calendar()
    now = day2.regular.start_ms + 30 * MIN_MS
    ctx, _ = build_ctx(tmp_path, StubClient({}), now_ms=now)
    try:
        ctx.scheduler.calendar = {"previous": day1, "today": day2}
        ctx.history_days = [day1]
        today_only = candles_frame(
            [bar(day2.regular.start_ms + i * MIN_MS) for i in range(30)])
        assert loops._curve_for(ctx, "AAA", today_only, now) is None   # 이력 부족

        # 5분 뒤 백필로 전일 봉이 생겼다 — 성공 TTL(1시간) 안이지만 다시 시도해야 한다
        with_history = candles_frame(
            [bar(day1.regular.start_ms + i * MIN_MS, vol_qu=5_000_000)
             for i in range(120)]
            + [bar(day2.regular.start_ms + i * MIN_MS) for i in range(30)])
        later = now + loops.CURVE_NONE_TTL_MS + 1
        curve = loops._curve_for(ctx, "AAA", with_history, later)
        assert curve is not None, "곡선 실패가 장시간 캐시되어 게이트가 꺼진 채 남는다"

        # 성공한 곡선은 여전히 1시간 캐시된다 (재계산 비용 억제)
        assert loops._curve_for(ctx, "AAA", today_only, later + 10 * MIN_MS) is curve
    finally:
        ctx.store.close()


# --------------------------------------------------------------------------- #
# 감사 H-6 — 세션 전환 시 베이스라인·전일종가·이력일 무효화
# --------------------------------------------------------------------------- #
def test_session_change_invalidates_baselines_and_prev_close(tmp_path):
    """승격 시점 값이 프로세스 수명 내내 얼어붙으면 2일차부터 라벨 분모가 틀린다 (감사 H-6).

    무효화는 유지하되 **한꺼번에 지우지 않는다** — 에포크를 올리고 심볼별로 흩어진 시각에
    재계산한다. 전환 즉시 전부 지우면 다음 라운드로빈 한 바퀴 안에 정원만큼의 일봉 호출이
    쏟아져 429 를 자초한다 (2026-08-04: 09:00 전환 42초 안에 429 3연발).
    """
    ctx, _ = build_ctx(tmp_path, StubClient({}))
    try:
        ctx.baselines["AAA"] = {"adv20_qu": 1}
        ctx.baseline_ms["AAA"] = ctx.clock.now_ms()
        ctx.prev_close["AAA"] = 1_000_000
        ctx.history_days = ["sentinel"]
        ctx.curves["AAA"] = (0, None)

        loops.reconfigure_tiers(ctx, "regular")

        # 값이 없는 것(재계산 트리거)들은 그대로 비운다 — API 호출이 없기 때문이다.
        assert ctx.prev_close == {} and ctx.history_days == [] and ctx.curves == {}
        # 베이스라인은 에포크로 무효화된다 (지우지 않는다 = 버스트 없음)
        assert ctx.baseline_epoch_ms >= ctx.baseline_ms["AAA"]
        # 오프셋이 지나면 재계산 대상이 된다
        late = ctx.baseline_epoch_ms + (loops.BASELINE_REFRESH_SPREAD_S + 1) * 1000
        assert loops._baseline_due(ctx, "AAA", late) is True
    finally:
        ctx.store.close()


def test_baseline_refresh_is_spread_not_a_thundering_herd(tmp_path):
    """세션 전환 직후 **동시에** 재계산되지 않는다 — 이것이 자작 429 의 원인이었다."""
    ctx, _ = build_ctx(tmp_path, StubClient({}))
    try:
        syms = [f"SYM{i:03d}" for i in range(200)]
        now = ctx.clock.now_ms()
        for s_ in syms:
            ctx.baselines[s_] = {"adv20_qu": 1}
            ctx.baseline_ms[s_] = now
        loops.reconfigure_tiers(ctx, "regular")          # 에포크만 올린다
        epoch = ctx.baseline_epoch_ms

        due_now = sum(1 for s_ in syms if loops._baseline_due(ctx, s_, epoch))
        assert due_now == 0, f"전환 즉시 {due_now} 종목이 한꺼번에 재계산된다 (버스트)"
        # 분산 창이 다 지나면 전부 재계산된다 (무효화 자체는 유지)
        end = epoch + (loops.BASELINE_REFRESH_SPREAD_S + 1) * 1000 + 1
        assert sum(1 for s_ in syms if loops._baseline_due(ctx, s_, end)) == len(syms)
        # 중간 시점에는 일부만 — 실제로 흩어져 있다
        mid = epoch + loops.BASELINE_REFRESH_SPREAD_S * 1000 // 2
        part = sum(1 for s_ in syms if loops._baseline_due(ctx, s_, mid))
        assert 0 < part < len(syms), f"분산되지 않았다 (mid={part})"
        # 같은 심볼은 항상 같은 자리 — 재시작에도 안정적이다
        assert (loops._baseline_spread_offset_ms("SYM001")
                == loops._baseline_spread_offset_ms("SYM001"))
    finally:
        ctx.store.close()


# --------------------------------------------------------------------------- #
# 감사 H-9 — 재시작 이어받기 구멍: 백필 강제 + 못 메우면 경고
# --------------------------------------------------------------------------- #
def test_resume_backfills_past_the_db_history_fast_path(tmp_path):
    """DB 에서 CANDLE_PAGE 이상 읽었다고 백필을 건너뛰면 정전 구간이 영구 구멍이 된다."""
    bars = [bar(DAY0 + i * MIN_MS) for i in range(30)]
    client = PagingClient(bars)
    ctx, _ = build_ctx(tmp_path, client, now_ms=DAY0 + 30 * MIN_MS)
    original = loops.CANDLE_PAGE
    loops.CANDLE_PAGE = 5
    try:
        # 정전 전: 봉 0..9 까지 수집돼 있었다 (DB 에 10봉 ≥ CANDLE_PAGE=5)
        ctx.store.upsert_candles_1m(bars[:10])
        ctx._resume_candle_ms["AAA"] = DAY0 + 9 * MIN_MS
        ctx.baselines["AAA"] = {"n_days": 1}             # 일봉 경로는 이 테스트 밖

        asyncio.run(loops._ensure_history(ctx, "AAA"))

        buf_ts = set(ctx.buffer("AAA").bars)
        missing = [DAY0 + i * MIN_MS for i in range(10, 30)
                   if DAY0 + i * MIN_MS not in buf_ts]
        assert missing == [], f"정전 구간이 메워지지 않았다: {len(missing)}분 구멍"
        assert ctx.counters.get("backfill_gaps", 0) == 0   # 닿았으므로 경고도 없다
    finally:
        loops.CANDLE_PAGE = original
        ctx.store.close()


def test_unreachable_resume_point_warns_loudly(tmp_path):
    """페이지 상한 때문에 재개 지점에 못 닿으면 **반드시** 경고한다 (탐지가 본질이다)."""
    bars = [bar(DAY0 + i * MIN_MS) for i in range(12)]
    client = PagingClient(bars)
    ctx, _ = build_ctx(tmp_path, client)
    original = loops.CANDLE_PAGE
    loops.CANDLE_PAGE = 4
    try:
        warns_before = ctx.notifier.counters["warn"]
        asyncio.run(loops._backfill_1m(ctx, "AAA", pages=1,
                                       stop_at_ms=DAY0 - 60 * MIN_MS))
        assert ctx.counters["backfill_gaps"] == 1
        assert ctx.notifier.counters["warn"] == warns_before + 1
    finally:
        loops.CANDLE_PAGE = original
        ctx.store.close()


# --------------------------------------------------------------------------- #
# 감사 ② — 재시도·실패 호출의 BudgetGuard 계상
# --------------------------------------------------------------------------- #
def test_failed_and_retried_attempts_reach_the_budget_guard(tmp_path):
    """예외로 끝난 호출·내부 재시도가 0회로 계상되면 실사용이 과소평가된다."""
    client = StubClient({})
    ctx, _ = build_ctx(tmp_path, client)
    try:
        base = ctx.budget.counters.get("MARKET_DATA", 0)
        # 실패로 끝난 호출: after_call 은 불리지 않지만 시도는 3회 있었다 (재시도 2회 포함).
        # 실제 client 는 시도마다 requests 와 **그룹별** sent_by_group 을, 재시도마다
        # retries 를 올린다 (client.py `_send`) — 더블도 그렇게 흉내낸다.
        client.sent("MARKET_DATA", 3, retries=2)
        ctx.sync_rate_limits("MARKET_DATA")
        assert ctx.budget.counters.get("MARKET_DATA", 0) == base + 3

        # 성공 호출: 시도 2회(재시도 1회) → 논리 1회가 아니라 2회로 계상
        client.sent("MARKET_DATA", 2, retries=1)
        ctx.after_call("MARKET_DATA")
        assert ctx.budget.counters.get("MARKET_DATA", 0) == base + 5
        assert ctx.counters["req_MARKET_DATA"] == 1       # 논리 카운터는 그대로 1

        # 새 시도가 없으면 다시 불려도 중복 계상하지 않는다 (고수위 비교)
        ctx.sync_rate_limits("MARKET_DATA")
        assert ctx.budget.counters.get("MARKET_DATA", 0) == base + 5
    finally:
        ctx.store.close()


# --------------------------------------------------------------------------- #
# tier2 저빈도 호가 (W5 에스컬레이션 2026-08-03) — 승격 전후 스프레드 궤적
# --------------------------------------------------------------------------- #
from tossmon.api.models import Orderbook, OrderbookLevel  # noqa: E402


class BookClient(StubClient):
    """호가를 돌려주는 스텁. 어떤 심볼이 조회됐는지 기록한다."""

    def __init__(self, known=None):
        super().__init__(known or {})
        self.book_calls: list[str] = []

    async def get_orderbook(self, symbol):
        self.book_calls.append(symbol)
        self.sent("MARKET_DATA")
        return Orderbook(symbol=symbol, ts_ms=DAY0,
                         bids=[OrderbookLevel(price_u=999_000, qty_u=100_000_000)],
                         asks=[OrderbookLevel(price_u=1_001_000, qty_u=90_000_000)])

    async def get_trades(self, symbol, count=50):
        self.sent("MARKET_DATA")
        return []


def _tier(ctx, symbol, tier):
    """symbol 을 지정 티어로 올린다 (2 = 1회, 3 = dwell 지나 2회)."""
    ctx.tiers.on_new_data(symbol, 0.9, 0)
    if tier >= 3:
        ctx.tiers.on_new_data(symbol, 0.9, 200_000)
    ctx.flush_changes()


def _book_ctx(tmp_path, period=600, **kw):
    client = BookClient()
    ctx, day = build_ctx(tmp_path, client, polling={"tier2_orderbook_s": period}, **kw)
    return ctx, client


def test_tier2_orderbook_polls_tier2_members_only(tmp_path):
    """tier2 멤버가 라운드로빈으로 폴링된다. tier3 는 자기 루프(16s)가 보므로 제외."""
    ctx, client = _book_ctx(tmp_path)
    try:
        _tier(ctx, "AAA", 2)
        _tier(ctx, "BBB", 2)
        _tier(ctx, "CCC", 3)                       # tier3 — 이 루프 대상이 아니다
        assert sorted(ctx.tiers.members(2)) == ["AAA", "BBB"]

        asyncio.run(loops.run_tier2_orderbook(ctx.client, ctx.store, ctx.cfg, ctx=ctx,
                                              cycles=4))
        assert set(client.book_calls) == {"AAA", "BBB"}      # tier3 CCC 는 없다
        assert client.book_calls.count("AAA") == 2           # 라운드로빈으로 번갈아
        assert ctx.counters["tier2_orderbook_snaps"] == 4
        rows = ctx.store._conn.execute(
            "SELECT DISTINCT symbol FROM orderbook_snap ORDER BY symbol").fetchall()
        assert [r[0] for r in rows] == ["AAA", "BBB"]        # 실제로 저장된다
        assert ctx.telemetry()["tier2_orderbook_snaps"] == 4
    finally:
        ctx.store.close()


def test_tier2_orderbook_paces_one_poll_per_symbol_per_period(tmp_path):
    """심볼당 주기 = period. 두 심볼이면 period/2 간격으로 번갈아 (예산 산식의 근거)."""
    ctx, client = _book_ctx(tmp_path, period=600)
    try:
        _tier(ctx, "AAA", 2)
        _tier(ctx, "BBB", 2)
        before = ctx.clock.now_ms()
        asyncio.run(loops.run_tier2_orderbook(ctx.client, ctx.store, ctx.cfg, ctx=ctx,
                                              cycles=2))
        elapsed_s = (ctx.clock.now_ms() - before) / 1000.0
        assert elapsed_s == pytest.approx(600.0)             # 2건 x (600/2)
        assert len(client.book_calls) == 2
    finally:
        ctx.store.close()


def test_tier2_orderbook_yields_first_under_budget_pressure(tmp_path):
    """예산 압박이면 tier2 호가만 건너뛴다 — tier3 테이프·호가는 그대로 돈다."""
    ctx, client = _book_ctx(tmp_path)
    try:
        _tier(ctx, "AAA", 2)
        _tier(ctx, "CCC", 3)
        # **지속 사용률**을 목표의 90% 위로. 예전에는 `peak_1s` 를 밀어 올렸는데, 그것은
        # 60초 중 최악의 1초라 지속 속도 목표와 단위가 안 맞았다 (docs/46 §4).
        # 값을 박아넣지 않고 target 에서 역산한다 — usage_ratio 가 바뀌면 target 도 바뀐다.
        over = ctx.budget.target(loops.GROUP_MARKET_DATA) * loops.TIER2_ORDERBOOK_HEADROOM
        ctx.budget.measured_rate = lambda group: over + 0.1

        asyncio.run(loops.run_tier2_orderbook(ctx.client, ctx.store, ctx.cfg, ctx=ctx,
                                              cycles=3))
        assert client.book_calls == []                        # tier2 호가는 전부 양보
        assert ctx.counters["tier2_orderbook_skipped_rate"] == 3
        assert ctx.telemetry()["tier2_orderbook_skipped"] == 3

        # 같은 압박에서도 tier3 마이크로 루프는 계속 수집한다 (밀어내지 않는다)
        asyncio.run(loops.run_tier3_micro(ctx.client, ctx.store, ctx.cfg, ctx=ctx,
                                          cycles=1))
        assert "CCC" in client.book_calls                     # tier3 는 정상
    finally:
        ctx.store.close()


def test_tier2_orderbook_backs_off_after_a_429(tmp_path):
    """429 직후 쿨다운 동안 tier2 호가는 물러난다 (사고 시 1순위 희생)."""
    # 한 사이클의 sleep(period/n)이 쿨다운(5분)보다 짧아야 쿨다운 자체를 검증할 수 있다.
    ctx, client = _book_ctx(tmp_path, period=60)
    try:
        _tier(ctx, "AAA", 2)
        ctx.budget.on_429("MARKET_DATA")                      # 사고 발생
        asyncio.run(loops.run_tier2_orderbook(ctx.client, ctx.store, ctx.cfg, ctx=ctx,
                                              cycles=2))
        assert client.book_calls == []                         # 쿨다운 동안 전부 양보
        assert ctx.counters["tier2_orderbook_skipped_429"] == 2

        ctx.clock.advance(loops.TIER2_ORDERBOOK_COOLDOWN_MS / 1000 + 1)   # 쿨다운 경과
        asyncio.run(loops.run_tier2_orderbook(ctx.client, ctx.store, ctx.cfg, ctx=ctx,
                                              cycles=1))
        assert client.book_calls == ["AAA"]                    # 회복 후 재개
    finally:
        ctx.store.close()


def test_tier2_orderbook_disabled_by_default_and_never_ends_the_task(tmp_path):
    """미설정/0 이면 호출 0건. **중요**: 그래도 task 가 끝나면 안 된다 —
    run_all 이 FIRST_COMPLETED 로 기다리므로 조기 종료는 수집 전체를 내린다."""
    for period in (0, None):
        # period=None -> 키 자체를 안 준다(미설정). polling 섹션은 그대로 둔다.
        sections = {} if period is None else {"tier2_orderbook_s": period}
        client = BookClient()
        ctx, _ = build_ctx(tmp_path, client, polling=sections)
        try:
            assert ctx.cfg.polling.tier2_orderbook_s == 0     # 기본 비활성
            _tier(ctx, "AAA", 2)
            before = ctx.clock.now_ms()
            asyncio.run(loops.run_tier2_orderbook(ctx.client, ctx.store, ctx.cfg,
                                                  ctx=ctx, cycles=3))
            assert client.book_calls == []                    # 호출 0
            # 즉시 return 이 아니라 idle 로 돌았다는 증거 (시계가 흘렀다)
            assert ctx.clock.now_ms() > before
            assert ctx.counters.get("tier2_orderbook_snaps", 0) == 0
        finally:
            ctx.store.close()


# --------------------------------------------------------------------------- #
# 테이프 결손 (2026-08-03 실측: 결손 190건 전부 n=50 — /trades 50건 상한)
# --------------------------------------------------------------------------- #
def test_trades_intervals_never_exceed_the_existing_budget():
    """불변식: 어떤 입력에도 총 호출률이 기존(len/base)을 넘지 않는다 (예산 중립)."""
    base = 4.0
    for n_members in (1, 2, 4, 8, 20, 50):
        members = [f"S{i}" for i in range(n_members)]
        for n_sat in range(0, n_members + 1):
            sat = set(members[:n_sat])
            iv = loops.tier3_trades_intervals(members, sat, base)
            assert set(iv) == set(members)
            total = sum(1.0 / v for v in iv.values())
            budget = n_members / base
            assert total <= budget + 1e-9, (n_members, n_sat, total, budget)
            assert all(v > 0 for v in iv.values())


def test_trades_intervals_speed_up_saturated_and_slow_down_calm():
    """포화 종목은 절반 주기, 그 대가는 한산한 종목에서 되돌려 받는다."""
    members = [f"S{i}" for i in range(20)]
    iv = loops.tier3_trades_intervals(members, {"S0", "S1", "S2"}, 4.0)
    assert iv["S0"] == iv["S1"] == iv["S2"] == 2.0          # 빠른 레인 = base/2
    calm = [iv[s] for s in members if s not in {"S0", "S1", "S2"}]
    assert all(c > 4.0 for c in calm)                       # 한산은 늦춰서 되돌려준다
    assert sum(1.0 / v for v in iv.values()) == pytest.approx(20 / 4.0)   # 총량 동일


def test_trades_intervals_unchanged_when_nothing_saturates():
    members = [f"S{i}" for i in range(6)]
    iv = loops.tier3_trades_intervals(members, set(), 4.0)
    assert set(iv.values()) == {4.0}                        # 평시엔 아무것도 안 바꾼다


def test_trades_intervals_fast_lane_is_capped():
    """포화가 많아도 빠른 레인은 상한이 있다 (예산·한산주기 상한을 함께 지킨다)."""
    members = [f"S{i}" for i in range(20)]
    iv = loops.tier3_trades_intervals(members, set(members), 4.0)
    assert sum(1 for v in iv.values() if v == 2.0) <= loops.TAPE_FAST_LANE_MAX
    assert sum(1.0 / v for v in iv.values()) <= 20 / 4.0 + 1e-9


class TapeClient(StubClient):
    """지정한 체결 목록을 돌려주는 스텁 (n=50 상한 재현용)."""

    def __init__(self, batches):
        super().__init__({})
        self.batches = list(batches)
        self.calls = 0

    async def get_trades(self, symbol, count=50):
        self.calls += 1
        out = self.batches.pop(0) if self.batches else []
        return out


def _trade(ts_ms, symbol="HOT"):
    return Trade(symbol=symbol, ts_ms=ts_ms, price_u=1_000_000, qty_u=1_000_000)


def test_saturated_gap_marks_the_symbol_for_the_fast_lane(tmp_path):
    """50건 상한 + 구간 결손이면 포화로 표시한다 — 적응형 주기의 입력."""
    base = DAY0
    full = [_trade(base + i * 10) for i in range(loops.TRADES_COUNT)]      # n=50
    later = [_trade(base + 100_000 + i * 10) for i in range(loops.TRADES_COUNT)]
    client = TapeClient([full, later])
    ctx, _ = build_ctx(tmp_path, client)
    try:
        asyncio.run(loops._poll_trades(ctx, "HOT"))        # 기준선
        assert ctx.tape_saturated_ms == {}
        asyncio.run(loops._poll_trades(ctx, "HOT"))        # 구간 결손 + n=50
        assert ctx.counters["tape_gaps"] == 1
        assert "HOT" in ctx.tape_saturated_ms              # 빠른 레인 대상
        assert ctx.telemetry()["tape_saturated"] == 1
    finally:
        ctx.store.close()


def test_unsaturated_gap_is_not_treated_as_a_polling_shortfall(tmp_path):
    """n<50 인 결손은 폴링이 느려서가 아니다 — 빠른 레인으로 옮기지 않는다."""
    base = DAY0
    client = TapeClient([[_trade(base)], [_trade(base + 100_000)]])
    ctx, _ = build_ctx(tmp_path, client)
    try:
        asyncio.run(loops._poll_trades(ctx, "SLOW"))
        asyncio.run(loops._poll_trades(ctx, "SLOW"))
        assert ctx.counters["tape_gaps"] == 1              # 결손은 기록하되
        assert ctx.tape_saturated_ms == {}                 # 주기는 건드리지 않는다
    finally:
        ctx.store.close()


# --------------------------------------------------------------------------- #
# 원본 건수 vs 저장 건수 (docs/42 §5-5, docs/43)
# --------------------------------------------------------------------------- #
def _trade_px(ts_ms, price_u, qty_u=1_000_000, symbol="HOT"):
    return Trade(symbol=symbol, ts_ms=ts_ms, price_u=price_u, qty_u=qty_u)


def test_within_poll_fold_is_counted_apart_from_re_delivery(tmp_path):
    """`n - stored` 를 뭉쳐 세면 **정보 소실**이 **재전달** 안에 묻힌다.

    같은 응답 안에서 PK 가 못 가른 행(=서로 다른 체결이 하나로 접힘, 진짜 소실)과,
    이전 폴이 이미 저장한 행(=정상 재전달)은 성질이 정반대다. 따로 세야 한다.
    """
    base = DAY0
    first = [_trade_px(base, 1_000_000), _trade_px(base + 1000, 2_000_000)]
    # 2번째 응답: 앞 두 건 재전달 + 같은 (ts, price, qty) 두 건(응답 안 접힘) + 신규 1건
    second = [_trade_px(base, 1_000_000), _trade_px(base + 1000, 2_000_000),
              _trade_px(base + 2000, 3_000_000), _trade_px(base + 2000, 3_000_000),
              _trade_px(base + 3000, 4_000_000)]
    ctx, _ = build_ctx(tmp_path, TapeClient([first, second]))
    try:
        asyncio.run(loops._poll_trades(ctx, "HOT"))
        asyncio.run(loops._poll_trades(ctx, "HOT"))
        assert ctx.counters["trades_raw_rows"] == 7        # 2 + 5 (원본 그대로)
        assert ctx.counters["trades_rows"] == 4            # 실제 저장된 서로 다른 행
        assert ctx.counters["trades_dup_same_poll"] == 1   # 응답 안에서 접힌 1건 = 소실
        assert ctx.counters["trades_dup_prev_poll"] == 2   # 재전달 2건 = 소실 아님
        tel = ctx.telemetry()
        assert tel["trades_raw_rows"] == 7 and tel["trades_rows"] == 4
        assert tel["trades_dup_same_poll"] == 1 and tel["trades_dup_prev_poll"] == 2
    finally:
        ctx.store.close()


def test_cap_marker_counts_polls_not_lost_trades(tmp_path):
    """상한 표시는 **하한**이다 — API 가 원본 총 건수를 안 주므로 "몇 건이 잘렸나"는
    못 잰다. 50건을 받은 폴 수만 셀 수 있고, 49건이면 잘리지 않은 것이 확실하다."""
    base = DAY0
    full = [_trade_px(base + i * 10, 1_000_000 + i) for i in range(loops.TRADES_COUNT)]
    short = [_trade_px(base + 10_000 + i * 10, 1_000_000 + i)
             for i in range(loops.TRADES_COUNT - 1)]
    ctx, _ = build_ctx(tmp_path, TapeClient([full, short]))
    try:
        asyncio.run(loops._poll_trades(ctx, "HOT"))
        assert ctx.counters["trades_polls_at_cap"] == 1
        asyncio.run(loops._poll_trades(ctx, "HOT"))
        assert ctx.counters["trades_polls_at_cap"] == 1    # 49건은 상한이 아니다
        assert ctx.counters["trades_polls"] == 2
    finally:
        ctx.store.close()


def test_empty_response_still_counts_as_a_poll(tmp_path):
    """빈 응답도 폴이다 — 분모를 빠뜨리면 `at_cap` 비율이 부풀어 오른다."""
    ctx, _ = build_ctx(tmp_path, TapeClient([[]]))
    try:
        asyncio.run(loops._poll_trades(ctx, "QUIET"))
        assert ctx.counters["trades_polls"] == 1
        assert ctx.counters.get("trades_raw_rows", 0) == 0
    finally:
        ctx.store.close()


# --------------------------------------------------------------------------- #
# 결손을 위치를 가진 사건으로 (docs/42 §5-6, docs/43)
# --------------------------------------------------------------------------- #
def _gap_rows(ctx):
    return ctx.store._conn.execute(
        "SELECT symbol, poll_ms, prev_poll_ms, gap_lo_ms, gap_hi_ms, span_hi_ms, "
        "n_raw, n_stored FROM tape_gaps ORDER BY id").fetchall()


def test_tape_gap_is_written_as_a_located_interval(tmp_path):
    """누적 카운터가 아니라 **어느 구간이 비었는지**가 남아야 하류가 뺄 수 있다."""
    base = DAY0
    full = [_trade(base + i * 10) for i in range(loops.TRADES_COUNT)]
    later = [_trade(base + 100_000 + i * 10) for i in range(loops.TRADES_COUNT)]
    ctx, _ = build_ctx(tmp_path, TapeClient([full, later]))
    try:
        asyncio.run(loops._poll_trades(ctx, "HOT"))
        assert _gap_rows(ctx) == []                        # 첫 폴은 결손을 못 판정한다
        asyncio.run(loops._poll_trades(ctx, "HOT"))
        rows = _gap_rows(ctx)
        assert len(rows) == 1
        sym, poll_ms, prev_poll_ms, lo, hi, span_hi, n_raw, n_stored = rows[0]
        assert sym == "HOT"
        assert (lo, hi) == (base + 490, base + 100_000)    # 비어 있는 열린 구간
        assert span_hi == base + 100_000 + 490             # 국소 체결률의 분모
        assert n_raw == loops.TRADES_COUNT                 # 상한이 원인임을 여기서 읽는다
        assert n_stored == loops.TRADES_COUNT
        assert poll_ms == ctx.clock.now_ms() and prev_poll_ms == poll_ms
        assert ctx.counters["tape_gaps"] == len(rows)      # 카운터와 테이블이 같은 사건
    finally:
        ctx.store.close()


def test_gap_row_records_that_polling_was_not_continuous_after_a_restart(tmp_path):
    """재기동 직후에는 폴링 연속성을 보증할 수 없다 — NULL 로 그렇게 적는다.

    `last_trade_ms` 는 상태 파일에서 복원되지만 **직전 폴 시각은 복원하지 않는다.**
    복원하면 정지 구간을 가로질러 "연속이었다"고 말하게 된다. docs/41 §4-2 는 이
    구분이 없어 "1시간 이상이면 tier3 재진입"이라는 임계로 803줄을 갈라내야 했다.
    """
    base = DAY0
    later = [_trade(base + 100_000 + i * 10) for i in range(loops.TRADES_COUNT)]
    ctx, _ = build_ctx(tmp_path, TapeClient([later]))
    try:
        ctx.last_trade_ms["HOT"] = base                    # 재기동으로 복원된 상태
        asyncio.run(loops._poll_trades(ctx, "HOT"))
        rows = _gap_rows(ctx)
        assert len(rows) == 1
        assert rows[0][2] is None                          # prev_poll_ms = 보증 불가
    finally:
        ctx.store.close()


def test_gap_row_write_failure_does_not_kill_the_poll(tmp_path):
    """관측용 부산물이 수집 자체를 멈추면 거꾸로다 — 실패는 카운터로 드러낸다."""
    base = DAY0
    full = [_trade(base + i * 10) for i in range(loops.TRADES_COUNT)]
    later = [_trade(base + 100_000 + i * 10) for i in range(loops.TRADES_COUNT)]
    ctx, _ = build_ctx(tmp_path, TapeClient([full, later]))

    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    try:
        asyncio.run(loops._poll_trades(ctx, "HOT"))
        ctx.store.record_tape_gap = boom
        stored = asyncio.run(loops._poll_trades(ctx, "HOT"))
        assert stored == loops.TRADES_COUNT                # 체결은 그대로 저장됐다
        assert ctx.counters["tape_gap_write_failures"] == 1
        assert ctx.counters["tape_gaps"] == 1              # 카운터는 여전히 센다
        assert ctx.telemetry()["tape_gap_write_failures"] == 1
    finally:
        ctx.store.close()


# --------------------------------------------------------------------------- #
# 로그 소음 — 티어 전이는 개별 줄이 아니라 구간 요약으로 본다
# --------------------------------------------------------------------------- #
def test_tier_transitions_do_not_flood_the_info_log(tmp_path):
    """실측: 최근 1,000줄 중 978줄이 티어 줄이라 워치독 탐지가 무력화됐다."""
    ctx, _ = build_ctx(tmp_path, StubClient({}))
    try:
        before = ctx.notifier.counters["info"]
        for i in range(50):
            ctx.tiers.force(f"S{i}", 2, "price_activity", 1.0, 0, compete=False)
        ctx.flush_changes()
        assert ctx.counters["promotions"] == 50            # 전이는 실제로 일어났고
        assert ctx.notifier.counters["info"] == before     # INFO 줄은 하나도 안 늘었다
    finally:
        ctx.store.close()


def test_telemetry_reports_tier_transition_deltas(tmp_path):
    """사람이 읽는 채널은 구간 요약이다 — 승격/강등 델타가 텔레메트리에 실린다."""
    ctx, _ = build_ctx(tmp_path, StubClient({}))
    try:
        for i in range(3):
            ctx.tiers.force(f"S{i}", 2, "price_activity", 1.0, 0, compete=False)
        ctx.flush_changes()
        data = ctx.report_telemetry(force=True)
        assert data["promotions_delta"] == 3
        ctx.clock.advance(loops.TELEMETRY_EVERY_S + 1)
        data2 = ctx.report_telemetry()
        assert data2["promotions_delta"] == 0              # 구간 델타지 누적이 아니다
    finally:
        ctx.store.close()


# --------------------------------------------------------------------------- #
# 429 그룹 귀속 (전역 카운터를 호출한 쪽 그룹에 귀속하면 엉뚱한 티어가 깎인다)
# --------------------------------------------------------------------------- #
class _FakeLimiter:
    """limiter 의 그룹별 429 흔적만 흉내낸다 (W1 소유 파일은 읽기만 한다)."""

    def __init__(self, blocked=None, backoff=None):
        self.blocked = blocked or {}
        self.backoff = backoff or {}

    def snapshot(self, group):
        return {"blocked_for_s": float(self.blocked.get(group, 0.0)),
                "backoff": float(self.backoff.get(group, 1.0))}


def test_429_is_attributed_to_the_group_that_actually_hit_it(tmp_path):
    """CHART 가 429 를 맞았는데 MARKET_DATA 루프가 관측해도 **CHART** 로 청구한다."""
    client = StubClient({})
    ctx, _ = build_ctx(tmp_path, client)
    try:
        client.limiter = _FakeLimiter(blocked={"MARKET_DATA_CHART": 2.0})
        client.counters["http_429"] = 1
        ctx.sync_rate_limits("MARKET_DATA")               # 호출자는 MARKET_DATA
        assert ctx.budget.rate_limited.get("MARKET_DATA_CHART") == 1
        assert ctx.budget.rate_limited.get("MARKET_DATA") is None
        assert ctx.counters.get("http_429_unattributed", 0) == 0
    finally:
        ctx.store.close()


def test_ambiguous_429_is_flagged_not_silently_misattributed(tmp_path):
    """후보가 여럿이면 귀속하지 않고 **귀속 불가를 표시**한다 (이중 청구도 없다)."""
    client = StubClient({})
    ctx, _ = build_ctx(tmp_path, client)
    try:
        client.limiter = _FakeLimiter(blocked={"MARKET_DATA": 1.0,
                                               "MARKET_DATA_CHART": 1.0})
        client.counters["http_429"] = 1
        ctx.sync_rate_limits("MARKET_DATA")
        assert ctx.counters["http_429_unattributed"] == 1
        # 호출자 그룹으로만 1회 청구 — 두 그룹에 이중 청구되지 않는다
        assert ctx.budget.rate_limited.get("MARKET_DATA") == 1
        assert ctx.budget.rate_limited.get("MARKET_DATA_CHART") is None
    finally:
        ctx.store.close()


def test_one_429_is_charged_once_across_loops(tmp_path):
    """여러 루프가 같은 전역 카운터를 봐도 정원은 한 번만 깎인다."""
    client = StubClient({})
    ctx, _ = build_ctx(tmp_path, client)
    try:
        client.limiter = _FakeLimiter(blocked={"MARKET_DATA": 1.0})
        client.counters["http_429"] = 1
        for group in ("MARKET_DATA", "MARKET_DATA_CHART", "RANKING"):
            ctx.sync_rate_limits(group)                   # 세 루프가 각각 관측
        assert sum(ctx.budget.rate_limited.values()) == 1
    finally:
        ctx.store.close()


# --------------------------------------------------------------------------- #
# 2026-08-04 전환: 랭킹 2종 → 2026-08-07 전환: + TOP_GAINERS(1d) = 3종
# (수집 설정 지문이 분석이 경계를 찾는 열쇠다)
# --------------------------------------------------------------------------- #
def test_the_collected_lists_are_the_two_volume_lists_plus_top_gainers():
    """금액 목록 2종은 여전히 안 받고, 등락률 급상승 1종이 더해졌다.

    `amount_u` 가 micro-KRW 오염 필드라 금액 용도로 못 쓰는데도 하루 80만 행을 쓰고
    있었다 (2026-08-04 제거). 남긴 두 종은 **건수 기준**이고, 둘의 순위 대비가 "개미만
    몰린 종목" 신호다.

    2026-08-07 사용자 결정(D-11)으로 `TOP_GAINERS` 가 더해졌다. 근거는 W1 실측:
    `TOP_GAINERS` 100종 중 **35% 는 우리 랭킹 이력에 한 번도 없다** (`docs/35` §5-6a).
    """
    assert loops.FEATURE_RANKING_TYPES == ("MARKET_TRADING_VOLUME",
                                           "TOSS_SECURITIES_TRADING_VOLUME")
    assert loops.RANKING_TYPES == ("MARKET_TRADING_VOLUME",
                                   "TOSS_SECURITIES_TRADING_VOLUME",
                                   "TOP_GAINERS")
    assert not any("AMOUNT" in t for t in loops.RANKING_TYPES)


def test_duration_is_a_function_of_ranking_type_not_a_free_argument():
    """★ 섞임 방지 장치의 **뿌리**. 이 성질이 깨지면 읽는 쪽의 분리가 통째로 무너진다.

    한 `ranking_type` 이 정확히 한 `duration` 을 결정하므로, `rankings_snap` 을
    `ranking_type` 으로 가르면 `duration` 은 자동으로 갈린다. 분석이 `GROUP BY
    ranking_type` 만 해도 집계창이 섞이지 않는 것은 이 성질 덕분이다.
    """
    from tossmon.api.models import RANKING_DURATIONS, duration_for

    # (1) 수집하는 모든 타입이 등록돼 있다 — 미등록 타입은 추측하지 않고 즉시 실패한다.
    for rtype in loops.RANKING_TYPES:
        assert rtype in RANKING_DURATIONS
        assert duration_for(rtype)
    with pytest.raises(KeyError):
        duration_for("NOT_A_RANKING_TYPE")

    # (2) 실제로 섞여 있다 — 이 테스트가 지키는 상황이 가정이 아니라는 확인.
    durations = {duration_for(t) for t in loops.RANKING_TYPES}
    assert durations == {"realtime", "1d"}

    # (3) 피처 목록은 **한 가지 집계창**이라야 한다. 토스 쏠림도는 두 목록의 순위 대비라
    #     서로 다른 창을 비교하면 그대로 틀린다 (실측 15.4배 차).
    assert {duration_for(t) for t in loops.FEATURE_RANKING_TYPES} == {"realtime"}
    assert duration_for("TOP_GAINERS") == "1d"                 # realtime 은 400 (W1 실측)


@pytest.mark.asyncio
async def test_rankings_loop_asks_each_list_with_its_own_duration(tmp_path):
    """상수만 늘리고 루프가 옛 목록·옛 duration 을 부르면 아무것도 안 바뀐다.

    `TOP_GAINERS` 에 `realtime` 을 보내면 400 이라 **한 행도 안 들어온다** (W1 실측).
    그래서 타입만이 아니라 **타입마다의 duration**과 깊이까지 고정한다.
    """
    class RankClient(StubClient):
        def __init__(self):
            super().__init__({})
            self.asked: list[tuple[str, str, int]] = []

        async def get_rankings(self, rtype, duration="realtime", market="US",
                               count=100, **kw):
            self.asked.append((rtype, duration, count))
            return RankingPage(ranking_type=rtype, duration=duration,
                               ranked_at_ms=DAY0, rows=[])

    client = RankClient()
    ctx, _day = build_ctx(tmp_path, client)
    await loops.rankings_once(ctx)
    assert [a[0] for a in client.asked] == list(loops.RANKING_TYPES)
    assert len(client.asked) == 3
    assert client.asked == [("MARKET_TRADING_VOLUME", "realtime", 100),
                            ("TOSS_SECURITIES_TRADING_VOLUME", "realtime", 100),
                            ("TOP_GAINERS", "1d", 100)]
    assert ctx.counters["ranking_snaps"] == 3          # 세 종 모두 스냅으로 계상된다
    assert ctx.counters.get("ranking_duration_mismatch", 0) == 0
    ctx.store.close()


@pytest.mark.asyncio
async def test_a_duration_the_server_did_not_honour_is_alerted(tmp_path):
    """타입→duration 전제가 서버 쪽에서 깨지면 조용히 넘어가면 안 된다.

    저장은 한다 (랭킹은 과거 조회 불가라 버리면 영구 유실). 다만 분석의 duration 분리가
    이 전제 위에 서 있으므로 카운터·경보로 반드시 드러낸다.
    """
    class LyingClient(StubClient):
        async def get_rankings(self, rtype, duration="realtime", **kw):
            # 서버가 요청과 다른 duration 을 돌려주는 상황.
            return RankingPage(ranking_type=rtype, duration="5d",
                               ranked_at_ms=DAY0, rows=[])

    ctx, _day = build_ctx(tmp_path, LyingClient({}))
    alerts: list[str] = []
    ctx.notifier.alert = alerts.append                 # type: ignore[method-assign]
    await loops.rankings_once(ctx)
    assert ctx.counters["ranking_duration_mismatch"] == len(loops.RANKING_TYPES)
    assert any("duration" in m for m in alerts)
    ctx.store.close()


@pytest.mark.asyncio
async def test_the_1d_list_never_enters_the_realtime_feature_path(tmp_path):
    """★ 섞임 방지 장치. `TOP_GAINERS`(1d)는 DB 까지만 가고 실시간 경로에 안 들어간다.

    실시간 버퍼는 토스 쏠림도(realtime 두 목록의 순위 대비)를 계산하려고 존재한다.
    같은 순간·같은 심볼인데 `1d` 의 `vol_qu` 가 `realtime` 의 중앙값 15.4배라
    (2026-08-07 실측), 버퍼에 섞이면 그 피처가 **예외 없이 조용히** 틀린다.
    검사보다 아예 안 들여보내는 쪽이 확실하다.
    """
    def rows(sym):
        return [RankingRow(rank=1, symbol=sym, last_u=3_000_000, base_u=3_000_000,
                           change_rate=0.5, vol_qu=1_000_000, amount_u=1_000_000)]

    class ThreeListClient(StubClient):
        async def get_rankings(self, rtype, duration="realtime", **kw):
            # 목록마다 다른 심볼을 담아 어느 목록이 버퍼에 들어갔는지 구분한다.
            sym = {"TOP_GAINERS": "GAINONLY"}.get(rtype, "VOLBOTH")
            return RankingPage(ranking_type=rtype, duration=duration,
                               ranked_at_ms=DAY0, rows=rows(sym))

    ctx, _day = build_ctx(tmp_path, ThreeListClient({}))
    # 버퍼에 **무엇이 건네지는가**를 직접 본다. 최종 `ctx.rankings.rows` 를 보면
    # 루프 끝의 `prune(keep=watchlist)` 가 섞여 들어와(스텁 심볼은 tier0 미통과)
    # "안 들어갔다" 와 "들어갔다가 정리됐다" 를 구분하지 못한다.
    handed: list[tuple[str, str]] = []
    real_add = ctx.rankings.add

    def spy_add(snap_ms, page, keep=None):
        handed.append((page.ranking_type, page.duration))
        return real_add(snap_ms, page, keep=keep)

    ctx.rankings.add = spy_add                         # type: ignore[method-assign]
    await loops.rankings_once(ctx)

    # ★ 1d 페이지는 실시간 버퍼에 **한 번도** 건네지지 않는다.
    assert handed == [("MARKET_TRADING_VOLUME", "realtime"),
                      ("TOSS_SECURITIES_TRADING_VOLUME", "realtime")]
    assert {d for _t, d in handed} == {"realtime"}
    # 승격 트리거(`_ranking_triggers` → `ctx.watch`)도 realtime 목록에만 돈다.
    # `_universe_logged` 는 watch 시도에서 걸러진 심볼이 남는 곳이라, "시도조차 안 했다" 와
    # "시도했는데 걸러졌다" 를 가른다 — 대조군이 있어야 이 단언이 공허하지 않다.
    assert ctx._universe_logged == {"VOLBOTH"}

    # DB 에는 세 종이 전부 있다 — 수집이 목적이므로 저장은 빠지면 안 된다.
    stored = dict(ctx.store._conn.execute(
        "SELECT ranking_type, COUNT(*) FROM rankings_snap GROUP BY 1").fetchall())
    assert set(stored) == set(loops.RANKING_TYPES)
    assert stored["TOP_GAINERS"] == 1
    # duration 도 타입별로 정확히 하나씩만 들어가 있다 (읽는 쪽이 기대는 성질).
    pairs = ctx.store._conn.execute(
        "SELECT ranking_type, COUNT(DISTINCT duration) FROM rankings_snap "
        "GROUP BY 1").fetchall()
    assert all(n == 1 for _t, n in pairs)
    assert ctx.store._conn.execute(
        "SELECT duration FROM rankings_snap WHERE ranking_type='TOP_GAINERS'"
    ).fetchone()[0] == "1d"

    # 승격 트리거도 1d 목록에서는 돌지 않는다 (승격 정책은 이번 결정에 없다).
    assert "GAINONLY" not in ctx.watchlist
    ctx.store.close()


def test_config_signature_records_the_collection_shape(tmp_path):
    """★ 요구사항 3 — 전환 경계가 텔레메트리 안에 남아야 한다."""
    ctx, _day = build_ctx(tmp_path, StubClient({}))
    sig = ctx.config_signature()
    assert sig == ctx.telemetry()["config_sig"]                # 5분마다 나가는 리포트에 실린다
    assert " " not in sig                                      # 한 줄 파싱을 깨지 않는다
    # 결정된 값이 지문에 그대로 보여야 사람이 로그만 보고 확인할 수 있다.
    # D-35 (나) 관측 모드(2026-08-28): 4s/4s -> 10s/10s. 배포 경계는 이 지문이 바뀌는 자리다.
    assert "rank3:" in sig and "t3max10" in sig and "ob10s" in sig and "tr10s" in sig
    ctx.store.close()


def test_config_signature_spells_out_the_duration_of_every_list(tmp_path):
    """★ D-11 요구 2 — 경계를 **데이터에 남긴다**. duration 이 지문에 없으면 안 된다.

    2026-08-07 부터 한 테이블에 `realtime` 과 `1d` 가 섞인다. 지문이 타입 수·타입명만
    담으면 분석은 이 경계 전후를 가를 수 있어도 **한 구간 안에서 집계창이 둘이라는 것**을
    모른다. `usage_ratio` 가 `config_sig` 에 없어서 08-04 밤의 0.85→0.70→0.85 가 데이터에
    안 남은 전례가 있다 (`DATA-QUALITY-PROGRAM` 규칙 3). 반복하지 않는다.
    """
    ctx, _day = build_ctx(tmp_path, StubClient({}))
    sig = ctx.config_signature()
    assert "rank3:" in sig
    assert "GAIN/1d" in sig                                    # 등락률 목록과 그 duration
    assert "MVOLUME/rt" in sig and "TVOLUME/rt" in sig         # 거래량 2종은 realtime
    assert f"@{loops.RANKING_COUNT}" in sig                    # 깊이도 경계다
    # 이 지문은 변경 **이전** 지문과 반드시 달라야 한다 — 안 그러면 뭉쳐서 분석된다.
    assert "rank2:M+T" not in sig
    ctx.store.close()


def test_config_signature_changes_when_the_collection_shape_changes(tmp_path):
    """지문이 안 바뀌면 경계를 못 찾는다 — 밀도/폭이 바뀌면 반드시 달라져야 한다."""
    base, _ = build_ctx(tmp_path / "a", StubClient({}))
    wider, _ = build_ctx(tmp_path / "b", StubClient({}), universe={"tier3_max": 20})
    denser, _ = build_ctx(tmp_path / "c", StubClient({}), polling={"tier3_orderbook_s": 2})
    sigs = {base.config_signature(), wider.config_signature(), denser.config_signature()}
    assert len(sigs) == 3
    for c in (base, wider, denser):
        c.store.close()


def test_startup_logs_the_config_boundary(tmp_path):
    """로그에도 한 줄 남는다 — 전환 시각을 로그에서 바로 찾을 수 있어야 한다."""
    ctx, _day = build_ctx(tmp_path, StubClient({}))
    seen: list[str] = []
    ctx.notifier.info = seen.append                    # type: ignore[method-assign]
    ctx.log_config_signature("start")
    lines = [m for m in seen if "COLLECTION-CONFIG" in m]
    assert len(lines) == 1 and ctx.config_signature() in lines[0]
    ctx.store.close()


@pytest.mark.asyncio
async def test_run_all_marks_the_boundary_before_collecting(tmp_path):
    """경계 줄은 **수집 시작 전에** 나가야 한다 — 뒤에 오면 첫 구간이 미표시로 남는다."""
    ctx, _day = build_ctx(tmp_path, StubClient({}))
    seen: list[str] = []
    ctx.notifier.info = seen.append                    # type: ignore[method-assign]
    await loops.run_all(ctx, cycles=1)
    assert any("COLLECTION-CONFIG" in m for m in seen)
    ctx.store.close()


# --------------------------------------------------------------------------- #
# 1초 창 준수 — tier1 배치 버스트 평탄화 (2026-08-04)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_tier1_batches_are_spread_so_they_do_not_burst_in_one_second(tmp_path):
    """★ 평균 0.178 req/s 뒤에 숨어 있던 8콜 버스트를 편다.

    1500종목/배치200 = 8콜. 연속으로 쏘면 그 1초에 MARKET_DATA 가 8회를 먹고, 같은 초의
    tier3 폴링과 겹치면 한도 10 을 넘는다 — 평균만 보면 절대 안 보이는 사고다.
    """
    client = StubClient({f"S{i}": Price(symbol=f"S{i}", ts_ms=DAY0, last_u=1_000_000)
                         for i in range(600)})
    ctx, _day = build_ctx(tmp_path, client, symbols=tuple(f"S{i}" for i in range(600)))
    try:
        start = ctx.clock.now_ms()
        await loops.tier1_sweep_once(ctx)
        # 600종목 = 3배치. 배치 사이가 벌어졌으므로 시간이 흘러야 한다.
        elapsed_s = (ctx.clock.now_ms() - start) / 1000.0
        assert client.counters["requests"] == 3
        assert elapsed_s >= 2.0, "배치가 여전히 연속으로 나간다 (버스트 그대로)"
        # 예산이 보는 초당 첨두가 배치 수보다 작아야 한다.
        assert ctx.budget.peak_1s(loops.GROUP_MARKET_DATA) < 3
    finally:
        ctx.store.close()


@pytest.mark.asyncio
async def test_spreading_does_not_overrun_the_sweep_period(tmp_path):
    """펴는 것이 주기를 잡아먹으면 안 된다 — 주기 절반 안에 끝나야 한다."""
    client = StubClient({f"S{i}": Price(symbol=f"S{i}", ts_ms=DAY0, last_u=1_000_000)
                         for i in range(2000)})
    ctx, _day = build_ctx(tmp_path, client, symbols=tuple(f"S{i}" for i in range(2000)),
                          polling={"tier1_sweep_s": 45})
    try:
        start = ctx.clock.now_ms()
        await loops.tier1_sweep_once(ctx)
        elapsed_s = (ctx.clock.now_ms() - start) / 1000.0
        assert elapsed_s <= 45 * 0.5 + 1e-6      # 주기 절반 이내
    finally:
        ctx.store.close()


# --------------------------------------------------------------------------- #
# 2026-08-04 정규장 개장 사고 — 예산 거버너가 정원을 통째로 날렸다 (docs/33)
# --------------------------------------------------------------------------- #
def test_one_groups_burst_is_not_charged_to_another_group(tmp_path):
    """★ 사고 1차 원인 — 계상이 **전역** 카운터의 델타를 호출한 그룹에 얹었다.

    실측: 22:30:38 "CHART 1초에 6회" 경보. 그때 tier2 는 18종목이고 주기는 110초라
    CHART 자체 지속률은 0.16 req/s 다. 게다가 하드캡이 CHART 를 1.15초에 5회로 묶는다 —
    6회는 **CHART 가 낼 수 있는 수가 아니다.** 개장에 동시 실행되던 다른 그룹(MARKET_DATA,
    RANKING, STOCK)의 호출이 전역 델타로 CHART 에 얹힌 것이다.

    2026-08-08 (docs/46): 귀속 근거가 `client.sent_by_group` 으로 바뀌었다. 더블도
    실제 client 처럼 그룹별로 세게 해서, 이 시나리오가 **계상까지 실제로 도달하도록** 둔다 —
    안 그러면 CHART 가 0건 계상되어 이 단언이 아무것도 시험하지 않는다.
    """
    client = StubClient({})
    ctx, _day = build_ctx(tmp_path, client)
    try:
        client.sent(loops.GROUP_CHART, 1)
        ctx.after_call(loops.GROUP_CHART, calls=1)         # CHART 자기 호출 1건
        ctx.clock.advance(0.05)
        # 그 사이 MARKET_DATA 가 10건 나갔다 — CHART 는 아무것도 안 했다.
        client.sent(loops.GROUP_MARKET_DATA, 10)
        ctx.clock.advance(0.05)
        client.sent(loops.GROUP_CHART, 1)
        ctx.after_call(loops.GROUP_CHART, calls=1)         # CHART 자기 호출 1건 더

        assert ctx.budget.counters.get(loops.GROUP_CHART, 0) == 2, "전제: CHART 2건"
        peak = ctx.budget.peak_1s(loops.GROUP_CHART)
        assert peak <= 2, (
            f"CHART 가 2건만 냈는데 첨두 {peak} 로 계상됐다 — 남의 버스트가 얹혔다")
    finally:
        ctx.store.close()


def test_an_open_burst_does_not_collapse_the_tier(tmp_path):
    """★ 사고 2차 원인 — 1초 버스트가 **지속 속도 손잡이**를 돌린다.

    실측 22:33:14: `budget shrink {MARKET_DATA: 9, MARKET_DATA_CHART: 299}` 로
    한 번에 316종목이 강등됐다. tier2 종목은 1/110 = 0.00909 req/s 라 "1초에 1회 초과" 를
    지속 초과로 환산하면 110종목을 빼라는 답이 나온다 — 산수는 맞지만 손잡이가 틀렸다.
    """
    ctx, _day = build_ctx(tmp_path, StubClient({}))
    try:
        ctx.tiers.set_capacity(ts_ms=ctx.clock.now_ms(), tier2_max=300, tier3_max=10)
        ctx.refresh_plan()
        # 개장의 실제 모양: **5초마다 7건이 몰리는 버스트**를 2분간 반복한다.
        # 평균은 7/5 = 1.4 req/s 로 CHART 목표(4.25)의 3분의 1이지만, 매 순간의
        # 초당 첨두는 7 이라 천장(4.04)을 계속 넘는다 — 버스트지 과부하가 아니다.
        for _ in range(24):
            for _ in range(7):
                ctx.budget.on_request(loops.GROUP_CHART)
                ctx.clock.advance(0.02)
            ctx.clock.advance(5.0 - 7 * 0.02)
            ctx.apply_budget()
        assert ctx.budget.measured_rate(loops.GROUP_CHART) < 2.0   # 지속률은 멀쩡하다

        cap2 = ctx.tiers.capacity.get(2)
        assert cap2 is not None and cap2 >= 150, (
            f"1초 버스트로 tier2 정원이 {cap2} 로 무너졌다 (300 에서 시작)")
    finally:
        ctx.store.close()


def test_a_genuine_sustained_overload_still_shrinks(tmp_path):
    """대조군 — 경보만 끄는 게 아니라는 증거. 진짜 지속 과부하는 여전히 깎아야 한다."""
    ctx, _day = build_ctx(tmp_path, StubClient({}))
    try:
        ctx.tiers.set_capacity(ts_ms=ctx.clock.now_ms(), tier2_max=300, tier3_max=10)
        ctx.refresh_plan()
        before = ctx.tiers.capacity.get(2)
        # 지속 과부하: CHART 를 6 req/s 로 3분간 (평균도 첨두도 천장 위)
        for _ in range(18):
            for _ in range(60):
                ctx.budget.on_request(loops.GROUP_CHART)
                ctx.clock.advance(1.0 / 6)
            ctx.apply_budget()
        after = ctx.tiers.capacity.get(2)
        assert after < before, "진짜 지속 과부하인데 축소가 전혀 없었다"
    finally:
        ctx.store.close()


def test_open_burst_keeps_filling_tier3_with_legitimate_promotions(tmp_path):
    """★ 요구사항 2 — "전이 0" 을 안정화로 읽지 않는다 (COORDINATOR-STATE §4.4-E).

    성공 기준은 **진동 0 그리고 신규 승격 > 0** 이다. 버스트 때문에 축소를 멈춘 것이
    "아무 일도 안 하게" 만든 것이라면 그건 고친 게 아니라 죽인 것이다.
    """
    ctx, _day = build_ctx(tmp_path, StubClient({}))
    try:
        ctx.tiers.set_capacity(ts_ms=ctx.clock.now_ms(), tier2_max=300, tier3_max=10)
        ctx.refresh_plan()
        promoted: set[str] = set()
        moves: dict[str, int] = {}

        for step in range(40):
            # 개장 버스트를 계속 때린다 (사고 조건 그대로)
            for _ in range(7):
                ctx.budget.on_request(loops.GROUP_CHART)
                ctx.clock.advance(0.02)
            # 진짜 뜨거운 종목들이 계속 들어온다
            now = ctx.clock.now_ms()
            for i in range(12):
                sym = f"HOT{i}"
                before = ctx.tiers.tier_of(sym)
                ctx.tiers.on_new_data(sym, 0.95 - i * 0.01, now)
                after = ctx.tiers.tier_of(sym)
                if after != before:
                    moves[sym] = moves.get(sym, 0) + 1
                if after == 3:
                    promoted.add(sym)
            filled = ctx.tiers.fill_to_capacity(3, now)
            promoted.update(filled)
            ctx.flush_changes()
            ctx.clock.advance(5.0)
            ctx.apply_budget()

        cap3 = ctx.tiers.capacity.get(3)
        assert cap3 is not None and cap3 >= 8, f"tier3 정원이 {cap3} 로 깎였다"
        assert len(promoted) > 0, "승격이 한 건도 없다 — 안정화가 아니라 동결이다"
        flappers = {s: n for s, n in moves.items() if n > 3}
        assert not flappers, f"진동하는 종목이 있다: {flappers}"
    finally:
        ctx.store.close()

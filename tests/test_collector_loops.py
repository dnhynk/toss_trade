"""수집 루프 단위 — 버퍼·회계·상태복원, 그리고 W1 이 실측한 함정 6종의 대응 검증."""
from __future__ import annotations

import asyncio

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


def _page(symbol, rank, amount_u, rtype="TOSS_SECURITIES_TRADING_AMOUNT"):
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
        self.counters = {"http_429": 0, "requests": 0}
        self.last_headers: dict[str, str] = {}
        self.requested: list[list[str]] = []

    async def get_prices(self, symbols):
        self.requested.append(list(symbols))
        self.counters["requests"] += 1
        # 함정1: 모르는 심볼은 404 가 아니라 응답에서 조용히 빠진다.
        return [p for p in (self.known.get(s) for s in symbols) if p is not None]

    async def get_us_calendar(self, date=None):
        return calendar_dict([simple_day("2026-07-30", DAY0)], 0)


def build_ctx(tmp_path, client, *, now_ms=None, symbols=(), **cfg_sections):
    cfg = make_config(tmp_path, **cfg_sections)
    store = Store(cfg.store.db_path)
    day = simple_day("2026-07-30", DAY0)
    clock = FrozenClock(now_ms if now_ms is not None else day.regular.start_ms + MIN_MS)
    ctx = CollectorContext.create(client, store, cfg, notifier=Notifier(console=False),
                                  clock=clock, symbols=symbols)
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
    assert ctx.prev_close["AAA"] != 9_999_999            # 진행형 봉을 전일종가로 쓰지 않는다
    assert ctx.baselines["AAA"]["n_days"] == 5
    # 저장은 받은 그대로 (당일 봉도 DB 에는 들어간다 — 자르는 것은 베이스라인 계산뿐)
    assert ctx.store._conn.execute("SELECT COUNT(*) FROM candles_1d").fetchone()[0] == 6
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
        ctx.detector._emitted[("ABCD", 111)] = "digest"

        ctx.drop_symbol_state("ABCD")

        assert "ABCD" not in ctx.buffers                  # 메모리는 회수하고
        assert "ABCD" not in ctx.curves
        assert ("ABCD", 111) in ctx.detector._emitted     # 이력은 남긴다
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
    assert caps_regular == {"tier2_max": 300, "tier3_max": 20}

    for i in range(5):                                   # tier3 를 5개 채운다
        sym = f"S{i}"
        ctx.tiers.on_new_data(sym, 0.9, 0)
        ctx.tiers.on_new_data(sym, 0.9, 200_000)
    ctx.flush_changes()
    assert len(ctx.tiers.members(3)) == 5

    caps_day = loops.reconfigure_tiers(ctx, "day")        # 얇은 세션 → 감시 축소
    assert caps_day["tier3_max"] == 8
    caps_after = loops.reconfigure_tiers(ctx, "after")
    assert caps_after["tier3_max"] == 10
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

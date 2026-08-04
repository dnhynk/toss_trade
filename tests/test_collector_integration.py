"""통합 — 실제 HTTP(mock 서버) + TossClient + 루프 + SQLite.

계약 C-9 대로 `live=False` 고정 토큰만 쓴다. 실서버 호출 경로는 이 파일에 없다.
"""
from __future__ import annotations

import asyncio

from tests.test_collector_helpers import (MIN_MS, FrozenClock, build_client, make_config,
                                          mock_server)
from tossmon.collector import loops
from tossmon.collector.loops import CollectorContext
from tossmon.collector.notifier import Notifier
from tossmon.store.writer import Store

WATCH = ("AAPL", "SNTI", "BTAI", "CRKN")


def set_inject(httpd, mode: str | None) -> None:
    """서버를 띄운 뒤 오류 주입을 켠다 — 컨텍스트 구성(캘린더 조회)은 정상 응답으로 마치고
    본문에서만 사고를 재현하기 위해서다."""
    httpd.RequestHandlerClass.inject = mode


async def make_ctx(base_url, tmp_path, *, symbols=WATCH, offset_min=1,
                   session_key="regular", **cfg_sections):
    """mock 서버를 향한 컨텍스트. 시계는 픽스처 캘린더의 세션 안으로 고정한다.

    한 테스트 = 한 이벤트루프다. httpx 커넥션 풀이 루프에 묶이므로 `asyncio.run` 을
    테스트당 두 번 부르면 두 번째에서 'Event loop is closed' 가 난다.
    """
    client = build_client(base_url, tmp_path)
    cal = await client.get_us_calendar()
    win = getattr(cal["today"], session_key)
    clock = FrozenClock(win.start_ms + offset_min * MIN_MS)
    cfg = make_config(tmp_path, **cfg_sections)
    store = Store(cfg.store.db_path)
    ctx = CollectorContext.create(client, store, cfg, notifier=Notifier(console=False),
                                  clock=clock, symbols=symbols)
    ctx.scheduler.calendar = cal
    ctx.scheduler.fetched_ms = clock.now_ms()
    ctx.session = session_key
    return ctx


async def close(ctx):
    await ctx.client.aclose()
    ctx.store.close()


# --------------------------------------------------------------------------- #
# 랭킹 — 최우선 수집 대상
# --------------------------------------------------------------------------- #
async def test_rankings_snapshot_every_configured_type_over_http(tmp_path):
    with mock_server() as (base_url, _httpd):
        ctx = await make_ctx(base_url, tmp_path)
        try:
            await loops.rankings_once(ctx)
            rows = ctx.store._conn.execute(
                "SELECT ranking_type, COUNT(*) FROM rankings_snap GROUP BY 1").fetchall()
            types = {r[0] for r in rows}
            assert types == set(loops.RANKING_TYPES)          # 설정된 종류 전부
            assert all(n > 0 for _t, n in rows)
            assert ctx.counters["ranking_snaps"] == len(loops.RANKING_TYPES)
            assert ctx.counters["req_RANKING"] == len(loops.RANKING_TYPES)
        finally:
            await close(ctx)


async def test_ranking_top_entry_promotes_and_accumulates_watchlist(tmp_path):
    """토스 랭킹 상위는 승격 트리거다 — 단, **tier0 유니버스 통과분만** (감사 F-2).

    mock 랭킹 픽스처의 상위권은 실서버처럼 메가캡이 대부분이다. 게이트가 없던 시절에는
    이들이 전부 워치리스트·tier2 로 들어갔다 — 지금은 `/stocks` 메타로 가격·시총을
    판정해 통과분만 남는지를 HTTP 전 구간으로 확인한다.
    """
    with mock_server() as (base_url, _httpd):
        ctx = await make_ctx(base_url, tmp_path, symbols=())
        try:
            await loops.rankings_once(ctx)
            assert ctx.watchlist                               # 통과분으로 스스로 채워졌다
            # 워치리스트는 전원 tier0 통과분이다
            assert all(ctx.universe_status.get(s) is True for s in ctx.watchlist)
            # 메가캡(가격 $20 초과)은 랭킹 상위라도 거부된다 — 픽스처의 실측 대형주들
            for mega in ("MSFT", "QQQ", "META", "NVDA", "SOXL"):
                assert mega not in ctx.watchlist, mega
            assert ctx.counters["universe_rejected"] > 0
            assert ctx.telemetry()["watch_outside_universe"] == 0
            promoted = ctx.store._conn.execute(
                "SELECT symbol FROM promotions WHERE reason='ranking_entry'").fetchall()
            assert promoted                                    # 통과분 승격은 여전히 동작한다
            assert all(ctx.tiers.tier_of(sym) >= 2 for (sym,) in promoted)
            assert all(ctx.universe_status.get(sym) is True for (sym,) in promoted)
        finally:
            await close(ctx)


async def test_ranking_rows_survive_a_full_snapshot_round_trip(tmp_path):
    """rankings 는 과거 조회가 불가능하다 — 응답 형태(price 블록 중첩)를 놓치면 영구 손실."""
    with mock_server() as (base_url, _httpd):
        ctx = await make_ctx(base_url, tmp_path)
        try:
            await loops.rankings_once(ctx)
            row = ctx.store._conn.execute(
                "SELECT symbol, last_u, vol_qu, amount_u FROM rankings_snap "
                "WHERE ranking_type='TOSS_SECURITIES_TRADING_VOLUME' AND rank=1"
            ).fetchone()
            assert row is not None
            symbol, last_u, vol_qu, amount_u = row
            assert symbol and last_u > 0 and vol_qu >= 0 and amount_u > 0
        finally:
            await close(ctx)


# --------------------------------------------------------------------------- #
# Tier 1 — 배치 스윕과 조용한 누락
# --------------------------------------------------------------------------- #
async def test_tier1_sweep_stores_activity_and_detects_silent_omission(tmp_path):
    """함정1: `--strict` mock 은 모르는 심볼을 200 응답에서 조용히 뺀다."""
    with mock_server(strict=True) as (base_url, _httpd):
        ctx = await make_ctx(base_url, tmp_path, symbols=(*WATCH, "NOSUCHSYM"))
        try:
            seen = await loops.tier1_sweep_once(ctx)
            assert seen == len(WATCH)                          # 4개만 돌아온다
            assert ctx.counters["prices_missing"] == 1
            assert ctx.missing_streak["NOSUCHSYM"] == 1
            assert "NOSUCHSYM" in ctx.watchlist                # 아직 유예
        finally:
            await close(ctx)


async def test_tier1_batches_are_chunked_by_200(tmp_path):
    with mock_server() as (base_url, _httpd):
        symbols = tuple(f"SYM{i:04d}" for i in range(450))
        ctx = await make_ctx(base_url, tmp_path, symbols=symbols)
        try:
            seen = await loops.tier1_sweep_once(ctx)
            assert seen == 450
            assert ctx.counters["req_MARKET_DATA"] == 3        # 200+200+50
        finally:
            await close(ctx)


# --------------------------------------------------------------------------- #
# Tier 3 — 테이프/호가
# --------------------------------------------------------------------------- #
async def test_tier3_micro_stores_trades_and_a_single_level_book(tmp_path):
    with mock_server() as (base_url, _httpd):
        ctx = await make_ctx(base_url, tmp_path)
        try:
            await loops._poll_trades(ctx, "AAPL")
            await loops._poll_orderbook(ctx, "AAPL")

            trades = ctx.store._conn.execute(
                "SELECT symbol, ts_ms, price_u, qty_u FROM trades_snap").fetchall()
            assert trades
            # 함정6: 응답에 symbol 이 없다 — client 가 주입한 값이 그대로 저장돼야 한다.
            assert {t[0] for t in trades} == {"AAPL"}

            book = ctx.store._conn.execute(
                "SELECT bid1_u, ask1_u, spread_u, depth_json FROM orderbook_snap"
            ).fetchone()
            assert book is not None
            bid1, ask1, spread, depth = book
            assert ask1 > bid1 and spread == ask1 - bid1
            assert depth.count('"price_u"') == 2               # A2 §1: 양쪽 1레벨뿐
        finally:
            await close(ctx)


async def test_repeated_trade_polls_are_idempotent(tmp_path):
    """폴링이 겹쳐도 (symbol, ts, price, qty) PK 가 중복을 흡수한다 (계약 C-6)."""
    with mock_server() as (base_url, _httpd):
        ctx = await make_ctx(base_url, tmp_path)
        try:
            first = await loops._poll_trades(ctx, "AAPL")
            second = await loops._poll_trades(ctx, "AAPL")
            total = ctx.store._conn.execute(
                "SELECT COUNT(*) FROM trades_snap").fetchone()[0]
            assert first > 0 and second == 0 and total == first
        finally:
            await close(ctx)


# --------------------------------------------------------------------------- #
# 예산 사고 (429)
# --------------------------------------------------------------------------- #
async def test_429_storm_reaches_the_budget_guard_and_shrinks_tier3(tmp_path):
    """재시도까지 실패해 예외로 끝난 429 도 가드가 봐야 한다 — 그게 사고 신호다."""
    with mock_server() as (base_url, httpd):
        ctx = await make_ctx(base_url, tmp_path, universe={"tier3_max": 6})
        set_inject(httpd, "429")
        try:
            for i in range(6):                                 # tier3 정원을 채워 둔다
                sym = f"S{i}"
                ctx.tiers.on_new_data(sym, 0.9, 0)
                ctx.tiers.on_new_data(sym, 0.9, 200_000)
            ctx.flush_changes()
            before = len(ctx.tiers.members(3))

            await loops.run_tier1_price_sweep(ctx.client, ctx.store, ctx.cfg,
                                             ctx=ctx, cycles=1)
            assert ctx.client.counters["http_429"] >= 1
            assert ctx.budget.rate_limited.get("MARKET_DATA", 0) >= 1
            assert ctx.counters["budget_shrinks"] >= 1
            assert ctx.tiers.capacity[3] < 6                   # 정원 축소
            assert len(ctx.tiers.members(3)) < before          # 넘치는 만큼 안전 강등
            assert ctx.running()                               # 429 로 죽지 않는다
        finally:
            await close(ctx)


async def test_schema_drift_is_skipped_not_fatal(tmp_path):
    with mock_server() as (base_url, httpd):
        ctx = await make_ctx(base_url, tmp_path)
        set_inject(httpd, "schema-drift")
        try:
            await loops.run_tier1_price_sweep(ctx.client, ctx.store, ctx.cfg,
                                             ctx=ctx, cycles=2)
            assert ctx.counters.get("schema_mismatch", 0) >= 1
            assert ctx.running()
        finally:
            await close(ctx)


async def test_forbidden_is_fatal_over_http(tmp_path):
    with mock_server() as (base_url, httpd):
        ctx = await make_ctx(base_url, tmp_path)
        set_inject(httpd, "403")
        try:
            await loops.run_rankings(ctx.client, ctx.store, ctx.cfg, ctx=ctx, cycles=3)
            assert not ctx.running()
            assert ctx.notifier.counters["alert"] >= 1
        finally:
            await close(ctx)


async def test_expired_token_is_retried_by_the_client_not_the_loop(tmp_path):
    """계약 C-5: AuthExpired 는 client 내부에서 1회 재시도된다 (루프는 모른다)."""
    with mock_server(inject="401", inject_every=2) as (base_url, _httpd):
        ctx = await make_ctx(base_url, tmp_path)
        try:
            await loops.run_rankings(ctx.client, ctx.store, ctx.cfg, ctx=ctx, cycles=1)
            assert ctx.client.counters["auth_refresh"] >= 1
            assert ctx.running()
        finally:
            await close(ctx)


# --------------------------------------------------------------------------- #
# 계약 C-2 개정 A5 — 전선(wire)에 실제로 나가는 adjusted 파라미터
# --------------------------------------------------------------------------- #
def record_queries(httpd) -> list[tuple[str, dict]]:
    """mock 서버가 **실제로 받은** 쿼리를 기록한다 (tools/ 는 건드리지 않는다)."""
    seen: list[tuple[str, dict]] = []
    handler = httpd.RequestHandlerClass
    original = handler._resolve

    def spy(self, path, query):
        seen.append((path, {k: v[0] for k, v in query.items()}))
        return original(self, path, query)

    handler._resolve = spy
    return seen


async def test_adjusted_parameter_on_the_wire_follows_a5(tmp_path):
    """스텁이 아니라 HTTP 쿼리스트링으로 확인한다 — 직렬화 단계에서 뒤집히면 무의미하다."""
    with mock_server() as (base_url, httpd):
        seen = record_queries(httpd)
        ctx = await make_ctx(base_url, tmp_path)
        try:
            await loops.tier2_symbol_once(ctx, "AAPL")     # 백필 + 일봉 + 증분
            candles = [q for path, q in seen if path == "/api/v1/candles"]
            assert candles, "캔들 요청이 한 건도 관측되지 않았다"

            by_interval: dict[str, set[str]] = {}
            for q in candles:
                by_interval.setdefault(q["interval"], set()).add(q["adjusted"])
            # 1분봉은 원주가 — 명목 가격대(동전주 여부)를 보존해야 한다
            assert by_interval["1m"] == {"false"}
            # 일봉은 수정주가 — 20일 베이스라인은 분할을 가로지른다
            assert by_interval["1d"] == {"true"}
        finally:
            await close(ctx)


# --------------------------------------------------------------------------- #
# 세션 / 캘린더 / 재시작
# --------------------------------------------------------------------------- #
async def test_session_is_driven_by_the_calendar_endpoint(tmp_path):
    with mock_server() as (base_url, _httpd):
        ctx = await make_ctx(base_url, tmp_path)
        try:
            cal = ctx.scheduler.calendar
            for name in ("day", "pre", "regular", "after"):
                win = getattr(cal["today"], name)
                ctx.clock._now = win.start_ms + MIN_MS
                assert ctx.current_session() == name
            ctx.clock._now = cal["today"].after.end_ms + MIN_MS
            assert ctx.current_session() == "closed"
            assert not ctx.collecting()
        finally:
            await close(ctx)


async def test_session_watch_reconfigures_tiers_on_transition(tmp_path):
    with mock_server() as (base_url, _httpd):
        ctx = await make_ctx(base_url, tmp_path, session_key="regular")
        try:
            ctx.session = "closed"
            await loops.run_session_watch(ctx, cycles=1)
            assert ctx.session == "regular"
            assert ctx.counters["session_changes"] == 1
            assert ctx.tiers.capacity[3] == ctx.cfg.universe.tier3_max

            ctx.clock._now = ctx.scheduler.calendar["today"].day.start_ms + MIN_MS
            await loops.run_session_watch(ctx, cycles=1)
            assert ctx.session == "day"
            assert ctx.tiers.capacity[3] < ctx.cfg.universe.tier3_max
        finally:
            await close(ctx)


async def test_restart_resumes_and_stays_idempotent(tmp_path):
    with mock_server() as (base_url, _httpd):
        ctx = await make_ctx(base_url, tmp_path)
        try:
            await loops.rankings_once(ctx)
            await loops.tier1_sweep_once(ctx)
            ctx.tiers.force("AAPL", 2, "price_activity", 0.9, ctx.clock.now_ms())
            ctx.flush_changes()
            await loops.tier2_symbol_once(ctx, "AAPL")
            bars_before = ctx.store._conn.execute(
                "SELECT COUNT(*) FROM candles_1m").fetchone()[0]
            ctx.save_state(force=True)
        finally:
            await close(ctx)

        assert bars_before > 0
        ctx2 = await make_ctx(base_url, tmp_path)
        try:
            assert ctx2.tiers.tier_of("AAPL") == 2             # 티어 복원
            assert ctx2.counters["resumes"] == 1
            await loops.tier2_symbol_once(ctx2, "AAPL")
            bars_after = ctx2.store._conn.execute(
                "SELECT COUNT(*) FROM candles_1m").fetchone()[0]
            # 같은 구간을 다시 받아도 (symbol, ts_ms) upsert 라 행이 불어나지 않는다
            assert bars_after == bars_before
            promos = ctx2.store._conn.execute(
                "SELECT COUNT(*) FROM promotions WHERE reason='resume'").fetchone()[0]
            assert promos == 0                                  # 이어받기는 승격이 아니다
        finally:
            await close(ctx2)


async def test_history_comes_from_the_db_when_the_backfill_already_ran(tmp_path):
    """docs/03 §6: 1분봉은 백필로 대체 가능 — 있으면 API 대신 DB 를 읽는다."""
    with mock_server() as (base_url, _httpd):
        ctx = await make_ctx(base_url, tmp_path)
        try:
            await loops.tier2_symbol_once(ctx, "AAPL")          # 1차: API 백필
            chart_calls = ctx.counters["req_MARKET_DATA_CHART"]
            assert chart_calls >= 2                             # 1m 백필 + 1d 베이스라인
        finally:
            await close(ctx)

        ctx2 = await make_ctx(base_url, tmp_path)
        try:
            await loops.tier2_symbol_once(ctx2, "AAPL")
            assert ctx2.counters.get("history_from_db", 0) == 1
            assert ctx2.counters["req_MARKET_DATA_CHART"] < chart_calls
        finally:
            await close(ctx2)


async def test_full_run_all_survives_a_few_cycles(tmp_path):
    """5개 task 를 동시에 띄워도 서로를 죽이지 않는다 (독립 task 규약)."""
    with mock_server() as (base_url, _httpd):
        ctx = await make_ctx(base_url, tmp_path)
        try:
            await asyncio.wait_for(loops.run_all(ctx, cycles=2), timeout=60)
            assert ctx.counters["ranking_snaps"] >= len(loops.RANKING_TYPES)
            assert ctx.store._conn.execute(
                "SELECT COUNT(*) FROM rankings_snap").fetchone()[0] > 0
            assert ctx.counters.get("loop_errors", 0) == 0
        finally:
            await close(ctx)

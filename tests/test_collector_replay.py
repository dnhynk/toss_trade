"""가속 리플레이 통합 — 정규장 한 세션(2.5시간분)을 수십 초로 압축해 전 파이프라인을 돌린다.

무엇을 증명하는가
    * 5개 루프가 **동시에** 돌면서 서로를 죽이지 않는다
    * 급등 종목은 승격되고 이벤트가 `events` 에 기록된다 (meta_json 라벨 포함)
    * 조용한 종목은 tier3 로 올라가지 않는다 (오탐 방지)
    * 랭킹·테이프·호가처럼 **사후 조회가 불가능한** 데이터가 실제로 쌓인다
    * 이력이 DB 에 있으면 API 백필 없이 RVOL 곡선이 선다 (docs/03 §6)

시간은 `VirtualClock(scale)` 이 압축한다. 데이터는 `ReplayClient` 가 "지금까지 완성된 봉"
만 내주므로, 루프 입장에서는 실서버와 구분되지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import time

from tests import synth
from tests.test_collector_helpers import (MIN_MS, ReplayClient, VirtualClock, make_config)
from tossmon.api.models import Candle
from tossmon.collector import loops
from tossmon.collector.loops import CollectorContext
from tossmon.collector.notifier import Notifier
from tossmon.store.writer import Store

#: 가상시간 배속. 155 분(9,300초)을 ~19 초로 압축한다.
SPEED = 500.0
#: 리플레이 구간 — 정규장 시작 5분 전부터 T0 이후 충분히 지난 시점까지.
LEAD_MIN = 5
SPAN_MIN = 155

RUNNER = "RUNR"
QUIET = "QUIT"


def _seed_history(store: Store, frames: dict, start_ms: int) -> int:
    """리플레이 시작 이전 봉을 DB 에 미리 넣는다 (= 백필러가 이미 돌아간 상태)."""
    total = 0
    for symbol, df in frames.items():
        hist = df[df["ts_ms"] < start_ms]
        rows = [Candle(symbol=symbol, ts_ms=int(r.ts_ms), open_u=int(r.open_u),
                       high_u=int(r.high_u), low_u=int(r.low_u), close_u=int(r.close_u),
                       vol_qu=int(r.vol_qu)) for r in hist.itertuples()]
        total += store.upsert_candles_1m(rows)
    return total


def _build(tmp_path):
    import pandas as pd

    df_run, truth_run = synth.make_scenario("coil_pop", seed=1, symbol=RUNNER)
    df_quiet, truth_quiet = synth.make_scenario("noise", seed=1, symbol=QUIET)
    frames = {RUNNER: df_run, QUIET: df_quiet}
    truths = {RUNNER: truth_run, QUIET: truth_quiet}
    calendar = truth_run["calendar"]
    md = truth_run["market_day"]

    start_ms = md.regular.start_ms - LEAD_MIN * MIN_MS
    end_ms = md.regular.start_ms + SPAN_MIN * MIN_MS
    rankings = pd.concat([truth_run["rankings"], truth_quiet["rankings"]],
                         ignore_index=True)

    cfg = make_config(tmp_path)
    store = Store(cfg.store.db_path)
    seeded = _seed_history(store, frames, start_ms)

    clock = VirtualClock(start_ms, scale=SPEED)
    client = ReplayClient(clock, frames, truths, calendar, rankings=rankings)
    ctx = CollectorContext.create(client, store, cfg, notifier=Notifier(console=False),
                                  clock=clock, symbols=(RUNNER, QUIET))
    return ctx, truths, md, end_ms, seeded


async def _run_until(ctx, end_ms: int, *, real_timeout: float = 120.0) -> float:
    """가상시각이 `end_ms` 를 넘으면 정지 — 실제 벽시계로 걸린 시간을 반환."""
    async def watchdog():
        while ctx.clock.now_ms() < end_ms and ctx.running():
            await asyncio.sleep(0.02)
        ctx.stop.set()

    started = time.monotonic()
    guard = asyncio.create_task(watchdog())
    try:
        await asyncio.wait_for(loops.run_all(ctx), timeout=real_timeout)
    finally:
        guard.cancel()
        await asyncio.gather(guard, return_exceptions=True)
    return time.monotonic() - started


async def test_regular_session_replay_detects_the_runner_and_ignores_the_quiet_one(tmp_path):
    ctx, truths, md, end_ms, seeded = _build(tmp_path)
    assert seeded > 0                                   # 이력이 DB 에 있다

    try:
        elapsed = await _run_until(ctx, end_ms)
        conn = ctx.store._conn

        # --- 압축이 실제로 일어났는가 -------------------------------------
        virtual_min = (ctx.clock.now_ms() - (md.regular.start_ms - LEAD_MIN * MIN_MS)) // MIN_MS
        assert virtual_min >= SPAN_MIN                   # 155분을
        assert elapsed < 90.0                            # 수십 초로

        # --- 사후 조회가 불가능한 데이터가 쌓였는가 -----------------------
        assert conn.execute("SELECT COUNT(*) FROM rankings_snap").fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM trades_snap").fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM orderbook_snap").fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM candles_1m WHERE symbol=?",
                            (RUNNER,)).fetchone()[0] > 0

        # --- 급등 종목은 tier3 까지 올라갔는가 -----------------------------
        tiers = dict(conn.execute(
            "SELECT symbol, MAX(to_tier) FROM promotions GROUP BY symbol").fetchall())
        assert tiers.get(RUNNER, 0) == 3
        assert tiers.get(QUIET, 0) < 3                   # 조용한 종목은 오르지 않는다

        # --- 이벤트가 제때 잡혔는가 ----------------------------------------
        events = conn.execute(
            "SELECT symbol, t0_ms, kind, session, meta_json FROM events").fetchall()
        assert events, "급등 이벤트가 하나도 기록되지 않았다"
        assert {e[0] for e in events} == {RUNNER}        # 오탐 없음

        truth_t0 = truths[RUNNER]["t0_expected_ms"]
        symbol, t0_ms, kind, session, meta_json = events[0]
        assert abs(t0_ms - truth_t0) <= 20 * MIN_MS      # 정답 T0 근처
        assert kind in ("win", "day", "both")
        assert session == "regular"

        # --- meta_json 에 A1 §4 추가 라벨이 실려 있는가 (W3 인수인계 §6) ----
        meta = json.loads(meta_json)
        for key in ("hod_ms", "rvol_at_t0", "rvol_gated", "shape", "outcome",
                    "t0_min_from_open", "float_rotation"):
            assert key in meta, key
        assert meta["realtime"] is True
        assert meta["score_path"] in ("precursor", "confirm")
        assert meta["detect_lag_min"] >= 0

        # --- 같은 이벤트를 매 사이클 다시 쓰지 않는가 (라이브 40% 중복 회귀) ---
        # 검출기는 매 사이클 버퍼 전체를 다시 스캔한다. 억제가 없으면 events 행이
        # 고유 (symbol, t0_ms) 보다 훨씬 많아진다.
        rows = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        uniq = conn.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT symbol, t0_ms FROM events)"
        ).fetchone()[0]
        # 장중에는 peak_ms/ret_close 가 새 봉마다 바뀌므로 재검출이 **정당한 갱신**이다.
        # 따라서 불변식은 "억제가 몇 번 걸렸나" 가 아니라 **모든 행이 설명되는가** 다:
        # 행 = 최초 검출 + 라벨이 실제로 바뀐 갱신. 순수 중복이 있으면 이 등식이 깨진다.
        new_rows = ctx.counters.get("events", 0)
        updates = ctx.counters.get("event_updates", 0)
        assert rows == new_rows + updates
        assert new_rows == uniq                           # 고유 이벤트당 최초 검출 1회
        assert ctx.detector.counters["events"] == uniq

        # --- alert 는 사람이 봐야 하는 것만인가 -------------------------------
        # 갱신은 절대 alert 가 아니다 — alert 는 최초 검출 중 신선한 것뿐이다.
        assert ctx.counters.get("event_alerts", 0) <= new_rows
        assert ctx.notifier.counters["alert"] <= uniq

        # --- 루프가 조용히 죽지 않았는가 ------------------------------------
        assert ctx.counters.get("loop_errors", 0) == 0
        assert ctx.counters.get("schema_mismatch", 0) == 0
        assert ctx.counters["ranking_snaps"] > 0
        assert ctx.counters.get("history_from_db", 0) >= 1   # API 백필 대신 DB 사용

        # --- 예산 가드가 커버리지를 조용히 갉아먹지 않았는가 (main 8056da9) ---
        # CHART 여유 22% 라면 승격 직후 백필이 겹쳐도 tier2 정원이 유지돼야 한다.
        assert ctx.counters.get("budget_shrinks", 0) == 0
        assert ctx.tiers.capacity[2] == ctx.cfg.universe.tier2_max
        assert ctx.tiers.capacity[3] == ctx.cfg.universe.tier3_max
        assert ctx.budget.rate_limited == {}
        assert ctx.budget.measured_rate("MARKET_DATA_CHART") <= \
            ctx.budget.target("MARKET_DATA_CHART")
    finally:
        ctx.store.close()


async def test_replay_state_survives_a_mid_session_restart(tmp_path):
    """세션 도중 죽어도 티어·워치리스트·마지막 수집 지점을 이어받는다."""
    ctx, truths, md, end_ms, _seeded = _build(tmp_path)
    half_ms = md.regular.start_ms + 40 * MIN_MS
    try:
        await _run_until(ctx, half_ms, real_timeout=90.0)
        ctx.save_state(force=True)
        tier_before = {s: st.tier for s, st in ctx.tiers.states.items() if st.tier > 1}
        bars_before = ctx.store._conn.execute(
            "SELECT COUNT(*) FROM candles_1m").fetchone()[0]
        counters_before = dict(ctx.counters)
    finally:
        ctx.store.close()

    assert tier_before, "재시작 전에 승격이 하나도 없었다면 이어받기를 검증할 수 없다"

    cfg = make_config(tmp_path)
    store = Store(cfg.store.db_path)
    clock = VirtualClock(half_ms, scale=SPEED)
    df_run, truth_run = synth.make_scenario("coil_pop", seed=1, symbol=RUNNER)
    df_quiet, truth_quiet = synth.make_scenario("noise", seed=1, symbol=QUIET)
    client = ReplayClient(clock, {RUNNER: df_run, QUIET: df_quiet},
                          {RUNNER: truth_run, QUIET: truth_quiet},
                          truth_run["calendar"], rankings=truth_run["rankings"])
    ctx2 = CollectorContext.create(client, store, cfg, notifier=Notifier(console=False),
                                   clock=clock)
    try:
        assert ctx2.counters["resumes"] == 1
        assert {s: st.tier for s, st in ctx2.tiers.states.items()
                if st.tier > 1} == tier_before
        assert set(ctx2.watchlist) >= {RUNNER, QUIET}
        assert ctx2.counters.get("candles_1m", 0) == counters_before.get("candles_1m", 0)
        assert ctx2.resume_point_ms(RUNNER) is not None

        await _run_until(ctx2, half_ms + 40 * MIN_MS, real_timeout=90.0)
        bars_after = ctx2.store._conn.execute(
            "SELECT COUNT(*) FROM candles_1m").fetchone()[0]
        assert bars_after > bars_before                  # 이어서 더 쌓였다
        assert ctx2.counters.get("loop_errors", 0) == 0
    finally:
        ctx2.store.close()

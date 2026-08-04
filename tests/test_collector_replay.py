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
    # synth 는 금액 목록 2종만 만든다 (W3 소유라 건드리지 않는다). 수집기는 2026-08-04
    # 부터 **건수 목록** 2종만 부르므로, 여기서 이름을 맞춰주지 않으면 리플레이가
    # 랭킹을 한 행도 못 받아 "랭킹 없이도 통과" 하는 무의미한 테스트가 된다.
    rankings = rankings.assign(ranking_type=rankings["ranking_type"].replace({
        "MARKET_TRADING_AMOUNT": "MARKET_TRADING_VOLUME",
        "TOSS_SECURITIES_TRADING_AMOUNT": "TOSS_SECURITIES_TRADING_VOLUME"}))
    assert set(rankings["ranking_type"]) == set(loops.RANKING_TYPES)

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
        # 중복 방지는 **두 층**이다:
        #   1) 검출기 해시 억제(W4) — 라벨이 그대로면 record_event 를 아예 부르지 않는다
        #   2) DB UNIQUE(symbol,t0_ms) + UPSERT(W2) — 라벨이 바뀌어 다시 부르면 같은 행을
        #      덮어쓴다. 갱신은 **행을 늘리지 않는다.**
        # 장중에는 peak_ms/ret_close 가 새 봉마다 바뀌므로 재검출이 정당한 갱신이고,
        # 그 갱신이 행 수에 새는지를 보는 것이 이 블록의 요지다.
        rows = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        uniq = conn.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT symbol, t0_ms FROM events)"
        ).fetchone()[0]
        new_rows = ctx.counters.get("events", 0)
        updates = ctx.counters.get("event_updates", 0)

        assert rows == uniq                               # 키당 정확히 한 행
        assert rows == new_rows                           # 행은 최초 검출로만 생긴다
        # 감사 I-1 불변식: (symbol, 매매일)당 이벤트 행 수 ≤ max_per_day(=1).
        # t0 가 이동하면 (symbol, t0_ms) 유니크로는 절대 잡히지 않는 중복이 이걸로 잡힌다.
        per_day = conn.execute(
            "SELECT symbol, t0_ms / 86400000, COUNT(*) FROM events GROUP BY 1, 2"
        ).fetchall()
        assert all(n <= 1 for _s, _d, n in per_day), per_day
        assert ctx.detector.counters["events"] == uniq
        # 갱신이 실제로 일어났는데도 행이 늘지 않았다는 것이 UPSERT 가 작동한 증거다.
        # (갱신이 0 이면 이 테스트는 아무것도 증명하지 못하므로 함께 단언한다.)
        assert updates > 0, "라벨 갱신이 한 번도 없었다면 UPSERT 경로가 검증되지 않는다"
        assert rows < new_rows + updates                  # 갱신은 행을 늘리지 않았다

        # ⚠️ 주의: UPSERT 가 순수 중복까지 흡수하므로 **행 수로는 검출기 억제 회귀를 잡을 수
        # 없다.** 억제가 통째로 깨져도 여기 등식은 그대로 성립한다(행만 덮어쓸 뿐).
        # 억제 자체의 회귀는 단위 테스트가 지킨다 — 지우지 말 것:
        #   test_collector_detector.py::test_identical_relabel_is_suppressed
        #   test_collector_detector.py::test_changed_labels_are_re_emitted_as_updates
        #   test_collector_loops.py::test_demotion_keeps_event_history_but_drops_heavy_state

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

        # 감사 H-9: "카운트가 늘었다" 는 구멍을 볼 수 없다 — 봉이 **빠짐없이** 이어졌는지를
        # 합성 정답(그 분에 봉이 존재해야 하는가)과 대조한다. 이 단언이 있었으면
        # 실측 40분/100분 구멍이 테스트에서 잡혔을 것이다.
        db_ts = {int(r[0]) for r in ctx2.store._conn.execute(
            "SELECT ts_ms FROM candles_1m WHERE symbol = ?", (RUNNER,)).fetchall()}
        lo, hi = min(db_ts), max(db_ts)
        expected = {int(t) for t in df_run["ts_ms"].tolist() if lo <= int(t) <= hi}
        missing = sorted(expected - db_ts)
        assert missing == [], f"candles_1m 에 {len(missing)}분 구멍: {missing[:5]}..."

        # 감사 I-1 불변식은 재시작을 가로질러도 성립해야 한다 (억제 상태는 DB 에서 복원).
        per_day = ctx2.store._conn.execute(
            "SELECT symbol, t0_ms / 86400000, COUNT(*) FROM events GROUP BY 1, 2"
        ).fetchall()
        assert all(n <= 1 for _s, _d, n in per_day), per_day
    finally:
        ctx2.store.close()

"""이중 계상(D1·D2)과 그 여파로 닫혀 있던 게이트 — docs/46.

`docs/45` 가 남긴 것: **리미터는 한 번도 안 깨졌다.** 580건의 "1초에 11~14회" 는
같은 호출을 두 번 센 결과다. 원인 두 개:

  D1  `sync_rate_limits(group)` 가 **전역** 시도 델타를 클램프 없이 `group` 에 얹는다.
      `_guarded` 의 `finally` 가 루프마다 이것을 부르고 기본 그룹이 MARKET_DATA 다.
  D2  델타를 빼앗긴 진짜 주인이 `max(booked, calls)` 바닥값으로 **또** 계상한다.

이 파일이 고정하는 것은 셋이다:

  A. **계상 = 송신.** 어떤 그룹도 남이 보낸 것을 자기 몫으로 세지 않고, 자기가 보낸 것을
     두 번 세지 않는다. 총계도 실제 송신과 같다.
  B. **게이트.** 정원 복원(`should_grow`)과 tier2 호가 게이트는 **60초 중 최악의 1초**를
     지속 속도 목표에 대고 재고 있었다 — 리미터가 완벽해도 트립하는 비교다.
  C. **대조군.** 진짜 지속 과부하·진짜 429·진짜 1초 창 위반에는 **여전히** 축소와
     게이트가 걸린다. 이것이 없으면 "경보를 껐다" 와 구분되지 않는다.

A 를 재현하는 테스트들은 수정 전 코드에서 **빨갛다**. C 는 수정 전후 모두 초록이어야
한다 — 그것이 대조군의 정의다.
"""
from __future__ import annotations

import pytest

from tossmon.collector import budget as budget_mod
from tossmon.collector import loops
from tossmon.collector.budget import (GROUP_CHART, GROUP_MARKET_DATA,
                                      GROUP_RANKING, BudgetGuard, TierPlan)
from tossmon.collector.loops import CollectorContext
from tossmon.collector.notifier import Notifier
from tossmon.store import Store

from .test_collector_helpers import FrozenClock, calendar_dict, make_config, simple_day

DAY0 = 1753833600000     # 2026-07-30 00:00:00 UTC (helpers 와 같은 기준)
MIN_MS = 60_000
LIMITS = {"MARKET_DATA": 10, "MARKET_DATA_CHART": 5, "RANKING": 5, "STOCK": 5}


# --------------------------------------------------------------------------- #
# 최소 client 더블 — 소켓 없이 "그룹 g 가 n 건 보냈다" 만 표현한다
# --------------------------------------------------------------------------- #
class _SendingClient:
    """`TossClient._send` 가 카운터를 올리는 방식을 그대로 흉내낸다.

    실제 `_send` 는 소켓 직전에 전역 `counters["requests"]` 와 **그룹별** 송신 수를
    함께 올린다. 그룹은 `_request` 가 allowlist 로 정한 값이라 추정이 아니다.
    """

    def __init__(self) -> None:
        self.counters = {"requests": 0, "http_429": 0, "retries": 0}
        self.sent_by_group: dict[str, int] = {}
        self.last_headers: dict[str, str] = {}
        self.last_status: int | None = None
        self.last_429: dict | None = None

    def send(self, group: str, n: int = 1, *, retries: int = 0) -> None:
        self.counters["requests"] += n
        self.counters["retries"] += retries
        self.sent_by_group[group] = self.sent_by_group.get(group, 0) + n

    async def get_us_calendar(self, date=None):
        return calendar_dict([simple_day("2026-07-30", DAY0)], 0)


def _build_ctx(tmp_path):
    cfg = make_config(tmp_path)
    store = Store(cfg.store.db_path)
    day = simple_day("2026-07-30", DAY0)
    clock = FrozenClock(day.regular.start_ms + MIN_MS)
    client = _SendingClient()
    # 사건 타임라인은 단조 시계를 쓴다 (docs/52 §5). `FrozenClock` 은 `sync=False` 라
    # 서버 오프셋이 없고 `advance` 로만 움직이므로, 이 테스트의 결정론적 단조 시계다.
    ctx = CollectorContext.create(client, store, cfg, notifier=Notifier(console=False),
                                  clock=clock, symbols=(),
                                  mono=lambda: clock.local_now_ms() / 1000.0)
    ctx.scheduler.calendar = calendar_dict([day], 0)
    ctx.scheduler.fetched_ms = clock.now_ms()
    ctx.session = "regular"
    return ctx, client


def _booked(budget, group: str) -> int:
    return int(budget.counters.get(group, 0))


# --------------------------------------------------------------------------- #
# A. 계상 = 송신
# --------------------------------------------------------------------------- #
def test_the_interleaved_scenario_books_exactly_what_was_sent(tmp_path):
    """`docs/45` §3.4 의 결정론적 재현 — 이제 4건 송신에 4건 계상이어야 한다.

    순서 (단일 이벤트루프, `loops.py` 그대로):

      1. tier3 가 MARKET_DATA 1건 송신 → 완료 → `after_call(MARKET_DATA)`
      2. 그 코루틴이 DB 를 쓰는 **동안** tier2(CHART) 3건이 소켓으로 나간다 (완료 전)
      3. tier3 의 `_guarded` 가 끝나며 `finally: sync_rate_limits(MARKET_DATA)`
      4. 뒤늦게 CHART 완료 3건

    수정 전: MD 4 / CHART 3 / 합 7 (실제 송신 4). 수정 후: MD 1 / CHART 3 / 합 4.
    """
    ctx, client = _build_ctx(tmp_path)
    budget = ctx.budget

    client.send(GROUP_MARKET_DATA, 1)                # 1
    ctx.after_call(GROUP_MARKET_DATA)

    client.send(GROUP_CHART, 3)                      # 2
    ctx.sync_rate_limits(GROUP_MARKET_DATA)          # 3  ← D1 이 여기서 3건을 훔쳤다

    for _ in range(3):                               # 4  ← D2 가 여기서 또 셌다
        ctx.after_call(GROUP_CHART)

    md, chart = _booked(budget, GROUP_MARKET_DATA), _booked(budget, GROUP_CHART)
    assert client.counters["requests"] == 4, "전제: 실제 송신은 4건이다"
    assert md == 1, f"MARKET_DATA 는 1건 보냈는데 {md}건 계상 (D1)"
    assert chart == 3, f"CHART 는 3건 보냈는데 {chart}건 계상"
    assert md + chart == 4, (
        f"총 계상 {md + chart} != 실제 송신 4 — 같은 호출이 두 번 계상된다")


def test_sync_rate_limits_never_books_another_groups_send(tmp_path):
    """D1 단독 — 자기는 한 건도 안 보냈는데 남의 송신을 자기 몫으로 계상하지 않는다."""
    ctx, client = _build_ctx(tmp_path)
    client.send(GROUP_CHART, 5)
    ctx.sync_rate_limits(GROUP_MARKET_DATA)          # _guarded finally, 기본 그룹 MD
    assert _booked(ctx.budget, GROUP_MARKET_DATA) == 0, (
        "MARKET_DATA 는 한 건도 안 보냈다 — 계상이 0 이 아니면 남의 것을 가져온 것이다")


def test_after_call_does_not_invent_a_send_that_never_happened(tmp_path):
    """D2 단독 — 델타가 0 이면 바닥값으로 1건을 지어내지 않는다.

    바닥값 `max(booked, max(1, calls))` 는 "논리 호출은 최소 1건은 나갔다" 는 뜻이었지만,
    D1 이 그 송신을 먼저 가져간 뒤에는 **같은 송신의 두 번째 계상**이 된다.
    """
    ctx, client = _build_ctx(tmp_path)
    client.send(GROUP_CHART, 1)
    ctx.sync_rate_limits(GROUP_CHART)                # CHART 자기 몫으로 정상 계상 (1건)
    assert _booked(ctx.budget, GROUP_CHART) == 1
    ctx.after_call(GROUP_CHART)                      # 같은 송신의 완료 — 새 송신이 아니다
    assert _booked(ctx.budget, GROUP_CHART) == 1, (
        "이미 계상된 송신을 완료 시점에 또 셌다 (D2 바닥값)")


def test_retries_are_booked_to_the_group_that_retried(tmp_path):
    """재시도는 **호출한 그룹**의 송신이다 — 계상에서 빠지지도, 남에게 가지도 않는다.

    감사 B-3(재시도 0회 계상) 이 되살아나면 실사용이 과소평가되어 한도 사고를 놓친다.
    """
    ctx, client = _build_ctx(tmp_path)
    client.send(GROUP_CHART, 3, retries=2)           # 논리 1건이 429 재시도로 3회 송신
    ctx.after_call(GROUP_CHART, calls=1)
    assert _booked(ctx.budget, GROUP_CHART) == 3, "재시도 2건이 계상에서 사라졌다"
    assert _booked(ctx.budget, GROUP_MARKET_DATA) == 0


def test_a_failed_call_still_books_its_own_sends(tmp_path):
    """예외로 끝난 호출도 예산을 태웠다 — `_guarded` 의 finally 가 그것을 계상한다.

    `after_call` 이 안 도는 경로라, 여기서 안 세면 실사용이 조용히 과소평가된다.
    """
    ctx, client = _build_ctx(tmp_path)
    client.send(GROUP_CHART, 2)                      # 보냈지만 재시도까지 실패
    ctx.sync_rate_limits(GROUP_CHART)                # _guarded finally
    assert _booked(ctx.budget, GROUP_CHART) == 2


def test_a_client_without_per_group_counters_falls_back_to_logical_calls(tmp_path):
    """그룹별 송신을 못 세는 client(테스트 더블·구버전)에서도 계상이 죽지 않는다."""
    ctx, client = _build_ctx(tmp_path)
    del client.sent_by_group                         # 그룹별 계상 미지원
    ctx.after_call(GROUP_MARKET_DATA, calls=3)
    assert _booked(ctx.budget, GROUP_MARKET_DATA) == 3


def test_peak_never_exceeds_what_the_group_actually_sent(tmp_path):
    """실제 리미터가 허용한 송신열 위에서 계상을 돌리면 첨두가 한도를 넘지 않는다.

    `docs/45` §2 의 구조적 증명이 **계측까지** 이어지는지 보는 테스트다: 하드캡이
    1.15초에 10건을 보장하고, 계상이 송신과 1:1 이면 어떤 1초에도 10 을 넘을 수 없다.
    """
    ctx, client = _build_ctx(tmp_path)
    from tossmon.api.limiter import _Bucket

    b = _Bucket(rate=1e9, window_cap=10)             # 버킷은 병목에서 제외 — 하드캡만 본다
    t = 0.0
    base_ms = ctx.clock.now_ms()
    for _ in range(40):
        wait = b.window_wait(t)
        if wait > 0.0:
            t += wait
        b.note_sent(t)
        ctx.clock._now = base_ms + int(t * 1000)
        client.send(GROUP_MARKET_DATA, 1)
        # 그 사이 CHART 도 자기 페이스로 나간다 — 이것이 MD 로 새면 첨두가 부푼다.
        client.send(GROUP_CHART, 1)
        ctx.after_call(GROUP_MARKET_DATA)

    peak = ctx.budget.peak_1s(GROUP_MARKET_DATA)
    assert peak <= 10, f"송신은 한도를 지켰는데 계상 첨두가 {peak} — 남의 송신이 얹혔다"


# --------------------------------------------------------------------------- #
# B. 게이트 — 60초 중 최악의 1초를 지속 속도 목표에 대고 재고 있었다
# --------------------------------------------------------------------------- #
def _guard_at_plan(clock) -> BudgetGuard:
    """계획대로(목표의 61%) 고르게 도는 MARKET_DATA — 과부하가 **아닌** 상태.

    현행 설정: 계획 5.18 req/s, 목표 8.5 req/s. 고르게 보내도 어떤 1초에는 6건이
    들어가므로 `peak_1s` 는 6 이 된다. 그 6 을 지속 속도 목표에 대고 재는 것이
    두 게이트가 하던 일이다.
    """
    # `clock` 은 세션·쿨다운용, `mono` 는 사건 타임라인용 — 이 테스트에서는 같은
    # 드라이버지만 자리가 다르다 (docs/52 §5).
    guard = BudgetGuard(dict(LIMITS), usage_ratio=0.85, clock=clock,
                        mono=lambda: clock.now_ms() / 1000.0)
    guard.set_plan(TierPlan(tier1_symbols=1500, tier2_symbols=300, tier3_symbols=10,
                            tier1_sweep_s=45, tier2_candle_s=110, tier3_trades_s=4,
                            tier3_orderbook_s=4, ranking_snap_s=12, ranking_types=3))
    return guard


class _Clock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = float(t)

    def now_ms(self) -> int:
        return int(self.t * 1000)

    def advance(self, dt: float) -> None:
        self.t += float(dt)


def _drive(guard: BudgetGuard, clock: _Clock, group: str, rate: float,
           seconds: float) -> None:
    """`rate` req/s 로 **고르게** 송신한다 (버스트 없음)."""
    step = 1.0 / rate
    end = clock.t + seconds
    while clock.t < end:
        guard.on_request(group)
        clock.advance(step)


#: 리미터가 MARKET_DATA 를 흘려보내는 실제 페이스 (버킷 rate = 10 × 0.85).
_LIMITER_STEP_S = 1.0 / 8.5


def _drive_tier3_shape(guard: BudgetGuard, clock: _Clock, seconds: float,
                       *, symbols: int = 10, period_s: float = 4.0) -> None:
    """**운영 MARKET_DATA 의 실제 모양**으로 송신한다 — 이것이 게이트 판정의 전제다.

    tier3 루프는 만기가 된 항목을 `for` 로 **연달아** 쏜다 (`loops.py` 의 `ready` 루프):
    10종목 × (체결·호가) = 20건이 4초마다 **한꺼번에** 만기가 되고, 리미터가 8.5 req/s 로
    흘려보내므로 2.35초 동안 몰렸다가 1.65초 쉰다.

    그래서 지속률은 20/4 = **5.0 req/s** (목표 8.5 의 59%) 인데 `peak_1s` 는 **9** 다.
    고르게 보내는 상태가 아니라 이쪽이 운영의 실제 모습이고, 두 게이트는 바로 이
    9 를 지속 속도 목표에 대고 재고 있었다.
    """
    end = clock.t + seconds
    calls = symbols * 2
    while clock.t < end:
        batch_start = clock.t
        for _ in range(calls):
            guard.on_request(GROUP_MARKET_DATA)
            clock.advance(_LIMITER_STEP_S)
        clock.t = batch_start + period_s               # 다음 만기까지 쉰다


def test_recovery_is_not_blocked_while_the_sustained_rate_is_well_inside_target(tmp_path):
    """정원 복원이 계획대로 도는 상태에서 막히면 안 된다.

    실측(927 표본): 복원이 **93.1% 의 시간 동안** 허위로 막혀 있었다. 원인은 두 개가
    겹친 것이다 — (1) 이중 계상이 첨두를 부풀렸고, (2) 그 첨두를 **지속 속도 목표**
    (8.5 req/s)의 70% = 5.95 에 대고 쟀다. (2)가 지배적이다: 계상을 고쳐도 tier3 루프의
    실제 모양(4초마다 20건 몰림)이면 첨두는 9 라 **리미터가 완벽해도 트립한다.**
    """
    clock = _Clock()
    guard = _guard_at_plan(clock)
    guard._last_shrink_s[GROUP_MARKET_DATA] = clock.t          # 깎인 적이 있어야 복원 대상
    clock.advance(budget_mod.RECOVER_AFTER_S + 1.0)
    _drive_tier3_shape(guard, clock, seconds=60.0)

    sustained = guard.measured_rate(GROUP_MARKET_DATA)
    assert sustained < guard.target(GROUP_MARKET_DATA) * budget_mod.RECOVER_USAGE_MAX, (
        f"전제 위반: 지속률 {sustained:.2f} 이 이미 회복 문턱을 넘는다")
    assert guard.peak_1s(GROUP_MARKET_DATA) > \
        guard.target(GROUP_MARKET_DATA) * budget_mod.RECOVER_USAGE_MAX, (
        "전제 위반: 첨두가 문턱을 안 넘으면 이 테스트가 아무것도 시험하지 않는다")

    grow = guard.should_grow()
    assert grow and grow.get(GROUP_MARKET_DATA), (
        "지속률이 목표의 61% 인데 복원이 막혔다 — 최댓값을 평균 예산에 비교하고 있다")


def test_shrink_and_recover_read_the_same_quantity(tmp_path):
    """축소는 지속률로 깎는데 복원은 첨두로 막으면 정원은 내려가기만 한다.

    2026-08-04 수정이 축소를 `measured_rate` 로 옮겼지만 복원은 `peak_1s` 에 남았다.
    같은 손잡이(정원 = 지속 속도)를 두 방향에서 **다른 자로** 재면 래칫이 된다.
    """
    clock = _Clock()
    guard = _guard_at_plan(clock)
    _drive_tier3_shape(guard, clock, seconds=60.0)
    ceiling = guard.shrink_ceiling(GROUP_MARKET_DATA)

    assert guard.measured_rate(GROUP_MARKET_DATA) <= ceiling, (
        "이 부하에서는 축소가 안 걸린다 (지속률 기준)")
    # 그렇다면 복원도 같은 자로 재야 한다 — 첨두가 아니라.
    guard._last_shrink_s[GROUP_MARKET_DATA] = 0.0
    assert guard.should_grow(), (
        "축소는 안 걸리는 부하인데 복원은 막힌다 — 두 판정이 다른 양을 본다")


def test_tier2_orderbook_gate_opens_at_the_planned_sustained_rate(tmp_path):
    """tier2 호가 게이트가 계획 부하에서 닫혀 있으면 안 된다.

    실측: **68.8% 의 시간 동안** 닫혀 있었고 누적 55,525회 양보했다. 문턱은
    `peak_1s >= 8.5 × 0.90 = 7.65` 인데, tier3 루프가 4초마다 20건을 몰아 쏘는 실제
    모양에서 첨두는 **9** 다 — 지속률이 목표의 59% 여도 게이트는 닫혀 있다.
    """
    ctx, _client = _build_ctx(tmp_path)
    ctx.tiers.set_capacity(ts_ms=ctx.clock.now_ms(), tier2_max=300, tier3_max=10)
    ctx.refresh_plan()
    base_ms = ctx.clock.now_ms()
    t = 0.0
    while t < 60.0:                                  # tier3 루프의 실제 모양 (위 헬퍼와 동일)
        batch_start = t
        for _ in range(20):
            ctx.clock._now = base_ms + int(t * 1000)
            ctx.budget.on_request(GROUP_MARKET_DATA)
            t += _LIMITER_STEP_S
        t = batch_start + 4.0
    ctx.clock._now = base_ms + int(t * 1000)

    sustained = ctx.budget.measured_rate(GROUP_MARKET_DATA)
    target = ctx.budget.target(GROUP_MARKET_DATA)
    assert sustained < target * loops.TIER2_ORDERBOOK_HEADROOM, (
        f"전제 위반: 지속률 {sustained:.2f} 이 이미 문턱을 넘는다")
    assert ctx.budget.peak_1s(GROUP_MARKET_DATA) >= target * loops.TIER2_ORDERBOOK_HEADROOM, (
        "전제 위반: 첨두가 문턱을 안 넘으면 이 테스트가 아무것도 시험하지 않는다")

    ok, why = loops.tier2_orderbook_allowed(ctx)
    assert ok, (f"계획 부하(지속 {sustained:.2f}/{target:.2f})에서 게이트가 닫혔다 "
                f"(사유 {why!r}) — 60초 중 최악의 1초를 지속 목표에 대고 잰다")
    ctx.store.close()


# --------------------------------------------------------------------------- #
# C. 대조군 — 진짜 과부하·진짜 429 에는 여전히 걸려야 한다
#    (수정 전후 **둘 다** 초록이어야 한다. 아니면 "경보를 껐다" 와 구분되지 않는다.)
# --------------------------------------------------------------------------- #
def test_control_a_genuine_sustained_overload_still_shrinks():
    """진짜 지속 과부하 — 천장 위를 60초 이상 유지하면 여전히 깎인다."""
    clock = _Clock()
    guard = _guard_at_plan(clock)
    ceiling = guard.shrink_ceiling(GROUP_MARKET_DATA)         # 8.5 × 0.95 = 8.08
    _drive(guard, clock, GROUP_MARKET_DATA, rate=9.5, seconds=60.0)
    assert guard.measured_rate(GROUP_MARKET_DATA) > ceiling
    guard.should_shrink()                                     # 첫 관측 — 지속성 타이머 시작
    _drive(guard, clock, GROUP_MARKET_DATA, rate=9.5,
           seconds=budget_mod.MEASURED_SUSTAIN_S + 1.0)
    out = guard.should_shrink()
    assert out and out.get(GROUP_MARKET_DATA, 0) > 0, (
        "진짜 지속 과부하인데 축소가 안 걸린다")


def test_control_a_genuine_overload_still_blocks_recovery():
    """진짜 지속 과부하에서는 복원이 여전히 막힌다."""
    clock = _Clock()
    guard = _guard_at_plan(clock)
    guard._last_shrink_s[GROUP_MARKET_DATA] = clock.t
    clock.advance(budget_mod.RECOVER_AFTER_S + 1.0)
    _drive(guard, clock, GROUP_MARKET_DATA, rate=8.0, seconds=60.0)   # 목표의 94%
    assert guard.measured_rate(GROUP_MARKET_DATA) > \
        guard.target(GROUP_MARKET_DATA) * budget_mod.RECOVER_USAGE_MAX
    assert guard.should_grow() is None, "빡빡한데 정원을 되돌렸다"


def test_control_a_real_one_second_violation_still_blocks_recovery():
    """**진짜** 1초 창 위반(한도 10 초과)이 관측되면 복원을 보류한다.

    이것이 `peak_1s` 의 **단위가 맞는** 유일한 용법이다: 1초 최댓값을 **1초 한도**에
    대고 잰다. 지속 속도 목표(8.5)에 대고 재는 것과 다르다.
    """
    clock = _Clock()
    guard = _guard_at_plan(clock)
    guard._last_shrink_s[GROUP_MARKET_DATA] = clock.t
    clock.advance(budget_mod.RECOVER_AFTER_S + 1.0)
    # 지속률은 낮은데(0.2 req/s) 한 초에 12건이 몰린 상태 — 다중 프로세스 같은 진짜 위반.
    for _ in range(12):
        guard.on_request(GROUP_MARKET_DATA)
        clock.advance(0.05)
    clock.advance(50.0)
    assert guard.measured_rate(GROUP_MARKET_DATA) < \
        guard.target(GROUP_MARKET_DATA) * budget_mod.RECOVER_USAGE_MAX
    assert guard.peak_1s(GROUP_MARKET_DATA) > guard.limit_of(GROUP_MARKET_DATA)
    assert guard.should_grow() is None, (
        "1초 한도를 실제로 넘긴 것이 관측됐는데 정원을 되돌렸다")


def test_control_a_429_still_blocks_recovery():
    """429 직후에는 여전히 복원이 막힌다 (RECOVER_AFTER_S)."""
    clock = _Clock()
    guard = _guard_at_plan(clock)
    guard._last_shrink_s[GROUP_MARKET_DATA] = clock.t
    guard.on_429(GROUP_MARKET_DATA)
    clock.advance(budget_mod.RECOVER_AFTER_S - 1.0)
    assert guard.should_grow() is None, "429 직후인데 정원을 되돌렸다"


def test_control_a_429_still_closes_the_tier2_gate(tmp_path):
    """429 직후에는 tier2 호가 게이트가 여전히 닫힌다."""
    ctx, _client = _build_ctx(tmp_path)
    ctx.tiers.set_capacity(ts_ms=ctx.clock.now_ms(), tier2_max=300, tier3_max=10)
    ctx.refresh_plan()
    ok, _why = loops.tier2_orderbook_allowed(ctx)
    assert ok, "전제 위반: 429 전에 이미 닫혀 있으면 대조군이 성립하지 않는다"
    ctx.budget.on_429(GROUP_MARKET_DATA)
    ok, why = loops.tier2_orderbook_allowed(ctx)
    assert not ok and why == "429", f"429 인데 게이트가 열렸다 ({ok}, {why!r})"
    ctx.store.close()


def test_control_a_genuine_sustained_overload_still_closes_the_tier2_gate(tmp_path):
    """진짜 지속 과부하에서는 tier2 호가 게이트가 여전히 닫힌다."""
    ctx, _client = _build_ctx(tmp_path)
    ctx.tiers.set_capacity(ts_ms=ctx.clock.now_ms(), tier2_max=300, tier3_max=10)
    ctx.refresh_plan()
    base_ms = ctx.clock.now_ms()
    for i in range(int(8.2 * 60)):                       # 목표 8.5 의 96%
        ctx.clock._now = base_ms + int(i * (1000 / 8.2))
        ctx.budget.on_request(GROUP_MARKET_DATA)
    ctx.clock._now = base_ms + 60_000
    ok, why = loops.tier2_orderbook_allowed(ctx)
    assert not ok and why == "rate", (
        f"지속 {ctx.budget.measured_rate(GROUP_MARKET_DATA):.2f} req/s 인데 게이트가 "
        f"열렸다 ({ok}, {why!r})")
    ctx.store.close()

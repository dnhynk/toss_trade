"""예산의 **사건 타임라인**은 단조 시계, **세션 판정**은 벽시계 — docs/52 §5.

docs/52 가 남긴 마지막 항목이다. 계상의 귀속(docs/46)·총량(docs/46)·시각의 **출처**
(docs/52 §3, 완료→송신)를 다 고쳐도 첨두가 11~13 에 남아 있었다. 주인은 계상이 아니라
**예산이 사건을 찍는 시계**였다:

    (수정 전) budget._now_s() = clock.now_ms()/1000 = 로컬 시각 + 서버 오프셋
    서버 오프셋              = 초 해상도 `Date` 헤더 9표본의 **중앙값**, `after_call` 마다 갱신
                             → ±180ms 로 출렁인다 (scheduler.py:170-184)

송신 간격이 118~157ms 인데 그 사이에 오프셋이 150ms 내려가면, 그 구간의 송신들이 예산
시계에서 **한 점으로 눌린다.** 눌린 개수가 그대로 `peak_1s` 다. 서버는 그런 초를 본 적이
없다 — 리미터 하드캡이 1.15초에 10건을 묶고 있기 때문이다.

이 파일이 고정하는 것:

  A. **재현** — 사건 타임라인을 벽시계에 얹으면(수정 전 배선) 지터만으로 첨두가 한도를
     넘는다. 같은 송신열을 단조 시계에 얹으면 진짜 송신 첨두와 같아진다.
  B. **벽시계로 남긴 판정** — 워밍업·축소 쿨다운·429 회복 대기는 **여전히 벽시계**를 본다.
     세션 경계가 서버 시각으로 정의되기 때문이다. 단조 시계를 아무리 밀어도 안 움직인다.
  C. **누출 없음** — 단조 시계의 **원점은 프로세스마다 다르고 재시작마다 리셋된다.**
     그래서 원점이 출력에 보이면 그것이 곧 누출이다. 같은 사건열을 원점만 바꿔 여러 번
     흘려 `snapshot`·`counters`·`limiter_peak`·`ctx.telemetry`·`ctx.state_snapshot` 이
     **완전히 같은지** 본다. 단 하나 `p95_1s`(=고정 버킷 위상 의존)만 느슨하게 본다 —
     이유는 해당 테스트 docstring 에 적었다.
  D. **재시작** — 원점이 통째로 점프해도 값이 말이 되는지 (창은 새로 차고, 음수 나이 없음).
"""
from __future__ import annotations

import json
import math
import random

from tossmon.api.models import reset_precision_stats
from tossmon.collector.budget import (GROUP_MARKET_DATA, MEASURED_WARMUP_S,
                                      RECOVER_AFTER_S, RECOVER_STEP_FRAC,
                                      SHRINK_COOLDOWN_S, BudgetGuard, TierPlan)
from tossmon.collector.loops import CollectorContext
from tossmon.collector.notifier import Notifier
from tossmon.store import Store

from .test_collector_helpers import FrozenClock, calendar_dict, make_config, simple_day
from .test_send_time_accounting import _limiter_send_times

DAY0 = 1753833600000     # 2026-07-30 00:00:00 UTC (helpers 와 같은 기준)
MIN_MS = 60_000
MD_LIMIT = 10
LIMITS = {"MARKET_DATA": 10, "MARKET_DATA_CHART": 5, "RANKING": 5, "STOCK": 5}

#: 출하 설정의 지속 속도 (10 × 0.85). 송신 간격 1/8.5 = 118ms.
SEND_INTERVAL_S = 1.0 / 8.5
#: 실측 RTT (docs/06 §9-4): 최소 54.6ms, p50 71.0ms, 최대 141.0ms, n=47.
RTT_MIN_S, RTT_P50_S, RTT_MAX_S = 0.0546, 0.0710, 0.1410
SEED = 20260812


# --------------------------------------------------------------------------- #
# 지터 모형 — 코드에서 읽은 것이지 지어낸 것이 아니다 (scheduler.py:170-184)
# --------------------------------------------------------------------------- #
class _ServerCorrectedClock:
    """`Clock.observe_headers` 가 만드는 **비단조 오프셋**을 그대로 흉내낸다.

    `Date` 는 **초 해상도**라 표본 오차가 [-500, +500]ms 이고(`_DATE_HEADER_BIAS_MS`
    500 은 중앙값만 잡는다), 오프셋은 최근 9표본의 **중앙값**이다. 표본이 굴러가면서
    중앙값이 앞뒤로 움직인다 — 그것이 여기서 재현하는 전부다.
    """

    def __init__(self, base_s: float, *, max_samples: int = 9) -> None:
        self.base_s = float(base_s)
        self.max_samples = int(max_samples)
        self._samples: list[float] = []
        self.offset_s = 0.0

    def observe(self, t: float) -> None:
        self._samples.append(math.floor(t) + 0.5 - t)      # 초 절삭 + 500ms 보정
        if len(self._samples) > self.max_samples:
            self._samples.pop(0)
        ordered = sorted(self._samples)
        self.offset_s = ordered[len(ordered) // 2]

    def now_s(self, t: float) -> float:
        return self.base_s + t + self.offset_s


def _plan() -> TierPlan:
    return TierPlan(tier1_symbols=1500, tier2_symbols=300, tier3_symbols=10,
                    tier1_sweep_s=45, tier2_candle_s=110, tier3_trades_s=4,
                    tier3_orderbook_s=4, ranking_snap_s=12, ranking_types=3)


class _Wall:
    """`now_ms()` 만 있는 최소 벽시계 (테스트가 직접 민다)."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = float(t)

    def now_ms(self) -> int:
        return int(self.t * 1000)


def _recover_step(have: int) -> int:
    """복원 1스텝 (`should_grow` 와 같은 산식) — 상수를 복제하지 않는다."""
    return max(1, math.ceil(have * RECOVER_STEP_FRAC))


def _true_peak(times: list[float]) -> int:
    peak = left = 0
    for right in range(len(times)):
        while times[right] - times[left] >= 1.0:
            left += 1
        peak = max(peak, right - left + 1)
    return peak


def _drive(guard: BudgetGuard, sends: list[float], wall: _ServerCorrectedClock,
           now_holder: dict, *, jitter: bool = True) -> int:
    """송신 1건마다 완료 1건을 관측하고 계상한다 (프로덕션의 `after_call` 모양).

    계상은 완료 시각에 일어나지만 **송신 시각으로** 찍힌다(`on_sends`) — docs/52 §3.
    완료 때마다 `Date` 헤더를 1건 관측하므로 오프셋이 초당 ~8회 갱신된다.

    RTT 는 실측 삼각분포다 (docs/06 §9-4). **일정한 RTT 를 쓰면 안 된다** — 그러면
    관측 시각의 초 안 위상이 몇 개 값으로 앨리어싱되어 9표본 중앙값이 굳어버린다.
    운영에서 오프셋이 실제로 흔들리는 이유가 그 위상이 퍼져 있기 때문이다.

    반환: 흘리는 동안 관측된 `peak_1s` 의 최댓값 (마지막 값만 보면 첨두를 놓친다).
    """
    rng = random.Random(SEED)
    peak = 0
    for t in sends:
        rtt = rng.triangular(RTT_MIN_S, RTT_MAX_S, RTT_P50_S)
        done = t + rtt
        if jitter:
            wall.observe(done)
        now_holder["mono"] = done
        now_holder["wall"] = wall.now_s(done)
        guard.on_sends(GROUP_MARKET_DATA, [rtt])
        peak = max(peak, guard.peak_1s(GROUP_MARKET_DATA))
    return peak


def _run(sends: list[float], *, event_clock: str,
         jitter: bool = True) -> tuple[BudgetGuard, int]:
    """같은 송신열을 `event_clock` 위에서 계상하고 (가드, 관측 첨두 최댓값)을 돌려준다.

    `event_clock="wall"` 이 **2026-08-12 수정 전** 배선이고 `"mono"` 가 수정 후다.
    다른 것은 이 한 가지뿐이다.
    """
    now: dict[str, float] = {"mono": 0.0, "wall": 0.0}
    wall = _ServerCorrectedClock(base_s=1.75e9)        # 벽시계는 epoch 규모다

    class _C:
        def now_ms(self) -> int:
            return int(now["wall"] * 1000)

    guard = BudgetGuard(dict(LIMITS), usage_ratio=0.85, clock=_C(),
                        mono=(lambda: now["wall"]) if event_clock == "wall"
                        else (lambda: now["mono"]))
    guard.set_plan(_plan())
    return guard, _drive(guard, sends, wall, now, jitter=jitter)


# --------------------------------------------------------------------------- #
# A. 재현 — 벽시계에 얹으면 지터만으로 첨두가 한도를 넘는다
# --------------------------------------------------------------------------- #
#: **진짜 리미터**가 허용한 송신 시각열 (`_limiter_send_times` = `_Bucket` 실물).
#: 계상이 옳다면 첨두는 이 열의 첨두와 같아야 한다.
SENDS = _limiter_send_times(600)


def test_wall_clock_jitter_alone_inflates_the_peak_past_the_hard_cap():
    """**수정 전 red.** 송신은 리미터를 지났는데 계상 첨두가 공시 한도를 넘는다.

    이 테스트에는 변수가 **하나뿐**이다: 예산이 사건을 찍는 시계. 송신열은 진짜
    리미터가 만들었고(1초 첨두 9), 계상도 옳다(송신 시각을 `on_sends` 로 준다).
    그런데도 첨두가 10 을 넘는다 — 서버가 본 적 없는 초과다.
    """
    assert _true_peak(SENDS) == 9, "전제: 송신 자체는 1초에 9건이다"

    guard, peak = _run(SENDS, event_clock="wall")
    assert peak > MD_LIMIT, (
        f"재현 실패 — 벽시계 배선에서 첨두 {peak}, 한도 {MD_LIMIT} 를 넘어야 한다")
    assert guard.counters["events_out_of_order"] > 0, (
        "오프셋이 뒤로 간 흔적이 없다 — 지터 모형이 실제로 안 돌았다")


def test_the_monotonic_event_timeline_recovers_the_true_send_peak():
    """**수정 후 green.** 같은 송신열·같은 계상 코드, 시계만 단조로 바꾼다."""
    guard, peak = _run(SENDS, event_clock="mono")
    assert peak == _true_peak(SENDS) == 9, f"첨두 {peak} != 진짜 송신 첨두 9"
    assert peak <= MD_LIMIT
    # 총량은 안 움직인다 — 이 수정은 **시각**만 건드린다.
    assert guard.counters[GROUP_MARKET_DATA] == len(SENDS)
    assert "events_out_of_order" not in guard.counters


def test_the_control_case_an_ideal_clock_gives_the_same_answer_both_ways():
    """**대조군.** 지터가 없으면 두 배선의 답이 같아야 한다.

    안 같으면 시계 분리가 지터 말고 **다른 것**도 바꿨다는 뜻이다. 즉 이 테스트가
    빨개지면 첨두가 내려간 이유는 "고쳤다" 가 아니라 "다른 걸 망가뜨렸다" 다.
    """
    wall_g, wall_peak = _run(SENDS, event_clock="wall", jitter=False)
    mono_g, mono_peak = _run(SENDS, event_clock="mono", jitter=False)
    assert wall_peak == mono_peak == _true_peak(SENDS) == 9
    assert wall_g.snapshot() == mono_g.snapshot()
    assert wall_g.counters == mono_g.counters


# --------------------------------------------------------------------------- #
# B. 벽시계로 남긴 판정 — 세션 경계는 서버 시각으로 정의된다
# --------------------------------------------------------------------------- #
def test_the_production_wiring_puts_the_event_timeline_on_a_monotonic_clock(tmp_path):
    """**배선 자체**를 고정한다 — 위 테스트들은 `mono` 를 주입해서 돌기 때문이다.

    프로덕션 기본값(`CollectorContext.create` 가 `mono` 를 안 받았을 때)이 다시
    `ctx.clock`(서버 보정 벽시계)이 되면 여기서 잡힌다. 벽시계를 1시간 밀어도
    사건 시계는 따라가지 않고, 세션 판정용 시계는 그대로 따라가야 한다.
    """
    cfg = make_config(tmp_path)
    day = simple_day("2026-07-30", DAY0)
    clock = FrozenClock(day.regular.start_ms + MIN_MS)
    ctx = CollectorContext.create(_SendingClient(), Store(cfg.store.db_path), cfg,
                                  notifier=Notifier(console=False), clock=clock,
                                  symbols=())                    # mono 안 준다 = 기본값
    event_before, wall_before = ctx.budget._mono_s(), ctx.budget._wall_s()
    clock.advance(3600.0)                                        # 벽시계만 1시간 민다

    assert ctx.budget._mono_s() - event_before < 60.0, (
        "사건 시계가 벽시계를 따라갔다 — 2026-08-12 수정 전 배선이다")
    assert ctx.budget._wall_s() - wall_before == 3600.0, (
        "세션 판정용 시계가 벽시계를 안 따라간다 — 세션 경계는 서버 시각이어야 한다")


def test_warmup_follows_the_wall_clock_not_the_monotonic_one():
    """워밍업은 "개장으로부터 180초" 다. 개장은 **서버 시각**으로 정의된다.

    단조 시계를 아무리 밀어도 워밍업이 안 끝나야 하고, 벽시계를 밀면 끝나야 한다.
    """
    wall, mono = _Wall(0.0), {"t": 0.0}
    g = BudgetGuard(dict(LIMITS), 0.85, clock=wall, mono=lambda: mono["t"])
    g.note_session_change()
    assert g.in_warmup()

    mono["t"] += MEASURED_WARMUP_S * 10               # 단조만 크게 민다
    assert g.in_warmup(), "워밍업이 단조 시계를 보고 있다 — 세션 판정은 벽시계여야 한다"

    wall.t += MEASURED_WARMUP_S + 1                   # 벽시계를 민다
    assert not g.in_warmup()


def test_the_shrink_cooldown_follows_the_wall_clock():
    """축소 쿨다운(30s)은 분 단위 판정이라 벽시계다."""
    wall, mono = _Wall(0.0), {"t": 0.0}
    g = BudgetGuard(dict(LIMITS), 0.85, clock=wall, mono=lambda: mono["t"])
    # 계획 자체가 천장을 넘는 설정 — 축소가 즉시(워밍업과 무관하게) 걸린다.
    g.set_plan(TierPlan(tier1_symbols=1500, tier2_symbols=300, tier3_symbols=30,
                        tier1_sweep_s=45, tier2_candle_s=110, tier3_trades_s=4,
                        tier3_orderbook_s=16, ranking_snap_s=12, ranking_types=3))
    assert g.should_shrink(), "전제: 이 계획은 첫 평가에서 축소를 부른다"

    mono["t"] += SHRINK_COOLDOWN_S * 100              # 단조만 크게 민다
    assert g.should_shrink() is None, "쿨다운이 단조 시계를 보고 있다"

    wall.t += SHRINK_COOLDOWN_S + 1                   # 벽시계로 쿨다운이 지났다
    assert g.should_shrink()


def test_the_429_recovery_wait_follows_the_wall_clock():
    """429 후 회복 대기(300s)도 분 단위 판정이라 벽시계다."""
    wall, mono = _Wall(0.0), {"t": 0.0}
    g = BudgetGuard(dict(LIMITS), 0.85, clock=wall, mono=lambda: mono["t"])
    g.set_plan(_plan())

    g.on_429(GROUP_MARKET_DATA)
    assert g._last_429_s[GROUP_MARKET_DATA] == wall.t, "429 시각이 단조 시계로 찍혔다"
    g._forced.clear()                                 # 축소 지시는 처리됐다고 친다
    g._last_shrink_s[GROUP_MARKET_DATA] = wall.t      # 깎인 적이 있어야 복원 대상

    mono["t"] += RECOVER_AFTER_S * 100                # 단조만 크게 민다
    assert g.should_grow() is None, "회복 대기가 단조 시계를 보고 있다"

    wall.t += RECOVER_AFTER_S + 1                     # 벽시계로 조용한 구간이 지났다
    have = _plan().symbols_of(GROUP_MARKET_DATA)
    assert g.should_grow() == {GROUP_MARKET_DATA: _recover_step(have)}


# --------------------------------------------------------------------------- #
# C. 누출 없음 — 단조 원점은 절대 시각이 아니다
# --------------------------------------------------------------------------- #
def test_monotonic_origin_never_leaks_into_any_guard_output():
    """**이 수정의 제일 위험한 지점.** `time.monotonic()` 의 원점은 임의값이다.

    OS·부팅 시각에 따라 다르고 재시작마다 리셋된다. 그래서 출력이 원점에 의존하면
    그것이 곧 누출이다 — 로그에 남으면 사람이 절대 시각으로 오독하고, DB 에 들어가면
    다른 프로세스의 값과 비교 불가능해진다.

    같은 사건열을 **원점만 바꿔** 여러 번 흘려 출력이 같은지 본다. 이 단언은
    "어느 필드가 위험한가" 를 내가 미리 맞힐 필요가 없다 — 전부 비교한다.

    ⚠️ **`p95_1s` / `per_second_counts` 는 예외이고, 그 이유는 이 수정과 무관하다.**
    그 둘은 정렬된 고정 1초 버킷을 세므로 **버킷 경계의 위상**에 의존하고, 위상은
    시계의 원점이 정하기 때문이다. 그래서 그 둘은 원래부터 위상 의존이고 (자기
    docstring 이 그렇게 말한다), 그래서 **판정에는 슬라이딩 `peak_1s` 를 쓴다.**
    여기서는 값이 아니라 **경계 하나 차이 이내**인지만 본다.
    """
    sends = [i * SEND_INTERVAL_S for i in range(300)]

    def run(origin: float):
        now: dict[str, float] = {"t": 0.0}

        class _C:
            def now_ms(self) -> int:
                return int((1.75e9 + now["t"]) * 1000)

        g = BudgetGuard(dict(LIMITS), 0.85, clock=_C(), mono=lambda: origin + now["t"])
        g.set_plan(_plan())
        for t in sends:
            now["t"] = t + 0.071
            g.on_sends(GROUP_MARKET_DATA, [0.071])
            g.on_limiter_window(GROUP_MARKET_DATA, 7.0)
        row = dict(g.snapshot()[GROUP_MARKET_DATA])
        p95 = row.pop("p95_1s")
        return row, p95, dict(g.counters), g.limiter_peak(GROUP_MARKET_DATA)

    # 0 (테스트에서 흔한 원점), 부팅 후 100일, epoch 규모, 그리고 분수 위상.
    base = run(0.0)
    for origin in (8.64e6, 1.75e9, 12345.678):
        got = run(origin)
        assert got[0] == base[0], f"단조 원점 {origin} 이 snapshot 에 보인다 — 누출이다"
        assert got[2] == base[2], f"단조 원점 {origin} 이 카운터에 보인다 — 누출이다"
        assert got[3] == base[3], f"단조 원점 {origin} 이 limiter_peak 에 보인다"
        assert abs(got[1] - base[1]) <= 1, (
            f"p95 가 원점에 따라 {base[1]} → {got[1]} — 버킷 위상 하나 차이를 넘었다")

    # 그리고 어떤 출력도 epoch 규모(1e9 초 = 2001년)가 아니다 — 시각이 섞여 있으면
    # 위 비교가 통과하더라도 사람이 오독할 수 있는 값이 남아 있다는 뜻이다.
    for key, value in base[0].items():
        assert abs(float(value)) < 1e6, f"{key}={value} — 시각처럼 보인다"


def test_the_monotonic_origin_does_not_leak_through_the_collector_either(tmp_path):
    """가드 밖에서도 마찬가지 — 텔레메트리 한 줄과 상태파일이 로그·디스크로 나간다.

    `snapshot()` 만 보면 `loops` 가 예산에서 뽑아 쓰는 다른 값(예: `md_limiter_peak`)이
    빠진다. 실제로 밖으로 나가는 두 표면을 통째로 비교한다.

    `md_p95_1s` 만 빼고 본다 — 고정 버킷 위상 때문이고, 이유는 위 테스트에 적었다.
    """
    def run(origin: float) -> tuple[str, str]:
        # 모듈 전역 누산기 — 원점과 무관하지만 두 실행에 걸쳐 쌓이므로 원점을 유일한
        # 변수로 두려면 초기화해야 한다.
        reset_precision_stats()
        cfg = make_config(tmp_path / f"o{int(origin)}")
        day = simple_day("2026-07-30", DAY0)
        clock = FrozenClock(day.regular.start_ms + MIN_MS)
        now: dict[str, float] = {"t": 0.0}
        client = _SendingClient()
        ctx = CollectorContext.create(client, Store(cfg.store.db_path), cfg,
                                      notifier=Notifier(console=False), clock=clock,
                                      symbols=(), mono=lambda: origin + now["t"])
        ctx.scheduler.calendar = calendar_dict([day], 0)
        ctx.scheduler.fetched_ms = clock.now_ms()
        ctx.session = "regular"
        for i in range(120):
            now["t"] = i * SEND_INTERVAL_S
            client.send(GROUP_MARKET_DATA)
            ctx.after_call(GROUP_MARKET_DATA)
        tel = {k: v for k, v in ctx.telemetry().items() if k != "md_p95_1s"}
        return (json.dumps(tel, sort_keys=True, default=str),
                json.dumps(ctx.state_snapshot(), sort_keys=True, default=str))

    assert run(0.0) == run(1.75e9), "단조 원점이 텔레메트리/상태파일에 새어나간다"


class _SendingClient:
    """`TossClient._send` 의 계상 seam (송신 시각은 못 주는 쪽 — 폴백 경로도 덮는다)."""

    def __init__(self) -> None:
        self.counters = {"requests": 0, "http_429": 0, "retries": 0}
        self.sent_by_group: dict[str, int] = {}
        self.last_headers: dict[str, str] = {}
        self.last_status: int | None = None
        self.last_429: dict | None = None

    def send(self, group: str, n: int = 1) -> None:
        self.counters["requests"] += n
        self.sent_by_group[group] = self.sent_by_group.get(group, 0) + n

    async def get_us_calendar(self, date=None):
        return calendar_dict([simple_day("2026-07-30", DAY0)], 0)


# --------------------------------------------------------------------------- #
# D. 재시작 — 원점이 통째로 점프해도 값이 말이 된다
# --------------------------------------------------------------------------- #
def test_a_restart_starts_the_event_window_from_scratch_and_that_is_correct():
    """재시작하면 단조 원점이 바뀐다. 그래도 값이 말이 돼야 한다.

    `_events` 는 **프로세스 수명**이다 (상태파일로 복원되지 않는다). 그래서 새 프로세스는
    빈 창에서 시작하고, 창이 덜 찬 동안 지속률은 과소평가된다 — 안전한 방향이다
    (기동 버스트로 티어를 줄이지 않는다, `measured_rate` docstring).

    위험한 오답은 옛 프로세스의 시각과 새 원점을 섞는 것이다. 섞이면 60초 창이
    "지난 40년" 이 되거나 사건이 전부 즉시 만료된다. 새 가드가 그러지 않는지 본다.
    """
    before, before_peak = _run(SENDS, event_clock="mono")
    assert before_peak == 9

    # 재시작: 새 가드 + 원점 점프(뒤로도 가능하다 — 부팅 시각이 기준이므로).
    now = {"mono": 42.0, "wall": 1.75e9}

    class _C:
        def now_ms(self) -> int:
            return int(now["wall"] * 1000)

    after = BudgetGuard(dict(LIMITS), 0.85, clock=_C(), mono=lambda: now["mono"])
    after.set_plan(_plan())
    assert after.peak_1s(GROUP_MARKET_DATA) == 0, "새 프로세스는 빈 창에서 시작한다"
    assert after.measured_rate(GROUP_MARKET_DATA) == 0.0

    wall = _ServerCorrectedClock(base_s=1.75e9)
    peak = _drive(after, [42.0 + t for t in SENDS], wall, now)
    assert peak == 9, "원점이 바뀌어도 첨두는 같은 답이어야 한다"
    # 창은 60초를 넘지 않는다 — 옛 원점이 섞였다면 여기서 전부 살아남는다.
    assert after.measured_rate(GROUP_MARKET_DATA) <= 10.0
    assert len(after._events[GROUP_MARKET_DATA]) <= 9 * 61

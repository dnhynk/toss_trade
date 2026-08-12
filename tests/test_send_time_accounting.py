"""계상 시각을 **완료 시각이 아니라 송신 시각**으로 — docs/45 §7 항목 3, docs/52.

`docs/46`(W4, 08-08)이 D1·D2 를 닫았다: 계상의 **귀속**과 **총량**은 이제 옳다.
남은 것은 **시각**이다. `after_call` / `sync_rate_limits` 는 응답이 돌아온 **뒤**에
불리므로, 예산에 찍히는 시각은 송신 시각이 아니라 완료 시각이다.

리미터가 지키는 것은 송신이고 우리가 세는 것은 완료다. 둘은 같은 양이 아니다:

    송신 (리미터가 보장)      t=0.000 0.115 0.230 ... 0.115×k   — 1.15초에 10건 이하
    완료 (우리가 찍는 시각)   응답 RTT + **이벤트루프 지연**만큼 뒤

완료가 몰리면 — DB 쓰기로 루프가 막혔다가 한꺼번에 풀리는, tier3 폴에서 늘 일어나는
모양 — 넓게 퍼져 나간 송신들이 **좁은 구간에 압축되어** 찍힌다. 그러면 `peak_1s` 는
"1초에 몇 건 보냈나" 가 아니라 "1초에 몇 건 **완료됐나**" 를 재게 되고, 그 값은
리미터의 하드캡 상한(=공시 한도)을 아무렇지 않게 넘는다.

이 파일이 고정하는 것:

  A. **재현** — 실제 리미터가 허용한 송신열 위에서 완료 시각으로 계상하면 첨두가
     구조적 상한(10)을 넘는다. 이 테스트는 수정 전 **빨갛다**.
  B. **수정** — 송신 시각으로 계상하면 첨두가 진짜 송신 첨두와 같아진다. 총량은 그대로다.
  C. **동어반복** — 송신 시각 계상에서 `peak_1s > limit` 는 **구조적으로 불가능**하다.
     그 위에 서 있는 경보/게이트 셋이 발화할 수 없다는 것을 실측으로 박아둔다.
     (docs/52 §6 — 이 사실을 숨기면 "경보가 조용하다" 를 "안전하다" 로 오독한다.)
  D. **대조군** — 리미터를 안 거친 송신(다중 프로세스·리미터 밖 경로)은 여전히 첨두로
     드러난다. 이것이 없으면 "계측을 껐다" 와 구분되지 않는다.
"""
from __future__ import annotations

from tossmon.api.client import SEND_TIME_HORIZON_S
from tossmon.api.limiter import WINDOW_HORIZON_S, _Bucket
from tossmon.collector import loops
from tossmon.collector.budget import GROUP_CHART, GROUP_MARKET_DATA
from tossmon.collector.loops import CollectorContext
from tossmon.collector.notifier import Notifier
from tossmon.store import Store

from .test_api_support import MockServer, make_client, mock_server, run  # noqa: F401
from .test_collector_helpers import FrozenClock, calendar_dict, make_config, simple_day

DAY0 = 1753833600000     # 2026-07-30 00:00:00 UTC (helpers 와 같은 기준)
MIN_MS = 60_000
MD_LIMIT = 10            # config 의 MARKET_DATA 공시 한도


# --------------------------------------------------------------------------- #
# client 더블 — 소켓 직전의 계상 seam 만 흉내낸다
# --------------------------------------------------------------------------- #
class _TimedClient:
    """`TossClient._send` 가 소켓 직전에 하는 것을 그대로 한다.

    실제 `_send` 는 그룹별 **송신 수**와 **송신 시각**을 같은 자리에서 남긴다.
    여기서는 단조 시계를 테스트가 직접 민다 (`mono`).
    """

    def __init__(self) -> None:
        self.counters = {"requests": 0, "http_429": 0, "retries": 0}
        self.sent_by_group: dict[str, int] = {}
        self.sent_at: dict[str, list[float]] = {}
        self.mono = 0.0
        self.last_headers: dict[str, str] = {}
        self.last_status: int | None = None
        self.last_429: dict | None = None
        #: 서버가 그 초에 **우리 말고 또 누가** 깎았다고 말할 건수 (기본 0 = 우리가 유일).
        self.foreign_per_second = 0
        self._drained: dict[str, set[int]] = {}

    # --- 송신 (소켓 직전) ---
    def send(self, group: str, at: float, n: int = 1) -> None:
        self.mono = at
        self.counters["requests"] += n
        self.sent_by_group[group] = self.sent_by_group.get(group, 0) + n
        self.sent_at.setdefault(group, []).extend([at] * n)

    # --- 계상 시점의 관측 (loops 가 읽는 면) ---
    def recent_send_ages(self, group: str, n: int) -> list[float]:
        times = self.sent_at.get(group, [])
        take = times[-n:] if n < len(times) else list(times)
        ages = [max(self.mono - t, 0.0) for t in take]
        return [3600.0] * (n - len(ages)) + ages

    # --- 서버 초 감사 (진짜 client 의 `drain_server_seconds` 자리) ---
    def drain_server_seconds(self) -> list[dict]:
        """서버가 `date` 초로 라벨하는 것을 흉내낸다 — **닫힌 초만** 내놓는다.

        기본값은 `consumed == own`, 즉 **우리가 유일한 발신자**다. 그것이 정상이고,
        정상에서 경보가 안 나는 것이 이 스위트의 대조군이다. 남의 소비를 만들려면
        `foreign_per_second` 를 올린다.
        """
        out: list[dict] = []
        for group, times in self.sent_at.items():
            bins: dict[int, int] = {}
            for t in times:
                bins[int(t)] = bins.get(int(t), 0) + 1
            done = self._drained.setdefault(group, set())
            for sec in sorted(bins):
                if sec in done or sec >= int(self.mono):
                    continue                          # 아직 안 닫힌 초는 정산하지 않는다
                done.add(sec)
                out.append({"group": group, "date": str(sec), "own": bins[sec],
                            "consumed": bins[sec] + self.foreign_per_second,
                            "limit_header": None})
        return out

    async def get_us_calendar(self, date=None):
        return calendar_dict([simple_day("2026-07-30", DAY0)], 0)


class _CountOnlyClient(_TimedClient):
    """송신 **시각**은 못 주고 **건수**만 주는 client (구버전·테스트 더블 폴백 경로)."""

    recent_send_ages = None


def _build_ctx(tmp_path, client):
    cfg = make_config(tmp_path)
    store = Store(cfg.store.db_path)
    day = simple_day("2026-07-30", DAY0)
    clock = FrozenClock(day.regular.start_ms + MIN_MS)
    # 예산의 **사건 타임라인**은 client 가 송신 시각을 찍는 그 단조 시계를 쓴다
    # (프로덕션에서는 둘 다 `time.monotonic`). 여기서는 그 자리에 시뮬 시각을 꽂는다 —
    # 벽시계(`clock`)를 꽂으면 예전 배선이 되고, 그것이 docs/52 §5 의 결함이다.
    ctx = CollectorContext.create(client, store, cfg, notifier=Notifier(console=False),
                                  clock=clock, symbols=(), mono=lambda: client.mono)
    ctx.scheduler.calendar = calendar_dict([day], 0)
    ctx.scheduler.fetched_ms = clock.now_ms()
    ctx.session = "regular"
    return ctx


def _limiter_send_times(n: int, *, cap: int = MD_LIMIT,
                        rate: float | None = None) -> list[float]:
    """**실제 리미터**가 허용하는 송신 시각열 (`limiter.acquire` 의 세 관문 그대로).

    `_Bucket` 실물을 쓰되 `await asyncio.sleep(wait)` 만 시각 전진으로 바꾼다.
    `rate=None` 이면 출하 설정과 같은 `cap × 0.85` — 버킷이 병목인 실제 모양이다.
    아주 큰 `rate` 를 주면 버킷이 빠지고 **하드캡만** 남는다 (포화 시험용).

    ⚠️ `+1e-6`: `asyncio.sleep(w)` 는 절대 일찍 깨지 않으므로 실제 경로에서 `now` 는
    경계보다 **위**다. 경계에 정확히 올려놓으면 `prune`(`x <= now-H`)과
    `window_wait`(`x+H-now`)의 부동소수 비교가 갈라져 캡이 한 건 새는데, 그것은
    이 드라이버가 만든 인공 상태이지 리미터의 운영 동작이 아니다 (docs/52 §8).
    결과 열이 정말로 캡을 지키는지는 아래에서 **검증한다** — 드라이버가 조용히
    불법 열을 만들면 이 파일의 모든 결론이 무의미해지므로.
    """
    b = _Bucket(rate=(cap * 0.85) if rate is None else rate, window_cap=cap)
    b.last = 0.0                       # 기준 시각을 0 으로 (monotonic 기본값 제거)
    b.tokens = 0.0                     # 리미터와 같이 **빈 버킷**으로 시작
    out: list[float] = []
    t = 0.0
    for _ in range(n):
        while True:
            wait = b.window_wait(t)
            if wait > 0.0:
                t += wait + 1e-6
                continue
            b.refill(t)
            if b.tokens >= 1.0:
                b.tokens -= 1.0
                b.note_sent(t)
                out.append(t)
                break
            # 실제 경로는 `await asyncio.sleep(...)` 뒤 `time.monotonic()` 를 다시 읽는다 —
            # 시계 해상도 때문에 반드시 유의미하게 전진한다. 순수 산술로 돌리면
            # `0.1176… × 8.5 = 0.9999…` 같은 잔차에 갇히므로 최소 전진량을 준다.
            t += max((1.0 - b.tokens) / b.effective_rate(), 1e-6)
    assert _true_peak_1s(out) <= cap, (
        f"드라이버가 캡 {cap} 을 넘는 송신열을 만들었다 (첨두 {_true_peak_1s(out)}) — "
        "이 열 위의 판정은 전부 무효다")
    return out


def _true_peak_1s(times: list[float]) -> int:
    """송신 시각열의 진짜 1초 슬라이딩 첨두 (budget.peak_1s 와 같은 정의)."""
    peak = left = 0
    for right in range(len(times)):
        while times[right] - times[left] >= 1.0:
            left += 1
        peak = max(peak, right - left + 1)
    return peak


def _observe(ctx, client, at: float, base_ms: int, group: str = GROUP_MARKET_DATA,
             *, via_finally: bool = False) -> None:
    """완료 시각 `at` 에서 계상을 돌린다 (`after_call` 또는 `_guarded` 의 finally)."""
    client.mono = at
    ctx.clock._now = base_ms + int(at * 1000)
    if via_finally:
        ctx.sync_rate_limits(group)
    else:
        ctx.after_call(group)


# --------------------------------------------------------------------------- #
# A. 재현 — 완료 시각 계상은 구조적 상한을 넘는 첨두를 만든다 (수정 전 red)
# --------------------------------------------------------------------------- #
def test_completion_time_accounting_inflates_peak_past_the_limiter_hard_cap(tmp_path):
    """송신은 한도를 지켰는데 계상 첨두가 한도를 넘는다 — 압축된 것은 시각이다.

    시나리오 (tier3 폴에서 늘 일어나는 모양):
      * 리미터가 20건을 2.35초에 걸쳐 내보낸다 (출하 설정 8.5 req/s, 진짜 첨두 9).
      * 첫 응답 하나가 먼저 돌아오고, 이벤트루프가 DB 쓰기로 막혀 있다가 나머지
        19건의 완료 콜백이 0.1초 뒤 **한꺼번에** 풀린다.
      * 완료 시각으로 계상하면 20건이 0.1초 구간에 압축된다.

    옛 계상: 첨두 20 (한도 10 의 두 배). 새 계상: 진짜 송신 첨두.
    """
    client = _TimedClient()
    ctx = _build_ctx(tmp_path, client)
    base_ms = ctx.clock.now_ms()

    times = _limiter_send_times(20)
    true_peak = _true_peak_1s(times)
    assert true_peak <= MD_LIMIT, "전제: 송신 자체는 한도를 지켰다"
    for t in times:
        client.send(GROUP_MARKET_DATA, t)

    last = times[-1]
    _observe(ctx, client, last + 0.05, base_ms)     # 먼저 돌아온 응답 1건
    _observe(ctx, client, last + 0.15, base_ms)     # 막혀 있던 19건이 한꺼번에

    peak = ctx.budget.peak_1s(GROUP_MARKET_DATA)
    assert peak == true_peak, (
        f"계상 첨두 {peak} != 진짜 송신 첨두 {true_peak} — 완료 시각으로 찍어서 "
        "송신이 압축됐다 (리미터는 1.15초에 10건을 넘길 수 없다, docs/45 §2)")


def test_a_slow_and_fast_completion_mix_reproduces_the_operational_shape(tmp_path):
    """운영 로그의 모양(첨두 11~13, p95 7~9)이 같은 원인에서 나오는가.

    완료가 **부분적으로만** 몰려도 첨두는 부푼다. RTT 를 빠름/느림으로 번갈아 주면
    (실측 54.6~141.0ms, docs/06) 완료가 두 갈래로 갈라지며 압축이 생긴다.
    """
    client = _TimedClient()
    ctx = _build_ctx(tmp_path, client)
    base_ms = ctx.clock.now_ms()

    times = _limiter_send_times(30)
    true_peak = _true_peak_1s(times)
    # 완료 = 송신 + RTT. 느린 응답이 뒤 송신의 완료와 같은 순간에 몰린다.
    # 송신과 완료를 **시간 순서대로** 섞는다 — 완료가 자기보다 늦은 송신을 앞지를 수 없다.
    timeline = sorted([(t, "send") for t in times]
                      + [(t + (0.60 if i % 3 == 0 else 0.06), "done")
                         for i, t in enumerate(times)])
    for at, kind in timeline:
        if kind == "send":
            client.send(GROUP_MARKET_DATA, at)
        else:
            _observe(ctx, client, at, base_ms)

    peak = ctx.budget.peak_1s(GROUP_MARKET_DATA)
    assert peak == true_peak, (
        f"계상 첨두 {peak} != 진짜 송신 첨두 {true_peak} — RTT 가 갈라지는 것만으로도 "
        "완료 시각 계상은 첨두를 부풀린다")


def test_the_failure_also_happens_through_the_guarded_finally_path(tmp_path):
    """`_guarded` 의 `finally: sync_rate_limits(group)` 경로도 같은 결함을 탄다.

    실패로 끝난 호출은 `after_call` 을 못 거치고 이 경로로만 계상된다 — 그리고
    이 경로는 루프 **몸통이 끝날 때** 불리므로 송신과의 시차가 더 크다.
    """
    client = _TimedClient()
    ctx = _build_ctx(tmp_path, client)
    base_ms = ctx.clock.now_ms()

    times = _limiter_send_times(20)
    true_peak = _true_peak_1s(times)
    for t in times:
        client.send(GROUP_MARKET_DATA, t)
    # 루프 몸통이 끝날 때 한 번에 — 20건이 (직전 계상, 지금] 구간에 균등 분포된다.
    _observe(ctx, client, times[-1] + 0.15, base_ms, via_finally=True)

    peak = ctx.budget.peak_1s(GROUP_MARKET_DATA)
    assert peak == true_peak, (
        f"finally 경로 계상 첨두 {peak} != 진짜 송신 첨두 {true_peak} — "
        "루프 몸통이 끝날 때 몰아 찍는다")


# --------------------------------------------------------------------------- #
# B. 수정 — 총량은 그대로, 시각만 진짜가 된다
# --------------------------------------------------------------------------- #
def test_send_time_accounting_preserves_the_total(tmp_path):
    """시각을 고쳐도 **건수**는 실제 송신과 1:1 이어야 한다 (docs/46 의 성과 유지)."""
    client = _TimedClient()
    ctx = _build_ctx(tmp_path, client)
    base_ms = ctx.clock.now_ms()

    times = _limiter_send_times(25)
    for t in times:
        client.send(GROUP_MARKET_DATA, t)
    _observe(ctx, client, times[-1] + 0.1, base_ms)

    booked = int(ctx.budget.counters.get(GROUP_MARKET_DATA, 0))
    assert booked == 25, f"송신 25건에 계상 {booked}건"
    assert client.counters["requests"] == 25


def test_other_groups_sends_are_still_not_booked_here(tmp_path):
    """D1 재발 방지 — 남의 그룹 송신은 시각이 있든 없든 내 몫이 아니다."""
    client = _TimedClient()
    ctx = _build_ctx(tmp_path, client)
    base_ms = ctx.clock.now_ms()

    chart = _limiter_send_times(8, cap=5)
    for t in chart:
        client.send(GROUP_CHART, t)
    # `_guarded` 의 finally — 기본 그룹은 MARKET_DATA 다 (D1 이 여기서 났었다).
    _observe(ctx, client, chart[-1] + 0.1, base_ms, via_finally=True)

    assert int(ctx.budget.counters.get(GROUP_MARKET_DATA, 0)) == 0, (
        "MARKET_DATA 는 한 건도 안 보냈다 — 계상이 0 이 아니면 남의 것을 가져온 것이다")
    assert int(ctx.budget.counters.get(GROUP_CHART, 0)) == 0, (
        "CHART 그룹으로 계상을 돌린 적이 없다")


def test_a_client_without_send_times_still_books_the_right_total(tmp_path):
    """송신 시각을 못 주는 client 는 **옛 경로로 폴백**한다 — 계상이 죽지 않는다."""
    client = _CountOnlyClient()
    ctx = _build_ctx(tmp_path, client)
    base_ms = ctx.clock.now_ms()

    times = _limiter_send_times(6)
    for t in times:
        client.send(GROUP_MARKET_DATA, t)
    _observe(ctx, client, times[-1] + 0.1, base_ms)

    assert int(ctx.budget.counters.get(GROUP_MARKET_DATA, 0)) == 6


def test_sends_older_than_the_horizon_are_still_counted(tmp_path):
    """송신 시각을 잃어버린 건(지평 밖)도 **건수에서는 사라지지 않는다.**

    시각을 못 찾았다고 총량이 줄면 실사용이 과소평가되고, 그것은 한도 사고를 놓치는
    방향의 오류다. 잃어버린 건은 창 밖으로 두되 카운터로 드러낸다.
    """
    client = _TimedClient()
    ctx = _build_ctx(tmp_path, client)
    base_ms = ctx.clock.now_ms()

    client.send(GROUP_MARKET_DATA, 0.0)
    client.sent_at[GROUP_MARKET_DATA].clear()       # 지평 밖으로 밀려나 시각을 잃었다
    _observe(ctx, client, 1.0, base_ms)

    assert int(ctx.budget.counters.get(GROUP_MARKET_DATA, 0)) == 1, "건수가 사라졌다"
    assert int(ctx.counters.get("budget_sends_without_time", 0)) == 1, (
        "시각을 잃은 송신이 카운터로 드러나지 않는다 — 조용한 손실")


# --------------------------------------------------------------------------- #
# C. 동어반복 — 이 수정이 만드는 것을 숨기지 않는다
# --------------------------------------------------------------------------- #
def test_peak_can_never_exceed_the_limit_once_accounting_is_send_time(tmp_path):
    """**동어반복 실측**: 모든 송신이 `limiter.acquire` 를 지나므로 첨두는 한도 이하다.

    그래서 `over_limit_1s` 경보, `should_grow` 의 1초-창 거부, tier2 게이트의 "burst"
    거부 셋 다 **이 프로세스의 송신만으로는 발화할 수 없다.** 그것을 결론으로
    적어두는 것이 이 테스트의 목적이다 — 조용한 경보를 안전으로 읽지 않기 위해.
    """
    client = _TimedClient()
    ctx = _build_ctx(tmp_path, client)
    base_ms = ctx.clock.now_ms()

    # 버킷을 병목에서 빼 **하드캡만** 남긴다 — 캡이 진짜 상한임을 시험하는 유일한 방법이다
    # (기본 usage_ratio 에서는 버킷이 먼저 걸려 하드캡이 발화조차 안 한다, docs/45 §2.2).
    times = _limiter_send_times(200, rate=1e9)
    for t in times:
        client.send(GROUP_MARKET_DATA, t)
        _observe(ctx, client, t + 0.07, base_ms)    # RTT p50 71ms

    peak = ctx.budget.peak_1s(GROUP_MARKET_DATA)
    limit = ctx.budget.limit_of(GROUP_MARKET_DATA)
    assert peak <= limit, f"첨두 {peak} > 한도 {limit}"
    assert not ctx.budget.over_limit_1s(GROUP_MARKET_DATA)
    assert int(ctx.budget.counters.get("over_limit_1s", 0)) == 0
    # 하드캡의 지평이 1.15초라 1.0초 창은 그보다 적게 담는다 — 이 여유가 상한의 이유다.
    assert peak <= int(limit) and WINDOW_HORIZON_S > 1.0


# --------------------------------------------------------------------------- #
# D. 대조군 — 리미터 밖 송신은 여전히 드러난다
# --------------------------------------------------------------------------- #
def test_a_send_that_bypassed_the_limiter_still_shows_up_as_a_burst(tmp_path):
    """리미터를 안 거친 송신(리미터 밖 경로)은 **두 관측 모두**에서 드러나야 한다.

    이것이 초록이어야 docs/52 §6 의 동어반복이 "계측을 껐다" 가 아니라 "이 경로로는
    못 넘는다" 라는 뜻이 된다.

    2026-08-12 (docs/52 §12): 경보 자리가 `peak_1s > limit` 에서 **서버 초 감사**로
    옮겼으므로 여기서도 둘 다 본다 — 계상 첨두(우리 시계)와 서버가 라벨한 초.
    """
    client = _TimedClient()
    ctx = _build_ctx(tmp_path, client)
    base_ms = ctx.clock.now_ms()

    # 한도 10 인데 같은 0.5초 안에 14건이 소켓으로 나갔다 (리미터를 안 지났다).
    for i in range(14):
        client.send(GROUP_MARKET_DATA, i * 0.035)
    _observe(ctx, client, 1.60, base_ms)          # 그 서버 초가 닫힐 때까지 간다

    assert ctx.budget.peak_1s(GROUP_MARKET_DATA) == 14
    assert ctx.budget.over_limit_1s(GROUP_MARKET_DATA)
    assert ctx.budget.server_over_limit_seconds(GROUP_MARKET_DATA) == 1, (
        "리미터 밖 송신 14건을 **서버 초 감사**가 못 봤다 — 경보 술어가 그쪽이므로 "
        "여기가 조용하면 계측이 꺼진 것과 같다")
    assert ctx.budget.server_window_violated(GROUP_MARKET_DATA), (
        "경보·게이트 술어가 리미터 밖 송신에 반응하지 않는다")


def test_the_real_client_records_a_send_time_for_every_send(tmp_path, mock_server):
    """**진짜 `TossClient`** 가 소켓 직전에 송신 시각을 남기는가 (더블이 아니라 실물).

    이것이 없으면 위 테스트들은 전부 `_TimedClient` 라는 더블의 성질만 고정한다 —
    계측을 client 에서 떼어내도 초록인 스위트가 된다.
    """
    c = make_client(mock_server.url, tmp_path)

    async def body():
        try:
            for _ in range(4):
                await c.get_prices(["AAPL"])
            sent = c.sent_by_group.get(GROUP_MARKET_DATA, 0)
            stamps = list(c.sent_at_by_group.get(GROUP_MARKET_DATA, ()))
            assert sent == 4
            assert len(stamps) == sent, (
                f"송신 {sent}건에 시각 {len(stamps)}건 — 소켓 직전 계측이 빠졌다")
            ages = c.recent_send_ages(GROUP_MARKET_DATA, sent)
            assert len(ages) == sent and all(a >= 0.0 for a in ages)
            assert ages == sorted(ages, reverse=True), "나이는 오래된 것부터여야 한다"
        finally:
            await c.aclose()

    run(body())


def test_the_real_client_pads_lost_send_times_outside_the_window(tmp_path, mock_server):
    """시각을 잃은 송신은 **창 밖 나이**로 채워야 한다 — 0 으로 채우면 손실이 첨두가 된다.

    나이 0 은 "방금 보냈다" 는 뜻이다. 시각을 모르는 건을 그렇게 채우면 잃어버린
    송신이 전부 지금 이 순간에 몰린 것으로 잡혀, 고치려던 압축을 계상이 직접 만든다.
    """
    c = make_client(mock_server.url, tmp_path)

    async def body():
        try:
            await c.get_prices(["AAPL"])
            c.sent_at_by_group[GROUP_MARKET_DATA].clear()   # 지평 밖으로 밀려났다
            ages = c.recent_send_ages(GROUP_MARKET_DATA, 3)
            assert ages == [SEND_TIME_HORIZON_S] * 3, (
                f"잃어버린 송신을 {ages} 로 채웠다 — 창 밖 나이여야 한다")
        finally:
            await c.aclose()

    run(body())


def test_client_send_times_agree_with_the_limiters_own_window(tmp_path, mock_server):
    """`docs/45` §7-3 이 지목한 **독립 대조**: client 의 송신 시각 vs `window_used`.

    리미터는 `acquire` 에서, client 는 `_send` 에서 같은 송신을 각각 기록한다. 둘은
    서로를 안 본다. 그래서 "리미터 창 안(1.15초)에 있는 송신 수" 를 양쪽에서 세어
    같은지 보면, 계상이 시각을 잃거나 지어내는 것을 잡을 수 있다.
    """
    c = make_client(mock_server.url, tmp_path)

    async def body():
        try:
            for _ in range(5):
                await c.get_prices(["AAPL"])
            sent = c.sent_by_group.get(GROUP_MARKET_DATA, 0)
            ages = c.recent_send_ages(GROUP_MARKET_DATA, sent)
            within = sum(1 for a in ages if a < WINDOW_HORIZON_S)
            used = c.limiter.snapshot(GROUP_MARKET_DATA)["window_used"]
            assert abs(within - used) <= 1, (
                f"client 는 창 안에 {within}건, 리미터는 {used}건으로 센다 — "
                "두 독립 관측이 갈라졌다면 한쪽 계측이 틀렸다")
        finally:
            await c.aclose()

    run(body())


# --------------------------------------------------------------------------- #
# E. 카운터 수명 — 수명이 다른 값을 나란히 찍지 않는다
# --------------------------------------------------------------------------- #
def _restart(tmp_path, ctx, client):
    """상태파일을 저장하고 **새 프로세스처럼** ctx·client 를 다시 만든다."""
    ctx.save_state(force=True)
    cfg = ctx.cfg
    fresh_client = _TimedClient()
    fresh = CollectorContext.create(fresh_client, Store(cfg.store.db_path), cfg,
                                    notifier=Notifier(console=False), clock=ctx.clock,
                                    symbols=(), resume=True,
                                    state_path=str(ctx.state_path))
    fresh.scheduler.calendar = ctx.scheduler.calendar
    fresh.scheduler.fetched_ms = ctx.clock.now_ms()
    fresh.session = ctx.session
    return fresh, fresh_client


def test_telemetry_declares_which_counters_reset_on_restart(tmp_path):
    """`counter_scope` 가 **사실**인지 재시작을 흉내내서 확인한다.

    한 줄에 수명이 다른 누적 카운터가 섞여 있고, 그것을 표시하지 않았던 것이 실제로
    운영 판단을 오염시켰다: `http_429_under_own_limit(430) > http_429(177)` 은 불가능해
    보이지만 분모가 다를 뿐이다. 이 테스트는 목록이 드리프트하면 죽는다 —
    목록만 있고 검증이 없으면 그 목록도 결국 거짓말이 된다.
    """
    client = _TimedClient()
    ctx = _build_ctx(tmp_path, client)

    client.counters["http_429"] = 7                    # 프로세스 수명 (client)
    ctx.budget.counters["over_limit_1s"] = 5           # 프로세스 수명 (budget)
    ctx.bump("http_429_under_own_limit", 9)            # 설치 수명 (ctx → 상태파일)
    ctx.bump("budget_shrinks", 4)                      # 설치 수명
    before = ctx.telemetry()

    fresh, _ = _restart(tmp_path, ctx, client)
    after = fresh.telemetry()

    declared = set(loops.PROC_SCOPED_COUNTERS)
    watched = {"http_429", "over_limit_1s",
               "http_429_under_own_limit", "budget_shrinks"}
    reset = {k for k in watched if int(before[k]) and not int(after[k])}
    assert reset == watched & declared, (
        f"재시작으로 0 이 된 카운터 {sorted(reset)} 가 선언 {sorted(watched & declared)} "
        "과 다르다 — `counter_scope` 가 거짓말을 하고 있다")
    assert int(after["http_429_under_own_limit"]) == 9, "설치 수명 값이 안 살아남았다"
    assert int(after["budget_shrinks"]) == 4
    assert after["counter_scope"] == loops.PROC_SCOPE_FIELD
    assert int(after["resumes"]) >= 1, "재시작 횟수가 안 보이면 두 수명을 못 나눈다"


def test_every_declared_process_scoped_counter_is_actually_emitted(tmp_path):
    """선언한 이름이 텔레메트리에 실제로 있는가 — 오타 하나로 표시가 무의미해진다."""
    client = _TimedClient()
    ctx = _build_ctx(tmp_path, client)
    keys = set(ctx.telemetry())
    missing = [k for k in loops.PROC_SCOPED_COUNTERS if k not in keys]
    assert not missing, f"선언했지만 텔레메트리에 없는 이름: {missing}"


def test_telemetry_carries_the_limiters_independent_peak(tmp_path):
    """`md_limiter_peak` 가 리미터의 자기 관측을 실어 나르는가 (표본 없으면 -1).

    이것이 `md_peak_1s` 와 **독립**이라야 "첨두가 조용하다" 를 교차 확인할 수 있다.
    """
    client = _TimedClient()
    ctx = _build_ctx(tmp_path, client)
    assert int(ctx.telemetry()["md_limiter_peak"]) == -1, "표본이 없으면 모름(-1)이다"

    ctx.budget.on_limiter_window(GROUP_MARKET_DATA, 6.0)
    ctx.budget.on_limiter_window(GROUP_MARKET_DATA, 9.0)
    ctx.budget.on_limiter_window(GROUP_MARKET_DATA, 4.0)
    assert int(ctx.telemetry()["md_limiter_peak"]) == 9


def test_a_genuine_sustained_rate_is_unchanged_by_the_time_fix(tmp_path):
    """지속률(`measured_rate`)은 **건수/윈도우** 라 시각 수정과 무관하게 같아야 한다.

    축소 판정이 보는 양이 이것이다 — 이 수정이 축소를 느슨하게 만들지 않았음을 고정한다.
    """
    client = _TimedClient()
    ctx = _build_ctx(tmp_path, client)
    base_ms = ctx.clock.now_ms()

    times = _limiter_send_times(60)
    for t in times:
        client.send(GROUP_MARKET_DATA, t)
    _observe(ctx, client, times[-1] + 0.1, base_ms)

    window = ctx.budget.window_s
    assert ctx.budget.measured_rate(GROUP_MARKET_DATA) == 60 / window

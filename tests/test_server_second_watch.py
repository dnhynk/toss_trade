"""서버 초 감시 — 침묵한 감시점 셋을 **서버가 이름 붙인 초** 위로 옮긴다 (docs/52 §12).

`docs/52` §6 이 스스로 적은 것이 이 파일의 출발점이다:

> 송신 시각 계상에서 `over_limit_1s`(첨두 > 한도)는 **이 프로세스의 송신만으로는
> 구조적으로 발화할 수 없다** … 그래서 다음 셋은 **조용해질 수밖에 없다**:
> `budget._note_over_limit`(발화 불가) · `budget.should_grow` 복원 거부(거부 불가) ·
> `loops.tier2_orderbook_allowed` 버스트 스킵(스킵 불가).
> **"조용하다" 를 "안전하다" 로 읽으면 안 된다.**

세 자리 모두 `peak_1s > limit_of` 를 보고 있었다. 그 값은 **우리 송신을 우리 시계로
센 것**이라, 리미터 하드캡이 1.15초 창을 지키는 한 참이 될 수 없다. 그런데 그 감시들이
원래 겨누던 것은 다른 것이었다 — 같은 자격증명으로 도는 **다른 발신자**와 리미터를
**안 지난 송신**. 둘 다 `sent_by_group` 으로는 볼 수 없다.

**옮긴 자리**: 서버가 매 응답에 실어 보내는 두 가지.

    date                      → 그 요청이 속한 창의 **이름** (벽시계 초 정렬 고정 1초)
    x-ratelimit-remaining     → 그 창에서 **서버가 센** 잔량

    limit - remaining  =  그 초에 나간 전체 요청 수   (서버가 셌다)
    우리 `own`         =  그 초에 **우리가** 보낸 수  (소켓에서 셌다)
    차이               =  **우리가 보내지 않은 요청**

우리 시계도 우리 계상도 개입하지 않는다. 근거는 docs/06 §9-1(톱니 20/20: 그 초의 k 번째
호출이 정확히 `remaining = limit - k`)과 §9-4(창 = 벽시계 초 정렬 고정 1초, 그룹별)이다.
그리고 이것은 docs/06 §9-6 이 "판별되지 않았다" 고 남긴 두 후보를 **429 없이** 가른다 —
기존 판별 수단(`under_own_limit`)은 429 를 맞아야만 값이 생겼다.

이 파일의 규율 (자리마다 넷):

    ① 울려야 하는 상황을 **문장으로** 쓴다
    ② 그 상황을 합성해 **옛 술어가 침묵하는 것**을 보인다   (빨강)
    ③ 새 술어가 **운다**                                    (초록)
    ④ 정상 부하에서 **안 우는 것**을 고정한다               (대조군)

④ 가 없으면 이 변경은 8월 초 개장 붕괴의 반복이다 — 없는 첨두를 보고 리미터를 조여
수집량이 줄었던 그 경로다 (docs/33).
"""
from __future__ import annotations

from email.utils import formatdate

import httpx
import pytest

from tests.test_api_support import LIMITS, make_client, mock_server, run  # noqa: F401
from tests.test_collector_helpers import (FrozenClock, calendar_dict, make_config,
                                          simple_day)
from tossmon.api import client as client_mod
from tossmon.collector import budget as budget_mod
from tossmon.collector import loops
from tossmon.collector.budget import (GROUP_CHART, GROUP_MARKET_DATA, GROUP_RANKING,
                                      BudgetGuard, TierPlan)
from tossmon.collector.loops import CollectorContext
from tossmon.collector.notifier import Notifier
from tossmon.store import Store

DAY0 = 1753833600000     # 2026-07-30 00:00:00 UTC (helpers 와 같은 기준)
MIN_MS = 60_000
MD_LIMIT = 10            # 공시 MARKET_DATA 한도 (config·SPEC_LIMITS 와 같다)
PRICES = "/api/v1/prices"
EPOCH = 1786500000       # 가짜 서버의 `date` 기준점 (초). 값 자체는 의미 없다.


# --------------------------------------------------------------------------- #
# 0. 계약을 그대로 구현한 가짜 서버 (docs/06 §9-1·§9-4)
# --------------------------------------------------------------------------- #
class _QuotaServer:
    """벽시계 초에 정렬된 **고정 1초 창**을 그룹마다 하나씩 센다.

    실측 계약 그대로다:
      * 매 응답에 `date` · `x-ratelimit-limit` · `x-ratelimit-remaining` (docs/06 §9-1)
      * 그 초의 k 번째 호출이 `remaining = limit - k` (톱니 20/20)
      * 초는 테스트가 `tick()` 으로 민다 — 실시간에 의존하면 결정론이 사라진다.

    `shadow` 는 **같은 자격증명으로 도는 다른 발신자**다: 매 서버 초의 맨 앞에서 그만큼을
    먼저 깎는다. 우리 client 는 그의 존재를 어디서도 볼 수 없다 (그것이 요점이다).
    """

    def __init__(self, limit: int = MD_LIMIT, *, shadow: int = 0,
                 send_headers: bool = True) -> None:
        self.limit = limit
        self.shadow = shadow
        self.send_headers = send_headers
        self.second = 0
        self.used: dict[int, int] = {}
        #: 그 응답 하나만 **앞 초의 카운터**를 실어 보낸다 (docs/06 §9-5 경계 렌더).
        self.boundary_render_at: set[int] = set()
        self.serves = 0

    def tick(self, n: int = 1) -> None:
        self.second += n

    def _consume(self) -> tuple[int, int]:
        used = self.used.get(self.second)
        if used is None:
            used = self.shadow                    # 남이 먼저 깎았다
        used += 1
        self.used[self.second] = used
        return self.second, used

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.serves += 1
        sec, used = self._consume()
        headers = {"date": formatdate(EPOCH + sec, usegmt=True)}
        if self.send_headers:
            shown = used
            if self.serves in self.boundary_render_at:
                # 경계에서 렌더된 응답: `date` 는 이 초인데 카운터는 **앞 초**의 것이다.
                shown = self.used.get(sec - 1, 0)
            headers["x-ratelimit-limit"] = str(self.limit)
            headers["x-ratelimit-remaining"] = str(max(0, self.limit - shown))
        return httpx.Response(200, headers=headers, json={"result": []})


def _wire(tmp_path, server: _QuotaServer, **kw):
    c = make_client("http://stub", tmp_path, **kw)
    c._http = httpx.AsyncClient(base_url="http://stub",
                                transport=httpx.MockTransport(server.handler))
    return c


async def _fire(client, n: int, *, group: str = GROUP_MARKET_DATA) -> None:
    """리미터를 **안 지나고** 소켓으로 n 건 내보낸다.

    `_send` 를 직접 부르는 이유는 둘이다. (1) 리미터 밖 송신 시나리오는 정의상 이 경로다.
    (2) 나머지 시나리오에서도 우리 송신 패턴을 **테스트가 정확히 정한다** — 리미터의
    실시간 대기에 기대면 초 배치가 실시간에 좌우되어 결정론이 사라진다. 우리 송신이
    한도를 지키는지는 시나리오마다 명시적으로 단언한다 (드라이버가 조용히 불법 열을
    만들면 이 파일의 결론이 전부 무의미하다 — docs/52 §3 과 같은 규율).
    """
    for _ in range(n):
        await client._send("GET", PRICES, group)


def _settle(client) -> list[dict]:
    """열려 있는 서버 초까지 전부 정산시키고 기록을 가져온다.

    테스트가 실시간 3초를 기다리지 않으려고 시각만 앞으로 민다 — 정산 로직은 프로덕션
    그대로다.
    """
    import time
    client._settle_server_seconds(time.monotonic() + client_mod.SERVER_SECOND_SETTLE_S + 1)
    return client.drain_server_seconds()


def _guard(notifier=None, *, mono=None) -> BudgetGuard:
    clock = FrozenClock(0)
    g = BudgetGuard(limits={GROUP_MARKET_DATA: float(MD_LIMIT)}, usage_ratio=0.85,
                    clock=clock, notifier=notifier,
                    mono=mono or (lambda: clock.local_now_ms() / 1000.0))
    g.set_plan(TierPlan(tier1_symbols=100, tier2_symbols=300, tier3_symbols=10,
                        tier1_sweep_s=45.0, tier2_candle_s=110.0, tier3_trades_s=3.0,
                        tier3_orderbook_s=4.0, ranking_snap_s=12.0))
    return g, clock


def _legal_burst(g: BudgetGuard, clock: FrozenClock, peak: int) -> None:
    """리미터가 낼 수 있는 **합법** 송신열을 예산에 계상한다 (첨두 = `peak`).

    한 초 안에 `peak` 건을 고르게 편다 — 하드캡이 실제로 내는 모양이고, 어떤 1초에도
    한도를 넘지 않는다.
    """
    for i in range(peak):
        g.on_request(GROUP_MARKET_DATA)
        clock.advance(0.9 / peak)


# --------------------------------------------------------------------------- #
# A. 관측 자체 — 진짜 `TossClient` 가 서버 초를 옳게 정산하는가
# --------------------------------------------------------------------------- #
def test_control_a_lone_sender_shows_no_foreign_consumption(tmp_path):
    """④ **대조군.** 우리가 유일한 발신자면 소진량 = 우리 송신 수. 남의 몫은 0 이다.

    이것이 초록이라야 아래 경보들이 "켜 놓기만 한 것" 이 아니라는 뜻이 된다.
    """
    srv = _QuotaServer()
    c = _wire(tmp_path, srv)

    async def body():
        try:
            for _ in range(6):                     # 6초 동안 매 초 한도 안에서 (8건)
                await _fire(c, 8)
                srv.tick()
            return _settle(c)
        finally:
            await c.aclose()

    recs = run(body())
    assert len(recs) == 6, f"서버 초 6개가 정산돼야 한다 (실제 {len(recs)})"
    assert all(r["own"] == 8 for r in recs), [r["own"] for r in recs]
    assert all(r["consumed"] == 8 for r in recs), [r["consumed"] for r in recs]
    assert all(r["foreign"] == 0 for r in recs), "혼자 쐈는데 남의 소비가 보인다"
    assert c.counters["server_second_foreign"] == 0
    assert c.counters["server_seconds"] == 6
    assert c.counters["server_second_unknown"] == 0


def test_a_shadow_sender_on_the_same_credential_shows_up_as_foreign(tmp_path):
    """같은 자격증명으로 도는 **다른 발신자**가 매 서버 초에서 보인다 (429 없이).

    docs/06 §9-6 이 "판별되지 않았다" 고 남긴 그 상황이다. 지금까지의 판별 수단
    (`under_own_limit`)은 **429 를 맞아야만** 값이 생겼는데, 여기서는 429 가 한 건도
    없다 — 그림자가 3, 우리가 5 라 서버 한도 10 을 아무도 안 넘기 때문이다.
    """
    srv = _QuotaServer(shadow=3)
    c = _wire(tmp_path, srv)

    async def body():
        try:
            for _ in range(5):
                await _fire(c, 5)                  # 우리 5 + 남 3 = 8 <= 10 → 429 없음
                srv.tick()
            return _settle(c)
        finally:
            await c.aclose()

    recs = run(body())
    assert c.counters["http_429"] == 0, "이 시나리오에는 429 가 없어야 한다 (전제)"
    assert len(recs) == 5
    assert all(r["own"] == 5 for r in recs), [r["own"] for r in recs]
    assert all(r["consumed"] == 8 for r in recs), [r["consumed"] for r in recs]
    assert all(r["foreign"] == 3 for r in recs), (
        "서버가 8 을 셌고 우리는 5 를 보냈는데 남의 3 이 안 보인다")
    assert c.counters["server_second_foreign"] == 5
    assert c.recent_server_seconds, "진단용 기록이 남지 않았다"


def test_a_send_outside_the_limiter_shows_up_as_our_own_overrun(tmp_path):
    """리미터를 **안 지난** 송신은 서버가 라벨한 초에서 우리 몫으로 드러난다.

    서버가 429 를 안 준 경우까지 잡는 것이 요점이다: 429 를 줬다면 기존 계측
    (`under_own_limit`)에도 흔적이 남지만, 안 줬으면 지금까지는 **아무 데도 안 남았다.**
    """
    srv = _QuotaServer()
    c = _wire(tmp_path, srv)

    async def body():
        try:
            await _fire(c, 13)                     # 한도 10 인데 한 초에 13건
            srv.tick()
            return _settle(c)
        finally:
            await c.aclose()

    recs = run(body())
    assert len(recs) == 1
    assert recs[0]["own"] == 13
    assert recs[0]["limit_header"] == MD_LIMIT
    assert recs[0]["foreign"] == 0, "우리가 넘긴 것을 남의 소비로 오독하면 안 된다"


def test_the_429_remaining_header_is_not_used_as_evidence(tmp_path):
    """429 응답의 잔량은 **안 믿는다** (docs/06 §9-3 — 다음 창의 상태로 보인 실측).

    믿으면 429 한 건이 그 초의 소진량을 통째로 바꿔 없는 외부 소비를 만든다.
    """
    srv = _QuotaServer()
    c = _wire(tmp_path, srv)

    def handler(request: httpx.Request) -> httpx.Response:
        # 429 인데 **다음 창**의 상태를 달고 온다 (limit=10, remaining=9 → 소진 1).
        return httpx.Response(429, headers={"date": formatdate(EPOCH, usegmt=True),
                                            "x-ratelimit-limit": "10",
                                            "x-ratelimit-remaining": "9"},
                              json={"error": {"code": "rate-limit-exceeded",
                                              "message": "x"}})

    c._http = httpx.AsyncClient(base_url="http://stub",
                                transport=httpx.MockTransport(handler))

    async def body():
        try:
            for _ in range(3):
                with pytest.raises(Exception):
                    await c._send("GET", PRICES, GROUP_MARKET_DATA)
            return _settle(c)
        finally:
            await c.aclose()

    recs = run(body())
    assert len(recs) == 1
    assert recs[0]["own"] == 3
    assert recs[0]["consumed"] == -1, "429 의 잔량을 근거로 썼다"
    assert recs[0]["foreign"] == 0, "모름이 사고로 둔갑했다"


def test_a_second_without_quota_headers_is_unknown_not_clean(tmp_path):
    """소진량을 모르는 초는 **모름**으로 센다 — 깨끗한 초로 세면 사각지대가 숨는다."""
    srv = _QuotaServer(send_headers=False)
    c = _wire(tmp_path, srv)

    async def body():
        try:
            await _fire(c, 4)
            srv.tick()
            return _settle(c)
        finally:
            await c.aclose()

    recs = run(body())
    assert recs[0]["consumed"] == -1 and recs[0]["foreign"] == 0
    assert c.counters["server_second_unknown"] == 1, (
        "헤더 없는 초가 텔레메트리의 사각지대로 안 잡힌다")


def test_out_of_order_arrivals_do_not_invent_foreign_consumption(tmp_path):
    """도착 **순서**가 서버 처리 순서와 달라도 없는 외부 소비를 만들면 안 된다.

    이것이 이 관측의 제일 조용한 오탐 경로다: 그 초의 3번째 요청의 응답이 먼저 도착하면
    "서버는 3 을 셌는데 우리는 1 을 보냈다" 로 보인다. 그래서 초가 **닫힌 뒤에만**
    판정하고, 소진량은 최댓값으로 잡는다 (도착 순서와 무관한 양).
    """
    srv = _QuotaServer()
    c = _wire(tmp_path, srv)
    order = [3, 1, 2]                              # 서버는 1,2,3 을 셌고 우리는 3,1,2 로 받는다
    seen = iter(order)

    def handler(request: httpx.Request) -> httpx.Response:
        srv.serves += 1
        srv.used[srv.second] = max(srv.used.get(srv.second, 0), next(seen))
        return httpx.Response(200, headers={
            "date": formatdate(EPOCH + srv.second, usegmt=True),
            "x-ratelimit-limit": str(srv.limit),
            "x-ratelimit-remaining": str(srv.limit - order[srv.serves - 1]),
        }, json={"result": []})

    c._http = httpx.AsyncClient(base_url="http://stub",
                                transport=httpx.MockTransport(handler))

    async def body():
        try:
            await _fire(c, 3)
            srv.tick()
            return _settle(c)
        finally:
            await c.aclose()

    recs = run(body())
    assert recs[0]["own"] == 3 and recs[0]["consumed"] == 3
    assert recs[0]["foreign"] == 0, "도착 순서가 없는 외부 소비를 만들었다"


# --------------------------------------------------------------------------- #
# B. 자리 1 — `budget._note_over_limit`
#
# ① **울려야 하는 상황**
#    (a) 리미터를 안 지난 송신 경로가 있어, 서버가 이름 붙인 한 초에 **우리 송신만으로**
#        공시 한도를 넘었다.
#    (b) 같은 자격증명으로 도는 **다른 발신자**가 그 초의 한도를 같이 깎고 있다.
#    둘 다 정원을 깎아서는 못 고치는 종류라 축소가 아니라 **경보**로 간다.
# --------------------------------------------------------------------------- #
class Rec:
    def __init__(self):
        self.alerts, self.warns = [], []

    def alert(self, m):
        self.alerts.append(m)

    def warn(self, m):
        self.warns.append(m)

    def info(self, m):
        pass


def test_red_the_old_predicate_is_silent_while_a_shadow_sender_eats_the_quota():
    """② **침묵 재현.** 남이 한도의 절반을 먹고 있는데 옛 술어는 끝까지 조용하다.

    옛 술어는 `peak_1s > limit_of` 였다. `peak_1s` 는 **우리 송신을 우리 시계로 센 값**
    이고 우리 리미터는 완벽하게 동작하고 있으므로(첨두 5, 한도 10), 이 상황에서 참이
    될 길이 없다. 그런데 상황은 위험하다 — 서버가 센 소진량은 매 초 9 다.
    """
    g, clock = _guard(notifier=Rec())
    _legal_burst(g, clock, peak=5)                 # 우리 송신은 합법 (첨두 5 <= 10)
    for _ in range(5):                             # 남이 매 초 4 를 먹는다
        g.on_server_second(GROUP_MARKET_DATA, own=5, consumed=9)

    assert g.peak_1s(GROUP_MARKET_DATA) == 5
    assert not (g.peak_1s(GROUP_MARKET_DATA) > g.limit_of(GROUP_MARKET_DATA)), (
        "옛 술어가 이 상황에서 참이면 이 테스트는 아무것도 재현하지 않는다")
    assert g.foreign_seconds(GROUP_MARKET_DATA) == 5, "새 관측은 매 초 그것을 본다"


def test_green_a_shadow_sender_raises_the_alarm_once_per_episode():
    """③ **초록.** 새 술어는 운다 — 그리고 에피소드당 한 번만 운다."""
    rec = Rec()
    g, clock = _guard(notifier=rec)
    for _ in range(budget_mod.FOREIGN_SECONDS_MIN):
        g.on_server_second(GROUP_MARKET_DATA, own=5, consumed=9)
    g.should_shrink()
    g.should_shrink()
    g.should_shrink()

    hits = [a for a in rec.alerts if "혼자 쓰고 있지 않다" in a]
    assert len(hits) == 1, f"에피소드당 1회여야 한다 (실제 {len(hits)})"
    assert g.counters["quota_not_ours"] == 1
    assert "최대 4건/초" in hits[0], hits[0]
    assert "다른 발신자" in hits[0], "경보가 조치를 안 가리킨다"


def test_green_a_send_outside_the_limiter_raises_the_other_alarm():
    """③ **초록 (b).** 우리 송신만으로 서버 초를 넘긴 경우는 **다른 문구**로 운다.

    원인도 조치도 다르다 — 이쪽은 우리 코드에 리미터를 안 지나는 경로가 있다는 뜻이고,
    저쪽은 발신자가 둘이라는 뜻이다. 문구가 엉뚱한 곳을 가리키면 엉뚱한 곳을 고친다.
    """
    rec = Rec()
    g, _clock = _guard(notifier=rec)
    g.on_server_second(GROUP_MARKET_DATA, own=13, consumed=13)
    g.should_shrink()

    hits = [a for a in rec.alerts if "리미터를 안 거치는" in a]
    assert len(hits) == 1, rec.alerts
    assert g.counters["over_limit_1s"] == 1
    assert g.counters.get("quota_not_ours", 0) == 0, "남 탓 경보까지 같이 울렸다"


def test_control_a_full_but_legal_second_never_raises_either_alarm():
    """④ **대조군.** 한도를 꽉 채운 합법 부하(우리 10, 남 0)에서는 둘 다 조용하다.

    경계값이 중요하다 — `own == limit` 은 초과가 아니다. 여기서 울리면 정상 포화마다
    ERROR 가 나고, 그런 가짜 반복이 경보 무시 습관을 만들어 진짜 경보를 묻는다.
    """
    rec = Rec()
    g, _clock = _guard(notifier=rec)
    for _ in range(60):
        g.on_server_second(GROUP_MARKET_DATA, own=MD_LIMIT, consumed=MD_LIMIT)
    g.should_shrink()

    assert g.server_over_limit_seconds(GROUP_MARKET_DATA) == 0
    assert g.foreign_seconds(GROUP_MARKET_DATA) == 0
    assert not g.quota_not_ours(GROUP_MARKET_DATA)
    assert rec.alerts == [], rec.alerts
    assert g.server_seconds_seen(GROUP_MARKET_DATA) == 60, (
        "분모가 안 보이면 위의 0 들은 아무 뜻도 없다 (docs/52 §7.2)")


def test_control_an_isolated_artifact_does_not_ring():
    """④ **대조군 (오탐).** 산발적 이상 **하나**로는 판정하지 않는다.

    실측으로 알려진 오탐 경로 둘이 있다 (`FOREIGN_SECONDS_MIN` 주석):
      * 경계 렌더 — `date` 의 초와 카운터가 한 초 어긋난다 (docs/06 §9-5 부수 관측)
      * 응답 없이 끝난 송신 — 서버는 처리했는데 우리 `own` 에는 안 들어간다
    둘 다 산발적이고, 다른 발신자는 지속적이다. 그 차이가 이 문턱의 근거다.
    """
    rec = Rec()
    g, _clock = _guard(notifier=rec)
    for i in range(30):
        # 30초 중 딱 2초에서만 남의 소비가 보인다 (문턱 3 미만).
        g.on_server_second(GROUP_MARKET_DATA, own=6,
                           consumed=8 if i in (7, 19) else 6)
    g.should_shrink()

    assert g.foreign_seconds(GROUP_MARKET_DATA) == 2
    assert not g.quota_not_ours(GROUP_MARKET_DATA), "산발적 아티팩트로 판정했다"
    assert rec.alerts == [], rec.alerts

    # 같은 크기의 이상이 **지속되면** 판정한다 (반대 방향).
    g2, _c2 = _guard(notifier=Rec())
    for _ in range(budget_mod.FOREIGN_SECONDS_MIN):
        g2.on_server_second(GROUP_MARKET_DATA, own=6, consumed=8)
    assert g2.quota_not_ours(GROUP_MARKET_DATA), "지속되는데도 판정하지 않았다"


def test_the_observation_window_lets_a_past_incident_expire():
    """관측 지평(60초)을 지나면 상태값은 사라진다 — 누적 카운터는 남는다.

    상태값이 안 사라지면 사고 한 번이 세션 내내 게이트를 닫는다 (docs/46 §5 의 실패).
    """
    clock = FrozenClock(0)
    mono = {"t": 0.0}
    g = BudgetGuard(limits={GROUP_MARKET_DATA: float(MD_LIMIT)}, usage_ratio=0.85,
                    clock=clock, mono=lambda: mono["t"])
    for _ in range(budget_mod.FOREIGN_SECONDS_MIN):
        g.on_server_second(GROUP_MARKET_DATA, own=5, consumed=9)
    assert g.quota_not_ours(GROUP_MARKET_DATA)

    mono["t"] += g.window_s + 1.0
    assert g.foreign_seconds(GROUP_MARKET_DATA) == 0, "창을 지난 사고가 안 빠졌다"
    assert not g.quota_not_ours(GROUP_MARKET_DATA)
    assert g.counters["server_seconds_foreign"] == budget_mod.FOREIGN_SECONDS_MIN, (
        "누적 카운터까지 사라지면 지나간 사고를 사람이 못 본다")


# --------------------------------------------------------------------------- #
# C. 자리 2 — `budget.should_grow` 복원 거부
#
# ① **거부가 일어나야 하는 상황**
#    "정원이 깎여 있고 회복 조건(429 조용 · 지속률 여유 · 계획 이내)은 다 만족했는데,
#     이 그룹의 초당 한도가 **우리 것만이 아니다.**"
#    복원은 "지금 한도에 여유가 있다" 를 전제로 한다. 다른 발신자가 그 초를 같이 깎고
#    있으면 그 전제가 거짓이고, 되돌리는 순간 429 를 다시 부른다.
# --------------------------------------------------------------------------- #
def _shrunk_and_quiet() -> tuple[BudgetGuard, FrozenClock]:
    """복원 조건을 **전부** 만족시킨 가드 — 남은 변수는 서버 초 감사 하나뿐이다."""
    g, clock = _guard()
    g._last_shrink_s[GROUP_MARKET_DATA] = 0.0          # 깎인 적이 있다
    clock.advance(budget_mod.RECOVER_AFTER_S + 10)     # 429 도 스텝도 조용했다
    return g, clock


def test_red_the_old_refusal_could_not_fire_while_a_shadow_sender_ate_the_quota():
    """② **침묵 재현.** 남이 한도의 40% 를 먹는 동안에도 옛 조건은 복원을 못 막았다."""
    g, _clock = _shrunk_and_quiet()
    for _ in range(10):
        g.on_server_second(GROUP_MARKET_DATA, own=5, consumed=9)

    assert not (g.peak_1s(GROUP_MARKET_DATA) > g.limit_of(GROUP_MARKET_DATA)), (
        "옛 조건(`peak_1s > limit`)이 참이면 이 재현은 성립하지 않는다")
    assert g.measured_rate(GROUP_MARKET_DATA) < g.target(GROUP_MARKET_DATA) * \
        budget_mod.RECOVER_USAGE_MAX, "지속률 조건도 복원을 안 막는다 (전제)"


def test_green_the_restore_is_refused_while_the_quota_is_not_ours():
    """③ **초록.** 같은 상황에서 복원이 거부된다."""
    g, _clock = _shrunk_and_quiet()
    for _ in range(budget_mod.FOREIGN_SECONDS_MIN):
        g.on_server_second(GROUP_MARKET_DATA, own=5, consumed=9)
    assert g.should_grow() is None, "한도를 나눠 쓰는 중인데 정원을 되돌렸다"


def test_green_the_restore_is_refused_after_a_send_outside_the_limiter():
    """③ **초록 (b).** 우리 송신이 서버 초를 넘긴 것이 관측돼도 거부한다."""
    g, _clock = _shrunk_and_quiet()
    g.on_server_second(GROUP_MARKET_DATA, own=13, consumed=13)
    assert g.should_grow() is None, "1초 한도를 넘긴 것이 관측됐는데 되돌렸다"


def test_control_a_clean_quota_still_restores():
    """④ **대조군.** 깨끗한 서버 초에서는 **여전히** 복원한다.

    이것이 초록이어야 위 거부가 "복원을 껐다" 와 구분된다. 복원이 죽으면 정원은
    내려가기만 하는 래칫이 되고, 429 한 건의 대가를 세션 내내 치른다 (docs/33).
    """
    g, _clock = _shrunk_and_quiet()
    for _ in range(60):
        g.on_server_second(GROUP_MARKET_DATA, own=MD_LIMIT, consumed=MD_LIMIT)
    assert g.should_grow(), "한도를 꽉 채운 합법 부하에서 복원이 막혔다"


def test_control_no_server_second_observed_yet_still_restores():
    """④ **대조군.** 관측이 **아직 없을 때**도 복원한다 — 모름으로 막으면 안 된다.

    기동 직후가 이 상태다. 모름을 사고로 읽으면 재시작마다 정원이 못 올라온다.
    """
    g, _clock = _shrunk_and_quiet()
    assert g.server_seconds_seen(GROUP_MARKET_DATA) == 0
    assert g.should_grow(), "관측이 없다는 이유로 복원이 막혔다"


# --------------------------------------------------------------------------- #
# D. 자리 3 — `loops.tier2_orderbook_allowed`
#
# ① **스킵이 일어나야 하는 상황**
#    "서버가 이름 붙인 초에서 이 그룹의 한도가 **우리 것만이 아니다.**"
#    이 루프는 예산의 마지막 여유를 쓰는 쪽이고, 그 여유가 실제로 우리 것일 때만 쓴다.
# --------------------------------------------------------------------------- #
class _SendingClient:
    """소켓 없이 "그룹 g 가 n 건 보냈다" 만 표현하는 최소 더블 (test_double_billing 과 같다)."""

    def __init__(self) -> None:
        self.counters = {"requests": 0, "http_429": 0, "retries": 0}
        self.sent_by_group: dict[str, int] = {}
        self.last_headers: dict[str, str] = {}
        self.last_status: int | None = None
        self.last_429: dict | None = None


def _build_ctx(tmp_path):
    cfg = make_config(tmp_path)
    store = Store(cfg.store.db_path)
    day = simple_day("2026-07-30", DAY0)
    clock = FrozenClock(day.regular.start_ms + MIN_MS)
    ctx = CollectorContext.create(_SendingClient(), store, cfg,
                                  notifier=Notifier(console=False), clock=clock,
                                  symbols=(),
                                  mono=lambda: clock.local_now_ms() / 1000.0)
    ctx.scheduler.calendar = calendar_dict([day], 0)
    ctx.scheduler.fetched_ms = clock.now_ms()
    ctx.session = "regular"
    ctx.tiers.set_capacity(ts_ms=clock.now_ms(), tier2_max=300, tier3_max=10)
    ctx.refresh_plan()
    return ctx


def test_red_the_old_gate_could_not_skip_while_a_shadow_sender_ate_the_quota(tmp_path):
    """② **침묵 재현.** 남이 한도의 40% 를 먹는데 옛 조건으로는 스킵이 안 일어난다."""
    ctx = _build_ctx(tmp_path)
    try:
        for _ in range(10):
            ctx.budget.on_server_second(GROUP_MARKET_DATA, own=5, consumed=9)
        assert not (ctx.budget.peak_1s(GROUP_MARKET_DATA)
                    > ctx.budget.limit_of(GROUP_MARKET_DATA)), (
            "옛 조건이 참이면 이 재현은 성립하지 않는다")
    finally:
        ctx.store.close()


def test_green_the_tier2_gate_yields_while_the_quota_is_not_ours(tmp_path):
    """③ **초록.** 같은 상황에서 게이트가 닫히고, 사유가 카운터로 드러난다."""
    ctx = _build_ctx(tmp_path)
    try:
        ok, _why = loops.tier2_orderbook_allowed(ctx)
        assert ok, "전제 위반: 시작부터 닫혀 있으면 이 테스트가 아무것도 안 시험한다"
        for _ in range(budget_mod.FOREIGN_SECONDS_MIN):
            ctx.budget.on_server_second(GROUP_MARKET_DATA, own=5, consumed=9)
        ok, why = loops.tier2_orderbook_allowed(ctx)
        assert not ok and why == "quota", (ok, why)
    finally:
        ctx.store.close()


def test_control_the_tier2_gate_stays_open_under_a_full_legal_load(tmp_path):
    """④ **대조군.** 한도를 꽉 채운 합법 부하 60초 동안 게이트는 **계속 열려 있다.**

    이 대조군이 이 파일에서 제일 중요하다. 이 자리의 옛 조건(`peak_1s` 를 지속 예산에
    대고 재기)은 정상 부하의 **69.3% 에서 닫혀** 55,314건을 양보했다 (docs/46 §5).
    경보를 되살리면서 그 실패를 반복하면 아무것도 나아지지 않는다.
    """
    ctx = _build_ctx(tmp_path)
    try:
        for _ in range(60):
            ctx.budget.on_server_second(GROUP_MARKET_DATA, own=MD_LIMIT,
                                        consumed=MD_LIMIT)
        ok, why = loops.tier2_orderbook_allowed(ctx)
        assert ok, f"합법 포화에서 게이트가 닫혔다 (사유 {why!r})"
    finally:
        ctx.store.close()


def test_control_a_429_and_a_sustained_overload_still_close_the_tier2_gate(tmp_path):
    """④ **대조군.** 먼저 있던 두 사유(429·지속 과부하)는 **그대로** 작동한다."""
    ctx = _build_ctx(tmp_path)
    try:
        ctx.budget.on_429(GROUP_MARKET_DATA)
        ok, why = loops.tier2_orderbook_allowed(ctx)
        assert not ok and why == "429", (ok, why)
    finally:
        ctx.store.close()

    ctx = _build_ctx(tmp_path)
    try:
        base_ms = ctx.clock.now_ms()
        for i in range(int(8.2 * 60)):                   # 목표 8.5 의 96%
            ctx.clock._now = base_ms + int(i * (1000 / 8.2))
            ctx.budget.on_request(GROUP_MARKET_DATA)
        ctx.clock._now = base_ms + 60_000
        ok, why = loops.tier2_orderbook_allowed(ctx)
        assert not ok and why == "rate", (ok, why)
    finally:
        ctx.store.close()


# --------------------------------------------------------------------------- #
# E. 배선 — client 의 관측이 실제로 예산 가드까지 가는가
#    (여기가 끊기면 위의 모든 초록은 "부품이 잘 만들어졌다" 일 뿐이다)
# --------------------------------------------------------------------------- #
def test_the_production_wiring_carries_server_seconds_from_client_to_budget(tmp_path):
    """**진짜 `TossClient`** 의 서버 초가 `sync_rate_limits` 를 타고 예산에 도착한다.

    더블이 아니라 실물로 본다 — 관측을 client 에서 떼어내도 초록인 스위트가 되면
    안 된다 (docs/52 §3 과 같은 규율).
    """
    srv = _QuotaServer(shadow=3)
    real = _wire(tmp_path, srv)
    ctx = _build_ctx(tmp_path)
    ctx.client = real
    try:
        async def body():
            for _ in range(budget_mod.FOREIGN_SECONDS_MIN + 1):
                await _fire(real, 5)
                srv.tick()
            # 열린 초까지 정산시킨 뒤 프로덕션 배선을 그대로 태운다.
            import time
            real._settle_server_seconds(
                time.monotonic() + client_mod.SERVER_SECOND_SETTLE_S + 1)
            ctx.sync_rate_limits(GROUP_MARKET_DATA)
            await real.aclose()

        run(body())

        assert ctx.budget.server_seconds_seen(GROUP_MARKET_DATA) >= \
            budget_mod.FOREIGN_SECONDS_MIN, "서버 초가 예산까지 안 왔다"
        assert ctx.budget.quota_not_ours(GROUP_MARKET_DATA), (
            "그림자 발신자가 예산 판정까지 도달하지 않았다")
        tel = ctx.telemetry()
        assert tel["md_foreign_s"] >= budget_mod.FOREIGN_SECONDS_MIN
        assert tel["md_foreign_max"] == 3
        assert tel["md_srv_s"] >= budget_mod.FOREIGN_SECONDS_MIN, (
            "분모가 텔레메트리에 안 실리면 0 을 읽을 수 없다")
    finally:
        ctx.store.close()


def test_the_same_server_second_is_never_counted_twice(tmp_path):
    """드레인은 **한 번만** 내준다 — 두 번 세면 남의 소비가 두 배로 보인다.

    계상 이중화로 이미 한 번 데었다 (D1·D2, docs/46). 같은 실패를 새 관측에서 반복하면
    "남이 4건 먹었다" 가 8건이 되어 엉뚱한 결론이 나온다.
    """
    srv = _QuotaServer(shadow=2)
    c = _wire(tmp_path, srv)

    async def body():
        try:
            for _ in range(3):
                await _fire(c, 4)
                srv.tick()
            first = _settle(c)
            second = _settle(c)                    # 곧바로 또 걷는다
            return first, second
        finally:
            await c.aclose()

    first, second = run(body())
    assert len(first) == 3
    assert second == [], f"같은 초를 두 번 내줬다 ({second})"


def test_a_client_without_the_observation_does_not_break_accounting(tmp_path):
    """이 관측을 못 주는 client(구버전·테스트 더블)에서도 계상이 죽지 않는다."""
    ctx = _build_ctx(tmp_path)
    try:
        assert not hasattr(ctx.client, "drain_server_seconds")
        ctx.sync_rate_limits(GROUP_MARKET_DATA)            # 예외가 나면 안 된다
        assert ctx.budget.server_seconds_seen(GROUP_MARKET_DATA) == 0
        ok, _why = loops.tier2_orderbook_allowed(ctx)
        assert ok, "관측이 없다는 이유로 게이트가 닫혔다"
    finally:
        ctx.store.close()


# --------------------------------------------------------------------------- #
# E. `foreign > 0` 이 **무엇의 증거인가** (2026-08-13, docs/55)
#
# 배포 첫날 `md_foreign_s` 가 2~5, `quota_not_ours` 경보가 11건 울렸다. docs/52 §12 §3 은
# 이 값이 "다른 발신자" 와 "그룹 밖 한도" 를 못 가른다고 적었는데, 아래는 **세 번째
# 후보**가 있음을 보인다: 남의 소비가 정확히 0 인데도 값이 오른다. 그러면 이 경보는
# 그 자체로는 어떤 가설의 증거도 아니다 — 가르는 것은 **그룹 사이의 비대칭**이다.
#
# 이 절의 규율은 앞과 같다: ① 문장 ② 합성 ③ 관측 ④ 대조군.
#
# ⚠️ 아래 `_invents_foreign_consumption_` 둘은 **지금 동작을 고정하는 특성 테스트**다.
# 옳은 동작이 아니라 **틀린 동작을 문서화한 것**이라, 이 관측을 docs/55 §7 대로 고치면
# 두 테스트는 뒤집혀야 한다 (`foreign == 0` 으로). 조용히 지우지 말 것 — 그러면 이
# 오탐이 다시 들어올 수 있다.
# --------------------------------------------------------------------------- #
def _scripted_client(tmp_path, script):
    """응답 하나당 `(date 초, 서버 카운터 값)` 을 그대로 실어 보내는 client.

    `_QuotaServer` 로는 못 만드는 열이 있다: **date 는 이 초인데 카운터는 앞 초의 것**
    (경계 렌더), 그리고 **정산이 끝난 초에 도착한 응답**. 둘 다 실측된 조건이다
    (docs/06 §9-5). 그래서 헤더를 테스트가 직접 정한다 — 서버 계약(`remaining =
    limit - k`)은 그대로 지킨다.
    """
    it = iter(script)

    def handler(request: httpx.Request) -> httpx.Response:
        sec, used = next(it)
        return httpx.Response(200, headers={
            "date": formatdate(EPOCH + sec, usegmt=True),
            "x-ratelimit-limit": str(MD_LIMIT),
            "x-ratelimit-remaining": str(MD_LIMIT - used),
        }, json={"result": []})

    c = make_client("http://stub", tmp_path)
    c._http = httpx.AsyncClient(base_url="http://stub",
                                transport=httpx.MockTransport(handler))
    return c


def _run_script(c, script, *, settle_after=None) -> list[dict]:
    """열을 흘리고 정산된 서버 초를 모아 온다. `settle_after` 건 뒤에 한 번 정산한다."""
    import time

    async def body():
        try:
            out: list[dict] = []
            for i in range(len(script)):
                await c._send("GET", PRICES, GROUP_MARKET_DATA)
                if settle_after is not None and i + 1 == settle_after:
                    c._settle_server_seconds(
                        time.monotonic() + client_mod.SERVER_SECOND_SETTLE_S + 1)
                    out += c.drain_server_seconds()
            c._settle_server_seconds(
                time.monotonic() + client_mod.SERVER_SECOND_SETTLE_S + 1)
            return out + c.drain_server_seconds()
        finally:
            await c.aclose()

    return run(body())


def test_a_boundary_render_invents_foreign_consumption_with_no_foreign_sender(tmp_path):
    """① 남의 소비가 **0** 인데 `foreign` 이 오른다 — 경계 렌더 한 건으로.

    초 0 에 5건을 보냈고 서버는 1..5 를 셌다. 그 중 마지막 응답만 `date=1` 로 렌더된다
    (docs/06 §9-5 의 실측 조건). 초 1 에는 1건만 보냈다.

    그러면 초 1 의 원장은 우리 몫 2건(늦은 것 + 새 것)에 소진량 5 를 갖는다 —
    `consumed` 를 **최댓값**으로 잡기 때문에 앞 초의 카운터가 이 초로 넘어온다.
    `foreign = 5 - 2 = 3`. **다른 발신자도, 공유 바구니도 없다.**
    """
    script = [(0, 1), (0, 2), (0, 3), (0, 4), (1, 5), (1, 1)]
    c = _scripted_client(tmp_path, script)
    recs = _run_script(c, script)

    late = [r for r in recs if r["date"] == formatdate(EPOCH + 1, usegmt=True)]
    assert len(late) == 1, [r["date"] for r in recs]
    assert late[0]["own"] == 2 and late[0]["consumed"] == 5
    assert late[0]["foreign"] == 3, (
        "경계 렌더가 남의 소비로 안 보이면 이 테스트의 전제가 깨진 것이다")
    assert c.counters["server_second_foreign"] == 1


def test_a_late_arrival_after_settlement_invents_foreign_consumption(tmp_path):
    """① 정산이 끝난 초에 응답이 늦게 오면 원장이 **재생성**되고 그것도 foreign 이 된다.

    정산은 그 초를 처음 본 뒤 `SERVER_SECOND_SETTLE_S`(3초) 가 지나면 끝난다. 그보다
    늦게 도착한 응답은 같은 `date` 로 **새 원장**을 만든다 — 우리 몫 1건에 소진량은 그
    창의 전체값이므로 `foreign = consumed - 1` 이다. 이벤트루프가 DB 쓰기로 막히는
    구간에서 응답이 3초 넘게 밀리는 것은 이 수집기에서 드문 일이 아니다.
    """
    script = [(0, 1), (0, 2), (0, 3), (0, 4)]
    c = _scripted_client(tmp_path, script)
    recs = _run_script(c, script, settle_after=3)

    assert len(recs) == 2, "같은 초가 두 번 정산되지 않았다 (재생성이 안 일어났다)"
    assert recs[0]["own"] == 3 and recs[0]["foreign"] == 0
    assert recs[1]["own"] == 1 and recs[1]["consumed"] == 4
    assert recs[1]["foreign"] == 3


def test_control_no_skew_no_foreign(tmp_path):
    """④ 대조군 — 어긋남이 없으면 같은 열에서 foreign 은 0 이다."""
    script = [(0, 1), (0, 2), (0, 3), (0, 4), (0, 5)]
    c = _scripted_client(tmp_path, script)
    recs = _run_script(c, script)
    assert len(recs) == 1 and recs[0]["own"] == 5 and recs[0]["foreign"] == 0


def test_the_three_second_threshold_does_not_separate_the_artifact():
    """① `FOREIGN_SECONDS_MIN = 3` 은 이 artifact 를 못 걸러낸다.

    지난 태스크에서 이 문턱의 근거를 "경계 렌더는 **산발적**이고 다른 발신자는
    지속적이다" 로 적었다 (docs/52 §12 §2). 그 진술은 검증되지 않은 선택이었다 —
    경계 렌더가 60초 창에서 세 번 있으면(초당 2건 보내는 그룹에서 응답의 몇 %가 초
    경계를 넘으면 그렇게 된다) 문턱을 넘고 경보가 오른다.
    """
    rec = Rec()
    g, _clock = _guard(rec)
    for _ in range(3):                             # 서로 떨어진 세 번의 경계 렌더
        g.on_server_second(GROUP_MARKET_DATA, own=2, consumed=5, limit_header=MD_LIMIT)
        for _ in range(9):                         # 그 사이는 깨끗하다
            g.on_server_second(GROUP_MARKET_DATA, own=2, consumed=2,
                               limit_header=MD_LIMIT)
    assert g.foreign_seconds(GROUP_MARKET_DATA) == 3
    assert g.quota_not_ours(GROUP_MARKET_DATA) is True, (
        "문턱 3 이 artifact 3건을 통과시키지 않으면 이 테스트의 전제가 깨진 것이다")
    g._note_over_limit(GROUP_MARKET_DATA)
    assert len(rec.alerts) == 1


# --- 가르는 관측: 그룹 사이의 비대칭 (docs/55 §5) --------------------------- #
def _feed(g, rows: list[tuple[str, int, int]]) -> None:
    for group, own, consumed in rows:
        g.on_server_second(group, own=own, consumed=consumed, limit_header=MD_LIMIT)


def test_a_shared_basket_shows_up_as_asymmetry_between_groups():
    """③ **바구니가 그룹들을 걸쳐 있으면** 송신이 적은 그룹에서 foreign 이 가장 크다.

    합성: 한 서버 초에 우리가 MARKET_DATA 5 · CHART 2 · RANKING 1 을 보냈고 서버는
    하나의 바구니에서 8 을 셌다. 세 그룹의 원장은 모두 소진량 8 을 보지만 자기 몫만
    세므로 RANKING 이 7, CHART 가 6, MARKET_DATA 가 3 을 "남의 소비" 로 본다.

    **이것이 가르는 자리다**: 다른 발신자라면 우리 그룹 구성과 무관하므로 이 순서가
    나올 이유가 없고, 창 어긋남이라면 크기가 그 그룹 **자기 송신**에 갇힌다.
    """
    g, _clock = _guard()
    for _ in range(5):
        _feed(g, [(GROUP_MARKET_DATA, 5, 8), (GROUP_CHART, 2, 8), (GROUP_RANKING, 1, 8)])

    md = g.foreign_sends(GROUP_MARKET_DATA)
    ch = g.foreign_sends(GROUP_CHART)
    rk = g.foreign_sends(GROUP_RANKING)
    assert (md, ch, rk) == (3, 6, 7)
    assert rk > ch > md, "공유 바구니의 비대칭이 관측에서 사라졌다"
    # 그리고 그 비대칭이 **로그로 나간다** — 이것이 없어서 첫날 못 갈랐다.
    line = g.describe()
    assert "RANKING=" in line and "frnmax7" in line, line
    assert "MARKET_DATA=" in line and "frnmax3" in line, line


def test_a_window_skew_artifact_is_bounded_by_the_group_own_sends():
    """③ **창 어긋남**이면 foreign 의 크기가 그 그룹 자기 송신의 변동폭에 갇힌다.

    RANKING 은 12초마다 3건을 낸다. 그 중 하나가 다음 초로 렌더돼도 소진량은 3 을 못
    넘으므로 `foreign <= 2` 다. 같은 초에 MARKET_DATA 가 5건을 보내고 있어도 그 5 는
    RANKING 의 원장에 **들어오지 않는다** — 바구니가 그룹별이기 때문이다.
    그래서 공유 바구니(위 테스트)와 크기가 갈린다.
    """
    g, _clock = _guard()
    for _ in range(5):
        _feed(g, [(GROUP_RANKING, 1, 3), (GROUP_MARKET_DATA, 3, 5)])

    assert g.foreign_sends(GROUP_RANKING) == 2, "어긋남이 자기 버스트를 넘었다"
    assert g.foreign_sends(GROUP_MARKET_DATA) == 2


def test_an_outside_sender_shows_no_asymmetry_between_groups():
    """③ **다른 발신자**면 그룹별 크기가 우리 송신 구성과 무관하다 (비대칭 없음).

    합성: 남이 매 초 2건씩 먹는다. 그가 어느 바구니를 먹는지는 우리 송신량과 상관없으므로
    세 그룹 모두 `foreign = 2` 로 같다. 위 두 테스트와 **모양이 다르다** — 그것이 세
    후보를 가르는 근거다.
    """
    g, _clock = _guard()
    for _ in range(5):
        _feed(g, [(GROUP_MARKET_DATA, 5, 7), (GROUP_CHART, 2, 4), (GROUP_RANKING, 1, 3)])

    sends = {gr: g.foreign_sends(gr)
             for gr in (GROUP_MARKET_DATA, GROUP_CHART, GROUP_RANKING)}
    assert set(sends.values()) == {2}, sends


def test_the_alarm_carries_the_per_group_numbers_that_split_the_hypotheses():
    """③ 경보 문구 하나로 세 후보를 좁힐 수 있어야 한다.

    첫날 경보에는 MARKET_DATA 의 수만 있었고, 그래서 그것을 받고도 어느 후보인지 알 수
    없었다. 이제 그룹별 `frn/관측·최대` 가 같은 문구에 실린다.
    """
    rec = Rec()
    g, _clock = _guard(rec)
    for _ in range(5):
        _feed(g, [(GROUP_MARKET_DATA, 5, 8), (GROUP_RANKING, 1, 8)])
    g._note_over_limit(GROUP_MARKET_DATA)

    assert len(rec.alerts) == 1
    msg = rec.alerts[0]
    assert "RANKING 5/5초·최대7" in msg, msg
    assert "MARKET_DATA 5/5초·최대3" in msg, msg
    assert "창 어긋남" in msg, "세 번째 후보가 경보에 없다"


def test_global_counters_are_not_rendered_as_groups():
    """④ 전역 카운터가 **유령 그룹**으로 렌더되면 이 진단을 읽을 수 없다.

    운영 로그에 실제로 이렇게 찍혀 있었다:
        `| budget ... quota_not_ours=peak0/p95:0/avg0.00/tgt0.85 server_seconds=peak0/...`
    한도 1.0 짜리 미지 그룹으로 보이므로 **"그 그룹은 깨끗하다" 로 읽힌다.**
    """
    g, _clock = _guard()
    _feed(g, [(GROUP_MARKET_DATA, 5, 8)])
    g.counters["quota_not_ours"] = 1
    g.counters["server_seconds_foreign"] = 1

    snap = g.snapshot()
    for name in ("quota_not_ours", "server_seconds", "server_seconds_foreign",
                 "server_seconds_over_limit", "over_limit_1s", "events_out_of_order"):
        assert name not in snap, f"{name} 이 그룹으로 렌더된다"
    assert GROUP_MARKET_DATA in snap


# --------------------------------------------------------------------------- #
# G. 사각지대 — `srv_s_unknown` 이 정확히 무엇을 세고, 그 크기를 어떻게 읽나
#    (docs/52 §13 · docs/58 §G-0 의 0-4)
#
# 운영 실측 (2026-08-13 10:11:03~14:36:50 KST, 텔레메트리 53표본, 프로세스 4개):
# `srv_s_unknown` 의 증가 **47 건 전부**가 429 였고 (429 없이 오른 구간 0/49),
# 429 가 그 서버 초에서 **우리 유일한 요청**이었을 때만 올랐다 (48/51).
# 형제 응답이 같은 초에 있던 3 건에서는 안 올랐다 (3/51) — 그 200 이 헤더를 줬기 때문이다.
#
# 아래 G1~G3 이 그 기전의 결정론적 재현이고, G4~G5 가 "크기를 읽을 수 있게" 하는 빨강이다.
# --------------------------------------------------------------------------- #
def _mixed_client(tmp_path, script, *, shadow: int = 0):
    """응답 하나당 `(date 초, status)` 를 그대로 내는 client.

    `_QuotaServer` 는 200 만 내고 `_scripted_client` 는 429 를 못 낸다. 사각지대는
    **200 과 429 가 같은 초에 섞일 때** 갈리므로 그 열을 직접 정한다.
    200 은 서버 계약대로 `remaining = limit - k` 를 싣고 (docs/06 §9-1), 429 는
    **다음 창의 잔량**을 싣는다 (docs/06 §9-3 의 실측 조건 — 우리는 그것을 안 믿는다).
    `shadow` 는 매 초 맨 앞에서 먼저 깎는 다른 발신자다.
    """
    it = iter(script)
    used: dict[int, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sec, status = next(it)
        used[sec] = used.get(sec, shadow) + 1
        date = formatdate(EPOCH + sec, usegmt=True)
        if status == 429:
            return httpx.Response(429, headers={
                "date": date,
                "x-ratelimit-limit": str(MD_LIMIT),
                "x-ratelimit-remaining": str(MD_LIMIT - 1),
            }, json={"error": {"code": "rate-limit-exceeded", "message": "x"}})
        return httpx.Response(200, headers={
            "date": date,
            "x-ratelimit-limit": str(MD_LIMIT),
            "x-ratelimit-remaining": str(MD_LIMIT - used[sec]),
        }, json={"result": []})

    c = make_client("http://stub", tmp_path)
    c._http = httpx.AsyncClient(base_url="http://stub",
                                transport=httpx.MockTransport(handler))
    return c


def _run_mixed(c, script) -> list[dict]:
    async def body():
        try:
            for _sec, status in script:
                if status == 429:
                    with pytest.raises(Exception):
                        await c._send("GET", PRICES, GROUP_MARKET_DATA)
                else:
                    await c._send("GET", PRICES, GROUP_MARKET_DATA)
            return _settle(c)
        finally:
            await c.aclose()

    return run(body())


def test_a_lone_429_is_the_whole_blind_spot(tmp_path):
    """① **기전.** 429 가 그 서버 초의 우리 유일한 요청이면 그 초는 사각지대가 된다.

    `note_quota` 가 `status == 429` 에서 곧바로 돌아가므로(docs/06 §9-3) 그 초의
    소진량을 아무도 안 채운다. 운영에서 이것이 48/51 이었다.
    """
    c = _mixed_client(tmp_path, [(0, 429), (1, 200), (2, 200)])
    recs = _run_mixed(c, [(0, 429), (1, 200), (2, 200)])

    blind = [r for r in recs if r["consumed"] < 0]
    assert len(recs) == 3, [r["date"] for r in recs]
    assert len(blind) == 1 and blind[0]["own"] == 1, recs
    assert c.counters["server_second_unknown"] == 1
    assert c.counters["server_seconds"] == 3, "분모가 안 늘면 크기를 못 읽는다"


def test_a_sibling_response_in_the_same_second_removes_the_blind_spot(tmp_path):
    """② 같은 초에 200 이 하나라도 있으면 사각지대가 아니다 — 운영의 3/51 이 이것이다.

    이 대조군이 없으면 "429 = 사각지대" 라는 더 거친 규칙을 참으로 읽게 된다.
    실측 3 건은 전부 `own_in_server_s=2` 였다.
    """
    script = [(0, 429), (0, 200)]
    c = _mixed_client(tmp_path, script)
    recs = _run_mixed(c, script)

    assert len(recs) == 1 and recs[0]["own"] == 2, recs
    assert recs[0]["consumed"] >= 0, "형제 200 의 헤더를 안 썼다"
    assert c.counters["server_second_unknown"] == 0


def test_the_blind_second_hides_a_foreign_sender_but_still_counts_as_observed(tmp_path):
    """③ **무엇이 조용해지나.** 사각지대에서는 `foreign` 이 구조적으로 0 이다.

    같은 초·같은 남의 소비(4건)인데 응답이 200 이면 `foreign=4`, 429 면 0 이다.
    그런데 그 초는 분모(`server_seconds_seen`)에는 **그대로 들어간다** — 즉 사각지대가
    "관측했고 깨끗했다" 로 읽힌다. docs/52 §12.5 는 *"깨끗한 초로 세지 않는다"* 고
    적었는데 분모에서는 그렇지 않다 (docs/52 §13 이 정정한다).
    """
    seen = _mixed_client(tmp_path, [(0, 200)], shadow=4)
    ok = _run_mixed(seen, [(0, 200)])
    assert ok[0]["own"] == 1 and ok[0]["foreign"] == 4, ok

    blind = _mixed_client(tmp_path, [(0, 429)], shadow=4)
    hidden = _run_mixed(blind, [(0, 429)])
    assert hidden[0]["own"] == 1 and hidden[0]["foreign"] == 0, hidden
    assert blind.counters["server_second_unknown"] == 1

    g, _clock = _guard()
    for rec in hidden:
        g.on_server_second(GROUP_MARKET_DATA, rec["own"], rec["consumed"],
                           rec["limit_header"])
    assert g.server_seconds_seen(GROUP_MARKET_DATA) == 1, "분모에는 들어간다"
    assert g.foreign_seconds(GROUP_MARKET_DATA) == 0, "그런데 볼 수 있는 것은 없다"


def test_the_blind_spot_counter_carries_its_denominator(tmp_path):
    """④ **빨강.** `srv_s_unknown` 옆에 분모가 없으면 그 값은 크기가 아니다.

    §7.2 에서 이미 한 번 걸린 실패다: 에포크 없는 0 은 아무 뜻도 없다. 8 이라는 값이
    8/12,000 인지 8/12 인지 텔레메트리만 보고는 못 고른다 — 실제로 코디네이터가
    *"0 → 8 로 변동, 미규명"* 으로 남긴 것이 그 상태였다.
    """
    script = [(0, 429), (1, 200), (2, 200), (3, 200)]
    real = _mixed_client(tmp_path, script)
    ctx = _build_ctx(tmp_path)
    ctx.client = real
    try:
        _run_mixed(real, script)
        ctx.sync_rate_limits(GROUP_MARKET_DATA)
        tel = ctx.telemetry()
        assert tel["srv_s_unknown"] == 1
        assert tel["srv_s_all"] == 4, (
            "사각지대의 분모(정산된 서버 초 전부)가 텔레메트리에 없다")
    finally:
        ctx.store.close()


def test_the_blind_second_is_visible_per_group_in_the_same_window(tmp_path):
    """⑤ **빨강.** 어느 그룹이 얼마나 안 보이는지가 `md_srv_s` 와 **같은 창**에 있어야 한다.

    `srv_s_unknown` 은 프로세스 수명·전 그룹이고 `md_srv_s` 는 60초·MARKET_DATA 다.
    수명도 범위도 다른 두 수로는 "지금 이 그룹의 몇 %가 안 보이나" 를 못 만든다.
    실측에서 사각지대는 전부 `MARKET_DATA_CHART` 였다 (429 51/51).
    """
    g, _clock = _guard()
    g.on_server_second(GROUP_MARKET_DATA, own=1, consumed=-1)
    g.on_server_second(GROUP_MARKET_DATA, own=5, consumed=5)

    assert g.server_seconds_seen(GROUP_MARKET_DATA) == 2
    assert g.server_seconds_unknown(GROUP_MARKET_DATA) == 1
    assert "/srv2/unk1/" in g.describe(), g.describe()

    ctx = _build_ctx(tmp_path)
    try:
        ctx.budget.on_server_second(GROUP_MARKET_DATA, own=1, consumed=-1)
        ctx.budget.on_server_second(GROUP_MARKET_DATA, own=5, consumed=5)
        tel = ctx.telemetry()
        assert tel["md_srv_s"] == 2 and tel["md_srv_unk"] == 1, tel
    finally:
        ctx.store.close()


def test_control_a_clean_load_leaves_the_blind_spot_at_zero(tmp_path):
    """⑥ **대조군.** 429 가 없으면 사각지대도 0 이고, 분모는 관측한 초 수와 같다.

    이것이 초록이라야 위 넷이 "값을 만들어 낸 것" 이 아니다.
    """
    srv = _QuotaServer()
    c = _wire(tmp_path, srv)

    async def body():
        try:
            for _ in range(5):
                await _fire(c, 4)
                srv.tick()
            return _settle(c)
        finally:
            await c.aclose()

    recs = run(body())
    assert len(recs) == 5 and all(r["consumed"] == 4 for r in recs), recs
    assert c.counters["server_second_unknown"] == 0
    assert c.counters["server_seconds"] == 5

    g, _clock = _guard()
    for rec in recs:
        g.on_server_second(GROUP_MARKET_DATA, rec["own"], rec["consumed"],
                           rec["limit_header"])
    assert g.server_seconds_unknown(GROUP_MARKET_DATA) == 0
    assert "/srv5/unk0/" in g.describe(), g.describe()

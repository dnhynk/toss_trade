"""TossClient — GET-only 하드 차단, 배치 청킹, 재시도, 모델 정규화 (계약 C-4·C-5).

모든 요청은 단일 _request() 관문을 지나며 endpoints.ALLOWLIST 를 검사한다.
관문을 우회한 요청도 전송 레벨(GuardedTransport)에서 한 번 더 차단된다 — 이중 차단.

정규화 규약 (계약이 명시하지 않아 W1 이 확정, docs/06_live_facts.md §정규화 참조):
- 캔들: API 는 최신→과거 순으로 주지만 `CandlePage.candles` 는 **과거→최신(ts_ms 오름차순)** 으로 뒤집는다.
- 체결: `Trade` 리스트도 **ts_ms 오름차순**.
- 호가: `bids` 는 가격 내림차순, `asks` 는 가격 오름차순 — 양쪽 모두 **index 0 == 최우선호가**.
- 400/404 응답은 재시도 대상이 아니므로 `SchemaMismatch(detail="http-400 code=...")` 로 올린다
  (계약 C-5 표에 해당 칸이 없어 "재시도 금지 + caller 가 로그·스킵" 정책이 같은 SchemaMismatch 에 매핑).
- 마이크로달러보다 미세한 가격(동전주의 소수 8자리)은 거부하지 않고 반올림한다 (계약 C-2 개정 A4).
  반올림 건수는 `counters["precision_rounded"]` 에 누적되어 조용히 넘어가지 않는다.

429 진단 (2026-08-04, docs/06 §9-3):
`last_headers` 는 "마지막 응답" 이라 429 뒤에 성공 응답이 하나만 지나가도 덮어써진다.
429 는 여기서 1회 재시도되므로 그 재시도가 성공하면 기록은 **항상 200 쪽**이 된다 —
운영 로그에 `HTTP-429-DETAIL ... status=200` 이 남은 것이 그래서였다.
그래서 429 는 받은 **그 자리에서** `last_429` / `recent_429s` / `RateLimited.evidence` 에
찍고, 이후 어떤 응답도 그것을 덮어쓰지 못한다. 429 를 진단할 때 `last_*` 를 보지 말 것.
"""
from __future__ import annotations

import asyncio
import random
import time
from collections import OrderedDict, deque
from contextlib import contextmanager
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Mapping, Sequence

import httpx

from . import models
from .endpoints import canonical_path, check_allowed, group_of
from .errors import (
    AuthExpired,
    Forbidden,
    ForbiddenEndpoint,
    RateLimited,
    SchemaMismatch,
    TransientHTTP,
)
from .limiter import GroupRateLimiter
from .models import (
    Candle,
    CandlePage,
    Orderbook,
    OrderbookLevel,
    Price,
    RankingPage,
    RankingRow,
    SessionWindow,
    StockMeta,
    Trade,
    UsMarketDay,
    dec_to_u,
    iso_to_ms,
    ms_to_iso,
)
from .tokens import TokenManager

BATCH_MAX = 200         # /prices · /stocks 배치 상한
CANDLE_MAX = 200         # /candles 호출당 최대 봉 수
TRADES_MAX = 50          # /trades 호출당 최대 건수
RANKING_MAX = 100        # /rankings 상위 100

TRANSIENT_BACKOFF_S = (0.5, 1.0, 2.0)
MAX_TRANSIENT_RETRIES = 3

# 429 기록 보관 개수. 한 번의 사고에서 여러 발이 나면 첫 발이 가장 중요한데 하나만 들고 있으면
# 그 첫 발이 밀려난다.
RECENT_429_KEEP = 8

# `date` 헤더 기준 초별 요청 수를 몇 초치나 들고 있을지. 429 원인 판별(우리 초과인가 아닌가)과
# **서버 초 감사**(아래 `_ServerSecond`)가 같이 쓴다. 몇 초면 충분하다.
SEC_COUNT_KEEP = 16

# 서버 초 하나를 **정산**하기까지 기다리는 시간 (초).
#
# 왜 기다리나: 같은 서버 초의 응답들이 우리 쪽에 **도착하는 순서**는 서버가 처리한 순서와
# 다르다. 도중에 판정하면 "그 초에 우리가 몇 건 보냈나" 가 아직 덜 찬 상태에서 서버의
# 소진량(`limit - remaining`)과 비교되어 **없는 외부 소비가 보인다.**
# 초가 닫히고 이 시간이 지난 뒤에만 판정한다.
SERVER_SECOND_SETTLE_S = 3.0

# 외부 소비가 관측된 서버 초 기록을 몇 개나 들고 있을지 (진단용, `recent_429s` 와 같은 역할).
SERVER_SECOND_KEEP = 8

# 그룹별 **송신 시각**을 몇 초치나 들고 있을지 (monotonic 초).
# 예산 가드의 관측 지평(`budget.WINDOW_S` = 60초)보다 넉넉해야 한다 — 계상이 잠깐 밀려도
# 그 사이 송신의 시각을 잃지 않는다. 여기서 밀려난 건은 어차피 60초 창 밖이라 첨두·지속률
# 어느 쪽에도 들어가지 않지만, **건수에서는 사라지면 안 되므로** 계상 쪽이 따로 센다
# (`loops.budget_sends_without_time`).
SEND_TIME_HORIZON_S = 180.0

# 429 진단에 남기는 응답 헤더. 시크릿이 실릴 수 있는 헤더는 애초에 목록에 없다
# (응답 헤더이지만 set-cookie 류를 통째로 로그에 흘리지 않기 위해 allowlist 로 간다).
DIAG_HEADER_PREFIXES = ("x-ratelimit", "ratelimit", "retry-after", "x-request-id", "date")

# Retry-After 가 없을 때의 대기값. 서버 창이 **벽시계 초에 정렬된 고정 1초**라
# (docs/06 §9-4) 1.0초를 기다리면 어느 시점에서 재든 반드시 다음 창으로 넘어간다.
DEFAULT_RETRY_AFTER_S = 1.0
# Retry-After 가 HTTP-date 로 올 때의 상한. 스펙상 가능하지만 실측 미관측이라,
# 시계 어긋남으로 터무니없는 값이 나와도 수집이 멈추지 않도록 자른다.
MAX_RETRY_AFTER_S = 60.0

_CAL_KEYS = (("previous", "previousBusinessDay"), ("today", "today"), ("next", "nextBusinessDay"))
_SESSIONS = (("day", "dayMarket"), ("pre", "preMarket"),
             ("regular", "regularMarket"), ("after", "afterMarket"))


class _ServerSecond:
    """**서버가 이름 붙인 한 초**의 감사 기록 — 우리 몫과 전체 소진량을 나란히 둔다.

    이 자리가 특별한 이유 (docs/52 §6 이 지목한 자리다):

    * 창의 이름을 **서버가** 준다 (`date` 헤더의 초). 우리 시계도, 우리 계상도 개입하지
      않는다. 예산의 `peak_1s` 는 우리가 우리 송신을 우리 시계로 센 값이라, 리미터가
      1.15초 하드캡을 지키는 한 **구조적으로** 한도를 넘을 수 없다 (docs/52 §6 증명).
      그 동어반복이 여기에는 없다.
    * 그리고 서버는 매 응답에 **자기가 센 잔량**을 실어 보낸다
      (`x-ratelimit-remaining`, docs/06 §9-1. 톱니 20/20 실측: 그 초의 k 번째 호출이
      정확히 `remaining = limit - k`). 그러니

          그 초의 총 소진량 = limit - remaining        (서버가 센 것, 전원 합계)
          그 초의 우리 몫   = 이 기록의 `own`          (우리가 소켓에서 센 것)
          차이              = **우리가 보내지 않은 요청**

      `sent_by_group` 만으로는 이것을 볼 수 없다 — 우리 client 의 송신만 세기 때문이다
      (docs/52 §6). 이 차이는 docs/06 §9-6 이 "판별되지 않았다" 고 남긴 두 후보
      (같은 자격증명의 다른 발신자 / 그룹 밖의 다른 한도)를 **429 없이** 가리킨다.
      기존 판별 수단(`under_own_limit`)은 429 를 맞아야만 값이 생겼다.

    ⚠️ **`consumed` 를 최댓값으로 잡는다.** 같은 초의 응답들이 우리에게 도착하는 순서는
    서버 처리 순서와 다르므로, 마지막에 도착한 응답의 잔량이 그 초의 최종 상태라는
    보장이 없다. 최댓값은 도착 순서와 무관하다.

    ⚠️ **429 응답의 잔량은 안 믿는다** (docs/06 §9-3: 거절당한 창이 아니라 다음 창의
    상태로 보인 실측이 있다). `limiter.update_from_headers` 와 같은 규칙이다.
    """
    __slots__ = ("at", "own", "consumed", "limit")

    def __init__(self, at: float) -> None:
        self.at = at            # 이 초를 처음 본 시각 (monotonic, 정산 타이머용)
        self.own = 0            # 우리가 이 초에 보낸 요청 수
        self.consumed = -1      # 서버가 말한 소진량의 최댓값. -1 = 모름
        self.limit: int | None = None

    def note_quota(self, headers: Mapping[str, str], status: int | None) -> None:
        if status == 429:
            return                                  # docs/06 §9-3 — 믿지 않는다
        limit = _int_or_none(headers.get("x-ratelimit-limit"))
        remaining = _int_or_none(headers.get("x-ratelimit-remaining"))
        if limit is None or remaining is None:
            return
        if limit <= 0 or remaining < 0 or remaining > limit:
            return                                  # 앞뒤가 안 맞는 헤더는 안 쓴다
        self.limit = limit
        self.consumed = max(self.consumed, limit - remaining)

    def foreign(self) -> int:
        """우리 것이 아닌 소비 건수. 소진량을 모르면 0 (모름은 사고가 아니다)."""
        if self.consumed < 0:
            return 0
        return max(0, self.consumed - self.own)


class GuardedTransport(httpx.AsyncHTTPTransport):
    """전송 레벨 2차 차단. _request 관문을 우회한 요청은 여기서 ForbiddenEndpoint."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        check_allowed(request.method, request.url.path)
        return await super().handle_async_request(request)


class TossClient:
    def __init__(self, base_url: str, tokens: TokenManager, limiter: GroupRateLimiter,
                 timeout_s: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.tokens = tokens
        self.limiter = limiter
        self.timeout_s = timeout_s
        # AUTH 그룹 rate limit 을 토큰 발급 경로에도 적용한다 (감사 A-4).
        # TokenManager 시그니처를 바꾸지 않고도 기존 호출자가 자동으로 이득을 보도록 주입한다.
        if getattr(tokens, "limiter", None) is None:
            try:
                tokens.limiter = limiter
            except AttributeError:
                pass
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout_s,
            transport=GuardedTransport(),
        )
        # 관측용 카운터 (BudgetGuard·리포트에서 읽는다).
        # precision_rounded: 계약 A4 의 초과 정밀도 반올림 건수 (조용한 반올림 방지).
        self.counters: dict[str, int] = {
            "requests": 0, "retries": 0, "http_429": 0, "http_5xx": 0, "auth_refresh": 0,
            "precision_rounded": 0,
            # 429 인데 Retry-After 가 없던 건수. 실측상 **오는 경우와 안 오는 경우가 둘 다 있다**
            # (docs/06 §9-3) — 0 이 아니면 기본 대기값(1.0s)에 의존하고 있다는 뜻이다.
            "http_429_no_retry_after": 0,
            # 429 를 받았는데 **그 서버 초에 우리가 보낸 요청이 한도 미만**이었던 건수.
            # 0 이 아니면 429 의 원인이 이 클라이언트 밖에 있다 (다른 프로세스 / 계정 전체 한도).
            "http_429_under_own_limit": 0,
            # 정산이 끝난 서버 초의 수 — 아래 두 값의 **분모**다.
            # 분모 없는 0 은 아무 뜻도 없다 (docs/52 §7.2 에서 이미 한 번 걸린 실패다).
            "server_seconds": 0,
            # 그 중 서버가 센 소진량이 **우리 송신보다 많았던** 초의 수 (= 남의 소비가 있었다).
            "server_second_foreign": 0,
            # 서버가 소진량을 알려주지 않아 판정할 수 없었던 초의 수 (헤더 없음/모순).
            "server_second_unknown": 0,
        }
        # 진단 전용: 마지막 응답의 상태/헤더. 동시 요청 중에는 어느 요청의 것인지 보장하지 않는다
        # (tools/live_probe.py 처럼 순차 실행하는 경우에만 의미가 있다).
        #
        # ⚠️ 429 진단에 이 둘을 쓰지 말 것. 429 뒤에 성공 응답이 하나만 지나가도 덮어써져
        # `status=200` 인 "429 기록" 이 남는다 (2026-08-04 실제로 그렇게 오독했다).
        # 429 는 아래 `last_429` / `recent_429s` 를 볼 것 — 그쪽은 429 응답 **그 자리에서**
        # 찍히고 이후 응답이 절대 덮어쓰지 않는다.
        self.last_status: int | None = None
        self.last_headers: dict[str, str] = {}

        # 그룹별 송신 수 (누적). **예산 계상의 유일한 귀속 근거**다 (docs/46).
        # `counters["requests"]` 는 전역이라 그 증가분에는 동시에 도는 다른 그룹의 송신이
        # 섞여 있다. 그 델타를 호출한 그룹에 얹던 것이 D1·D2 의 이중 계상이었다.
        # 여기서는 추정이 없다 — `group` 은 `_request` 가 allowlist 로 정한 값이고,
        # 이 카운터는 소켓 직전에 `requests` 와 **같은 자리에서** 오른다.
        self.sent_by_group: dict[str, int] = {}

        # 그룹별 **송신 시각** (monotonic 초). `sent_by_group` 과 **같은 자리**에서 남긴다.
        #
        # 왜 필요한가 (docs/45 §7-3 → docs/52): 예산 계상은 `after_call`/`sync_rate_limits`
        # 에서 일어나는데 그 둘은 **응답이 돌아온 뒤**에 불린다. 그래서 예산에 찍히는 시각은
        # 송신 시각이 아니라 **완료 시각**이었다. 리미터가 지키는 것은 송신이고 우리가 센
        # 것은 완료라, 완료가 몰리면(이벤트루프가 DB 쓰기로 막혔다 풀리는, tier3 폴에서 늘
        # 나는 모양) 넓게 퍼진 송신이 좁은 구간으로 압축되어 첨두가 부푼다.
        # 건수 귀속을 고쳐도(docs/46) 시각은 안 고쳐지므로 잔여가 남았다.
        self.sent_at_by_group: dict[str, deque[float]] = {}

        # 마지막 429 응답의 완전한 기록(상태·헤더·본문 error code). 200 이 덮어쓰지 않는다.
        self.last_429: dict[str, Any] | None = None
        self.recent_429s: deque[dict[str, Any]] = deque(maxlen=RECENT_429_KEEP)
        # (group, 서버 date 초) → 그 서버 초의 감사 기록. 429 가 우리 탓인지 판별용이자
        # **외부 소비 감시**의 원장이다 (`_ServerSecond` docstring 참조).
        self._sec_counts: OrderedDict[tuple[str, str], _ServerSecond] = OrderedDict()
        # 정산이 끝났지만 아직 예산 가드가 안 가져간 서버 초들 (`drain_server_seconds`).
        self._settled_seconds: deque[dict[str, Any]] = deque()
        # 외부 소비가 보인 서버 초의 원본 기록 (사람이 볼 진단용).
        self.recent_server_seconds: deque[dict[str, Any]] = deque(maxlen=SERVER_SECOND_KEEP)

    # ---- 단일 전송 관문 --------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        """유일한 전송 관문. allowlist 검사 → limiter.acquire → 재시도 정책(C-5)."""
        canon = check_allowed(method, path)       # 위반이면 ForbiddenEndpoint (잡지 말 것)
        group = group_of(canon)
        auth_retried = rl_retried = 0
        transient_retried = 0

        while True:
            await self.limiter.acquire(group)
            used: list[str] = []
            try:
                return await self._send(method, path, group, used, **kwargs)
            except AuthExpired:
                if auth_retried >= 1:
                    raise
                auth_retried += 1
                self.counters["auth_refresh"] += 1
                self.counters["retries"] += 1
                # 우리가 **실제로 쓴** 토큰만 무효화한다 (감사 A-2). 그 사이 다른 루프가
                # 새 토큰을 발급했다면 그것은 유효하므로 건드리면 안 된다.
                await self.tokens.invalidate(used[0] if used else None)
            except RateLimited as exc:
                self.counters["http_429"] += 1
                self.limiter.on_429(group, exc.retry_after_s)
                if rl_retried >= 1:
                    raise
                rl_retried += 1
                self.counters["retries"] += 1
                await asyncio.sleep(exc.retry_after_s)
            except TransientHTTP:
                self.counters["http_5xx"] += 1
                if transient_retried >= MAX_TRANSIENT_RETRIES:
                    raise
                delay = TRANSIENT_BACKOFF_S[min(transient_retried, len(TRANSIENT_BACKOFF_S) - 1)]
                transient_retried += 1
                self.counters["retries"] += 1
                await asyncio.sleep(delay + random.uniform(0.0, delay / 2))

    async def _send(self, method: str, path: str, group: str,
                    used: list[str] | None = None, **kwargs) -> dict:
        token = await self.tokens.get()
        if used is not None:
            used.append(token)      # CAS 무효화용 — 이 요청이 실제로 쓴 토큰 (감사 A-2)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        headers.update(kwargs.pop("headers", None) or {})
        self.counters["requests"] += 1
        self.sent_by_group[group] = self.sent_by_group.get(group, 0) + 1
        self._note_sent_at(group)
        try:
            resp = await self._http.request(method, path, headers=headers, **kwargs)
        except ForbiddenEndpoint:
            raise
        except httpx.TimeoutException as exc:
            raise TransientHTTP(0, f"timeout: {type(exc).__name__}") from exc
        except httpx.HTTPError as exc:
            raise TransientHTTP(0, f"transport error: {type(exc).__name__}") from exc

        own_in_second = self._count_in_server_second(group, resp.headers,
                                                     resp.status_code)
        self.limiter.update_from_headers(group, resp.headers,
                                         status=resp.status_code)
        self.last_status = resp.status_code
        self.last_headers = dict(resp.headers)
        if resp.status_code == 429:
            # **429 응답 그 자리에서** 기록한다. 뒤따르는 200 이 덮어쓸 수 없는 자리에.
            self._record_429(method, path, group, resp, own_in_second)
        return _classify(resp, self.last_429 if resp.status_code == 429 else None)

    # ---- 송신 시각 (예산 계상의 시각 근거) --------------------------------

    def _note_sent_at(self, group: str) -> None:
        """이 송신의 시각을 남긴다. `_send` 의 소켓 직전에서만 불린다."""
        now = time.monotonic()
        q = self.sent_at_by_group.get(group)
        if q is None:
            q = self.sent_at_by_group[group] = deque()
        q.append(now)
        horizon = now - SEND_TIME_HORIZON_S
        while q and q[0] < horizon:
            q.popleft()

    def recent_send_ages(self, group: str, n: int) -> list[float]:
        """가장 최근 `n` 건의 송신이 **지금으로부터 몇 초 전**이었나 (오래된 것부터).

        시각이 아니라 **나이**를 돌려주는 이유: 이 시계는 `time.monotonic()` 이고 예산
        가드의 시계는 서버 보정 벽시계(`Clock.now_ms`)다. 원시 시각을 넘기면 시간 기준이
        섞인다. 나이는 두 기준 어디서나 같은 뜻이다.

        지평 밖으로 밀려나 시각을 잃은 건은 `SEND_TIME_HORIZON_S` 로 채운다 — 그 나이는
        예산의 관측 창(60초) 밖이라 첨두·지속률에 영향이 없고, 호출자가 "시각을 잃은
        송신" 으로 셀 수 있다. **건수를 줄이지는 않는다**: 총량이 조용히 줄면 실사용이
        과소평가되고 그것은 한도 사고를 놓치는 방향의 오류다.
        """
        if n <= 0:
            return []
        now = time.monotonic()
        q = self.sent_at_by_group.get(group)
        ages: list[float] = []
        for t in reversed(q or ()):        # 뒤에서 n 건만 — 지평 전체를 복사하지 않는다
            ages.append(max(now - t, 0.0))
            if len(ages) >= n:
                break
        ages.reverse()
        if len(ages) < n:
            ages = [SEND_TIME_HORIZON_S] * (n - len(ages)) + ages
        return ages

    # ---- 429 진단 --------------------------------------------------------

    def _count_in_server_second(self, group: str, headers: Mapping[str, str],
                                status: int | None = None) -> int:
        """이 응답이 속한 **서버 초**에 우리가 보낸 요청 수 (이 요청 포함).

        서버 창이 벽시계 1초 고정이므로(docs/06 §9-4), `date` 헤더의 초가 곧 창 이름이다.
        이 수가 그룹 한도보다 작은데도 429 가 났다면 원인은 이 클라이언트 밖에 있다 —
        같은 자격증명을 쓰는 다른 프로세스이거나, 그룹별이 아닌 다른 한도다.
        (경계에서 렌더된 응답은 `date` 와 카운터가 한 초 어긋날 수 있으므로 ±1 의 오차가 있다.)

        같은 자리에서 **서버가 센 소진량**도 접는다 — `_ServerSecond` docstring 참조.
        """
        now = time.monotonic()
        self._settle_server_seconds(now)
        date = headers.get("date") or headers.get("Date")
        if not date:
            return 0
        key = (group, str(date))
        rec = self._sec_counts.get(key)
        if rec is None:
            rec = self._sec_counts[key] = _ServerSecond(at=now)
        rec.own += 1
        rec.note_quota(headers, status)
        self._sec_counts.move_to_end(key)
        while len(self._sec_counts) > SEC_COUNT_KEEP:
            self._retire_server_second(*self._sec_counts.popitem(last=False))
        return rec.own

    # ---- 서버 초 감사 (남의 소비를 429 없이 본다) --------------------------

    def _settle_server_seconds(self, now: float) -> None:
        """`SERVER_SECOND_SETTLE_S` 가 지난 서버 초를 정산한다.

        앞에서부터 자르지 않고 **전수 검사**한다: 이 OrderedDict 의 순서는 마지막 접근
        순서(`move_to_end`)이지 생성 순서가 아니라, 앞 하나만 보고 멈추면 뒤에 남은
        오래된 초가 영영 정산되지 않을 수 있다. 길이가 `SEC_COUNT_KEEP`(16)로 묶여 있어
        전수 검사가 싸다.
        """
        cutoff = now - SERVER_SECOND_SETTLE_S
        stale = [k for k, rec in self._sec_counts.items() if rec.at <= cutoff]
        for key in stale:
            self._retire_server_second(key, self._sec_counts.pop(key))

    def _retire_server_second(self, key: tuple[str, str], rec: "_ServerSecond") -> None:
        """정산된 서버 초 하나를 **사실 그대로** 내놓는다 (판정은 예산 가드가 한다).

        여기서 한도와 비교하지 않는 이유: 한도 모델(`limit_of`)은 예산 가드의 것이고,
        client 가 자기 나름의 한도를 또 들면 두 곳이 조용히 갈라진다. 이 함수는
        **관측**만 낸다 — 우리가 몇 건 보냈나, 서버는 몇 건이 나갔다고 했나.
        """
        group, date = key
        self.counters["server_seconds"] += 1
        out = {
            "group": group,
            "date": date,
            "own": rec.own,
            # 서버가 그 초에 소진했다고 말한 최댓값. 모르면 -1.
            "consumed": rec.consumed,
            "limit_header": rec.limit,
            # 우리 것이 아닌 소비. `consumed` 를 모르면 0 (모름은 사고가 아니다).
            "foreign": rec.foreign(),
        }
        if rec.consumed < 0:
            self.counters["server_second_unknown"] += 1
        if out["foreign"] > 0:
            self.counters["server_second_foreign"] += 1
            self.recent_server_seconds.append(dict(out, at_ms=int(time.time() * 1000)))
        self._settled_seconds.append(out)

    def drain_server_seconds(self) -> list[dict[str, Any]]:
        """정산이 끝난 서버 초들을 **한 번만** 내준다 (호출자가 가져가면 비워진다).

        고수위 재조회가 아니라 드레인인 이유: 같은 초를 두 번 세면 "남의 소비" 가 두 배로
        보인다. 계상 이중화로 이미 한 번 데었다 (D1·D2, docs/46).
        """
        self._settle_server_seconds(time.monotonic())
        out = list(self._settled_seconds)
        self._settled_seconds.clear()
        return out

    def _record_429(self, method: str, path: str, group: str,
                    resp: httpx.Response, own_in_second: int) -> None:
        headers = {k: v for k, v in resp.headers.items()
                   if k.lower().startswith(DIAG_HEADER_PREFIXES)}
        raw_retry = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
        if raw_retry is None:
            self.counters["http_429_no_retry_after"] += 1
        limit_hdr = _int_or_none(resp.headers.get("x-ratelimit-limit"))
        under_own = bool(limit_hdr and own_in_second and own_in_second < limit_hdr)
        if under_own:
            self.counters["http_429_under_own_limit"] += 1
        rec: dict[str, Any] = {
            "at_ms": int(time.time() * 1000),
            "method": method.upper(),
            "path": canonical_path(path),
            "group": group,
            "status": resp.status_code,
            "headers": headers,
            "retry_after_present": raw_retry is not None,
            "retry_after_s": _retry_after(resp.headers),
            "error_code": _err_code(resp),
            "own_requests_in_that_server_second": own_in_second,
            "limit_header": limit_hdr,
            # True 면 "우리가 그 초에 한도만큼 쏘지 않았는데 429" — 원인이 밖에 있다는 신호.
            "under_own_limit": under_own,
        }
        self.last_429 = rec
        self.recent_429s.append(rec)

    @contextmanager
    def _track_rounding(self):
        """이 블록에서 일어난 초과 정밀도 반올림을 client 카운터에 귀속시킨다 (계약 A4).

        `dec_to_u` 는 모듈 함수라 client 를 모른다. 정규화 구간을 감싸 모듈 카운터의
        증분만 가져온다. 단일 asyncio 루프에서 정규화는 동기 구간이라 교차 오염이 없다.
        """
        before = models.precision_rounded_total()
        try:
            yield
        finally:
            self.counters["precision_rounded"] += (
                models.precision_rounded_total() - before)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- 시세 -----------------------------------------------------------

    async def get_prices(self, symbols: Sequence[str]) -> list[Price]:
        """자동 200개 청킹."""
        out: list[Price] = []
        for chunk in _chunks(symbols, BATCH_MAX):
            body = await self._request("GET", "/api/v1/prices",
                                       params={"symbols": ",".join(chunk)})
            with self._track_rounding():
                for item in _as_list(body, "prices"):
                    out.append(Price(
                        symbol=_req_str(item, "symbol", "prices"),
                        ts_ms=_opt_ms(item.get("timestamp")),
                        last_u=_req_u(item, "lastPrice", "prices"),
                    ))
        return out

    async def get_candles(self, symbol: str, interval: Literal["1m", "1d"],
                          count: int = 200, before_ms: int | None = None,
                          adjusted: bool = True) -> CandlePage:
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "count": min(int(count), CANDLE_MAX),
            "adjusted": "true" if adjusted else "false",
        }
        if before_ms is not None:
            params["before"] = ms_to_iso(before_ms)
        body = await self._request("GET", "/api/v1/candles", params=params)
        result = _as_obj(body, "candles")
        raw = result.get("candles")
        if not isinstance(raw, list):
            raise SchemaMismatch("candles: result.candles is not a list")
        with self._track_rounding():
            candles = [
                Candle(
                    symbol=symbol,
                    ts_ms=_req_ms(item, "timestamp", "candles"),
                    open_u=_req_u(item, "openPrice", "candles"),
                    high_u=_req_u(item, "highPrice", "candles"),
                    low_u=_req_u(item, "lowPrice", "candles"),
                    close_u=_req_u(item, "closePrice", "candles"),
                    vol_qu=_req_u(item, "volume", "candles"),
                )
                for item in raw
            ]
        candles.sort(key=lambda c: c.ts_ms)   # 과거 → 최신
        return CandlePage(candles=candles, next_before_ms=_opt_ms(result.get("nextBefore")))

    async def get_trades(self, symbol: str, count: int = 50) -> list[Trade]:
        body = await self._request("GET", "/api/v1/trades",
                                   params={"symbol": symbol,
                                           "count": min(int(count), TRADES_MAX)})
        with self._track_rounding():
            trades = [
                Trade(
                    symbol=symbol,
                    ts_ms=_req_ms(item, "timestamp", "trades"),
                    price_u=_req_u(item, "price", "trades"),
                    qty_u=_req_u(item, "volume", "trades"),
                )
                for item in _as_list(body, "trades")
            ]
        trades.sort(key=lambda t: t.ts_ms)
        return trades

    async def get_orderbook(self, symbol: str) -> Orderbook:
        body = await self._request("GET", "/api/v1/orderbook", params={"symbol": symbol})
        result = _as_obj(body, "orderbook")
        with self._track_rounding():
            bids = _levels(result.get("bids"), "orderbook.bids")
            asks = _levels(result.get("asks"), "orderbook.asks")
        bids.sort(key=lambda lv: lv.price_u, reverse=True)   # index 0 = 최우선 매수
        asks.sort(key=lambda lv: lv.price_u)                 # index 0 = 최우선 매도
        return Orderbook(symbol=symbol, ts_ms=_opt_ms(result.get("timestamp")),
                         bids=bids, asks=asks)

    # ---- 랭킹·종목 ------------------------------------------------------

    async def get_rankings(self, ranking_type: str, duration: str = "realtime",
                           market: str = "US", count: int = 100,
                           exclude_caution: bool = False) -> RankingPage:
        body = await self._request("GET", "/api/v1/rankings", params={
            "type": ranking_type,
            "marketCountry": market,
            "duration": duration,
            "count": min(int(count), RANKING_MAX),
            "excludeInvestmentCaution": "true" if exclude_caution else "false",
        })
        result = _as_obj(body, "rankings")
        raw = result.get("rankings")
        if not isinstance(raw, list):
            raise SchemaMismatch("rankings: result.rankings is not a list")
        rows: list[RankingRow] = []
        with self._track_rounding():
            for item in raw:
                price = item.get("price")
                if not isinstance(price, dict):
                    raise SchemaMismatch("rankings: row.price missing")
                change = price.get("changeRate")
                rows.append(RankingRow(
                    rank=_req_int(item, "rank", "rankings"),
                    symbol=_req_str(item, "symbol", "rankings"),
                    last_u=_req_u(price, "lastPrice", "rankings.price"),
                    base_u=_req_u(price, "basePrice", "rankings.price"),
                    change_rate=None if change is None else float(Decimal(str(change))),
                    vol_qu=_req_u(item, "tradingVolume", "rankings"),
                    amount_u=_req_u(item, "tradingAmount", "rankings"),
                ))
        return RankingPage(
            ranking_type=ranking_type,
            duration=duration,
            ranked_at_ms=_opt_ms(result.get("rankedAt")),
            rows=rows,
        )

    async def get_stocks(self, symbols: Sequence[str]) -> list[StockMeta]:
        """자동 200개 청킹."""
        out: list[StockMeta] = []
        for chunk in _chunks(symbols, BATCH_MAX):
            body = await self._request("GET", "/api/v1/stocks",
                                       params={"symbols": ",".join(chunk)})
            with self._track_rounding():
                for item in _as_list(body, "stocks"):
                    list_date = item.get("listDate")
                    out.append(StockMeta(
                        symbol=_req_str(item, "symbol", "stocks"),
                        name=_req_str(item, "name", "stocks"),
                        market=_req_str(item, "market", "stocks"),
                        security_type=_req_str(item, "securityType", "stocks"),
                        is_common=bool(item.get("isCommonShare", False)),
                        status=_req_str(item, "status", "stocks"),
                        list_date=None if list_date is None else str(list_date),
                        shares_outstanding_qu=_req_u(item, "sharesOutstanding", "stocks"),
                    ))
        return out

    # ---- 시장 정보 ------------------------------------------------------

    async def get_us_calendar(self, date: str | None = None) -> dict[str, UsMarketDay]:
        """keys: "previous" | "today" | "next"."""
        params = {"date": date} if date else None
        body = await self._request("GET", "/api/v1/market-calendar/US", params=params)
        result = _as_obj(body, "market-calendar/US")
        out: dict[str, UsMarketDay] = {}
        for out_key, api_key in _CAL_KEYS:
            node = result.get(api_key)
            if not isinstance(node, dict):
                raise SchemaMismatch(f"market-calendar/US: missing {api_key}")
            out[out_key] = UsMarketDay(
                date=_req_str(node, "date", "market-calendar/US"),
                **{name: _window(node.get(api_name)) for name, api_name in _SESSIONS},
            )
        return out

    async def get_exchange_rate(self) -> Decimal:
        """KRW per USD (참고용 표시 환율)."""
        body = await self._request("GET", "/api/v1/exchange-rate",
                                   params={"baseCurrency": "USD", "quoteCurrency": "KRW"})
        result = _as_obj(body, "exchange-rate")
        rate = result.get("rate")
        if rate is None:
            raise SchemaMismatch("exchange-rate: missing rate")
        try:
            return Decimal(str(rate))
        except ArithmeticError as exc:
            raise SchemaMismatch(f"exchange-rate: bad rate {rate!r}") from exc


# ---- 응답 분류 ----------------------------------------------------------


def _classify(resp: httpx.Response, evidence: dict | None = None) -> dict:
    """HTTP 응답 → envelope dict 또는 계약 C-5 예외."""
    status = resp.status_code
    if status == 429:
        raise RateLimited(_retry_after(resp.headers), "rate limited", evidence=evidence)
    if status == 401:
        raise AuthExpired(f"401 {_err_code(resp)}")
    if status == 403:
        raise Forbidden(f"403 {_err_code(resp)}")
    if status >= 500:
        raise TransientHTTP(status, f"{status} {_err_code(resp)}")
    if status >= 400:
        # 400/404/409/415/422: 재시도해도 같은 결과 → caller 가 로그+스킵 (계약 C-5 SchemaMismatch 정책)
        raise SchemaMismatch(f"http-{status} {_err_code(resp)}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise SchemaMismatch(f"non-json body ({resp.headers.get('content-type')})") from exc
    if not isinstance(body, dict):
        raise SchemaMismatch(f"envelope is {type(body).__name__}, expected object")
    return body


def _retry_after(headers: Mapping[str, str]) -> float:
    """Retry-After → 초. 실측은 정수 초 문자열이고, **없는 경우도 있다** (docs/06 §9-3).

    없으면 1.0초를 쓴다. 서버 창이 벽시계 초에 정렬된 고정 1초라 1.0초를 기다리면
    어느 위상에서 재든 반드시 다음 창으로 넘어간다 (docs/06 §9-4).
    HTTP-date 형식은 스펙상 가능하지만 미관측이다 — 들어오면 해석하되 상한을 건다.
    """
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return DEFAULT_RETRY_AFTER_S
    text = str(raw).strip()
    try:
        return min(max(float(text), 0.0), MAX_RETRY_AFTER_S)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER_S
    if when is None:
        return DEFAULT_RETRY_AFTER_S
    delta = when.timestamp() - time.time()
    return min(max(delta, 0.0), MAX_RETRY_AFTER_S)


def _int_or_none(raw: Any) -> int | None:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _err_code(resp: httpx.Response) -> str:
    """에러 envelope 에서 code/message 만 뽑는다 (시크릿 미포함 필드)."""
    try:
        err = resp.json().get("error")
    except (ValueError, AttributeError):
        return "<unparseable>"
    if isinstance(err, dict):
        return f"code={err.get('code')} message={err.get('message')}"
    if isinstance(err, str):
        return f"error={err}"
    return "<no error field>"


# ---- 파싱 헬퍼 ----------------------------------------------------------


def _chunks(symbols: Sequence[str], size: int) -> list[list[str]]:
    items = [str(s) for s in symbols if str(s).strip()]
    return [items[i:i + size] for i in range(0, len(items), size)]


def _result(body: dict, where: str) -> Any:
    if "result" not in body:
        raise SchemaMismatch(f"{where}: envelope has no 'result'")
    return body["result"]


def _as_list(body: dict, where: str) -> list[dict]:
    result = _result(body, where)
    if not isinstance(result, list):
        raise SchemaMismatch(f"{where}: result is {type(result).__name__}, expected list")
    for item in result:
        if not isinstance(item, dict):
            raise SchemaMismatch(f"{where}: list item is {type(item).__name__}")
    return result


def _as_obj(body: dict, where: str) -> dict:
    result = _result(body, where)
    if not isinstance(result, dict):
        raise SchemaMismatch(f"{where}: result is {type(result).__name__}, expected object")
    return result


def _req_str(item: Mapping[str, Any], key: str, where: str) -> str:
    val = item.get(key)
    if not isinstance(val, str) or not val:
        raise SchemaMismatch(f"{where}: missing/blank field {key!r}")
    return val


def _req_int(item: Mapping[str, Any], key: str, where: str) -> int:
    val = item.get(key)
    if isinstance(val, bool) or not isinstance(val, int):
        raise SchemaMismatch(f"{where}: field {key!r} is not an int ({val!r})")
    return val


def _req_u(item: Mapping[str, Any], key: str, where: str) -> int:
    if key not in item or item[key] is None:
        raise SchemaMismatch(f"{where}: missing field {key!r}")
    try:
        return dec_to_u(item[key])
    except SchemaMismatch as exc:
        raise SchemaMismatch(f"{where}.{key}: {exc.detail}") from exc


def _req_ms(item: Mapping[str, Any], key: str, where: str) -> int:
    if key not in item or item[key] is None:
        raise SchemaMismatch(f"{where}: missing field {key!r}")
    try:
        return iso_to_ms(item[key])
    except SchemaMismatch as exc:
        raise SchemaMismatch(f"{where}.{key}: {exc.detail}") from exc


def _opt_ms(raw: Any) -> int | None:
    return None if raw is None else iso_to_ms(raw)


def _levels(raw: Any, where: str) -> list[OrderbookLevel]:
    if not isinstance(raw, list):
        raise SchemaMismatch(f"{where}: not a list")
    out: list[OrderbookLevel] = []
    for item in raw:
        if not isinstance(item, dict):
            raise SchemaMismatch(f"{where}: level is {type(item).__name__}")
        out.append(OrderbookLevel(price_u=_req_u(item, "price", where),
                                  qty_u=_req_u(item, "volume", where)))
    return out


def _window(raw: Any) -> SessionWindow | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SchemaMismatch(f"session window is {type(raw).__name__}")
    return SessionWindow(start_ms=_req_ms(raw, "startTime", "session"),
                         end_ms=_req_ms(raw, "endTime", "session"))


__all__ = ["TossClient", "canonical_path"]

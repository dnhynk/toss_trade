"""세션 스케줄러 — 계약 C-8. 소유: W4.

세션 판정은 `/market-calendar/US` 응답만 사용한다 (KST/ET·서머타임 하드코딩 금지, 계약 C-1).
서버가 이미 서머타임을 반영한 **절대시각**을 주므로, 우리는 그 구간에 지금이 들어가는지만 본다
— 그래서 3월/11월 전환일에 코드가 바뀔 일이 없다.

세션 경계 (W1 실측, docs/06 §6, KST)
    day 09:00–17:00 / pre 17:00–22:30 / regular 22:30–05:00 / after 05:00–08:50
데이마켓과 프리마켓이 17:00 경계를 공유하므로 구간 판정은 **반열림 `[start, end)`** 로 한다.

시간 기준
--------
로컬 시계가 틀어져 있으면 세션 판정과 `snap_ms` 가 통째로 어긋난다. `Clock` 이 HTTP `Date`
헤더로 서버-로컬 오차를 상시 추정해 보정한다(중앙값). 정확도는 초 단위지만 세션 경계 판정에는
충분하다.
"""
from __future__ import annotations

import asyncio
import statistics
import time
from collections import deque
from email.utils import parsedate_to_datetime
from typing import Iterable, Mapping, Sequence

from ..api.errors import Forbidden, TossApiError
from ..api.models import SessionWindow, UsMarketDay

SESSION_NAMES: tuple[str, ...] = ("day", "pre", "regular", "after")
CLOSED = "closed"

#: 캘린더 자동 갱신 주기 (초). 하루가 넘어가면 today/next 가 밀리므로 주기적으로 다시 받는다.
CALENDAR_TTL_S = 6 * 3600
#: 캘린더 조회 실패 시 재시도 백오프 (초).
CALENDAR_RETRY_S = (5.0, 15.0, 60.0, 300.0)
#: 서버-로컬 시간차가 이보다 크면 경보 (초).
CLOCK_SKEW_ALERT_S = 5.0
#: `Date` 헤더는 초 해상도라 참값이 [t, t+1) 에 균일 분포한다 — 중앙값 보정.
_DATE_HEADER_BIAS_MS = 500


# --------------------------------------------------------------------------- #
# 계약 C-8: current_session
# --------------------------------------------------------------------------- #
def calendar_windows(cal: Mapping[str, UsMarketDay] | Iterable[UsMarketDay]
                     ) -> list[tuple[int, int, str]]:
    """(start_ms, end_ms, session) 시간순 목록. dict(previous/today/next)·리스트 모두 받는다."""
    days: Iterable[UsMarketDay]
    days = cal.values() if isinstance(cal, Mapping) else cal
    out: list[tuple[int, int, str]] = []
    for md in days:
        if md is None:
            continue
        for name in SESSION_NAMES:
            win: SessionWindow | None = getattr(md, name, None)
            if win is not None and win.end_ms > win.start_ms:
                out.append((int(win.start_ms), int(win.end_ms), name))
    out.sort()
    return out


def current_session(cal: dict[str, UsMarketDay], now_ms: int) -> str:
    """"day" | "pre" | "regular" | "after" | "closed" — 반열림 구간 `[start, end)`."""
    for start_ms, end_ms, name in calendar_windows(cal):
        if start_ms <= now_ms < end_ms:
            return name
    return CLOSED


def next_transition_ms(cal: Mapping[str, UsMarketDay] | Iterable[UsMarketDay],
                       now_ms: int) -> int | None:
    """다음 세션 전환 시각 (현재 세션의 끝 또는 다음 세션의 시작). 없으면 None."""
    best: int | None = None
    for start_ms, end_ms, _name in calendar_windows(cal):
        for edge in (start_ms, end_ms):
            if edge > now_ms and (best is None or edge < best):
                best = edge
    return best


def session_window(cal: Mapping[str, UsMarketDay] | Iterable[UsMarketDay],
                   now_ms: int) -> SessionWindow | None:
    """`now_ms` 가 속한 세션의 윈도우. 세션 밖이면 None."""
    for start_ms, end_ms, _name in calendar_windows(cal):
        if start_ms <= now_ms < end_ms:
            return SessionWindow(start_ms=start_ms, end_ms=end_ms)
    return None


def trading_day_of(md: UsMarketDay) -> tuple[int, int] | None:
    """한 매매일의 (첫 세션 시작, 마지막 세션 끝)."""
    wins = calendar_windows([md])
    if not wins:
        return None
    return wins[0][0], wins[-1][1]


def exclude_today_1d_cutoff(md: UsMarketDay | None) -> int | None:
    """일봉 베이스라인에서 **당일 진행형 봉**을 잘라낼 컷오프 ms (함정4).

    일봉 timestamp 는 00:00 ET(=13:00 KST) 고정 날짜 키다(docs/06 §2-2). 당일 봉은 장중에도
    이미 존재하며 진행형이라 완성봉으로 쓰면 베이스라인이 깨진다.

    컷오프 = **오늘 정규장 시작 − 24h**. 오늘 일봉(정규장 −9.5h)은 컷오프보다 뒤라 잘리고,
    전일 일봉(−33.5h)은 앞이라 남는다. 서머타임 ±1h 로는 이 24h 여유가 뒤집히지 않는다.
    정규장이 없는 날에는 그 날의 첫 세션 시작을 기준으로 같은 여유를 준다.
    """
    if md is None:
        return None
    anchor = md.regular.start_ms if md.regular is not None else None
    if anchor is None:
        bounds = trading_day_of(md)
        if bounds is None:
            return None
        anchor = bounds[0]
    return int(anchor) - 86_400_000


# --------------------------------------------------------------------------- #
# 시계 (로컬-서버 오차 보정)
# --------------------------------------------------------------------------- #
class Clock:
    """벽시계 + 서버 오프셋. 컬렉터의 모든 시각은 이 객체를 통해서만 얻는다.

    테스트는 `now_ms`/`sleep` 만 재정의해 가속 리플레이용 가상 시계를 만든다.
    """

    def __init__(self, *, max_samples: int = 9, alert_skew_s: float = CLOCK_SKEW_ALERT_S,
                 notifier=None, sync: bool = True) -> None:
        self._samples: deque[int] = deque(maxlen=max_samples)
        self.offset_ms = 0
        self.alert_skew_ms = int(alert_skew_s * 1000)
        self.notifier = notifier
        #: False 면 서버 시각을 따라가지 않는다 (가상 시계·오프라인 리플레이).
        self.sync = bool(sync)
        self._skew_alerted = False

    # ---- 시각 ----------------------------------------------------------

    def local_now_ms(self) -> int:
        return int(time.time() * 1000)

    def now_ms(self) -> int:
        """서버 보정 시각 (UTC epoch ms, 계약 C-1)."""
        return self.local_now_ms() + self.offset_ms

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(float(seconds), 0.0))

    async def sleep_until(self, deadline_ms: int) -> None:
        await self.sleep(max(deadline_ms - self.now_ms(), 0) / 1000.0)

    # ---- 오프셋 추정 ----------------------------------------------------

    def observe_server_ms(self, server_ms: int, local_ms: int | None = None) -> int:
        """서버 시각 표본 1건 반영. 반환: 갱신된 오프셋(ms)."""
        if not self.sync:
            return self.offset_ms
        local = self.local_now_ms() if local_ms is None else int(local_ms)
        self._samples.append(int(server_ms) - local)
        self.offset_ms = int(statistics.median(self._samples))
        if abs(self.offset_ms) >= self.alert_skew_ms and not self._skew_alerted:
            self._skew_alerted = True
            if self.notifier is not None:
                self.notifier.alert(
                    f"clock skew {self.offset_ms / 1000:.1f}s between local and server — "
                    "세션 판정/스냅샷 시각을 서버 기준으로 보정한다")
        return self.offset_ms

    def observe_headers(self, headers: Mapping[str, str] | None) -> bool:
        """HTTP `Date` 헤더로 오프셋 갱신. 헤더가 없거나 파싱 불가면 False."""
        if not headers:
            return False
        raw = headers.get("date") or headers.get("Date")
        if not raw:
            return False
        try:
            dt = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return False
        if dt is None or dt.tzinfo is None:
            return False
        self.observe_server_ms(int(dt.timestamp() * 1000) + _DATE_HEADER_BIAS_MS)
        return True


# --------------------------------------------------------------------------- #
# 세션 스케줄러
# --------------------------------------------------------------------------- #
class SessionScheduler:
    """캘린더를 받아 캐시하고, 지금이 어느 세션인지 알려준다.

    네트워크가 끊겨도 마지막으로 받은 캘린더로 계속 판정한다 — 캘린더는 하루 단위 정보라
    수 분의 단절로 무효가 되지 않는다. 캘린더를 **한 번도** 받지 못했으면 `closed` 를 반환해
    예산을 태우지 않는다(모르는 채로 폴링하지 않는다).
    """

    def __init__(self, client, clock: Clock | None = None, *, notifier=None,
                 ttl_s: float = CALENDAR_TTL_S) -> None:
        self.client = client
        self.clock = clock or Clock(notifier=notifier)
        self.notifier = notifier
        self.ttl_s = float(ttl_s)
        self.calendar: dict[str, UsMarketDay] = {}
        self._by_date: dict[str, UsMarketDay] = {}
        self.fetched_ms: int | None = None
        self.session: str = CLOSED
        self.failures = 0

    # ---- 캘린더 --------------------------------------------------------

    def needs_refresh(self, now_ms: int | None = None) -> bool:
        if not self.calendar or self.fetched_ms is None:
            return True
        now = self.clock.now_ms() if now_ms is None else now_ms
        if now - self.fetched_ms >= self.ttl_s * 1000:
            return True
        wins = calendar_windows(self.calendar)
        # 캐시가 커버하지 못하는 시각으로 넘어갔으면 다시 받는다.
        return bool(wins) and not (wins[0][0] <= now < wins[-1][1])

    async def refresh(self, *, force: bool = False) -> bool:
        """캘린더 갱신. 실패해도 예외를 올리지 않고 False (마지막 캘린더 유지)."""
        if not force and not self.needs_refresh():
            return True
        try:
            cal = await self.client.get_us_calendar()
        except Forbidden:
            raise                                   # 치명 — 최상위가 처리 (계약 C-5)
        except (TossApiError, OSError) as exc:
            self.failures += 1
            if self.notifier is not None:
                self.notifier.warn(f"calendar refresh failed ({self.failures}): "
                                   f"{type(exc).__name__}: {exc}")
            return False
        self.clock.observe_headers(getattr(self.client, "last_headers", None))
        self.calendar = cal
        self._by_date.update({md.date: md for md in cal.values() if md is not None})
        self.fetched_ms = self.clock.now_ms()
        self.failures = 0
        return True

    async def ensure(self) -> bool:
        """캘린더가 없거나 낡았으면 갱신 시도. 반환: 쓸 수 있는 캘린더가 있는가."""
        if self.needs_refresh():
            await self.refresh(force=True)
        return bool(self.calendar)

    async def history(self, days: int) -> list[UsMarketDay]:
        """오늘 이전의 영업일 `days` 개 (오래된 순). RVOL 곡선 분모용.

        `/market-calendar/US?date=` 를 뒤로 걸어가며 모은다. MARKET_INFO(3/s) 그룹이고
        하루 한 번이면 되므로 예산 영향은 무시할 수 있다. 실패하면 모은 만큼만 돌려준다.
        """
        if days <= 0 or not await self.ensure():
            return []
        out: list[UsMarketDay] = []
        cursor = self.calendar.get("previous")
        while cursor is not None and len(out) < days:
            out.append(cursor)
            cached = self._by_date.get(cursor.date)
            if cached is not None and cached is not cursor:
                cursor = cached
            if len(out) >= days:
                break
            try:
                page = await self.client.get_us_calendar(date=cursor.date)
            except Forbidden:
                raise
            except (TossApiError, OSError) as exc:
                if self.notifier is not None:
                    self.notifier.warn(f"calendar history stopped at {cursor.date}: "
                                       f"{type(exc).__name__}: {exc}")
                break
            self._by_date.update({md.date: md for md in page.values() if md is not None})
            cursor = page.get("previous")
        out.reverse()
        return out

    # ---- 세션 판정 ------------------------------------------------------

    def session_at(self, now_ms: int | None = None) -> str:
        if not self.calendar:
            return CLOSED
        now = self.clock.now_ms() if now_ms is None else now_ms
        return current_session(self.calendar, now)

    def today(self) -> UsMarketDay | None:
        return self.calendar.get("today")

    def market_day_at(self, now_ms: int) -> UsMarketDay | None:
        """`now_ms` 가 속한 매매일 (정규장이 자정을 넘으므로 today 와 다를 수 있다)."""
        for md in self.calendar.values():
            bounds = trading_day_of(md)
            if bounds is not None and bounds[0] <= now_ms < bounds[1]:
                return md
        return None

    def calendar_list(self, extra: Sequence[UsMarketDay] = ()) -> list[UsMarketDay]:
        """분석 함수(`calendar=` 인자)에 넘길 시간순 목록. 날짜 중복은 제거한다."""
        merged: dict[str, UsMarketDay] = {}
        for md in list(extra) + [md for md in self.calendar.values() if md is not None]:
            if md is not None:
                merged.setdefault(md.date, md)
        return sorted(merged.values(), key=lambda md: (trading_day_of(md) or (0, 0))[0])

    def next_transition_ms(self, now_ms: int | None = None) -> int | None:
        now = self.clock.now_ms() if now_ms is None else now_ms
        return next_transition_ms(self.calendar, now)

    def poll(self, now_ms: int | None = None) -> tuple[str, str] | None:
        """세션을 다시 판정하고, 바뀌었으면 (이전, 현재) 를 반환한다."""
        new = self.session_at(now_ms)
        if new == self.session:
            return None
        prev, self.session = self.session, new
        return prev, new


__all__ = [
    "CLOSED", "SESSION_NAMES", "Clock", "SessionScheduler", "calendar_windows",
    "current_session", "exclude_today_1d_cutoff", "next_transition_ms", "session_window",
    "trading_day_of",
]

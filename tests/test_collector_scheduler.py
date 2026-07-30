"""세션 스케줄러 (계약 C-8) — 캘린더 기반 판정, 서머타임 무하드코딩, 시계 오차 보정."""
from __future__ import annotations

import asyncio
from email.utils import formatdate

import pytest

from tests.test_collector_helpers import (MIN_MS, FrozenClock, calendar_dict, simple_day)
from tossmon.api.errors import TransientHTTP
from tossmon.api.models import SessionWindow, UsMarketDay
from tossmon.collector.scheduler import (CLOSED, Clock, SessionScheduler,
                                         current_session, exclude_today_1d_cutoff,
                                         next_transition_ms, session_window)

DAY0 = 1_785_000_000_000          # 임의의 기준 (절대시각만 쓰므로 값 자체는 의미 없다)
HOUR_MS = 3_600_000


@pytest.fixture()
def cal():
    days = [simple_day("2026-07-29", DAY0),
            simple_day("2026-07-30", DAY0 + 24 * HOUR_MS),
            simple_day("2026-07-31", DAY0 + 48 * HOUR_MS)]
    return calendar_dict(days, 1), days


def test_sessions_are_half_open_intervals(cal):
    """day/pre 가 경계를 공유하므로 `[start, end)` 여야 중복 없이 갈린다 (docs/06 §6)."""
    c, days = cal
    today = days[1]
    assert current_session(c, today.day.start_ms) == "day"
    assert current_session(c, today.day.end_ms - 1) == "day"
    assert current_session(c, today.day.end_ms) == "pre"          # 경계는 다음 세션
    assert current_session(c, today.pre.end_ms) == "regular"
    assert current_session(c, today.regular.end_ms) == "after"
    assert current_session(c, today.after.end_ms) == CLOSED


def test_previous_day_session_spilling_past_midnight(cal):
    """정규장은 자정을 넘긴다 — previous 의 윈도우로도 판정돼야 한다."""
    c, days = cal
    yesterday = days[0]
    assert current_session(c, yesterday.regular.start_ms + 60 * MIN_MS) == "regular"


def test_no_dst_hardcoding_answer_follows_the_calendar():
    """같은 벽시계 시각이라도 캘린더가 1시간 밀리면 판정도 따라 밀린다.

    서머타임은 서버가 절대시각에 이미 반영해 준다 (docs/06 §6). 우리 쪽에 규칙이 있으면
    이 테스트가 깨진다.
    """
    base = simple_day("2026-03-08", DAY0)
    shifted = simple_day("2026-03-08", DAY0 - HOUR_MS)
    probe = base.regular.start_ms - 30 * MIN_MS            # 정규장 30분 전
    assert current_session({"today": base}, probe) == "pre"
    assert current_session({"today": shifted}, probe) == "regular"


def test_closed_when_calendar_has_no_windows():
    empty = UsMarketDay(date="2026-01-01", day=None, pre=None, regular=None, after=None)
    assert current_session({"today": empty}, DAY0) == CLOSED
    assert current_session({}, DAY0) == CLOSED


def test_next_transition_and_window(cal):
    c, days = cal
    today = days[1]
    probe = today.regular.start_ms + 5 * MIN_MS
    assert next_transition_ms(c, probe) == today.regular.end_ms
    win = session_window(c, probe)
    assert win == SessionWindow(start_ms=today.regular.start_ms,
                                end_ms=today.regular.end_ms)
    assert session_window(c, days[2].after.end_ms + 1) is None


def test_exclude_today_1d_cutoff_drops_only_the_progressing_bar(cal):
    """함정4: 당일 일봉은 장중에도 존재하며 진행형이다."""
    _c, days = cal
    today = days[1]
    cutoff = exclude_today_1d_cutoff(today)
    today_1d_ts = today.regular.start_ms - int(9.5 * HOUR_MS)     # 00:00 ET
    yesterday_1d_ts = today_1d_ts - 24 * HOUR_MS
    assert today_1d_ts > cutoff                                    # 잘린다
    assert yesterday_1d_ts <= cutoff                               # 남는다


def test_exclude_today_1d_cutoff_survives_dst_shift():
    """±1h 서머타임 이동으로는 24h 여유가 뒤집히지 않는다."""
    for shift in (-HOUR_MS, 0, HOUR_MS):
        md = simple_day("2026-11-01", DAY0 + shift)
        cutoff = exclude_today_1d_cutoff(md)
        assert md.regular.start_ms - int(9.5 * HOUR_MS) > cutoff
        assert md.regular.start_ms - int(33.5 * HOUR_MS) <= cutoff


# --------------------------------------------------------------------------- #
# 시계
# --------------------------------------------------------------------------- #
def test_clock_learns_offset_from_date_header():
    clock = Clock()
    local = clock.local_now_ms()
    server = local + 42_000
    clock.observe_headers({"date": formatdate(server / 1000, usegmt=True)})
    assert 40_000 <= clock.offset_ms <= 44_000            # 초 해상도 ±1s
    assert clock.now_ms() > local


def test_clock_uses_median_and_ignores_a_single_outlier():
    clock = Clock()
    base = clock.local_now_ms()
    for delta in (1000, 1100, 900, 1_000_000, 1050):
        clock.observe_server_ms(base + delta, local_ms=base)
    assert abs(clock.offset_ms - 1050) <= 100


def test_clock_sync_can_be_disabled_for_virtual_time():
    clock = Clock(sync=False)
    clock.observe_headers({"date": formatdate(usegmt=True)})
    assert clock.offset_ms == 0


def test_clock_alerts_once_on_large_skew():
    class Rec:
        def __init__(self):
            self.msgs = []

        def alert(self, msg):
            self.msgs.append(msg)

    rec = Rec()
    clock = Clock(notifier=rec, alert_skew_s=1.0)
    base = clock.local_now_ms()
    clock.observe_server_ms(base + 30_000, local_ms=base)
    clock.observe_server_ms(base + 31_000, local_ms=base)
    assert len(rec.msgs) == 1 and "clock skew" in rec.msgs[0]


# --------------------------------------------------------------------------- #
# SessionScheduler
# --------------------------------------------------------------------------- #
class FakeCalClient:
    def __init__(self, days, *, fail_after=None):
        self.days = days
        self.fail_after = fail_after
        self.calls = 0
        self.last_headers: dict[str, str] = {}

    async def get_us_calendar(self, date=None):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise TransientHTTP(0, "network down")
        idx = 1 if date is None else next(
            (i for i, md in enumerate(self.days) if md.date == date), 1)
        return calendar_dict(self.days, idx)


def _days(n=5):
    return [simple_day(f"2026-07-{25 + i}", DAY0 + i * 24 * HOUR_MS) for i in range(n)]


def test_scheduler_refresh_and_session(cal):
    days = _days()
    client = FakeCalClient(days)
    clock = FrozenClock(days[1].regular.start_ms + MIN_MS)
    sched = SessionScheduler(client, clock)
    assert asyncio.run(sched.ensure()) is True
    assert sched.session_at() == "regular"
    assert sched.poll() == (CLOSED, "regular")
    assert sched.poll() is None                     # 두 번째는 변화 없음


def test_scheduler_keeps_last_calendar_when_network_dies(cal):
    days = _days()
    client = FakeCalClient(days, fail_after=1)
    clock = FrozenClock(days[1].regular.start_ms + MIN_MS)
    sched = SessionScheduler(client, clock, ttl_s=0.0)   # 매번 갱신 시도
    assert asyncio.run(sched.ensure()) is True
    assert asyncio.run(sched.refresh(force=True)) is False   # 실패해도 예외 없음
    assert sched.session_at() == "regular"               # 마지막 캘린더로 계속 판정
    assert sched.failures == 1


def test_scheduler_returns_closed_without_any_calendar():
    client = FakeCalClient(_days(), fail_after=0)
    sched = SessionScheduler(client, FrozenClock(DAY0))
    assert asyncio.run(sched.ensure()) is False
    assert sched.session_at() == CLOSED               # 모르면 폴링하지 않는다


def test_scheduler_history_walks_backwards():
    days = _days(5)
    client = FakeCalClient(days)
    clock = FrozenClock(days[1].regular.start_ms)
    sched = SessionScheduler(client, clock)
    hist = asyncio.run(sched.history(2))
    assert [md.date for md in hist] == [days[0].date]   # 캘린더가 커버하는 만큼만
    merged = [md.date for md in sched.calendar_list(extra=hist)]
    assert merged == sorted(set(merged))                # 시간순·중복 없음


def test_scheduler_needs_refresh_when_time_leaves_cache():
    days = _days()
    client = FakeCalClient(days)
    clock = FrozenClock(days[1].regular.start_ms)
    sched = SessionScheduler(client, clock)
    asyncio.run(sched.ensure())
    assert sched.needs_refresh() is False
    clock.advance(5 * 24 * 3600)                        # 캐시 밖으로 이동
    assert sched.needs_refresh() is True

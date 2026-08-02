"""대량 백필 러너 (tools/backfill.py) — 사전등록 §2.8 집행 회귀.

무엇을 지키는가
    * 스크린 경계값: +15% / z>=3 / 레인지 20% 가 **정확히 그 값에서** 갈린다 (얼린 값).
    * [D-25, D+2] **매매일** 산정 (주말·연말 걸침 포함), 달력 부족 시 클램프가 드러난다.
    * 1분봉은 원주가(adjusted=false)로만 — 수정주가로 바꾸면 기동이 거부된다 (§7-e).
    * 멱등 재실행: 체크포인트가 이미 받은 구간을 건너뛴다 (호출 0회).
    * before 는 inclusive (docs/06 함정3) — 경계 봉이 이중 계상되지 않는다.
    * 무체결 분 캔들 부재(함정5)는 오류가 아니다 — 커버리지는 실봉 수로만 센다.
    * 홀드아웃(2026-05-01~07-29) 매매일의 판정 근거·수치는 어떤 출력에도 없다 (§6.2).
"""
from __future__ import annotations

import asyncio
import importlib.util
import math
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.test_collector_helpers import make_config
from tossmon.api.models import Candle, CandlePage, SessionWindow, UsMarketDay
from tossmon.store.writer import Store

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_backfill():
    import sys

    if "tossmon_backfill" in sys.modules:
        return sys.modules["tossmon_backfill"]
    spec = importlib.util.spec_from_file_location(
        "tossmon_backfill", REPO_ROOT / "tools" / "backfill.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["tossmon_backfill"] = module               # dataclass 가 모듈을 찾는다
    spec.loader.exec_module(module)
    return module


BF = _load_backfill()

MIN_MS = 60_000
DAY_MS = 86_400_000


def utc_ms(date: str, hour: int = 0, minute: int = 0) -> int:
    dt = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int((dt + timedelta(hours=hour, minutes=minute)).timestamp() * 1000)


def weekdays(start: str, end: str) -> list[str]:
    cur = datetime.strptime(start, "%Y-%m-%d")
    stop = datetime.strptime(end, "%Y-%m-%d")
    out = []
    while cur <= stop:
        if cur.weekday() < 5:
            out.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return out


def market_day(date: str) -> UsMarketDay:
    """토스 4세션 하루 — 데이 09:00 KST(=00:00 UTC) 시작, 총 1430분."""
    base = utc_ms(date)
    return UsMarketDay(
        date=date,
        day=SessionWindow(start_ms=base, end_ms=base + 480 * MIN_MS),
        pre=SessionWindow(start_ms=base + 480 * MIN_MS, end_ms=base + 810 * MIN_MS),
        regular=SessionWindow(start_ms=base + 810 * MIN_MS, end_ms=base + 1200 * MIN_MS),
        after=SessionWindow(start_ms=base + 1200 * MIN_MS, end_ms=base + 1430 * MIN_MS),
    )


def daily_bar(symbol: str, date: str, *, close=1_000_000, high=None, low=None,
              open_=None, vol=5_000_000) -> Candle:
    # 일봉 timestamp = 00:00 ET(여름 04:00 UTC) 고정 날짜 키 (docs/06 §2-2)
    return Candle(symbol=symbol, ts_ms=utc_ms(date, hour=4), open_u=open_ or close,
                  high_u=high or close + 1_000, low_u=low or close - 1_000,
                  close_u=close, vol_qu=vol)


# --------------------------------------------------------------------------- #
# 스크린 경계값 (§2.8 얼린 값 — 그 값에서 정확히 갈려야 한다)
# --------------------------------------------------------------------------- #
def _screen(bars):
    days, stats = BF.screen_daily(bars)
    return days, stats


def _flat(symbol, dates, **kw):
    return [BF.DailyBar(utc_ms(d, hour=4), 1_000_000, 1_001_000, 999_000,
                        1_000_000, 5_000_000) for d in dates]


def test_screen_ret_boundary_is_exactly_15_percent():
    dates = weekdays("2026-02-02", "2026-02-06")
    bars = _flat("X", dates[:2])
    bars.append(BF.DailyBar(utc_ms(dates[2], hour=4), 1_150_000, 1_151_000, 1_149_000,
                            1_150_000, 5_000_000))          # 정확히 +15.0%
    bars.append(BF.DailyBar(utc_ms(dates[3], hour=4), 1_322_499, 1_322_500, 1_322_400,
                            1_322_499, 5_000_000))          # +14.9999...% — 미달
    days, _ = _screen(bars)
    assert [d.date for d in days] == [dates[2]]
    assert days[0].reasons == ["ret"]


def test_screen_range_boundary_is_exactly_20_percent():
    dates = weekdays("2026-02-02", "2026-02-06")
    bars = _flat("X", dates[:1])
    bars.append(BF.DailyBar(utc_ms(dates[1], hour=4), 1_000_000, 1_200_000, 1_000_000,
                            1_000_000, 5_000_000))          # H/L-1 = 정확히 20%
    bars.append(BF.DailyBar(utc_ms(dates[2], hour=4), 1_000_000, 1_199_999, 1_000_000,
                            1_000_000, 5_000_000))          # 19.9999% — 미달
    days, _ = _screen(bars)
    assert [d.date for d in days] == [dates[1]]
    assert days[0].reasons == ["range"]


def test_screen_logvol_z_boundary_is_exactly_3():
    dates = weekdays("2026-01-05", "2026-02-04")            # 20+ 매매일
    prior = dates[:20]
    vols = [5_000_000 + (i % 2) * 1_000_000 for i in range(20)]   # 분산이 0이 아니게
    bars = [BF.DailyBar(utc_ms(d, hour=4), 1_000_000, 1_001_000, 999_000,
                        1_000_000, v) for d, v in zip(prior, vols)]
    logs = [math.log(v) for v in vols]
    mean, sd = statistics.fmean(logs), statistics.stdev(logs)
    v_pass = math.exp(mean + 3.0 * sd) * 1.000001           # z 살짝 >= 3
    v_fail = math.exp(mean + 3.0 * sd) * 0.999  # z 살짝 < 3 (창은 그대로 20일이 되게 뒤에서 검증)
    bars.append(BF.DailyBar(utc_ms(dates[20], hour=4), 1_000_000, 1_001_000, 999_000,
                            1_000_000, int(v_pass)))
    days, _ = _screen(bars)
    assert [d.date for d in days] == [dates[20]]
    assert days[0].reasons == ["logvol_z"]

    bars_fail = bars[:-1] + [BF.DailyBar(utc_ms(dates[20], hour=4), 1_000_000,
                                         1_001_000, 999_000, 1_000_000, int(v_fail))]
    days_fail, _ = _screen(bars_fail)
    assert days_fail == []


def test_screen_zero_volume_and_flat_variance_do_not_crash_or_pass():
    """무체결(볼륨 0)·분산 0 은 판정 불가로 세고, 통과로 치지 않는다."""
    dates = weekdays("2026-02-02", "2026-02-27")
    bars = _flat("X", dates[:15])
    bars.append(BF.DailyBar(utc_ms(dates[15], hour=4), 1_000_000, 1_001_000, 999_000,
                            1_000_000, 999_000_000))        # 분산 0 창 → z 판정 불가
    bars.append(BF.DailyBar(utc_ms(dates[16], hour=4), 1_000_000, 1_001_000, 0,
                            1_000_000, 0))                  # low=0, vol=0
    days, stats = _screen(bars)
    assert days == []                                        # 아무것도 통과하지 않는다
    assert stats["range_undefined"] >= 1


# --------------------------------------------------------------------------- #
# [D-25, D+2] 매매일 창 (겨울 자정 넘김·주말·연말 걸침)
# --------------------------------------------------------------------------- #
def make_calendar(dates: list[str]) -> "BF.TradingCalendar":
    cal = BF.TradingCalendar()
    for d in dates:
        cal.add(market_day(d))
    return cal


def test_window_spans_25_back_2_forward_trading_days_across_the_year_end():
    dates = weekdays("2025-11-17", "2026-02-27")            # 연말·신정 걸침
    cal = make_calendar(dates)
    d = "2026-01-09"
    days, start_ms, end_ms, clamp = cal.window_for(d)
    assert clamp is None
    i = dates.index(d)
    assert days == dates[i - 25:i + 3]                      # 25 + D + 2 = 28 매매일
    assert len(days) == 28
    assert start_ms == utc_ms(days[0])                      # 첫 매매일 첫 세션 시작
    assert end_ms == utc_ms(days[-1]) + 1430 * MIN_MS       # 마지막 매매일 마지막 세션 끝
    # 주말은 창에 들어오지 않는다
    assert all(datetime.strptime(x, "%Y-%m-%d").weekday() < 5 for x in days)


def test_window_crossing_utc_midnight_uses_session_bounds_not_dates():
    """겨울(EST)은 정규장·애프터가 UTC 자정을 넘는다 — 경계는 세션 ms 로 정한다."""
    base = utc_ms("2026-01-15", hour=14)                    # 겨울: 세션이 늦게 시작
    md = UsMarketDay(
        date="2026-01-15",
        day=SessionWindow(start_ms=base, end_ms=base + 8 * 60 * MIN_MS),
        pre=None, regular=None,
        after=SessionWindow(start_ms=base + 8 * 60 * MIN_MS,
                            end_ms=base + 15 * 60 * MIN_MS))   # 다음 UTC 날로 넘어간다
    bounds = BF.day_bounds(md)
    assert bounds == (base, base + 15 * 60 * MIN_MS)
    assert BF._utc_date(bounds[1]) == "2026-01-16"          # 실제로 자정을 넘었다


def test_window_clamps_when_the_calendar_cannot_reach_25_days_back():
    dates = weekdays("2026-01-05", "2026-02-27")
    cal = make_calendar(dates)
    days, _s, _e, clamp = cal.window_for(dates[5])          # 앞이 5일뿐
    assert clamp == "calendar_short_back"
    assert days[0] == dates[0]


def test_merge_spans_merges_overlaps_only():
    assert BF.merge_spans([(0, 10), (5, 20), (30, 40)]) == [(0, 20), (30, 40)]
    assert BF.merge_spans([(30, 40), (0, 10)]) == [(0, 10), (30, 40)]


# --------------------------------------------------------------------------- #
# FakeClient — before inclusive(함정3)·심볼 주입·달력 걷기까지 실서버 규약 재현
# --------------------------------------------------------------------------- #
class FakeBackfillClient:
    def __init__(self, trading_days: list[str], daily: dict[str, list[Candle]],
                 minute: dict[str, list[Candle]]):
        self.trading_days = trading_days
        self.daily = {s: sorted(rows, key=lambda c: c.ts_ms) for s, rows in daily.items()}
        self.minute = {s: sorted(rows, key=lambda c: c.ts_ms) for s, rows in minute.items()}
        self.calls = {"1d": 0, "1m": 0, "calendar": 0}
        self.adjusted_seen: dict[str, set] = {"1d": set(), "1m": set()}

    async def get_candles(self, symbol, interval, count=200, before_ms=None,
                          adjusted=True):
        self.calls[interval] += 1
        self.adjusted_seen[interval].add(adjusted)
        rows = (self.daily if interval == "1d" else self.minute).get(symbol, [])
        if before_ms is not None:
            rows = [c for c in rows if c.ts_ms <= before_ms]     # inclusive (함정3)
        page = rows[-count:]
        if not page:
            return CandlePage(candles=[], next_before_ms=None)
        oldest = page[0].ts_ms
        more = any(c.ts_ms < oldest for c in rows)
        return CandlePage(candles=list(page),
                          next_before_ms=oldest if more else None)

    async def get_us_calendar(self, date=None):
        self.calls["calendar"] += 1
        ds = self.trading_days
        anchor = ds[-1] if date is None else max((x for x in ds if x <= date),
                                                 default=ds[0])
        i = ds.index(anchor)
        return {"previous": market_day(ds[max(0, i - 1)]),
                "today": market_day(anchor),
                "next": market_day(ds[min(len(ds) - 1, i + 1)])}


TRAIN_DAYS = weekdays("2026-01-05", "2026-03-06")
HOLDOUT_DAYS = weekdays("2026-05-04", "2026-07-10")
ALL_DAYS = TRAIN_DAYS + HOLDOUT_DAYS
SPIKE_D = "2026-02-20"                                       # 훈련 구간
HOLDOUT_D = "2026-06-15"                                     # 홀드아웃 구간


def _spike_daily(symbol: str, days: list[str], spike: str) -> list[Candle]:
    out = []
    prev = 1_000_000
    for d in days:
        if d == spike:
            out.append(daily_bar(symbol, d, close=int(prev * 1.30),
                                 high=int(prev * 1.31), low=int(prev * 1.29),
                                 open_=int(prev * 1.295)))
        else:
            out.append(daily_bar(symbol, d, close=prev))
    return out


def _minutes(symbol: str, date: str, n: int, *, start_min: int = 0,
             step_min: int = 2) -> list[Candle]:
    """무체결 분이 섞인(2분 간격) 실봉 — 함정5: 빈 분은 봉 자체가 없다."""
    base = utc_ms(date)
    return [Candle(symbol=symbol, ts_ms=base + (start_min + i * step_min) * MIN_MS,
                   open_u=1_000_000, high_u=1_001_000, low_u=999_000,
                   close_u=1_000_000, vol_qu=1_000_000) for i in range(n)]


def _build_run(tmp_path, *, symbols=("SPKY",), minute=None):
    daily = {"SPKY": _spike_daily("SPKY", TRAIN_DAYS, SPIKE_D),
             "HLDO": _spike_daily("HLDO", HOLDOUT_DAYS, HOLDOUT_D)}
    if minute is None:
        window_start_day = TRAIN_DAYS[TRAIN_DAYS.index(SPIKE_D) - 25]
        minute = {"SPKY":
                  _minutes("SPKY", window_start_day, 1, start_min=0)      # 창 시작 봉
                  + _minutes("SPKY", TRAIN_DAYS[TRAIN_DAYS.index(SPIKE_D) - 1], 210)
                  + _minutes("SPKY", SPIKE_D, 240)}
    client = FakeBackfillClient(ALL_DAYS, daily, minute)
    cfg = make_config(tmp_path)
    store = Store(tmp_path / "bf.db")
    checkpoint = BF.Checkpoint(tmp_path / "out" / "checkpoint.json")
    runner = BF.Runner(cfg=cfg, client=client, store=store, checkpoint=checkpoint,
                       calendar=BF.TradingCalendar(), out_dir=tmp_path / "out")
    return runner, client, store


def test_full_run_screens_backfills_and_writes_an_honest_manifest(tmp_path):
    runner, client, store = _build_run(tmp_path)
    try:
        manifest = asyncio.run(runner.run(["SPKY"]))
        # 스크린: 정확히 스파이크 하루, 사유는 ret 뿐
        cands = manifest["symbols"]["SPKY"]["candidate_days"]
        assert [c["date"] for c in cands] == [SPIKE_D]
        assert cands[0]["reasons"] == ["ret"]
        assert manifest["screen_reason_counts_train_val"]["ret"] == 1
        # 원주가 강제 — 실제 호출 파라미터와 매니페스트 증거가 일치한다 (§7-e ①)
        assert client.adjusted_seen["1m"] == {False}
        assert client.adjusted_seen["1d"] == {True}
        win = manifest["symbols"]["SPKY"]["windows"][0]
        assert win["adjusted"] == "false"
        assert manifest["call_params"]["candles_1m"]["adjusted"] == "false"
        # 창 시작(D-25 매매일)까지 실제로 닿았다
        assert win["status"] == "done"
        # 커버리지 = 실봉 수 (무체결 분을 채워 세지 않는다) + DB 와 일치 (경계 중복 없음)
        db_bars = store._conn.execute(
            "SELECT COUNT(*) FROM candles_1m WHERE symbol='SPKY'").fetchone()[0]
        assert db_bars == 1 + 210 + 240
        assert win["bars_in_window"] == db_bars
        # 실보관 깊이는 프로브 값이다
        assert manifest["symbols"]["SPKY"]["probed_oldest_1m_utc"] is not None
    finally:
        store.close()


def test_rerun_is_idempotent_and_makes_zero_new_calls(tmp_path):
    runner, client, store = _build_run(tmp_path)
    try:
        asyncio.run(runner.run(["SPKY"]))
        calls_before = dict(client.calls)
        bars_before = store._conn.execute(
            "SELECT COUNT(*) FROM candles_1m").fetchone()[0]

        runner2, client2, store2 = _build_run(tmp_path)      # 같은 체크포인트 경로
        store2.close()                                        # 같은 DB 를 다시 연다
        runner2.store = None
        manifest2 = asyncio.run(runner2.run(["SPKY"]))
        assert client2.calls["1d"] == 0                       # 스크린 재호출 없음
        assert client2.calls["1m"] == 0                       # 창 재호출 없음
        assert manifest2["symbols"]["SPKY"]["windows"][0]["status"] == "done"
        bars_after = store._conn.execute(
            "SELECT COUNT(*) FROM candles_1m").fetchone()[0]
        assert bars_after == bars_before
    finally:
        store.close()


def test_retention_shorter_than_window_is_probed_and_reported_not_fatal(tmp_path):
    """소형주 실보관(320일)형: 창 시작에 못 닿으면 partial + 사유, 깊이는 프로브 기록."""
    minute = {"SPKY": _minutes("SPKY", SPIKE_D, 240)}         # 스파이크 당일치만 존재
    runner, client, store = _build_run(tmp_path, minute=minute)
    try:
        manifest = asyncio.run(runner.run(["SPKY"]))
        win = manifest["symbols"]["SPKY"]["windows"][0]
        assert win["status"] == "partial"
        assert win["skip_reason"] == "retention_exhausted"
        assert manifest["symbols"]["SPKY"]["retention_exhausted_1m"] is True
        oldest = manifest["symbols"]["SPKY"]["probed_oldest_1m_utc"]
        assert oldest is not None and oldest.startswith(SPIKE_D)
        assert manifest["failures"].get("retention_exhausted") == 1
    finally:
        store.close()


def test_holdout_candidates_expose_coverage_only(tmp_path):
    """§6.2: 홀드아웃 (심볼,일)의 판정 근거·수치는 매니페스트 어디에도 없다."""
    runner, client, store = _build_run(tmp_path)
    try:
        manifest = asyncio.run(runner.run(["SPKY", "HLDO"]))
        hldo = manifest["symbols"]["HLDO"]["candidate_days"]
        assert [c["date"] for c in hldo] == [HOLDOUT_D]
        assert set(hldo[0]) == {"date", "withheld"}           # 날짜 외 아무것도 없다
        assert hldo[0]["withheld"] == "holdout"
        # 근거 카운트는 훈련·검증 구간만 — 홀드아웃 건은 별도 일수로만 센다
        assert manifest["screen_reason_counts_train_val"]["ret"] == 1   # SPKY 뿐
        assert manifest["holdout_candidate_days"] == 1
        # 커버리지(창 날짜·봉 수)는 허용 — 창은 존재한다
        assert manifest["symbols"]["HLDO"]["windows"]
    finally:
        store.close()


def test_adjusted_tampering_refuses_to_run(tmp_path, monkeypatch):
    """§7-e: 1분봉을 수정주가로 바꾸면 실행 자체가 거부된다."""
    runner, client, store = _build_run(tmp_path)
    try:
        monkeypatch.setattr(BF, "BACKFILL_1M_ADJUSTED", True)
        with pytest.raises(RuntimeError, match="prereg 7-e"):
            asyncio.run(runner.run(["SPKY"]))
        assert client.calls["1m"] == 0                        # 한 호출도 나가지 않았다
    finally:
        store.close()


def test_screen_only_plans_windows_without_any_1m_call(tmp_path):
    """2단계 실행 절차: screen-only 뒤의 --estimate 는 정확한 창으로 계산돼야 한다."""
    runner, client, store = _build_run(tmp_path)
    try:
        manifest = asyncio.run(runner.run(["SPKY"], screen_only=True))
        assert client.calls["1m"] == 0                        # 1분봉 호출 0
        assert client.calls["1d"] >= 1                        # 스크린은 돌았다
        win = manifest["symbols"]["SPKY"]["windows"][0]
        assert win["status"] == "todo" and win["bars_in_window"] == 0
        est = BF.estimate(runner.cfg, runner.checkpoint, runner.calendar, 1)
        assert "checkpoint windows=1" in est["basis"]         # 가정이 아니라 실측 창

        # 이어서 본실행 — 스크린 재호출 없이 창만 채운다. 백필은 일봉 활동일을 읽어
        # 앵커를 잡으므로 store 를 열어 둔다 (candles_1d 읽기 + candles_1m 쓰기).
        store.close()
        runner2, client2, store2 = _build_run(tmp_path)
        try:
            manifest2 = asyncio.run(runner2.run(["SPKY"]))
            assert client2.calls["1d"] == 0
            assert client2.calls["1m"] >= 1
            assert manifest2["symbols"]["SPKY"]["windows"][0]["status"] == "done"
        finally:
            store2.close()
    finally:
        pass


def test_date_bound_prunes_planned_windows_but_keeps_screen_record(tmp_path):
    """라이브 운영 발견: 일봉 보관이 2001년까지 닿아 무경계 창이 수십만 일이 된다.

    --from 은 **창 계획**에만 적용된다 — 스크린 결과(§2.8 표본 프레임)는 그대로 남고,
    캐시된 스크린 위에 재실행해도 경계 밖 창은 계획·호출에서 빠지며, 이미 등록된
    미착수(todo) 창은 정리된다.
    """
    runner, client, store = _build_run(tmp_path)
    try:
        asyncio.run(runner.run(["SPKY"], screen_only=True))     # 무경계 창 등록 (todo)
        assert len(runner.checkpoint.data["windows"]) == 1

        runner2, client2, store2 = _build_run(tmp_path)
        store2.close()
        runner2.store = None
        runner2.date_from = "2026-03-01"                        # SPIKE_D(02-20) 를 배제
        manifest = asyncio.run(runner2.run(["SPKY"]))
        assert client2.calls["1m"] == 0                         # 경계 밖 — 호출 없음
        assert runner2.checkpoint.data["windows"] == {}         # 미착수 창 정리됨
        cands = manifest["symbols"]["SPKY"]["candidate_days"]
        assert [c["date"] for c in cands] == [SPIKE_D]          # 스크린 기록은 보존
    finally:
        store.close()


def test_estimate_uses_checkpoint_and_needs_no_client(tmp_path):
    runner, client, store = _build_run(tmp_path)
    try:
        asyncio.run(runner.run(["SPKY"]))
        est = BF.estimate(runner.cfg, runner.checkpoint, runner.calendar, 1)
        assert est["screen_done_symbols"] == 1
        assert est["screen_calls"] >= 1                       # 실측 페이지 수 반영
        assert "checkpoint windows=1" in est["basis"]
        for sc in est["scenarios"].values():
            assert sc["total_calls"] > 0 and sc["est_hours"] >= 0
        # 유니버스가 더 크면 남은 심볼 몫이 가정으로 계상된다
        est2 = BF.estimate(runner.cfg, runner.checkpoint, runner.calendar, 100,
                           assume_span_days=550)
        assert est2["screen_calls"] == est["screen_calls"] + 99 * math.ceil(550 / 200)
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# 앵커 결함 회귀 (라이브 진단 A-1): 한정-깊이 API + 무체결 갭 -> 창 중간 결측
# --------------------------------------------------------------------------- #
def _bar_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


class DepthLimitedClient:
    """실서버 1분봉 API 의 **앵커별 한정 깊이**를 재현한다 (진단 근거).

    `before` 앵커에서 데이터 보유일의 **연속 구간**(무체결 갭 > max_gap 매매일 전에서
    끊김)만 서빙하고, 그 아래에 더 오래된 데이터가 있어도 next_before_ms=None 을 준다.
    창 끝에서 한 번만 후진하면 첫 갭에서 멈춘다 — AARD 형 결함. 각 활황일에 재앵커
    (수정)해야 전부 수집된다. 1일봉은 실서버처럼 전부 서빙한다.
    """

    def __init__(self, trading_days, daily, minute, max_gap=2):
        self.trading_days = list(trading_days)
        self.tindex = {d: i for i, d in enumerate(self.trading_days)}
        self.daily = {s: sorted(v, key=lambda c: c.ts_ms) for s, v in daily.items()}
        self.minute = {s: sorted(v, key=lambda c: c.ts_ms) for s, v in minute.items()}
        self.max_gap = max_gap
        self.calls = {"1d": 0, "1m": 0, "calendar": 0}
        self.adjusted_seen = {"1d": set(), "1m": set()}

    async def get_candles(self, symbol, interval, count=200, before_ms=None,
                          adjusted=True):
        self.calls[interval] += 1
        self.adjusted_seen[interval].add(adjusted)
        if interval == "1d":
            rows = self.daily.get(symbol, [])
            if before_ms is not None:
                rows = [c for c in rows if c.ts_ms <= before_ms]
            page = rows[-count:]
            if not page:
                return CandlePage(candles=[], next_before_ms=None)
            oldest = page[0].ts_ms
            return CandlePage(candles=list(page),
                              next_before_ms=oldest if any(c.ts_ms < oldest for c in rows)
                              else None)
        rows = self.minute.get(symbol, [])
        if before_ms is not None:
            rows = [c for c in rows if c.ts_ms <= before_ms]
        if not rows:
            return CandlePage(candles=[], next_before_ms=None)
        days_desc = sorted({_bar_date(c.ts_ms) for c in rows}, reverse=True)
        reach, prev = set(), None
        for d in days_desc:
            if prev is None or (self.tindex[prev] - self.tindex[d]) <= self.max_gap:
                reach.add(d); prev = d
            else:
                break                                    # 갭이 크면 API 는 여기서 끊는다
        block = [c for c in rows if _bar_date(c.ts_ms) in reach]
        page = block[-count:]
        oldest = page[0].ts_ms
        more = any(c.ts_ms < oldest for c in block)
        return CandlePage(candles=list(page), next_before_ms=oldest if more else None)

    async def get_us_calendar(self, date=None):
        self.calls["calendar"] += 1
        ds = self.trading_days
        anchor = ds[-1] if date is None else max((x for x in ds if x <= date),
                                                 default=ds[0])
        i = ds.index(anchor)
        return {"previous": market_day(ds[max(0, i - 1)]),
                "today": market_day(anchor),
                "next": market_day(ds[min(len(ds) - 1, i + 1)])}


AARD_DAYS = weekdays("2025-10-01", "2026-03-06")
AARD_EVENTS = ["2025-12-01", "2025-12-11", "2025-12-23", "2026-01-08",
               "2026-01-21", "2026-02-03"]              # ~8-9 매매일 간격 -> 창 병합


def _aard_daily():
    prev = 1_000_000
    out = []
    for d in AARD_DAYS:
        if d in AARD_EVENTS:
            out.append(daily_bar("AARD", d, close=int(prev * 1.30),
                                  high=int(prev * 1.31), low=int(prev * 1.29),
                                  open_=int(prev * 1.295)))
        else:
            out.append(daily_bar("AARD", d, close=prev))
    return out


def _aard_minute():
    """1분봉은 활황일(이벤트일)에만 60봉씩, 그 사이는 진짜 무체결(갭)."""
    rows = []
    for d in AARD_EVENTS:
        rows += _minutes("AARD", d, 60, start_min=810)   # 정규장 구간
    return rows


def _aard_run(tmp_path):
    client = DepthLimitedClient(AARD_DAYS, {"AARD": _aard_daily()},
                                {"AARD": _aard_minute()}, max_gap=2)
    cfg = make_config(tmp_path)
    store = Store(tmp_path / "bf.db")
    ck = BF.Checkpoint(tmp_path / "out" / "checkpoint.json")
    runner = BF.Runner(cfg=cfg, client=client, store=store, checkpoint=ck,
                       calendar=BF.TradingCalendar(), out_dir=tmp_path / "out")
    return runner, client, store


def test_depthlimited_client_reproduces_the_gap_refusal(tmp_path):
    """대조: 창 끝에서 한 번 후진하면 최신 이벤트일만 오고 next_before=None 이다."""
    client = DepthLimitedClient(AARD_DAYS, {"AARD": _aard_daily()},
                                {"AARD": _aard_minute()}, max_gap=2)
    end = utc_ms("2026-02-05") + 1430 * MIN_MS
    page = asyncio.run(client.get_candles("AARD", "1m", count=200, before_ms=end))
    got_days = {_bar_date(c.ts_ms) for c in page.candles}
    assert got_days == {"2026-02-03"}                    # 최신 이벤트일만
    assert page.next_before_ms is None                   # 갭 아래는 안 준다(옛 코드가 멈추던 지점)


def test_anchor_fix_collects_every_event_day_across_gaps(tmp_path):
    """수정 회귀: 후보일 앵커 + 갭 넘김으로 병합 창의 **모든** 이벤트일을 수집한다.

    수정 전(창 끝 단일 앵커)에는 최신 이벤트일만 수집되고 88.6% 창이 25% 미만이었다.
    """
    runner, client, store = _aard_run(tmp_path)
    try:
        manifest = asyncio.run(runner.run(["AARD"]))
        # 이벤트일이 스크린 후보로 잡혔다
        cands = [c["date"] for c in manifest["symbols"]["AARD"]["candidate_days"]]
        assert set(AARD_EVENTS) <= set(cands)
        # 창이 병합돼 소수의 광역 창이 됐는데도 모든 이벤트일이 DB 에 있다
        for d in AARD_EVENTS:
            lo = utc_ms(d); hi = lo + 1430 * MIN_MS
            n = store._conn.execute(
                "SELECT COUNT(*) FROM candles_1m WHERE symbol='AARD' "
                "AND ts_ms>=? AND ts_ms<?", (lo, hi)).fetchone()[0]
            assert n == 60, f"{d} collected {n}/60 bars (gap-crossing failed)"
        total = store._conn.execute(
            "SELECT COUNT(*) FROM candles_1m WHERE symbol='AARD'").fetchone()[0]
        assert total == 60 * len(AARD_EVENTS)            # 6 이벤트일 x 60 = 360
        # 효율: 휴면일을 프로브하지 않는다 — 호출은 이벤트일 수 규모 (수십 매매일 아님)
        assert client.calls["1m"] <= 3 * len(AARD_EVENTS) + 3
    finally:
        store.close()


def test_anchor_fix_is_idempotent_on_rerun(tmp_path):
    """재실행이 봉을 중복·유실하지 않는다 (upsert 멱등, 기존 자산 보존).

    AARD 창은 1분봉이 창 시작(D-25)까지 없어 partial(retention) 이므로 재실행이
    재시도한다 — 정상. 보장하는 것은 **봉 수 불변**이다(중복 저장 없음).
    """
    runner, client, store = _aard_run(tmp_path)
    try:
        asyncio.run(runner.run(["AARD"]))
        bars1 = store._conn.execute(
            "SELECT COUNT(*) FROM candles_1m WHERE symbol='AARD'").fetchone()[0]
        store.close()

        runner2, client2, store2 = _aard_run(tmp_path)
        asyncio.run(runner2.run(["AARD"]))
        bars2 = store2._conn.execute(
            "SELECT COUNT(*) FROM candles_1m WHERE symbol='AARD'").fetchone()[0]
        assert bars2 == bars1                            # 봉 수 불변 (중복 없음)
        store2.close()
    finally:
        pass


def test_redrive_repages_done_windows_but_stays_idempotent(tmp_path):
    """--redrive: 옛 코드가 done 으로 남긴 창을 새 앵커 로직으로 다시 받되 봉은 그대로."""
    runner, client, store = _aard_run(tmp_path)
    try:
        asyncio.run(runner.run(["AARD"]))
        bars1 = store._conn.execute(
            "SELECT COUNT(*) FROM candles_1m WHERE symbol='AARD'").fetchone()[0]
    finally:
        store.close()

    runner2, client2, store2 = _aard_run(tmp_path)
    runner2.redrive = True
    try:
        asyncio.run(runner2.run(["AARD"]))
        assert client2.calls["1m"] > 0                   # 재구동 -> 다시 페이징
        bars2 = store2._conn.execute(
            "SELECT COUNT(*) FROM candles_1m WHERE symbol='AARD'").fetchone()[0]
        assert bars2 == bars1                            # upsert 멱등 -> 봉 수 불변
        for d in AARD_EVENTS:                            # 여전히 전 이벤트일 완비
            lo = utc_ms(d); hi = lo + 1430 * MIN_MS
            n = store2._conn.execute(
                "SELECT COUNT(*) FROM candles_1m WHERE symbol='AARD' "
                "AND ts_ms>=? AND ts_ms<?", (lo, hi)).fetchone()[0]
            assert n == 60
    finally:
        store2.close()

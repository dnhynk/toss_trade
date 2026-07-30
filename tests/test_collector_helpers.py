"""W4 컬렉터 테스트 공용 헬퍼 (테스트 케이스가 아니라 지원 모듈).

파일명이 `test_collector*` 인 것은 소유 경로 규칙(`tests/test_collector*.py`) 때문이다.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import socket
import threading
import time
from pathlib import Path

import pandas as pd

from tossmon.api.models import (Candle, MICRO, Orderbook, OrderbookLevel, Price,
                                RankingPage, RankingRow, SessionWindow, UsMarketDay)
from tossmon.collector.scheduler import Clock
from tossmon.config import parse_config

MIN_MS = 60_000
REPO_ROOT = Path(__file__).resolve().parent.parent

#: config.example.yaml 와 같은 키 구성 (실 파일을 읽지 않고 테스트에서 조립한다).
BASE_CONFIG: dict = {
    "api": {"base_url": "http://127.0.0.1:8899", "live": False, "keys_path": "api_keys",
            "token_state_path": "data/token_state.json", "timeout_s": 10.0,
            "usage_ratio": 0.7},
    "limits": {"AUTH": 5, "STOCK": 5, "MARKET_DATA": 10, "MARKET_DATA_CHART": 5,
               "RANKING": 5, "MARKET_INFO": 3},
    "store": {"db_path": "data/tossmon.db", "archive_dir": "data/archive"},
    "universe": {"price_min_usd": "0.10", "price_max_usd": "20.00",
                 "mcap_min_usd": "10000000", "mcap_max_usd": "300000000",
                 "tier1_max": 1500, "tier2_max": 300, "tier3_max": 20},
    "detector": {"event_window_min": 30, "event_ret_min": 0.15, "event_day_ret_min": 0.30,
                 "event_rvol_min": 3.0, "promote_hysteresis_s": 120},
    "polling": {"tier1_sweep_s": 45, "tier2_candle_s": 90, "tier3_trades_s": 4,
                "tier3_orderbook_s": 16, "ranking_snap_s": 12},
}


def config_dict(**sections) -> dict:
    """BASE_CONFIG 를 섹션 단위로 얕게 덮어쓴 dict."""
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in BASE_CONFIG.items()}
    for name, values in sections.items():
        if values is None:
            out.pop(name, None)
        else:
            out.setdefault(name, {})
            out[name] = {**out[name], **values} if isinstance(values, dict) else values
    return out


def make_config(tmp_path: Path | None = None, **sections):
    """테스트용 Config. ambient env 가 새어들지 않도록 env 는 비워서 파싱한다."""
    data = config_dict(**sections)
    if tmp_path is not None:
        data["store"] = {**data["store"], "db_path": str(tmp_path / "tossmon.db"),
                         "archive_dir": str(tmp_path / "archive")}
    return parse_config(data, env={})


# --------------------------------------------------------------------------- #
# 시계
# --------------------------------------------------------------------------- #
class FrozenClock(Clock):
    """수동으로 밀어주는 시계 — 결정론적 단위테스트용."""

    def __init__(self, now_ms: int, **kw):
        kw.setdefault("sync", False)
        super().__init__(**kw)
        self._now = int(now_ms)
        self.slept: list[float] = []

    def local_now_ms(self) -> int:
        return self._now

    def advance(self, seconds: float) -> int:
        self._now += int(seconds * 1000)
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(float(seconds))
        self.advance(seconds)
        await asyncio.sleep(0)             # 다른 task 에 양보


class VirtualClock(Clock):
    """실시간을 `scale` 배로 가속하는 시계 — 가속 리플레이 통합테스트용.

    1 초를 자면 실제로는 `1/scale` 초만 잔다. 세션 1개(수 시간)를 수십 초로 압축한다.
    """

    def __init__(self, start_ms: int, scale: float = 100.0, **kw):
        kw.setdefault("sync", False)       # 서버 Date 헤더로 가상시각을 오염시키지 않는다
        super().__init__(**kw)
        self.start_ms = int(start_ms)
        self.scale = float(scale)
        self._t0 = time.monotonic()

    def local_now_ms(self) -> int:
        return self.start_ms + int((time.monotonic() - self._t0) * 1000 * self.scale)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(float(seconds), 0.0) / self.scale)


# --------------------------------------------------------------------------- #
# 캘린더
# --------------------------------------------------------------------------- #
def calendar_dict(days: list[UsMarketDay], index: int) -> dict[str, UsMarketDay]:
    """synth 캘린더 → `/market-calendar/US` 응답 모양 (previous/today/next)."""
    out = {"today": days[index]}
    if index > 0:
        out["previous"] = days[index - 1]
    if index + 1 < len(days):
        out["next"] = days[index + 1]
    return out


def simple_day(date: str, base_ms: int) -> UsMarketDay:
    """세션 4개가 붙어 있는 하루 (경계 공유 — 반열림 판정 검증용)."""
    return UsMarketDay(
        date=date,
        day=SessionWindow(start_ms=base_ms, end_ms=base_ms + 480 * MIN_MS),
        pre=SessionWindow(start_ms=base_ms + 480 * MIN_MS, end_ms=base_ms + 810 * MIN_MS),
        regular=SessionWindow(start_ms=base_ms + 810 * MIN_MS,
                              end_ms=base_ms + 1200 * MIN_MS),
        after=SessionWindow(start_ms=base_ms + 1200 * MIN_MS,
                            end_ms=base_ms + 1430 * MIN_MS),
    )


# --------------------------------------------------------------------------- #
# 리플레이 클라이언트 (HTTP 없음 — synth 데이터를 시각 기준으로 되돌려준다)
# --------------------------------------------------------------------------- #
class ReplayClient:
    """`TossClient` 표면을 흉내내는 리플레이어.

    실측 함정을 **의도적으로 재현**한다:
      * `before` 는 inclusive (함정3)
      * 아직 봉이 없는 심볼은 `timestamp=None` (함정2 / A2 §3 first_print)
      * 모르는 심볼은 404 가 아니라 응답에서 조용히 누락 (함정1)
      * 완성된 봉만 보인다 (진행 중인 분은 아직 없음)
    """

    def __init__(self, clock, frames: dict[str, pd.DataFrame], truths: dict[str, dict],
                 calendar: list[UsMarketDay], *, rankings: pd.DataFrame | None = None):
        self.clock = clock
        self.frames = {s: df.sort_values("ts_ms").reset_index(drop=True)
                       for s, df in frames.items()}
        self.truths = truths
        self.calendar = calendar
        self.rankings = rankings
        self.counters = {"requests": 0, "http_429": 0, "retries": 0}
        self.last_headers: dict[str, str] = {}
        self.calls: dict[str, int] = {}

    # ---- 내부 ----------------------------------------------------------

    def _note(self, what: str) -> None:
        self.calls[what] = self.calls.get(what, 0) + 1
        self.counters["requests"] += 1

    def _visible(self, symbol: str) -> pd.DataFrame:
        df = self.frames.get(symbol)
        if df is None:
            return pd.DataFrame()
        now = self.clock.now_ms()
        return df[df["ts_ms"] + MIN_MS <= now]          # 완성봉만

    @staticmethod
    def _to_candles(symbol: str, df: pd.DataFrame) -> list[Candle]:
        return [Candle(symbol=symbol, ts_ms=int(r.ts_ms), open_u=int(r.open_u),
                       high_u=int(r.high_u), low_u=int(r.low_u), close_u=int(r.close_u),
                       vol_qu=int(r.vol_qu)) for r in df.itertuples()]

    # ---- TossClient 표면 -------------------------------------------------

    async def get_prices(self, symbols):
        self._note("prices")
        out = []
        for sym in symbols:
            if sym not in self.frames:
                continue                                # 함정1: 조용한 누락
            vis = self._visible(sym)
            if vis.empty:
                first = self.frames[sym].iloc[0]
                out.append(Price(symbol=sym, ts_ms=None, last_u=int(first.open_u)))
            else:
                last = vis.iloc[-1]
                out.append(Price(symbol=sym, ts_ms=int(last.ts_ms),
                                 last_u=int(last.close_u)))
        return out

    async def get_candles(self, symbol, interval, count=200, before_ms=None,
                          adjusted=True):
        from tossmon.api.models import CandlePage

        self._note(f"candles_{interval}")
        if interval == "1d":
            df = self.truths.get(symbol, {}).get("df_1d")
            if df is None or df.empty:
                return CandlePage(candles=[], next_before_ms=None)
            sub = df.tail(count)
            return CandlePage(candles=self._to_candles(symbol, sub), next_before_ms=None)

        vis = self._visible(symbol)
        if before_ms is not None:
            vis = vis[vis["ts_ms"] <= int(before_ms)]   # 함정3: inclusive
        if vis.empty:
            return CandlePage(candles=[], next_before_ms=None)
        page = vis.tail(count)
        oldest = int(page["ts_ms"].to_numpy()[0])
        more = bool((vis["ts_ms"] < oldest).any())
        return CandlePage(candles=self._to_candles(symbol, page),
                          next_before_ms=oldest if more else None)

    async def get_trades(self, symbol, count=50):
        from tossmon.api.models import Trade

        self._note("trades")
        vis = self._visible(symbol)
        if vis.empty:
            return []
        out = []
        for row in vis.tail(3).itertuples():           # 최근 3분에서 표본 추출
            n = 4
            qty = max(int(row.vol_qu) // n, 1)
            for i in range(n):
                out.append(Trade(symbol=symbol, ts_ms=int(row.ts_ms) + i * 15_000,
                                 price_u=int(row.close_u), qty_u=qty))
        return sorted(out, key=lambda t: t.ts_ms)[-count:]

    async def get_orderbook(self, symbol):
        self._note("orderbook")
        vis = self._visible(symbol)
        if vis.empty:
            return Orderbook(symbol=symbol, ts_ms=None, bids=[], asks=[])
        last = vis.iloc[-1]
        mid = int(last.close_u)
        # A2 §1: 미국 호가는 최우선 1레벨뿐이다.
        return Orderbook(symbol=symbol, ts_ms=int(last.ts_ms),
                         bids=[OrderbookLevel(price_u=mid - 1000, qty_u=100 * MICRO)],
                         asks=[OrderbookLevel(price_u=mid + 1000, qty_u=90 * MICRO)])

    async def get_rankings(self, ranking_type, duration="realtime", market="US",
                           count=100, exclude_caution=False):
        self._note("rankings")
        now = self.clock.now_ms()
        rows: list[RankingRow] = []
        snap_ms = None
        if self.rankings is not None and not self.rankings.empty:
            sub = self.rankings[(self.rankings["snap_ms"] <= now)
                                & (self.rankings["ranking_type"] == ranking_type)]
            if not sub.empty:
                snap_ms = int(sub["snap_ms"].max())
                sub = sub[sub["snap_ms"] == snap_ms].sort_values("rank").head(count)
                rows = [RankingRow(rank=int(r.rank), symbol=str(r.symbol),
                                   last_u=int(r.last_u), base_u=int(r.last_u),
                                   change_rate=None, vol_qu=int(r.vol_qu),
                                   amount_u=int(r.amount_u)) for r in sub.itertuples()]
        return RankingPage(ranking_type=ranking_type, duration=duration,
                           ranked_at_ms=snap_ms, rows=rows)

    async def get_us_calendar(self, date=None):
        self._note("calendar")
        idx = 0
        if date is None:
            now = self.clock.now_ms()
            for i, md in enumerate(self.calendar):
                wins = [w for w in (md.day, md.pre, md.regular, md.after) if w]
                if wins and wins[0].start_ms <= now < wins[-1].end_ms:
                    idx = i
                    break
            else:
                idx = min(len(self.calendar) - 1, max(0, len(self.calendar) // 2))
        else:
            idx = next((i for i, md in enumerate(self.calendar) if md.date == date), 0)
        return calendar_dict(self.calendar, idx)

    async def aclose(self):
        return None


# --------------------------------------------------------------------------- #
# mock 서버 (tools/mock_server.py 를 in-process 로 띄운다)
# --------------------------------------------------------------------------- #
def _load_mock_module():
    """tools/ 는 패키지가 아니므로 파일 경로로 로드한다 (tools/ 를 건드리지 않는다)."""
    spec = importlib.util.spec_from_file_location(
        "tossmon_mock_server", REPO_ROOT / "tools" / "mock_server.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _MockArgs:
    def __init__(self, **kw):
        self.inject = kw.get("inject")
        self.inject_every = kw.get("inject_every", 1)
        self.inject_latency_s = kw.get("inject_latency_s", 12.0)
        self.enforce_limits = kw.get("enforce_limits", False)
        self.strict = kw.get("strict", False)
        self.verbose = False


@contextlib.contextmanager
def mock_server(**kw):
    """(base_url, httpd) 를 주는 컨텍스트 매니저."""
    module = _load_mock_module()
    port = _free_port()
    httpd = module.build_server(port, _MockArgs(**kw))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def build_client(base_url: str, tmp_path: Path, *, usage_ratio: float = 0.7):
    """mock 전용 TossClient (계약 C-9: live=False → 고정 토큰, 발급 시도 없음)."""
    from tossmon.api.client import TossClient
    from tossmon.api.limiter import GroupRateLimiter
    from tossmon.api.tokens import TokenManager

    tokens = TokenManager(tmp_path / "api_keys_absent", tmp_path / "token_state.json",
                          live=False)
    limiter = GroupRateLimiter(dict(BASE_CONFIG["limits"]), usage_ratio)
    return TossClient(base_url, tokens, limiter, timeout_s=5.0)

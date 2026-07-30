"""수집 루프 — 계약 C-8. 소유: W4. asyncio 단일 프로세스, 루프별 독립 task.

우선순위 (docs/03 §6 계획 변경)
-----------------------------
1. **랭킹 스냅샷** — 과거 조회가 불가능한 유일한 데이터. 무슨 일이 있어도 이 루프는 돈다.
2. **tier3 테이프(`/trades`)** — 사후 복원 불가. A2 §1 대로 3~5s 로 조밀하게.
3. **tier1 가격 스윕** — `first_print`/`staleness` 로 "깨어나는 동전주" 를 잡는다 (A2 §3).
4. **tier3 호가** — 1레벨뿐이라 정보량이 적다. 15~20s.
5. **tier2 1분봉** — 1024일 백필로 대체 가능하므로 **가장 낮은 우선순위**. 실시간 폴링은
   승격 판단에 필요한 최소한만 하고, 이력은 DB(백필 결과)에서 읽어 쓴다.

실측이 확인한 함정 대응 (docs/06)
-------------------------------
* 함정1 미존재 심볼은 404 가 아니라 **200 + 조용한 누락**  → 요청/응답 심볼 대조 (`_sweep_chunk`)
* 함정2 체결이 없어도 `lastPrice` 는 온다              → `last_u` 를 현재가로 쓰지 않는다
* 함정3 `before` 는 inclusive                        → `next_before_ms - 1` 로 경계봉 중복 제거
* 함정4 당일 일봉은 진행형                            → `exclude_today_1d_cutoff` 로 잘라내고 베이스라인
* 함정5 체결 없는 분은 봉 자체가 없다                  → 0거래량으로 채우는 것은 analysis 계층이 한다
                                                      (`_minute_volumes`). 저장은 받은 대로만 한다.
* 함정6 `/trades` 에 symbol 필드 없음                 → client 가 주입 (W1 구현 완료)
* 수정주가  1분봉=원주가 / 일봉=수정주가 (계약 A5)     → `candle_adjusted()` 단일 출처

견고성
-----
* 모든 루프 몸통은 `_guarded` 를 지난다. `Forbidden` 만 치명(전체 정지), 나머지는 로그+계속.
* 재시작 이어받기: 티어/워치리스트/마지막 수집 지점을 상태파일에 원자적으로 저장하고 복원한다.
* 메모리: 심볼별 봉 버퍼·랭킹 버퍼 모두 상한이 있고, 강등된 심볼의 상태는 즉시 버린다.
* int64 오버플로: 거래대금 누적은 **Python int** 로만 한다 (`tape_stats`). pandas 정수 누적은
  조용히 랩어라운드한다.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

from ..analysis.baselines import compute_daily_baseline
from ..analysis.labeling import EventParams
from ..api.client import BATCH_MAX, TossClient
from ..api.errors import Forbidden, SchemaMismatch, TossApiError
from ..api.models import Candle, Price, RankingPage, precision_stats
from ..config import Config
from ..store.writer import Store
from .budget import GROUP_CHART, GROUP_MARKET_DATA, GROUP_RANKING, BudgetGuard, TierPlan
from .detector import (EventDetector, PriceActivityTracker, TierChange, TierStateMachine,
                       activity_score, build_curve)
from .notifier import Notifier
from .scheduler import (CLOSED, Clock, SessionScheduler, exclude_today_1d_cutoff,
                        session_window)

MIN_MS = 60_000
DAY_MS = 86_400_000

#: 랭킹 4종 (docs/03 §1: 전 티어 승격 트리거 겸 토스 쏠림도 시계열).
RANKING_TYPES: tuple[str, ...] = (
    "MARKET_TRADING_AMOUNT", "MARKET_TRADING_VOLUME",
    "TOSS_SECURITIES_TRADING_AMOUNT", "TOSS_SECURITIES_TRADING_VOLUME",
)
#: 토스 랭킹 이 순위 안에 새로 들어오면 그 자체로 tier2 승격 트리거.
RANKING_PROMOTE_TOP = 10
#: 랭킹 버퍼 보존 기간·심볼당 행 상한 (실시간 피처용. 영구 기록은 DB 가 한다).
RANKING_KEEP_MS = 3 * 3600 * 1000
RANKING_ROWS_PER_SYMBOL = 720

CANDLE_PAGE = 200
TRADES_COUNT = 50

#: 계약 C-2 개정 A5 — **interval 별 수정주가 규약. 캔들 호출은 반드시 이 표를 참조한다.**
#:
#: 1분봉은 **원주가**(adjusted=False): 수정주가는 과거 가격을 분할비로 나눠 그 시점의
#: **명목 가격대를 지운다**. CRKN 2025-03-05 은 실제로 $1.99 에 거래됐는데 수정주가로는
#: $0.10 로 보인다(W5 라이브 실측, 분할비 ≈19). 이 프로젝트의 논지가 "동전주 가격대에서
#: 벌어지는 현상" 이므로 그 오분류는 치명적이고, sub-penny 호가·LULD 밴드($0.75/$3)
#: 같은 미국 제도도 전부 명목 가격 기준이다. 분할은 장중에 일어나지 않으므로 일중 상대
#: 계산(수익률·RVOL·VWAP 괴리)은 원주가로도 완전히 동일하다 — 잃는 것이 없다.
#:
#: 일봉은 **수정주가**(adjusted=True): 20일 베이스라인·ATR·former runner 탐지처럼
#: **분할을 가로지르는 다일 계산**은 연속성이 필요하다.
#:
#: ⚠️ 두 계열을 **가격 수준으로 직접 비교하지 마라** (A5 금지 조항). 다일 가격 비교는
#: 일봉으로만, 비율 비교는 각 계열 안에서만 한다.
CANDLE_ADJUSTED: dict[str, bool] = {"1m": False, "1d": True}

#: 승격 직후 API 백필 상한 (페이지). 이력은 원칙적으로 DB(백필 결과)에서 읽는다.
MAX_BACKFILL_PAGES = 3
#: 곡선/이력에 쓸 날 수. 곡선 분모는 **당일 제외** (자기오염 방지).
HISTORY_DAYS = 3
MAX_BARS_PER_SYMBOL = 4 * 1440
#: 곡선 재계산 최소 간격 (ms).
CURVE_TTL_MS = 3600_000

#: 세션이 닫혔을 때 루프가 도는 간격 (초).
IDLE_SLEEP_S = 5.0
#: 상태파일 저장 간격 (초).
STATE_SAVE_S = 60.0
#: 주기 텔레메트리 출력 간격 (초). 무인 실행이라 로그가 유일한 관측 창이다.
TELEMETRY_EVERY_S = 300.0
#: 이벤트 alert 를 낼 최대 검출 지연(분). 이보다 오래된 이벤트는 지금 행동할 대상이 아니라
#: 기록 대상이므로 info 로 내린다 — 재기동 직후 알림 폭주도 이 조건이 막는다.
EVENT_ALERT_MAX_LAG_MIN = 15
#: 마이크로 단위(1e-6)를 넘는 소수 자릿수 — 이보다 크면 반올림이 일어난다 (계약 A4).
MICRO_DIGITS = 6
#: 연속으로 이만큼 응답에서 빠지면 워치리스트에서 내린다 (함정1: 상장폐지·오타).
MISSING_STREAK_DROP = 5
#: tier1 활동 스코어가 이 이상이면 tier2 로 올린다.
ACTIVITY_PROMOTE_SCORE = 0.7
#: 상위 티어인데 이 시간 동안 새 데이터가 없으면 강등 (수집 실패·거래 정지).
STALE_DEMOTE_MULT = 6.0
#: 세션별 티어 정원 배율 — 얇은 세션에 감시 폭을 줄여 예산을 랭킹/테이프로 돌린다.
SESSION_TIER_SCALE: dict[str, float] = {
    "regular": 1.0, "pre": 0.8, "after": 0.5, "day": 0.4, CLOSED: 0.0}

CANDLE_COLS = ["symbol", "ts_ms", "open_u", "high_u", "low_u", "close_u", "vol_qu"]
RANKING_COLS = ["snap_ms", "ranking_type", "duration", "rank", "symbol",
                "last_u", "vol_qu", "amount_u"]



def candle_adjusted(interval: str) -> bool:
    """`interval` 에 맞는 `adjusted` 값 (계약 A5).

    호출부가 이 함수를 거치게 해서 "한쪽만 고치는" 실수를 구조적으로 막는다.
    `TossClient.get_candles` 의 기본값은 바꾸지 않는다 — 명시가 계약이다(A5 적용 절).
    """
    try:
        return CANDLE_ADJUSTED[interval]
    except KeyError:
        raise ValueError(
            f"unknown candle interval {interval!r} — 계약 A5 는 '1m'(원주가)과 "
            f"'1d'(수정주가)만 정의한다. 새 interval 은 계약 개정이 먼저다") from None


# --------------------------------------------------------------------------- #
# 버퍼
# --------------------------------------------------------------------------- #
def candles_frame(rows: Iterable[Candle]) -> pd.DataFrame:
    data = [{"symbol": c.symbol, "ts_ms": int(c.ts_ms), "open_u": int(c.open_u),
             "high_u": int(c.high_u), "low_u": int(c.low_u), "close_u": int(c.close_u),
             "vol_qu": int(c.vol_qu)} for c in rows]
    df = pd.DataFrame(data, columns=CANDLE_COLS)
    dtypes = {c: "int64" for c in CANDLE_COLS if c != "symbol"}
    if df.empty:
        return df.astype(dtypes)
    return df.sort_values("ts_ms").reset_index(drop=True).astype(dtypes)


class SymbolBuffer:
    """심볼별 1분봉 링버퍼. ts_ms 키라 중복 수신(함정3)은 자연히 흡수된다."""

    def __init__(self, symbol: str, max_bars: int = MAX_BARS_PER_SYMBOL) -> None:
        self.symbol = symbol
        self.max_bars = max_bars
        self.bars: dict[int, tuple[int, int, int, int, int]] = {}
        self._frame: pd.DataFrame | None = None

    def upsert(self, candles: Iterable[Candle]) -> int:
        """새로 들어온 **봉 수**(중복 제외)를 반환 — 카운팅이 틀어지지 않게 키로 센다."""
        new = 0
        for c in candles:
            key = int(c.ts_ms)
            if key not in self.bars:
                new += 1
            self.bars[key] = (int(c.open_u), int(c.high_u), int(c.low_u),
                              int(c.close_u), int(c.vol_qu))
        if new:
            self._frame = None
            self._trim()
        return new

    def _trim(self) -> None:
        if len(self.bars) <= self.max_bars:
            return
        for key in sorted(self.bars)[:len(self.bars) - self.max_bars]:
            self.bars.pop(key, None)

    def prune_before(self, ts_ms: int) -> int:
        drop = [k for k in self.bars if k < ts_ms]
        for key in drop:
            self.bars.pop(key, None)
        if drop:
            self._frame = None
        return len(drop)

    def last_ts(self) -> int | None:
        return max(self.bars) if self.bars else None

    def frame(self) -> pd.DataFrame:
        if self._frame is None:
            rows = [{"symbol": self.symbol, "ts_ms": ts, "open_u": o, "high_u": h,
                     "low_u": lo, "close_u": c, "vol_qu": v}
                    for ts, (o, h, lo, c, v) in sorted(self.bars.items())]
            df = pd.DataFrame(rows, columns=CANDLE_COLS)
            dtypes = {c: "int64" for c in CANDLE_COLS if c != "symbol"}
            self._frame = df.astype(dtypes) if not df.empty else df.astype(dtypes)
        return self._frame

    def __len__(self) -> int:
        return len(self.bars)


class RankingBuffer:
    """실시간 피처용 랭킹 버퍼 — **심볼별로만** 들고 있어 메모리가 선형으로 늘지 않는다.

    영구 기록은 `rankings_snap` 테이블이 한다. 여기는 `extract_precursor_features` 가
    토스 쏠림도를 계산할 최근 구간만 남긴다.
    """

    def __init__(self, keep_ms: int = RANKING_KEEP_MS,
                 rows_per_symbol: int = RANKING_ROWS_PER_SYMBOL) -> None:
        self.keep_ms = keep_ms
        self.rows_per_symbol = rows_per_symbol
        self.rows: dict[str, deque[dict]] = {}
        self.last_snap_ms: int | None = None

    def add(self, snap_ms: int, page: RankingPage, keep: set[str] | None = None) -> int:
        added = 0
        for row in page.rows:
            if keep is not None and row.symbol not in keep and row.rank > RANKING_PROMOTE_TOP:
                continue
            q = self.rows.setdefault(row.symbol, deque(maxlen=self.rows_per_symbol))
            q.append({"snap_ms": int(snap_ms), "ranking_type": page.ranking_type,
                      "duration": page.duration, "rank": int(row.rank),
                      "symbol": row.symbol, "last_u": int(row.last_u),
                      "vol_qu": int(row.vol_qu), "amount_u": int(row.amount_u)})
            added += 1
        self.last_snap_ms = int(snap_ms)
        return added

    def frame(self, symbol: str) -> pd.DataFrame:
        rows = list(self.rows.get(symbol, ()))
        df = pd.DataFrame(rows, columns=RANKING_COLS)
        dtypes = {"snap_ms": "int64", "rank": "int64", "last_u": "int64",
                  "vol_qu": "int64", "amount_u": "int64"}
        return df.astype(dtypes) if not df.empty else df.astype(dtypes)

    def prune(self, now_ms: int, keep: set[str] | None = None) -> int:
        cutoff = now_ms - self.keep_ms
        dropped = 0
        for symbol in list(self.rows):
            q = self.rows[symbol]
            while q and q[0]["snap_ms"] < cutoff:
                q.popleft()
                dropped += 1
            if not q or (keep is not None and symbol not in keep):
                self.rows.pop(symbol, None)
        return dropped


def tape_stats(trades: Sequence) -> dict[str, int | float]:
    """테이프 표본 통계 (A2 §2: `/trades` 는 최대 50건이라 **표본**이다).

    ⚠️ `price_u * qty_u` 는 건당 ~1e14, 세션 누적이면 int64 상한(9.2e18)을 쉽게 넘는다.
    pandas/numpy 정수 누적은 조용히 랩어라운드하므로 **Python int** 로만 합산한다.
    """
    if not trades:
        return {"n": 0, "min_ts_ms": 0, "max_ts_ms": 0, "notional_u": 0, "qty_qu": 0,
                "mean_qty_qu": 0.0}
    notional = 0
    qty = 0
    for t in trades:
        notional += int(t.price_u) * int(t.qty_u)      # Python int — 임의정밀도
        qty += int(t.qty_u)
    return {"n": len(trades), "min_ts_ms": int(trades[0].ts_ms),
            "max_ts_ms": int(trades[-1].ts_ms),
            "notional_u": notional // 1_000_000,       # price_u × qty_u → 마이크로달러
            "qty_qu": qty, "mean_qty_qu": qty / len(trades)}


# --------------------------------------------------------------------------- #
# 컬렉터 컨텍스트
# --------------------------------------------------------------------------- #
@dataclass
class CollectorContext:
    """모든 루프가 공유하는 상태. 루프 함수는 계약 시그니처를 지키고 이걸 keyword 로 받는다."""

    cfg: Config
    client: TossClient
    store: Store
    notifier: Notifier
    clock: Clock
    scheduler: SessionScheduler
    budget: BudgetGuard
    tiers: TierStateMachine
    detector: EventDetector
    activity: PriceActivityTracker = field(default_factory=PriceActivityTracker)
    rankings: RankingBuffer = field(default_factory=RankingBuffer)
    buffers: dict[str, SymbolBuffer] = field(default_factory=dict)
    baselines: dict[str, dict] = field(default_factory=dict)
    prev_close: dict[str, int] = field(default_factory=dict)
    shares_out: dict[str, int] = field(default_factory=dict)
    curves: dict[str, tuple[int, object]] = field(default_factory=dict)
    watchlist: list[str] = field(default_factory=list)
    history_days: list = field(default_factory=list)
    last_trade_ms: dict[str, int] = field(default_factory=dict)
    missing_streak: dict[str, int] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    session: str = CLOSED
    state_path: Path | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    _last_saved_ms: int = 0
    _last_telemetry_ms: int = 0
    _max_digits_seen: int = 0
    _http429: int = 0
    #: 재시작 시 복원한 심볼별 마지막 봉 시각 (백필 시작점 힌트).
    _resume_candle_ms: dict[str, int] = field(default_factory=dict)

    # ---- 생성 ----------------------------------------------------------

    @classmethod
    def create(cls, client: TossClient, store: Store, cfg: Config, *,
               notifier: Notifier | None = None, clock: Clock | None = None,
               scheduler: SessionScheduler | None = None,
               symbols: Sequence[str] = (), state_path: Path | str | None = None,
               resume: bool = True) -> "CollectorContext":
        det = cfg.require_detector()
        uni = cfg.require_universe()
        polling = cfg.require_polling()
        notifier = notifier or Notifier(_default_log_path(cfg))
        clock = clock or Clock(notifier=notifier)
        scheduler = scheduler or SessionScheduler(client, clock, notifier=notifier)
        budget = BudgetGuard(dict(cfg.limits), cfg.api.usage_ratio,
                             clock=clock, notifier=notifier)
        tiers = TierStateMachine(
            det.promote_hysteresis_s, tier2_max=uni.tier2_max, tier3_max=uni.tier3_max,
            stale_demote_s=polling.tier2_candle_s * STALE_DEMOTE_MULT)
        detector = EventDetector(
            EventParams(window_min=det.event_window_min, ret_min=det.event_ret_min,
                        day_ret_min=det.event_day_ret_min, rvol_min=det.event_rvol_min),
            notifier=notifier)
        ctx = cls(cfg=cfg, client=client, store=store, notifier=notifier, clock=clock,
                  scheduler=scheduler, budget=budget, tiers=tiers, detector=detector,
                  watchlist=[s.upper() for s in symbols],
                  state_path=Path(state_path) if state_path
                  else _default_state_path(cfg))
        if resume:
            ctx.load_state()
        ctx.refresh_plan()
        return ctx

    # ---- 실행 제어 ------------------------------------------------------

    def running(self) -> bool:
        return not self.stop.is_set()

    def shutdown(self, reason: str) -> None:
        if self.running():
            self.notifier.alert(f"collector stopping: {reason}")
            self.stop.set()

    def current_session(self) -> str:
        """캘린더가 있으면 그것으로, 없으면 세션 감시가 마지막에 써 둔 값으로 판정한다."""
        if self.scheduler.calendar:
            return self.scheduler.session_at(self.clock.now_ms())
        return self.session

    def collecting(self) -> bool:
        """세션이 열려 있고 캘린더를 확보했는가."""
        return self.running() and self.current_session() != CLOSED

    def bump(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    # ---- API 호출 회계 ---------------------------------------------------

    def after_call(self, group: str, calls: int = 1) -> None:
        """호출 1건(또는 배치 n건) 관측 — 예산 카운터 + 서버 시각 보정 + 429 감지."""
        for _ in range(max(1, calls)):
            self.budget.on_request(group)
        self.bump(f"req_{group}", max(1, calls))
        self.clock.observe_headers(getattr(self.client, "last_headers", None))
        self.sync_rate_limits(group)

    def sync_rate_limits(self, group: str = GROUP_MARKET_DATA) -> None:
        """client 가 관측한 429 증가분을 예산 가드에 반영한다.

        **실패로 끝난 호출에서도** 반드시 불려야 한다 — 재시도까지 실패해 예외로 빠져나간
        429 야말로 예산 사고의 신호이기 때문이다. 고수위(`_http429`)로 비교하므로
        여러 번 불려도 중복 계상되지 않는다.
        """
        seen429 = int(getattr(self.client, "counters", {}).get("http_429", 0))
        if seen429 > self._http429:
            self._http429 = seen429
            self.budget.on_429(group)

    def refresh_plan(self) -> None:
        """**정원이 다 찼을 때**의 호출률로 계획을 세운다 (최악 케이스 예측).

        현재 인원이 아니라 정원으로 계산하는 이유: 예산은 "지금 몇 개를 보고 있나" 가 아니라
        "다 채우면 넘치나" 로 검증해야 사고를 미리 막는다. 그래서 이 계산은
        `config/config.example.yaml` 하단 산식과 정확히 같은 답을 낸다.
        """
        uni = self.cfg.require_universe()
        plan = TierPlan.from_config(
            self.cfg,
            tier1_symbols=uni.tier1_max,
            tier2_symbols=self.tiers.capacity.get(2) or uni.tier2_max,
            tier3_symbols=self.tiers.capacity.get(3) or uni.tier3_max)
        self.budget.set_plan(plan)
        over = self.budget.validate_plan()
        if over:
            detail = ", ".join(f"{g}+{v:.2f} req/s" for g, v in sorted(over.items()))
            self.notifier.alert(
                f"budget: configured plan exceeds usage_ratio budget ({detail}) — "
                "자동 축소한다. 설정을 재검토하라 (config 하단 산식 참조)")
            self.apply_budget(force=True)

    def apply_budget(self, *, force: bool = False) -> dict[str, int] | None:
        """BudgetGuard 지시대로 티어 정원을 줄인다 (안전 강등)."""
        orders = self.budget.should_shrink()
        if not orders:
            return None
        uni = self.cfg.require_universe()
        now = self.clock.now_ms()
        caps: dict[str, int | None] = {"tier2_max": None, "tier3_max": None}
        for group, drop in orders.items():
            # 축소 폭은 **정원** 기준으로 계산됐으므로 정원에서 뺀다. 현재 인원에서 빼면
            # (인원 1 − 지시 33) 처럼 정원이 통째로 무너진다.
            if group == GROUP_MARKET_DATA:
                have = self.tiers.capacity.get(3) or uni.tier3_max
                caps["tier3_max"] = max(1, have - drop)
            elif group == GROUP_CHART:
                have = self.tiers.capacity.get(2) or uni.tier2_max
                caps["tier2_max"] = max(1, have - drop)
        if caps["tier2_max"] is None and caps["tier3_max"] is None:
            return orders
        changes = self.tiers.set_capacity(ts_ms=now, **caps)
        self.notifier.warn(
            f"budget shrink {orders} → caps={{tier2:{self.tiers.capacity[2]}, "
            f"tier3:{self.tiers.capacity[3]}}} demoted={len(changes)}")
        self.flush_changes()
        self.bump("budget_shrinks")
        if not force:
            self.refresh_plan()
        return orders

    # ---- 이벤트 알림 등급 -------------------------------------------------

    def current_session_start_ms(self) -> int | None:
        """지금 속한 세션의 시작 시각. 세션 밖이거나 캘린더가 없으면 None."""
        if not self.scheduler.calendar:
            return None
        win = session_window(self.scheduler.calendar, self.clock.now_ms())
        return None if win is None else int(win.start_ms)

    def announce_event(self, emission, result=None) -> str:
        """이벤트 알림 등급 결정. 반환: 실제로 사용한 등급.

        무인 야간 운영에서 `alert` 는 **사람이 반드시 봐야 하는 것**만 남아야 의미가 있다.
        그래서 alert 는 아래를 전부 만족할 때만 낸다:

          * **최초 검출** — 라벨 갱신(peak/ret 이 장중에 채워지는 것)은 정보일 뿐이다.
          * **현재 세션 안** — 과거 세션 백필로 뒤늦게 잡힌 이벤트는 지금 행동할 대상이 아니다
            (실측: 프리마켓 수집 중에 `session=day` 이벤트가 ERROR 로 올라왔다).
          * **충분히 신선함** — 검출 지연이 `EVENT_ALERT_MAX_LAG_MIN` 이내.
            재기동 직후 같은 세션의 오래된 이벤트가 한꺼번에 올라오는 것도 이 조건이 막는다.

        나머지는 전부 `info` 다 — 기록은 남되 알림 채널을 오염시키지 않는다.
        """
        t0 = int(emission.t0_ms)
        now = self.clock.now_ms()
        lag_min = (now - t0) // MIN_MS
        session_start = self.current_session_start_ms()
        past_session = session_start is not None and t0 < session_start
        fresh = lag_min <= EVENT_ALERT_MAX_LAG_MIN
        extra = (f"path={result.path} score={result.score:.3f}" if result is not None
                 else "")
        kind = str(emission.record.get("kind"))

        if emission.is_new and not past_session and fresh:
            self.notifier.event(emission.symbol, t0, kind, extra)
            self.bump("event_alerts")
            return "alert"

        why = ("label-update" if not emission.is_new
               else "past-session" if past_session else f"stale({lag_min}m)")
        self.notifier.info(
            f"event[{why}] {emission.symbol} kind={kind} t0_ms={t0} "
            f"session={emission.session} {extra}".rstrip())
        self.bump("event_infos")
        return "info"

    # ---- 텔레메트리 (무인 실행의 유일한 관측 창) --------------------------

    def telemetry(self) -> dict[str, object]:
        """주기 리포트 한 줄에 들어갈 관측치.

        정밀도 지표(계약 A4)를 포함한다: `precision_rounded` 는 마이크로달러보다 미세한
        값을 만나 반올림한 건수이고, `max_digits` 는 관측된 최대 소수 자릿수다.
        **`max_digits` 가 갑자기 커지면 API 응답 형식이 바뀐 신호**라 조기 경보로 쓴다
        (W1 인수인계). A5 로 1분봉을 원주가로 받게 되면서 분할 보정이 만들던 긴 소수가
        사라지므로 평시 값은 오히려 낮아져야 한다 — 그래서 상승이 더 잘 보인다.
        """
        stats = precision_stats()
        parsed = int(stats.get("parsed", 0) or 0)
        rounded = int(stats.get("rounded", 0) or 0)
        return {
            "session": self.current_session(),
            "watch": len(self.watchlist),
            "tier2": len(self.tiers.at_least(2)),
            "tier3": len(self.tiers.members(3)),
            "events": int(self.counters.get("events", 0)),
            "promotions": int(self.counters.get("promotions", 0)),
            "tape_gaps": int(self.counters.get("tape_gaps", 0)),
            "api_errors": int(self.counters.get("api_errors", 0)),
            "precision_rounded": int(getattr(self.client, "counters", {})
                                     .get("precision_rounded", 0)),
            "precision_parsed": parsed,
            "precision_rounded_pct": round(100.0 * rounded / parsed, 3) if parsed else 0.0,
            "precision_max_digits": int(stats.get("max_digits", 0) or 0),
        }

    def report_telemetry(self, *, force: bool = False) -> dict[str, object] | None:
        """주기 텔레메트리 출력 + 정밀도 드리프트 조기 경보."""
        now = self.clock.now_ms()
        if not force and now - self._last_telemetry_ms < TELEMETRY_EVERY_S * 1000:
            return None
        self._last_telemetry_ms = now
        data = self.telemetry()
        self.notifier.info("telemetry " + " ".join(f"{k}={v}" for k, v in data.items())
                           + " | " + self.budget.describe())
        self._check_precision_drift()
        return data

    def _check_precision_drift(self) -> None:
        """소수 자릿수 신고점 = 응답 형식 변화 의심 신호 (W1 인수인계)."""
        stats = precision_stats()
        digits = int(stats.get("max_digits", 0) or 0)
        if digits <= self._max_digits_seen:
            return
        previous, self._max_digits_seen = self._max_digits_seen, digits
        sample = stats.get("last_raw")
        detail = (f"max decimal digits {previous} → {digits} "
                  f"(micro 단위는 {MICRO_DIGITS}자리, 초과분은 반올림됨; 표본 {sample!r})")
        if previous:
            # 이미 기준선이 있는데 더 커졌다 — 응답 형식이 바뀐 쪽을 먼저 의심한다.
            self.notifier.alert(f"precision drift: {detail} — API 응답 형식 변화 의심. "
                                "저장값이 조용히 반올림되고 있으니 확인하라")
        else:
            self.notifier.warn(f"precision baseline: {detail}")
        self.bump("precision_drift")

    # ---- 티어 변경 기록 --------------------------------------------------

    def flush_changes(self) -> int:
        changes = self.tiers.drain_changes()
        for ch in changes:
            self._record_change(ch)
        return len(changes)

    def _record_change(self, ch: TierChange) -> None:
        try:
            self.store.record_promotion(ch.symbol, ch.ts_ms, ch.from_tier, ch.to_tier,
                                        ch.reason, float(ch.score))
        except Exception as exc:
            self.notifier.warn(f"record_promotion failed for {ch.symbol}: "
                               f"{type(exc).__name__}: {exc}")
        self.notifier.promotion(ch.symbol, ch.from_tier, ch.to_tier, ch.reason, ch.score)
        self.bump("promotions" if ch.to_tier > ch.from_tier else "demotions")
        if ch.to_tier < 2:
            self.drop_symbol_state(ch.symbol)

    def drop_symbol_state(self, symbol: str) -> None:
        """강등된 심볼의 **무거운** 상태만 버린다 (장시간 실행 메모리 안정성).

        ⚠️ 검출 이력(`detector.forget`)은 여기서 지우지 않는다. 강등→재승격은 흔한 churn
        인데(랭킹 진입 승격 → stale 강등 → 재진입) 이력을 지우면 재승격 직후 같은 이벤트를
        전부 다시 기록한다 — 라이브에서 관측된 40% 중복의 원인이 정확히 이것이었다.
        이력은 (symbol, t0_ms) 단위로 상한이 걸려 있어 그냥 둬도 메모리가 자라지 않는다.
        """
        self.buffers.pop(symbol, None)
        self.curves.pop(symbol, None)

    # ---- 워치리스트 ------------------------------------------------------

    def watch(self, symbol: str) -> bool:
        sym = symbol.upper()
        if sym in self.watchlist:
            return False
        uni = self.cfg.require_universe()
        if len(self.watchlist) >= uni.tier1_max:
            return False
        self.watchlist.append(sym)
        self.bump("watchlist_added")
        return True

    def unwatch(self, symbol: str) -> None:
        if symbol in self.watchlist:
            self.watchlist.remove(symbol)
        self.missing_streak.pop(symbol, None)
        self.activity.prune(set(self.watchlist))
        self.drop_symbol_state(symbol)
        self.bump("watchlist_dropped")

    def tier1_symbols(self) -> list[str]:
        uni = self.cfg.require_universe()
        return self.watchlist[:uni.tier1_max]

    def buffer(self, symbol: str) -> SymbolBuffer:
        buf = self.buffers.get(symbol)
        if buf is None:
            buf = self.buffers[symbol] = SymbolBuffer(symbol)
        return buf

    def calendar_list(self) -> list:
        return self.scheduler.calendar_list(extra=self.history_days)

    # ---- 상태 저장/복원 --------------------------------------------------

    def state_snapshot(self) -> dict:
        return {
            "version": 1,
            "saved_ms": self.clock.now_ms(),
            "session": self.session,
            "watchlist": list(self.watchlist),
            "tiers": {s: st.tier for s, st in self.tiers.states.items() if st.tier > 1},
            "last_candle_ms": {s: b.last_ts() for s, b in self.buffers.items()
                               if b.last_ts() is not None},
            "last_trade_ms": dict(self.last_trade_ms),
            "last_ranking_snap_ms": self.rankings.last_snap_ms,
            "missing_streak": dict(self.missing_streak),
            "counters": dict(self.counters),
        }

    def save_state(self, *, force: bool = False) -> bool:
        if self.state_path is None:
            return False
        now = self.clock.now_ms()
        if not force and now - self._last_saved_ms < STATE_SAVE_S * 1000:
            return False
        self._last_saved_ms = now
        try:
            _atomic_write_json(self.state_path, self.state_snapshot())
        except OSError as exc:
            self.notifier.warn(f"state save failed: {type(exc).__name__}: {exc}")
            return False
        return True

    def load_state(self) -> bool:
        """마지막 수집 지점 복원. 파일이 없거나 깨졌으면 조용히 새로 시작한다."""
        if self.state_path is None or not self.state_path.exists():
            return False
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.notifier.warn(f"state file unreadable ({exc}) — starting fresh")
            return False
        if not isinstance(data, dict) or int(data.get("version", 0)) != 1:
            return False
        saved_ms = int(data.get("saved_ms") or 0)
        for sym in data.get("watchlist") or ():
            if sym not in self.watchlist:
                self.watchlist.append(str(sym))
        for sym, tier in (data.get("tiers") or {}).items():
            self.tiers.seed(str(sym), int(tier), saved_ms)
        self.last_trade_ms.update({str(k): int(v)
                                   for k, v in (data.get("last_trade_ms") or {}).items()})
        self.missing_streak.update({str(k): int(v)
                                    for k, v in (data.get("missing_streak") or {}).items()})
        self.counters.update({str(k): int(v)
                              for k, v in (data.get("counters") or {}).items()})
        self._resume_candle_ms.update({str(k): int(v) for k, v
                                       in (data.get("last_candle_ms") or {}).items()})
        self.notifier.info(
            f"resumed from {self.state_path}: watch={len(self.watchlist)} "
            f"tier2+={len(self.tiers.at_least(2))} saved_ms={saved_ms}")
        self.bump("resumes")
        return True

    def resume_point_ms(self, symbol: str) -> int | None:
        """재시작 전 마지막 수집 지점 (없으면 None)."""
        buf = self.buffers.get(symbol)
        if buf is not None and buf.last_ts() is not None:
            return buf.last_ts()
        return self._resume_candle_ms.get(symbol)


def _default_state_path(cfg: Config) -> Path | None:
    store_cfg = cfg.store
    if store_cfg is None:
        return None
    return Path(store_cfg.db_path).parent / "collector_state.json"


def _default_log_path(cfg: Config) -> Path | None:
    store_cfg = cfg.store
    if store_cfg is None:
        return None
    return Path(store_cfg.db_path).parent / "collector.log"


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"), sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# 공통 루프 골격
# --------------------------------------------------------------------------- #
async def _guarded(ctx: CollectorContext, name: str, coro,
                   group: str = GROUP_MARKET_DATA) -> bool:
    """루프 몸통 1회 실행. 치명(Forbidden)만 정지시키고 나머지는 삼킨다."""
    try:
        await coro
        return True
    except asyncio.CancelledError:
        raise
    except Forbidden as exc:
        ctx.shutdown(f"{name}: Forbidden — IP 미등록/권한 문제로 수집 중단 ({exc})")
        return False
    except SchemaMismatch as exc:
        ctx.bump("schema_mismatch")
        ctx.notifier.warn(f"{name}: schema mismatch (skip): {exc}")
        return False
    except (TossApiError, OSError) as exc:
        ctx.bump("api_errors")
        ctx.notifier.warn(f"{name}: {type(exc).__name__}: {exc}")
        return False
    except Exception as exc:                       # 예상 못 한 예외로 루프가 죽으면 안 된다
        ctx.bump("loop_errors")
        ctx.notifier.warn(f"{name}: unexpected {type(exc).__name__}: {exc}")
        return False
    finally:
        # 실패로 끝난 호출의 429 도 예산 가드가 봐야 한다.
        ctx.sync_rate_limits(group)


async def _loop(ctx: CollectorContext, name: str, period_s: float, body,
                cycles: int | None, group: str = GROUP_MARKET_DATA) -> None:
    """`period_s` 주기로 `body()` 를 도는 표준 루프. 세션이 닫혀 있으면 쉰다."""
    done = 0
    while ctx.running() and (cycles is None or done < cycles):
        if not ctx.collecting():
            await ctx.clock.sleep(min(period_s, IDLE_SLEEP_S))
            done += 1
            continue
        await _guarded(ctx, name, body(), group)
        ctx.apply_budget()
        ctx.save_state()
        done += 1
        if ctx.running() and (cycles is None or done < cycles):
            await ctx.clock.sleep(period_s)


# --------------------------------------------------------------------------- #
# 랭킹 루프 — 최우선 (과거 조회 불가)
# --------------------------------------------------------------------------- #
async def run_rankings(client: TossClient, store: Store, cfg: Config, *,
                       ctx: CollectorContext | None = None,
                       cycles: int | None = None) -> None:
    ctx = ctx or CollectorContext.create(client, store, cfg)
    period = float(cfg.require_polling().ranking_snap_s)
    await _loop(ctx, "rankings", period, lambda: rankings_once(ctx), cycles, GROUP_RANKING)


async def rankings_once(ctx: CollectorContext) -> int:
    """랭킹 4종 스냅샷 → DB + 실시간 버퍼 + 승격 트리거."""
    stored = 0
    watch = set(ctx.watchlist)
    for rtype in RANKING_TYPES:
        page = await ctx.client.get_rankings(rtype, duration="realtime", market="US",
                                             count=100)
        ctx.after_call(GROUP_RANKING)
        # snap_ms 는 우리 관측 시각. rankedAt 은 12~23초 뒤처지므로 참고값으로만 쓴다.
        snap_ms = ctx.clock.now_ms()
        stored += ctx.store.insert_rankings(snap_ms, page)
        ctx.rankings.add(snap_ms, page, keep=watch)
        _ranking_triggers(ctx, page, snap_ms)
        ctx.bump("ranking_snaps")
    ctx.rankings.prune(ctx.clock.now_ms(), keep=set(ctx.watchlist) | set(ctx.buffers))
    return stored


def _ranking_triggers(ctx: CollectorContext, page: RankingPage, snap_ms: int) -> None:
    """랭킹 상위 진입 = 승격 트리거 (docs/03 §1). 신규 심볼은 워치리스트에 누적한다."""
    toss = page.ranking_type.startswith("TOSS_SECURITIES")
    for row in page.rows:
        if row.rank > RANKING_PROMOTE_TOP:
            continue
        ctx.watch(row.symbol)
        if toss and ctx.tiers.tier_of(row.symbol) < 2:
            score = 0.5 + 0.3 * (RANKING_PROMOTE_TOP - row.rank) / RANKING_PROMOTE_TOP
            if ctx.tiers.force(row.symbol, 2, "ranking_entry", score, snap_ms) is not None:
                ctx.bump("ranking_promotions")
    ctx.flush_changes()


# --------------------------------------------------------------------------- #
# Tier 1 — 가격 스윕 (A2 §3: first_print / staleness 가 1순위 트리거)
# --------------------------------------------------------------------------- #
async def run_tier1_price_sweep(client: TossClient, store: Store, cfg: Config, *,
                                ctx: CollectorContext | None = None,
                                cycles: int | None = None) -> None:
    ctx = ctx or CollectorContext.create(client, store, cfg)
    period = float(cfg.require_polling().tier1_sweep_s)
    await _loop(ctx, "tier1", period, lambda: tier1_sweep_once(ctx), cycles,
                GROUP_MARKET_DATA)


async def tier1_sweep_once(ctx: CollectorContext) -> int:
    symbols = ctx.tier1_symbols()
    seen = 0
    for i in range(0, len(symbols), BATCH_MAX):
        seen += await _sweep_chunk(ctx, symbols[i:i + BATCH_MAX])
    ctx.flush_changes()
    stale = ctx.tiers.sweep(ctx.clock.now_ms())
    if stale:
        ctx.bump("stale_demotions", len(stale))
        ctx.flush_changes()
    ctx.apply_budget()
    return seen


async def _sweep_chunk(ctx: CollectorContext, chunk: list[str]) -> int:
    if not chunk:
        return 0
    prices: list[Price] = await ctx.client.get_prices(chunk)
    ctx.after_call(GROUP_MARKET_DATA)
    now = ctx.clock.now_ms()

    # 함정1: 미존재 심볼은 404 가 아니라 **200 + 조용한 누락**이다.
    # 요청 200개에 응답 180개일 수 있으므로 반드시 대조한다.
    returned = {p.symbol for p in prices}
    missing = [s for s in chunk if s not in returned]
    if missing:
        ctx.bump("prices_missing", len(missing))
        ctx.notifier.warn(f"tier1: {len(missing)}/{len(chunk)} symbols missing from "
                          f"/prices response (e.g. {missing[:5]})")
        for sym in missing:
            streak = ctx.missing_streak.get(sym, 0) + 1
            ctx.missing_streak[sym] = streak
            if streak >= MISSING_STREAK_DROP:
                ctx.notifier.warn(f"tier1: dropping {sym} after {streak} consecutive "
                                  "absences (delisted or bad symbol)")
                ctx.unwatch(sym)

    for price in prices:
        ctx.missing_streak.pop(price.symbol, None)
        _on_price(ctx, price, now)
    return len(prices)


def _on_price(ctx: CollectorContext, price: Price, now_ms: int) -> None:
    """`/prices` 1건 → 활동 상태 → 승격 판단.

    ⚠️ 함정2: `lastPrice` 는 체결이 없어도 온다. 여기서 `last_u` 로 수익률을 계산하지 않고
    "변했는가" 만 본다. 진짜 활동 신호는 `timestamp` 쪽이다.
    """
    state = ctx.activity.update(price, now_ms)
    if state.no_print:
        ctx.bump("price_no_print")
    if ctx.tiers.tier_of(price.symbol) >= 2:
        return                               # 상위 티어는 봉 기반 스코어가 관장한다
    score = activity_score(state)
    if state.first_print:
        reason = "first_print"
    elif state.awakened:
        reason = "staleness_drop"
    elif score >= ACTIVITY_PROMOTE_SCORE:
        reason = "price_activity"
    else:
        return
    if ctx.tiers.force(price.symbol, 2, reason, score, now_ms) is not None:
        ctx.bump(f"promote_{reason}")


# --------------------------------------------------------------------------- #
# Tier 2 — 1분봉 (우선순위 최하: 백필로 대체 가능)
# --------------------------------------------------------------------------- #
async def run_tier2_candles(client: TossClient, store: Store, cfg: Config, *,
                            ctx: CollectorContext | None = None,
                            cycles: int | None = None) -> None:
    """심볼당 `tier2_candle_s` 주기가 되도록 라운드로빈으로 한 종목씩 폴링한다."""
    ctx = ctx or CollectorContext.create(client, store, cfg)
    period = float(cfg.require_polling().tier2_candle_s)
    cursor = 0
    done = 0
    while ctx.running() and (cycles is None or done < cycles):
        done += 1
        symbols = sorted(ctx.tiers.at_least(2))
        if not ctx.collecting() or not symbols:
            await ctx.clock.sleep(IDLE_SLEEP_S)
            continue
        cursor %= len(symbols)
        symbol = symbols[cursor]
        cursor += 1
        await _guarded(ctx, f"tier2:{symbol}", tier2_symbol_once(ctx, symbol), GROUP_CHART)
        ctx.save_state()
        await ctx.clock.sleep(period / len(symbols))


async def tier2_symbol_once(ctx: CollectorContext, symbol: str) -> int:
    await _ensure_history(ctx, symbol)
    page = await ctx.client.get_candles(symbol, "1m", count=CANDLE_PAGE,
                                        adjusted=candle_adjusted("1m"))
    ctx.after_call(GROUP_CHART)
    if not page.candles:
        return 0
    ctx.store.upsert_candles_1m(page.candles)
    new = ctx.buffer(symbol).upsert(page.candles)
    ctx.bump("candles_1m", new)
    _detect(ctx, symbol)
    return new


def _detect(ctx: CollectorContext, symbol: str) -> None:
    """새 봉 기준으로 스코어 + 이벤트. 피처·이벤트는 전부 analysis 재사용이다."""
    buf = ctx.buffers.get(symbol)
    if buf is None or not len(buf):
        return
    df = buf.frame()
    now = ctx.clock.now_ms()
    result = ctx.detector.evaluate(
        symbol, df, rankings=ctx.rankings.frame(symbol), calendar=ctx.calendar_list(),
        curve=_curve_for(ctx, symbol, df, now), baseline=ctx.baselines.get(symbol),
        shares_outstanding_qu=ctx.shares_out.get(symbol),
        prev_close_u=ctx.prev_close.get(symbol), now_ms=now)
    if result is None:
        return
    ctx.tiers.on_new_data(symbol, result.score, result.ts_ms, reason=result.path)
    ctx.flush_changes()
    for emission in result.events:
        try:
            ctx.store.record_event(emission.record)
        except Exception as exc:
            ctx.notifier.warn(f"record_event failed for {symbol}: "
                              f"{type(exc).__name__}: {exc}")
            continue
        ctx.bump("events" if emission.is_new else "event_updates")
        ctx.announce_event(emission, result)


def _curve_for(ctx: CollectorContext, symbol: str, df: pd.DataFrame, now_ms: int):
    """시간대 보정 RVOL 분모. **당일은 제외**한다 (자기오염 방지 — W3 인수인계 §4)."""
    cached = ctx.curves.get(symbol)
    if cached is not None and now_ms - cached[0] < CURVE_TTL_MS:
        return cached[1]
    calendar = ctx.calendar_list()
    today = ctx.scheduler.market_day_at(now_ms) or ctx.scheduler.today()
    exclude = (today.date,) if today is not None else ()
    curve = build_curve(df, calendar, exclude_dates=exclude)
    ctx.curves[symbol] = (now_ms, curve)
    return curve


async def _ensure_history(ctx: CollectorContext, symbol: str) -> None:
    """승격 직후 1회: DB(백필 결과) → 부족하면 제한적 API 백필 → 일봉 베이스라인."""
    buf = ctx.buffer(symbol)
    if ctx.baselines.get(symbol) is not None and len(buf) > 0:
        return
    now = ctx.clock.now_ms()
    if not len(buf):
        loaded = _load_history_from_db(ctx, symbol, now)
        ctx.bump("history_from_db", 1 if loaded else 0)
        if loaded < CANDLE_PAGE:
            # 재시작이면 마지막 수집 지점까지만 메운다 (이어받기).
            await _backfill_1m(ctx, symbol, stop_at_ms=ctx.resume_point_ms(symbol))
    if ctx.baselines.get(symbol) is None:
        await _refresh_baseline(ctx, symbol, now)


def _load_history_from_db(ctx: CollectorContext, symbol: str, now_ms: int) -> int:
    """이미 백필된 1분봉이 있으면 API 대신 DB 에서 읽는다 (예산 절약).

    실시간 1분봉의 우선순위가 낮아진 이유가 바로 이것이다 (docs/03 §6).
    """
    store_cfg = ctx.cfg.store
    if store_cfg is None:
        return 0
    try:
        from ..store.reader import Reader

        reader = getattr(ctx, "_reader", None)
        if reader is None:
            reader = Reader(Path(store_cfg.db_path))
            ctx._reader = reader                       # type: ignore[attr-defined]
        df = reader.read_candles_1m(symbol, now_ms - HISTORY_DAYS * DAY_MS, now_ms)
    except Exception:
        return 0                                        # DB 가 없거나 비어 있으면 그냥 API 로
    if df is None or df.empty:
        return 0
    rows = [Candle(symbol=symbol, ts_ms=int(r.ts_ms), open_u=int(r.open_u),
                   high_u=int(r.high_u), low_u=int(r.low_u), close_u=int(r.close_u),
                   vol_qu=int(r.vol_qu)) for r in df.itertuples()]
    return ctx.buffer(symbol).upsert(rows)


async def _backfill_1m(ctx: CollectorContext, symbol: str,
                       pages: int = MAX_BACKFILL_PAGES,
                       stop_at_ms: int | None = None) -> int:
    """역방향 페이지네이션 백필. `stop_at_ms` 까지 닿으면 멈춘다 (재시작 이어받기).

    함정3: `before` 는 **inclusive** 라 `nextBefore` 를 그대로 넘기면 경계 봉이 중복된다.
    저장은 upsert 라 멱등이지만 카운팅이 틀어지므로 1ms 당겨 요청한다.
    """
    before: int | None = None
    total = 0
    buf = ctx.buffer(symbol)
    for _ in range(max(1, pages)):
        page = await ctx.client.get_candles(symbol, "1m", count=CANDLE_PAGE,
                                            before_ms=before,
                                            adjusted=candle_adjusted("1m"))
        ctx.after_call(GROUP_CHART)
        if not page.candles:
            break
        ctx.store.upsert_candles_1m(page.candles)
        total += buf.upsert(page.candles)
        oldest = int(page.candles[0].ts_ms)             # client 가 오름차순 정규화
        if stop_at_ms is not None and oldest <= int(stop_at_ms):
            break                                        # 마지막 수집 지점까지 메웠다
        if page.next_before_ms is None:
            break
        before = int(page.next_before_ms) - 1
    ctx.bump("backfill_bars", total)
    return total


async def _refresh_baseline(ctx: CollectorContext, symbol: str, now_ms: int) -> None:
    """일봉 베이스라인. 함정4: **당일 봉은 진행형**이라 완성봉으로 쓰면 안 된다."""
    page = await ctx.client.get_candles(symbol, "1d", count=60,
                                        adjusted=candle_adjusted("1d"))
    ctx.after_call(GROUP_CHART)
    if not page.candles:
        return
    ctx.store.upsert_candles_1d(page.candles)
    today = ctx.scheduler.market_day_at(now_ms) or ctx.scheduler.today()
    cutoff = exclude_today_1d_cutoff(today)
    rows = [c for c in page.candles if cutoff is None or int(c.ts_ms) <= cutoff]
    if len(rows) < len(page.candles):
        ctx.bump("daily_today_bar_dropped", len(page.candles) - len(rows))
    if not rows:
        return
    ctx.baselines[symbol] = compute_daily_baseline(candles_frame(rows))
    ctx.prev_close[symbol] = int(rows[-1].close_u)


# --------------------------------------------------------------------------- #
# Tier 3 — 마이크로 (테이프 조밀 / 호가 성기게, A2 §1)
# --------------------------------------------------------------------------- #
async def run_tier3_micro(client: TossClient, store: Store, cfg: Config, *,
                          ctx: CollectorContext | None = None,
                          cycles: int | None = None) -> None:
    """`/trades` 와 `/orderbook` 을 **서로 다른 주기**로 돌린다.

    A2 §1: 미국 호가는 최우선 1레벨뿐이라 정보량이 적고, 문헌상 최상위 피처는 테이프의
    시장가 매수 버스트다. 그래서 테이프를 조밀하게, 호가를 성기게 본다.
    """
    ctx = ctx or CollectorContext.create(client, store, cfg)
    polling = cfg.require_polling()
    trades_s = float(polling.tier3_trades_s)
    book_s = float(polling.tier3_orderbook_s)
    due: dict[tuple[str, str], int] = {}
    done = 0
    while ctx.running() and (cycles is None or done < cycles):
        done += 1
        members = sorted(ctx.tiers.members(3))
        if not ctx.collecting() or not members:
            await ctx.clock.sleep(IDLE_SLEEP_S)
            continue
        now = ctx.clock.now_ms()
        for symbol in members:
            due.setdefault((symbol, "trades"), now)
            due.setdefault((symbol, "book"), now)
        for key in [k for k in due if k[0] not in set(members)]:
            due.pop(key, None)

        ready = sorted((k for k, t in due.items() if t <= now), key=lambda k: due[k])
        for symbol, what in ready:
            if what == "trades":
                await _guarded(ctx, f"tier3:trades:{symbol}", _poll_trades(ctx, symbol),
                               GROUP_MARKET_DATA)
                due[(symbol, what)] = ctx.clock.now_ms() + int(trades_s * 1000)
            else:
                await _guarded(ctx, f"tier3:book:{symbol}", _poll_orderbook(ctx, symbol),
                               GROUP_MARKET_DATA)
                due[(symbol, what)] = ctx.clock.now_ms() + int(book_s * 1000)
        ctx.save_state()
        nxt = min(due.values()) if due else None
        wait = min(trades_s, book_s) if nxt is None else max(
            (nxt - ctx.clock.now_ms()) / 1000.0, 0.0)
        await ctx.clock.sleep(min(wait, IDLE_SLEEP_S) if wait > 0 else 0.0)


async def _poll_trades(ctx: CollectorContext, symbol: str) -> int:
    trades = await ctx.client.get_trades(symbol, count=TRADES_COUNT)
    ctx.after_call(GROUP_MARKET_DATA)
    if not trades:
        return 0
    stored = ctx.store.insert_trades(trades)
    stats = tape_stats(trades)
    ctx.bump("trades_rows", stored)

    # A2 §2: `/trades` 는 최대 50건이라 **표본**이다. 이번 응답의 최소 ts 가 직전 응답의
    # 최대 ts 보다 크면 그 사이 체결을 놓친 것 — 구간 누락을 카운트해 남긴다.
    prev_max = ctx.last_trade_ms.get(symbol)
    if prev_max is not None and int(stats["min_ts_ms"]) > prev_max:
        ctx.bump("tape_gaps")
        ctx.notifier.warn(
            f"tape gap {symbol}: prev_max={prev_max} < this_min={stats['min_ts_ms']} "
            f"(n={stats['n']}) — 표본 사이 체결 누락")
    ctx.last_trade_ms[symbol] = max(prev_max or 0, int(stats["max_ts_ms"]))
    return stored


async def _poll_orderbook(ctx: CollectorContext, symbol: str) -> int:
    ob = await ctx.client.get_orderbook(symbol)
    ctx.after_call(GROUP_MARKET_DATA)
    snap_ms = ctx.clock.now_ms()
    ctx.store.insert_orderbook(snap_ms, ob)
    ctx.bump("orderbook_snaps")
    return 1


# --------------------------------------------------------------------------- #
# 세션 감시 — 캘린더 갱신 + 세션 전환 시 티어 재구성
# --------------------------------------------------------------------------- #
async def run_session_watch(ctx: CollectorContext, *, tick_s: float = 10.0,
                            cycles: int | None = None) -> None:
    done = 0
    while ctx.running() and (cycles is None or done < cycles):
        done += 1
        await _guarded(ctx, "session", _session_tick(ctx))
        await ctx.clock.sleep(tick_s)


async def _session_tick(ctx: CollectorContext) -> None:
    await ctx.scheduler.ensure()
    if not ctx.history_days and ctx.scheduler.calendar:
        ctx.history_days = await ctx.scheduler.history(HISTORY_DAYS)
    changed = ctx.scheduler.poll(ctx.clock.now_ms())
    if changed is None:
        ctx.session = ctx.scheduler.session
        ctx.report_telemetry()                       # 주기 출력 (내부에서 간격 제한)
        return
    prev, new = changed
    ctx.session = new
    ctx.notifier.info(f"session {prev} → {new}")
    ctx.bump("session_changes")
    reconfigure_tiers(ctx, new)
    ctx.report_telemetry(force=True)                 # 세션 경계는 자연스러운 요약 지점


def reconfigure_tiers(ctx: CollectorContext, session: str) -> dict[str, int]:
    """세션 전환 시 티어 재구성.

    얇은 세션(day/after)은 체결 자체가 드물어 tier3 를 가득 채워도 대부분 빈 폴링이 된다.
    정원을 줄여 그만큼의 예산을 랭킹·테이프로 돌린다. 정원이 줄면 최약체부터 안전 강등된다.
    """
    uni = ctx.cfg.require_universe()
    scale = SESSION_TIER_SCALE.get(session, 1.0)
    caps = {"tier2_max": max(1, int(uni.tier2_max * scale)) if scale > 0 else 1,
            "tier3_max": max(1, int(uni.tier3_max * scale)) if scale > 0 else 1}
    changes = ctx.tiers.set_capacity(ts_ms=ctx.clock.now_ms(), reason="session_change",
                                     **caps)
    ctx.flush_changes()
    ctx.curves.clear()                                  # 날이 바뀌면 곡선도 다시 만든다
    ctx.refresh_plan()
    if changes:
        ctx.notifier.info(f"session {session}: tier caps {caps} (demoted {len(changes)})")
    return caps


# --------------------------------------------------------------------------- #
# 오케스트레이션
# --------------------------------------------------------------------------- #
async def run_all(ctx: CollectorContext, *, cycles: int | None = None) -> None:
    """4개 수집 루프 + 세션 감시를 각각 독립 task 로 돌린다."""
    cfg = ctx.cfg
    tasks = [
        asyncio.create_task(run_session_watch(ctx, cycles=cycles), name="session"),
        asyncio.create_task(run_rankings(ctx.client, ctx.store, cfg, ctx=ctx,
                                         cycles=cycles), name="rankings"),
        asyncio.create_task(run_tier1_price_sweep(ctx.client, ctx.store, cfg, ctx=ctx,
                                                  cycles=cycles), name="tier1"),
        asyncio.create_task(run_tier3_micro(ctx.client, ctx.store, cfg, ctx=ctx,
                                            cycles=cycles), name="tier3"),
        asyncio.create_task(run_tier2_candles(ctx.client, ctx.store, cfg, ctx=ctx,
                                              cycles=cycles), name="tier2"),
    ]
    stopper = asyncio.create_task(ctx.stop.wait(), name="stop")
    try:
        if cycles is None:
            # 무인 실행: 아무 루프가 끝나거나(=이상) stop 이 걸릴 때까지 함께 산다.
            await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
        else:
            await asyncio.wait(tasks)
    finally:
        ctx.stop.set()
        for task in tasks + [stopper]:
            task.cancel()
        await asyncio.gather(*tasks, stopper, return_exceptions=True)
        ctx.save_state(force=True)
        ctx.report_telemetry(force=True)             # 종료 요약 (무인 실행의 마지막 기록)


__all__ = [
    "CANDLE_ADJUSTED", "CollectorContext", "RANKING_TYPES", "RankingBuffer", "SymbolBuffer",
    "candle_adjusted", "candles_frame", "rankings_once", "reconfigure_tiers", "run_all",
    "run_rankings",
    "run_session_watch", "run_tier1_price_sweep", "run_tier2_candles", "run_tier3_micro",
    "tape_stats", "tier1_sweep_once", "tier2_symbol_once",
]

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
import hashlib
import json
import math
import os
import tempfile
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Sequence


import pandas as pd

from ..analysis.baselines import compute_daily_baseline
from ..analysis.labeling import EventParams
from ..api.client import BATCH_MAX, TossClient
from ..api.errors import (AuthExpired, Forbidden, ForbiddenEndpoint, SchemaMismatch,
                          TossApiError)
from ..api.models import Candle, Price, RankingPage, StockMeta, precision_stats
from ..config import Config
from ..store.writer import Store
from ..universe.filters import market_cap_u, passes_tier0
from .budget import GROUP_CHART, GROUP_MARKET_DATA, GROUP_RANKING, BudgetGuard, TierPlan
from .detector import (ACTIVITY_ENTRY_SCORE, EventDetector, PriceActivityTracker,
                       TierChange, TierStateMachine, activity_score, build_curve)
from .mismatch import NotFoundTally, is_symbol_not_found, symbol_from_loop_name
from .notifier import Notifier
from .scheduler import (CLOSED, Clock, SessionScheduler, exclude_today_1d_cutoff,
                        session_window, trading_day_of)

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
#: 재시작 이어받기 백필 상한 (페이지). 정전 구간을 메울 때만 이 한도까지 늘린다 —
#: 이걸로도 재개 지점에 못 닿으면 `_backfill_1m` 이 **반드시 경고**한다 (감사 H-9:
#: 1분봉은 수백 일 보관되므로 구멍은 탐지만 되면 나중에 메울 수 있다).
RESUME_BACKFILL_MAX_PAGES = 12
#: 곡선/이력에 쓸 날 수. 곡선 분모는 **당일 제외** (자기오염 방지).
HISTORY_DAYS = 3
MAX_BARS_PER_SYMBOL = 4 * 1440
#: 곡선 재계산 최소 간격 (ms).
CURVE_TTL_MS = 3600_000
#: 곡선 계산 **실패**(None)의 재시도 간격 (ms). 성공 TTL(1시간)로 None 을 캐시하면
#: 승격 직후 이력이 모자란 1시간 내내 RVOL 게이트가 조용히 꺼진다 (감사 C-1).
CURVE_NONE_TTL_MS = 5 * MIN_MS
#: `/stocks` 예산 그룹 (계약 C-3 스펙의 STOCK 그룹).
GROUP_STOCK = "STOCK"

#: 세션 전환 후 베이스라인 재계산을 이 시간에 걸쳐 **분산**한다 (초, 심볼별 결정적 오프셋).
#: 예전에는 전환 즉시 baselines 를 통째로 비워서, 다음 라운드로빈 한 바퀴(110초) 안에
#: tier2 정원만큼의 일봉 호출이 한꺼번에 나갔다 — 세션 시작 직후 429 의 자작 원인이다
#: (2026-08-04: 09:00 전환 42초 안에 429 3연발). 무효화는 유지하되 시각만 흩는다.
BASELINE_REFRESH_SPREAD_S = 900

#: 테이프 포화(50건 상한 + 구간 결손) 로 인정하는 유효기간 — 이 안에 다시 포화하지 않으면
#: 평상 주기로 돌아간다.
TAPE_SATURATION_TTL_MS = 2 * MIN_MS
#: 빠른 레인(절반 주기)에 동시에 둘 수 있는 최대 종목 수. 실측상 포화는 3~4종목에 집중된다.
TAPE_FAST_LANE_MAX = 4
#: 한산한 종목 주기의 상한 배수 — 되돌려 받는 대가로도 이보다 늦추지는 않는다.
TAPE_SLOW_MAX_MULT = 2.0

#: tier2 호가는 **여유가 있을 때만** 수집한다 — 측정 MARKET_DATA 사용률이 목표의 이 비율을
#: 넘으면 그 폴을 건너뛴다. tier3 테이프(4s)·호가(16s)·랭킹·캔들을 절대 밀어내지 않기
#: 위한 "1순위 희생" 규칙이다 (W5 에스컬레이션 2026-08-03).
#:
#: 이 루프는 의도적으로 `TierPlan.rates()` 에 넣지 않는다: 계획에 넣으면 예산 초과 시
#: BudgetGuard 가 `SHRINK_TIER[MARKET_DATA]=3` 규칙대로 **tier3 정원을 깎아** 정작
#: 지켜야 할 고빈도 수집이 줄어든다. 계획 밖에서 런타임 여유만 쓰고, 압박이 오면
#: 스스로 물러나는 구조가 우선순위를 정확히 표현한다.
TIER2_ORDERBOOK_HEADROOM = 0.90
#: 429 를 맞으면 이 시간 동안 tier2 호가를 쉰다 (사고 직후 가장 먼저 물러난다).
TIER2_ORDERBOOK_COOLDOWN_MS = 5 * MIN_MS

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
    #: 심볼 → tier0(유니버스) 통과 여부. 워치리스트 등록 게이트가 이걸 본다 (감사 F-2).
    #: 출처는 둘: `symbols` 테이블(build_universe 결과) 시드 + 랭킹 신규 심볼의
    #: `/stocks` 실시간 판정. 여기 없는(미확인) 심볼은 등록하지 않는다.
    universe_status: dict[str, bool] = field(default_factory=dict)
    #: 운영자가 CLI `--symbols` 로 명시한 심볼 — 유니버스 게이트를 우회한다.
    pinned: set[str] = field(default_factory=set)
    session: str = CLOSED
    state_path: Path | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    _last_saved_ms: int = 0
    _last_telemetry_ms: int = 0
    _max_digits_seen: int = 0
    _http429: int = 0
    #: client.counters["requests"] 고수위 — 재시도·실패까지 포함한 실제 HTTP 시도를
    #: BudgetGuard 에 계상하기 위한 기준점.
    _http_requests_seen: int = 0
    #: tier2 호가 양보 판정용 — 관측한 429 고수위와 쿨다운 종료 시각.
    _tier2_book_429_seen: int = 0
    _tier2_book_cooldown_ms: int = 0
    #: 지금 세션에서 허용된 정원 상한 (회복의 천장). reconfigure_tiers 가 갱신한다.
    session_caps: dict[int, int] = field(default_factory=dict)
    #: 심볼별 베이스라인 계산 시각 + 세션 전환 에포크 (분산 재계산용).
    baseline_ms: dict[str, int] = field(default_factory=dict)
    baseline_epoch_ms: int = 0
    #: 심볼별 `stock-not-found` 404 횟수 — 반복 404 심볼 판단 재료 (`mismatch.NotFoundTally`).
    not_found: NotFoundTally = field(default_factory=NotFoundTally)
    #: 티어 전이 구간 요약용 고수위 (개별 줄은 DEBUG, 사람은 이 델타를 본다).
    _last_promotions: int = 0
    _last_demotions: int = 0
    #: 테이프 포화(50건 상한 + 구간 결손) 관측 시각 — 적응형 폴링 주기의 입력.
    tape_saturated_ms: dict[str, int] = field(default_factory=dict)
    #: 유니버스 거부를 이미 로그한 심볼 (같은 심볼이 12초마다 로그를 도배하지 않게).
    _universe_logged: set[str] = field(default_factory=set)
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
        pinned = {s.upper() for s in symbols}
        ctx = cls(cfg=cfg, client=client, store=store, notifier=notifier, clock=clock,
                  scheduler=scheduler, budget=budget, tiers=tiers, detector=detector,
                  watchlist=[s.upper() for s in symbols],
                  pinned=pinned,
                  # 운영자 명시 심볼은 유니버스 판정을 기다리지 않는다.
                  universe_status={s: True for s in pinned},
                  state_path=Path(state_path) if state_path
                  else _default_state_path(cfg))
        # 재시도·실패 계상 기준점 — ctx 생성 이전의 호출(캘린더 등)은 귀속하지 않는다.
        ctx._http_requests_seen = int(
            (getattr(client, "counters", None) or {}).get("requests", 0) or 0)
        if resume:
            ctx.load_state()
            # 재기동 직후 억제 집합이 비면 오늘 이벤트가 전부 "신규" 로 재기록된다 (감사 F-3).
            _seed_event_suppression(ctx)
        _seed_universe_from_db(ctx)
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

    def _unaccounted_attempts(self) -> int:
        """마지막 계상 이후 client 가 실제로 보낸 HTTP 시도 수 (재시도 포함).

        고수위 비교라 여러 번 불려도 같은 시도를 두 번 세지 않는다. 여러 루프가
        한 이벤트루프에서 겹칠 때 드물게 다른 그룹의 시도가 이쪽에 귀속될 수 있는데,
        총량은 정확하고 방향은 보수적(과대 계상)이라 예산 감시 목적에는 안전하다.
        """
        counters = getattr(self.client, "counters", None)
        if not isinstance(counters, dict):
            return 0
        seen = int(counters.get("requests", 0) or 0)
        delta = seen - self._http_requests_seen
        if delta <= 0:
            return 0
        self._http_requests_seen = seen
        return delta

    def after_call(self, group: str, calls: int = 1) -> None:
        """호출 1건(또는 배치 n건) 관측 — 예산 카운터 + 서버 시각 보정 + 429 감지.

        예산에는 논리 호출 수가 아니라 **실제 HTTP 시도 수**(재시도 포함)를 계상한다 —
        재시도가 0회로 계상되면 실사용이 과소평가되어 한도 사고를 놓친다 (감사 ②).
        시도 수를 관측할 수 없는 클라이언트(테스트 더블)는 논리 호출 수로 폴백한다.
        """
        attempts = self._unaccounted_attempts()
        for _ in range(max(max(1, calls), attempts)):
            self.budget.on_request(group)
        self.bump(f"req_{group}", max(1, calls))
        self.clock.observe_headers(getattr(self.client, "last_headers", None))
        self.sync_rate_limits(group)

    def sync_rate_limits(self, group: str = GROUP_MARKET_DATA) -> None:
        """client 가 관측한 429 증가분·미계상 시도를 예산 가드에 반영한다.

        **실패로 끝난 호출에서도** 반드시 불려야 한다 — 재시도까지 실패해 예외로 빠져나간
        호출도 실제로 예산을 태웠고, 그 429 야말로 예산 사고의 신호이기 때문이다.
        고수위 비교이므로 여러 번 불려도 중복 계상되지 않는다.
        """
        for _ in range(self._unaccounted_attempts()):
            self.budget.on_request(group)
        seen429 = int(getattr(self.client, "counters", {}).get("http_429", 0))
        if seen429 > self._http429:
            self._http429 = seen429
            # 429 **원문 헤더**를 남긴다 (2026-08-04 지시): 지금까지 카운트만 있어서
            # "한도의 1/5 을 쓰는데 왜 429 인가" 를 판별할 수 없었다. Retry-After 나
            # X-RateLimit-* 가 오면 우리 한도 모델(그룹별 초당)이 틀렸다는 증거가 된다.
            hdrs = dict(getattr(self.client, "last_headers", {}) or {})
            keep = {k: v for k, v in hdrs.items()
                    if k.lower().startswith(("x-rate", "retry-after", "x-request",
                                             "date", "ratelimit"))}
            self.notifier.warn(
                f"HTTP-429-DETAIL group={group} status="
                f"{getattr(self.client, 'last_status', None)} headers={keep or hdrs}")
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

    def restore_budget(self) -> dict[str, int] | None:
        """429 없이 조용하면 깎였던 정원을 단계적으로 되돌린다 (래칫 해제).

        천장은 지금 세션의 정원(`session_caps`)이다 — 세션 배율을 무시하고 config 최대까지
        올리면 얇은 세션에서 빈 폴링에 예산을 태운다.
        """
        grows = self.budget.should_grow()
        if not grows:
            return None
        uni = self.cfg.require_universe()
        ceil2 = self.session_caps.get(2, uni.tier2_max)
        ceil3 = self.session_caps.get(3, uni.tier3_max)
        caps: dict[str, int | None] = {"tier2_max": None, "tier3_max": None}
        for group, add in grows.items():
            if group == GROUP_MARKET_DATA:
                have = self.tiers.capacity.get(3) or uni.tier3_max
                if have < ceil3:
                    caps["tier3_max"] = min(ceil3, have + add)
            elif group == GROUP_CHART:
                have = self.tiers.capacity.get(2) or uni.tier2_max
                if have < ceil2:
                    caps["tier2_max"] = min(ceil2, have + add)
        if caps["tier2_max"] is None and caps["tier3_max"] is None:
            return None
        self.tiers.set_capacity(ts_ms=self.clock.now_ms(), reason="budget_restore",
                                **caps)
        self.notifier.info(
            f"budget restore {grows} → caps={{tier2:{self.tiers.capacity[2]}, "
            f"tier3:{self.tiers.capacity[3]}}} (ceiling {ceil2}/{ceil3})")
        self.bump("budget_restores")
        self.refresh_plan()
        return caps

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
        now = self.clock.now_ms()
        # 랭킹 스냅 간격 이상 — 랭킹은 과거 조회 불가라 이 루프가 멈추면 영구 손실이다.
        # 마지막 스냅 이후 경과(초). 한 번도 못 받았으면 -1. 세션 열림인데 이 값이 폴 주기의
        # 수 배로 커지면 랭킹 루프가 조용히 멈춘 것 — 워치독이 이걸로 잡는다.
        snap = self.rankings.last_snap_ms
        ranking_snap_age_s = -1 if snap is None else max(0, (now - int(snap)) // 1000)
        # tier1 스윕 조회 성공률 — /prices 요청 대비 조용한 누락(함정1) 비율의 역수.
        seen = int(self.counters.get("prices_seen", 0))
        miss = int(self.counters.get("prices_missing", 0))
        req = seen + miss
        fetch_success_pct = round(100.0 * seen / req, 1) if req else 100.0
        return {
            "session": self.current_session(),
            "watch": len(self.watchlist),
            # 워치리스트에 있으면서 tier0 미통과인 심볼 수 — 0 이 아니면 유니버스 게이트가
            # 새는 것이다 (감사 F-2 재발 감시. pinned 는 운영자 책임이라 True 로 계상).
            "watch_outside_universe": sum(
                1 for s in self.watchlist if self.universe_status.get(s) is False),
            "universe_rejected": int(self.counters.get("universe_rejected", 0)),
            "tier2": len(self.tiers.at_least(2)),
            "tier3": len(self.tiers.members(3)),
            # 정원을 로그에서 뒤지지 않고 바로 본다 (래칫 감시용).
            "tier2_cap": int(self.tiers.capacity.get(2) or 0),
            "tier3_cap": int(self.tiers.capacity.get(3) or 0),
            "budget_shrinks": int(self.counters.get("budget_shrinks", 0)),
            "budget_restores": int(self.counters.get("budget_restores", 0)),
            "http_429": int(getattr(self.client, "counters", {}).get("http_429", 0)),
            "events": int(self.counters.get("events", 0)),
            "promotions": int(self.counters.get("promotions", 0)),
            "tape_gaps": int(self.counters.get("tape_gaps", 0)),
            # 지금 50건 상한에 걸려 있는 종목 수 — 0 이 아니면 그 종목은 적응형
            # 빠른 레인으로 옮겨져 있다 (예산 중립 재배분).
            "tape_saturated": len(self.tape_saturated_ms),
            # 절대 임계를 못 넘어 비어 있던 tier3 정원을 상대 순위로 채운 횟수.
            "tier3_capacity_fills": int(self.counters.get("tier3_capacity_fills", 0)),
            "api_errors": int(self.counters.get("api_errors", 0)),
            # ↓ "조용히 죽거나 나빠지는" 사각을 드러내는 최소 집합 (워치독 5분 판독용).
            # 인증 실패(별도 노출), 광역 catch 로 삼켜지던 것들, 쓰기 실패, 수집 건강도.
            "auth_failures": int(self.counters.get("auth_failures", 0)),
            "loop_errors": int(self.counters.get("loop_errors", 0)),
            # ⚠️ 이 둘은 **반드시 따로** 읽어야 한다. `schema_mismatch` 는 "응답 모양이
            # 바뀌었으니 그날 데이터를 의심하라"이고, `symbol_not_found` 는 "상장폐지·거래정지
            # 종목을 건너뛰었다"로 정상 상태다. 예전에는 후자가 전자로 집계돼 멀쩡한 하루를
            # 의심하게 만들었다 (실측 5/5 건이 전자로 오분류).
            "schema_mismatch": int(self.counters.get("schema_mismatch", 0)),
            "symbol_not_found": int(self.counters.get("symbol_not_found", 0)),
            # 몇 종목에 몰렸나 — 1~2 종목에 반복되면 워치리스트 정리 후보,
            # 갑자기 전 종목으로 퍼지면 그때는 계약 변경을 의심해야 한다.
            "symbol_not_found_symbols": len(self.not_found.counts),
            "event_write_failures": int(self.counters.get("event_write_failures", 0)),
            "promotion_write_failures": int(self.counters.get("promotion_write_failures", 0)),
            "rankings_write_failures": int(self.counters.get("rankings_write_failures", 0)),
            "rankings_clamped": int(self.counters.get("rankings_clamped", 0)),
            "ranking_snap_age_s": ranking_snap_age_s,
            "prices_missing": miss,
            "fetch_success_pct": fetch_success_pct,
            "candles_1m": int(self.counters.get("candles_1m", 0)),
            # tier2 저빈도 호가 — 켜져 있으면 snaps 가 늘어야 하고, 예산 압박으로
            # 양보하면 skipped 가 는다 (조용한 미수집 방지, W5 워치독 판독용).
            "tier2_orderbook_snaps": int(self.counters.get("tier2_orderbook_snaps", 0)),
            "tier2_orderbook_skipped": int(
                self.counters.get("tier2_orderbook_skipped_rate", 0)
                + self.counters.get("tier2_orderbook_skipped_429", 0)),
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
        # 티어 전이는 개별 줄(DEBUG) 대신 **구간 요약**으로 사람에게 보인다.
        promo = int(self.counters.get("promotions", 0))
        demo = int(self.counters.get("demotions", 0))
        data["promotions_delta"] = promo - self._last_promotions
        data["demotions_delta"] = demo - self._last_demotions
        self._last_promotions, self._last_demotions = promo, demo
        self.notifier.info("telemetry " + " ".join(f"{k}={v}" for k, v in data.items())
                           + " | " + self.budget.describe())
        self._report_repeat_not_found()
        self._check_precision_drift()
        return data

    def _report_repeat_not_found(self) -> None:
        """같은 심볼이 반복해서 404 면 이름을 밝힌다 (워치리스트 정리 후보).

        카운트만으로는 "1종목이 40번" 과 "40종목이 1번씩" 을 구분할 수 없는데 둘은 뜻이
        완전히 다르다 — 전자는 그 종목만 빼면 되고, 후자는 계약 변경을 의심해야 한다.
        정리 자체는 하지 않는다(범위 밖). 반복이 없으면 한 줄도 내지 않는다.
        """
        repeats = self.not_found.repeat_symbols()
        if not repeats:
            return
        top = " ".join(f"{sym}={cnt}" for sym, cnt in repeats[:10])
        more = f" +{len(repeats) - 10} more" if len(repeats) > 10 else ""
        self.notifier.info(
            f"symbol-not-found repeats: {top}{more} "
            f"(delisted/halted candidates for watchlist cleanup; not a contract change)")

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
            self.bump("promotion_write_failures")      # 삼켜지던 사각 승격
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
        """워치리스트 등록 — **tier0 유니버스 게이트를 통과한 심볼만** (감사 F-2).

        이 게이트가 없으면 거래대금 랭킹 상위(정의상 메가캡)가 그대로 워치리스트가 되어
        tier2 정원을 잠식하고, tier3 후보는 tier2 에서만 나오므로 tier3 진입까지 봉쇄된다.
        걸러진 심볼은 심볼당 1회 로그를 남긴다 — 조용히 거르면 "왜 이 종목이 없지" 를
        나중에 추적할 수 없다.
        """
        sym = symbol.upper()
        if sym in self.watchlist:
            return False
        ok = self.universe_status.get(sym)
        if ok is not True:
            self.bump("watch_rejected_universe" if ok is False else "watch_unknown_universe")
            if sym not in self._universe_logged:
                self._universe_logged.add(sym)
                why = "tier0 필터 미통과" if ok is False else "메타 미확인"
                self.notifier.info(f"universe: not watching {sym} ({why})")
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


def _open_reader(ctx: CollectorContext):
    """`store` DB 의 읽기 전용 Reader. DB 가 없으면 None."""
    store_cfg = ctx.cfg.store
    if store_cfg is None:
        return None
    try:
        from ..store.reader import Reader

        return Reader(Path(store_cfg.db_path))
    except Exception:
        return None


def _seed_universe_from_db(ctx: CollectorContext) -> int:
    """`symbols` 테이블(build_universe 결과)을 유니버스 캐시 + 워치리스트 시드로 읽는다.

    감사 F-2: collector 는 지금까지 `symbols` 를 읽는 코드가 전혀 없었다 — 워치리스트
    출처가 CLI 인자와 랭킹 누적뿐이라 유니버스 빌드가 돌아도 소비되지 않았다.
    테이블의 행은 전부 tier0 통과분이므로 그대로 통과 캐시가 되고, tier1 이상은
    워치리스트 시드가 된다 (랭킹은 시드가 아니라 승격 트리거다 — docs/03 §1).
    """
    reader = _open_reader(ctx)
    if reader is None:
        return 0
    try:
        df = reader.symbols()
    except Exception:
        return 0
    finally:
        reader.close()
    if df is None or df.empty:
        return 0
    uni = ctx.cfg.require_universe()
    seeded = 0
    for row in df.itertuples():
        sym = str(row.symbol).upper()
        ctx.universe_status.setdefault(sym, True)
        shares = int(getattr(row, "shares_outstanding_qu", 0) or 0)
        if shares > 0:
            ctx.shares_out.setdefault(sym, shares)
        if int(getattr(row, "tier", 0) or 0) >= 1 and sym not in ctx.watchlist \
                and len(ctx.watchlist) < uni.tier1_max:
            ctx.watchlist.append(sym)
            seeded += 1
    if seeded:
        ctx.bump("watchlist_seeded_from_db", seeded)
        ctx.notifier.info(f"universe: seeded {seeded} tier1 symbols from the symbols table "
                          f"(watch={len(ctx.watchlist)})")
    return seeded


def _seed_event_suppression(ctx: CollectorContext) -> int:
    """최근 이틀치 `events` 를 검출기 억제 상태로 복원한다 (재시작 이어받기, 감사 F-3)."""
    reader = _open_reader(ctx)
    if reader is None:
        return 0
    try:
        now = ctx.clock.now_ms()
        df = reader.read_events(now - 2 * DAY_MS, now + DAY_MS)
    except Exception:
        return 0
    finally:
        reader.close()
    if df is None or df.empty:
        return 0
    for row in df.itertuples():
        ctx.detector.seed_suppression(str(row.symbol), int(row.t0_ms))
    ctx.bump("event_suppression_seeded", len(df))
    return len(df)


# --------------------------------------------------------------------------- #
# 공통 루프 골격
# --------------------------------------------------------------------------- #
async def _guarded(ctx: CollectorContext, name: str, coro,
                   group: str = GROUP_MARKET_DATA) -> bool:
    """루프 몸통 1회 실행. 치명(Forbidden·ForbiddenEndpoint)만 정지시키고 나머지는 삼킨다."""
    try:
        await coro
        return True
    except asyncio.CancelledError:
        raise
    except ForbiddenEndpoint as exc:
        # 계약 A6: allowlist 위반은 코드 버그이자 "주문 계열 엔드포인트에 도달했다"는
        # 신호다 — Phase 2 의 마지막 방어선. A6 이 TossApiError 상속을 끊어 광역
        # 핸들러에 잡히지 않게 했으므로, 아래 catch-all 이 warn 한 줄로 삼켜 수집이
        # 계속되는 일이 없도록 **여기서 명시적으로** 잡아 경보와 함께 전체를 세운다.
        # 조용히 죽지도(미처리 예외), 조용히 돌지도(catch-all) 않는다.
        ctx.bump("forbidden_endpoint")
        ctx.shutdown(f"{name}: ForbiddenEndpoint — 비허용(주문 계열) 엔드포인트 도달, "
                     f"수집 전체 중단 ({exc})")
        return False
    except Forbidden as exc:
        ctx.shutdown(f"{name}: Forbidden — IP 미등록/권한 문제로 수집 중단 ({exc})")
        return False
    except AuthExpired as exc:
        # 인증 실패를 **별도 카운터**로 노출한다 (사각 (i), 2026-08-01: 재발급이 env 부재로
        # 전부 실패했는데 api_errors=0 이라 외부에서 정상으로 보였다). AuthExpired 는
        # TossApiError 하위라 아래 광역 핸들러에 잡히면 api_errors 로 뭉개진다 — 여기서
        # 먼저 잡아 `AUTH-FAILURE` 로 로그(워치독 grep 문자열)하고 auth_failures 로 센다.
        ctx.bump("auth_failures")
        ctx.notifier.warn(f"{name}: AUTH-FAILURE (token expired/rejected) "
                          f"{type(exc).__name__}: {exc}")
        return False
    except SchemaMismatch as exc:
        # `client._classify` 는 재시도 불가한 4xx 를 전부 SchemaMismatch 로 올린다 — 그래서
        # 성격이 다른 두 가지가 한 카운터에 섞였다. `schema_mismatch` 는 "그날 데이터를
        # 믿지 마라"의 유일한 근거이므로 **없는 종목으로 묽어지면 안 된다** (mismatch.py 참조).
        # 실측(2026-07-31~08-04): 이 카운터가 오른 5건이 전부 상장폐지·거래정지였다.
        if is_symbol_not_found(exc.detail):
            ctx.bump("symbol_not_found")
            symbol = symbol_from_loop_name(name)
            seen = ctx.not_found.add(symbol) if symbol else 0
            # 반복 404 는 워치리스트에서 빼는 게 맞을 수 있으나 그 판단은 여기 범위 밖이다
            # (코디네이터 지시) — 몇 번째인지만 드러내 사람이 고를 수 있게 한다.
            repeat = f" (x{seen})" if seen > 1 else ""
            ctx.notifier.warn(f"{name}: symbol not found (skip){repeat}: "
                              f"delisted/halted/renamed, not a contract change")
            return False
        ctx.bump("schema_mismatch")
        ctx.notifier.warn(f"{name}: schema mismatch (skip): {exc}")
        return False
    except (TossApiError, OSError) as exc:
        ctx.bump("api_errors")
        ctx.notifier.warn(f"{name}: {type(exc).__name__}: {exc}")
        return False
    except Exception as exc:                       # 예상 못 한 예외로 루프가 죽으면 안 된다
        # 토큰 발급/리스 실패는 RuntimeError 로 올라온다 (W1 tokens.py: env 부재·리스 충돌).
        # 이것도 인증 사각이므로 auth_failures 로 승격한다 — 나머지만 loop_errors.
        msg = str(exc)
        if isinstance(exc, RuntimeError) and any(
                m in msg for m in ("TOSS_BASE_URL", "token", "lease")):
            ctx.bump("auth_failures")
            ctx.notifier.warn(f"{name}: AUTH-FAILURE (token issuance/lease) "
                              f"{type(exc).__name__}: {exc}")
        else:
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
        ctx.restore_budget()
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


#: SQLite INTEGER(int64) 상한. 애프터 세션 랭킹 페이로드의 마이크로 단위 필드가
#: 이걸 넘는 것이 라이브에서 실측됐다 (2026-08-01 05:00~ 애프터 전환 직후 전 폴 유실).
SQLITE_INT_MAX = 2 ** 63 - 1

_RANKING_INT_FIELDS = ("last_u", "base_u", "vol_qu", "amount_u")


def _clamp_ranking_page(ctx: CollectorContext, page: RankingPage) -> RankingPage:
    """int64 초과 필드를 **행 단위로** 클램프한 페이지를 돌려준다 (핫픽스 2026-08-01).

    rankings_snap 컬럼은 전부 NOT NULL 이라 NULL 은 불가 — ±(2^63-1) 로 클램프한다.
    클램프된 행은 값 자체(정확히 9223372036854775807)가 표식이며, 심볼·필드·원값을
    warn 로그와 `rankings_clamped` 카운터로 남긴다. 저장(executemany 단일 트랜잭션)뿐
    아니라 RankingBuffer.frame() 의 int64 astype 도 같은 값에 죽으므로 저장·버퍼·트리거
    **전에** 한 번 지나야 한다.
    """
    rows = list(page.rows)
    dirty = False
    for i, row in enumerate(rows):
        over = {name: int(getattr(row, name)) for name in _RANKING_INT_FIELDS
                if getattr(row, name) is not None
                and abs(int(getattr(row, name))) > SQLITE_INT_MAX}
        if not over:
            continue
        dirty = True
        rows[i] = replace(row, **{name: (SQLITE_INT_MAX if v > 0 else -SQLITE_INT_MAX)
                                  for name, v in over.items()})
        ctx.bump("rankings_clamped", len(over))
        detail = ", ".join(f"{name}={v}" for name, v in sorted(over.items()))
        ctx.notifier.warn(
            f"rankings clamp {page.ranking_type} rank={row.rank} {row.symbol}: "
            f"{detail} > int64 max — {SQLITE_INT_MAX} 로 클램프해 저장")
    if not dirty:
        return page
    return replace(page, rows=rows)


async def rankings_once(ctx: CollectorContext) -> int:
    """랭킹 4종 스냅샷 → DB + 실시간 버퍼 + 유니버스 판정 + 승격 트리거."""
    stored = 0
    watch = set(ctx.watchlist)
    for rtype in RANKING_TYPES:
        page = await ctx.client.get_rankings(rtype, duration="realtime", market="US",
                                             count=100)
        ctx.after_call(GROUP_RANKING)
        # snap_ms 는 우리 관측 시각. rankedAt 은 12~23초 뒤처지므로 참고값으로만 쓴다.
        snap_ms = ctx.clock.now_ms()
        page = _clamp_ranking_page(ctx, page)
        try:
            stored += ctx.store.insert_rankings(snap_ms, page)
        except Exception as exc:                # 저장 실패가 이 폴의 나머지를 죽이면 안 된다
            # 랭킹은 과거 조회가 불가능한 유일한 데이터 — 유실은 카운터로 반드시 드러낸다
            # (docs/11 §11-1: 이번 사고는 api_errors=0 인 채 110분을 조용히 샜다).
            ctx.bump("rankings_write_failures")
            ctx.notifier.warn(f"rankings store failed ({page.ranking_type}): "
                              f"{type(exc).__name__}: {exc} — 이 폴 분량은 복구 불가 유실")
        ctx.rankings.add(snap_ms, page, keep=watch)
        # 워치리스트 후보(상위권)의 tier0 판정을 트리거 **이전에** 확정한다 (감사 F-2).
        await _resolve_universe(
            ctx, [(row.symbol, int(row.last_u)) for row in page.rows
                  if row.rank <= RANKING_PROMOTE_TOP], snap_ms)
        _ranking_triggers(ctx, page, snap_ms)
        ctx.bump("ranking_snaps")
    await _resolve_watchlist_unknowns(ctx)
    ctx.rankings.prune(ctx.clock.now_ms(), keep=set(ctx.watchlist) | set(ctx.buffers))
    return stored


async def _resolve_universe(ctx: CollectorContext,
                            candidates: Sequence[tuple[str, int]], snap_ms: int) -> None:
    """(symbol, last_u) 후보의 tier0 통과 여부를 `/stocks` 배치로 확정해 캐시한다.

    가격은 후보가 들고 온 관측치(랭킹 행·`/prices`)를 쓰고, 시총 분모(발행주식수)만
    `/stocks` 에서 받는다. 판정은 심볼당 1회 캐시되므로 정상 상태에서 추가 호출은 0이다.
    미통과 심볼이 이미 워치리스트에 있으면(pinned 제외) **내리면서 경고**한다 —
    구 상태파일에서 복원된 대형주가 조용히 예산을 먹는 것을 여기서 끊는다.
    """
    todo = [(s.upper(), int(last_u)) for s, last_u in candidates
            if s.upper() not in ctx.universe_status]
    if not todo:
        return
    get_stocks = getattr(ctx.client, "get_stocks", None)
    if get_stocks is None:
        return                       # 메타를 줄 수 없는 클라이언트 — 미확인으로 남는다(등록 거부)
    metas: list[StockMeta] = await get_stocks([s for s, _ in todo])
    ctx.after_call(GROUP_STOCK, calls=max(1, math.ceil(len(todo) / BATCH_MAX)))
    by_symbol = {m.symbol.upper(): m for m in metas}
    uni = ctx.cfg.require_universe()
    for sym, last_u in todo:
        meta = by_symbol.get(sym)
        if meta is None:
            # 함정1: 미존재 심볼은 200 + 조용한 누락. 통과로 둘 수는 없다.
            ctx.universe_status[sym] = False
            ctx.bump("universe_meta_missing")
        else:
            price = Price(symbol=sym, ts_ms=int(snap_ms), last_u=int(last_u))
            ok = bool(passes_tier0(meta, price, uni))
            ctx.universe_status[sym] = ok
            if ok:
                ctx.shares_out.setdefault(sym, int(meta.shares_outstanding_qu))
        if ctx.universe_status[sym]:
            continue
        ctx.bump("universe_rejected")
        if sym not in ctx._universe_logged:
            ctx._universe_logged.add(sym)
            cap = (market_cap_u(meta, Price(symbol=sym, ts_ms=None, last_u=int(last_u)))
                   if meta is not None else None)
            ctx.notifier.info(
                f"universe: {sym} rejected at tier0 (last_u={last_u} "
                f"mcap_u={cap if cap is not None else 'unknown'})")
        if sym in ctx.watchlist and sym not in ctx.pinned:
            ctx.notifier.warn(f"universe: dropping {sym} from watchlist — tier0 미통과 "
                              "(구 상태 복원분 정리)")
            ctx.unwatch(sym)


async def _resolve_watchlist_unknowns(ctx: CollectorContext) -> None:
    """유니버스 판정이 없는 워치리스트 심볼(주로 구 상태파일 복원분)을 확정한다."""
    unknowns = [s for s in ctx.watchlist if s not in ctx.universe_status]
    if not unknowns:
        return
    get_prices = getattr(ctx.client, "get_prices", None)
    if get_prices is None or getattr(ctx.client, "get_stocks", None) is None:
        return
    for i in range(0, len(unknowns), BATCH_MAX):
        chunk = unknowns[i:i + BATCH_MAX]
        prices: list[Price] = await get_prices(chunk)
        ctx.after_call(GROUP_MARKET_DATA)
        await _resolve_universe(ctx, [(p.symbol, int(p.last_u)) for p in prices],
                                ctx.clock.now_ms())
        # 응답에서 빠진 심볼(함정1)은 미확인으로 남는다 — tier1 스윕의 missing_streak 이 처리한다.


def _ranking_triggers(ctx: CollectorContext, page: RankingPage, snap_ms: int) -> None:
    """랭킹 상위 진입 = 승격 트리거 (docs/03 §1). 신규 심볼은 워치리스트에 누적한다.

    유니버스 게이트는 `ctx.watch()` 안에 있다 — 등록되지 못한 심볼은 승격도 하지 않는다.
    승격 점수는 0.0 이다 (감사 H-7): 랭킹은 "볼 이유"이지 유망도가 아니다. 랭킹 유래
    점수(0.50~0.77)를 스코어 채널에 섞으면 실제 스코어(0.3~0.6)를 항상 이겨 정원이 찼을 때
    진짜 표적을 축출한다. 0.0 이면 정원이 찼을 때 기존 멤버를 밀어내지 못하고(진입만 허용),
    자리가 있으면 들어가서 실제 봉 데이터로 스코어를 증명해야 남는다.
    """
    toss = page.ranking_type.startswith("TOSS_SECURITIES")
    for row in page.rows:
        if row.rank > RANKING_PROMOTE_TOP:
            continue
        sym = row.symbol.upper()
        ctx.watch(sym)
        if sym not in ctx.watchlist:
            continue                     # 유니버스 게이트 또는 tier1 정원에 걸렸다
        if toss and ctx.tiers.tier_of(sym) < 2:
            # 랭킹 진입도 고정 진입 점수로 **경쟁**한다 — 유지선 아래 점유자만 밀어낸다.
            if ctx.tiers.force(sym, 2, "ranking_entry", ACTIVITY_ENTRY_SCORE,
                               snap_ms, record_score=0.0) is not None:
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
    ctx.bump("prices_seen", len(returned))              # 조회 성공률 지표 (데이터 건강도)
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
    # 활동 신호는 **고정 진입 점수(ACTIVITY_ENTRY_SCORE)** 로 경쟁한다.
    #
    # 원래 결함은 "경쟁" 자체가 아니라 **포화된 점수**였다: activity_score 는 정상 거래
    # 종목이면 거의 1.000 이라 0.3~0.6 짜리 진짜 표적을 매 스윕 밀어냈다(8/03 요동).
    # 그렇다고 빈자리만 쓰게 하면(compete=False) tier2 가 닫힌 집합이 되고, tier2 는
    # tier3 의 유일한 진입로라 tier3 가 말라죽는다(8/04 실측: evicted=0, tier3 10->3).
    # 고정 점수는 유지선(0.22) 아래 점유자만 재활용하고, 활동끼리는 동점이라 진동하지
    # 않는다. 원래 활동 강도는 record_score 로 promotions 테이블에 그대로 남긴다.
    if ctx.tiers.force(price.symbol, 2, reason, ACTIVITY_ENTRY_SCORE, now_ms,
                       record_score=score) is not None:
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
    """새 봉 기준으로 스코어 + 이벤트. 피처·이벤트는 전부 analysis 재사용이다.

    이벤트 판정은 **현재 매매일로 제한**한다 (감사 F-3): 버퍼에는 최대 4일이 남아 있고
    검출기는 매 사이클 전체를 재스캔하므로, 제한이 없으면 전일 이벤트가 당일 거래량이
    섞인 곡선(그 t0 기준 미래)으로 재판정되어 rvol 라벨이 무너지고 UPSERT 가 깨끗한
    기록을 덮어쓴다. 전일 이벤트는 이미 기록됐다 — 다시 판정할 이유가 없다.
    """
    buf = ctx.buffers.get(symbol)
    if buf is None or not len(buf):
        return
    df = buf.frame()
    now = ctx.clock.now_ms()
    md = ctx.scheduler.market_day_at(now) or ctx.scheduler.today()
    bounds = trading_day_of(md) if md is not None else None
    result = ctx.detector.evaluate(
        symbol, df, rankings=ctx.rankings.frame(symbol), calendar=ctx.calendar_list(),
        curve=_curve_for(ctx, symbol, df, now), baseline=ctx.baselines.get(symbol),
        shares_outstanding_qu=ctx.shares_out.get(symbol),
        prev_close_u=ctx.prev_close.get(symbol), now_ms=now,
        detect_from_ms=bounds[0] if bounds is not None else None)
    if result is None:
        return
    ctx.tiers.on_new_data(symbol, result.score, result.ts_ms, reason=result.path)
    ctx.flush_changes()
    for emission in result.events:
        try:
            ctx.store.record_event(emission.record)
        except Exception as exc:
            # 삼켜지던 사각 승격 (사고 분류): 이벤트 쓰기 실패도 카운터로 드러낸다.
            ctx.bump("event_write_failures")
            ctx.notifier.warn(f"record_event failed for {symbol}: "
                              f"{type(exc).__name__}: {exc}")
            continue
        ctx.bump("events" if emission.is_new else "event_updates")
        ctx.announce_event(emission, result)


def _curve_for(ctx: CollectorContext, symbol: str, df: pd.DataFrame, now_ms: int):
    """시간대 보정 RVOL 분모. **당일은 제외**한다 (자기오염 방지 — W3 인수인계 §4).

    실패(None)는 성공 TTL(1시간)로 캐시하지 않는다 (감사 C-1): 승격 직후에는 버퍼에
    당일 봉뿐이라 곡선이 자주 None 인데, 그걸 1시간 고착시키면 백필로 이력이 생긴 뒤에도
    RVOL 게이트가 꺼진 채(`rvol_gated=False`) 가격 조건만으로 이벤트가 기록된다.
    """
    cached = ctx.curves.get(symbol)
    if cached is not None:
        ttl = CURVE_TTL_MS if cached[1] is not None else CURVE_NONE_TTL_MS
        if now_ms - cached[0] < ttl:
            return cached[1]
    calendar = ctx.calendar_list()
    today = ctx.scheduler.market_day_at(now_ms) or ctx.scheduler.today()
    exclude = (today.date,) if today is not None else ()
    curve = build_curve(df, calendar, exclude_dates=exclude)
    ctx.curves[symbol] = (now_ms, curve)
    return curve


def _baseline_spread_offset_ms(symbol: str) -> int:
    """심볼별 결정적 분산 오프셋 — 같은 심볼은 항상 같은 위치라 재시작에도 안정적이다."""
    digest = hashlib.sha1(symbol.encode("utf-8")).hexdigest()[:8]
    # 1초 이상으로 잡아 **전환 시각에 동시에 터지는 심볼이 없게** 한다.
    return (int(digest, 16) % BASELINE_REFRESH_SPREAD_S + 1) * 1000


def _baseline_due(ctx: CollectorContext, symbol: str, now_ms: int) -> bool:
    """지금 이 심볼의 일봉 베이스라인을 (재)계산해야 하는가."""
    if ctx.baselines.get(symbol) is None:
        return True                                   # 아예 없으면 즉시 필요하다
    if ctx.baseline_ms.get(symbol, 0) > ctx.baseline_epoch_ms:
        return False                                  # 이번 에포크 **이후**에 갱신됐다
    return now_ms >= ctx.baseline_epoch_ms + _baseline_spread_offset_ms(symbol)


async def _ensure_history(ctx: CollectorContext, symbol: str) -> None:
    """승격 직후 1회: DB(백필 결과) → API 백필로 재개 지점까지 연결 → 일봉 베이스라인.

    감사 H-9: 예전에는 DB 에서 `CANDLE_PAGE` 이상 읽으면 백필을 통째로 건너뛰었다 —
    그 분기에서는 `resume_point_ms` 가 조회조차 되지 않아, 정전이 실시간 폴링의 커버
    범위(최신 200봉)를 넘으면 `candles_1m` 에 **영구적이고 조용한 구멍**이 남았다.
    지금은 이어받기 지점이 있으면 정전 폭만큼 페이지를 늘려 **항상** 잇고,
    상한 안에서 못 닿으면 `_backfill_1m` 이 경고를 남긴다 (탐지가 우선이다 —
    1분봉은 수백 일 보관되므로 구멍은 알기만 하면 나중에 메울 수 있다).
    """
    buf = ctx.buffer(symbol)
    now = ctx.clock.now_ms()
    need_baseline = _baseline_due(ctx, symbol, now)
    if not need_baseline and len(buf) > 0:
        return
    if not len(buf):
        loaded = _load_history_from_db(ctx, symbol, now)
        ctx.bump("history_from_db", 1 if loaded else 0)
        resume = ctx.resume_point_ms(symbol)
        gap_bars = None if resume is None else max(0, (now - int(resume)) // MIN_MS)
        if loaded >= CANDLE_PAGE and gap_bars is not None and gap_bars < CANDLE_PAGE:
            # 이력이 충분하고 공백이 한 페이지 미만 — 바로 뒤의 실시간 폴링(count=200)이
            # 그 구간을 덮으므로 백필이 필요 없다. (한 페이지를 넘는 공백은 실시간 폴링이
            # 영원히 못 덮는다 — 그게 감사 H-9 의 구멍이었다.)
            pass
        else:
            pages = MAX_BACKFILL_PAGES
            if gap_bars is not None:
                # 최신 페이지가 now 에 정렬되므로 +1 페이지 여유를 둔다.
                pages = min(max(MAX_BACKFILL_PAGES, gap_bars // CANDLE_PAGE + 1),
                            RESUME_BACKFILL_MAX_PAGES)
            await _backfill_1m(ctx, symbol, pages=pages, stop_at_ms=resume)
    if need_baseline:
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

    감사 H-9: `stop_at_ms` 가 있는데 페이지 상한/이력 끝 때문에 거기 못 닿고 끝나면
    `candles_1m` 에 구멍이 남는 것이다 — **조용히 끝내지 않고 반드시 경고한다.**
    """
    before: int | None = None
    total = 0
    buf = ctx.buffer(symbol)
    oldest: int | None = None
    reached = stop_at_ms is None
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
            reached = True
            break                                        # 마지막 수집 지점까지 메웠다
        if page.next_before_ms is None:
            break
        before = int(page.next_before_ms) - 1
    ctx.bump("backfill_bars", total)
    if not reached:
        ctx.bump("backfill_gaps")
        ctx.notifier.warn(
            f"backfill gap {symbol}: oldest fetched={oldest} did not reach resume point "
            f"{stop_at_ms} — candles_1m 에 구멍이 남았다 (1분봉 보관기간 내 수동 백필로 "
            "메울 수 있다)")
    return total


def _raw_prev_close(ctx: CollectorContext, symbol: str, now_ms: int) -> int | None:
    """전일 **정규장 마지막 1분봉(원주가)** 종가. 없으면 None.

    당일 조건 판정(`close/prev_close - 1 >= day_ret_min`)의 분모다. 당일 1분봉은 원주가
    (A5)인데, 예전에는 이 분모를 **일봉(수정주가) 종가**로 채워 §2.3 이 금지한 원주가/
    수정주가 혼합이 라이브에서 일어났다 — 분할이 있었던 종목(RECT 등)에서 전일 대비
    가짜 갭이 생겨 day 트리거가 오탐한다. 원주가 1분봉끼리 비교하도록 전일 정규장
    마지막 봉 종가를 쓴다. 버퍼에 전일이 없으면 None 을 반환해 detect_events 의 원주가
    폴백(labeling.py: 프레임 직전 봉 종가 → 당일 첫 봉 시가)에 맡긴다 — 어느 경로든 원주가다.
    """
    buf = ctx.buffers.get(symbol)
    if buf is None or not len(buf):
        return None
    md_today = ctx.scheduler.market_day_at(now_ms) or ctx.scheduler.today()
    tb = trading_day_of(md_today) if md_today is not None else None
    if tb is None:
        return None
    today_start = tb[0]
    prev_md = None
    prev_end = None
    for md in ctx.calendar_list():                      # 시간순
        b = trading_day_of(md)
        if b is None or b[1] > today_start:             # 당일 이상은 제외
            continue
        if prev_end is None or b[1] > prev_end:
            prev_md, prev_end = md, b[1]
    if prev_md is None or prev_md.regular is None:
        return None
    reg = prev_md.regular
    df = buf.frame()
    ts = df["ts_ms"].to_numpy()
    mask = (ts >= int(reg.start_ms)) & (ts < int(reg.end_ms))
    closes = df["close_u"].to_numpy()[mask]
    if closes.size == 0:
        return None
    return int(closes[-1])


async def _refresh_baseline(ctx: CollectorContext, symbol: str, now_ms: int) -> None:
    """일봉 베이스라인 + 전일종가. 함정4: **당일 봉은 진행형**이라 완성봉으로 쓰면 안 된다.

    일봉(수정주가)은 ADV20·ATR20 등 **다일 계산**에만 쓰고, 당일 조건 분모(prev_close)는
    원주가 1분봉에서 뽑는다 (`_raw_prev_close`) — 계열 혼합 금지 (§2.3, 라이브 오탐 방지).
    """
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
    ctx.baseline_ms[symbol] = now_ms                  # 분산 재계산 기준점
    raw_pc = _raw_prev_close(ctx, symbol, now_ms)
    if raw_pc is not None:
        ctx.prev_close[symbol] = raw_pc                 # 원주가 전일 정규장 마지막 종가
    else:
        # 원주가 전일종가를 못 구하면 수정주가 종가를 **쓰지 않는다** — 혼합 대신
        # 미설정으로 두어 detect_events 의 원주가 폴백에 맡긴다 (라이브 오탐 방지).
        ctx.prev_close.pop(symbol, None)


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
        # 빈 tier3 정원을 측정된 상위 후보로 채운다 — 빈 슬롯은 순손실이다
        # (절대 임계 0.60 은 개장 직후에만 넘어서, 그 뒤 장 내내 정원이 비어 있었다).
        if ctx.collecting():
            filled = ctx.tiers.fill_to_capacity(3, ctx.clock.now_ms())
            if filled:
                ctx.bump("tier3_capacity_fills", len(filled))
                ctx.flush_changes()
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

        # 포화 종목은 절반 주기, 그만큼 한산한 종목을 늦춰 **총 호출률은 그대로** 둔다.
        saturated = {s for s, t in ctx.tape_saturated_ms.items()
                     if now - t <= TAPE_SATURATION_TTL_MS}
        for stale_sym in [s for s in ctx.tape_saturated_ms
                          if now - ctx.tape_saturated_ms[s] > TAPE_SATURATION_TTL_MS]:
            ctx.tape_saturated_ms.pop(stale_sym, None)
        intervals = tier3_trades_intervals(members, saturated, trades_s)

        ready = sorted((k for k, t in due.items() if t <= now), key=lambda k: due[k])
        for symbol, what in ready:
            if what == "trades":
                await _guarded(ctx, f"tier3:trades:{symbol}", _poll_trades(ctx, symbol),
                               GROUP_MARKET_DATA)
                due[(symbol, what)] = ctx.clock.now_ms() + int(
                    intervals.get(symbol, trades_s) * 1000)
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
        # 50건 상한에 걸린 채 구간이 비면 **폴링이 체결 속도를 못 따라간 것**이다
        # (2026-08-03 실측: 결손 190건 전부 n=50, ZEO/FUSE/CIGL/PUSA 4종목 집중).
        # 그 종목만 적응형으로 더 자주 본다 — 예산은 tier3_trades_intervals 가 지킨다.
        if int(stats["n"]) >= TRADES_COUNT:
            ctx.tape_saturated_ms[symbol] = ctx.clock.now_ms()
            ctx.bump("tape_saturated_polls")
        ctx.notifier.warn(
            f"tape gap {symbol}: prev_max={prev_max} < this_min={stats['min_ts_ms']} "
            f"(n={stats['n']}) — 표본 사이 체결 누락")
    ctx.last_trade_ms[symbol] = max(prev_max or 0, int(stats["max_ts_ms"]))
    return stored


def tier3_trades_intervals(members: Sequence[str], saturated: set[str], base_s: float,
                           *, fast_max: int = TAPE_FAST_LANE_MAX,
                           slow_max_mult: float = TAPE_SLOW_MAX_MULT) -> dict[str, float]:
    """심볼별 `/trades` 폴링 주기(초). **총 호출률은 기존과 같다 (예산 중립).**

    포화 종목(50건 상한 + 구간 결손)은 절반 주기로 자주 보고, 그만큼을 한산한 종목에서
    **되돌려 받는다** — 예산을 더 쓰지 않고 밀도를 필요한 곳으로 옮기는 재배분이다.
    한산한 종목은 n<50 이라 이미 전 체결을 받고 있으므로 주기를 늦춰도 잃는 것이 없다.

    총 허용률 = `len(members)/base_s` (지금 계획과 동일). 이 상한을 만족할 때까지
    빠른 레인 인원을 줄이므로, 어떤 입력에도 예산을 넘지 않는다.
    """
    syms = list(members)
    if not syms or base_s <= 0:
        return {}
    budget = len(syms) / base_s                   # 유지할 총 req/s
    fast_s = base_s / 2.0
    cand = [s for s in syms if s in saturated]
    n_fast = min(len(cand), fast_max, len(syms))
    slow_s = base_s
    while n_fast > 0:
        n_slow = len(syms) - n_fast
        rate_fast = n_fast / fast_s
        if n_slow == 0:
            if rate_fast <= budget:
                break
            n_fast -= 1
            continue
        rate_slow = budget - rate_fast
        if rate_slow > 0:
            candidate_slow = n_slow / rate_slow
            if candidate_slow <= base_s * slow_max_mult:
                slow_s = candidate_slow
                break
        n_fast -= 1                                # 예산·한산주기 상한을 못 지키면 축소
    fast = set(cand[:n_fast])
    if not fast:
        return {s: base_s for s in syms}
    return {s: (fast_s if s in fast else slow_s) for s in syms}


async def _poll_orderbook(ctx: CollectorContext, symbol: str, *,
                          tier2: bool = False) -> int:
    ob = await ctx.client.get_orderbook(symbol)
    ctx.after_call(GROUP_MARKET_DATA)
    snap_ms = ctx.clock.now_ms()
    ctx.store.insert_orderbook(snap_ms, ob)
    ctx.bump("orderbook_snaps")
    if tier2:
        ctx.bump("tier2_orderbook_snaps")
    return 1


# --------------------------------------------------------------------------- #
# Tier 2 — 저빈도 호가 (승격 전후 스프레드 궤적. 여유가 있을 때만)
# --------------------------------------------------------------------------- #
def tier2_orderbook_allowed(ctx: CollectorContext) -> tuple[bool, str]:
    """지금 tier2 호가를 한 건 쏴도 되는가. (허용?, 거절 사유) 를 돌려준다.

    tier2 호가는 **가장 먼저 희생되는** 수집이다 — 예산 압박(측정 사용률이 목표의
    `TIER2_ORDERBOOK_HEADROOM` 초과)이나 429 직후 쿨다운이면 건너뛴다. 스킵은 조용히
    지나가지 않고 카운터로 드러난다 (`tier2_orderbook_skipped_*`).
    """
    now = ctx.clock.now_ms()
    seen429 = int(ctx.budget.rate_limited.get(GROUP_MARKET_DATA, 0))
    if seen429 > ctx._tier2_book_429_seen:
        ctx._tier2_book_429_seen = seen429
        ctx._tier2_book_cooldown_ms = now + TIER2_ORDERBOOK_COOLDOWN_MS
    if now < ctx._tier2_book_cooldown_ms:
        return False, "429"
    target = ctx.budget.target(GROUP_MARKET_DATA)
    if target > 0 and ctx.budget.measured_rate(GROUP_MARKET_DATA) >= \
            target * TIER2_ORDERBOOK_HEADROOM:
        return False, "rate"
    return True, ""


async def run_tier2_orderbook(client: TossClient, store: Store, cfg: Config, *,
                              ctx: CollectorContext | None = None,
                              cycles: int | None = None) -> None:
    """tier2 멤버를 라운드로빈으로 돌며 심볼당 `tier2_orderbook_s` 주기로 호가 1건.

    왜 필요한가 (W5 에스컬레이션): 호가가 tier3 승격 **이후**에만 수집돼 승격 전후
    스프레드 궤적을 원리상 측정할 수 없었다 — "유동성이 몰릴 때 스프레드가 좁아지는가"
    (진입창 설계)와 주문 크기별 비용(감사 A3)이 여기 걸려 있다.

    tier3 멤버는 제외한다 (`members(2)`) — 이미 `tier3_orderbook_s`(16s)로 조밀하게
    받고 있어 중복 호출이 될 뿐이다. 주기가 0/미설정이면 루프 자체가 즉시 끝난다.
    """
    ctx = ctx or CollectorContext.create(client, store, cfg)
    period = float(getattr(cfg.require_polling(), "tier2_orderbook_s", 0) or 0)
    disabled = period <= 0                       # 설정 한 줄로 되돌린다
    if disabled:
        ctx.notifier.info("tier2 orderbook: disabled (polling.tier2_orderbook_s=0)")
    cursor = 0
    done = 0
    while ctx.running() and (cycles is None or done < cycles):
        done += 1
        if disabled:
            # ⚠️ 여기서 return 하면 안 된다 — run_all 이 FIRST_COMPLETED 로 기다리므로
            # 이 task 가 끝나면 **수집 전체가 내려간다.** 비활성일 때는 쉬기만 한다.
            await ctx.clock.sleep(IDLE_SLEEP_S)
            continue
        symbols = sorted(ctx.tiers.members(2))   # tier3 는 자기 루프가 조밀하게 본다
        if not ctx.collecting() or not symbols:
            await ctx.clock.sleep(IDLE_SLEEP_S)
            continue
        cursor %= len(symbols)
        symbol = symbols[cursor]
        cursor += 1
        ok, why = tier2_orderbook_allowed(ctx)
        if ok:
            await _guarded(ctx, f"tier2book:{symbol}",
                           _poll_orderbook(ctx, symbol, tier2=True), GROUP_MARKET_DATA)
        else:
            ctx.bump(f"tier2_orderbook_skipped_{why}")
        await ctx.clock.sleep(period / len(symbols))


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
    ctx.session_caps = {2: caps["tier2_max"], 3: caps["tier3_max"]}   # 회복의 천장
    changes = ctx.tiers.set_capacity(ts_ms=ctx.clock.now_ms(), reason="session_change",
                                     **caps)
    ctx.flush_changes()
    ctx.curves.clear()                                  # 날이 바뀌면 곡선도 다시 만든다
    # 감사 H-6: baselines/prev_close/history_days 는 심볼당 1회만 계산되고 아무도 지우지
    # 않았다 — 승격 시점의 ADV20·전일종가가 프로세스 수명 내내 얼어붙어, 2일차부터
    # `day` 트리거의 분모가 틀린 날의 종가가 된다. 세션 전환마다 무효화해 재계산시킨다
    # (`_ensure_history`/`_session_tick` 이 다음 사이클에 자연히 다시 채운다).
    # 감사 H-6 의 무효화는 유지하되 **한꺼번에 지우지 않는다** — 에포크만 올리고 심볼별로
    # 흩어진 시각에 재계산한다 (`_baseline_due`). 전환 직후 일봉 호출 버스트를 없앤다.
    ctx.baseline_epoch_ms = ctx.clock.now_ms()
    ctx.prev_close.clear()
    ctx.history_days = []
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
        # 비활성(주기 0/미설정)이면 즉시 끝나는 task 다 — 켜져 있을 때만 일한다.
        asyncio.create_task(run_tier2_orderbook(ctx.client, ctx.store, cfg, ctx=ctx,
                                                cycles=cycles), name="tier2book"),
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
    "run_session_watch", "run_tier1_price_sweep", "run_tier2_candles",
    "run_tier2_orderbook", "run_tier3_micro", "tier2_orderbook_allowed",
    "tape_stats", "tier1_sweep_once", "tier2_symbol_once",
    "tier3_trades_intervals",
]

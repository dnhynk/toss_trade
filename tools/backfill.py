"""대량 과거 백필 러너 — 사전등록 §2.8 집행. 소유: W4.

    python tools/backfill.py --estimate            # 호출량·소요시간 추정 (API 호출 없음)
    python tools/backfill.py                       # 스크린 -> 백필 -> 매니페스트
    python tools/backfill.py --symbols SNTI,BTAI   # 유니버스 명시 (기본: symbols 테이블)

무엇을 하는가 (docs/12_preregistration.md §2.8 확정 문언 — 임의 변경 금지)
    1. 스크린: 일봉(수정주가), 직전 20 매매일 통계로
       ① 당일 종가 수익률 >= +15%  ② 일봉 로그거래량 z >= 3  ③ 당일 레인지(H/L-1) >= 20%
       중 하나라도 충족하는 (심볼, 매매일) 이 백필 후보다.
    2. 백필: 후보별 [D-25, D+2] **매매일** 구간의 1분봉을 **원주가(adjusted=false)** 로
       수집·저장한다. 매매일 산정은 /market-calendar/US (§2.5). 겹치는 창은 병합한다.
    3. 매니페스트(§8 산출물 1): 심볼별 실보관 깊이(가정 말고 **프로브** — 페이지네이션이
       실제로 닿은 가장 오래된 봉), 요청·수집 구간, `adjusted=false` 호출 파라미터 증거
       (§7-e 대체 안전망 ①), 심볼 디렉토리 스냅샷 날짜, 스크린 판정 근거 카운트,
       실패·스킵 사유별 카운트.

왜 서두르는가 (§6.1): 소형주 1분봉 보관은 ~320일이고 매일 하루씩 사라진다 —
백필 지연은 훈련 기간의 **영구 손실**이다.

홀드아웃 취급 (§6.2 준수 방식)
    수집·저장은 전 구간 한다 (§6.2 는 '열람' 금지지 수집 금지가 아니다). 그러나
    홀드아웃(2026-05-01 ~ 2026-07-29) 매매일에 대해서는 stdout·로그·매니페스트 어디에도
    가격·수익률·z 값·판정 근거를 내지 않는다 — 커버리지(날짜·봉 수)만 허용.
    스크린 근거 카운트는 훈련·검증 구간만 집계하고, 홀드아웃은 후보 일수만 센다.

예산·안전
    - 호출은 전부 W1 의 TossClient + GroupRateLimiter 를 그대로 지난다 (우회 금지, 계약 C-4).
    - 재시작 안전: 체크포인트(checkpoint.json)에 스크린 결과·완료 창·캘린더 캐시를 남기고,
      재실행 시 이미 받은 구간을 건너뛴다. 저장은 upsert 라 멱등이다.
    - 라이브는 이중 잠금: 설정이 live 라도 --allow-live 플래그 없이는 기동을 거부한다.
      (라이브 실행은 코디네이터 승인 후 — 계약 C-9, A6 전환 규칙.)

우아한 정지 (`--stop-file PATH`)
    지정 경로에 파일이 나타나면 다음 경계(심볼 스크린 사이·창 사이·1분봉 페이지 사이)에서
    체크포인트를 저장하고 종료코드 4 로 끝난다. 슈퍼바이저(tools/backfill_supervisor.py)가
    이 채널로 정상 정지를 전달한다 — 강제 킬과 달리 진행 중 창의 페이지 진행까지 남는다.

종료코드: 0 완료, 2 기동 거부, 3 Forbidden/ForbiddenEndpoint 치명, 4 stop-file 정지, 130 SIGINT.

콘솔 출력은 ASCII 전용이다 (Windows cp949 콘솔 안전). 파일 출력은 UTF-8.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:                     # tools/ 는 패키지가 아니다
    sys.path.insert(0, str(REPO_ROOT))

from tossmon.api.client import TossClient  # noqa: E402
from tossmon.api.errors import Forbidden, ForbiddenEndpoint, TossApiError  # noqa: E402
from tossmon.api.limiter import GroupRateLimiter  # noqa: E402
from tossmon.api.models import SessionWindow, UsMarketDay  # noqa: E402
from tossmon.api.tokens import TokenManager  # noqa: E402
from tossmon.config import Config, load_config  # noqa: E402
from tossmon.store.writer import Store  # noqa: E402

MIN_MS = 60_000
DAY_MS = 86_400_000
PAGE = 200

# ---------------------------------------------------------------------------
# §2.8 얼린 값 — 사전등록 개정 절차(§9) 없이 바꾸지 않는다. CLI 로도 열지 않는다.
# ---------------------------------------------------------------------------
SCREEN_RET_MIN = 0.15            # ① 당일 종가 수익률 >= +15%
SCREEN_LOGVOL_Z_MIN = 3.0        # ② 일봉 로그거래량 z >= 3
SCREEN_RANGE_MIN = 0.20          # ③ 당일 레인지 H/L - 1 >= 20%
#: 비율 판정은 마이크로달러 정수 교차곱으로 한다 — float 나눗셈은 정확히 +15.0% 인 날을
#: 1 ULP 오차로 떨어뜨린다 (경계 포함이 §2.8 문언이다).
_RET_MIN_BP = round(SCREEN_RET_MIN * 10_000)       # 1500
_RANGE_MIN_BP = round(SCREEN_RANGE_MIN * 10_000)   # 2000
SCREEN_LOOKBACK = 20             # 직전 20 매매일 통계
WINDOW_BACK = 25                 # [D-25 매매일,
WINDOW_FWD = 2                   #  D+2 매매일]

#: 계약 A5 / §7-e: 스크린 일봉은 수정주가, 백필 1분봉은 **원주가**.
#: 이 상수를 바꾸면 기동 시점 가드와 회귀 테스트가 잡는다 — 혼입의 발생 경로를
#: 원천에서 증명하는 것이 §7-e 대체 안전망 ① 이다.
DAILY_ADJUSTED = True
BACKFILL_1M_ADJUSTED = False

#: §6.1 홀드아웃 — 이 구간의 가격·수익률·판정 수치는 어떤 출력에도 내지 않는다.
HOLDOUT_START = "2026-05-01"
HOLDOUT_END = "2026-07-29"

#: 폭주 방지 상한 (한 창에서 이 이상 페이지를 넘기면 중단하고 partial 로 기록).
MAX_PAGES_PER_WINDOW = 400
#: 캘린더 역방향 걷기 상한 (약 8년 — 보관 최대 1702일의 여유 상한).
MAX_CALENDAR_WALK = 2100

GROUP_CHART = "MARKET_DATA_CHART"
GROUP_INFO = "MARKET_INFO"


class StopRequested(Exception):
    """--stop-file 감지 — 정상 정지 요청 (체크포인트 저장 후 종료코드 4)."""


def _utc_date(ts_ms: int) -> str:
    """일봉 timestamp -> 시장 날짜. 일봉 키는 00:00 ET(=04~05:00 UTC)라 UTC 날짜와 같다."""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _utc_iso(ts_ms: int | None) -> str | None:
    if ts_ms is None:
        return None
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()


def in_holdout(date: str) -> bool:
    return HOLDOUT_START <= date <= HOLDOUT_END


# ---------------------------------------------------------------------------
# 매매일 달력 (§2.5: /market-calendar/US 만으로 산정)
# ---------------------------------------------------------------------------
def _day_to_json(md: UsMarketDay) -> dict:
    out: dict = {"date": md.date}
    for name in ("day", "pre", "regular", "after"):
        win: SessionWindow | None = getattr(md, name)
        out[name] = None if win is None else [int(win.start_ms), int(win.end_ms)]
    return out


def _day_from_json(raw: dict) -> UsMarketDay:
    def win(v):
        return None if v is None else SessionWindow(start_ms=int(v[0]), end_ms=int(v[1]))

    return UsMarketDay(date=str(raw["date"]), day=win(raw.get("day")),
                       pre=win(raw.get("pre")), regular=win(raw.get("regular")),
                       after=win(raw.get("after")))


def day_bounds(md: UsMarketDay) -> tuple[int, int] | None:
    """한 매매일의 (첫 세션 시작 ms, 마지막 세션 끝 ms)."""
    wins = [w for w in (md.day, md.pre, md.regular, md.after) if w is not None]
    if not wins:
        return None
    return min(w.start_ms for w in wins), max(w.end_ms for w in wins)


class TradingCalendar:
    """`/market-calendar/US` 를 역방향으로 걸어 모은 매매일 사전. 체크포인트에 캐시된다."""

    def __init__(self, days: dict[str, UsMarketDay] | None = None):
        self.days: dict[str, UsMarketDay] = dict(days or {})
        self.walk_calls = 0
        self.exhausted_at: str | None = None

    # ---- 직렬화 ---------------------------------------------------------
    def to_json(self) -> dict:
        return {d: _day_to_json(md) for d, md in self.days.items()}

    @classmethod
    def from_json(cls, raw: dict | None) -> "TradingCalendar":
        days = {}
        for d, item in (raw or {}).items():
            try:
                days[str(d)] = _day_from_json(item)
            except (KeyError, TypeError, ValueError):
                continue
        return cls(days)

    # ---- 조회 -----------------------------------------------------------
    def dates(self) -> list[str]:
        return sorted(self.days)

    def add(self, md: UsMarketDay | None) -> bool:
        if md is None or md.date in self.days:
            return False
        self.days[md.date] = md
        return True

    async def ensure_back_to(self, client, target_date: str) -> None:
        """가장 이른 캐시 날짜가 target_date 이하가 될 때까지 역방향으로 걷는다.

        비진행 가드: previous 가 없거나 날짜가 줄지 않으면(고정 픽스처·이력 끝) 중단하고
        `exhausted_at` 에 기록한다 — 무한 루프 금지.
        """
        if not self.days:
            page = await client.get_us_calendar()
            self.walk_calls += 1
            for key in ("previous", "today", "next"):
                self.add(page.get(key))
        for _ in range(MAX_CALENDAR_WALK):
            earliest = self.dates()[0]
            if earliest <= target_date:
                return
            page = await client.get_us_calendar(date=earliest)
            self.walk_calls += 1
            for key in ("previous", "today", "next"):
                self.add(page.get(key))
            prev = page.get("previous")
            if prev is None or prev.date >= earliest:
                self.exhausted_at = earliest           # 이력 끝 (또는 mock 고정 응답)
                return
        self.exhausted_at = self.dates()[0]

    async def ensure_forward_to(self, client, target_date: str) -> None:
        """가장 늦은 캐시 날짜가 target_date 이상이 될 때까지 순방향으로 걷는다.

        D+2 매매일이 '오늘' 너머일 수 있어(최근 후보) 며칠 앞의 달력이 필요하다.
        비진행이면(달력 끝) 조용히 멈춘다 — 창은 clamp 로 드러난다.
        """
        if not self.days:
            page = await client.get_us_calendar()
            self.walk_calls += 1
            for key in ("previous", "today", "next"):
                self.add(page.get(key))
        for _ in range(WINDOW_FWD * 4 + 10):
            latest = self.dates()[-1]
            if latest >= target_date:
                return
            page = await client.get_us_calendar(date=latest)
            self.walk_calls += 1
            for key in ("previous", "today", "next"):
                self.add(page.get(key))
            nxt = page.get("next")
            if nxt is None or nxt.date <= latest:
                return

    def window_for(self, d: str) -> tuple[list[str], int, int, str | None] | None:
        """스크린 통과일 D -> ([D-25, D+2] 매매일 목록, 시작 ms, 끝 ms, 클램프 사유).

        달력이 D-25 까지 못 닿으면 있는 만큼으로 클램프하고 사유를 돌려준다 —
        요청 창 자체(§2.8)는 불변이고, 부족분은 매니페스트에 커버리지 갭으로 남는다.
        """
        ds = self.dates()
        if d not in self.days:
            return None
        i = ds.index(d)
        lo = max(0, i - WINDOW_BACK)
        hi = min(len(ds) - 1, i + WINDOW_FWD)
        clamps = []
        if i - WINDOW_BACK < 0:
            clamps.append("calendar_short_back")
        if i + WINDOW_FWD > len(ds) - 1:
            clamps.append("calendar_short_fwd")
        clamp = "+".join(clamps) or None
        span_days = ds[lo:hi + 1]
        b0 = day_bounds(self.days[span_days[0]])
        b1 = day_bounds(self.days[span_days[-1]])
        if b0 is None or b1 is None:
            return None
        return span_days, b0[0], b1[1], clamp


# ---------------------------------------------------------------------------
# 스크린 (§2.8 — 순수 함수라 그대로 테스트된다)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DailyBar:
    ts_ms: int
    open_u: int
    high_u: int
    low_u: int
    close_u: int
    vol_qu: int


@dataclass
class ScreenDay:
    date: str
    reasons: list[str]                    # "ret" | "logvol_z" | "range"


def screen_daily(bars: list[DailyBar]) -> tuple[list[ScreenDay], dict[str, int]]:
    """일봉(오름차순) -> 스크린 통과일 + 판정 부가 카운트.

    해석 확정(코드가 정본, 보고서에 명시):
      - ret 은 직전 일봉 종가 대비 — 첫 봉은 판정 불가(카운트).
      - z 는 직전 최대 20개 일봉의 **양의 거래량** 로그 표본. 표본 < 2 또는 표준편차 0
        이면 z 판정 불가(카운트) — 임계 자체는 §2.8 그대로.
      - 레인지는 low > 0 일 때만 (low=0 이면 판정 불가 카운트).
    """
    out: list[ScreenDay] = []
    stats = {"first_bar_no_ret": 0, "z_insufficient": 0, "range_undefined": 0}
    logs: list[float] = []
    prev_close: int | None = None
    for bar in bars:
        reasons: list[str] = []
        date = _utc_date(bar.ts_ms)
        if prev_close is None:
            stats["first_bar_no_ret"] += 1
        elif prev_close > 0 and \
                (bar.close_u - prev_close) * 10_000 >= prev_close * _RET_MIN_BP:
            reasons.append("ret")
        window = logs[-SCREEN_LOOKBACK:]
        if len(window) < 2:
            stats["z_insufficient"] += 1
        elif bar.vol_qu > 0:
            mean = sum(window) / len(window)
            var = sum((x - mean) ** 2 for x in window) / (len(window) - 1)
            sd = math.sqrt(var)
            if sd > 0 and (math.log(bar.vol_qu) - mean) / sd >= SCREEN_LOGVOL_Z_MIN:
                reasons.append("logvol_z")
        if bar.low_u <= 0:
            stats["range_undefined"] += 1
        elif (bar.high_u - bar.low_u) * 10_000 >= bar.low_u * _RANGE_MIN_BP:
            reasons.append("range")
        if reasons:
            out.append(ScreenDay(date=date, reasons=reasons))
        prev_close = bar.close_u
        if bar.vol_qu > 0:
            logs.append(math.log(bar.vol_qu))
    return out, stats


def merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """겹치거나 맞닿는 [시작, 끝] ms 구간 병합 (후보 창 중복 수집 방지)."""
    merged: list[list[int]] = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


# ---------------------------------------------------------------------------
# 체크포인트 (멱등 재실행)
# ---------------------------------------------------------------------------
def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Checkpoint:
    """재시작 안전의 정본. 스크린 결과·창 진행 상태·캘린더 캐시를 든다."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"version": 1, "screen": {}, "windows": {}, "calendar": {}}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and int(raw.get("version", 0)) == 1:
                    self.data = raw
            except (OSError, ValueError):
                pass                               # 깨진 체크포인트는 새로 시작

    def save(self) -> None:
        _atomic_write(self.path, self.data)

    # ---- 스크린 ---------------------------------------------------------
    def screen_of(self, symbol: str) -> dict | None:
        return self.data["screen"].get(symbol)

    def set_screen(self, symbol: str, result: dict) -> None:
        self.data["screen"][symbol] = result

    # ---- 창 -------------------------------------------------------------
    def window_key(self, symbol: str, span: tuple[int, int]) -> str:
        return f"{symbol}:{span[0]}:{span[1]}"

    def window_state(self, symbol: str, span: tuple[int, int]) -> dict:
        return self.data["windows"].setdefault(
            self.window_key(symbol, span),
            {"symbol": symbol, "start_ms": span[0], "end_ms": span[1], "status": "todo",
             "oldest_ms": None, "newest_ms": None, "bars": 0, "calls": 0,
             "adjusted": "false" if not BACKFILL_1M_ADJUSTED else "true",
             "skip_reason": None})


# ---------------------------------------------------------------------------
# 백필 실행기
# ---------------------------------------------------------------------------
@dataclass
class Runner:
    cfg: Config
    client: object
    store: Store | None
    checkpoint: Checkpoint
    calendar: TradingCalendar
    out_dir: Path
    date_from: str | None = None
    date_to: str | None = None
    stop_file: Path | None = None
    redrive: bool = False
    failures: dict[str, int] = field(default_factory=dict)
    totals: dict[str, int] = field(default_factory=lambda: {
        "calls_daily": 0, "calls_1m": 0, "bars_daily": 0, "bars_1m": 0})

    def fail(self, reason: str, n: int = 1) -> None:
        self.failures[reason] = self.failures.get(reason, 0) + n

    # ---- 우아한 정지 (--stop-file) --------------------------------------
    def _stop_requested(self) -> bool:
        return self.stop_file is not None and self.stop_file.exists()

    def _check_stop(self) -> None:
        """경계 지점에서 호출 — 정지 요청이면 체크포인트를 남기고 즉시 올린다."""
        if self._stop_requested():
            self.checkpoint.save()
            raise StopRequested(f"stop file present: {self.stop_file}")

    # ---- 1단계: 일봉 스크린 ---------------------------------------------
    async def screen_symbol(self, symbol: str) -> dict:
        cached = self.checkpoint.screen_of(symbol)
        if cached is not None and cached.get("done"):
            return cached
        bars: dict[int, DailyBar] = {}
        before: int | None = None
        pages = 0
        stop_ms = None
        if self.date_from is not None:
            # 스크린 시작일보다 20 매매일(여유 40 달력일) 더 옛날이면 그만 걷는다.
            stop_ms = int(datetime.strptime(self.date_from, "%Y-%m-%d")
                          .replace(tzinfo=timezone.utc).timestamp() * 1000) - 40 * DAY_MS
        while pages < 32:                                   # 일봉 ~6400개 상한 (>25년)
            page = await self.client.get_candles(symbol, "1d", count=PAGE,
                                                 before_ms=before,
                                                 adjusted=DAILY_ADJUSTED)
            pages += 1
            self.totals["calls_daily"] += 1
            if not page.candles:
                break
            if self.store is not None:
                self.store.upsert_candles_1d(page.candles)
            oldest_before = min(bars) if bars else None
            for c in page.candles:
                bars[int(c.ts_ms)] = DailyBar(int(c.ts_ms), int(c.open_u), int(c.high_u),
                                              int(c.low_u), int(c.close_u), int(c.vol_qu))
            oldest_now = min(bars)
            if oldest_before is not None and oldest_now >= oldest_before:
                break                                       # 비진행(고정 픽스처) 가드
            if stop_ms is not None and oldest_now <= stop_ms:
                break
            if page.next_before_ms is None:
                break
            before = int(page.next_before_ms) - 1           # docs/06: before 는 inclusive
        series = [bars[k] for k in sorted(bars)]
        self.totals["bars_daily"] += len(series)
        days, stats = screen_daily(series)
        if self.date_from is not None:
            days = [d for d in days if d.date >= self.date_from]
        if self.date_to is not None:
            days = [d for d in days if d.date <= self.date_to]
        result = {
            "done": True, "daily_pages": pages, "daily_bars": len(series),
            "oldest_daily": _utc_date(series[0].ts_ms) if series else None,
            "newest_daily": _utc_date(series[-1].ts_ms) if series else None,
            "screen_stats": stats,
            # 홀드아웃 매매일은 사유를 기록하지 않는다 (§6.2 — 날짜만).
            "candidates": [
                {"date": d.date, "withheld": "holdout"} if in_holdout(d.date)
                else {"date": d.date, "reasons": d.reasons}
                for d in days],
        }
        self.checkpoint.set_screen(symbol, result)
        self.checkpoint.save()
        return result

    # ---- 2단계: 1분봉 창 백필 -------------------------------------------
    def _active_anchors(self, symbol: str, start_ms: int, end_ms: int) -> list[int]:
        """창 안의 **스크린 후보일**(= 1분봉 데이터가 뭉쳐 있는 곳)의 세션-끝 앵커(내림차순).

        결함 수정의 핵심 (진단 보고서): Toss 1분봉 API 는 각 `before` 앵커에서 한정된
        깊이만 서빙하고 무체결 갭을 만나면 nextBefore=None 을 준다. 창 끝에서 한 번만
        후진하면 첫 갭에서 멈춰 창의 88.6% 가 25% 미만만 수집됐다.

        수정: 창 끝(before=end_ms)에서 시작해, 블록이 소진(nextBefore=None)되면 **다음
        후보일 앵커로 갭을 건너뛰어** 재개한다. 이 소형주들은 1분봉이 활황일(스크린 후보)
        에만 뭉쳐 있고 그 사이는 진짜로 무체결이므로, 후보일에만 앵커를 두면 휴면일을
        프로브하지 않고도 데이터 보유일을 전부 수집한다 (갭 폭과 무관하게).
        """
        sc = self.checkpoint.screen_of(symbol) or {}
        anchors: list[int] = []
        for cand in sc.get("candidates", ()):
            md = self.calendar.days.get(cand.get("date"))
            if md is None:
                continue
            b = day_bounds(md)
            if b is None:
                continue
            b0, b1 = b
            if b1 - 1 < start_ms or b0 > end_ms:            # 창과 안 겹치는 후보는 제외
                continue
            anchors.append(min(b1 - 1, end_ms))
        return sorted(set(anchors), reverse=True)

    async def backfill_window(self, symbol: str, span: tuple[int, int]) -> dict:
        state = self.checkpoint.window_state(symbol, span)
        if state["status"] == "done":
            return state
        start_ms, end_ms = span
        anchors = self._active_anchors(symbol, start_ms, end_ms)   # 내림차순 (후보일 전부)
        if not anchors:
            # 창 안에 후보일이 없다 (병합 창엔 항상 있으나 방어적).
            state["status"] = "partial" if state["bars"] else "skipped"
            state["skip_reason"] = state["skip_reason"] or (
                "retention_exhausted" if state["bars"] else "no_active_days")
            if state["status"] == "partial":
                self.fail(state["skip_reason"])
            self.checkpoint.save()
            return state

        def next_anchor_below(x: int) -> int | None:
            for a in anchors:                               # 내림차순 — 첫 a < x
                if a < x:
                    return a
            return None

        # 재개: 이미 받은 가장 오래된 봉 바로 아래부터 잇는다. 앵커 목록은 필터하지 않는다 —
        # 재개 지점이 후보일 블록 중간이어도 그 블록을 계속 페이징해야 하기 때문 (멱등).
        resume_oldest = int(state["oldest_ms"]) if state["oldest_ms"] is not None else None
        before = end_ms if resume_oldest is None else resume_oldest - 1
        pages = 0
        reached_start = False
        page_capped = False
        while True:
            if pages >= MAX_PAGES_PER_WINDOW:
                page_capped = True
                break
            self._check_stop()                              # 페이지 사이 정지 경계
            page = await self.client.get_candles(symbol, "1m", count=PAGE,
                                                 before_ms=before,
                                                 adjusted=BACKFILL_1M_ADJUSTED)
            pages += 1
            state["calls"] += 1
            self.totals["calls_1m"] += 1
            if page.candles:
                if self.store is not None:
                    self.store.upsert_candles_1m(page.candles)
                in_window = [c for c in page.candles
                             if start_ms <= int(c.ts_ms) <= end_ms]
                new_oldest = int(page.candles[0].ts_ms)
                new_newest = int(page.candles[-1].ts_ms)
                prev_oldest = state["oldest_ms"]
                state["bars"] += len(in_window)
                self.totals["bars_1m"] += len(in_window)
                state["oldest_ms"] = (new_oldest if prev_oldest is None
                                      else min(int(prev_oldest), new_oldest))
                state["newest_ms"] = (new_newest if state["newest_ms"] is None
                                      else max(int(state["newest_ms"]), new_newest))
                if new_oldest <= start_ms:
                    reached_start = True
                    break
                nb = page.next_before_ms
                if nb is not None and int(nb) - 1 < before:
                    before = int(nb) - 1                    # 같은 블록 계속 (docs/06 inclusive)
                else:
                    # 블록 소진 (nextBefore=None) — 갭을 넘어 다음(더 오래된) 후보일로 재앵커
                    nxt = next_anchor_below(new_oldest)
                    if nxt is None:
                        break
                    before = nxt
            else:
                # 이 앵커에 1분봉이 없다 (보관 소멸) — 다음 후보일로 갭 넘김
                nxt = next_anchor_below(before)
                if nxt is None:
                    break
                before = nxt
        if reached_start:
            state["status"] = "done"
            state["skip_reason"] = None
        elif page_capped:
            state["status"] = "partial"
            state["skip_reason"] = "page_cap"
        else:
            state["status"] = "partial" if state["bars"] else "skipped"
            if state["skip_reason"] is None:
                state["skip_reason"] = "retention_exhausted"
        if state["status"] == "partial":
            self.fail(state["skip_reason"] or "partial")
        self.checkpoint.save()
        return state

    # ---- 오케스트레이션 --------------------------------------------------
    async def run(self, symbols: list[str], *, screen_only: bool = False) -> dict:
        if BACKFILL_1M_ADJUSTED is not False:
            raise RuntimeError("BACKFILL_1M_ADJUSTED must be False (prereg 7-e). "
                               "Refusing to run: adjusted 1m series would poison "
                               "the nominal price band analysis.")
        # 1) 스크린
        for i, symbol in enumerate(symbols):
            self._check_stop()                              # 심볼 사이 정지 경계
            try:
                await self.screen_symbol(symbol)
            except (Forbidden, ForbiddenEndpoint):
                raise                                        # 치명 — 전체 중단
            except (TossApiError, OSError) as exc:
                self.fail(f"screen_error:{type(exc).__name__}")
                print(f"[screen] {symbol}: {type(exc).__name__} (skip)")
            if (i + 1) % 25 == 0:
                print(f"[screen] {i + 1}/{len(symbols)} symbols done")
        # 2) 달력 확보 (가장 이른 후보일 - 25 매매일 ~ 가장 늦은 후보일 + 2 매매일)
        earliest = latest = None
        for symbol in symbols:
            sc = self.checkpoint.screen_of(symbol) or {}
            for cand in sc.get("candidates", ()):
                if earliest is None or cand["date"] < earliest:
                    earliest = cand["date"]
                if latest is None or cand["date"] > latest:
                    latest = cand["date"]
        if earliest is not None:
            self._check_stop()                              # 달력 걷기 전 정지 경계
            def shift(date: str, days: int) -> str:
                return _utc_date(int(datetime.strptime(date, "%Y-%m-%d")
                                     .replace(tzinfo=timezone.utc).timestamp() * 1000)
                                 + days * DAY_MS)
            try:
                # 25 매매일 <= 40 달력일, 2 매매일 <= 7 달력일 여유.
                await self.calendar.ensure_back_to(self.client, shift(earliest, -40))
                await self.calendar.ensure_forward_to(self.client, shift(latest, 7))
            except (Forbidden, ForbiddenEndpoint):
                raise
            except (TossApiError, OSError) as exc:
                # 달력이 모자라면 창이 clamp 로 드러난다 — 걷기 실패로 전체를 세우지 않는다.
                self.fail(f"calendar_error:{type(exc).__name__}")
            self.checkpoint.data["calendar"] = self.calendar.to_json()
            self.checkpoint.save()
        # 3) 창 계산 + 병합 — 호출이 없는 로컬 연산. screen-only 모드도 여기까지는 하며,
        #    체크포인트에 창이 확정되므로 --estimate 가 **정확한** 1분봉 호출량을 낸다.
        planned: list[tuple[str, tuple[int, int]]] = []
        for symbol in symbols:
            sc = self.checkpoint.screen_of(symbol) or {}
            spans: list[tuple[int, int]] = []
            for cand in sc.get("candidates", ()):
                # 날짜 경계는 창 계획에도 적용한다 — 스크린 결과(§2.8 표본 프레임 기록)는
                # 그대로 두고 **가져올 창만** 자른다. 1분봉 보관(~320일) 밖의 창은 어차피
                # 빈 페이지 프로브만 남기므로, 경계는 데이터 손실 없이 호출량을 자른다.
                if self.date_from is not None and cand["date"] < self.date_from:
                    continue
                if self.date_to is not None and cand["date"] > self.date_to:
                    continue
                win = self.calendar.window_for(cand["date"])
                if win is None:
                    self.fail("day_not_in_calendar")
                    continue
                _days, w_start, w_end, clamp = win
                if clamp:
                    self.fail(clamp)
                spans.append((w_start, w_end))
            for span in merge_spans(spans):
                self.checkpoint.window_state(symbol, span)      # todo 로 등록
                planned.append((symbol, span))
        # 경계가 바뀌어 계획에서 빠진, **손대지 않은**(todo·0봉) 창은 정리한다 —
        # 진행분이 있는 창은 절대 지우지 않는다 (멱등 재개 보존).
        planned_keys = {self.checkpoint.window_key(s, sp) for s, sp in planned}
        for key in list(self.checkpoint.data["windows"]):
            w = self.checkpoint.data["windows"][key]
            if key not in planned_keys and w.get("status") == "todo" \
                    and not w.get("bars"):
                self.checkpoint.data["windows"].pop(key)
        if self.redrive:
            # 앵커 결함 수정 후 재구동: 옛 코드가 done/partial 로 남긴 창은 창 끝
            # 블록만 받은 것이라, 상태·진행 포인터를 초기화해 새 앵커 로직으로 전 구간을
            # 다시 받게 한다. DB 의 기존 봉은 upsert 멱등이라 보존된다(중복 저장 없음).
            reset = 0
            for s, sp in planned:
                st = self.checkpoint.window_state(s, sp)
                if st["status"] != "todo" or st.get("oldest_ms") is not None:
                    st.update(status="todo", oldest_ms=None, newest_ms=None,
                              bars=0, calls=0, skip_reason=None)
                    reset += 1
            print(f"[redrive] reset {reset} window(s) to re-page with the anchor fix")
        self.checkpoint.save()
        if screen_only:
            return self.manifest(symbols)
        # 4) 백필
        for symbol, span in planned:
            self._check_stop()                              # 창 사이 정지 경계
            try:
                await self.backfill_window(symbol, span)
            except (Forbidden, ForbiddenEndpoint):
                raise
            except (TossApiError, OSError) as exc:
                self.fail(f"backfill_error:{type(exc).__name__}")
                print(f"[backfill] {symbol}: {type(exc).__name__} (window kept "
                      "as partial; rerun resumes)")
        return self.manifest(symbols)

    # ---- 매니페스트 (§8 산출물 1) ---------------------------------------
    def manifest(self, symbols: list[str]) -> dict:
        reason_counts = {"ret": 0, "logvol_z": 0, "range": 0}
        holdout_days = 0
        candidates_total = 0
        per_symbol: dict[str, dict] = {}
        for symbol in symbols:
            sc = self.checkpoint.screen_of(symbol)
            if sc is None:
                continue
            windows = []
            oldest_1m = None
            exhausted = False
            for key, w in self.checkpoint.data["windows"].items():
                if w["symbol"] != symbol:
                    continue
                if w["oldest_ms"] is not None:
                    oldest_1m = (w["oldest_ms"] if oldest_1m is None
                                 else min(oldest_1m, w["oldest_ms"]))
                if w.get("skip_reason") == "retention_exhausted":
                    exhausted = True
                windows.append({
                    "requested_utc": [_utc_iso(w["start_ms"]), _utc_iso(w["end_ms"])],
                    "collected_utc": [_utc_iso(w["oldest_ms"]), _utc_iso(w["newest_ms"])],
                    "bars_in_window": w["bars"], "calls": w["calls"],
                    "status": w["status"], "skip_reason": w["skip_reason"],
                    "adjusted": w["adjusted"],       # §7-e 안전망 ① — 호출 파라미터 증거
                })
            for cand in sc.get("candidates", ()):
                candidates_total += 1
                if "withheld" in cand:
                    holdout_days += 1                # 사유·수치 비공개 (§6.2)
                else:
                    for r in cand["reasons"]:
                        reason_counts[r] += 1
            per_symbol[symbol] = {
                "daily_pages": sc["daily_pages"], "daily_bars": sc["daily_bars"],
                "oldest_daily": sc["oldest_daily"], "newest_daily": sc["newest_daily"],
                "probed_oldest_1m_utc": _utc_iso(oldest_1m),
                "retention_exhausted_1m": exhausted,
                "screen_stats": sc["screen_stats"],
                "candidate_days": sc.get("candidates", []),
                "windows": windows,
            }
        commit = None
        try:
            commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT),
                                    capture_output=True, text=True, timeout=10
                                    ).stdout.strip() or None
        except Exception:
            pass
        return {
            "generated_utc": datetime.now(tz=timezone.utc).isoformat(),
            "tool": "tools/backfill.py", "commit": commit,
            "prereg": "docs/12_preregistration.md section 2.8 / 6.1 / 7-e / 8-1",
            "call_params": {                          # §7-e 대체 안전망 ① — 출처 증명
                "candles_1m": {"interval": "1m", "adjusted": "false"},
                "candles_1d": {"interval": "1d", "adjusted": "true"},
            },
            "screen_thresholds": {"ret_min": SCREEN_RET_MIN,
                                  "logvol_z_min": SCREEN_LOGVOL_Z_MIN,
                                  "range_min": SCREEN_RANGE_MIN,
                                  "lookback_days": SCREEN_LOOKBACK,
                                  "window": [f"D-{WINDOW_BACK}", f"D+{WINDOW_FWD}"]},
            "universe": self.checkpoint.data.get("universe_meta", {}),
            "screen_reason_counts_train_val": {
                **reason_counts,
                "note": "holdout (2026-05-01..2026-07-29) days excluded per "
                        "prereg 6.2; coverage only",
            },
            "holdout_candidate_days": holdout_days,
            "candidates_total": candidates_total,
            "calendar": {"days_resolved": len(self.calendar.days),
                         "walk_calls": self.calendar.walk_calls,
                         "exhausted_at": self.calendar.exhausted_at},
            "failures": dict(sorted(self.failures.items())),
            "totals": dict(self.totals),
            "symbols": per_symbol,
        }

    def write_manifest(self, manifest: dict) -> tuple[Path, Path]:
        jpath = self.out_dir / "backfill_manifest.json"
        _atomic_write(jpath, manifest)
        cpath = self.out_dir / "backfill_manifest.csv"
        with open(cpath, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["symbol", "window_from_utc", "window_to_utc", "status",
                        "bars_in_window", "calls", "adjusted", "skip_reason",
                        "probed_oldest_1m_utc"])
            for symbol, info in sorted(manifest["symbols"].items()):
                for win in info["windows"]:
                    w.writerow([symbol, win["requested_utc"][0], win["requested_utc"][1],
                                win["status"], win["bars_in_window"], win["calls"],
                                win["adjusted"], win["skip_reason"] or "",
                                info["probed_oldest_1m_utc"] or ""])
        return jpath, cpath


# ---------------------------------------------------------------------------
# 호출량 추정 (--estimate — 실제 호출 없음)
# ---------------------------------------------------------------------------
def estimate(cfg: Config, checkpoint: Checkpoint, calendar: TradingCalendar,
             universe_size: int, *, assume_span_days: int = 550,
             assume_hit_rate: float = 0.005,
             bars_per_day_scenarios: tuple[int, ...] = (150, 400, 1140)) -> dict:
    """스크린·달력·1분봉 백필의 총 호출 수와 예산 상한 하 소요 시간을 추정한다.

    스크린 결과가 체크포인트에 있으면 **정확한** 후보 창으로 계산하고, 없으면
    가정(assume_*)을 명시해 계산한다 — 내일 아침 실행 판단 근거 (§ 태스크 요구 6·7).
    """
    ratio = cfg.api.usage_ratio
    chart_rate = float(cfg.limits.get(GROUP_CHART, 5.0)) * ratio
    info_rate = float(cfg.limits.get(GROUP_INFO, 3.0)) * ratio

    screened = [s for s, v in checkpoint.data["screen"].items() if v.get("done")]
    daily_pages_known = sum(v["daily_pages"] for v in checkpoint.data["screen"].values()
                            if v.get("done"))
    remaining = max(0, universe_size - len(screened))
    daily_pages_est = remaining * math.ceil(assume_span_days / PAGE)
    screen_calls = daily_pages_known + daily_pages_est

    # 달력: 가장 이른 후보 - 40 달력일까지 역방향 걷기 (~1콜/매매일). 캐시만큼 절약.
    calendar_calls = max(0, math.ceil(assume_span_days * 1.05) - len(calendar.days))

    known_windows: list[tuple[int, int, dict]] = []
    for w in checkpoint.data["windows"].values():
        known_windows.append((w["start_ms"], w["end_ms"], w))
    if known_windows:
        window_days = sum((b - a) / DAY_MS for a, b, _w in known_windows)
        pending = [w for _a, _b, w in known_windows if w["status"] != "done"]
        basis = f"checkpoint windows={len(known_windows)} (pending={len(pending)})"
    else:
        est_candidates = max(1, int(universe_size * assume_span_days * assume_hit_rate))
        window_days = est_candidates * (WINDOW_BACK + WINDOW_FWD + 1)
        basis = (f"assumed hit_rate={assume_hit_rate} -> candidates~{est_candidates} "
                 "(windows unmerged upper bound)")

    scenarios = {}
    for bpd in bars_per_day_scenarios:
        calls_1m = int(window_days * bpd / PAGE) + max(1, int(window_days
                                                              / (WINDOW_BACK + 3)))
        secs = screen_calls / chart_rate + calls_1m / chart_rate \
            + calendar_calls / info_rate
        scenarios[f"bars_per_day_{bpd}"] = {
            "calls_1m": calls_1m,
            "total_calls": screen_calls + calls_1m + calendar_calls,
            "est_hours": round(secs / 3600, 2),
        }
    return {
        "universe_size": universe_size,
        "screen_done_symbols": len(screened),
        "screen_calls": screen_calls,
        "calendar_calls": calendar_calls,
        "window_trading_days": round(window_days, 1),
        "basis": basis,
        "budget_rates_rps": {"MARKET_DATA_CHART": round(chart_rate, 2),
                             "MARKET_INFO": round(info_rate, 2)},
        "scenarios": scenarios,
        "assumptions": {"span_days": assume_span_days, "hit_rate": assume_hit_rate,
                        "note": "scenarios bracket sparse smallcap (150 bars/day), "
                                "typical (400), theoretical max (1140)"},
    }


# ---------------------------------------------------------------------------
# 유니버스
# ---------------------------------------------------------------------------
def load_universe(cfg: Config, symbols_arg: str | None) -> tuple[list[str], dict]:
    if symbols_arg:
        syms = sorted({s.strip().upper() for s in symbols_arg.split(",") if s.strip()})
        return syms, {"source": "cli", "size": len(syms),
                      "directory_snapshot_utc": None}
    store_cfg = cfg.store
    if store_cfg is None:
        raise SystemExit("config has no store section and no --symbols given")
    from tossmon.store.reader import Reader

    reader = Reader(Path(store_cfg.db_path))
    try:
        df = reader.symbols()
    finally:
        reader.close()
    if df is None or df.empty:
        raise SystemExit("symbols table is empty - run build_universe first or pass "
                         "--symbols")
    snap_ms = int(df["updated_ms"].max()) if "updated_ms" in df else None
    syms = sorted({str(s).upper() for s in df["symbol"].tolist()})
    return syms, {"source": "symbols-table", "size": len(syms),
                  "directory_snapshot_utc": _utc_iso(snap_ms)}


def build_client(cfg: Config) -> TossClient:
    """W1 client 를 규약대로 조립한다 — limiter 우회 금지 (계약 C-4)."""
    tokens = TokenManager(cfg.api.keys_path, cfg.api.token_state_path, cfg.api.live)
    limiter = GroupRateLimiter(dict(cfg.limits), cfg.api.usage_ratio)
    return TossClient(cfg.api.base_url, tokens, limiter, timeout_s=cfg.api.timeout_s)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
async def amain(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg.api.live and not args.allow_live:
        print("REFUSED: config api.live=true but --allow-live not given. "
              "Live backfill needs coordinator approval (contract C-9/A6).")
        return 2
    if BACKFILL_1M_ADJUSTED is not False:
        print("REFUSED: BACKFILL_1M_ADJUSTED tampered (must be False, prereg 7-e).")
        return 2

    db_path = Path(args.db) if args.db else (Path(cfg.store.db_path) if cfg.store
                                             else None)
    if db_path is None:
        print("REFUSED: no db path (config store.db_path or --db)")
        return 2
    out_dir = Path(args.out) if args.out else db_path.parent / "backfill"
    out_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = Checkpoint(out_dir / "checkpoint.json")
    calendar = TradingCalendar.from_json(checkpoint.data.get("calendar"))

    symbols, uni_meta = load_universe(cfg, args.symbols)
    checkpoint.data["universe_meta"] = uni_meta

    if args.estimate:
        est = estimate(cfg, checkpoint, calendar, len(symbols),
                       assume_span_days=args.assume_span_days,
                       assume_hit_rate=args.assume_hit_rate)
        print(json.dumps(est, indent=2, sort_keys=True))
        _atomic_write(out_dir / "backfill_estimate.json", est)
        return 0

    client = build_client(cfg)
    store = Store(db_path)
    runner = Runner(cfg=cfg, client=client, store=store, checkpoint=checkpoint,
                    calendar=calendar, out_dir=out_dir,
                    date_from=args.date_from, date_to=args.date_to,
                    stop_file=Path(args.stop_file) if args.stop_file else None,
                    redrive=args.redrive)
    started = time.monotonic()
    try:
        manifest = await runner.run(symbols, screen_only=args.screen_only)
    except StopRequested as exc:
        print(f"STOPPED: {exc} - checkpoint saved; rerun resumes")
        checkpoint.save()
        return 4
    except (Forbidden, ForbiddenEndpoint) as exc:
        print(f"FATAL: {type(exc).__name__}: {exc} - aborting whole run")
        checkpoint.save()
        return 3
    finally:
        await client.aclose()
        store.close()
    jpath, cpath = runner.write_manifest(manifest)
    elapsed = time.monotonic() - started
    mode = "screen-only" if args.screen_only else "backfill"
    print(f"{mode} done in {elapsed:.0f}s: symbols={len(symbols)} "
          f"candidates={manifest['candidates_total']} "
          f"(holdout_days={manifest['holdout_candidate_days']} - detail withheld) "
          f"calls_1d={manifest['totals']['calls_daily']} "
          f"calls_1m={manifest['totals']['calls_1m']} "
          f"bars_1m={manifest['totals']['bars_1m']}")
    print(f"manifest: {jpath}")
    print(f"manifest: {cpath}")
    if args.screen_only:
        est = estimate(cfg, checkpoint, calendar, len(symbols),
                       assume_span_days=args.assume_span_days,
                       assume_hit_rate=args.assume_hit_rate)
        _atomic_write(out_dir / "backfill_estimate.json", est)
        print("estimate (exact - from screened windows):")
        print(json.dumps(est, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="bulk historical 1m backfill per preregistration 2.8 "
                    "(screen -> [D-25,D+2] windows -> manifest)")
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--db", default=None, help="target sqlite db (default: config)")
    ap.add_argument("--out", default=None, help="checkpoint/manifest dir "
                                                "(default: <db dir>/backfill)")
    ap.add_argument("--symbols", default=None,
                    help="comma separated universe override (default: symbols table)")
    ap.add_argument("--from", dest="date_from", default=None,
                    help="screen start date YYYY-MM-DD (default: retention-limited)")
    ap.add_argument("--to", dest="date_to", default=None,
                    help="screen end date YYYY-MM-DD (default: all available)")
    ap.add_argument("--estimate", action="store_true",
                    help="no API calls - print call count / duration estimate")
    ap.add_argument("--screen-only", action="store_true",
                    help="run screen + calendar + window planning, skip 1m backfill "
                         "(then --estimate is exact)")
    ap.add_argument("--assume-span-days", type=int, default=550)
    ap.add_argument("--assume-hit-rate", type=float, default=0.005)
    ap.add_argument("--allow-live", action="store_true",
                    help="required on top of api.live=true (coordinator approval)")
    ap.add_argument("--stop-file", default=None,
                    help="graceful stop: exit 4 with checkpoint saved when this "
                         "file appears (supervisor channel)")
    ap.add_argument("--redrive", action="store_true",
                    help="reset planned windows (done/partial) to re-page fully with "
                         "the anchor fix; existing DB bars are preserved (idempotent)")
    args = ap.parse_args(argv)
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("interrupted - checkpoint saved; rerun to resume")
        return 130


if __name__ == "__main__":
    sys.exit(main())

"""합성 데이터 생성기 — 소유: W3 (최우선 산출물).

현실적인 1분봉/랭킹 시나리오: coil→폭발형, 즉발형, 페이드형(HOD 조기·VWAP 상실),
덤프형, 노이즈형. 라벨 정답을 함께 반환해 검출기 평가의 ground truth 로 사용.

설계 규약
---------
* 시간: UTC epoch ms `int` (계약 C-1). 세션 경계는 `UsMarketDay`/`SessionWindow` 로만 표현.
  합성 캘린더는 **명시된 고정 ET 오프셋**(기본 -4h = EDT)으로 만든다. 실서비스는 반드시
  `/market-calendar/US` 응답을 쓸 것 — 여기서 오프셋을 고정하는 이유는 재현성뿐이다.
* 가격: 마이크로달러 `int` 만 사용. 모든 가격 전이는 ppm(백만분율) 정수 연산
  (`price_u * (1_000_000 + ppm) // 1_000_000`). float 가격 연산 없음 (계약 C-2).
* 거래량: 마이크로주 `int`(`vol_qu`). 분포 노이즈는 생성 과정에서만 float로 계산하고
  마지막에 `int` 로 절단한다.
* 캔들 공백: 체결이 없거나 홀트/수집중단 구간은 **행 자체를 생성하지 않는다** (API 동작 모사).

세션 스펙 (docs/01 §5, KST → ET 환산)
    day     ET 20:00(D-1) ~ 03:50(D)   470분   (대체거래소 경유, 유동성 희박)
    pre     ET 04:00 ~ 09:30           330분
    regular ET 09:30 ~ 16:00           390분
    after   ET 16:00 ~ 18:00           120분
"""
from __future__ import annotations

import math
import random
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime, timedelta, timezone

import pandas as pd

from tossmon.api.models import MICRO, SessionWindow, UsMarketDay

MIN_MS = 60_000

CANDLE_COLS = ["symbol", "ts_ms", "open_u", "high_u", "low_u", "close_u", "vol_qu"]
RANKING_COLS = ["snap_ms", "ranking_type", "duration", "rank", "symbol",
                "last_u", "vol_qu", "amount_u"]

#: 세션별 (ET 자정 기준 시작분, 종료분). day 세션은 전일 20:00 시작이라 음수.
#: W1 라이브 실측 정정본 (docs/01 §5, 2026-07-30): day 09:00-17:00 / pre 17:00-22:30 /
#: regular 22:30-05:00 / after 05:00-08:50 KST.
SESSION_MINUTES: dict[str, tuple[int, int]] = {
    "day": (-240, 240),      # KST 09:00-17:00 = ET 20:00(D-1)-04:00, 480분
    "pre": (240, 570),       # KST 17:00-22:30 = ET 04:00-09:30, 330분
    "regular": (570, 960),   # KST 22:30-05:00 = ET 09:30-16:00, 390분
    "after": (960, 1190),    # KST 05:00-08:50 = ET 16:00-19:50, 230분
}
SESSION_ORDER = ("day", "pre", "regular", "after")

SCENARIO_KINDS = ("coil_pop", "instant", "fade", "dump", "noise", "daymarket", "halt_gap")

DEFAULT_ET_OFFSET_H = -4          # EDT. 합성 전용 고정값.
DEFAULT_SHARES_OUT = 12_000_000   # 저플로트 러너 스윗스팟 (docs/02 §4.3)
DEFAULT_ADV_SHARES = 800_000      # 20일 평균 거래량(주)
DEFAULT_BASE_PRICE_U = 3_500_000  # $3.50


# --------------------------------------------------------------------------- #
# 결정론적 RNG
# --------------------------------------------------------------------------- #
def _stable_hash(s: str) -> int:
    h = 0
    for ch in s:
        h = (h * 131 + ord(ch)) % (2 ** 31)
    return h


def _rng(kind: str, seed: int) -> random.Random:
    """프로세스 간 재현 가능한 RNG (`hash()` 는 PYTHONHASHSEED 로 흔들리므로 금지)."""
    return random.Random((seed * 1_000_003 + _stable_hash(kind)) % (2 ** 31))


# --------------------------------------------------------------------------- #
# 캘린더
# --------------------------------------------------------------------------- #
def _et_midnight_ms(d: _date, et_offset_h: int) -> int:
    tz = timezone(timedelta(hours=et_offset_h))
    return int(datetime(d.year, d.month, d.day, tzinfo=tz).timestamp() * 1000)


#: 반일장(조기폐장) 정규장 종료 — 13:00 ET = ET 자정 기준 780분.
#: 추수감사절 다음날·크리스마스 이브·독립기념일 전날 등 매년 여러 번 발생한다.
HALF_DAY_REGULAR_END_MIN = 780
#: 반일장의 애프터장 길이는 정상일과 동일하게 유지한다(조기폐장만 모사).
AFTER_LEN_MIN = SESSION_MINUTES["after"][1] - SESSION_MINUTES["after"][0]


def make_calendar(n_days: int = 3,
                  start: str = "2026-06-01",
                  et_offset_h: int = DEFAULT_ET_OFFSET_H,
                  half_days: Iterable[str] | None = None) -> list[UsMarketDay]:
    """주말을 건너뛴 `n_days` 개의 `UsMarketDay` (4세션 전부 채움).

    `et_offset_h` 로 겨울(EST, -5)을 만들 수 있다 — EST 에서는 애프터장이 UTC 자정을
    넘으므로 "UTC 날짜 = 매매일" 전제가 깨진다(감사 M-3). 회귀 테스트용.

    `half_days` 에 ISO 날짜를 주면 그 날은 **반일장**(정규장 09:30-13:00 ET, 210분)이 되고
    애프터장이 그만큼 당겨진다 — 분-of-session 곡선 오염 회귀 테스트용(감사 M-4).
    """
    half = set(half_days or ())
    d = _date.fromisoformat(start)
    out: list[UsMarketDay] = []
    while len(out) < n_days:
        if d.weekday() < 5:
            base = _et_midnight_ms(d, et_offset_h)
            bounds = dict(SESSION_MINUTES)
            if d.isoformat() in half:
                r0 = bounds["regular"][0]
                bounds["regular"] = (r0, HALF_DAY_REGULAR_END_MIN)
                bounds["after"] = (HALF_DAY_REGULAR_END_MIN,
                                   HALF_DAY_REGULAR_END_MIN + AFTER_LEN_MIN)
            win = {name: SessionWindow(start_ms=base + m0 * MIN_MS,
                                       end_ms=base + m1 * MIN_MS)
                   for name, (m0, m1) in bounds.items()}
            out.append(UsMarketDay(date=d.isoformat(), day=win["day"], pre=win["pre"],
                                   regular=win["regular"], after=win["after"]))
        d += timedelta(days=1)
    return out


def session_windows(md: UsMarketDay) -> list[tuple[str, SessionWindow]]:
    """시간순 (세션명, 윈도우) 목록."""
    return [(name, getattr(md, name)) for name in SESSION_ORDER
            if getattr(md, name) is not None]


# --------------------------------------------------------------------------- #
# 세션 내 분 위치별 기준 거래량 (U자형)
# --------------------------------------------------------------------------- #
#: 세션별 "일평균거래량(ADV) 대비 세션 총량" 비중.
SESSION_VOL_SHARE = {"day": 0.04, "pre": 0.10, "regular": 0.82, "after": 0.04}


def _minute_weights(session: str, n: int) -> list[float]:
    """세션 내 분 위치별 상대 거래량 가중치 (합=1)."""
    w: list[float] = []
    for m in range(n):
        if session == "regular":
            x = 1.0 + 4.0 * math.exp(-m / 18.0) + 2.2 * math.exp(-(n - 1 - m) / 15.0)
        elif session == "pre":
            x = 0.35 + 2.5 * math.exp(-(n - 1 - m) / 45.0)      # 개장 직전 집중
        elif session == "after":
            x = 0.3 + 3.0 * math.exp(-m / 20.0)                 # 종료 직후 집중
        else:                                                    # day — 얇고 평탄
            x = 0.5 + 0.5 * math.exp(-abs(m - n * 0.35) / 90.0)
        w.append(x)
    s = sum(w)
    return [x / s for x in w]


# --------------------------------------------------------------------------- #
# 세그먼트 기반 가격 플랜
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Seg:
    """`n` 분 동안 총 `move` (비율) 이동. `vol` = 기준 거래량 배수."""
    n: int
    move: float = 0.0
    vol: float = 1.0
    wick_ppm: int = 1_200
    jitter_ppm: int = 900
    shape: str = "even"           # even | accel | decel
    tag: str = ""


def _seg_ppm(seg: Seg) -> list[int]:
    """세그먼트 총 이동을 분당 ppm 리스트로 분배 (로그 공간 균등/가속/감속)."""
    if seg.n <= 0:
        return []
    if seg.shape == "accel":
        w = [(i + 1) ** 1.6 for i in range(seg.n)]
    elif seg.shape == "decel":
        w = [(seg.n - i) ** 1.6 for i in range(seg.n)]
    else:
        w = [1.0] * seg.n
    tot = sum(w)
    total_log = math.log1p(seg.move)
    return [int(round(math.expm1(total_log * (x / tot)) * 1_000_000)) for x in w]


@dataclass
class _Plan:
    """한 매매일의 세션별 세그먼트 플랜 + 결측 구간."""
    segs: dict[str, list[Seg]] = field(default_factory=dict)
    #: (세션, 세션내 시작분, 길이분, 재개 갭 비율) — 행을 생성하지 않는 구간
    holes: list[tuple[str, int, int, float]] = field(default_factory=list)
    open_gap: dict[str, float] = field(default_factory=dict)   # 세션 시작 시 갭


def _fill(session: str, segs: list[Seg]) -> list[Seg]:
    """세션 길이에 맞게 마지막을 잔여 구간으로 패딩하거나 초과분을 절단."""
    n_target = SESSION_MINUTES[session][1] - SESSION_MINUTES[session][0]
    used = sum(s.n for s in segs)
    if used < n_target:
        return segs + [Seg(n=n_target - used, move=0.0,
                           vol=segs[-1].vol if segs else 1.0, tag="pad")]
    if used > n_target:
        out, left = [], n_target
        for s in segs:
            if left <= 0:
                break
            take = min(s.n, left)
            out.append(Seg(n=take, move=s.move * take / s.n, vol=s.vol, wick_ppm=s.wick_ppm,
                           jitter_ppm=s.jitter_ppm, shape=s.shape, tag=s.tag))
            left -= take
        return out
    return segs


# --------------------------------------------------------------------------- #
# 시나리오 플랜 정의
# --------------------------------------------------------------------------- #
def _quiet_plan() -> _Plan:
    """이벤트 없는 평범한 날 — 시간대 보정 RVOL 곡선의 분모를 만드는 데 쓴다."""
    return _Plan(segs={
        "day": _fill("day", [Seg(n=470, move=0.0, vol=0.6, wick_ppm=2500)]),
        "pre": _fill("pre", [Seg(n=330, move=0.0, vol=0.9, wick_ppm=1800)]),
        "regular": _fill("regular", [Seg(n=390, move=0.0, vol=1.0, wick_ppm=1500,
                                         jitter_ppm=1100)]),
        "after": _fill("after", [Seg(n=120, move=0.0, vol=0.9, wick_ppm=2200)]),
    })


def _plan_for(kind: str) -> _Plan:
    if kind == "coil_pop":
        # 코일(저변동·거래량 서서히 증가) → 폭발 → 완만한 페이드. HOD 장중.
        return _Plan(
            segs={
                "day": _fill("day", [Seg(n=470, move=0.01, vol=0.6, wick_ppm=2500)]),
                "pre": _fill("pre", [Seg(n=270, move=0.02, vol=0.8, wick_ppm=1500),
                                     Seg(n=60, move=0.05, vol=1.8, wick_ppm=1800, tag="warmup")]),
                "regular": _fill("regular", [
                    Seg(n=75, move=0.012, vol=0.85, wick_ppm=700, jitter_ppm=350, tag="coil"),
                    Seg(n=16, move=0.38, vol=12.0, wick_ppm=3500, jitter_ppm=2500,
                        shape="accel", tag="pump"),
                    Seg(n=14, move=0.06, vol=6.0, wick_ppm=3000, tag="peak"),
                    Seg(n=120, move=-0.11, vol=2.4, wick_ppm=2200, shape="decel", tag="fade"),
                    Seg(n=165, move=0.03, vol=1.2, wick_ppm=1500, tag="drift"),
                ]),
                "after": _fill("after", [Seg(n=120, move=-0.02, vol=1.0, wick_ppm=2500)]),
            },
            open_gap={"regular": 0.01},
        )

    if kind == "instant":
        # 전조 없음: 단일 분봉 폭발 (La Morgia — 정보는 시작 직후 수 초에 집중).
        return _Plan(
            segs={
                "day": _fill("day", [Seg(n=470, move=0.0, vol=0.5, wick_ppm=2500)]),
                "pre": _fill("pre", [Seg(n=330, move=0.005, vol=0.7, wick_ppm=1500)]),
                # 급등 전까지는 거래량도 완전히 평범해야 한다("전조 없음"이 이 시나리오의 요지).
                # 그래서 vol_scale 은 노이즈 수준(1.2)이고 폭발 구간만 배수를 키운다.
                "regular": _fill("regular", [
                    Seg(n=120, move=0.008, vol=1.0, wick_ppm=900, tag="flat"),
                    Seg(n=1, move=0.22, vol=300.0, wick_ppm=6000, jitter_ppm=0, tag="pump"),
                    Seg(n=4, move=0.09, vol=110.0, wick_ppm=4000, tag="pump2"),
                    Seg(n=60, move=-0.12, vol=18.0, wick_ppm=2500, shape="decel", tag="fade"),
                    Seg(n=205, move=-0.03, vol=5.0, wick_ppm=1500, tag="drift"),
                ]),
                "after": _fill("after", [Seg(n=120, move=-0.01, vol=0.9, wick_ppm=2500)]),
            },
        )

    if kind == "fade":
        # 프리마켓 갭업 러너: HOD 개장 15분 내, 종일 페이드, VWAP 아래 마감 (docs/02 §2.4).
        return _Plan(
            segs={
                "day": _fill("day", [Seg(n=470, move=0.03, vol=0.9, wick_ppm=3000)]),
                "pre": _fill("pre", [
                    Seg(n=150, move=0.04, vol=1.2, wick_ppm=2000, tag="quiet"),
                    Seg(n=90, move=0.34, vol=9.0, wick_ppm=4000, shape="accel", tag="pump"),
                    Seg(n=90, move=0.05, vol=6.0, wick_ppm=3000, tag="hold"),
                ]),
                "regular": _fill("regular", [
                    Seg(n=9, move=0.09, vol=14.0, wick_ppm=4500, shape="decel", tag="peak"),
                    Seg(n=51, move=-0.22, vol=6.0, wick_ppm=3000, shape="decel", tag="fade1"),
                    Seg(n=180, move=-0.20, vol=2.0, wick_ppm=2000, tag="fade2"),
                    Seg(n=150, move=-0.08, vol=1.2, wick_ppm=1800, tag="fade3"),
                ]),
                "after": _fill("after", [Seg(n=120, move=-0.03, vol=1.0, wick_ppm=2500)]),
            },
            open_gap={"pre": 0.06, "regular": 0.03},
        )

    if kind == "dump":
        # 급등 직후 붕괴 + 하방 LULD 홀트(캔들 공백) + 재개 갭다운.
        return _Plan(
            segs={
                "day": _fill("day", [Seg(n=470, move=0.005, vol=0.5, wick_ppm=2500)]),
                "pre": _fill("pre", [Seg(n=330, move=0.03, vol=1.0, wick_ppm=1800)]),
                "regular": _fill("regular", [
                    Seg(n=30, move=0.11, vol=3.0, wick_ppm=1500, tag="build"),
                    Seg(n=15, move=0.40, vol=20.0, wick_ppm=5000, shape="accel", tag="pump"),
                    Seg(n=4, move=-0.22, vol=25.0, wick_ppm=6000, shape="accel", tag="dump"),
                    Seg(n=6, move=-0.06, vol=12.0, wick_ppm=5000, tag="halt_lead"),
                    Seg(n=60, move=-0.18, vol=4.0, wick_ppm=3500, tag="post_halt"),
                    Seg(n=275, move=-0.05, vol=1.3, wick_ppm=2000, tag="drift"),
                ]),
                "after": _fill("after", [Seg(n=120, move=-0.04, vol=1.1, wick_ppm=3000)]),
            },
            # 55분째부터 5분 하방 홀트, 재개 시 -7% 갭
            holes=[("regular", 55, 5, -0.07)],
        )

    if kind == "noise":
        # 이벤트 아님: 30분 최대 +6%, 당일 +9%, RVOL ~1.3. 검출되면 오탐.
        return _Plan(
            segs={
                "day": _fill("day", [Seg(n=470, move=0.005, vol=0.7, wick_ppm=2500)]),
                "pre": _fill("pre", [Seg(n=330, move=0.01, vol=1.1, wick_ppm=1800)]),
                "regular": _fill("regular", [
                    Seg(n=60, move=0.04, vol=1.4, wick_ppm=1500, tag="up"),
                    Seg(n=60, move=-0.03, vol=1.2, wick_ppm=1500, tag="down"),
                    Seg(n=90, move=0.05, vol=1.3, wick_ppm=1500, tag="up2"),
                    Seg(n=180, move=-0.02, vol=1.1, wick_ppm=1500, tag="drift"),
                ]),
                "after": _fill("after", [Seg(n=120, move=0.005, vol=0.9, wick_ppm=2200)]),
            },
        )

    if kind == "daymarket":
        # 한국 낮(데이마켓) 얇은 유동성 위 급등 → 프리에서 소멸 → 정규장 지속 실패.
        return _Plan(
            segs={
                "day": _fill("day", [
                    Seg(n=120, move=0.01, vol=0.5, wick_ppm=2500, tag="quiet"),
                    Seg(n=25, move=0.36, vol=14.0, wick_ppm=6000, shape="accel", tag="pump"),
                    Seg(n=15, move=0.04, vol=8.0, wick_ppm=5000, tag="peak"),
                    Seg(n=180, move=-0.24, vol=3.0, wick_ppm=4000, shape="decel", tag="fade"),
                    Seg(n=130, move=-0.06, vol=1.0, wick_ppm=3000, tag="drift"),
                ]),
                "pre": _fill("pre", [Seg(n=330, move=-0.05, vol=1.4, wick_ppm=2000)]),
                "regular": _fill("regular", [
                    Seg(n=60, move=-0.03, vol=2.0, wick_ppm=2000),
                    Seg(n=330, move=-0.02, vol=1.0, wick_ppm=1500),
                ]),
                "after": _fill("after", [Seg(n=120, move=0.0, vol=0.8, wick_ppm=2500)]),
            },
            open_gap={"pre": -0.02},
        )

    if kind == "halt_gap":
        # 결측 내성 테스트용: 상방 홀트 2회 + 수집 중단(12분 데이터 구멍).
        return _Plan(
            segs={
                "day": _fill("day", [Seg(n=470, move=0.0, vol=0.4, wick_ppm=2500)]),
                "pre": _fill("pre", [Seg(n=330, move=0.02, vol=0.9, wick_ppm=1800)]),
                "regular": _fill("regular", [
                    Seg(n=40, move=0.01, vol=0.9, wick_ppm=800, tag="coil"),
                    Seg(n=10, move=0.19, vol=15.0, wick_ppm=5000, shape="accel", tag="pump"),
                    Seg(n=10, move=0.16, vol=12.0, wick_ppm=5000, shape="accel", tag="pump2"),
                    Seg(n=30, move=-0.10, vol=4.0, wick_ppm=3000, tag="fade"),
                    Seg(n=300, move=-0.06, vol=1.2, wick_ppm=2000, tag="drift"),
                ]),
                "after": _fill("after", [Seg(n=120, move=-0.01, vol=0.9, wick_ppm=2500)]),
            },
            holes=[("regular", 50, 5, 0.05),      # 상방 LULD 홀트 (재개 갭업)
                   ("regular", 66, 5, 0.03),      # 두 번째 홀트
                   ("pre", 100, 12, 0.0)],        # 수집 중단 (갭 없음)
        )

    raise ValueError(f"unknown scenario kind: {kind!r} (choose from {SCENARIO_KINDS})")


# --------------------------------------------------------------------------- #
# 봉 생성
# --------------------------------------------------------------------------- #
def _step(price_u: int, ppm: int) -> int:
    return max(1, (price_u * (1_000_000 + ppm)) // 1_000_000)


def _bar(rng: random.Random, symbol: str, ts_ms: int, open_u: int, close_u: int,
         wick_ppm: int, vol_qu: int) -> dict:
    hi = _step(max(open_u, close_u), rng.randint(0, wick_ppm))
    lo = _step(min(open_u, close_u), -rng.randint(0, wick_ppm))
    return {"symbol": symbol, "ts_ms": ts_ms, "open_u": open_u,
            "high_u": hi, "low_u": max(1, lo), "close_u": close_u, "vol_qu": int(vol_qu)}


def _build_day_bars(rng: random.Random,
                    symbol: str,
                    md: UsMarketDay,
                    plan: _Plan,
                    start_price_u: int,
                    adv_shares: float,
                    vol_scale: float = 1.0) -> tuple[list[dict], dict[str, list[int]], int]:
    """한 매매일의 1분봉 생성.

    반환: (rows, tag_index, last_close_u).
    `tag_index`: 세그먼트 tag → 해당 구간 ts_ms 목록 (ground truth 산출용).
    """
    rows: list[dict] = []
    tag_index: dict[str, list[int]] = {}
    price_u = start_price_u

    for session, win in session_windows(md):
        segs = plan.segs.get(session)
        if not segs:
            continue
        n_total = (win.end_ms - win.start_ms) // MIN_MS
        weights = _minute_weights(session, n_total)
        session_total_qu = adv_shares * SESSION_VOL_SHARE[session] * vol_scale * MICRO

        gap = plan.open_gap.get(session, 0.0)
        if gap:
            price_u = _step(price_u, int(round(gap * 1_000_000)))

        holes = [(h[1], h[2], h[3]) for h in plan.holes if h[0] == session]

        m = 0
        for seg in segs:
            for base_ppm in _seg_ppm(seg):
                if m >= n_total:
                    break
                ts = win.start_ms + m * MIN_MS

                hole = next(((s, d, g) for (s, d, g) in holes if s <= m < s + d), None)
                if hole is not None:
                    if m == hole[0] + hole[1] - 1 and hole[2]:
                        price_u = _step(price_u, int(round(hole[2] * 1_000_000)))
                    m += 1
                    continue

                ppm = base_ppm + (rng.randint(-seg.jitter_ppm, seg.jitter_ppm)
                                  if seg.jitter_ppm else 0)
                open_u = price_u
                close_u = _step(open_u, ppm)

                vol_noise = math.exp(rng.gauss(0.0, 0.45))
                move_kick = 1.0 + abs(ppm) / 40_000.0
                qu = session_total_qu * weights[m] * seg.vol * vol_noise * move_kick
                if session in ("day", "after") and rng.random() < 0.22:
                    qu = 0.0                      # 얇은 세션의 체결 공백

                if qu <= 0:
                    price_u = close_u
                    m += 1
                    continue

                rows.append(_bar(rng, symbol, ts, open_u, close_u, seg.wick_ppm, int(qu)))
                if seg.tag:
                    tag_index.setdefault(seg.tag, []).append(ts)
                price_u = close_u
                m += 1

    return rows, tag_index, price_u


def _candles_df(rows: list[dict]) -> pd.DataFrame:
    dtypes = {c: "int64" for c in CANDLE_COLS if c != "symbol"}
    df = pd.DataFrame(rows, columns=CANDLE_COLS)
    if df.empty:
        return df.astype(dtypes)
    return df.sort_values("ts_ms").reset_index(drop=True).astype(dtypes)


# --------------------------------------------------------------------------- #
# 일봉 이력 (베이스라인용)
# --------------------------------------------------------------------------- #
def make_history_1d(symbol: str,
                    end_price_u: int,
                    n_days: int = 25,
                    adv_shares: float = DEFAULT_ADV_SHARES,
                    seed: int = 0,
                    cal_start: str = "2026-05-01",
                    et_offset_h: int = DEFAULT_ET_OFFSET_H) -> pd.DataFrame:
    """이벤트 직전까지의 일봉 `n_days` 개. 마지막 종가 == `end_price_u`."""
    rng = _rng(f"hist:{symbol}", seed)
    cal = make_calendar(n_days, start=cal_start, et_offset_h=et_offset_h)
    closes = [end_price_u]
    for _ in range(n_days - 1):
        closes.append(_step(closes[-1], -rng.randint(-45_000, 45_000)))
    closes.reverse()

    rows: list[dict] = []
    prev = closes[0]
    for md, close_u in zip(cal, closes):
        open_u = _step(prev, rng.randint(-15_000, 15_000))
        hi = _step(max(open_u, close_u), rng.randint(2_000, 35_000))
        lo = _step(min(open_u, close_u), -rng.randint(2_000, 35_000))
        vol = int(adv_shares * math.exp(rng.gauss(0.0, 0.35)) * MICRO)
        assert md.regular is not None
        rows.append({"symbol": symbol, "ts_ms": md.regular.start_ms, "open_u": open_u,
                     "high_u": hi, "low_u": max(1, lo), "close_u": close_u, "vol_qu": vol})
        prev = close_u
    return _candles_df(rows)


# --------------------------------------------------------------------------- #
# 랭킹 스냅샷
# --------------------------------------------------------------------------- #
FILLER_SYMBOLS = ("FILA", "FILB", "FILC", "FILD")

#: docs/01 §3.2 — 토스 쏠림도는 이 두 type 의 같은 심볼 amount 비율.
RANK_TYPES = ("MARKET_TRADING_AMOUNT", "TOSS_SECURITIES_TRADING_AMOUNT")


def _empty_rankings() -> pd.DataFrame:
    return pd.DataFrame(columns=RANKING_COLS).astype(
        {"snap_ms": "int64", "rank": "int64", "last_u": "int64",
         "vol_qu": "int64", "amount_u": "int64"})


def _make_rankings(rng: random.Random,
                   symbol: str,
                   df_1m: pd.DataFrame,
                   md: UsMarketDay,
                   entries: dict[str, int | None],
                   toss_share_curve: tuple[float, float],
                   interval_min: int = 1,
                   n_filler: int = len(FILLER_SYMBOLS)) -> pd.DataFrame:
    """`rankings_snap` 규약(long format)의 합성 스냅샷.

    `entries[ranking_type]` = 해당 랭킹 최초 진입 ts_ms (None=미진입).
    `toss_share_curve` = (최초 진입 시점 토스 비중, 세션 종료 시점 토스 비중) 선형 보간.
    """
    if df_1m.empty:
        return _empty_rankings()

    ts = df_1m["ts_ms"].to_numpy()
    close = df_1m["close_u"].to_numpy()
    cumvol = df_1m["vol_qu"].cumsum().to_numpy()

    sessions = session_windows(md)
    t_start, t_end = sessions[0][1].start_ms, sessions[-1][1].end_ms
    entered = [t for t in entries.values() if t is not None]
    t_first = min(entered) if entered else t_end

    rows: list[dict] = []
    i = 0
    for snap in range(t_start, t_end, interval_min * MIN_MS):
        while i + 1 < len(ts) and ts[i + 1] <= snap:
            i += 1
        if ts[i] > snap:
            continue
        last_u = int(close[i])
        vol_qu = int(cumvol[i])
        amount_u = int(last_u * (vol_qu / MICRO))

        span = max(1, t_end - t_first)
        frac = max(0.0, min(1.0, (snap - t_first) / span))
        share = toss_share_curve[0] + (toss_share_curve[1] - toss_share_curve[0]) * frac

        for rtype in RANK_TYPES:
            entry = entries.get(rtype)
            cands: list[tuple[str, int, int, int]] = []
            if entry is not None and snap >= entry:
                amt = int(amount_u * share) if rtype.startswith("TOSS") else amount_u
                cands.append((symbol, last_u, vol_qu, amt))
            for f in range(n_filler):
                base = int(4_000_000 * (f + 1) * (1.0 + 0.05 * rng.random()))
                famt = int(base * MICRO * (0.4 if rtype.startswith("TOSS") else 1.0))
                cands.append((FILLER_SYMBOLS[f], 10_000_000 + f * 1_000_000,
                              base * MICRO // 10, famt))
            cands.sort(key=lambda c: -c[3])
            for rank, (sym, lu, vq, amt) in enumerate(cands, start=1):
                rows.append({"snap_ms": snap, "ranking_type": rtype, "duration": "realtime",
                             "rank": rank, "symbol": sym, "last_u": lu,
                             "vol_qu": vq, "amount_u": amt})

    df = pd.DataFrame(rows, columns=RANKING_COLS)
    return df.astype({"snap_ms": "int64", "rank": "int64", "last_u": "int64",
                      "vol_qu": "int64", "amount_u": "int64"})


# --------------------------------------------------------------------------- #
# ground truth 측정 (labeling 모듈과 독립적으로 계산)
# --------------------------------------------------------------------------- #
def _vwap_last_u(df: pd.DataFrame) -> int | None:
    """세션 VWAP (마이크로달러).

    주의: `tp_u * vol_qu` 는 봉당 ~1e17, 390봉 누적 ~1e19 로 **int64 상한(9.2e18)을
    넘는다**. numpy/pandas 정수 누적은 조용히 오버플로하므로 Python 임의정밀도 int로만
    합산한다 (baselines.session_vwap_u 도 동일 규칙).
    """
    if df.empty:
        return None
    tps = ((df["high_u"] + df["low_u"] + df["close_u"]) // 3).tolist()
    vols = df["vol_qu"].tolist()
    den = sum(int(v) for v in vols)
    if den == 0:
        return None
    num = sum(int(t) * int(v) for t, v in zip(tps, vols))
    return num // den


def _first_price_trigger(day: pd.DataFrame, prev_close_u: int, window_min: int = 30,
                         ret_min: float = 0.15,
                         day_ret_min: float = 0.30) -> tuple[int | None, str | None]:
    """가격 조건(30분 +ret_min 또는 당일 +day_ret_min) 최초 충족 봉 — **나이브 참조 구현**.

    labeling 의 벡터화 rolling 구현과 의도적으로 다른 방식(좌측 포인터 스캔)으로 계산해
    교차 검증한다. RVOL 게이트는 적용하지 않으므로, 검출기의 T0 는 항상 이 시각 이후다.
    """
    ts = [int(x) for x in day["ts_ms"].tolist()]
    close = [int(x) for x in day["close_u"].tolist()]
    lo = 0
    for i in range(len(ts)):
        while ts[lo] < ts[i] - window_min * MIN_MS:
            lo += 1
        win_min = min(close[lo:i + 1])
        win_ok = close[i] / win_min - 1.0 >= ret_min
        day_ok = close[i] / prev_close_u - 1.0 >= day_ret_min
        if win_ok or day_ok:
            return ts[i], ("both" if (win_ok and day_ok) else ("win" if win_ok else "day"))
    return None, None


def _measure_truth(df_1m: pd.DataFrame, md: UsMarketDay, t0_ms: int | None,
                   prev_close_u: int, shares_out_qu: int) -> dict:
    sessions = session_windows(md)
    t_start, t_end = sessions[0][1].start_ms, sessions[-1][1].end_ms
    day = df_1m[(df_1m["ts_ms"] >= t_start) & (df_1m["ts_ms"] < t_end)]

    out: dict = {}
    if day.empty:
        return out

    hi = day["high_u"].to_numpy()
    hod_i = int(hi.argmax())
    out["hod_ms"] = int(day["ts_ms"].to_numpy()[hod_i])
    out["hod_u"] = int(hi[hod_i])
    out["day_close_u"] = int(day["close_u"].to_numpy()[-1])
    out["day_volume_qu"] = int(day["vol_qu"].sum())
    out["float_rotation"] = (out["day_volume_qu"] / shares_out_qu
                            if shares_out_qu else float("nan"))
    out["day_ret_from_prev_close"] = out["day_close_u"] / prev_close_u - 1.0
    out["hod_ret_from_prev_close"] = out["hod_u"] / prev_close_u - 1.0

    reg = md.regular
    if reg is not None:
        rg = df_1m[(df_1m["ts_ms"] >= reg.start_ms) & (df_1m["ts_ms"] < reg.end_ms)]
        if not rg.empty:
            vw = _vwap_last_u(rg)
            if vw is not None:
                out["regular_vwap_last_u"] = vw
                out["regular_close_u"] = int(rg["close_u"].to_numpy()[-1])
                out["closed_below_vwap"] = out["regular_close_u"] < vw
                out["vwap_close_rel"] = out["regular_close_u"] / vw - 1.0
            rg_hi = rg["high_u"].to_numpy()
            out["regular_hod_ms"] = int(rg["ts_ms"].to_numpy()[int(rg_hi.argmax())])
            out["hod_min_from_open"] = int((out["regular_hod_ms"] - reg.start_ms) // MIN_MS)

    if t0_ms is not None:
        # T0 의 종가가 기준가 = "T0 봉을 보고 진입"의 현실적 참조점.
        at = day[day["ts_ms"] <= t0_ms]
        base_u = int(at["close_u"].to_numpy()[-1]) if not at.empty else prev_close_u
        out["t0_base_close_u"] = base_u
        out["peak_ret_from_t0"] = out["hod_u"] / base_u - 1.0
        # "종가" = **정규장 마지막 봉** (labeling 의 ret_close 와 동일 기준). 애프터마켓까지
        # 포함한 값은 ret_lastbar_from_t0 로 따로 둔다.
        close_ref_u = out.get("regular_close_u", out["day_close_u"])
        out["ret_close_from_t0"] = close_ref_u / base_u - 1.0
        out["ret_lastbar_from_t0"] = out["day_close_u"] / base_u - 1.0
        w30 = day[(day["ts_ms"] > t0_ms) & (day["ts_ms"] <= t0_ms + 30 * MIN_MS)]
        out["ret_30m_from_t0"] = (int(w30["close_u"].to_numpy()[-1]) / base_u - 1.0
                                 if not w30.empty else None)
        after_peak = day[day["ts_ms"] >= out["hod_ms"]]
        thr = out["hod_u"] * 80 // 100
        hitn = after_peak[after_peak["low_u"] <= thr]
        out["peak_to_minus20_min"] = (
            int((int(hitn["ts_ms"].to_numpy()[0]) - out["hod_ms"]) // MIN_MS)
            if not hitn.empty else None)
    return out


def _find_gaps(df_1m: pd.DataFrame, md: UsMarketDay,
               min_gap_min: int = 2) -> dict[str, list[tuple[int, int]]]:
    """세션별 캔들 공백(홀트/체결공백/수집중단) — {세션: [(공백 시작 ts_ms, 길이 분)]}."""
    out: dict[str, list[tuple[int, int]]] = {}
    for name, win in session_windows(md):
        s = df_1m[(df_1m["ts_ms"] >= win.start_ms) & (df_1m["ts_ms"] < win.end_ms)]
        ts = s["ts_ms"].to_numpy()
        found = [(int(a) + MIN_MS, int((b - a) // MIN_MS) - 1)
                 for a, b in zip(ts[:-1], ts[1:])
                 if int((b - a) // MIN_MS) - 1 >= min_gap_min]
        out[name] = found
    return out


# --------------------------------------------------------------------------- #
# 공개 API
# --------------------------------------------------------------------------- #
#: 시나리오별 메타 — 이벤트 여부, T0 기준 세그먼트 tag, 랭킹 진입 지연(분, 음수=선행),
#: 토스 쏠림도 곡선, 기대 세션/결과, 다음날 갭, 거래량 배율.
SCENARIO_META: dict[str, dict] = {
    "coil_pop": {
        "is_event": True, "pump_tag": "pump",
        "rank_lag_min": {"MARKET_TRADING_AMOUNT": -18, "TOSS_SECURITIES_TRADING_AMOUNT": -9},
        "toss_share": (0.08, 0.52), "session": "regular", "outcome": "hold",
        "next_gap": -0.06, "vol_scale": 9.0},
    "instant": {
        "is_event": True, "pump_tag": "pump",
        "rank_lag_min": {"MARKET_TRADING_AMOUNT": 2, "TOSS_SECURITIES_TRADING_AMOUNT": 4},
        "toss_share": (0.05, 0.35), "session": "regular", "outcome": "fade",
        "next_gap": -0.04, "vol_scale": 1.2},
    "fade": {
        "is_event": True, "pump_tag": "pump",
        "rank_lag_min": {"MARKET_TRADING_AMOUNT": -30, "TOSS_SECURITIES_TRADING_AMOUNT": -5},
        "toss_share": (0.10, 0.45), "session": "pre", "outcome": "fade",
        "next_gap": -0.09, "vol_scale": 11.0},
    "dump": {
        "is_event": True, "pump_tag": "pump",
        "rank_lag_min": {"MARKET_TRADING_AMOUNT": 1, "TOSS_SECURITIES_TRADING_AMOUNT": 3},
        "toss_share": (0.06, 0.48), "session": "regular", "outcome": "dump",
        "next_gap": -0.15, "vol_scale": 10.0},
    "noise": {
        "is_event": False, "pump_tag": None, "rank_lag_min": {},
        "toss_share": (0.05, 0.09), "session": None, "outcome": None,
        "next_gap": 0.01, "vol_scale": 1.2},
    "daymarket": {
        "is_event": True, "pump_tag": "pump",
        "rank_lag_min": {"MARKET_TRADING_AMOUNT": 4, "TOSS_SECURITIES_TRADING_AMOUNT": -6},
        "toss_share": (0.20, 0.70), "session": "day", "outcome": "fade",
        "next_gap": -0.02, "vol_scale": 5.0},
    "halt_gap": {
        "is_event": True, "pump_tag": "pump",
        "rank_lag_min": {"MARKET_TRADING_AMOUNT": -10, "TOSS_SECURITIES_TRADING_AMOUNT": -4},
        "toss_share": (0.09, 0.50), "session": "regular", "outcome": "fade",
        "next_gap": -0.05, "vol_scale": 8.0},
}


def make_scenario(kind: str, seed: int = 0, *,
                  symbol: str | None = None,
                  shares_outstanding: int = DEFAULT_SHARES_OUT,
                  adv_shares: float = DEFAULT_ADV_SHARES,
                  base_price_u: int = DEFAULT_BASE_PRICE_U,
                  cal_start: str = "2026-06-01",
                  include_next_day: bool = True,
                  history_days: int = 3,
                  et_offset_h: int = DEFAULT_ET_OFFSET_H,
                  half_days: Iterable[str] | None = None) -> tuple[pd.DataFrame, dict]:
    """(df_1m, truth_labels) 반환. kind: coil_pop|instant|fade|dump|noise|daymarket|halt_gap.

    df_1m 컬럼: symbol, ts_ms, open_u, high_u, low_u, close_u, vol_qu (전부 int64,
    ts_ms 오름차순). 체결 공백/홀트 구간은 행이 아예 없다.

    구성: `history_days` 개의 평범한 날(이벤트 없음) → 이벤트 당일 → (선택) 다음날.
    시간대 보정 RVOL 의 분모는 이력일로 만들어야 하므로 기본 3일을 붙인다.
    분모용 캘린더는 `truth["baseline_calendar"]`(이벤트 당일 제외)을 쓸 것.

    truth 주요 키
        is_event               급등 이벤트를 포함하는가 (noise=False)
        t0_expected_ms         기대 T0 (pump 세그먼트 첫 봉). 검출기는 tolerance 내면 통과
        t0_tolerance_min       권장 허용 오차(분)
        session_expected       T0 가 속한 세션
        hod_ms / hod_u         당일(4세션 전체) 고가 시각·가격 — 독립 계산
        peak_ret_from_t0       T0 종가 대비 HOD 수익률
        ret_close_from_t0      T0 종가 대비 당일 마지막 종가 수익률
        closed_below_vwap      정규장 종가가 정규장 VWAP 아래인가
        vwap_close_rel         정규장 종가/VWAP - 1
        hod_min_from_open      정규장 HOD의 개장 후 경과분 (docs/02 §2.4 기저율 검증용)
        float_rotation         당일 누적거래량 / 발행주식수
        peak_to_minus20_min    피크 → 피크대비 -20% 도달 분 (미도달 None)
        ranking_first_entry_ms {ranking_type: ts_ms|None}
        ranking_lead_lag_min   {ranking_type: 진입-T0 분. 음수=선행}
        next_day_gap           다음날 시가/당일 종가 - 1 (include_next_day=False 면 None)
        gaps                   캔들 공백 [(시작 ts_ms, 길이 분)]
        calendar / market_day  UsMarketDay 목록, 이벤트 당일
        df_1d                  이벤트 직전까지 일봉 이력 (베이스라인용)
        rankings               랭킹 스냅샷 DataFrame (long format, 2개 type)
        prev_close_u           전일 정규장 종가
        shares_outstanding_qu  발행주식수(마이크로주)
    """
    if kind not in SCENARIO_KINDS:
        raise ValueError(f"unknown scenario kind: {kind!r} (choose from {SCENARIO_KINDS})")
    meta = SCENARIO_META[kind]
    sym = symbol or f"SY{_stable_hash(kind) % 900 + 100}"
    rng = _rng(kind, seed)
    plan = _plan_for(kind)

    n_days = history_days + (2 if include_next_day else 1)
    cal = make_calendar(n_days, start=cal_start, et_offset_h=et_offset_h,
                        half_days=half_days)
    md = cal[history_days]

    # 이벤트 전 평범한 날들 — RVOL 곡선의 분모(시간대 보정 베이스라인) 재료
    rows: list[dict] = []
    price_u = base_price_u
    for hmd in cal[:history_days]:
        hrows, _htags, price_u = _build_day_bars(
            rng, sym, hmd, _quiet_plan(), price_u, adv_shares, vol_scale=1.0)
        rows.extend(hrows)

    # 전일 종가 = 직전 매매일의 **정규장** 마지막 종가 (없으면 base_price_u)
    prev_close_u = base_price_u
    if history_days and rows:
        preg = cal[history_days - 1].regular
        prior = [r for r in rows if preg.start_ms <= r["ts_ms"] < preg.end_ms]
        if prior:
            prev_close_u = int(prior[-1]["close_u"])

    ev_rows, tags, last_close_u = _build_day_bars(
        rng, sym, md, plan, price_u, adv_shares, vol_scale=meta["vol_scale"])
    rows.extend(ev_rows)

    if include_next_day:
        gap = meta["next_gap"]
        nd_plan = _Plan(
            segs={"pre": _fill("pre", [Seg(n=330, move=gap * 0.4, vol=1.5, wick_ppm=2500)]),
                  "regular": _fill("regular", [
                      Seg(n=60, move=gap * 0.4, vol=3.0, wick_ppm=3000),
                      Seg(n=330, move=-0.03, vol=1.0, wick_ppm=2000)])},
            open_gap={"pre": gap * 0.2},
        )
        nd_rows, _nd_tags, _ = _build_day_bars(
            rng, sym, cal[history_days + 1], nd_plan, last_close_u, adv_shares,
            vol_scale=max(1.5, meta["vol_scale"] * 0.35))
        rows = rows + nd_rows

    df_1m = _candles_df(rows)

    pump_tag = meta["pump_tag"]
    pump_start_ms = min(tags[pump_tag]) if (pump_tag and tags.get(pump_tag)) else None

    # T0 정답: 나이브 참조 구현으로 "가격 조건 최초 충족" 봉을 직접 찾는다.
    sess = session_windows(md)
    day_df = df_1m[(df_1m["ts_ms"] >= sess[0][1].start_ms)
                   & (df_1m["ts_ms"] < sess[-1][1].end_ms)]
    t0_expected_ms, t0_reason = _first_price_trigger(day_df, prev_close_u)

    truth = _measure_truth(df_1m, md, t0_expected_ms, prev_close_u,
                           shares_outstanding * MICRO)

    # 갭 정의는 labeling 과 동일하게 **정규장 시가 대비 전일 정규장 종가** (docs/07 §3.6)
    next_day_gap: float | None = None
    if include_next_day and truth.get("regular_close_u"):
        nreg = cal[history_days + 1].regular
        nd = df_1m[(df_1m["ts_ms"] >= nreg.start_ms) & (df_1m["ts_ms"] < nreg.end_ms)]
        if not nd.empty:
            next_day_gap = int(nd["open_u"].to_numpy()[0]) / truth["regular_close_u"] - 1.0

    entries: dict[str, int | None] = {}
    for rtype in RANK_TYPES:
        lag = meta["rank_lag_min"].get(rtype)
        entries[rtype] = (None if (lag is None or t0_expected_ms is None)
                          else max(sess[0][1].start_ms, t0_expected_ms + lag * MIN_MS))
    rankings = _make_rankings(rng, sym, day_df, md, entries, meta["toss_share"])

    truth.update({
        "kind": kind,
        "symbol": sym,
        "seed": seed,
        "is_event": bool(meta["is_event"]),
        "t0_expected_ms": t0_expected_ms,
        "t0_expected_reason": t0_reason,
        "t0_tolerance_min": 5,
        "pump_start_ms": pump_start_ms,
        "session_expected": meta["session"],
        "expected_outcome": meta["outcome"],
        "next_day_gap": next_day_gap,
        "gaps": _find_gaps(df_1m, md),
        "calendar": cal,
        "market_day": md,
        "history_days": history_days,
        "event_day_index": history_days,
        # RVOL 곡선의 분모는 **이벤트 당일을 제외한** 날들로 만들어야 한다 (docs/07 §2.4)
        "baseline_calendar": cal[:history_days],
        "prev_close_u": prev_close_u,
        "shares_outstanding_qu": shares_outstanding * MICRO,
        "adv_shares": adv_shares,
        "df_1d": make_history_1d(sym, prev_close_u, adv_shares=adv_shares, seed=seed),
        "rankings": rankings,
        "ranking_first_entry_ms": dict(entries),
        "ranking_lead_lag_min": {
            k: (None if (v is None or t0_expected_ms is None)
                else int((v - t0_expected_ms) // MIN_MS))
            for k, v in entries.items()},
    })
    return df_1m, truth


@dataclass(frozen=True)
class SynthBundle:
    """여러 시나리오를 합친 데이터셋 (평가·리포트 테스트용)."""
    df_1m: pd.DataFrame
    df_1d: pd.DataFrame
    rankings: pd.DataFrame
    calendar: list[UsMarketDay]
    truths: list[dict]

    @property
    def symbols(self) -> list[str]:
        return [t["symbol"] for t in self.truths]

    def truth_for(self, symbol: str) -> dict:
        for t in self.truths:
            if t["symbol"] == symbol:
                return t
        raise KeyError(symbol)


def make_dataset(counts: dict[str, int] | None = None, seed: int = 0,
                 cal_start: str = "2026-06-01") -> SynthBundle:
    """시나리오별 `n` 개씩 생성해 하나의 번들로 합친다 (심볼 자동 부여)."""
    counts = counts or {k: 2 for k in SCENARIO_KINDS}
    frames_1m, frames_1d, frames_rank, truths = [], [], [], []
    idx = 0
    for kind, n in counts.items():
        for j in range(n):
            idx += 1
            sym = f"S{idx:03d}{kind[0].upper()}"
            df_1m, truth = make_scenario(kind, seed=seed * 100 + j, symbol=sym,
                                         cal_start=cal_start)
            frames_1m.append(df_1m)
            frames_1d.append(truth["df_1d"])
            frames_rank.append(truth["rankings"])
            truths.append(truth)
    df_1m = pd.concat(frames_1m, ignore_index=True).sort_values(
        ["symbol", "ts_ms"]).reset_index(drop=True)
    df_1d = pd.concat(frames_1d, ignore_index=True).sort_values(
        ["symbol", "ts_ms"]).reset_index(drop=True)
    rankings = pd.concat(frames_rank, ignore_index=True).sort_values(
        ["snap_ms", "ranking_type", "rank"]).reset_index(drop=True)
    return SynthBundle(df_1m=df_1m, df_1d=df_1d, rankings=rankings,
                       calendar=truths[0]["calendar"], truths=truths)


def drop_random_bars(df_1m: pd.DataFrame, frac: float = 0.05, seed: int = 0) -> pd.DataFrame:
    """무작위 결측 주입 (수집 누락 내성 테스트용)."""
    rng = _rng("drop", seed)
    keep = [rng.random() >= frac for _ in range(len(df_1m))]
    return df_1m[pd.Series(keep, index=df_1m.index)].reset_index(drop=True)

"""검출기 — 계약 C-8. 소유: W4. 피처 계산은 `tossmon.analysis` 재사용 (중복 구현 금지).

두 개의 독립 경로
----------------
W3 실측이 보여준 사실: `coil_pop` 은 T0 이전 누적 RVOL 이 16 을 넘어 **전조로 잡히지만**,
`instant` 는 T0 이전 RVOL 이 1.2(완전 정상)이고 T0 봉에서만 7.5 로 튄다. 즉 즉발형은
원리상 전조 탐지가 불가능하다(La Morgia 문헌과 정합). 그래서 경로를 둘로 나눈다:

* **전조 경로** `precursor_score` — T0 이전 축적(RVOL 궤적·토스 쏠림도·코일)을 본다.
  리드타임이 있는 승격이지만 즉발형은 놓친다.
* **확인 경로** `confirm_score` — T0 봉 자체의 폭발(봉 거래량 z, 5분 수익률, 코일 급반전)을 본다.
  리드타임은 없지만 "시작 후 수 분 내 확인-진입" 이 가능한 신호다.

둘 중 큰 쪽으로 티어를 올리되 승격 사유(`reason`)에 어느 경로였는지를 남긴다 —
`promotions` 테이블이 나중에 두 경로의 리드타임을 따로 평가할 수 있어야 하기 때문이다.

스코어 설계 규약
--------------
* 모든 피처는 **버킷/램프로 [0,1] 정규화**한 뒤 가중합한다. `rvol_curve_5` 처럼 분모가 작아
  100배를 넘길 수 있는 값은 절대 스케일로 쓰지 않는다.
* `coil_score` 는 부호가 반직관적이다: **양수 = 변동성 수축 진행**(전조),
  **강한 음수 = 방금 폭발 시작**(확인). 두 경로가 이 부호를 반대로 쓴다.
* 미가용(NaN) 피처는 **0점**이다. 가용한 것만으로 정규화하지 않는다 — 데이터가 없을수록
  점수가 낮아야 승격이 보수적으로 일어난다.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Iterable

import pandas as pd

from ..analysis.baselines import minute_of_session_volume_curve, rvol_series
from ..analysis.features import extract_precursor_features
from ..analysis.labeling import EventParams, detect_events
from ..api.models import Price, UsMarketDay

MIN_MS = 60_000
DAY_MS = 86_400_000
_NAN = float("nan")

#: 계약 C-6 `events` 의 고정 7컬럼. 나머지 라벨은 전부 meta_json 으로 간다.
EVENT_CORE_COLUMNS = ("t0_ms", "kind", "peak_ms", "peak_ret", "ret_30m", "ret_close", "session")

#: 토스 쏠림도 피처가 볼 랭킹 2종. **`loops.FEATURE_RANKING_TYPES` 와 같아야 한다.**
#:
#: 수집 목록(`loops.RANKING_TYPES`)이 아니다 — 2026-08-07 부터 수집은 `TOP_GAINERS`(1d)를
#: 포함한 3종이지만, 쏠림도는 두 목록의 **순위 대비**라 같은 집계창(둘 다 `realtime`)이라야
#: 뜻이 있다. 실측상 `1d` 의 `vol_qu` 는 같은 순간 `realtime` 의 중앙값 15.4배다.
#:
#: `features.py` 기본값은 아직 금액 2종(…_AMOUNT)인데, 2026-08-04 부터 수집기는 거래량
#: 2종만 받는다(1391c6e). 넘기지 않으면 두 프레임이 비어 `_toss_concentration_features`
#: 가 **예외 없이 0** 을 내고, `score_paths` 의 가중치 0.18(toss_share 0.10 +
#: toss_share_slope_30 0.04 + toss_in_ranking 0.04)이 조용히 사라진다. tier3 임계 0.60 은
#: 이 항이 살아 있을 때 잡은 값이라 승격이 소리 없이 줄어든다.
#:
#: `loops` 를 import 해서 맞추지 않는 이유는 순환 참조다(loops -> detector). 대신 두
#: 목록이 어긋나면 테스트가 잡는다.
#:
#: 두 `amount_u` 의 **비율**은 계약 C-2 상 적법하다 — 금지된 것은 달러 금액으로 쓰는
#: 것이고, 같은 스냅 두 값의 비는 무차원이라 micro-KRW 계수가 상쇄된다.
RANKING_TOSS_TYPE = "TOSS_SECURITIES_TRADING_VOLUME"
RANKING_MARKET_TYPE = "MARKET_TRADING_VOLUME"

#: 티어별 (승격 임계, 강등 임계). 강등선이 낮아 그 사이가 히스테리시스 밴드다.
DEFAULT_THRESHOLDS: dict[int, tuple[float, float]] = {2: (0.35, 0.22), 3: (0.60, 0.42)}

#: 티어 정원이 찼을 때, 최약체를 밀어내려면 이만큼 더 높아야 한다 (자리 뺏기 플래핑 방지).
EVICTION_MARGIN = 0.08

#: 활동·랭킹 유래 승격이 tier2 진입 경쟁에 들고 들어가는 **고정 점수** (2026-08-04).
#:
#: 임의값이 아니라 tier2 강등선(DEFAULT_THRESHOLDS[2][1]=0.22)에 EVICTION_MARGIN 을
#: 더한 값이다. 이 한 상수가 세 가지를 동시에 만든다:
#:   (a) **회전 복원** — 이미 유지선(0.22) 아래로 떨어진 점유자는 밀어낼 수 있다.
#:       빈 슬롯만 기다리면 tier2 가 닫힌 집합이 되고, tier2 는 tier3 의 유일한 진입로라
#:       tier3 가 말라죽는다 (2026-08-04 라이브: 배포 후 evicted=0, tier3 10->3).
#:   (b) **진동 차단** — 활동 진입끼리는 동점이라 마진 때문에 서로 밀어내지 못한다.
#:       (0.30 + 0.08 >= 0.30) 이것이 8/03 요동의 재발을 막는다.
#:   (c) **진짜 표적 보호** — 실제 검출 스코어가 0.22 이상인 종목은 활동 신호가
#:       건드리지 못한다. 포화된 activity_score(1.000)가 0.3~0.6 짜리 표적을 밀어내던
#:       것이 원래 결함이었다.
ACTIVITY_ENTRY_SCORE = 0.30

#: 약함이 **입증되어** 내려온 종목(evicted/score_decay)을 활동 신호로 다시 올리기까지의
#: 대기 시간(초). 봉 데이터로 이미 유지선 아래임이 확인된 종목을 몇 분 만에 재표집하는 것은
#: 예산 낭비이자 진동이다(승격마다 이력 백필이 따라붙는다). 데이터가 없어서 내려온
#: `stale` 에는 적용하지 않는다 — 그건 약함의 증거가 아니다.
#: 실제 검출 스코어 경로(`on_new_data`)는 이 쿨다운을 무시한다 — 증거는 쿨다운을 이긴다.
ACTIVITY_REENTRY_COOLDOWN_S = 600

def _fill_floor(tier: int) -> float:
    """정원 채우기의 최저 자격 점수 = **그 티어의 유지선**(tier3 = 0.42).

    빈 슬롯이 손실이라고 해서 아무거나 올리면 "tier3 = 뜨거운 종목"이라는 의미가 깨진다
    — 리플레이 회귀가 실제로 이걸 잡았다(조용한 종목이 정원을 채워 tier3 에 올라갔다).
    원칙: **그 티어에서 곧바로 강등할 종목은 승격시키지 않는다.** 유지선이 자연스러운
    하한이고, 절대 승격선(0.60)을 못 넘어도 유지선 위면 정원을 채울 자격이 있다.
    """
    return DEFAULT_THRESHOLDS.get(tier, (0.0, 0.0))[1]

#: `first_print`(체결 개시) 는 A2 §3 의 1순위 트리거라 스코어와 무관하게 이 티어로 올린다.
FIRST_PRINT_TIER = 2
#: staleness 가 이 비율 이하로 급감하면 "깨어남" 으로 본다.
STALENESS_DROP_RATIO = 0.25
#: staleness 급감을 활동 신호로 인정하는 최소 직전 정체 시간 (초).
STALENESS_DORMANT_S = 900.0


# --------------------------------------------------------------------------- #
# 정규화 헬퍼
# --------------------------------------------------------------------------- #
def _ok(value: float | None) -> bool:
    return value is not None and isinstance(value, (int, float)) and not math.isnan(float(value))


def _ramp(value: float | None, lo: float, hi: float) -> float:
    """[lo, hi] 를 [0,1] 로 선형 매핑 후 클리핑. NaN → 0."""
    if not _ok(value) or hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (float(value) - lo) / (hi - lo)))


def _bucket(value: float | None, thresholds: tuple[float, float, float],
            scores: tuple[float, float, float] = (0.35, 0.7, 1.0)) -> float:
    """임계 3단 버킷. 절대 스케일이 신뢰할 수 없는 피처는 전부 이걸로 쓴다."""
    if not _ok(value):
        return 0.0
    v = float(value)
    if v >= thresholds[2]:
        return scores[2]
    if v >= thresholds[1]:
        return scores[1]
    if v >= thresholds[0]:
        return scores[0]
    return 0.0


def _binary(value: float | None) -> float:
    return 1.0 if _ok(value) and float(value) > 0 else 0.0


def _weighted(parts: Iterable[tuple[float, float]]) -> float:
    """(weight, component) → 가중합. 가중치 합이 1 이므로 결과는 [0,1]."""
    return max(0.0, min(1.0, sum(w * c for w, c in parts)))


# --------------------------------------------------------------------------- #
# 계약 C-8: precursor_score  (전조 경로)
# --------------------------------------------------------------------------- #
def precursor_score(feats: dict[str, float]) -> float:
    """T0 이전 축적 신호 → [0,1]. 키는 `analysis.features.feature_names()` 를 가정한다."""
    f = feats.get
    vol_z = max(_bucket(f("vol_z_5"), (1.0, 2.0, 3.0)),
                _bucket(f("vol_z_15"), (1.0, 2.0, 3.0)))
    awakening = _ramp(f("dormant_ratio_prior_day"), 0.3, 0.9) * (
        1.0 - _ramp(f("no_print_ratio_5"), 0.0, 1.0))
    return _weighted((
        # 거래량 축적 — 전조의 본체 (docs/03 §3 검증질문 1)
        (0.20, _bucket(f("rvol_at_cutoff"), (1.5, 3.0, 5.0))),
        (0.12, vol_z),
        (0.10, _bucket(f("vol_bar_z_max_60"), (2.0, 3.0, 4.0))),
        (0.06, _ramp(f("vol_slope_30"), 0.0, 0.05)),
        # 토스 쏠림도 (docs/01 §3.2)
        (0.10, _bucket(f("toss_share"), (0.10, 0.25, 0.45))),
        (0.04, _ramp(f("toss_share_slope_30"), 0.0, 0.01)),
        (0.04, _binary(f("toss_in_ranking"))),
        # 가격 궤적 — 양수 coil = 수축 진행 (폭발 전 압축)
        (0.06, _ramp(f("coil_score"), 0.0, 1.5)),
        (0.05, _ramp(f("dist_from_vwap"), 0.0, 0.03)),
        (0.05, _bucket(f("new_high_count_30"), (1.0, 3.0, 6.0))),
        (0.04, _ramp(f("up_bar_ratio_30"), 0.5, 0.8)),
        (0.06, _ramp(f("ret_15"), 0.0, 0.08)),
        # 휴면 → 활동 개시 (A2 §3 의 봉 기반 프록시)
        (0.04, awakening),
        # 이력
        (0.02, _binary(f("former_runner"))),
        (0.02, _bucket(f("float_rotation_pre"), (0.05, 0.20, 0.50))),
    ))


# --------------------------------------------------------------------------- #
# 확인 경로 (즉발형 대응)
# --------------------------------------------------------------------------- #
def confirm_score(feats: dict[str, float]) -> float:
    """T0 봉 자체의 폭발 강도 → [0,1]. `include_t0=True` 로 뽑은 피처에만 의미가 있다.

    전조가 없는 즉발형(`instant`)을 잡는 유일한 경로다. 리드타임을 주지 않는 대신
    "시작 후 수 분 내 확인" 을 가능하게 한다.

    **C-7 개정 A2 이후 `include_t0=True` 가 기본값이자 정상 컷오프다** — 종료 라벨이라
    T0 봉은 t0 에 완결이고, 이 경로는 그 봉을 읽는 것이 존재 이유다(룩어헤드가 아니다).
    `include_t0=False` 는 이제 "연구용 엄격"이 아니라 **1봉 과보수 모드 표기**이며,
    그 모드에서는 `rvol_at_cutoff`·`ret_5` 가 T0 봉을 못 봐 이 경로가 통째로 무의미해진다.
    **랭킹 컷(`snap_ms < t0_ms`)은 이 플래그와 무관하게 항상 엄격이다** (A2 §2).
    """
    f = feats.get
    coil_flip = _ramp(-float(f("coil_score")) if _ok(f("coil_score")) else _NAN, 0.5, 2.5)
    near_hod = _ramp(f("dist_from_hod"), -0.05, 0.0)
    return _weighted((
        (0.28, _bucket(f("rvol_at_cutoff"), (2.0, 4.0, 7.0))),
        (0.22, _bucket(f("vol_bar_z_max_60"), (2.5, 4.0, 6.0))),
        (0.20, _ramp(f("ret_5"), 0.03, 0.15)),
        (0.12, coil_flip),
        (0.08, near_hod),
        (0.10, _bucket(f("vol_z_5"), (1.5, 3.0, 4.5))),
    ))


def score_paths(feats: dict[str, float]) -> tuple[float, str, float, float]:
    """(채택 스코어, 경로명, 전조 스코어, 확인 스코어)."""
    p, c = precursor_score(feats), confirm_score(feats)
    return (p, "precursor", p, c) if p >= c else (c, "confirm", p, c)


# --------------------------------------------------------------------------- #
# A2 §3: /prices 파생 상태 (실시간 경로는 봉 프록시보다 이쪽이 정확하다)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PriceState:
    """`/prices` 한 건에서 파생되는 활동 상태.

    ⚠️ `last_u` 는 **현재가가 아니다**. 체결이 없어도 값이 오므로(직전 종가/체결가)
    수익률 계산에 쓰면 안 된다 (docs/06 §1-1). 여기서는 "변했는가" 만 본다.
    """
    symbol: str
    ts_ms: int | None
    last_u: int
    no_print: bool
    staleness_s: float
    first_print: bool
    price_changed: bool
    ts_advanced: bool
    prev_staleness_s: float
    awakened: bool

    def as_meta(self) -> dict[str, float | bool | None]:
        return {"ts_ms": self.ts_ms, "no_print": self.no_print,
                "staleness_s": None if math.isnan(self.staleness_s) else self.staleness_s,
                "first_print": self.first_print, "price_changed": self.price_changed,
                "ts_advanced": self.ts_advanced, "awakened": self.awakened}


class PriceActivityTracker:
    """폴링 간 `/prices` 상태 전이를 들고 있다가 A2 §3 파생값을 만든다."""

    def __init__(self) -> None:
        self._last_ts: dict[str, int | None] = {}
        self._last_u: dict[str, int] = {}
        self._last_staleness: dict[str, float] = {}
        self._seen: set[str] = set()

    def update(self, price: Price, now_ms: int) -> PriceState:
        sym = price.symbol
        seen = sym in self._seen
        prev_ts = self._last_ts.get(sym)
        prev_u = self._last_u.get(sym)
        prev_stale = self._last_staleness.get(sym, _NAN)

        no_print = price.ts_ms is None
        staleness = _NAN if no_print else max(0.0, (now_ms - int(price.ts_ms)) / 1000.0)
        # first_print: null → 값 전이. 첫 관측(비교 대상 없음)은 전이로 치지 않는다.
        first_print = seen and prev_ts is None and price.ts_ms is not None
        ts_advanced = (price.ts_ms is not None and prev_ts is not None
                       and int(price.ts_ms) > int(prev_ts))
        price_changed = prev_u is not None and int(price.last_u) != int(prev_u)
        awakened = first_print or (
            _ok(prev_stale) and _ok(staleness)
            and prev_stale >= STALENESS_DORMANT_S
            and staleness <= prev_stale * STALENESS_DROP_RATIO)

        self._seen.add(sym)
        self._last_ts[sym] = price.ts_ms
        self._last_u[sym] = int(price.last_u)
        self._last_staleness[sym] = staleness
        return PriceState(symbol=sym, ts_ms=price.ts_ms, last_u=int(price.last_u),
                          no_print=no_print, staleness_s=staleness, first_print=first_print,
                          price_changed=price_changed, ts_advanced=ts_advanced,
                          prev_staleness_s=prev_stale, awakened=bool(awakened))

    def prune(self, keep: set[str]) -> int:
        """감시 대상에서 빠진 심볼의 상태를 버린다 (장시간 실행 메모리 안정성)."""
        drop = self._seen - keep
        for sym in drop:
            self._last_ts.pop(sym, None)
            self._last_u.pop(sym, None)
            self._last_staleness.pop(sym, None)
            self._seen.discard(sym)
        return len(drop)

    def __len__(self) -> int:
        return len(self._seen)


def activity_score(state: PriceState) -> float:
    """Tier 1 스윕의 활동 점수 → [0,1].

    A2 §3 대로 **`first_print` 와 `staleness_s` 급감이 1순위**다. 조용하던 동전주가
    깨어나는 순간이 이 전략의 핵심이고, `lastPrice` 변화만 보면 그 순간을 놓친다.
    """
    if state.first_print:
        return 1.0
    if state.awakened:
        return 0.85
    score = 0.0
    if state.ts_advanced:
        score += 0.45
    if state.price_changed:
        score += 0.25
    if _ok(state.staleness_s):
        # 최근 체결일수록 활동적 — 60초 이내면 만점, 30분이면 0.
        score += 0.30 * (1.0 - _ramp(state.staleness_s, 60.0, 1800.0))
    return max(0.0, min(1.0, score))


# --------------------------------------------------------------------------- #
# 티어 상태머신
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TierChange:
    symbol: str
    from_tier: int
    to_tier: int
    reason: str
    score: float
    ts_ms: int


@dataclass
class _SymbolState:
    tier: int = 1
    score: float = 0.0
    #: None = 아직 관측/변경된 적 없음. 0 은 유효한 시각이므로 sentinel 로 쓰지 않는다.
    last_ts_ms: int | None = None
    changed_ms: int | None = None
    below_since_ms: int | None = None
    #: 약함 입증으로 강등된 뒤 활동 신호 재승격을 막는 시각 (0 = 제한 없음).
    reentry_block_ms: int = 0
    #: `on_new_data` 로 **실제 검출 스코어를 받은 적이 있는가**. 활동 신호로 들어온
    #: 미측정 종목과 봉 데이터로 판정된 종목을 구분한다 (정원 채우기의 자격 조건).
    scored: bool = False
    reason: str = "seed"


class TierStateMachine:
    """티어 승격/강등 + 히스테리시스 (플래핑 방지). promotions 기록은 호출측(Store).

    플래핑 방지는 세 겹이다:
      1. 승격선 > 강등선 (밴드)
      2. 강등은 `hysteresis_s` 동안 **연속으로** 강등선 아래일 때만
      3. 티어 변경 후 `hysteresis_s` 동안은 그 심볼을 다시 움직이지 않는다 (dwell)
    정원이 찬 티어에 들어가려면 최약체보다 `EVICTION_MARGIN` 이상 높아야 한다 —
    비슷한 점수끼리 자리를 주고받는 것도 플래핑이다.
    """

    def __init__(self, hysteresis_s: int, *,
                 thresholds: dict[int, tuple[float, float]] | None = None,
                 tier2_max: int | None = None, tier3_max: int | None = None,
                 stale_demote_s: float | None = None):
        self.hysteresis_s = int(hysteresis_s)
        self.thresholds = dict(thresholds or DEFAULT_THRESHOLDS)
        self.capacity: dict[int, int | None] = {2: tier2_max, 3: tier3_max}
        self.stale_demote_s = stale_demote_s
        self.states: dict[str, _SymbolState] = {}
        self.pending: list[TierChange] = []       # 정원 밀림 등 부수적 변경

    # ---- 조회 ----------------------------------------------------------

    def tier_of(self, symbol: str) -> int:
        st = self.states.get(symbol)
        return st.tier if st is not None else 1

    def members(self, tier: int) -> list[str]:
        return [s for s, st in self.states.items() if st.tier == tier]

    def at_least(self, tier: int) -> list[str]:
        return [s for s, st in self.states.items() if st.tier >= tier]

    def score_of(self, symbol: str) -> float:
        st = self.states.get(symbol)
        return st.score if st is not None else 0.0

    def reason_of(self, symbol: str) -> str:
        st = self.states.get(symbol)
        return st.reason if st is not None else "seed"

    def drain_changes(self) -> list[TierChange]:
        out, self.pending = self.pending, []
        return out

    def seed(self, symbol: str, tier: int, ts_ms: int = 0) -> None:
        """재시작 이어받기 — 이력 없이 티어만 복원한다 (승격 기록을 남기지 않는다)."""
        st = self.states.setdefault(symbol, _SymbolState())
        st.tier = int(tier)
        st.changed_ms = int(ts_ms)
        st.last_ts_ms = int(ts_ms)
        st.reason = "resume"

    # ---- 계약 표면 ------------------------------------------------------

    def on_new_data(self, symbol: str, score: float, ts_ms: int,
                    reason: str = "score") -> int | None:
        """새 tier 를 반환하거나, 변경 없으면 None."""
        st = self.states.setdefault(symbol, _SymbolState())
        st.score = float(score)
        st.scored = True                       # 봉 데이터로 실제 판정을 받았다
        st.last_ts_ms = int(ts_ms)

        target = self._target_tier(st.score)
        if target > st.tier:
            st.below_since_ms = None
            return self._change(symbol, st, min(target, st.tier + 1), reason, ts_ms)
        if target < st.tier:
            return self._maybe_demote(symbol, st, target, ts_ms)
        st.below_since_ms = None
        return None

    def force(self, symbol: str, tier: int, reason: str, score: float,
              ts_ms: int, *, compete: bool = True,
              record_score: float | None = None) -> int | None:
        """스코어와 무관한 승격 (A2 §3 `first_print`). 강등에는 쓰지 않는다.

        dwell 은 **우회하지 않는다** (감사 H-7/J-1): 랭킹 스냅샷은 12초마다 오므로
        여기가 dwell 을 건너뛰면 "강등 1ms 뒤 재승격" 왕복이 영구히 돈다.
        방금 강등된 심볼은 `hysteresis_s` 가 지나야 다시 올라올 수 있다.

        `compete=False` (활동 신호 유래 승격 — 2026-08-03 라이브 진단):
        **빈자리에만 들어가고 기존 멤버를 밀어내지 않으며, 스코어 채널도 오염시키지
        않는다.** `activity_score` 는 정상 거래 중인 종목이면 거의 전부 1.000 이 나오는
        비변별 신호라(정규장 실측: 1,167 종목이 14,497회 승격, 종목당 평균 12.4회),
        경쟁시키면 (a) 매 스윕마다 최약체를 축출해 1→2/2→1 왕복이 1:1 로 돌고
        (b) st.score 가 1.000 으로 굳어 **실제 검출 스코어(0.3~0.6) 를 가진 진짜 표적이
        영구히 최약체가 되어 축출된다.** 랭킹 진입(H-7)에서 이미 같은 결함을 고쳤다.
        """
        st = self.states.setdefault(symbol, _SymbolState())
        if compete:
            st.score = max(st.score, float(score))
        st.last_ts_ms = int(ts_ms)
        if tier <= st.tier:
            return None
        if ts_ms < st.reentry_block_ms:
            return None            # 이미 약함이 입증된 종목 — 재표집 대기 중
        st.below_since_ms = None
        return self._change(symbol, st, int(tier), reason, ts_ms,
                            may_evict=compete,
                            record_score=float(score if record_score is None
                                               else record_score))

    # ---- 유지보수 -------------------------------------------------------

    def sweep(self, now_ms: int) -> list[TierChange]:
        """데이터가 끊긴 상위 티어 심볼을 내린다 (상장폐지·심볼 오타·수집 실패)."""
        if not self.stale_demote_s:
            return []
        cutoff = now_ms - int(self.stale_demote_s * 1000)
        start = len(self.pending)
        for symbol, st in list(self.states.items()):
            if st.tier <= 1 or st.last_ts_ms is None or st.last_ts_ms > cutoff:
                continue
            if st.changed_ms is not None and now_ms - st.changed_ms < self.hysteresis_s * 1000:
                continue
            self._change(symbol, st, st.tier - 1, "stale", now_ms, ignore_dwell=True)
        # 변경은 pending 에 남긴다 — 기록은 호출측이 drain_changes() 로 한 번에 한다.
        return list(self.pending[start:])

    def set_capacity(self, *, tier2_max: int | None = None,
                     tier3_max: int | None = None, ts_ms: int = 0,
                     reason: str = "budget_shrink") -> list[TierChange]:
        """정원 축소 (BudgetGuard 지시). 넘치는 만큼 최약체부터 내린다."""
        if tier2_max is not None:
            self.capacity[2] = max(1, int(tier2_max))
        if tier3_max is not None:
            self.capacity[3] = max(1, int(tier3_max))
        start = len(self.pending)
        for tier in (3, 2):
            cap = self.capacity.get(tier)
            if cap is None:
                continue
            members = sorted(self.members(tier), key=lambda s: self.states[s].score)
            for symbol in members[:max(0, len(members) - cap)]:
                self._change(symbol, self.states[symbol], tier - 1, reason, ts_ms,
                             ignore_dwell=True)
        return list(self.pending[start:])

    def release(self, symbol: str, ts_ms: int, reason: str) -> int | None:
        """티어를 한 칸 내린다 — **자리를 빌려 준 쪽이 돌려받는 경로** (D-21).

        `_maybe_demote` 와 다르다: 저쪽은 *스코어가 약해졌다*는 판정이고 이쪽은
        *빌린 기간이 끝났다*는 사실이다. 그래서 dwell 을 무시하고(빌린 기간이 dwell 보다
        짧을 수 있다) `reentry_block_ms` 도 걸지 않는다 — 약함이 입증된 적이 없으므로
        재진입 쿨다운은 빌려 준 쪽이 자기 규칙으로 관리한다.
        """
        st = self.states.get(symbol)
        if st is None or st.tier <= 1:
            return None
        return self._change(symbol, st, st.tier - 1, reason, ts_ms, ignore_dwell=True)

    def fill_to_capacity(self, tier: int, ts_ms: int, *,
                         reason: str = "capacity_fill",
                         min_score: float | None = None,
                         reserve: int = 0) -> list[TierChange]:
        """빈 정원을 **바로 아래 티어의 측정된 최고 점수 후보**로 채운다.

        왜 필요한가 (2026-08-04 진단): tier3 진입이 **절대 임계(0.60)** 하나에만 걸려
        있어서, 그 임계를 넘는 일이 개장 직후(09:30~11:00 ET)에만 몰린다. 실측 시간대별
        tier3 승격은 22시 50건 / 23시 44건 -> **00시 이후 0건**이고, 그 뒤로는 stale 로
        빠져나가기만 해 정원 20 중 19가 장 내내 **빈 채로** 남았다. 빈 슬롯은 순손실이다
        (체결 테이프가 우리의 주 비용 측정 도구인데 0건이 된다).

        그래서 절대 임계 대신 **상대 순위**로 정원을 채운다: 측정된(`scored`) 후보 중
        점수 상위부터, dwell 을 지킨 것만, 빈자리 수만큼. 임계를 넘는 종목이 있으면
        그 종목이 자연히 1순위이므로 기존 경로를 밀어내지 않는다.

        `reserve` (D-21, 기본 0 = 지금 동작): 이만큼의 자리는 **채우지 않고 비워 둔다.**
        랭킹 차선이 `compete=False` 로 들어오려면(축출 금지) 빈자리가 있어야 하는데,
        이 함수가 매 사이클 정원을 꽉 채우므로 예약 없이는 차선이 **영원히 못 들어온다.**
        """
        cap = self.capacity.get(tier)
        if cap is None:
            return []
        floor = _fill_floor(tier) if min_score is None else min_score
        free = cap - len(self.members(tier)) - max(0, int(reserve))
        if free <= 0:
            return []
        cands = [s for s in self.members(tier - 1)
                 if self.states[s].scored and self.states[s].score >= floor
                 and (self.states[s].changed_ms is None
                      or ts_ms - self.states[s].changed_ms >= self.hysteresis_s * 1000)]
        cands.sort(key=lambda s: self.states[s].score, reverse=True)
        start = len(self.pending)
        for symbol in cands[:free]:
            self._change(symbol, self.states[symbol], tier, reason, ts_ms)
        return list(self.pending[start:])

    def prune(self, keep: set[str]) -> int:
        drop = [s for s in self.states if s not in keep and self.states[s].tier <= 1]
        for symbol in drop:
            self.states.pop(symbol, None)
        return len(drop)

    # ---- 내부 ----------------------------------------------------------

    def _target_tier(self, score: float) -> int:
        target = 1
        for tier in sorted(self.thresholds):
            if score >= self.thresholds[tier][0]:
                target = tier
        return target

    def _maybe_demote(self, symbol: str, st: _SymbolState, target: int,
                      ts_ms: int) -> int | None:
        down = self.thresholds.get(st.tier, (0.0, 0.0))[1]
        if st.score >= down:
            st.below_since_ms = None
            return None
        if st.below_since_ms is None:
            st.below_since_ms = ts_ms
            return None
        if ts_ms - st.below_since_ms < self.hysteresis_s * 1000:
            return None
        st.below_since_ms = None
        return self._change(symbol, st, max(target, st.tier - 1), "score_decay", ts_ms)

    def _change(self, symbol: str, st: _SymbolState, new_tier: int, reason: str,
                ts_ms: int, *, ignore_dwell: bool = False, may_evict: bool = True,
                record_score: float | None = None) -> int | None:
        if new_tier == st.tier:
            return None
        if not ignore_dwell and st.changed_ms is not None and \
                ts_ms - st.changed_ms < self.hysteresis_s * 1000:
            return None                                   # dwell — 아직 못 움직인다
        if new_tier > st.tier and not self._make_room(symbol, new_tier, st.score, ts_ms,
                                                      may_evict=may_evict):
            return None
        # 기록용 스코어: 비경쟁 승격은 st.score 를 올리지 않으므로(스코어 채널 보호)
        # promotions 테이블에는 실제 트리거 강도를 남긴다 — 관측을 잃지 않는다.
        change = TierChange(symbol=symbol, from_tier=st.tier, to_tier=new_tier,
                            reason=reason,
                            score=st.score if record_score is None else record_score,
                            ts_ms=int(ts_ms))
        if new_tier < st.tier and reason in ("evicted", "score_decay"):
            # 약함이 입증돼 내려간다 — 활동 신호의 즉시 재표집을 막는다.
            st.reentry_block_ms = int(ts_ms) + ACTIVITY_REENTRY_COOLDOWN_S * 1000
        st.tier = new_tier
        st.changed_ms = int(ts_ms)
        st.reason = reason
        self.pending.append(change)
        return new_tier

    def _make_room(self, symbol: str, tier: int, score: float, ts_ms: int, *,
                   may_evict: bool = True) -> bool:
        cap = self.capacity.get(tier)
        if cap is None:
            return True
        members = [s for s in self.members(tier) if s != symbol]
        if len(members) < cap:
            return True
        if not may_evict:
            return False            # 활동 신호 유래 승격은 빈자리에만 들어간다 (축출 금지)
        weakest = min(members, key=lambda s: self.states[s].score)
        if self.states[weakest].score + EVICTION_MARGIN >= score:
            return False                                  # 밀어낼 만큼 강하지 않다
        # 방금 티어가 바뀐 심볼은 밀어내지 않는다 — 축출↔재승격 왕복(핑퐁)의 씨앗이다.
        victim = self.states[weakest]
        if victim.changed_ms is not None and \
                ts_ms - victim.changed_ms < self.hysteresis_s * 1000:
            return False
        st = self.states[weakest]
        self.pending.append(TierChange(symbol=weakest, from_tier=st.tier,
                                       to_tier=st.tier - 1, reason="evicted",
                                       score=st.score, ts_ms=int(ts_ms)))
        st.tier -= 1
        st.changed_ms = int(ts_ms)
        st.reentry_block_ms = int(ts_ms) + ACTIVITY_REENTRY_COOLDOWN_S * 1000
        st.reason = "evicted"
        return True


# --------------------------------------------------------------------------- #
# 실시간 이벤트 검출
# --------------------------------------------------------------------------- #
def _jsonable(value):
    """numpy/NaN → JSON 안전값. NaN 은 None 으로 (json.dumps 의 NaN 토큰은 비표준)."""
    if value is None:
        return None
    if isinstance(value, (bool, str)):
        return value
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (AttributeError, ValueError):
            return str(value)
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, (int, str, bool)) or value is None:
        return value
    return str(value)


def _opt_str(value) -> str | None:
    return None if value is None else str(value)


def label_hash(row: dict) -> str:
    """검출 라벨의 지문. **라벨이 실제로 바뀌었는지** 판정하는 유일한 기준이다.

    `detect_events` 가 낸 행만 해싱한다 — 우리 쪽 기록용 메타(`detected_ms`,
    `detect_lag_min`, `score_*`)는 사이클마다 값이 달라지므로 섞으면 해시가 매번 바뀌어
    억제가 통째로 무력화된다.
    """
    payload = {k: _jsonable(v) for k, v in sorted(row.items())}
    blob = json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False,
                      allow_nan=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def event_record(row: dict, symbol: str, *, extra_meta: dict | None = None) -> dict:
    """`detect_events` 한 행 → `Store.record_event` 입력.

    계약 C-6 `events` 는 7컬럼 + meta_json 뿐이라, A1 §4 의 추가 라벨 18종은 전부
    `meta_json` 으로 넣는다. `analysis.report.expand_meta_json()` 이 되펼친다 —
    안 넣으면 리포트의 q6·기저율 대조가 통째로 빈다.
    """
    core = {k: _jsonable(row.get(k)) for k in EVENT_CORE_COLUMNS}
    meta = {k: _jsonable(v) for k, v in row.items()
            if k not in EVENT_CORE_COLUMNS and k != "symbol"}
    meta.update({k: _jsonable(v) for k, v in (extra_meta or {}).items()})
    out = {"symbol": symbol, **core}
    out["meta_json"] = json.dumps(meta, separators=(",", ":"), sort_keys=True,
                                  ensure_ascii=False, allow_nan=False)
    return out


@dataclass(frozen=True)
class EventEmission:
    """한 번의 `record_event` 대상. 최초 검출인지 **라벨 갱신**인지를 함께 들고 다닌다.

    T0 시점에는 `peak_ms`·`peak_ret`·`ret_close` 가 미확정이고 장이 진행되며 채워진다.
    그래서 같은 (symbol, t0_ms) 를 완전히 차단하면 라벨이 T0 시점 값에 얼어붙는다 —
    **라벨이 실제로 바뀐 경우에만** 다시 보내고, 그 외에는 억제한다.
    """
    record: dict
    t0_ms: int
    is_new: bool
    session: str | None = None

    @property
    def symbol(self) -> str:
        return str(self.record.get("symbol", ""))


@dataclass
class DetectionResult:
    symbol: str
    ts_ms: int
    score: float
    path: str
    precursor: float
    confirm: float
    feats: dict[str, float] = field(default_factory=dict)
    events: list[EventEmission] = field(default_factory=list)


class EventDetector:
    """새 1분봉이 들어올 때마다 스코어와 이벤트를 갱신한다.

    피처·이벤트 계산은 전부 `tossmon.analysis` 재사용이다 (중복 구현 금지).

    **컷오프는 계약 `docs/04` C-7 개정 A2 (2026-08-09)** 다. 이 파일의 다른 `A2 §n` 은
    전부 C-6/C-8 개정 A2(호가·테이프·`/prices`)를 가리키므로 반드시 구분해서 읽어라.

    A2 는 **봉과 스냅을 갈라놓았다.** 하나의 플래그로 같이 밀면 안 된다:

        캔들 `ts_ms  <= t0_ms`   — 봉은 **구간**이고 `ts_ms` 는 **종료 라벨**이라
                                   `ts_ms = T` 인 봉은 `[T−60초, T)` 를 담아 **T 에 이미
                                   완결**이다. 그래서 아래 `include_t0=True` 다.
        랭킹 `snap_ms <  t0_ms`  — 랭킹은 구간이 아니라 **순간**이고, 도착 시 **중앙
                                   16.1초 늙어 있다**(W1 실측, `docs/35`). `snap_ms = t0`
                                   인 스냅은 t0 에 손에 없다. **넓히면 진짜 룩어헤드다.**

    **A1 §1 에서 무엇이 바뀌었나** (이 자리에 있던 옛 주석이 인용하던 조항이다):
    A1 은 `include_t0=True` 의 근거를 *"W4 실시간 검출기는 T0 봉 종료 시점에 판정하므로"*
    라고 적었다. 그 문장은 **봉 라벨이 시작 시각이라는 전제**에서 나왔고 그 전제가 틀렸다 —
    종료 라벨이면 T0 봉은 실시간 검출기만이 아니라 **누구에게나** t0 에 관측 가능하다.
    즉 이 플래그는 더 이상 "실시간이라 봐준다"가 아니라 **컷오프 모드 표기**다.
    그리고 랭킹은 그 반대편으로 **조용히 엄격해졌다** — 옛 코드는 플래그 하나로 봉과 스냅을
    같이 밀었고 그것이 A2 §2 위반이었다(W3 실측: `ranking_snaps_pre` 4.0 대 6.0).
    아래 `evaluate()` 는 랭킹 컷을 **넘기지 않는다**; `features.extract_precursor_features`
    안에서 `include_t0=False` 가 박혀 나간다. 자물쇠는 `tests/test_a2_collector_alignment.py`.

    **A2 는 자기유리 개정이다**(이벤트당 캔들 1봉 증가). 인용할 때 이 문장을 함께 인용하라 —
    `docs/04` C-7 개정 A2 의 "방향 고지" 문단이 그렇게 요구한다.
    """

    def __init__(self, params: EventParams, *, notifier=None, max_per_day: int = 1,
                 seen_limit: int = 4096):
        self.params = params
        self.notifier = notifier
        self.max_per_day = int(max_per_day)
        self.seen_limit = int(seen_limit)
        #: (symbol, UTC 매매일) → {t0_ms: 마지막으로 내보낸 라벨 해시}.
        #:
        #: 검출기는 매 사이클 버퍼를 다시 스캔하므로 억제가 없으면 같은 이벤트가
        #: 사이클마다 재기록된다(라이브 실측: 7분에 15행 / 고유 9개 = 40% 중복).
        #: 해시가 바뀔 때만 다시 내보낸다.
        #:
        #: 키가 (symbol, t0_ms) 가 아니라 **매매일 단위**인 이유 (감사 ⑨/I-1):
        #: DB 의 UNIQUE(symbol, t0_ms) 와 같은 키를 쓰면 방어가 2층이 아니라 같은 방어가
        #: 두 번 있는 것이다 — RVOL 게이트가 꺼졌다 켜져 t0 가 이동하면 두 층이 동시에
        #: 뚫려 같은 급등이 두 행이 된다. 매매일당 t0 수를 `max_per_day` 로 상한하면
        #: t0 가 어디로 움직여도 새 행이 생기지 않는다.
        #: 토스 매매일(KST 09:00~다음날 07:00)은 UTC 날짜와 1:1 이므로 (docs/07 §3.1)
        #: UTC 날짜를 매매일 키로 쓴다.
        self._emitted: dict[tuple[str, int], dict[int, str]] = {}
        self.counters: dict[str, int] = {"scored": 0, "events": 0, "errors": 0,
                                         "suppressed": 0, "updated": 0,
                                         "t0_shift_suppressed": 0}

    def evaluate(self, symbol: str, df_1m: pd.DataFrame, *,
                 rankings: pd.DataFrame | None = None,
                 calendar: list[UsMarketDay] | None = None,
                 curve: pd.Series | None = None,
                 baseline: dict | None = None,
                 shares_outstanding_qu: int | None = None,
                 prev_close_u: int | None = None,
                 now_ms: int | None = None,
                 detect_from_ms: int | None = None) -> DetectionResult | None:
        """마지막 완성봉 기준으로 스코어 + 신규 이벤트를 낸다.

        `detect_from_ms` 가 주어지면 **이벤트 판정만** 그 시각 이후 봉으로 제한한다
        (감사 F-3: 버퍼에 남은 전일 이벤트를 당일이 섞인 곡선으로 재판정하면
        `rvol_at_t0` 가 미래 거래량으로 오염되고 UPSERT 가 깨끗한 기록을 덮어쓴다 —
        전일 이벤트는 이미 기록됐으므로 다시 판정할 이유가 없다).
        피처/스코어는 버퍼 전체를 계속 쓴다 — 전조 피처에는 이력이 필요하다.
        """
        if df_1m is None or df_1m.empty:
            return None
        t0_ms = int(df_1m["ts_ms"].to_numpy()[-1])
        rk = rankings if rankings is not None else _empty_rankings()
        # `include_t0` 는 **캔들 전용 모드 표기**다 (C-7 개정 A2 §1). 랭킹은 이 값과 무관하게
        # 항상 `snap_ms < t0_ms` 엄격이다 (A2 §2) — 그 컷은 features 안에 박혀 있고 여기서
        # 넘기지 않는다. 실시간 버퍼는 직전 봉 종료 이후 받은 스냅을 계속 들고 있으므로
        # (실측: 검출 1회당 평균 7행, 버퍼의 5.5%) 이 컷이 비어 있는 방어가 아니다.
        feats = extract_precursor_features(
            df_1m, rk, t0_ms, include_t0=True, symbol=symbol, curve=curve,
            calendar=calendar, baseline=baseline,
            shares_outstanding_qu=shares_outstanding_qu,
            toss_type=RANKING_TOSS_TYPE, market_type=RANKING_MARKET_TYPE)
        self.counters["scored"] += 1
        score, path, prec, conf = score_paths(feats)

        rv = None
        if curve is not None and not curve.empty:
            rv = rvol_series(df_1m, curve, calendar=calendar)
        rk_events = rankings if (rankings is not None and not rankings.empty) else None
        events = self._new_events(symbol, df_1m, rankings=rk_events, calendar=calendar,
                                  rvol=rv, prev_close_u=prev_close_u,
                                  shares_outstanding_qu=shares_outstanding_qu,
                                  scores=(prec, conf, path),
                                  now_ms=now_ms if now_ms is not None else t0_ms,
                                  detect_from_ms=detect_from_ms)
        return DetectionResult(symbol=symbol, ts_ms=t0_ms, score=score, path=path,
                               precursor=prec, confirm=conf, feats=feats, events=events)

    def _new_events(self, symbol: str, df_1m: pd.DataFrame, *, rankings, calendar,
                    rvol, prev_close_u, shares_outstanding_qu,
                    scores: tuple[float, float, str], now_ms: int,
                    detect_from_ms: int | None = None) -> list[dict]:
        df_detect = df_1m
        if detect_from_ms is not None and not df_1m.empty:
            df_detect = df_1m[df_1m["ts_ms"] >= int(detect_from_ms)]
            if df_detect.empty:
                return []
        try:
            found = detect_events(
                df_detect, self.params, calendar=calendar, rvol_series=rvol,
                prev_close_u=prev_close_u, shares_outstanding_qu=shares_outstanding_qu,
                rankings=rankings, max_per_day=self.max_per_day)
        except Exception as exc:                       # 검출 실패가 수집을 죽이면 안 된다
            self.counters["errors"] += 1
            if self.notifier is not None:
                self.notifier.warn(f"detect_events({symbol}) failed: "
                                   f"{type(exc).__name__}: {exc}")
            return []
        if found is None or found.empty:
            return []
        out: list[EventEmission] = []
        prec, conf, path = scores
        for row in found.to_dict("records"):
            t0 = int(row["t0_ms"])
            day = self._emitted.setdefault((symbol, t0 // DAY_MS), {})
            digest = label_hash(row)
            previous = day.get(t0)
            if previous == digest:
                self.counters["suppressed"] += 1
                continue                       # 라벨이 그대로면 다시 쓸 이유가 없다
            if previous is None and len(day) >= self.max_per_day:
                # t0 이동 (감사 I-1): 같은 매매일에 이미 기록한 이벤트의 t0 가 게이트
                # 변화/prev_close 변화로 다른 봉으로 옮겨왔다. (symbol, t0) 만 보면
                # 새 이벤트로 보여 DB 에 두 번째 행이 생긴다 — 조용히 버리지 않고 남긴다.
                self.counters["t0_shift_suppressed"] += 1
                if self.notifier is not None:
                    self.notifier.warn(
                        f"event t0 shift suppressed {symbol}: day already has "
                        f"t0={sorted(day)} — new t0={t0} would duplicate the event")
                continue
            day[t0] = digest
            is_new = previous is None
            self.counters["events" if is_new else "updated"] += 1
            record = event_record(row, symbol, extra_meta={
                "realtime": True, "detected_ms": int(now_ms), "include_t0": True,
                "score_precursor": prec, "score_confirm": conf, "score_path": path,
                "detect_lag_min": (int(now_ms) - t0) // MIN_MS,
                "label_revision": 0 if is_new else 1,
            })
            out.append(EventEmission(record=record, t0_ms=t0, is_new=is_new,
                                     session=_opt_str(row.get("session"))))
        self._evict()
        return out

    def seed_suppression(self, symbol: str, t0_ms: int) -> None:
        """DB 에 이미 있는 이벤트를 억제 상태로 복원한다 (재시작 이어받기).

        재기동 직후 억제 집합이 비어 있으면 버퍼의 모든 이벤트가 "신규" 로 재기록된다
        (감사 F-3: state_snapshot 은 이 상태를 저장하지 않는다 — 진실은 events 테이블에
        있으므로 거기서 되살린다). 라벨 해시는 알 수 없어 빈 지문을 넣는다 — 같은 t0 의
        첫 재검출은 해시가 달라 **갱신**(is_new=False)으로 나가고, t0 가 이동한 재검출은
        매매일 상한에 걸려 새 행을 만들지 못한다.
        """
        t0 = int(t0_ms)
        self._emitted.setdefault((symbol, t0 // DAY_MS), {}).setdefault(t0, "")

    def _evict(self) -> None:
        """무인 실행 메모리 안정성 — 오래된 (symbol, 매매일) 부터 버린다 (삽입 순서 = 시간 순)."""
        excess = len(self._emitted) - self.seen_limit
        for key in list(self._emitted)[:max(0, excess)]:
            self._emitted.pop(key, None)

    def emitted_count(self) -> int:
        return len(self._emitted)

    def forget(self, symbol: str) -> int:
        """그 심볼의 **이벤트 기록 이력**을 버린다.

        ⚠️ 티어 강등에서는 부르지 마라. 강등→재승격은 흔한 churn 인데 여기서 이력을 지우면
        재승격 직후 같은 이벤트를 전부 다시 기록한다(이 버그가 실제로 라이브에서 났다).
        유니버스에서 영구 제외할 때만 쓴다.
        """
        drop = [k for k in self._emitted if k[0] == symbol]
        for key in drop:
            self._emitted.pop(key, None)
        return len(drop)


def build_curve(df_hist_1m: pd.DataFrame, calendar: list[UsMarketDay],
                exclude_dates: Iterable[str] = ()) -> pd.Series | None:
    """시간대 보정 RVOL 의 분모 곡선.

    **이벤트 당일을 반드시 제외한다** — 당일을 분모에 넣으면 RVOL 이 1 쪽으로 축소되는
    자기오염이 생긴다 (W3 인수인계 §4).
    """
    if df_hist_1m is None or df_hist_1m.empty or not calendar:
        return None
    try:
        curve = minute_of_session_volume_curve(
            df_hist_1m, calendar, exclude_dates=tuple(exclude_dates))
    except Exception:
        return None
    return None if curve is None or curve.empty else curve


def _empty_rankings() -> pd.DataFrame:
    return pd.DataFrame({"snap_ms": pd.Series(dtype="int64"),
                         "ranking_type": pd.Series(dtype="object"),
                         "rank": pd.Series(dtype="int64"),
                         "symbol": pd.Series(dtype="object"),
                         "amount_u": pd.Series(dtype="int64"),
                         "vol_qu": pd.Series(dtype="int64"),
                         "last_u": pd.Series(dtype="int64")})


__all__ = [
    "DEFAULT_THRESHOLDS", "DetectionResult", "EventDetector", "EventEmission",
    "EVENT_CORE_COLUMNS", "label_hash",
    "PriceActivityTracker", "PriceState", "TierChange", "TierStateMachine",
    "activity_score", "build_curve", "confirm_score", "event_record", "precursor_score",
    "score_paths",
]

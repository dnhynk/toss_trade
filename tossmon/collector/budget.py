"""예산 가드 — 계약 C-8. 소유: W4. 한도 70% 초과 예측 시 티어 자동 축소.

**초과는 버그가 아니라 사고로 취급한다.** 그래서 이 모듈은 두 방향에서 본다:

1. **계획 검증** (`validate_plan`) — 설정값만으로 계산한 초당 호출수가 예산을 넘는지
   기동 시점에 확인한다. `config/config.example.yaml` 하단의 산식과 1:1 이며,
   설정이 예산을 넘긴 채로 기동되면 경보 + 자동 축소한다 (코디네이터 지시).
2. **실사용 관측** (`on_request`) — 슬라이딩 윈도우로 실제 초당 호출수를 재고,
   계획과 실측 중 큰 쪽을 "예측"으로 삼는다. 재시도·백필처럼 계획에 없는 호출이
   예산을 먹는 경우를 이쪽이 잡는다.

`GroupRateLimiter` 와 역할이 다르다: limiter 는 **호출을 늦춰서** 한도를 지키고(대기),
BudgetGuard 는 **감시 대상을 줄여서** 지연 자체가 생기지 않게 한다. 늦추기만 하면 큐가 밀려
tier3 폴링 주기가 조용히 무너지므로 둘 다 필요하다.
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Mapping

from ..api.endpoints import DEFAULT_LIMIT, SPEC_LIMITS

GROUP_MARKET_DATA = "MARKET_DATA"
GROUP_CHART = "MARKET_DATA_CHART"
GROUP_RANKING = "RANKING"

#: 그룹 → 그 그룹이 먹여 살리는 티어 (축소 지시의 대상).
SHRINK_TIER: dict[str, int] = {GROUP_MARKET_DATA: 3, GROUP_CHART: 2}

#: `self.counters` 안에서 **그룹 이름이 아닌** 키들. 그룹별 요청 수와 한 dict 을 쓰기
#: 때문에 목록이 필요하다 — `snapshot()` 이 이것을 그룹으로 착각하면 유령 그룹이 렌더된다.
GLOBAL_COUNTERS: frozenset[str] = frozenset({
    "events_out_of_order", "server_seconds", "server_seconds_over_limit",
    "server_seconds_foreign", "over_limit_1s", "quota_not_ours",
})

#: 배치 상한 (/prices, /stocks). client.BATCH_MAX 와 같은 값.
BATCH_MAX = 200
#: 랭킹 스냅샷 종류 수의 **기본값**. 2026-08-04 사용자 결정으로 4종(MARKET/TOSS ×
#: AMOUNT/VOLUME) → 2종(거래량 2종)이 됐고, 2026-08-07 사용자 결정(D-11)으로
#: `TOP_GAINERS` 가 더해져 **3종**이다. 디스크가 목적이고 RANKING 그룹이라
#: MARKET_DATA 호가 예산과는 무관하다.
#:
#: 실제 계산은 `loops.RANKING_TYPES` 를 세어 `from_config(ranking_types=...)` 로
#: 넘어온다 — 수집기가 실제로 부르는 목록이 곧 예산의 근거여야 드리프트가 없다.
#: 이 상수는 그 인자를 주지 않았을 때의 대비값일 뿐이고, 둘의 일치는 테스트가 고정한다.
RANKING_TYPES = 3

#: 실사용 관측 **지평** (초). 이 구간의 초당 분포를 본다 — 이 값 자체는 판정 기준이 아니다.
WINDOW_S = 60.0

#: **서버가 판정하는 창 (초).** 이 프로젝트의 레이트리밋 모델 전체가 이 값 위에 서 있다.
#:
#: 서버는 벽시계 기준 **고정 1초 창**으로 센다 (docs/06 §9-2: MARKET_INFO 한도 3 에서
#: 같은 1초 안에 4번째 호출이 429). 그런데 예전 모델은 60초 **평균**으로 사용률을 재고
#: 정원을 깎았다. 둘은 전혀 다른 것을 측정한다:
#:
#:     60초에 420회 = 평균 7.0 req/s  -> "한도 10 의 70%, 여유 있음"
#:     그런데 그 420회가 1초에 15회씩 몰렸다면 -> 서버 기준으로는 **매번 위반**
#:
#: 2026-08-04 아침의 "한도의 1/5 인데 429" 가 정확히 이 착시였다. 평균은 버스트를
#: 숨긴다. 그래서 판정 근거를 평균에서 **초당 첨두**로 옮긴다.
SERVER_WINDOW_S = 1.0

#: 초당 첨두를 **슬라이딩**으로 잰다 (정렬된 고정 버킷이 아니라).
#:
#: 우리 시계와 서버 창의 **위상**을 모르기 때문이다. 정렬 버킷으로 세면 경계에 걸친
#: 버스트가 두 버킷으로 쪼개져 과소평가되는데, 서버 위상이 다르면 그 둘이 한 창에 들어간다.
#: 어떤 위상의 고정 창이든 그 안의 호출 수는 **슬라이딩 1초 최대 이하**이므로, 슬라이딩
#: 최대는 위상과 무관하게 안전한 상계다. 과대평가 쪽으로 틀리는 것이 옳은 방향이다.
PEAK_SLIDING = True
#: **지속 사용률 상한 배수** — 축소 판정이 쓰는 천장 (target × HEADROOM).
HEADROOM = 0.95
#: 계획이 **반드시 비워둬야 하는 여유** (target 대비). 계획에 없는 호출이 전부 여기서
#: 나간다: 재시도, 승격 직후 이력 백필, 세션 전환 후 베이스라인 갱신, tier2 호가
#: (의도적으로 계획 밖). 설정 검증은 이 여유를 **포함해서** 통과해야 한다.
#:
#: 예전에는 계획을 target(7.00)에, 축소를 target×HEADROOM(6.65)에 대고 재는 **기준
#: 불일치**가 있었다. 그래서 "검증은 통과했는데 축소 트리거 바로 아래 0.233 req/s"
#: 라는 상태가 정상으로 취급됐다 (2026-08-04 실측). 이제 두 판정이 같은 천장을 쓰고,
#: 계획은 그보다 PLAN_RESERVE_FRAC 만큼 더 아래여야 한다.
PLAN_RESERVE_FRAC = 0.10

#: 세션 전환 직후 이 시간 동안은 **측정치 기반 축소를 하지 않는다** (워밍업).
#: 개장 직후는 원래 측정이 튀는 구간인데, 그 순간의 측정으로 정원을 깎으면 자격이
#: 충분한 종목까지 쫓겨난다 (실측: 08-03 22:34 개장 32초 만에 점수 0.65 를 축출,
#: 앞에 429 라인 없음 — 순수 measured-overshoot). 429(진짜 사고)와 계획 초과(설정 오류)는
#: 워밍업과 무관하게 즉시 반응한다.
MEASURED_WARMUP_S = 180.0
#: 측정치 기반 축소는 초과가 이만큼 **연속으로 유지될 때만** 실행한다. 한 번 튀는 것으로
#: 정원을 깎지 않는다. 건수가 아니라 경과 시간 기준이다 (감사 H-4 와 같은 이유).
MEASURED_SUSTAIN_S = 60.0
#: 축소 후 목표 사용률 — 경계에 딱 붙이면 곧바로 다시 넘는다.
SHRINK_TO = 0.9
#: 같은 그룹에 축소를 다시 지시하기까지의 최소 간격 (초). 플래핑 방지.
SHRINK_COOLDOWN_S = 30.0
#: 429 를 맞으면 이 비율만큼 추가로 줄인다 (사고 대응).
#: 0.2 는 과했다 — 429 한 건에 정원 120 이 24 씩 깎여 8회 만에 300->76 이 됐다
#: (2026-08-04 실측). 회복 경로가 생겼으니 한 번에 크게 자를 이유가 없다.
RATE_LIMITED_SHRINK_FRAC = 0.10

#: 마지막 429 이후 이만큼 조용하면 정원을 한 단계 **되돌린다**.
#: 이것이 없으면 축소는 일방통행 래칫이 되어, 429 한 건의 대가를 세션 내내 치른다.
RECOVER_AFTER_S = 300.0
#: 회복 1스텝에 되돌리는 폭 (그 티어 상한 대비).
RECOVER_STEP_FRAC = 0.25
#: **축소 1스텝의 상한** (현재 정원 대비). 회복 폭과 대칭이다.
#:
#: 2026-08-04 개장 사고: 단 한 번의 지시가 tier2 를 300 -> 1 로 만들었다(drop=299).
#: 내려갈 땐 한 번에 299, 올라올 땐 한 스텝에 1 — 이 비대칭에 근거가 없었다.
#: 축소 근거가 옳더라도 **한 번에 정원을 통째로 날릴 이유는 없다**: 한 스텝 깎고 다시
#: 재면 되고, 과부하가 진짜면 다음 스텝에서 또 깎인다. 틀렸을 때의 대가만 줄어든다.
SHRINK_MAX_STEP_FRAC = 0.25
#: 회복은 실사용이 목표의 이 비율 아래일 때만 — 빡빡한데 되돌리면 429 를 다시 부른다.
RECOVER_USAGE_MAX = 0.70

#: **"우리 것이 아닌 소비" 로 판정하기까지 필요한 서버 초의 수** (관측 지평 안에서).
#:
#: 한 초짜리 증거로는 판정하지 않는다. 오탐 경로가 실측으로 둘 알려져 있기 때문이다:
#:
#: 1. **경계 렌더** (docs/06 §9-5 부수 관측) — 서버 초 경계에서 렌더된 응답은 `date` 의
#:    초와 레이트리밋 카운터가 한 초 어긋날 수 있다 (`date=02:58:00` 인데 `remaining=0`
#:    이 앞 초의 3번째 호출로 계산된 값이었다). 그러면 앞 초의 소진량이 이 초의 기록에
#:    얹혀 없는 외부 소비처럼 보인다.
#: 2. **응답 없이 끝난 송신** — 타임아웃·연결 끊김으로 응답을 못 받은 요청은 서버는
#:    처리했는데 우리 `own` 에는 안 들어간다 (`date` 헤더가 없으니 초를 모른다).
#:
#: 둘 다 **산발적**이다. 반면 같은 자격증명을 쓰는 다른 발신자는 자기가 도는 동안 거의
#: 매 초를 깎는다 — 지속성이 이 둘을 가른다. 오탐 하나의 대가가 "정원 축소"라서 이 방향의
#: 보수성이 옳다 (2026-08-04 개장 붕괴: 없는 첨두를 보고 리미터를 조여 수집량이 줄었다).
FOREIGN_SECONDS_MIN = 3


@dataclass(frozen=True)
class TierPlan:
    """설정값 → 그룹별 계획 호출률 (req/s).

    산식은 `config/config.example.yaml` 하단 주석과 동일하다. 숫자를 두 곳에 두지 않으려고
    주석이 아니라 여기서 계산한다 — 설정을 바꾸면 이 계산이 따라 움직이고,
    예산을 넘기면 `validate_plan()` 이 기동 시점에 잡는다.
    """
    tier1_symbols: int
    tier2_symbols: int
    tier3_symbols: int
    tier1_sweep_s: float
    tier2_candle_s: float
    tier3_trades_s: float
    tier3_orderbook_s: float
    ranking_snap_s: float
    ranking_types: int = RANKING_TYPES
    batch_max: int = BATCH_MAX

    @classmethod
    def from_config(cls, cfg, *, tier1_symbols: int, tier2_symbols: int,
                    tier3_symbols: int, ranking_types: int | None = None) -> "TierPlan":
        polling = cfg.require_polling()
        return cls(
            ranking_types=RANKING_TYPES if ranking_types is None else int(ranking_types),
            tier1_symbols=int(tier1_symbols),
            tier2_symbols=int(tier2_symbols),
            tier3_symbols=int(tier3_symbols),
            tier1_sweep_s=float(polling.tier1_sweep_s),
            tier2_candle_s=float(polling.tier2_candle_s),
            tier3_trades_s=float(polling.tier3_trades_s),
            tier3_orderbook_s=float(polling.tier3_orderbook_s),
            ranking_snap_s=float(polling.ranking_snap_s),
        )

    def rates(self) -> dict[str, float]:
        batches = math.ceil(self.tier1_symbols / self.batch_max) if self.tier1_symbols else 0
        return {
            GROUP_MARKET_DATA: (batches / self.tier1_sweep_s
                                + self.tier3_symbols / self.tier3_trades_s
                                + self.tier3_symbols / self.tier3_orderbook_s),
            GROUP_CHART: self.tier2_symbols / self.tier2_candle_s,
            GROUP_RANKING: self.ranking_types / self.ranking_snap_s,
        }

    def per_symbol_cost(self, group: str) -> float:
        """그 그룹에서 심볼 1개를 줄일 때 절약되는 req/s."""
        if group == GROUP_MARKET_DATA:
            return 1.0 / self.tier3_trades_s + 1.0 / self.tier3_orderbook_s
        if group == GROUP_CHART:
            return 1.0 / self.tier2_candle_s
        return 0.0

    def symbols_of(self, group: str) -> int:
        if group == GROUP_MARKET_DATA:
            return self.tier3_symbols
        if group == GROUP_CHART:
            return self.tier2_symbols
        return 0


class BudgetGuard:
    def __init__(self, limits: dict[str, float], usage_ratio: float = 0.7, *,
                 window_s: float = WINDOW_S, headroom: float = HEADROOM,
                 clock=None, notifier=None, mono=None):
        self.limits = dict(limits)
        self.usage_ratio = float(usage_ratio)
        self.window_s = float(window_s)
        self.headroom = float(headroom)
        self.clock = clock
        #: **사건 타임라인 전용 시계** (단조). `_wall_s` 와 갈라 두는 이유는 아래 참조.
        self.mono = time.monotonic if mono is None else mono
        self.notifier = notifier
        self.plan: TierPlan | None = None
        self.counters: dict[str, int] = {}
        self.rate_limited: dict[str, int] = {}
        self._events: dict[str, deque[float]] = {}
        #: 리미터의 자기 관측 표본 (시각, window_used). 예산 첨두와 **독립**이다.
        self._limiter_window: dict[str, deque[tuple[float, float]]] = {}
        #: **서버가 이름 붙인 초**의 감사 (시각, 우리 몫, 남의 몫). 우리 시계·계상과 독립이다.
        self._server_seconds: dict[str, deque[tuple[float, int, int]]] = {}
        self._last_shrink_s: dict[str, float] = {}
        self._last_429_s: dict[str, float] = {}
        self._last_grow_s: dict[str, float] = {}
        self._measured_over_since: dict[str, float] = {}
        self._warmup_until_s: float = 0.0
        self._over_limit_active: dict[str, bool] = {}
        self._foreign_active: dict[str, bool] = {}
        self._forced: dict[str, float] = {}      # 429 로 강제 축소해야 할 비율

    # ---- 시간 ----------------------------------------------------------
    #
    # **이 가드는 시계를 둘 쓴다. 하나로 합치면 안 된다.**
    #
    # `_wall_s()` — 서버 보정 벽시계 (`clock.now_ms()`). **세션과 맞물린 판정**이 본다:
    #     워밍업(개장 후 180초), 축소 쿨다운, 429 후 회복 대기, 지속 초과 유지 시간.
    #     "개장 직후인가" 는 서버 시각으로 정의되므로 이쪽은 서버를 따라가는 것이 맞다.
    #
    # `_mono_s()` — 단조 시계 (기본 `time.monotonic`). **사건 타임라인**이 본다:
    #     `_events`·`_limiter_window` 의 스탬프와 그 컷오프(`peak_1s`·`p95_1s`·
    #     `measured_rate`·`limiter_peak`).
    #
    # 왜 갈랐나 (2026-08-12, docs/52 §5). 예산의 사건 시각이 서버 보정 벽시계 위에
    # 얹혀 있었다. 그 오프셋은 **초 해상도** `Date` 헤더 9표본의 중앙값이고 `after_call`
    # 마다(초당 ~6회) 갱신되므로 ±180ms 로 출렁인다 (`scheduler.py:170-184`).
    # 송신 간격이 118~157ms 라, 오프셋이 150ms 내려가는 순간 그 사이의 송신들이 예산
    # 시계에서 **한 점으로 눌린다.** 눌린 만큼이 그대로 `peak_1s` 가 됐다:
    # 같은 송신열에 프로덕션 계상 코드를 흘린 오프라인 재계상(`tools/replay_send_time.py`,
    # 30분·174표본)에서 이상적 시계는 첨두 중앙 10 / >10 표본 0-174 인데
    # 지터 시계는 첨두 중앙 12 / >10 표본 174-174 다.
    # 08-12 운영 로그 104표본 중 44건(42.3%)이 한도 10 초과였고 11~13 에 몰려 있었다 —
    # 진짜 송신 첨두는 리미터 하드캡이 묶는 10 이다.
    #
    # 기존 시계 경보로는 못 잡는다: 임계가 5초(`CLOCK_SKEW_ALERT_S`)이고 지터는 0.2초다.
    #
    # ⚠️ **`_mono_s()` 는 절대 시각이 아니다.** 원점은 프로세스마다 다르고 재시작마다
    # 리셋된다. 그래서 이 값은 **차이로만** 쓰고 로그·경보·DB·상태파일 어디에도
    # 새어나가면 안 된다 (`snapshot()`/`describe()` 는 전부 개수·비율이다).
    # `test_budget_event_clock.py::test_monotonic_origin_never_leaks_*` 가 고정한다
    # (같은 사건열을 원점만 바꿔 흘려 출력이 같은지 본다).
    #
    # 이 시계는 `client.recent_send_ages()` 의 시계와 **같아야** 한다 — 둘 다
    # `time.monotonic` 이라 `on_sends` 의 `now - age` 가 송신 시각을 정확히 복원한다.

    def _wall_s(self) -> float:
        """세션·워밍업·쿨다운이 보는 시각 (서버 보정 벽시계)."""
        if self.clock is not None:
            return self.clock.now_ms() / 1000.0
        return time.monotonic()

    def _mono_s(self) -> float:
        """사건 타임라인이 보는 시각 (단조). **절대 시각이 아니다.**"""
        return float(self.mono())

    # ---- 한도 ----------------------------------------------------------

    def limit_of(self, group: str) -> float:
        if group in self.limits:
            return float(self.limits[group])
        return float(SPEC_LIMITS.get(group, DEFAULT_LIMIT))

    def has_window_hardcap(self) -> bool:
        """리미터에 슬라이딩 1초 하드캡이 있는가 (W1 d6b47f1).

        이 한 가지가 usage_ratio 상한의 의미를 통째로 바꾼다 — 아래 두 메서드 참조.
        """
        try:
            from ..api import limiter as _lim
        except Exception:
            return False
        return hasattr(_lim, "WINDOW_HORIZON_S")

    def max_safe_usage_ratio(self) -> float:
        """리미터가 **계획한 속도를 실제로 낼 수 있는** usage_ratio 상한.

        하드캡 **전후로 이 값의 의미가 다르다.**

        (1) 하드캡이 없을 때 — 상한은 **429 안전선**이었다. 토큰버킷은 유휴 직후 1초에
            `capacity + rate = rate × (1+BURST_FRACTION)` 을 통과시키므로

                limit × usage_ratio × 1.3 <= limit   →   usage_ratio <= 1/1.3 = 0.769

            이 위로 올리면 정원을 아무리 깎아도 유휴 직후 한 번의 버스트로 한도를 넘었다.

        (2) 하드캡이 있을 때 (지금) — **한도 초과는 구조적으로 불가능해졌다.** 슬라이딩 캡이
            `WINDOW_HORIZON_S`(1.15s) 안에서 `window_cap`(= 공시 한도) 개를 넘기지 않고,
            모든 1.0초 구간은 어떤 1.15초 구간에 포함되므로 서버 창 위상과 무관하게 지켜진다.
            그래서 상한의 의미가 "429 안전선" 에서 **"리미터가 낼 수 있는 지속 속도"** 로 바뀐다:

                지속 상한 = window_cap / WINDOW_HORIZON_S = limit / 1.15
                          →  usage_ratio <= 1/1.15 = 0.870

            이 위로 올리면 429 가 나는 게 아니라 **하드캡이 병목이 되어 호출이 큐에 밀린다.**
            그러면 폴링 주기가 조용히 늘어나 tier3 4초가 4초가 아니게 된다 — 이 모듈이
            애초에 막으려던 실패다(늦추기만 하면 큐가 밀린다, 모듈 docstring 참조).

        즉 지금 0.85 가 안전한 이유는 "버스트가 작아서" 가 아니라 **하드캡이 창을 지키기
        때문**이다. 하드캡이 사라지면 이 값은 자동으로 0.769 로 되돌아간다.
        """
        try:
            from ..api import limiter as _lim
        except Exception:                      # 리미터 정책을 못 읽으면 보수적으로
            return 1.0
        if self.has_window_hardcap():
            horizon = float(getattr(_lim, "WINDOW_HORIZON_S"))
            return 1.0 / horizon if horizon > 0 else 1.0
        return 1.0 / (1.0 + float(getattr(_lim, "BURST_FRACTION", 0.3)))

    def worst_case_1s(self, group: str) -> float:
        """이 설정에서 **한 초에 나갈 수 있는 최대 호출 수** (리미터 정책 기준).

        하드캡이 있으면 캡 자체가 상계다. 없으면 버킷의 `capacity + rate` 다.
        이 값이 공시 한도를 넘으면 429 는 시간 문제다.
        """
        try:
            from ..api import limiter as _lim
        except Exception:
            return float("inf")
        limit = self.limit_of(group)
        if self.has_window_hardcap():
            return float(int(limit))           # window_cap = 공시 한도 (정수 절삭)
        rate = limit * self.usage_ratio
        capacity = max(1.0, rate * float(getattr(_lim, "BURST_FRACTION", 0.3)))
        return rate + capacity

    def check_usage_ratio(self) -> float | None:
        """usage_ratio 가 리미터가 지킬 수 있는 범위를 넘으면 초과분을 돌려준다(경보).

        이 검사가 없으면 "예산은 통과했는데 리미터는 못 내는" 설정이 조용히 배포된다.
        """
        ceiling = self.max_safe_usage_ratio()
        if self.usage_ratio <= ceiling:
            return None
        over = self.usage_ratio - ceiling
        if self.notifier is not None:
            # 하드캡 유무에 따라 **실패 방식이 다르다.** 문구가 틀리면 엉뚱한 곳을 고친다.
            if self.has_window_hardcap():
                why = ("하드캡이 병목이 되어 호출이 큐에 밀린다 — 429 는 안 나지만 "
                       "폴링 주기가 조용히 늘어난다(tier3 4초가 4초가 아니게 된다)")
            else:
                why = ("유휴 직후 1초에 공시 한도를 넘긴다 — 정원을 깎아도 못 고친다. "
                       "리미터에 1초 하드캡을 먼저 넣어야 이 값을 올릴 수 있다")
            self.notifier.alert(
                f"budget: usage_ratio {self.usage_ratio:.3f} > 리미터가 낼 수 있는 "
                f"상한 {ceiling:.3f} — {why}")
        return over

    def target(self, group: str) -> float:
        """이 그룹에 허용된 초당 호출수 (= 공시 한도 × usage_ratio)."""
        return self.limit_of(group) * self.usage_ratio

    # ---- 관측 ----------------------------------------------------------

    def on_request(self, group: str) -> None:
        now = self._mono_s()
        self.counters[group] = self.counters.get(group, 0) + 1
        q = self._events.setdefault(group, deque())
        q.append(now)
        cutoff = now - self.window_s
        while q and q[0] < cutoff:
            q.popleft()

    def on_requests(self, group: str, n: int) -> None:
        """`n` 건을 **직전 계상 시각과 지금 사이에 고르게 펴서** 계상한다.

        ⚠️ **폴백 경로다.** 송신 시각을 알 수 있으면 `on_sends()` 를 쓴다 — 아래 균등
        분포의 전제("직전 계상 이후 구간에 일어났다")는 참이지만 그 구간이 **계상 간격**
        이지 송신 간격이 아니어서, 완료가 몰리면 송신이 압축되어 첨두가 부푼다 (docs/52).
        송신 시각을 못 주는 client(구버전·테스트 더블)에서만 이쪽으로 내려온다.

        재시도는 지수 백오프로 **떨어져서** 나가는데, 한 시각에 몰아 계상하면 초당 첨두가
        가짜로 치솟는다. 2026-08-04 13:10:06 실측: 첨두 13회로 "리미터가 1초 창을 못
        지킨다" 경보가 났는데 같은 구간 `http_429=0` 이었다 — 서버는 13회를 본 적이 없다.
        하드캡(W1)이 창을 지키고 있으므로 그 경보는 **내 계측이 만든 허위**였다.

        시도들의 정확한 시각은 client 가 알려주지 않지만, **직전 계상 이후 구간 안에서
        일어났다는 것은 확실하다.** 그 구간에 균등 분포시키면 총량은 그대로 보존하면서
        첨두는 실제 밀도에 가까워진다. 가짜 경보는 진짜 경보를 묻는다.
        """
        if n <= 1:
            if n == 1:
                self.on_request(group)
            return
        now = self._mono_s()
        q = self._events.setdefault(group, deque())
        start = q[-1] if q else now - SERVER_WINDOW_S
        span = max(now - start, 1e-3)
        for i in range(n):
            q.append(start + span * (i + 1) / n)
        self.counters[group] = self.counters.get(group, 0) + n
        cutoff = now - self.window_s
        while q and q[0] < cutoff:
            q.popleft()

    def on_sends(self, group: str, ages_s) -> None:
        """`n` 건을 **각자의 송신 시각**으로 계상한다 (완료 시각이 아니라).

        `ages_s`: 각 송신이 **지금으로부터 몇 초 전**이었나, 오래된 것부터.
        시각이 아니라 나이로 받는 이유는 시계의 **원점**이 다를 수 있기 때문이다 —
        나이는 어느 기준에서든 같은 뜻이다. (2026-08-12 이후 양쪽 다 `time.monotonic`
        이므로 `now - age` 는 송신 시각을 그대로 복원한다. 그 전에는 이쪽이 서버 보정
        벽시계였고, 그 오프셋 지터가 배치 **사이**의 간격을 눌렀다 — docs/52 §5.3.)

        이것이 `on_requests` 의 균등 분포를 대체한다. 균등 분포는 "직전 계상 이후 구간에
        일어났다" 는 것만 알 때의 최선이었지만, 그 구간 자체가 **계상 간격**이지 송신
        간격이 아니었다: 완료가 몰리면(이벤트루프가 막혔다 풀리는 모양) 넓게 퍼져 나간
        송신들이 좁은 구간에 압축되어 찍힌다. 실측(테스트): 리미터가 진짜 첨두 9 로
        내보낸 20건이 완료 시각 계상에서 **첨두 20** 으로 잡혔다.

        큐는 **오름차순**을 유지해야 한다 (`peak_1s` 의 두 포인터와 왼쪽 pruning 이 그
        전제 위에 있다). 송신은 그룹 락 때문에 순서대로 나가고 이제 시계도 단조라
        어긋날 이유가 줄었지만, 정렬 복구는 남겨 둔다 — `ages_s` 는 남이 주는 값이고,
        어긋나면 첨두가 **조용히** 틀리기 때문이다. 어긋난 횟수는 카운터로 드러난다.
        """
        ages = [float(a) for a in ages_s]
        if not ages:
            return
        now = self._mono_s()
        q = self._events.setdefault(group, deque())
        was = q[-1] if q else None
        stamped = sorted(now - max(a, 0.0) for a in ages)
        q.extend(stamped)
        if was is not None and stamped[0] < was:
            # 순서가 어긋났다 — 정렬을 복구하지 않으면 첨두 계산이 조용히 틀린다.
            self.counters["events_out_of_order"] = (
                self.counters.get("events_out_of_order", 0) + 1)
            self._events[group] = q = deque(sorted(q))
        self.counters[group] = self.counters.get(group, 0) + len(ages)
        cutoff = now - self.window_s
        while q and q[0] < cutoff:
            q.popleft()

    # ---- 리미터의 자기 관측 (독립 대조) ---------------------------------

    def on_limiter_window(self, group: str, used: float) -> None:
        """리미터가 **자기 창**(`snapshot()["window_used"]`)에서 세고 있는 송신 수 1표본.

        예산 첨두와 **독립인 관측**이다: 예산은 client 가 소켓 직전에 찍은 시각으로 세고,
        이쪽은 리미터가 `_Bucket.sent` 에 직접 쌓은 것을 센다. 둘이 어긋나면 계상이
        틀렸다는 뜻이고, 어긋나지 않으면 "첨두가 한도 아래" 가 계측 착시가 아니라는 뜻이다.

        ⚠️ 창 길이가 다르다: 리미터는 `WINDOW_HORIZON_S`(1.15초), 예산은 1.0초다.
        그래서 정상 상태의 기대 관계는 **`peak_1s <= limiter_peak`** 이지 같음이 아니다.
        """
        now = self._mono_s()
        q = self._limiter_window.setdefault(group, deque())
        q.append((now, float(used)))
        cutoff = now - self.window_s
        while q and q[0][0] < cutoff:
            q.popleft()

    def limiter_peak(self, group: str) -> float:
        """관측 지평 안에서 리미터 창 점유의 최댓값. 표본이 없으면 -1 (모름)."""
        q = self._limiter_window.get(group)
        if not q:
            return -1.0
        cutoff = self._mono_s() - self.window_s
        while q and q[0][0] < cutoff:
            q.popleft()
        return max((v for _, v in q), default=-1.0)

    # ---- 서버 초 감사 (우리 시계·계상과 독립인 관측) ----------------------
    #
    # **왜 이것이 따로 필요한가.** 예산의 `peak_1s` 는 우리 송신을 우리 시계로 센 값이고,
    # 리미터가 1.15초 하드캡을 지키는 한 그것이 한도를 넘는 일은 **구조적으로 없다**
    # (docs/52 §6 증명 + `test_peak_can_never_exceed_the_limit_once_accounting_is_send_time`).
    # 그래서 `peak_1s > limit` 에 매달린 감시는 전부 조용해질 수밖에 없었다.
    # **"조용하다" 는 "안전하다" 가 아니다** — 그 감시들이 원래 겨누던 것(다중 프로세스,
    # 리미터 밖 송신)은 애초에 우리 송신 카운터로 볼 수 있는 것이 아니었다.
    #
    # 여기서 보는 두 양은 창의 이름도 총량도 **서버가** 준다 (`client._ServerSecond`):
    #
    #   own      — 그 서버 초에 우리가 소켓으로 내보낸 수
    #   foreign  — (limit - remaining) - own, 즉 **우리가 보내지 않은 소비**
    #
    # `own > limit` 은 우리 리미터가 서버 창 하나를 실제로 넘겼다는 뜻이고(하드캡을 안 지난
    # 송신 경로가 있다는 뜻이다), `foreign > 0` 은 같은 자격증명으로 도는 다른 발신자나
    # 그룹 밖 한도의 존재를 뜻한다 — docs/06 §9-6 이 "판별되지 않았다" 고 남긴 그 둘이다.

    def on_server_second(self, group: str, own: int, consumed: int,
                         limit_header: int | None = None) -> None:
        """정산이 끝난 서버 초 하나를 받는다 (`client.drain_server_seconds()` 의 한 건).

        `consumed` 가 음수면 그 초의 소진량을 모른다는 뜻이고, 그때 `foreign` 은 0 이다 —
        **모름을 사고로 세지 않는다.** 반대로 `own` 은 헤더와 무관하게 항상 참값이므로
        `own > limit` 판정은 그 경우에도 유효하다.
        """
        limit = float(limit_header) if limit_header else self.limit_of(group)
        own = int(own)
        foreign = 0 if consumed is None or consumed < 0 else max(0, int(consumed) - own)
        over = 1 if limit > 0 and own > limit else 0
        now = self._mono_s()
        q = self._server_seconds.setdefault(group, deque())
        q.append((now, over, foreign))
        cutoff = now - self.window_s
        while q and q[0][0] < cutoff:
            q.popleft()
        self.counters["server_seconds"] = self.counters.get("server_seconds", 0) + 1
        if over:
            self.counters["server_seconds_over_limit"] = (
                self.counters.get("server_seconds_over_limit", 0) + 1)
        if foreign:
            self.counters["server_seconds_foreign"] = (
                self.counters.get("server_seconds_foreign", 0) + 1)

    def _server_window(self, group: str) -> list[tuple[float, int, int]]:
        q = self._server_seconds.get(group)
        if not q:
            return []
        cutoff = self._mono_s() - self.window_s
        while q and q[0][0] < cutoff:
            q.popleft()
        return list(q)

    def server_seconds_seen(self, group: str) -> int:
        """관측 지평 안에서 정산된 서버 초의 수 — 아래 두 값의 **분모**다.

        분모를 같이 내는 이유: 에포크 없는 0 은 아무 뜻도 없다. 0/0 (아직 아무 초도 안
        닫혔다)과 0/58 (58초를 봤는데 깨끗하다)은 전혀 다른 진술이다 (docs/52 §7.2).
        """
        return len(self._server_window(group))

    def server_over_limit_seconds(self, group: str) -> int:
        """서버가 이름 붙인 초 중 **우리 송신만으로** 한도를 넘긴 초의 수."""
        return sum(o for _, o, _ in self._server_window(group))

    def foreign_seconds(self, group: str) -> int:
        """우리 것이 아닌 소비가 보인 서버 초의 수."""
        return sum(1 for _, _, f in self._server_window(group) if f > 0)

    def foreign_sends(self, group: str) -> int:
        """그 중 최대 몇 건이 우리 것이 아니었나 (한 초 기준)."""
        return max((f for _, _, f in self._server_window(group)), default=0)

    def quota_not_ours(self, group: str) -> bool:
        """**이 그룹의 초당 한도를 우리 혼자 쓰고 있지 않다**는 판정.

        산발적 오탐(경계 렌더·응답 없이 끝난 송신)과 가르려고 `FOREIGN_SECONDS_MIN` 초
        이상에서 보일 때만 참이다 — 그 상수 주석에 근거가 있다.
        """
        return self.foreign_seconds(group) >= FOREIGN_SECONDS_MIN

    def server_window_violated(self, group: str) -> bool:
        """정원을 되돌리거나 마지막 여유를 쓰면 **안 되는** 상태인가.

        둘 중 하나면 참이다: 우리가 서버 창을 실제로 넘겼거나(리미터 밖 송신),
        그 창을 우리 혼자 쓰고 있지 않거나(다른 발신자). 어느 쪽이든 "지금 우리 몫이
        한도만큼 있다" 는 전제가 깨진 것이라 되돌릴 때가 아니다.
        """
        return self.server_over_limit_seconds(group) > 0 or self.quota_not_ours(group)

    def on_429(self, group: str) -> None:
        """429 는 사고다 — 다음 `should_shrink()` 에서 강제로 줄인다."""
        self._last_429_s[group] = self._wall_s()      # 회복 대기(5분) — 분 단위 판정
        self.rate_limited[group] = self.rate_limited.get(group, 0) + 1
        self._forced[group] = max(self._forced.get(group, 0.0), RATE_LIMITED_SHRINK_FRAC)
        if self.notifier is not None:
            self.notifier.warn(f"budget: 429 on {group} "
                               f"(count={self.rate_limited[group]}) — forcing tier shrink")

    def measured_rate(self, group: str) -> float:
        """윈도우 평균 **지속** 사용률 (req/s).

        분모는 관측 구간이 아니라 **윈도우 전체**다. 이유: 백필 4연발처럼 수 ms 안에 몰린
        버스트를 관측 구간으로 나누면 수백 req/s 가 나와 티어가 통째로 날아간다.
        순간 버스트를 흡수하는 것은 `GroupRateLimiter`(대기)의 일이고, 여기서 봐야 하는 것은
        "이 페이스를 계속 유지하면 한도를 넘는가" 다. 창이 덜 찬 기동 직후에는 과소평가되는데,
        그쪽이 안전한 방향이다 (기동 버스트로 티어를 줄이지 않는다).
        """
        q = self._events.get(group)
        if not q:
            return 0.0
        cutoff = self._mono_s() - self.window_s
        while q and q[0] < cutoff:
            q.popleft()
        return len(q) / self.window_s

    # ---- 초당 분포 (판정의 근거) ---------------------------------------

    def _live_events(self, group: str) -> list[float]:
        q = self._events.get(group)
        if not q:
            return []
        cutoff = self._mono_s() - self.window_s
        while q and q[0] < cutoff:
            q.popleft()
        return list(q)

    def peak_1s(self, group: str) -> int:
        """관측 지평 안에서 **어느 1초 구간의 최대 호출 수** (슬라이딩).

        서버의 고정 1초 창이 어떤 위상이든 그 창의 호출 수는 이 값 이하다 — 즉 이것은
        위상과 무관한 안전한 상계다. **축소 판정이 보는 값이 이것이다.**
        """
        ev = self._live_events(group)
        if not ev:
            return 0
        peak = 0
        left = 0
        for right in range(len(ev)):
            while ev[right] - ev[left] >= SERVER_WINDOW_S:
                left += 1
            peak = max(peak, right - left + 1)
        return peak

    def per_second_counts(self, group: str) -> list[int]:
        """정렬된 1초 버킷별 호출 수 (분포 통계용).

        첨두 판정에는 `peak_1s` 를 쓴다 — 이쪽은 경계에 걸친 버스트를 쪼개므로
        분위수를 볼 때만 쓴다. 호출이 없던 초도 0 으로 채워 넣는다: 빈 초를 빼면
        "쉬는 시간"이 분모에서 사라져 p95 가 실제보다 높게 나온다.
        """
        ev = self._live_events(group)
        if not ev:
            return []
        buckets: dict[int, int] = {}
        for t in ev:
            key = int(t // SERVER_WINDOW_S)
            buckets[key] = buckets.get(key, 0) + 1
        lo, hi = min(buckets), max(buckets)
        return [buckets.get(k, 0) for k in range(lo, hi + 1)]

    def p95_1s(self, group: str) -> float:
        """초당 호출 수의 95 분위. 평균이 숨기는 쏠림을 드러낸다."""
        counts = sorted(self.per_second_counts(group))
        if not counts:
            return 0.0
        idx = min(len(counts) - 1, int(math.ceil(0.95 * len(counts)) - 1))
        return float(counts[max(0, idx)])

    def _note_over_limit(self, group: str) -> None:
        """한도 사고 **에피소드**를 1회만 계상·경보한다. 근거는 **서버 초 감사**다.

        2026-08-12 재조준 (docs/52 §12). 예전 조건은 `peak_1s > limit` 이었고, 그것은
        송신 시각 계상 + 단조 시계로 옮긴 뒤 **발화할 수 없는 조건**이 됐다 — 리미터
        하드캡이 어떤 1초에도 한도 초과를 못 내기 때문이다 (docs/52 §6 증명).
        운영 로그에서 실제로 그렇게 됐다: 옛 배선의 마지막 프로세스는 `over_limit_1s=435`
        였고, 08-12 19:11 재기동 이후 `md_peak_1s=10` 에 `over_limit_1s=0` 이다.
        **그 0 은 안전해졌다는 뜻이 아니라 이 자리가 눈을 감았다는 뜻이었다.**

        지금 세는 것은 우리 시계가 아니라 **서버가 이름 붙인 초**이고, 사고는 둘이다:

        * `own > limit` — 리미터를 안 지난 송신 경로가 있다 (하드캡이 못 막은 것).
        * `foreign > 0` — 그 초의 소진량이 우리 송신보다 많다. 같은 자격증명의 다른
          발신자이거나 그룹 밖의 한도다 (docs/06 §9-6 의 미판별 두 후보).

        둘은 **원인도 조치도 다르므로 에피소드를 따로 센다.** 관측 지평(60s) 동안 값이
        남는 것은 예전과 같으므로, 조건이 내려갔다 다시 올라올 때만 새 에피소드로 친다.
        """
        limit = self.limit_of(group)
        over = self.server_over_limit_seconds(group) > 0
        was_over = self._over_limit_active.get(group, False)
        self._over_limit_active[group] = over
        if over and not was_over:
            self.counters["over_limit_1s"] = self.counters.get("over_limit_1s", 0) + 1
            if self.notifier is not None:
                # 2026-08-08 정정 (docs/45 → docs/46). 예전 문구는 "리미터가 1초 창을 못
                # 지키고 있다" 였고 그것이 운영에서 580건 났다. **전부 허위였다** — 그 580건은
                # 같은 호출을 두 번 센 결과였다. 지금 이 경보가 오르면 근거가 다르다:
                # 우리 계상이 아니라 **서버가 라벨한 초**에서 우리 송신을 센 값이다.
                self.notifier.alert(
                    f"budget: {group} 서버가 라벨한 1초에 우리 송신이 공시 한도 "
                    f"{limit:.0f} 를 넘었다 (최근 60초 중 "
                    f"{self.server_over_limit_seconds(group)}초). 리미터 하드캡은 이 값을 "
                    "낼 수 없다 (docs/45 §2) — 리미터를 안 거치는 송신 경로를 의심하라. "
                    "티어를 깎아도 고쳐지지 않는다")

        foreign = self.quota_not_ours(group)
        was_foreign = self._foreign_active.get(group, False)
        self._foreign_active[group] = foreign
        if foreign and not was_foreign:
            self.counters["quota_not_ours"] = self.counters.get("quota_not_ours", 0) + 1
            if self.notifier is not None:
                self.notifier.alert(
                    f"budget: {group} 초당 한도를 우리 혼자 쓰고 있지 않다 — 최근 60초 중 "
                    f"{self.foreign_seconds(group)}초에서 서버가 센 소진량이 우리 송신보다 "
                    f"많았다 (최대 {self.foreign_sends(group)}건/초, 관측 "
                    f"{self.server_seconds_seen(group)}초). 원인 후보 셋이고 "
                    f"**그룹별 비대칭으로 가른다** — {self._foreign_by_group()}: "
                    "(1) 같은 자격증명의 다른 발신자(수집기 다중 기동·백필·프로브) — "
                    "비대칭 없음. (2) 우리 그룹 구획과 다른 서버 바구니 — 송신이 적은 "
                    "그룹에서 가장 크다. (3) 창 어긋남(경계 렌더·정산 뒤 늦은 도착) — "
                    "크기가 그 그룹 자기 송신 변동폭에 갇힌다 (docs/55 §4 에서 남의 소비 "
                    "0 으로 재현했다). (1)·(2)면 티어를 깎아도 안 고쳐진다")

    def _foreign_by_group(self) -> str:
        """경보 문구에 실을 **그룹별 foreign** 요약 (`그룹 frn/관측·최대`).

        경보 하나만 보고 원인 후보를 좁힐 수 있어야 한다. 지금까지는 경보를 맞은 그룹의
        수만 실려 있었고, 그래서 이 경보를 받고도 세 후보 중 어느 쪽인지 알 수 없었다
        (2026-08-13 실제로 그랬다 — docs/55 §3).

        관측이 있는 그룹만 싣는다. 관측 0 인 그룹을 "깨끗함" 으로 보이게 하지 않는다.
        """
        seen = [(g, self.server_seconds_seen(g)) for g in sorted(self._server_seconds)]
        parts = [f"{g} {self.foreign_seconds(g)}/{n}초·최대{self.foreign_sends(g)}"
                 for g, n in seen if n > 0]
        return " | ".join(parts) if parts else "그룹별 관측 없음"

    def over_limit_1s(self, group: str) -> bool:
        """**계상 첨두가 한도를 넘겼나** — 즉 우리 계상이 한도 초과를 주장하는가.

        ⚠️ **경보의 술어가 아니다** (2026-08-12, docs/52 §12). 송신 시각 계상 + 단조
        시계 이후 이 값은 하드캡을 지난 송신으로는 참이 될 수 없고, 참이 되는 경우는
        둘뿐이다: 리미터를 안 지난 송신이거나 **계상이 틀렸거나**. 후자를 잡는 데
        쓰인다 (`test_limiter_vs_counter.py`, W1: 오귀속 1건이 첨두를 11 로 만든다).

        경보·게이트·복원 거부는 전부 `server_over_limit_seconds()` /
        `quota_not_ours()` 로 옮겼다 — 그쪽은 창의 이름과 총량을 **서버가** 준다.
        """
        return self.peak_1s(group) > self.limit_of(group)

    # ---- 계획 ----------------------------------------------------------

    def set_plan(self, plan: TierPlan | None) -> None:
        self.plan = plan

    def planned_rate(self, group: str) -> float:
        return self.plan.rates().get(group, 0.0) if self.plan is not None else 0.0

    def predicted_rate(self, group: str) -> float:
        """예측 사용률 = max(계획, **초당 첨두**).

        실측 쪽을 60초 평균이 아니라 첨두로 잡는다 — 서버가 1초 창으로 재기 때문이다.
        재시도·백필처럼 계획에 없는 호출은 대개 몰려서 나가므로 평균에는 거의 안 보이고
        첨두에만 보인다.
        """
        return max(self.planned_rate(group), float(self.peak_1s(group)))

    def shrink_ceiling(self, group: str) -> float:
        """축소 판정 천장 — 계획·실측 **둘 다** 이 값에 대고 잰다 (기준 일치)."""
        return self.target(group) * self.headroom

    def plan_ceiling(self, group: str) -> float:
        """설정이 지켜야 할 상한 = 축소 천장에서 계획 밖 호출용 여유를 뺀 값."""
        return self.target(group) * (self.headroom - PLAN_RESERVE_FRAC)

    def note_session_change(self) -> None:
        """세션 전환을 알린다 — 이후 `MEASURED_WARMUP_S` 동안 측정 기반 축소를 멈춘다.

        **벽시계다.** 세션 경계는 서버 시각으로 정의되고, 워밍업은 그 경계로부터
        180초라는 뜻이기 때문이다 (docs/52 §5.4).
        """
        self._warmup_until_s = self._wall_s() + MEASURED_WARMUP_S
        self._measured_over_since.clear()

    def in_warmup(self) -> bool:
        return self._wall_s() < self._warmup_until_s

    def reserve_deficit(self) -> dict[str, float]:
        """계획이 여유(PLAN_RESERVE_FRAC)를 못 남긴 그룹 → 부족분 req/s.

        축소를 부르진 않지만 **경보 대상**이다: 이 상태의 설정은 계획 밖 호출이 조금만
        나가도 곧바로 축소 트리거를 건드린다.
        """
        if self.plan is None:
            return {}
        out: dict[str, float] = {}
        for group, rate in self.plan.rates().items():
            ceiling = self.plan_ceiling(group)
            if rate > ceiling:
                out[group] = rate - ceiling
        return out

    def validate_plan(self) -> dict[str, float]:
        """설정값만으로 예산 초과를 예측한다. 반환: {group: 초과 req/s} (없으면 빈 dict)."""
        if self.plan is None:
            return {}
        over: dict[str, float] = {}
        for group, rate in self.plan.rates().items():
            ceiling = self.shrink_ceiling(group)      # 축소 판정과 **같은 천장**
            if rate > ceiling:
                over[group] = rate - ceiling
        return over

    # ---- 축소 지시 ------------------------------------------------------

    def should_shrink(self) -> dict[str, int] | None:
        """그룹별 초과 예측 시 `{group: 줄일 심볼 수}`. 아니면 None.

        대상 티어는 `SHRINK_TIER` (MARKET_DATA→tier3, MARKET_DATA_CHART→tier2).
        랭킹은 과거 조회가 불가능한 유일한 데이터라 **축소 대상이 아니다** — 넘치면 경보만 낸다.

        판정 근거는 **초당 첨두(`peak_1s`)이지 60초 평균이 아니다.** 서버가 1초 창으로
        재기 때문이다 — 평균이 천장 아래라도 특정 1초에 몰렸으면 그것이 진짜 위반이고,
        반대로 평균이 높아도 고르게 퍼져 있으면 서버는 아무 불만이 없다.
        """
        if self.plan is None:
            return None
        now = self._wall_s()          # 쿨다운·지속 유지 시간 — 분 단위 판정은 벽시계
        out: dict[str, int] = {}
        for group in (GROUP_MARKET_DATA, GROUP_CHART, GROUP_RANKING):
            target = self.target(group)
            planned = self.planned_rate(group)
            # **정원은 지속 속도 손잡이다.** 그래서 축소의 근거는 지속률이고, 첨두가 아니다.
            #
            # 2026-08-04 22:33 개장 사고가 이 구분을 안 해서 났다: CHART 가 1초에 7회로
            # 튀자 그 첨두를 지속 초과로 환산해 "299종목을 빼라" 가 나왔다. tier2 한 종목은
            # 1/110 = 0.00909 req/s 라 1 req/s 를 줄이려면 110종목이 필요하기 때문이다 —
            # 산수는 맞지만 손잡이가 문제에 안 맞는다. 버스트는 **타이밍** 문제이고 그건
            # 리미터(대기)가 고친다. 정원을 깎아도 같은 버스트는 또 난다 —
            # 경보 문구가 이미 그렇게 말하고 있었는데 코드는 반대로 행동했다 (docs/33).
            sustained_rate = self.measured_rate(group)
            predicted = max(planned, sustained_rate)
            forced = self._forced.get(group, 0.0)
            ceiling = self.shrink_ceiling(group)      # 계획·지속률 공통 천장
            over_plan = planned > ceiling             # 설정 오류 — 즉시 반응
            over_rate = sustained_rate > ceiling      # 진짜 지속 과부하
            # 지속률 기반도 **지속성**을 요구하고 **워밍업 중에는 아예 보지 않는다**.
            if over_rate:
                self._measured_over_since.setdefault(group, now)
                sustained = now - self._measured_over_since[group] >= MEASURED_SUSTAIN_S
            else:
                self._measured_over_since.pop(group, None)
                sustained = False
            # 서버 한도 자체를 넘긴 초가 있으면 **정원 문제가 아니다** — 리미터가 1초 창을
            # 못 지키고 있다는 뜻이라 경보로 올린다 (축소해도 같은 버스트는 또 난다).
            # **에피소드 단위로** 센다: 첨두는 관측 지평(60s) 동안 남아 있으므로 매 평가마다
            # 세면 한 번의 버스트가 수십 건으로 부풀고, 그런 가짜 반복이 경보 무시 습관을
            # 만든다 (랭킹 forced 경보에서 이미 겪은 실패다).
            self._note_over_limit(group)
            over = over_plan or (sustained and not self.in_warmup())
            if not over and not forced:
                continue
            if group not in SHRINK_TIER:
                # 축소 불가 그룹(랭킹 — 과거 조회가 불가능한 유일한 데이터)은 자동으로
                # 할 수 있는 것이 없다. **사람이 개입해야 하는 상황에서만** 경보한다:
                #   (a) over   — 계획/실측이 실제로 예산을 넘었다 → 주기(ranking_snap_s)나
                #                한도 설정을 재검토해야 한다
                #   (b) forced — 서버가 429 를 반환했다 → 예산 이내였는데도 맞았다면
                #                우리가 아는 한도 인식 자체가 틀렸다는 뜻이다
                # 예전에는 (b)로 진입해도 (a)의 "predicted > target" 문구로 경보해
                # "0.33 > 3.50" 같은 **거짓 ERROR** 가 났고, forced 가 소거되지 않아
                # 같은 경보가 매 사이클 반복됐다 — 가짜 경보는 경보 무시 습관을 만들어
                # 진짜 경보를 묻는다 (W5 healthcheck 오탐과 같은 지적).
                self._forced.pop(group, None)          # 1회 경보 후 소거 (반복 방지)
                if self.notifier is not None:
                    if over:
                        self.notifier.alert(
                            f"budget: {group} predicted {predicted:.2f} req/s > target "
                            f"{target:.2f} — 랭킹은 축소 대상이 아니다. 주기/한도를 재검토하라")
                    else:
                        self.notifier.alert(
                            f"budget: 429 on {group} (usage {predicted:.2f}/{target:.2f} "
                            f"req/s, 예산 이내) — 랭킹은 축소 대상이 아니며 한도 인식이 "
                            "틀렸을 수 있다. 주기/한도를 재검토하라")
                continue
            last = self._last_shrink_s.get(group)
            if last is not None and now - last < SHRINK_COOLDOWN_S:
                continue
            n = self._shrink_symbols(group, predicted, target, forced)
            if n > 0:
                out[group] = n
                self._last_shrink_s[group] = now
                self._forced.pop(group, None)
        return out or None

    def should_grow(self) -> dict[str, int] | None:
        """429 없이 조용했고 여유도 있으면 그룹별 **되돌릴 심볼 수**를 돌려준다.

        축소만 있고 회복이 없으면 429 한 건이 세션 전체의 수집 범위를 깎는다
        (2026-08-04 실측: 429 8회에 tier2 300->76, tier3 20->2, 자동 복귀 없음).
        회복 조건은 보수적이다 — 마지막 429 이후 `RECOVER_AFTER_S`, 실사용이 목표의
        `RECOVER_USAGE_MAX` 미만, 그리고 계획도 목표 이내일 때만 한 스텝 올린다.
        """
        if self.plan is None:
            return None
        now = self._wall_s()          # 429 후 대기·스텝 간격 — 분 단위 판정은 벽시계
        out: dict[str, int] = {}
        for group in (GROUP_MARKET_DATA, GROUP_CHART):
            if group not in self._last_shrink_s:
                continue                                  # 깎인 적이 없으면 되돌릴 것도 없다
            last429 = self._last_429_s.get(group)
            if last429 is not None and now - last429 < RECOVER_AFTER_S:
                continue                                  # 아직 사고 직후다
            if self._forced.get(group):
                continue                                  # 처리 안 된 축소 지시가 남아 있다
            last_grow = self._last_grow_s.get(group)
            if last_grow is not None and now - last_grow < RECOVER_AFTER_S:
                continue                                  # 스텝 간 최소 간격
            target = self.target(group)
            if target <= 0:
                continue
            # **축소와 같은 양을 본다.** 2026-08-04 수정이 축소를 지속률(`measured_rate`)로
            # 옮기면서 복원은 `peak_1s` 에 남겨뒀는데, 그 비대칭에 근거가 없었다:
            # 지속률로 깎고 첨두로 복원을 막으면 정원은 **내려가기만 한다.**
            #
            # 게다가 그 비교는 단위가 안 맞았다. `peak_1s` 는 관측 지평(60초) 중 **최악의
            # 1초**이고 `target` 은 **지속 속도** 예산(8.5 req/s)이다. 최댓값을 평균 예산에
            # 대고 재면 리미터가 완벽해도 트립한다 — tier3 루프가 4초마다 20건을 몰아 쏘는
            # 실제 모양에서 지속률은 5.0(목표의 59%)인데 첨두는 9 이고, 문턱은 5.95 다.
            # 오프라인 재생(939 표본, docs/46 §5): 이 조건이 **93.8% 의 표본**에서
            # 불만족이었고, 정원을 1 까지 깎아도 중앙 첨두가 문턱 위였다.
            if self.measured_rate(group) > target * RECOVER_USAGE_MAX:
                continue                                  # 지속 사용률이 아직 빡빡하다
            # **거부가 일어나야 하는 상황** (2026-08-12 재조준, docs/52 §12):
            #
            #   "정원이 깎여 있고 회복 조건(429 조용·지속률 여유·계획 이내)은 다 만족했는데,
            #    이 그룹의 초당 한도가 **우리 것만이 아니다**."
            #
            # 정원 복원은 "지금 한도에 여유가 있다" 를 전제로 한다. 같은 자격증명으로 도는
            # 다른 발신자가 그 초를 같이 깎고 있으면 그 전제가 거짓이고, 되돌리는 순간
            # 429 를 다시 부른다 — 그리고 리미터는 프로세스 간 조율을 못 하므로 우리 쪽
            # 관측만으로는 영영 안 보인다 (docs/06 §9-6, §9-7 요건 2).
            #
            # 예전 조건은 `peak_1s > limit_of` 였고 의도는 같았지만 **실행되지 않았다**:
            # 리미터 하드캡이 있는 한 우리 송신을 우리 시계로 센 첨두는 한도를 넘을 수
            # 없다 (docs/52 §6). 즉 이 자리는 "거부를 안 하는" 것이 아니라 "거부를 못 하는"
            # 상태였다. 이제 서버가 이름 붙인 초에서 잰다.
            if self.server_window_violated(group):
                continue                                  # 우리 몫이 한도만큼 있지 않다
            if self.planned_rate(group) > target:
                continue                                  # 계획 자체가 초과 상태
            have = self.plan.symbols_of(group)
            if have <= 0:
                continue
            out[group] = max(1, math.ceil(have * RECOVER_STEP_FRAC))
            self._last_grow_s[group] = now
        return out or None

    def _shrink_symbols(self, group: str, predicted: float, target: float,
                        forced: float) -> int:
        assert self.plan is not None
        cost = self.plan.per_symbol_cost(group)
        have = self.plan.symbols_of(group)
        if cost <= 0 or have <= 0:
            return 0
        excess = max(predicted - target * SHRINK_TO, 0.0)
        n = math.ceil(excess / cost) if excess > 0 else 0
        if forced:
            n = max(n, math.ceil(have * forced))
        # 한 스텝 상한 — 근거가 옳아도 한 번에 정원을 통째로 날리지 않는다.
        n = min(n, max(1, math.ceil(have * SHRINK_MAX_STEP_FRAC)))
        return int(max(0, min(n, have - 1 if have > 1 else have)))

    # ---- 관측 덤프 ------------------------------------------------------

    def snapshot(self) -> dict[str, dict[str, float]]:
        # `self.counters` 에는 그룹별 요청 수와 **전역 카운터**(`server_seconds`,
        # `quota_not_ours` …)가 같이 산다. 그것을 그대로 그룹 목록으로 쓰면 전역 카운터
        # 이름이 유령 그룹으로 렌더된다 — 운영 로그에 실제로 그렇게 찍혀 있었다
        # (`| budget … quota_not_ours=peak0/p95:0/avg0.00/tgt0.85`). 유령 그룹은 한도 1.0
        # 짜리 미지 그룹으로 보이므로 **"그 그룹은 깨끗하다" 로 읽힌다.** 이 진단을 읽으려고
        # 만든 줄이 진단을 방해하고 있었다 (docs/55 §6).
        groups = (set(self.counters) - set(GLOBAL_COUNTERS)) | set(self._events) | set(
            self.plan.rates() if self.plan is not None else {})
        return {
            group: {
                "limit": self.limit_of(group),
                "target": self.target(group),
                "peak_1s": float(self.peak_1s(group)),
                "p95_1s": self.p95_1s(group),
                "planned": self.planned_rate(group),
                "measured": self.measured_rate(group),
                "requests": float(self.counters.get(group, 0)),
                "http_429": float(self.rate_limited.get(group, 0)),
                # 서버 초 감사 — **분모(`server_seconds`)를 같이 낸다.** 0/0 과 0/58 은
                # 다른 진술이고, 분모 없는 0 을 안전으로 읽은 실패가 이미 있었다 (docs/52 §7.2).
                "server_seconds": float(self.server_seconds_seen(group)),
                "server_over_1s": float(self.server_over_limit_seconds(group)),
                "foreign_seconds": float(self.foreign_seconds(group)),
                "foreign_max": float(self.foreign_sends(group)),
            }
            for group in sorted(groups)
        }

    def describe(self) -> str:
        """텔레메트리 꼬리의 `| budget …` 한 줄.

        서버 초 감사를 **그룹마다 나란히** 싣는다 (2026-08-13, docs/55 §5). 텔레메트리
        본문은 `md_foreign_s` 처럼 MARKET_DATA 만 싣는데, docs/52 §12 §3 이 "못 갈랐다" 고
        남긴 두 후보는 **그룹 사이의 비대칭**으로만 갈린다:

          * **바구니가 우리 그룹들을 걸쳐 있다** — 소진량에 남의 그룹 송신이 섞여 보이므로
            **송신이 적은 그룹에서 foreign 이 가장 크다.** RANKING(0.25/s)이 MARKET_DATA
            (2/s)보다 큰 `frn`/`frnmax` 를 내면 그 방향이다.
          * **같은 자격증명의 다른 발신자** — 그의 송신은 우리 그룹 구성과 무관하므로
            비대칭이 없다. 그룹마다 자기 관측 기회에 비례해 비슷하게 보인다.
          * **창 어긋남(경계 렌더·늦은 도착)** — 크기가 **그 그룹 자기 송신의 변동폭**에
            갇힌다. 3건 버스트를 내는 RANKING 은 최대 2 를 넘을 수 없다 (docs/55 §4).

        `srv` 가 분모다 — 분모 없는 0 은 아무 뜻도 없다 (docs/52 §7.2).
        """
        # 첨두를 먼저 보여준다 — 평균만 보면 버스트가 안 보인다(2026-08-04 착시).
        parts = [f"{g}=peak{int(s['peak_1s'])}/p95:{s['p95_1s']:.0f}"
                 f"/avg{s['measured']:.2f}/tgt{s['target']:.2f}"
                 f"/srv{int(s['server_seconds'])}/over{int(s['server_over_1s'])}"
                 f"/frn{int(s['foreign_seconds'])}/frnmax{int(s['foreign_max'])}"
                 for g, s in self.snapshot().items()]
        return "budget " + " ".join(parts)


def limits_from(mapping: Mapping[str, float] | None) -> dict[str, float]:
    return {str(k): float(v) for k, v in (mapping or {}).items()}


__all__ = ["BudgetGuard", "TierPlan", "SHRINK_TIER", "GROUP_CHART", "GROUP_MARKET_DATA",
           "GROUP_RANKING", "limits_from"]

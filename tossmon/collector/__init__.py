"""티어드 수집 루프와 전조 검출기 (Phase 1 실시간 경로). 거래 코드 없음.

구성 (docs/03 §4)
    scheduler  — /market-calendar/US 기반 세션 인지 + 서버 시각 보정
    loops      — tier1 가격 스윕 / tier2 1분봉 / tier3 마이크로 / 랭킹 스냅샷
    detector   — 전조·확인 두 경로 스코어, 티어 상태머신, 실시간 이벤트 검출
    budget     — 그룹별 사용량 관측 + 예산 초과 예측 시 안전 강등
    notifier   — 콘솔·파일 로그 (텔레그램은 인터페이스만)
"""

from .budget import BudgetGuard, TierPlan
from .detector import (EventDetector, PriceActivityTracker, TierStateMachine,
                       activity_score, confirm_score, precursor_score)
from .loops import (CollectorContext, run_all, run_rankings, run_session_watch,
                    run_tier1_price_sweep, run_tier2_candles, run_tier3_micro)
from .notifier import Notifier
from .scheduler import Clock, SessionScheduler, current_session

__all__ = [
    "BudgetGuard", "Clock", "CollectorContext", "EventDetector", "Notifier",
    "PriceActivityTracker", "SessionScheduler", "TierPlan", "TierStateMachine",
    "activity_score", "confirm_score", "current_session", "precursor_score", "run_all",
    "run_rankings", "run_session_watch", "run_tier1_price_sweep", "run_tier2_candles",
    "run_tier3_micro",
]

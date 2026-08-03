"""거래 **세션**을 1급 차원으로 다루는 단 하나의 정의부 (사용자 지적 2026-08-03).

## 왜 이 모듈이 생겼나

사용자 지적: **"정규장이 아닌 거래 데이터에서는 거래량과 유동성이 현저히 낮다."**
그런데 우리는 지금까지 **전 세션을 뭉쳐서** 단일 비용 2.38% 를 적용해 왔다.
코디네이터 실측에서 세션 간 스프레드가 **8배**까지 벌어졌다($2~5 대역 중앙값:
주간 1.10% / 정규장 2.15% / 애프터 2.94% / 프리마켓 8.86%).

세션을 안 가르면 "총수익 0.5% < 비용 2.38%" 라는 결론이 **세션 혼합의 산물**일 수 있다.
그래서 세션은 필터가 아니라 **차원**이다 — 모든 표에 열로 들어간다.

## 경계 (KST 기준)

| 세션 | KST | 설명 |
|---|---|---|
| `day` | 09:00–17:00 | 토스 주간거래 |
| `pre` | 17:00–22:30 | 프리마켓 |
| `regular` | 22:30–05:00 | **미국 정규장** (자정을 넘는다) |
| `after` | 05:00–08:50 | 애프터마켓 |
| `closed` | 08:50–09:00 | 어느 세션도 아님 |

## 두 가지 날짜가 있다 — 헷갈리면 안 된다

- **달력 날짜**(UTC 또는 KST): 스냅이 찍힌 날.
- **세션 사이클 날짜**(`session_date`): **한 미국 거래일에 속하는 한 묶음.**
  KST 09:00 에 시작해 **다음 날 08:50 에 끝난다.** 즉 KST 09:00 이전 시각은
  **전날 사이클**에 속한다. 정규장이 자정을 넘으므로 이 구분이 없으면 같은 정규장이
  두 날로 쪼개진다.

## 한계 — **고정 오프셋 근사다**

실제 미국 세션 경계는 서머타임에 따라 한 시간 움직인다. 이 모듈은 `UsMarketDay`
달력이 없을 때 쓰는 **고정 KST 경계 근사**이며, 현재 자료 구간(2026-07~08, 미국 EDT)
에서만 검증됐다. 달력을 쓸 수 있게 되면 `session_of` 를 그쪽으로 갈아끼우고
**이 파일 하나만 고치면 되도록** 정의를 여기에 모아 둔다(중복 정의 금지).
"""
from __future__ import annotations

import pandas as pd

KST_OFFSET_MS = 9 * 3_600_000
MIN_MS = 60_000
DAY_MIN = 1440

#: (시작분, 끝분) — KST 자정 기준 분. `regular` 만 자정을 넘으므로 따로 처리한다.
DAY_START, DAY_END = 9 * 60, 17 * 60            # 09:00-17:00
PRE_START, PRE_END = 17 * 60, 22 * 60 + 30      # 17:00-22:30
REG_START, REG_END = 22 * 60 + 30, 5 * 60       # 22:30-05:00 (자정 넘음)
AFT_START, AFT_END = 5 * 60, 8 * 60 + 50        # 05:00-08:50

#: 사이클 시작 시각(KST 분). 이 시각 **이전**은 전날 사이클이다.
CYCLE_START_MIN = DAY_START

#: 표에 쓰는 고정 순서. 시간 순서대로 둔다.
SESSIONS = ("day", "pre", "regular", "after", "closed")


def kst_minute_of_day(ts_ms: int) -> int:
    """epoch ms -> KST 자정 기준 분."""
    return int(((int(ts_ms) + KST_OFFSET_MS) // MIN_MS) % DAY_MIN)


def session_of(ts_ms: int) -> str:
    """그 시각이 속한 **세션**. 어느 세션도 아니면 `closed`.

    경계는 **시작 포함, 끝 제외**다. 진입이 세션 경계에 걸치면 **진입 시각 기준**으로
    배정한다 — 이탈이 다음 세션에서 일어나도 그 진입은 진입 세션에 속한다.
    (비용·유동성은 들어갈 때 결정되고, 한 진입을 두 세션에 나눠 셀 수는 없다.)
    """
    m = kst_minute_of_day(ts_ms)
    if DAY_START <= m < DAY_END:
        return "day"
    if PRE_START <= m < PRE_END:
        return "pre"
    if m >= REG_START or m < REG_END:
        return "regular"
    if AFT_START <= m < AFT_END:
        return "after"
    return "closed"


def session_date(ts_ms: int) -> str:
    """**세션 사이클 날짜** — KST 09:00 에 시작해 다음 날 08:50 에 끝나는 한 묶음.

    KST 09:00 이전은 **전날 사이클**이다. 이게 없으면 자정을 넘는 정규장이 두 날로
    쪼개져 날 군집 수가 부풀고, 같은 정규장 진입이 다른 날로 셈해진다.
    """
    ts = pd.Timestamp(int(ts_ms) + KST_OFFSET_MS, unit="ms")
    if kst_minute_of_day(ts_ms) < CYCLE_START_MIN:
        ts = ts - pd.Timedelta(days=1)
    return ts.strftime("%Y-%m-%d")


def sessions_of(ts: pd.Series) -> pd.Series:
    """벡터화 판정 — 행마다 파이썬 호출을 돌리면 러너가 눈에 띄게 느려진다."""
    if ts is None or len(ts) == 0:
        return pd.Series(dtype="object")
    m = ((pd.to_numeric(ts, errors="coerce") + KST_OFFSET_MS) // MIN_MS) % DAY_MIN
    out = pd.Series("closed", index=ts.index, dtype="object")
    out[(m >= DAY_START) & (m < DAY_END)] = "day"
    out[(m >= PRE_START) & (m < PRE_END)] = "pre"
    out[(m >= REG_START) | (m < REG_END)] = "regular"
    out[(m >= AFT_START) & (m < AFT_END)] = "after"
    return out


def session_counts(ts: pd.Series) -> dict:
    """세션별 건수. **0 건인 세션도 키를 남긴다** — 표에서 조용히 사라지지 않도록."""
    got = sessions_of(ts).value_counts().to_dict() if len(ts) else {}
    return {s: int(got.get(s, 0)) for s in SESSIONS}

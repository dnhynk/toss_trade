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


# --------------------------------------------------------------------------- #
# 봉 라벨 규약 — **이 저장소에서 이 규약이 정의되는 유일한 곳** (docs/12 §6.1)
# --------------------------------------------------------------------------- #
def bar_start_ms(ts_ms: int) -> int:
    """1분봉 라벨 → 그 봉이 **담는 구간의 시작 시각**.

    `candles_1m.ts_ms` 는 **종료 시각 라벨**이다 (사전등록 §6.1, 2026-08-07 개정).
    `ts_ms = T` 인 봉은 `[T−60초, T)` 를 담고 시각 `T` 에 **이미 완결**돼 있다.
    근거: docs/36 §1 의 세 갈래 독립 검증(일봉 거래량 정확 일치 87/88 = 98.9% vs
    시작 라벨 1/88, 개장 스파이크 13:31Z, 마감 절벽 21:00Z) + W5 docs/31 §2.

    **캔들 라벨을 달력·세션·창 경계와 비교하거나 분 슬롯에 배치할 때는 반드시
    이 함수를 통과시킨다.** 라벨을 그대로 비교하면 경계식이 60초 밀린다 —
    `start <= ts < end` 가 아니라 `start < ts <= end` 가 옳고, 그 둘은
    `start <= bar_start_ms(ts) < end` 로 같은 식이 된다.

    **체결 테이프·호가 스냅에는 쓰지 마라.** 그쪽 `ts_ms`/`snap_ms` 는 라벨이 아니라
    **진짜 순간**이다 (docs/36 §2-0). 그래서 `session_of`·`session_date` 자체는
    이 보정을 하지 않고, 캔들을 다루는 **호출부**가 이 함수를 씌운다.
    """
    return int(ts_ms) - MIN_MS


def bar_starts_ms(ts) -> pd.Series:
    """`bar_start_ms` 의 벡터화판. 숫자로 못 읽는 값은 NaN 으로 남긴다."""
    return pd.to_numeric(ts, errors="coerce") - MIN_MS


#: **홀드아웃 봉인 구간** (docs/12 §6.1). §6.2-4: "홀드아웃 기간 데이터는 기술통계·
#: 튜닝·탐색 **어디에도 등장하면 안 된다.**" 랭킹 스냅은 07-31 부터라 문제가 없지만
#: `candles_1m` 은 **백필이라 07-27~07-29 를 담고 있다** — 그대로 쓰면 봉인을 깬다.
#: 그래서 필터를 라이브러리에 두고 러너가 반드시 지나가게 한다.
HOLDOUT_START = "2026-05-01"
HOLDOUT_END = "2026-07-29"


def is_holdout(ts_ms: int) -> bool:
    """그 시각이 봉인 구간에 속하나 (**세션 사이클 날짜** 기준).

    **라벨이 아니라 봉이 담는 구간으로 판정한다** (`docs/12` §6.2 항목 5,
    2026-08-07 개정 — 사용자 승인, 개정문 `docs/40`). 실측으로 라벨
    `2026-07-30T00:00Z` 의 9봉이 넘어왔었다(`docs/36` §3-3).
    """
    return HOLDOUT_START <= session_date(bar_start_ms(ts_ms)) <= HOLDOUT_END


def drop_holdout(df: pd.DataFrame, ts_col: str = "ts_ms") -> dict:
    """봉인 구간 행을 **버리고 몇 개 버렸는지 돌려준다.**

    조용히 거르지 않는다 — 버린 수를 산출물에 실어야 다음 사람이 봉인이 지켜졌는지
    확인할 수 있다.
    """
    if df is None or len(df) == 0:
        return {"kept": df, "n_dropped": 0, "n_kept": 0}
    mask = pd.to_numeric(df[ts_col], errors="coerce").map(
        lambda m: is_holdout(int(m)) if m == m else False)
    return {"kept": df[~mask].copy(), "n_dropped": int(mask.sum()),
            "n_kept": int((~mask).sum())}


#: **수집기 재시작 경계** (2026-08-04 00:07:28 KST, 공백 34초). 이 시각 이후
#: **티어 승격 정책이 달라져 어떤 종목이 조밀하게 수집되는지가 다르다**
#: (승격률 110/분 -> 1/분 미만). `trades_snap` 기반 분석은 **이 경계를 넘어 뭉치면
#: 안 된다** — 표본 구성이 바뀐 것을 시장 변화로 오독하게 된다.
COLLECTOR_RESTART_MS = 1785769648000

#: **수집기 경계는 하나가 아니다.** 2026-08-04 00:07:28 KST 에 이어 **02:26 KST** 에도
#: 수집 동작이 바뀌었다. 경계를 하나만 알고 있으면 두 번째 경계를 넘어 뭉치게 된다.
COLLECTOR_BOUNDARIES_MS = (COLLECTOR_RESTART_MS, 1785777960000)
COLLECTOR_ERAS = ("era0_pre_restart", "era1_post_restart", "era2_post_0226")


def collector_era(ts_ms: int) -> str:
    """수집기 경계로 나눈 **수집 시기**. 시기마다 조밀 수집 대상 종목이 다르다.

    경계를 넘어 `trades_snap` 을 뭉치면 **표본 구성 변화를 시장 변화로 오독**한다.
    """
    ts = int(ts_ms)
    idx = sum(1 for b in COLLECTOR_BOUNDARIES_MS if ts >= b)
    return COLLECTOR_ERAS[idx]


def eras_of(ts) -> pd.Series:
    """벡터화 판정."""
    if ts is None or len(ts) == 0:
        return pd.Series(dtype="object")
    # 규칙은 `collector_era` 한 곳에만 둔다 — 두 군데면 언젠가 갈라진다.
    v = pd.to_numeric(ts, errors="coerce")
    return pd.Series([collector_era(int(x)) if x == x else COLLECTOR_ERAS[0]
                      for x in v], index=ts.index, dtype="object")


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

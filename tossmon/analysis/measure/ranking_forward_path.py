"""**랭킹 사건의 전방 가격 경로** — `docs/59` 가 만든 사건 격자 위에서 결과를 잰다.

## 이 모듈의 지위 — **탐색이다. 판정이 아니다**

`docs/58` §G-5 가 *"G-2 착수 전에 새 사전등록이 필요하다"* 를 아직 살려두고 있고,
그 사전등록은 코디네이터가 따로 쓰고 있다. **이 모듈은 그것과 독립이고 판정을 하지
않는다.** 통과/실패를 쓰지 않고, 비용을 차감하지 않는다(그것은 G-3 이다).

**산출물에 붙는 라벨 넷** (사용자 결정, `docs/58` §G-2). 콘솔·JSON 양쪽에 싣는다:

1. 탐색 · 판정 아님
2. 9 세션(07-31, 08-03~07, 08-10~12)은 앞으로 확증에 재사용 불가
3. 이 9 세션은 폴 주기 12.4 초 아래 데이터다 - 서버 10 초 격자 틱의 29% 미수신,
   받은 랭킹은 중앙 16.1 초 늙음 (`docs/35`, W1 실측)
4. 확증은 2026-08-13 이후 데이터로만 하며, 08-18 현재 3 세션뿐이라 아직 못 연다

**"성립 안 함" 이라고 쓰지 않는다.** 정확한 진술은
*"N 초 창, 표본 M 에서 관측되지 않았다"* 이다.

## 사건 정의 - **`docs/59` 의 격자를 그대로 쓴다.** 새로 만들지 않는다

`hires_events.chunk_events` 를 **그대로 불러** 사건을 만든다. 근거 셋:

1. **가격이 사건 정의에 한 번도 안 들어간다.** 그 성질이 설계 A 의 죽음
   (*"탐지가 상승을 소진한다"*)을 구조적으로 막는다 - 그 성질을 잃지 않으려면
   정의를 다시 쓰지 말고 불러 써야 한다.
2. **격자로 센다.** 파라미터 하나에 답이 붙는지가 이 프로젝트가 반복해 데인 자리다
   (설계 B 의 +2.36% 중 약 1.85pp 가 우리 임계값이었다).
3. **표본 수가 이미 알려져 있다.** `docs/59` §6-2 · §7-2 의 "잴 수 있는 것" 이
   여기서 나올 표본 수의 상한이고, 두 문서가 어긋나면 그 자체가 오류 신호다.

> ### ★ 반드시 병기한다 - `TOP_GAINERS` 는 **서버가 가격으로 고른 목록**이다
>
> 우리 사건 정의에는 가격이 없지만, `TOP_GAINERS` 라는 **목록 자체**가 "오늘 많이
> 오른 순" 이다. 즉 그 목록에 새로 든다는 것은 **서버 쪽에서 이미 가격 임계를 넘었다**
> 는 뜻이고, 설계 A 를 죽인 *"탐지가 상승을 소진한다"* 와 같은 모양의 오염이 **우리
> 코드 밖에서** 들어온다. `TOSS_SECURITIES_TRADING_VOLUME` 은 거래량 기준이라 그
> 축에서는 더 깨끗하다. **그래서 둘을 나란히 낸다** - 하나를 고르지 않는다.

## 앵커 - `t0` 뒤 **처음 오는 초 막대**

`t0` 는 우리가 스냅을 **받은** 시각이다(서버 재계산 시각이 아니다, 중앙 16.1 초 늙음).
앵커는 `ts > t0` 인 첫 막대다 - **`t0` 와 같은 초의 막대는 안 쓴다.** 그 초 안에는
`t0` 이전 체결이 섞여 있고, `docs/59` 의 커버리지 창도 `(t0, t0+W]` 라 같은 경계다.
그래서 여기서 나오는 "앵커가 있는 사건" 수가 `docs/59` 의 "잴 수 있는 것" 과
**대조 가능**하다.

앵커까지 걸린 시간(`anchor_lag_s`)을 **버리지 않고 분포로 낸다.** `docs/44` §3-2 가
배운 것이 그것이다 - *"진입 지연의 실체는 2.56 초가 아니라 다음 체결이 안 온다"* 이다.

## 대조 - **D-19 를 정면으로 다루는 자리**

D-19: *발화가 몰리는 종목이 원래 더 출렁이는 종목일 수 있다.* 그래서 위약을
**같은 종목 · 같은 정규장 · 다른 시각**에서 뽑고 밴드를 한 칸씩 켠다
(`docs/44` §11-1 의 축, §14-1 의 사다리를 **그대로** 불러 쓴다):

| 팔 | `rv60` | `nbar60` | 무엇을 묻나 |
|---|---|---|---|
| `placebo_unmatched` | - | - | 아무 순간과 다른가 |
| `placebo_vol_matched` | +-20% | - | 같은 변동성의 순간과 다른가 |
| `placebo_vol_density_matched` | +-20% | +-20% | 같은 활발함까지 맞추면 무엇이 남는가 |

**분리하지 못하면 "분리 못 했다" 라고 적는다.** 분리한 척하지 않는다.

## D-20 - 짝을 못 지은 사건은 **세고 프로파일한다**

`docs/44` §14-4 와 **같은 자**로 잰다: 짝 잃은 쪽의 `nbar60` 중앙값 · 앵커 지연 ·
전방 창에 막대가 0 인 비율. 성긴 테이프 사건은 **정합으로 원리상 답이 안 나오므로**
정합 표에서 빠지는데, 뺐다는 것과 그 프로파일을 같이 낸다. 세는 것만으로는 부족하다 -
잃은 것이 한쪽으로 치우쳐 있으면 **남은 표본 자체가 옮겨간다**.

## 눈금과 CI

- **눈금 둘을 같이 낸다** (`docs/44` §13-14): 사건 가중(정본)과 종목당 균등.
  §14-6 에서 눈금에 따라 부호가 뒤집힌 적이 있다 - 한 눈금만 내면 그것을 못 본다.
- **CI 는 세션 군집 부트스트랩**이고, 거래일 군집이 5 미만이면 **내지 않는다**
  (`STRATEGY-VERDICTS` §4.4-B 의 규율. 규율이 아니라 코드가 막는다).

## 하지 않는 것

- **판정하지 않는다.** 통과/실패를 쓰지 않는다.
- **비용을 차감하지 않는다.** G-3 이고 사전등록이 필요하다.
- **홀드아웃(2026-05-01~07-29)을 열지 않는다.** `hires_events.holdout_floor_ms` 로
  바닥을 걸고 `session.drop_holdout` 으로 한 번 더 거른다.
- **D-21 경계를 넘어 뭉치지 않는다.** 세션마다 시대(`era`)를 붙이고 시대별로 낸다.
- **라이브 워크트리에 쓰지 않는다.** DB 는 `mode=ro`, API 호출 0 건.

실행: `python -m tossmon.analysis.measure.ranking_forward_path [db] [--since-d21]
[--until-d21] [--until-ms N] [--out DIR] [--name NAME]`
-> `out/ranking_forward_path.json`. **콘솔 ASCII.**
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from tossmon.analysis import hires_events as HE
from tossmon.analysis import session as SS
from tossmon.analysis.measure.density_matched_placebo import (
    add_trade_count,
    draw_stratified,
    stratified_index,
)
from tossmon.analysis.measure.tick_resolution import pct_table
from tossmon.analysis.measure.vol_matched_placebo import (
    MATCH_DRAWS,
    VOL_LOOKBACK_S,
    VOL_MATCH_TOL,
    arm_entry_outcomes,
    arm_forward_probe,
    build_universe,
)

#: 산출물은 소스 옆이 아니라 저장소 루트의 `out/` 에 쓴다(`.gitignore` 대상).
OUT_DIR = Path(__file__).resolve().parents[3] / "out"

SEC_MS = 1_000

#: 전방 창. **300 초는 `docs/59` 의 커버리지 창과 같은 값**이라 두 문서의 표본 수가
#: 서로 대조된다. 30/60 은 해상도 쪽 - `docs/59` §9-3 이 "300 초(커버리지) vs 60 초
#: (해상도)" 를 갈림길로 넘겼다. **하나를 고르지 않고 셋 다 낸다.**
PROBE_HORIZONS_S = (30, 60, 300)

#: 앵커를 기다리는 상한. `docs/59` 의 "잴 수 있는 것"(300 초 안 체결 1 건 이상)과
#: **같은 정의**가 되도록 최대 지평과 맞춘다.
ANCHOR_MAX_WAIT_S = max(PROBE_HORIZONS_S)

#: 위약이 자기 사건의 측정 구간과 겹치지 않게 하는 간격. `vol_matched_placebo` 의
#: 180 초는 지평 120 초용이다 - 우리 지평이 300 초라 **그대로 쓰면 겹친다.**
SELF_GAP_S = max(PROBE_HORIZONS_S) + VOL_LOOKBACK_S

#: 헤드라인 지표. "몰림이 보이는가" 는 **앵커 뒤 최고가까지의 상승폭**이다.
#: `end_ret` 이 아니라 `max_ret` 인 이유: 골격 문언이 "몰린다" 이지 "300 초 뒤에도
#: 높다" 가 아니다. `end_ret` 도 같은 표에 낸다.
HEADLINE_H = 300
HEADLINE = f"max_ret_{HEADLINE_H}s"

#: 랭킹 타입 둘. `docs/59` §9-3 이 넘긴 갈림길 그대로 - **고르지 않고 둘 다 낸다.**
#: `TOP_GAINERS` = 잴 수 있는 것 최다 / `TOSS_..._VOLUME` = 표적 층 사건 최다.
RANKING_SPECS = (
    ("TOP_GAINERS", "1d"),
    ("TOSS_SECURITIES_TRADING_VOLUME", "realtime"),
)

#: 주 칸 셋. `docs/59` §5 의 대표 칸과 **같은 칸**이라 표본 수가 서로 대조된다.
#: 위약 사다리는 이 칸에만 붙인다.
PRIMARY_CELLS = (
    ("E1_new_entry", "N50"),
    ("E2_rank_jump", "K10"),
    ("E3_dwell_start", "N50_M3"),
)

#: 격자 쓸이 - **답이 파라미터 하나에 붙는지**를 본다. 실제 팔만 낸다.
SWEEP_CELLS = (
    ("E1_new_entry", "N10"), ("E1_new_entry", "N20"),
    ("E1_new_entry", "N50"), ("E1_new_entry", "N100"),
    ("E2_rank_jump", "K5"), ("E2_rank_jump", "K10"), ("E2_rank_jump", "K20"),
    ("E3_dwell_start", "N50_M3"), ("E3_dwell_start", "N50_M6"),
)

ALL_CELLS = tuple(sorted(set(PRIMARY_CELLS) | set(SWEEP_CELLS)))

#: 사다리. **한 칸에 하나씩만** 밴드가 켜진다(`docs/44` §14-1 의 규율).
PLACEBO_ARMS = (
    ("placebo_unmatched", False, False),
    ("placebo_vol_matched", True, False),
    ("placebo_vol_density_matched", True, True),
)

#: 부트스트랩. 군집은 **거래일**이다 - 같은 날의 사건은 같은 장세를 공유하므로
#: 사건 단위 재추출은 CI 를 실제보다 좁게 만든다(§4.4-A 가 "48 건 중 39 건이 하루"
#: 로 데인 자리다).
BOOTSTRAP_N = 10_000
BOOTSTRAP_SEED = 20260818
#: **거래일 군집 5 미만이면 통합 CI 를 내지 않는다** (`STRATEGY-VERDICTS` §4.4-B).
MIN_SESSION_CLUSTERS = 5

#: 추첨 씨앗. `vol_matched_placebo.SEED` 를 그대로 쓰지 않는 이유는 그 씨앗이 발화
#: 집합용이고 여기는 사건 집합이라 대응이 없기 때문이다. 값은 이 태스크의 날짜다.
SEED = 20260818

#: 균형표에 싣는 정합 키와 감시 항목. `ntrade60` 은 **정합 키가 아니다** -
#: 4 초 칸당 50 건 상한에 검열돼 있다(`docs/41` §4).
BALANCE_KEYS = ("rv60", "nbar60", "ntrade60")

#: 앵커 **이후**의 진단 둘. 정합 키가 아니라 **위약이 사건과 같은 자리에서 뽑혔는지**
#: 보는 눈이다.
#:
#: - `t_in_session_s`: 개장으로부터 몇 초. 사건이 개장 첫머리에 몰려 있고 위약이
#:   장 전체에 고루 퍼져 있으면 두 팔의 전방 창 **깊이가 다르다** - 마감에 가까운
#:   앵커는 300 초 창이 잘리기 때문이다. 그러면 격차가 시장이 아니라 **창 절단**이다.
#: - `n_bars_300s`: 전방 창에 실제로 들어온 막대 수. 그 절단이 일어났는지 직접 센다.
POSITION_KEYS = ("t_in_session_s", f"n_bars_{max(PROBE_HORIZONS_S)}s")

#: 수익률 성격의 지표 - 양(+) 비율을 같이 낸다.
RETURN_METRICS = tuple(
    [f"max_ret_{h}s" for h in PROBE_HORIZONS_S]
    + [f"end_ret_{h}s" for h in PROBE_HORIZONS_S]
    + ["mfe_60s", "mae_60s"])

#: **산출물이 싣는 지표 전부.** 테스트가 부분집합이 아니라 **동일 집합**으로 대조한다.
REPORTED_METRICS = tuple(
    list(RETURN_METRICS)
    + [f"t_max_{h}s" for h in PROBE_HORIZONS_S]
    + [f"n_bars_{h}s" for h in PROBE_HORIZONS_S]
    + list(BALANCE_KEYS) + ["t_in_session_s"])

#: **모든 산출물에 붙는 라벨 넷.** 사용자 결정(`docs/58` §G-2). ASCII 로만 쓴다 -
#: 콘솔이 cp949 이고 이 줄들은 화면에도 그대로 나간다.
LABELS = (
    "EXPLORATION - NOT A VERDICT",
    "the 9 sessions (07-31, 08-03..07, 08-10..12) can never be reused for confirmation",
    "those 9 sessions sit under a 12.4s poll: 29% of the server's 10s grid ticks were "
    "never received and the rankings we did get were a median 16.1s old (docs/35, W1)",
    "confirmation may use only data after 2026-08-13, and as of 2026-08-18 that is "
    "3 sessions - not enough to open yet",
)

#: 이 모듈이 **쓰면 안 되는** 문구. 산출물 전체를 훑어 이 조각이 없는지 테스트가 본다.
#: "관측되지 않았다" 와 "성립하지 않는다" 는 다른 문장이다.
FORBIDDEN_PHRASES = ("does not hold", "no alpha", "strategy works", "confirmed")


# --------------------------------------------------------------------------- #
# 적재
# --------------------------------------------------------------------------- #
def session_second_bars(conn: sqlite3.Connection, lo_ms: int, hi_ms: int) -> dict:
    """`[lo_ms, hi_ms)` 안의 **종목별 초 막대**. 창을 인자로 받는다.

    `tick_resolution.second_bars` 와 **같은 집계**(초 안의 순서를 쓰지 않는 유일한
    안전한 표현)이지만 그 함수는 `WINDOW_START_MS` 를 박아 두어 07-31 · 08-03 을
    통째로 잘라낸다. `docs/59` 의 사건 격자는 07-31 부터라 그대로는 못 쓴다.
    그리고 종목마다 질의를 던지지 않고 **창 하나를 한 번에** 읽는다.

    창을 정규장으로 닫는 이유: 전방 창과 직전 60 초 창이 **장 마감을 넘어 시간외로
    새지 않게** 하기 위해서다. 그 대가로 마감 직전 사건은 전방 막대가 적어지는데,
    그것은 `n_bars` 로 드러난다 - **0 이면 모르는 것**이지 "안 올랐다" 가 아니다.
    """
    q = ("SELECT symbol, ts_ms, COUNT(*), MIN(price_u), MAX(price_u), "
         "       SUM(price_u * qty_u), SUM(qty_u) "
         "FROM trades_snap WHERE ts_ms >= ? AND ts_ms < ? "
         "GROUP BY symbol, ts_ms ORDER BY symbol, ts_ms")
    df = pd.read_sql_query(q, conn, params=(int(lo_ms), int(hi_ms)))
    if df.empty:
        return {}
    df.columns = ["symbol", "ts_ms", "n", "lo", "hi", "notional", "qty"]
    out = {}
    for sym, sub in df.groupby("symbol", sort=True):
        ts = sub.ts_ms.to_numpy(dtype="int64")
        if ts.size < 2:
            continue
        n = sub.n.to_numpy(dtype="int64")
        lo = sub.lo.to_numpy(dtype="float64")
        hi = sub.hi.to_numpy(dtype="float64")
        qty = sub.qty.to_numpy(dtype="float64")
        notional = sub.notional.to_numpy(dtype="float64")
        vwap = np.where(qty > 0, notional / np.maximum(qty, 1.0), (lo + hi) / 2.0)
        out[str(sym)] = (ts, n, lo, hi, vwap)
    return out


def session_events(conn: sqlite3.Connection, rtype: str, duration: str,
                   day0_ms: int, floor_ms: int, until_ms: int) -> pd.DataFrame:
    """한 (랭킹 타입, UTC 세션)의 사건 표. **`hires_events` 의 정의를 그대로 부른다.**

    사건 탐지는 **UTC 하루 전체**에서 하고(직전 스냅이 정규장 개장 전일 수 있다),
    그 뒤 `t0` 가 정규장 안인 것만 남긴다. 여기서 창을 좁혀 탐지하면 개장 첫 스냅이
    통째로 사건이 되어 버린다 - 그건 시장 사실이 아니라 우리 창의 경계다.
    """
    lo = max(day0_ms, floor_ms)
    hi = min(day0_ms + HE.DAY_MS, until_ms + 1)
    if hi <= lo:
        return pd.DataFrame()
    df = HE.load_rankings(conn, rtype, duration, lo, hi)
    if df.empty:
        return pd.DataFrame()
    ev = HE.chunk_events(HE.rank_matrix(df), rtype)
    if ev.empty:
        return ev
    ev = SS.drop_holdout(ev, ts_col="t0_ms")["kept"]
    if ev.empty:
        return ev
    open_ms = day0_ms + HE.REGULAR_OPEN_S * SEC_MS
    close_ms = day0_ms + HE.REGULAR_CLOSE_S * SEC_MS
    t0 = ev.t0_ms.to_numpy(dtype="int64")
    return ev[(t0 >= open_ms) & (t0 < close_ms)].copy()


# --------------------------------------------------------------------------- #
# 앵커
# --------------------------------------------------------------------------- #
def anchor_bars(ts: np.ndarray, t0_ms: np.ndarray, *,
                max_wait_s: int = ANCHOR_MAX_WAIT_S) -> tuple[np.ndarray, np.ndarray]:
    """`t0` **뒤** 처음 오는 막대의 인덱스와 그때까지 걸린 초.

    `side='right'` 인 이유: `t0` 와 **같은 초**의 막대에는 `t0` 이전 체결이 섞여 있다.
    `docs/59` 의 커버리지 창도 `(t0, t0+W]` 라 같은 경계다.

    창 안에 막대가 없으면 인덱스 -1 · 지연 `nan`. **길이를 줄이지 않는다** -
    줄이면 사건 쪽 표식과 짝이 어긋난다(§4.4-D 의 "짝 161 vs 정합 160" 사고).
    """
    t0 = np.asarray(t0_ms, dtype="int64")
    idx = np.searchsorted(np.asarray(ts, dtype="int64"), t0, side="right")
    lag = np.full(t0.shape, np.nan)
    inside = idx < np.asarray(ts).size
    if inside.any():
        lag[inside] = (np.asarray(ts)[idx[inside]] - t0[inside]) / 1000.0
    ok = inside & (np.nan_to_num(lag, nan=np.inf) <= float(max_wait_s))
    lag[~ok] = np.nan
    return np.where(ok, idx, -1).astype("int64"), lag


# --------------------------------------------------------------------------- #
# 한 팔의 **원값** — 요약은 나중에 한 번만 한다
# --------------------------------------------------------------------------- #
def overlapping_windows(anchor_ms: np.ndarray, sym_id: np.ndarray, *,
                        horizon_s: int = HEADLINE_H) -> int:
    """전방 창이 **같은 종목의 다른 사건**과 겹치는 앵커 수.

    한 종목이 상위권을 들락거리면 사건이 촘촘히 난다. 그 창들이 겹치면 사건들이
    **같은 가격 움직임을 여러 번 세는 것**이 되어 표본 수가 실제보다 커 보인다.
    부트스트랩을 거래일로 묶는 것이 날 사이 상관은 잡지만 날 **안**의 겹침은 못
    잡는다. 그래서 겹친 수를 따로 세어 낸다 - 고치지 않고 **적는다**.
    """
    t = np.asarray(anchor_ms, dtype="int64")
    s = np.asarray(sym_id, dtype="int64")
    n = 0
    for u in np.unique(s):
        v = np.sort(t[s == u])
        if v.size < 2:
            continue
        d = np.diff(v) <= horizon_s * 1000
        n += int((np.r_[d, False] | np.r_[False, d]).sum())
    return n


def raw_metrics(uni: dict, syms: list, sym_id: np.ndarray, bar: np.ndarray, *,
                open_ms: int) -> dict:
    """한 팔의 전방 경로를 **원값 배열로** 돌려준다.

    세션별로 요약해서 나중에 가중평균하지 않는 이유: 그렇게 하면 중앙값과 백분위가
    **가중평균이지 백분위가 아니게** 된다. 원값을 이어 붙여 **한 번만** 요약한다.

    실제와 위약이 `arm_entry_outcomes` · `arm_forward_probe` 라는 **같은 함수**를
    통과한다. 여기서 다시 구현하면 차이가 기계에서 나온다(`docs/44` §11 의 규율).
    """
    m = int(np.asarray(sym_id).size)
    out = {k: np.full(m, np.nan) for k in REPORTED_METRICS}
    out["_symbol"] = np.asarray([syms[int(i)] for i in sym_id], dtype=object)
    if m == 0:
        return out
    ent = arm_entry_outcomes(uni, syms, sym_id, bar, lag_s=0.0)
    for k in ("mfe_60s", "mae_60s"):
        out[k] = np.asarray(ent[k], dtype="float64")
    probe = arm_forward_probe(uni, syms, sym_id, bar, horizons=PROBE_HORIZONS_S)
    for h in PROBE_HORIZONS_S:
        out[f"max_ret_{h}s"] = np.asarray(probe[h]["max_ret"], dtype="float64")
        out[f"end_ret_{h}s"] = np.asarray(probe[h]["end_ret"], dtype="float64")
        out[f"t_max_{h}s"] = np.asarray(probe[h]["t_max_s"], dtype="float64")
        out[f"n_bars_{h}s"] = np.asarray(probe[h]["n_bars"], dtype="float64")
    for si in np.unique(sym_id):
        sel = np.flatnonzero(sym_id == si)
        u = uni[syms[int(si)]]
        for k in BALANCE_KEYS:
            out[k][sel] = np.asarray(u[k], dtype="float64")[bar[sel]]
        out["t_in_session_s"][sel] = (
            np.asarray(u["ts"], dtype="int64")[bar[sel]] - int(open_ms)) / 1000.0
    return out


def summarize(raw: dict) -> dict:
    """원값 배열을 **한 번에** 요약. 중앙값만 읽지 않고 평균·양(+) 비율을 같이 낸다."""
    n = int(raw["_symbol"].size) if "_symbol" in raw else 0
    out = {"n": n}
    for k in REPORTED_METRICS:
        a = np.asarray(raw.get(k, np.zeros(0)), dtype="float64")
        f = a[np.isfinite(a)]
        t = pct_table(f)
        t["n_finite"] = int(f.size)
        if k in RETURN_METRICS and f.size:
            t["share_positive"] = float((f > 0).mean())
        out[k] = t
    for h in PROBE_HORIZONS_S:
        a = np.asarray(raw.get(f"n_bars_{h}s", np.zeros(0)), dtype="float64")
        out[f"no_forward_bar_share_{h}s"] = (
            float((a == 0).mean()) if a.size else None)
    return out


def two_scales(values: np.ndarray, symbols: np.ndarray) -> dict:
    """**눈금 둘.** 사건 가중(정본)과 종목당 균등.

    `docs/44` §14-6 에서 눈금에 따라 잔차의 부호가 뒤집혔다. 한 눈금만 내면 그것을
    못 본다 - 그래서 암묵으로 두지 않고 표에 적는다.
    """
    v = np.asarray(values, dtype="float64")
    s = np.asarray(symbols, dtype=object)
    ok = np.isfinite(v)
    if not ok.any():
        return {"event_weighted": None, "symbol_uniform": None,
                "n_events": 0, "n_symbols": 0}
    v, s = v[ok], s[ok]
    per = [float(v[s == u].mean()) for u in np.unique(s)]
    return {"event_weighted": float(v.mean()),
            "symbol_uniform": float(np.mean(per)),
            "n_events": int(v.size), "n_symbols": int(len(per))}


# --------------------------------------------------------------------------- #
# 군집 부트스트랩
# --------------------------------------------------------------------------- #
def cluster_bootstrap_diff(real: dict, plac: dict, *, n_boot: int = BOOTSTRAP_N,
                           seed: int = BOOTSTRAP_SEED,
                           min_clusters: int = MIN_SESSION_CLUSTERS,
                           n_comparisons: int = 1) -> dict:
    """**세션을 군집으로** 재추출한 (실제 평균 - 위약 평균)의 CI. 95% 와 **본페로니**.

    `real` · `plac` 은 `{세션: 값 배열}` 이다. 재추출 단위가 사건이 아니라 **거래일**
    이다 - 같은 날의 사건은 같은 장세를 공유한다.

    **군집이 `min_clusters` 미만이면 CI 를 내지 않는다.** 사유를 문자열로 돌려준다.

    `n_comparisons` 는 **실제로 들여다본 칸 수**다. `STRATEGY-VERDICTS` §4.4-F 가
    정확히 이 자리에서 뒤집혔다 - 보정 분모를 5 에서 **20**(실제로 본 칸 수)으로
    바로잡자 *"유의하게 음수"* 였던 CI 가 0 을 교차했다. 그래서 여기서는 러너가
    **자기가 만든 칸 수를 세어** 넣고, 보정 전후를 **둘 다** 낸다.
    """
    keys = [k for k in sorted(set(real) | set(plac))
            if np.isfinite(np.asarray(real.get(k, np.zeros(0)), "float64")).any()
            and np.isfinite(np.asarray(plac.get(k, np.zeros(0)), "float64")).any()]
    rs = [np.asarray(real[k], "float64")[np.isfinite(np.asarray(real[k], "float64"))]
          for k in keys]
    ps = [np.asarray(plac[k], "float64")[np.isfinite(np.asarray(plac[k], "float64"))]
          for k in keys]
    rv = np.concatenate(rs) if rs else np.zeros(0)
    pv = np.concatenate(ps) if ps else np.zeros(0)
    out = {"n_clusters": len(keys), "clusters": keys,
           "n_real": int(rv.size), "n_placebo": int(pv.size),
           "mean_real": float(rv.mean()) if rv.size else None,
           "mean_placebo": float(pv.mean()) if pv.size else None,
           "mean_diff": (float(rv.mean() - pv.mean())
                         if rv.size and pv.size else None),
           "ci95": None, "crosses_zero": None,
           "n_comparisons": int(n_comparisons),
           "ci_bonferroni": None, "crosses_zero_bonferroni": None,
           "ci_withheld": None}
    if len(keys) < min_clusters:
        out["ci_withheld"] = (
            f"trading-day clusters {len(keys)} < {min_clusters}: no pooled CI is "
            f"produced (STRATEGY-VERDICTS 4.4-B). The runner enforces this, not "
            f"discipline.")
        return out
    rng = np.random.default_rng(seed)
    pick = rng.integers(0, len(keys), size=(n_boot, len(keys)))
    r_sum = np.array([a.sum() for a in rs]); r_n = np.array([a.size for a in rs])
    p_sum = np.array([a.sum() for a in ps]); p_n = np.array([a.size for a in ps])
    rm = r_sum[pick].sum(axis=1) / np.maximum(r_n[pick].sum(axis=1), 1)
    pm = p_sum[pick].sum(axis=1) / np.maximum(p_n[pick].sum(axis=1), 1)
    d = rm - pm
    lo, hi = np.quantile(d, [0.025, 0.975])
    out["ci95"] = [float(lo), float(hi)]
    out["crosses_zero"] = bool(lo <= 0.0 <= hi)
    a = 0.05 / max(1, int(n_comparisons))
    blo, bhi = np.quantile(d, [a / 2, 1.0 - a / 2])
    out["ci_bonferroni"] = [float(blo), float(bhi)]
    out["crosses_zero_bonferroni"] = bool(blo <= 0.0 <= bhi)
    return out


# --------------------------------------------------------------------------- #
# 짝 프로파일 (D-20 의 자리)
# --------------------------------------------------------------------------- #
def pairing_profile(raw: dict, lag_s: np.ndarray, paired: np.ndarray) -> dict:
    """**남은 것과 잃은 것을 같은 자로** 프로파일한다 (`docs/44` §14-4 의 표).

    세는 것만으로는 부족하다 - 잃은 것이 한쪽으로 치우쳐 있으면 남은 표본 자체가
    옮겨간다. 그래서 `nbar60` · 앵커 지연 · 전방 막대 0 비율을 양쪽에서 낸다.
    """
    paired = np.asarray(paired, dtype=bool)

    def one(mask: np.ndarray) -> dict:
        m = int(mask.sum())
        if m == 0:
            return {"n": 0}
        nb = np.asarray(raw["nbar60"], "float64")[mask]
        rv = np.asarray(raw["rv60"], "float64")[mask]
        nbar = np.asarray(raw[f"n_bars_{HEADLINE_H}s"], "float64")[mask]
        lg = np.asarray(lag_s, "float64")[mask]
        return {"n": m,
                "nbar60_p50": float(np.nanmedian(nb)),
                "rv60_missing_share": float((~np.isfinite(rv)).mean()),
                "anchor_lag_s_p50": float(np.nanmedian(lg)),
                "forward_no_bar_share": float((nbar == 0).mean())}

    return {"kept": one(paired), "lost": one(~paired)}


def merge_pairing(items: list) -> dict:
    """세션별 추첨 결과를 **합계로만** 접는다. 조용히 줄이지 않는다."""
    keys = ("n_fires", "n_paired", "n_unpaired", "unpaired_key_missing",
            "unpaired_empty_band", "unpaired_gap_excluded_only")
    out = {k: int(sum(int(d[k]) for d in items)) for k in keys}
    out["share_unpaired"] = (out["n_unpaired"] / out["n_fires"]
                             if out["n_fires"] else None)
    return out


def merge_profile(items: list) -> dict:
    """짝 프로파일을 **표본 수로 가중해** 하나로. `n` 은 합, 나머지는 가중평균이다."""
    out = {}
    for side in ("kept", "lost"):
        rows = [d[side] for d in items if d[side].get("n")]
        n = sum(r["n"] for r in rows)
        if not n:
            out[side] = {"n": 0}
            continue
        out[side] = {"n": int(n)}
        for k in ("nbar60_p50", "rv60_missing_share", "anchor_lag_s_p50",
                  "forward_no_bar_share"):
            out[side][k] = float(sum(r[k] * r["n"] for r in rows) / n)
    return out


# --------------------------------------------------------------------------- #
# 러너
# --------------------------------------------------------------------------- #
def era_of(t_ms: int) -> str:
    """D-21 시대 이름. 경계는 `hires_events.D21_BOUNDARY_UTC` **하나만** 쓴다."""
    return "d21_after" if int(t_ms) >= HE.iso_ms(HE.D21_BOUNDARY_UTC) else "d21_before"


def scan_sessions(conn: sqlite3.Connection, floor_ms: int, until_ms: int) -> list:
    """정규장 스냅이 있는 UTC 세션 목록. 시대를 **세션마다** 붙인다."""
    grid = HE.load_snap_grid(conn, floor_ms, until_ms)
    out = []
    for d in np.unique((grid // HE.DAY_MS) * HE.DAY_MS) if grid.size else []:
        d = int(d)
        o = d + HE.REGULAR_OPEN_S * SEC_MS
        c = d + HE.REGULAR_CLOSE_S * SEC_MS
        n = int(((grid >= o) & (grid < c)).sum())
        if n:
            out.append({"session": HE.ms_iso(d)[:10], "day0_ms": d, "open_ms": o,
                        "close_ms": c, "regular_snaps": n, "era": era_of(o)})
    return out


def _blank_cell() -> dict:
    return {"n_events_regular": 0, "n_symbol_has_tape": 0, "n_anchored": 0,
            "n_window_overlap": 0, "lag": [], "real": {}, "arms": {}}


def _blank_arm() -> dict:
    return {"raw": {}, "real_paired": {}, "pairing": [], "profile": []}


def run(db: Path, *, since_ms: int | None = None, until_ms: int | None = None,
        progress: bool = False) -> dict:
    """세션마다 사건을 만들고 앵커를 붙이고 위약 사다리를 태운다."""
    conn = HE.open_ro(db)
    try:
        floor_ms = HE.holdout_floor_ms()
        if since_ms is not None:
            floor_ms = max(floor_ms, int(since_ms))
        db_max = int(conn.execute("SELECT MAX(snap_ms) FROM rankings_snap").fetchone()[0])
        until = int(until_ms) if until_ms is not None else db_max
        sessions = scan_sessions(conn, floor_ms, until)
        cells: dict = {}

        for s in sessions:
            bars = session_second_bars(conn, s["open_ms"], s["close_ms"])
            if not bars:
                continue
            uni = add_trade_count(build_universe(bars, lookback_s=VOL_LOOKBACK_S), bars)
            syms = sorted(uni)
            code = {sym: i for i, sym in enumerate(syms)}
            index = stratified_index(uni, syms)
            for rtype, duration in RANKING_SPECS:
                ev = session_events(conn, rtype, duration, s["day0_ms"],
                                    floor_ms, until)
                if ev.empty:
                    continue
                kind_col = ev.kind.to_numpy(dtype=object)
                cell_col = ev.cell.to_numpy(dtype=object)
                for kind, cell in ALL_CELLS:
                    key = f"{rtype}|{kind}|{cell}"
                    box = cells.setdefault(key, _blank_cell())
                    m = (kind_col == kind) & (cell_col == cell)
                    box["n_events_regular"] += int(m.sum())
                    if not m.any():
                        continue
                    sub = ev[m]
                    sid = sub.symbol.map(code).to_numpy(dtype="float64")
                    has = np.isfinite(sid)
                    box["n_symbol_has_tape"] += int(has.sum())
                    if not has.any():
                        continue
                    sid = sid[has].astype("int64")
                    t0 = sub.t0_ms.to_numpy(dtype="int64")[has]
                    bar = np.full(sid.size, -1, dtype="int64")
                    lag = np.full(sid.size, np.nan)
                    for u in np.unique(sid):
                        sel = np.flatnonzero(sid == u)
                        bar[sel], lag[sel] = anchor_bars(uni[syms[int(u)]]["ts"], t0[sel])
                    ok = bar >= 0
                    box["n_anchored"] += int(ok.sum())
                    if not ok.any():
                        continue
                    sid, bar, lag = sid[ok], bar[ok], lag[ok]
                    box["lag"].append(lag)
                    box["n_window_overlap"] += overlapping_windows(
                        np.asarray([uni[syms[int(i)]]["ts"][b]
                                    for i, b in zip(sid, bar)], dtype="int64"), sid)
                    real = raw_metrics(uni, syms, sid, bar, open_ms=s["open_ms"])
                    box["real"][s["session"]] = real
                    if progress:
                        print(f"  {s['session']} {s['era']:<10} {rtype:<32}"
                              f"{kind:<15}{cell:<9} ev={int(m.sum()):>6,}"
                              f" anchored={int(ok.sum()):>5,}", flush=True)
                    if (kind, cell) not in PRIMARY_CELLS:
                        continue
                    for arm, mb, ms in PLACEBO_ARMS:
                        # 팔마다 **새 씨앗**을 준다 - 두 팔의 차이가 밴드 하나여야
                        # 하는데, 난수 스트림을 이어 쓰면 추첨 자체가 달라진다.
                        d = draw_stratified(uni, syms, index, sid, bar,
                                            match_band=mb, match_strat=ms,
                                            draws=MATCH_DRAWS, gap_s=SELF_GAP_S,
                                            rng=np.random.default_rng(SEED))
                        a = box["arms"].setdefault(arm, _blank_arm())
                        a["pairing"].append(d)
                        a["profile"].append(pairing_profile(real, lag, d["paired"]))
                        a["raw"][s["session"]] = raw_metrics(
                            uni, syms, d["sym"], d["bar"], open_ms=s["open_ms"])
                        sel = np.flatnonzero(d["paired"])
                        a["real_paired"][s["session"]] = raw_metrics(
                            uni, syms, sid[sel], bar[sel], open_ms=s["open_ms"])
        return {"db": str(db), "since_ms": int(floor_ms),
                "since_utc": HE.ms_iso(floor_ms), "until_ms": until,
                "until_utc": HE.ms_iso(until), "db_max_snap_utc": HE.ms_iso(db_max),
                "sessions": sessions, "cells": cells}
    finally:
        conn.close()


def _cat(by_session: dict, metric: str) -> np.ndarray:
    if not by_session:
        return np.zeros(0, dtype="float64")
    return np.concatenate([np.asarray(by_session[k][metric], "float64")
                           for k in sorted(by_session)])


def _cat_raw(by_session: dict) -> dict:
    if not by_session:
        return {"_symbol": np.zeros(0, dtype=object)}
    keys = sorted(by_session)
    out = {"_symbol": np.concatenate([by_session[k]["_symbol"] for k in keys])}
    for m in REPORTED_METRICS:
        out[m] = np.concatenate([np.asarray(by_session[k][m], "float64") for k in keys])
    return out


def _per_session(by_session: dict, metric: str) -> dict:
    return {k: np.asarray(v[metric], "float64") for k, v in by_session.items()}


def cell_records(res: dict) -> list:
    """`(랭킹타입, 사건, 칸)` 하나마다 문서가 싣는 레코드."""
    sess_era = {s["session"]: s["era"] for s in res["sessions"]}
    # **먼저 칸 수를 센다.** 보정 분모는 "실제로 들여다본 칸 수" 여야 한다
    # (`STRATEGY-VERDICTS` §4.4-F). 러너가 세므로 사람이 5 라고 적을 수 없다.
    n_comp = sum(1 for box in res["cells"].values()
                 for arm, _b, _s in PLACEBO_ARMS if arm in box["arms"])
    recs = []
    for key, box in sorted(res["cells"].items()):
        rtype, kind, cell = key.split("|")
        lag = np.concatenate(box["lag"]) if box["lag"] else np.zeros(0)
        real_raw = _cat_raw(box["real"])
        rec = {"ranking_type": rtype, "kind": kind, "cell": cell,
               "primary": [kind, cell] in [list(c) for c in PRIMARY_CELLS],
               "funnel": {
                   "n_events_regular": box["n_events_regular"],
                   "n_symbol_has_tape": box["n_symbol_has_tape"],
                   "n_anchored": box["n_anchored"],
                   "share_anchored": (box["n_anchored"] / box["n_events_regular"]
                                      if box["n_events_regular"] else None),
                   "n_window_overlap": box["n_window_overlap"],
                   "share_window_overlap": (box["n_window_overlap"] / box["n_anchored"]
                                            if box["n_anchored"] else None),
                   "anchor_lag_s": pct_table(lag[np.isfinite(lag)])},
               "arms": {}, "diff": {}, "scales": {}, "sparse_tape": {},
               "by_era": {}}
        if box["real"]:
            rec["arms"]["real_all"] = summarize(real_raw)
            rec["scales"]["real_all"] = two_scales(real_raw[HEADLINE],
                                                   real_raw["_symbol"])
        for era in sorted({sess_era.get(k, "unknown") for k in box["real"]}):
            sub = {k: v for k, v in box["real"].items()
                   if sess_era.get(k, "unknown") == era}
            v = _cat(sub, HEADLINE)
            v = v[np.isfinite(v)]
            rec["by_era"][era] = {
                "n_sessions": len(sub), "n": int(v.size),
                "mean": float(v.mean()) if v.size else None,
                "p50": float(np.median(v)) if v.size else None,
                "share_positive": float((v > 0).mean()) if v.size else None}
        for arm, _mb, _ms in PLACEBO_ARMS:
            a = box["arms"].get(arm)
            if not a:
                continue
            rec["arms"][arm] = summarize(_cat_raw(a["raw"]))
            rec["arms"][arm]["pairing"] = merge_pairing(a["pairing"])
            rec["arms"][arm + "__real_on_paired"] = summarize(
                _cat_raw(a["real_paired"]))
            rec["sparse_tape"][arm] = merge_profile(a["profile"])
            rec["diff"][arm] = cluster_bootstrap_diff(
                _per_session(a["real_paired"], HEADLINE),
                _per_session(a["raw"], HEADLINE), n_comparisons=n_comp)
            praw = _cat_raw(a["raw"])
            rec["scales"][arm] = two_scales(praw[HEADLINE], praw["_symbol"])
        recs.append(rec)
    return recs


def balance_records(res: dict) -> list:
    """**정합이 실제로 물렸는가** - 주장이 아니라 표로 (`docs/44` §14-5).

    `ntrade60` 은 감시 항목이다: 4 초 칸당 50 건 상한에 검열돼 있어(`docs/41` §4)
    정합 키로 쓰지 않았다. 막대 수를 맞췄을 때 이 값이 **따라 맞는지**만 본다.
    """
    recs = []
    for key, box in sorted(res["cells"].items()):
        rtype, kind, cell = key.split("|")
        if (kind, cell) not in PRIMARY_CELLS or not box["real"]:
            continue
        row = {"ranking_type": rtype, "kind": kind, "cell": cell, "arms": {}}
        # **짝 지은 모집단 위에서 비교한다.** `real_all` 을 위약과 나란히 놓으면
        # 정합이 아니라 표본 교체를 보게 된다(`docs/44` §14-4 의 "가로로만 읽어라").
        pairs = [("real_all", _cat_raw(box["real"]))]
        for arm, _b, _s in PLACEBO_ARMS:
            a = box["arms"].get(arm)
            if not a:
                continue
            pairs.append((arm + "__real_on_paired", _cat_raw(a["real_paired"])))
            pairs.append((arm, _cat_raw(a["raw"])))
        for name, raw in pairs:
            row["arms"][name] = {
                "n": int(raw["_symbol"].size),
                **{k: pct_table(np.asarray(raw[k], "float64")[
                    np.isfinite(np.asarray(raw[k], "float64"))])
                   for k in tuple(BALANCE_KEYS) + POSITION_KEYS}}
        recs.append(row)
    return recs


def build_report(res: dict) -> dict:
    """산출물 하나. **문서에 실리는 수치는 전부 여기를 지나간다.**"""
    return {
        "labels": list(LABELS),
        "conditions": HE.MEASUREMENT_CONDITIONS,
        "db": res["db"],
        "window": {"since_utc": res["since_utc"], "until_utc": res["until_utc"],
                   "db_max_snap_utc": res["db_max_snap_utc"],
                   "d21_boundary_utc": HE.D21_BOUNDARY_UTC,
                   "eras_in_window": sorted({s["era"] for s in res["sessions"]})},
        "holdout": {"window": [SS.HOLDOUT_START, SS.HOLDOUT_END]},
        "design": {
            "event_grid": "hires_events.chunk_events (docs/59) - price never enters "
                          "the event definition",
            "top_gainers_caveat": (
                "TOP_GAINERS is a price-ranked list on the SERVER side: entering it "
                "means a price threshold was already crossed upstream of us. That is "
                "the same shape as the design-A death mode ('detection consumes the "
                "rise'), arriving from outside our code. "
                "TOSS_SECURITIES_TRADING_VOLUME is volume-ranked and is cleaner on "
                "that axis. Both are reported; neither is chosen."),
            "anchor": ("first second-bar strictly after t0, within "
                       f"{ANCHOR_MAX_WAIT_S}s; a bar in t0's own second is NOT used"),
            "horizons_s": list(PROBE_HORIZONS_S),
            "headline": HEADLINE,
            "metrics": list(REPORTED_METRICS),
            "placebo_arms": [a for a, _b, _s in PLACEBO_ARMS],
            "placebo_axis": "same symbol, same regular session, different moment",
            "match_keys": {"vol": "rv60", "density": "nbar60",
                           "watched_not_matched": "ntrade60 (censored at 50/4s bucket, "
                                                  "docs/41 s4)",
                           "tol": VOL_MATCH_TOL, "lookback_s": VOL_LOOKBACK_S},
            "draws_per_event": MATCH_DRAWS, "self_gap_s": SELF_GAP_S, "seed": SEED,
            "bootstrap": {"n": BOOTSTRAP_N, "seed": BOOTSTRAP_SEED,
                          "cluster": "trading day (UTC session)",
                          "min_clusters": MIN_SESSION_CLUSTERS,
                          "multiplicity": (
                              "both a plain 95% CI and a Bonferroni CI are printed. "
                              "The Bonferroni denominator is the number of (cell, arm) "
                              "differences this run actually produced - the runner "
                              "counts it, nobody types it. STRATEGY-VERDICTS 4.4-F is "
                              "the case where fixing that denominator (5 -> 20) turned "
                              "a 'significantly negative' CI into one that crosses "
                              "zero.")},
            "costs": "NOT subtracted - that is G-3 and needs a preregistration",
            "max_ret_is_mechanically_positive": (
                "max_ret is a MAXIMUM over the forward window, so its expectation is "
                "above zero at ANY anchor, including a random one. The real arm's "
                "max_ret alone therefore says nothing. Only the difference against a "
                "matched placebo anchored in the same symbol and session carries "
                "information, and that difference is table [4]."),
            "session_window": "regular only, 13:30-20:00 UTC; bars are cut at the "
                              "close so no window leaks into after-hours",
        },
        "sessions": res["sessions"],
        "cells": cell_records(res),
        "balance": balance_records(res),
    }


# --------------------------------------------------------------------------- #
# 콘솔 - **ASCII 만.** cp949 에서 비 ASCII 는 UnicodeEncodeError 로 죽는다.
# --------------------------------------------------------------------------- #
def _f(v, nd=5):
    return "-" if v is None or not np.isfinite(float(v)) else f"{float(v):+.{nd}f}"


def _p(v):
    return "-" if v is None or not np.isfinite(float(v)) else f"{100.0 * float(v):5.1f}%"


def _wrap(s: str, width: int) -> list:
    out, line = [], ""
    for word in str(s).split():
        if len(line) + len(word) + 1 > width:
            out.append(line); line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


def print_report(rep: dict) -> None:
    """문서에 그대로 붙일 실행 출력. **요약표가 아니라 러너가 만든 수치다.**"""
    d, w, c = rep["design"], rep["window"], rep["conditions"]
    print("=" * 78)
    print("ranking events -> FORWARD PRICE PATH   (docs/58 G-2 exploration)")
    print("=" * 78)
    for i, s in enumerate(rep["labels"], 1):
        for j, line in enumerate(_wrap(s, 70)):
            print(f"  [{i}] {line}" if j == 0 else f"      {line}")
    print("-- measurement conditions (attach to EVERY number below) --")
    print(f"  poll period {c['poll_period_s']}s vs server grid "
          f"{c['server_recompute_grid_s']}s -> "
          f"{100 * c['unreceived_grid_tick_share']:.0f}% of grid ticks never received")
    print(f"  ranking age at receipt: median {c['received_ranking_age_median_s']}s")
    print(f"  5s poll deployed: {c['poll_5s_deployed']}   source: {c['source']}")
    print(f"  window {w['since_utc']} .. {w['until_utc']}   "
          f"eras {','.join(w['eras_in_window'])}  "
          f"(D-21 boundary {w['d21_boundary_utc']})")
    print(f"  anchor  : {d['anchor']}")
    print(f"  headline: {d['headline']}   horizons {d['horizons_s']}s")
    print(f"  costs   : {d['costs']}")
    print(f"  placebo : {d['placebo_axis']}; {d['draws_per_event']} draws/event, "
          f"self-gap {d['self_gap_s']}s, seed {d['seed']}")
    print(f"  session : {d['session_window']}")
    print("  ! TOP_GAINERS caveat:")
    for line in _wrap(d["top_gainers_caveat"], 70):
        print(f"      {line}")

    print("\n[1] sessions in window")
    print(f"{'session':<12}{'era':<12}{'reg_snaps':>10}")
    for s in rep["sessions"]:
        print(f"{s['session']:<12}{s['era']:<12}{s['regular_snaps']:>10}")

    print("\n[2] funnel - how many ranking events can be measured at all")
    print("    events(regular) -> symbol has tape inside that regular session ->")
    print("    an anchor bar exists in (t0, t0+300s].  'anchored' is the same set")
    print("    docs/59 calls MEASURABLE (docs/59 counts tape over the whole UTC day,")
    print("    this counts it inside the regular window, so this is the tighter cut).")
    print("    overlap% = anchored events whose 300s window overlaps ANOTHER event")
    print("    of the same symbol: those events re-count one price move, so the")
    print("    effective sample is smaller than the count. Reported, not fixed.")
    print(f"{'ranking_type':<32}{'kind':<15}{'cell':<8}{'events':>9}{'has_tape':>10}"
          f"{'anchored':>10}{'share':>8}{'overlap':>9}{'lag_p50':>10}")
    for r in rep["cells"]:
        f = r["funnel"]
        lg = f["anchor_lag_s"].get("p50")
        print(f"{r['ranking_type']:<32}{r['kind']:<15}{r['cell']:<8}"
              f"{f['n_events_regular']:>9,}{f['n_symbol_has_tape']:>10,}"
              f"{f['n_anchored']:>10,}{_p(f['share_anchored']):>8}"
              f"{_p(f['share_window_overlap']):>9}"
              f"{('-' if lg is None else f'{lg:.1f}s'):>10}")

    print("\n[3] forward path, REAL arm, every anchored event")
    print("    max_ret = highest bar after the anchor / anchor - 1 (MFE).")
    print("    no_bar% = the forward window held no bar at all: WE DO NOT KNOW,")
    print("    which is not the same statement as 'it did not move'.")
    print("    ! READ THIS BEFORE READING THE NUMBERS:")
    for line in _wrap(d["max_ret_is_mechanically_positive"], 70):
        print(f"      {line}")
    print(f"{'ranking_type':<32}{'kind':<15}{'cell':<8}{'h':>5}{'n':>7}"
          f"{'max_ret':>10}{'end_ret':>10}{'end>0':>7}{'no_bar':>8}{'t_max':>8}")
    for r in rep["cells"]:
        a = r["arms"].get("real_all")
        if not a or not a.get("n"):
            continue
        for h in PROBE_HORIZONS_S:
            mr, er = a[f"max_ret_{h}s"], a[f"end_ret_{h}s"]
            tmax = a[f"t_max_{h}s"].get("p50")
            tcol = "-" if tmax is None else f"{float(tmax):.0f}s"
            print(f"{r['ranking_type']:<32}{r['kind']:<15}{r['cell']:<8}{h:>5}"
                  f"{mr.get('n', 0):>7,}{_f(mr.get('mean')):>10}"
                  f"{_f(er.get('mean')):>10}{_p(er.get('share_positive')):>7}"
                  f"{_p(a.get(f'no_forward_bar_share_{h}s')):>8}{tcol:>8}")

    print("\n[4] D-19 - the placebo ladder: same symbol, same session, other moment.")
    print("    Read ACROSS a row, never DOWN a column - each arm keeps a different")
    print("    subset of events (the ones it could pair).  'real' is recomputed on")
    print(f"    that arm's paired subset.  headline = {rep['design']['headline']}")
    print("    Two CIs: plain 95%, then Bonferroni over every (cell, arm) difference")
    print("    this run made.  docs 4.4-F is the case where the correction denominator")
    print("    was the whole finding, so the runner counts the cells itself.")
    print(f"{'ranking_type':<32}{'cell':<8}{'arm':<30}{'n_real':>7}{'n_plac':>7}"
          f"{'real':>10}{'placebo':>10}{'diff':>10}  ci95")
    withheld = set()
    for r in rep["cells"]:
        for arm in rep["design"]["placebo_arms"]:
            x = r["diff"].get(arm)
            if not x:
                continue
            if x["ci95"] is None:
                ci = f"NO CI (clusters {x['n_clusters']})"
                if x["ci_withheld"]:
                    withheld.add(x["ci_withheld"])
            else:
                ci = (f"[{x['ci95'][0]:+.5f},{x['ci95'][1]:+.5f}]"
                      + ("  cross" if x["crosses_zero"] else "  EXCL")
                      + f" | bonf[{x['ci_bonferroni'][0]:+.5f},"
                        f"{x['ci_bonferroni'][1]:+.5f}]"
                      + ("  cross" if x["crosses_zero_bonferroni"] else "  EXCL"))
            print(f"{r['ranking_type']:<32}{r['cell']:<8}{arm:<30}"
                  f"{x['n_real']:>7,}{x['n_placebo']:>7,}{_f(x['mean_real']):>10}"
                  f"{_f(x['mean_placebo']):>10}{_f(x['mean_diff']):>10}  {ci}")
    for msg in sorted(withheld):
        for line in _wrap(msg, 74):
            print(f"    {line}")

    print("\n[5] two scales for the headline (docs/44 s13-14: the sign has flipped")
    print("    between scales before, so neither is left implicit)")
    print(f"{'ranking_type':<32}{'cell':<8}{'arm':<30}{'event_w':>10}{'sym_unif':>10}"
          f"{'n_ev':>8}{'n_sym':>7}")
    for r in rep["cells"]:
        for arm, sc in r["scales"].items():
            if not sc or not sc.get("n_events"):
                continue
            print(f"{r['ranking_type']:<32}{r['cell']:<8}{arm:<30}"
                  f"{_f(sc['event_weighted']):>10}{_f(sc['symbol_uniform']):>10}"
                  f"{sc['n_events']:>8,}{sc['n_symbols']:>7,}")

    print("\n[6] D-20 - the events that could NOT be paired, profiled (docs/44 s14-4).")
    print("    Counting is not enough: if what we lose is lopsided, the surviving")
    print("    sample itself has moved.  The lost side is the sparse-tape side.")
    print(f"{'ranking_type':<32}{'cell':<8}{'arm':<30}{'side':<6}{'n':>7}"
          f"{'nbar60_p50':>11}{'lag_p50':>9}{'no_fwd_bar':>11}")
    for r in rep["cells"]:
        for arm, f in r["sparse_tape"].items():
            for side in ("kept", "lost"):
                b = f.get(side, {})
                if not b.get("n"):
                    continue
                print(f"{r['ranking_type']:<32}{r['cell']:<8}{arm:<30}{side:<6}"
                      f"{b['n']:>7,}{b['nbar60_p50']:>11.1f}"
                      f"{b['anchor_lag_s_p50']:>8.1f}s"
                      f"{_p(b['forward_no_bar_share']):>11}")

    print("\n[7] pairing census - nothing is dropped silently")
    print(f"{'ranking_type':<32}{'cell':<8}{'arm':<30}{'events':>7}{'paired':>8}"
          f"{'unpaired':>9}{'key_miss':>9}{'empty':>7}{'gap_only':>9}")
    for r in rep["cells"]:
        for arm in rep["design"]["placebo_arms"]:
            p = r["arms"].get(arm, {}).get("pairing")
            if not p:
                continue
            print(f"{r['ranking_type']:<32}{r['cell']:<8}{arm:<30}"
                  f"{p['n_fires']:>7,}{p['n_paired']:>8,}{p['n_unpaired']:>9,}"
                  f"{p['unpaired_key_missing']:>9,}{p['unpaired_empty_band']:>7,}"
                  f"{p['unpaired_gap_excluded_only']:>9,}")

    print("\n[8] parameter sweep - does the answer stick to one grid cell?")
    print("    real arm only, headline metric, split by D-21 era.  The two eras are")
    print("    NEVER added together (docs/59 s4 extended to the D-21 boundary).")
    print(f"{'ranking_type':<32}{'kind':<15}{'cell':<8}{'era':<12}{'sess':>5}{'n':>7}"
          f"{'mean':>10}{'p50':>10}{'pos%':>7}")
    for r in rep["cells"]:
        for era, b in r["by_era"].items():
            print(f"{r['ranking_type']:<32}{r['kind']:<15}{r['cell']:<8}{era:<12}"
                  f"{b['n_sessions']:>5}{b['n']:>7,}{_f(b['mean']):>10}"
                  f"{_f(b['p50']):>10}{_p(b['share_positive']):>7}")

    print("\n[9] balance - did the matching actually bite? (docs/44 s14-5)")
    print("    ntrade60 is WATCHED, not matched: it is censored at 50 per 4s bucket.")
    print("    t_in_sess / fwd_bars are NOT match keys either - they are the check on")
    print("    window truncation: if the real arm sits at the open and the placebo is")
    print("    spread over the day, the two arms get forward windows of different")
    print("    depth and any gap is OUR window, not the market.")
    print("    Compare each 'arm' row with the '__real_on_paired' row directly above")
    print("    it - never with 'real_all', which is a different population.")
    for row in rep["balance"]:
        print(f"-- {row['ranking_type']}  {row['kind']} {row['cell']}")
        print(f"   {'arm':<32}{'n':>7}{'rv60_p50':>11}{'nbar60_p50':>12}"
              f"{'ntrade60_p50':>14}{'t_in_sess_p50':>15}{'fwd_bars_p50':>14}")
        for arm, b in row["arms"].items():
            nb = b[f"n_bars_{HEADLINE_H}s"]
            print(f"   {arm:<32}{b['n']:>7,}"
                  f"{b['rv60'].get('p50', float('nan')):>11.5f}"
                  f"{b['nbar60'].get('p50', float('nan')):>12.1f}"
                  f"{b['ntrade60'].get('p50', float('nan')):>14.1f}"
                  f"{b['t_in_session_s'].get('p50', float('nan')):>14.0f}s"
                  f"{nb.get('p50', float('nan')):>14.1f}")

    print("\n" + "=" * 78)
    print("This runner decides nothing.  It reports what was measured, the sample")
    print("behind each number, and what could not be separated.")
    print("=" * 78)


def main(argv: list) -> int:
    db = HE.DB
    since_ms = until_ms = None
    out_dir = OUT_DIR
    name = "ranking_forward_path"
    args = list(argv[1:])
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--until-ms":
            until_ms = int(args[i + 1]); i += 2
        elif a == "--since-ms":
            since_ms = int(args[i + 1]); i += 2
        elif a == "--since-d21":
            since_ms = HE.iso_ms(HE.D21_BOUNDARY_UTC); i += 1
        elif a == "--until-d21":
            until_ms = HE.iso_ms(HE.D21_BOUNDARY_UTC) - 1; i += 1
        elif a == "--out":
            out_dir = Path(args[i + 1]); i += 2
        elif a == "--name":
            name = args[i + 1]; i += 2
        else:
            db = Path(a); i += 1
    print("scanning sessions (UTC session x ranking type x cell) ...", flush=True)
    res = run(db, since_ms=since_ms, until_ms=until_ms, progress=True)
    rep = build_report(res)
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{name}.json"
    p.write_text(json.dumps(rep, indent=1), encoding="utf-8")
    print_report(rep)
    print(f"-> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

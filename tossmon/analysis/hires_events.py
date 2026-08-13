"""**고해상도 랭킹 사건의 자** — 사건을 정의하고 **표본만 센다. 결과는 재지 않는다.**

## 이 모듈이 하는 일과 하지 않는 일

**한다**: 랭킹 시계열만으로 사건을 정의하고(E1·E2·E3), 사건마다 **테이프가 있는지**를
세고, 구성 경계와 종목 층으로 갈라 **표본 수**를 낸다.

**하지 않는다**: **전방 수익·MFE·알파를 계산하지 않는다.** 판정도 하지 않는다.
이 프로젝트는 **표본을 세기 전에 판정해서 세 번 틀렸다**
(`coordination/STRATEGY-VERDICTS.md` §4.4-A·4.4-C·4.4-D). 결과 측정은 이 러너가 낸
표본 수를 보고 격자를 정한 **다음 태스크**의 일이다.

## 왜 사건을 **랭킹만으로** 정의하는가

가격 임계로 사건을 정의하면 설계 A 와 같은 죽음을 반복한다 —
*"슈팅은 이미 임계만큼 올랐음으로 정의되므로 탐지가 상승을 소진한다"*
(`STRATEGY-VERDICTS` §4.4). 임계 1/2/3/5% 를 전부 쓸어도 탐지 후 남은 상승폭 중앙이
**매번 정확히 0.0000** 이었고, 그것은 시장이 아니라 **정의의 성질**이었다.
그래서 여기서는 **가격이 사건 정의에 한 번도 들어가지 않는다.** `last_u` 는 층을
가르는 데만 쓰고(§4-4), 사건 발생 여부에는 관여하지 않는다.

## ★ `t0` 의 정의 — 이 문장을 문서에 그대로 옮긴다

> **`t0` 는 우리가 그 스냅을 *받은* 시각(`snap_ms`)이지 서버가 랭킹을 재계산한
> 시각이 아니다. 둘의 차이가 중앙 16.1 초다** (`docs/35`, W1 실측).

## 모든 표에 붙는 측정 조건 (`docs/35` · `docs/58` §1-3)

폴 주기는 **아직 12.4 초**다. 5 초 배포는 안 됐다. 즉 이 데이터 전체가
**"중앙 16.1 초 늙은 랭킹 · 서버 10 초 격자 틱의 29% 미수신"** 조건에서 쌓였다.
여기서 나오는 어떤 수치도 그 조건 없이 인용하면 안 된다.

## 구성 경계를 넘어 뭉치지 않는다

랭킹 4 종 -> 2 종(`docs/30` §1-1), 08-04 배포 4 회(`docs/30` §1), `TOP_GAINERS` 개시
(`docs/39` 정정 상자 — **제목은 거짓이고 본문만 인용한다**), 송신시각 계상 수정
(`docs/52`). 랭킹 타입은 **절대 뭉치지 않는다** — 08-04 와 08-07 경계에서 표본이
조용히 섞인다.

## 홀드아웃

봉인 구간 **2026-05-01 ~ 07-29** 는 SQL 바닥(`holdout_floor_ms`)으로 **적재 자체를
막고**, 그 뒤에 `session.drop_holdout` 으로 사건 표를 한 번 더 거른다. 버린 행 수는
산출물에 싣는다 — 조용히 거르지 않는다(§4.4-E 가 같은 방식으로 297,506 행을 버렸다).
이 자료는 전부 07-31 이후라 실제로는 0 행이 걸리지만, **가드는 데이터가 아니라
코드가 지켜야 한다.**

실행: `python -m tossmon.analysis.hires_events [db_path] [--until-ms N] [--out DIR]`
-> `out/hires_events.json`. **라이브 API 호출 0. DB 는 `mode=ro`. 콘솔 ASCII.**
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from tossmon.analysis import session as SS

#: 살아 있는 수집 DB. **쓰기로 열지 않는다** — 수집기가 같은 파일에 쓰고 있다.
DB = Path("C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db")

#: 산출물은 소스 옆이 아니라 저장소 루트의 `out/` 에 쓴다(`.gitignore` 대상).
OUT_DIR = Path(__file__).resolve().parents[2] / "out"

SEC_MS = 1_000
MIN_MS = 60_000
DAY_MS = 86_400_000

#: 랭킹 타입 다섯. **뭉치지 않는다** — `(ranking_type, duration)` 이 한 쌍이다.
#: `TOP_GAINERS` 만 `duration='1d'` 이고 나머지는 `realtime` 이다(`docs/39` §2).
RANKING_SPECS = (
    ("MARKET_TRADING_VOLUME", "realtime"),
    ("TOSS_SECURITIES_TRADING_VOLUME", "realtime"),
    ("MARKET_TRADING_AMOUNT", "realtime"),
    ("TOSS_SECURITIES_TRADING_AMOUNT", "realtime"),
    ("TOP_GAINERS", "1d"),
)

#: 사건 격자. **하나로 고정하지 않는다** — 파라미터 하나에 답이 붙는지가 이 프로젝트가
#: 반복해 데인 자리다(`min_rise` 임계에 답이 1:1 로 따라갔다, §4.4-A).
TOP_NS = (10, 20, 50, 100)
JUMP_KS = (5, 10, 20)
#: E2 의 **시작 순위 층**. 층 넷은 "전체" 칸을 **분할**한다(겹치지 않는다).
JUMP_FROM_TIERS = ((1, 10), (11, 20), (21, 50), (51, 100))
#: E3 체류 길이 — **스냅 수**다(초가 아니다). 스냅 하나가 약 12.4 초다.
DWELL_SNAPS = (3, 6)

#: 테이프 커버리지 창(초). t0 **직후** `(t0, t0+W]`.
COVERAGE_S = (60, 300)
#: 사전 활동도 창(초). t0 **직전** `[t0-W, t0)`. 다음 태스크의 정합에 쓰인다.
PRE_S = 60

#: 이보다 앞 스냅이 멀면 "직전 스냅"이 인접이 아니다. 공칭 폴 12.4 초의 약 2.4 배.
#: 사건을 **버리지 않고 표시만** 한다 — 조용히 줄이지 않는다.
ADJACENCY_MAX_MS = 30 * SEC_MS

#: 랭킹 스냅 간격이 이보다 벌어지면 **수집기가 죽어 있던 것**으로 본다(`docs/41` §1-2 와
#: 같은 방식). 커버리지 창이 이 구간에 걸린 사건은 "체결 0" 이 시장이 아니라 우리다.
DOWNTIME_MIN_MS = 60 * SEC_MS

#: 종목 층 — `docs/16` 의 표적은 **미국 소형주·동전주**다. 단위는 마이크로달러
#: (`last_u` 19400 = $0.0194). 경계는 하한 포함·상한 제외.
PRICE_BANDS_U = (
    ("p0_2", 0, 2_000_000),
    ("p2_5", 2_000_000, 5_000_000),
    ("p5_10", 5_000_000, 10_000_000),
    ("p10_up", 10_000_000, None),
)

#: 미국 정규장 = **13:30~20:00 UTC**. 이 창은 UTC 날짜 안에 온전히 들어가므로
#: UTC 일자 묶음이 곧 세션 묶음이다. `SS.sessions_of` 의 `regular` 와 같은 구간이다
#: (KST 22:30~05:00 = UTC 13:30~20:00).
REGULAR_OPEN_S = 13 * 3600 + 1800
REGULAR_CLOSE_S = 20 * 3600
REGULAR_SPAN_S = REGULAR_CLOSE_S - REGULAR_OPEN_S

#: 부호화 키의 자리수. `sym_code * TS_STRIDE + ts_ms` 로 (종목, 시각) 을 하나의 정렬
#: 가능한 int64 로 만든다. `ts_ms` 는 1.79e12 로 2**42 = 4.4e12 아래다.
TS_STRIDE = 1 << 42

#: **모든 표에 붙는 측정 조건.** `docs/35`(W1 2026-08-07 실측) · `docs/58` §1-3.
#: 폴 주기 5 초 배포는 **아직 안 됐다** — 이 데이터 전체가 아래 조건에서 쌓였다.
MEASUREMENT_CONDITIONS = {
    "poll_period_s": 12.4,
    "server_recompute_grid_s": 10.0,
    "unreceived_grid_tick_share": 0.29,
    "received_ranking_age_median_s": 16.1,
    "t0_definition": ("t0 = snap_ms = the moment WE received the snapshot, "
                      "not the moment the server recomputed the ranking; "
                      "the gap between them is a median 16.1s (docs/35)"),
    "source": "docs/35 (W1, 2026-08-07) / docs/58 section 1-3",
    "poll_5s_deployed": False,
}

#: **구성 경계.** 앞뒤로 표본을 따로 센다 — 넘어 뭉치면 표본 구성 변화를 시장 변화로
#: 오독한다(`session.py` 의 `collector_era` 가 같은 이유로 존재한다).
CONFIG_BOUNDARIES = (
    {"key": "rank_4_to_2", "utc": "2026-08-04T02:22:42Z",
     "what": "ranking types 4 -> 2 (both _AMOUNT lists removed)",
     "evidence": "docs/30 section 1-1 (11:22:42 KST) / USER-INPUT-QUEUE D-8"},
    {"key": "deploys_20260804_last", "utc": "2026-08-04T04:39:42Z",
     "what": "last of the four 2026-08-04 deploys (usage_ratio 0.70 -> 0.85)",
     "evidence": "docs/30 section 1 (13:39:42 KST)"},
    {"key": "top_gainers_start", "utc": "2026-08-07T13:26:11Z",
     "what": "TOP_GAINERS (1d) collection begins",
     "evidence": "docs/39 correction box (22:26 KST) - the TITLE of docs/39 is false, "
                 "only the body is cited"},
    {"key": "send_time_accounting", "utc": "2026-08-12T10:11:02Z",
     "what": "accounting moved from completion time to send time; collector restarted",
     "evidence": "docs/52 section 5.5 (19:11:02 KST restart) / "
                 "coordination/daily/2026-08-12.md section 4.16"},
)

#: 백분위 표에 쓰는 눈금.
PCTS = (10, 25, 50, 75, 90, 99)

#: **문서가 싣는 사건 칸 레코드의 필드 전체.** 테스트가 **부분집합이 아니라 동일
#: 집합**으로 대조한다 — 필드를 늘리고 이 목록을 잊으면 깨진다(H-1 재발 방지).
REPORTED_FIELDS = (
    "ranking_type", "kind", "cell",
    "n", "n_regular", "n_measurable_300",
    "n_no_tape_symbol", "n_zero_60", "n_zero_300", "n_zero_pre60",
    "share_no_tape_symbol", "share_zero_60", "share_zero_300",
    "n_stale_prev", "n_downtime_300", "n_tape_gap_300",
    "mean_60", "mean_300", "mean_pre60",
    "p50_60", "p50_300", "p50_pre60",
)


# --------------------------------------------------------------------------- #
# 공통
# --------------------------------------------------------------------------- #
def open_ro(db: Path) -> sqlite3.Connection:
    """**읽기 전용**으로 연다. 수집기가 같은 파일에 쓰고 있으므로 쓰기로 열면 안 된다."""
    return sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True, timeout=180)


def iso_ms(iso: str) -> int:
    """`2026-08-04T02:22:42Z` -> epoch ms. 경계 상수를 매직넘버로 두지 않기 위해서다."""
    return int(pd.Timestamp(iso).timestamp() * 1000)


def ms_iso(ms) -> str:
    """epoch ms -> `2026-08-04T02:22:42Z`. 콘솔·JSON 모두 ASCII 로 남는다."""
    if ms is None or not np.isfinite(float(ms)):
        return "-"
    return pd.Timestamp(int(ms), unit="ms", tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def holdout_floor_ms() -> int:
    """봉인 구간이 **확실히 끝나는** 첫 ms. 값을 박지 않고 `SS.is_holdout` 으로 찾는다.

    `SS.HOLDOUT_END` 는 **세션 사이클 날짜**이고 `SS.is_holdout` 은 봉 라벨 보정
    (`bar_start_ms`, -60 초)까지 씌운다. 그 두 규칙을 여기서 다시 쓰면 언젠가 갈라지므로
    **라이브러리에 물어서** 바닥을 정한다 — 라이브러리가 바뀌면 이 값도 따라 바뀐다.
    """
    ms = int(pd.Timestamp(SS.HOLDOUT_END + "T00:00:00Z").timestamp() * 1000)
    while SS.is_holdout(ms):
        ms += MIN_MS
    return ms


def pct_table(values) -> dict:
    """백분위 표. 표본이 없으면 `n=0` 만 돌려준다 — 빈 배열에 중앙값을 묻지 않는다."""
    a = np.asarray(values, dtype="float64")
    a = a[np.isfinite(a)]
    if a.size == 0:
        return {"n": 0}
    out = {"n": int(a.size), "mean": float(a.mean()), "max": float(a.max())}
    for p in PCTS:
        out[f"p{p:02d}"] = float(np.percentile(a, p))
    return out


def price_band_codes(last_u: np.ndarray) -> np.ndarray:
    """t0 시점 `last_u` 를 `docs/16` 의 표적 층 경계로 가른다. 값이 없으면 `unknown`."""
    out = np.full(last_u.shape, "unknown", dtype=object)
    ok = np.isfinite(last_u)
    for name, lo, hi in PRICE_BANDS_U:
        m = ok & (last_u >= lo)
        if hi is not None:
            m &= last_u < hi
        out[m] = name
    return out


# --------------------------------------------------------------------------- #
# 적재
# --------------------------------------------------------------------------- #
def load_symbol_meta(conn: sqlite3.Connection) -> pd.DataFrame:
    """`symbols` 의 `market` · `security_type`. 없는 종목은 `unknown` 으로 남긴다."""
    return pd.read_sql_query(
        "SELECT symbol, market, security_type FROM symbols", conn).set_index("symbol")


def load_snap_grid(conn: sqlite3.Connection, floor_ms: int, until_ms: int) -> np.ndarray:
    """랭킹 타입을 **전부 합친** 스냅 시각. 수집기 생사는 타입별이 아니라 프로세스 단위다."""
    q = ("SELECT DISTINCT snap_ms FROM rankings_snap "
         "WHERE snap_ms >= ? AND snap_ms <= ? ORDER BY snap_ms")
    return pd.read_sql_query(q, conn, params=(floor_ms, until_ms)).snap_ms.to_numpy()


def downtime_intervals(grid: np.ndarray) -> list[tuple[int, int]]:
    """스냅 간격이 `DOWNTIME_MIN_MS` 를 넘는 구간 = **수집기가 죽어 있던 구간**.

    커버리지 창이 여기 걸리면 "체결 0 건"은 시장이 아니라 **우리 쪽 결손**이다.
    그 둘을 안 가르면 다음 태스크가 우리 정전을 유동성 부재로 읽는다.
    """
    if grid.size < 2:
        return []
    d = np.diff(grid)
    idx = np.nonzero(d > DOWNTIME_MIN_MS)[0]
    return [(int(grid[i]), int(grid[i + 1])) for i in idx]


def load_rankings(conn: sqlite3.Connection, rtype: str, duration: str,
                  lo_ms: int, hi_ms: int) -> pd.DataFrame:
    """한 타입·한 창의 랭킹 원자료. **홀드아웃 바닥은 호출부가 `lo_ms` 로 건다.**"""
    q = ("SELECT snap_ms, rank, symbol, last_u FROM rankings_snap "
         "WHERE ranking_type = ? AND duration = ? AND snap_ms >= ? AND snap_ms < ? "
         "ORDER BY snap_ms, rank")
    return pd.read_sql_query(q, conn, params=(rtype, duration, lo_ms, hi_ms))


def load_trade_keys(conn: sqlite3.Connection, lo_ms: int, hi_ms: int,
                    codes: dict[str, int], *, session_lo: int, session_hi: int
                    ) -> tuple[np.ndarray, set[str]]:
    """체결을 `(종목코드, ts_ms)` 부호화 키로 **정렬해서** 돌려준다.

    창마다 SQL 을 던지면 사건 수만큼 질의가 나간다(수십만 건). 대신 하루치를 한 번
    읽어 `searchsorted` 로 센다 — 답은 같고 시간은 세 자릿수 차이가 난다.

    두 번째 반환값은 **그 세션 안에** 체결이 한 건이라도 수집된 종목의 집합이다.
    적재 창은 커버리지 때문에 세션보다 넓으므로(`+300초`), 그 넓은 창으로 세면
    "세션에 테이프가 없었다" 가 조용히 과소집계된다. 그래서 창을 갈라 센다.
    """
    df = pd.read_sql_query(
        "SELECT symbol, ts_ms FROM trades_snap WHERE ts_ms >= ? AND ts_ms < ?",
        conn, params=(lo_ms, hi_ms))
    if df.empty:
        return np.zeros(0, dtype="int64"), set()
    ts = df.ts_ms.to_numpy(dtype="int64")
    seen = set(df.symbol[(ts >= session_lo) & (ts < session_hi)].unique())
    code = df.symbol.map(codes).to_numpy(dtype="float64")
    ok = np.isfinite(code)
    if not ok.any():
        return np.zeros(0, dtype="int64"), seen
    keys = code[ok].astype("int64") * TS_STRIDE + ts[ok]
    keys.sort()
    return keys, seen


def load_tape_gaps(conn: sqlite3.Connection, lo_ms: int, hi_ms: int,
                   codes: dict[str, int]) -> tuple[np.ndarray, np.ndarray]:
    """`tape_gaps` 를 부호화 (시작키 정렬, 끝키 누적최대) 쌍으로.

    **이 테이블은 2026-08-07 17:41Z 부터만 있다.** 그 앞 세션에는 결손 **기록 자체가
    없다** — 결손이 없었다는 뜻이 아니다. 그 사실은 `tape_gap_census` 가 따로 낸다.
    """
    df = pd.read_sql_query(
        "SELECT symbol, gap_lo_ms, gap_hi_ms FROM tape_gaps "
        "WHERE gap_lo_ms < ? AND gap_hi_ms >= ?", conn, params=(hi_ms, lo_ms))
    if df.empty:
        return np.zeros(0, dtype="int64"), np.zeros(0, dtype="int64")
    code = df.symbol.map(codes).to_numpy(dtype="float64")
    ok = np.isfinite(code)
    if not ok.any():
        return np.zeros(0, dtype="int64"), np.zeros(0, dtype="int64")
    c = code[ok].astype("int64")
    lo = c * TS_STRIDE + df.gap_lo_ms.to_numpy(dtype="int64")[ok]
    hi = c * TS_STRIDE + df.gap_hi_ms.to_numpy(dtype="int64")[ok]
    order = np.argsort(lo)
    return lo[order], np.maximum.accumulate(hi[order])


# --------------------------------------------------------------------------- #
# 순위 행렬
# --------------------------------------------------------------------------- #
def rank_matrix(df: pd.DataFrame) -> dict:
    """(스냅 x 종목) 순위 행렬과 같은 모양의 `last_u` 행렬.

    **같은 스냅에 같은 종목이 두 순위로 들어오면 pivot 이 터진다.** 그리고 조용히
    처리하면 §4.4-D 의 "pandas 중복 인덱스 브로드캐스트" 와 같은 자리가 된다.
    그래서 **좋은 순위 하나만 남기고 버린 행 수를 돌려준다.**
    """
    if df.empty:
        return {"snaps": np.zeros(0, dtype="int64"), "symbols": np.zeros(0, dtype=object),
                "rank": np.zeros((0, 0), dtype="float32"),
                "last_u": np.zeros((0, 0), dtype="float64"), "n_dup_dropped": 0}
    d = df.sort_values(["snap_ms", "rank"], kind="mergesort")
    before = len(d)
    d = d.drop_duplicates(["snap_ms", "symbol"], keep="first")
    piv_r = d.pivot(index="snap_ms", columns="symbol", values="rank")
    piv_p = d.pivot(index="snap_ms", columns="symbol", values="last_u")
    return {
        "snaps": piv_r.index.to_numpy(dtype="int64"),
        "symbols": piv_r.columns.to_numpy(dtype=object),
        "rank": piv_r.to_numpy(dtype="float32"),
        "last_u": piv_p.to_numpy(dtype="float64"),
        "n_dup_dropped": int(before - len(d)),
    }


def forward_run_lengths(present: np.ndarray) -> np.ndarray:
    """`out[i, j]` = `present[i, j]` 부터 **아래로 이어지는** True 의 길이.

    E3 이 요구하는 것은 "여기서부터 M 스냅 이상 머무는가" 이므로 앞이 아니라 뒤를 센다.
    """
    t = present.shape[0]
    out = np.zeros(present.shape, dtype=np.int32)
    if t == 0:
        return out
    out[t - 1] = present[t - 1]
    for i in range(t - 2, -1, -1):
        out[i] = np.where(present[i], out[i + 1] + 1, 0)
    return out


# --------------------------------------------------------------------------- #
# 사건 정의 — **가격은 한 번도 들어가지 않는다**
# --------------------------------------------------------------------------- #
def new_entry_events(rank: np.ndarray, n_top: int) -> tuple[np.ndarray, np.ndarray]:
    """**E1 신규진입** — 직전 스냅에 top-N 에 없던 종목이 들어온 **첫** 스냅.

    첫 행은 직전 스냅이 없으므로 사건이 될 수 없다(청크 경계). 그 사실은
    `chunk_report` 가 센다 — 조용히 버리지 않는다.
    """
    p = np.isfinite(rank) & (rank <= n_top)
    if p.shape[0] < 2:
        return np.zeros(0, dtype="int64"), np.zeros(0, dtype="int64")
    r, c = np.nonzero(p[1:] & ~p[:-1])
    return r + 1, c


def rank_jump_events(rank: np.ndarray, k: int
                     ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """**E2 순위점프** — 연속 스냅 사이 순위가 K 이상 **개선**된 지점.

    양쪽 스냅에 다 있어야 한다(들어오면서 점프한 것은 E1 이지 E2 가 아니다).
    세 번째 반환값은 **시작 순위**이고 층별 집계에 쓴다.
    """
    if rank.shape[0] < 2:
        z = np.zeros(0, dtype="int64")
        return z, z, np.zeros(0, dtype="float32")
    prev, cur = rank[:-1], rank[1:]
    m = np.isfinite(prev) & np.isfinite(cur) & ((prev - cur) >= k)
    r, c = np.nonzero(m)
    return r + 1, c, prev[r, c]


def dwell_start_events(rank: np.ndarray, n_top: int, m_snaps: int
                       ) -> tuple[np.ndarray, np.ndarray, int]:
    """**E3 체류시작** — top-N 에 **연속 M 스냅 이상** 머무르기 시작한 지점.

    시작점은 E1 과 같은 자리(직전에 없다가 들어온 스냅)이고, 거기서부터의 체류 길이가
    M 이상인 것만 남긴다. 따라서 **E3(N, M) 은 E1(N) 의 부분집합**이다.

    세 번째 반환값은 **우절단**(청크 끝에 걸려 길이를 못 잰 진입) 수다. 길이가 M 미만인
    것과 **모르는 것**은 다르다 — 세어서 내보낸다.
    """
    p = np.isfinite(rank) & (rank <= n_top)
    if p.shape[0] < 2:
        return np.zeros(0, dtype="int64"), np.zeros(0, dtype="int64"), 0
    run = forward_run_lengths(p)
    start = p[1:] & ~p[:-1]
    r, c = np.nonzero(start)
    r = r + 1
    length = run[r, c]
    reaches_end = (r + length) >= p.shape[0]
    keep = length >= m_snaps
    censored = int(np.count_nonzero(reaches_end & ~keep))
    return r[keep], c[keep], censored


# --------------------------------------------------------------------------- #
# 테이프 커버리지 — **이 태스크의 진짜 산출물**
# --------------------------------------------------------------------------- #
def window_counts(keys: np.ndarray, codes: np.ndarray, t0: np.ndarray,
                  lo_off_ms: int, hi_off_ms: int) -> np.ndarray:
    """`(t0+lo, t0+hi]` 안의 체결 건수. 부호화 키 하나로 종목·시각을 함께 자른다.

    경계: **하한 제외 · 상한 포함.** 사전 창은 호출부가 `lo=-60s, hi=-1ms` 가 아니라
    `[t0-60s, t0)` 이 되도록 오프셋을 준다.
    """
    if keys.size == 0 or t0.size == 0:
        return np.zeros(t0.shape, dtype="int64")
    base = codes.astype("int64") * TS_STRIDE
    lo = base + (t0 + lo_off_ms)
    hi = base + (t0 + hi_off_ms)
    return (np.searchsorted(keys, hi, side="right")
            - np.searchsorted(keys, lo, side="right"))


def interval_overlap(lo_keys: np.ndarray, hi_prefix_max: np.ndarray,
                     codes: np.ndarray, t0: np.ndarray, span_ms: int) -> np.ndarray:
    """`[t0, t0+span]` 이 그 종목의 구간 하나라도 겹치는가.

    `hi_prefix_max` 는 **부호화 값의 누적 최대**다. 앞 종목의 부호화 값은 코드가 작아
    이번 종목의 어떤 부호화 값보다도 작으므로, 전역 누적 최대를 써도 **종목 경계를
    넘지 않는다.** 덕분에 종목마다 자르지 않고 한 번의 `searchsorted` 로 끝난다.
    """
    out = np.zeros(t0.shape, dtype=bool)
    if lo_keys.size == 0 or t0.size == 0:
        return out
    base = codes.astype("int64") * TS_STRIDE
    idx = np.searchsorted(lo_keys, base + (t0 + span_ms), side="right")
    ok = idx > 0
    out[ok] = hi_prefix_max[idx[ok] - 1] >= (base[ok] + t0[ok])
    return out


def downtime_overlap(intervals: list[tuple[int, int]], t0: np.ndarray,
                     span_ms: int) -> np.ndarray:
    """커버리지 창이 **수집기 정전** 구간에 걸리는가. 구간이 서른 몇 개뿐이라 그냥 훑는다."""
    out = np.zeros(t0.shape, dtype=bool)
    for lo, hi in intervals:
        out |= (t0 <= hi) & ((t0 + span_ms) >= lo)
    return out


# --------------------------------------------------------------------------- #
# 청크 (랭킹 타입 x UTC 세션 하루)
# --------------------------------------------------------------------------- #
def chunk_events(mat: dict, rtype: str) -> pd.DataFrame:
    """한 청크의 사건 전부를 `(kind, cell)` 표로 편다. **아직 테이프는 안 붙인다.**"""
    rank, snaps = mat["rank"], mat["snaps"]
    parts: list[pd.DataFrame] = []

    def _add(kind: str, cell: str, rows: np.ndarray, cols: np.ndarray) -> None:
        if rows.size == 0:
            return
        parts.append(pd.DataFrame({"kind": kind, "cell": cell,
                                   "row": rows.astype("int64"),
                                   "col": cols.astype("int64")}))

    for n in TOP_NS:
        _add("E1_new_entry", f"N{n}", *new_entry_events(rank, n))
    for k in JUMP_KS:
        r, c, frm = rank_jump_events(rank, k)
        _add("E2_rank_jump", f"K{k}", r, c)
        for lo, hi in JUMP_FROM_TIERS:
            m = (frm >= lo) & (frm <= hi)
            _add("E2_rank_jump", f"K{k}_from{lo}_{hi}", r[m], c[m])
    for n in TOP_NS:
        for m_snaps in DWELL_SNAPS:
            r, c, _cens = dwell_start_events(rank, n, m_snaps)
            _add("E3_dwell_start", f"N{n}_M{m_snaps}", r, c)

    if not parts:
        return pd.DataFrame(columns=["kind", "cell", "row", "col"])
    ev = pd.concat(parts, ignore_index=True)
    ev["ranking_type"] = rtype
    ev["t0_ms"] = snaps[ev.row.to_numpy()]
    prev = np.where(ev.row.to_numpy() > 0, snaps[ev.row.to_numpy() - 1], -1)
    ev["prev_gap_ms"] = ev.t0_ms.to_numpy() - prev
    ev["symbol"] = mat["symbols"][ev.col.to_numpy()]
    ev["last_u"] = mat["last_u"][ev.row.to_numpy(), ev.col.to_numpy()]
    return ev


def dwell_censored(mat: dict) -> dict:
    """E3 의 **우절단** 건수만 따로 센다 — 길이가 모자란 것과 **모르는 것**은 다르다."""
    out = {}
    for n in TOP_NS:
        for m_snaps in DWELL_SNAPS:
            _r, _c, cens = dwell_start_events(mat["rank"], n, m_snaps)
            out[f"N{n}_M{m_snaps}"] = cens
    return out


def attach_tape(ev: pd.DataFrame, keys: np.ndarray, traded: set[str],
                gaps: tuple[np.ndarray, np.ndarray],
                downtime: list[tuple[int, int]]) -> pd.DataFrame:
    """사건마다 테이프를 붙인다. **0 건인 이유를 갈라서** 붙이는 것이 요점이다.

    - `no_tape_symbol`: 그 세션에 그 종목의 체결이 **한 건도 수집되지 않았다**.
      티어 승격이 랭킹이 아니라 별도 점수로 정해지므로 이것이 압도적 다수다.
    - `downtime_*`: 창이 **수집기 정전**에 걸렸다.
    - `tape_gap_*`: 창이 `tape_gaps` 가 기록한 **50 건 상한 결손**에 걸렸다.

    셋 중 어느 것도 아닌 0 건만이 "그 시각 그 종목에 체결이 없었다" 는 시장 사실이다.
    """
    if ev.empty:
        for c in ("n_pre60", "n_60", "n_300", "no_tape_symbol",
                  "downtime_60", "downtime_300", "tape_gap_60", "tape_gap_300"):
            ev[c] = pd.Series(dtype="int64" if c.startswith("n_") else "bool")
        return ev
    codes = ev.col.to_numpy(dtype="int64")
    t0 = ev.t0_ms.to_numpy(dtype="int64")
    ev["n_pre60"] = window_counts(keys, codes, t0, -PRE_S * SEC_MS - 1, -1)
    for w in COVERAGE_S:
        ev[f"n_{w}"] = window_counts(keys, codes, t0, 0, w * SEC_MS)
        ev[f"downtime_{w}"] = downtime_overlap(downtime, t0, w * SEC_MS)
        ev[f"tape_gap_{w}"] = interval_overlap(gaps[0], gaps[1], codes, t0, w * SEC_MS)
    ev["no_tape_symbol"] = ~ev.symbol.isin(traded).to_numpy()
    return ev


#: 청크 집계의 **키**. 사건 하나가 이 조합 하나에 속한다.
GROUP_KEYS = ("ranking_type", "session", "band", "kind", "cell", "price_band",
              "market", "security_type")


def aggregate_chunk(ev: pd.DataFrame) -> pd.DataFrame:
    """청크의 사건 표를 **집계로 접는다.** 원 사건 행은 청크 밖으로 나가지 않는다.

    사건이 백만 단위라 전부 들고 있으면 메모리가 터진다. 합계는 결합적이므로
    청크별 집계를 다시 합쳐도 전체 집계와 같다.
    """
    if ev.empty:
        return pd.DataFrame(columns=list(GROUP_KEYS) + ["n"])
    g = ev.groupby(list(GROUP_KEYS), dropna=False, observed=True)
    out = g.agg(
        n=("t0_ms", "size"),
        n_no_tape_symbol=("no_tape_symbol", "sum"),
        n_zero_60=("zero_60", "sum"),
        n_zero_300=("zero_300", "sum"),
        n_zero_pre60=("zero_pre60", "sum"),
        n_stale_prev=("stale_prev", "sum"),
        n_downtime_60=("downtime_60", "sum"),
        n_downtime_300=("downtime_300", "sum"),
        n_tape_gap_60=("tape_gap_60", "sum"),
        n_tape_gap_300=("tape_gap_300", "sum"),
        sum_60=("n_60", "sum"),
        sum_300=("n_300", "sum"),
        sum_pre60=("n_pre60", "sum"),
    ).reset_index()
    return out


def session_label_agrees(day_lo_ms: int, day_hi_ms: int) -> bool:
    """UTC 하루의 **양 끝**이 `SS.session_date` 로도 같은 라벨인가.

    태스크 전제(*"UTC 일자 묶음이 곧 세션 묶음이다"*)는 KST 사이클 시작이 09:00,
    즉 UTC 00:00 이라서 성립한다. 전제를 믿지 않고 **청크마다 확인**한다 —
    `session.py` 의 경계가 바뀌면 여기가 먼저 거짓이 된다.
    """
    want = pd.Timestamp(int(day_lo_ms), unit="ms", tz="UTC").strftime("%Y-%m-%d")
    return (SS.session_date(day_lo_ms) == want
            and SS.session_date(day_hi_ms - 1) == want)


def label_events(ev: pd.DataFrame, meta: pd.DataFrame, session: str) -> pd.DataFrame:
    """세션·세션띠·가격층·시장·파생 플래그를 붙인다. **세션 정의는 `session.py` 하나다.**

    `session` 은 청크의 UTC 날짜를 그대로 받는다. 행마다 `SS.session_date` 를 부르면
    백만 번의 `pd.Timestamp` 생성이 되어 러너가 분 단위로 느려진다 — 대신
    `session_label_agrees` 로 **청크당 두 번** 확인한다.
    """
    if ev.empty:
        return ev
    t0 = ev.t0_ms
    ev["band"] = SS.sessions_of(t0).to_numpy()
    ev["session"] = session
    ev["price_band"] = price_band_codes(ev.last_u.to_numpy(dtype="float64"))
    joined = meta.reindex(ev.symbol.to_numpy())
    ev["market"] = np.where(pd.isna(joined.market.to_numpy()), "unknown",
                            joined.market.to_numpy())
    ev["security_type"] = np.where(pd.isna(joined.security_type.to_numpy()), "unknown",
                                   joined.security_type.to_numpy())
    ev["stale_prev"] = ev.prev_gap_ms.to_numpy() > ADJACENCY_MAX_MS
    for w in COVERAGE_S:
        ev[f"zero_{w}"] = ev[f"n_{w}"].to_numpy() == 0
    ev["zero_pre60"] = ev.n_pre60.to_numpy() == 0
    return ev


# --------------------------------------------------------------------------- #
# 세션 지도와 경계 인구조사
# --------------------------------------------------------------------------- #
def session_universe(conn: sqlite3.Connection, grid: np.ndarray,
                     downtime: list[tuple[int, int]]) -> list[dict]:
    """UTC 세션 하루마다 **정규장 창이 얼마나 덮였는가.**

    부분 세션을 조용히 빼거나 온전한 것처럼 섞지 않기 위해서다. 덮임률은
    `(정규장 6.5 시간 - 앞뒤 잘림 - 안쪽 정전) / 6.5 시간` 이다.
    """
    if grid.size == 0:
        return []
    dates = pd.to_datetime(grid, unit="ms", utc=True).strftime("%Y-%m-%d")
    out = []
    for d in sorted(set(dates)):
        day0 = int(pd.Timestamp(d + "T00:00:00Z").timestamp() * 1000)
        ro_ms, rc_ms = day0 + REGULAR_OPEN_S * SEC_MS, day0 + REGULAR_CLOSE_S * SEC_MS
        inside = grid[(grid >= ro_ms) & (grid < rc_ms)]
        if inside.size == 0:
            out.append({"session": d, "regular_snaps": 0, "regular_coverage": 0.0,
                        "first_snap": None, "last_snap": None, "downtime_s": 0})
            continue
        miss = (int(inside[0]) - ro_ms) + (rc_ms - int(inside[-1]))
        for lo, hi in downtime:
            miss += max(0, min(hi, rc_ms) - max(lo, ro_ms))
        cov = max(0.0, 1.0 - miss / (REGULAR_SPAN_S * SEC_MS))
        out.append({"session": d, "regular_snaps": int(inside.size),
                    "regular_coverage": round(cov, 4),
                    "first_snap": ms_iso(inside[0]), "last_snap": ms_iso(inside[-1]),
                    "downtime_s": int(sum(max(0, min(hi, rc_ms) - max(lo, ro_ms))
                                          for lo, hi in downtime) / 1000)})
    return out


def boundary_census(conn: sqlite3.Connection, floor_ms: int, until_ms: int) -> list[dict]:
    """**구성 경계 지도** — 경계마다 앞뒤로 타입별 스냅·행이 각각 몇 개인가.

    경계를 넘어 뭉치면 표본 구성 변화를 시장 변화로 오독한다. 그 유혹을 없애려면
    **뭉친 수를 아예 내지 않고 앞뒤를 따로 내는 것**이 맞다.
    """
    rows = []
    for b in CONFIG_BOUNDARIES:
        bms = iso_ms(b["utc"])
        for rtype, duration in RANKING_SPECS:
            q = ("SELECT COUNT(*) n, COUNT(DISTINCT snap_ms) snaps FROM rankings_snap "
                 "WHERE ranking_type = ? AND duration = ? "
                 "AND snap_ms >= ? AND snap_ms < ?")
            # `until_ms` 는 **양쪽에** 걸린다. 앞쪽만 열어 두면 관측 창을 좁혀 다시
            # 돌렸을 때 "경계 앞" 숫자만 안 변해서 두 실행이 조용히 어긋난다.
            pre = conn.execute(q, (rtype, duration, floor_ms,
                                   min(bms, until_ms + 1))).fetchone()
            post = conn.execute(q, (rtype, duration, bms, until_ms + 1)).fetchone()
            rows.append({"boundary": b["key"], "boundary_utc": b["utc"],
                         "what": b["what"], "evidence": b["evidence"],
                         "ranking_type": rtype, "duration": duration,
                         "rows_before": int(pre[0]), "snaps_before": int(pre[1]),
                         "rows_after": int(post[0]), "snaps_after": int(post[1])})
    return rows


def universe_gate_census(conn: sqlite3.Connection, floor_ms: int,
                         until_ms: int) -> list[dict]:
    """**랭킹에 오른 종목 중 몇이 우리 유니버스·테이프 안에 있었나.**

    §6 의 "체결 0 건 90% 대" 를 읽는 사람은 곧바로 *"그럼 그 종목들이 안 움직였나"*
    로 갈 수 있다. 그게 아니라는 것을 이 표가 보인다 — 상당수는 **`symbols` 테이블에
    존재조차 하지 않는다.** 티어 승격이 랭킹이 아니라 별도 점수로 정해지기 때문이고,
    따라서 0 건은 시장 사실이 아니라 **수집 범위**다.

    **셋은 포함 관계가 아니다.** `symbols` 는 **지금 시점의** 유니버스 스냅샷이고
    (`updated_ms` 로 갱신된다) 체결 행은 **과거 사실**이라, 8 월 초에 체결이 수집됐지만
    그 뒤 표에서 빠진 종목이 있으면 `with_any_trade_row > in_symbols_table` 이 된다.
    실제로 `MARKET_TRADING_AMOUNT` 에서 그렇게 나온다. 포함 관계라고 적으면
    그 칸을 읽는 사람이 우리 표가 고장 난 줄 안다 — **그래서 안 적는다.**

    확실한 것은 하나다: **랭킹에 오른 종목 수가 나머지 둘보다 훨씬 크다.**
    """
    rows = []
    for rtype, duration in RANKING_SPECS:
        q = ("SELECT COUNT(*) FROM (SELECT DISTINCT symbol FROM rankings_snap "
             "WHERE ranking_type = ? AND duration = ? AND snap_ms >= ? AND snap_ms <= ?")
        base = (rtype, duration, floor_ms, until_ms)
        ranked = conn.execute(q + ")", base).fetchone()[0]
        in_uni = conn.execute(
            q + " AND symbol IN (SELECT symbol FROM symbols))", base).fetchone()[0]
        with_tape = conn.execute(
            q + " AND symbol IN (SELECT DISTINCT symbol FROM trades_snap "
                "WHERE ts_ms >= ? AND ts_ms <= ?))", base + (floor_ms, until_ms)
        ).fetchone()[0]
        rows.append({"ranking_type": rtype, "duration": duration,
                     "ranked_symbols": int(ranked),
                     "in_symbols_table": int(in_uni),
                     "with_any_trade_row": int(with_tape),
                     "share_with_tape": (round(with_tape / ranked, 6)
                                         if ranked else None)})
    return rows


def tape_gap_census(conn: sqlite3.Connection, until_ms: int) -> dict:
    """`tape_gaps` 자체의 지도. **이 테이블이 언제부터 있는지가 결론의 일부다.**

    2026-08-07 17:41Z 이전 세션에는 결손 **기록이 없다**. 결손이 없었다는 뜻이 아니라
    **재는 장치가 없었다**는 뜻이다. 그 앞뒤를 같은 자로 비교하면 안 된다.
    """
    n, lo, hi, syms = conn.execute(
        "SELECT COUNT(*), MIN(poll_ms), MAX(poll_ms), COUNT(DISTINCT symbol) "
        "FROM tape_gaps WHERE poll_ms <= ?", (until_ms,)).fetchone()
    per_day = pd.read_sql_query(
        "SELECT date(poll_ms/1000,'unixepoch') d, COUNT(*) n, "
        "COUNT(DISTINCT symbol) syms FROM tape_gaps WHERE poll_ms <= ? "
        "GROUP BY 1 ORDER BY 1", conn, params=(until_ms,))
    return {"n": int(n or 0), "first_poll": ms_iso(lo), "last_poll": ms_iso(hi),
            "symbols": int(syms or 0),
            "instrument_starts": ms_iso(lo),
            "note": ("sessions before the first row have NO gap record at all - "
                     "that is missing instrumentation, not absence of gaps"),
            "per_day": per_day.to_dict("records")}


# --------------------------------------------------------------------------- #
# 러너
# --------------------------------------------------------------------------- #
def run(db: Path, *, until_ms: int | None = None, progress: bool = False) -> dict:
    """전 랭킹 타입 x 전 UTC 세션을 돌며 사건을 세고 테이프를 붙인다.

    `progress` 는 청크마다 한 줄을 찍는다. 러너가 수 분을 도는데 아무 말이 없으면
    다음 사람이 멈춘 줄 알고 죽인다 — 그리고 진행 줄이 **어느 청크가 비었는지**를
    표에 나오기 전에 보여 준다.
    """
    conn = open_ro(db)
    try:
        floor_ms = holdout_floor_ms()
        db_max = int(conn.execute("SELECT MAX(snap_ms) FROM rankings_snap").fetchone()[0])
        until = int(until_ms) if until_ms is not None else db_max
        meta = load_symbol_meta(conn)
        grid = load_snap_grid(conn, floor_ms, until)
        downtime = downtime_intervals(grid)
        sessions = session_universe(conn, grid, downtime)
        agg_parts: list[pd.DataFrame] = []
        raw: dict[tuple, list[np.ndarray]] = {}
        chunks: list[dict] = []
        censored: dict[str, dict] = {}
        label_mismatch: list[tuple[str, str]] = []
        holdout_dropped = 0
        dup_dropped = 0

        for rtype, duration in RANKING_SPECS:
            for s in sessions:
                day0 = int(pd.Timestamp(s["session"] + "T00:00:00Z").timestamp() * 1000)
                lo = max(day0, floor_ms)
                hi = min(day0 + DAY_MS, until + 1)
                if hi <= lo:
                    continue
                if not session_label_agrees(lo, hi):
                    label_mismatch.append((rtype, s["session"]))
                df = load_rankings(conn, rtype, duration, lo, hi)
                if df.empty:
                    continue
                mat = rank_matrix(df)
                dup_dropped += mat["n_dup_dropped"]
                codes = {sym: i for i, sym in enumerate(mat["symbols"])}
                keys, traded = load_trade_keys(conn, lo - PRE_S * SEC_MS,
                                               hi + max(COVERAGE_S) * SEC_MS, codes,
                                               session_lo=lo, session_hi=hi)
                gaps = load_tape_gaps(conn, lo - PRE_S * SEC_MS,
                                      hi + max(COVERAGE_S) * SEC_MS, codes)
                ev = chunk_events(mat, rtype)
                if ev.empty:
                    continue
                # ★ 홀드아웃 가드 — SQL 바닥 위에 한 번 더. 버린 수를 산출물에 싣는다.
                cut = SS.drop_holdout(ev, ts_col="t0_ms")
                holdout_dropped += cut["n_dropped"]
                ev = cut["kept"]
                if ev.empty:
                    continue
                ev = attach_tape(ev, keys, traded, gaps, downtime)
                ev = label_events(ev, meta, s["session"])
                agg_parts.append(aggregate_chunk(ev))
                for (rt, kind, cell, band), sub in ev.groupby(
                        ["ranking_type", "kind", "cell", "band"], observed=True):
                    raw.setdefault((rt, kind, cell, band), []).append(
                        np.stack([sub.n_60.to_numpy(), sub.n_300.to_numpy(),
                                  sub.n_pre60.to_numpy()]).astype("int32"))
                cens = dwell_censored(mat)
                for cell, v in cens.items():
                    d = censored.setdefault(f"{rtype}|{cell}", {"censored": 0})
                    d["censored"] += v
                chunks.append({"ranking_type": rtype, "session": s["session"],
                               "snaps": int(mat["snaps"].size),
                               "symbols": int(mat["symbols"].size),
                               "events": int(len(ev)),
                               "traded_symbols": len(traded)})
                if progress:
                    c = chunks[-1]
                    print(f"  {c['ranking_type']:<32} {c['session']}  "
                          f"snaps={c['snaps']:>5}  ranked_syms={c['symbols']:>5}  "
                          f"traded_syms={c['traded_symbols']:>4}  "
                          f"events={c['events']:>7,}", flush=True)
        agg = (pd.concat(agg_parts, ignore_index=True) if agg_parts
               else pd.DataFrame(columns=list(GROUP_KEYS) + ["n"]))
        return {"db": str(db), "until_ms": until, "until_utc": ms_iso(until),
                "db_max_snap_utc": ms_iso(db_max),
                "holdout_floor_ms": floor_ms, "holdout_floor_utc": ms_iso(floor_ms),
                "holdout_dropped": int(holdout_dropped),
                "session_label_mismatch": [list(x) for x in label_mismatch],
                "duplicate_symbol_rows_dropped": int(dup_dropped),
                "sessions": sessions, "downtime": [(ms_iso(a), ms_iso(b))
                                                   for a, b in downtime],
                "chunks": chunks, "dwell_censored": censored,
                "agg": agg, "raw": raw,
                "boundaries": boundary_census(conn, floor_ms, until),
                "universe_gate": universe_gate_census(conn, floor_ms, until),
                "tape_gaps": tape_gap_census(conn, until)}
    finally:
        conn.close()


def cell_records(agg: pd.DataFrame, raw: dict) -> list[dict]:
    """`(랭킹타입, 사건, 칸)` 하나마다 문서가 싣는 레코드. **`REPORTED_FIELDS` 와 동치.**"""
    if agg.empty:
        return []
    keys = ["ranking_type", "kind", "cell"]
    tot = agg.groupby(keys, observed=True).sum(numeric_only=True).reset_index()
    reg = (agg[agg.band == "regular"].groupby(keys, observed=True)
           .n.sum().rename("n_regular").reset_index())
    tot = tot.merge(reg, on=keys, how="left")
    tot["n_regular"] = tot.n_regular.fillna(0).astype("int64")
    recs = []
    for _i, r in tot.iterrows():
        k = (r.ranking_type, r.kind, r.cell)
        arrs = [a for (rt, kd, cl, _b), lst in raw.items()
                if (rt, kd, cl) == k for a in lst]
        stack = (np.concatenate(arrs, axis=1) if arrs
                 else np.zeros((3, 0), dtype="int32"))
        n = int(r.n)
        recs.append({
            "ranking_type": r.ranking_type, "kind": r.kind, "cell": r.cell,
            "n": n, "n_regular": int(r.n_regular),
            # ★ 다음 태스크가 실제로 쓸 수 있는 표본. 나머지는 **결과를 잴 상대가 없다.**
            "n_measurable_300": int(r.n - r.n_zero_300),
            "n_no_tape_symbol": int(r.n_no_tape_symbol),
            "n_zero_60": int(r.n_zero_60), "n_zero_300": int(r.n_zero_300),
            "n_zero_pre60": int(r.n_zero_pre60),
            "share_no_tape_symbol": round(r.n_no_tape_symbol / n, 6) if n else None,
            "share_zero_60": round(r.n_zero_60 / n, 6) if n else None,
            "share_zero_300": round(r.n_zero_300 / n, 6) if n else None,
            "n_stale_prev": int(r.n_stale_prev),
            "n_downtime_300": int(r.n_downtime_300),
            "n_tape_gap_300": int(r.n_tape_gap_300),
            "mean_60": round(r.sum_60 / n, 4) if n else None,
            "mean_300": round(r.sum_300 / n, 4) if n else None,
            "mean_pre60": round(r.sum_pre60 / n, 4) if n else None,
            "p50_60": float(np.median(stack[0])) if stack.shape[1] else None,
            "p50_300": float(np.median(stack[1])) if stack.shape[1] else None,
            "p50_pre60": float(np.median(stack[2])) if stack.shape[1] else None,
        })
    return recs


def coverage_percentiles(raw: dict) -> list[dict]:
    """칸마다 창 안 체결 건수의 **분포**. 정규장만 낸다.

    중앙값 하나로 뭉개지 않는 이유는 `docs/44` §2-1 이 이미 비싸게 배웠다 —
    p10 이 3 초고 p75 가 54 초인 분포를 "중앙 32 초"로 부르면 18 배가 한 이름에 들어간다.
    여기서도 0 건이 중앙값을 차지하는 동안 꼬리에 수백 건이 있을 수 있다.
    """
    out = []
    for (rt, kind, cell, band), lst in sorted(raw.items()):
        if band != "regular" or not lst:
            continue
        st = np.concatenate(lst, axis=1)
        out.append({"ranking_type": rt, "kind": kind, "cell": cell, "band": band,
                    "n_60": pct_table(st[0]), "n_300": pct_table(st[1]),
                    "n_pre60": pct_table(st[2])})
    return out


def slice_table(agg: pd.DataFrame, extra: str, *, band: str | None = None
                ) -> list[dict]:
    """`(랭킹타입, 사건, 칸)` 에 축 하나를 더 붙인 표 (가격층·시장·세션·세션띠)."""
    if agg.empty:
        return []
    d = agg if band is None else agg[agg.band == band]
    if d.empty:
        return []
    keys = ["ranking_type", "kind", "cell", extra]
    g = d.groupby(keys, observed=True).sum(numeric_only=True).reset_index()
    g["share_no_tape_symbol"] = np.where(g.n > 0, g.n_no_tape_symbol / g.n, np.nan)
    g["share_zero_300"] = np.where(g.n > 0, g.n_zero_300 / g.n, np.nan)
    cols = keys + ["n", "n_no_tape_symbol", "n_zero_60", "n_zero_300",
                   "share_no_tape_symbol", "share_zero_300"]
    return g[cols].round(6).to_dict("records")


def build_report(res: dict) -> dict:
    """산출물 하나. **문서에 실리는 수치는 전부 여기를 지나간다.**"""
    agg, raw = res["agg"], res["raw"]
    return {
        "conditions": MEASUREMENT_CONDITIONS,
        "db": res["db"], "until_utc": res["until_utc"],
        "db_max_snap_utc": res["db_max_snap_utc"],
        "holdout": {"floor_utc": res["holdout_floor_utc"],
                    "window": [SS.HOLDOUT_START, SS.HOLDOUT_END],
                    "dropped_events": res["holdout_dropped"]},
        "session_label_mismatch": res["session_label_mismatch"],
        "duplicate_symbol_rows_dropped": res["duplicate_symbol_rows_dropped"],
        "grid": {"top_n": list(TOP_NS), "jump_k": list(JUMP_KS),
                 "jump_from_tiers": [list(t) for t in JUMP_FROM_TIERS],
                 "dwell_snaps": list(DWELL_SNAPS),
                 "coverage_s": list(COVERAGE_S), "pre_s": PRE_S,
                 "price_bands_u": [list(b) for b in PRICE_BANDS_U]},
        "sessions": res["sessions"], "downtime": res["downtime"],
        "chunks": res["chunks"], "dwell_censored": res["dwell_censored"],
        "boundaries": res["boundaries"], "universe_gate": res["universe_gate"],
        "tape_gaps": res["tape_gaps"],
        "cells": cell_records(agg, raw),
        "coverage_pct_regular": coverage_percentiles(raw),
        "by_price_band": slice_table(agg, "price_band"),
        "by_price_band_regular": slice_table(agg, "price_band", band="regular"),
        "by_market": slice_table(agg, "market"),
        "by_security_type": slice_table(agg, "security_type"),
        "by_session": slice_table(agg, "session"),
        "by_band": slice_table(agg, "band"),
    }


# --------------------------------------------------------------------------- #
# 콘솔 — **ASCII 만.** cp949 에서 비 ASCII 는 UnicodeEncodeError 로 죽는다.
# --------------------------------------------------------------------------- #
def _pct(x) -> str:
    return "-" if x is None or not np.isfinite(float(x)) else f"{100.0 * float(x):6.2f}%"


def _num(x, width: int) -> str:
    return f"{'-':>{width}}" if x is None else f"{float(x):>{width}.0f}"


def print_report(rep: dict) -> None:
    """문서에 그대로 붙일 실행 출력. **요약표가 아니라 러너가 만든 수치다.**"""
    c = rep["conditions"]
    print("=" * 78)
    print("hires ranking event inventory - SAMPLE COUNTS ONLY (no forward return/MFE)")
    print("=" * 78)
    print(f"db                 : {rep['db']}")
    print(f"observed until     : {rep['until_utc']}  (db max snap {rep['db_max_snap_utc']})")
    print(f"holdout floor      : {rep['holdout']['floor_utc']}  "
          f"window {rep['holdout']['window'][0]}..{rep['holdout']['window'][1]}  "
          f"dropped events {rep['holdout']['dropped_events']}")
    print(f"duplicate sym rows : {rep['duplicate_symbol_rows_dropped']}")
    print(f"session label check: mismatched chunks "
          f"{len(rep['session_label_mismatch'])} (UTC date == session.session_date)")
    print("-- measurement conditions (attach to EVERY number below) --")
    print(f"  poll period {c['poll_period_s']}s vs server grid {c['server_recompute_grid_s']}s"
          f" -> {_pct(c['unreceived_grid_tick_share'])} of grid ticks never received")
    print(f"  ranking age at receipt: median {c['received_ranking_age_median_s']}s")
    print(f"  5s poll deployed: {c['poll_5s_deployed']}   source: {c['source']}")
    print(f"  t0 = {c['t0_definition']}")

    print("\n[1] session universe (UTC date == session; regular = 13:30-20:00Z)")
    print(f"{'session':<12}{'reg_snaps':>10}{'coverage':>10}  first..last (regular)")
    for s in rep["sessions"]:
        print(f"{s['session']:<12}{s['regular_snaps']:>10}"
              f"{_pct(s['regular_coverage']):>10}  "
              f"{(s['first_snap'] or '-')}..{(s['last_snap'] or '-')}")
    print(f"collector downtime intervals (> {DOWNTIME_MIN_MS // 1000}s): "
          f"{len(rep['downtime'])}")
    for a, b in rep["downtime"]:
        print(f"    {a} -> {b}")

    print("\n[2] config boundary census (rows / distinct snaps, before | after)")
    for b in CONFIG_BOUNDARIES:
        rows = [r for r in rep["boundaries"] if r["boundary"] == b["key"]]
        print(f"-- {b['key']}  {b['utc']}  ({b['what']})")
        print(f"   evidence: {b['evidence']}")
        print(f"   {'ranking_type':<32}{'rows_before':>13}{'snaps_before':>14}"
              f"{'rows_after':>12}{'snaps_after':>13}")
        for r in rows:
            print(f"   {r['ranking_type']:<32}{r['rows_before']:>13,}"
                  f"{r['snaps_before']:>14,}{r['rows_after']:>12,}"
                  f"{r['snaps_after']:>13,}")

    print("\n[3] WHY coverage is low: ranked symbols vs our universe vs our tape")
    print("    The gap is OUR collection scope, not the market - tier promotion is")
    print("    not driven by these rankings, so most ranked symbols are never taped.")
    print("    NOT nested: 'in_universe' is the symbols table AS OF NOW, 'with_tape' is")
    print("    a past fact, so a dropped symbol can make with_tape > in_universe.")
    print("    share = with_tape / ranked.")
    print(f"   {'ranking_type':<32}{'ranked':>8}{'in_universe':>13}{'with_tape':>11}"
          f"{'share':>9}")
    for r in rep["universe_gate"]:
        print(f"   {r['ranking_type']:<32}{r['ranked_symbols']:>8,}"
              f"{r['in_symbols_table']:>13,}{r['with_any_trade_row']:>11,}"
              f"{_pct(r['share_with_tape']):>9}")

    print("\n[4] event counts + tape coverage, by ranking type x event x cell")
    print("    zero_60/zero_300 = events with NO trade in the window;")
    print("    no_tape = the symbol has no collected trade in that whole session")
    print("    MEASURABLE = events with >=1 trade in (t0, t0+300s] - the only ones a")
    print("    forward-outcome task can use at all. Everything else has no counterpart.")
    hdr = (f"{'kind':<15}{'cell':<16}{'n':>9}{'n_reg':>9}{'measur':>8}{'no_tape':>9}"
           f"{'zero60':>9}{'zero300':>9}{'p50_60':>8}{'p50_300':>9}{'stale':>7}")
    for rtype, _dur in RANKING_SPECS:
        rows = [r for r in rep["cells"] if r["ranking_type"] == rtype]
        print(f"-- {rtype}  (cells {len(rows)})")
        if not rows:
            print("   (no events)")
            continue
        print("   " + hdr)
        for r in rows:
            print(f"   {r['kind']:<15}{r['cell']:<16}{r['n']:>9,}{r['n_regular']:>9,}"
                  f"{r['n_measurable_300']:>8,}"
                  f"{_pct(r['share_no_tape_symbol']):>9}{_pct(r['share_zero_60']):>9}"
                  f"{_pct(r['share_zero_300']):>9}{_num(r['p50_60'], 8)}"
                  f"{_num(r['p50_300'], 9)}{r['n_stale_prev']:>7,}")

    print("\n[5] price band at t0 (last_u), REGULAR session only")
    print(f"    bands: {[b[0] for b in PRICE_BANDS_U]} "
          f"= $0-2 / $2-5 / $5-10 / $10+")
    print(f"   {'ranking_type':<32}{'kind':<15}{'cell':<10}{'band':<9}{'n':>9}"
          f"{'no_tape':>9}{'zero300':>9}{'measur':>8}")
    for r in rep["by_price_band_regular"]:
        if r["cell"] not in ("N50", "K10", "N50_M3"):
            continue
        print(f"   {r['ranking_type']:<32}{r['kind']:<15}{r['cell']:<10}"
              f"{r['price_band']:<9}{r['n']:>9,}"
              f"{_pct(r['share_no_tape_symbol']):>9}{_pct(r['share_zero_300']):>9}"
              f"{r['n'] - r['n_zero_300']:>8,}")

    print("\n[6] market / security_type (cell N50, all bands)")
    for tbl, col in (("by_market", "market"), ("by_security_type", "security_type")):
        for r in rep[tbl]:
            if r["cell"] != "N50" or r["kind"] != "E1_new_entry":
                continue
            print(f"   {r['ranking_type']:<32}{col:<15}{r[col]:<10}{r['n']:>9,}"
                  f"{_pct(r['share_no_tape_symbol']):>9}")

    print("\n[7] trade-count distribution inside the 300s window, REGULAR only")
    print("    a median of 0 does not mean the tail is empty - both are printed")
    print(f"   {'ranking_type':<32}{'kind':<15}{'cell':<10}{'n':>8}"
          f"{'p50':>7}{'p90':>8}{'p99':>9}{'max':>9}{'mean':>9}")
    for r in rep["coverage_pct_regular"]:
        if r["cell"] not in ("N50", "K10", "N50_M3") or r["n_300"]["n"] == 0:
            continue
        d = r["n_300"]
        print(f"   {r['ranking_type']:<32}{r['kind']:<15}{r['cell']:<10}{d['n']:>8,}"
              f"{d['p50']:>7.0f}{d['p90']:>8.0f}{d['p99']:>9.0f}{d['max']:>9.0f}"
              f"{d['mean']:>9.1f}")

    print("\n[8] tape_gaps table")
    t = rep["tape_gaps"]
    print(f"   rows {t['n']:,}  symbols {t['symbols']}  "
          f"first poll {t['first_poll']}  last poll {t['last_poll']}")
    print(f"   NOTE: {t['note']}")
    for r in t["per_day"]:
        print(f"     {r['d']}  gaps {r['n']:>6,}  symbols {r['syms']:>4}")

    print("\n[9] E3 right-censored dwell starts (run hit the chunk end)")
    tot = sum(v["censored"] for v in rep["dwell_censored"].values())
    print(f"   total {tot:,} across {len(rep['dwell_censored'])} (type, cell) pairs")
    print("=" * 78)


def main(argv: list[str]) -> int:
    db = DB
    until_ms = None
    out_dir = OUT_DIR
    args = list(argv[1:])
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--until-ms":
            until_ms = int(args[i + 1]); i += 2
        elif a == "--out":
            out_dir = Path(args[i + 1]); i += 2
        else:
            db = Path(a); i += 1
    print("scanning chunks (ranking type x UTC session) ...", flush=True)
    res = run(db, until_ms=until_ms, progress=True)
    rep = build_report(res)
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / "hires_events.json"
    p.write_text(json.dumps(rep, indent=1), encoding="utf-8")
    print_report(rep)
    print(f"-> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

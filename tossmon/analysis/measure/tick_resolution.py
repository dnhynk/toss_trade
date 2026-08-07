"""**0단계 관문 — 우리 스트림이 무엇을 볼 수 있고 무엇을 못 보는가** (docs/41).

## 왜 이 모듈이 먼저인가

이 프로젝트는 **해상도 대조를 후보 6개를 닫은 뒤에** 했다. 그래서 초 단위 현상을
1분봉으로 판정했고 사용자가 그것을 지적했다(`docs/29`). 이번에는 **관문으로 앞에** 둔다.

**여기서는 전략을 판정하지 않는다.** 재는 것은 계측기 자신이다 —
눈금(grain) · 주기(cadence) · 관측 지연(observation lag) · 구조적 사각(saturation) ·
감시 폭(coverage). 그 뒤에야 "무엇을 검정할 수 있는가"를 물을 수 있다.

## 이 모듈이 지키는 경계 (태스크 지시)

- **1분봉(`candles_1m`)을 읽지 않는다.** 캔들 라벨 규약이 미해결(D-10)이라 창이 밀린다.
  **체결(`trades_snap`)·호가(`orderbook_snap`)·랭킹(`rankings_snap`) 원자료만** 쓴다.
- **`session_date` 에 의존하지 않는다.** 애프터 종료 경계가 바뀔 예정(D-8 곁 결정)이라
  과거 사이클 배정이 재계산된다. 세션 구분은 **고정 ET 시계**(`SESSION_BANDS`)로 하고,
  경계를 리포트에 그대로 실어 나중에 갈아끼울 수 있게 한다.
- **08-04 이전을 섞지 않는다.** 체결 종목 폭이 10배 다르다(`WINDOW_START_MS`).
- **라이브 콜 0.** DB 는 `mode=ro` 로만 연다. 수집기는 가동 중이다.

실행: `python -m tossmon.analysis.measure.tick_resolution [db_path] [--log <collector.log>]`
산출물 `out/tick_resolution.json`.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------------- #
# 관측 창과 세션 경계 — 전부 절대 시각. 여기 적힌 것이 리포트의 측정 조건이다.
# --------------------------------------------------------------------------- #

#: 2026-08-04T08:00:00Z = 08-04 04:00 ET. 폭이 열린 고해상도 구간의 시작.
#: 이 이전은 체결 종목 폭이 10배 좁아 **같은 표에 섞지 않는다.**
WINDOW_START_MS = 1_785_830_400_000

#: 관측 창 전체가 EDT(UTC-4) 안에 있다 — 2026 미국 서머타임 전환은 3월과 11월이다.
#: 그래서 고정 오프셋으로 ET 벽시계를 정확히 복원할 수 있다.
ET_OFFSET_S = -4 * 3600

#: ET 벽시계 기준 세션 띠. **`session_date` 를 쓰지 않기 위한 대체물이며,
#: 경계가 바뀌면 여기만 고치면 된다.** (시작초, 끝초) — 하루 안의 초.
SESSION_BANDS: tuple[tuple[str, int, int], ...] = (
    ("pre",       4 * 3600,          int(9.5 * 3600)),
    ("regular",   int(9.5 * 3600),   16 * 3600),
    ("after",     16 * 3600,         20 * 3600),
    ("overnight", 20 * 3600,         24 * 3600 + 4 * 3600),   # 넘어감 처리는 아래
)

#: `/trades` 한 응답의 건수 상한 (`collector.loops.TRADES_COUNT`). 이 값이 곧 사각지대다.
TRADES_COUNT_CAP = 50

#: tier3 체결·호가 폴 주기(초) — `config.polling.tier3_trades_s` / `tier3_orderbook_s`.
TIER3_POLL_S = 4.0

#: 슈팅 정의 — `docs/29` 와 **같은 정의**를 쓴다. 정의를 바꾸면 6.0초와 비교가 안 된다.
SHOT_RISE = 0.01
SHOT_MAX_SECONDS = 60

#: 랭킹 가격 나이 추정에서 훑을 지연 후보(초).
RANK_AGE_OFFSETS_S = tuple(range(0, 46))

#: 랭킹 나이 추정에 쓸 종목 수 (체결 건수 상위). 크게 잡을수록 느려지기만 한다.
RANK_AGE_TOP_SYMBOLS = 40

OUT_DIR = Path("out")

PCTS = (1, 5, 10, 25, 50, 75, 90, 99)


# --------------------------------------------------------------------------- #
# 공통
# --------------------------------------------------------------------------- #
def open_ro(db: Path) -> sqlite3.Connection:
    """**읽기 전용**으로 연다. 수집기가 같은 파일에 쓰고 있다."""
    return sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=180)


def pct_table(values) -> dict:
    """백분위 표. 표본이 없으면 `n=0` 만 돌려준다 — 빈 배열에 중앙값을 묻지 않는다."""
    a = np.asarray(values, dtype="float64")
    a = a[np.isfinite(a)]
    if a.size == 0:
        return {"n": 0}
    out = {"n": int(a.size), "min": float(a.min()), "max": float(a.max()),
           "mean": float(a.mean())}
    for p in PCTS:
        out[f"p{p:02d}"] = float(np.percentile(a, p))
    return out


def et_second_of_day(ts_ms: np.ndarray) -> np.ndarray:
    return ((ts_ms // 1000) + ET_OFFSET_S) % 86400


def session_of(ts_ms: np.ndarray) -> np.ndarray:
    """ET 벽시계로 세션 띠를 붙인다. 경계는 `SESSION_BANDS` 가 전부다."""
    sod = et_second_of_day(np.asarray(ts_ms))
    out = np.full(sod.shape, "overnight", dtype=object)
    for name, lo, hi in SESSION_BANDS:
        if name == "overnight":
            continue
        out[(sod >= lo) & (sod < hi)] = name
    return out


def et_day(ts_ms: np.ndarray) -> np.ndarray:
    """ET 달력 날짜(정수 일련번호). 리포트 표기용."""
    return ((np.asarray(ts_ms) // 1000) + ET_OFFSET_S) // 86400


# --------------------------------------------------------------------------- #
# 1. 관측 창 인벤토리 — 무엇이 실제로 들어 있는가
# --------------------------------------------------------------------------- #
def inventory(conn: sqlite3.Connection) -> dict:
    end_ms = conn.execute("SELECT MAX(ts_ms) FROM trades_snap").fetchone()[0]
    rows = {}
    for table, tcol in (("trades_snap", "ts_ms"), ("orderbook_snap", "snap_ms"),
                        ("rankings_snap", "snap_ms")):
        rows[table] = conn.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT symbol) FROM {table} WHERE {tcol} >= ?",
            (WINDOW_START_MS,)).fetchone()
    per_day = conn.execute(
        """SELECT strftime('%Y-%m-%d', ts_ms/1000, 'unixepoch', '-4 hours') d,
                  COUNT(*), COUNT(DISTINCT symbol)
           FROM trades_snap WHERE ts_ms >= ? GROUP BY d ORDER BY d""",
        (WINDOW_START_MS,)).fetchall()
    per_session = {}
    ts = np.asarray([r[0] for r in conn.execute(
        "SELECT ts_ms FROM trades_snap WHERE ts_ms >= ?", (WINDOW_START_MS,))],
        dtype="int64")
    if ts.size:
        sess = session_of(ts)
        for name in ("pre", "regular", "after", "overnight"):
            per_session[name] = int((sess == name).sum())
    return {
        "window_start_ms": WINDOW_START_MS,
        "window_end_ms": int(end_ms) if end_ms else None,
        "window_note": ("08-04 04:00 ET 부터. 이전 구간은 체결 종목 폭이 10배 좁아 "
                        "같은 표에 섞지 않는다."),
        "session_bands_et": [{"name": n, "from_s": lo, "to_s": hi}
                             for n, lo, hi in SESSION_BANDS],
        "rows": {k: {"rows": v[0], "symbols": v[1]} for k, v in rows.items()},
        "trades_per_et_day": [{"et_date": d, "rows": n, "symbols": s}
                              for d, n, s in per_day],
        "trade_rows_per_session": per_session,
    }


def uptime_gaps(conn: sqlite3.Connection, *, min_gap_s: float = 120.0) -> dict:
    """수집기가 살아 있었는가 — 랭킹 스냅 간격으로 본다.

    랭킹은 **종목·시장 상황과 무관하게 12초마다** 도는 유일한 루프라 가동 대리지표로 맞다.
    체결이 없는 시간대를 "수집기가 죽었다"로 오독하지 않기 위해 이 대리지표를 쓴다.
    """
    snaps = np.asarray([r[0] for r in conn.execute(
        """SELECT DISTINCT snap_ms FROM rankings_snap
           WHERE snap_ms >= ? AND ranking_type = 'TOSS_SECURITIES_TRADING_VOLUME'
           ORDER BY snap_ms""", (WINDOW_START_MS,))], dtype="int64")
    if snaps.size < 2:
        return {"n_snaps": int(snaps.size), "gaps": []}
    d = np.diff(snaps) / 1000.0
    big = np.nonzero(d >= min_gap_s)[0]
    return {
        "n_snaps": int(snaps.size),
        "interval_s": pct_table(d),
        "min_gap_s": min_gap_s,
        "gaps": [{"from_ms": int(snaps[i]), "to_ms": int(snaps[i + 1]),
                  "gap_s": float(d[i])} for i in big],
        "total_gap_s": float(d[big].sum()) if big.size else 0.0,
        "covered_s": float(snaps[-1] - snaps[0]) / 1000.0,
    }


# --------------------------------------------------------------------------- #
# 2. 눈금 — 각 스트림의 시간 해상도 하한
# --------------------------------------------------------------------------- #
def trade_grain(conn: sqlite3.Connection) -> dict:
    """체결 시각의 **양자화 단위**. 이것이 '틱 해상도'의 진짜 하한이다.

    ⚠️ **같은 초 안의 순서는 복원 불가능하다.** `trades_snap` 은 `WITHOUT ROWID` 이고
    PK 가 `(symbol, ts_ms, price_u, qty_u)` 라, `ORDER BY ts_ms` 로 읽으면 같은 초의
    행들이 **가격 오름차순**으로 나온다. 이것을 시간 순서로 착각하면 "초 안에서 가격이
    계속 올랐다" 는 **저장 순서가 만들어낸 가짜 상승**을 보게 된다. 초 미만 구간의 모든
    순서 분석은 이 모듈에서 금지하고, **초 단위 집계**로만 내려간다.

    또 하나: `writer.insert_trades` 는 `(symbol, ts_ms, price_u, qty_u)` 를 **집합으로**
    묶는다. 같은 초·같은 가격·같은 수량의 체결 둘은 **한 건으로 접힌다** — 응답 안에서도,
    응답 사이에서도. 즉 **체결 건수는 구조적 과소집계**이며 바쁜 초일수록 심하다.
    """
    hist = conn.execute(
        "SELECT ts_ms % 1000 r, COUNT(*) FROM trades_snap WHERE ts_ms >= ? "
        "GROUP BY r ORDER BY 2 DESC LIMIT 8", (WINDOW_START_MS,)).fetchall()
    total = sum(n for _, n in hist)
    rows, pairs = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT symbol || ':' || ts_ms) FROM trades_snap "
        "WHERE ts_ms >= ?", (WINDOW_START_MS,)).fetchone()
    # 중복 접힘의 노출도: 같은 (종목, 초, 가격) 에 여러 행이 있으면 수량만 다른 것이고,
    # 수량이 겹쳤다면 그 체결은 애초에 저장되지 않았다.
    grp = conn.execute(
        "SELECT COUNT(*), SUM(c), SUM(CASE WHEN c >= 2 THEN c ELSE 0 END) FROM ("
        "  SELECT COUNT(*) c FROM trades_snap WHERE ts_ms >= ? "
        "  GROUP BY symbol, ts_ms, price_u)", (WINDOW_START_MS,)).fetchone()
    qty_top = conn.execute(
        "SELECT qty_u, COUNT(*) FROM trades_snap WHERE ts_ms >= ? "
        "GROUP BY qty_u ORDER BY 2 DESC LIMIT 10", (WINDOW_START_MS,)).fetchall()
    top_share = (sum(n for _, n in qty_top) / rows) if rows else 0.0
    return {
        "residual_ms_histogram": [{"residual_ms": r, "rows": n} for r, n in hist],
        "sub_second_rows": int(total - hist[0][1]) if hist else 0,
        "grain_ms": 1000 if (hist and hist[0][0] == 0
                             and hist[0][1] == total) else None,
        "rows": int(rows),
        "distinct_symbol_seconds": int(pairs),
        "rows_per_symbol_second": (float(rows) / pairs) if pairs else 0.0,
        "intra_second_order_recoverable": False,
        "dedup_collision": {
            "price_groups": int(grp[0] or 0),
            "rows_in_multi_row_price_groups_share": (float(grp[2] or 0) / rows)
                                                    if rows else 0.0,
            "qty_top10_share": float(top_share),
            "qty_top10": [{"qty_u": q, "rows": n} for q, n in qty_top],
            "measurable": False,
            "note": ("접힌 건수는 **우리 기록 어디에도 없다** — 수집기가 응답 원본 건수와 "
                     "저장 건수를 따로 세지 않는다. 수량 상위 10값이 전체의 "
                     "상당 비율이면 충돌 확률이 높다는 간접 증거만 남는다."),
        },
    }


def stream_cadence(conn: sqlite3.Connection) -> dict:
    """우리가 **실제로** 얼마나 자주 봤는가 — 설정값이 아니라 관측된 간격."""
    ob = {}
    per_sym: dict[str, list[int]] = {}
    for sym, snap in conn.execute(
            "SELECT symbol, snap_ms FROM orderbook_snap WHERE snap_ms >= ? "
            "ORDER BY symbol, snap_ms", (WINDOW_START_MS,)):
        per_sym.setdefault(sym, []).append(snap)
    fast, slow = [], []
    for sym, snaps in per_sym.items():
        if len(snaps) < 2:
            continue
        d = np.diff(np.asarray(snaps, dtype="int64")) / 1000.0
        fast.append(d[d < 60.0])
        slow.append(d[(d >= 60.0) & (d < 3600.0)])
    ob["tier3_lane_interval_s"] = pct_table(np.concatenate(fast) if fast else [])
    ob["tier2_lane_interval_s"] = pct_table(np.concatenate(slow) if slow else [])
    ob["symbols"] = len(per_sym)

    rk = {}
    for rtype in ("TOSS_SECURITIES_TRADING_VOLUME", "MARKET_TRADING_VOLUME",
                  "TOP_GAINERS"):
        snaps = np.asarray([r[0] for r in conn.execute(
            "SELECT DISTINCT snap_ms FROM rankings_snap WHERE snap_ms >= ? "
            "AND ranking_type = ? ORDER BY snap_ms", (WINDOW_START_MS, rtype))],
            dtype="int64")
        d = np.diff(snaps) / 1000.0 if snaps.size > 1 else np.array([])
        rk[rtype] = pct_table(d[d < 300.0]) if d.size else {"n": 0}
    return {"orderbook": ob, "rankings": rk,
            "trades_config_s": TIER3_POLL_S,
            "trades_note": ("`trades_snap` 에는 **폴 시각 컬럼이 없다.** 그래서 체결 폴 "
                            "주기는 DB 로 직접 못 잰다. 같은 tier3 루프의 호가 주기를 "
                            "대리지표로 쓰고, 설정값(4초)과 대조한다.")}


# --------------------------------------------------------------------------- #
# 3. 관측 지연 — 사건 발생 → 우리 DB 에 행이 생긴 시각
# --------------------------------------------------------------------------- #
def orderbook_lag(conn: sqlite3.Connection, *, active_min_trades: int = 2000) -> dict:
    """`snap_ms - ts_ms` — 서버가 찍은 시각과 **우리 벽시계 수신 시각**의 차.

    `snap_ms` 는 응답을 받은 직후 우리가 찍는다(`loops._poll_orderbook`).
    `ts_ms` 는 응답 본문의 `timestamp` 다. 이 둘의 차가 곧 관측 지연인데,
    **`ts_ms` 가 무엇인지부터 확인해야 한다** — 아래 `ts_is_event_time` 이 그 검증이다.
    """
    rows = conn.execute(
        "SELECT symbol, snap_ms, ts_ms FROM orderbook_snap "
        "WHERE snap_ms >= ? AND ts_ms IS NOT NULL", (WINDOW_START_MS,)).fetchall()
    if not rows:
        return {"n": 0}
    sym = np.asarray([r[0] for r in rows], dtype=object)
    snap = np.asarray([r[1] for r in rows], dtype="int64")
    ts = np.asarray([r[2] for r in rows], dtype="int64")
    lag = (snap - ts) / 1000.0

    active = {s for s, in conn.execute(
        "SELECT symbol FROM trades_snap WHERE ts_ms >= ? GROUP BY symbol "
        "HAVING COUNT(*) >= ?", (WINDOW_START_MS, active_min_trades))}
    mask = np.asarray([s in active for s in sym])
    sess = session_of(snap)

    out = {
        "all": pct_table(lag),
        "active_symbols": pct_table(lag[mask]),
        "active_symbol_count": len(active),
        "active_min_trades": active_min_trades,
        "by_session": {n: pct_table(lag[(sess == n) & mask])
                       for n in ("pre", "regular", "after", "overnight")},
        "definition": "snap_ms(우리 수신 벽시계) - ts_ms(응답 본문 timestamp), 초",
    }
    return out


def orderbook_ts_is_event_time(conn: sqlite3.Connection, *,
                               limit_symbols: int = 12) -> dict:
    """**`ts_ms` 는 시장 사건 시각인가, 서버 계산 시각인가.**

    가설: 사건 시각(호가/체결이 마지막으로 바뀐 때)이다.
    반증 관측: 사건 시각이라면 활발한 종목에서 `ts_ms` 가 **직전 체결 시각과 붙어 있어야**
    한다. 서버 계산 시각이라면 체결과 무관하게 항상 수신 직전 값이라 이 차가 0 근처로
    몰리는 대신 `snap_ms - ts_ms` 쪽이 좁아야 한다.
    """
    syms = [s for s, in conn.execute(
        "SELECT symbol FROM trades_snap WHERE ts_ms >= ? GROUP BY symbol "
        "ORDER BY COUNT(*) DESC LIMIT ?", (WINDOW_START_MS, limit_symbols))]
    diffs = []
    for s in syms:
        tt = np.asarray([r[0] for r in conn.execute(
            "SELECT DISTINCT ts_ms FROM trades_snap WHERE symbol = ? AND ts_ms >= ? "
            "ORDER BY ts_ms", (s, WINDOW_START_MS))], dtype="int64")
        ob = np.asarray([r[0] for r in conn.execute(
            "SELECT ts_ms FROM orderbook_snap WHERE symbol = ? AND snap_ms >= ? "
            "AND ts_ms IS NOT NULL ORDER BY ts_ms", (s, WINDOW_START_MS))],
            dtype="int64")
        if tt.size == 0 or ob.size == 0:
            continue
        idx = np.searchsorted(tt, ob, side="right") - 1
        ok = idx >= 0
        diffs.append((ob[ok] - tt[idx[ok]]) / 1000.0)
    d = np.concatenate(diffs) if diffs else np.array([])
    return {
        "symbols": syms,
        "ob_ts_minus_prev_trade_s": pct_table(d),
        "share_within_2s": float((np.abs(d) <= 2.0).mean()) if d.size else None,
        "reading": ("0 근처에 몰리면 `ts_ms` 는 **시장 사건 시각**이고 "
                    "`snap_ms - ts_ms` 를 관측 지연으로 읽을 수 있다."),
    }


def trade_observation_lag(cadence: dict, ob_lag: dict) -> dict:
    """체결 스트림의 관측 지연 — **분해해서** 낸다. 하나의 숫자로 뭉치지 않는다.

    `trades_snap` 에 수신 시각 컬럼이 없어 직접 측정이 불가능하다. 측정 가능한 조각으로
    나눈다: (a) 폴 대기 (b) 파이프라인 지연(호가에서 실측).

    **눈금(1초)은 지연이 아니라 시각의 불확실성이다.** 더하지 않고 따로 적는다 —
    둘을 합치면 "언제 알았나"와 "언제 일어났나"를 뒤섞게 된다.
    """
    lane = cadence["orderbook"]["tier3_lane_interval_s"]
    poll_p50 = lane.get("p50")
    floor = ob_lag.get("active_symbols", {}).get("p01")
    return {
        "timestamp_uncertainty_s": 1.0,
        "poll_wait_s": {"p50": (poll_p50 / 2.0) if poll_p50 else None,
                        "worst": poll_p50,
                        "source": "tier3 호가 실측 주기(같은 루프의 대리지표)"},
        "pipeline_floor_s": floor,
        "pipeline_floor_source": "활발한 종목의 호가 (snap_ms - ts_ms) p01",
        "delay_p50_s": (None if (poll_p50 is None or floor is None)
                        else round(poll_p50 / 2.0 + floor, 2)),
        "delay_worst_s": (None if (poll_p50 is None or floor is None)
                          else round(poll_p50 + floor, 2)),
        "assumption": ("체결 폴이 호가 폴과 같은 주기로 돈다고 가정했다. 실제로는 "
                       "포화 종목이 절반 주기(2초), 한산 종목이 더 긴 주기로 재배분된다"
                       "(`loops.tier3_trades_intervals`) — 즉 이 값은 **평균적인 종목** 기준."),
        "not_included": ("테이프가 포화된 폴에서는 응답 50건이 창을 다 못 덮으므로 "
                         "그 구간의 체결은 **영영 안 온다.** 지연이 아니라 결손이다 — "
                         "`tape_saturation` 참조."),
    }


# --------------------------------------------------------------------------- #
# 4. 구조적 사각 — 한 폴 50건 상한
# --------------------------------------------------------------------------- #
def tape_saturation(conn: sqlite3.Connection, *, bucket_s: float = TIER3_POLL_S) -> dict:
    """**한 폴 최대 50건**이 실제로 물렸는가.

    저장된 행만 세므로 이 값은 **하한**이다 — 잘려나간 체결은 애초에 DB 에 없다.
    """
    b = int(bucket_s * 1000)
    counts = np.asarray([r[0] for r in conn.execute(
        "SELECT COUNT(*) c FROM trades_snap WHERE ts_ms >= ? "
        "GROUP BY symbol, ts_ms / ?", (WINDOW_START_MS, b))], dtype="int64")
    persec = np.asarray([r[0] for r in conn.execute(
        "SELECT COUNT(*) c FROM trades_snap WHERE ts_ms >= ? "
        "GROUP BY symbol, ts_ms", (WINDOW_START_MS,))], dtype="int64")
    total_rows = int(counts.sum())
    sat = counts >= TRADES_COUNT_CAP
    near = counts >= (TRADES_COUNT_CAP * 0.9)
    return {
        "bucket_s": bucket_s,
        "cap": TRADES_COUNT_CAP,
        "n_buckets": int(counts.size),
        "bucket_rows": pct_table(counts),
        "buckets_at_cap": int(sat.sum()),
        "buckets_at_cap_share": float(sat.mean()) if counts.size else 0.0,
        "buckets_near_cap_share": float(near.mean()) if counts.size else 0.0,
        "rows_in_capped_buckets_share": (float(counts[sat].sum() / total_rows)
                                         if total_rows else 0.0),
        "rows_per_second": pct_table(persec),
        "note": ("저장된 행만 센 **하한**이다. 잘린 체결은 DB 에 없으므로 실제 포화는 "
                 "이보다 크다. 로그의 `tape gap` 경고가 잘린 사건을 직접 센다."),
    }


TAPE_GAP_RE = re.compile(
    r"^(?P<t>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+\s+WARNING tape gap (?P<sym>\S+): "
    r"prev_max=(?P<prev>\d+) < this_min=(?P<cur>\d+) \(n=(?P<n>\d+)\)")


def tape_gaps_from_log(log_path: Path) -> dict:
    """수집기 로그의 `tape gap` 경고 — **잘려나간 구간을 직접 센 유일한 기록.**

    로그 줄에는 `prev_max` 와 `this_min` 이 있어 **놓친 구간의 길이**를 알 수 있다.
    다만 `prev_max` 가 며칠 전이면 그건 결손이 아니라 **tier3 재진입**이다. 갈라 센다.
    """
    if not log_path or not log_path.exists():
        return {"available": False, "path": str(log_path)}
    n_lines = 0
    at_cap = 0
    holes: list[float] = []
    holes_at_cap: list[float] = []
    reentry = 0
    syms: dict[str, int] = {}
    with log_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "tape gap" not in line:
                continue
            m = TAPE_GAP_RE.match(line)
            if not m:
                continue
            n_lines += 1
            n = int(m.group("n"))
            hole_s = (int(m.group("cur")) - int(m.group("prev"))) / 1000.0
            if hole_s > 3600.0:           # 한 시간 넘는 구멍 = 재진입, 결손 아님
                reentry += 1
                continue
            holes.append(hole_s)
            syms[m.group("sym")] = syms.get(m.group("sym"), 0) + 1
            if n >= TRADES_COUNT_CAP:
                at_cap += 1
                holes_at_cap.append(hole_s)
    top = sorted(syms.items(), key=lambda kv: -kv[1])[:10]
    return {
        "available": True,
        "path": str(log_path),
        "lines": n_lines,
        "reentry_excluded": reentry,
        "within_session_gaps": len(holes),
        "at_cap": at_cap,
        "at_cap_share": (at_cap / len(holes)) if holes else 0.0,
        "hole_s": pct_table(holes),
        "hole_s_at_cap": pct_table(holes_at_cap),
        "top_symbols": [{"symbol": s, "gaps": n} for s, n in top],
        "note": ("로그 보존 구간 전체를 센 값이다 — DB 관측 창과 시작이 다를 수 있다. "
                 "회전 보존기간 밖은 못 본다."),
    }


# --------------------------------------------------------------------------- #
# 5. 감시 폭 — 그 순간 우리는 몇 종목을 보고 있었나
# --------------------------------------------------------------------------- #
def watch_width(conn: sqlite3.Connection) -> dict:
    """분 단위로 **체결이 관측된 고유 종목 수**.

    `/trades` 는 tier3 에만 돈다. 따라서 이 값은 tier3 동시 인원의 **하한 대리지표**다
    (그 분에 체결이 한 건도 없던 tier3 종목은 안 잡힌다).
    """
    rows = conn.execute(
        "SELECT ts_ms/60000 m, COUNT(DISTINCT symbol) k FROM trades_snap "
        "WHERE ts_ms >= ? GROUP BY m ORDER BY m", (WINDOW_START_MS,)).fetchall()
    if not rows:
        return {"n_minutes": 0}
    minutes = np.asarray([r[0] for r in rows], dtype="int64")
    k = np.asarray([r[1] for r in rows], dtype="int64")
    sess = session_of(minutes * 60000)
    span_min = int((minutes.max() - minutes.min()) + 1)
    return {
        "n_minutes_with_trades": int(minutes.size),
        "span_minutes": span_min,
        "minutes_without_trades": span_min - int(minutes.size),
        "symbols_per_minute": pct_table(k),
        "by_session": {n: pct_table(k[sess == n])
                       for n in ("pre", "regular", "after", "overnight")},
        "distinct_symbols_window": int(conn.execute(
            "SELECT COUNT(DISTINCT symbol) FROM trades_snap WHERE ts_ms >= ?",
            (WINDOW_START_MS,)).fetchone()[0]),
        "tier3_max_config": 10,
        "note": ("창 전체의 고유 종목 수는 **회전의 합계**이지 동시 인원이 아니다. "
                 "동시 인원은 `symbols_per_minute` 쪽이다."),
    }


# --------------------------------------------------------------------------- #
# 6. 랭킹은 얼마나 늙어서 도착하는가 — DB 안에서 자체 검증
# --------------------------------------------------------------------------- #
def ranking_price_age(conn: sqlite3.Connection, *,
                      ranking_type: str = "TOSS_SECURITIES_TRADING_VOLUME",
                      top_symbols: int = RANK_AGE_TOP_SYMBOLS,
                      placebo: bool = False, seed: int = 20260807) -> dict:
    """랭킹 행의 `last_u` 가 **몇 초 전 테이프 가격인가.**

    방법: 각 랭킹 행 `(symbol, snap_ms, last_u)` 에 대해 지연 후보 `d` 를 훑으며
    `snap_ms - d` 시점의 **직전 체결가**와 `last_u` 가 같은지 본다. 일치율이 최대인 `d`
    가 그 필드의 나이다.

    가격이 오래 안 변한 구간은 어떤 `d` 에서도 맞아 곡선을 평평하게 만든다. 그래서
    **직전 60초 안에 가격이 움직인 행만** 쓴다 — 정보가 있는 행만 남긴다.

    이 값은 `order`(순위열)의 나이가 아니라 **가격 필드의 나이**다. `docs/35` §5-3 이
    가격 필드가 격자보다 자주 돈다는 것을 실측했으므로, 이것은 랭킹 전체 나이의 **하한**이다.

    `placebo=True` 면 랭킹 행의 시각을 종목 안에서 **무작위로 섞는다.** 봉우리가 시각
    정렬에서 온 것인지, 가격 수준이 겹쳐 우연히 맞는 것인지 가른다.
    """
    rng = np.random.default_rng(seed)
    syms = [s for s, in conn.execute(
        "SELECT symbol FROM trades_snap WHERE ts_ms >= ? GROUP BY symbol "
        "ORDER BY COUNT(*) DESC LIMIT ?", (WINDOW_START_MS, top_symbols))]
    hits = np.zeros(len(RANK_AGE_OFFSETS_S), dtype="int64")
    used = 0
    for s in syms:
        tr = conn.execute(
            "SELECT ts_ms, price_u FROM trades_snap WHERE symbol = ? AND ts_ms >= ? "
            "ORDER BY ts_ms", (s, WINDOW_START_MS)).fetchall()
        rk = conn.execute(
            "SELECT snap_ms, last_u FROM rankings_snap WHERE symbol = ? "
            "AND snap_ms >= ? AND ranking_type = ? ORDER BY snap_ms",
            (s, WINDOW_START_MS, ranking_type)).fetchall()
        if len(tr) < 100 or len(rk) < 100:
            continue
        tts = np.asarray([r[0] for r in tr], dtype="int64")
        tpx = np.asarray([r[1] for r in tr], dtype="int64")
        rts = np.asarray([r[0] for r in rk], dtype="int64")
        rpx = np.asarray([r[1] for r in rk], dtype="int64")

        def px_at(when_ms: np.ndarray):
            i = np.searchsorted(tts, when_ms, side="right") - 1
            ok = i >= 0
            v = np.full(when_ms.shape, -1, dtype="int64")
            v[ok] = tpx[i[ok]]
            # 60초 넘게 체결이 없던 시점은 "가격을 모르는" 것으로 버린다
            stale = np.zeros(when_ms.shape, dtype=bool)
            stale[ok] = (when_ms[ok] - tts[i[ok]]) > 60_000
            v[stale] = -1
            return v

        now_px = px_at(rts)
        old_px = px_at(rts - 60_000)
        live = (now_px > 0) & (old_px > 0) & (now_px != old_px)
        if not live.any():
            continue
        used += int(live.sum())
        rts_l, rpx_l = rts[live], rpx[live]
        if placebo:                      # 시각과 가격의 짝을 끊는다
            rpx_l = rng.permutation(rpx_l)
        for j, d in enumerate(RANK_AGE_OFFSETS_S):
            hits[j] += int((px_at(rts_l - d * 1000) == rpx_l).sum())

    if used == 0:
        return {"n_rows": 0}
    rate = hits / used
    best = int(np.argmax(rate))
    return {
        "ranking_type": ranking_type,
        "placebo": placebo,
        "symbols_used": len(syms),
        "n_rows": used,
        "profile": [{"offset_s": d, "match_rate": float(rate[j])}
                    for j, d in enumerate(RANK_AGE_OFFSETS_S)],
        "best_offset_s": RANK_AGE_OFFSETS_S[best],
        "best_match_rate": float(rate[best]),
        "match_rate_at_0s": float(rate[0]),
        "definition": ("랭킹 행의 last_u 가 (snap_ms - d) 시점의 직전 체결가와 같을 확률. "
                       "직전 60초 안에 가격이 움직인 행만."),
        "bound": ("이것은 **가격 필드**의 나이다. docs/35 §5-3 실측상 순위열은 이보다 "
                  "느리게 갱신되므로, 랭킹 전체 나이의 하한으로만 읽어야 한다."),
    }


# --------------------------------------------------------------------------- #
# 7. 슈팅 길이 — 관문의 대조항. docs/29 와 **같은 정의**로만 잰다
# --------------------------------------------------------------------------- #
def intra_second_order_artifact(conn: sqlite3.Connection, *,
                                min_trades: int = 500,
                                seed: int = 20260807) -> dict:
    """**저장 순서를 시간 순서로 읽으면 무엇이 생기는가** — 크기를 잰다.

    가설: `trades_snap` 이 `WITHOUT ROWID`, PK `(symbol, ts_ms, price_u, qty_u)` 이므로
    `ORDER BY symbol, ts_ms` 는 같은 초 안을 **가격 오름차순**으로 돌려준다.
    반증 관측: 그렇다면 같은 초 안의 연속 차분에 **하락이 0건**이어야 한다.
    실측 결과가 아래 `stored_order` 다. `shuffled` 는 같은 초 안을 무작위로 섞은 대조군.

    이 표가 큰 이유: `tick_instrument.load_ticks` 가 정확히 이 정렬을 쓰고,
    거기서 나온 매수/매도 판정(`tick_classify`)과 슈팅 지속시간(`docs/29`)이
    이 순서 위에 서 있다.
    """
    rng = np.random.default_rng(seed)
    syms = [s for s, in conn.execute(
        "SELECT symbol FROM trades_snap WHERE ts_ms >= ? GROUP BY symbol "
        "HAVING COUNT(*) >= ?", (WINDOW_START_MS, min_trades))]
    up = dn = flat = 0
    sup = sdn = sflat = 0
    rows_total = rows_multi = 0
    for s in syms:
        rows = conn.execute(
            "SELECT ts_ms, price_u FROM trades_snap WHERE symbol = ? AND ts_ms >= ? "
            "ORDER BY symbol, ts_ms", (s, WINDOW_START_MS)).fetchall()
        if len(rows) < 2:
            continue
        ts = np.asarray([r[0] for r in rows], dtype="int64")
        px = np.asarray([r[1] for r in rows], dtype="float64")
        same = ts[1:] == ts[:-1]                    # 같은 초 안의 인접 쌍만 본다
        d = np.sign(px[1:] - px[:-1])[same]
        up += int((d > 0).sum()); dn += int((d < 0).sum()); flat += int((d == 0).sum())
        rows_total += len(px)
        _, counts = np.unique(ts, return_counts=True)
        rows_multi += int(counts[counts >= 2].sum())
        # 대조군: 초 안을 섞는다. 초 경계는 그대로 두고 안쪽만 바꾼다.
        order = np.lexsort((rng.random(len(ts)), ts))
        spx = px[order]
        sd = np.sign(spx[1:] - spx[:-1])[same]
        sup += int((sd > 0).sum()); sdn += int((sd < 0).sum())
        sflat += int((sd == 0).sum())
    tot = up + dn + flat
    stot = sup + sdn + sflat
    return {
        "symbols": len(syms), "min_trades_per_symbol": min_trades,
        "rows": rows_total,
        "rows_in_multi_trade_seconds_share": (rows_multi / rows_total)
                                             if rows_total else 0.0,
        "intra_second_pairs": tot,
        "stored_order": {"up": up, "down": dn, "flat": flat,
                         "up_share": (up / tot) if tot else 0.0,
                         "down_share": (dn / tot) if tot else 0.0},
        "shuffled": {"up": sup, "down": sdn, "flat": sflat,
                     "up_share": (sup / stot) if stot else 0.0,
                     "down_share": (sdn / stot) if stot else 0.0},
        "verdict": ("저장 순서에서 하락이 0건이면 초 안의 순서는 시간이 아니라 "
                    "가격 정렬이다. 그 위에서 계산한 틱 규칙·지속시간은 편향된다."),
    }


def price_scale(conn: sqlite3.Connection) -> dict:
    """**1% 가 몇 호가인가.** 임계를 가격 눈금과 대조하지 않으면 뜻을 알 수 없다."""
    px = np.asarray([r[0] for r in conn.execute(
        "SELECT price_u FROM trades_snap WHERE ts_ms >= ?", (WINDOW_START_MS,))],
        dtype="float64")
    if px.size == 0:
        return {"n": 0}
    dollars = px / 1e6
    return {
        "trade_price_usd": pct_table(dollars),
        "one_percent_usd_at_p50": float(np.percentile(dollars, 50) * 0.01),
        "penny_share": float((dollars < 1.0).mean()),
        "note": ("체결 가격이 $1 근처면 1% 는 1센트 — 최소 호가 한두 칸이다. "
                 "그런 종목에서 '+1% 슈팅'은 시장 사건이라기보다 **호가 눈금**일 수 있다."),
    }


def second_bars(conn: sqlite3.Connection, symbol: str):
    """한 종목의 **초 단위 막대**. 초 안의 순서를 쓰지 않는 유일한 안전한 표현.

    초 안에서는 `n`(건수) · `lo` · `hi` · `vwap` 만 정의된다. `open`/`close` 는
    저장 순서에 의존하므로 **만들지 않는다**(`trade_grain` 의 경고 참조).
    """
    rows = conn.execute(
        "SELECT ts_ms, COUNT(*), MIN(price_u), MAX(price_u), "
        "       SUM(price_u * qty_u), SUM(qty_u) "
        "FROM trades_snap WHERE symbol = ? AND ts_ms >= ? "
        "GROUP BY ts_ms ORDER BY ts_ms", (symbol, WINDOW_START_MS)).fetchall()
    if not rows:
        return None
    ts = np.asarray([r[0] for r in rows], dtype="int64")
    n = np.asarray([r[1] for r in rows], dtype="int64")
    lo = np.asarray([r[2] for r in rows], dtype="float64")
    hi = np.asarray([r[3] for r in rows], dtype="float64")
    qty = np.asarray([r[5] for r in rows], dtype="float64")
    notional = np.asarray([r[4] for r in rows], dtype="float64")
    vwap = np.where(qty > 0, notional / np.maximum(qty, 1.0), (lo + hi) / 2.0)
    return ts, n, lo, hi, vwap


def shot_durations(conn: sqlite3.Connection, *, rise: float = SHOT_RISE,
                   max_seconds: int = SHOT_MAX_SECONDS,
                   min_trades: int = 500, price_mode: str = "vwap") -> dict:
    """저점에서 `max_seconds` 안에 `rise` 이상 오르는 구간의 **지속시간 분포**.

    규칙은 `docs/29` 의 `find_shot_starts` 와 같다(정의를 바꾸면 6.0초와 비교가 안 된다).
    다만 **입력이 다르다**: 체결 행 하나하나가 아니라 **초 막대의 vwap** 위에서 돈다.
    행 단위로 돌리면 같은 초의 가격 오름차순 저장 순서가 상승을 **만들어낸다**
    (실측: 그렇게 재면 슈팅의 25%가 지속시간 0초로 나온다 — 전부 한 초 안의 정렬 artefact).

    **초 안에서 일어난 상승은 이 방법으로 볼 수 없다.** 그래서 그 몫을
    `intra_second_rise_share` 로 따로 센다 — 그것이 우리 자의 사각이다.
    """
    if price_mode not in ("vwap", "mid", "lo_to_hi"):
        raise ValueError(f"unknown price_mode {price_mode!r}")
    conditions = {"rise": rise, "max_seconds": max_seconds, "price_mode": price_mode,
                  "price_series": f"초 막대 {price_mode} (초 안 순서는 쓰지 않는다)",
                  "min_trades_per_symbol": min_trades}
    syms = [s for s, in conn.execute(
        "SELECT symbol FROM trades_snap WHERE ts_ms >= ? GROUP BY symbol "
        "HAVING COUNT(*) >= ?", (WINDOW_START_MS, min_trades))]
    durations, starts, rises = [], [], []
    intra_hits = intra_total = 0
    for s in syms:
        bars = second_bars(conn, s)
        if bars is None or bars[0].size < 2:
            continue
        ts, _n, lo, hi, vwap = bars
        ok = lo > 0
        intra_total += int(ok.sum())
        intra_hits += int(((hi[ok] / lo[ok] - 1.0) >= rise).sum())
        if price_mode == "vwap":
            anchor, peak = vwap, vwap
        elif price_mode == "lo_to_hi":            # 가장 공격적: 저가 → 고가
            anchor, peak = lo, hi
        else:                                      # "mid"
            mid = (lo + hi) / 2.0
            anchor, peak = mid, mid
        m = len(ts)
        i = 0
        while i < m - 1:
            hi_j, hi_px = -1, peak[i]
            j = i + 1
            while j < m and ts[j] - ts[i] <= max_seconds * 1000:
                if peak[j] > hi_px:
                    hi_px, hi_j = peak[j], j
                j += 1
            if hi_j > 0 and anchor[i] > 0 and hi_px / anchor[i] - 1.0 >= rise:
                durations.append((ts[hi_j] - ts[i]) / 1000.0)
                starts.append(int(ts[i]))
                rises.append(float(hi_px / anchor[i] - 1.0))
                i = hi_j
            else:
                i += 1
    if not durations:
        return {**conditions, "n_shots": 0, "symbols_scanned": len(syms),
                "duration_s": {"n": 0},
                "intra_second_rise_share": ((intra_hits / intra_total)
                                            if intra_total else 0.0),
                "intra_second_seconds": intra_total}
    d = np.asarray(durations)
    st = np.asarray(starts, dtype="int64")
    sess = session_of(st)
    return {
        **conditions,
        "symbols_scanned": len(syms),
        "n_shots": int(d.size),
        "duration_s": pct_table(d),
        "total_rise": pct_table(np.asarray(rises)),
        "share_under_4s": float((d <= 4.0).mean()),
        "share_under_16s": float((d <= 16.0).mean()),
        "share_under_30s": float((d <= 30.0).mean()),
        "by_session": {n: pct_table(d[sess == n])
                       for n in ("pre", "regular", "after", "overnight")},
        "intra_second_rise_share": (intra_hits / intra_total) if intra_total else 0.0,
        "intra_second_seconds": intra_total,
        "definition": "docs/29 find_shot_starts 와 같은 규칙 (저점→60초 내 +1%)",
        "caveat": ("지속시간의 분해능은 **1초**다. 0초는 '순간'이 아니라 '한 초 안'이며, "
                   "그 안에서 무슨 일이 있었는지는 우리 자료로 알 수 없다."),
    }


# --------------------------------------------------------------------------- #
# 조립
# --------------------------------------------------------------------------- #
def build_report(conn: sqlite3.Connection, *, log_path: Path | None = None) -> dict:
    inv = inventory(conn)
    cad = stream_cadence(conn)
    ob = orderbook_lag(conn)
    rep = {
        "inventory": inv,
        "uptime": uptime_gaps(conn),
        "trade_grain": trade_grain(conn),
        "cadence": cad,
        "orderbook_lag": ob,
        "orderbook_ts_meaning": orderbook_ts_is_event_time(conn),
        "trade_observation_lag": trade_observation_lag(cad, ob),
        "tape_saturation": tape_saturation(conn),
        "tape_gaps_log": tape_gaps_from_log(log_path) if log_path
                         else {"available": False},
        "watch_width": watch_width(conn),
        "ranking_price_age": ranking_price_age(conn),
        "ranking_price_age_placebo": ranking_price_age(conn, placebo=True),
        "intra_second_order_artifact": intra_second_order_artifact(conn),
        "price_scale": price_scale(conn),
        "shot_durations": shot_durations(conn, price_mode="vwap"),
        # ★ 답이 가격 표현을 따라가는지 본다. 따라가면 그건 시장이 아니라 우리 자다.
        "shot_durations_by_price_mode": {
            mode: {k: v for k, v in shot_durations(conn, price_mode=mode).items()
                   if k in ("n_shots", "duration_s", "share_under_4s",
                            "share_under_16s", "total_rise")}
            for mode in ("vwap", "mid", "lo_to_hi")},
        "shot_durations_by_threshold": {
            f"{r:.0%}": {k: v for k, v in
                         shot_durations(conn, rise=r, price_mode="vwap").items()
                         if k in ("n_shots", "duration_s", "total_rise")}
            for r in (0.01, 0.02, 0.03)},
    }
    rep["gate"] = gate_contrast(rep)
    return rep


#: W1 실측 (`docs/35` §5-2) — 랭킹 **순위열**은 우리 손에 들어올 때 이미 이만큼 늙어 있다.
#: 프리마켓 1회 측정이고 우리 DB 에는 `rankedAt` 이 없어(§2-1) 여기서 재현할 수 없다.
RANKING_ORDER_STALENESS_P50_S = 16.1
RANKING_ORDER_STALENESS_SOURCE = "docs/35 §5-2 (W1 실측, 2026-08-07 프리마켓, n=360)"


def gate_contrast(rep: dict) -> dict:
    """★ 관문의 결론 — **어느 스트림이 트리거가 될 수 있는가.**

    판정 기준 하나뿐이다: **그 스트림의 관측 지연이 슈팅 지속시간보다 짧은가.**
    길면 우리가 알았을 때 사건은 이미 끝나 있다.

    랭킹은 **두 줄**로 적는다. 같은 응답 안에 시계가 둘이기 때문이다(`docs/35` §5-3):
    가격 필드는 초 단위로 돌고, 순위열은 10초 격자에 묶여 있다. 전략이 말하는
    "급상승 랭킹" 은 **순위열** 쪽이다.
    """
    shot_p50 = rep["shot_durations"].get("duration_s", {}).get("p50")
    trade_lag = rep["trade_observation_lag"].get("delay_p50_s")
    ob_lane = rep["cadence"]["orderbook"]["tier3_lane_interval_s"].get("p50")
    ob_pipe = rep["orderbook_lag"].get("active_symbols", {}).get("p01")
    rank_poll = rep["cadence"]["rankings"].get(
        "TOSS_SECURITIES_TRADING_VOLUME", {}).get("p50")
    rank_age = rep["ranking_price_age"].get("best_offset_s")

    streams = []
    if trade_lag is not None:
        streams.append({"stream": "trades_snap", "field": "체결",
                        "lag_p50_s": trade_lag,
                        "detail": "폴 대기(주기/2) + 파이프라인 하한",
                        "measured_here": True})
    if ob_lane is not None and ob_pipe is not None:
        streams.append({"stream": "orderbook_snap", "field": "호가",
                        "lag_p50_s": round(ob_lane / 2.0 + ob_pipe, 2),
                        "detail": "폴 대기(주기/2) + 파이프라인 하한",
                        "measured_here": True})
    if rank_poll is not None and rank_age is not None:
        streams.append({"stream": "rankings_snap", "field": "가격 필드(lastPrice)",
                        "lag_p50_s": round(rank_poll / 2.0 + rank_age, 2),
                        "detail": "폴 대기(주기/2) + 가격필드 나이(이 모듈 실측)",
                        "measured_here": True})
        streams.append({"stream": "rankings_snap", "field": "순위열(order) ← 전략이 쓰는 것",
                        "lag_p50_s": round(rank_poll / 2.0
                                           + RANKING_ORDER_STALENESS_P50_S, 2),
                        "detail": f"폴 대기(주기/2) + {RANKING_ORDER_STALENESS_SOURCE}",
                        "measured_here": False})
    for s in streams:
        s["shorter_than_shot_p50"] = (None if shot_p50 is None
                                      else bool(s["lag_p50_s"] < shot_p50))
    return {
        "shot_duration_p50_s": shot_p50,
        "streams": streams,
        "trigger_capable": [s["stream"] + "/" + s["field"] for s in streams
                            if s.get("shorter_than_shot_p50")],
        "rule": "관측 지연 < 슈팅 지속시간(p50) 이어야 그 스트림이 트리거가 될 수 있다.",
        "coverage_caveat": ("지연을 통과해도 **그 종목을 보고 있어야** 발화한다. "
                            "체결·호가는 분당 중앙 "
                            f"{rep['watch_width']['symbols_per_minute'].get('p50')} 종목만 "
                            "덮는다 — `watch_width` 참조. 랭킹만 100종을 한 번에 덮는다."),
    }


def _fmt(v, nd=2):
    return "n/a" if v is None else f"{v:.{nd}f}"


def main(db: Path, *, log_path: Path | None = None,
         out_dir: Path | None = None) -> int:
    conn = open_ro(db)
    rep = build_report(conn, log_path=log_path)

    inv = rep["inventory"]
    print("=== [docs/41 sec 1] OBSERVATION WINDOW (absolute ms, no session_date)")
    print(f"  window {inv['window_start_ms']} .. {inv['window_end_ms']}")
    for r in inv["trades_per_et_day"]:
        print(f"    {r['et_date']}  trade rows {r['rows']:>7}  symbols {r['symbols']:>4}")
    print(f"  trade rows by ET session band: {inv['trade_rows_per_session']}")
    up = rep["uptime"]
    print(f"  collector uptime gaps >= {up.get('min_gap_s')}s: {len(up.get('gaps', []))}"
          f"  total {_fmt(up.get('total_gap_s'), 0)}s of {_fmt(up.get('covered_s'), 0)}s")

    g = rep["trade_grain"]
    print("\n=== [docs/41 sec 2] STREAM GRAIN")
    print(f"  trades ts_ms grain: {g['grain_ms']} ms  (sub-second rows {g['sub_second_rows']})")
    print(f"  rows per (symbol, second): {g['rows_per_symbol_second']:.2f}  "
          f"(intra-second order recoverable: {g['intra_second_order_recoverable']})")
    dc = g["dedup_collision"]
    print(f"  dedup exposure: rows sharing (symbol,second,price) "
          f"{dc['rows_in_multi_row_price_groups_share']:.2%}, "
          f"qty top-10 values cover {dc['qty_top10_share']:.2%} of rows")
    cad = rep["cadence"]
    print(f"  orderbook tier3 lane interval p50 "
          f"{_fmt(cad['orderbook']['tier3_lane_interval_s'].get('p50'))}s  "
          f"(n={cad['orderbook']['tier3_lane_interval_s'].get('n')})")
    for k, v in cad["rankings"].items():
        print(f"  ranking {k:<34} interval p50 {_fmt(v.get('p50'))}s (n={v.get('n')})")

    print("\n=== [docs/41 sec 3] OBSERVATION LAG (event -> row in our DB)")
    m = rep["orderbook_ts_meaning"]
    print(f"  orderbook ts_ms vs previous trade: p50 "
          f"{_fmt(m['ob_ts_minus_prev_trade_s'].get('p50'))}s, "
          f"within 2s {_fmt(m.get('share_within_2s'), 3)}")
    ob = rep["orderbook_lag"]
    a = ob.get("active_symbols", {})
    print(f"  orderbook (snap_ms - ts_ms) active symbols: p01 {_fmt(a.get('p01'))}s  "
          f"p50 {_fmt(a.get('p50'))}s  p90 {_fmt(a.get('p90'))}s  n={a.get('n')}")
    tl = rep["trade_observation_lag"]
    print(f"  trades observation DELAY: p50 {_fmt(tl.get('delay_p50_s'))}s  "
          f"worst {_fmt(tl.get('delay_worst_s'))}s "
          f"(poll/2 + pipeline {_fmt(tl.get('pipeline_floor_s'))}s); "
          f"timestamp uncertainty {_fmt(tl.get('timestamp_uncertainty_s'),0)}s separately")

    ts_ = rep["tape_saturation"]
    print("\n=== [docs/41 sec 4] TAPE SATURATION (structural blind spot)")
    print(f"  {ts_['bucket_s']}s buckets: {ts_['n_buckets']}, at cap(>= {ts_['cap']}) "
          f"{ts_['buckets_at_cap']} ({ts_['buckets_at_cap_share']:.4%}), "
          f"rows inside them {ts_['rows_in_capped_buckets_share']:.2%}")
    print(f"  rows per (symbol, second): p50 {_fmt(ts_['rows_per_second'].get('p50'))} "
          f"p99 {_fmt(ts_['rows_per_second'].get('p99'))} max {_fmt(ts_['rows_per_second'].get('max'))}")
    lg = rep["tape_gaps_log"]
    if lg.get("available"):
        print(f"  log tape gaps: {lg['lines']} lines, within-session {lg['within_session_gaps']}, "
              f"at cap {lg['at_cap']} ({lg['at_cap_share']:.1%}), "
              f"hole p50 {_fmt(lg['hole_s'].get('p50'))}s p90 {_fmt(lg['hole_s'].get('p90'))}s")

    ar = rep["intra_second_order_artifact"]
    print("\n=== [docs/41 sec 4b] INTRA-SECOND ORDER IS NOT TIME ORDER")
    print(f"  rows inside multi-trade seconds: "
          f"{ar['rows_in_multi_trade_seconds_share']:.1%} of {ar['rows']} rows")
    print(f"  stored order  : up {ar['stored_order']['up_share']:.4f}  "
          f"down {ar['stored_order']['down_share']:.4f}  "
          f"(n pairs {ar['intra_second_pairs']})")
    print(f"  shuffled ctrl : up {ar['shuffled']['up_share']:.4f}  "
          f"down {ar['shuffled']['down_share']:.4f}")
    ps = rep["price_scale"]
    print(f"  trade price USD p50 {_fmt(ps['trade_price_usd'].get('p50'))}  "
          f"-> 1% = ${ps['one_percent_usd_at_p50']:.4f}; "
          f"under $1: {ps['penny_share']:.1%}")

    w = rep["watch_width"]
    print("\n=== [docs/41 sec 5] WATCH WIDTH (were we even looking?)")
    print(f"  symbols per minute: p50 {_fmt(w['symbols_per_minute'].get('p50'),1)}  "
          f"p90 {_fmt(w['symbols_per_minute'].get('p90'),1)}  "
          f"max {_fmt(w['symbols_per_minute'].get('max'),0)}")
    for k, v in w["by_session"].items():
        print(f"    {k:<10} p50 {_fmt(v.get('p50'),1)}  n_min {v.get('n')}")
    print(f"  distinct symbols over whole window (rotation total, NOT concurrent): "
          f"{w['distinct_symbols_window']}")

    ra = rep["ranking_price_age"]
    pb = rep["ranking_price_age_placebo"]
    print("\n=== [docs/41 sec 6] RANKING PRICE-FIELD AGE (measured inside our own DB)")
    if ra.get("n_rows"):
        print(f"  rows used {ra['n_rows']}, best offset {ra['best_offset_s']}s "
              f"(match {ra['best_match_rate']:.3f}), at 0s {ra['match_rate_at_0s']:.3f}")
        prof = {x["offset_s"]: x["match_rate"] for x in ra["profile"]}
        print("  profile: " + "  ".join(f"{d}s={prof[d]:.3f}"
                                        for d in (0, 1, 2, 5, 10, 16, 30, 45) if d in prof))
        if pb.get("n_rows"):
            pp = {x["offset_s"]: x["match_rate"] for x in pb["profile"]}
            print(f"  placebo (times shuffled): best {pb['best_offset_s']}s "
                  f"match {pb['best_match_rate']:.3f}; at 1s {pp.get(1, float('nan')):.3f}")
        print(f"  ORDER field is NOT this: {RANKING_ORDER_STALENESS_P50_S}s "
              f"({RANKING_ORDER_STALENESS_SOURCE})")
    else:
        print("  not enough rows")

    sd = rep["shot_durations"]
    print("\n=== [docs/41 sec 7] SHOT DURATION (same definition as docs/29)")
    if sd.get("n_shots"):
        print(f"  price series: {sd['price_series']}")
        print(f"  shots {sd['n_shots']} over {sd['symbols_scanned']} symbols  "
              f"duration p50 {_fmt(sd['duration_s'].get('p50'),1)}s  "
              f"p90 {_fmt(sd['duration_s'].get('p90'),1)}s  "
              f"<=4s {sd['share_under_4s']:.1%}  <=16s {sd['share_under_16s']:.1%}")
        for k, v in sd["by_session"].items():
            print(f"    {k:<10} n {v.get('n')}  p50 {_fmt(v.get('p50'),1)}s  "
                  f"p90 {_fmt(v.get('p90'),1)}s")
        print(f"  seconds whose own hi/lo range alone is >= {sd['rise']:.0%}: "
              f"{sd['intra_second_rise_share']:.3%} of {sd['intra_second_seconds']} seconds "
              f"(invisible to any sequencing we can do)")
        print("  --- does the answer follow our price representation?")
        for mode, v in rep["shot_durations_by_price_mode"].items():
            print(f"    {mode:<9} shots {v['n_shots']:>6}  duration p50 "
                  f"{_fmt(v['duration_s'].get('p50'),1)}s  <=4s {v['share_under_4s']:.1%}")
        print("  --- does the answer follow our threshold?")
        for th, v in rep["shot_durations_by_threshold"].items():
            print(f"    rise {th:<4} shots {v['n_shots']:>6}  duration p50 "
                  f"{_fmt(v['duration_s'].get('p50'),1)}s  total rise p50 "
                  f"{_fmt(v['total_rise'].get('p50'),4)}")

    gt = rep["gate"]
    print("\n=== [docs/41 GATE] WHICH STREAM CAN BE A TRIGGER")
    print(f"  shot duration p50 = {_fmt(gt['shot_duration_p50_s'],1)}s")
    for s in gt["streams"]:
        print(f"    {s['stream']:<16} {s['field']:<28} lag p50 {_fmt(s['lag_p50_s'])}s  "
              f"faster: {s['shorter_than_shot_p50']}")
        print(f"        [{s['detail']}]")
    print(f"  -> trigger capable: {gt['trigger_capable']}")
    print(f"  -> {gt['coverage_caveat']}")

    out = out_dir or OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    path = out / "tick_resolution.json"
    path.write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    log = None
    if "--log" in args:
        i = args.index("--log")
        log = Path(args[i + 1])
        del args[i:i + 2]
    db = Path(args[0]) if args else Path(
        r"C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db")
    sys.exit(main(db, log_path=log))

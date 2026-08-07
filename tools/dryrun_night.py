"""라이브 리허설 1세션 자동 리포트 — 소유: W5.

**라이브 API를 호출하지 않는다.** collector(W4)가 이미 수집해 놓은 SQLite DB(read-only)와
로그 파일만 읽어 사후 분석한다 — 리허설이 끝난 뒤(또는 진행 중에도 안전하게) 아무나 실행할
수 있다. 실행 자체에는 라이브 리스가 필요 없다.

산출물 4가지 (docs/08_runbook.md §라이브 리허설 참고):
    1. 수집 커버리지 — 테이블별 심볼 수·행 수, candles_1m 은 세션 길이 대비 봉 채움률
    2. rate limit 여유 — 로그 기반 429 카운트(최선노력, W4 로그 포맷 확정 전엔 unavailable)
    3. 결측 구간 — 5분 이상 공백(캔들) / 예상 주기의 3배 이상 공백(랭킹·트레이드·호가)
    4. 이벤트 후보 목록 — `tossmon.analysis.labeling.detect_events` 재사용(중복 구현 금지).
       베이스라인 RVOL 없이 돌리므로 전부 `rvol_gated=False` — 가격 조건만의 1차 스크리닝이다.

사용법:
    python tools/dryrun_night.py --hours-back 8
    python tools/dryrun_night.py --start "2026-07-30T22:30:00+09:00" --end "2026-07-31T05:00:00+09:00"
    python tools/dryrun_night.py --out data/reports/dryrun_2026-07-30.md
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ops.healthcheck import scan_logs  # noqa: E402  (로그 스캔 재사용 — 중복 구현 금지)
from ops.opsconfig import load_ops_config  # noqa: E402
from tossmon.analysis.labeling import EventParams, detect_events  # noqa: E402
from tossmon.api.models import iso_to_ms, ms_to_iso_kst  # noqa: E402

# 세션 폴링 주기 기대값 — config/config.example.yaml 의 기본값과 일치(계약 C-9 단일 출처
# tossmon/config.py 가 아직 스텁이라, 여기서는 리포트용 상수로만 고정한다).
EXPECTED_INTERVAL_S = {
    "rankings_snap": 12,
    "trades_snap": 8,
    "orderbook_snap": 8,
}
GAP_FLAG_MULTIPLIER = 3          # 예상 주기의 몇 배 이상이면 "결측 구간"으로 표시
CANDLE_GAP_FLAG_MIN = 5          # 1분봉: 이 이상 공백이면 표시 (labeling.py 의 홀트 프록시와 동일 기준)
MIN_MS = 60_000
DAY_MS = 86_400_000


def now_ms() -> int:
    return int(time.time() * 1000)


def _ro_connect(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(db_path.resolve().as_posix(), safe='/:')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def load_window(conn: sqlite3.Connection, table: str, ts_col: str, columns: list[str],
                start_ms: int, end_ms: int) -> pd.DataFrame:
    cols = ", ".join(columns)
    q = f"SELECT {cols} FROM {table} WHERE {ts_col} BETWEEN ? AND ? ORDER BY {ts_col}"
    frame = pd.read_sql_query(q, conn, params=(start_ms, end_ms))
    if ts_col in frame.columns:
        frame[ts_col] = frame[ts_col].astype("int64")
    return frame


@dataclass
class SymbolGaps:
    symbol: str
    rows: int
    gaps: list[tuple[int, int, float]]  # (gap_start_ms, gap_end_ms, minutes)


def find_time_gaps(ts_values: list[int], flag_gap_ms: float) -> list[tuple[int, int, float]]:
    out = []
    for a, b in zip(ts_values[:-1], ts_values[1:]):
        if b - a >= flag_gap_ms:
            out.append((a, b, round((b - a) / 60000.0, 1)))
    return out


def candle_coverage(df: pd.DataFrame, start_ms: int, end_ms: int) -> dict[str, dict]:
    """symbol -> {bars, expected_minutes, coverage_pct, gaps}. 결측 분은 무체결로 정상(docs/06 §2)
    — CANDLE_GAP_FLAG_MIN 이상 공백만 "결측 구간"으로 표시한다."""
    out: dict[str, dict] = {}
    if df.empty:
        return out
    expected_minutes = max(1, (end_ms - start_ms) // MIN_MS)
    for symbol, g in df.groupby("symbol"):
        ts = sorted(int(t) for t in g["ts_ms"].tolist())
        gaps = find_time_gaps(ts, CANDLE_GAP_FLAG_MIN * MIN_MS)
        out[symbol] = {
            "bars": len(ts),
            "expected_minutes": int(expected_minutes),
            "coverage_pct": round(100.0 * len(ts) / expected_minutes, 1),
            "gaps": gaps,
        }
    return out


def snapshot_gaps(df: pd.DataFrame, ts_col: str, expected_interval_s: float,
                  symbol_col: str | None = None) -> dict[str, list[tuple[int, int, float]]]:
    """rankings/trades/orderbook — 심볼별(또는 전체) 예상 주기의 GAP_FLAG_MULTIPLIER배 이상 공백."""
    flag_ms = expected_interval_s * GAP_FLAG_MULTIPLIER * 1000
    out: dict[str, list[tuple[int, int, float]]] = {}
    if df.empty:
        return out
    if symbol_col and symbol_col in df.columns:
        groups = df.groupby(symbol_col)
    else:
        groups = [("(all)", df)]
    for key, g in groups:
        ts = sorted(int(t) for t in g[ts_col].tolist())
        gaps = find_time_gaps(ts, flag_ms)
        if gaps:
            out[str(key)] = gaps
    return out


@dataclass
class CoverageReport:
    table_counts: dict[str, int]
    candle_coverage: dict[str, dict]
    snapshot_gaps: dict[str, dict[str, list[tuple[int, int, float]]]]


def build_coverage(db_path: Path, start_ms: int, end_ms: int) -> CoverageReport:
    conn = _ro_connect(db_path)
    try:
        present = _existing_tables(conn)
        table_counts: dict[str, int] = {}
        cov: dict[str, dict] = {}
        snap_gaps: dict[str, dict] = {}

        if "candles_1m" in present:
            df = load_window(conn, "candles_1m", "ts_ms", ["symbol", "ts_ms"], start_ms, end_ms)
            table_counts["candles_1m"] = len(df)
            cov = candle_coverage(df, start_ms, end_ms)

        if "trades_snap" in present:
            df = load_window(conn, "trades_snap", "ts_ms", ["symbol", "ts_ms"], start_ms, end_ms)
            table_counts["trades_snap"] = len(df)
            snap_gaps["trades_snap"] = snapshot_gaps(
                df, "ts_ms", EXPECTED_INTERVAL_S["trades_snap"], symbol_col="symbol")

        if "orderbook_snap" in present:
            df = load_window(conn, "orderbook_snap", "snap_ms", ["symbol", "snap_ms"], start_ms, end_ms)
            table_counts["orderbook_snap"] = len(df)
            snap_gaps["orderbook_snap"] = snapshot_gaps(
                df, "snap_ms", EXPECTED_INTERVAL_S["orderbook_snap"], symbol_col="symbol")

        if "rankings_snap" in present:
            df = load_window(conn, "rankings_snap", "snap_ms", ["ranking_type", "snap_ms"],
                             start_ms, end_ms)
            table_counts["rankings_snap"] = len(df)
            snap_gaps["rankings_snap"] = snapshot_gaps(
                df, "snap_ms", EXPECTED_INTERVAL_S["rankings_snap"], symbol_col="ranking_type")

        return CoverageReport(table_counts=table_counts, candle_coverage=cov, snapshot_gaps=snap_gaps)
    finally:
        conn.close()


def find_event_candidates(db_path: Path, start_ms: int, end_ms: int,
                          params: EventParams | None = None) -> pd.DataFrame:
    """W3 의 detect_events 재사용. 베이스라인이 없어 rvol_series 는 넘기지 않는다
    → 전 이벤트 rvol_gated=False(A1 §6) — 가격 조건만의 1차 후보 목록이다."""
    params = params or EventParams()
    conn = _ro_connect(db_path)
    try:
        present = _existing_tables(conn)
        if "candles_1m" not in present:
            return pd.DataFrame()
        df = load_window(conn, "candles_1m", "ts_ms",
                         ["symbol", "ts_ms", "open_u", "high_u", "low_u", "close_u", "vol_qu"],
                         start_ms, end_ms)
    finally:
        conn.close()
    if df.empty:
        return df
    return detect_events(df, params)


def _build_tier_episodes(promo: pd.DataFrame, end_ms: int) -> list[dict]:
    """promotions 행렬(symbol,ts_ms,from_tier,to_tier,reason)을 심볼별 시간순으로 훑어
    "티어 T 로 승격(reason) → 그 티어에서 강등"을 하나의 에피소드(entry_ts, exit_ts)로 묶는다.
    리포트 종료 시점까지 강등되지 않은 에피소드는 exit_ts=end_ms, open=True 로 남긴다.
    """
    episodes: list[dict] = []
    for symbol, g in promo.groupby("symbol", sort=False):
        open_entries: dict[int, tuple[int, str]] = {}
        for row in g.itertuples():
            if row.to_tier > row.from_tier:
                open_entries[row.to_tier] = (row.ts_ms, row.reason)
            elif row.to_tier < row.from_tier:
                entry = open_entries.pop(row.from_tier, None)
                if entry is not None:
                    entry_ts, reason = entry
                    episodes.append({"symbol": symbol, "tier": row.from_tier, "reason": reason,
                                     "entry_ts": entry_ts, "exit_ts": row.ts_ms, "open": False})
        for tier, (entry_ts, reason) in open_entries.items():
            episodes.append({"symbol": symbol, "tier": tier, "reason": reason,
                             "entry_ts": entry_ts, "exit_ts": end_ms, "open": True})
    return episodes


def promotion_reason_stats(db_path: Path, start_ms: int, end_ms: int) -> pd.DataFrame:
    """reason 별 승격 건수, 그중 이벤트로 이어진 비율("정밀도"), 평균/중앙값 체류시간(분).

    "이벤트로 이어졌다" = 그 승격 에피소드 구간(entry_ts~exit_ts, 진행 중이면 end_ms까지)
    안에 같은 심볼의 events.t0_ms 가 하나라도 있다는 뜻이다(에피소드 단위 — 심볼 전체 이력에
    이벤트가 있었다는 것과는 다르다. 심볼당 여러 번 승격되면 어느 승격이 실제로 이벤트를
    수반했는지가 섞이지 않게 하기 위함).
    """
    conn = _ro_connect(db_path)
    try:
        promo = pd.read_sql_query(
            "SELECT symbol, ts_ms, from_tier, to_tier, reason FROM promotions "
            "WHERE ts_ms BETWEEN ? AND ? ORDER BY symbol, ts_ms",
            conn, params=(start_ms, end_ms))
        events = pd.read_sql_query(
            "SELECT symbol, t0_ms FROM events WHERE t0_ms BETWEEN ? AND ?",
            conn, params=(start_ms, end_ms))
    finally:
        conn.close()

    if promo.empty:
        return pd.DataFrame(columns=["reason", "promotions", "led_to_event_pct",
                                     "avg_residency_min", "median_residency_min",
                                     "residency_samples"])

    promo["ts_ms"] = promo["ts_ms"].astype("int64")
    event_ts_by_symbol = events.groupby("symbol")["t0_ms"].apply(list).to_dict() \
        if not events.empty else {}

    episodes = _build_tier_episodes(promo, end_ms)
    for ep in episodes:
        ev_list = event_ts_by_symbol.get(ep["symbol"], [])
        ep["had_event"] = any(ep["entry_ts"] <= t0 <= ep["exit_ts"] for t0 in ev_list)
        ep["duration_min"] = (ep["exit_ts"] - ep["entry_ts"]) / 60000.0

    ep_df = pd.DataFrame(episodes)
    reason_counts = promo[promo["to_tier"] > promo["from_tier"]]["reason"].value_counts()

    rows = []
    for reason, count in reason_counts.sort_values(ascending=False).items():
        sub = ep_df[ep_df["reason"] == reason] if not ep_df.empty else ep_df
        led_pct = round(100.0 * sub["had_event"].mean(), 1) if len(sub) else None
        avg_res = round(sub["duration_min"].mean(), 1) if len(sub) else None
        med_res = round(sub["duration_min"].median(), 1) if len(sub) else None
        rows.append({
            "reason": reason, "promotions": int(count), "led_to_event_pct": led_pct,
            "avg_residency_min": avg_res, "median_residency_min": med_res,
            "residency_samples": int(len(sub)),
        })
    return pd.DataFrame(rows)


def event_rvol_gate_breakdown(db_path: Path, start_ms: int, end_ms: int) -> dict:
    """events.meta_json 의 rvol_gated 값별 건수(계약 C-7 A1 §6 — 게이트 꺼진 이벤트는
    RVOL 통계를 오염시키므로 분리 보고해야 한다)."""
    import json

    conn = _ro_connect(db_path)
    try:
        present = _existing_tables(conn)
        if "events" not in present:
            return {"total": 0, "gated_true": 0, "gated_false": 0, "unknown": 0}
        rows = conn.execute(
            "SELECT meta_json FROM events WHERE t0_ms BETWEEN ? AND ?",
            (start_ms, end_ms)).fetchall()
    finally:
        conn.close()
    gated_true = gated_false = unknown = 0
    for (meta_raw,) in rows:
        gated = None
        if meta_raw:
            try:
                gated = json.loads(meta_raw).get("rvol_gated")
            except (ValueError, TypeError):
                gated = None
        if gated is True:
            gated_true += 1
        elif gated is False:
            gated_false += 1
        else:
            unknown += 1
    return {"total": len(rows), "gated_true": gated_true, "gated_false": gated_false,
           "unknown": unknown}


_KV_RE = re.compile(r"(\w+)=(\S+)")
_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})\s")


def _parse_log_ts(line: str) -> datetime | None:
    """로그 줄 맨 앞의 `%(asctime)s`(로컬 시각, 밀리초 콤마 구분)를 naive datetime 으로 파싱한다.

    시스템 로컬 시각(이 리허설 환경에서는 KST)이라는 전제 — UTC 변환은 하지 않는다. 이 리포트가
    쓰는 건 로그 안에서의 **상대 경과 시간**(세션 전환 간격, 재시작 공백)뿐이라 절대시간대
    변환이 필요 없다.
    """
    m = _LOG_TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)},{m.group(2)}", "%Y-%m-%d %H:%M:%S,%f")
    except ValueError:
        return None


def parse_telemetry_lines(log_dir: Path) -> list[dict]:
    """collector.log 의 'telemetry ...' 라인을 전부 파싱한다.

    형식(`tossmon/collector/loops.py:report_telemetry`): 로그 접두어(asctime+levelname) 뒤에
    ``telemetry k=v k=v ... | budget GROUP=measured/target GROUP=measured/target``.
    접두어 포맷에 의존하지 않도록 "telemetry " 와 " | budget " 마커로만 자른다("ts" 필드만
    별도로 접두어의 asctime 을 파싱).
    """
    log_path = log_dir / "collector.log"
    if not log_path.exists():
        return []
    out: list[dict] = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        idx = line.find("telemetry ")
        if idx == -1 or " | budget " not in line:
            continue
        rest = line[idx + len("telemetry "):]
        kv_part, budget_part = rest.split(" | budget ", 1)
        out.append({
            "ts": _parse_log_ts(line),
            "fields": dict(_KV_RE.findall(kv_part)),
            "budget": dict(_KV_RE.findall(budget_part)),
        })
    return out


_SESSION_TRANSITION_RE = re.compile(r"session (\w+) → (\w+)\s*$")


def parse_session_transitions(log_dir: Path) -> list[dict]:
    """`_session_tick`(loops.py)이 남기는 "session X → Y" 라인을 시각과 함께 추출한다."""
    log_path = log_dir / "collector.log"
    if not log_path.exists():
        return []
    out: list[dict] = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _SESSION_TRANSITION_RE.search(line)
        if not m:
            continue
        out.append({"ts": _parse_log_ts(line), "from": m.group(1), "to": m.group(2)})
    return out


def restart_gaps(log_dir: Path) -> list[dict]:
    """"collector start" 라인마다(최초 시작 제외) 직전 로그 활동과의 시간차를 잰다 —
    무계획/계획 재시작 각각의 공백(분)을 리포트에 정량적으로 남기기 위함."""
    log_path = log_dir / "collector.log"
    if not log_path.exists():
        return []
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    start_idxs = [i for i, line in enumerate(lines) if "collector start base_url=" in line]
    out: list[dict] = []
    for n, idx in enumerate(start_idxs):
        start_ts = _parse_log_ts(lines[idx])
        if n == 0:
            out.append({"restart_at": start_ts, "gap_min": None, "note": "최초 시작"})
            continue
        prev_ts = None
        for j in range(idx - 1, -1, -1):
            prev_ts = _parse_log_ts(lines[j])
            if prev_ts is not None:
                break
        gap_min = None
        if start_ts is not None and prev_ts is not None:
            gap_min = round((start_ts - prev_ts).total_seconds() / 60.0, 2)
        out.append({"restart_at": start_ts, "gap_min": gap_min, "note": f"재시작 #{n}"})
    return out


def budget_actual_summary(entries: list[dict]) -> dict[str, dict]:
    """텔레메트리에 실린 budget "measured/target" 문자열에서 그룹별 실측 범위를 뽑는다."""
    out: dict[str, dict] = {}
    for e in entries:
        for group, val in e.get("budget", {}).items():
            if "/" not in val:
                continue
            measured_s, target_s = val.split("/", 1)
            try:
                measured, target = float(measured_s), float(target_s)
            except ValueError:
                continue
            g = out.setdefault(group, {"min": measured, "max": measured, "target": target,
                                       "samples": 0})
            g["min"] = min(g["min"], measured)
            g["max"] = max(g["max"], measured)
            g["target"] = target
            g["samples"] += 1
    return out


def tape_gap_rate_by_session(entries: list[dict]) -> dict[str, dict]:
    """세션 레이블별로 누적 tape_gaps 의 (최댓값-최솟값)/경과분 을 낸다.

    entries 는 시간순(로그에 쓰인 순서)이라고 전제한다. 세션 레이블이 이 리허설에서 각각
    한 번씩만 등장했다면(pre→regular→after→closed→day) 그룹화만으로 충분하고, 세션이 같은
    이름으로 두 번 등장하면(예: 다음날 프리마켓) 이 함수는 그 구간들을 합쳐서 하나의 평균으로
    낸다 — 리포트에서 이 한계를 명시한다.
    """
    out: dict[str, dict] = {}
    for e in entries:
        session = e.get("fields", {}).get("session")
        ts = e.get("ts")
        gaps_raw = e.get("fields", {}).get("tape_gaps")
        if session is None or ts is None or gaps_raw is None:
            continue
        try:
            gaps = int(gaps_raw)
        except ValueError:
            continue
        g = out.setdefault(session, {"first_ts": ts, "last_ts": ts,
                                     "first_gaps": gaps, "last_gaps": gaps, "samples": 0})
        if ts < g["first_ts"]:
            g["first_ts"], g["first_gaps"] = ts, gaps
        if ts > g["last_ts"]:
            g["last_ts"], g["last_gaps"] = ts, gaps
        g["samples"] += 1
    result: dict[str, dict] = {}
    for session, g in out.items():
        span_min = (g["last_ts"] - g["first_ts"]).total_seconds() / 60.0
        delta = g["last_gaps"] - g["first_gaps"]
        rate = round(delta / span_min, 3) if span_min > 0 else None
        result[session] = {"delta_gaps": delta, "span_min": round(span_min, 1),
                           "rate_per_min": rate, "samples": g["samples"]}
    return result


_FAKE_BUDGET_ERROR_RE = re.compile(
    r"budget: (\w+) predicted ([\d.]+) req/s > target ([\d.]+)")


def count_fake_budget_errors(log_dir: Path) -> dict:
    """W4에 보고할 관측용 버그 카운트 — budget.py의 랭킹 초과 경보 비교 로직이 뒤집혀 있어
    predicted <= target 인데도 ERROR 로 찍히는 건수를 센다(W5 소유 아님, 수정하지 않는다)."""
    log_path = log_dir / "collector.log"
    if not log_path.exists():
        return {"total": 0, "fake": 0, "real": 0}
    total = fake = real = 0
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _FAKE_BUDGET_ERROR_RE.search(line)
        if not m:
            continue
        total += 1
        predicted, target = float(m.group(2)), float(m.group(3))
        if predicted <= target:
            fake += 1
        else:
            real += 1
    return {"total": total, "fake": fake, "real": real}


def count_collector_starts(log_dir: Path) -> int:
    """`tossmon/collector/__main__.py`의 시작 로그 라인 수 — 1개 초과면 세션 중 재시작이 있었다는 뜻."""
    log_path = log_dir / "collector.log"
    if not log_path.exists():
        return 0
    return log_path.read_text(encoding="utf-8", errors="replace").count("collector start base_url=")


@dataclass
class TelemetrySummary:
    samples: int
    last_fields: dict[str, str]
    last_budget: dict[str, str]
    max_precision_digits: int
    collector_starts: int


def summarize_telemetry(entries: list[dict], collector_starts: int) -> TelemetrySummary | None:
    if not entries:
        return None
    max_digits = 0
    for e in entries:
        try:
            max_digits = max(max_digits, int(e["fields"].get("precision_max_digits", 0)))
        except ValueError:
            continue
    return TelemetrySummary(
        samples=len(entries), last_fields=entries[-1]["fields"], last_budget=entries[-1]["budget"],
        max_precision_digits=max_digits, collector_starts=collector_starts,
    )


def render_markdown(start_ms: int, end_ms: int, coverage: CoverageReport,
                    events: pd.DataFrame, log_stats, generated_ms: int,
                    telemetry: TelemetrySummary | None = None,
                    session_transitions: list[dict] | None = None,
                    budget_summary: dict[str, dict] | None = None,
                    tape_gap_rates: dict[str, dict] | None = None,
                    promotion_stats: pd.DataFrame | None = None,
                    rvol_gate: dict | None = None,
                    fake_budget_errors: dict | None = None,
                    restarts: list[dict] | None = None,
                    data_caveat: str | None = None) -> str:
    lines = [
        "# 라이브 리허설 리포트",
        "",
    ]
    if data_caveat:
        lines += ["> ⚠️ **데이터 한계**: " + data_caveat, ""]
    lines += [
        f"- 세션 구간(KST): {ms_to_iso_kst(start_ms)} ~ {ms_to_iso_kst(end_ms)}",
        f"- 생성 시각(KST): {ms_to_iso_kst(generated_ms)}",
        "",
        "## 1. 수집 커버리지",
        "",
    ]
    if not coverage.table_counts:
        lines.append("(DB에 해당 구간 데이터 없음 — 리허설이 아직 실행되지 않았거나 시간 창이 어긋남)")
    else:
        for table, n in sorted(coverage.table_counts.items()):
            lines.append(f"- `{table}`: {n:,} rows")
        if coverage.candle_coverage:
            pct_values = [c["coverage_pct"] for c in coverage.candle_coverage.values()]
            lines.append("")
            lines.append(
                f"### candles_1m 채움률 요약 — {len(pct_values)}개 심볼, "
                f"평균 {sum(pct_values) / len(pct_values):.1f}%, "
                f"중앙값 {sorted(pct_values)[len(pct_values) // 2]:.1f}%"
            )
            lines.append(
                "> ⚠️ 이 window(15시간)에는 `closed` 세션(폴링 없음)이 포함돼 있어 "
                "coverage%가 세션 전체를 하나로 뭉뚱그리면 구조적으로 낮게 나온다 — "
                "심볼 간 **상대 비교**용으로만 쓸 것, 절대치로 \"수집이 부실하다\"고 "
                "읽지 말 것."
            )
            lines.append("")
            worst_n = 25
            worst = sorted(coverage.candle_coverage.items(), key=lambda kv: kv[1]["coverage_pct"])
            lines.append(f"#### 채움률 최하위 {min(worst_n, len(worst))}개 (전체 {len(worst)}개 중)")
            lines.append("")
            lines.append("| symbol | bars | expected_min | coverage% | 5분+ 공백 수 |")
            lines.append("|---|---:|---:|---:|---:|")
            for symbol, c in worst[:worst_n]:
                lines.append(f"| {symbol} | {c['bars']} | {c['expected_minutes']} | "
                             f"{c['coverage_pct']}% | {len(c['gaps'])} |")
            if len(worst) > worst_n:
                lines.append(f"\n_(나머지 {len(worst) - worst_n}개 심볼 생략 — coverage% 상위)_")

    lines += ["", "## 2. Rate limit 여유 (로그 기반, 최선노력)", ""]
    if log_stats.count_429 is None:
        lines.append(
            "- 429 카운트: unavailable — collector(W4) 로그 포맷이 아직 표준화되지 않았거나 "
            f"`{coverage_log_dir_note()}` 안에 최근 로그 파일이 없습니다. 오류 아님."
        )
    else:
        lines.append(f"- 429 카운트: {log_stats.count_429} (스캔 파일 {log_stats.files_scanned}개, "
                     f"창 {log_stats.window_s:.0f}s)")
        lines.append(f"- request 관련 로그 라인 수: {log_stats.request_lines}")

    lines += ["", "## 3. 결측 구간 (예상 주기 대비, 긴 공백 상위순)", ""]
    all_gaps: list[tuple[str, str, int, int, float]] = []
    for table, gap in coverage.snapshot_gaps.items():
        for key, gaps in gap.items():
            for a, b, mins in gaps:
                all_gaps.append((table, key, a, b, mins))
    for symbol, c in coverage.candle_coverage.items():
        for a, b, mins in c["gaps"]:
            all_gaps.append(("candles_1m", symbol, a, b, mins))
    if not all_gaps:
        lines.append("(표시할 만한 결측 구간 없음)")
    else:
        all_gaps.sort(key=lambda g: g[4], reverse=True)
        gap_cap = 40
        lines.append(f"- 총 결측 구간 {len(all_gaps)}건 — 가장 긴 {min(gap_cap, len(all_gaps))}건만 표시")
        lines.append("")
        for table, key, a, b, mins in all_gaps[:gap_cap]:
            lines.append(f"- `{table}`[{key}]: {ms_to_iso_kst(a)} ~ {ms_to_iso_kst(b)} "
                         f"({mins}분 공백)")
        if len(all_gaps) > gap_cap:
            lines.append(f"\n_(나머지 {len(all_gaps) - gap_cap}건 생략 — 공백 길이 짧은 순)_")

    lines += ["", "## 4. 이벤트 후보 목록 (가격 조건만, rvol_gated=False)", ""]
    if events is None or events.empty:
        lines.append("(후보 없음 — 이 창에서 급등 조건을 충족한 1분봉이 없음)")
    else:
        lines.append("| symbol | t0(KST) | kind | peak_ret | ret_30m | shape | outcome |")
        lines.append("|---|---|---|---:|---:|---|---|")
        for _, row in events.iterrows():
            t0 = ms_to_iso_kst(int(row["t0_ms"])) if pd.notna(row["t0_ms"]) else "?"
            lines.append(
                f"| {row.get('symbol', '?')} | {t0} | {row.get('kind', '?')} | "
                f"{row.get('peak_ret', float('nan')):.3f} | {row.get('ret_30m', float('nan')):.3f} | "
                f"{row.get('shape', '?')} | {row.get('outcome', '?')} |"
            )
        lines.append("")
        lines.append(
            "> 주의: 베이스라인 RVOL 없이 가격 조건만으로 검출했다(`rvol_gated=False`, "
            "계약 C-7 A1 §6). W3의 `evaluate.py`/analyzer 리포트로 RVOL 게이트를 적용한 "
            "정밀도 재검증을 권장한다."
        )

    lines += ["", "## 5. Collector 텔레메트리 (precision/budget/재시작)", ""]
    if telemetry is None:
        lines.append(
            "- collector.log에 `telemetry` 라인이 없다 — 세션이 아직 실행되지 않았거나, "
            "5분 주기가 아직 한 번도 안 돌았거나(세션 전환/종료 시 강제 출력도 되니 그마저도 "
            "없다면 collector가 시작조차 못 했을 가능성)."
        )
    else:
        f, b = telemetry.last_fields, telemetry.last_budget
        lines.append(f"- 텔레메트리 샘플 수: {telemetry.samples}")
        lines.append(f"- collector 시작 횟수(로그 기준): {telemetry.collector_starts}"
                     + (" — ⚠️ 세션 중 재시작 발생" if telemetry.collector_starts > 1 else " (재시작 없음)"))
        lines.append(f"- 마지막 세션/워치리스트: session={f.get('session')} watch={f.get('watch')} "
                     f"tier2={f.get('tier2')} tier3={f.get('tier3')}")
        lines.append(f"- 이벤트/승격/테이프갭/API에러(누적): events={f.get('events')} "
                     f"promotions={f.get('promotions')} tape_gaps={f.get('tape_gaps')} "
                     f"api_errors={f.get('api_errors')}")
        lines.append(f"- 정밀도(A4): precision_rounded={f.get('precision_rounded')} "
                     f"parsed={f.get('precision_parsed')} "
                     f"rounded_pct={f.get('precision_rounded_pct')}% "
                     f"max_digits(마지막)={f.get('precision_max_digits')} "
                     f"max_digits(세션 전체 최고치)={telemetry.max_precision_digits}")
        if b:
            lines.append("- 그룹별 rate limit 사용률(마지막 샘플, measured/target req/s):")
            for group, val in sorted(b.items()):
                lines.append(f"  - {group}: {val}")

    lines += ["", "## 6. 세션 전환 이력", ""]
    if not session_transitions:
        lines.append("(세션 전환 로그 없음)")
    else:
        for t in session_transitions:
            ts = t["ts"].isoformat(sep=" ", timespec="seconds") if t["ts"] else "?"
            lines.append(f"- {ts}: `{t['from']}` → `{t['to']}`")

    lines += ["", "## 7. 그룹별 예산 실측 vs 계산치", ""]
    if not budget_summary:
        lines.append("(텔레메트리 없음)")
    else:
        lines.append("| group | 실측 min/max (req/s) | target(70%) | 계산상 만석 상한(참고) |")
        lines.append("|---|---|---:|---:|")
        calc_ref = {"MARKET_DATA": "6.42 (tier3 20/20 만석 기준)",
                    "MARKET_DATA_CHART": "2.73 (tier2 300/300 만석 기준)",
                    "RANKING": "0.25 (3종/12s, 사실상 고정)"}
        for group, g in sorted(budget_summary.items()):
            lines.append(f"| {group} | {g['min']:.2f} ~ {g['max']:.2f} | {g['target']:.2f} | "
                         f"{calc_ref.get(group, 'n/a')} |")
        lines.append("")
        lines.append(
            "> 실측이 계산상 만석 상한보다 훨씬 낮은 것은 모순이 아니다 — 계산은 tier2/tier3가 "
            "**정원을 다 채웠을 때**의 상한이고(`CollectorContext.refresh_plan`이 의도적으로 "
            "정원 기준 계획을 세운다 — \"최악 케이스를 미리 막는다\"), 이번 리허설에서는 "
            "유니버스가 그 정원을 채우지 못했다(§8 tier3 체류 이력 참고)."
        )

    lines += ["", "## 8. 승격 사유별 정밀도·체류시간", ""]
    if promotion_stats is None or promotion_stats.empty:
        lines.append("(승격 기록 없음)")
    else:
        lines.append("| reason | 승격 수 | 이벤트로 이어진 비율 | 평균 체류(분) | 중앙값 체류(분) | 표본 수 |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for _, r in promotion_stats.iterrows():
            led = "n/a" if r["led_to_event_pct"] is None else f"{r['led_to_event_pct']}%"
            avg = "n/a" if r["avg_residency_min"] is None else f"{r['avg_residency_min']}"
            med = "n/a" if r["median_residency_min"] is None else f"{r['median_residency_min']}"
            lines.append(f"| {r['reason']} | {r['promotions']:,} | {led} | {avg} | {med} | "
                         f"{r['residency_samples']:,} |")
        lines.append("")
        lines.append(
            '> "이벤트로 이어진 비율"은 그 승격이 만든 **개별 티어 체류 구간(에피소드)** 안에 '
            "같은 심볼의 이벤트가 있었는지를 본다(심볼 전체 이력이 아니라 그 승격 건 자체 기준). "
            "체류시간은 강등(from_tier=해당 티어)으로 에피소드가 닫힌 것만 계산했고, 리포트 "
            "종료 시점까지 안 닫힌 에피소드는 종료 시각까지로 잘라 표본에 포함했다."
        )

    lines += ["", "## 9. 이벤트 rvol_gated 분리", ""]
    if rvol_gate is None or rvol_gate["total"] == 0:
        lines.append("(이 구간에 기록된 이벤트 없음)")
    else:
        lines.append(f"- 총 이벤트: {rvol_gate['total']}")
        lines.append(f"- `rvol_gated=true`(RVOL 게이트 적용됨, 통계에 넣어도 안전): "
                     f"{rvol_gate['gated_true']}")
        lines.append(f"- `rvol_gated=false`(게이트 미적용 — 계약 A1 §6에 따라 집계에서 "
                     f"제외하거나 분리 보고해야 함): {rvol_gate['gated_false']}")
        if rvol_gate["unknown"]:
            lines.append(f"- 판정 불명(meta_json 파싱 실패 등): {rvol_gate['unknown']}")

    lines += ["", "## 10. 재시작 이력", ""]
    if not restarts:
        lines.append("(재시작 없음 — 최초 시작만 있음)")
    else:
        for r in restarts:
            ts = r["restart_at"].isoformat(sep=" ", timespec="seconds") if r["restart_at"] else "?"
            gap = "—" if r["gap_min"] is None else f"공백 {r['gap_min']}분"
            lines.append(f"- {ts} ({r['note']}, {gap})")

    lines += ["", "## 11. 테이프 갭 세션별 발생률", ""]
    if not tape_gap_rates:
        lines.append("(텔레메트리에 세션/tape_gaps 필드 없음)")
    else:
        lines.append("| session | Δtape_gaps | 구간(분) | 분당 발생률 | 샘플 수 |")
        lines.append("|---|---:|---:|---:|---:|")
        for session, g in tape_gap_rates.items():
            rate = "n/a" if g["rate_per_min"] is None else f"{g['rate_per_min']}"
            lines.append(f"| {session} | {g['delta_gaps']} | {g['span_min']} | {rate} | "
                         f"{g['samples']} |")

    lines += ["", "## 12. 알려진 버그 — BudgetGuard RANKING 비교 로직 역전 (W4 소유, 참고용)", ""]
    if fake_budget_errors is None or fake_budget_errors["total"] == 0:
        lines.append("(해당 로그 패턴 없음)")
    else:
        lines.append(
            f"- `budget: RANKING predicted X req/s > target Y` 패턴 총 {fake_budget_errors['total']}건 "
            f"중 **{fake_budget_errors['fake']}건이 predicted ≤ target인데도 ERROR로 찍힌 가짜 경보**"
            f"(나머지 {fake_budget_errors['real']}건은 실제로 predicted > target)."
        )
        lines.append(
            "> 무인 운영에서 가짜 ERROR는 경보 무시 습관을 만든다(healthcheck의 429 오탐과 같은 "
            "부류의 문제). 이 워커의 소유 코드가 아니므로 수정하지 않고 빈도만 기록했다 — "
            "코디네이터가 W4에 전달 예정."
        )

    return "\n".join(lines) + "\n"


_LOG_DIR_NOTE = ["data/logs"]


def coverage_log_dir_note() -> str:
    return _LOG_DIR_NOTE[0]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=None, help="ops_config.yaml 경로 (db_path/log_dir 참조)")
    ap.add_argument("--db", default=None, help="db 경로 (미지정 시 ops_config의 db_path)")
    ap.add_argument("--start", default=None, help="세션 시작 ISO8601(+오프셋). --hours-back 와 배타적")
    ap.add_argument("--end", default=None, help="세션 종료 ISO8601(+오프셋). 미지정 시 현재 시각")
    ap.add_argument("--hours-back", type=float, default=8.0,
                    help="현재 시각 기준 몇 시간 전부터 볼지 (기본 8h — 정규장 6.5h 커버)")
    ap.add_argument("--out", default=None, help="마크다운 저장 경로 (미지정 시 stdout)")
    ap.add_argument("--data-caveat", default=None,
                    help="리포트 서두에 넣을 데이터 한계 문구 (예: 유니버스 필터 미적용 경고)")
    args = ap.parse_args(argv)

    cfg = load_ops_config(args.config)
    db_path = Path(args.db) if args.db else cfg.db_path
    _LOG_DIR_NOTE[0] = str(cfg.log_dir)

    now = now_ms()
    end_ms = iso_to_ms(args.end) if args.end else now
    start_ms = iso_to_ms(args.start) if args.start else end_ms - int(args.hours_back * 3600_000)

    coverage = build_coverage(db_path, start_ms, end_ms)
    events = find_event_candidates(db_path, start_ms, end_ms)
    log_stats = scan_logs(cfg.log_dir, window_s=max(1.0, (end_ms - start_ms) / 1000.0))
    telemetry_entries = parse_telemetry_lines(cfg.log_dir)
    telemetry = summarize_telemetry(telemetry_entries, count_collector_starts(cfg.log_dir))
    session_transitions = parse_session_transitions(cfg.log_dir)
    budget_summary = budget_actual_summary(telemetry_entries)
    tape_gap_rates = tape_gap_rate_by_session(telemetry_entries)
    promotion_stats = promotion_reason_stats(db_path, start_ms, end_ms)
    rvol_gate = event_rvol_gate_breakdown(db_path, start_ms, end_ms)
    fake_budget_errors = count_fake_budget_errors(cfg.log_dir)
    restarts = restart_gaps(cfg.log_dir)

    report = render_markdown(
        start_ms, end_ms, coverage, events, log_stats, now, telemetry,
        session_transitions=session_transitions, budget_summary=budget_summary,
        tape_gap_rates=tape_gap_rates, promotion_stats=promotion_stats, rvol_gate=rvol_gate,
        fake_budget_errors=fake_budget_errors, restarts=restarts, data_caveat=args.data_caveat,
    )

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        print(f"wrote {out_path}", file=sys.stderr)
    else:
        for stream in (sys.stdout,):
            try:
                stream.reconfigure(encoding="utf-8")
            except (AttributeError, ValueError):
                pass
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

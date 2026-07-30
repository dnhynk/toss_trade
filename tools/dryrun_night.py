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


_KV_RE = re.compile(r"(\w+)=(\S+)")


def parse_telemetry_lines(log_dir: Path) -> list[dict]:
    """collector.log 의 'telemetry ...' 라인을 전부 파싱한다.

    형식(`tossmon/collector/loops.py:report_telemetry`): 로그 접두어(asctime+levelname) 뒤에
    ``telemetry k=v k=v ... | budget GROUP=measured/target GROUP=measured/target``.
    접두어 포맷에 의존하지 않도록 "telemetry " 와 " | budget " 마커로만 자른다.
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
            "fields": dict(_KV_RE.findall(kv_part)),
            "budget": dict(_KV_RE.findall(budget_part)),
        })
    return out


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
                    telemetry: TelemetrySummary | None = None) -> str:
    lines = [
        "# 라이브 리허설 리포트",
        "",
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
            lines.append("")
            lines.append("### candles_1m 채움률 (심볼별)")
            lines.append("")
            lines.append("| symbol | bars | expected_min | coverage% | 5분+ 공백 수 |")
            lines.append("|---|---:|---:|---:|---:|")
            for symbol, c in sorted(coverage.candle_coverage.items()):
                lines.append(f"| {symbol} | {c['bars']} | {c['expected_minutes']} | "
                             f"{c['coverage_pct']}% | {len(c['gaps'])} |")

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

    lines += ["", "## 3. 결측 구간 (예상 주기 대비)", ""]
    any_gap = False
    for table, gap in coverage.snapshot_gaps.items():
        for key, gaps in gap.items():
            any_gap = True
            for a, b, mins in gaps[:10]:
                lines.append(f"- `{table}`[{key}]: {ms_to_iso_kst(a)} ~ {ms_to_iso_kst(b)} "
                             f"({mins}분 공백)")
    for symbol, c in coverage.candle_coverage.items():
        for a, b, mins in c["gaps"][:10]:
            any_gap = True
            lines.append(f"- `candles_1m`[{symbol}]: {ms_to_iso_kst(a)} ~ {ms_to_iso_kst(b)} "
                         f"({mins}분 공백)")
    if not any_gap:
        lines.append("(표시할 만한 결측 구간 없음)")

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

    report = render_markdown(start_ms, end_ms, coverage, events, log_stats, now, telemetry)

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

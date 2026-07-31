"""헬스체크 — 소유: W5.

한 화면에 (a) 테이블별 마지막 수집 시각, (b) DB 증가율(행/초), (c) rate limit
429 카운트·초당 호출수(가능한 경우), (d) 디스크 여유 공간을 보여준다.

설계 원칙: **라이브 API를 호출하지 않는다.** 로컬 SQLite(read-only)와 로그 파일,
디스크 사용량만 읽는다 — 리스 없는 워커가 실행해도 안전하고, 리스 보유 중에도
수집 프로세스와 경합하지 않는다 (Store 계약 C-6: 분석/리포트는 read-only URI만 사용).

429 카운트·요청 관련 로그 라인 수는 collector(W4)의 `collector.log`를 스캔한 것이다
(형식은 `tossmon/collector/budget.py`의 `"budget: 429 on <group>"` — 계약 A4/텔레메트리,
main 6b6fb4a). 로그 파일이 아직 없으면(수집 미시작 등) "unavailable"로 표시된다 —
오류가 아니라 정상적인 저하 동작이다.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from .opsconfig import OpsConfig, load_ops_config

# 계약 C-6 테이블 → 시간 컬럼. events/promotions/candles_1d 는 실시간 수집 진행 여부의 보조
# 지표일 뿐 — 아래 POLLING_TABLES 로 "상시 폴링돼야 하는" 테이블과 구분한다.
TABLES_TS: dict[str, str] = {
    "candles_1m": "ts_ms",
    "candles_1d": "ts_ms",
    "trades_snap": "ts_ms",
    "rankings_snap": "snap_ms",
    "orderbook_snap": "snap_ms",
    "events": "t0_ms",
    "promotions": "ts_ms",
}

# overall_status 를 좌우하는(=CRIT/WARN 게이트) 테이블. 세션이 열려 있으면 계속 갱신돼야 한다.
# candles_1d 는 승격 직후 1회만 갱신되는 베이스라인이고(tossmon/collector/loops.py
# `_refresh_baseline`), events/promotions 는 실제 이벤트/승격이 있을 때만 생기는 **사건 로그**다
# — "한동안 새 이벤트가 없다"는 정상이지 장애가 아니다. 이 셋을 게이트에 넣으면 항상 오탐
# CRIT 가 뜬다(라이브 리허설에서 실제로 발견 — docs/11 §5).
POLLING_TABLES = {"candles_1m", "trades_snap", "rankings_snap", "orderbook_snap"}

# collector 로그에서 429/요청 수를 세는 패턴. W4 로그 포맷 확정(계약, main 6b6fb4a):
# BudgetGuard.on_429() 가 429를 만나면 정확히 "budget: 429 on <group>" 로 남긴다
# (tossmon/collector/budget.py). 예전에는 `\b429\b` 단독 매칭이었는데, 라이브 리허설에서
# 실제로 오탐이 났다 — 밀리초 타임스탬프(`22:13:16,429`)나 epoch ms(`t0_ms=...429...`)에
# 우연히 "429"가 들어간 숫자를 전부 429 이벤트로 셌다. "budget: 429 on" 처럼 실제로 이
# 문맥에서만 나오는 문자열로 좁힌다.
RE_429 = re.compile(r"budget: 429 on\b|RateLimited|rate.?limit.?exceeded", re.IGNORECASE)
RE_REQUEST = re.compile(r"\brequest(ed|s)?\b", re.IGNORECASE)

STATUS_OK, STATUS_WARN, STATUS_CRIT = "OK", "WARN", "CRIT"
_EXIT_CODE = {STATUS_OK: 0, STATUS_WARN: 1, STATUS_CRIT: 2}


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


@dataclass
class TableStat:
    count: int
    last_ts_ms: int | None


def collect_db_stats(db_path: Path) -> dict[str, TableStat] | None:
    """DB가 없으면 None(정상 — 아직 수집 미시작). 있으면 테이블별 count/last_ts."""
    if not db_path.exists():
        return None
    conn = _ro_connect(db_path)
    try:
        present = _existing_tables(conn)
        out: dict[str, TableStat] = {}
        for table, col in TABLES_TS.items():
            if table not in present:
                continue
            row = conn.execute(f"SELECT COUNT(*), MAX({col}) FROM {table}").fetchone()
            out[table] = TableStat(count=row[0] or 0, last_ts_ms=row[1])
        return out
    finally:
        conn.close()


@dataclass
class GrowthRate:
    rows_per_sec: float
    window_s: float


def compute_growth(
    current: dict[str, TableStat], state_path: Path, now: int
) -> dict[str, GrowthRate | None]:
    """이전 실행 스냅샷(state_path)과 비교해 테이블별 증가율(행/초)을 낸다.

    첫 실행이거나 시계가 뒤로 간 경우(재부팅/서머타임 등)는 None으로 스킵한다.
    """
    prev_counts: dict[str, int] = {}
    prev_ts = None
    if state_path.exists():
        try:
            snap = json.loads(state_path.read_text(encoding="utf-8"))
            prev_counts = snap.get("counts", {})
            prev_ts = snap.get("ts_ms")
        except (ValueError, OSError):
            prev_counts, prev_ts = {}, None

    out: dict[str, GrowthRate | None] = {}
    if prev_ts is not None and now > prev_ts:
        window_s = (now - prev_ts) / 1000.0
        for table, stat in current.items():
            before = prev_counts.get(table)
            if before is None or window_s <= 0:
                out[table] = None
                continue
            delta = stat.count - before
            out[table] = GrowthRate(rows_per_sec=max(delta, 0) / window_s, window_s=window_s)
    else:
        out = {table: None for table in current}

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"ts_ms": now, "counts": {t: s.count for t, s in current.items()}}),
        encoding="utf-8",
    )
    return out


@dataclass
class LogStats:
    count_429: int | None
    request_lines: int | None
    files_scanned: int
    window_s: float


def scan_logs(log_dir: Path, window_s: float = 300.0, now: float | None = None) -> LogStats:
    """최근 window_s 내 수정된 로그를 훑어 429/요청 라인을 센다 (최선노력, 포맷 미확정 대비).

    collector가 아직 표준 로그를 남기지 않으면(파일 없음) count_429/request_lines는
    None으로 "unavailable"을 표시한다.
    """
    now = time.time() if now is None else now
    if not log_dir.exists():
        return LogStats(count_429=None, request_lines=None, files_scanned=0, window_s=window_s)

    files = [
        p for p in log_dir.glob("*.log") if p.is_file() and (now - p.stat().st_mtime) <= window_s
    ]
    if not files:
        return LogStats(count_429=None, request_lines=None, files_scanned=0, window_s=window_s)

    c429 = 0
    creq = 0
    for p in files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        c429 += len(RE_429.findall(text))
        creq += len(RE_REQUEST.findall(text))
    return LogStats(count_429=c429, request_lines=creq, files_scanned=len(files), window_s=window_s)


@dataclass
class DiskStat:
    path: str
    free_gb: float
    total_gb: float
    status: str


def check_disk(paths: list[Path], warn_free_gb: float, critical_free_gb: float) -> list[DiskStat]:
    seen_roots: set[str] = set()
    out: list[DiskStat] = []
    for p in paths:
        p = p.resolve()
        # 존재하지 않는 하위 경로라도 드라이브 사용량은 조회 가능하도록 상위로 거슬러 올라간다.
        probe = p
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        usage = shutil.disk_usage(str(probe))
        root = str(probe.drive if hasattr(probe, "drive") and probe.drive else probe.anchor)
        key = root or str(probe)
        if key in seen_roots:
            continue
        seen_roots.add(key)
        free_gb = usage.free / (1024**3)
        total_gb = usage.total / (1024**3)
        if free_gb < critical_free_gb:
            status = STATUS_CRIT
        elif free_gb < warn_free_gb:
            status = STATUS_WARN
        else:
            status = STATUS_OK
        out.append(DiskStat(path=str(p), free_gb=round(free_gb, 2), total_gb=round(total_gb, 2),
                             status=status))
    return out


def staleness_status(last_ts_ms: int | None, now: int, warn_min: int, crit_min: int) -> tuple[str, float | None]:
    """last_ts_ms is None(테이블에 행이 아직 없음)은 WARN이 아니라 OK — 수집 미시작/휴장일 수
    있으므로 운영자가 docs/08 세션 시간표로 판단한다(오탐 방지). 데이터가 있었다가 갱신이
    멈춘 경우만 나이로 WARN/CRIT 판정한다."""
    if last_ts_ms is None:
        return STATUS_OK, None
    age_min = (now - last_ts_ms) / 60000.0
    if age_min >= crit_min:
        return STATUS_CRIT, age_min
    if age_min >= warn_min:
        return STATUS_WARN, age_min
    return STATUS_OK, age_min


@dataclass
class Report:
    generated_ms: int
    db_present: bool
    tables: dict[str, dict]
    disks: list[DiskStat]
    logs: LogStats
    overall_status: str


def worse(a: str, b: str) -> str:
    order = {STATUS_OK: 0, STATUS_WARN: 1, STATUS_CRIT: 2}
    return a if order[a] >= order[b] else b


def build_report(cfg: OpsConfig, now: int | None = None) -> Report:
    now = now_ms() if now is None else now
    overall = STATUS_OK

    db_stats = collect_db_stats(cfg.db_path)
    tables: dict[str, dict] = {}
    if db_stats is None:
        overall = worse(overall, STATUS_WARN)
    else:
        growth = compute_growth(db_stats, cfg.state_dir / "healthcheck_prev.json", now)
        for table, stat in db_stats.items():
            status, age_min = staleness_status(
                stat.last_ts_ms, now, cfg.stale_minutes_warn, cfg.stale_minutes_critical
            )
            is_gating = table in POLLING_TABLES
            if is_gating:
                overall = worse(overall, status)
            g = growth.get(table)
            tables[table] = {
                "count": stat.count,
                "last_ts_ms": stat.last_ts_ms,
                "age_min": None if age_min is None else round(age_min, 1),
                "rows_per_sec": None if g is None else round(g.rows_per_sec, 3),
                # status 는 실제 나이 기준 판정 그대로 보여준다(정보 가치가 있다) — 다만
                # gates_overall=False 인 테이블(candles_1d/events/promotions)은 이 값이
                # CRIT/WARN 이어도 overall_status 를 끌어올리지 않는다("사건이 뜸하다"는
                # 정상이지 장애가 아니다 — 라이브 리허설에서 실제로 오탐을 낸 뒤 수정, docs/11 §5).
                "status": status,
                "gates_overall": is_gating,
            }

    disks = check_disk(
        [cfg.db_path, cfg.log_dir], cfg.disk.warn_free_gb, cfg.disk.critical_free_gb
    )
    for d in disks:
        overall = worse(overall, d.status)

    logs = scan_logs(cfg.log_dir)

    return Report(
        generated_ms=now,
        db_present=db_stats is not None,
        tables=tables,
        disks=disks,
        logs=logs,
        overall_status=overall,
    )


def render_text(r: Report) -> str:
    lines = [
        f"=== tossmon healthcheck @ {r.generated_ms} ===  overall: {r.overall_status}",
        "",
    ]
    if not r.db_present:
        lines.append("DB: (없음 — 아직 수집 미시작)")
    else:
        lines.append(f"{'table':<16}{'count':>10}{'age(min)':>10}{'rows/s':>10}{'status':>8}")
        for table, s in sorted(r.tables.items()):
            age = "n/a" if s["age_min"] is None else f"{s['age_min']:.1f}"
            rps = "n/a" if s["rows_per_sec"] is None else f"{s['rows_per_sec']:.2f}"
            note = "" if s.get("gates_overall", True) else "  (정보용 — overall 미반영)"
            lines.append(f"{table:<16}{s['count']:>10}{age:>10}{rps:>10}{s['status']:>8}{note}")
    lines.append("")
    c429 = "unavailable" if r.logs.count_429 is None else str(r.logs.count_429)
    creq = "unavailable" if r.logs.request_lines is None else str(r.logs.request_lines)
    lines.append(
        f"logs: 429={c429} request_lines={creq} "
        f"(files={r.logs.files_scanned}, window={r.logs.window_s:.0f}s)"
    )
    lines.append("")
    for d in r.disks:
        lines.append(f"disk[{d.path}]: free={d.free_gb:.1f}GB / {d.total_gb:.1f}GB  status={d.status}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=None, help="ops_config.yaml 경로")
    ap.add_argument("--json", action="store_true", help="JSON 출력 (자동화용)")
    args = ap.parse_args(argv)

    cfg = load_ops_config(args.config)
    report = build_report(cfg)

    try:  # Windows 콘솔 기본 cp949 대응 (tools/live_probe.py 와 동일 패턴)
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    if args.json:
        payload = {
            "generated_ms": report.generated_ms,
            "db_present": report.db_present,
            "tables": report.tables,
            "disks": [d.__dict__ for d in report.disks],
            "logs": report.logs.__dict__,
            "overall_status": report.overall_status,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))

    return _EXIT_CODE[report.overall_status]


if __name__ == "__main__":
    raise SystemExit(main())

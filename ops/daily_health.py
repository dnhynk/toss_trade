"""일일 무결성 점검 — 소유: W5 (하드닝 스펙 coordination/specs/w5_5day_hardening.md §6).

하루 1회(작업 스케줄러 tossmon-dailyhealth, 08:52 KST — 폐장 직후 조용한 구간)
전날 수집분(KST 전일 09:00 → 당일 08:50)의 랭킹 스냅 간격·봉 수·이벤트 수를 요약해
`data/daily_health_<YYYYMMDD>.txt` 로 남긴다. 데이터 품질이 조용히 나빠지는 것을
사후에라도 알 수 있게 하는 것이 목적 — 경보가 아니라 기록이다(경보는 watchdog.ps1).

원칙: 라이브 API 호출 없음, DB 는 read-only URI 로만(계약 C-6), 실패해도 요약 파일은
반드시 남긴다(부분 실패를 파일 안에 기록).
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from .healthcheck import _ro_connect
from .opsconfig import load_ops_config

# 수집 하루의 경계(KST): 주간장 개시 09:00 ~ 다음날 폐장 08:50
DAY_START_H = 9


def window_for(date_str: str | None, now: float | None = None) -> tuple[int, int, str]:
    """(start_ms, end_ms, label). date_str(YYYYMMDD)은 '수집일이 끝나는 날' — 기본 오늘."""
    if date_str:
        end_day = datetime.strptime(date_str, "%Y%m%d")
    else:
        end_day = datetime.fromtimestamp(time.time() if now is None else now)
    end = end_day.replace(hour=8, minute=50, second=0, microsecond=0)
    start = (end_day - timedelta(days=1)).replace(hour=DAY_START_H, minute=0, second=0,
                                                  microsecond=0)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000), end.strftime("%Y%m%d")


def q1(conn, sql: str, params=()) -> int | None:
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    except Exception as e:  # noqa: BLE001 — 요약은 부분 실패를 삼키고 기록한다
        return None


def rankings_gaps(conn, start_ms: int, end_ms: int) -> tuple[int, float | None, float | None]:
    """(고유 폴링 시각 수, 최대 간격 분, 중앙값 간격 초). 실패 시 (0, None, None)."""
    try:
        rows = conn.execute(
            "SELECT DISTINCT snap_ms FROM rankings_snap WHERE snap_ms BETWEEN ? AND ? "
            "ORDER BY snap_ms", (start_ms, end_ms)).fetchall()
    except Exception:
        return 0, None, None
    ts = [r[0] for r in rows]
    if len(ts) < 2:
        return len(ts), None, None
    gaps = [(b - a) / 1000.0 for a, b in zip(ts, ts[1:])]
    gaps_sorted = sorted(gaps)
    return len(ts), round(max(gaps) / 60.0, 1), round(gaps_sorted[len(gaps_sorted) // 2], 1)


def build_summary(cfg, date_str: str | None) -> str:
    start_ms, end_ms, label = window_for(date_str)
    lines = [
        f"=== tossmon daily health {label} ===",
        f"window (KST): {datetime.fromtimestamp(start_ms/1000):%Y-%m-%d %H:%M} "
        f"~ {datetime.fromtimestamp(end_ms/1000):%Y-%m-%d %H:%M}",
        f"generated: {datetime.now():%Y-%m-%d %H:%M:%S}",
        "",
    ]
    try:
        conn = _ro_connect(cfg.db_path)
    except Exception as e:  # noqa: BLE001
        lines.append(f"DB OPEN FAILED: {e!r} — 수집 프로세스/디스크 상태를 확인할 것")
        return "\n".join(lines)
    try:
        n_snap = q1(conn, "SELECT COUNT(*) FROM rankings_snap WHERE snap_ms BETWEEN ? AND ?",
                    (start_ms, end_ms))
        n_polls, max_gap_min, med_gap_s = rankings_gaps(conn, start_ms, end_ms)
        n_1m = q1(conn, "SELECT COUNT(*) FROM candles_1m WHERE ts_ms BETWEEN ? AND ?",
                  (start_ms, end_ms))
        n_tr = q1(conn, "SELECT COUNT(*) FROM trades_snap WHERE ts_ms BETWEEN ? AND ?",
                  (start_ms, end_ms))
        n_ob = q1(conn, "SELECT COUNT(*) FROM orderbook_snap WHERE snap_ms BETWEEN ? AND ?",
                  (start_ms, end_ms))
        n_ev = q1(conn, "SELECT COUNT(*) FROM events WHERE t0_ms BETWEEN ? AND ?",
                  (start_ms, end_ms))
        n_pr = q1(conn, "SELECT COUNT(*) FROM promotions WHERE ts_ms BETWEEN ? AND ?",
                  (start_ms, end_ms))
        lines += [
            f"rankings_snap rows : {n_snap}",
            f"  poll timestamps  : {n_polls}  max_gap={max_gap_min}min  median_gap={med_gap_s}s",
            f"candles_1m rows    : {n_1m}",
            f"trades_snap rows   : {n_tr}",
            f"orderbook_snap rows: {n_ob}",
            f"events             : {n_ev}",
            f"promotions rows    : {n_pr}",
        ]
        if n_snap == 0:
            lines.append("!! rankings_snap 0건 — 주말/휴장이 아니라면 수집 장애 흔적")
        if max_gap_min is not None and max_gap_min > 30:
            lines.append(f"!! 랭킹 폴링 최대 공백 {max_gap_min}분 — 세션 전환(정상) 또는 "
                         "장애 구간인지 collector.log/ALERT 파일과 대조할 것")
    finally:
        conn.close()

    # 마지막 텔레메트리 스냅샷 + 최근 24h ALERT 파일 목록 (로그/파일 기반, DB 무관)
    log_path = cfg.log_dir / "collector.log"
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
        tele = [ln for ln in tail if "telemetry session=" in ln]
        if tele:
            lines += ["", "last telemetry:", "  " + tele[-1].strip()]
    except OSError:
        lines.append("collector.log unreadable")
    try:
        day_ago = time.time() - 86400
        alerts = sorted(p.name for p in cfg.log_dir.glob("ALERT_*.txt")
                        if p.stat().st_mtime >= day_ago)
        lines.append("")
        lines.append(f"ALERT files (24h): {len(alerts)}")
        lines += [f"  {a}" for a in alerts]
    except OSError:
        pass
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=None)
    ap.add_argument("--date", default=None, help="YYYYMMDD (수집일이 끝나는 날, 기본 오늘)")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Windows 콘솔 cp949 대응
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    cfg = load_ops_config(args.config)
    text = build_summary(cfg, args.date)
    _, _, label = window_for(args.date)
    out = cfg.log_dir / f"daily_health_{label}.txt"
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

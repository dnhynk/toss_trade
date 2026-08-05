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

from . import gap_audit as GA
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


def rankings_gaps(conn, start_ms: int, end_ms: int) -> tuple[int, dict | None, float | None]:
    """(고유 폴링 시각 수, 공백 요약, 중앙값 간격 초). 실패 시 (0, None, None).

    **간격만 세지 않는다.** 예전 이 함수는 연속한 두 폴의 차이 중 최대만 `max_gap` 이라는
    이름으로 냈다. 그러면 구멍이 창 가장자리에 있을 때 비교할 다음 폴이 창 밖이라 간격이
    아예 안 만들어지고, 리포트는 이상 없다고 말한다. 08-05 아침에 그렇게 됐다 — 08:01:46
    에 수집이 멈추고 창은 08:50 에 끝났는데 리포트에는 `max_gap=3.4min` 이 찍혔고, 진짜
    공백 48.2분은 어디에도 없었다. 이 파일은 아침에 **가장 먼저 읽히는** 문서다.

    그래서 계산 자체를 `gap_audit.edge_holes` 로 옮겼다 — 두 도구가 같은 정의를 쓰게
    하려는 것이다. 여기서 다시 구현하면 한쪽만 고쳐지는 날이 온다.
    """
    try:
        rows = conn.execute(
            "SELECT DISTINCT snap_ms FROM rankings_snap WHERE snap_ms BETWEEN ? AND ? "
            "ORDER BY snap_ms", (start_ms, end_ms)).fetchall()
    except Exception:
        return 0, None, None
    eh = GA.edge_holes([r[0] for r in rows], start_ms, end_ms)
    return eh["n_obs"], eh, eh["inter_poll"]["p50"]


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
        n_polls, edge, med_gap_s = rankings_gaps(conn, start_ms, end_ms)
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
            f"  poll timestamps  : {n_polls}  median_gap={med_gap_s}s",
        ]
        # `max_gap` 이라는 이름을 없앴다. 값이 "관측 사이 최대 간격"인데 이름은 "최대
        # 공백"으로 읽혀서, 창 끝 48.2분을 3.4분이라고 보고했다. 이제 `max_hole`(가장자리
        # 포함)이 먼저 나오고, 그것이 무엇으로 이루어졌는지가 아래에 펼쳐진다.
        if edge is not None:
            lines += ["  " + ln for ln in GA.edge_hole_lines(
                "랭킹 폴", edge, GA.planned_windows(cfg.log_dir, cfg.state_dir))]
        lines += [
            f"candles_1m rows    : {n_1m}",
            f"trades_snap rows   : {n_tr}",
            f"orderbook_snap rows: {n_ob}",
            f"events             : {n_ev}",
            f"promotions rows    : {n_pr}",
        ]
        if n_snap == 0:
            lines.append("!! rankings_snap 0건 — 주말/휴장이 아니라면 수집 장애 흔적")
        if edge is not None and edge["max_hole_s"] > 30 * 60:
            lines.append(f"!! 랭킹 폴링 최대 공백 {edge['max_hole_s'] / 60.0:.1f}분 "
                         f"({edge['max_hole_kind']}) — 세션 전환(정상) 또는 장애 구간인지 "
                         "collector.log/ALERT 파일과 대조할 것")
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
    # 계획 정비(PLANNED_)와 진짜 사고(ALERT_)를 분리해서 센다 — 아침에 한 줄만 보고
    # "ALERT 0건이면 무사"라고 판단할 수 있어야 한다(워치독이 접두어로 구분해 쓴다).
    try:
        day_ago = time.time() - 86400
        alerts = sorted(p.name for p in cfg.log_dir.glob("ALERT_*.txt")
                        if p.stat().st_mtime >= day_ago)
        planned = sorted(p.name for p in cfg.log_dir.glob("PLANNED_*.txt")
                         if p.stat().st_mtime >= day_ago)
        notes = sorted(p.name for p in cfg.log_dir.glob("NOTE_*.txt")
                       if p.stat().st_mtime >= day_ago)
        tradeoffs = sorted(p.name for p in cfg.log_dir.glob("TRADEOFF_*.txt")
                           if p.stat().st_mtime >= day_ago)
        # 네 등급의 뜻을 아침 독자가 외우고 있다고 가정하지 않는다 — 리포트가 스스로
        # 설명한다. 계약 본문은 ops/watchdog.ps1 머리말과 docs/11 §21.
        lines.append("")
        lines.append("파일 등급 4종: ALERT_=고장(고쳐라) / PLANNED_=사람이 일부러(무시) / "
                     "NOTE_=설계대로(참고) / TRADEOFF_=시스템이 포기함(읽고 결정)")
        lines.append(f"ALERT files (24h, 진짜 문제 — 고쳐라): {len(alerts)}")
        lines += [f"  {a}" for a in alerts]
        if not alerts:
            lines.append("  (없음 — 무인 구간에 사고 없음)")
        lines.append(f"TRADEOFF files (24h, 시스템이 무언가를 포기함 — 고장 아님, "
                     f"당신이 판단할 것): {len(tradeoffs)}")
        lines += [f"  {t}" for t in tradeoffs]
        if tradeoffs:
            lines.append("  ↑ 각 파일에 무엇을/무엇을 위해/얼마나 오래/얼마나 가 적혀 있다. "
                         "오래 지속돼도 ALERT_ 로 올라가지 않는다(설계) — 크기를 보고 판단할 것.")
        lines.append(f"PLANNED files (24h, 계획된 정비 — 무시): {len(planned)}")
        lines += [f"  {p}" for p in planned]
        lines.append(f"NOTE files (24h, 설계대로 동작한 기록 — 사고 아님): {len(notes)}")
        lines += [f"  {n}" for n in notes]
    except OSError:
        pass

    # 결손 감사 — "어제 데이터가 초 단위 사건을 담고 있는가"에 답하는 절.
    # 위의 행 수 요약은 **양**만 말한다. 양이 많아도 6초짜리 사건이 조각나 있으면
    # 쓸 수 없으므로, 아침 한 장에 결손 자체가 같이 찍혀야 한다
    # (coordination/DATA-QUALITY-PROGRAM.md 축 1). 감사가 깨져도 위 요약은 살아야 하므로
    # 실패는 삼키고 사유만 남긴다 — 이 파일은 경보가 아니라 기록이다.
    lines.append("")
    try:
        lines.append(GA.audit(cfg, start_ms, end_ms, label))
    except Exception as e:  # noqa: BLE001
        lines.append(f"GAP AUDIT FAILED: {e!r} — ops/gap_audit.py 를 직접 돌려 확인할 것")
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

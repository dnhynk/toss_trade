"""일일 무결성 점검 — 소유: W5 (하드닝 스펙 coordination/specs/w5_5day_hardening.md §6).

하루 1회(작업 스케줄러 tossmon-dailyhealth, 08:52 KST — 폐장 직후 조용한 구간)
전날 수집분(KST 전일 09:00 → 당일 08:50)의 랭킹 스냅 간격·봉 수·이벤트 수를 요약해
`data/daily_health_<YYYYMMDD>.txt` 로 남긴다. 데이터 품질이 조용히 나빠지는 것을
사후에라도 알 수 있게 하는 것이 목적 — 경보가 아니라 기록이다(경보는 watchdog.ps1).

원칙: 라이브 API 호출 없음, DB 는 read-only URI 로만(계약 C-6), 실패해도 요약 파일은
반드시 남긴다(부분 실패를 파일 안에 기록).

**결번은 영영 없어진다** — 작업 스케줄러는 놓친 실행을 따라잡지 않는다. 2026-08-06 이
그랬다(03:46 비정상 종료 → 09:36 부팅, 예약 시각 08:52 에 기계가 꺼져 있었다). 그런데
그 결번된 창이 이 프로젝트 최대 공백(랭킹 폴 295.5분)이 난 창이었다 — **사고가 자기
자신을 감춘다.** 그래서 매 성공 실행이 빠진 날을 뒤늦게라도 채운다(`run_catchup`).
"""
from __future__ import annotations

import argparse
import os
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


def catchup_header_lines(cfg, end_ms: int) -> list[str]:
    """따라잡기로 만든 파일의 머리말 — 정시 생성분과 구분되게, 그리고 못 본 것을 적는다.

    사후 재구성은 DB 로는 정시분과 같은 것을 보지만 **로그로 보는 절은 다르다**:
    `[체결 tape gap]`·`config_sig`·`수집기 재기동`은 collector.log(+회전본 .gz)에서만
    읽히고, 회전 보존기간 밖은 못 본다. 그 한계가 파일 안에 없으면 '결손 0건'이
    '못 봤다'와 구분되지 않는다.
    """
    late_h = (time.time() - end_ms / 1000.0) / 3600.0
    return [
        f"!! CATCH-UP — 정시(스케줄러 08:52)에 만들어지지 않은 리포트다. 창이 끝난 지 "
        f"{late_h:.1f}시간 뒤에 사후 재구성했다.",
        "   결번 사유는 이 파일이 모른다(기계 꺼짐 / 스케줄러 미실행 / 실행 실패). "
        "data/watchdog.log 와 같은 날짜의 ALERT_ 파일로 대조할 것.",
        "   사후 재구성이 못 보는 것:",
        f"   - [체결 tape gap]·config_sig·수집기 재기동 횟수는 collector.log 에만 남는다. "
        f"회전 보존 {cfg.log_retention_days}일 밖이면 그 절은 비었거나 부분적이다 — "
        "'결손 0건'이 아니라 **'못 봤다'**로 읽을 것.",
        "   - 'last telemetry' 절은 싣지 않았다. collector.log 꼬리는 생성 시각의 상태이지 "
        "이 창의 상태가 아니다.",
    ]


def build_summary(cfg, date_str: str | None, catchup: bool = False) -> str:
    start_ms, end_ms, label = window_for(date_str)
    lines = [
        f"=== tossmon daily health {label} ==={'  [CATCH-UP]' if catchup else ''}",
        f"window (KST): {datetime.fromtimestamp(start_ms/1000):%Y-%m-%d %H:%M} "
        f"~ {datetime.fromtimestamp(end_ms/1000):%Y-%m-%d %H:%M}",
        f"generated: {datetime.now():%Y-%m-%d %H:%M:%S}",
    ]
    if catchup:
        lines += catchup_header_lines(cfg, end_ms)
    lines.append("")
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
        # 휴장 구간을 같이 넘긴다. 안 넘기면 주말·휴장일 창(관측 0개)이 창 전체를
        # `ALERT_` 로 찍는다 — 08-02·08-09(일)·08-03(월)이 실제로 그랬고, 그러면
        # `max_hole` 줄이 매주 늑대를 외쳐 읽는 사람이 그 줄을 건너뛰게 된다.
        closed_now = GA.closed_spans(cfg.log_dir, start_ms, end_ms)
        if edge is not None:
            lines += ["  " + ln for ln in GA.edge_hole_lines(
                "랭킹 폴", edge, GA.planned_windows(cfg.log_dir, cfg.state_dir), closed_now)]
        lines += [
            f"candles_1m rows    : {n_1m}",
            f"trades_snap rows   : {n_tr}",
            f"orderbook_snap rows: {n_ob}",
            f"events             : {n_ev}",
            f"promotions rows    : {n_pr}",
        ]
        if n_snap == 0:
            lines.append("!! rankings_snap 0건 — 주말/휴장이 아니라면 수집 장애 흔적")
        # 창 전체가 빈 날(기계가 하루 종일 꺼져 있던 날)에도 파일은 남긴다 — 파일이
        # 없다는 것은 "안 봤다"는 뜻이어야 하고, 파일이 있다는 것은 "봤다"는 뜻이어야
        # 한다. 다만 **왜** 비었는지는 이 도구가 가르지 못하므로 판정하지 않는다.
        if not any([n_snap, n_1m, n_tr, n_ob]):
            lines += [
                "!! 이 창에는 데이터가 하나도 없다 (랭킹/1분봉/체결/호가 전부 0건).",
                "   사유는 이 도구로 구분되지 않는다: (가) 기계가 창 내내 꺼져 있었다 "
                "(나) 수집기가 안 떠 있었다 (다) 휴장이라 볼 것이 없었다 "
                "(라) DB 가 이 구간을 잃었다. 판정하지 않는다 — 아래 파일 목록과 "
                "data/watchdog.log 로 대조할 것.",
                "   **이 파일의 존재는 '봤다'는 뜻이지 '수집됐다'는 뜻이 아니다.**",
            ]
        # 이 `!!` 줄은 등급과 **따로** 도는 문턱이라, 등급만 고치면 배너는 주말마다 그대로
        # 늑대를 외친다. 그래서 여기서도 같은 근거를 쓴다: 최대 공백이 통째로 휴장 구간
        # 안이면 배너를 내리지 않는다. 근거가 없으면(= 못 봤으면) 그대로 외친다.
        if edge is not None and edge["max_hole_s"] > 30 * 60:
            worst = max(edge["holes"], key=lambda h: h.seconds)
            explained = closed_now and GA.uncovered_span_ms(
                worst.start_ms, worst.end_ms, closed_now) <= 0
            if not explained:
                lines.append(f"!! 랭킹 폴링 최대 공백 {edge['max_hole_s'] / 60.0:.1f}분 "
                             f"({edge['max_hole_kind']}) — 세션 전환(정상) 또는 장애 구간인지 "
                             "collector.log/ALERT 파일과 대조할 것")
            else:
                lines.append(f"   (최대 공백 {edge['max_hole_s'] / 60.0:.1f}분은 전부 휴장 "
                             "구간이다 — telemetry session=closed 로 확인됨)")
    finally:
        conn.close()

    # 마지막 텔레메트리 스냅샷 + 창 안의 ALERT 파일 목록 (로그/파일 기반, DB 무관)
    # 따라잡기 분에서는 이 꼬리가 **생성 시각**의 상태라 창과 무관하다 — 실으면 08-06
    # 리포트에 08-07 의 텔레메트리가 찍힌다. 그래서 싣지 않고 머리말에 그렇게 적는다.
    log_path = cfg.log_dir / "collector.log"
    if not catchup:
        try:
            tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
            tele = [ln for ln in tail if "telemetry session=" in ln]
            if tele:
                lines += ["", "last telemetry:", "  " + tele[-1].strip()]
        except OSError:
            lines.append("collector.log unreadable")
    # 계획 정비(PLANNED_)와 진짜 사고(ALERT_)를 분리해서 센다 — 아침에 한 줄만 보고
    # "ALERT 0건이면 무사"라고 판단할 수 있어야 한다(워치독이 접두어로 구분해 쓴다).
    # 세는 창을 **리포트 창에 맞춘다**. 예전 기준은 "지금부터 24시간"이었고, 정시 실행에서는
    # 두 창이 거의 겹쳐 결과가 사실상 같지만 따라잡기 분에서는 다른 날의 파일이 실린다
    # (08-06 리포트에 08-07 의 ALERT 가 실린다). 아침 독자가 "ALERT 0건이면 무사"로 읽는
    # 줄이므로 어느 창을 센 것인지가 값보다 중요하다.
    try:
        lo, hi = start_ms / 1000.0, end_ms / 1000.0

        def _in_window(prefix: str) -> list[str]:
            return sorted(p.name for p in cfg.log_dir.glob(f"{prefix}_*.txt")
                          if lo <= p.stat().st_mtime <= hi)

        alerts = _in_window("ALERT")
        planned = _in_window("PLANNED")
        notes = _in_window("NOTE")
        tradeoffs = _in_window("TRADEOFF")
        # 네 등급의 뜻을 아침 독자가 외우고 있다고 가정하지 않는다 — 리포트가 스스로
        # 설명한다. 계약 본문은 ops/watchdog.ps1 머리말과 docs/11 §21.
        lines.append("")
        lines.append("파일 등급 4종: ALERT_=고장(고쳐라) / PLANNED_=사람이 일부러(무시) / "
                     "NOTE_=설계대로(참고) / TRADEOFF_=시스템이 포기함(읽고 결정)")
        lines.append(f"ALERT files (창 안, 진짜 문제 — 고쳐라): {len(alerts)}")
        lines += [f"  {a}" for a in alerts]
        if not alerts:
            lines.append("  (없음 — 무인 구간에 사고 없음)")
        lines.append(f"TRADEOFF files (창 안, 시스템이 무언가를 포기함 — 고장 아님, "
                     f"당신이 판단할 것): {len(tradeoffs)}")
        lines += [f"  {t}" for t in tradeoffs]
        if tradeoffs:
            lines.append("  ↑ 각 파일에 무엇을/무엇을 위해/얼마나 오래/얼마나 가 적혀 있다. "
                         "오래 지속돼도 ALERT_ 로 올라가지 않는다(설계) — 크기를 보고 판단할 것.")
        lines.append(f"PLANNED files (창 안, 계획된 정비 — 무시): {len(planned)}")
        lines += [f"  {p}" for p in planned]
        lines.append(f"NOTE files (창 안, 설계대로 동작한 기록 — 사고 아님): {len(notes)}")
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


def existing_labels(log_dir: Path) -> set[str]:
    """이미 발행된 아침 리포트의 날짜 라벨들. **파일의 존재가 유일한 기준이다** —
    별도의 '마지막 실행' 상태 파일을 두지 않는다. 상태 파일은 기계가 죽을 때 같이
    죽거나 산출물과 어긋나지만, 산출물 자체는 어긋날 수 없다."""
    out = set()
    for p in log_dir.glob("daily_health_*.txt"):
        lbl = p.name[len("daily_health_"):-len(".txt")]
        if len(lbl) == 8 and lbl.isdigit():
            out.add(lbl)
    return out


def missing_labels(log_dir: Path, today_label: str,
                   catchup_days: int) -> tuple[list[str], list[str]]:
    """(따라잡을 라벨들, 상한 밖이라 포기한 라벨들).

    범위는 **이미 있는 가장 오래된 리포트의 다음 날 ~ 어제**다. 즉 채우는 것은
    기록열 안의 *결번*이지 기록을 과거로 소급 확장하는 것이 아니다 — 그렇게 하지
    않으면 수집기가 존재하지도 않던 날들에 대해 "데이터 0건" 파일을 끝없이 찍는다.

    상한(`catchup_days`)을 넘긴 결번은 **버리되 조용히 버리지 않는다** — 호출자가
    목록을 출력한다. 잘라낸 것을 말하지 않는 상한은 "다 봤다"로 읽힌다.
    """
    have = existing_labels(log_dir)
    if not have:
        return [], []  # 기록열이 아직 없다 — 채울 결번도 없다
    today = datetime.strptime(today_label, "%Y%m%d")
    day = datetime.strptime(min(have), "%Y%m%d") + timedelta(days=1)
    gaps = []
    while day < today:
        lbl = day.strftime("%Y%m%d")
        if lbl not in have:
            gaps.append(lbl)
        day += timedelta(days=1)
    cutoff = (today - timedelta(days=catchup_days)).strftime("%Y%m%d")
    return [g for g in gaps if g >= cutoff], [g for g in gaps if g < cutoff]


def write_if_absent(path: Path, text: str) -> bool:
    """없을 때만 쓴다(멱등). 임시 파일에 다 쓴 뒤 옮기는 이유: 쓰는 도중에 기계가 죽으면
    부분 파일이 남고, 그러면 '존재한다'가 참이 되어 그 날은 영영 재시도되지 않는다."""
    if path.exists():
        return False
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        if path.exists():  # 동시 실행이 먼저 채웠다면 그쪽을 남긴다
            return False
        os.replace(tmp, path)
        return True
    finally:
        # 디스크가 찼거나 옮기기가 실패하면 조각이 남는다. `data/` 는 disk_guard 가
        # 여유 공간을 보는 곳이고, 여기 쓰레기가 쌓이는 것은 그 자체로 사고다.
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def run_catchup(cfg, today_label: str) -> dict:
    """결번된 아침 리포트를 뒤늦게 채운다 — 한 날이 실패해도 나머지는 계속 간다.

    한 날의 재구성이 예외로 죽으면 그 날은 **파일을 남기지 않고** 넘어간다. 부분 파일을
    남기면 다음 실행이 '있다'고 보고 영영 재시도하지 않기 때문이다.
    """
    todo, dropped = missing_labels(cfg.log_dir, today_label, cfg.daily_health_catchup_days)
    made, skipped, failed = [], [], []
    for lbl in todo:
        out = cfg.log_dir / f"daily_health_{lbl}.txt"
        try:
            if write_if_absent(out, build_summary(cfg, lbl, catchup=True)):
                made.append(lbl)
            else:
                skipped.append(lbl)
        except Exception as e:  # noqa: BLE001 — 한 날의 실패가 나머지를 막지 않는다
            failed.append(f"{lbl}: {e!r}")
    return {"made": made, "skipped": skipped, "failed": failed, "dropped": dropped}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=None)
    ap.add_argument("--date", default=None, help="YYYYMMDD (수집일이 끝나는 날, 기본 오늘)")
    ap.add_argument("--force", action="store_true",
                    help="--date 가 가리키는 리포트가 이미 있어도 덮어쓴다 (기본은 거부)")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Windows 콘솔 cp949 대응
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    cfg = load_ops_config(args.config)
    _, _, label = window_for(args.date)
    # 지난 날을 명시해서 부르는 것은 정의상 사후 재구성이다 — 사람이 손으로 만든
    # 08-06 리포트가 정시 생성분과 똑같이 생겨서는 안 된다.
    _, _, today_label = window_for(None)
    out = cfg.log_dir / f"daily_health_{label}.txt"
    # **`--date` 는 기존 리포트를 덮지 않는다.** 따라잡기 경로는 `write_if_absent` 로
    # 이미 보호돼 있었는데(bf81c3b) 사람이 과거 날을 재구성하는 이 경로만 무방비였다.
    # 아침 리포트는 기록이고, 08-06 정전을 잡아낸 근거가 바로 그 기록이었다 — 다시 만든
    # 것이 원본을 지우면 그 근거가 사라진다. 존재 확인을 **재구성보다 먼저** 하는 이유:
    # 어차피 거부할 파일 하나 때문에 몇 분짜리 사후 재구성을 치를 이유가 없다.
    # 정시 경로(`--date` 없음)는 일부러 보호하지 않는다 — 그날 것을 늘 최신으로 갱신하는
    # 것이 그 경로의 일이고, 거기서 거부하면 재실행이 깨진다.
    if args.date is not None and out.exists() and not args.force:
        print(f"거부: {out} 가 이미 있다 — 덮지 않았다(아무것도 쓰지 않음).\n"
              f"      그 날의 리포트는 기록이다. 정말 다시 만들려면 `--force` 를 주거나 "
              f"기존 파일을 먼저 옮겨라.")
        return 4
    text = build_summary(cfg, args.date, catchup=(label != today_label))
    replaced = args.date is not None and out.exists()
    out.write_text(text, encoding="utf-8")
    print(text)
    if replaced:
        print(f"덮어썼다(--force): {out} — 이전 내용은 사라졌다")
    else:
        print(f"written: {out}")
    # 따라잡기는 **예약 실행 경로에서만** 돈다. `--date` 는 사람이 특정 날을 다시
    # 만들려고 주는 인자이고, 그때 옆 날들까지 만들어내면 그건 요청 안 한 동작이다.
    # 오늘 것을 먼저 쓰고 나서 도는 이유: 정시 리포트가 따라잡기 비용에 밀리면 안 된다.
    if args.date is None:
        r = run_catchup(cfg, label)
        print(f"catch-up: made={len(r['made'])} {r['made']} "
              f"skipped(exists)={len(r['skipped'])} failed={r['failed']}")
        if r["dropped"]:
            print(f"catch-up: 상한({cfg.daily_health_catchup_days}일) 밖이라 포기한 결번 "
                  f"{len(r['dropped'])}일: {r['dropped']} — 필요하면 "
                  f"`--date <YYYYMMDD>` 로 수동 재구성할 것")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

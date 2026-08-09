"""`ops/daily_health.py` 의 결번 따라잡기 검증 — 소유: W5.

## 이 스위트가 지키려는 것

아침 리포트는 **결번되면 영영 없다.** 작업 스케줄러는 놓친 실행을 따라잡지 않기 때문이다.
2026-08-06 이 정확히 그랬다: 기계가 03:46 비정상 종료, 09:36 부팅 — 예약 시각 08:52 에
꺼져 있었다. 그리고 그 결번된 창이 이 프로젝트에서 가장 큰 공백(랭킹 폴 295.5분)이 난
창이었다. **사고가 자기 자신을 감춘다** — 기계를 죽인 사고가 그 사고를 드러낼 리포트도
같이 죽인다.

그래서 여기서 검증하는 것은 "따라잡기 코드가 돈다"가 아니라 다음 네 가지다:

- 결번된 날이 **실제로 채워지는가** (호출만 되고 파일이 안 생기면 없는 것과 같다)
- **두 번 돌려도 기존 파일이 안 바뀌는가** — 재실행이 멱등이어야 한다. 아침 리포트는
  기록이므로 뒤늦은 실행이 과거를 다시 쓰면 안 된다.
- 상한 밖 결번을 **조용히 버리지 않는가** — 잘라낸 것을 말하지 않는 상한은 "다 봤다"로
  읽힌다.
- 사후 생성분이 **정시 생성분과 구분되는가**, 그리고 못 본 것을 파일 안에 적는가.

DB 는 tmp_path 안의 빈 스키마 파일이다 — 라이브 DB 는 절대 건드리지 않는다.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3

import pytest

from ops import daily_health as DH
from ops.opsconfig import DiskThresholds, OpsConfig

# gap_audit 이 읽는 테이블만. 전부 비워 두면 "데이터가 하나도 없는 날" 경로가 그대로 돈다.
_SCHEMA = """
    CREATE TABLE promotions (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT,
        ts_ms INTEGER, from_tier INTEGER, to_tier INTEGER, reason TEXT, score REAL);
    CREATE TABLE rankings_snap (id INTEGER PRIMARY KEY AUTOINCREMENT, snap_ms INTEGER,
        ranking_type TEXT, duration TEXT, rank INTEGER, symbol TEXT,
        last_u INTEGER, vol_qu INTEGER, amount_u INTEGER);
    CREATE TABLE orderbook_snap (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT,
        snap_ms INTEGER, ts_ms INTEGER, depth_json TEXT);
    CREATE TABLE candles_1m (symbol TEXT, ts_ms INTEGER, open_u INTEGER, high_u INTEGER,
        low_u INTEGER, close_u INTEGER, vol_qu INTEGER, PRIMARY KEY (symbol, ts_ms));
    CREATE TABLE trades_snap (symbol TEXT, ts_ms INTEGER, price_u INTEGER, qty_u INTEGER,
        PRIMARY KEY (symbol, ts_ms, price_u, qty_u));
    CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, t0_ms INTEGER);
"""


def _cfg(tmp_path: pathlib.Path, catchup_days: int = 7) -> OpsConfig:
    db = tmp_path / "tossmon.db"
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return OpsConfig(
        db_path=db, log_dir=tmp_path, state_dir=tmp_path / "state",
        archive_dir=tmp_path / "archive", disk=DiskThresholds(0.0, 0.0),
        stale_minutes_warn=5, stale_minutes_critical=15, log_retention_days=14,
        log_max_bytes=1000, collector_cmd=["python", "-c", "pass"],
        max_restarts_per_window=5, restart_window_s=600,
        restart_backoff_base_s=1.0, restart_backoff_cap_s=10.0,
        daily_health_catchup_days=catchup_days)


def _label(days_ago: int) -> str:
    return (dt.datetime.now() - dt.timedelta(days=days_ago)).strftime("%Y%m%d")


def _seed(log_dir: pathlib.Path, *days_ago: int) -> None:
    """해당 날짜의 아침 리포트가 이미 발행돼 있었던 것으로 둔다."""
    for d in days_ago:
        (log_dir / f"daily_health_{_label(d)}.txt").write_text(
            f"=== tossmon daily health {_label(d)} ===\n원본\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1. 결번 탐지 — 범위와 상한
# --------------------------------------------------------------------------- #
def test_missing_labels_finds_the_hole_in_the_series(tmp_path):
    """08-06 의 재현: 어제 것만 없다."""
    _seed(tmp_path, 3, 2)                       # D-3, D-2 는 있고
    todo, dropped = DH.missing_labels(tmp_path, _label(0), 7)
    assert todo == [_label(1)]                  # D-1 이 결번
    assert dropped == []


def test_missing_labels_does_not_extend_the_series_backwards(tmp_path):
    """가장 오래된 리포트보다 이전 날은 결번이 아니다 — 수집기가 없던 날이다.

    이 가드가 없으면 상한(기본 7일)만큼 과거로 '데이터 0건' 파일을 찍어낸다.
    """
    _seed(tmp_path, 2)                          # 기록열은 D-2 에서 시작한다
    todo, dropped = DH.missing_labels(tmp_path, _label(0), 7)
    assert todo == [_label(1)]                  # D-3, D-4 ... 는 만들지 않는다
    assert dropped == []


def test_missing_labels_reports_what_the_cap_dropped(tmp_path):
    """상한 밖 결번은 **버리되 목록으로 돌려준다.** 조용히 자르는 상한은 '다 봤다'로 읽힌다."""
    _seed(tmp_path, 6)                          # 6일 전 것만 있고 그 뒤는 전부 결번
    todo, dropped = DH.missing_labels(tmp_path, _label(0), 3)
    assert todo == [_label(3), _label(2), _label(1)]
    assert dropped == [_label(5), _label(4)]


def test_no_reports_at_all_means_nothing_to_catch_up(tmp_path):
    """첫 실행: 채울 결번이 없다(기록열 자체가 없다)."""
    assert DH.missing_labels(tmp_path, _label(0), 7) == ([], [])


# --------------------------------------------------------------------------- #
# 2. 따라잡기 — 채워지는가 / 두 번 돌려도 안 바뀌는가
# --------------------------------------------------------------------------- #
def test_catchup_fills_the_missing_day_and_is_idempotent(tmp_path):
    """**이 스위트의 핵심.** 결번이 채워지고, 두 번째 실행은 아무것도 안 건드린다."""
    cfg = _cfg(tmp_path)
    _seed(tmp_path, 3, 2)
    gone = tmp_path / f"daily_health_{_label(1)}.txt"
    kept = tmp_path / f"daily_health_{_label(2)}.txt"
    assert not gone.exists()

    r1 = DH.run_catchup(cfg, _label(0))
    assert r1["made"] == [_label(1)], r1
    assert r1["failed"] == [], r1
    assert gone.exists()

    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
              for p in tmp_path.glob("daily_health_*.txt")}
    r2 = DH.run_catchup(cfg, _label(0))
    assert r2 == {"made": [], "skipped": [], "failed": [], "dropped": []}, r2
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
             for p in tmp_path.glob("daily_health_*.txt")}
    assert after == before, "재실행이 기존 파일을 건드렸다"
    # 원래 있던 파일의 내용이 그대로인지도 직접 본다 (mtime 만으로는 부족)
    assert kept.read_text(encoding="utf-8").endswith("원본\n")


def test_catchup_marks_the_file_and_writes_down_what_it_cannot_see(tmp_path):
    """사후 생성분은 정시 생성분과 구분돼야 하고, 못 본 것을 스스로 말해야 한다."""
    cfg = _cfg(tmp_path)
    _seed(tmp_path, 3, 2)
    DH.run_catchup(cfg, _label(0))
    body = (tmp_path / f"daily_health_{_label(1)}.txt").read_text(encoding="utf-8")
    assert "[CATCH-UP]" in body                      # 제목에서 바로 보인다
    assert "정시" in body and "사후 재구성" in body
    assert "tape gap" in body and "회전 보존 14일" in body   # 조회 한계가 적혀 있다
    assert "last telemetry:" not in body             # 생성 시각의 꼬리를 싣지 않는다


def test_a_day_with_no_data_still_gets_a_file_that_refuses_to_judge(tmp_path):
    """기계가 하루 종일 꺼져 있던 날. 파일은 남기되 사유는 판정하지 않는다.

    파일이 없다는 것은 '안 봤다'는 뜻이어야 하고, 파일이 있다는 것은 '봤다'는 뜻이어야
    한다. 둘을 섞으면 다음 사람이 결번과 무사고를 구분할 수 없다.
    """
    cfg = _cfg(tmp_path)                              # DB 는 스키마만 있고 전부 0건
    _seed(tmp_path, 3, 2)
    DH.run_catchup(cfg, _label(0))
    body = (tmp_path / f"daily_health_{_label(1)}.txt").read_text(encoding="utf-8")
    assert "데이터가 하나도 없다" in body
    assert "판정하지 않는다" in body
    assert "'봤다'는 뜻이지" in body


def test_catchup_never_overwrites_even_a_stale_looking_file(tmp_path):
    """이미 있는 파일은 내용이 무엇이든 안 건드린다 — 아침 리포트는 기록이다.

    두 겹으로 막는다: `missing_labels` 가 애초에 목록에서 빼고, 그래도 뚫리면
    `write_if_absent` 가 거부한다. 아래는 두 번째 겹을 직접 겨눈다 — 첫 겹이
    실수로 넓어져도 과거를 다시 쓰지는 않는다는 뜻이다.
    """
    cfg = _cfg(tmp_path)
    _seed(tmp_path, 3, 2)
    victim = tmp_path / f"daily_health_{_label(1)}.txt"
    victim.write_text("사람이 손으로 남긴 메모\n", encoding="utf-8")
    r = DH.run_catchup(cfg, _label(0))
    assert r["made"] == []
    assert victim.read_text(encoding="utf-8") == "사람이 손으로 남긴 메모\n"
    assert DH.write_if_absent(victim, "덮어쓰기 시도") is False
    assert victim.read_text(encoding="utf-8") == "사람이 손으로 남긴 메모\n"


def test_write_if_absent_leaves_no_partial_file_when_the_write_dies(tmp_path, monkeypatch):
    """부분 파일이 남으면 '존재한다'가 참이 되어 그 날은 영영 재시도되지 않는다."""
    target = tmp_path / "daily_health_20260806.txt"

    def boom(self, *a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(pathlib.Path, "write_text", boom)
    with pytest.raises(OSError):
        DH.write_if_absent(target, "본문")
    assert not target.exists()
    assert list(tmp_path.glob("daily_health_*.txt")) == []


# --------------------------------------------------------------------------- #
# 3. 배선 — 만들었는데 안 불리면 없는 것과 같다 (H-1 의 형태)
# --------------------------------------------------------------------------- #
def test_main_actually_calls_the_catchup(tmp_path):
    """`run_catchup` 이 `main()` 에서 실제로 불리는지. 예약 실행 경로(--date 없음)에서만."""
    src = pathlib.Path(DH.__file__).read_text(encoding="utf-8")
    body = src.split("def main(")[1]
    assert "run_catchup(" in body, (
        "ops/daily_health.py main() 이 run_catchup 을 부르지 않는다 — "
        "결번은 계속 결번으로 남는다.")


def test_explicit_date_does_not_trigger_catchup(tmp_path):
    """`--date` 는 사람이 특정 날을 다시 만들려고 주는 인자다. 옆 날까지 만들면 요청 밖이다."""
    cfg = _cfg(tmp_path)
    _seed(tmp_path, 3, 2)
    monkey_label = _label(1)
    DH.main(["--config", str(_write_cfg(tmp_path)), "--date", monkey_label])
    made = sorted(p.name for p in tmp_path.glob("daily_health_*.txt"))
    assert made == sorted([f"daily_health_{_label(d)}.txt" for d in (3, 2, 1)]), made
    # 지난 날을 명시해서 부른 것도 사후 재구성이므로 표시가 붙는다
    assert "[CATCH-UP]" in (tmp_path / f"daily_health_{monkey_label}.txt").read_text(
        encoding="utf-8")


def _write_cfg(tmp_path: pathlib.Path) -> pathlib.Path:
    """`main()` 이 읽을 수 있는 실제 YAML — 경로는 전부 tmp_path 안."""
    p = tmp_path / "ops_config.yaml"
    q = str(tmp_path).replace("\\", "/")
    p.write_text(
        f'db_path: "{q}/tossmon.db"\nlog_dir: "{q}"\nstate_dir: "{q}/state"\n'
        f'archive_dir: "{q}/archive"\ndaily_health:\n  catchup_days: 7\n',
        encoding="utf-8")
    return p


def test_main_fills_the_hole_end_to_end(tmp_path, capsys):
    """CLI 로 한 번 돌리면 오늘 것 + 결번이 같이 남는다."""
    _cfg(tmp_path)                                   # DB 파일만 만들어 둔다
    _seed(tmp_path, 3, 2)
    rc = DH.main(["--config", str(_write_cfg(tmp_path))])
    assert rc == 0
    assert (tmp_path / f"daily_health_{_label(0)}.txt").exists()   # 오늘 것
    assert (tmp_path / f"daily_health_{_label(1)}.txt").exists()   # 결번이 채워졌다
    out = capsys.readouterr().out
    assert f"catch-up: made=1 ['{_label(1)}']" in out


# --------------------------------------------------------------------------- #
# 4. `--date` 가 기존 리포트를 말없이 덮어쓰지 않는가 (2026-08-09 발견)
#
# 따라잡기 경로는 `write_if_absent` 로 덮지 않게 만들어 놨는데(bf81c3b), **사람이
# `--date` 로 과거 날을 재구성하는 경로만 무방비**였다 — `out.write_text(...)` 무조건
# 덮어쓰기. 리포트 무결성이 08-06 정전을 잡아낸 근거였던 만큼(COORDINATOR-STATE §4.11)
# 원본이 사라지는 것은 그 근거가 사라지는 것이다.
#
# 정시 경로(`--date` 없음)는 **일부러 그대로 둔다.** 그날의 리포트를 늘 최신으로
# 갱신하는 것이 그 경로의 일이고, 거기서 거부하면 재실행이 깨진다.
# --------------------------------------------------------------------------- #
def test_explicit_date_refuses_to_overwrite_an_existing_report(tmp_path, capsys):
    """이것이 고치기 전 실패다 — 원본이 조용히 사라졌다."""
    _cfg(tmp_path)
    label = _label(2)
    original = tmp_path / f"daily_health_{label}.txt"
    original.write_text("원본 — 08-06 정전의 근거", encoding="utf-8")

    rc = DH.main(["--config", str(_write_cfg(tmp_path)), "--date", label])

    assert original.read_text(encoding="utf-8") == "원본 — 08-06 정전의 근거", (
        "--date 가 기존 리포트를 덮어썼다 — 그 날의 근거가 사라진다")
    assert rc != 0, "아무것도 안 썼으면 종료코드로 말해야 한다"
    out = capsys.readouterr().out
    assert "거부" in out and "--force" in out, "거부한 사실이 stdout 에 안 찍힌다"


def test_explicit_date_with_force_overwrites_and_says_so(tmp_path, capsys):
    """사람이 명시적으로 덮으려는 경로는 남긴다 — 다만 조용히는 아니다."""
    _cfg(tmp_path)
    label = _label(2)
    original = tmp_path / f"daily_health_{label}.txt"
    original.write_text("원본", encoding="utf-8")

    rc = DH.main(["--config", str(_write_cfg(tmp_path)), "--date", label, "--force"])

    assert rc == 0
    assert original.read_text(encoding="utf-8") != "원본", "--force 인데 안 덮었다"
    out = capsys.readouterr().out
    assert "덮어썼다" in out and "--force" in out, "덮은 사실이 stdout 에 안 찍힌다"


def test_explicit_date_still_writes_when_the_file_is_absent(tmp_path, capsys):
    """보호가 새 파일 생성까지 막으면 재구성 기능 자체가 죽는다."""
    _cfg(tmp_path)
    label = _label(2)
    rc = DH.main(["--config", str(_write_cfg(tmp_path)), "--date", label])
    assert rc == 0
    assert (tmp_path / f"daily_health_{label}.txt").exists()
    assert "written:" in capsys.readouterr().out


def test_the_scheduled_path_still_overwrites_todays_report(tmp_path):
    """정시 경로는 보호 대상이 아니다 — 오늘 것은 늘 최신이어야 하고 재실행이 깨지면 안 된다."""
    _cfg(tmp_path)
    today = tmp_path / f"daily_health_{_label(0)}.txt"
    today.write_text("낡은 오늘치", encoding="utf-8")
    rc = DH.main(["--config", str(_write_cfg(tmp_path))])
    assert rc == 0
    assert today.read_text(encoding="utf-8") != "낡은 오늘치"


def test_a_refused_date_does_not_pay_for_the_reconstruction(tmp_path, monkeypatch):
    """거부할 것을 알면서 재구성부터 하면 몇 분을 버린다 — 존재 확인이 먼저다."""
    _cfg(tmp_path)
    label = _label(2)
    (tmp_path / f"daily_health_{label}.txt").write_text("원본", encoding="utf-8")
    called = []
    monkeypatch.setattr(DH, "build_summary", lambda *a, **k: called.append(1) or "x")
    DH.main(["--config", str(_write_cfg(tmp_path)), "--date", label])
    assert called == [], "거부할 파일인데 build_summary 를 불렀다"

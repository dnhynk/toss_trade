"""`tools/tape_saturation_probe.py` 가리개 — 숫자가 조용히 뜻을 바꾸지 않게.

이 도구가 만드는 표는 `docs/43` 의 결론 그 자체다. 임계·경계·"하한"이라는 성격이
리팩터링에 밀려 조용히 바뀌면 문서가 거짓말을 하게 되므로 여기서 못 박는다.
"""
from __future__ import annotations

import sqlite3

from tools import tape_saturation_probe as probe
from tossmon.store.writer import Store

DAY0 = 1_785_000_000_000


def _db(tmp_path, rows):
    path = tmp_path / "probe.db"
    with Store(path) as store:
        store._conn.executemany(
            "INSERT OR IGNORE INTO trades_snap VALUES (?, ?, ?, ?)", rows)
        store._conn.commit()
    return path


def _tape(symbol, start_ms, n, step_ms=1000):
    """가격을 매 행 다르게 줘서 PK 접힘 없이 n 행이 그대로 들어가게 한다."""
    return [(symbol, start_ms + i * step_ms, 1_000_000 + i, 1_000_000)
            for i in range(n)]


def test_bucket_is_saturated_at_the_cap_not_above_it(tmp_path):
    """포화 판정은 `>= 50` 이다. `> 50` 으로 미끄러지면 딱 50건인 칸 — 상한이 물린
    가장 전형적인 칸 — 이 통째로 표에서 빠진다."""
    rows = _tape("HOT", DAY0, probe.CAP, step_ms=10)          # 1초 안에 정확히 50건
    conn = probe._ro(_db(tmp_path, rows))
    try:
        table = "\n".join(probe.cadence_table(conn, None))
    finally:
        conn.close()
    line = [ln for ln in table.splitlines() if ln.startswith("| 1s ")][0]
    cells = [c.strip() for c in line.strip("|").split("|")]
    #        P   칸수  포화칸  비율      포화행       비율
    assert cells[1] == "1"                                     # 1초 칸 하나에 다 들어갔다
    assert cells[2] == "1" and cells[3] == "100.00%"           # 그 칸이 포화다
    assert cells[4] == f"{probe.CAP}" and cells[5] == "100.0%"


def test_only_gaps_short_enough_for_continuous_polling_are_counted(tmp_path):
    """4초 폴이 연속이면 구멍이 폴 주기의 두 배를 넘을 수 없다. 그보다 긴 것은
    tier3 재진입·재기동이고, 섞으면 "상한 때문"이라는 결론이 오염된다."""
    log = tmp_path / "collector.log"
    log.write_text(
        # 연속 폴링 중 상한 결손 (2초)
        f"2026-08-05 00:00:00,000 WARNING tape gap HOT: prev_max={DAY0} "
        f"< this_min={DAY0 + 2000} (n=50) — 표본 사이 체결 누락\n"
        # 재진입 (2시간)
        f"2026-08-05 02:00:00,000 WARNING tape gap OLD: prev_max={DAY0} "
        f"< this_min={DAY0 + 7_200_000} (n=50) — 표본 사이 체결 누락\n",
        encoding="utf-8")
    conn = probe._ro(_db(tmp_path, _tape("HOT", DAY0 + 2000, 30)))
    try:
        text = "\n".join(probe.gap_table(conn, log, None))
    finally:
        conn.close()
    assert "결손 줄 2건" in text                                # 둘 다 읽되
    assert "**1건 (50.0%)**" in text                            # 하나만 연속 폴링분
    assert "> 3600s | 1 |" in text                              # 긴 것은 버리지 않고 보인다


def test_missing_estimate_is_labelled_as_a_lower_bound(tmp_path):
    """"하한"이라는 말이 빠지면 이 숫자는 즉시 거짓이 된다 — 잘린 체결은 DB 에 없고
    국소 체결률 자체가 검열된 값이기 때문이다."""
    log = tmp_path / "collector.log"
    log.write_text(
        f"2026-08-05 00:00:00,000 WARNING tape gap HOT: prev_max={DAY0} "
        f"< this_min={DAY0 + 2000} (n=50) — 표본 사이 체결 누락\n", encoding="utf-8")
    # 구멍 직후 10초에 100건 = 10건/초. 구멍 2초 → 누락 하한 20건.
    conn = probe._ro(_db(tmp_path, _tape("HOT", DAY0 + 2000, 100, step_ms=100)))
    try:
        text = "\n".join(probe.gap_table(conn, log, None))
    finally:
        conn.close()
    assert "≈ 20건" in text
    assert "하한" in text and "하한인 이유" in text


def test_probe_never_opens_the_database_for_writing(tmp_path):
    """계약 C-6: 분석 도구는 수집 중인 DB 를 절대 쓰지 않는다."""
    conn = probe._ro(_db(tmp_path, _tape("HOT", DAY0, 3)))
    try:
        try:
            conn.execute("INSERT INTO trades_snap VALUES ('X', 1, 1, 1)")
        except sqlite3.OperationalError as exc:
            assert "readonly" in str(exc).lower() or "query_only" in str(exc).lower()
        else:                                                   # pragma: no cover
            raise AssertionError("read-only 가 아니다")
    finally:
        conn.close()

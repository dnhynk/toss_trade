"""`tools/d21_verdict.py` 테스트 — 소유: W3.

라이브 3.5 GB DB 에 의존하는 테스트는 하나도 없다. **합성 픽스처 DB** 와 합성 로그만 쓴다.

여기서 지키는 것
--------------
1. **얼린 상수가 사전등록 문서와 같은가** — 러너가 미러링한 §2 기준선·§3 밴드 경계를
   `coordination/D21-COVERAGE-PREREG.md` 에서 다시 파싱해 대조한다. 상수를 몰래 고치면
   여기서 깨진다. (사전등록의 값어치가 "결과보다 앞섰는가"에 달려 있으므로 이게 제일 중요하다)
2. **자정을 넘는 KST→UTC 로그 파싱과 회전본 병합** — 창 13:30~20:00 UTC 는 22:30 KST ~
   다음날 05:00 KST 라 **00:10 KST logrotate 를 지나간다.** 회전본을 빼면 창 중간이
   조용히 잘린다
3. **공백·쿨다운 탐지, 밴딩 경계**
4. **게이트** — 자가검사 실패 / 대상 창 부재 / 무효 조건에서 판정도 밴드도 내지 않는가

`main()` 게이트 테스트는 `BASELINE_TOP10`·`BASELINE_SUMMARY` 를 작은 합성 세트로
monkeypatch 한다. 검증 대상이 *기계*이고, 얼린 값 자체는 1번 테스트가 지키기 때문이다.
"""
from __future__ import annotations

import datetime as dt
import gzip
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools import d21_verdict as dv  # noqa: E402
from tossmon.api.models import RankingPage, RankingRow, Trade  # noqa: E402
from tossmon.store.writer import Store  # noqa: E402

SEC = 1000
PREREG = REPO_ROOT / "coordination" / "D21-COVERAGE-PREREG.md"


# --------------------------------------------------------------------------- #
# 창 산술 · 시각
# --------------------------------------------------------------------------- #
def test_window_ms_is_1330_to_2000_utc():
    lo, hi = dv.window_ms("2026-08-14")
    assert dv.utc_str(lo) == "2026-08-14 13:30:00"
    assert dv.utc_str(hi) == "2026-08-14 20:00:00"
    assert hi - lo == 6 * 3600 * SEC + 1800 * SEC      # 6.5 시간


def test_window_in_kst_crosses_local_midnight():
    lo, hi = dv.window_ms("2026-08-14")
    assert dv.kst_str(lo) == "2026-08-14 22:30:00"
    assert dv.kst_str(hi) == "2026-08-15 05:00:00"     # 날짜가 넘어간다


def test_log_ts_parsing_maps_local_kst_to_utc_across_midnight():
    lo, hi = dv.window_ms("2026-08-14")
    # 창 시작 직후(같은 날 밤)
    assert dv.parse_log_ts_ms("2026-08-14 22:30:05,001 INFO x") == lo + 5 * SEC
    # 자정을 넘긴 뒤 — KST 날짜가 08-15 인데 여전히 08-14 창 안이다
    ts = dv.parse_log_ts_ms("2026-08-15 04:59:59,999 INFO x")
    assert lo < ts < hi
    # 창 끝 이후
    assert dv.parse_log_ts_ms("2026-08-15 05:00:01,000 INFO x") > hi


def test_parse_log_ts_returns_none_for_non_timestamped_lines():
    assert dv.parse_log_ts_ms("Traceback (most recent call last):") is None
    assert dv.parse_log_ts_ms("") is None
    assert dv.parse_log_ts_ms("2026-13-99 99:99:99,000 INFO bad") is None


# --------------------------------------------------------------------------- #
# 로그 병합 — 회전본을 빼면 창 중간이 잘린다
# --------------------------------------------------------------------------- #
def _write_gz(path: Path, lines: list[str]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _telemetry(ts: str, sig: str, md: int, rank: int) -> str:
    return ("%s INFO    telemetry session=regular config_sig=%s watch=1500 tier2=40 tier3=8 "
            "md_peak_1s=%d md_p95_1s=%d.0 chart_peak_1s=2 rank_peak_1s=%d over_limit_1s=0 "
            "api_errors=0" % (ts, sig, md, md, rank))


def test_log_sources_takes_rotated_gz_skips_old_and_foreign(tmp_path):
    lo, _ = dv.window_ms("2026-08-14")            # 22:30 KST 08-14 ~ 05:00 KST 08-15
    log = tmp_path / "collector.log"
    log.write_text("x\n", encoding="utf-8")
    inside = tmp_path / "collector.20260815-001000.log.gz"     # 창 안 00:10 KST 회전
    older = tmp_path / "collector.20260810-001000.log.gz"      # 창보다 훨씬 전
    foreign = tmp_path / "collector.stdout.20260815-001000.log.gz"   # 다른 논리 파일
    for p in (inside, older, foreign):
        _write_gz(p, ["x"])

    got = dv.log_sources(log, lo)
    assert got == [inside, log]
    assert older not in got and foreign not in got


def test_scan_log_merges_telemetry_across_the_0010_kst_rotation(tmp_path):
    lo, hi = dv.window_ms("2026-08-14")
    sig = "rank3:TVOLUME@100,t3max10,rkpTVOLUME@10/k2/h300s/rotate/cd600s"
    log = tmp_path / "collector.log"
    # 회전본: 자정 전(23:00 KST) 표본. 회전 스탬프는 00:10 KST
    _write_gz(tmp_path / "collector.20260815-001000.log.gz", [
        _telemetry("2026-08-14 23:00:00,000", sig, 7, 3),
        _telemetry("2026-08-14 23:05:00,000", sig, 9, 3),
    ])
    # 살아 있는 로그: 자정 뒤(01:00 KST) 표본 + 창 밖 한 줄
    log.write_text("\n".join([
        _telemetry("2026-08-15 01:00:00,000", sig, 10, 3),
        _telemetry("2026-08-15 06:00:00,000", sig, 99, 99),   # 창 밖 (05:00 KST 이후)
    ]) + "\n", encoding="utf-8")

    scan = dv.scan_log(log, lo, hi)
    assert scan["telemetry"] == 3            # 회전본 2 + 살아있는 로그 1, 창 밖은 제외
    assert list(scan["sigs"]) == [sig] and scan["sigs"][sig] == 3
    assert scan["md"] == [7, 9, 10]
    assert scan["rank"] == [3, 3, 3]
    assert len(scan["files"]) == 2            # 회전본을 실제로 열었다


def test_scan_log_without_rotated_sibling_would_see_only_half(tmp_path):
    """회귀 방어: 회전본이 없으면(=빼먹으면) 창 전반이 사라진다는 것을 명시한다."""
    lo, hi = dv.window_ms("2026-08-14")
    sig = "sig1"
    log = tmp_path / "collector.log"
    log.write_text(_telemetry("2026-08-15 01:00:00,000", sig, 10, 3) + "\n", encoding="utf-8")
    assert dv.scan_log(log, lo, hi)["telemetry"] == 1
    _write_gz(tmp_path / "collector.20260815-001000.log.gz",
              [_telemetry("2026-08-14 23:00:00,000", sig, 7, 3)])
    assert dv.scan_log(log, lo, hi)["telemetry"] == 2


def test_scan_log_counts_probe_pattern_in_and_out_of_window(tmp_path):
    lo, hi = dv.window_ms("2026-08-14")
    log = tmp_path / "collector.log"
    log.write_text("\n".join([
        "2026-08-14 23:10:00,000 INFO    live_probe fired stride=5",     # 창 안
        "2026-08-16 10:00:00,000 INFO    tape saturation probe done",    # 창 밖
    ]) + "\n", encoding="utf-8")
    scan = dv.scan_log(log, lo, hi)
    assert len(scan["probe_in_window"]) == 1
    assert scan["probe_anywhere"] == 2


# --------------------------------------------------------------------------- #
# 순수 로직
# --------------------------------------------------------------------------- #
def test_find_gaps_only_counts_strictly_over_the_threshold():
    base = 1_000_000
    ms = [base, base + 60 * SEC, base + 121 * SEC, base + 400 * SEC]
    gaps = dv.find_gaps(ms, 60)
    # 정확히 60초는 "초과"가 아니라 제외. 61초와 279초만 걸린다
    assert [round(g[2]) for g in gaps] == [61, 279]
    assert dv.find_gaps([], 60) == [] and dv.find_gaps([base], 60) == []


def test_cooldown_violations_counts_pairs_under_600s_per_symbol():
    rows = [
        ("AAA", 0), ("AAA", 599 * SEC),            # 599s < 600 -> 위반
        ("BBB", 0), ("BBB", 600 * SEC),            # 정확히 600s -> 위반 아님
        ("CCC", 0), ("CCC", 100 * SEC), ("CCC", 200 * SEC),   # 인접 쌍 2건
    ]
    viol = dv.cooldown_violations(rows, 600)
    assert [(v[0], round(v[3])) for v in viol] == [("AAA", 599), ("CCC", 100), ("CCC", 100)]
    assert dv.cooldown_violations([], 600) == []


def test_prereg_median_takes_the_upper_middle_at_even_n():
    # 사전등록 §2 top-10 이 얼린 17.1% 는 두 가운데(17.0, 17.1) 중 위쪽이다.
    vals = [7.3, 11.1, 3.3, 22.9, 8.6, 17.0, 25.9, 17.1, 21.2, 19.0]
    assert dv.prereg_median(vals) == 17.1
    assert dv.prereg_median([1.0, 2.0, 3.0]) == 2.0


def test_p95_nearest_rank():
    # 최근접 순위: ceil(0.95*n) 번째로 작은 값. n=100 이면 95 번째(0-based 94)다.
    assert dv.p95_nearest_rank([5]) == 5
    assert dv.p95_nearest_rank([0] * 95 + [9] * 5) == 0      # 94 번 칸이 아직 0
    assert dv.p95_nearest_rank([0] * 94 + [9] * 6) == 9      # 94 번 칸이 9 로 바뀌는 경계
    assert dv.p95_nearest_rank(list(range(1, 21))) == 19     # n=20 -> ceil(19)=19 -> idx 18


def test_histogram_is_ascii_value_count_pairs():
    assert dv.histogram([3, 3, 0, 10]) == "0:1 3:2 10:1"


# --------------------------------------------------------------------------- #
# 밴딩 — 얼린 규칙의 적용이다. 경계를 문서 그대로 지키는가
# --------------------------------------------------------------------------- #
def test_band_tape_b_boundaries_follow_3_1_exactly():
    assert dv.band_tape_b(25.91)[0].startswith("INCREASED")
    assert dv.band_tape_b(25.9)[0].startswith("VERDICT WITHHELD")   # "> 25.9" 라 미포함
    assert dv.band_tape_b(17.1)[0].startswith("VERDICT WITHHELD")   # "17.1~25.9" 라 포함
    assert dv.band_tape_b(17.09)[0].startswith("NOT INCREASED")
    assert all(b[1] == "3-1" for b in (dv.band_tape_b(0.0), dv.band_tape_b(99.0)))


def test_band_seat_a_has_no_band_above_65_because_3_2_defines_none():
    assert dv.band_seat_a(55.1)[0].startswith("SIM MATCHED LIVE")
    assert dv.band_seat_a(45.0)[0].startswith("SIM MATCHED LIVE")
    assert dv.band_seat_a(44.9)[0].startswith("HALF MATCHED")
    assert dv.band_seat_a(25.0)[0].startswith("HALF MATCHED")
    assert dv.band_seat_a(24.9)[0].startswith("SIM WAS WRONG")
    assert dv.band_seat_a(65.0)[0].startswith("SIM MATCHED LIVE")
    # 문서에 65% 위 밴드가 없다 -> 지어내지 않는다
    assert dv.band_seat_a(65.1)[0].startswith("NO BAND")


def test_band_cost_boundaries_follow_3_3_exactly():
    assert dv.band_cost(154)[0].startswith("CLEARLY DECREASED")
    assert dv.band_cost(155)[0].startswith("INSIDE BASELINE RANGE")
    assert dv.band_cost(246)[0].startswith("INSIDE BASELINE RANGE")
    assert dv.band_cost(247)[0].startswith("INCREASED")


# --------------------------------------------------------------------------- #
# 얼린 상수 == 사전등록 문서
# --------------------------------------------------------------------------- #
def _prereg_text() -> str:
    return PREREG.read_text(encoding="utf-8")


def _section(text: str, anchor: str, end_anchor: str) -> str:
    i = text.index(anchor)
    j = text.index(end_anchor, i)
    return text[i:j]


def test_frozen_baseline_table_matches_the_prereg_document():
    """§2 top-10 표를 문서에서 다시 파싱해 `BASELINE_TOP10` 과 대조한다."""
    text = _prereg_text()
    body = _section(text, "## 2.", "## 3.")
    rows = re.findall(r"^\|\s*(\d{2}-\d{2})\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*"
                      r"([\d.]+)%\s*\|\s*(\d+)\s*\|", body, re.M)
    assert len(rows) == 10, "prereg section 2 top-10 table should have 10 rows"
    parsed = tuple(("2026-" + md, int(r), int(t), float(p), int(c))
                   for md, r, t, p, c in rows)
    assert parsed == dv.BASELINE_TOP10


def test_frozen_baseline_summaries_match_the_prereg_document():
    """§2 의 두 요약 줄(n / min / 중앙 / max)을 백분율만 뽑아 대조한다 (ASCII 로 비교)."""
    body = _section(_prereg_text(), "## 2.", "## 3.")
    lines = [ln for ln in body.splitlines() if ln.startswith("**n=")]
    assert len(lines) == 2, "expected one summary line for top-10 and one for top-100"
    for line, topn in zip(lines, (10, 100)):
        n = int(re.search(r"n=(\d+)", line).group(1))
        pcts = [float(x) for x in re.findall(r"([\d.]+)%", line)]
        assert (n, *pcts) == dv.BASELINE_SUMMARY[topn]


def test_frozen_band_edges_match_the_prereg_document():
    """§3-1 / §3-2 / §3-3 / §3-4 의 숫자를 문서에서 뽑아 미러링 상수와 대조한다."""
    text = _prereg_text()

    s31 = _section(text, "### 3-1.", "### 3-2.")
    assert float(re.search(r">\s*([\d.]+)%", s31).group(1)) == dv.B_MAX
    assert float(re.search(r"<\s*([\d.]+)%", s31).group(1)) == dv.B_MED

    s32 = _section(text, "### 3-2.", "### 3-3.")
    assert float(re.search(r"([\d.]+)%\**\s*\(", s32).group(1)) == dv.SIM_PRED_A_PCT
    edges = [(float(a), float(b)) for a, b in re.findall(r"\*?\*?(\d+)~(\d+)%", s32)]
    assert (dv.A_SIM_LO, dv.A_SIM_HI) in edges
    assert (dv.A_WRONG, dv.A_SIM_LO) in edges
    assert float(re.search(r"<\s*(\d+)%", s32).group(1)) == dv.A_WRONG

    s33 = _section(text, "### 3-3.", "### 3-4.")
    assert float(re.search(r"<\s*(\d+),", s33).group(1)) == dv.COST_LO
    lo, hi = re.search(r"(\d+)~(\d+)\(", s33.replace(" ", "")).groups()
    assert (int(lo), int(hi)) == (dv.COST_LO, dv.COST_HI)

    s34 = _section(text, "### 3-4.", "## 4.")
    # `초` = "초"(seconds). §3-4.1 의 "60 초 넘는" 이 유일한 초 단위 문턱이다.
    assert int(re.search(r"(\d+)\s*초", s34).group(1)) == dv.GAP_LIMIT_S


def test_source_carries_the_unreproduced_tag_for_the_sim_number():
    """55.1% 를 인용하는 자리에는 `[미재현]` 이 붙어 있어야 한다 (COORDINATOR-STATE §1-2b)."""
    src = (REPO_ROOT / "tools" / "d21_verdict.py").read_text(encoding="utf-8")
    assert "[미재현]" in src            # [미재현]
    assert dv.SIM_PRED_TAG == "[UNREPRODUCED]"      # ASCII 콘솔 표기 (§1f)


# --------------------------------------------------------------------------- #
# 합성 픽스처 DB
# --------------------------------------------------------------------------- #
def _seed(db_path: Path, day: str, *, ranked10: int, tape10: int, extra100: int = 0,
          capfill: int = 0, tier3: tuple[str, ...] = (), hold_expired: int = 0,
          dense: bool = False, gap_after: int | None = None,
          late_start_s: int = 0) -> None:
    """한 정규장 창에 합성 행을 심는다.

    `dense=True` 면 창 전체를 60 초 간격 스냅으로 채운다 (공백 0, 양쪽 끝 0 초) —
    무효 조건을 안 건드리는 대상 세션을 만들 때 쓴다. `gap_after` 는 그중 n 번째 스냅
    뒤를 비워 공백을 하나 만든다.
    """
    lo, hi = dv.window_ms(day)
    store = Store(db_path)
    try:
        syms10 = ["T%03d" % i for i in range(ranked10)]
        syms100 = ["U%03d" % i for i in range(extra100)]
        if dense:
            step = 60 * SEC
            times = list(range(lo + late_start_s * SEC, hi + 1, step))
            if gap_after is not None:
                times = times[:gap_after] + times[gap_after + 5:]
        else:
            times = [lo + 1000 + i * 12 * SEC for i in range(max(ranked10, extra100, 1))]
        for i, t in enumerate(times):
            rows = [RankingRow(rank=1, symbol=syms10[i % len(syms10)], last_u=1_000_000,
                               base_u=1_000_000, change_rate=0.0, vol_qu=100,
                               amount_u=100_000_000)]
            if syms100:
                rows.append(RankingRow(rank=11, symbol=syms100[i % len(syms100)],
                                       last_u=1_000_000, base_u=1_000_000, change_rate=0.0,
                                       vol_qu=100, amount_u=100_000_000))
            store.insert_rankings(t, RankingPage(ranking_type=dv.RT, duration="realtime",
                                                 ranked_at_ms=t, rows=rows))
        store.insert_trades([Trade(symbol=s, ts_ms=lo + 5 * SEC, price_u=1_000_000, qty_u=1000)
                             for s in syms10[:tape10]])
        for i in range(capfill):
            store.record_promotion("C%04d" % i, lo + 10 * SEC, 2, 3, "capacity_fill", 0.5)
        for sym, offset_s in tier3:
            store.record_promotion(sym, lo + offset_s * SEC, 2, 3, "ranking_tier3", 0.9)
        for i in range(hold_expired):
            store.record_promotion("H%04d" % i, lo + 20 * SEC, 3, 2, "ranking_hold_expired", 0.1)
    finally:
        store.close()


def _cur(db_path: Path):
    import sqlite3
    con = sqlite3.connect("file:%s?mode=ro" % db_path.as_posix(), uri=True)
    return con, con.cursor()


def test_population_and_coverage_queries_follow_the_prereg_definitions(tmp_path):
    db = tmp_path / "t.db"
    _seed(db, "2026-08-14", ranked10=5, tape10=2, extra100=3, capfill=7,
          tier3=(("T000", 30), ("T001", 40), ("T002", 50)), hold_expired=4)
    lo, hi = dv.window_ms("2026-08-14")
    con, cur = _cur(db)
    try:
        p10 = dv.population(cur, 10, lo, hi)
        p100 = dv.population(cur, 100, lo, hi)
        assert sorted(p10) == ["T000", "T001", "T002", "T003", "T004"]
        assert len(p100) == 8                       # rank<=100 은 rank 1 행까지 포함한다
        assert dv.tape_covered(cur, p10, lo, hi) == 2
        assert dv.seat_covered(cur, p10, lo, hi) == 3
        assert dv.capacity_fill_count(cur, lo, hi) == 7
        assert dv.reason_count(cur, "ranking_hold_expired", lo, hi) == 4
        # 창 밖은 세지 않는다
        assert dv.capacity_fill_count(cur, hi + 1, hi + 10_000) == 0
        assert dv.tape_covered(cur, [], lo, hi) == 0
        assert dv.seat_covered(cur, [], lo, hi) == 0
    finally:
        con.close()


def test_seat_coverage_ignores_other_promotion_reasons(tmp_path):
    """A 는 `reason='ranking_tier3'` 만 본다. `capacity_fill` 로 앉은 것은 A 가 아니다."""
    db = tmp_path / "t.db"
    _seed(db, "2026-08-14", ranked10=3, tape10=0)
    lo, hi = dv.window_ms("2026-08-14")
    store = Store(db)
    try:
        store.record_promotion("T000", lo + SEC, 2, 3, "capacity_fill", 0.5)
        store.record_promotion("T001", lo + SEC, 2, 3, "confirm", 0.5)
    finally:
        store.close()
    con, cur = _cur(db)
    try:
        assert dv.seat_covered(cur, ["T000", "T001", "T002"], lo, hi) == 0
    finally:
        con.close()


def test_snap_ms_list_can_be_filtered_by_ranking_type(tmp_path):
    db = tmp_path / "t.db"
    _seed(db, "2026-08-14", ranked10=2, tape10=0)
    lo, hi = dv.window_ms("2026-08-14")
    store = Store(db)
    try:
        store.insert_rankings(lo + 99 * SEC, RankingPage(
            ranking_type="OTHER_TYPE", duration="realtime", ranked_at_ms=lo + 99 * SEC,
            rows=[RankingRow(rank=1, symbol="ZZZ", last_u=1, base_u=1, change_rate=0.0,
                             vol_qu=1, amount_u=1)]))
    finally:
        store.close()
    con, cur = _cur(db)
    try:
        assert len(dv.snap_ms_list(cur, lo, hi)) == 3               # 전 타입 합집합 (§3-4.1)
        assert len(dv.snap_ms_list(cur, lo, hi, dv.RT)) == 2        # 진단용 타입별
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# main() 게이트
# --------------------------------------------------------------------------- #
def _patch_baseline(monkeypatch, days: tuple[tuple, ...], summary: dict) -> None:
    monkeypatch.setattr(dv, "BASELINE_TOP10", days)
    monkeypatch.setattr(dv, "BASELINE_SUMMARY", summary)
    monkeypatch.setattr(dv, "BASELINE_DAYS", tuple(d[0] for d in days))


def test_main_refuses_a_verdict_when_the_baseline_selfcheck_fails(tmp_path, capsys):
    """사전등록된 기준선을 재현 못 하면 판정을 내지 않는다 — 자가검사가 자격이다."""
    db = tmp_path / "t.db"
    _seed(db, "2026-08-13", ranked10=3, tape10=1)      # 얼린 58/11 과 다르다
    rc = dv.main(["--session", "2026-08-13", "--db", str(db),
                  "--log", str(tmp_path / "none.log")])
    out = capsys.readouterr().out
    assert rc == 2
    assert "SELF-CHECK: FAIL" in out
    assert "MISMATCH" in out
    assert "NO VERDICT" in out
    for forbidden in ("BANDING", "[3-1]", "VERDICT EMITTED"):
        assert forbidden not in out


def test_main_degrades_cleanly_when_the_target_window_is_empty(tmp_path, capsys, monkeypatch):
    db = tmp_path / "t.db"
    _seed(db, "2026-08-13", ranked10=4, tape10=1)
    _patch_baseline(monkeypatch, (("2026-08-13", 4, 1, 25.0, 0),), {10: (1, 25.0, 25.0, 25.0),
                                                                   100: (1, 25.0, 25.0, 25.0)})
    rc = dv.main(["--session", "2026-08-14", "--db", str(db),
                  "--log", str(tmp_path / "none.log")])
    out = capsys.readouterr().out
    assert rc == 3
    assert "SELF-CHECK: PASS" in out
    assert "distinct snap_ms in window : 0" in out
    assert "NO VERDICT (target window data absent)" in out
    for forbidden in ("[3-1]", "[3-3]", "VERDICT EMITTED"):
        assert forbidden not in out


def test_main_reports_invalid_on_a_gap_and_emits_no_band(tmp_path, capsys, monkeypatch):
    db = tmp_path / "t.db"
    _seed(db, "2026-08-13", ranked10=4, tape10=1)
    _seed(db, "2026-08-14", ranked10=8, tape10=4, capfill=200, dense=True, gap_after=100)
    _patch_baseline(monkeypatch, (("2026-08-13", 4, 1, 25.0, 0),), {10: (1, 25.0, 25.0, 25.0),
                                                                   100: (1, 25.0, 25.0, 25.0)})
    log = tmp_path / "collector.log"
    log.write_text(_telemetry("2026-08-14 23:00:00,000", "sig1", 8, 3) + "\n", encoding="utf-8")
    rc = dv.main(["--session", "2026-08-14", "--db", str(db), "--log", str(log)])
    out = capsys.readouterr().out
    assert rc == 4
    assert "TRIGGERED" in out and "3-4.1 gaps>60s (1)" in out
    assert "INVALID" in out
    # 진단은 나오지만 커버리지 표와 밴드는 안 나온다 (§3-4.4)
    assert "WINDOW DIAGNOSTICS" in out and "md_peak_1s" in out
    for forbidden in ("A cover%", "[3-1]", "VERDICT EMITTED"):
        assert forbidden not in out


def test_main_reports_invalid_when_config_sig_changes_in_window(tmp_path, capsys, monkeypatch):
    db = tmp_path / "t.db"
    _seed(db, "2026-08-13", ranked10=4, tape10=1)
    _seed(db, "2026-08-14", ranked10=8, tape10=4, capfill=200, dense=True)
    _patch_baseline(monkeypatch, (("2026-08-13", 4, 1, 25.0, 0),), {10: (1, 25.0, 25.0, 25.0),
                                                                   100: (1, 25.0, 25.0, 25.0)})
    log = tmp_path / "collector.log"
    log.write_text("\n".join([
        _telemetry("2026-08-14 23:00:00,000", "sigA", 8, 3),
        _telemetry("2026-08-15 02:00:00,000", "sigB", 8, 3),     # 창 안에서 서명이 바뀌었다
    ]) + "\n", encoding="utf-8")
    rc = dv.main(["--session", "2026-08-14", "--db", str(db), "--log", str(log)])
    out = capsys.readouterr().out
    assert rc == 4
    assert "3-4.2 config_sig distinct=2" in out
    assert "[3-1]" not in out


def test_main_reports_partial_when_the_session_starts_late(tmp_path, capsys, monkeypatch):
    db = tmp_path / "t.db"
    _seed(db, "2026-08-13", ranked10=4, tape10=1)
    _seed(db, "2026-08-14", ranked10=8, tape10=4, dense=True, late_start_s=3600)
    _patch_baseline(monkeypatch, (("2026-08-13", 4, 1, 25.0, 0),), {10: (1, 25.0, 25.0, 25.0),
                                                                   100: (1, 25.0, 25.0, 25.0)})
    log = tmp_path / "collector.log"
    log.write_text(_telemetry("2026-08-14 23:00:00,000", "sig1", 8, 3) + "\n", encoding="utf-8")
    rc = dv.main(["--session", "2026-08-14", "--db", str(db), "--log", str(log)])
    out = capsys.readouterr().out
    assert rc == 4
    assert "PARTIAL DATA" in out and "3-4.4 partial data" in out
    assert "[3-1]" not in out


def test_main_emits_bands_and_cooldown_violations_when_valid(tmp_path, capsys, monkeypatch):
    db = tmp_path / "t.db"
    _seed(db, "2026-08-13", ranked10=4, tape10=1)
    # top-10 모집단 8, 테이프 4 -> B=50% (>25.9 -> INCREASED)
    # 좌석 4/8 -> A=50% (45~65 -> SIM MATCHED), 비용 100 (<155 -> CLEARLY DECREASED)
    # T000 은 300 초 간격으로 두 번 앉는다 -> 쿨다운 위반 1건
    _seed(db, "2026-08-14", ranked10=8, tape10=4, capfill=100, dense=True, hold_expired=2,
          tier3=(("T000", 60), ("T000", 360), ("T001", 120), ("T002", 180), ("T003", 240)))
    _patch_baseline(monkeypatch, (("2026-08-13", 4, 1, 25.0, 0),), {10: (1, 25.0, 25.0, 25.0),
                                                                   100: (1, 25.0, 25.0, 25.0)})
    log = tmp_path / "collector.log"
    log.write_text("\n".join([
        _telemetry("2026-08-14 23:00:00,000", "sig1", 9, 3),
        _telemetry("2026-08-15 02:00:00,000", "sig1", 10, 4),
    ]) + "\n", encoding="utf-8")
    rc = dv.main(["--session", "2026-08-14", "--db", str(db), "--log", str(log)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "INVALIDATION: none triggered" in out
    assert "[3-1] B top-10 50.0%" in out and "INCREASED" in out
    assert "[3-2] A top-10 50.0%" in out and "SIM MATCHED LIVE" in out
    assert "[3-3] cost 100" in out and "CLEARLY DECREASED" in out
    assert "55.1% [UNREPRODUCED]" in out
    assert "cooldown violations" in out and "300.0s" in out
    assert "ranking_hold_expired releases in window   : 2" in out
    assert "VERDICT EMITTED" in out


def test_main_output_is_pure_ascii_on_the_valid_path(tmp_path, capsys, monkeypatch):
    """cp949 콘솔에서 죽지 않아야 한다 (§1f). 이 레포에서 실제로 그것 때문에 죽은 적이 있다."""
    db = tmp_path / "t.db"
    _seed(db, "2026-08-13", ranked10=4, tape10=1)
    _seed(db, "2026-08-14", ranked10=8, tape10=4, capfill=100, dense=True)
    _patch_baseline(monkeypatch, (("2026-08-13", 4, 1, 25.0, 0),), {10: (1, 25.0, 25.0, 25.0),
                                                                   100: (1, 25.0, 25.0, 25.0)})
    log = tmp_path / "collector.log"
    # 로그에 비 ASCII 를 섞어도 출력은 ASCII 여야 한다
    log.write_text(_telemetry("2026-08-14 23:00:00,000", "sig어", 9, 3) + "\n",
                   encoding="utf-8")
    dv.main(["--session", "2026-08-14", "--db", str(db), "--log", str(log)])
    out = capsys.readouterr().out
    out.encode("cp949")                      # 여기서 UnicodeEncodeError 가 나면 실패
    assert out.isascii()


def test_main_marks_a_baseline_session_run_as_a_dry_run(tmp_path, capsys, monkeypatch):
    """기준선 세션으로 돌린 출력이 판정으로 오독되지 않게 표시한다."""
    db = tmp_path / "t.db"
    _seed(db, "2026-08-13", ranked10=8, tape10=2, capfill=200, dense=True)
    _patch_baseline(monkeypatch, (("2026-08-13", 8, 2, 25.0, 200),), {10: (1, 25.0, 25.0, 25.0),
                                                                     100: (1, 25.0, 25.0, 25.0)})
    log = tmp_path / "collector.log"
    log.write_text(_telemetry("2026-08-13 23:00:00,000", "sig1", 9, 3) + "\n", encoding="utf-8")
    rc = dv.main(["--session", "2026-08-13", "--db", str(db), "--log", str(log)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "BASELINE session" in out and "DRY RUN" in out


def test_main_says_operator_must_confirm_the_no_probe_condition(tmp_path, capsys, monkeypatch):
    """§3-4.3 은 기계로 확인할 수 없다. 지어낸 검사보다 정직한 라벨이 낫다."""
    db = tmp_path / "t.db"
    _seed(db, "2026-08-13", ranked10=4, tape10=1)
    _seed(db, "2026-08-14", ranked10=8, tape10=4, capfill=200, dense=True)
    _patch_baseline(monkeypatch, (("2026-08-13", 4, 1, 25.0, 0),), {10: (1, 25.0, 25.0, 25.0),
                                                                   100: (1, 25.0, 25.0, 25.0)})
    log = tmp_path / "collector.log"
    log.write_text(_telemetry("2026-08-14 23:00:00,000", "sig1", 9, 3) + "\n", encoding="utf-8")
    dv.main(["--session", "2026-08-14", "--db", str(db), "--log", str(log)])
    out = capsys.readouterr().out
    assert "OPERATOR CONFIRMATION REQUIRED" in out
    assert "CANNOT prove" in out
    assert "OPERATOR MUST CONFIRM" in out


def test_main_flags_the_boundary_between_frozen_constant_and_exact_baseline_max(
        tmp_path, capsys, monkeypatch):
    """얼린 25.9% 와 정확한 기준선 최대(예: 25.9259%) 사이에 떨어지면 운영자에게 넘긴다."""
    db = tmp_path / "t.db"
    # 기준선 세션의 정확한 최대를 27/104 = 25.9615% 로 만든다
    _seed(db, "2026-08-13", ranked10=104, tape10=27)
    _patch_baseline(monkeypatch, (("2026-08-13", 104, 27, 25.96, 0),),
                    {10: (1, 25.96, 25.96, 25.96), 100: (1, 25.96, 25.96, 25.96)})
    # 대상 창: 1000 종목 중 259 -> 25.9% ... 를 살짝 넘는 25.91% 를 만든다 (2591/10000 은 과하므로
    # 200 중 52 = 26.0% 는 위쪽 밖이다. 104 중 27 = 25.9615% 가 딱 사이에 들어간다)
    _seed(db, "2026-08-14", ranked10=104, tape10=27, capfill=200, dense=True)
    log = tmp_path / "collector.log"
    log.write_text(_telemetry("2026-08-14 23:00:00,000", "sig1", 9, 3) + "\n", encoding="utf-8")
    rc = dv.main(["--session", "2026-08-14", "--db", str(db), "--log", str(log)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "BOUNDARY" in out
    assert "OPERATOR MUST DECIDE" in out


def test_seeded_fixture_never_touches_the_live_worktree(tmp_path):
    """이 테스트 파일이 라이브 경로를 기본값으로 건드리지 않는다는 것을 못 박는다."""
    assert dv.DEFAULT_DB.endswith("w5-ops/data/tossmon.db")
    assert "w5-ops" not in str(tmp_path)
    # 기본 인자 없이 도는 테스트가 없다는 것 — 모든 main() 호출에 --db 를 준다
    src = Path(__file__).read_text(encoding="utf-8")
    for call in re.findall(r"dv\.main\(\[(.*?)\]\)", src, re.S):
        assert "--db" in call


def test_run_self_check_returns_the_recomputed_percentages(tmp_path, capsys, monkeypatch):
    import sqlite3
    db = tmp_path / "t.db"
    _seed(db, "2026-08-13", ranked10=4, tape10=1)
    _patch_baseline(monkeypatch, (("2026-08-13", 4, 1, 25.0, 0),), {10: (1, 25.0, 25.0, 25.0),
                                                                   100: (1, 25.0, 25.0, 25.0)})
    con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True)
    try:
        ok, pct10, pct100 = dv.run_self_check(con.cursor())
    finally:
        con.close()
    assert ok is True
    assert pct10 == pytest.approx([25.0])
    assert pct100 == pytest.approx([25.0])
    assert "SELF-CHECK: PASS" in capsys.readouterr().out


def test_utc_and_kst_strings_are_nine_hours_apart():
    ms = int(dt.datetime(2026, 8, 14, 13, 30, tzinfo=dt.timezone.utc).timestamp() * 1000)
    assert dv.utc_str(ms) == "2026-08-14 13:30:00"
    assert dv.kst_str(ms) == "2026-08-14 22:30:00"

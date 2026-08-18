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


def test_log_sources_takes_numbered_backups_newest_last_and_skips_stdout(tmp_path):
    """`RotatingFileHandler` 번호 백업(`collector.log.N`)도 창에 들어와야 한다.

    2026-08-17 창이 통째로 `collector.log.1` 에 들어가 있었는데 러너가 그 이름을 몰라
    `telemetry=0` -> `3-4.2 config_sig distinct=0` 으로 유효한 창이 무효가 났다.
    `.1` 이 `.2` 보다 **새 것**이므로 순서는 `.2` -> `.1` -> 살아 있는 로그다.
    """
    lo, _ = dv.window_ms("2026-08-14")
    log = tmp_path / "collector.log"
    log.write_text("live\n", encoding="utf-8")
    b1 = tmp_path / "collector.log.1"          # 살아 있는 로그 바로 앞 구간
    b2 = tmp_path / "collector.log.2"          # 그보다 앞
    for q in (b1, b2):
        q.write_text("x\n", encoding="utf-8")
    gz = tmp_path / "collector.20260815-001000.log.gz"     # 창 안 .gz 회전
    _write_gz(gz, ["x"])
    stdout = tmp_path / "collector.stdout.log"             # 다른 논리 파일
    stdout_b1 = tmp_path / "collector.stdout.log.1"        # 그 번호 백업
    for q in (stdout, stdout_b1):
        q.write_text("x\n", encoding="utf-8")

    got = dv.log_sources(log, lo)
    assert got == [gz, b2, b1, log]
    assert stdout not in got and stdout_b1 not in got


def test_scan_log_merges_telemetry_from_a_numbered_backup(tmp_path):
    """창 전체가 `collector.log.1` 에 있어도 `config_sig` 가 한 종으로 잡혀야 한다."""
    lo, hi = dv.window_ms("2026-08-14")
    sig = "rank3:TVOLUME@100,t3max10,rkpTVOLUME@10/k2/h300s/rotate/cd600s"
    log = tmp_path / "collector.log"
    log.write_text("", encoding="utf-8")       # 회전 직후: 살아 있는 로그는 비어 있다
    (tmp_path / "collector.log.1").write_text("\n".join([
        _telemetry("2026-08-14 23:00:00,000", sig, 7, 3),
        _telemetry("2026-08-15 01:00:00,000", sig, 10, 3),
        _telemetry("2026-08-15 06:00:00,000", sig, 99, 99),   # 창 밖 (05:00 KST 이후)
    ]) + "\n", encoding="utf-8")

    got = dv.scan_log(log, lo, hi)
    assert got["telemetry"] == 2
    assert list(got["sigs"]) == [sig] and got["sigs"][sig] == 2
    assert got["md"] == [7, 10]


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


# --------------------------------------------------------------------------- #
# §6-2 미관측 총량 — 개정 1 의 핵심. 앞머리·꼬리는 전액, 내부만 60 초 면제
# --------------------------------------------------------------------------- #
def test_unobserved_breakdown_charges_edges_in_full_and_exempts_60s_inside():
    lo, hi = 0, 23_400 * SEC
    # 창 시작 100 초 뒤 시작, 끝나기 50 초 전 종료, 내부에 200 초 공백 하나
    snaps = [100 * SEC, 300 * SEC, 500 * SEC, hi - 50 * SEC]
    head, tail, interior = dv.unobserved_breakdown(snaps, lo, hi, 60)
    assert head == 100.0                      # 전액 — 60 초 면제를 주지 않는다
    assert tail == 50.0                       # 50 < 60 인데도 전액 센다
    # 내부: 200-60=140, 200-60=140, (23350-500)-60=22790
    assert interior == pytest.approx(140.0 + 140.0 + (22_850.0 - 60.0))


def test_unobserved_breakdown_is_zero_on_a_perfectly_covered_window():
    lo, hi = 0, 23_400 * SEC
    snaps = list(range(lo, hi + 1, 60 * SEC))      # 60 초 간격, 양 끝에 딱 붙는다
    assert dv.unobserved_breakdown(snaps, lo, hi, 60) == (0.0, 0.0, 0.0)


def test_unobserved_breakdown_of_an_empty_window_is_the_whole_window():
    """스냅이 없으면 0 이 아니라 창 전체다 — 0 은 '완전히 관측했다'는 정반대 뜻이 된다."""
    lo, hi = 0, 23_400 * SEC
    assert dv.unobserved_breakdown([], lo, hi, 60) == (23_400.0, 0.0, 0.0)


def test_the_6_2_threshold_is_half_a_percent_of_the_window():
    assert dv.UNOBS_FRAC_LIMIT == 0.005
    assert dv.WINDOW_S == 23_400.0
    assert dv.UNOBS_FRAC_LIMIT * dv.WINDOW_S == pytest.approx(117.0)


def test_6_2_reproduces_the_two_sessions_6_3_disclosed():
    """§6-3 이 *"규칙을 쓸 때 이미 알고 있었다"* 고 공개한 두 값을 식으로 재현한다.

    08-14 는 46.5 초 -> 0.20% (통과), 08-05 는 3,955 초 -> 16.9% (무효). 새 규칙이
    무엇을 통과시키고 무엇을 잡는지가 이 두 줄에 다 들어 있다.
    """
    frac_0814 = (33.7 + 10.5 + 2.3) / dv.WINDOW_S
    frac_0805 = (3929.5 + 15.2 + 10.6) / dv.WINDOW_S
    assert round(100 * frac_0814, 2) == 0.20 and frac_0814 <= dv.UNOBS_FRAC_LIMIT
    assert round(100 * frac_0805, 1) == 16.9 and frac_0805 > dv.UNOBS_FRAC_LIMIT


def test_a_single_gap_no_longer_decides_but_the_total_does():
    """개정 1 의 축 변경: 61 초 공백 하나로는 안 걸리고, 총량이 넘으면 걸린다."""
    lo, hi = 0, 23_400 * SEC
    # (a) 61 초 공백 하나 — 옛 §3-4.1 이면 무효, §6-2 에서는 1 초만 청구된다
    one = list(range(lo, hi + 1, 60 * SEC))
    one[100] += 1 * SEC                        # 61 초 공백 1 건 (뒤 간격은 59 초)
    assert len(dv.find_gaps(one, 60)) == 1
    assert sum(dv.unobserved_breakdown(one, lo, hi, 60)) / dv.WINDOW_S <= dv.UNOBS_FRAC_LIMIT
    # (b) 60 초를 넘는 공백은 하나도 없는데 꼬리만 3,929.5 초 — 옛 규칙은 통과시켰다
    early_stop = [t for t in range(lo, hi - 3929 * SEC, 60 * SEC)]
    assert dv.find_gaps(early_stop, 60) == []
    frac = sum(dv.unobserved_breakdown(early_stop, lo, hi, 60)) / dv.WINDOW_S
    assert frac > dv.UNOBS_FRAC_LIMIT


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
    # §6-6 의 유효 6 세션(08-03/06/10/11/12/13)에서 같은 정의가 19.0% 를 낸다 —
    # 두 가운데(17.1, 19.0) 중 위쪽이다. 통계적 중앙값(18.05)으로 바꾸면 재현 못 한다.
    assert dv.prereg_median([11.1, 8.6, 25.9, 17.1, 21.2, 19.0]) == 19.0


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
def test_band_tape_b_boundaries_follow_6_6a_exactly():
    """개정 1: 통과선 25.9 는 그대로, 아래 경계만 17.1 -> 19.0 (더 엄격해진 방향)."""
    assert dv.band_tape_b(25.91)[0].startswith("INCREASED")
    assert dv.band_tape_b(25.9)[0].startswith("VERDICT WITHHELD")   # "> 25.9" 라 미포함
    assert dv.band_tape_b(19.0)[0].startswith("VERDICT WITHHELD")   # "19.0~25.9" 라 포함
    assert dv.band_tape_b(18.99)[0].startswith("NOT INCREASED")
    # 개정 전이면 보류였을 구간이 이제 "안 늘었다"로 떨어진다
    assert dv.band_tape_b(17.1)[0].startswith("NOT INCREASED")
    assert all(b[1] == "6-6a" for b in (dv.band_tape_b(0.0), dv.band_tape_b(99.0)))


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


def test_frozen_pre_revision_summaries_match_prereg_2():
    """§2 의 두 요약 줄은 **개정 전** 값이다 -> `BASELINE_SUMMARY_PREREV` 와 대조한다."""
    body = _section(_prereg_text(), "## 2.", "## 3.")
    lines = [ln for ln in body.splitlines() if ln.startswith("**n=")]
    assert len(lines) == 2, "expected one summary line for top-10 and one for top-100"
    for line, topn in zip(lines, (10, 100)):
        n = int(re.search(r"n=(\d+)", line).group(1))
        pcts = [float(x) for x in re.findall(r"([\d.]+)%", line)]
        assert (n, *pcts) == dv.BASELINE_SUMMARY_PREREV[topn]


def test_frozen_revised_summaries_match_prereg_6_6():
    """현행 요약(유효 6 세션)은 §6-6 의 표에서 뽑아 `BASELINE_SUMMARY` 와 대조한다."""
    body = _section(_prereg_text(), "## 6-6.", "### 6-6a.")
    got = {}
    for ln in body.splitlines():
        if not ln.startswith("| top-"):
            continue
        nums = [float(x) for x in re.findall(r"[\d.]+", ln.replace("*", ""))]
        topn, n, lo, med, hi = nums
        got[int(topn)] = (int(n), lo, med, hi)
    assert got == dv.BASELINE_SUMMARY
    # 개정으로 표본이 줄었다는 사실 자체를 못 박는다 (§6-6b 가 검정력 대가를 적은 이유)
    assert got[10][0] == 6 and dv.BASELINE_SUMMARY_PREREV[10][0] == 10


def test_frozen_unobserved_table_matches_prereg_6_6():
    """§6-6 의 재판정 표(unobs% / VALID) 를 파싱해 `BASELINE_UNOBS` 와 대조한다.

    08-14 줄도 그 표에 있지만 **기준선 세션이 아니다** — §6-6c 가 *"규칙의 효과를
    검산하려고 적은 것이지 판정이 아니다"* 라고 못 박았으므로 여기서도 제외한다.
    """
    body = _section(_prereg_text(), "## 6-6.", "### 6-6a.")
    rows = re.findall(r"^(\d{4}-\d{2}-\d{2})\s+[\d.]+\s+[\d.]+\s+[\d.]+\s+[\d.]+\s+"
                      r"([\d.]+)%\s+(VALID|INVALID)", body, re.M)
    assert len(rows) == 11, "6-6 table should carry the 10 baseline sessions plus 08-14"
    parsed = tuple((d, float(p), v == "VALID") for d, p, v in rows
                   if d in dv.BASELINE_DAYS)
    assert parsed == dv.BASELINE_UNOBS
    # 08-14 는 표에 유효로 적혀 있지만 기준선에 들어가지 않는다
    assert [d for d, _, _ in rows if d not in dv.BASELINE_DAYS] == ["2026-08-14"]
    # 얼린 라벨이 얼린 숫자와 모순되지 않는가 — 문서 자체의 자기정합성
    for _day, pct, valid in dv.BASELINE_UNOBS:
        assert valid == (pct / 100.0 <= dv.UNOBS_FRAC_LIMIT)
    assert sum(1 for _, _, v in dv.BASELINE_UNOBS if v) == 6


def test_frozen_gap_rule_matches_prereg_6_2():
    """§6-2 의 식과 문턱을 문서에서 뽑아 미러링 상수와 대조한다."""
    s62 = _section(_prereg_text(), "## 6-2.", "## 6-3.")
    assert float(re.search(r"unobserved_frac\s*>\s*([\d.]+)", s62).group(1)) \
        == dv.UNOBS_FRAC_LIMIT
    assert float(re.search(r"unobserved_s\s*/\s*(\d+)", s62).group(1)) == dv.WINDOW_S
    # 60 은 `max(0, ... - 60)` 안의 면제분으로만 남는다 — 더 이상 무효 문턱이 아니다
    assert int(re.search(r"max\(0,.*?-\s*(\d+)\)", s62).group(1)) == dv.GAP_LIMIT_S
    assert float(re.search(r"0\.5% = ([\d.]+) ", s62.replace("(", "").replace(")", ""))
                 .group(1)) == dv.UNOBS_FRAC_LIMIT * dv.WINDOW_S


def test_frozen_band_edges_match_the_prereg_document():
    """§6-6a / §3-2 / §3-3 의 숫자를 문서에서 뽑아 미러링 상수와 대조한다."""
    text = _prereg_text()

    # §3-1 의 표는 취소선으로 얼려 있고 "아래 경계를 인용하지 마라"고 적혀 있다.
    # 현행 경계는 §6-6a 의 표에서만 읽는다
    s66a = _section(text, "### 6-6a.", "### 6-6b.")
    rows = {}
    for ln in s66a.splitlines():
        if not ln.startswith("|") or "n=" not in ln:
            continue
        nums = [float(x) for x in re.findall(r"[\d.]+", ln.replace("*", "").replace("~", ""))]
        rows[int(nums[0])] = nums[1:]          # n -> [통과선, 보류lo, 보류hi, 아래경계]
    assert rows[6] == [dv.B_MAX, dv.B_MED, dv.B_MAX, dv.B_MED]
    assert rows[10] == [dv.B_MAX, dv.B_MED_PREREV, dv.B_MAX, dv.B_MED_PREREV]
    # 통과선이 개정에서 안 움직였다는 것 — §6-6a 의 방어 논거 전체가 여기 걸려 있다
    assert rows[6][0] == rows[10][0] == dv.B_MAX
    assert dv.B_MED > dv.B_MED_PREREV, "revision 1 must have made the lower edge stricter"

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


def test_the_struck_3_4_1_rule_is_marked_superseded_in_the_document():
    """§3-4.1 은 지워지지 않았다. **취소선 + §6-2 로 대체됐다는 표시**가 있어야 한다.

    이 파일의 §1~§4 는 결과를 본 뒤 고치지 않는다는 것이 사전등록의 값어치이므로,
    옛 규칙이 조용히 삭제되면 그것 자체가 사고다 (§0 이 적은 `f1693c8` 사고).
    """
    s34 = _section(_prereg_text(), "### 3-4.", "## 4.")
    first = s34.split("2.")[0]
    assert "~~" in first, "3-4.1 must stay in the document, struck through"
    assert "6-2" in first, "3-4.1 must point at the clause that replaced it"
    assert "60" in first and "0.5%" in first


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
          dense: bool = False, gap_after: int | None = None, gap_len: int = 5,
          late_start_s: int = 0, step_s: int = 60) -> None:
    """한 정규장 창에 합성 행을 심는다.

    `dense=True` 면 창 전체를 `step_s` 간격 스냅으로 채운다 (공백 0, 양쪽 끝 0 초) —
    무효 조건을 안 건드리는 대상 세션을 만들 때 쓴다. `gap_after` 는 그중 n 번째 스냅
    뒤 `gap_len` 개를 지워 공백을 하나 만든다. 공백 길이는 `(gap_len+1)*step_s` 초이고
    §6-2 가 청구하는 몫은 거기서 60 초를 뺀 값이다.

    `late_start_s` 를 쓸 때는 `step_s` 가 `23400 - late_start_s` 를 나누는지 보라.
    안 나누면 **꼬리에 나머지가 남아** 앞머리 말고 미관측이 더 붙는다 (60 초 격자로
    100 초 늦게 시작하면 꼬리 20 초가 더 생겨 총 120 초 = 0.513% 로 예산을 넘긴다).
    """
    lo, hi = dv.window_ms(day)
    store = Store(db_path)
    try:
        syms10 = ["T%03d" % i for i in range(ranked10)]
        syms100 = ["U%03d" % i for i in range(extra100)]
        if dense:
            step = step_s * SEC
            times = list(range(lo + late_start_s * SEC, hi + 1, step))
            if gap_after is not None:
                times = times[:gap_after] + times[gap_after + gap_len:]
        else:
            times = [lo + 1000 + i * 12 * SEC for i in range(max(ranked10, extra100, 1))]
        # 스냅 수가 종목 수보다 적으면 모집단이 조용히 깎인다 — 픽스처 실수를 여기서 잡는다.
        assert len(times) >= max(ranked10, extra100), (
            "fixture needs >= %d snapshots to seed %d symbols, got %d"
            % (max(ranked10, extra100), max(ranked10, extra100), len(times)))
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
def _patch_baseline(monkeypatch, days: tuple[tuple, ...], summary: dict,
                    unobs: tuple[tuple, ...] | None = None) -> None:
    """합성 기준선으로 갈아끼운다. 검증 대상은 *기계*이고 얼린 값 자체는 앞의 미러링
    테스트가 지킨다.

    `unobs` 를 안 주면 모든 세션을 **유효(0%)** 로 본다 — `dense=True` 픽스처가 창을
    60 초 간격으로 꽉 채우므로 실제 재계산도 0% 가 나온다. 유효 집합과 전체 집합이
    같아지므로 개정 전 요약도 같은 값으로 패치한다.
    """
    monkeypatch.setattr(dv, "BASELINE_TOP10", days)
    monkeypatch.setattr(dv, "BASELINE_SUMMARY", summary)
    monkeypatch.setattr(dv, "BASELINE_SUMMARY_PREREV", summary)
    monkeypatch.setattr(dv, "BASELINE_DAYS", tuple(d[0] for d in days))
    monkeypatch.setattr(dv, "BASELINE_UNOBS",
                        unobs if unobs is not None
                        else tuple((d[0], 0.0, True) for d in days))


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
    for forbidden in ("BANDING", "[6-6a]", "VERDICT EMITTED"):
        assert forbidden not in out


def test_main_degrades_cleanly_when_the_target_window_is_empty(tmp_path, capsys, monkeypatch):
    """부재 안내문이 `DEFAULT_SESSION` 이 아니라 `--session` 을 따라가는가 (명세 §2-4).

    개정 전에는 `--session` 이 무엇이든 *"The 2026-08-14 window opens at 22:30 KST"* 라고
    찍혔다 (`daily/2026-08-15.md` §13-1). 그래서 여기서는 일부러 `DEFAULT_SESSION` 과
    **다른** 날짜를 준다 — 같은 날짜를 주면 상수를 박아 둬도 테스트가 통과한다.
    """
    db = tmp_path / "t.db"
    _seed(db, "2026-08-13", ranked10=4, tape10=1, dense=True)
    _patch_baseline(monkeypatch, (("2026-08-13", 4, 1, 25.0, 0),), {10: (1, 25.0, 25.0, 25.0),
                                                                   100: (1, 25.0, 25.0, 25.0)})
    assert dv.DEFAULT_SESSION == "2026-08-15" != "2026-08-16"
    rc = dv.main(["--session", "2026-08-16", "--db", str(db),
                  "--log", str(tmp_path / "none.log")])
    out = capsys.readouterr().out
    assert rc == 3
    assert "SELF-CHECK: PASS" in out
    assert "distinct snap_ms in window : 0" in out
    assert "NO VERDICT (target window data absent)" in out
    assert "The 2026-08-16 window opens at 22:30 KST" in out
    assert "2026-08-14 window opens" not in out
    for forbidden in ("[6-6a]", "[3-3]", "VERDICT EMITTED"):
        assert forbidden not in out


def test_main_reports_invalid_when_the_unobserved_total_is_over_budget(
        tmp_path, capsys, monkeypatch):
    """§6-2: 판정하는 것은 총량이다. 공백 목록은 진단으로 남지만 결정하지 않는다."""
    db = tmp_path / "t.db"
    _seed(db, "2026-08-13", ranked10=4, tape10=1, dense=True)
    # 60 초 간격 스냅 5 개를 지운다 -> 360 초 공백 1 건, 청구 300 초 = 1.282% > 0.5%
    _seed(db, "2026-08-15", ranked10=8, tape10=4, capfill=200, dense=True, gap_after=100)
    _patch_baseline(monkeypatch, (("2026-08-13", 4, 1, 25.0, 0),), {10: (1, 25.0, 25.0, 25.0),
                                                                   100: (1, 25.0, 25.0, 25.0)})
    log = tmp_path / "collector.log"
    log.write_text(_telemetry("2026-08-15 23:00:00,000", "sig1", 8, 3) + "\n", encoding="utf-8")
    rc = dv.main(["--session", "2026-08-15", "--db", str(db), "--log", str(log)])
    out = capsys.readouterr().out
    assert rc == 4
    assert "TRIGGERED" in out and "6-2 unobserved 1.282% > 0.5%" in out
    assert "interior_s      =      300.0" in out       # 360 - 60 면제
    assert "head_s          =        0.0" in out and "tail_s          =        0.0" in out
    assert "OVER BUDGET" in out
    # 공백 목록은 남지만 판정하지 않는다고 명시한다 (명세 §2-1)
    assert "gap list (DIAGNOSTIC, decides nothing): 1 gap(s) over 60s" in out
    assert "360.0s   charged 300.0s" in out
    assert "INVALID" in out
    # 진단은 나오지만 커버리지 표와 밴드는 안 나온다 (§3-4.4)
    assert "WINDOW DIAGNOSTICS" in out and "md_peak_1s" in out
    for forbidden in ("A cover%", "[6-6a]", "VERDICT EMITTED"):
        assert forbidden not in out


def test_main_passes_a_gap_over_60s_when_the_total_stays_inside_budget(
        tmp_path, capsys, monkeypatch):
    """개정의 축 변경을 반대편에서 확인한다 — 옛 §3-4.1 이면 무효였을 창이 통과한다.

    스냅 하나만 지우면 120 초 공백 1 건(청구 60 초)이다. 옛 규칙은 *"60 초 초과 공백이
    1 건이라도"* 무효였으므로 걸렸지만, §6-2 에서는 0.256% 로 예산 안이다.
    """
    db = tmp_path / "t.db"
    _seed(db, "2026-08-13", ranked10=4, tape10=1, dense=True)
    _seed(db, "2026-08-15", ranked10=8, tape10=1, capfill=200, dense=True, gap_after=100,
          gap_len=1)
    _patch_baseline(monkeypatch, (("2026-08-13", 4, 1, 25.0, 0),), {10: (1, 25.0, 25.0, 25.0),
                                                                   100: (1, 25.0, 25.0, 25.0)})
    log = tmp_path / "collector.log"
    log.write_text(_telemetry("2026-08-15 23:00:00,000", "sig1", 8, 3) + "\n", encoding="utf-8")
    rc = dv.main(["--session", "2026-08-15", "--db", str(db), "--log", str(log)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "gap list (DIAGNOSTIC, decides nothing): 1 gap(s) over 60s" in out
    assert "WITHIN BUDGET" in out
    assert "INVALIDATION: none triggered" in out
    assert "VERDICT EMITTED" in out


def test_main_reports_invalid_when_config_sig_changes_in_window(tmp_path, capsys, monkeypatch):
    db = tmp_path / "t.db"
    _seed(db, "2026-08-13", ranked10=4, tape10=1, dense=True)
    _seed(db, "2026-08-15", ranked10=8, tape10=4, capfill=200, dense=True)
    _patch_baseline(monkeypatch, (("2026-08-13", 4, 1, 25.0, 0),), {10: (1, 25.0, 25.0, 25.0),
                                                                   100: (1, 25.0, 25.0, 25.0)})
    log = tmp_path / "collector.log"
    log.write_text("\n".join([
        _telemetry("2026-08-15 23:00:00,000", "sigA", 8, 3),
        _telemetry("2026-08-16 02:00:00,000", "sigB", 8, 3),     # 창 안에서 서명이 바뀌었다
    ]) + "\n", encoding="utf-8")
    rc = dv.main(["--session", "2026-08-15", "--db", str(db), "--log", str(log)])
    out = capsys.readouterr().out
    assert rc == 4
    assert "3-4.2 config_sig distinct=2" in out
    assert "[6-6a]" not in out


def test_main_charges_a_late_start_to_the_total_instead_of_a_separate_partial_rule(
        tmp_path, capsys, monkeypatch):
    """W3 결정(명세 §2-1): 가장자리 검사를 §6-2 총량에 접었다. 라벨은 남지만 판정은 총량이 한다.

    1 시간 늦게 시작한 창은 `head_s = 3600` -> 15.385% 로 §6-2 가 잡는다. 옛 러너는 같은
    창을 *"3-4.4 partial data"* 라는 **별도 조건**으로 잡았다 — 그 조건은 이제 없다.
    """
    db = tmp_path / "t.db"
    _seed(db, "2026-08-13", ranked10=4, tape10=1, dense=True)
    _seed(db, "2026-08-15", ranked10=8, tape10=4, dense=True, late_start_s=3600)
    _patch_baseline(monkeypatch, (("2026-08-13", 4, 1, 25.0, 0),), {10: (1, 25.0, 25.0, 25.0),
                                                                   100: (1, 25.0, 25.0, 25.0)})
    log = tmp_path / "collector.log"
    log.write_text(_telemetry("2026-08-15 23:00:00,000", "sig1", 8, 3) + "\n", encoding="utf-8")
    rc = dv.main(["--session", "2026-08-15", "--db", str(db), "--log", str(log)])
    out = capsys.readouterr().out
    assert rc == 4
    assert "head_s          =     3600.0" in out       # 60 초 면제 없이 전액
    assert "6-2 unobserved 15.385% > 0.5%" in out
    assert "3-4.4 partial data" not in out             # 별도 조건은 사라졌다
    assert "AN EDGE EXCEEDS IT" in out                 # 라벨은 남는다
    assert "FOLDED INTO 6-2, KEPT AS A LABEL" in out
    assert "[6-6a]" not in out


def test_a_late_start_inside_budget_is_no_longer_invalidated_by_the_edge_rule(
        tmp_path, capsys, monkeypatch):
    """겹침을 풀 때 실제로 갈리는 자리 — 100 초 늦게 시작한 창.

    옛 가장자리 규칙(head > 60s)이면 무효였다. §6-2 에서는 0.427% 로 사용자가 고른 0.5%
    안이다. 둘 다 두면 *"게다가 한쪽 가장자리 60 초 이하"* 라는 아무도 쓰지 않은 규칙이
    사용자 결정 위에 얹힌다 — 그래서 접었다.
    """
    db = tmp_path / "t.db"
    _seed(db, "2026-08-13", ranked10=4, tape10=1, dense=True)
    # 50 초 격자라야 23,400-100 이 딱 나뉘어 꼬리가 0 이 된다 -> 미관측은 앞머리 100 초뿐
    _seed(db, "2026-08-15", ranked10=8, tape10=1, capfill=200, dense=True, late_start_s=100,
          step_s=50)
    _patch_baseline(monkeypatch, (("2026-08-13", 4, 1, 25.0, 0),), {10: (1, 25.0, 25.0, 25.0),
                                                                   100: (1, 25.0, 25.0, 25.0)})
    log = tmp_path / "collector.log"
    log.write_text(_telemetry("2026-08-15 23:00:00,000", "sig1", 8, 3) + "\n", encoding="utf-8")
    rc = dv.main(["--session", "2026-08-15", "--db", str(db), "--log", str(log)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "head_s          =      100.0" in out
    assert "AN EDGE EXCEEDS IT" in out            # 라벨은 켜지지만
    assert "WITHIN BUDGET" in out                 # 판정은 총량이 한다
    assert "INVALIDATION: none triggered" in out
    assert "VERDICT EMITTED" in out


def test_main_seals_the_08_14_window_and_refuses_to_re_judge_it(tmp_path, capsys, monkeypatch):
    """§6-4/§6-6c: 08-14 는 새 규칙에서 유효 범위여도 **재판정하지 않는다**.

    사용자 결정이 *"앞으로만"* 이었다. 그 밤의 공백을 본 뒤에 바뀐 규칙으로 다시 재면
    개정의 정당성을 잃는다. 러너는 숫자만 찍고 판정을 내지 않아야 한다.
    """
    db = tmp_path / "t.db"
    _seed(db, "2026-08-13", ranked10=4, tape10=1, dense=True)
    # 새 규칙이면 통과했을 창을 심는다 (공백 0) — 그래도 판정이 나오면 안 된다
    _seed(db, "2026-08-14", ranked10=8, tape10=4, capfill=200, dense=True)
    _patch_baseline(monkeypatch, (("2026-08-13", 4, 1, 25.0, 0),), {10: (1, 25.0, 25.0, 25.0),
                                                                   100: (1, 25.0, 25.0, 25.0)})
    log = tmp_path / "collector.log"
    log.write_text(_telemetry("2026-08-14 23:00:00,000", "sig1", 8, 3) + "\n", encoding="utf-8")
    rc = dv.main(["--session", "2026-08-14", "--db", str(db), "--log", str(log)])
    out = capsys.readouterr().out
    assert rc == 4
    assert "SEALED SESSION" in out
    assert "6-4 sealed session (kept INVALID, not re-judged)" in out
    assert "WITHIN BUDGET" in out                 # 숫자는 숨기지 않는다
    assert "INVALID" in out
    for forbidden in ("A cover%", "[6-6a]", "VERDICT EMITTED"):
        assert forbidden not in out


def test_only_08_14_is_sealed():
    """봉인은 §6-4 가 이름 붙인 창 하나뿐이다. 오늘 밤 창은 봉인되면 안 된다."""
    assert set(dv.SEALED_SESSIONS) == {"2026-08-14"}
    assert dv.DEFAULT_SESSION not in dv.SEALED_SESSIONS


def test_main_emits_bands_and_cooldown_violations_when_valid(tmp_path, capsys, monkeypatch):
    db = tmp_path / "t.db"
    _seed(db, "2026-08-13", ranked10=4, tape10=1, dense=True)
    # top-10 모집단 8, 테이프 4 -> B=50% (>25.9 -> INCREASED)
    # 좌석 4/8 -> A=50% (45~65 -> SIM MATCHED), 비용 100 (<155 -> CLEARLY DECREASED)
    # T000 은 300 초 간격으로 두 번 앉는다 -> 쿨다운 위반 1건
    _seed(db, "2026-08-15", ranked10=8, tape10=4, capfill=100, dense=True, hold_expired=2,
          tier3=(("T000", 60), ("T000", 360), ("T001", 120), ("T002", 180), ("T003", 240)))
    _patch_baseline(monkeypatch, (("2026-08-13", 4, 1, 25.0, 0),), {10: (1, 25.0, 25.0, 25.0),
                                                                   100: (1, 25.0, 25.0, 25.0)})
    log = tmp_path / "collector.log"
    log.write_text("\n".join([
        _telemetry("2026-08-15 23:00:00,000", "sig1", 9, 3),
        _telemetry("2026-08-16 02:00:00,000", "sig1", 10, 4),
    ]) + "\n", encoding="utf-8")
    rc = dv.main(["--session", "2026-08-15", "--db", str(db), "--log", str(log)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "INVALIDATION: none triggered" in out
    assert "[6-6a] B top-10 50.0%" in out and "INCREASED" in out
    assert "[3-2] A top-10 50.0%" in out and "SIM MATCHED LIVE" in out
    assert "[3-3] cost 100" in out and "CLEARLY DECREASED" in out
    assert "55.1% [UNREPRODUCED]" in out
    assert "cooldown violations" in out and "300.0s" in out
    assert "ranking_hold_expired releases in window   : 2" in out
    assert "VERDICT EMITTED" in out
    # 개정 전 아래 경계를 인용하지 말라는 안내가 결과와 함께 나간다 (§6-5)
    assert "17.1% -> 19.0%" in out and "Do not quote the pre-revision 17.1%" in out
    assert "6-session baseline" in out and "1/7 ~ 0.14" in out


def test_main_output_is_pure_ascii_on_the_valid_path(tmp_path, capsys, monkeypatch):
    """cp949 콘솔에서 죽지 않아야 한다 (§1f). 이 레포에서 실제로 그것 때문에 죽은 적이 있다."""
    db = tmp_path / "t.db"
    _seed(db, "2026-08-13", ranked10=4, tape10=1, dense=True)
    _seed(db, "2026-08-15", ranked10=8, tape10=4, capfill=100, dense=True)
    _patch_baseline(monkeypatch, (("2026-08-13", 4, 1, 25.0, 0),), {10: (1, 25.0, 25.0, 25.0),
                                                                   100: (1, 25.0, 25.0, 25.0)})
    log = tmp_path / "collector.log"
    # 로그에 비 ASCII 를 섞어도 출력은 ASCII 여야 한다
    log.write_text(_telemetry("2026-08-15 23:00:00,000", "sig어", 9, 3) + "\n",
                   encoding="utf-8")
    dv.main(["--session", "2026-08-15", "--db", str(db), "--log", str(log)])
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
    _seed(db, "2026-08-13", ranked10=4, tape10=1, dense=True)
    _seed(db, "2026-08-15", ranked10=8, tape10=4, capfill=200, dense=True)
    _patch_baseline(monkeypatch, (("2026-08-13", 4, 1, 25.0, 0),), {10: (1, 25.0, 25.0, 25.0),
                                                                   100: (1, 25.0, 25.0, 25.0)})
    log = tmp_path / "collector.log"
    log.write_text(_telemetry("2026-08-15 23:00:00,000", "sig1", 9, 3) + "\n", encoding="utf-8")
    dv.main(["--session", "2026-08-15", "--db", str(db), "--log", str(log)])
    out = capsys.readouterr().out
    assert "OPERATOR CONFIRMATION REQUIRED" in out
    assert "CANNOT prove" in out
    assert "OPERATOR MUST CONFIRM" in out


def test_main_flags_the_boundary_between_frozen_constant_and_exact_baseline_max(
        tmp_path, capsys, monkeypatch):
    """얼린 25.9% 와 정확한 기준선 최대(예: 25.9259%) 사이에 떨어지면 운영자에게 넘긴다."""
    db = tmp_path / "t.db"
    # 기준선 세션의 정확한 최대를 27/104 = 25.9615% 로 만든다
    _seed(db, "2026-08-13", ranked10=104, tape10=27, dense=True)
    _patch_baseline(monkeypatch, (("2026-08-13", 104, 27, 25.96, 0),),
                    {10: (1, 25.96, 25.96, 25.96), 100: (1, 25.96, 25.96, 25.96)})
    # 대상 창: 104 중 27 = 25.9615% 가 얼린 25.9 와 정확값 25.9615 사이에 딱 들어간다
    _seed(db, "2026-08-15", ranked10=104, tape10=27, capfill=200, dense=True)
    log = tmp_path / "collector.log"
    log.write_text(_telemetry("2026-08-15 23:00:00,000", "sig1", 9, 3) + "\n", encoding="utf-8")
    rc = dv.main(["--session", "2026-08-15", "--db", str(db), "--log", str(log)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "BOUNDARY" in out
    assert "OPERATOR MUST DECIDE" in out


def test_main_flags_the_lower_boundary_at_the_baseline_median(tmp_path, capsys, monkeypatch):
    """얼린 **19.0%**(개정 1) 와 정확한 중앙 사이도 같은 틈이다. 아래쪽도 운영자에게 넘긴다.

    개정 전에는 이 틈이 17.1 / 17.1428 였다. 구조는 그대로 두고 경계만 §6-6a 로 옮겼다.
    """
    db = tmp_path / "t.db"
    # 기준선 정확한 중앙 = 12/63 = 19.0476%
    _seed(db, "2026-08-13", ranked10=63, tape10=12, dense=True)
    _patch_baseline(monkeypatch, (("2026-08-13", 63, 12, 19.05, 0),),
                    {10: (1, 19.05, 19.05, 19.05), 100: (1, 19.05, 19.05, 19.05)})
    # 대상 창: 342 중 65 = 19.0058% -> 얼린 19.0 이상(보류)이지만 19.0476 미만.
    # (dense 는 60 초 간격 391 스냅이므로 종목 수는 391 을 넘을 수 없다)
    _seed(db, "2026-08-15", ranked10=342, tape10=65, capfill=200, dense=True)
    log = tmp_path / "collector.log"
    log.write_text(_telemetry("2026-08-15 23:00:00,000", "sig1", 9, 3) + "\n", encoding="utf-8")
    rc = dv.main(["--session", "2026-08-15", "--db", str(db), "--log", str(log)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "VERDICT WITHHELD" in out          # 밴드는 문서 그대로 적용된다
    assert "BOUNDARY" in out and "baseline median" in out
    assert "OPERATOR MUST DECIDE" in out


def test_boundary_note_fires_when_the_frozen_median_sits_ABOVE_the_exact_one(
        tmp_path, capsys, monkeypatch):
    """개정 1 에서 뒤집힌 방향 — 얼린 19.0% > 정확 18.9655% (라이브 08-13 = 11/58).

    옛 한쪽 비교(`B_MED <= b10 < 정확`)는 이 배치에서 영영 발화하지 않는다. 그러면
    18.97~19.00% 구간이 조용히 지나간다 — 오늘 밤 값이 거기 떨어질 수 있다.
    """
    db = tmp_path / "t.db"
    # 기준선 정확한 중앙 = 11/58 = 18.9655% < 얼린 19.0%
    _seed(db, "2026-08-13", ranked10=58, tape10=11, dense=True)
    _patch_baseline(monkeypatch, (("2026-08-13", 58, 11, 18.97, 0),),
                    {10: (1, 18.97, 18.97, 18.97), 100: (1, 18.97, 18.97, 18.97)})
    # 대상 창: 정확 18.9655 이상 & 얼린 19.0 미만이어야 한다 -> 79 중 15 = 18.9873%
    _seed(db, "2026-08-15", ranked10=79, tape10=15, capfill=200, dense=True)
    log = tmp_path / "collector.log"
    log.write_text(_telemetry("2026-08-15 23:00:00,000", "sig1", 9, 3) + "\n", encoding="utf-8")
    rc = dv.main(["--session", "2026-08-15", "--db", str(db), "--log", str(log)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "NOT INCREASED" in out               # 얼린 19.0 을 그대로 적용한 결과
    assert "BOUNDARY at the baseline median" in out
    assert "the frozen one" in out and "is the higher of the two" in out
    assert "OPERATOR MUST DECIDE" in out


def test_no_boundary_note_when_the_value_is_clear_of_both_edges(tmp_path, capsys, monkeypatch):
    db = tmp_path / "t.db"
    _seed(db, "2026-08-13", ranked10=63, tape10=12, dense=True)
    _patch_baseline(monkeypatch, (("2026-08-13", 63, 12, 19.05, 0),),
                    {10: (1, 19.05, 19.05, 19.05), 100: (1, 19.05, 19.05, 19.05)})
    _seed(db, "2026-08-15", ranked10=10, tape10=1, capfill=200, dense=True)   # 10.0%
    log = tmp_path / "collector.log"
    log.write_text(_telemetry("2026-08-15 23:00:00,000", "sig1", 9, 3) + "\n", encoding="utf-8")
    rc = dv.main(["--session", "2026-08-15", "--db", str(db), "--log", str(log)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "NOT INCREASED" in out
    assert "BOUNDARY" not in out


def test_the_summary_is_drawn_from_the_valid_subset_only(tmp_path, capsys, monkeypatch):
    """§6-6 의 핵심 구조: 10 행 표는 전부 찍되 **밴드는 유효 세션에서만** 뽑는다.

    무효 세션의 커버%가 요약에 섞이면 자와 관측이 다른 자가 된다 (§6-4 셋째 줄).
    """
    import sqlite3
    db = tmp_path / "t.db"
    _seed(db, "2026-08-12", ranked10=4, tape10=1, dense=True)                      # 25.0%, 유효
    _seed(db, "2026-08-13", ranked10=4, tape10=3, dense=True, late_start_s=3600)   # 75.0%, 무효
    _patch_baseline(monkeypatch,
                    (("2026-08-12", 4, 1, 25.0, 0), ("2026-08-13", 4, 3, 75.0, 0)),
                    {10: (1, 25.0, 25.0, 25.0), 100: (1, 25.0, 25.0, 25.0)},
                    unobs=(("2026-08-12", 0.0, True), ("2026-08-13", 15.385, False)))
    monkeypatch.setattr(dv, "BASELINE_SUMMARY_PREREV",
                        {10: (2, 25.0, 75.0, 75.0), 100: (2, 25.0, 75.0, 75.0)})
    con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True)
    try:
        ok, pct10, _ = dv.run_self_check(con.cursor())
    finally:
        con.close()
    out = capsys.readouterr().out
    assert ok is True
    assert pct10 == pytest.approx([25.0])          # 무효 세션의 75.0% 는 빠졌다
    assert "valid subset (6-6): 1 of 2 sessions" in out
    assert "INVALID" in out and "VALID" in out     # 10 행 표에 둘 다 보인다
    assert "75.0%" in out                          # 표에는 남는다 (데이터 무결성 검사)
    assert "pre-revision all-session" in out       # 개정 전 요약이 병기된다


def test_self_check_fails_when_the_recomputed_validity_disagrees_with_6_6(
        tmp_path, capsys, monkeypatch):
    """§6-6 의 라벨과 재계산이 어긋나면 **고치지 말고 멈춘다** (명세 §3).

    상수를 다시 유도해 덮어쓰면 사전등록이 사전등록이 아니게 된다. 어긋남 자체가 사건이다.
    """
    db = tmp_path / "t.db"
    _seed(db, "2026-08-13", ranked10=4, tape10=1, dense=True)     # 실제로는 0% 미관측 = 유효
    _patch_baseline(monkeypatch, (("2026-08-13", 4, 1, 25.0, 0),),
                    {10: (1, 25.0, 25.0, 25.0), 100: (1, 25.0, 25.0, 25.0)},
                    unobs=(("2026-08-13", 9.999, False),))        # 문서가 무효라고 주장
    rc = dv.main(["--session", "2026-08-15", "--db", str(db),
                  "--log", str(tmp_path / "none.log")])
    out = capsys.readouterr().out
    assert rc == 2
    assert "SELF-CHECK: FAIL" in out
    assert "unobs% 0.000 vs 9.999" in out and "valid True!=False" in out
    assert "do NOT edit the constants" in out
    for forbidden in ("[6-6a]", "VERDICT EMITTED"):
        assert forbidden not in out


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
    _seed(db, "2026-08-13", ranked10=4, tape10=1, dense=True)
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

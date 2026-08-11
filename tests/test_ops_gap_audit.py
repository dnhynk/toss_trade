"""`ops/gap_audit.py` 검증 — 소유: W5.

## 이 스위트가 지키려는 것

결손 감사는 **틀렸을 때 조용하다.** 숫자가 나오기 때문이다. 실제로 이 도구를 처음
돌렸을 때 "체결은 있는데 봉이 없는 분 40%" 가 나왔고 그건 결손이 아니라 **봉 라벨
오프셋에 대한 내 가정이 틀린 것**이었다(`docs/31` §2). 그래서 여기서 검증하는 것은
"함수가 돈다"가 아니라 **분류가 실제로 갈라지는가** 다:

- 심어 놓은 오프셋을 **되찾아내는가** (가정이 아니라 측정인가)
- 안 보던 시간을 결손으로 세지 않는가 (티어 끊김 / 수집기 부재 / 응답 미포화)
- **진짜 결손은 여전히 A 로 남는가** — 셋을 다 걸러내고 나면 아무것도 안 남는 도구가
  되기 쉽다. 그건 "결손 없음"을 만들어내는 도구이지 재는 도구가 아니다.
- 최신 구간(아직 봉이 안 온 분)을 결손으로 세지 않는가

## 배선 가드 (H-1 재발 방지, `docs/30` §3)

공개 함수를 **열거하지 않고 AST 로 발견해서** 전부 `main()` 도달을 요구한다.
그리고 `daily_health.main()` 이 실제로 이 모듈을 부르는지도 확인한다 —
**만들었는데 아침 리포트에 안 찍히면 그건 없는 것과 같다.** 이 프로젝트에서 두 번
일어났고 두 번 다 문서 수치가 애드혹 스크립트 산물이었다.
"""
from __future__ import annotations

import ast
import datetime as dt
import gzip
import pathlib
import sqlite3

import pytest

from ops import daily_health as DH
from ops import gap_audit as GA

MIN = GA.MINUTE_MS


# --------------------------------------------------------------------------- #
# 픽스처 — 살아 있는 DB 는 절대 건드리지 않는다 (메모리 SQLite + tmp_path)
# --------------------------------------------------------------------------- #
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


def _mem_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    return conn


def _base_ms() -> int:
    """정규장 한복판의 분 경계 (2026-08-04 23:00 KST)."""
    return int(dt.datetime(2026, 8, 4, 23, 0, 0).timestamp() * 1000)


# --------------------------------------------------------------------------- #
# 1. 세션 — 08:50~09:00 은 세션이 아니다
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("hh,mm,want", [
    (9, 0, "day"), (16, 59, "day"),
    (17, 0, "pre"), (22, 29, "pre"),
    (22, 30, "regular"), (23, 59, "regular"), (0, 30, "regular"), (4, 59, "regular"),
    (5, 0, "after"), (8, 49, "after"),
    (8, 50, "none"), (8, 59, "none"),      # 토스 캘린더에 세션이 없는 구간
])
def test_kst_session_boundaries(hh, mm, want):
    ms = int(dt.datetime(2026, 8, 4, hh, mm).timestamp() * 1000)
    assert GA.kst_session(ms) == want


def test_percentiles_empty_and_basic():
    assert GA.percentiles([])["n"] == 0
    p = GA.percentiles([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    assert p["n"] == 10 and p["min"] == 1.0 and p["max"] == 10.0


# --------------------------------------------------------------------------- #
# 2. 티어 복원 — 추정이 아니라 기록에서 나온다
# --------------------------------------------------------------------------- #
def test_tier_reconstruction_is_exact_before_and_after_transitions():
    tl = {"AAA": [(1000, 2, 3), (5000, 3, 2)]}
    assert GA.tier_at(tl, "AAA", 500) == 2          # 첫 전이 이전 = from_tier
    assert GA.tier_at(tl, "AAA", 1000) == 3
    assert GA.tier_at(tl, "AAA", 4999) == 3
    assert GA.tier_at(tl, "AAA", 9999) == 2
    assert GA.tier_at(tl, "ZZZ", 1000) is None      # 이력 없음은 0 이 아니라 '모름'


def test_stayed_in_tier_rejects_interrupted_membership():
    tl = {"AAA": [(1000, 2, 3), (5000, 3, 2), (6000, 2, 3)]}
    assert GA.stayed_in_tier(tl, "AAA", 3, 2000, 4000) is True
    assert GA.stayed_in_tier(tl, "AAA", 3, 2000, 7000) is False   # 중간에 이탈
    assert GA.stayed_in_tier(tl, "AAA", 3, 500, 900) is False     # 시작 시점이 tier2


# --------------------------------------------------------------------------- #
# 3. 봉 라벨 오프셋 — 심은 것을 되찾아야 한다
# --------------------------------------------------------------------------- #
def test_candle_label_offset_recovers_planted_close_labeling():
    """봉이 **구간 종료 시각**으로 라벨된 데이터를 만들고, 도구가 +1 을 찾아내는지 본다.

    존재율만 보면 봉 밀도가 높을 때 아무 오프셋이나 그럴듯해 보이므로, 거래량이 정확히
    일치하는 오프셋만 채택돼야 한다. 그래서 **모든 분에 봉을 깔아 두고** 거래량만
    +1 자리에 맞춘다 — 존재율로는 못 가르는 상황을 일부러 만든 것이다.
    """
    b = _base_ms()
    trades = {("AAA", b + i * MIN): (100 + i, 3) for i in range(20)}
    candles = {}
    for i in range(-3, 25):                       # 모든 분에 봉이 있다 (존재율은 동률)
        candles[("AAA", b + i * MIN)] = 999_999
    for i in range(20):                           # 거래량만 +1 자리에 정확히 맞춘다
        candles[("AAA", b + (i + 1) * MIN)] = 100 + i
    out = GA.candle_label_offset(trades, candles)
    assert out["best_offset_min"] == 1
    assert out["best"]["vol_exact"] == 20


def test_candle_label_offset_is_none_without_trades():
    assert GA.candle_label_offset({}, {})["best_offset_min"] is None


# --------------------------------------------------------------------------- #
# 4. 커버리지 — 아직 안 온 봉을 결손으로 세지 않는다
# --------------------------------------------------------------------------- #
def test_candle_coverage_excludes_trailing_edge_but_keeps_real_hole():
    """최신 구간(봉 미도착)은 제외하되, **가운데 진짜 구멍은 남아야 한다.**

    최신 구간을 잘라내는 규칙이 구멍까지 같이 삼키면 "창을 길게 잡을수록 깨끗해지는"
    가짜 지표가 된다.
    """
    b = _base_ms()
    trades = {("AAA", b + i * MIN): (10, 1) for i in range(10)}
    candles = {("AAA", b + (i + 1) * MIN): 10 for i in range(10)}
    del candles[("AAA", b + 4 * MIN)]             # 가운데 진짜 구멍 (체결 분 b+3)
    del candles[("AAA", b + 10 * MIN)]            # 마지막 = 아직 안 온 것
    cc = GA.candle_coverage(trades, candles, {}, 1)
    assert cc["trailing_edge_skipped"] == 1
    assert cc["missing"] == 1
    assert cc["by_symbol_top"] == [("AAA", 1)]


# --------------------------------------------------------------------------- #
# 5. 테이프 완결성 — 심은 결손을 잡고, 안 보던 분은 안 센다
# --------------------------------------------------------------------------- #
def test_tape_completeness_flags_shortfall_and_ignores_unwatched_minutes():
    b = _base_ms()
    tl = {"AAA": [(b - MIN, 2, 3)],               # 계속 tier3
          "BBB": [(b - MIN, 3, 2)]}               # tier2 라 애초에 안 보던 종목
    trades = {("AAA", b): (60, 5), ("AAA", b + MIN): (100, 9),
              ("BBB", b): (10, 1)}
    candles = {("AAA", b + MIN): 100, ("AAA", b + 2 * MIN): 100,
               ("BBB", b + MIN): 999}
    tc = GA.tape_completeness(trades, candles, tl, 1)
    assert tc["minutes"] == 2                     # BBB 는 tier3 가 아니라 제외
    assert tc["complete"] == 1
    assert len(tc["short"]) == 1
    assert tc["short"][0]["symbol"] == "AAA"
    assert tc["short"][0]["ratio"] == pytest.approx(0.6)


def test_tape_completeness_skips_zero_volume_bars():
    """봉 거래량 0 이면 비율이 정의되지 않는다 — 0/0 을 100% 나 0% 로 만들지 않는다."""
    b = _base_ms()
    tl = {"AAA": [(b - MIN, 2, 3)]}
    tc = GA.tape_completeness({("AAA", b): (0, 0)}, {("AAA", b + MIN): 0}, tl, 1)
    assert tc["minutes"] == 0


# --------------------------------------------------------------------------- #
# 6. 결손 분류 — 세 버킷이 실제로 갈라지는가
# --------------------------------------------------------------------------- #
def _ev(sym, prev, this, n, wall=None):
    return GA.GapEvent(wall_ms=wall if wall is not None else this,
                       symbol=sym, prev_max_ms=prev, this_min_ms=this, n=n)


def test_classify_gaps_separates_all_three_buckets():
    b = _base_ms()
    tl = {"AAA": [(b - 10 * MIN, 2, 3)],                       # 내내 tier3
          "BBB": [(b - 10 * MIN, 2, 3), (b + 1000, 3, 2)]}     # 중간에 이탈
    ranking = [b + k * 12_000 for k in range(-20, 20)]         # 12초 간격, 촘촘
    res = GA.classify_gaps(
        [_ev("AAA", b, b + 3000, 50),          # A: 잘렸고 내내 tier3 이고 살아 있었다
         _ev("BBB", b, b + 3000, 50),          # B: 구간에 tier3 를 벗어났다
         _ev("AAA", b, b + 3000, 12)],         # C: 응답이 안 잘렸다
        tl, [], ranking, 12.0)
    got = {k: len(v) for k, v in res["buckets"].items()}
    assert got == {"A_in_watch": 1, "B_not_watched": 1, "C_not_cap_bound": 1}


def test_classify_gaps_keeps_short_gaps_in_A_despite_sparse_ranking_polls():
    """**회귀 테스트.** 결손 대부분은 1~3초인데 랭킹 폴은 12초 간격이다.

    생존 판정을 구간 안쪽만으로 하면 멀쩡한 3초 결손이 전부 '수집기 부재'로 밀려나
    A 버킷이 비어 버린다 — 결손을 재는 대신 **결손 없음을 만들어내는** 도구가 된다.
    처음 구현이 정확히 그랬고 이 테스트가 잡았다.
    """
    b = _base_ms()
    tl = {"AAA": [(b - 10 * MIN, 2, 3)]}
    ranking = [b + k * 12_000 for k in range(-20, 20)]   # 건강한 12초 간격
    for span_ms in (1000, 2000, 3000, 6000):
        res = GA.classify_gaps([_ev("AAA", b, b + span_ms, 50)], tl, [], ranking, 12.0)
        assert len(res["buckets"]["A_in_watch"]) == 1, f"{span_ms}ms 결손이 A 에서 밀려났다"


def test_classify_gaps_moves_dead_collector_window_to_B():
    """재기동 줄이 없는 정지(행)도 B 로 가야 한다 — 랭킹 폴 밀도가 근거다."""
    b = _base_ms()
    tl = {"AAA": [(b - 3600_000, 2, 3)]}
    long_gap = _ev("AAA", b, b + 600_000, 50)      # 10분짜리 구간
    alive = GA.classify_gaps([long_gap], tl, [],
                             [b + k * 12_000 for k in range(60)], 12.0)
    assert len(alive["buckets"]["A_in_watch"]) == 1
    dead = GA.classify_gaps([long_gap], tl, [], [b + 1000], 12.0)   # 폴이 사실상 없음
    assert len(dead["buckets"]["B_not_watched"]) == 1
    assert dead["reclassified_by_liveness"] == 1


def test_gap_span_is_seconds_not_minutes():
    """6초 현상을 재는 도구다. 단위를 분으로 뭉개면 이 태스크의 의미가 사라진다."""
    assert _ev("AAA", 1_000_000, 1_006_000, 50).span_s == 6.0


# --------------------------------------------------------------------------- #
# 7. 로그 파싱 — 회전본(.gz)까지 읽어야 한다
# --------------------------------------------------------------------------- #
def test_parse_log_reads_plain_and_rotated(tmp_path):
    gz = tmp_path / "collector.log.20260803.gz"
    with gzip.open(gz, "wt", encoding="utf-8") as fh:
        fh.write("2026-08-03 10:00:00,111 WARNING tape gap OLD: prev_max=1000 < "
                 "this_min=4000 (n=50) - x\n")
    (tmp_path / "collector.log").write_text(
        "2026-08-04 12:00:00,000 INFO    collector start base_url=x\n"
        "2026-08-04 12:00:01,000 INFO    COLLECTION-CONFIG start sig=rank2:X,t3max10\n"
        "2026-08-04 12:00:02,000 WARNING tape gap NEW: prev_max=9000 < this_min=11000 "
        "(n=50) - x\n"
        "2026-08-04 12:00:03,000 INFO    unrelated line\n", encoding="utf-8")
    gaps, restarts, sigs = GA.parse_log(tmp_path)
    assert [g.symbol for g in gaps] == ["OLD", "NEW"]     # 회전본이 먼저
    assert gaps[0].span_s == 3.0 and gaps[1].span_s == 2.0
    assert len(restarts) == 1
    assert sigs[0][1] == "rank2:X,t3max10"


def test_config_sig_reports_every_regime_active_in_the_window():
    """창을 가로지르는 설정 경계를 **뭉치지 않는다.**

    `COLLECTION-CONFIG` 는 기동 때만 찍히므로 창 안의 줄만 보면 창 시작 시점에 유효했던
    지문이 통째로 빠진다. 실제로 08-03~08-04 창(랭킹 4종·호가 16초 + 랭킹 2종·호가 4초)
    에서 최신 지문 하나만 실렸다 — 분석이 그걸 한 덩어리로 읽으면 조용히 틀린다.
    """
    sigs = [(1_000, "OLD"), (5_000, "NEW")]
    out = GA.config_sig_lines(sigs, 2_000, 9_000)
    assert out[0] == "config_sig: 2종"
    assert "OLD" in out[1] and "NEW" in out[2]
    assert any("설정 경계를 가로지른다" in ln for ln in out)


def test_config_sig_single_regime_has_no_boundary_warning():
    out = GA.config_sig_lines([(1_000, "ONLY")], 2_000, 9_000)
    assert out[0] == "config_sig: 1종"
    assert not any("가로지른다" in ln for ln in out)


def test_config_sig_missing_is_said_out_loud():
    """지문이 없으면 **없다고 말하고**, 대신 볼 표지를 가리켜야 한다.

    `COLLECTION-CONFIG` 는 08-04 11:22:42 배포부터 찍히므로 그 이전 창에는 지문이 아예
    없다. 없는 것을 최신 지문으로 채우면 경계가 다른 데이터가 한 덩어리가 된다.
    """
    out = GA.config_sig_lines([], 2_000, 9_000)
    assert "로그에 없음" in out[0]
    assert any("사실상의 지문" in ln for ln in out)


def test_parse_log_survives_missing_dir(tmp_path):
    gaps, restarts, sigs = GA.parse_log(tmp_path / "nope")
    assert (gaps, restarts, sigs) == ([], [], [])


# --------------------------------------------------------------------------- #
# 8. DB 질의 — 티어별 분리와 타입별 분리
# --------------------------------------------------------------------------- #
def test_orderbook_intervals_split_by_tier_and_exclude_membership_churn():
    """tier3(4초)와 tier2(600초)를 풀링하면 둘 다 거짓말이 된다."""
    conn = _mem_db()
    b = _base_ms()
    conn.execute("INSERT INTO promotions (symbol, ts_ms, from_tier, to_tier, reason) "
                 "VALUES ('AAA', ?, 2, 3, 'x')", (b - MIN,))
    conn.execute("INSERT INTO promotions (symbol, ts_ms, from_tier, to_tier, reason) "
                 "VALUES ('BBB', ?, 3, 2, 'x')", (b - MIN,))
    for i in range(5):
        conn.execute("INSERT INTO orderbook_snap (symbol, snap_ms, depth_json) "
                     "VALUES ('AAA', ?, '{}')", (b + i * 4000,))
    for i in range(3):
        conn.execute("INSERT INTO orderbook_snap (symbol, snap_ms, depth_json) "
                     "VALUES ('BBB', ?, '{}')", (b + i * 600_000,))
    conn.commit()
    tl = GA.tier_timeline(conn)
    out = GA.orderbook_intervals(conn, b - MIN, b + 3_000_000, tl)
    assert out["tier3"]["p50"] == 4.0 and out["tier3"]["n"] == 4
    assert out["tier2"]["p50"] == 600.0 and out["tier2"]["n"] == 2
    conn.close()


def test_tier_population_is_time_weighted_not_a_snapshot():
    """"결손 없음"은 tier3 종목 수 없이 읽으면 안 된다 — 시간가중이라야 의미가 있다.

    창 절반은 2종목, 절반은 0종목이면 평균 1.0 이어야 한다. 끝점만 보면 0 이 나오고
    그러면 "10종목 보는데 결손 0" 과 "1종목 보는데 결손 0" 이 같아 보인다.
    """
    a, b = 0, 1000
    tl = {"AAA": [(0, 2, 3), (500, 3, 2)],
          "BBB": [(0, 2, 3), (500, 3, 2)]}
    pop = GA.tier_population(tl, 3, a, b)
    assert pop["avg"] == 1.0 and pop["max"] == 2 and pop["min"] == 0


def test_tier_population_sweep_matches_naive_recount():
    """스윕 구현이 **구간마다 다시 세는** 방식과 같은 값을 내는지 대조한다.

    속도 때문에 스윕으로 바꿨다(하루치 창 133초 -> 1초 미만). 최적화가 값을 바꾸면
    조용히 틀린 측정 조건이 리포트에 실린다 — 그래서 느린 쪽을 기준으로 남겨 대조한다.
    """
    tl = {
        "A": [(100, 2, 3), (400, 3, 2), (700, 2, 3)],
        "B": [(100, 3, 2), (250, 2, 3), (250, 3, 2)],   # 같은 시각 전이 2건
        "C": [(50, 2, 3)],
        "D": [(900, 2, 3)],                             # 창 밖 직전
    }
    start, end = 0, 1000
    edges = sorted({start, end} | {ts for ev in tl.values() for ts, _f, _t in ev
                                   if start < ts < end})
    for tier in (2, 3):
        naive = sum(
            sum(1 for s in tl if GA.tier_at(tl, s, a) == tier) * (b - a)
            for a, b in zip(edges, edges[1:]))
        assert GA.tier_population(tl, tier, start, end)["avg"] == round(naive / end, 2)


def test_ranking_intervals_split_by_type():
    """두 목록은 연달아 찍히므로 풀링하면 중앙값이 실제 폴 주기가 아니게 된다."""
    conn = _mem_db()
    b = _base_ms()
    for i in range(4):
        for j, rt in enumerate(("MARKET_TRADING_VOLUME", "TOSS_SECURITIES_TRADING_VOLUME")):
            conn.execute("INSERT INTO rankings_snap (snap_ms, ranking_type, duration, rank, "
                         "symbol, last_u, vol_qu, amount_u) VALUES (?,?,'d',1,'A',1,1,1)",
                         (b + i * 12_000 + j * 300, rt))
    conn.commit()
    out = GA.ranking_intervals(conn, b - MIN, b + 3_000_000)
    assert out["MARKET_TRADING_VOLUME"]["p50"] == 12.0
    assert out["(pooled, all types)"]["p50"] != 12.0     # 풀링은 다른 값이 나온다
    conn.close()


# --------------------------------------------------------------------------- #
# 8.5 창 **가장자리** 구멍 — 간격만 세는 도구가 구조적으로 못 보는 것
#
# 08-05 아침 실측: 08:01:46 에 수집이 멈추고 창은 08:50 에 끝났는데 리포트는
# `max_gap=3.4min` 이라고 적었다. 48.2분짜리 구멍이 **간격 목록에 아예 안 들어왔기**
# 때문이다 — 비교할 다음 폴이 창 밖이다. 아래 스위트가 지키는 것은 두 가지이고 **둘 다
# 있어야** 한다:
#   (a) 가장자리 구멍이 보고되는가
#   (b) **가운데 구멍은 여전히 보고되는가** — 가장자리를 보게 만들면서 가운데를 잃으면
#       고친 게 아니라 옮긴 것이다. 이 프로젝트에서 "가드를 만들었다"가 실제로는 목록에
#       적힌 것만 검사한 사례가 세 번 있다.
# --------------------------------------------------------------------------- #
_W_START = int(dt.datetime(2026, 8, 4, 9, 0, 0).timestamp() * 1000)
_W_END = int(dt.datetime(2026, 8, 5, 8, 50, 0).timestamp() * 1000)
_CADENCE_MS = 300_000       # 5분 — 실측 12초 대신 테스트를 가볍게 하려는 값일 뿐이다


def _polls(a_ms: int, b_ms: int, step_ms: int = _CADENCE_MS) -> list[int]:
    """[a,b] 를 채우는 폴 시각. **끝점을 반드시 포함한다** — 안 그러면 "구멍"의 크기가
    격자 나머지만큼 흔들려서 테스트가 무엇을 재는지 알 수 없어진다."""
    ts = list(range(a_ms, b_ms + 1, step_ms))
    if ts[-1] != b_ms:
        ts.append(b_ms)
    return ts


def test_trailing_hole_is_reported_when_collection_dies_at_the_window_end():
    """(a) 창 **끝**에서 수집이 죽으면 구멍이 나와야 한다 — 08-05 아침의 정확한 모양.

    핵심 단언은 `max_inter_poll` 이 **여전히 작다**는 것이다. 즉 옛 계산은 이 데이터를
    보고도 "이상 없음"이라고 말한다. 그 사실을 테스트가 직접 붙들고 있어야, 누가 나중에
    가장자리 계산을 걷어내면 이 테스트가 죽는다.
    """
    dies_at = _W_END - 48 * 60_000                    # 창 끝 48분 전에 정지
    eh = GA.edge_holes(_polls(_W_START, dies_at), _W_START, _W_END)
    trailing = [h for h in eh["holes"] if h.name == "trailing_hole"][0]
    assert round(trailing.minutes) == 48
    assert eh["max_hole_kind"] == "trailing_hole"
    assert round(eh["max_hole_s"] / 60.0) == 48
    # 옛 계산(연속 두 폴의 차이)은 이 48분을 못 본다 — 그것이 이 버그의 전부였다.
    assert eh["inter_poll"]["max"] == _CADENCE_MS / 1000.0


def test_mid_window_hole_is_still_reported():
    """(b) **대조군.** 가운데 구멍은 여전히 보여야 한다.

    가장자리를 보게 만드는 수정이 가운데를 잃으면 고친 것이 아니라 옮긴 것이다.
    """
    hole_a = _W_START + 3 * 3_600_000
    hole_b = hole_a + 45 * 60_000
    ts = _polls(_W_START, hole_a) + _polls(hole_b, _W_END)
    eh = GA.edge_holes(ts, _W_START, _W_END)
    assert eh["max_hole_kind"] == "inter_poll"
    assert round(eh["inter_poll"]["max"] / 60.0) == 45
    assert round(eh["max_hole_s"] / 60.0) == 45
    for h in eh["holes"]:                              # 가장자리는 붙어 있다
        assert h.seconds <= _CADENCE_MS / 1000.0


def test_leading_hole_is_reported_when_collection_starts_late():
    """창 **앞쪽** 구멍도 같은 이유로 안 보였다 — 비교할 이전 폴이 창 밖이다."""
    starts_at = _W_START + 30 * 60_000
    eh = GA.edge_holes(_polls(starts_at, _W_END), _W_START, _W_END)
    leading = [h for h in eh["holes"] if h.name == "leading_hole"][0]
    assert round(leading.minutes) == 30
    assert eh["max_hole_kind"] == "leading_hole"
    assert eh["inter_poll"]["max"] == _CADENCE_MS / 1000.0    # 옛 계산은 못 본다


def test_a_mid_window_outage_cannot_mask_an_edge_hole():
    """가운데 큰 정지가 가장자리 **판정 기준**을 끌어올려서는 안 된다.

    등급 판정의 비교 대상으로 `max` 를 쓰면, 창 가운데 60분짜리 정지가 하나 있는 것만으로
    창 끝 50분 구멍이 "정상 범위"가 되어 다시 조용해진다. 그래서 p99 를 쓴다. 오늘 창에
    실제로 3.4분짜리 가운데 정지가 있었으므로 가상의 걱정이 아니다.
    """
    mid_a = _W_START + 2 * 3_600_000
    mid_b = mid_a + 60 * 60_000
    dies_at = _W_END - 50 * 60_000
    ts = _polls(_W_START, mid_a) + _polls(mid_b, dies_at)
    eh = GA.edge_holes(ts, _W_START, _W_END)
    trailing = [h for h in eh["holes"] if h.name == "trailing_hole"][0]
    assert round(trailing.minutes) == 50
    assert eh["inter_poll"]["max"] == 60 * 60.0        # 가운데 정지가 max 를 60분으로 올린다
    grade, why = GA.grade_hole(trailing, [], eh["inter_poll"]["p99"])
    assert grade == "ALERT_", f"가운데 정지가 가장자리 구멍을 삼켰다: {why}"


def test_empty_window_is_one_whole_hole_not_a_clean_report():
    """관측이 0개면 "간격 없음"이 아니라 **창 전체가 구멍**이다."""
    eh = GA.edge_holes([], _W_START, _W_END)
    assert eh["n_obs"] == 0
    assert [h.name for h in eh["holes"]] == ["whole_window"]
    assert eh["max_hole_s"] == (_W_END - _W_START) / 1000.0
    assert eh["inter_poll"]["max"] is None
    grade, _why = GA.grade_hole(eh["holes"][0], [], eh["inter_poll"]["p99"])
    assert grade == "ALERT_"


def test_single_observation_window_has_no_normal_gap_to_hide_behind():
    """폴이 하나면 간격이 없으므로 비교 기준도 없다 — 가장자리 구멍을 그냥 인정해야 한다."""
    only = _W_START + 60 * 60_000
    eh = GA.edge_holes([only], _W_START, _W_END)
    assert eh["inter_poll"]["p99"] is None
    grades = {h.name: GA.grade_hole(h, [], None)[0] for h in eh["holes"]}
    assert grades == {"leading_hole": "ALERT_", "trailing_hole": "ALERT_"}


def test_tiny_edge_hole_inside_normal_cadence_is_not_called_a_hole():
    """창을 어디서 자르든 가장자리엔 최대 한 폴 주기가 남는다 — 그건 결손이 아니다."""
    eh = GA.edge_holes(_polls(_W_START + 1_000, _W_END - 1_000), _W_START, _W_END)
    for h in eh["holes"]:
        assert GA.grade_hole(h, [], eh["inter_poll"]["p99"])[0] == "NOTE_"


# --- 등급 결정: PLANNED 창 안이면 PLANNED_, 밖이면 ALERT_ (docs/34 4등급 그대로) --- #
def test_hole_inside_a_planned_window_is_planned_and_outside_is_alert():
    a = _W_END - 48 * 60_000
    hole = GA.Hole("trailing_hole", a, _W_END)
    inside = [(a - 60_000, _W_END + 600_000, "laptop lid closed")]
    outside = [(_W_START, _W_START + 60_000, "unrelated maintenance")]
    assert GA.grade_hole(hole, inside, 12.0)[0] == "PLANNED_"
    assert "laptop lid closed" in GA.grade_hole(hole, inside, 12.0)[1]
    assert GA.grade_hole(hole, outside, 12.0)[0] == "ALERT_"
    assert GA.grade_hole(hole, [], 12.0)[0] == "ALERT_"


def test_partially_covered_hole_is_alert_and_says_how_much_is_unexplained():
    """절반만 계획 창에 걸치면 나머지 절반은 **설명되지 않았다.** PLANNED_ 로 삼키지 않는다."""
    a = _W_END - 48 * 60_000
    hole = GA.Hole("trailing_hole", a, _W_END)
    partial = [(a, a + 24 * 60_000, "half-covered")]
    grade, why = GA.grade_hole(hole, partial, 12.0)
    assert grade == "ALERT_"
    assert "24.0분" in why and "half-covered" in why


def test_uncovered_span_handles_multiple_disjoint_windows():
    assert GA.uncovered_span_ms(0, 100, [(0, 40, "a"), (60, 100, "b")]) == 20
    assert GA.uncovered_span_ms(0, 100, [(0, 100, "a")]) == 0
    assert GA.uncovered_span_ms(0, 100, []) == 100


# --- 계획 창 복원: 라이브 마커는 만료되면 지워진다, 남는 것은 PLANNED_*.txt 뿐 --- #
def _write_planned(dirp: pathlib.Path, name: str, until: str | None, reason: str) -> None:
    head = "PLANNED MAINTENANCE - this was expected, not an outage."
    if until:
        head += f" window until {until}"
    (dirp / name).write_text(f"{head}\nreason: {reason}\n\n[INFO] body\n", encoding="utf-8")


def test_planned_windows_are_reconstructed_from_alert_file_headers(tmp_path):
    """워치독이 창 동안 쓴 `PLANNED_*.txt` 머리말이 **지워지지 않는 유일한 기록**이다.

    라이브 마커(`state_dir/PLANNED`)는 만료 시 워치독이 지운다(잊힌 마커가 진짜 장애를
    침묵시키지 않게 하려는 설계). 그래서 아침 리포트가 도는 시점엔 이미 없다.
    """
    _write_planned(tmp_path, "PLANNED_20260805_074559_log_tape_gap.txt",
                   "2026-08-05 09:00:00", "laptop lid closed (announced 06:55)")
    wins = GA.planned_windows(tmp_path, tmp_path / "state")
    assert len(wins) == 1
    a, b, why = wins[0]
    assert dt.datetime.fromtimestamp(a / 1000) == dt.datetime(2026, 8, 5, 7, 45, 59)
    assert dt.datetime.fromtimestamp(b / 1000) == dt.datetime(2026, 8, 5, 9, 0, 0)
    assert "laptop lid closed" in why


def test_planned_file_without_a_window_header_claims_no_interval(tmp_path):
    """STOP 파일 같은 운영자 행위는 그 **순간**의 증거일 뿐 창이 아니다.

    창이라고 우기면 아무 시각의 PLANNED_ 파일 하나가 그 근처 구멍을 전부 설명해 버린다.
    """
    _write_planned(tmp_path, "PLANNED_20260805_074559_watch_process_dead.txt",
                   None, "operator-driven action (STOP file)")
    assert GA.planned_windows(tmp_path, tmp_path / "state") == []


def test_planned_windows_merge_when_they_overlap(tmp_path):
    _write_planned(tmp_path, "PLANNED_20260805_070000_a.txt", "2026-08-05 08:00:00", "first")
    _write_planned(tmp_path, "PLANNED_20260805_073000_b.txt", "2026-08-05 09:00:00", "second")
    wins = GA.planned_windows(tmp_path, tmp_path / "state")
    assert len(wins) == 1
    assert dt.datetime.fromtimestamp(wins[0][1] / 1000) == dt.datetime(2026, 8, 5, 9, 0, 0)
    assert "first" in wins[0][2] and "second" in wins[0][2]


def test_planned_windows_reads_the_live_marker_when_it_still_exists(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    until = dt.datetime.now().replace(microsecond=0) + dt.timedelta(hours=1)
    (state / "PLANNED").write_text(f"reason=rebase onto main\nuntil={until:%Y-%m-%d %H:%M:%S}\n",
                                   encoding="utf-8")
    wins = GA.planned_windows(tmp_path, state)
    assert len(wins) == 1
    assert dt.datetime.fromtimestamp(wins[0][1] / 1000) == until
    assert "rebase onto main" in wins[0][2]


def test_expired_live_marker_does_not_invent_a_past_window(tmp_path):
    """`until` 이 파일 시각보다 앞이면 **이미 만료된** 마커다 — 워치독이 곧 지운다.

    이걸 `[until, mtime]` 구간으로 뒤집어 읽으면 있지도 않았던 계획 창을 과거에 만들고,
    그 시간대의 진짜 공백이 `PLANNED_` 로 조용히 삼켜진다. (테스트를 쓰다 실제로 이
    동작이 나와서 고쳤다.)
    """
    state = tmp_path / "state"
    state.mkdir()
    past = dt.datetime.now() - dt.timedelta(hours=3)
    (state / "PLANNED").write_text(f"reason=stale\nuntil={past:%Y-%m-%d %H:%M:%S}\n",
                                   encoding="utf-8")
    assert GA.planned_windows(tmp_path, state) == []


def test_planned_windows_survives_missing_dirs(tmp_path):
    assert GA.planned_windows(tmp_path / "nope", tmp_path / "also_nope") == []


# --- 리포트 문자열: 사람이 읽는 쪽에서도 속지 않아야 한다 --- #
def test_edge_hole_lines_name_both_edges_and_shout_when_unexplained():
    dies_at = _W_END - 48 * 60_000
    eh = GA.edge_holes(_polls(_W_START, dies_at), _W_START, _W_END)
    text = "\n".join(GA.edge_hole_lines("랭킹 폴", eh, []))
    assert "leading_hole" in text and "trailing_hole" in text
    assert "max_hole" in text and "48.0min" in text
    assert "ALERT_" in text
    assert "!!" in text
    planned = [(dies_at - 60_000, _W_END + 60_000, "laptop lid closed")]
    quiet = "\n".join(GA.edge_hole_lines("랭킹 폴", eh, planned))
    assert "PLANNED_" in quiet and "ALERT_" not in quiet and "!!" not in quiet


# --- 끝에서 끝까지: 아침 리포트 본문이 실제로 달라지는가 --- #
def _file_db(path: pathlib.Path, snap_ms: list[int]) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.executemany("INSERT INTO rankings_snap (snap_ms, ranking_type, duration, rank, "
                     "symbol, last_u, vol_qu, amount_u) VALUES (?,'MV','d',1,'AAA',1,1,1)",
                     [(m,) for m in snap_ms])
    conn.commit()
    conn.close()


def _cfg_for(tmp_path: pathlib.Path):
    from ops.opsconfig import DiskThresholds, OpsConfig
    return OpsConfig(
        db_path=tmp_path / "tossmon.db", log_dir=tmp_path, state_dir=tmp_path / "state",
        archive_dir=tmp_path / "archive", disk=DiskThresholds(0.0, 0.0),
        stale_minutes_warn=5, stale_minutes_critical=15, log_retention_days=14,
        log_max_bytes=1000, collector_cmd=["python", "-c", "pass"],
        max_restarts_per_window=5, restart_window_s=600,
        restart_backoff_base_s=1.0, restart_backoff_cap_s=10.0)


def test_daily_health_report_shows_the_trailing_hole_and_no_longer_says_max_gap(tmp_path):
    """**아침 리포트 본문**이 48분을 말해야 한다. 여기까지 와야 고친 것이다.

    `max_gap` 이라는 이름도 같이 지킨다 — 값은 맞는데 이름이 "최대 공백"으로 읽혀서
    48.2분을 3.4분이라고 보고하게 만든 것이 그 이름이었다.
    """
    dies_at = _W_END - 48 * 60_000
    _file_db(tmp_path / "tossmon.db", _polls(_W_START, dies_at))
    text = DH.build_summary(_cfg_for(tmp_path), "20260805")
    assert "trailing_hole" in text and "48.0min" in text
    assert "leading_hole" in text
    assert "max_hole" in text
    assert "max_gap" not in text, "속이는 이름이 아직 리포트에 남아 있다"
    assert "ALERT_ 계획 정비 창 기록이 없다" in text or "설명되지 않은" in text


def test_daily_health_report_grades_the_hole_planned_when_the_window_covers_it(tmp_path):
    """오늘 아침의 실제 모양 — 사람이 노트북을 닫았고 09:00 만료 창이 걸려 있었다."""
    dies_at = _W_END - 48 * 60_000
    _file_db(tmp_path / "tossmon.db", _polls(_W_START, dies_at))
    _write_planned(tmp_path, "PLANNED_20260805_074559_log_tape_gap.txt",
                   "2026-08-05 09:00:00", "laptop lid closed (announced 06:55)")
    text = DH.build_summary(_cfg_for(tmp_path), "20260805")
    assert "trailing_hole" in text and "48.0min" in text
    assert "PLANNED_ 계획 정비 창 안" in text
    assert "laptop lid closed" in text
    edge = [ln for ln in text.splitlines() if "trailing_hole" in ln or "-> " in ln]
    assert not any("ALERT_" in ln for ln in edge)


def test_daily_health_still_reports_a_mid_window_hole(tmp_path):
    """대조군 — 아침 리포트에서도 **가운데 구멍은 여전히** 나와야 한다."""
    hole_a = _W_START + 3 * 3_600_000
    ts = _polls(_W_START, hole_a) + _polls(hole_a + 45 * 60_000, _W_END)
    _file_db(tmp_path / "tossmon.db", ts)
    text = DH.build_summary(_cfg_for(tmp_path), "20260805")
    assert "max_inter_poll  :  45.0min" in text
    assert "[inter_poll]" in text
    assert "최대 공백 45.0분" in text


# --------------------------------------------------------------------------- #
# 9. 배선 가드 — 거부 기본값 (docs/30 section 3)
# --------------------------------------------------------------------------- #
#: 도달 불가능해도 되는 공개 함수와 **사유**. 허용 목록이 아니다 — 여기 없는 공개
#: 함수는 전부 `main()` 에서 도달 가능해야 한다.
NOT_WIRED = {"main": "entry point itself - reachability is measured from here"}


def _public_functions(src: str) -> set[str]:
    tree = ast.parse(src)
    return {n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not n.name.startswith("_")}


def _call_graph(src: str) -> dict[str, set[str]]:
    tree = ast.parse(src)
    defined = {n.name for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    graph: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        called: set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                fn = sub.func
                name = (fn.id if isinstance(fn, ast.Name)
                        else fn.attr if isinstance(fn, ast.Attribute) else None)
                if name in defined:
                    called.add(name)
            elif isinstance(sub, ast.Name) and sub.id in defined:
                called.add(sub.id)
        graph[node.name] = called
    return graph


def _reachable(src: str, entry: str = "main") -> set[str]:
    graph = _call_graph(src)
    seen, stack = set(), [entry]
    while stack:
        for nxt in graph.get(stack.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def unwired_public_functions(src: str, *, entry: str = "main", opt_out=NOT_WIRED) -> set[str]:
    return _public_functions(src) - _reachable(src, entry) - set(opt_out)


def test_every_public_function_is_reachable_from_main():
    src = pathlib.Path(GA.__file__).read_text(encoding="utf-8")
    unwired = unwired_public_functions(src)
    assert not unwired, (
        f"ops/gap_audit.py: public functions unreachable from main(): {sorted(unwired)} "
        "- they will never produce output. Wire them, or add them to NOT_WIRED with a reason.")


def test_the_wiring_guard_can_actually_fail():
    """실패할 수 있음을 증명하지 못한 가드는 가드가 아니다."""
    src = ("def main():\n    return helper()\n"
           "def helper():\n    return 1\n"
           "def orphan_metric():\n    return 2\n")
    assert unwired_public_functions(src) == {"orphan_metric"}


def test_daily_health_main_actually_reaches_the_gap_audit():
    """**만들었으면 아침 리포트에 실제로 찍혀야 한다.** 이것이 H-1 의 정확한 형태였다.

    함수만 추가하고 배선하지 않으면 테스트는 늘고 산출물은 아무도 안 만든다.
    """
    src = pathlib.Path(DH.__file__).read_text(encoding="utf-8")
    live = _reachable(src, "main") | {"main"}
    tree = ast.parse(src)
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in live:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
                    if sub.value.id in {"GA", "gap_audit"}:
                        used.add(sub.attr)
    assert used, ("ops/daily_health.py main() 에서 gap_audit 을 부르지 않는다 — "
                  "결손 감사가 아침 리포트에 찍히지 않는다는 뜻이다.")


# --------------------------------------------------------------------------- #
# 10. 장이 안 열린 창을 사고로 부르지 않는다 (2026-08-09 발견)
#
# 일요일 리포트(창 = 토 09:00 ~ 일 08:50)에는 미국장이 아예 없다. 그런데 관측 0개 ->
# `whole_window` 1430.0분 -> `ALERT_` 가 났다. 실측으로 08-02(일)·08-09(일)·**08-03(월)**
# 셋 다 같은 증상이다(월요일은 미확인이었으므로 다시 쟀다 — 창 = 일 09:00 ~ 월 08:50).
# 기계는 깨어 있었고 수집기도 살아 있었다(`col_up=90,301s`, 재기동 0회). 볼 것이 없었다.
#
# `max_hole` 은 이 리포트에서 가장 중요한 한 줄이다 — 08-06 정전을 옛 지표 대비 227배
# 차이로 잡아낸 지표다. 그게 주말마다 늑대를 외치면 읽는 사람이 그 줄을 건너뛰게 된다.
#
# ## 근거를 어디서 가져오는가 — `collector.log` 의 `telemetry session=` 줄
#
# `tossmon/analysis/session.py` 는 **쓸 수 없다**: 시각(time-of-day)만 보고 요일·휴장일
# 개념이 없다. 실측으로 `session_of(일요일 03:00 KST) == 'regular'` 다. 그것으로 고치면
# 이 버그는 그대로 남는다. 반면 수집기는 `/market-calendar/US` 로 세션을 켜고 끄며 그
# 판정을 5분마다 로그에 적는다 — **재계산이 아니라 그때 실제로 있었던 일의 기록**이고,
# gap_audit 이 이미 읽는 파일 안에 있다.
#
# ## 그리고 이 설계가 08-06 을 계속 잡는 이유
#
# **텔레메트리의 부재는 "장이 닫혔다"가 아니다.** 두 개의 인접한 `closed` 관측 사이가
# 충분히 짧을 때만 그 구간을 닫힘으로 인정한다. 08-06 처럼 수집기가 죽어 있었으면 줄
# 자체가 없으므로 닫힘 근거도 없고, 구멍은 `ALERT_` 로 남는다.
# --------------------------------------------------------------------------- #
def _tel(when: dt.datetime, session: str) -> str:
    return (f"{when:%Y-%m-%d %H:%M:%S},000 INFO    telemetry session={session} "
            "watch=1500 api_errors=0\n")


def _closed_log(dirp: pathlib.Path, a_ms: int, b_ms: int, session: str = "closed",
                step_s: int = 300) -> None:
    """[a,b] 를 5분 간격 telemetry 로 채운다 — 실측 케이던스가 p50=301s 다."""
    out = []
    t = a_ms
    while t <= b_ms:
        out.append(_tel(dt.datetime.fromtimestamp(t / 1000.0), session))
        t += step_s * 1000
    (dirp / "collector.log").write_text("".join(out), encoding="utf-8")


def test_the_sunday_window_is_a_note_not_an_alert(tmp_path):
    """고치기 전 실패 — 08-02·08-09 일요일 창이 그대로 ALERT_ 였다."""
    _closed_log(tmp_path, _W_START, _W_END)
    spans = GA.closed_spans(tmp_path, _W_START, _W_END)
    eh = GA.edge_holes([], _W_START, _W_END)
    hole = eh["holes"][0]
    assert hole.name == "whole_window"
    grade, why = GA.grade_hole(hole, [], None, spans)
    assert grade == "NOTE_", f"장이 안 열린 창이 아직 {grade} 다: {why}"
    assert "장이 열리지 않" in why


def test_the_monday_window_gets_the_same_treatment(tmp_path):
    """미확인이었던 월요일 창(일 09:00 ~ 월 08:50) — 실측 결과 같은 증상이었다."""
    mon_a = int(dt.datetime(2026, 8, 2, 9, 0, 0).timestamp() * 1000)
    mon_b = int(dt.datetime(2026, 8, 3, 8, 50, 0).timestamp() * 1000)
    _closed_log(tmp_path, mon_a, mon_b)
    grade, _ = GA.grade_hole(GA.Hole("whole_window", mon_a, mon_b), [], None,
                             GA.closed_spans(tmp_path, mon_a, mon_b))
    assert grade == "NOTE_"


# ---- ★ 대조군: 진짜 공백은 계속 잡혀야 한다. 여기가 무너지면 이 작업은 실패다 ---- #
def test_the_20260806_outage_is_still_an_alert(tmp_path):
    """08-06 의 295.5분 trailing_hole — 기계가 꺼져 있어서 텔레메트리가 **없다**.

    주말 예외가 이것까지 삼키면 이 프로젝트 최대 공백을 못 보게 된다.
    """
    dies_at = _W_END - 295 * 60_000
    _closed_log(tmp_path, _W_START, dies_at, session="regular")   # 죽기 전까지만 로그가 있다
    hole = GA.Hole("trailing_hole", dies_at, _W_END)
    grade, why = GA.grade_hole(hole, [], 12.0, GA.closed_spans(tmp_path, _W_START, _W_END))
    assert grade == "ALERT_", f"08-06 정전이 {grade} 로 삼켜졌다: {why}"
    assert "295" in why


def test_a_hole_during_an_open_session_is_still_an_alert(tmp_path):
    """텔레메트리가 있는데 `closed` 가 아니면 장은 열려 있었다 — 구멍은 진짜다."""
    dies_at = _W_END - 60 * 60_000
    _closed_log(tmp_path, _W_START, _W_END, session="regular")
    hole = GA.Hole("trailing_hole", dies_at, _W_END)
    grade, _ = GA.grade_hole(hole, [], 12.0, GA.closed_spans(tmp_path, _W_START, _W_END))
    assert grade == "ALERT_"


def test_absent_telemetry_is_never_read_as_market_closed(tmp_path):
    """로그가 통째로 없으면 '닫혔다'가 아니라 '못 봤다'다."""
    assert GA.closed_spans(tmp_path, _W_START, _W_END) == []
    assert GA.grade_hole(GA.Hole("whole_window", _W_START, _W_END), [], None, [])[0] == "ALERT_"


def test_a_long_telemetry_gap_between_two_closed_lines_is_not_bridged(tmp_path):
    """실측 08-01 09:15 -> 08-02 21:50 (2,196분). 양끝이 `closed` 라도 그 사이는 못 봤다.

    이 다리를 놓아버리면 주말에 수집기가 죽어도 전부 NOTE_ 가 된다.
    """
    mid = (_W_START + _W_END) // 2
    (tmp_path / "collector.log").write_text(
        _tel(dt.datetime.fromtimestamp(_W_START / 1000.0), "closed")
        + _tel(dt.datetime.fromtimestamp(_W_END / 1000.0), "closed"), encoding="utf-8")
    spans = GA.closed_spans(tmp_path, _W_START, _W_END)
    assert GA.uncovered_span_ms(_W_START, mid, spans) > 0, "관측이 없는 구간을 닫힘으로 덮었다"
    assert GA.grade_hole(GA.Hole("whole_window", _W_START, _W_END), [], None, spans)[0] == "ALERT_"


def test_a_partly_closed_hole_reports_only_the_unexplained_remainder(tmp_path):
    """절반은 휴장, 절반은 설명 없음 — NOTE_ 로 삼키지 않고 남은 만큼만 ALERT_."""
    mid = _W_START + 12 * 3_600_000
    _closed_log(tmp_path, _W_START, mid)
    grade, why = GA.grade_hole(GA.Hole("whole_window", _W_START, _W_END), [], None,
                               GA.closed_spans(tmp_path, _W_START, _W_END))
    assert grade == "ALERT_"
    # 720분이 관측됐고 마지막 관측 뒤로 한 케이던스(5분)만큼 가장자리 보정이 붙는다.
    assert "705" in why, why


def test_planned_and_closed_can_both_contribute(tmp_path):
    """계획 정비 + 휴장이 합쳐서 전부 덮으면 사람이 한 쪽이 이긴다(PLANNED_ = 무시)."""
    mid = _W_START + 12 * 3_600_000
    _closed_log(tmp_path, _W_START, mid)
    planned = [(mid, _W_END, "lid closed")]
    grade, why = GA.grade_hole(GA.Hole("whole_window", _W_START, _W_END), planned, None,
                               GA.closed_spans(tmp_path, _W_START, _W_END))
    assert grade == "PLANNED_", why


# ---- 파서 ---- #
def test_closed_spans_only_joins_adjacent_closed_observations(tmp_path):
    a = dt.datetime(2026, 8, 8, 12, 0, 0)
    lines = "".join([
        _tel(a, "closed"),
        _tel(a + dt.timedelta(minutes=5), "closed"),
        _tel(a + dt.timedelta(minutes=10), "regular"),   # 장이 열린다 — 다리가 끊긴다
        _tel(a + dt.timedelta(minutes=15), "closed"),
        _tel(a + dt.timedelta(minutes=20), "closed"),
    ])
    (tmp_path / "collector.log").write_text(lines, encoding="utf-8")
    w0 = int(a.timestamp() * 1000)
    spans = GA.closed_spans(tmp_path, w0 - 3_600_000, w0 + 3_600_000)
    assert len(spans) == 2, spans
    # 가장자리 보정이 **열린 세션 관측을 건너뛰면 안 된다.** 실제로 낸 버그다: 양쪽으로
    # 한 케이던스씩 늘렸더니 minute 10 의 `regular` 를 뛰어넘어 두 구간이 하나로 붙었다.
    open_at = w0 + 10 * 60_000
    for lo, hi, _w in spans:
        assert not (lo < open_at < hi), f"장이 열려 있던 시각을 휴장으로 덮었다: {spans}"
    assert spans[0][1] == w0 + 5 * 60_000, "오른쪽에 관측이 있는데 늘렸다"
    assert spans[1][0] == w0 + 15 * 60_000, "왼쪽에 관측이 있는데 늘렸다"


def test_closed_spans_ignores_lines_outside_the_window(tmp_path):
    a = dt.datetime(2026, 8, 8, 12, 0, 0)
    (tmp_path / "collector.log").write_text(
        _tel(a, "closed") + _tel(a + dt.timedelta(minutes=5), "closed"), encoding="utf-8")
    far = int(dt.datetime(2026, 8, 1, 0, 0, 0).timestamp() * 1000)
    assert GA.closed_spans(tmp_path, far, far + 3_600_000) == []


def test_closed_spans_reads_rotated_logs_too(tmp_path):
    """회전본을 안 읽으면 주말 아침에 막 회전된 날이 통째로 ALERT_ 로 돌아온다."""
    a = dt.datetime(2026, 8, 8, 12, 0, 0)
    import gzip as _gz
    with _gz.open(tmp_path / "collector.log.20260808.gz", "wt", encoding="utf-8") as fh:
        fh.write(_tel(a, "closed") + _tel(a + dt.timedelta(minutes=5), "closed"))
    (tmp_path / "collector.log").write_text("", encoding="utf-8")
    w0 = int(a.timestamp() * 1000)
    assert len(GA.closed_spans(tmp_path, w0 - 60_000, w0 + 3_600_000)) == 1

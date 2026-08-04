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
def _mem_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
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
    """)
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

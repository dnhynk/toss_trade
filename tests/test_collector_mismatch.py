"""`schema_mismatch` 와 `symbol_not_found` 의 경계를 고정한다 — 소유: W4.

왜 이 파일이 있는가 (2026-08-04 실측):

    2026-07-31 20:40:55 WARNING tier2:AVAT: schema mismatch (skip): http-404 code=stock-not-found
    2026-08-03 23:16:12 WARNING tier2:BLRK: ...  23:16:26 tier2:CABR: ...
    2026-08-03 23:18:13 WARNING tier2:MACI: ...  23:21:31 tier2:JSM: ...

collector.log 전수에서 `schema_mismatch` 로 집계된 것은 **5건이고 전부 이 형태**다.
진짜 응답 모양 변경은 한 번도 없었는데, 이 카운터에 걸린 경보 문구는
"the API response shape changed. A restart will NOT fix this. Inspect the endpoint
contract before trusting today's data" 였다 — **멀쩡한 하루치 데이터를 의심하게 만들었다.**

`schema_mismatch` 는 "그날 데이터를 믿지 마라"의 유일한 근거다. 묽어지면 안 된다.
그래서 경계를 양쪽에서 못박는다: 없는 종목이 `schema_mismatch` 를 올리지 않는 것과,
**진짜 모양 변경이 `symbol_not_found` 로 새지 않는 것**을 같은 무게로 검증한다.
"""
from __future__ import annotations

import asyncio

import pytest

from tossmon.api.errors import SchemaMismatch
from tossmon.collector import loops
from tossmon.collector.mismatch import (
    COUNTER_SCHEMA_MISMATCH,
    COUNTER_SYMBOL_NOT_FOUND,
    NotFoundTally,
    counter_for,
    is_symbol_not_found,
    parse_http_detail,
    symbol_from_loop_name,
)
from tests.test_collector_loops import StubClient, build_ctx

#: 라이브에서 실제로 온 문자열 그대로 (message 끝의 마침표까지 포함).
#: `client._classify` 가 `f"http-{status} {_err_code(resp)}"` 로 만든다.
LIVE_NOT_FOUND = "http-404 code=stock-not-found message=종목을 찾을 수 없습니다."


# --------------------------------------------------------------------------- #
# 분류 조건 — 상태코드와 에러코드 **둘 다** 맞아야 한다
# --------------------------------------------------------------------------- #
def test_live_stock_not_found_detail_is_symbol_not_found():
    """실측 문자열이 없는 종목으로 분류된다 (이 테스트가 회귀의 기준선이다)."""
    assert parse_http_detail(LIVE_NOT_FOUND) == (404, "stock-not-found")
    assert is_symbol_not_found(LIVE_NOT_FOUND) is True
    assert counter_for(LIVE_NOT_FOUND) == COUNTER_SYMBOL_NOT_FOUND


@pytest.mark.parametrize("detail", [
    "http-404 code=invalid-parameter message=잘못된 요청입니다.",
    "http-404 code=endpoint-not-found message=",
    "http-404 code= message=empty code",
    "http-404 error=plain-string-error",          # _err_code 의 str 분기
    "http-404 <no error field>",                  # error 키 자체가 없다
    "http-404 <unparseable>",                     # 본문이 JSON 이 아니다
])
def test_404_with_a_different_code_stays_schema_mismatch(detail):
    """404 지만 code 가 다르면 계약 변경 쪽으로 남긴다.

    본문을 읽을 수 없는 404(`<unparseable>`/`<no error field>`)도 여기다 — "없는 종목"
    이라고 판단할 **근거가 없는데** 조용히 넘기면 진짜 사고를 놓친다. 판단 불가는
    안전한 쪽(의심)으로 분류한다.
    """
    assert is_symbol_not_found(detail) is False
    assert counter_for(detail) == COUNTER_SCHEMA_MISMATCH


@pytest.mark.parametrize("detail", [
    "http-400 code=stock-not-found message=종목을 찾을 수 없습니다.",
    "http-409 code=stock-not-found",
    "http-422 code=stock-not-found",
    "http-200 code=stock-not-found",
])
def test_same_code_with_a_different_status_stays_schema_mismatch(detail):
    """code 는 같은데 상태코드가 다르면 서버 동작이 바뀐 것이다 — 넘기지 않는다."""
    assert is_symbol_not_found(detail) is False
    assert counter_for(detail) == COUNTER_SCHEMA_MISMATCH


@pytest.mark.parametrize("detail", [
    "candles: result.candles is not a list",
    "rankings: row.price missing",
    "prices: missing/blank field 'lastPrice'",
    "envelope is list, expected object",
    "non-json body (text/html)",
    "naive timestamp (offset required): '2026-08-04T09:00:00'",
    "exchange-rate: bad rate 'N/A'",
])
def test_real_shape_changes_stay_schema_mismatch(detail):
    """진짜 모양 변경은 그대로 `schema_mismatch` — 이 카운터가 묽어지면 안 된다."""
    assert parse_http_detail(detail) == (None, None)
    assert counter_for(detail) == COUNTER_SCHEMA_MISMATCH


@pytest.mark.parametrize("detail", [
    # 중첩 필드 오류는 `f"{where}.{key}: {exc.detail}"` 로 감싸인다. 부분일치로 가르면
    # 이런 문자열이 "없는 종목" 으로 새어 진짜 사고가 조용해진다.
    "candles.code: http-404 code=stock-not-found",
    "prices.symbol: not a decimal: 'http-404 code=stock-not-found'",
    "rankings: row.name missing (was 'stock-not-found')",
    " http-404 code=stock-not-found",              # 앞에 공백 — 우리가 만든 형태가 아니다
    "xhttp-404 code=stock-not-found",
])
def test_substring_lookalikes_do_not_leak_into_symbol_not_found(detail):
    """부분일치 금지 — detail 이 `http-<status>` 로 **시작**할 때만 HTTP 유래로 본다."""
    assert is_symbol_not_found(detail) is False
    assert counter_for(detail) == COUNTER_SCHEMA_MISMATCH


@pytest.mark.parametrize("detail", ["", "http-", "http-40 code=stock-not-found",
                                    "http-4040 code=stock-not-found"])
def test_malformed_details_are_not_symbol_not_found(detail):
    assert is_symbol_not_found(detail) is False


# --------------------------------------------------------------------------- #
# 루프 이름 → 심볼
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name,expected", [
    ("tier2:AVAT", "AVAT"),
    ("tier3:trades:RGNT", "RGNT"),
    ("tier3:book:HOWL", "HOWL"),
    ("tier2book:JSM", "JSM"),
    ("tier2:BRK.A", "BRK.A"),
    ("tier2:AA-B", "AA-B"),
    ("tier1", None),                   # 배치 스윕 — 심볼 하나로 귀속시킬 수 없다
    ("rankings", None),
    ("session", None),
    ("", None),
    ("tier3:trades:", None),
    ("tier3:trades:lowercase", None),   # 정규화된 심볼은 대문자다
])
def test_symbol_from_loop_name(name, expected):
    assert symbol_from_loop_name(name) == expected


# --------------------------------------------------------------------------- #
# 심볼별 집계 — "반복 404 심볼" 판단 재료 (판단·정리 자체는 이 태스크 범위 밖)
# --------------------------------------------------------------------------- #
def test_tally_counts_per_symbol_and_flags_repeat_offenders():
    tally = NotFoundTally()
    assert tally.add("avat") == 1                  # 대소문자 정규화
    assert tally.add("AVAT") == 2
    assert tally.add("BLRK") == 1
    assert tally.total() == 3
    assert tally.repeat_symbols() == [("AVAT", 2)]         # 1회짜리는 후보가 아니다
    assert tally.top(1) == [("AVAT", 2)]
    assert tally.add("") == 0                      # 심볼 없는 루프는 기록하지 않는다
    assert tally.total() == 3


def test_tally_is_bounded_and_says_so_instead_of_going_silent():
    """무인 장시간 실행 — 심볼 사전이 무한히 자라면 안 된다.

    상한에 닿으면 **새** 심볼만 포기하고, 이미 추적 중인 반복 404 심볼은 계속 센다.
    포기한 건수는 `untracked=` 로 드러난다 (조용한 절단 금지).
    """
    tally = NotFoundTally(max_symbols=2)
    tally.add("AAA")
    tally.add("BBB")
    assert tally.add("CCC") == 0                   # 상한 초과 — 개별 추적 포기
    assert tally.overflow == 1
    assert tally.add("AAA") == 2                   # 기존 심볼은 계속 센다
    assert tally.total() == 4
    assert "untracked=1" in tally.describe()


def test_tally_describe_is_ascii_only_and_empty_when_unused():
    tally = NotFoundTally()
    assert tally.describe() == ""
    for sym in ("AVAT", "AVAT", "BLRK", "CABR", "MACI", "JSM", "ZZZ"):
        tally.add(sym)
    line = tally.describe(n=3)
    line.encode("ascii")                           # 콘솔 안전 (비ASCII 금지)
    assert line.startswith("AVAT=2")
    assert "+3 more" in line                       # 6종목 중 3개만 보여줬다고 밝힌다


# --------------------------------------------------------------------------- #
# `_guarded` 회귀 — 여기가 실제 사고 지점이다
# --------------------------------------------------------------------------- #
def _raise_in_tier2(detail):
    class Broken(StubClient):
        async def get_candles(self, *a, **kw):
            raise SchemaMismatch(detail)

    return Broken


def _run_guarded(ctx, name, detail):
    async def body():
        raise SchemaMismatch(detail)

    return asyncio.run(loops._guarded(ctx, name, body()))


def test_stock_not_found_404_does_not_raise_schema_mismatch(tmp_path):
    """없는 종목(상장폐지·거래정지)이 "API 계약이 바뀌었다" 로 집계되지 않는다."""
    ctx, _ = build_ctx(tmp_path, StubClient({}))
    try:
        assert _run_guarded(ctx, "tier2:AVAT", LIVE_NOT_FOUND) is False   # 그 종목만 스킵
        assert ctx.counters.get(COUNTER_SCHEMA_MISMATCH, 0) == 0
        assert ctx.counters.get(COUNTER_SYMBOL_NOT_FOUND, 0) == 1
        assert ctx.running()                                   # 루프는 계속 돈다
    finally:
        ctx.store.close()


def test_real_shape_change_still_raises_schema_mismatch(tmp_path):
    """반대 방향 — 진짜 모양 변경은 여전히 `schema_mismatch` 로 올라간다."""
    ctx, _ = build_ctx(tmp_path, StubClient({}))
    try:
        _run_guarded(ctx, "tier2:AVAT", "candles: result.candles is not a list")
        assert ctx.counters.get(COUNTER_SCHEMA_MISMATCH, 0) == 1
        assert ctx.counters.get(COUNTER_SYMBOL_NOT_FOUND, 0) == 0
    finally:
        ctx.store.close()


def test_symbol_not_found_is_tallied_per_symbol(tmp_path):
    """어떤 심볼이 얼마나 자주 404 인지 남긴다 (워치리스트 정리 판단 재료)."""
    ctx, _ = build_ctx(tmp_path, StubClient({}))
    try:
        for name in ("tier2:AVAT", "tier3:trades:AVAT", "tier2:BLRK", "tier1"):
            _run_guarded(ctx, name, LIVE_NOT_FOUND)
        assert ctx.counters.get(COUNTER_SYMBOL_NOT_FOUND, 0) == 4
        assert ctx.not_found.counts.get("AVAT") == 2           # 루프가 달라도 같은 심볼
        assert ctx.not_found.counts.get("BLRK") == 1
        assert ctx.not_found.repeat_symbols() == [("AVAT", 2)]
        assert "tier1" not in ctx.not_found.counts             # 심볼 아닌 이름은 안 센다
    finally:
        ctx.store.close()


def test_telemetry_exposes_symbol_not_found_separately(tmp_path):
    """워치독·사람이 5분마다 읽는 줄에서 두 카운터가 **따로** 보인다."""
    ctx, _ = build_ctx(tmp_path, StubClient({}))
    try:
        _run_guarded(ctx, "tier2:AVAT", LIVE_NOT_FOUND)
        _run_guarded(ctx, "tier2:BBB", "candles: result.candles is not a list")
        data = ctx.telemetry()
        assert data["symbol_not_found"] == 1
        assert data["schema_mismatch"] == 1
        assert data["symbol_not_found_symbols"] == 1           # 몇 종목이 걸렸나
    finally:
        ctx.store.close()


def test_repeat_report_names_symbols_and_stays_quiet_when_there_is_no_repeat(tmp_path):
    """카운트만으로는 "1종목이 40번" 과 "40종목이 1번씩" 을 구분할 수 없다 — 이름을 밝힌다.

    반복이 없을 때는 한 줄도 내지 않는다 (평시 로그 소음 금지, W5 워치독 판독 보호).
    """
    ctx, _ = build_ctx(tmp_path, StubClient({}))
    try:
        for name in ("tier2:AAA", "tier2:BBB"):                # 1회씩 — 반복 아님
            _run_guarded(ctx, name, LIVE_NOT_FOUND)
        before = ctx.notifier.counters["info"]
        ctx._report_repeat_not_found()
        assert ctx.notifier.counters["info"] == before          # 조용하다

        _run_guarded(ctx, "tier2:AAA", LIVE_NOT_FOUND)          # 이제 AAA 가 2회
        ctx._report_repeat_not_found()
        assert ctx.notifier.counters["info"] == before + 1
    finally:
        ctx.store.close()


def test_repeat_404_symbols_are_reported_but_not_dropped(tmp_path):
    """반복 404 심볼은 **보고만** 한다 — 워치리스트에서 빼는 판단은 범위 밖(코디네이터 지시)."""
    ctx, _ = build_ctx(tmp_path, StubClient({}), symbols=("AVAT",))
    try:
        for _ in range(4):
            _run_guarded(ctx, "tier2:AVAT", LIVE_NOT_FOUND)
        assert ctx.not_found.counts["AVAT"] == 4
        assert "AVAT" in ctx.watchlist                         # 자동으로 빼지 않는다
        # 사람이 볼 수 있게 로그에는 남는다.
        assert ctx.notifier.counters["warn"] >= 1
    finally:
        ctx.store.close()

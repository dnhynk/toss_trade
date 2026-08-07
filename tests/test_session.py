"""세션 차원 (docs/23 §10-Q) — 경계·사이클 날짜·비용 모델의 정직성.

## 왜 이 파일이 있나

사용자 지적(2026-08-03): **"정규장이 아닌 거래 데이터에서는 거래량과 유동성이 현저히
낮다."** 우리는 그때까지 **전 세션을 뭉쳐** 단일 비용 2.38% 를 써 왔다.

여기서 지키는 것:

1. **경계가 한 곳에만 있다** — 두 러너가 같은 함수를 쓰고, 각자 재정의하지 않는다.
2. **자정을 넘는 정규장이 두 날로 쪼개지지 않는다**(사이클 날짜).
3. **낼 수 없는 비용을 지어내지 않는다** — 최우선 호가만 있는 세션에서 큰 클립 비용은
   `measurable=False` 이고 `NaN` 이다. 0 이나 다른 세션 값으로 채우지 않는다.
"""
from __future__ import annotations

import pathlib
import sqlite3

import pandas as pd
import pytest

from tossmon.analysis import session as SS
from tossmon.analysis.measure import design_b as D
from tossmon.analysis.measure import exit_value as V

KST = SS.KST_OFFSET_MS
HOUR = 3_600_000


def at_kst(h: int, m: int = 0, *, day: int = 0) -> int:
    """2026-08-03 KST `h:m` 의 epoch ms (편의용 고정 기준일)."""
    base = int(pd.Timestamp("2026-08-03T00:00:00").value // 1_000_000) - KST
    return base + day * 24 * HOUR + h * HOUR + m * 60_000


# --------------------------------------------------------------------------- #
# 1. 경계
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("h,m,expected", [
    (9, 0, "day"), (12, 0, "day"), (16, 59, "day"),
    (17, 0, "pre"), (22, 29, "pre"),
    (22, 30, "regular"), (23, 59, "regular"), (0, 30, "regular"), (4, 59, "regular"),
    (5, 0, "after"), (8, 49, "after"),
    (8, 55, "closed"),
])
def test_session_boundaries_are_start_inclusive_end_exclusive(h, m, expected):
    assert SS.session_of(at_kst(h, m)) == expected


def test_regular_session_spans_midnight():
    """정규장이 자정을 넘는다 — 이걸 놓치면 한 세션이 두 조각으로 갈린다."""
    assert SS.session_of(at_kst(23, 30)) == "regular"
    assert SS.session_of(at_kst(1, 0, day=1)) == "regular"


def test_every_session_name_is_declared():
    for h in range(24):
        assert SS.session_of(at_kst(h)) in SS.SESSIONS


# --------------------------------------------------------------------------- #
# 2. 사이클 날짜
# --------------------------------------------------------------------------- #
def test_one_us_trading_cycle_shares_a_single_session_date():
    """09:00 에 시작해 다음 날 08:50 에 끝나는 한 묶음이 **같은 날짜**를 가져야 한다."""
    d = SS.session_date(at_kst(9, 0))
    for h, m, day in ((12, 0, 0), (18, 0, 0), (23, 0, 0), (2, 0, 1), (7, 0, 1)):
        assert SS.session_date(at_kst(h, m, day=day)) == d, (h, m, day)


def test_next_cycle_starts_at_nine_kst():
    a = SS.session_date(at_kst(8, 0, day=1))
    b = SS.session_date(at_kst(9, 0, day=1))
    assert a != b


# --------------------------------------------------------------------------- #
# 3. 벡터화 판정이 스칼라와 일치한다
# --------------------------------------------------------------------------- #
def test_vectorised_and_scalar_agree():
    ts = pd.Series([at_kst(h, mm) for h in range(24) for mm in (0, 31)])
    fast = SS.sessions_of(ts).tolist()
    slow = [SS.session_of(int(x)) for x in ts]
    assert fast == slow


def test_session_counts_keeps_zero_sessions_visible():
    """0 건인 세션이 표에서 사라지면 '없다'와 '안 쟀다'가 구분되지 않는다."""
    got = SS.session_counts(pd.Series([at_kst(12)]))
    assert set(got) == set(SS.SESSIONS)
    assert got["day"] == 1 and got["regular"] == 0


# --------------------------------------------------------------------------- #
# 4. 정의는 한 곳에만 있다
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mod", [D, V])
def test_runners_share_the_session_definition_instead_of_redefining_it(mod):
    src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    assert "session as SS" in src, f"{mod.__name__} must import the shared module"
    for redefinition in ("def session_of(", "22 * 60 + 30", "KST_OFFSET"):
        assert redefinition not in src, (
            f"{mod.__name__} re-defines a session boundary - keep it in session.py")


# --------------------------------------------------------------------------- #
# 5. 비용 모델 — 낼 수 없는 칸을 지어내지 않는다
# --------------------------------------------------------------------------- #
def _books(rows) -> pd.DataFrame:
    """(session, n_ask_lv, cost, exhausted) 로 최소 호가표를 만든다."""
    recs = []
    for sess, lv, cost, exh in rows:
        r = {"symbol": f"S{len(recs) % 7}", "snap_ms": 0, "session": sess,
             "n_bid_lv": lv, "n_ask_lv": lv, "rel_spread": cost / 2,
             "mid_usd": 3.0, "tob_min_usd": 500.0, "band": "$2-5"}
        for clip in D.SESSION_CLIPS:
            r[f"rt_{clip}"] = cost
            r[f"exhausted_{clip}"] = exh and clip > 100
        recs.append(r)
    return pd.DataFrame(recs)


def test_top_of_book_only_sessions_cannot_report_large_clip_costs():
    """**이 검사가 이 작업의 핵심 정직성이다.**

    최우선 호가 1단만 저장된 세션에서 $500 이상 클립 비용은 **알 수 없다.**
    다른 세션 값이나 0 으로 채우면 전략 판정이 통째로 틀어진다.
    """
    books = _books([("regular", 1, 0.03, True)] * 60)
    got = D.session_clip_costs(books)["regular"]["by_clip"]
    assert got[100]["measurable"] is True
    for clip in (500, 1000, 2000):
        assert got[clip]["measurable"] is False
        assert got[clip]["median"] != got[clip]["median"]        # NaN
        assert "top-of-book" in got[clip]["reason"]


def test_multi_level_sessions_do_report_clip_costs():
    books = _books([("day", 10, 0.02, False)] * 60)
    got = D.session_clip_costs(books)["day"]["by_clip"]
    assert all(got[c]["measurable"] for c in D.SESSION_CLIPS)


def test_thin_sessions_are_not_judged():
    books = _books([("after", 10, 0.05, False)] * 5)
    got = D.session_clip_costs(books)["after"]["by_clip"][100]
    assert got["measurable"] is False and got["n"] == 5


def test_session_round_trip_returns_nan_rather_than_a_substitute():
    """못 잰 세션은 **NaN** 이다 — 단일 2.38% 로 슬쩍 되돌아가지 않는다."""
    model = {"all_bands": D.session_clip_costs(_books([("regular", 1, 0.03, True)] * 60))}
    assert D.session_round_trip(model, "regular", clip=100) == pytest.approx(0.03)
    assert D.session_round_trip(model, "regular", clip=2000) != \
        D.session_round_trip(model, "regular", clip=2000)
    assert D.session_round_trip(model, "nonexistent") != \
        D.session_round_trip(model, "nonexistent")


def test_within_symbol_contrast_uses_only_symbols_present_in_both_sessions():
    """세션마다 종목 구성이 다르므로 **같은 종목**만 비교해야 한다."""
    books = pd.DataFrame([
        {"symbol": "A", "session": "regular", "rel_spread": 0.02},
        {"symbol": "A", "session": "pre", "rel_spread": 0.12},
        {"symbol": "B", "session": "regular", "rel_spread": 0.03},
        {"symbol": "C", "session": "pre", "rel_spread": 0.90},     # regular 없음 -> 제외
    ])
    got = D.within_symbol_cost_contrast(books)
    assert got["available"] is True
    pre = got["pairs"]["pre"]
    assert pre["n_symbols"] == 1                     # A 만 짝이 맺힌다
    assert pre["median_within_symbol_delta"] == pytest.approx(0.10)
    assert pre["powered"] is False                   # n<10 이면 판정하지 않는다


def test_depth_completeness_is_reported_so_the_cost_table_can_be_read_honestly():
    books = _books([("day", 10, 0.02, False)] * 3 + [("regular", 1, 0.03, True)] * 3)
    got = D.depth_completeness(books)
    assert got["day"]["multi_level_rate"] == pytest.approx(1.0)
    assert got["regular"]["multi_level_rate"] == pytest.approx(0.0)


def test_missing_orderbook_table_yields_an_empty_model_not_a_crash():
    conn = sqlite3.connect(":memory:")
    assert D.book_rows(conn).empty
    conn.close()


# --------------------------------------------------------------------------- #
# 6. 수집기 재시작 경계 — 표본 구성이 바뀐 지점
# --------------------------------------------------------------------------- #
def test_collector_restart_boundary_is_the_announced_instant():
    """2026-08-04 00:07:28 KST. 이 뒤로 티어 승격 정책이 다르다."""
    kst = pd.Timestamp(SS.COLLECTOR_RESTART_MS + SS.KST_OFFSET_MS, unit="ms")
    assert kst.strftime("%Y-%m-%d %H:%M:%S") == "2026-08-04 00:07:28"


def test_era_split_is_start_inclusive_on_the_post_side():
    assert SS.collector_era(SS.COLLECTOR_RESTART_MS - 1) == "era0_pre_restart"
    assert SS.collector_era(SS.COLLECTOR_RESTART_MS) == "era1_post_restart"


def test_there_is_more_than_one_collector_boundary():
    """경계를 하나만 알면 두 번째 경계를 넘어 뭉치게 된다 (02:26 KST)."""
    assert len(SS.COLLECTOR_BOUNDARIES_MS) == len(SS.COLLECTOR_ERAS) - 1
    assert list(SS.COLLECTOR_BOUNDARIES_MS) == sorted(SS.COLLECTOR_BOUNDARIES_MS)
    last = SS.COLLECTOR_BOUNDARIES_MS[-1]
    assert SS.collector_era(last - 1) == "era1_post_restart"
    assert SS.collector_era(last) == "era2_post_0226"


def test_vectorised_era_matches_the_scalar_rule():
    ts = pd.Series([SS.COLLECTOR_RESTART_MS - 60_000, SS.COLLECTOR_RESTART_MS,
                    SS.COLLECTOR_RESTART_MS + 60_000])
    assert SS.eras_of(ts).tolist() == [SS.collector_era(int(x)) for x in ts]


# --------------------------------------------------------------------------- #
# 7. 홀드아웃 봉인 — 봉 라벨은 **종료 시각**이다
#    (docs/12 §6.1 봉 라벨 규약 · §6.2 항목 5, 2026-08-07 개정, 사용자 승인)
# --------------------------------------------------------------------------- #
def utc_ms(iso: str) -> int:
    """UTC ISO 문자열 -> epoch ms.

    **합성 픽스처를 쓰지 않는다.** 기존 1분봉 테스트가 이 결함을 놓친 이유는
    생성기와 소비자가 **같은 규약**을 써서 어느 쪽으로 틀려도 초록이었기 때문이다
    (`docs/36` §4). 그래서 여기서는 경계를 **절대 시각으로 못 박는다.**
    """
    return int(pd.Timestamp(iso).value // 1_000_000)


@pytest.mark.parametrize("label,sealed", [
    # 앞 경계 — 라벨 05-01T00:00Z 봉이 담는 것은 사이클 04-30 이라 **봉인 밖**이다.
    ("2026-04-30T23:59:00Z", False),        # 담는 구간 23:58-23:59 -> 04-30
    ("2026-05-01T00:00:00Z", False),        # 담는 구간 23:59-00:00 -> 04-30  <- 개정 전 과보수
    ("2026-05-01T00:01:00Z", True),         # 담는 구간 00:00-00:01 -> 05-01, 봉인 첫 봉
    # 뒷 경계 — 라벨 07-30T00:00Z 봉이 담는 것은 사이클 07-29 라 **봉인 안**이다.
    ("2026-07-29T23:59:00Z", True),         # 담는 구간 23:58-23:59 -> 07-29
    ("2026-07-30T00:00:00Z", True),         # 담는 구간 23:59-00:00 -> 07-29  <- 개정 전 누출
    ("2026-07-30T00:01:00Z", False),        # 담는 구간 00:00-00:01 -> 07-30, 봉인 밖 첫 봉
])
def test_holdout_seal_reads_the_bar_label_as_an_end_time(label, sealed):
    """`ts_ms = T` 인 봉은 `[T-60초, T)` 를 담는다 — 소속은 `T` 가 아니라 `T-60초`."""
    assert SS.is_holdout(utc_ms(label)) is sealed, label


def test_the_two_boundary_labels_move_in_opposite_directions():
    """**어느 봉이 어느 쪽으로 갔는지**를 고정한다 — 개수만 세면 둘 다 1/1 이라 안 잡힌다."""
    front, back = utc_ms("2026-05-01T00:00:00Z"), utc_ms("2026-07-30T00:00:00Z")
    got = SS.drop_holdout(pd.DataFrame({"ts_ms": [front, back]}))
    assert got["n_dropped"] == 1 and got["n_kept"] == 1
    assert got["kept"]["ts_ms"].tolist() == [front]      # 앞 경계는 **되돌려받는다**


def test_the_offset_is_a_full_minute_not_one_millisecond():
    """개정 문언이 `ts_ms - 60_000` 이다. 분 정렬 봉에서는 `- 1` 과 동등하지만
    `drop_holdout` 은 `trades_snap`(체결 시각, 분 정렬 아님)에도 걸리므로 갈린다.
    문언과 코드가 갈라지면 다음 사람이 또 헷갈린다 — 이번 사고가 정확히 그것이었다.
    """
    assert SS.is_holdout(utc_ms("2026-05-01T00:00:30Z")) is False
    assert SS.is_holdout(utc_ms("2026-07-30T00:00:30Z")) is True

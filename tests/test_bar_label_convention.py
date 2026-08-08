"""봉 라벨 규약을 **절대 시각으로** 못 박는다 — 소유: W3 (docs/47).

## 왜 이 파일이 따로 있나

`docs/36` §4 가 밝힌 실패는 이것이다: 합성 생성기(`tests/synth.py`)와 소비자
(`tossmon/analysis/**`)가 **같은 규약**을 쓰면, 그 규약이 API 사실과 어긋나도
**모든 테스트가 초록**이다. 실제로 이 저장소는 1분봉 라벨을 시작 시각으로 읽으면서
1,850개 테스트를 전부 통과시켰다 — "성공을 반환하는 조용한 실패".

그래서 이 파일은 **합성기를 쓰지 않는다.** 모든 시각을 하드코딩한 epoch ms 로 두고,
그 옆에 ISO 문자열을 함께 적어 사람이 눈으로 대조할 수 있게 한다. 생성기가 어떤
규약으로 바뀌어도 이 파일은 따라 움직이지 않는다.

## 못 박는 사실 (사전등록 §6.1, docs/36 §1 3중 검증)

`candles_1m.ts_ms = T` 인 봉은 `[T−60초, T)` 를 담고 시각 `T` 에 **이미 완결**돼 있다.
따라서 구간 소속은 `start <= ts < end` 가 아니라 **`start < ts <= end`** 다.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from tossmon.analysis import baselines as B
from tossmon.analysis import features as F
from tossmon.analysis import labeling as L
from tossmon.analysis import session as SS
from tossmon.api.models import SessionWindow, UsMarketDay

MIN_MS = 60_000

# --------------------------------------------------------------------------- #
# 하드코딩된 절대 시각 — 아래 self-check 가 ISO 표기와 일치함을 매 실행 확인한다
# --------------------------------------------------------------------------- #
REG_OPEN = 1_780_320_600_000     # 2026-06-01T13:30:00Z  EDT 정규장 개장 (09:30 ET)
REG_CLOSE = 1_780_344_000_000    # 2026-06-01T20:00:00Z  EDT 정규장 종료 (16:00 ET)
PRE_OPEN = 1_780_300_800_000     # 2026-06-01T08:00:00Z  EDT 프리마켓 개장 (04:00 ET)
NEXT_OPEN = 1_780_407_000_000    # 2026-06-02T13:30:00Z  다음 매매일 개장
HOLDOUT_FRONT = 1_777_593_600_000  # 2026-05-01T00:00:00Z 봉인 **앞** 경계 라벨
HOLDOUT_BACK = 1_785_369_600_000   # 2026-07-30T00:00:00Z 봉인 **뒤** 경계 라벨


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def test_hardcoded_constants_are_what_the_comments_say() -> None:
    """상수를 잘못 적어 놓고 그 상수에 맞춰 통과하는 사고를 막는다."""
    assert _iso(REG_OPEN) == "2026-06-01T13:30:00Z"
    assert _iso(REG_CLOSE) == "2026-06-01T20:00:00Z"
    assert _iso(PRE_OPEN) == "2026-06-01T08:00:00Z"
    assert _iso(NEXT_OPEN) == "2026-06-02T13:30:00Z"
    assert _iso(HOLDOUT_FRONT) == "2026-05-01T00:00:00Z"
    assert _iso(HOLDOUT_BACK) == "2026-07-30T00:00:00Z"


# --------------------------------------------------------------------------- #
# 규약 자체
# --------------------------------------------------------------------------- #
def test_bar_start_is_one_minute_before_the_label() -> None:
    """라벨 20:00:00Z 인 봉은 19:59:00Z~20:00:00Z 를 담는다."""
    assert SS.bar_start_ms(REG_CLOSE) == 1_780_343_940_000
    assert _iso(SS.bar_start_ms(REG_CLOSE)) == "2026-06-01T19:59:00Z"
    assert SS.bar_start_ms(REG_OPEN) == REG_OPEN - MIN_MS


def test_bar_starts_is_the_vectorised_twin() -> None:
    s = pd.Series([REG_OPEN, REG_CLOSE], dtype="int64")
    assert SS.bar_starts_ms(s).tolist() == [REG_OPEN - MIN_MS, REG_CLOSE - MIN_MS]


def test_membership_is_start_exclusive_end_inclusive() -> None:
    """`start <= ts < end` 가 아니라 `start < ts <= end` 다 (사전등록 §6.1)."""
    def belongs(ts: int) -> bool:
        b = SS.bar_start_ms(ts)
        return REG_OPEN <= b < REG_CLOSE

    assert not belongs(REG_OPEN), "라벨 = 개장 시각인 봉은 프리마켓 마지막 분이다"
    assert belongs(REG_OPEN + MIN_MS), "정규장 첫 분의 봉은 라벨 = 개장 + 1분"
    assert belongs(REG_CLOSE), "정규장 마지막 분(종가 경매)의 봉은 라벨 = 종료 시각"
    assert not belongs(REG_CLOSE + MIN_MS)


# --------------------------------------------------------------------------- #
# 홀드아웃 봉인 — docs/36 §3-3 · docs/40 §1 의 실측을 절대 시각으로 고정
# --------------------------------------------------------------------------- #
def test_holdout_boundaries_use_the_bar_content_not_the_label() -> None:
    """앞 경계는 과보수를 풀고, 뒤 경계는 9봉 누출을 막는다 (사전등록 §6.2 항목 5)."""
    # 라벨 2026-05-01T00:00Z 봉의 내용은 04-30 23:59~05-01 00:00 = 사이클 04-30 → 합법
    assert not SS.is_holdout(HOLDOUT_FRONT)
    # 라벨 2026-07-30T00:00Z 봉의 내용은 07-29 23:59~07-30 00:00 = 사이클 07-29 → 봉인
    assert SS.is_holdout(HOLDOUT_BACK)
    # 그 바로 다음 봉부터가 라이브 확인 구간이다
    assert not SS.is_holdout(HOLDOUT_BACK + MIN_MS)
    # 앞 경계 직전 봉은 검증 구간(04-30) 안이라 여전히 합법
    assert not SS.is_holdout(HOLDOUT_FRONT - MIN_MS)


def test_drop_holdout_drops_exactly_the_sealed_bars() -> None:
    df = pd.DataFrame({"ts_ms": [HOLDOUT_FRONT, HOLDOUT_BACK, HOLDOUT_BACK + MIN_MS]})
    got = SS.drop_holdout(df, "ts_ms")
    assert got["n_dropped"] == 1
    assert got["kept"]["ts_ms"].tolist() == [HOLDOUT_FRONT, HOLDOUT_BACK + MIN_MS]


# --------------------------------------------------------------------------- #
# 공통 픽스처 — **합성기를 쓰지 않는다**
# --------------------------------------------------------------------------- #
def _market_day() -> UsMarketDay:
    """2026-06-01 EDT. 프리마켓 08:00–13:30Z, 정규장 13:30–20:00Z."""
    return UsMarketDay(
        date="2026-06-01", day=None,
        pre=SessionWindow(start_ms=PRE_OPEN, end_ms=REG_OPEN),
        regular=SessionWindow(start_ms=REG_OPEN, end_ms=REG_CLOSE), after=None)


def _frame(rows: list[tuple[int, int, int]]) -> pd.DataFrame:
    """(ts_ms, close_u, vol_qu) → 규약 준수 1분봉."""
    return pd.DataFrame(
        [{"symbol": "LAB", "ts_ms": t, "open_u": c, "high_u": c, "low_u": c,
          "close_u": c, "vol_qu": v} for t, c, v in rows]
    ).astype({"ts_ms": "int64", "open_u": "int64", "high_u": "int64",
              "low_u": "int64", "close_u": "int64", "vol_qu": "int64"})


# --------------------------------------------------------------------------- #
# 세션 판정 (baselines)
# --------------------------------------------------------------------------- #
def test_locate_session_at_the_open_and_the_close() -> None:
    md = _market_day()
    assert B.locate_session(md, REG_OPEN)[0] == "pre"
    assert B.locate_session(md, REG_OPEN + MIN_MS)[0] == "regular"
    assert B.locate_session(md, REG_CLOSE)[0] == "regular"
    assert B.locate_session(md, REG_CLOSE + MIN_MS) is None


def test_curve_locate_minute_zero_is_the_bar_after_the_open() -> None:
    """세션 분 0 은 라벨 `개장 + 1분` 이고, 분 389 는 라벨 = 종료 시각이다."""
    md = _market_day()
    curve = B.minute_of_session_volume_curve(_frame([(REG_OPEN + MIN_MS, 100, 1)]), [md])
    assert B.curve_locate(curve, REG_OPEN, calendar=[md])[:2] == ("pre", 329)
    assert B.curve_locate(curve, REG_OPEN + MIN_MS, calendar=[md])[:2] == ("regular", 0)
    assert B.curve_locate(curve, REG_CLOSE, calendar=[md])[:2] == ("regular", 389)
    assert B.curve_key(curve, REG_CLOSE, calendar=[md]) == ("regular", 390, 389)


def test_session_vwap_keeps_the_closing_auction_bar() -> None:
    """예전 규약은 정규장 **마지막 분을 통째로 버렸다** (docs/36 §3-3)."""
    df = _frame([(REG_OPEN, 100, 10),                # 프리마켓 마지막 분
                 (REG_OPEN + MIN_MS, 200, 10),       # 정규장 첫 분
                 (REG_CLOSE, 300, 10)])              # 정규장 종가 경매
    vw = B.session_vwap_u(df, _market_day().regular)
    assert vw.index.tolist() == [REG_OPEN + MIN_MS, REG_CLOSE]
    assert int(vw.iloc[-1]) == (200 * 10 + 300 * 10) // 20


def test_rvol_series_resets_at_the_bar_after_the_open() -> None:
    md = _market_day()
    df = _frame([(REG_OPEN, 100, 999), (REG_OPEN + MIN_MS, 100, 10),
                 (REG_OPEN + 2 * MIN_MS, 100, 10)])
    curve = B.minute_of_session_volume_curve(df, [md], min_days=1)
    rv = B.rvol_series(df, curve, calendar=[md])
    # 라벨 REG_OPEN 봉은 프리마켓 소속이라 정규장 누적에 들어가면 안 된다
    assert rv.loc[REG_OPEN + MIN_MS] == pytest.approx(1.0)
    assert rv.loc[REG_OPEN + 2 * MIN_MS] == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# 창 (features)
# --------------------------------------------------------------------------- #
def test_minute_volumes_maps_content_slots_not_labels() -> None:
    """`_minute_volumes(pre, A, B)` 는 **내용** [A, B) 다 — 라벨로는 (A, B]."""
    df = _frame([(REG_OPEN, 100, 7),                 # 내용 13:29~13:30 → 창 밖
                 (REG_OPEN + MIN_MS, 100, 11),       # 내용 13:30~13:31 → 슬롯 0
                 (REG_OPEN + 3 * MIN_MS, 100, 13)])  # 내용 13:32~13:33 → 슬롯 2
    got = F._minute_volumes(df, REG_OPEN, REG_OPEN + 3 * MIN_MS)
    assert got == [11.0, 0.0, 13.0], "창이 60초 밀리면 [7, 11, 0] 이 된다"


def test_volume_window_ends_at_the_cutoff_bar_inclusive() -> None:
    """창 `vol_sum_5_qu` 는 컷오프 봉을 포함한 최근 5분 내용이다."""
    rows = [(REG_OPEN + m * MIN_MS, 100, 10) for m in range(1, 8)]
    df = _frame(rows)
    cutoff = REG_OPEN + 7 * MIN_MS
    feats = F.extract_precursor_features(df, pd.DataFrame(), cutoff + MIN_MS,
                                         windows_min=(5,), calendar=[_market_day()],
                                         symbol="LAB")
    assert feats["cutoff_ms"] == float(cutoff)
    assert feats["vol_sum_5_qu"] == 50.0, "컷오프 봉 포함 5봉 × 10"


def test_session_first_print_lead_is_zero_when_the_first_minute_traded() -> None:
    """세션 첫 분부터 체결이 있으면 리드타임은 0 이다 (예전 규약에선 1 이었다)."""
    df = _frame([(REG_OPEN + MIN_MS, 100, 10), (REG_OPEN + 2 * MIN_MS, 100, 10)])
    feats = F.extract_precursor_features(df, pd.DataFrame(),
                                         REG_OPEN + 3 * MIN_MS,
                                         calendar=[_market_day()], symbol="LAB")
    assert feats["session_first_print_lead_min"] == 0.0


def test_assign_market_days_puts_the_open_labelled_bar_outside() -> None:
    spans = F.market_day_spans([_market_day()])
    got = F.assign_market_days([PRE_OPEN, PRE_OPEN + MIN_MS, REG_CLOSE,
                                REG_CLOSE + MIN_MS], spans)
    assert got == [None, "2026-06-01", "2026-06-01", None]


# --------------------------------------------------------------------------- #
# 라벨링 (labeling)
# --------------------------------------------------------------------------- #
def test_close_ref_is_the_closing_auction_bar() -> None:
    """`ret_close` 의 기준가는 정규장 **마지막** 봉(라벨 = 종료 시각)이다."""
    md = _market_day()
    rows = [(REG_OPEN, 100, 10)]                              # 프리마켓 마지막 분
    rows += [(REG_OPEN + m * MIN_MS, 100, 10) for m in range(1, 5)]
    rows += [(REG_OPEN + 5 * MIN_MS, 130, 10)]                # T0 후보 (+30%)
    rows += [(REG_CLOSE - MIN_MS, 120, 10), (REG_CLOSE, 111, 10)]
    reg = L._regular_frame(_frame(rows), md)
    assert reg["ts_ms"].tolist()[0] == REG_OPEN + MIN_MS
    assert reg["ts_ms"].tolist()[-1] == REG_CLOSE
    assert int(reg["close_u"].to_numpy()[-1]) == 111, \
        "예전 규약은 끝에서 두 번째 봉(120)을 종가로 썼다"


def test_t0_min_from_open_is_zero_for_the_first_regular_bar() -> None:
    md = _market_day()
    rows = [(REG_OPEN, 100, 10), (REG_OPEN + MIN_MS, 200, 10)]
    rows += [(REG_OPEN + m * MIN_MS, 200, 10) for m in range(2, 6)]
    ev = L.detect_events(_frame(rows), L.EventParams(), calendar=[md],
                         prev_close_u=100)
    assert len(ev) == 1
    assert int(ev["t0_ms"].iloc[0]) == REG_OPEN + MIN_MS
    assert ev["session"].iloc[0] == "regular"
    assert ev["t0_min_from_open"].iloc[0] == 0.0


def test_next_day_gap_uses_the_first_regular_bar_not_premarket() -> None:
    """예전 규약은 "정규장 시가" 자리에 **프리마켓 마지막 분의 시가**를 넣었다."""
    md = _market_day()
    nxt = UsMarketDay(
        date="2026-06-02", day=None,
        pre=SessionWindow(start_ms=NEXT_OPEN - 5 * MIN_MS, end_ms=NEXT_OPEN),
        regular=SessionWindow(start_ms=NEXT_OPEN, end_ms=NEXT_OPEN + 10 * MIN_MS),
        after=None)
    rows = [(REG_OPEN + MIN_MS, 100, 10)]
    rows += [(REG_OPEN + m * MIN_MS, 100, 10) for m in range(2, 6)]
    rows += [(REG_OPEN + 6 * MIN_MS, 200, 10), (REG_CLOSE, 200, 10)]
    df = _frame(rows)
    # 다음날: 라벨 NEXT_OPEN 봉 = 프리마켓 마지막 분(시가 999), 그 다음이 정규장 첫 봉
    # 다음날 라벨 NEXT_OPEN 봉 = 프리마켓 마지막 분. **시가 999** 가 미끼다 —
    # 예전 규약은 이 봉을 "정규장 첫 봉"으로 보고 999 를 갭 계산에 넣었다.
    nxt_rows = pd.DataFrame([
        {"symbol": "LAB", "ts_ms": NEXT_OPEN, "open_u": 999, "high_u": 999,
         "low_u": 200, "close_u": 200, "vol_qu": 10},
        {"symbol": "LAB", "ts_ms": NEXT_OPEN + MIN_MS, "open_u": 100, "high_u": 200,
         "low_u": 100, "close_u": 200, "vol_qu": 10},
    ]).astype({c: "int64" for c in ("ts_ms", "open_u", "high_u", "low_u", "close_u",
                                    "vol_qu")})
    nd = pd.concat([df, nxt_rows], ignore_index=True)
    # 다일 프레임이므로 전일 종가는 매매일별로 준다 (3차 감사 F-4)
    ev = L.detect_events(nd, L.EventParams(), calendar=[md, nxt],
                         prev_close_u={("LAB", "2026-06-01"): 100})
    first = ev[ev["t0_ms"] < NEXT_OPEN]
    assert len(first) == 1
    assert first["next_day_gap"].iloc[0] == pytest.approx(100 / 200 - 1.0), \
        "정규장 첫 봉의 시가(100)를 써야 한다 — 프리마켓 미끼(999)가 아니다"

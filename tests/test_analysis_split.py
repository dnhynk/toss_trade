"""확정 문언 집행 회귀 테스트 — 소유: W3.

    §7-e  분할일 식별(r = 일봉 수정종가 / 같은 매매일 1분봉 마지막 종가) + 전달 파이프라인
    §2.2  일봉 베이스라인 as-of 앵커 (일봉 입력 룩어헤드)
    §2.7  시총 경계 ±10% 밴드 감도 카운트

각 테스트는 **수정 전 구현에서 실패한다** (stash 증명).
"""
from __future__ import annotations

import math

import dataclasses

import pandas as pd
import pytest

from tests import synth
from tossmon.analysis import baselines as B
from tossmon.analysis import evaluate as E
from tossmon.analysis import features as F
from tossmon.analysis import labeling as L
from tossmon.api.models import UsMarketDay

MIN_MS = B.MIN_MS
MICRO = 1_000_000


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows).astype({"ts_ms": "int64", "open_u": "int64",
                                      "high_u": "int64", "low_u": "int64",
                                      "close_u": "int64", "vol_qu": "int64"})


def _on_day(ev: pd.DataFrame, md: UsMarketDay) -> pd.DataFrame:
    if ev.empty:
        return ev
    wins = synth.session_windows(md)
    lo, hi = wins[0][1].start_ms, wins[-1][1].end_ms
    return ev[(ev["t0_ms"] >= lo) & (ev["t0_ms"] < hi)]


def _day(cal: list[UsMarketDay], date: str) -> UsMarketDay:
    return [md for md in cal if md.date == date][0]


def _meta(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _events(specs: list[tuple[str, int]]) -> pd.DataFrame:
    return pd.DataFrame([{"symbol": s, "t0_ms": t, "kind": "win", "peak_ms": t,
                          "peak_ret": 0.2, "ret_30m": 0.1, "ret_close": 0.0,
                          "session": "regular", "rvol_gated": True}
                         for s, t in specs])


def _bars_at(specs: list[tuple[str, int, int]]) -> pd.DataFrame:
    return _frame([{"symbol": s, "ts_ms": t, "open_u": c, "high_u": c, "low_u": c,
                    "close_u": c, "vol_qu": 1_000_000} for s, t, c in specs])


# =========================================================================== #
# §7-e — 분할일 식별
# =========================================================================== #
#: 일봉 ts 실규약 — 00:00 ET = 정규장 시작 − 570분 (docs/06 §2-2).
#: 3차 감사 T-1: 예전 fixture 는 정규장 시작에 뒀는데, 그 값은 어떤 매매일 구성에서도
#: 세션 스팬 안·앵커 뒤라서 F-1·F-2(b) 가 **원리상 검출 불가능**했다.
ET_MIDNIGHT_OFFSET_MIN = 570


def _daily_ts(md: UsMarketDay) -> int:
    return md.regular.start_ms - ET_MIDNIGHT_OFFSET_MIN * MIN_MS


def _split_fixture(split_on: str | None = "2026-06-04", ratio: int = 10, *,
                   drop_split_daily: bool = False, dayless: bool = False):
    """정상 5매매일 + (선택) 특정 매매일에 역분할.

    1분봉은 **원주가**라 분할일에 가격이 `ratio` 배 점프하고, 일봉은 **수정주가**라 전 구간
    일정하다. 따라서 r = 일봉/1분봉 이 분할일에 1/ratio 로 점프한다 (§7-e 신호).
    일봉 ts 는 **실규약(00:00 ET)** 이다.

    `drop_split_daily` — 분할일 자신의 일봉 1개 결측 (감사 F-2 트리거 (a)).
    `dayless` — 분할일이 day 세션 없는 매매일 (감사 F-2 트리거 (b) / F-1 과 같은 근본 원인).
    """
    cal = synth.make_calendar(5, start="2026-06-01")
    if dayless and split_on is not None:
        cal = [dataclasses.replace(md, day=None) if md.date == split_on else md
               for md in cal]
    rows_1m, rows_1d = [], []
    raw, adj = 1_000_000, 1_000_000
    for md in cal:
        if split_on is not None and md.date == split_on:
            raw = raw * ratio
        for m in range(5):
            rows_1m.append({"symbol": "SP", "ts_ms": md.regular.start_ms + m * MIN_MS,
                            "open_u": raw, "high_u": raw, "low_u": raw,
                            "close_u": raw, "vol_qu": 1_000_000})
        if drop_split_daily and split_on is not None and md.date == split_on:
            continue
        rows_1d.append({"symbol": "SP", "ts_ms": _daily_ts(md), "open_u": adj,
                        "high_u": adj, "low_u": adj, "close_u": adj,
                        "vol_qu": 5_000_000})
    return _frame(rows_1m), _frame(rows_1d), cal


def test_split_ratio_series_is_constant_without_a_split() -> None:
    df_1m, df_1d, cal = _split_fixture(split_on=None)
    r = B.split_ratio_series(df_1m, df_1d, cal)
    assert len(r) == 5
    assert r.nunique() == 1, "분할이 없으면 r 은 상수여야 한다"


def test_detect_split_dates_finds_the_split_day() -> None:
    """r 이 직전 매매일 대비 1/1.5 이하로 급변한 매매일을 분할일로 등록한다."""
    df_1m, df_1d, cal = _split_fixture(split_on="2026-06-04", ratio=10)
    assert B.detect_split_dates(df_1m, df_1d, cal) == {"2026-06-04"}


def test_split_threshold_is_frozen_at_1_5() -> None:
    assert B.SPLIT_RATIO_THRESHOLD == 1.5, "임계는 얼린 값이다 (§7-e)"


def test_dividend_scale_noise_is_not_a_split() -> None:
    """배당 조정 수준(≲1.1배) 변동은 분할로 잡히면 안 된다."""
    cal = synth.make_calendar(4, start="2026-06-01")
    rows_1m, rows_1d = [], []
    for i, md in enumerate(cal):
        raw = 1_000_000
        adj = int(1_000_000 * (1.0 + 0.05 * i))     # r 이 5%씩 표류
        for m in range(3):
            rows_1m.append({"symbol": "SP", "ts_ms": md.regular.start_ms + m * MIN_MS,
                            "open_u": raw, "high_u": raw, "low_u": raw,
                            "close_u": raw, "vol_qu": 1_000_000})
        rows_1d.append({"symbol": "SP", "ts_ms": _daily_ts(md), "open_u": adj,
                        "high_u": adj, "low_u": adj, "close_u": adj, "vol_qu": 5_000_000})
    assert B.detect_split_dates(_frame(rows_1m), _frame(rows_1d), cal) == set()


def test_first_market_day_is_never_a_split_day() -> None:
    """비교할 직전 매매일이 없으므로 첫 매매일은 분할일이 아니다 (경계조건)."""
    df_1m, df_1d, cal = _split_fixture(split_on="2026-06-01", ratio=10)
    assert B.detect_split_dates(df_1m, df_1d, cal) == set()


def test_detect_split_dates_by_symbol() -> None:
    df_1m, df_1d, cal = _split_fixture(split_on="2026-06-04", ratio=10)
    assert B.detect_split_dates_by_symbol(df_1m, df_1d, cal) == {"SP": {"2026-06-04"}}


def test_missing_day_before_the_split_widens_registration() -> None:
    """결측이 분할 **전날**이면 (마지막 관측일, 관측일] 이 넓게 등록된다 (보수적 확대)."""
    df_1m, df_1d, cal = _split_fixture(split_on="2026-06-04", ratio=10)
    df_1d = df_1d[df_1d["ts_ms"] != _daily_ts(_day(cal, "2026-06-03"))]
    rep = B.split_scan_report(df_1m, df_1d, cal)
    assert rep["split_dates"] == {"2026-06-03", "2026-06-04"}
    assert rep["n_r_uncomputable"] == 1
    assert rep["n_widened"] == 1


def test_split_day_own_daily_bar_missing_still_protects_the_split_day() -> None:
    """3차 감사 F-2 트리거 (a) 재현 — 분할일 자신의 일봉이 없어도 그 날이 보호돼야 한다.

    예전 구현은 점프를 **관측한 날**(D+1)만 등록해서, 정작 가짜 갭이 나는 D 는 무방비였다.
    그 결과 `split_dates` 를 성실히 넘겨도 +900% `kind='day'` 이벤트가 카탈로그에 남았다.
    """
    df_1m, df_1d, cal = _split_fixture(split_on="2026-06-04", ratio=10,
                                       drop_split_daily=True)
    rep = B.split_scan_report(df_1m, df_1d, cal)
    assert "2026-06-04" in rep["split_dates"], "진짜 분할일이 등록돼야 한다"
    assert rep["n_r_uncomputable"] == 1
    assert rep["n_widened"] >= 1

    ev = L.detect_events(df_1m, L.EventParams(), calendar=cal,
                         split_dates=rep["split_dates"])
    assert ev[ev["kind"].isin(["day", "both"])].empty, "가짜 갭 이벤트가 남으면 안 된다"


def test_split_day_without_day_session_is_detected() -> None:
    """3차 감사 F-2 트리거 (b) 재현 — day=None 매매일의 00:00 ET 일봉이 버려지면 안 된다."""
    df_1m, df_1d, cal = _split_fixture(split_on="2026-06-04", ratio=10, dayless=True)
    rep = B.split_scan_report(df_1m, df_1d, cal)
    assert rep["split_dates"] == {"2026-06-04"},         "날짜 단위 매핑이면 day 세션이 없어도 r 이 계산된다"
    assert rep["n_r_uncomputable"] == 0
    ev = L.detect_events(df_1m, L.EventParams(), calendar=cal,
                         split_dates=rep["split_dates"])
    assert ev[ev["kind"].isin(["day", "both"])].empty


def test_split_scan_report_counts_uncomputable_days() -> None:
    """§7-e 보고 의무 — r 미계산 (심볼, 매매일) 건수를 노출한다."""
    df_1m, df_1d, cal = _split_fixture(split_on=None)
    drop = {_daily_ts(_day(cal, d)) for d in ("2026-06-02", "2026-06-03")}
    rep = B.split_scan_report(df_1m, df_1d[~df_1d["ts_ms"].isin(drop)], cal)
    assert rep["n_r_uncomputable"] == 2
    assert rep["r_uncomputable_dates"] == ["2026-06-02", "2026-06-03"]
    assert rep["n_observed_days"] == 3
    assert rep["split_dates"] == set()


def test_empty_inputs_yield_no_splits() -> None:
    _df_1m, df_1d, cal = _split_fixture()
    assert B.detect_split_dates(pd.DataFrame(), df_1d, cal) == set()
    assert B.split_ratio_series(pd.DataFrame(), df_1d, cal).empty


# =========================================================================== #
# §7-e — 분할일 전달 파이프라인
# =========================================================================== #
def test_split_day_excluded_from_day_condition() -> None:
    """분할일의 가짜 갭이 당일 조건으로 잡히면 카탈로그가 오염된다 (§7-e/§2.3)."""
    df_1m, df_1d, cal = _split_fixture(split_on="2026-06-04", ratio=10)
    splits = B.detect_split_dates(df_1m, df_1d, cal)
    split_md = _day(cal, "2026-06-04")

    naive = L.detect_events(df_1m, L.EventParams(), calendar=cal)
    assert len(_on_day(naive, split_md)) == 1, \
        "split_dates 를 안 넘기면 가짜 +900% 갭이 이벤트가 된다"
    assert _on_day(naive, split_md).iloc[0]["kind"] in ("day", "both")

    guarded = L.detect_events(df_1m, L.EventParams(), calendar=cal, split_dates=splits)
    assert _on_day(guarded, split_md).empty
    assert guarded.attrs["split_dates_applied"] is True
    assert guarded.attrs["split_excluded"] == 1


def test_split_dates_accepts_per_symbol_mapping() -> None:
    df_1m, df_1d, cal = _split_fixture(split_on="2026-06-04", ratio=10)
    by_sym = B.detect_split_dates_by_symbol(df_1m, df_1d, cal)
    ev = L.detect_events(df_1m, L.EventParams(), calendar=cal, split_dates=by_sym)
    assert _on_day(ev, _day(cal, "2026-06-04")).empty


def test_window_condition_survives_on_a_split_day() -> None:
    """분할일에도 윈도우 조건은 살아 `kind='win'` 이 허용된다 (§7-e 문언)."""
    cal = synth.make_calendar(2, start="2026-06-01")
    rows = []
    for m in range(5):
        rows.append({"symbol": "SP", "ts_ms": cal[0].regular.start_ms + m * MIN_MS,
                     "open_u": 100, "high_u": 100, "low_u": 100, "close_u": 100,
                     "vol_qu": 1_000_000})
    for m in range(10):
        c = 1000 if m < 5 else 1160          # 장중 +16% → 윈도우 조건
        rows.append({"symbol": "SP", "ts_ms": cal[1].regular.start_ms + m * MIN_MS,
                     "open_u": 1000, "high_u": c, "low_u": 1000, "close_u": c,
                     "vol_qu": 1_000_000})
    ev = L.detect_events(_frame(rows), L.EventParams(), calendar=cal,
                         split_dates={"2026-06-02"})
    cur = _on_day(ev, cal[1])
    assert len(cur) == 1
    assert cur.iloc[0]["kind"] == "win", "당일 조건만 빠지고 윈도우 조건은 남는다"


def test_split_dates_not_applied_is_marked_in_report() -> None:
    """split_dates 없이 돌린 결과는 리포트에 표식이 남는다 (주 분석 금지)."""
    df_1m, _df_1d, cal = _split_fixture(split_on="2026-06-04", ratio=10)
    naive = L.detect_events(df_1m, L.EventParams(), calendar=cal)
    assert naive.attrs["split_dates_applied"] is False
    res = E.run_all(naive, pd.DataFrame(), pd.DataFrame(), df_1m)
    assert "(split_dates_not_applied)" in set(res["sample_filter"]["reason"])


def test_split_excluded_counted_with_symbol_day_scope() -> None:
    """분할 제외는 (심볼,매매일) 단위라 이벤트 제외 건수와 합산되면 안 된다."""
    df_1m, df_1d, cal = _split_fixture(split_on="2026-06-04", ratio=10)
    splits = B.detect_split_dates(df_1m, df_1d, cal)
    ev = L.detect_events(df_1m, L.EventParams(), calendar=cal, split_dates=splits)
    meta = _meta([{"symbol": "SP", "security_type": "STOCK", "status": "ACTIVE",
                   "is_common": 1, "shares_outstanding_qu": 10_000_000 * MICRO}])
    _kept, reasons = E.apply_sample_filter(ev, meta, df_1m=df_1m,
                                           split_excluded=ev.attrs["split_excluded"],
                                           split_dates_applied=True)
    row = reasons[reasons["reason"] == "split_excluded"].iloc[0]
    assert row["n"] == 1
    assert row["scope"] == "symbol_day"
    assert "(split_dates_not_applied)" not in set(reasons["reason"])


def test_features_drop_gap_on_split_day() -> None:
    """분할일에는 `gap_from_prev_close` 를 계산하지 않는다 (원주가 가짜 갭 방지)."""
    df_1m, _df_1d, cal = _split_fixture(split_on="2026-06-04", ratio=10)
    t0 = _day(cal, "2026-06-04").regular.start_ms + 4 * MIN_MS
    kw = {"calendar": cal, "symbol": "SP", "prev_close_u": 1_000_000}
    plain = F.extract_precursor_features(df_1m, pd.DataFrame(), t0, **kw)
    assert not math.isnan(plain["gap_from_prev_close"])
    guarded = F.extract_precursor_features(df_1m, pd.DataFrame(), t0,
                                           split_dates={"2026-06-04"}, **kw)
    assert math.isnan(guarded["gap_from_prev_close"])


# =========================================================================== #
# §2.2 — 일봉 베이스라인 as-of 앵커
# =========================================================================== #
def _daily_frame(n: int, start_vol: int = 1_000_000):
    cal = synth.make_calendar(n, start="2026-06-01")
    rows = [{"symbol": "D", "ts_ms": _daily_ts(md), "open_u": 1_000_000,
             "high_u": 1_100_000, "low_u": 900_000, "close_u": 1_000_000,
             "vol_qu": start_vol * (i + 1)} for i, md in enumerate(cal)]
    return _frame(rows), cal


def test_daily_baseline_as_of_excludes_future_bars() -> None:
    """앵커가 있으면 D 이후 일봉이 섞여 있어도 결과가 같아야 한다 (§2.2)."""
    df, cal = _daily_frame(30)
    d = cal[20].date
    full = B.compute_daily_baseline(df, as_of_date=d, calendar=cal)
    past = df[df["ts_ms"] < _daily_ts(cal[20])]
    truncated = B.compute_daily_baseline(past, as_of_date=d, calendar=cal)
    assert full == truncated, "미래 일봉이 결과를 바꾸면 룩어헤드다"
    assert full["as_of_applied"] is True and full["as_of_mode"] == "date"
    assert full["n_days"] == 20


def test_daily_baseline_future_corruption_has_no_effect() -> None:
    """D 이후 일봉을 극단 변조해도 앵커가 있으면 흔들리지 않는다."""
    df, cal = _daily_frame(30)
    d = cal[20].date
    poisoned = df.copy()
    fut = poisoned["ts_ms"] >= _daily_ts(cal[20])
    poisoned.loc[fut, "vol_qu"] = poisoned.loc[fut, "vol_qu"] * 1000
    poisoned.loc[fut, "high_u"] = poisoned.loc[fut, "high_u"] * 50
    poisoned.loc[fut, "close_u"] = poisoned.loc[fut, "close_u"] * 50
    assert (B.compute_daily_baseline(df, as_of_date=d, calendar=cal)
            == B.compute_daily_baseline(poisoned, as_of_date=d, calendar=cal))


def test_eval_day_own_daily_bar_never_enters_even_without_day_session() -> None:
    """3차 감사 F-1 재현(repro1) — day 세션이 없는 매매일에서도 평가일 자기 일봉은 배제된다.

    구현이 ms 부등호였을 때, day=None 매매일의 앵커(= pre 시작 04:00 ET)가 일봉 ts
    (00:00 ET)보다 **뒤**라서 평가일 자신의 종가·거래량이 베이스라인에 들어갔다.
    이벤트일은 정의상 폭등일이라 자기 거래량이 분모에 섞이면 z 가 구조적으로 눌린다.
    """
    for drop_day_session in (False, True):
        cal = synth.make_calendar(21, start="2026-06-01")
        ev = cal[-1]
        if drop_day_session:
            ev = dataclasses.replace(ev, day=None)
            cal = cal[:-1] + [ev]
        df, _c = _daily_frame(21)
        poisoned = df.copy()
        last = poisoned["ts_ms"] == _daily_ts(ev)
        poisoned.loc[last, "vol_qu"] = 1_000_000_000
        poisoned.loc[last, "close_u"] = 50_000_000
        poisoned.loc[last, "high_u"] = 50_000_000

        clean = B.prereg_daily_baseline(df, ev.date, cal)
        dirty = B.prereg_daily_baseline(poisoned, ev.date, cal)
        assert clean == dirty, (
            f"day_session_missing={drop_day_session}: 평가일 자기 일봉이 들어갔다 "
            "(3차 감사 F-1 룩어헤드)")
        assert clean["n_days"] == 20


def test_daily_baseline_ms_only_anchor_is_flagged_unsafe() -> None:
    """캘린더 없이 raw ms 만 주면 실규약 일봉(00:00 ET)을 못 거른다 — 모드로 드러낸다."""
    df, cal = _daily_frame(5)
    b = B.compute_daily_baseline(df, as_of_ms=cal[0].regular.start_ms)
    assert b["as_of_mode"] == "ms_unanchored_by_date"
    assert b["n_days"] == 1, "ms 비교는 평가일 자기 일봉을 통과시킨다(그래서 불안전 표시)"
    safe = B.compute_daily_baseline(df, as_of_date=cal[0].date, calendar=cal)
    assert safe["as_of_mode"] == "date" and safe["n_days"] == 0


def test_daily_baseline_promotes_ms_to_date_when_calendar_given() -> None:
    df, cal = _daily_frame(30)
    by_ms = B.compute_daily_baseline(df, as_of_ms=cal[20].regular.start_ms, calendar=cal)
    by_date = B.compute_daily_baseline(df, as_of_date=cal[20].date, calendar=cal)
    assert by_ms["as_of_mode"] == "date"
    assert by_ms["as_of_date"] == by_date["as_of_date"] == cal[20].date
    # as_of_ms 는 호출자가 준 원본을 그대로 기록하므로 둘이 다를 수 있다 — 통계는 같아야 한다
    drop = {"as_of_ms"}
    assert ({k: v for k, v in by_ms.items() if k not in drop}
            == {k: v for k, v in by_date.items() if k not in drop}), \
        "캘린더가 있으면 raw ms 도 date 로 승격돼 같은 답을 낸다"


def test_prereg_daily_baseline_requires_date_and_calendar() -> None:
    """3차 감사 F-1 권고: (date, calendar) 서명이라 raw ms 오전달이 불가능하다."""
    df, cal = _daily_frame(30)
    b = B.prereg_daily_baseline(df, cal[20].date, cal)
    assert b["as_of_applied"] is True and b["as_of_mode"] == "date"
    assert b["window_days"] == B.DEFAULT_WINDOW_DAYS
    with pytest.raises(TypeError):
        B.prereg_daily_baseline(df)
    with pytest.raises(ValueError):
        B.prereg_daily_baseline(df, cal[20].date, [])


def test_daily_baseline_anchor_before_all_data() -> None:
    df, cal = _daily_frame(5)
    b = B.compute_daily_baseline(df, as_of_date=cal[0].date, calendar=cal)
    assert b["n_days"] == 0 and b["adv20_qu"] is None
    assert b["as_of_applied"] is True


# =========================================================================== #
# §2.7 — 시총 경계 ±10% 밴드
# =========================================================================== #
def test_mcap_band_counts_both_sides() -> None:
    """밴드 건수를 걸린 쪽/통과한 쪽 **양쪽 모두** 보고한다 (§2.7)."""
    t = 1_780_000_000_000
    specs = {
        "LOW_IN": 2_000_000,      # $10M  — 하단 밴드 안, 통과
        "LOW_OUT": 1_900_000,     # $9.5M — 하단 밴드 안, 제외
        "HIGH_IN": 59_000_000,    # $295M — 상단 밴드 안, 통과
        "HIGH_OUT": 62_000_000,   # $310M — 상단 밴드 안, 제외
        "MIDDLE": 20_000_000,     # $100M — 밴드 밖, 통과
    }
    events = _events([(s, t) for s in specs])
    df_1m = _bars_at([(s, t, 5 * MICRO) for s in specs])
    meta = _meta([{"symbol": s, "security_type": "STOCK", "status": "ACTIVE",
                   "is_common": 1, "shares_outstanding_qu": sh * MICRO}
                  for s, sh in specs.items()])
    _kept, reasons = E.apply_sample_filter(events, meta, df_1m=df_1m)
    by = dict(zip(reasons["reason"], reasons["n"]))
    assert by["mcap_band_low_kept"] == 1
    assert by["mcap_band_low_excluded"] == 1
    assert by["mcap_band_high_kept"] == 1
    assert by["mcap_band_high_excluded"] == 1
    bands = reasons[reasons["reason"].isin(E.MCAP_BAND_ROWS)]
    assert (bands["scope"] == "mcap_band_sensitivity").all(), \
        "밴드는 감도 보고 전용 — 이벤트 제외 사유와 섞이면 안 된다"


def test_mcap_band_does_not_move_the_verdict() -> None:
    """판정은 확정 경계 [$10M, $300M] 로만 — 밴드는 경계를 움직이지 않는다."""
    t = 1_780_000_000_000
    events = _events([("EDGE_LOW", t), ("EDGE_HIGH", t)])
    df_1m = _bars_at([("EDGE_LOW", t, 5 * MICRO), ("EDGE_HIGH", t, 5 * MICRO)])
    meta = _meta([
        {"symbol": "EDGE_LOW", "security_type": "STOCK", "status": "ACTIVE",
         "is_common": 1, "shares_outstanding_qu": 1_900_000 * MICRO},   # $9.5M
        {"symbol": "EDGE_HIGH", "security_type": "STOCK", "status": "ACTIVE",
         "is_common": 1, "shares_outstanding_qu": 62_000_000 * MICRO},  # $310M
    ])
    kept, _reasons = E.apply_sample_filter(events, meta, df_1m=df_1m)
    assert kept.empty, "밴드 안이어도 확정 경계 밖이면 제외다"


# =========================================================================== #
# 권고 수정 — F-3 / F-4 / F-5
# =========================================================================== #
def test_split_dates_without_calendar_raises() -> None:
    """3차 감사 F-3: 조용히 무위가 되고 provenance 만 '적용됨'으로 남는 것을 막는다."""
    df_1m, _df_1d, _cal = _split_fixture(split_on="2026-06-04", ratio=10)
    with pytest.raises(ValueError, match="calendar"):
        L.detect_events(df_1m, L.EventParams(), split_dates={"2026-06-04"})
    with pytest.raises(ValueError, match="calendar"):
        F.extract_precursor_features(df_1m, pd.DataFrame(),
                                     int(df_1m["ts_ms"].iloc[-1]),
                                     split_dates={"2026-06-04"})


def test_scalar_prev_close_on_multiday_frame_raises() -> None:
    """3차 감사 F-4: 스칼라 전일 종가가 모든 매매일에 적용되면 당일 조건이 전부 오염된다."""
    df_1m, _df_1d, cal = _split_fixture(split_on=None)
    with pytest.raises(ValueError, match="F-4"):
        L.detect_events(df_1m, L.EventParams(), calendar=cal, prev_close_u=1_000_000)
    with pytest.raises(ValueError, match="F-4"):
        L.detect_events(df_1m, L.EventParams(), calendar=cal,
                        prev_close_u={"SP": 1_000_000})


def test_per_day_prev_close_mapping_is_accepted() -> None:
    df_1m, _df_1d, cal = _split_fixture(split_on=None)
    ev = L.detect_events(df_1m, L.EventParams(), calendar=cal,
                         prev_close_u={("SP", "2026-06-03"): 1_000_000})
    assert isinstance(ev, pd.DataFrame)


def test_single_day_frame_still_accepts_scalar_prev_close() -> None:
    """하루치 프레임에서는 스칼라가 여전히 유효하다 (F-4 는 다일 프레임만 막는다)."""
    cal = synth.make_calendar(1, start="2026-06-01")
    rows = [{"symbol": "SP", "ts_ms": cal[0].regular.start_ms + m * MIN_MS,
             "open_u": 100, "high_u": 131, "low_u": 100, "close_u": 131,
             "vol_qu": 1_000_000} for m in range(5)]
    ev = L.detect_events(_frame(rows), L.EventParams(), calendar=cal,
                         prev_close_u=100)
    assert len(ev) == 1 and ev.iloc[0]["kind"] in ("day", "both")


def test_half_day_reason_distinguishes_not_run_from_zero() -> None:
    """3차 감사 F-5: curve 미제공은 '0건'이 아니라 '검사 안 함'이다."""
    t = 1_780_000_000_000
    events = _events([("GOOD", t)])
    df_1m = _bars_at([("GOOD", t, 5 * MICRO)])
    meta = _meta([{"symbol": "GOOD", "security_type": "STOCK", "status": "ACTIVE",
                   "is_common": 1, "shares_outstanding_qu": 10_000_000 * MICRO}])
    _kept, reasons = E.apply_sample_filter(events, meta, df_1m=df_1m, curve=None)
    row = reasons[reasons["reason"] == "half_day_length_sample"].iloc[0]
    assert math.isnan(row["n"]), "검사하지 않았으면 0 이 아니라 NaN 이다"
    prov = reasons[reasons["reason"] == "(half_day_check_not_run)"]
    assert len(prov) == 1 and prov.iloc[0]["scope"] == "provenance"


def test_r_uncomputable_is_reported_in_sample_filter() -> None:
    """§7-e 보고 의무 — r 미계산 건수가 사유 표에 (심볼,매매일) scope 로 실린다."""
    t = 1_780_000_000_000
    events = _events([("GOOD", t)])
    df_1m = _bars_at([("GOOD", t, 5 * MICRO)])
    meta = _meta([{"symbol": "GOOD", "security_type": "STOCK", "status": "ACTIVE",
                   "is_common": 1, "shares_outstanding_qu": 10_000_000 * MICRO}])
    _kept, reasons = E.apply_sample_filter(events, meta, df_1m=df_1m, r_uncomputable=7)
    row = reasons[reasons["reason"] == "r_uncomputable"].iloc[0]
    assert row["n"] == 7 and row["scope"] == "symbol_day"

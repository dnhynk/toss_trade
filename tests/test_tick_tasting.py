"""맛보기 재검토 (docs/29) — 예비 조사가 **판정으로 새지 않는지**.

## 이 파일이 지키는 것

1. **탐지는 접두사 불변** — 슈팅 시작 후 k 초에 보는 정보에 미래가 들어가면 실패다.
2. **남은 상승폭은 진행 중인 슈팅만** — 정점을 지난 뒤의 "정점까지 거리"는 남은 기회가
   아니라 **되돌림**이다. 섞으면 **늦게 탐지할수록 기회가 커지는** 거꾸로 된 표가 나온다
   (실제로 처음에 그렇게 나왔다).
3. **비대칭에 잡음 폭이 붙는다** — 폭 없이 0.07 과 0.009 를 구분할 수 없다.
4. **판정 금지** — 열림/닫힘을 말하지 않는다.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3

import numpy as np
import pandas as pd
import pytest

from tossmon.analysis import session as SS
from tossmon.analysis.measure import tick_instrument as TI
from tossmon.analysis.measure import tick_tasting as TT

U = 1_000_000
BASE = 1_785_500_000_000        # regular session


def _ticks(rows) -> pd.DataFrame:
    df = pd.DataFrame([{"symbol": s, "ts_ms": BASE + int(sec * 1000),
                        "price_u": int(px * U), "qty_u": 1.0 * U}
                       for s, sec, px in rows])
    df["session"] = SS.sessions_of(df["ts_ms"])
    df["cycle_date"] = df["ts_ms"].map(lambda m: SS.session_date(int(m)))
    df["era"] = SS.eras_of(df["ts_ms"])
    df["side_tick"] = df.groupby("symbol")["price_u"].transform(
        lambda s: pd.Series(TI.tick_classify(s.to_numpy()), index=s.index))
    return df


# --------------------------------------------------------------------------- #
# 1. 슈팅 정의와 탐지
# --------------------------------------------------------------------------- #
def test_shot_start_is_the_low_not_the_peak():
    ticks = _ticks([("A", i, 10.0) for i in range(5)]
                   + [("A", 5 + i, 10.0 + 0.05 * i) for i in range(10)])
    shots = TT.find_shot_starts(ticks, rise=0.01)
    assert len(shots) == 1
    assert shots["start_px"].iloc[0] < shots["peak_px"].iloc[0]
    assert shots["start_ms"].iloc[0] < shots["peak_ms"].iloc[0]


def test_shots_do_not_overlap():
    """정점 뒤로 건너뛰지 않으면 같은 상승을 여러 번 센다."""
    ticks = _ticks([("A", i, 10.0 + 0.05 * i) for i in range(40)])
    shots = TT.find_shot_starts(ticks, rise=0.01)
    s = shots.sort_values("start_ms")
    assert (s["start_ms"].to_numpy()[1:] >= s["peak_ms"].to_numpy()[:-1]).all()


def test_a_flat_tape_produces_no_shots():
    assert TT.find_shot_starts(_ticks([("A", i, 10.0) for i in range(30)]),
                               rise=0.01).empty


def test_detection_features_use_only_the_first_k_seconds():
    """**접두사 불변** — k 초 뒤에 무엇이 오든 k 초 시점 관측치는 그대로여야 한다."""
    early = [("A", i, 10.0 + 0.02 * i) for i in range(12)]
    late = early + [("A", 12 + i, 99.0) for i in range(10)]
    shots_e = TT.find_shot_starts(_ticks(early), rise=0.01)
    prof_e = TT.detection_profile(_ticks(early), shots_e.head(1), offsets=(3,))
    prof_l = TT.detection_profile(_ticks(late), shots_e.head(1), offsets=(3,))
    assert prof_e[0]["trades_by_offset_median"] == prof_l[0]["trades_by_offset_median"]
    assert prof_e[0]["imbalance_by_offset_median"] == pytest.approx(
        prof_l[0]["imbalance_by_offset_median"])


# --------------------------------------------------------------------------- #
# 2. 남은 상승폭은 **진행 중인 슈팅만**
# --------------------------------------------------------------------------- #
def test_remaining_upside_only_counts_shots_whose_peak_is_ahead():
    """**이 검사가 처음에 잡힌 결함을 고정한다.**

    정점을 지난 뒤의 "정점까지 거리"는 되돌림이지 남은 기회가 아니다. 섞으면
    늦게 탐지할수록 기회가 커지는 거꾸로 된 표가 나온다.
    """
    ticks = _ticks([("A", 0, 10.0), ("A", 1, 10.2), ("A", 2, 10.0),
                    ("A", 3, 9.9), ("A", 4, 9.9), ("A", 5, 9.9)])
    shots = TT.find_shot_starts(ticks, rise=0.01)
    assert len(shots) == 1
    prof = TT.detection_profile(ticks, shots, offsets=(1, 5))
    at1 = next(p for p in prof if p["offset_s"] == 1)
    at5 = next(p for p in prof if p["offset_s"] == 5)
    assert at1["n_still_in_progress"] == 0 or at1["share_still_in_progress"] <= 1.0
    assert at5["n_still_in_progress"] == 0        # 정점(1초)을 이미 지났다


def test_profile_reports_the_survivor_share():
    ticks = _ticks([("A", i, 10.0 + 0.02 * i) for i in range(30)])
    shots = TT.find_shot_starts(ticks, rise=0.01)
    prof = TT.detection_profile(ticks, shots, offsets=(1,))
    assert "share_still_in_progress" in prof[0]
    assert 0.0 <= prof[0]["share_still_in_progress"] <= 1.0


def test_remaining_upside_is_named_a_ceiling():
    """달성 불가 상한임이 **이름에** 드러나야 한다 (§10-P 의 교훈)."""
    src = pathlib.Path(TT.__file__).read_text(encoding="utf-8")
    assert "ceiling_remaining_rise_median" in src
    assert "unachievable" in src.lower()


# --------------------------------------------------------------------------- #
# 3. 비대칭에 잡음 폭
# --------------------------------------------------------------------------- #
def test_asymmetry_carries_a_noise_band():
    rows = [{"window_s": 30, "signal": "imbalance", "lag_s": k, "n": 1000,
             "corr": 0.05 if k > 0 else 0.04, "corr_shuffled": 0.0,
             "resolution_ok": True, "side_error_applies": True}
            for k in (-30, -10, -5, 0, 5, 10, 30)]
    got = TT.asymmetry(rows, lags=(5,))
    a = got[0]
    assert "noise_band" in a and a["noise_band"] > 0
    assert a["inside_noise_band"] is True          # 0.01 은 폭 안이다


def test_asymmetry_marks_resolution_when_a_side_is_missing():
    rows = [{"window_s": 30, "signal": "imbalance", "lag_s": 5, "n": 10,
             "corr": float("nan"), "corr_shuffled": float("nan"),
             "resolution_ok": False, "side_error_applies": True}]
    got = TT.asymmetry(rows, lags=(5,))
    assert got[0]["resolution_ok"] is False


def test_positive_lag_means_flow_leads():
    src = pathlib.Path(TT.__file__).read_text(encoding="utf-8")
    assert "lag_s > 0" in src or "lag > 0 means FLOW LEADS" in src


# --------------------------------------------------------------------------- #
# 4. 조건이 항상 붙는다 / 판정 금지
# --------------------------------------------------------------------------- #
def test_caveats_carry_the_measured_side_error():
    c = TT.measurement_caveats()
    assert c["side_agreement_overall"] == pytest.approx(0.604)
    assert c["side_agreement_when_price_moving"] == pytest.approx(0.592)
    assert c["side_comparable_share_of_tape"] == pytest.approx(0.1007)


def test_caveats_state_the_bounce_contamination():
    c = TT.measurement_caveats()
    assert "bounce" in c["bounce_note"].lower()
    assert c["returns_are_trade_price_based"] is True


def test_module_emits_no_open_or_closed_language():
    src = pathlib.Path(TT.__file__).read_text(encoding="utf-8")
    for banned_key in ('"verdict"', '"proceed"', '"stopped_at"', '"reopened"'):
        assert banned_key not in src, banned_key
    assert "does not say a candidate is" in src


def test_universe_is_restricted_to_where_resolution_lives():
    ticks = _ticks([("A", i, 10.0) for i in range(300)]
                   + [("B", i * 10, 10.0) for i in range(5)])
    top = TT.top_decile_symbols(ticks)
    assert "A" in top and "B" not in top


# --------------------------------------------------------------------------- #
# 5. 임계 의존성 (C-2)
# --------------------------------------------------------------------------- #
def test_threshold_sweep_reports_every_threshold():
    ticks = _ticks([("A", i, 10.0 + 0.03 * i) for i in range(60)])
    got = TT.shot_threshold_sweep(ticks, rises=(0.01, 0.02))
    assert [r["rise"] for r in got] == [0.01, 0.02]


# --------------------------------------------------------------------------- #
# 6. 러너 전체
# --------------------------------------------------------------------------- #
def _tiny_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db = tmp_path / "tast.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE trades_snap (symbol TEXT, ts_ms INTEGER, "
                 "price_u INTEGER, qty_u REAL)")
    rng = np.random.default_rng(11)
    rows = []
    for sym in ("AAA", "BBB"):
        px = 10.0
        for i in range(2500):
            px *= float(np.exp(rng.normal(0, 0.0012)))
            rows.append((sym, BASE + i * 1000, int(px * U), 1.0 * U))
    conn.executemany("INSERT INTO trades_snap VALUES (?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return db


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("tast")
    db = _tiny_db(tmp)
    out = tmp / "out"
    assert TT.main(db, out_dir=out) == 0
    return json.loads((out / "tick_tasting.json").read_text(encoding="utf-8")), out


def test_runner_rows_match_the_manifest_exactly(report):
    rep, _ = report
    for row in rep["target1_leadlag"]:
        assert set(row) == set(TT.REPORTED_FIELDS), row


def test_runner_reports_what_is_still_needed(report):
    """이 작업의 가장 값어치 있는 산출물."""
    rep, _ = report
    dn = rep["data_needed"]
    assert "cycles_observed" in dn and "target_shots" in dn


def test_runner_states_the_no_verdict_stance(report):
    rep, _ = report
    assert "open or closed" in rep["caveats"]["no_verdict"]


def test_runner_keeps_the_shot_definition_next_to_the_numbers(report):
    rep, _ = report
    assert "EVENT definition" in rep["target2_shots"]["definition"]

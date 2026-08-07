"""파급범위 목록과 편향 측정 (docs/42) — **목록이 코드에서 떨어져 썩지 않게.**

## 이 파일이 지키는 것

1. **목록의 모든 지점이 실재한다.** 함수 이름이 바뀌면 여기서 먼저 깨진다.
2. **`다`(판정 불가)를 `가`(안전)에 넣지 않는다.** 등급은 셋뿐이고 전부 세어진다.
3. **편향 측정이 방향을 맞게 낸다.** 저장 순서는 매수 쪽으로, 창 수익률은 위쪽으로.
4. **대조군이 실제로 중립이다.** 초 안을 섞으면 매수/매도가 반반, 수익률 평균이 0 근처.
"""
from __future__ import annotations

import importlib
import sqlite3

import numpy as np
import pytest

from tossmon.analysis.measure import intra_second_bias as B
from tossmon.analysis.measure import tick_resolution as TR

U = 1_000_000
BASE = TR.WINDOW_START_MS + 40 * 3600 * 1000


def _db(tmp_path, trades):
    p = tmp_path / "t.db"
    conn = sqlite3.connect(p)
    conn.executescript("""
        CREATE TABLE trades_snap (symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL,
            price_u INTEGER NOT NULL, qty_u INTEGER NOT NULL,
            PRIMARY KEY (symbol, ts_ms, price_u, qty_u)) WITHOUT ROWID;
    """)
    conn.executemany("INSERT OR IGNORE INTO trades_snap VALUES (?,?,?,?)", trades)
    conn.commit()
    conn.close()
    return p


# --------------------------------------------------------------------------- #
# 1. 목록이 실재를 가리키는가
# --------------------------------------------------------------------------- #
def test_every_listed_point_actually_exists():
    missing = []
    for row in B.INVENTORY:
        if row["module"] is None:
            continue
        mod = importlib.import_module(row["module"])
        if not hasattr(mod, row["attr"]):
            missing.append(f"{row['module']}.{row['attr']}")
    assert missing == []


def test_undecidable_points_have_no_code_location():
    """`다` 는 코드의 결함이 아니라 **데이터에 없는 정보**다. 함수를 가리키면 안 된다."""
    for row in B.INVENTORY:
        if row["klass"] == B.K_UNKNOWN:
            assert row["module"] is None and row["attr"] is None


def test_only_three_classes_and_all_are_counted():
    classes = {r["klass"] for r in B.INVENTORY}
    assert classes <= {B.K_SAFE, B.K_DIRTY, B.K_UNKNOWN}
    counts = B.inventory_counts()
    assert sum(counts.values()) == len(B.INVENTORY)
    assert counts[B.K_UNKNOWN] > 0                  # 못 재는 것을 0으로 적지 않는다


def test_every_point_says_why():
    for row in B.INVENTORY:
        assert row["why"].strip()
        assert row["output"].strip()


def test_shot_derived_outputs_are_not_filed_as_safe():
    """슈팅 경계를 입력으로 받는 것은 전부 오염을 물려받는다."""
    for row in B.INVENTORY:
        if row["attr"] in ("detection_profile", "shot_threshold_sweep",
                           "data_needed", "find_shot_starts"):
            assert row["klass"] == B.K_DIRTY, row


# --------------------------------------------------------------------------- #
# 2. M1 — 틱 규칙의 크기와 방향
# --------------------------------------------------------------------------- #
def test_tick_rule_bias_direction_is_toward_buy(tmp_path):
    """한 초에 여러 건이 있으면 저장 순서는 전부 매수로, 섞으면 반반이 되어야 한다."""
    rows = []
    for sec in range(400):
        for px in (1.00, 1.02, 1.01, 1.03):
            rows.append(("AAA", BASE + sec * 1000, int(px * U), 1))
    p = _db(tmp_path, rows)
    got = B.tick_rule_bias(TR.open_ro(p), min_trades=100)
    assert got["stored_order"]["buy"] > 0.7
    # 남은 매도는 전부 **초 경계**에서 나온다 — 초 안이 오름차순이라 다음 초 첫 행에서
    # 한 번 떨어진다(톱니). 4건짜리 초라면 정확히 1/4 이다.
    assert got["stored_order"]["sell"] == pytest.approx(0.25, abs=0.01)
    assert got["shuffled_control"]["sell"] > 0.3    # 대조군은 중립에 가깝다
    assert got["buy_share_excess"] > 0.25
    assert got["rows_classified_differently_share"] > 0.25


def test_tick_rule_bias_vanishes_when_each_second_holds_one_trade(tmp_path):
    """초마다 한 건이면 섞을 것이 없다 — 편향이 0 이어야 한다."""
    rows = [("AAA", BASE + s * 1000, int((1.0 + (s % 5) / 100) * U), 1)
            for s in range(600)]
    p = _db(tmp_path, rows)
    got = B.tick_rule_bias(TR.open_ro(p), min_trades=100)
    assert got["rows_classified_differently_share"] == 0.0
    assert got["buy_share_excess"] == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# 3. M2 — 창 수익률의 크기와 방향
# --------------------------------------------------------------------------- #
def test_window_return_bias_is_positive_on_a_flat_market(tmp_path):
    """가격이 **전혀 추세가 없어도** 저장 순서는 양의 수익률을 만들어낸다."""
    rows = []
    for sec in range(600):                          # 매 초 같은 세 가격, 추세 0
        for px in (1.00, 1.01, 1.02):
            rows.append(("AAA", BASE + sec * 1000, int(px * U), 1))
    p = _db(tmp_path, rows)
    got = B.window_return_bias(TR.open_ro(p), windows_s=(10,), min_trades=100)
    w = got["by_window"]["10s"]
    assert w["stored_ret_bp"]["mean"] > 100          # 위로 밀린다
    assert abs(w["shuffled_ret_bp"]["mean"]) < w["stored_ret_bp"]["mean"] / 2
    assert w["mean_excess_bp"] > 0


def test_window_return_bias_shrinks_as_the_window_grows(tmp_path):
    """창이 길수록 첫·끝 초의 비중이 줄어 편향이 옅어져야 한다."""
    rows = []
    for sec in range(1200):
        for px in (1.00, 1.01, 1.02):
            rows.append(("AAA", BASE + sec * 1000, int(px * U), 1))
    p = _db(tmp_path, rows)
    got = B.window_return_bias(TR.open_ro(p), windows_s=(5, 60), min_trades=100)
    assert (got["by_window"]["5s"]["mean_excess_bp"]
            > got["by_window"]["60s"]["mean_excess_bp"])


# --------------------------------------------------------------------------- #
# 4. 판정 금지
# --------------------------------------------------------------------------- #
def test_report_says_it_is_not_a_reverdict(tmp_path):
    rows = [("AAA", BASE + s * 1000, int(1.0 * U), 1) for s in range(600)]
    p = _db(tmp_path, rows)
    rep = B.build_report(TR.open_ro(p))
    assert "다시 계산하지 않았다" in rep["not_a_verdict"]
    assert set(rep["inventory_counts"]) == {B.K_SAFE, B.K_DIRTY, B.K_UNKNOWN}

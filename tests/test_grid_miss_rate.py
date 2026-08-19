"""`docs/35` §5-1 의 미수신율 정의를 못박는다 — 그리고 계기의 결함 하나를.

이 파일이 지키는 것 셋:

1. **정의**: 분모는 `Σ(델타/10)`(부호 포함), 분자는 `Σ(델타/10 − 1)`(양의 델타만).
   프리마켓 원문 숫자(34 슬롯 · 10 미수신 · 29.4%)를 **그대로 재현**해야 한다.
   재현이 깨지면 우리는 원문과 **다른 것**을 재고 있는 것이다.
2. **첫 전이 누락**: `_analyze_arm` 이 첫 전이의 델타를 빠뜨리던 결함의 회귀 검사.
3. **별칭**: 같은 응답열을 솎아도 미수신율이 안 움직인다는 것 — 이것이
   *"29% 는 우리 폴 주기 탓이 아니다"* 의 근거다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import tools.grid_miss_rate as g
import tools.live_probe as lp

ROOT = Path(__file__).resolve().parents[1]
PROBE_JSON = ROOT / "coordination" / "daily" / "2026-08-18_probe_d_output.json"


# ============================================================ 1. 정의


def test_definition_reproduces_the_premarket_numbers_docs35_printed():
    """`docs/35` §5-1 의 34 / 10 / 29.4% 가 이 코드에서 그대로 나와야 한다."""
    deltas = [k for k, n in g.PREMARKET_HIST_DOCS35.items() for _ in range(n)]
    r = g.miss_rate(deltas)
    assert r["deltas"] == 26, "원문은 델타 26 개라고 적었다"
    assert r["slots"] == 34, "원문의 분모 34 (15×1 + 10×2 + 1×(−1))"
    assert r["missed"] == 10, "원문의 분자 10 (+20s 가 각각 한 슬롯을 건너뛴다)"
    assert round(r["rate"] * 100, 1) == 29.4


def test_premarket_wilson_matches_the_interval_docs62_quotes():
    """`docs/62` §2-1 이 인용한 Wilson [16.8%, 46.2%] 와 같아야 한다."""
    deltas = [k for k, n in g.PREMARKET_HIST_DOCS35.items() for _ in range(n)]
    r = g.miss_rate(deltas)
    assert round(r["lo"] * 100, 1) == 16.8
    assert round(r["hi"] * 100, 1) == 46.2


def test_negative_delta_counts_in_the_denominator_but_not_the_numerator():
    """역행 델타는 원문이 분모에서 −1 로만 셌다. 분자에 넣으면 정의가 달라진다."""
    assert g.miss_rate([-10.0])["slots"] == -1
    assert g.miss_rate([-10.0])["missed"] == 0
    assert g.miss_rate([10.0, 10.0])["missed"] == 0, "+10 은 건너뛴 슬롯이 없다"
    assert g.miss_rate([20.0])["missed"] == 1, "+20 은 한 슬롯"
    assert g.miss_rate([30.0])["missed"] == 2, "+30 은 두 슬롯"


# ============================================================ 2. 첫 전이 누락


def _grid_polls(stamps: list[str]) -> list[dict]:
    return [{"i": i, "sent_epoch": 1000.0 + i, "rankedAt": s, "order": s,
             "volume": s, "all": f"a{i}", "last": "L", "rate": "R"}
            for i, s in enumerate(stamps)]


def test_first_transition_delta_is_counted():
    """★ 회귀 검사. 예전 `_analyze_arm` 은 **첫 전이**의 델타를 버렸다.

    아래 열은 전이가 둘(+20, +10)이다. 하나만 세면 그 결함이 돌아온 것이다.
    """
    stamps = (["2026-08-18T22:30:49.000+09:00"] * 3
              + ["2026-08-18T22:31:09.000+09:00"] * 3
              + ["2026-08-18T22:31:19.000+09:00"] * 3)
    deltas = g.deltas_from_polls(_grid_polls(stamps))
    assert deltas == [20.0, 10.0], "첫 전이(+20)가 빠지면 미수신을 과소보고한다"


def test_live_probe_analyzer_now_agrees_with_the_recount():
    """계기(`_analyze_arm`)와 이 도구가 **같은 델타 수**를 내야 한다."""
    stamps = (["2026-08-18T22:30:49.000+09:00"] * 3
              + ["2026-08-18T22:31:09.000+09:00"] * 3
              + ["2026-08-18T22:31:19.000+09:00"] * 3)
    polls = _grid_polls(stamps)
    hist = lp._analyze_arm(polls)["rankedAt"]["server_stamp_delta_hist"]
    assert hist == {10.0: 1, 20.0: 1}
    assert sum(hist.values()) == len(g.deltas_from_polls(polls))


def test_analyzer_delta_count_equals_change_count():
    """델타 수는 전이 수와 **같아야** 한다. `changes − 1` 이면 결함이 돌아온 것이다."""
    stamps = []
    for i in range(6):
        stamps += [f"2026-08-18T22:3{i}:09.000+09:00"] * 2
    rec = lp._analyze_arm(_grid_polls(stamps))["rankedAt"]
    assert sum(rec["server_stamp_delta_hist"].values()) == rec["changes"]


# ============================================================ 3. 실측 자료


@pytest.mark.skipif(not PROBE_JSON.exists(), reason="프로브 산출물이 없다")
class TestArchivedProbe:
    """2026-08-18 정규장 프로브. **다시 만들 수 없는 자료라 값을 못박는다.**"""

    @staticmethod
    def _arm1s():
        doc = json.loads(PROBE_JSON.read_text(encoding="utf-8"))
        return doc["ranking_cadence"]["volume_realtime"]["arms"]["1s"]

    def test_one_second_arm_recount(self):
        """1 초 팔에서 다시 세면 6/18 = 33.3% 다 (저장된 히스토그램은 5/16)."""
        r = g.miss_rate(g.deltas_from_polls(self._arm1s()["raw_polls"]))
        assert r["deltas"] == 12
        assert (r["missed"], r["slots"]) == (6, 18)
        assert round(r["rate"] * 100, 1) == 33.3

    def test_stored_histogram_was_short_by_exactly_one_delta(self):
        """저장된 값은 **수정 전** 코드가 낸 것이다 — 그 사실을 기록으로 남긴다."""
        arm = self._arm1s()
        stored = arm["rankedAt"]["server_stamp_delta_hist"]
        assert sum(stored.values()) == arm["rankedAt"]["changes"] - 1
        assert sum(stored.values()) + 1 == len(g.deltas_from_polls(arm["raw_polls"]))

    def test_only_four_of_the_six_grid_positions_were_used(self):
        """★ `docs/62` §6-3 이 미리 적어 둔 분기점. 여섯 자리가 아니면 분모가 바뀐다."""
        occ = g.seconds_occupancy(self._arm1s()["raw_polls"])
        assert set(occ["seconds"]) == {"09", "19", "39", "49"}
        assert "29" not in occ["seconds"] and "59" not in occ["seconds"]
        assert set(occ["millis"]) == {".000"}, "격자는 여전히 정각 10 초다"

    @pytest.mark.parametrize("stride", [1, 2, 4, 5, 8])
    def test_decimation_does_not_move_the_rate(self, stride):
        """★ 같은 응답열을 굵은 자로 다시 읽어도 미수신율이 안 움직인다.

        이것이 *"29% 는 우리 폴 주기 탓이 아니다"* 의 직접 근거다. 폴 주기가
        원인이라면 자를 굵게 할수록 값이 올라야 한다.
        """
        polls = self._arm1s()["raw_polls"]
        r = g.miss_rate(g.deltas_from_polls(polls[::stride]))
        assert (r["missed"], r["slots"]) == (6, 18)

    def test_skips_are_at_fixed_positions_not_independent(self):
        """+30 이 한 번도 안 나온다 — 독립적으로 빠진다면 나와야 한다."""
        polls = self._arm1s()["raw_polls"]
        gg = g.geometric_expectation(g.deltas_from_polls(polls))
        assert gg["observed"][30.0] == 0
        assert gg["expected_if_independent"][30.0] > 0
        assert gg["observed"][10.0] == gg["observed"][20.0], "고정 자리면 반반이다"

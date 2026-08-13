"""정규장 재측정(`--cadence-profile open`)의 **예산과 표본**을 못박는다 — docs/62.

이 파일이 지키는 것 셋:

1. **`planned_calls` 가 실제 콜 수보다 작으면 안 된다.** 그 필드는 상한 여유를 읽는
   자리라 과소계상이 곧 "여유가 있다" 는 거짓말이 된다. 예전 `+6` 이 팔 밖 콜을
   3 건 빠뜨리고 있었다 (단발 4 + 겹침 3 + `get_stocks` 배치 2 = 9).
2. **솎기 stride 에서 옛 값을 빼면 안 된다.** 프리마켓 표(`docs/35` §5-4)가 그 stride
   위에서 만들어졌다 — 빼면 세션 비교의 한쪽이 사라진다. `5` 는 더한 것이다.
3. **표본 수는 폴 간격이 아니라 지속 시간이 정한다.** 격자 슬롯 = `dur / grid` 라
   간격을 반으로 줄이면 콜만 두 배가 되고 슬롯은 그대로다. 이 사실을 모르고
   "1 초 팔이니 촘촘하다" 로 읽은 것이 `docs/35` 29.4%(n=34) 의 함정이었다.

라이브 호출 0 — 전부 산술과 스텁이다.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import tools.cadence_power as cp
import tools.live_probe as lp

ROOT = Path(__file__).resolve().parents[1]


# ============================================================ 1. 예산 산술


def test_open_profile_planned_calls_counts_the_calls_outside_the_arms():
    """`planned_calls` 는 팔 밖 콜(단발·겹침·`get_stocks`)을 빠뜨리면 안 된다."""
    op = lp.CADENCE_PROFILES["open"]
    arms = (sum(int(d / g) for _l, g, d in op["vol_arms"])
            + sum(int(d / g) for _l, g, d in op["gain_arms"]))
    assert arms == 445, "open 팔 구성이 바뀌었다 — docs/62 의 예산표를 다시 계산하라"
    assert lp.CADENCE_FIXED_CALLS >= 9, (
        "단발 4 + 겹침 3 + get_stocks 2 = 9 보다 작게 잡으면 상한 여유를 과대보고한다")
    assert arms + lp.CADENCE_FIXED_CALLS <= op["call_cap"], "상한을 넘는 계획이다"


def test_open_profile_leaves_headroom_under_the_pre_registered_cap():
    """상한은 **사전 고정** 안전값이다 (docs/35 §4). 계획이 그것을 먹어치우면 안 된다."""
    op = lp.CADENCE_PROFILES["open"]
    planned = (sum(int(d / g) for _l, g, d in op["vol_arms"])
               + sum(int(d / g) for _l, g, d in op["gain_arms"])
               + lp.CADENCE_FIXED_CALLS)
    assert planned == 454
    assert op["call_cap"] - planned >= 90, "중단조건이 걸릴 여지가 없을 만큼 빡빡하다"


# ============================================================ 2. 솎기 stride


def test_decimation_strides_keep_the_premarket_set_and_add_five():
    """옛 stride 를 빼면 프리마켓 표와 대조가 끊긴다. `5` 는 **더한** 것이어야 한다."""
    strides = set(lp.CADENCE["strides"])
    assert {1, 2, 4, 8, 12} <= strides, "docs/35 §5-4 표의 stride 가 사라졌다"
    assert 5 in strides, "폴 주기 후보 5 초를 같은 응답열에서 못 읽는다 (docs/62)"


def test_decimation_costs_no_extra_calls():
    """솎기는 **같은 응답열을 다시 읽는 것**이라 콜을 더 쓰지 않는다.

    이것이 5 초 팔을 콜 0 으로 얻는 근거다 — 별도 팔은 60 콜이고 시간대도 다르다.
    """
    # 서버 10 초 격자를 그대로 흉내낸다 — `rankedAt` 은 10 초마다 한 번만 움직인다.
    def stamp(slot: int) -> str:
        return f"2026-08-14T00:00:{slot * 10 % 60:02d}.000+09:00"

    polls = [{"i": i, "sent_epoch": 1000.0 + i, "rankedAt": stamp(i // 10),
              "order": f"o{i // 10}", "volume": f"v{i // 10}", "all": f"a{i}",
              "last": "L", "rate": "R", "n": 100}
             for i in range(60)]
    full = lp._analyze_arm(polls, stride=1)
    five = lp._analyze_arm(polls, stride=5)
    assert full["polls"] == 60
    assert five["polls"] == 12, "stride 5 는 60 폴을 12 로 솎아야 한다"
    # 같은 입력을 다시 읽었을 뿐 — 새 요청이 나갈 자리가 없다.
    assert five["stride"] == 5


# ============================================================ 3. 표본 = 지속시간


@pytest.mark.parametrize("gap_s", [1.0, 5.0, 12.0])
def test_grid_slots_depend_on_duration_not_on_poll_gap(gap_s):
    """★ 간격을 줄여도 슬롯은 안 는다. 이걸 놓치면 콜만 태우고 CI 는 그대로다."""
    assert cp.slots_for(300.0) == 30
    assert cp.slots_for(1200.0) == 120
    # 같은 지속시간이면 간격이 달라도 슬롯 수는 같다.
    assert cp.slots_for(600.0) == 60


def test_wilson_interval_is_wide_at_the_sample_size_we_actually_have():
    """`docs/35` 의 29.4% 는 n=34 다. 정밀한 숫자로 인용되면 안 된다."""
    p, lo, hi = cp.wilson(cp.OBSERVED["misses"], cp.OBSERVED["slots"])
    assert p == pytest.approx(10 / 34, rel=1e-6)
    assert lo < 0.20 and hi > 0.44, "구간이 이보다 좁으면 계산이 바뀐 것이다"
    assert (hi - lo) / 2 > 0.10, "반폭이 10pp 를 넘는다는 것이 이 문서의 요점이다"


def test_wilson_stays_inside_zero_one_where_wald_would_not():
    """가장자리에서 Wald 는 구간을 [0,1] 밖으로 내보낸다. 그래서 Wilson 을 쓴다."""
    for k, n in ((0, 30), (30, 30), (1, 30)):
        _, lo, hi = cp.wilson(k, n)
        assert 0.0 <= lo <= hi <= 1.0


def test_open_profile_as_written_cannot_separate_the_two_components():
    """★ 이 측정이 존재하는 이유는 **분해**인데, 지금 팔 구성으로는 못 한다.

    두 팔 다 300 초 = 30 슬롯이고, 30 vs 30 에서 갈리는 최소 격차가 20pp 를 넘는다.
    """
    n = cp.slots_for(300.0)
    assert n == 30
    _, half, ok = cp.can_separate(0.294, n, 0.294 + 0.15, n)
    assert not ok, "15pp 격차가 갈린다면 계산이 바뀐 것이다"
    assert half > 0.20, "반폭이 20pp 아래면 이 문서의 결론을 다시 계산하라"


def test_longer_arms_are_what_narrow_the_interval():
    """지속시간을 늘리면 좁아진다 — 간격이 아니라."""
    short = cp.half_width(0.294, cp.slots_for(300.0))
    long_ = cp.half_width(0.294, cp.slots_for(1200.0))
    assert long_ < short
    assert cp.slots_needed(0.294, 10.0) > cp.slots_needed(0.294, 16.0)


# ============================================================ 4. 문서 대조


def test_docs62_quotes_the_same_call_total_as_the_code():
    """문서의 숫자가 코드에서 나오게 못박는다 — 둘이 갈라지면 문서가 조용히 거짓이 된다."""
    doc = (ROOT / "docs" / "62_open_cadence_plan.md").read_text(encoding="utf-8")
    op = lp.CADENCE_PROFILES["open"]
    planned = (sum(int(d / g) for _l, g, d in op["vol_arms"])
               + sum(int(d / g) for _l, g, d in op["gain_arms"])
               + lp.CADENCE_FIXED_CALLS)
    assert str(planned) in doc, f"docs/62 가 계획 콜 수 {planned} 를 안 적었다"
    assert str(op["call_cap"]) in doc, "docs/62 가 상한을 안 적었다"


def test_cadence_power_runs_and_reports_zero_live_calls():
    """계획 숫자는 재현 가능해야 한다 — 애드혹 계산이면 다음 사람이 못 검산한다."""
    assert cp.main([]) == 0

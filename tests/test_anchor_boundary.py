"""매매일 앵커 경계 계측기 (`docs/51` §2, `docs/48` §11-7 요구 #1).

## 이 파일이 지키는 것

1. **기전이 실제로 그렇게 돈다.** 매매일 앵커가 같으면 `strict_lt` 창은 `obs_le` 창의
   접두이고 첫 교차가 같은 봉에서 잡힌다. 앵커가 하루 밀리면 창이 통째로 옮겨가
   **리드 값까지** 달라진다. 둘 다 `_first_bar_vol_z_cross` 로 직접 보인다.
2. **따름정리는 두 주장이고 계측기가 둘 다 센다** — 검출 항등과 리드 값 동일.
   리드 값만 어긋난 건이 `detect_identity_broken` 에 숨지 않아야 한다.
3. **반증 관측이 실제로 반증한다.** 앵커가 같은데 깨진 건이 있으면
   `same_anchor_n > 0` · `all_anchor_changed = False` 로 드러나야 한다. 이 두 칸이
   0/True 로 고정되면 가설은 반증 불가능해지고 그러면 관측이 아니다.
4. **대칭차다.** 한 방향(`strict` 검출인데 `obs` 리드 ≥ 1 아님)만 세면 반대 방향
   어긋남을 놓친다.
5. **표본이 `cutoff_tautology` 와 같은 60건이라는 것은 구성으로 보장된다** — 캘린더·
   심볼 선정·적재를 다시 쓰지 않고 그 모듈에서 가져온다.
6. **판정 금지.** 이 모듈에는 후보를 살리거나 죽이는 상수가 없다.
"""
from __future__ import annotations

import math

import pandas as pd

from tossmon.analysis import features as F
from tossmon.analysis.measure import anchor_boundary as AB
from tossmon.analysis.measure import cutoff_tautology as CT

MIN = 60_000
NAN = float("nan")
#: 평탄한 기저는 sd = 0 이라 z 가 정의되지 않는다 — 실제 봉처럼 흔들리는 값을 쓴다.
NORMAL = (80, 100, 120, 140, 160)


def frame(day_lo: int, n: int, surges: dict[int, int]) -> pd.DataFrame:
    """슬롯 i = `[day_lo−240분 + i·60초, …)` 를 담은 봉들. 라벨은 슬롯 끝이다 (§6.1)."""
    lo = day_lo - 240 * MIN
    vols = [NORMAL[i % len(NORMAL)] for i in range(n)]
    for i, v in surges.items():
        vols[i] = v
    return pd.DataFrame({"ts_ms": [lo + (i + 1) * MIN for i in range(n)],
                         "vol_qu": vols})


# --------------------------------------------------------------------------- #
# 1. 기전 — 앵커가 같으면 접두, 다르면 창이 통째로 옮겨간다
# --------------------------------------------------------------------------- #
def test_same_day_anchor_makes_strict_a_prefix_so_first_cross_is_identical():
    """앵커가 같으면 `strict_lt` 는 `obs_le` 의 접두 — 첫 교차는 **같은 봉**이다."""
    day_lo = 10_000 * MIN
    df = frame(day_lo, 245, {242: 5000})                 # 스캔 3번째 분에서 서지
    hit_obs = F._first_bar_vol_z_cross(df, day_lo, day_lo + 5 * MIN, 3.0)
    hit_strict = F._first_bar_vol_z_cross(df, day_lo, day_lo + 4 * MIN, 3.0)
    assert hit_obs == hit_strict == day_lo + 3 * MIN


def test_same_day_anchor_new_detection_can_only_be_lead_zero():
    """꼬리에서만 잡히면 그 봉이 T0 봉이다 — 리드 0. (§4-0 의 구조 주장)"""
    day_lo = 10_000 * MIN
    df = frame(day_lo, 245, {244: 9000})                 # 마지막 분에만 서지
    t0 = day_lo + 5 * MIN
    assert F._first_bar_vol_z_cross(df, day_lo, t0, 3.0) == t0          # obs: 리드 0
    assert F._first_bar_vol_z_cross(df, day_lo, t0 - MIN, 3.0) is None  # strict: 미검출


def test_day_anchor_shift_breaks_the_corollary_in_one_direction():
    """앵커가 갈리면 접두 관계가 깨진다 — `strict` 는 500분 리드, `obs` 는 리드 0."""
    day_b = 10_000 * MIN                                  # 둘째 매매일 시작
    day_a = day_b - 600 * MIN                             # 첫째 매매일 시작
    df = frame(day_a, 841, {340: 8000, 840: 7000})        # 첫째 날 안 + T0 봉
    t0 = day_b + MIN                                      # 둘째 날 **첫 봉** = T0

    # obs_le: 앵커 = 둘째 날. 스캔 구간이 `(day_b, t0]` 한 분뿐이다.
    hit_obs = F._first_bar_vol_z_cross(df, day_b, t0, 3.0)
    # strict_lt: 컷오프가 첫째 날 마지막 봉 → 앵커 = 첫째 날. 그날의 서지를 잡는다.
    hit_strict = F._first_bar_vol_z_cross(df, day_a, day_b, 3.0)

    assert (t0 - hit_obs) // MIN == 0                     # obs 는 리드 0 밖에 낼 수 없다
    assert (t0 - hit_strict) // MIN == 500                # strict 는 500분 전을 가리킨다
    # ★ 따라서 {obs 리드 ≥ 1} 에는 없고 {strict 검출} 에는 있다 = 복원 실패, 한 방향


# --------------------------------------------------------------------------- #
# 2. 집계 — 따름정리의 두 주장을 둘 다 세는가
# --------------------------------------------------------------------------- #
def row(t0, obs, strict, *, day=False, sess=False, warm=False, gap=1):
    return {"t0_ms": t0, "vol_surge_obs": obs, "vol_surge_strict": strict,
            "mode_changed_day": day, "mode_changed_session": sess,
            "strict_warmup_short": warm, "cutoff_gap_min": gap}


def block(rows):
    return AB.recovery_block(rows, "vol_surge_obs", "vol_surge_strict",
                             "mode_changed_day")


def test_corollary_holds_when_only_lead_zero_is_added():
    """리드 0 이 새로 생기는 것은 따름정리가 **예측하는** 일이다 — 깨짐이 아니다."""
    b = block([row(1, 5.0, 5.0), row(2, 0.0, NAN), row(3, 12.0, 12.0)])
    assert b["detect_obs_le"] == 3 and b["detect_strict_lt"] == 2
    assert b["lead_zero_obs_le"] == 1 and b["lead_ge1_obs_le"] == 2
    assert b["corollary_holds"] is True
    assert b["detect_identity_broken"]["n"] == 0
    assert b["lead_value_broken"]["n"] == 0
    assert b["lead_equal_n"] == 2


def test_lead_value_mismatch_is_counted_even_when_detection_agrees():
    """검출은 맞는데 **리드가 다른** 건 — 검출 항등만 세면 통째로 놓친다."""
    rows = [row(1, 5.0, 5.0), row(2, 9.0, 300.0, day=True)]
    b = block(rows)
    assert b["detect_identity_broken"]["n"] == 0          # 검출은 양쪽 다 있다
    assert b["lead_value_broken"]["n"] == 1               # 그러나 값이 다르다
    assert b["lead_value_broken"]["t0_ms"] == [2]
    assert b["corollary_holds"] is False                  # ★ 따름정리는 깨졌다
    assert b["lead_equal_n"] == 1


def test_detect_identity_is_a_symmetric_difference():
    """양방향을 다 센다 — `strict` 만 검출된 건도, `obs` 만 리드 ≥ 1 인 건도."""
    rows = [row(1, NAN, 7.0), row(2, 4.0, NAN)]
    b = block(rows)
    assert b["detect_identity_broken"]["n"] == 2
    assert sorted(b["detect_identity_broken"]["t0_ms"]) == [1, 2]


# --------------------------------------------------------------------------- #
# 3. 반증 관측이 실제로 반증하는가
# --------------------------------------------------------------------------- #
def test_falsifier_fires_when_a_break_has_the_same_anchor():
    """앵커가 같은데 깨진 건이 있으면 앵커 가설은 **틀렸다** — 그것이 보여야 한다."""
    rows = [row(1, 9.0, NAN, day=True), row(2, 4.0, NAN, day=False)]
    d = block(rows)["detect_identity_broken"]
    assert d["n"] == 2
    assert d["same_anchor_n"] == 1
    assert d["all_anchor_changed"] is False


def test_falsifier_is_silent_when_every_break_changed_anchor():
    """전부 앵커가 바뀐 건이면 가설은 지지된다 (반증되지 않았다는 뜻일 뿐이다)."""
    d = block([row(1, 9.0, NAN, day=True), row(2, NAN, 3.0, day=True)])[
        "detect_identity_broken"]
    assert d["n"] == 2 and d["same_anchor_n"] == 0 and d["all_anchor_changed"] is True


def test_alternative_explanations_travel_with_every_break():
    """깨진 건마다 대안 설명 칸이 함께 나온다 — 앵커만으로 설명했는지 보이려면 필요하다."""
    d = block([row(1, 9.0, NAN, day=True, sess=True, warm=True, gap=7)])[
        "detect_identity_broken"]
    assert d["also_changed_session_n"] == 1
    assert d["warmup_short_n"] == 1
    assert d["gap_gt_1min_n"] == 1


def test_no_breaks_leaves_all_anchor_changed_undecided_not_true():
    """깨진 건이 0 이면 `all(...)` 은 공허하게 참이다 — None 으로 둔다."""
    d = block([row(1, 5.0, 5.0)])["detect_identity_broken"]
    assert d["n"] == 0 and d["all_anchor_changed"] is None


# --------------------------------------------------------------------------- #
# 4. 표본 동일성 · 판정 금지
# --------------------------------------------------------------------------- #
def test_sample_construction_is_borrowed_not_rewritten():
    """캘린더·심볼 선정·적재·상한을 다시 쓰지 않는다 — 같은 60건이 구성으로 보장된다."""
    for name in ("calendar_from_daily", "pick_symbols", "load_bars", "ro"):
        assert getattr(AB, name) is getattr(CT, name)
    assert AB.TRAIN_END_MS == CT.TRAIN_END_MS == 1_767_225_600_000


def full_row(t0, obs, strict, **kw):
    """`summarise` 가 읽는 칸을 다 갖춘 행."""
    r = {**row(t0, obs, strict), "cutoff_obs": t0,
         "day_lo_obs_from_calendar": True, "day_lo_strict_from_calendar": True,
         "t0_bar_crosses_z3": False,
         **{f"cross_obs_{t:g}": 1.0 for t in F.RVOL_CROSS_THRESHOLDS},
         **{f"cross_strict_{t:g}": 1.0 for t in F.RVOL_CROSS_THRESHOLDS}}
    r.update(kw)
    return r


def test_summary_reports_both_anchors_and_the_fallback_count():
    """세션 앵커와 폴백 건수를 함께 낸다 — 매매일 앵커 결론의 대안 설명이다."""
    s = AB.summarise([full_row(1, 5.0, 5.0, day_lo_strict_from_calendar=False)])
    assert s["events"] == 1
    assert s["mode_changed_day_n"] == 0
    assert s["mode_changed_session_n"] == 0
    assert s["day_lo_fallback_n"] == 1
    assert set(s["rvol_first_cross"]) == {"thr_2", "thr_3", "thr_5"}


def test_summary_separates_the_two_explanations_of_zero_lead_zero():
    """리드 0 이 안 생겼을 때 (가) T0 봉이 안 넘었다 / (나) 창이 T0 를 안 봤다 를 가른다."""
    s = AB.summarise([full_row(1, 5.0, 5.0, t0_bar_crosses_z3=True),
                      full_row(2, 7.0, 7.0, cutoff_obs=2 - MIN)])
    assert s["t0_bar_crosses_z3_n"] == 1        # (가) 를 세는 칸
    assert s["cutoff_obs_is_t0_n"] == 1         # (나) 를 세는 칸 — 60/60 이어야 정상이다


def test_module_declares_no_verdict_thresholds():
    """판정 상수가 없어야 한다 — 이 계측기는 §4 판정을 다시 내리지 않는다."""
    src = (AB.__file__ or "")
    assert src.endswith("anchor_boundary.py")
    banned = ("rvol_min", "ret_min", "day_ret_min", "CI", "reject", "adopt")
    names = [n for n in dir(AB) if n.isupper()]
    assert not [n for n in names if any(b.lower() in n.lower() for b in banned)]


def test_nan_helper_matches_the_measure_convention():
    assert AB._fin(1.0) is True
    assert AB._fin(NAN) is False
    assert AB._fin(math.inf) is True          # 무한대는 결측이 아니다

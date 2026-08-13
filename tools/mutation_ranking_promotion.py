"""랭킹 차선(D-21) 변이 게이트 — 심어놓은 결함이 전부 죽어야 통과한다.

## 왜 이 파일이 생겼나

`docs/61` 의 첫 판은 테스트 30 건을 달고 왔고 그중 7 건이 *"기본값 꺼짐이라 라이브가
안 바뀐다"* 를 증명한다고 적었다. 코디네이터가 규율대로 **직접 깨봤더니**
(`COORDINATOR-STATE` §5-3) 둘이 안 죽었다:

    A  `fill_to_capacity` 가 `reserve` 를 통째로 무시하게 만들었다   -> 30 passed
    B  차선이 `compete=True` 로 **축출하게** 만들었다                 -> 30 passed

둘 다 같은 모양이었다 — **픽스처가 보호 대상 동작을 도달 불가능하게 만들어 단언이
실패할 수 없었다.** A 는 tier2 에 채울 후보가 없어 `reserve` 가 결과를 못 갈랐고,
B 는 점유자 점수가 0.90 이라 `compete` 가 어느 쪽이든 못 밀어냈다.

B 쪽이 더 위험했다: 2026-08-04 에 수집이 무너진 자리가 정확히 축출이고
(`STRATEGY-VERDICTS` §4.4-E), docstring 이 *"`compete=False` 가 그것을 막는다"* 고
적어 둔 그 문장을 지키는 테스트가 **없었다.**

그래서 "테스트가 몇 건인가" 를 근거로 병합하지 않는다. **이 게이트가 근거다.**

## 무엇을 심나

라이브 배포 판단이 걸린 성질만 심는다. 세 부류다:

    F1~F6  **꺼짐 불변** — 플래그가 꺼진 채로 머지돼도 라이브가 안 바뀐다
    L1~L8  **차선의 계약** — 축출 금지 / 좌석 상한 / 반납 / 쿨다운 / 컷
    C1~C4  **설정** — 반쯤 켜진 상태가 조용히 꺼진 것처럼 보이지 않는다

규율·검증 다섯 가지(앵커 1회, 바이트 변화, 디스크 일치, assert 로만 탐지 인정, 복원
검증)는 `tools/mutation_accounting.py` 것을 그대로 쓴다. 이 파일은 **변이 목록과 대상
스위트만** 다르다.

## 실행법

    python tools/mutation_ranking_promotion.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.mutation_accounting import GateError, Mutation, run  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LOOPS = ROOT / "tossmon" / "collector" / "loops.py"
DETECTOR = ROOT / "tossmon" / "collector" / "detector.py"
CONFIG = ROOT / "tossmon" / "config.py"

#: 차선 방어가 들어 있는 파일만 돌린다 — 게이트를 빠르게 유지한다.
#: `test_collector_loops` 를 같이 도는 이유: 차선이 `rankings_once` 안에 배선돼 있어서
#: 배선을 끊는 변이는 그쪽 스위트가 먼저 잡을 수도 있다. 어느 쪽이 잡든 탐지다.
SUITES = ("tests/test_ranking_promotion.py", "tests/test_collector_loops.py",
          "tests/test_collector_config.py")

MUTATIONS: tuple[Mutation, ...] = (
    # ---- F: 꺼짐 불변 (이 PR 이 머지돼도 라이브가 안 바뀐다) -------------------
    Mutation(
        "F1", CONFIG,
        "꺼져 있어도 `config_sig` 에 꼬리를 붙인다 — 데이터에 **없는 경계**를 만든다",
        '        if not self.enabled:\n            return ""',
        '        if False:\n            return ""',
        ("test_config_signature_is_byte_identical_while_the_flag_is_off",)),
    Mutation(
        "F2", CONFIG,
        "켜져 있는데 지문을 **안** 바꾼다 — 경계가 데이터에 안 남는다 (D-8 의 실패)",
        '        if not self.enabled:\n            return ""',
        '        if True:\n            return ""',
        ("test_config_signature_changes_the_moment_it_is_turned_on",)),
    Mutation(
        "F3", CONFIG,
        "절이 없을 때 **켜진** 기본값을 준다 (기본값 꺼짐이 거짓이 된다)",
        "    top_n: int = 0",
        "    top_n: int = 10",
        ("test_the_section_is_absent_by_default_and_that_means_off",)),
    Mutation(
        "F4", LOOPS,
        "꺼져 있어도 차선을 돌린다 — 머지만으로 라이브 동작이 바뀐다",
        "    if not rp.enabled or page.ranking_type not in rp.types:\n        return",
        "    if False:\n        return",
        ("test_the_lane_does_nothing_at_all_while_the_flag_is_off",)),
    Mutation(
        "F5", LOOPS,
        "꺼져 있어도 정원 한 칸을 비워 둔다 — tier3 가 매일 한 칸씩 논다",
        "                reserve=ctx.cfg.ranking_promotion.tier3_slots)",
        "                reserve=ctx.cfg.ranking_promotion.tier3_slots + 1)",
        ("test_capacity_fill_reserves_nothing_while_the_flag_is_off",)),
    Mutation(
        "F6", LOOPS,
        "꺼진 채 기동했는데 옛 상태파일의 좌석을 되살린다 (아무도 안 놓아 준다)",
        "        if self.cfg.ranking_promotion.enabled:",
        "        if True:",
        ("test_a_stale_state_file_cannot_resurrect_seats_while_the_flag_is_off",)),

    # ---- L: 차선의 계약 -------------------------------------------------------
    Mutation(
        "L1", LOOPS,
        "★ 차선이 **축출한다** — 2026-08-04 에 수집이 무너진 바로 그 자리",
        "                           compete=False, record_score=0.0) is None:",
        "                           compete=True, record_score=0.0) is None:",
        ("test_the_lane_never_evicts_an_existing_tier3_member",)),
    Mutation(
        "L2", DETECTOR,
        "★ `fill_to_capacity` 가 `reserve` 를 무시한다 — 차선이 영원히 굶는다",
        "        free = cap - len(self.members(tier)) - max(0, int(reserve))",
        "        free = cap - len(self.members(tier))",
        ("test_the_reserve_is_what_makes_a_seat_exist_at_all",)),
    Mutation(
        "L3", LOOPS,
        "좌석 상한을 안 지킨다 — 예산 산식(좌석당 0.583 req/s)이 무너진다",
        "        if len(ctx.ranking_seats_ms) >= rp.tier3_slots:",
        "        if False:",
        ("test_the_lane_can_never_hold_more_than_its_slots",
         "test_a_ranked_symbol_reaches_tier3_not_tier2")),
    Mutation(
        "L4", LOOPS,
        "좌석을 **안 놓아 준다** — 전이 0 은 안정화가 아니라 동결이다 (§4.4-E)",
        "    for sym in [s for s, until in ctx.ranking_seats_ms.items() if until <= snap_ms]:",
        "    for sym in []:",
        ("test_rotate_releases_the_seat_after_hold_s_even_if_still_ranked",
         "test_the_lane_keeps_promoting_instead_of_freezing")),
    Mutation(
        "L5", LOOPS,
        "`rotate` 를 `sticky` 처럼 매 스냅 연장한다 (폭이 사라진다)",
        '            if rp.policy == "sticky":',
        "            if True:",
        ("test_rotate_releases_the_seat_after_hold_s_even_if_still_ranked",)),
    Mutation(
        "L6", LOOPS,
        "재진입 쿨다운을 무시한다 — 같은 종목이 좌석을 주고받는다 (진동)",
        "        if freed is not None and snap_ms - freed < rp.cooldown_s * 1000:",
        "        if False:",
        ("test_a_released_symbol_cannot_sit_down_again_inside_the_cooldown",)),
    Mutation(
        "L7", LOOPS,
        "`top_n` 컷을 없앤다 — 상위 100 위 전부가 승격 후보가 된다",
        "        if row.rank > rp.top_n:",
        "        if False:",
        ("test_top_n_is_the_cut",)),
    Mutation(
        "L8", LOOPS,
        "유니버스 게이트를 우회한다 (감사 F-2) — 메가캡이 tier3 를 먹는다",
        "        if sym not in ctx.watchlist:\n            continue\n        if sym in ctx.ranking_seats_ms:",
        "        if False:\n            continue\n        if sym in ctx.ranking_seats_ms:",
        ("test_an_unwatchable_symbol_is_never_promoted",)),
    Mutation(
        "L9", LOOPS,
        "설정에 없는 랭킹 타입도 차선에 들인다",
        "    if not rp.enabled or page.ranking_type not in rp.types:\n        return",
        "    if not rp.enabled:\n        return",
        ("test_only_the_configured_ranking_types_feed_the_lane",)),
    Mutation(
        "L10", LOOPS,
        "가드가 이미 내려놓은 종목을 좌석 만료가 **한 칸 더** 내린다 (tier2 자리까지)",
        "        if ctx.tiers.tier_of(sym) < 3:\n            continue",
        "        if False:\n            continue",
        ("test_a_seat_that_the_budget_guard_already_took_is_not_charged_twice",)),
    Mutation(
        "L11", LOOPS,
        "좌석을 상태파일에 **안 저장한다** — 재시작마다 고아가 tier3 를 쥔다",
        '            "ranking_seats_ms": dict(self.ranking_seats_ms),',
        '            "ranking_seats_ms": {},',
        ("test_seats_survive_a_restart_so_no_orphan_holds_a_tier3_slot",)),
    Mutation(
        "L12", LOOPS,
        "차선 좌석 수를 텔레메트리에 **안 싣는다** (배포 후 동결을 못 읽는다)",
        '            "rk_t3_seats": len(self.ranking_seats_ms),',
        '            "rk_t3_seats": 0,',
        ("test_telemetry_carries_the_lane_fields_and_they_read_zero_when_off",
         "test_a_stale_state_file_cannot_resurrect_seats_while_the_flag_is_off")),

    # ---- C: 설정 --------------------------------------------------------------
    Mutation(
        "C1", CONFIG,
        "반쯤 켜진 설정을 **조용히 꺼진 것처럼** 통과시킨다",
        "    if given and not cfg.enabled:",
        "    if False:",
        ("test_a_half_configured_section_is_a_startup_error",)),
    Mutation(
        "C2", CONFIG,
        "`policy` 오타를 기동 시점에 안 잡는다 (조용히 rotate 로 돈다)",
        "    if policy not in RankingPromotionConfig.POLICIES:",
        "    if False:",
        ("test_bad_values_fail_at_startup_not_silently",)),
    Mutation(
        "C3", CONFIG,
        "모르는 키를 조용히 무시한다 — 오타 하나가 설정 전체를 무력화한다",
        "    if unknown:",
        "    if False:",
        ("test_bad_values_fail_at_startup_not_silently",)),
    Mutation(
        "C4", CONFIG,
        "`enabled` 를 느슨하게 — 좌석이 0 인데 켜진 것으로 본다",
        "        return bool(self.types) and self.top_n > 0 and self.tier3_slots > 0 \\\n            and self.hold_s > 0",
        "        return bool(self.types) and self.top_n > 0",
        ("test_a_half_configured_section_is_a_startup_error",
         "test_the_section_is_absent_by_default_and_that_means_off")),
)


def main() -> int:
    results = run(mutations=MUTATIONS, suites=SUITES, what="ranking-lane defects")
    survived = [r for r in results if not r["killed"]]
    print(f"\n{len(results) - len(survived)}/{len(results)} mutations killed")
    if survived:
        print("\nSURVIVING DEFECT (the suite does not defend this):")
        for r in survived:
            print(f"  - {r['id']} [{r['target']}]: {r['what']}")
        return 1
    print("all planted ranking-lane defects were caught")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GateError as exc:
        print(f"GATE BROKEN: {exc}")
        sys.exit(2)

"""랭킹 진입을 tier3 승격 사유로 (D-21, docs/61). **기본값 꺼짐**을 먼저 고정한다.

이 파일의 첫 절(A)이 제일 중요하다: **이 PR 이 머지돼도 라이브 동작은 하나도 안 바뀐다.**
배포는 코디네이터가 휴장 창에 따로 하고(D-18), 그 전까지 켜지면 안 된다. "기본값이
꺼져 있다" 는 주장은 말이 아니라 **같은 입력에 같은 출력**으로 증명되어야 한다 —
승격 결정, `config_sig`, 정원 채우기, 텔레메트리 키까지 전부.

두 번째 절(B)부터가 켰을 때의 동작이고, 마지막 절(D)이 2026-08-04 붕괴의 재발 방지다:
`compete=False` 로 축출을 껐더니 tier2 가 닫힌 집합이 되어 tier3 가 10->3 으로
말라죽었다 (`STRATEGY-VERDICTS` §4.4-E). 여기서는 **정반대 방향**으로 같은 함정을
밟을 수 있다 — 좌석을 안 놓아 주면 차선이 그대로 동결된다. 그래서 성공 기준은
**진동 0 그리고 신규 승격 > 0** 이다.
"""
from __future__ import annotations

import pytest

from tests.test_api_support import run
from tests.test_collector_helpers import make_config
from tests.test_collector_loops import DAY0, StubClient, build_ctx
from tossmon.api.models import RankingPage, RankingRow
from tossmon.collector import loops
from tossmon.collector.detector import ACTIVITY_ENTRY_SCORE, EVICTION_MARGIN
from tossmon.config import RankingPromotionConfig, parse_config
from tossmon.store import Store

MIN_MS = 60_000
GAINERS = "TOP_GAINERS"

#: 켠 설정 한 벌. 실제 배포값이 아니라 **테스트가 고르는 값**이다 — 배포값은 코디네이터가
#: 라이브 워크트리에서 정한다 (docs/61 §6).
ON = {"types": [GAINERS], "top_n": 3, "tier3_slots": 2, "hold_s": 300,
      "policy": "rotate", "cooldown_s": 600}


def _page(symbols, rtype=GAINERS, duration="1d"):
    return RankingPage(ranking_type=rtype, duration=duration, ranked_at_ms=None,
                       rows=[RankingRow(rank=i + 1, symbol=s, last_u=1_000_000,
                                        base_u=1_000_000, change_rate=0.1,
                                        vol_qu=10, amount_u=100)
                             for i, s in enumerate(symbols)])


def _ctx(tmp_path, symbols, **sections):
    client = StubClient({})
    ctx, _day = build_ctx(tmp_path, client, symbols=symbols, **sections)
    for s in symbols:
        ctx.universe_status[s] = True          # tier0 게이트는 이 파일의 시험 대상이 아니다
        if s not in ctx.watchlist:
            ctx.watchlist.append(s)
    return ctx


def _raise_to(ctx, symbol, tier, score, ts_ms):
    """스코어 경로로 `tier` 까지 올린다 — `on_new_data` 는 **한 번에 한 칸**이고
    dwell(`promote_hysteresis_s`=120s)을 지키므로 호출을 벌려야 한다."""
    for step in range(tier - 1):
        ctx.tiers.on_new_data(symbol, score, ts_ms + step * 5 * MIN_MS)
    ctx.flush_changes()


# --------------------------------------------------------------------------- #
# A. 기본값 꺼짐 — **이 PR 이 머지돼도 아무것도 안 바뀐다**
# --------------------------------------------------------------------------- #
def test_the_section_is_absent_by_default_and_that_means_off():
    cfg = make_config()
    rp = cfg.ranking_promotion
    assert rp.enabled is False
    assert (rp.types, rp.top_n, rp.tier3_slots, rp.hold_s) == ((), 0, 0, 0)


def test_config_signature_is_byte_identical_while_the_flag_is_off(tmp_path):
    """지문이 흔들리면 데이터에 **없는 경계**가 생긴다.

    `usage_ratio` 가 지문에 없어서 D-8 변경이 데이터에 안 남았던 전례의 반대 실패다:
    이번엔 안 바뀐 것을 바뀐 것처럼 적는 쪽.
    """
    off = _ctx(tmp_path / "a", ("AAA",))
    explicit_off = _ctx(tmp_path / "b", ("AAA",),
                        ranking_promotion={"types": [], "top_n": 0, "tier3_slots": 0,
                                           "hold_s": 0})
    try:
        assert off.config_signature() == explicit_off.config_signature()
        assert "rkp" not in off.config_signature()
    finally:
        off.store.close()
        explicit_off.store.close()


def test_config_signature_changes_the_moment_it_is_turned_on(tmp_path):
    """반대 방향도 고정한다 — 켜는 순간은 **반드시** 경계가 남아야 한다."""
    off = _ctx(tmp_path / "a", ("AAA",))
    on = _ctx(tmp_path / "b", ("AAA",), ranking_promotion=ON)
    try:
        sig_on = on.config_signature()
        assert sig_on != off.config_signature()
        assert sig_on.startswith(off.config_signature())      # 앞부분은 그대로
        assert ",rkpGAIN@3/k2/h300s/rotate/cd600s" in sig_on, sig_on
        assert " " not in sig_on, "k=v 파싱이 깨진다"
    finally:
        off.store.close()
        on.store.close()


def test_the_lane_does_nothing_at_all_while_the_flag_is_off(tmp_path):
    """꺼진 상태에서 랭킹 스냅을 흘려도 tier3 는 **한 칸도** 안 움직인다."""
    ctx = _ctx(tmp_path, ("AAA", "BBB", "CCC"))
    try:
        before = dict(ctx.tiers.states)
        loops._ranking_tier3_lane(ctx, _page(["AAA", "BBB", "CCC"]), DAY0)
        assert ctx.ranking_seats_ms == {}
        assert ctx.tiers.members(3) == []
        assert ctx.counters.get("ranking_tier3_promotions", 0) == 0
        assert {s: st.tier for s, st in ctx.tiers.states.items()} == \
            {s: st.tier for s, st in before.items()}
    finally:
        ctx.store.close()


def _reserve_passed_by_the_tier3_loop(tmp_path, **sections) -> int:
    """`run_tier3_micro` 가 `fill_to_capacity` 에 **실제로 넘기는** `reserve` 를 잡는다.

    테스트가 `fill_to_capacity` 를 직접 부르면 그 인자는 **테스트가 고른 값**이지
    프로덕션이 넘기는 값이 아니다. 그 차이 때문에 호출 자리를 `tier3_slots + 1` 로
    바꿔도 아무도 안 죽었다 (docs/61 §10 의 F5). 그래서 여기서는 배선을 직접 잡는다.
    """
    ctx = _ctx(tmp_path, ("AAA",), **sections)
    seen: list[int] = []
    real = ctx.tiers.fill_to_capacity

    def spy(tier, ts_ms, **kw):
        if tier == 3:
            seen.append(int(kw.get("reserve", 0)))
        return real(tier, ts_ms, **kw)

    ctx.tiers.fill_to_capacity = spy                    # type: ignore[method-assign]
    try:
        run(loops.run_tier3_micro(ctx.client, ctx.store, ctx.cfg, ctx=ctx, cycles=1))
    finally:
        ctx.store.close()
    assert seen, "전제 위반: tier3 루프가 fill_to_capacity 를 아예 안 불렀다"
    return seen[0]


def test_the_tier3_loop_reserves_nothing_while_the_flag_is_off(tmp_path):
    """꺼져 있으면 **프로덕션 호출 자리**가 `reserve=0` 을 넘긴다.

    이 자리가 조용히 1 이라도 되면 tier3 가 매일 한 칸씩 비고, 그건 순손실이다
    (빈 슬롯 = 체결 테이프 0 건, `fill_to_capacity` docstring).
    """
    assert _reserve_passed_by_the_tier3_loop(tmp_path) == 0


def test_the_tier3_loop_reserves_exactly_the_configured_slots_when_on(tmp_path):
    """④ **대조군.** 켜면 같은 자리가 `tier3_slots` 를 넘긴다 — 두 값이 갈려야 배선이다."""
    got = _reserve_passed_by_the_tier3_loop(tmp_path, ranking_promotion=ON)
    assert got == ON["tier3_slots"] == 2, got


def test_telemetry_carries_the_lane_fields_and_they_read_zero_when_off(tmp_path):
    """0 이 나오는 것과 **키가 없는 것**은 다르다 — 없으면 배포 후에도 못 읽는다."""
    ctx = _ctx(tmp_path, ("AAA",))
    try:
        tel = ctx.telemetry()
        assert tel["rk_t3_seats"] == 0
        assert tel["rk_t3_promotions"] == 0
        assert "tier3_cap" in tel, "분모 없는 0 은 뜻이 없다 (docs/52 §7.2)"
    finally:
        ctx.store.close()


def test_telemetry_actually_counts_the_seats_when_the_lane_holds_them(tmp_path):
    """④ **대조군.** 0 만 단언하면 그 필드를 **상수 0 으로 박아도** 안 죽는다.

    실제로 안 죽었다 (docs/61 §10 의 L12). 배포 후 동결을 읽는 유일한 표면이 이것이라
    "0 이 나온다" 가 아니라 **"실제 좌석 수가 나온다"** 를 고정해야 한다.
    """
    ctx = _ctx(tmp_path, ("AAA", "BBB", "CCC"), ranking_promotion=ON)
    try:
        loops._ranking_tier3_lane(ctx, _page(["AAA", "BBB", "CCC"]), DAY0)
        assert len(ctx.ranking_seats_ms) == 2, "전제 위반: 좌석을 안 잡았다"
        tel = ctx.telemetry()
        assert tel["rk_t3_seats"] == 2, tel["rk_t3_seats"]
        assert tel["rk_t3_promotions"] == 2, tel["rk_t3_promotions"]
    finally:
        ctx.store.close()


# --------------------------------------------------------------------------- #
# B. 켰을 때 — 랭킹 진입이 **tier3** 에 닿는다
# --------------------------------------------------------------------------- #
def test_a_ranked_symbol_reaches_tier3_not_tier2(tmp_path):
    """이 태스크의 본체다.

    기존 `_ranking_triggers` 는 tier2 까지만 올린다. 그런데 `trades_snap` 을 쓰는 것은
    tier3 뿐이라(`_poll_trades` 는 `run_tier3_micro` 에서만 불린다) tier2 승격은 테이프
    커버리지를 한 건도 안 늘린다 — 실측으로 13 일 동안 `ranking_entry` 1,011 건이 전부
    `to_tier=2` 였다 (docs/61 §1).
    """
    ctx = _ctx(tmp_path, ("AAA", "BBB", "CCC"), ranking_promotion=ON)
    try:
        loops._ranking_tier3_lane(ctx, _page(["AAA", "BBB", "CCC"]), DAY0)
        assert sorted(ctx.tiers.members(3)) == ["AAA", "BBB"], "좌석 2 개만 쓴다"
        assert ctx.tiers.tier_of("CCC") == 1, "좌석이 없으면 안 올린다"
        assert ctx.counters["ranking_tier3_promotions"] == 2
        assert ctx.counters["ranking_tier3_blocked"] == 1
        assert set(ctx.ranking_seats_ms) == {"AAA", "BBB"}
    finally:
        ctx.store.close()


def _weak_tier3_holder(tmp_path, symbols=("AAA", "BBB", "OLD")):
    """정원 1 을 **약해진 점유자**(`OLD`, 점수 0.10)가 쥐고 있는 상태.

    점수가 `ACTIVITY_ENTRY_SCORE`(0.30) − `EVICTION_MARGIN`(0.08) 아래라야
    **축출이 가능하다.** 그래야 `compete=False` 가 그 축출을 막는 것이 관측된다 —
    점유자가 0.90 이면 `compete` 가 어느 쪽이든 못 밀어내므로 그 플래그를 못 가른다
    (실제로 그 픽스처가 사보타주 B 를 놓쳤다, docs/61 §10).

    0.10 은 `ACTIVITY_ENTRY_SCORE` 의 docstring (a) 가 겨눈 상태 그대로다 —
    *"이미 유지선(0.22) 아래로 떨어진 점유자는 밀어낼 수 있다."*
    """
    ctx = _ctx(tmp_path, symbols, ranking_promotion=ON)
    ctx.tiers.capacity[3] = 1
    _raise_to(ctx, "OLD", 3, 0.90, DAY0)
    assert ctx.tiers.members(3) == ["OLD"], "전제 위반: 점유자가 tier3 에 없다"
    weak = ACTIVITY_ENTRY_SCORE - EVICTION_MARGIN - 0.05
    assert weak > 0.0, "상수가 바뀌었다 — 이 픽스처의 전제를 다시 세워라"
    ctx.tiers.states["OLD"].score = weak        # 봉 데이터로 약해졌다
    return ctx


def test_control_this_fixture_can_actually_evict(tmp_path):
    """④ **대조군이자 반공허 단언.** 이 픽스처에서 축출이 **실제로 일어난다.**

    아래 테스트의 *"안 밀어냈다"* 는 축출이 **가능한** 상태에서만 뜻이 있다.
    이 대조군이 없으면 `compete` 를 무엇으로 두든 초록이고, 그것이 정확히
    2026-08-04 을 막는다는 가드가 비어 있던 방식이다.
    """
    ctx = _weak_tier3_holder(tmp_path)
    try:
        got = ctx.tiers.force("AAA", 3, "x", ACTIVITY_ENTRY_SCORE,
                              DAY0 + 20 * MIN_MS, compete=True, record_score=0.0)
        assert got == 3, "compete=True 인데도 못 들어갔다 — 픽스처가 축출 불가 상태다"
        assert ctx.tiers.tier_of("OLD") == 2, "축출이 안 일어났다"
    finally:
        ctx.store.close()


def test_the_lane_never_evicts_an_existing_tier3_member(tmp_path):
    """축출은 2026-08-04 에 무너진 자리다. 차선은 **빈자리에만** 들어간다.

    위 대조군과 **같은 픽스처**다 — 저기서는 밀어냈고 여기서는 안 밀어낸다.
    갈리는 것은 `compete` 하나뿐이므로 이 단언이 그 플래그를 지킨다.
    """
    ctx = _weak_tier3_holder(tmp_path)
    try:
        loops._ranking_tier3_lane(ctx, _page(["AAA", "BBB"]), DAY0 + 20 * MIN_MS)
        assert ctx.tiers.members(3) == ["OLD"], "기존 멤버를 밀어냈다 (compete=True?)"
        assert ctx.tiers.tier_of("AAA") == 1 and ctx.tiers.tier_of("BBB") == 1
        assert ctx.counters.get("ranking_tier3_promotions", 0) == 0
        assert ctx.counters["ranking_tier3_no_seat"] == 2
    finally:
        ctx.store.close()


#: 정원 5 를 채울 **여유 후보**. 5 명이라야 `reserve` 가 결과를 가른다 —
#: 후보가 정원보다 적으면 예약이 있으나 없으나 같은 수가 차서 단언이 공허해진다.
_FILL_CANDS = ("S1", "S2", "S3", "S4", "S5")


def _filled_with_reserve(tmp_path, reserve: int):
    """**같은 상태**에서 `reserve` 만 바꿔 tier3 정원을 채운다."""
    ctx = _ctx(tmp_path, ("AAA",) + _FILL_CANDS, ranking_promotion=ON)
    ctx.tiers.capacity[3] = 5
    for s in _FILL_CANDS:
        _raise_to(ctx, s, 2, 0.50, DAY0)
    ctx.tiers.fill_to_capacity(3, DAY0 + 5 * MIN_MS, reserve=reserve)
    return ctx


def test_the_reserve_is_what_makes_a_seat_exist_at_all(tmp_path):
    """예약이 없으면 `fill_to_capacity` 가 매 사이클 정원을 꽉 채워 차선이 굶는다.

    **두 실행이 갈려야 가드다.** 같은 상태·같은 후보에서 `reserve` 만 0 과 2 로 두고
    비교한다. 첫 단언이 **반공허 장치**다 — 후보가 모자라 정원이 애초에 안 차면
    `reserve` 를 통째로 무시해도 초록이 되고, 실제로 그렇게 뚫렸다 (docs/61 §10).
    """
    greedy = _filled_with_reserve(tmp_path / "greedy", 0)
    keep = _filled_with_reserve(tmp_path / "keep", 2)
    try:
        assert len(greedy.tiers.members(3)) == 5, (
            "전제 위반: 후보가 모자라 정원이 안 찼다 — 이 상태에서는 reserve 가 "
            "결과를 못 가르므로 아래 단언이 공허하다")
        assert len(keep.tiers.members(3)) == 3, "예약분까지 채웠다 (reserve 무시)"

        # 그리고 그 빈자리가 실제로 차선의 좌석이 된다 — 예약의 존재 이유가 이것이다.
        loops._ranking_tier3_lane(greedy, _page(["AAA"]), DAY0 + 6 * MIN_MS)
        assert greedy.tiers.tier_of("AAA") == 1, "정원이 꽉 찼는데 들어갔다 = 축출한 것이다"
        loops._ranking_tier3_lane(keep, _page(["AAA"]), DAY0 + 6 * MIN_MS)
        assert keep.tiers.tier_of("AAA") == 3, "예약해 둔 자리에 차선이 못 들어갔다"
    finally:
        greedy.store.close()
        keep.store.close()


def test_an_unwatchable_symbol_is_never_promoted(tmp_path):
    """유니버스 게이트(감사 F-2)는 차선에도 그대로 걸린다."""
    ctx = _ctx(tmp_path, ("AAA",), ranking_promotion=ON)
    try:
        ctx.universe_status["MEGA"] = False
        loops._ranking_tier3_lane(ctx, _page(["MEGA", "AAA"]), DAY0)
        assert ctx.tiers.tier_of("MEGA") == 1
        assert ctx.tiers.tier_of("AAA") == 3
    finally:
        ctx.store.close()


def test_only_the_configured_ranking_types_feed_the_lane(tmp_path):
    ctx = _ctx(tmp_path, ("AAA",), ranking_promotion=ON)
    try:
        loops._ranking_tier3_lane(
            ctx, _page(["AAA"], rtype="MARKET_TRADING_VOLUME", duration="realtime"), DAY0)
        assert ctx.tiers.tier_of("AAA") == 1
        loops._ranking_tier3_lane(ctx, _page(["AAA"]), DAY0)
        assert ctx.tiers.tier_of("AAA") == 3
    finally:
        ctx.store.close()


def test_top_n_is_the_cut(tmp_path):
    """**컷이 유일한 제한이라야** 이 단언이 컷을 지킨다.

    좌석 수를 후보 수보다 적게 두면 좌석이 먼저 막아서 컷을 통째로 없애도 같은 답이
    나온다 — 실제로 그렇게 뚫렸다 (docs/61 §10 의 L7). 그래서 좌석을 **넉넉히**(4) 준다.
    """
    ctx = _ctx(tmp_path, ("A1", "A2", "A3", "A4"),
               ranking_promotion={**ON, "top_n": 2, "tier3_slots": 4})
    try:
        ctx.tiers.capacity[3] = 10
        loops._ranking_tier3_lane(ctx, _page(["A1", "A2", "A3", "A4"]), DAY0)
        assert sorted(ctx.tiers.members(3)) == ["A1", "A2"], "컷 밖 종목이 들어왔다"
        assert len(ctx.ranking_seats_ms) == 2, "좌석이 남아 있는데 2 개만 앉았다 = 컷이 잡았다"
    finally:
        ctx.store.close()


# --------------------------------------------------------------------------- #
# C. 좌석 반납 — **놓아 주지 않으면 동결이다**
# --------------------------------------------------------------------------- #
def test_rotate_releases_the_seat_after_hold_s_even_if_still_ranked(tmp_path):
    """`rotate` 는 폭을 산다: 상위권에 남아 있어도 시간이 되면 놓는다.

    실측에서 `sticky` 는 9 정규장에서 커버리지 3~18%, `rotate` 는 20~57% 였다
    (docs/61 §3). 머리가 잘 안 움직이기 때문이다 (`docs/35` §5-5).
    """
    ctx = _ctx(tmp_path, ("AAA", "BBB"), ranking_promotion={**ON, "tier3_slots": 1})
    try:
        loops._ranking_tier3_lane(ctx, _page(["AAA"]), DAY0)
        assert ctx.tiers.members(3) == ["AAA"]
        loops._ranking_tier3_lane(ctx, _page(["AAA"]), DAY0 + 100_000)   # 아직 유지
        assert ctx.tiers.members(3) == ["AAA"]
        loops._ranking_tier3_lane(ctx, _page(["AAA"]), DAY0 + 301_000)   # hold 만료
        assert ctx.tiers.members(3) == [], "놓아 주지 않으면 차선이 동결된다"
        assert ctx.counters["ranking_tier3_releases"] == 1
        assert ctx.tiers.tier_of("AAA") == 2, "한 칸만 내린다"
    finally:
        ctx.store.close()


def test_sticky_keeps_the_seat_while_the_symbol_stays_in_the_top_n(tmp_path):
    ctx = _ctx(tmp_path, ("AAA", "BBB"),
               ranking_promotion={**ON, "tier3_slots": 1, "policy": "sticky"})
    try:
        loops._ranking_tier3_lane(ctx, _page(["AAA"]), DAY0)
        loops._ranking_tier3_lane(ctx, _page(["AAA"]), DAY0 + 200_000)
        loops._ranking_tier3_lane(ctx, _page(["AAA"]), DAY0 + 400_000)
        assert ctx.tiers.members(3) == ["AAA"], "상위 N 에 있는데 놓았다"
        loops._ranking_tier3_lane(ctx, _page(["BBB"]), DAY0 + 800_000)   # AAA 이탈
        assert ctx.tiers.members(3) == ["BBB"]
    finally:
        ctx.store.close()


def test_a_released_symbol_cannot_sit_down_again_inside_the_cooldown(tmp_path):
    """진동 차단. 같은 종목이 좌석을 주고받으면 승격마다 백필이 따라붙는다."""
    ctx = _ctx(tmp_path, ("AAA",), ranking_promotion={**ON, "tier3_slots": 1})
    try:
        loops._ranking_tier3_lane(ctx, _page(["AAA"]), DAY0)
        loops._ranking_tier3_lane(ctx, _page(["AAA"]), DAY0 + 301_000)     # 반납
        assert ctx.tiers.members(3) == []
        loops._ranking_tier3_lane(ctx, _page(["AAA"]), DAY0 + 400_000)     # 쿨다운 안
        assert ctx.tiers.members(3) == []
        assert ctx.counters["ranking_tier3_cooldown"] >= 1
        loops._ranking_tier3_lane(ctx, _page(["AAA"]), DAY0 + 1_000_000)   # 쿨다운 밖
        assert ctx.tiers.members(3) == ["AAA"]
    finally:
        ctx.store.close()


# --------------------------------------------------------------------------- #
# D. 2026-08-04 재발 방지 — **진동 0 그리고 신규 승격 > 0**
# --------------------------------------------------------------------------- #
def test_the_lane_keeps_promoting_instead_of_freezing(tmp_path):
    """*"전이 0 을 안정화로 읽지 마라"* (`STRATEGY-VERDICTS` §4.4-E).

    좌석이 1 개인데 후보가 계속 오는 상황을 20 스냅 흘린다. 건강한 차선은 좌석을
    **계속 돌린다** — 한 종목이 영구히 앉아 있으면 그건 안정이 아니라 동결이다.
    """
    syms = [f"S{i:02d}" for i in range(20)]
    ctx = _ctx(tmp_path, tuple(syms),
               ranking_promotion={**ON, "tier3_slots": 1, "hold_s": 60,
                                  "cooldown_s": 60})
    try:
        for i, s in enumerate(syms):
            loops._ranking_tier3_lane(ctx, _page([s]), DAY0 + i * 120_000)
        assert ctx.counters["ranking_tier3_promotions"] == 20, "차선이 얼었다"
        assert ctx.counters["ranking_tier3_releases"] == 19
        assert len(ctx.tiers.members(3)) == 1, "좌석 수를 넘겼다"
    finally:
        ctx.store.close()


def test_the_lane_can_never_hold_more_than_its_slots(tmp_path):
    """예산 산식이 이 상한 위에 서 있다 — 좌석 하나가 0.583 req/s 다 (docs/61 §4)."""
    syms = [f"S{i:02d}" for i in range(30)]
    ctx = _ctx(tmp_path, tuple(syms),
               ranking_promotion={**ON, "top_n": 30, "tier3_slots": 2, "hold_s": 10_000})
    try:
        ctx.tiers.capacity[3] = 50
        for i in range(5):
            loops._ranking_tier3_lane(ctx, _page(syms), DAY0 + i * 1000)
            assert len(ctx.ranking_seats_ms) <= 2
            assert len(ctx.tiers.members(3)) <= 2
    finally:
        ctx.store.close()


def test_a_seat_that_the_budget_guard_already_took_is_not_charged_twice(tmp_path):
    """가드가 먼저 좌석을 깎아 갔으면 만료 때 **또** 내리지 않는다.

    차선 멤버는 `compete=False` 라 `st.score` 가 0.0 이고, `set_capacity` 는 최약체부터
    내리므로 예산 축소가 오면 **차선이 제일 먼저** tier2 로 내려간다. 그 상태에서 좌석
    만료가 한 칸 더 내리면 좌석이 산 적 없는 tier2 자리까지 뺏는다.
    """
    ctx = _ctx(tmp_path, ("AAA",), ranking_promotion={**ON, "tier3_slots": 1})
    try:
        loops._ranking_tier3_lane(ctx, _page(["AAA"]), DAY0)
        assert ctx.tiers.tier_of("AAA") == 3
        ctx.tiers.set_capacity(tier3_max=1, ts_ms=DAY0 + 1000)   # 가드가 깎는다
        ctx.tiers.capacity[3] = 0 or ctx.tiers.capacity[3]
        ctx.tiers.release("AAA", DAY0 + 2000, "budget_shrink")   # -> tier2
        assert ctx.tiers.tier_of("AAA") == 2
        loops._ranking_tier3_lane(ctx, _page(["ZZZ"]), DAY0 + 301_000)
        assert ctx.tiers.tier_of("AAA") == 2, "좌석이 tier2 자리까지 가져갔다"
        assert ctx.counters.get("ranking_tier3_releases", 0) == 0
        assert "AAA" not in ctx.ranking_seats_ms, "좌석은 놓아야 한다"
    finally:
        ctx.store.close()


def test_seats_survive_a_restart_so_no_orphan_holds_a_tier3_slot(tmp_path):
    """좌석을 저장 안 하면 **재시작마다 고아 좌석**이 남는다.

    티어는 `seed` 로 복원되는데 좌석 원장이 비어 있으면 아무도 그 tier3 멤버를 놓아
    주지 않는다. 좌석 하나가 0.583 req/s 라(docs/61 §4) 그대로 예산이 샌다.
    """
    ctx = _ctx(tmp_path, ("AAA", "BBB"), ranking_promotion={**ON, "tier3_slots": 1})
    try:
        loops._ranking_tier3_lane(ctx, _page(["AAA"]), DAY0)
        assert ctx.tiers.members(3) == ["AAA"]
        assert ctx.save_state(force=True) is True
    finally:
        ctx.store.close()

    ctx2 = _ctx(tmp_path, (), ranking_promotion={**ON, "tier3_slots": 1})
    try:
        assert ctx2.tiers.tier_of("AAA") == 3, "전제 위반: 티어가 복원되지 않았다"
        assert set(ctx2.ranking_seats_ms) == {"AAA"}, "좌석이 안 살아났다 (고아 발생)"
        loops._ranking_tier3_lane(ctx2, _page(["BBB"]), DAY0 + 301_000)
        assert ctx2.tiers.tier_of("AAA") == 2, "고아가 tier3 를 계속 쥐고 있다"
    finally:
        ctx2.store.close()


def test_a_stale_state_file_cannot_resurrect_seats_while_the_flag_is_off(tmp_path):
    """꺼진 채로 기동했는데 옛 좌석이 살아나면 아무도 안 놓아 준다."""
    ctx = _ctx(tmp_path, ("AAA",), ranking_promotion={**ON, "tier3_slots": 1})
    try:
        loops._ranking_tier3_lane(ctx, _page(["AAA"]), DAY0)
        assert ctx.save_state(force=True) is True
    finally:
        ctx.store.close()

    off = _ctx(tmp_path, ())
    try:
        assert off.ranking_seats_ms == {}
        assert off.telemetry()["rk_t3_seats"] == 0
    finally:
        off.store.close()


# --------------------------------------------------------------------------- #
# E. 설정 파싱 — 반쯤 켜진 상태가 **조용히 꺼진 것처럼** 보이면 안 된다
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("section", [
    {"types": ["TOP_GAINERS"]},
    {"types": ["TOP_GAINERS"], "top_n": 3},
    {"types": ["TOP_GAINERS"], "top_n": 3, "tier3_slots": 1},
    {"top_n": 3, "tier3_slots": 1, "hold_s": 300},
])
def test_a_half_configured_section_is_a_startup_error(section):
    with pytest.raises(ValueError, match="half-configured"):
        make_config(ranking_promotion=section)


@pytest.mark.parametrize("section,match", [
    ({"types": "TOP_GAINERS", "top_n": 3, "tier3_slots": 1, "hold_s": 300}, "must be a list"),
    ({"types": ["TOP_GAINERS"], "top_n": -1, "tier3_slots": 1, "hold_s": 300}, ">= 0"),
    ({"types": ["TOP_GAINERS"], "top_n": 3, "tier3_slots": 1, "hold_s": 300,
      "policy": "greedy"}, "policy must be one of"),
    ({"types": ["TOP_GAINERS"], "top_n": 3, "tier3_slots": 1, "hold_s": 300,
      "slots": 2}, "unknown keys"),
])
def test_bad_values_fail_at_startup_not_silently(section, match):
    with pytest.raises(ValueError, match=match):
        make_config(ranking_promotion=section)


def test_the_signature_of_a_disabled_config_is_empty():
    assert RankingPromotionConfig().signature() == ""
    assert RankingPromotionConfig(types=("TOP_GAINERS",), top_n=3,
                                  tier3_slots=1, hold_s=300).signature() != ""


def test_an_absent_section_parses_to_the_off_default():
    data = {"api": {"base_url": "http://x", "live": False, "keys_path": "k",
                    "token_state_path": "t", "timeout_s": 1.0, "usage_ratio": 0.5}}
    assert parse_config(data, env={}).ranking_promotion.enabled is False

"""계약 C-7 개정 A2 (`docs/04`) 를 **실시간 검출기 쪽에서** 고정한다. 소유: W4.

`tests/test_cutoff_amendment_a2.py`(W3)는 분석 레이어 함수(`features.cut_frame` /
`extract_precursor_features`)를 잠갔다. **이 파일은 그 위층** — `EventDetector.evaluate()`
가 실제로 무엇을 보고 판정하는지를 잠근다. 계약 문언은 `tossmon/analysis` 를 대상으로
쓰여 있지만, **룩어헤드가 실제 매매 판단으로 새는 경로는 여기다.**

고정하는 것은 둘이고 서로 다르다:

    A2 §1 캔들(구간·종료 라벨) : `ts_ms   <= t0_ms`
        검출기는 `include_t0=True` 를 넘긴다. A1 은 이 값의 근거를 *"실시간 검출기는 T0 봉
        종료 시점에 판정하므로"* 라고 적었지만 **그 근거는 시작 라벨 전제라 틀렸다** —
        종료 라벨이면 T0 봉은 누구에게나 t0 에 완결이다. 플래그는 이제 **모드 표기**다.

    A2 §2 랭킹(순간·도착 지연)  : `snap_ms <  t0_ms`  ← **엄격 유지**
        옛 코드는 `include_t0` 하나로 봉과 스냅을 **같이** 밀었고 그것이 A2 §2 위반이었다.
        지금은 갈라져 있다. **이 파일이 그 분리의 자물쇠다.**

**A2 는 자기유리 개정이다**(이벤트당 캔들 1봉 증가) — `docs/04` C-7 개정 A2 "방향 고지".

`tests/synth` 를 쓰지 않는다. 모든 시각은 손으로 적은 절대 epoch ms 다 — 합성기와 소비자가
같은 라벨 규약을 공유하면 규약 결함을 그대로 통과시킨다(`docs/36` §4, `docs/47` §6).
"""
from __future__ import annotations

import asyncio
import math

import pandas as pd

from tests.test_collector_helpers import MIN_MS, FrozenClock, build_client, make_config, mock_server
from tossmon.analysis.features import extract_precursor_features
from tossmon.analysis.labeling import EventParams
from tossmon.collector import loops
from tossmon.collector.detector import (RANKING_MARKET_TYPE, RANKING_TOSS_TYPE,
                                        EventDetector, score_paths)
from tossmon.collector.loops import CollectorContext
from tossmon.collector.notifier import Notifier
from tossmon.store.writer import Store

# --------------------------------------------------------------------------- #
# 절대 시각 — 손으로 적었다. 2025-11-03(월) 09:31:00 ET = 14:31:00 UTC.
# 정규장 두 번째 1분봉의 **종료** 라벨이다.
# --------------------------------------------------------------------------- #
T0_MS = 1_762_180_260_000          # 2025-11-03T14:31:00Z
SYM = "AAA"

BAR_T0_M3 = T0_MS - 3 * MIN_MS     # 14:28:00Z
BAR_T0_M2 = T0_MS - 2 * MIN_MS     # 14:29:00Z
BAR_T0_M1 = T0_MS - 1 * MIN_MS     # 14:30:00Z
BAR_T0 = T0_MS                     # 14:31:00Z

SNAP_M70S = T0_MS - 70_000         # T0−70초 — 손에 있다
SNAP_M10S = T0_MS - 10_000         # T0−10초 — 손에 있다
SNAP_AT_T0 = T0_MS                 # T0 정각 — **손에 없다** (도착 시 중앙 16.1초 늙음)
SNAP_P10S = T0_MS + 10_000         # T0+10초 — 미래
SNAP_P30S = T0_MS + 30_000         # T0+30초 — 미래

#: 랭킹에서 파생되는 피처 전부. 하나라도 미래 스냅에 반응하면 그것이 룩어헤드다.
RANKING_FEATURES = ("toss_share", "toss_share_max", "toss_share_slope_30",
                    "toss_in_ranking", "toss_rank_best", "market_rank_best",
                    "minutes_since_toss_entry", "ranking_snaps_pre")


def _bars() -> pd.DataFrame:
    ts = [BAR_T0_M3, BAR_T0_M2, BAR_T0_M1, BAR_T0]
    return pd.DataFrame({
        "symbol": [SYM] * 4,
        "ts_ms": ts,
        "open_u": [1_000_000] * 4,
        "high_u": [1_010_000] * 4,
        "low_u": [990_000] * 4,
        "close_u": [1_000_000, 1_001_000, 1_002_000, 1_003_000],
        "vol_qu": [100_000, 100_000, 100_000, 900_000],
    })


def _rankings(rows) -> pd.DataFrame:
    """(snap_ms, rank, 내 거래대금) → 검출기가 실제로 쓰는 랭킹 2종 프레임.

    타입은 `RANKING_*_TYPE`(거래량 랭킹)이다 — 분석 레이어 기본값(거래대금)과 다르므로
    기본값으로 만들면 쏠림도 피처가 통째로 NaN 이 되어 **테스트가 조용히 죽는다.**
    """
    out = []
    for snap, rank, mine_u in rows:
        out.append({"symbol": SYM, "snap_ms": snap, "ranking_type": RANKING_TOSS_TYPE,
                    "rank": rank, "amount_u": mine_u, "duration": "DAY",
                    "last_u": 1_000_000, "vol_qu": 1})
        out.append({"symbol": SYM, "snap_ms": snap, "ranking_type": RANKING_MARKET_TYPE,
                    "rank": rank, "amount_u": 100, "duration": "DAY",
                    "last_u": 1_000_000, "vol_qu": 1})
    return pd.DataFrame(out)


#: t0 전에는 조용하고(90위, 쏠림 1%), **t0 정각부터 폭발**한다(1위, 쏠림 80~99%).
#: 컷이 새면 즉시 드러나도록 만든 배치다.
QUIET_BEFORE = [(SNAP_M70S, 90, 1), (SNAP_M10S, 90, 1)]
LOUD_FROM_T0 = [(SNAP_AT_T0, 1, 80), (SNAP_P10S, 1, 95), (SNAP_P30S, 1, 99)]


def _evaluate(rows):
    det = EventDetector(EventParams(), max_per_day=1)
    return det.evaluate(SYM, _bars(), rankings=_rankings(rows))


def _same(a, b) -> bool:
    if a is None or b is None:
        return a is b
    try:
        return (math.isnan(a) and math.isnan(b)) or a == b
    except TypeError:
        return a == b


# --------------------------------------------------------------------------- #
# A2 §1 — 검출기의 캔들 컷은 t0 봉에서 **끝난다** (그 다음이 아니라)
# --------------------------------------------------------------------------- #
def test_detector_cutoff_lands_exactly_on_the_t0_bar() -> None:
    """종료 라벨이므로 T0 봉은 t0 에 완결이다 — 컷오프 지연이 0 이어야 한다."""
    r = _evaluate(QUIET_BEFORE)
    assert int(r.feats["cutoff_ms"]) == T0_MS
    assert r.feats["cutoff_lag_min"] == 0.0
    assert r.feats["n_bars_pre"] == 4.0          # T0 봉을 포함해 4봉 전부
    assert r.feats["include_t0"] == 1.0          # 모드 표기 = 관측가능 컷오프


# --------------------------------------------------------------------------- #
# A2 §2 — 랭킹은 **캔들 플래그를 따라가지 않는다**
# --------------------------------------------------------------------------- #
def test_detector_ignores_the_snapshot_stamped_exactly_at_t0() -> None:
    """`snap_ms >= t0` 스냅을 아무리 요란하게 만들어도 검출기가 움직이면 안 된다.

    이것이 A2 §2 위반의 유일한 실측 증거다 — 통과하면 라이브 경로 누출이 **0** 이다.
    """
    clean = _evaluate(QUIET_BEFORE)
    poisoned = _evaluate(QUIET_BEFORE + LOUD_FROM_T0)

    for k in RANKING_FEATURES:
        assert _same(clean.feats.get(k), poisoned.feats.get(k)), \
            f"{k}: {clean.feats.get(k)} → {poisoned.feats.get(k)} — t0 이후 스냅이 샜다"
    moved = [k for k in clean.feats if not _same(clean.feats.get(k), poisoned.feats.get(k))]
    assert moved == [], f"랭킹 오염이 피처로 샜다: {moved}"
    assert clean.score == poisoned.score
    assert clean.path == poisoned.path
    assert len(clean.events) == len(poisoned.events)


def test_the_poison_is_actually_poisonous() -> None:
    """엔진 자기시험 — 같은 행을 t0 **이전**으로 옮기면 피처가 실제로 움직여야 한다.

    이게 없으면 위 테스트는 "아무 일도 안 일어나는 것을 확인" 하는 죽은 테스트가 된다
    (`test_lookahead_contract.py` 의 심은 위반 자기시험과 같은 이유).
    """
    clean = _evaluate(QUIET_BEFORE)
    shifted = _evaluate(QUIET_BEFORE + [(s - 2 * MIN_MS, r, a) for s, r, a in LOUD_FROM_T0])
    moved = [k for k in RANKING_FEATURES
             if not _same(clean.feats.get(k), shifted.feats.get(k))]
    assert len(moved) >= 5, f"프로브가 죽었다 — 움직인 피처: {moved}"
    assert shifted.feats["toss_rank_best"] == 1.0        # 90위 → 1위
    assert shifted.feats["ranking_snaps_pre"] > clean.feats["ranking_snaps_pre"]


def test_widening_the_ranking_cut_would_cost_a_measurable_amount() -> None:
    """컷을 `<=` 로 밀면 얼마나 움직이는가 — **자물쇠가 비어 있지 않다는 증거**.

    실측(2026-08-09): `ranking_snaps_pre` 4.0 → 6.0 — W3 가 `docs/49` §6 에서 잰 것과
    같은 수다. 채택 스코어는 +0.140 움직인다. tier2 승격선이 0.35 이므로 승격 문턱의
    **40%** 를 공짜로 얻는 셈이고, 그 근거는 t0 에 손에 없던 스냅이다.
    """
    rows = QUIET_BEFORE + [(SNAP_AT_T0, 1, 80)]
    strict = _rankings(rows)
    # `<=` 가 들여보냈을 바로 그 집합 (t0 정각 스냅을 1ms 앞으로 옮긴 것과 동치)
    loose = _rankings([(s - 1 if s >= T0_MS else s, r, a) for s, r, a in rows])

    def feats(rk):
        return extract_precursor_features(
            _bars(), rk, T0_MS, include_t0=True, symbol=SYM,
            toss_type=RANKING_TOSS_TYPE, market_type=RANKING_MARKET_TYPE)

    fs, fl = feats(strict), feats(loose)
    assert fs["ranking_snaps_pre"] == 4.0 and fl["ranking_snaps_pre"] == 6.0
    assert fs["toss_rank_best"] == 90.0 and fl["toss_rank_best"] == 1.0
    delta = score_paths(fl)[0] - score_paths(fs)[0]
    assert delta >= 0.10, f"컷 완화의 스코어 영향 {delta:+.3f} — 실측 +0.140 에서 벗어났다"


# --------------------------------------------------------------------------- #
# 라이브 경로 — 버퍼는 실제로 t0 이후 스냅을 컷 앞에 들이민다
# --------------------------------------------------------------------------- #
def test_the_live_buffer_really_hands_post_t0_snapshots_to_the_cut(tmp_path) -> None:
    """`snap_ms` 는 관측 시각이고 t0 는 직전 완성봉이다 — 둘 사이 폴링분은 전부 `> t0`.

    mock HTTP 전 구간(`rankings_once` → `tier2_symbol_once` → `_detect`)을 실제로 돌려
    검출 시점 버퍼가 무엇을 들고 있었는지 센다. 여기서 0 이 나오면 위 자물쇠들은
    **라이브에서 한 번도 걸리지 않는 장식**이라는 뜻이다.
    """
    seen: list[tuple[int, int]] = []          # (t0 이후 행 수, 전체 행 수)

    async def run():
        with mock_server() as (base_url, _httpd):
            client = build_client(base_url, tmp_path)
            cal = await client.get_us_calendar()
            cfg = make_config(tmp_path)
            store = Store(cfg.store.db_path)
            clock = FrozenClock(cal["today"].regular.start_ms + 30 * MIN_MS)
            ctx = CollectorContext.create(client, store, cfg,
                                          notifier=Notifier(console=False),
                                          clock=clock, symbols=("AAPL", "SNTI"))
            ctx.scheduler.calendar = cal
            ctx.scheduler.fetched_ms = clock.now_ms()
            ctx.session = "regular"
            real_detect = loops._detect

            def spy(c, symbol):
                buf = c.buffers.get(symbol)
                rk = c.rankings.frame(symbol)
                if buf is not None and len(buf) and not rk.empty:
                    bar = int(buf.frame()["ts_ms"].to_numpy()[-1])
                    snaps = [int(s) for s in rk["snap_ms"].tolist()]
                    seen.append((sum(1 for s in snaps if s >= bar), len(snaps)))
                return real_detect(c, symbol)

            loops._detect = spy
            try:
                # 픽스처 마지막 봉을 t0 로 잡고, 그 봉이 닫힌 **직후**로 시계를 옮긴다.
                # (그렇게 하지 않으면 픽스처 봉이 시계보다 4.7시간 낡아 측정이 무의미해진다)
                await loops.tier2_symbol_once(ctx, "AAPL")
                t0 = int(ctx.buffer("AAPL").frame()["ts_ms"].to_numpy()[-1])
                clock.advance((t0 + 1_000 - clock.now_ms()) / 1000)
                ctx.rankings.rows.clear()
                seen.clear()
                for _ in range(3):                      # 한 봉 안쪽, 10초 격자
                    await loops.rankings_once(ctx)
                    for sym in sorted(ctx.tiers.at_least(2)):
                        await loops.tier2_symbol_once(ctx, sym)
                    clock.advance(10)
            finally:
                loops._detect = real_detect
                await client.aclose()
                store.close()

    asyncio.run(run())
    assert seen, "검출이 한 번도 랭킹 버퍼를 보지 않았다 — 측정이 성립하지 않는다"
    after = sum(n for n, _ in seen)
    assert after > 0, "버퍼에 t0 이후 스냅이 하나도 없다 — 자물쇠가 라이브에서 무의미해진다"

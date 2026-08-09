"""계약 C-7 개정 A2 (`docs/04`) — 컷오프가 **관측 가능성** 기준으로 갈라진다. 소유: W3.

**이 파일은 합성기(`tests/synth`)를 쓰지 않는다.** 모든 시각은 손으로 적은 절대 epoch ms 다.
합성기와 소비자가 같은 라벨 규약을 공유하면 규약 결함을 그대로 통과시킨다는 것이
`docs/36` §4 에서 실증됐고, D-10(`docs/47` §6)이 깨진 테스트 17개를 그 방식으로 고쳤다.

고정하는 것은 **둘이고 서로 다르다** — 한 플래그로 밀면 안 된다:

    A2 §1 캔들(구간·종료 라벨) : `ts_ms   <= t0_ms`
        `ts_ms = T` 봉은 `[T−60초, T)` 를 담아 **T 에 이미 완결**이다(사전등록 §6.1).
        따라서 T0 봉은 실시간 검출기에만이 아니라 **누구에게나 t0 에 관측 가능**하다.

    A2 §2 랭킹(순간·도착 지연)  : `snap_ms <  t0_ms`  ← **엄격 유지**
        랭킹은 구간이 아니라 순간이고, W1 실측으로 도착 시 **중앙 16.1초 늙어 있다**
        (`docs/35`). `snap_ms = t0` 인 스냅은 **t0 에 우리 손에 없다.**
        **여기를 넓히면 그것은 진짜 룩어헤드다.**

둘째 것이 이번 수정에서 제일 위험한 지점이다 — 캔들 쪽 플래그가 랭킹으로 새면
과보수 1봉을 되찾는 대신 실제 누출을 연다. 아래 `test_ranking_stays_strict_*` 세 개가
그것을 막는 자물쇠다.
"""
from __future__ import annotations

import sqlite3

import pandas as pd

from tossmon.analysis import features as F
from tossmon.analysis import rotation as ROT

# --------------------------------------------------------------------------- #
# 절대 시각 — 손으로 적었다. 어떤 헬퍼도 이 값을 만들어 주지 않는다.
# 2025-11-03(월) 09:31:00 ET = 14:31:00 UTC. 정규장 두 번째 1분봉의 **종료** 라벨.
# --------------------------------------------------------------------------- #
T0_MS = 1_762_180_260_000          # 2025-11-03T14:31:00Z
MIN_MS = 60_000

BAR_T0_M3 = 1_762_180_080_000      # T0−3분  14:28:00Z
BAR_T0_M2 = 1_762_180_140_000      # T0−2분  14:29:00Z
BAR_T0_M1 = 1_762_180_200_000      # T0−1분  14:30:00Z
BAR_T0 = 1_762_180_260_000      # T0      14:31:00Z  (= T0_MS)
BAR_T0_P1 = 1_762_180_320_000      # T0+1분  14:32:00Z

SNAP_M70S = 1_762_180_190_000      # T0−70초 14:29:50Z
SNAP_M10S = 1_762_180_250_000      # T0−10초 14:30:50Z
SNAP_AT_T0 = 1_762_180_260_000      # T0 정각 14:31:00Z ← 손에 없다 (docs/35)
SNAP_P10S = 1_762_180_270_000      # T0+10초 14:31:10Z

SYM = "AAA"


def _bars() -> pd.DataFrame:
    ts = [BAR_T0_M3, BAR_T0_M2, BAR_T0_M1, BAR_T0, BAR_T0_P1]
    return pd.DataFrame({
        "symbol": [SYM] * 5,
        "ts_ms": ts,
        "open_u": [1_000_000] * 5,
        "high_u": [1_010_000] * 5,
        "low_u": [990_000] * 5,
        "close_u": [1_000_000, 1_001_000, 1_002_000, 1_003_000, 1_004_000],
        "vol_qu": [100_000, 100_000, 100_000, 900_000, 900_000],
    })


def _rankings() -> pd.DataFrame:
    """T0 정각 스냅만 순위를 1위로 둔다 — 새어 들어오면 `toss_rank_best` 가 즉시 드러난다."""
    snaps = [SNAP_M70S, SNAP_M10S, SNAP_AT_T0, SNAP_P10S]
    ranks = [9, 9, 1, 1]
    rows = []
    for s, r in zip(snaps, ranks):
        rows.append({"symbol": SYM, "snap_ms": s, "ranking_type": F.TOSS_RANK_TYPE,
                     "rank": r, "amount_u": 5_000_000})
        rows.append({"symbol": SYM, "snap_ms": s, "ranking_type": F.MARKET_RANK_TYPE,
                     "rank": r, "amount_u": 10_000_000})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# A2 §1 — 캔들은 `<=` 이고, 그것이 **기본값**이다
# --------------------------------------------------------------------------- #
def test_candle_cutoff_admits_the_bar_labelled_t0_by_default() -> None:
    """A2 §1: 기본 컷오프가 관측가능(`obs_le`) 이다. 라벨 T0 봉이 들어와야 한다."""
    got = list(F.cut_frame(_bars(), T0_MS)["ts_ms"])
    assert got == [BAR_T0_M3, BAR_T0_M2, BAR_T0_M1, BAR_T0], got


def test_candle_cutoff_still_excludes_everything_after_t0() -> None:
    """넓힌 것은 정확히 한 봉이다. T0+1분은 어느 모드에서도 못 들어온다."""
    assert BAR_T0_P1 not in list(F.cut_frame(_bars(), T0_MS)["ts_ms"])
    assert BAR_T0_P1 not in list(F.cut_frame(_bars(), T0_MS, include_t0=True)["ts_ms"])
    assert BAR_T0_P1 not in list(F.cut_frame(_bars(), T0_MS, include_t0=False)["ts_ms"])


def test_strict_mode_survives_for_cutoff_mode_tagging() -> None:
    """`strict_lt` 는 사라지지 않는다 — 사전등록 §1 P1 `cutoff_mode` 표기 규약이 요구한다."""
    got = list(F.cut_frame(_bars(), T0_MS, include_t0=False)["ts_ms"])
    assert got == [BAR_T0_M3, BAR_T0_M2, BAR_T0_M1], got


def test_extract_reports_cutoff_at_t0_with_zero_lag() -> None:
    """`cutoff_lag_min ≥ 1` 이라는 구조적 60초 사각이 사라진다 (docs/12 §1 P1 개정 상자)."""
    f = F.extract_precursor_features(_bars(), _rankings(), T0_MS, symbol=SYM)
    assert f["cutoff_ms"] == float(BAR_T0)
    assert f["cutoff_lag_min"] == 0.0
    assert f["include_t0"] == 1.0          # obs_le


# --------------------------------------------------------------------------- #
# A2 §2 — 랭킹은 엄격하다. **캔들 플래그와 무관하게.** (이번 수정의 자물쇠)
# --------------------------------------------------------------------------- #
def test_ranking_stays_strict_under_the_observable_candle_default() -> None:
    """기본(캔들 `<=`)에서도 `snap_ms = t0` 스냅은 들어오면 안 된다."""
    f = F.extract_precursor_features(_bars(), _rankings(), T0_MS, symbol=SYM)
    # 손에 있는 스냅은 T0−70초·T0−10초 둘뿐 (type 2종 × 2스냅 = 4행)
    assert f["ranking_snaps_pre"] == 4.0
    assert f["toss_rank_best"] == 9.0, "T0 정각 스냅(rank 1)이 새어 들어왔다 — 진짜 룩어헤드"
    assert f["market_rank_best"] == 9.0


def test_ranking_stays_strict_even_when_the_candle_flag_says_include_t0() -> None:
    """**한 플래그로 밀면 여기서 터진다.** W4 실시간 경로(`include_t0=True`)도 마찬가지다."""
    f = F.extract_precursor_features(_bars(), _rankings(), T0_MS, symbol=SYM,
                                     include_t0=True)
    assert f["ranking_snaps_pre"] == 4.0, "캔들 플래그가 랭킹 컷오프로 샜다 (A2 §2 위반)"
    assert f["toss_rank_best"] == 9.0
    assert f["market_rank_best"] == 9.0


def test_ranking_cutoff_is_identical_across_both_candle_modes() -> None:
    """랭킹 파생 피처는 캔들 모드에 **불변**이어야 한다 — 시각 기준점(`minutes_since_*`) 제외."""
    a = F.extract_precursor_features(_bars(), _rankings(), T0_MS, symbol=SYM,
                                     include_t0=False)
    b = F.extract_precursor_features(_bars(), _rankings(), T0_MS, symbol=SYM,
                                     include_t0=True)
    for k in ("ranking_snaps_pre", "toss_in_ranking", "toss_rank_best",
              "market_rank_best", "toss_share", "toss_share_max"):
        x, y = a[k], b[k]
        assert (x == y) or (x != x and y != y), f"{k}: {x} != {y} — 랭킹이 모드에 반응했다"


def test_poisoning_the_t0_snapshot_moves_nothing() -> None:
    """`snap_ms >= t0` 를 변조해도 쏠림도 피처가 흔들리지 않아야 한다 (**A2 §3** 의무).

    옛 A1 §1 3항이 아니라 A2 §3 이다 — A1 §1 은 2026-08-09 에 A2 로 대체됐고,
    "미래 데이터를 넣어도 결과가 동일함을 증명하는 테스트" 의무는 A2 §3 이 이어받았다.
    """
    clean = _rankings()
    dirty = clean.copy()
    fut = dirty["snap_ms"] >= T0_MS
    dirty.loc[fut, "amount_u"] = dirty.loc[fut, "amount_u"] * 999
    dirty.loc[fut, "rank"] = 1
    a = F.extract_precursor_features(_bars(), clean, T0_MS, symbol=SYM, include_t0=True)
    b = F.extract_precursor_features(_bars(), dirty, T0_MS, symbol=SYM, include_t0=True)
    for k in ("toss_share", "toss_share_max", "toss_share_slope_30", "toss_rank_best",
              "market_rank_best", "minutes_since_toss_entry", "ranking_snaps_pre"):
        x, y = a[k], b[k]
        assert (x == y) or (x != x and y != y), f"{k} 가 t0 이후 랭킹에 반응했다"


# --------------------------------------------------------------------------- #
# A2 §1 — `rotation.print_frame` 의 `t_to` 도 캔들이다
# --------------------------------------------------------------------------- #
def test_print_frame_t_to_is_inclusive_because_ts_is_an_end_label() -> None:
    p = ROT.print_frame(_bars(), t_to=T0_MS)
    assert list(p["ts_ms"]) == [BAR_T0_M3, BAR_T0_M2, BAR_T0_M1, BAR_T0]


def test_print_frame_t_to_still_excludes_the_next_bar() -> None:
    p = ROT.print_frame(_bars(), t_to=T0_MS)
    assert BAR_T0_P1 not in list(p["ts_ms"])


# --------------------------------------------------------------------------- #
# A2 §1 — `rule_search` 의 pre/post 경계 (캔들 양쪽)
# --------------------------------------------------------------------------- #
def _mini_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE candles_1m (symbol TEXT, ts_ms INTEGER, open_u INTEGER,"
                 " high_u INTEGER, low_u INTEGER, close_u INTEGER, vol_qu INTEGER)")
    df = _bars()
    conn.executemany(
        "INSERT INTO candles_1m VALUES (?,?,?,?,?,?,?)",
        [(SYM, int(r.ts_ms), int(r.open_u), int(r.high_u), int(r.low_u),
          int(r.close_u), int(r.vol_qu)) for r in df.itertuples()])
    conn.commit()
    return conn


def test_rule_search_pre_ends_at_t0_and_post_starts_after_it() -> None:
    """pre 는 T0 봉을 담고(관측 가능), post 는 T0 봉을 담지 않는다(그 내용은 t0 **이전**)."""
    from tossmon.analysis.measure import rule_search as RS
    conn = _mini_db()
    try:
        pre, post = RS.event_frames(conn, SYM, T0_MS)
    finally:
        conn.close()
    assert list(pre["ts_ms"])[-1] == BAR_T0, "T0 봉이 pre 에 없다 (A2 §1 미적용)"
    assert BAR_T0 not in list(post["ts_ms"]), \
        "post 의 첫 봉이 T0 라벨이다 — 그 봉의 내용은 [T0−60초, T0) 이라 진입 전이다"
    assert list(post["ts_ms"]) == [BAR_T0_P1]


def test_rule_search_frames_do_not_overlap() -> None:
    """같은 봉이 신호와 성과 양쪽에 들어가면 안 된다."""
    from tossmon.analysis.measure import rule_search as RS
    conn = _mini_db()
    try:
        pre, post = RS.event_frames(conn, SYM, T0_MS)
    finally:
        conn.close()
    assert not (set(pre["ts_ms"]) & set(post["ts_ms"]))

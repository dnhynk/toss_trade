"""전방 깊이 격차의 **기전** 러너 (`docs/66`). 이 파일이 지키는 것 아홉.

1. **짝 복원이 원장과 어긋나면 죽는다.** 러너의 추첨 원장(slot/paired)과 원값 행 수가
   1:1 이어야 하고, 어긋나면 조용히 계속하는 대신 예외여야 한다(§4.4-D 의 사고).
2. **부호 규약이 코드에 박힌 대로다.** d_fwd(실제-위약)와 observed_gap(위약-실제)은
   **일부러 반대 방향**이다 - 각각 잇는 표(docs/65 [11])의 부호를 따른다.
3. **순열 진단이 실제로 판별한다.** 심은 효과에는 작은 p, 교환 가능한 잡음에는 큰 p.
4. **군집 5 미만이면 CI 를 코드가 막는다** (`STRATEGY-VERDICTS` §4.4-B).
5. **보정 분모를 러너가 센다** (§4.4-F). 사람이 적은 수가 아니어야 한다.
6. **확증 팔을 열 수 없다.** 이 모듈에는 여는 스위치 자체가 없고, 심은 08-13 이후
   세션이 산출물에 닿지 않아야 한다. 데이터가 없는 것과 코드가 막는 것을 가른다.
7. **진단에는 CI 가 없다** - [M1] 과 절단 없는 부분집합은 검정이 아니다.
8. **라벨 다섯이 붙고 판정 문구가 없다.**
9. **콘솔이 ASCII 다** (cp949).
"""
from __future__ import annotations

import json
import sqlite3

import numpy as np
import pytest

from tossmon.analysis.measure import forward_depth_mechanism as FDM
from tossmon.analysis.measure import ranking_forward_path as RFP
from tests.test_ranking_forward_path import (
    DAYS,
    _make_db,
    _rank_rows,
    _trade_rows,
)


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    return _make_db(tmp_path_factory.mktemp("fdm") / "t.db")


@pytest.fixture(scope="module")
def res(db):
    """러너의 원결과를 한 번만 만들어 공유한다 (짝 원장 검산용)."""
    return RFP.run(db)


@pytest.fixture(scope="module")
def report(db, tmp_path_factory):
    out = tmp_path_factory.mktemp("fdm_out")
    assert FDM.main(["prog", str(db), "--out", str(out), "--name", "fdm"]) == 0
    return json.loads((out / "fdm.json").read_text(encoding="utf-8")), out


# --------------------------------------------------------------------------- #
# 1. 짝 복원
# --------------------------------------------------------------------------- #
def test_pair_table_matches_the_ledger_counts(res):
    """짝 수 = 원장의 n_paired 합. 복원이 표본을 만들거나 잃으면 안 된다."""
    pairs = FDM.pair_table(res)
    assert pairs, "no pairs came out of the synthetic db"
    for (rtype, kind, cell, arm), p in pairs.items():
        box = res["cells"][f"{rtype}|{kind}|{cell}|all"]
        ledger = sum(int(d["n_paired"]) for d in box["arms"][arm]["pairing"])
        got = sum(int(v["d_fwd"].size) for v in p["by_session"].values())
        assert got == ledger, (rtype, cell, arm)


def test_pair_table_dies_on_a_corrupt_ledger(res):
    """행 수가 원장과 어긋나면 **죽어야 한다** - 조용히 계속하면 짝이 뒤섞인다."""
    import copy
    bad = copy.deepcopy(res)
    for box in bad["cells"].values():
        for arm in FDM.MECH_ARMS:
            a = box["arms"].get(arm)
            if a and a["pairing"] and a["pairing"][0]["n_paired"] > 0:
                a["pairing"][0]["paired"] = np.append(
                    np.asarray(a["pairing"][0]["paired"]), True)
                with pytest.raises(ValueError):
                    FDM.pair_table(bad)
                return
    pytest.skip("synthetic db produced no paired arm to corrupt")


def test_primary_arm_pre_residual_is_band_bound(report):
    """일차 팔의 짝 내 `nbar300` 잔차는 +-20% 밴드 안이어야 한다.

    합성 테이프는 1 초 격자라 앵커의 `nbar300` 이 301 로 알려져 있다 - 잔차
    중앙값이 그 20% 를 넘으면 밴드가 물리지 않은 것이다.
    """
    rep, _ = report
    rows = [r for r in rep["paired_depth_response"] if r["primary_arm"]]
    assert rows, "primary arm produced no records"
    for r in rows:
        assert abs(r["d_nbar300_p50"]) <= 0.2 * 301 + 1e-9, r["cell"]


# --------------------------------------------------------------------------- #
# 2·3. 부호 규약과 순열의 판별력 - 합성 짝으로 직접
# --------------------------------------------------------------------------- #
def _synthetic_pairs(*, lift: float, n_pairs: int = 40, seed: int = 7) -> dict:
    """세션 하나, 짝 n_pairs 개. 위약 3 개는 공통 잡음, 실제 = 잡음 + lift."""
    rng = np.random.default_rng(seed)
    base = rng.normal(100.0, 10.0, size=(n_pairs, 4))
    real = base[:, 0] + lift
    plac = [base[i, 1:] for i in range(n_pairs)]
    sess = {"d_fwd": real - base[:, 1:].mean(axis=1),
            "d_nbar300": np.zeros(n_pairs), "d_nbar60": np.zeros(n_pairs),
            "d_t_to_close": np.zeros(n_pairs),
            "untruncated": np.ones(n_pairs, dtype=bool),
            "real_fwd": real, "plac_fwd": plac,
            "symbol": np.asarray(["SYM"] * n_pairs, dtype=object)}
    return {("R", "E1_new_entry", "N50", FDM.PRIMARY_ARM):
            {"by_session": {"2026-08-10": sess}}}


def test_signs_oppose_by_construction():
    """실제가 깊으면 d_fwd 는 양(+), observed_gap(docs/65 부호)은 음(-)이어야 한다."""
    pairs = _synthetic_pairs(lift=50.0)
    m2 = FDM.paired_depth_response(pairs)
    m3 = FDM.gap_permutation(pairs, n_perm=200)
    assert m2[0]["d_fwd_bars"]["mean"] > 0
    assert m3[0]["observed_gap"] < 0


def test_permutation_flags_a_planted_effect():
    p = FDM.gap_permutation(_synthetic_pairs(lift=50.0), n_perm=2_000)[0]
    assert p["p_two_sided"] < 0.01


def test_permutation_passes_exchangeable_noise():
    p = FDM.gap_permutation(_synthetic_pairs(lift=0.0), n_perm=2_000)[0]
    assert p["p_two_sided"] > 0.05


def test_permutation_p_is_never_zero():
    """add-one 꼴 - 0 이 나오면 '불가능' 을 주장하는 p 다."""
    p = FDM.gap_permutation(_synthetic_pairs(lift=1e6), n_perm=500)[0]
    assert p["p_two_sided"] >= 1.0 / 501


# --------------------------------------------------------------------------- #
# 4·5. CI 규율과 보정 분모
# --------------------------------------------------------------------------- #
def test_ci_is_withheld_below_five_clusters():
    by = {f"2026-08-{d:02d}": np.asarray([1.0, 2.0]) for d in (3, 4, 5, 6)}
    out = FDM.cluster_bootstrap_mean(by)
    assert out["ci95"] is None and "clusters 4 < 5" in out["ci_withheld"]


def test_ci_appears_at_five_clusters_and_bonferroni_is_wider():
    rng = np.random.default_rng(3)
    by = {f"2026-08-{d:02d}": rng.normal(1.0, 0.5, 30) for d in range(3, 9)}
    out = FDM.cluster_bootstrap_mean(by, n_comparisons=12)
    assert out["ci95"] is not None and out["ci_withheld"] is None
    lo, hi = out["ci95"]
    blo, bhi = out["ci_bonferroni"]
    assert blo <= lo and hi <= bhi
    assert out["n_comparisons"] == 12


def test_the_runner_counts_its_own_family(report):
    """보정 분모 = 이 실행이 만든 (칸 x 팔) 레코드 수. 사람이 적지 않는다."""
    rep, _ = report
    m2 = rep["paired_depth_response"]
    assert m2, "no paired records"
    for r in m2:
        assert r["diff"]["n_comparisons"] == len(m2)


def test_m2_and_m3_cover_the_same_cells(report):
    rep, _ = report
    k2 = {(r["ranking_type"], r["cell"], r["arm"])
          for r in rep["paired_depth_response"]}
    k3 = {(r["ranking_type"], r["cell"], r["arm"])
          for r in rep["gap_permutation"]}
    assert k2 == k3 and k2


def test_two_sessions_in_the_synthetic_db_mean_no_ci(report):
    """합성 DB 는 거래일 2 개다 - CI 유보 경로가 실제로 지나가는지 본다."""
    rep, _ = report
    for r in rep["paired_depth_response"]:
        assert r["diff"]["ci95"] is None
        assert "no pooled CI" in r["diff"]["ci_withheld"]


# --------------------------------------------------------------------------- #
# 6. 확증 팔 - 코드가 막는다
# --------------------------------------------------------------------------- #
def test_the_module_has_no_switch_that_opens_the_confirmation_arm():
    """인자 파서(`main`)에 `--all-sessions` 에 해당하는 것이 **없어야 한다.**
    (모듈 독스트링은 그 스위치가 없다는 사실을 서술하므로 검사 대상이 아니다.)"""
    import inspect
    src = inspect.getsource(FDM.main)
    assert "--all-sessions" not in src
    assert "exploration_only" not in src


def test_a_confirmation_session_is_planted_but_never_reaches_the_report(tmp_path):
    """08-14 를 통째로 심고 산출물에 안 닿는지 본다. 같은 DB 를 러너의
    `--all-sessions` 로 열면 나타난다 - 안 나타나면 가드가 아니라 데이터 부재다."""
    path = _make_db(tmp_path / "floor.db", with_holdout=False)
    conn = sqlite3.connect(path)
    conn.executemany("INSERT INTO rankings_snap (snap_ms, ranking_type, duration, "
                     "rank, symbol, last_u, vol_qu, amount_u) VALUES (?,?,?,?,?,?,?,?)",
                     _rank_rows("2026-08-14"))
    conn.executemany("INSERT INTO trades_snap VALUES (?,?,?,?)",
                     _trade_rows("2026-08-14"))
    conn.commit()
    conn.close()

    out = tmp_path / "out"
    assert FDM.main(["prog", str(path), "--out", str(out), "--name", "f"]) == 0
    rep = json.loads((out / "f.json").read_text(encoding="utf-8"))
    assert rep["arm"]["sessions_used"] == list(DAYS)
    assert all(s in RFP.EXPLORATION_SESSIONS for s in rep["arm"]["sessions_used"])

    opened = RFP.run(path, exploration_only=False)
    assert "2026-08-14" in [s["session"] for s in opened["sessions"]], (
        "the planted session is missing, so the guard is not what removed it")


def test_build_report_dies_if_a_forbidden_session_slips_through(res):
    """러너의 이중 가드가 뚫려도 **이 모듈이 셋째 벽에서 죽는지** 본다."""
    import copy
    bad = copy.deepcopy(res)
    bad["sessions"].append({"session": "2026-08-14", "day0_ms": 0, "open_ms": 0,
                            "close_ms": 0, "regular_snaps": 1, "era": "d21_after"})
    with pytest.raises(ValueError, match="exploration arm"):
        FDM.build_report(bad)


def test_holdout_rows_never_reach_the_report(report):
    """합성 DB 는 홀드아웃 날(06-15)을 심는다 - 산출물에 안 닿아야 한다."""
    rep, _ = report
    assert "2026-06-15" not in rep["arm"]["sessions_used"]


# --------------------------------------------------------------------------- #
# 7. 진단에는 CI 가 없다
# --------------------------------------------------------------------------- #
def test_diagnostics_carry_no_ci(report):
    rep, _ = report
    for r in rep["depth_predictability"]:
        assert "ci95" not in r and "diagnostic" in r["no_ci_reason"]
    for r in rep["paired_depth_response"]:
        u = r["untruncated"]
        assert "ci95" not in u and "diagnostic" in u["note"]
        assert u["n"] <= r["n_pairs"]


def test_spearman_matches_hand_values():
    assert FDM.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert FDM.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    # 동순위: x=[1,2,2,3] 의 평균 순위 [1,2.5,2.5,4] vs y=[1,2,3,4] -> rho = sqrt(0.9)
    assert FDM.spearman([1, 2, 2, 3], [1, 2, 3, 4]) == pytest.approx(
        0.9486832980505138, abs=1e-9)
    assert FDM.spearman([1.0, np.nan], [1.0, 2.0]) is None


# --------------------------------------------------------------------------- #
# 8·9. 라벨 · 판정 문구 · ASCII
# --------------------------------------------------------------------------- #
def test_the_report_carries_the_five_labels_and_no_verdict(report):
    rep, _ = report
    assert rep["labels"] == list(RFP.LABELS)
    blob = json.dumps(rep).lower()
    for phrase in RFP.FORBIDDEN_PHRASES:
        assert phrase not in blob, phrase


def test_decision_rules_are_in_the_output_before_any_reader(report):
    """판정식이 산출물에 그대로 박혀 있어야 한다 - 사후 서술이 아니라."""
    rep, _ = report
    rules = rep["design"]["decision_rules"]
    assert "written before the run" in rules
    assert "post-treatment" in rules


def test_console_output_is_pure_ascii(db, tmp_path, capsys):
    assert FDM.main(["prog", str(db), "--out", str(tmp_path), "--name", "a"]) == 0
    text = capsys.readouterr().out
    assert text and all(ord(c) < 128 for c in text), (
        [c for c in text if ord(c) >= 128][:5])


def test_runner_writes_outside_the_source_tree(report):
    import pathlib
    _rep, out = report
    src_dir = pathlib.Path(FDM.__file__).resolve().parent
    assert RFP.OUT_DIR.resolve() != src_dir
    assert (out / "fdm.json").exists()
